"""Order reservation lifecycle and operational commands.

Remote charges never release stock on local timeout alone: overdue charges can
still be paid. Deletion/reconciliation with the gateway is required first.
"""
from datetime import timedelta
import click
from flask import current_app, g, jsonify, request
from sqlalchemy import text
from .auth import require_roles
from .extensions import db
from .models import (AsaasPayment, DeliveryStatus, Order, OrderStatus,
                     PaymentStatus, Product, TransactionStatus, UserRole)
from .utils import utcnow


def cancel_unpaid_order(order, reason, actor_id=None):
    """Caller owns the order row lock; commit is deliberately left to caller."""
    if order.status == OrderStatus.CANCELLED:
        return
    if order.payment_status != PaymentStatus.PENDING:
        raise ValueError("Pedido pago exige revisão e estorno antes do cancelamento")
    if order.delivery and order.delivery.status not in {
        DeliveryStatus.AWAITING_DRIVER, DeliveryStatus.ASSIGNED,
    }:
        raise ValueError("Entrega já iniciada; contate o suporte")
    from .api.routes import adjust_stock
    products = db.session.query(Product).filter(
        Product.id.in_([i.product_id for i in order.items if i.product_id])
    ).order_by(Product.id).with_for_update().all()
    product_map = {p.id: p for p in products}
    for item in order.items:
        if item.product_id in product_map:
            adjust_stock(product_map[item.product_id], item.quantity, "return_in",
                         actor_id=actor_id, reason=reason,
                         reference_type="order", reference_id=order.id)
    order.status = OrderStatus.CANCELLED
    order.cancelled_at = utcnow()
    order.cancellation_reason = reason[:255]
    if order.delivery:
        order.delivery.status = DeliveryStatus.CANCELLED
    for tx in order.transactions:
        if tx.status == TransactionStatus.PENDING:
            tx.status = "cancelled"


def register_operations(app):
    @app.get(app.config["API_PREFIX"] + "/ready")
    def ready():
        try:
            db.session.execute(text("SELECT 1"))
            # Also verifies migrations have created the business schema.
            db.session.query(Order.id).limit(1).all()
            return jsonify({"status": "ready"})
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Readiness failed")
            return jsonify({"status": "unavailable"}), 503

    @app.post(app.config["API_PREFIX"] + "/orders/<int:order_id>/cancel")
    @require_roles(UserRole.CUSTOMER, UserRole.ADMIN)
    def cancel_order(order_id):
        q = Order.query.filter_by(id=order_id)
        if g.current_user.role != UserRole.ADMIN:
            q = q.filter_by(customer_id=g.current_user.id)
        order = q.with_for_update().first()
        if not order:
            return jsonify({"error": "pedido não encontrado"}), 404
        pay = AsaasPayment.query.filter_by(order_id=order.id).first()
        if pay and pay.state not in {"cancelled"}:
            return jsonify({"error": "Cobrança emitida: solicite cancelamento ao suporte para conciliação no gateway"}), 409
        try:
            cancel_unpaid_order(order, "Cancelamento solicitado", g.current_user.id)
            db.session.commit()
        except ValueError as error:
            db.session.rollback()
            return jsonify({"error": str(error)}), 409
        return jsonify({"id": order.id, "status": order.status})

    @app.post(app.config["API_PREFIX"] + "/admin/orders/<int:order_id>/confirm-offline-payment")
    @require_roles(UserRole.ADMIN)
    def confirm_offline_payment(order_id):
        order = Order.query.filter_by(id=order_id).with_for_update().first()
        if not order:
            return jsonify({"error": "pedido não encontrado"}), 404
        if order.payment_method not in {"cash", "card_on_delivery"} or order.status == OrderStatus.CANCELLED:
            return jsonify({"error": "pedido não aceita confirmação manual"}), 409
        payload = request.get_json(silent=True) or {}
        reference = str(payload.get("reference") or "").strip()
        if not reference or len(reference) > 255:
            return jsonify({"error": "informe comprovante/referência do recebimento"}), 400
        if order.payment_status == PaymentStatus.PENDING:
            from .models import AuditLog
            order.payment_status = PaymentStatus.PAID
            db.session.add(AuditLog(actor_id=g.current_user.id, action="offline_payment",
                                   entity="order", entity_id=order.id,
                                   diff={"reference": reference}, ip=request.remote_addr))
            db.session.commit()
        return jsonify({"id": order.id, "payment_status": order.payment_status})

    @app.cli.command("expire-orders")
    def expire_orders():
        """Release expired reservations that have NO remote charge."""
        candidates = Order.query.filter(
            Order.status == OrderStatus.PENDING,
            Order.payment_status == PaymentStatus.PENDING,
            Order.payment_method.in_(["pix", "card_online"]),
            Order.payment_release_at < utcnow(),
            ~Order.id.in_(db.session.query(AsaasPayment.order_id)),
        ).order_by(Order.id).with_for_update(skip_locked=True).all()
        count = 0
        for order in candidates:
            cancel_unpaid_order(order, "Prazo de pagamento expirado sem cobrança emitida")
            count += 1
        db.session.commit()
        click.echo(f"Reservas expiradas: {count}")

    @app.post(app.config["API_PREFIX"] + "/admin/payments/orders/<int:order_id>/reconcile")
    @require_roles(UserRole.ADMIN)
    def reconcile_payment(order_id):
        from .payments import _api, GatewayError, _public
        from decimal import Decimal
        order = Order.query.filter_by(id=order_id).with_for_update().first()
        pay = AsaasPayment.query.filter_by(order_id=order_id).first()
        if not order or not pay:
            return jsonify({"error": "cobrança não encontrada"}), 404
        payload = request.get_json(silent=True) or {}
        external_id = str(payload.get("external_id") or pay.external_id or "")
        if not external_id.startswith("pay_") or not external_id.replace("_", "").isalnum():
            return jsonify({"error": "external_id inválido"}), 400
        try:
            remote = _api("GET", "/payments/" + external_id)
            if (remote.get("externalReference") != f"sw-order-{order.id}"
                    or Decimal(str(remote.get("value"))) != pay.amount):
                return jsonify({"error": "referência ou valor da cobrança incompatível"}), 409
            pay.external_id = external_id
            status = remote.get("status")
            if status in {"RECEIVED", "CONFIRMED"}:
                if order.payment_status != PaymentStatus.REFUNDED:
                    order.payment_status = PaymentStatus.PAID
                    pay.state = "paid_after_cancel_review" if order.status == OrderStatus.CANCELLED else "paid"
            elif status == "REFUNDED":
                order.payment_status = PaymentStatus.REFUNDED
                pay.state = "refunded"
            elif remote.get("deleted"):
                pay.state = "cancelled"
                cancel_unpaid_order(order, "Cobrança removida; conciliação administrativa", g.current_user.id)
            elif order.payment_status == PaymentStatus.PENDING and order.status != OrderStatus.CANCELLED:
                pay.state = "overdue" if status == "OVERDUE" else "pending" if status == "PENDING" else "gateway_review"
            db.session.commit()
            return jsonify(_public(pay))
        except (GatewayError, ValueError, TypeError):
            db.session.rollback()
            return jsonify({"error": "Não foi possível conciliar; confira o gateway"}), 502

    @app.post(app.config["API_PREFIX"] + "/admin/payments/orders/<int:order_id>/cancel")
    @require_roles(UserRole.ADMIN)
    def cancel_remote_payment(order_id):
        from .payments import _api, GatewayError
        order = Order.query.filter_by(id=order_id).with_for_update().first()
        pay = AsaasPayment.query.filter_by(order_id=order_id).first()
        if not order or not pay or not pay.external_id:
            return jsonify({"error": "Concilie a cobrança antes de cancelar"}), 409
        if order.payment_status != PaymentStatus.PENDING:
            return jsonify({"error": "Pagamento recebido exige estorno no gateway"}), 409
        try:
            # Fresh remote status protects against a delayed paid webhook.
            remote = _api("GET", "/payments/" + pay.external_id)
            if remote.get("status") not in {"PENDING", "OVERDUE"}:
                return jsonify({"error": "Concilie o status atual antes de cancelar"}), 409
            if not remote.get("deleted"):
                deleted = _api("DELETE", "/payments/" + pay.external_id)
                if not deleted.get("deleted"):
                    raise GatewayError("Remoção não confirmada")
            cancel_unpaid_order(order, "Cobrança cancelada no gateway", g.current_user.id)
            pay.state = "cancelled"
            db.session.commit()
            return jsonify({"id": order.id, "status": order.status})
        except (GatewayError, ValueError):
            db.session.rollback()
            return jsonify({"error": "Cancelamento inconclusivo; concilie antes de repetir"}), 409

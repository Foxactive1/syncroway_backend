"""SyncroWay / Asaas PIX hosted-invoice integration (sandbox-first).

Deliberately does NOT execute driver bank transfers: eligibility is recorded only
once the delivery is completed AND the order has been paid. This avoids
mistaking an internal ledger settlement for an actual transfer.
"""
import hmac
import json
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import current_app, g, jsonify, request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from .auth import require_roles
from .extensions import db
from .models import (
    AsaasCustomer, AsaasPayment, AsaasRecipientWallet, AsaasWebhookEvent,
    Delivery, DeliveryStatus, Order, OrderItem, OrderStatus, PaymentStatus,
    Store, Transaction, TransactionStatus, TransactionType, UserRole,
)
from .utils import money


class GatewayError(Exception):
    pass


def _api(method, path, payload=None):
    config = current_app.config
    if config.get("ASAAS_ENV") not in {"sandbox", "production"}:
        raise GatewayError("ASAAS_ENV inválido")
    if not config.get("ASAAS_API_KEY"):
        raise GatewayError("ASAAS_API_KEY não configurada")
    origin = ("https://api-sandbox.asaas.com" if config["ASAAS_ENV"] == "sandbox"
              else "https://api.asaas.com")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = Request(
        origin + "/v3" + path, data=body, method=method,
        headers={
            "access_token": config["ASAAS_API_KEY"],
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "SyncroWay/1.0",
        },
    )
    try:
        with urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        try:
            snippet = error.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            snippet = ""
        current_app.logger.warning(
            "Asaas HTTP %s em %s — body: %s", error.code, path, snippet
        )
        raise GatewayError(f"Gateway retornou HTTP {error.code}") from error
    except (URLError, TimeoutError) as error:
        raise GatewayError(
            "Falha ou timeout na comunicação com o gateway; "
            "concilie antes de tentar novamente"
        ) from error


def _cents(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _store_order(order):
    """Cada pedido atual pertence a exatamente uma loja.

    Usa joinedload para evitar N+1 ao acessar item.product.store_id.
    """
    if order.type != "product":
        return None
    items = (
        db.session.query(OrderItem)
        .options(joinedload(OrderItem.product))
        .filter(
            OrderItem.order_id == order.id,
            OrderItem.product_id.isnot(None),
        )
        .all()
    )
    ids = {item.product.store_id for item in items if item.product}
    if len(ids) != 1:
        return None
    return db.session.get(Store, ids.pop())


def _public(payment):
    return {
        "id": payment.id,
        "order_id": payment.order_id,
        "state": payment.state,
        "billing_type": payment.billing_type,
        "amount": money(payment.amount),
        "invoice_url": payment.invoice_url,
    }


def _customer_id(user, cpf):
    existing = AsaasCustomer.query.filter_by(user_id=user.id).first()
    if existing:
        return existing.external_id
    digits = "".join(char for char in str(cpf or "") if char.isdigit())
    if len(digits) not in (11, 14):
        raise ValueError("Informe CPF/CNPJ válido do pagador")
    # Não persistimos CPF/CNPJ localmente. Em caso de timeout, concilie
    # manualmente o customer externo antes de tentar de novo (Asaas pode
    # criar duplicatas).
    remote = _api("POST", "/customers", {
        "name": user.name,
        "email": user.email,
        "cpfCnpj": digits,
        "externalReference": f"sw-user-{user.id}",
    })
    record = AsaasCustomer(user_id=user.id, external_id=remote["id"])
    db.session.add(record)
    db.session.commit()
    return record.external_id


def register_payment_routes(bp):
    @bp.post("/admin/payments/store-wallets/<int:store_id>")
    @require_roles(UserRole.ADMIN)
    def save_store_wallet(store_id):
        store = db.session.get(Store, store_id)
        if not store:
            return jsonify({"error": "loja não encontrada"}), 404
        data = request.get_json(silent=True) or {}
        wallet = str(data.get("wallet_id") or "").strip()
        if not wallet or len(wallet) > 100:
            return jsonify({"error": "wallet_id inválido"}), 400
        row = AsaasRecipientWallet.query.filter_by(store_id=store.id).first()
        if not row:
            row = AsaasRecipientWallet(store_id=store.id, wallet_id=wallet)
            db.session.add(row)
        row.wallet_id = wallet
        # Deve ser ativada SOMENTE após operador verificar o dono da carteira.
        row.enabled = bool(data.get("verified", False))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return jsonify({"error": "carteira já associada a outra loja"}), 409
        return jsonify({"store_id": store.id, "enabled": row.enabled})

    @bp.post("/payments/orders/<int:order_id>")
    @require_roles(UserRole.CUSTOMER)
    def create_order_payment(order_id):
        if not current_app.config.get("ASAAS_ENABLED"):
            return jsonify({"error": "gateway não habilitado"}), 503
        order = (db.session.query(Order)
                 .filter_by(id=order_id, customer_id=g.current_user.id)
                 .with_for_update()
                 .first())
        if not order:
            return jsonify({"error": "pedido não encontrado"}), 404
        if order.payment_status != PaymentStatus.PENDING \
                or order.status == OrderStatus.CANCELLED:
            return jsonify({"error": "pedido não está aguardando pagamento"}), 409
        if order.payment_method not in ("pix", "card_online"):
            return jsonify({"error": "forma de pagamento offline"}), 409
        if order.total_amount <= 0:
            return jsonify({"error": "valor do pedido inválido"}), 409
        existing = AsaasPayment.query.filter_by(order_id=order.id).first()
        if existing:
            if existing.external_id:
                return jsonify(_public(existing)), 200
            return jsonify({
                "error": "cobrança com criação inconclusiva; "
                         "concilie na Asaas antes de repetir"
            }), 409
        store = _store_order(order)
        if not store:
            return jsonify({"error": "pedido sem loja única"}), 409
        wallet = AsaasRecipientWallet.query.filter_by(
            store_id=store.id, enabled=True
        ).first()
        if not wallet:
            return jsonify({
                "error": "loja ainda não possui carteira Asaas verificada"
            }), 409
        # Valida antes de criar o marcador durável "creating".
        incoming = request.get_json(silent=True) or {}
        digits = "".join(c for c in str(incoming.get("cpf_cnpj") or "")
                         if c.isdigit())
        if not AsaasCustomer.query.filter_by(user_id=g.current_user.id).first() \
                and len(digits) not in (11, 14):
            return jsonify({
                "error": "CPF/CNPJ do pagador é necessário na primeira cobrança"
            }), 400
        # Percentual sobre *netValue*: reparte taxas proporcionalmente. A parte
        # do entregador fica na conta emissora e só é paga APÓS a entrega.
        merchant_gross = _cents(order.subtotal - order.commission_value)
        share = (merchant_gross / order.total_amount * 100).quantize(
            Decimal("0.0001")
        )
        if share < 0 or share > 100:
            return jsonify({"error": "configuração de split inválida"}), 409
        pay = AsaasPayment(
            order_id=order.id,
            amount=order.total_amount,
            billing_type="PIX" if order.payment_method == "pix" else "CREDIT_CARD",
            split_wallet_id=wallet.wallet_id,
            split_percent=share,
        )
        db.session.add(pay)
        # Marcador durável ANTES da chamada remota evita cobrança dupla em retry.
        db.session.commit()
        try:
            customer = _customer_id(g.current_user, incoming.get("cpf_cnpj"))
            remote = _api("POST", "/payments", {
                "customer": customer,
                "billingType": pay.billing_type,
                "value": float(pay.amount),
                "dueDate": (date.today() + timedelta(days=1)).isoformat(),
                "description": f"SyncroWay pedido {order.code}"[:500],
                "externalReference": f"sw-order-{order.id}",
                "split": [{
                    "walletId": pay.split_wallet_id,
                    "percentualValue": float(pay.split_percent),
                }],
            })
            pay.external_id = remote["id"]
            pay.invoice_url = remote.get("invoiceUrl")
            pay.state = "pending"
            db.session.commit()
            current_app.logger.info(
                "Asaas cobrança criada order=%s pay=%s external=%s",
                order.id, pay.id, pay.external_id,
            )
            return jsonify(_public(pay)), 201
        except ValueError as error:
            pay.state = "unknown"
            pay.error = "customer-data"
            db.session.commit()
            return jsonify({"error": str(error)}), 400
        except (GatewayError, KeyError) as error:
            pay.state = "unknown"
            pay.error = "gateway-error"
            db.session.commit()
            current_app.logger.warning(
                "Cobrança inconclusiva para pedido %s: %s", order.id, error
            )
            return jsonify({
                "error": "cobrança inconclusiva; "
                         "contate o suporte para conciliação"
            }), 502

    @bp.get("/payments/orders/<int:order_id>")
    @require_roles(UserRole.CUSTOMER)
    def read_order_payment(order_id):
        order = Order.query.filter_by(
            id=order_id, customer_id=g.current_user.id
        ).first()
        if not order:
            return jsonify({"error": "pedido não encontrado"}), 404
        pay = AsaasPayment.query.filter_by(order_id=order_id).first()
        if not pay:
            return jsonify({"error": "cobrança não encontrada"}), 404
        return jsonify({
            **_public(pay),
            "order_payment_status": order.payment_status,
        })

    @bp.get("/payments/orders/<int:order_id>/pix")
    @require_roles(UserRole.CUSTOMER)
    def get_order_pix(order_id):
        order = Order.query.filter_by(
            id=order_id, customer_id=g.current_user.id
        ).first()
        pay = AsaasPayment.query.filter_by(order_id=order_id).first() if order else None
        if not pay or pay.billing_type != "PIX" or not pay.external_id:
            return jsonify({"error": "Pix não disponível"}), 404
        try:
            qr = _api("GET", f"/payments/{pay.external_id}/pixQrCode")
            return jsonify({
                "payload": qr.get("payload"),
                "encodedImage": qr.get("encodedImage"),
                "expirationDate": qr.get("expirationDate"),
            })
        except GatewayError:
            return jsonify({"error": "não foi possível consultar o Pix"}), 502

    @bp.post("/webhooks/asaas")
    def asaas_webhook():
        token = current_app.config.get("ASAAS_WEBHOOK_TOKEN", "")
        provided = request.headers.get("asaas-access-token", "")
        if not token or not hmac.compare_digest(token, provided):
            return jsonify({"error": "não autorizado"}), 401
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "JSON inválido"}), 400
        eid = str(payload.get("id") or "")
        event = str(payload.get("event") or "")
        remote = payload.get("payment") or {}
        pid = remote.get("id") if isinstance(remote, dict) else None
        if not eid or len(eid) > 160 or not event:
            return jsonify({"error": "evento inválido"}), 400
        # Insert atômico + efeitos de negócio. UNIQUE rejeita duplicatas.
        if db.session.get(AsaasWebhookEvent, eid):
            return jsonify({"ok": True, "duplicate": True})
        db.session.add(AsaasWebhookEvent(
            id=eid, event_type=event, payment_id=pid, payload=payload,
        ))
        pay = AsaasPayment.query.filter_by(external_id=pid).first() if pid else None
        if pay:
            order = db.session.get(Order, pay.order_id)
            if event in {"PAYMENT_RECEIVED", "PAYMENT_CONFIRMED"}:
                # Não restaura estado refunded em callbacks fora de ordem.
                if order.payment_status not in {PaymentStatus.REFUNDED,
                                                PaymentStatus.PAID}:
                    order.payment_status = PaymentStatus.PAID
                if order.payment_status == PaymentStatus.PAID:
                    pay.state = "paid"
            elif event in {"PAYMENT_REFUNDED", "PAYMENT_PARTIALLY_REFUNDED"}:
                # Refunds parciais exigem reconciliação financeira manual.
                if event == "PAYMENT_REFUNDED":
                    order.payment_status = PaymentStatus.REFUNDED
                    pay.state = "refunded"
                else:
                    pay.state = "partial_refund_review"
            elif event in {"PAYMENT_DELETED", "PAYMENT_OVERDUE"}:
                if order.payment_status == PaymentStatus.PENDING:
                    pay.state = ("expired" if event == "PAYMENT_OVERDUE"
                                 else "cancelled")
            elif event == "PAYMENT_SPLIT_DONE":
                # Não assume que toda wallet foi paga; concilie splitId/valores.
                pass
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return jsonify({"ok": True, "duplicate": True})
        return jsonify({"ok": True})

    @bp.get("/driver/receivables")
    @require_roles(UserRole.DRIVER)
    def driver_receivables():
        txs = (
            db.session.query(Transaction)
            .join(Order, Transaction.order_id == Order.id)
            .join(Delivery, Delivery.order_id == Order.id)
            .filter(
                Transaction.type == TransactionType.DELIVERY_FEE,
                Transaction.beneficiary_id == g.current_user.id,
            )
            .all()
        )
        eligible = [
            tx for tx in txs
            if tx.order.payment_status == PaymentStatus.PAID
            and tx.order.delivery.status == DeliveryStatus.DELIVERED
        ]
        total = sum((t.amount for t in eligible), Decimal("0"))
        pending = sum((t.amount for t in eligible
                       if t.status == TransactionStatus.PENDING), Decimal("0"))
        settled = sum((t.amount for t in eligible
                       if t.status == TransactionStatus.SETTLED), Decimal("0"))
        return jsonify({
            "eligible_total": money(total),
            "eligible_pending": money(pending),
            "transferred_total": money(settled),
            "note": ("Liquidado internamente não comprova transferência; "
                     "confirme comprovante externo."),
        })
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from hashlib import sha256
from email_validator import validate_email, EmailNotValidError
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from werkzeug.security import generate_password_hash

from ..auth import _decode_token, create_tokens, require_auth, require_roles
from ..extensions import db, limiter
from ..models import (
    Appointment,
    AppointmentStatus,
    Cart,
    CartItem,
    Category,
    Delivery,
    DeliveryStatus,
    MediaAsset,
    MediaAssetUsage,
    DriverProfile,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    Product,
    RefreshToken,
    Service,
    ServiceModality,
    ServicePricingModel,
    ServiceProviderProfile,
    StockMovement,
    StockMovementType,   # ← adicionar
    Store,
    Transaction,
    TransactionStatus,
    TransactionType,
    User,
    UserRole,
)
from ..utils import as_bool, iso_utc, money, parse_decimal, utcnow

api_bp = Blueprint("api", __name__)


def auth_limit_key():
    data = request.get_json(silent=True)
    email = str(data.get("email", "")) if isinstance(data, dict) else ""
    return "account:" + sha256(email.strip().lower().encode()).hexdigest()



# ═══════════════════════════════════════════════════════════════
# CONSTANTES DE VALIDAÇÃO
# ═══════════════════════════════════════════════════════════════

# Teto absoluto para taxas de entrega.
# NUMERIC(12,2) do Postgres = até 10^10. 10^15 é folga suficiente para
# qualquer valor legítimo e bloqueia 1e999999/1e308/afins mesmo se
# `parse_decimal` regredir. Defesa em profundidade.
MAX_DELIVERY_FEE = Decimal("1e15")


def _parse_fee(raw, field_name):
    """`None` (ausente/null) vira 0; qualquer outro valor é validado.

    Nota: "" é erro — cliente deve mandar null ou omitir o campo. Só None
    tem default. Evita que um form vazio silenciosamente vire taxa zero.
    """
    if raw is None:
        return Decimal("0")
    parsed = parse_decimal(raw, field_name)
    if not parsed.is_finite() or abs(parsed) > MAX_DELIVERY_FEE:
        raise ValueError(f"{field_name} fora do intervalo permitido")
    return parsed


# ═══════════════════════════════════════════════════════════════
# PARSERS
# ═══════════════════════════════════════════════════════════════

def parse_iso_datetime(value):
    s = str(value)
    # Bounding do tamanho antes do parser evita payloads abusivos.
    if len(s) > 64:
        raise ValueError("data deve estar em formato ISO 8601 válido")
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("data deve estar em formato ISO 8601 válido") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=ZoneInfo(current_app.config["DEFAULT_TIMEZONE"])
        )
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def parse_period_args(default_days=30):
    """Converte date_from/date_to ou period em intervalo UTC inclusivo."""
    period = str(request.args.get("period", "")).strip().lower()
    today = datetime.now(timezone.utc).date()
    presets = {"7d": 7, "30d": 30, "90d": 90}

    def parse_day(value, field):
        try:
            return date.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} deve usar AAAA-MM-DD") from exc

    raw_start = request.args.get("date_from")
    raw_end = request.args.get("date_to")
    end_day = parse_day(raw_end, "date_to") if raw_end else today
    if raw_start:
        start_day = parse_day(raw_start, "date_from")
    elif period == "all":
        start_day = date(2000, 1, 1)
    else:
        days = presets.get(period, default_days)
        start_day = end_day - timedelta(days=days - 1)
    if start_day > end_day:
        raise ValueError("date_from não pode ser posterior a date_to")
    return (
        datetime.combine(start_day, time.min),
        datetime.combine(end_day, time.max),
        start_day.isoformat(),
        end_day.isoformat(),
    )


# ═══════════════════════════════════════════════════════════════
# SERIALIZERS
# ═══════════════════════════════════════════════════════════════

def user_json(user):
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "phone": user.phone,
        "role": user.role,
        "active": user.active,
    }


def delivery_json(delivery, *, expose_addresses=True):
    return {
        "id": delivery.id,
        "order_id": delivery.order_id,
        "driver": user_json(delivery.driver) if delivery.driver else None,
        "status": delivery.status,
        "pickup_address": delivery.pickup_address if expose_addresses else None,
        "delivery_address": delivery.delivery_address if expose_addresses else None,
        "delivery_fee": money(delivery.delivery_fee),
        "distance_km": (
            money(delivery.distance_km)
            if delivery.distance_km is not None else None
        ),
        "accepted_at": iso_utc(delivery.accepted_at),
        "picked_up_at": iso_utc(delivery.picked_up_at),
        "delivered_at": iso_utc(delivery.delivered_at),
        "created_at": iso_utc(delivery.created_at),
    }


def store_json(store):
    return {
        "id": store.id,
        "name": store.name,
        "category": store.category.name if store.category else None,
        "category_id": store.category_id,
        "description": store.description,
        "image_url": store.image_url,
        "city": store.city,
        "state": store.state,
        "accepts_orders": store.accepts_orders,
        "accepts_appointments": store.accepts_appointments,
    }


def product_json(product):
    return {
        "id": product.id,
        "store_id": product.store_id,
        "store_name": product.store.name if product.store else None,
        "category_id": product.category_id,
        "name": product.name,
        "description": product.description,
        "sku": product.sku,
        "price": money(product.price),
        "stock": product.stock,
        "minimum_stock": product.minimum_stock,
        "stock_status": (
            "out" if product.stock <= 0
            else "low" if product.stock <= product.minimum_stock
            else "ok"
        ),
        "inventory_value": money(product.price * product.stock),
        "image_url": product.image_url,
    }


def stock_movement_json(movement):
    return {
        "id": movement.id,
        "product_id": movement.product_id,
        "product_name": movement.product.name if movement.product else None,
        "type": movement.type,
        "quantity": movement.quantity,
        "stock_before": movement.stock_before,
        "stock_after": movement.stock_after,
        "reason": movement.reason,
        "reference_type": movement.reference_type,
        "reference_id": movement.reference_id,
        "actor": user_json(movement.actor) if movement.actor else None,
        "created_at": iso_utc(movement.created_at),
    }


def transaction_json(transaction):
    return {
        "id": transaction.id,
        "order_id": transaction.order_id,
        "type": transaction.type,
        "amount": money(transaction.amount),
        "beneficiary": transaction.beneficiary,
        "beneficiary_id": transaction.beneficiary_id,
        "status": transaction.status,
        "settled_at": iso_utc(transaction.settled_at),
        "created_at": iso_utc(transaction.created_at),
    }


def service_json(service):
    return {
        "id": service.id,
        "store_id": service.store_id,
        "store_name": service.store.name if service.store else None,
        "provider_id": service.provider_id,
        "provider_name": service.provider.name if service.provider else None,
        "category_id": service.category_id,
        "name": service.title,
        "title": service.title,
        "description": service.description,
        "image_url": service.image_url,
        "duration_minutes": service.duration_minutes,
        "price": money(service.price) if service.price is not None else None,
        "pricing_model": service.pricing_model,
        "hourly_rate": (
            money(service.hourly_rate) if service.hourly_rate is not None else None
        ),
        "visit_fee": money(service.visit_fee) if service.visit_fee is not None else None,
        "modality": service.modality,
        "rating": money(service.rating_avg),
        "active": service.active,
    }


def order_summary_json(order):
    return {
        "id": order.id,
        "code": order.code,
        "type": order.type,
        "status": order.status,
        "payment_status": order.payment_status,
        "payment_deadline": iso_utc(order.payment_release_at),
        "payment_method": order.payment_method,
        "subtotal": money(order.subtotal),
        "delivery_fee": money(order.delivery_fee),
        "total": money(order.total_amount),
        "address": order.address,
        "created_at": iso_utc(order.created_at),
        "delivery": delivery_json(order.delivery) if order.delivery else None,
    }


def driver_profile_json(profile):
    return {
        "id": profile.id,
        "document": profile.document,
        "license_number": profile.license_number,
        "vehicle_type": profile.vehicle_type,
        "vehicle_plate": profile.vehicle_plate,
        "available": profile.available,
        "approved": profile.approved,
        "latitude": (float(profile.current_latitude)
                     if profile.current_latitude is not None else None),
        "longitude": (float(profile.current_longitude)
                      if profile.current_longitude is not None else None),
    }


def provider_profile_json(profile):
    return {
        "id": profile.id,
        "user_id": profile.user_id,
        "headline": profile.headline,
        "bio": profile.bio,
        "years_experience": profile.years_experience,
        "offers_remote": profile.offers_remote,
        "offers_in_person": profile.offers_in_person,
        "service_radius_km": money(profile.service_radius_km),
        "city": profile.city,
        "state": profile.state,
        "verification_status": profile.verification_status,
        "is_available": profile.is_available,
        "rating": money(profile.rating_avg),
        "rating_count": profile.rating_count,
        "completed_jobs": profile.completed_jobs,
    }


# ═══════════════════════════════════════════════════════════════
# DOMAIN HELPERS
# ═══════════════════════════════════════════════════════════════

def adjust_stock(product, delta, movement_type, *, actor_id,
                 reason=None, reference_type=None, reference_id=None):
    """Aplica delta ao estoque E registra o movimento atomicamente.

    Substitui o padrão sujeito a erro:
        stock_before = product.stock
        product.stock += delta
        register_stock_movement(..., stock_before=stock_before)
    """
    before = product.stock
    after = before + delta
    if after < 0:
        raise ValueError("estoque não pode ficar negativo")
    product.stock = after
    db.session.add(StockMovement(
        product_id=product.id,
        actor_id=actor_id,
        type=movement_type,
        quantity=delta,
        stock_before=before,
        stock_after=after,
        reason=reason,
        reference_type=reference_type,
        reference_id=reference_id,
    ))


def get_or_create_cart(user_id: int):
    cart = Cart.query.filter_by(customer_id=user_id).first()
    if not cart:
        cart = Cart(customer_id=user_id)
        db.session.add(cart)
        db.session.flush()
    return cart


def cart_json(cart):
    # Eager load: /cart é chamado com frequência e item.product/store
    # disparavam N+1.
    rows = (
        CartItem.query
        .filter_by(cart_id=cart.id)
        .options(
            joinedload(CartItem.product).joinedload(Product.store),
            joinedload(CartItem.service).joinedload(Service.store),
        )
        .all()
    )
    subtotal = Decimal("0")
    items = []
    for item in rows:
        subject = item.product or item.service
        if not subject or not subject.active or subject.price is None:
            continue
        line = subject.price * item.quantity
        subtotal += line
        items.append({
            "id": item.id,
            "product_id": item.product_id,
            "service_id": item.service_id,
            "type": "product" if item.product_id else "service",
            "store_id": subject.store_id,
            "name": item.product.name if item.product else item.service.title,
            "store_name": subject.store.name if subject.store else None,
            "quantity": item.quantity,
            "unit_price": money(subject.price),
            "line_total": money(line),
            "image_url": subject.image_url,
        })
    return {
        "id": cart.id,
        "items": items,
        "subtotal": money(subtotal),
        "item_count": sum(x["quantity"] for x in items),
    }


def owned_store(store_id):
    store = db.session.get(Store, store_id)
    if not store:
        return None, (jsonify({"error": "loja não encontrada"}), 404)
    if g.current_user.role != UserRole.ADMIN and store.owner_id != g.current_user.id:
        return None, (jsonify({"error": "acesso negado"}), 403)
    return store, None


# ═══════════════════════════════════════════════════════════════
# HEALTH + CONFIG PÚBLICA
# ═══════════════════════════════════════════════════════════════

@api_bp.get("/health")
def api_health():
    return jsonify({"status": "ok", "service": "syncroway-api"})


@api_bp.get("/public-config")
def public_config():
    """Config pública usada pelo frontend para adaptar a UX.

    Não expõe segredos — apenas flags booleanas e valores derivados de
    defaults seguros. Decisões financeiras continuam 100% server-side.
    """
    return jsonify({
        "asaas_enabled": bool(current_app.config.get("ASAAS_ENABLED")),
        "delivery_fee_per_store": str(
            current_app.config.get("DELIVERY_FEE_PER_STORE", "0")
        ),
        "payment_methods": (["pix", "card_online", "cash", "card_on_delivery"]
                            if current_app.config.get("ASAAS_ENABLED") else ["cash", "card_on_delivery"]),
        "api_prefix": current_app.config.get("API_PREFIX", "/api"),
    })


# ═══════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════

@api_bp.post("/auth/register")
@limiter.limit("5 per minute")
def register():
    data = request.get_json(silent=True) or {}
    if not all(str(data.get(k, "")).strip() for k in ("name", "email", "password")):
        return jsonify({"error": "name, email e password são obrigatórios"}), 400
    if not all(isinstance(data.get(k), str) for k in ("name", "email", "password")):
        return jsonify({"error": "name, email e password devem ser textos"}), 400
    if len(data["name"]) > 120 or len(data["email"]) > 180 or len(data["password"]) > 256:
        return jsonify({"error": "campo excede o tamanho permitido"}), 400
    try:
        email = validate_email(data["email"].strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return jsonify({"error": "e-mail inválido"}), 400
    if len(str(data["password"])) < 8:
        return jsonify({"error": "password deve possuir ao menos 8 caracteres"}), 400

    user = User(
        name=data["name"].strip(),
        email=email,
        password_hash=generate_password_hash(data["password"]),
        phone=data.get("phone"),
        role=UserRole.CUSTOMER,
    )
    db.session.add(user)
    try:
        db.session.flush()  # dispara UNIQUE(email) antes de qualquer outro write
        db.session.add(Cart(customer_id=user.id))
        tokens = create_tokens(user, commit=False)
        db.session.commit()  # transação única: user + cart + refresh token
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "e-mail já cadastrado"}), 409

    return jsonify({"user": user_json(user), **tokens}), 201


@api_bp.post("/auth/login")
@limiter.limit("10 per minute")
@limiter.limit("10 per minute", key_func=auth_limit_key)
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    user = User.query.filter_by(email=email).first()
    if not user or not user.active or not user.check_password(password):
        return jsonify({"error": "e-mail ou senha inválidos"}), 401
    user.last_login_at = utcnow()
    # last_login_at + refresh token em um único commit.
    tokens = create_tokens(user, commit=False)
    db.session.commit()
    return jsonify({"user": user_json(user), **tokens})


@api_bp.post("/auth/refresh")
@limiter.limit("30 per minute")
def refresh_token():
    data = request.get_json(silent=True) or {}
    token_str = data.get("refresh_token")
    if not token_str:
        return jsonify({"error": "refresh_token é obrigatório"}), 400

    payload = _decode_token(token_str, expected_type="refresh")
    if not payload:
        return jsonify({"error": "token inválido ou expirado"}), 401

    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "token inválido"}), 401

    # Lock pessimista: duas requisições concorrentes com o mesmo refresh
    # não podem rotacionar duas vezes.
    db_token = (
        RefreshToken.query
        .filter_by(token=token_str)
        .with_for_update()
        .first()
    )
    if not db_token or db_token.revoked or db_token.user_id != user_id:
        return jsonify({"error": "token revogado ou não encontrado"}), 401
    if db_token.expires_at and db_token.expires_at < utcnow():
        return jsonify({"error": "token expirado"}), 401

    user = db.session.get(User, user_id)
    if not user or not user.active:
        return jsonify({"error": "usuário inválido"}), 401

    db_token.revoked = True
    # Rotação e revogação no mesmo commit.
    tokens = create_tokens(user, commit=False)
    db.session.commit()
    return jsonify(tokens)


@api_bp.post("/auth/logout")
@require_auth
def logout():
    data = request.get_json(silent=True) or {}
    token_str = data.get("refresh_token")
    if token_str:
        db_token = RefreshToken.query.filter_by(
            token=token_str, user_id=g.current_user.id
        ).first()
        if db_token:
            db_token.revoked = True
            db.session.commit()
    return jsonify({"message": "Logout realizado com sucesso"}), 200


@api_bp.get("/me")
@require_auth
def me():
    return jsonify(user_json(g.current_user))


# ═══════════════════════════════════════════════════════════════
# CATALOG (PÚBLICO)
# ═══════════════════════════════════════════════════════════════

@api_bp.get("/categories")
def categories():
    rows = Category.query.filter_by(active=True).order_by(Category.name).all()
    return jsonify([
        {"id": c.id, "name": c.name, "slug": c.slug, "kind": c.kind, "icon": c.icon}
        for c in rows
    ])


@api_bp.get("/stores")
def stores():
    query = request.args.get("q", "").strip()
    category_id = request.args.get("category_id", type=int)
    stmt = Store.query.filter_by(active=True)
    if category_id:
        stmt = stmt.filter(Store.category_id == category_id)
    if query:
        like = f"%{query}%"
        stmt = stmt.filter(or_(Store.name.ilike(like),
                               Store.description.ilike(like)))
    return jsonify([store_json(x)
                    for x in stmt.order_by(Store.name.asc()).all()])


@api_bp.get("/stores/<int:store_id>")
def store_detail(store_id):
    store = Store.query.filter_by(id=store_id, active=True).first_or_404()
    return jsonify({
        **store_json(store),
        "products": [product_json(p) for p in store.products if p.active],
        "services": [service_json(s) for s in store.services if s.active],
    })


@api_bp.get("/products")
def products():
    query = request.args.get("q", "").strip()
    store_id = request.args.get("store_id", type=int)
    category_id = request.args.get("category_id", type=int)
    stmt = Product.query.filter_by(active=True).join(
        Store, Product.store_id == Store.id
    ).filter(Store.active.is_(True))
    if store_id:
        stmt = stmt.filter(Product.store_id == store_id)
    if category_id:
        stmt = stmt.filter(Product.category_id == category_id)
    if query:
        like = f"%{query}%"
        stmt = stmt.filter(or_(Product.name.ilike(like),
                               Product.description.ilike(like)))
    return jsonify([product_json(x) for x in stmt.order_by(Product.name).all()])


@api_bp.get("/services")
def services():
    query = request.args.get("q", "").strip()
    store_id = request.args.get("store_id", type=int)
    category_id = request.args.get("category_id", type=int)
    stmt = Service.query.filter(Service.active.is_(True)).outerjoin(
        Store, Service.store_id == Store.id
    ).join(User, Service.provider_id == User.id).filter(
        User.active.is_(True),
        or_(Service.store_id.is_(None), Store.active.is_(True)),
    )
    if store_id:
        stmt = stmt.filter(Service.store_id == store_id)
    if category_id:
        stmt = stmt.filter(Service.category_id == category_id)
    if query:
        like = f"%{query}%"
        stmt = stmt.filter(or_(Service.title.ilike(like),
                               Service.description.ilike(like)))
    return jsonify([service_json(x) for x in stmt.order_by(Service.title).all()])


@api_bp.get("/catalog")
def catalog():
    """Contrato agregado usado pelo marketplace web."""
    item_type = request.args.get("type", "").strip().lower()
    query = request.args.get("q", "").strip()
    category_value = request.args.get("category", "").strip()

    category = None
    if category_value:
        aliases = {
            "veterinaria": "veterinaria-pet",
            "casa": "casa-manutencao",
            "moda": "moda",
            "beleza": "beleza",
        }
        slug = aliases.get(category_value, category_value)
        category = Category.query.filter_by(slug=slug, active=True).first()
        if not category:
            return jsonify({"items": [], "total": 0})

    items = []
    if item_type in {"", "product"}:
        stmt = Product.query.filter(Product.active.is_(True)).join(Store).filter(
            Store.active.is_(True)
        )
        if category:
            stmt = stmt.filter(Product.category_id == category.id)
        if query:
            like = f"%{query}%"
            stmt = stmt.filter(or_(Product.name.ilike(like),
                                   Product.description.ilike(like)))
        for product in stmt.order_by(Product.name).all():
            items.append({
                **product_json(product),
                "type": "product",
                "category": product.category.slug,
                "store": product.store.name,
                "rating": 0,
                "eta": "Consulte a entrega",
                "badge": "Disponível",
                "emoji": "🛍️",
                "art": "product",
            })

    if item_type in {"", "service"}:
        stmt = Service.query.filter(Service.active.is_(True)).outerjoin(
            Store, Service.store_id == Store.id
        ).join(User, Service.provider_id == User.id).filter(
            User.active.is_(True),
            or_(Service.store_id.is_(None), Store.active.is_(True)),
        )
        if category:
            stmt = stmt.filter(Service.category_id == category.id)
        if query:
            like = f"%{query}%"
            stmt = stmt.filter(or_(Service.title.ilike(like),
                                   Service.description.ilike(like)))
        for service in stmt.order_by(Service.title).all():
            data = service_json(service)
            items.append({
                **data,
                "type": "service",
                "category": service.category.slug,
                "store": data["store_name"] or data["provider_name"],
                "rating": data["rating"],
                "eta": "Consulte a agenda",
                "badge": "Agendável",
                "emoji": "🛠️",
                "art": "service",
            })

    return jsonify({"items": items, "total": len(items)})


# ═══════════════════════════════════════════════════════════════
# CART
# ═══════════════════════════════════════════════════════════════

@api_bp.get("/cart")
@require_roles(UserRole.CUSTOMER)
def get_cart():
    return jsonify(cart_json(get_or_create_cart(g.current_user.id)))


@api_bp.post("/cart/items")
@require_roles(UserRole.CUSTOMER)
def add_cart_item():
    data = request.get_json(silent=True) or {}
    product_id = data.get("product_id")
    try:
        product_id = int(product_id)
        quantity = int(data.get("quantity", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "product_id e quantity devem ser inteiros"}), 400
    if not product_id or quantity < 1:
        return jsonify({"error": "product_id e quantity válidos são obrigatórios"}), 400

    product = (
        db.session.query(Product)
        .filter_by(id=product_id)
        .with_for_update()
        .first()
    )
    if (not product or not product.active or not product.store
            or not product.store.active or not product.store.accepts_orders
            or product.stock < quantity):
        db.session.rollback()
        return jsonify({"error": "produto indisponível ou estoque insuficiente"}), 409

    cart = get_or_create_cart(g.current_user.id)
    item = CartItem.query.filter_by(cart_id=cart.id,
                                    product_id=product.id).first()
    if item:
        if item.quantity + quantity > product.stock:
            db.session.rollback()
            return jsonify({"error": "estoque insuficiente"}), 409
        item.quantity += quantity
    else:
        db.session.add(CartItem(cart_id=cart.id, product_id=product.id,
                                quantity=quantity))
    db.session.commit()
    return jsonify(cart_json(cart)), 201


@api_bp.patch("/cart/items/<int:item_id>")
@require_roles(UserRole.CUSTOMER)
def update_cart_item(item_id):
    item = CartItem.query.get_or_404(item_id)
    if item.cart.customer_id != g.current_user.id:
        return jsonify({"error": "item não pertence ao usuário"}), 403
    data = request.get_json(silent=True) or {}
    try:
        quantity = int(data.get("quantity", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "quantity deve ser inteiro"}), 400
    if quantity <= 0:
        db.session.delete(item)
    else:
        if not item.product or quantity > item.product.stock:
            return jsonify({"error": "estoque insuficiente"}), 409
        item.quantity = quantity
    db.session.commit()
    return jsonify(cart_json(item.cart))


@api_bp.delete("/cart/items/<int:item_id>")
@require_roles(UserRole.CUSTOMER)
def delete_cart_item(item_id):
    item = CartItem.query.get_or_404(item_id)
    if item.cart.customer_id != g.current_user.id:
        return jsonify({"error": "item não pertence ao usuário"}), 403
    cart = item.cart
    db.session.delete(item)
    db.session.commit()
    return jsonify(cart_json(cart))


# ═══════════════════════════════════════════════════════════════
# CHECKOUT
# ═══════════════════════════════════════════════════════════════

@api_bp.post("/checkout")
@require_roles(UserRole.CUSTOMER)
def checkout():
    data = request.get_json(silent=True) or {}
    address = str(data.get("address", "")).strip()
    payment_method = str(data.get("payment_method",
                                  PaymentMethod.CASH)).strip().lower()

    if not address:
        return jsonify({"error": "endereço é obrigatório"}), 400
    valid_payment_methods = {
        PaymentMethod.PIX, PaymentMethod.CASH,
        "card_online", "card_on_delivery",
    }
    if payment_method not in valid_payment_methods:
        return jsonify({"error": "forma de pagamento inválida"}), 400

    if payment_method in {"pix", "card_online"} and not current_app.config.get("ASAAS_ENABLED"):
        return jsonify({"error": "Pagamento online indisponível; escolha pagamento na entrega"}), 409

    # Frete sempre pertence ao servidor. Compatibilidade explícita só em testes/dev.
    if (current_app.config.get("APP_ENV") == "production"
            or not current_app.config.get("DELIVERY_FEE_FROM_CLIENT")):
        configured_fee = Decimal(str(
            current_app.config.get("DELIVERY_FEE_PER_STORE", "0")
        ))
        if configured_fee < 0:
            return jsonify({"error": "configuração de frete inválida"}), 503
        data.pop("delivery_fees", None)
        data["delivery_fee"] = str(configured_fee)

    try:
        # `_parse_fee` aceita None como 0, mas rejeita "" (form vazio),
        # NaN/Infinity e magnitudes acima de MAX_DELIVERY_FEE.
        default_delivery_fee = _parse_fee(
            data.get("delivery_fee"), "delivery_fee"
        )

        raw_delivery_fees = data.get("delivery_fees") or {}
        if not isinstance(raw_delivery_fees, dict):
            raise ValueError("delivery_fees deve ser um objeto por loja")

        delivery_fees = {
            int(store_id): _parse_fee(
                value, f"delivery_fees.{store_id}"
            )
            for store_id, value in raw_delivery_fees.items()
        }
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

    # Serialize double-clicks/retries on the same customer's cart.
    db.session.query(User.id).filter_by(id=g.current_user.id).with_for_update().first()
    cart = get_or_create_cart(g.current_user.id)
    cart_items = list(cart.items)
    if not cart_items:
        return jsonify({"error": "carrinho vazio"}), 400
    if any(item.service_id for item in cart_items):
        return jsonify({
            "error": "serviços devem ser contratados pelo fluxo de agendamento"
        }), 409

    product_ids = sorted({item.product_id for item in cart_items if item.product_id})

    try:
        locked_products = db.session.query(Product).filter(
            Product.id.in_(product_ids)
        ).order_by(Product.id).with_for_update().all()
        products_map = {product.id: product for product in locked_products}

        items_by_store = {}
        for item in cart_items:
            product = products_map.get(item.product_id)
            if (not product or not product.active or not product.store
                    or not product.store.active or not product.store.accepts_orders
                    or product.stock < item.quantity):
                db.session.rollback()
                product_name = product.name if product else f"ID {item.product_id}"
                return jsonify({
                    "error": f"Produto indisponível ou estoque insuficiente: {product_name}"
                }), 409
            items_by_store.setdefault(product.store_id, []).append((item, product))

        unknown_fee_stores = set(delivery_fees) - set(items_by_store)
        if unknown_fee_stores:
            db.session.rollback()
            return jsonify({"error": "delivery_fees contém loja fora do carrinho"}), 400

        created_orders = []
        aggregate_subtotal = Decimal("0")
        aggregate_delivery_fee = Decimal("0")

        for store_id, grouped_items in items_by_store.items():
            store = grouped_items[0][1].store
            store_delivery_fee = delivery_fees.get(store_id, default_delivery_fee)
            order = Order(
                code=f"SW{uuid4().hex[:10].upper()}",
                customer_id=g.current_user.id,
                type="product",
                status=OrderStatus.PENDING,
                payment_status=PaymentStatus.PENDING,
                payment_method=payment_method,
                payment_release_at=(utcnow() + timedelta(minutes=current_app.config["ORDER_PAYMENT_MINUTES"])
                                    if payment_method in {"pix", "card_online"} else None),
                delivery_fee=store_delivery_fee,
                address=address,
                notes=data.get("notes"),
            )
            db.session.add(order)
            db.session.flush()

            subtotal = Decimal("0")
            commission_total = Decimal("0")
            for item, product in grouped_items:
                line_total = product.price * item.quantity
                subtotal += line_total
                commission_total += line_total * (store.commission_rate or Decimal("0"))
                order.items.append(OrderItem(
                    product_id=product.id,
                    item_name=product.name,
                    item_type="product",
                    quantity=item.quantity,
                    unit_price=product.price,
                    total_price=line_total,
                ))

                # adjust_stock cuida do ledger atomicamente.
                adjust_stock(
                    product, -item.quantity, "sale",
                    actor_id=None,
                    reason=f"Venda do pedido {order.code}",
                    reference_type="order",
                    reference_id=order.id,
                )
                db.session.delete(item)

            order.subtotal = subtotal
            order.total_amount = subtotal + store_delivery_fee
            order.commission_value = commission_total.quantize(Decimal("0.01"))
            db.session.add(Delivery(
                order_id=order.id,
                status=DeliveryStatus.AWAITING_DRIVER,
                pickup_address=store.address,
                delivery_address=address,
                delivery_fee=store_delivery_fee,
            ))
            db.session.add_all([
                Transaction(
                    order_id=order.id,
                    type=TransactionType.COMMISSION,
                    amount=order.commission_value,
                    beneficiary="platform",
                    status=TransactionStatus.PENDING,
                ),
                Transaction(
                    order_id=order.id,
                    type=TransactionType.STORE_RECEIVABLE,
                    amount=subtotal - order.commission_value,
                    beneficiary="store",
                    beneficiary_id=store.owner_id,
                    status=TransactionStatus.PENDING,
                ),
                Transaction(
                    order_id=order.id,
                    type=TransactionType.DELIVERY_FEE,
                    amount=store_delivery_fee,
                    beneficiary="driver",
                    status=TransactionStatus.PENDING,
                ),
            ])
            created_orders.append(order)
            aggregate_subtotal += subtotal
            aggregate_delivery_fee += store_delivery_fee

        db.session.commit()

        orders_payload = [order_summary_json(order) for order in created_orders]
        response = {
            "orders": orders_payload,
            "order_count": len(orders_payload),
            "subtotal": money(aggregate_subtotal),
            "delivery_fee": money(aggregate_delivery_fee),
            "total": money(aggregate_subtotal + aggregate_delivery_fee),
        }
        if len(created_orders) == 1:
            order = created_orders[0]
            response.update({
                "id": order.id,
                "code": order.code,
                "status": order.status,
                "payment_status": order.payment_status,
                "payment_method": order.payment_method,
            })
        return jsonify(response), 201
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Falha ao processar checkout")
        return jsonify({
            "error": "Erro ao processar o checkout, tente novamente."
        }), 500


# ═══════════════════════════════════════════════════════════════
# ORDERS
# ═══════════════════════════════════════════════════════════════

@api_bp.get("/orders")
@require_auth
def my_orders():
    rows = Order.query.filter_by(customer_id=g.current_user.id).order_by(
        Order.created_at.desc()
    ).all()
    return jsonify([order_summary_json(x) for x in rows])


@api_bp.get("/orders/<int:order_id>")
@require_auth
def order_detail(order_id):
    order = db.get_or_404(Order, order_id)
    allowed = (order.customer_id == g.current_user.id
               or g.current_user.role == UserRole.ADMIN)
    if g.current_user.role == UserRole.MERCHANT:
        allowed = any(
            item.product and item.product.store.owner_id == g.current_user.id
            for item in order.items
        )
    elif g.current_user.role == UserRole.PROVIDER:
        allowed = order.provider_id == g.current_user.id
    elif g.current_user.role == UserRole.DRIVER:
        allowed = bool(order.delivery
                       and order.delivery.driver_id == g.current_user.id)
    if not allowed:
        return jsonify({"error": "acesso negado"}), 403
    return jsonify({
        **order_summary_json(order),
        "customer_id": order.customer_id,
        "items": [
            {
                "id": item.id,
                "product_id": item.product_id,
                "service_id": item.service_id,
                "name": item.item_name,
                "type": item.item_type,
                "quantity": item.quantity,
                "unit_price": money(item.unit_price),
                "total_price": money(item.total_price),
            } for item in order.items
        ],
    })


# ═══════════════════════════════════════════════════════════════
# APPOINTMENTS (CLIENTE)
# ═══════════════════════════════════════════════════════════════

@api_bp.post("/appointments")
@require_roles(UserRole.CUSTOMER)
def create_appointment():
    data = request.get_json(silent=True) or {}
    service_id = data.get("service_id") or data.get("serviceId")
    scheduled_at = data.get("scheduled_at")
    if not scheduled_at and data.get("date") and data.get("time"):
        scheduled_at = f"{data['date']}T{data['time']}:00"
    if not service_id or not scheduled_at:
        return jsonify({"error": "service_id e scheduled_at são obrigatórios"}), 400

    try:
        service_id = int(service_id)
    except (TypeError, ValueError):
        return jsonify({"error": "service_id inválido"}), 400

    service = db.session.get(Service, service_id)
    store_unavailable = (service and service.store
                         and not service.store.accepts_appointments)
    if (not service or not service.active or not service.provider
            or not service.provider.active or store_unavailable):
        return jsonify({"error": "serviço indisponível para agendamento"}), 409

    try:
        when = parse_iso_datetime(scheduled_at)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    now_utc = utcnow()
    if when <= now_utc:
        return jsonify({"error": "o horário precisa ser futuro"}), 400

    # Serializa reservas concorrentes para o mesmo profissional.
    # Postgres: FOR UPDATE. SQLite: no-op (dev).
    db.session.query(User.id).filter_by(
        id=service.provider_id
    ).with_for_update().first()

    duration = service.duration_minutes or 30
    requested_end = when + timedelta(minutes=duration)
    candidates = Appointment.query.filter_by(
        provider_id=service.provider_id
    ).filter(
        Appointment.status.notin_([
            AppointmentStatus.CANCELLED,
            AppointmentStatus.COMPLETED,
        ]),
        Appointment.scheduled_at < requested_end,
    ).all()
    for existing in candidates:
        existing_duration = existing.service.duration_minutes or 30
        existing_end = existing.scheduled_at + timedelta(minutes=existing_duration)
        if existing_end > when:
            return jsonify({"error": "profissional indisponível nesse intervalo"}), 409

    appt = Appointment(
        customer_id=g.current_user.id,
        service_id=service.id,
        provider_id=service.provider_id,
        scheduled_at=when,
        notes=data.get("notes"),
    )
    db.session.add(appt)
    db.session.commit()

    return jsonify({
        "id": appt.id,
        "status": appt.status,
        "scheduled_at": iso_utc(appt.scheduled_at),
        "service": service_json(service),
    }), 201


@api_bp.get("/appointments")
@require_roles(UserRole.CUSTOMER)
def my_appointments():
    rows = Appointment.query.filter_by(customer_id=g.current_user.id).order_by(
        Appointment.scheduled_at.asc()
    ).all()
    return jsonify([{
        "id": x.id,
        "status": x.status,
        "scheduled_at": iso_utc(x.scheduled_at),
        "notes": x.notes,
        "service": service_json(x.service),
    } for x in rows])


# ═══════════════════════════════════════════════════════════════
# MERCHANT — STORES / PRODUTOS / INVENTÁRIO
# ═══════════════════════════════════════════════════════════════

@api_bp.post("/merchant/stores")
@require_roles(UserRole.MERCHANT, UserRole.ADMIN)
def create_store():
    data = request.get_json(silent=True) or {}
    if not all(str(data.get(k, "")).strip() for k in ("name", "category_id")):
        return jsonify({"error": "name e category_id são obrigatórios"}), 400
    try:
        category_id = int(data["category_id"])
        owner_id = (
            int(data.get("owner_id", g.current_user.id))
            if g.current_user.role == UserRole.ADMIN
            else g.current_user.id
        )
    except (TypeError, ValueError):
        return jsonify({"error": "category_id ou owner_id inválido"}), 400
    category = db.session.get(Category, category_id)
    if not category:
        return jsonify({"error": "categoria não encontrada"}), 404
    owner = db.session.get(User, owner_id) if owner_id else None
    if not owner or owner.role not in {UserRole.MERCHANT, UserRole.ADMIN}:
        return jsonify({"error": "proprietário inválido para a loja"}), 409
    store = Store(
        owner_id=owner.id,
        category_id=category.id,
        name=data["name"].strip(),
        slug=data.get("slug"),
        description=data.get("description"),
        image_url=data.get("image_url"),
        address=data.get("address"),
        city=data.get("city"),
        state=data.get("state"),
        zip_code=data.get("zip_code"),
        accepts_orders=as_bool(data.get("accepts_orders"), True),
        accepts_appointments=as_bool(data.get("accepts_appointments"), False),
        accepts_services=as_bool(data.get("accepts_services"), False),
    )
    db.session.add(store)
    db.session.commit()
    return jsonify(store_json(store)), 201


@api_bp.get("/merchant/dashboard")
@api_bp.get("/dashboard/merchant")
@require_roles(UserRole.MERCHANT, UserRole.ADMIN)
def merchant_dashboard():
    if g.current_user.role != UserRole.ADMIN:
        stores = Store.query.filter_by(owner_id=g.current_user.id).all()
    else:
        stores = Store.query.all()
    allowed_store_ids = [store.id for store in stores]

    try:
        selected_store_id = (
            int(request.args["store_id"]) if request.args.get("store_id") else None
        )
        period_start, period_end, date_from, date_to = parse_period_args()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if selected_store_id and selected_store_id not in allowed_store_ids:
        return jsonify({"error": "loja inválida ou sem permissão"}), 403
    store_ids = [selected_store_id] if selected_store_id else allowed_store_ids

    # Agregação via SQL: evita carregar todo o catálogo em memória.
    if not store_ids:
        product_count = 0
        service_count = 0
        low_stock_count = 0
        inventory_value = Decimal("0")
    else:
        base = Product.query.filter(Product.store_id.in_(store_ids))
        product_count = base.count()
        service_count = Service.query.filter(
            Service.store_id.in_(store_ids)
        ).count()
        low_stock_count = base.filter(
            Product.active.is_(True),
            Product.stock <= Product.minimum_stock,
        ).count()
        inventory_value = db.session.query(
            func.coalesce(func.sum(Product.price * Product.stock), 0)
        ).filter(Product.store_id.in_(store_ids)).scalar() or Decimal("0")

    financial_rows = []
    if store_ids:
        financial_rows = db.session.query(OrderItem, Product, Store, Order).join(
            Product, OrderItem.product_id == Product.id
        ).join(Store, Product.store_id == Store.id).join(
            Order, OrderItem.order_id == Order.id
        ).filter(
            Store.id.in_(store_ids),
            Order.created_at.between(period_start, period_end),
            Order.status != OrderStatus.CANCELLED,
        ).all()

    order_ids = {order.id for _, _, _, order in financial_rows}
    gross_revenue = sum(
        (item.total_price for item, _, _, _ in financial_rows), Decimal("0")
    )
    platform_fees = sum(
        (item.total_price * (store.commission_rate or Decimal("0"))
         for item, _, store, _ in financial_rows),
        Decimal("0"),
    )
    net_revenue = gross_revenue - platform_fees
    paid_revenue = sum(
        (item.total_price for item, _, _, order in financial_rows
         if order.payment_status == PaymentStatus.PAID),
        Decimal("0"),
    )

    return jsonify({
        "stores": [store_json(store) for store in stores],
        "selected_store_id": selected_store_id,
        "period": {"date_from": date_from, "date_to": date_to},
        "product_count": product_count,
        "service_count": service_count,
        "low_stock_count": low_stock_count,
        "inventory_value": money(inventory_value),
        "orders_count": len(order_ids),
        "revenue": money(gross_revenue),
        "finance": {
            "gross_revenue": money(gross_revenue),
            "platform_fees": money(platform_fees),
            "net_revenue": money(net_revenue),
            "paid_revenue": money(paid_revenue),
            "average_ticket": (
                money(gross_revenue / len(order_ids)) if order_ids else 0
            ),
        },
    })


@api_bp.post("/merchant/stores/<int:store_id>/products")
@require_roles(UserRole.MERCHANT, UserRole.ADMIN)
def merchant_create_product(store_id):
    store, error = owned_store(store_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    required = ("name", "category_id", "price")
    if any(data.get(k) in (None, "") for k in required):
        return jsonify({"error": "name, category_id e price são obrigatórios"}), 400
    try:
        category_id = int(data["category_id"])
    except (TypeError, ValueError):
        return jsonify({"error": "category_id inválido"}), 400
    category = db.session.get(Category, category_id)
    if not category:
        return jsonify({"error": "categoria não encontrada"}), 404
    try:
        price = parse_decimal(data["price"], "price")
        stock = int(data.get("stock", 0))
        if stock < 0:
            raise ValueError("stock não pode ser negativo")
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        minimum_stock = int(data.get("minimum_stock", 5))
        if minimum_stock < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "minimum_stock deve ser inteiro não negativo"}), 400

    # stock inicial = 0; o valor real é aplicado via adjust_stock (ledger).
    product = Product(
        store_id=store.id,
        category_id=category.id,
        name=data["name"].strip(),
        description=data.get("description"),
        sku=data.get("sku"),
        price=price,
        stock=0,
        minimum_stock=minimum_stock,
        image_url=data.get("image_url"),
        active=True,
    )
    db.session.add(product)
    try:
        db.session.flush()
        if stock:
            adjust_stock(
                product, stock, "initial",
                actor_id=g.current_user.id,
                reason="Saldo inicial",
            )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "SKU já cadastrado"}), 409
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Erro ao criar produto")
        return jsonify({"error": "erro interno ao criar produto"}), 500
    return jsonify(product_json(product)), 201


@api_bp.get("/merchant/products")
@require_roles(UserRole.MERCHANT, UserRole.ADMIN)
def merchant_products():
    query = Product.query.join(Store, Product.store_id == Store.id)
    if g.current_user.role != UserRole.ADMIN:
        query = query.filter(Store.owner_id == g.current_user.id)
    if request.args.get("store_id"):
        try:
            query = query.filter(Product.store_id == int(request.args["store_id"]))
        except ValueError:
            return jsonify({"error": "store_id inválido"}), 400
    stock_status = str(request.args.get("stock_status", "")).lower()
    if stock_status == "low":
        query = query.filter(Product.stock > 0,
                             Product.stock <= Product.minimum_stock)
    elif stock_status == "out":
        query = query.filter(Product.stock <= 0)
    elif stock_status == "ok":
        query = query.filter(Product.stock > Product.minimum_stock)
    if request.args.get("q"):
        like = f"%{request.args['q'].strip()}%"
        query = query.filter(or_(Product.name.ilike(like),
                                 Product.sku.ilike(like)))
    return jsonify([
        product_json(product) | {"active": product.active}
        for product in query.order_by(Product.name).all()
    ])


@api_bp.get("/merchant/inventory")
@require_roles(UserRole.MERCHANT, UserRole.ADMIN)
def merchant_inventory():
    query = Product.query.join(Store, Product.store_id == Store.id)
    if g.current_user.role != UserRole.ADMIN:
        query = query.filter(Store.owner_id == g.current_user.id)
    if request.args.get("store_id"):
        try:
            query = query.filter(Product.store_id == int(request.args["store_id"]))
        except ValueError:
            return jsonify({"error": "store_id inválido"}), 400
    products = query.order_by(Product.stock.asc(), Product.name.asc()).all()
    inventory_value = sum(
        (product.price * product.stock for product in products), Decimal("0")
    )
    return jsonify({
        "summary": {
            "products": len(products),
            "units": sum(product.stock for product in products),
            "low_stock": sum(0 < product.stock <= product.minimum_stock
                             for product in products),
            "out_of_stock": sum(product.stock <= 0 for product in products),
            "inventory_value": money(inventory_value),
        },
        "items": [
            product_json(product) | {"active": product.active}
            for product in products
        ],
    })


@api_bp.get("/merchant/stock-movements")
@require_roles(UserRole.MERCHANT, UserRole.ADMIN)
def merchant_stock_movements():
    query = StockMovement.query.join(
        Product, StockMovement.product_id == Product.id
    ).join(Store, Product.store_id == Store.id)

    if g.current_user.role != UserRole.ADMIN:
        query = query.filter(Store.owner_id == g.current_user.id)

    if request.args.get("store_id"):
        try:
            query = query.filter(Product.store_id == int(request.args["store_id"]))
        except ValueError:
            return jsonify({"error": "store_id inválido"}), 400

    if request.args.get("product_id"):
        try:
            query = query.filter(
                StockMovement.product_id == int(request.args["product_id"])
            )
        except ValueError:
            return jsonify({"error": "product_id inválido"}), 400

    # Filtro por tipo (validado contra o conjunto fechado de tipos).
    movement_type = str(request.args.get("type", "")).strip().lower()
    if movement_type:
        if movement_type not in StockMovementType.ALL:
            valid = ", ".join(sorted(StockMovementType.ALL))
            return jsonify({
                "error": f"type inválido; use um de: {valid}"
            }), 400
        query = query.filter(StockMovement.type == movement_type)

    # Filtro por período opcional: aceita period=7d|30d|90d|all ou
    # date_from/date_to no formato AAAA-MM-DD. Sem parâmetros, sem filtro.
    if any(request.args.get(k) for k in ("period", "date_from", "date_to")):
        try:
            period_start, period_end, _, _ = parse_period_args(default_days=30)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        query = query.filter(
            StockMovement.created_at.between(period_start, period_end)
        )

    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 200)
    except ValueError:
        return jsonify({"error": "limit inválido"}), 400

    rows = query.order_by(StockMovement.created_at.desc()).limit(limit).all()
    return jsonify([stock_movement_json(row) for row in rows])

@api_bp.post("/merchant/products/<int:product_id>/movements")
@require_roles(UserRole.MERCHANT, UserRole.ADMIN)
def merchant_create_movement(product_id):
    """Lança um movimento pontual no ledger.

    Payload: {type, quantity, reason}
    - `quantity` sempre positivo; o sinal vem do tipo.
    - `reason` obrigatório para loss/internal_use/return_*.
    """
    product = (db.session.query(Product)
               .filter_by(id=product_id)
               .with_for_update()
               .first())
    if not product:
        return jsonify({"error": "produto não encontrado"}), 404
    if (g.current_user.role != UserRole.ADMIN
            and product.store.owner_id != g.current_user.id):
        return jsonify({"error": "acesso negado"}), 403

    data = request.get_json(silent=True) or {}
    movement_type = str(data.get("type") or "").strip().lower()
    reason = str(data.get("reason") or "").strip() or None

    if movement_type not in StockMovementType.MANUAL_TYPES:
        valid = ", ".join(sorted(StockMovementType.MANUAL_TYPES))
        return jsonify({"error": f"type deve ser um de: {valid}"}), 400

    if movement_type in StockMovementType.REQUIRE_REASON and not reason:
        return jsonify({
            "error": f"reason é obrigatório para type={movement_type}"
        }), 400

    try:
        qty = parse_decimal(
            data.get("quantity"), "quantity", minimum=Decimal("0.01")
        )
        if qty != qty.to_integral_value():
            raise ValueError("quantity deve ser inteiro")
        qty_int = int(qty)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    direction = StockMovementType.DIRECTIONS[movement_type]
    signed = qty_int * direction

    try:
        adjust_stock(
            product, signed, movement_type,
            actor_id=g.current_user.id,
            reason=reason,
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 409

    return jsonify({
        "product_id": product.id,
        "type": movement_type,
        "quantity": signed,
        "stock_after": product.stock,
    }), 201


@api_bp.post("/merchant/inventory/count")
@require_roles(UserRole.MERCHANT, UserRole.ADMIN)
def merchant_inventory_count():
    """Contagem em lote. Recebe [{product_id, counted_stock, reason?}].

    Aplica `adjustment` apenas nos produtos divergentes. Devolve o diff.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, list) or not data:
        return jsonify({
            "error": "esperado array não-vazio de {product_id, counted_stock}"
        }), 400
    if len(data) > 500:
        return jsonify({"error": "máximo de 500 itens por chamada"}), 400

    try:
        ids = [int(entry["product_id"]) for entry in data]
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "product_id inválido em algum item"}), 400

    products = (db.session.query(Product)
                .filter(Product.id.in_(ids))
                .with_for_update()
                .all())
    by_id = {p.id: p for p in products}

    for pid in ids:
        product = by_id.get(pid)
        if not product:
            return jsonify({"error": f"produto {pid} não encontrado"}), 404
        if (g.current_user.role != UserRole.ADMIN
                and product.store.owner_id != g.current_user.id):
            return jsonify({
                "error": f"produto {pid} não pertence à sua loja"
            }), 403

    diverged = []
    try:
        for entry in data:
            pid = int(entry["product_id"])
            product = by_id[pid]
            counted = parse_decimal(
                entry.get("counted_stock"), "counted_stock"
            )
            if counted != counted.to_integral_value():
                return jsonify({
                    "error": f"counted_stock inválido para produto {pid}"
                }), 400
            counted_int = int(counted)
            delta = counted_int - product.stock
            if delta == 0:
                continue
            adjust_stock(
                product, delta, StockMovementType.ADJUSTMENT,
                actor_id=g.current_user.id,
                reason=str(
                    entry.get("reason") or "Contagem de inventário"
                ).strip(),
            )
            diverged.append({
                "product_id": product.id,
                "name": product.name,
                "before": counted_int - delta,
                "after": counted_int,
                "delta": delta,
            })
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 409

    return jsonify({
        "counted": len(data),
        "diverged": len(diverged),
        "details": diverged,
    })


@api_bp.get("/merchant/inventory/alerts")
@require_roles(UserRole.MERCHANT, UserRole.ADMIN)
def merchant_inventory_alerts():
    """Produtos que precisam de reposição, ordenados por gravidade."""
    query = Product.query.join(Store, Product.store_id == Store.id).filter(
        Product.active.is_(True)
    )
    if g.current_user.role != UserRole.ADMIN:
        query = query.filter(Store.owner_id == g.current_user.id)
    if request.args.get("store_id"):
        try:
            query = query.filter(
                Product.store_id == int(request.args["store_id"])
            )
        except ValueError:
            return jsonify({"error": "store_id inválido"}), 400

    rows = query.order_by(Product.stock.asc()).all()
    return jsonify({
        "out_of_stock": [product_json(p) for p in rows if p.stock <= 0],
        "low_stock": [
            product_json(p) for p in rows
            if 0 < p.stock <= p.minimum_stock
        ],
        "total": sum(1 for p in rows if p.stock <= p.minimum_stock),
    })

@api_bp.patch("/merchant/products/<int:product_id>")
@require_roles(UserRole.MERCHANT, UserRole.ADMIN)
def merchant_update_product(product_id):
    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({"error": "produto não encontrado"}), 404
    if (g.current_user.role != UserRole.ADMIN
            and product.store.owner_id != g.current_user.id):
        return jsonify({"error": "acesso negado"}), 403
    data = request.get_json(silent=True) or {}
    if "name" in data:
        name = str(data["name"]).strip()
        if not name:
            return jsonify({"error": "nome não pode ser vazio"}), 400
        product.name = name
    if "price" in data:
        try:
            price = Decimal(str(data["price"]))
            if price < 0:
                raise ValueError
            product.price = price
        except Exception:
            return jsonify({"error": "preço inválido"}), 400
    if "stock" in data:
        try:
            target = int(data["stock"])
            if target < 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "estoque inválido"}), 400
        delta = target - product.stock
        if delta:
            try:
                adjust_stock(
                    product, delta, "manual_adjustment",
                    actor_id=g.current_user.id,
                    reason=data.get("stock_reason") or "Ajuste no cadastro",
                )
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 409
    for field in ("description", "sku", "image_url"):
        if field in data:
            setattr(product, field, data[field])
    if "active" in data:
        product.active = as_bool(data["active"])
    if "minimum_stock" in data:
        try:
            minimum_stock = int(data["minimum_stock"])
            if minimum_stock < 0:
                raise ValueError
            product.minimum_stock = minimum_stock
        except (TypeError, ValueError):
            return jsonify({"error": "minimum_stock inválido"}), 400
    if "category_id" in data:
        try:
            category_id = int(data["category_id"])
        except (TypeError, ValueError):
            return jsonify({"error": "category_id inválido"}), 400
        category = db.session.get(Category, category_id)
        if not category:
            return jsonify({"error": "categoria não encontrada"}), 404
        product.category_id = category.id
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "SKU já cadastrado"}), 409
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Erro ao atualizar produto")
        return jsonify({"error": "erro interno ao atualizar produto"}), 500
    return jsonify(product_json(product) | {"active": product.active})


@api_bp.patch("/merchant/products/<int:product_id>/stock")
@require_roles(UserRole.MERCHANT, UserRole.ADMIN)
def merchant_update_stock(product_id):
    product = (db.session.query(Product)
               .filter_by(id=product_id)
               .with_for_update()
               .first())
    if not product:
        return jsonify({"error": "produto não encontrado"}), 404
    if (g.current_user.role != UserRole.ADMIN
            and product.store.owner_id != g.current_user.id):
        return jsonify({"error": "acesso negado"}), 403
    data = request.get_json(silent=True) or {}
    try:
        target = (int(data["stock"]) if "stock" in data
                  else product.stock + int(data.get("delta", 0)))
    except (TypeError, ValueError):
        return jsonify({"error": "stock ou delta deve ser inteiro"}), 400
    if target < 0:
        return jsonify({"error": "estoque não pode ficar negativo"}), 409
    delta = target - product.stock
    if delta:
        adjust_stock(
            product, delta, "manual_adjustment",
            actor_id=g.current_user.id,
            reason=str(data.get("reason") or "Ajuste manual").strip(),
        )
    db.session.commit()
    return jsonify(product_json(product) | {"active": product.active})


@api_bp.delete("/merchant/products/<int:product_id>")
@require_roles(UserRole.MERCHANT, UserRole.ADMIN)
def merchant_archive_product(product_id):
    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({"error": "produto não encontrado"}), 404
    if (g.current_user.role != UserRole.ADMIN
            and product.store.owner_id != g.current_user.id):
        return jsonify({"error": "acesso negado"}), 403
    product.active = False
    db.session.commit()
    return jsonify({"id": product.id, "active": False,
                    "message": "produto arquivado"})


@api_bp.post("/merchant/stores/<int:store_id>/services")
@require_roles(UserRole.MERCHANT, UserRole.ADMIN)
def merchant_create_service(store_id):
    store, error = owned_store(store_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    required = ("name", "category_id", "price")
    if any(data.get(k) in (None, "") for k in required):
        return jsonify({"error": "name, category_id e price são obrigatórios"}), 400
    try:
        category_id = int(data["category_id"])
    except (TypeError, ValueError):
        return jsonify({"error": "category_id inválido"}), 400
    category = db.session.get(Category, category_id)
    if not category:
        return jsonify({"error": "categoria não encontrada"}), 404
    try:
        price = parse_decimal(data["price"], "price")
        duration = int(data.get("duration_minutes", 30))
        if duration <= 0:
            raise ValueError("duration_minutes deve ser positivo")
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

    service = Service(
        provider_id=store.owner_id,
        store_id=store.id,
        category_id=category.id,
        title=data["name"].strip(),
        description=data.get("description"),
        image_url=data.get("image_url"),
        pricing_model=ServicePricingModel.FIXED,
        duration_minutes=duration,
        price=price,
        modality=data.get("modality", ServiceModality.AT_SHOP),
        active=True,
    )
    db.session.add(service)
    store.accepts_appointments = True
    store.accepts_services = True
    db.session.commit()
    return jsonify(service_json(service)), 201


# ═══════════════════════════════════════════════════════════════
# MERCHANT — APPOINTMENTS
# ═══════════════════════════════════════════════════════════════

@api_bp.patch("/merchant/appointments/<int:appointment_id>")
@require_roles(UserRole.MERCHANT, UserRole.ADMIN)
def merchant_update_appointment(appointment_id):
    appointment = db.session.get(Appointment, appointment_id)
    if not appointment:
        return jsonify({"error": "agendamento não encontrado"}), 404
    if g.current_user.role != UserRole.ADMIN:
        store = appointment.service.store
        if not store or store.owner_id != g.current_user.id:
            return jsonify({"error": "acesso negado"}), 403

    status = str((request.get_json(silent=True) or {})
                 .get("status", "")).strip().lower()
    if status not in {
        AppointmentStatus.PENDING,
        AppointmentStatus.CONFIRMED,
        AppointmentStatus.COMPLETED,
        AppointmentStatus.CANCELLED,
    }:
        return jsonify({"error": "status inválido"}), 400
    if status == appointment.status:
        return jsonify({"id": appointment.id, "status": appointment.status})

    transitions = {
        AppointmentStatus.PENDING: {
            AppointmentStatus.CONFIRMED, AppointmentStatus.CANCELLED,
        },
        AppointmentStatus.CONFIRMED: {
            AppointmentStatus.COMPLETED, AppointmentStatus.CANCELLED,
        },
    }
    if status not in transitions.get(appointment.status, set()):
        return jsonify({"error": "transição de status inválida"}), 409

    appointment.status = status
    db.session.commit()
    return jsonify({"id": appointment.id, "status": appointment.status})


@api_bp.get("/merchant/appointments")
@require_roles(UserRole.MERCHANT, UserRole.ADMIN)
def merchant_appointments():
    query = Appointment.query.join(
        Service, Appointment.service_id == Service.id
    ).join(Store, Service.store_id == Store.id)
    if g.current_user.role != UserRole.ADMIN:
        query = query.filter(Store.owner_id == g.current_user.id)
    rows = query.order_by(Appointment.scheduled_at.asc()).all()
    return jsonify([{
        "id": x.id,
        "status": x.status,
        "scheduled_at": iso_utc(x.scheduled_at),
        "customer": user_json(x.customer),
        "service": service_json(x.service),
    } for x in rows])


# ═══════════════════════════════════════════════════════════════
# ADMIN
# ═══════════════════════════════════════════════════════════════

@api_bp.post("/admin/users")
@require_roles(UserRole.ADMIN)
def admin_create_user():
    data = request.get_json(silent=True) or {}
    if not all(str(data.get(k, "")).strip()
               for k in ("name", "email", "password", "role")):
        return jsonify({
            "error": "name, email, password e role são obrigatórios"
        }), 400
    role = str(data["role"]).strip().lower()
    allowed_roles = {
        UserRole.CUSTOMER, UserRole.MERCHANT, UserRole.DRIVER,
        UserRole.PROVIDER, UserRole.ADMIN,
    }
    if role not in allowed_roles:
        return jsonify({
            "error": "role deve ser customer, merchant, provider, driver ou admin"
        }), 400
    email = data["email"].strip().lower()

    user = User(
        name=data["name"].strip(),
        email=email,
        password_hash=generate_password_hash(data["password"]),
        phone=data.get("phone"),
        role=role,
    )
    db.session.add(user)
    try:
        db.session.flush()
        if role == UserRole.CUSTOMER:
            db.session.add(Cart(customer_id=user.id))
        elif role == UserRole.PROVIDER:
            db.session.add(ServiceProviderProfile(user_id=user.id))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "e-mail já cadastrado"}), 409
    return jsonify(user_json(user)), 201


@api_bp.post("/admin/drivers")
@require_roles(UserRole.ADMIN)
def admin_create_driver():
    data = request.get_json(silent=True) or {}
    required = (
        "name", "email", "password", "document",
        "license_number", "vehicle_type", "vehicle_plate",
    )
    if any(not str(data.get(key, "")).strip() for key in required):
        return jsonify({"error": f"campos obrigatórios: {', '.join(required)}"}), 400
    email = str(data["email"]).strip().lower()

    driver = User(
        name=str(data["name"]).strip(),
        email=email,
        password_hash=generate_password_hash(str(data["password"])),
        phone=data.get("phone"),
        role=UserRole.DRIVER,
    )
    db.session.add(driver)
    try:
        db.session.flush()
        db.session.add(DriverProfile(
            user_id=driver.id,
            document=str(data["document"]).strip(),
            license_number=str(data["license_number"]).strip(),
            vehicle_type=str(data["vehicle_type"]).strip().lower(),
            vehicle_plate=str(data["vehicle_plate"]).strip().upper(),
            approved=as_bool(data.get("approved"), True),
            available=as_bool(data.get("available"), False),
        ))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({
            "error": "e-mail, documento, CNH ou placa já cadastrados"
        }), 409
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Erro ao criar motorista")
        return jsonify({"error": "erro interno ao criar motorista"}), 500
    return jsonify({
        **user_json(driver),
        "driver_profile": driver_profile_json(driver.driver_profile),
    }), 201


@api_bp.get("/admin/dashboard")
@api_bp.get("/dashboard/admin")
@require_roles(UserRole.ADMIN)
def admin_dashboard():
    revenue = db.session.query(
        func.coalesce(func.sum(Order.total_amount), 0)
    ).filter(Order.payment_status == PaymentStatus.PAID).scalar()
    platform_revenue = db.session.query(
        func.coalesce(func.sum(Order.commission_value), 0)
    ).filter(
        Order.payment_status == PaymentStatus.PAID,
        Order.status != OrderStatus.CANCELLED,
    ).scalar()
    users_by_role = dict(db.session.query(
        User.role, func.count(User.id)
    ).group_by(User.role).all())
    orders_by_status = dict(db.session.query(
        Order.status, func.count(Order.id)
    ).group_by(Order.status).all())
    deliveries_by_status = dict(db.session.query(
        Delivery.status, func.count(Delivery.id)
    ).group_by(Delivery.status).all())
    recent_orders = Order.query.order_by(Order.created_at.desc()).limit(10).all()
    return jsonify({
        "kpis": {
            "users": User.query.count(),
            "stores": Store.query.count(),
            "orders": Order.query.count(),
            "drivers": User.query.filter_by(role=UserRole.DRIVER).count(),
            "available_drivers": DriverProfile.query.filter_by(
                approved=True, available=True
            ).count(),
            "gross_revenue": money(revenue),
            "platform_revenue": money(platform_revenue),
        },
        "users_by_role": users_by_role,
        "orders_by_status": orders_by_status,
        "deliveries_by_status": deliveries_by_status,
        "recent_orders": [order_summary_json(order) for order in recent_orders],
    })


@api_bp.get("/admin/finance")
@require_roles(UserRole.ADMIN)
def admin_finance():
    try:
        period_start, period_end, date_from, date_to = parse_period_args()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    orders = Order.query.filter(
        Order.created_at.between(period_start, period_end),
        Order.status != OrderStatus.CANCELLED,
    ).all()
    paid_orders = [o for o in orders if o.payment_status == PaymentStatus.PAID]
    pending_orders = [o for o in orders if o.payment_status == PaymentStatus.PENDING]
    gmv = sum((o.total_amount for o in paid_orders), Decimal("0"))
    commissions = sum((o.commission_value for o in paid_orders), Decimal("0"))
    delivery_fees = sum((o.delivery_fee for o in paid_orders), Decimal("0"))
    pending_receivables = sum((o.total_amount for o in pending_orders),
                              Decimal("0"))

    transaction_rows = Transaction.query.filter(
        Transaction.created_at.between(period_start, period_end)
    ).all()
    settled = sum((t.amount for t in transaction_rows
                   if t.status == TransactionStatus.SETTLED), Decimal("0"))
    open_amount = sum((t.amount for t in transaction_rows
                       if t.status == TransactionStatus.PENDING), Decimal("0"))

    return jsonify({
        "period": {"date_from": date_from, "date_to": date_to},
        "orders": {
            "total": len(orders),
            "paid": len(paid_orders),
            "pending": len(pending_orders),
        },
        "gmv": money(gmv),
        "gross_revenue": money(gmv),
        "platform_revenue": money(commissions),
        "delivery_fees": money(delivery_fees),
        "pending_receivables": money(pending_receivables),
        "transactions": {
            "count": len(transaction_rows),
            "settled_amount": money(settled),
            "open_amount": money(open_amount),
        },
    })


@api_bp.get("/admin/transactions")
@require_roles(UserRole.ADMIN)
def admin_transactions():
    query = Transaction.query
    status = str(request.args.get("status", "")).strip().lower()
    beneficiary = str(request.args.get("beneficiary", "")).strip().lower()
    transaction_type = str(request.args.get("type", "")).strip().lower()
    if status:
        query = query.filter(Transaction.status == status)
    if beneficiary:
        query = query.filter(Transaction.beneficiary == beneficiary)
    if transaction_type:
        query = query.filter(Transaction.type == transaction_type)
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 500)
    except (TypeError, ValueError):
        return jsonify({"error": "limit inválido"}), 400
    rows = query.order_by(Transaction.created_at.desc()).limit(limit).all()
    return jsonify([transaction_json(t) for t in rows])


@api_bp.patch("/admin/transactions/<int:transaction_id>/settle")
@require_roles(UserRole.ADMIN)
def admin_settle_transaction(transaction_id):
    # Early-out antes de qualquer IO: em modo gateway a liquidação manual
    # é proibida (deve existir comprovante externo).
    if current_app.config.get("ASAAS_ENABLED"):
        return jsonify({
            "error": ("liquidação manual indisponível: "
                      "comprove transferência externa")
        }), 409

    transaction = (db.session.query(Transaction)
                   .filter_by(id=transaction_id)
                   .with_for_update()
                   .first())
    if not transaction:
        return jsonify({"error": "transação não encontrada"}), 404
    if transaction.status == TransactionStatus.SETTLED:
        return jsonify(transaction_json(transaction))
    if transaction.status != TransactionStatus.PENDING:
        return jsonify({
            "error": "somente transações pendentes podem ser liquidadas"
        }), 409
    if transaction.order and transaction.order.payment_status != PaymentStatus.PAID:
        return jsonify({"error": "liquidação exige pagamento confirmado"}), 409
    if transaction.beneficiary == "driver":
        delivery = transaction.order.delivery if transaction.order else None
        if not delivery or delivery.status != DeliveryStatus.DELIVERED:
            return jsonify({
                "error": "taxa de entrega só pode ser liquidada após a entrega"
            }), 409
    transaction.status = TransactionStatus.SETTLED
    transaction.settled_at = utcnow()
    db.session.commit()
    return jsonify(transaction_json(transaction))


@api_bp.get("/admin/users")
@require_roles(UserRole.ADMIN)
def admin_users():
    role = request.args.get("role")
    query = User.query
    if role:
        query = query.filter_by(role=role.strip().lower())
    return jsonify([
        user_json(user) for user in query.order_by(User.created_at.desc()).all()
    ])


@api_bp.get("/admin/stores")
@require_roles(UserRole.ADMIN)
def admin_stores():
    rows = Store.query.order_by(Store.created_at.desc()).all()
    return jsonify([
        {
            **store_json(store),
            "owner": user_json(store.owner),
            "active": store.active,
            "address": store.address,
            "zip_code": store.zip_code,
        }
        for store in rows
    ])


@api_bp.patch("/admin/stores/<int:store_id>")
@require_roles(UserRole.ADMIN)
def admin_update_store(store_id):
    store = db.session.get(Store, store_id)
    if not store:
        return jsonify({"error": "loja não encontrada"}), 404
    data = request.get_json(silent=True) or {}
    for field in ("active", "accepts_orders",
                  "accepts_appointments", "accepts_services"):
        if field in data:
            setattr(store, field, as_bool(data[field]))
    if "name" in data:
        name = str(data["name"]).strip()
        if not name:
            return jsonify({"error": "name não pode ser vazio"}), 400
        store.name = name
    for field in ("description", "address", "city", "state",
                  "zip_code", "image_url"):
        if field in data:
            setattr(store, field, str(data[field]).strip() or None)
    db.session.commit()
    return jsonify({
        **store_json(store),
        "owner": user_json(store.owner),
        "active": store.active,
    })


@api_bp.get("/admin/orders")
@require_roles(UserRole.ADMIN)
def admin_orders():
    status = request.args.get("status")
    query = Order.query
    if status:
        query = query.filter_by(status=status.strip().lower())
    return jsonify([
        order_summary_json(order)
        for order in query.order_by(Order.created_at.desc()).all()
    ])


@api_bp.get("/admin/deliveries")
@require_roles(UserRole.ADMIN)
def admin_deliveries():
    status = request.args.get("status")
    query = Delivery.query
    if status:
        query = query.filter_by(status=status.strip().lower())
    return jsonify([
        delivery_json(delivery)
        for delivery in query.order_by(Delivery.created_at.desc()).all()
    ])


@api_bp.get("/admin/drivers")
@require_roles(UserRole.ADMIN)
def admin_drivers():
    rows = User.query.filter_by(role=UserRole.DRIVER).order_by(User.name).all()
    return jsonify([
        {
            **user_json(user),
            "driver_profile": (
                driver_profile_json(user.driver_profile)
                if user.driver_profile else None
            ),
        }
        for user in rows
    ])


@api_bp.patch("/admin/drivers/<int:user_id>")
@require_roles(UserRole.ADMIN)
def admin_update_driver(user_id):
    driver = db.session.get(User, user_id)
    if not driver or driver.role != UserRole.DRIVER or not driver.driver_profile:
        return jsonify({"error": "motorista não encontrado"}), 404
    data = request.get_json(silent=True) or {}
    for field in ("approved", "available"):
        if field in data:
            setattr(driver.driver_profile, field, as_bool(data[field]))
    if "active" in data:
        driver.active = as_bool(data["active"])
    db.session.commit()
    return jsonify({
        **user_json(driver),
        "driver_profile": driver_profile_json(driver.driver_profile),
    })


@api_bp.post("/admin/deliveries/<int:delivery_id>/assign")
@require_roles(UserRole.ADMIN)
def admin_assign_delivery(delivery_id):
    delivery = db.session.get(Delivery, delivery_id)
    if not delivery:
        return jsonify({"error": "entrega não encontrada"}), 404

    # Bloqueio de gateway SÓ depois do get(delivery) para evitar None.order.
    if (current_app.config.get("ASAAS_ENABLED")
            and delivery.order.payment_method in {"pix", "card_online"}
            and delivery.order.payment_status != PaymentStatus.PAID):
        return jsonify({"error": "pedido ainda não pago"}), 409

    driver_id = (request.get_json(silent=True) or {}).get("driver_id")
    driver = db.session.get(User, driver_id) if driver_id else None
    if (not driver or driver.role != UserRole.DRIVER or not driver.active
            or not driver.driver_profile
            or not driver.driver_profile.approved):
        return jsonify({"error": "motorista inválido ou não aprovado"}), 409
    if delivery.status not in {DeliveryStatus.AWAITING_DRIVER,
                               DeliveryStatus.ASSIGNED}:
        return jsonify({"error": "entrega não pode mais ser atribuída"}), 409
    delivery.driver = driver
    delivery.status = DeliveryStatus.ASSIGNED
    Transaction.query.filter_by(
        order_id=delivery.order_id,
        type=TransactionType.DELIVERY_FEE,
        status=TransactionStatus.PENDING,
    ).update({"beneficiary_id": driver.id}, synchronize_session=False)
    db.session.commit()
    return jsonify(delivery_json(delivery))


# ═══════════════════════════════════════════════════════════════
# DRIVER
# ═══════════════════════════════════════════════════════════════

@api_bp.get("/driver/dashboard")
@require_roles(UserRole.DRIVER)
def driver_dashboard():
    profile = g.current_user.driver_profile
    if not profile:
        return jsonify({"error": "perfil de motorista não cadastrado"}), 409
    try:
        period_start, period_end, date_from, date_to = parse_period_args()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    active = Delivery.query.filter_by(driver_id=g.current_user.id).filter(
        Delivery.status.in_([
            DeliveryStatus.ASSIGNED,
            DeliveryStatus.ACCEPTED,
            DeliveryStatus.PICKED_UP,
        ])
    ).all()
    available = Delivery.query.join(Order).filter(
        Delivery.driver_id.is_(None), Delivery.status == DeliveryStatus.AWAITING_DRIVER,
        Order.status != OrderStatus.CANCELLED,
        or_(Order.payment_method.in_(["cash", "card_on_delivery"]),
            Order.payment_status == PaymentStatus.PAID),
    ).order_by(Delivery.created_at).limit(100).all()
    completed_query = Delivery.query.filter_by(
        driver_id=g.current_user.id, status=DeliveryStatus.DELIVERED
    )
    period_deliveries = completed_query.filter(
        Delivery.delivered_at.between(period_start, period_end)
    ).order_by(Delivery.delivered_at.desc()).all()

    today_start = datetime.combine(datetime.now(timezone.utc).date(), time.min)
    today_end = datetime.combine(datetime.now(timezone.utc).date(), time.max)
    earnings_total = db.session.query(
        func.coalesce(func.sum(Delivery.delivery_fee), 0)
    ).filter(
        Delivery.driver_id == g.current_user.id,
        Delivery.status == DeliveryStatus.DELIVERED,
    ).scalar() or Decimal("0")
    earnings_today = db.session.query(
        func.coalesce(func.sum(Delivery.delivery_fee), 0)
    ).filter(
        Delivery.driver_id == g.current_user.id,
        Delivery.status == DeliveryStatus.DELIVERED,
        Delivery.delivered_at.between(today_start, today_end),
    ).scalar() or Decimal("0")
    earnings_period = sum(
        (delivery.delivery_fee for delivery in period_deliveries), Decimal("0")
    )

    return jsonify({
        "profile": driver_profile_json(profile),
        "period": {"date_from": date_from, "date_to": date_to},
        "active_deliveries": [
            delivery_json(
                delivery,
                expose_addresses=delivery.status in {
                    DeliveryStatus.ACCEPTED, DeliveryStatus.PICKED_UP,
                },
            )
            for delivery in active
        ],
        "available_deliveries": (
            [delivery_json(d, expose_addresses=False) for d in available]
            if profile.approved and profile.available else []
        ),
        "completed_deliveries": completed_query.count(),
        "history": [delivery_json(delivery) for delivery in period_deliveries[:50]],
        "earnings": {
            "today": money(earnings_today),
            "period": money(earnings_period),
            "total": money(earnings_total),
            "completed_in_period": len(period_deliveries),
            "average_per_delivery": (
                money(earnings_period / len(period_deliveries))
                if period_deliveries else 0
            ),
        },
    })


@api_bp.get("/driver/deliveries/history")
@require_roles(UserRole.DRIVER)
def driver_delivery_history():
    try:
        period_start, period_end, date_from, date_to = parse_period_args(
            default_days=90
        )
        limit = min(max(int(request.args.get("limit", 100)), 1), 200)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    rows = Delivery.query.filter(
        Delivery.driver_id == g.current_user.id,
        Delivery.status == DeliveryStatus.DELIVERED,
        Delivery.delivered_at.between(period_start, period_end),
    ).order_by(Delivery.delivered_at.desc()).limit(limit).all()
    total = sum((delivery.delivery_fee for delivery in rows), Decimal("0"))
    return jsonify({
        "period": {"date_from": date_from, "date_to": date_to},
        "total_earnings": money(total),
        "count": len(rows),
        "items": [delivery_json(delivery) for delivery in rows],
    })


@api_bp.patch("/driver/status")
@require_roles(UserRole.DRIVER)
def driver_status():
    profile = g.current_user.driver_profile
    if not profile or not profile.approved:
        return jsonify({"error": "motorista não aprovado"}), 403
    data = request.get_json(silent=True) or {}
    if "available" in data:
        profile.available = as_bool(data["available"])

    ranges = {
        "latitude":  (Decimal("-90"),  Decimal("90"),  "current_latitude"),
        "longitude": (Decimal("-180"), Decimal("180"), "current_longitude"),
    }
    for field, (low, high, target) in ranges.items():
        if field not in data:
            continue
        try:
            value = parse_decimal(data[field], field, minimum=low)
        except ValueError:
            return jsonify({"error": f"{field} inválida"}), 400
        if value > high:
            return jsonify({"error": f"{field} fora do intervalo permitido"}), 400
        setattr(profile, target, value)

    db.session.commit()
    return jsonify(driver_profile_json(profile))


@api_bp.post("/driver/deliveries/<int:delivery_id>/accept")
@require_roles(UserRole.DRIVER)
def driver_accept_delivery(delivery_id):
    profile = g.current_user.driver_profile
    if not profile or not profile.approved or not profile.available:
        return jsonify({
            "error": "motorista precisa estar aprovado e disponível"
        }), 403
    delivery = (db.session.query(Delivery)
                .filter_by(id=delivery_id)
                .with_for_update()
                .first())
    if not delivery:
        return jsonify({"error": "entrega não encontrada"}), 404
    if (current_app.config.get("ASAAS_ENABLED")
            and delivery.order.payment_method in {"pix", "card_online"}
            and delivery.order.payment_status != PaymentStatus.PAID):
        return jsonify({"error": "pedido ainda não pago"}), 409
    if delivery.driver_id not in (None, g.current_user.id) \
            or delivery.status not in {DeliveryStatus.AWAITING_DRIVER,
                                       DeliveryStatus.ASSIGNED}:
        return jsonify({"error": "entrega indisponível"}), 409
    delivery.driver_id = g.current_user.id
    delivery.status = DeliveryStatus.ACCEPTED
    delivery.accepted_at = utcnow()
    profile.available = False
    Transaction.query.filter_by(
        order_id=delivery.order_id,
        type=TransactionType.DELIVERY_FEE,
        status=TransactionStatus.PENDING,
    ).update({"beneficiary_id": g.current_user.id},
             synchronize_session=False)
    db.session.commit()
    return jsonify(delivery_json(delivery))


@api_bp.patch("/driver/deliveries/<int:delivery_id>")
@require_roles(UserRole.DRIVER)
def driver_update_delivery(delivery_id):
    delivery = db.session.get(Delivery, delivery_id)
    if not delivery or delivery.driver_id != g.current_user.id:
        return jsonify({"error": "entrega não encontrada"}), 404
    new_status = str((request.get_json(silent=True) or {})
                     .get("status", "")).lower()
    transitions = {
        DeliveryStatus.ACCEPTED: DeliveryStatus.PICKED_UP,
        DeliveryStatus.PICKED_UP: DeliveryStatus.DELIVERED,
    }
    if transitions.get(delivery.status) != new_status:
        return jsonify({"error": "transição de status inválida"}), 409
    delivery.status = new_status
    now = utcnow()
    if new_status == DeliveryStatus.PICKED_UP:
        delivery.picked_up_at = now
        delivery.order.status = OrderStatus.OUT_FOR_DELIVERY
    else:
        delivery.delivered_at = now
        delivery.order.status = OrderStatus.DELIVERED
        g.current_user.driver_profile.available = True
    db.session.commit()
    return jsonify(delivery_json(delivery))


# ═══════════════════════════════════════════════════════════════
# PROVIDER (autônomo)
# ═══════════════════════════════════════════════════════════════

@api_bp.get("/provider/profile")
@require_roles(UserRole.PROVIDER)
def provider_profile():
    profile = g.current_user.provider_profile
    if not profile:
        profile = ServiceProviderProfile(user_id=g.current_user.id)
        db.session.add(profile)
        db.session.commit()
    return jsonify(provider_profile_json(profile))


@api_bp.patch("/provider/profile")
@require_roles(UserRole.PROVIDER)
def provider_update_profile():
    profile = g.current_user.provider_profile
    if not profile:
        profile = ServiceProviderProfile(user_id=g.current_user.id)
        db.session.add(profile)
    data = request.get_json(silent=True) or {}
    for field in ("headline", "bio", "city", "state"):
        if field in data:
            setattr(profile, field, str(data[field]).strip() or None)
    if "years_experience" in data:
        try:
            years = int(data["years_experience"])
            if years < 0:
                raise ValueError
            profile.years_experience = years
        except (TypeError, ValueError):
            return jsonify({"error": "years_experience inválido"}), 400
    if "service_radius_km" in data:
        try:
            profile.service_radius_km = parse_decimal(
                data["service_radius_km"], "service_radius_km"
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    for field in ("offers_remote", "offers_in_person", "is_available"):
        if field in data:
            setattr(profile, field, as_bool(data[field]))
    db.session.commit()
    return jsonify(provider_profile_json(profile))


@api_bp.get("/provider/dashboard")
@require_roles(UserRole.PROVIDER)
def provider_dashboard():
    services_count = Service.query.filter_by(
        provider_id=g.current_user.id, active=True
    ).count()
    appointments_query = Appointment.query.filter_by(provider_id=g.current_user.id)
    pending = appointments_query.filter(
        Appointment.status.in_([
            AppointmentStatus.PENDING, AppointmentStatus.CONFIRMED,
        ])
    ).count()
    completed = appointments_query.filter_by(
        status=AppointmentStatus.COMPLETED
    ).count()
    return jsonify({
        "profile": (
            provider_profile_json(g.current_user.provider_profile)
            if g.current_user.provider_profile else None
        ),
        "services_count": services_count,
        "pending_appointments": pending,
        "completed_appointments": completed,
    })


@api_bp.get("/provider/services")
@require_roles(UserRole.PROVIDER)
def provider_services():
    rows = Service.query.filter_by(provider_id=g.current_user.id).order_by(
        Service.title
    ).all()
    return jsonify([service_json(service) for service in rows])


@api_bp.post("/provider/services")
@require_roles(UserRole.PROVIDER)
def provider_create_service():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or data.get("name") or "").strip()
    try:
        category_id = int(data["category_id"])
    except (KeyError, TypeError, ValueError):
        category_id = None
    category = db.session.get(Category, category_id) if category_id else None
    pricing_model = str(
        data.get("pricing_model", ServicePricingModel.FIXED)
    ).strip().lower()
    if not title or not category:
        return jsonify({"error": "title/name e category_id são obrigatórios"}), 400
    if pricing_model not in {
        ServicePricingModel.FIXED,
        ServicePricingModel.HOURLY,
        ServicePricingModel.ON_QUOTE,
    }:
        return jsonify({"error": "pricing_model inválido"}), 400
    try:
        price = (parse_decimal(data["price"], "price")
                 if data.get("price") is not None else None)
        hourly_rate = (parse_decimal(data["hourly_rate"], "hourly_rate")
                       if data.get("hourly_rate") is not None else None)
        visit_fee = parse_decimal(data.get("visit_fee", 0), "visit_fee")
        duration = int(data.get("duration_minutes", 60))
        if duration <= 0:
            raise ValueError("duration_minutes deve ser positivo")
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    if pricing_model == ServicePricingModel.FIXED and price is None:
        return jsonify({
            "error": "price é obrigatório para serviço de preço fixo"
        }), 400
    if pricing_model == ServicePricingModel.HOURLY and hourly_rate is None:
        return jsonify({
            "error": "hourly_rate é obrigatório para serviço por hora"
        }), 400

    service = Service(
        provider_id=g.current_user.id,
        category_id=category.id,
        title=title,
        description=data.get("description"),
        image_url=data.get("image_url"),
        pricing_model=pricing_model,
        price=price,
        hourly_rate=hourly_rate,
        visit_fee=visit_fee,
        duration_minutes=duration,
        modality=data.get("modality", ServiceModality.IN_PERSON),
        active=True,
    )
    db.session.add(service)
    db.session.commit()
    return jsonify(service_json(service)), 201


@api_bp.patch("/provider/services/<int:service_id>")
@require_roles(UserRole.PROVIDER)
def provider_update_service(service_id):
    service = Service.query.filter_by(
        id=service_id, provider_id=g.current_user.id
    ).first()
    if not service:
        return jsonify({"error": "serviço não encontrado"}), 404
    data = request.get_json(silent=True) or {}
    if "title" in data or "name" in data:
        title = str(data.get("title") or data.get("name") or "").strip()
        if not title:
            return jsonify({"error": "título não pode ser vazio"}), 400
        service.title = title
    for field in ("description", "image_url", "modality"):
        if field in data:
            setattr(service, field, data[field])
    for field in ("price", "hourly_rate", "visit_fee"):
        if field in data:
            try:
                setattr(service, field, parse_decimal(data[field], field))
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
    if "active" in data:
        service.active = as_bool(data["active"])
    db.session.commit()
    return jsonify(service_json(service))


@api_bp.get("/provider/appointments")
@require_roles(UserRole.PROVIDER)
def provider_appointments():
    rows = Appointment.query.filter_by(
        provider_id=g.current_user.id
    ).order_by(Appointment.scheduled_at).all()
    return jsonify([{
        "id": appointment.id,
        "status": appointment.status,
        "scheduled_at": iso_utc(appointment.scheduled_at),
        "notes": appointment.notes,
        "customer": user_json(appointment.customer),
        "service": service_json(appointment.service),
    } for appointment in rows])


@api_bp.patch("/provider/appointments/<int:appointment_id>")
@require_roles(UserRole.PROVIDER)
def provider_update_appointment(appointment_id):
    appointment = Appointment.query.filter_by(
        id=appointment_id, provider_id=g.current_user.id
    ).first()
    if not appointment:
        return jsonify({"error": "agendamento não encontrado"}), 404
    status = str((request.get_json(silent=True) or {})
                 .get("status", "")).strip().lower()
    if status not in {
        AppointmentStatus.CONFIRMED,
        AppointmentStatus.COMPLETED,
        AppointmentStatus.CANCELLED,
    }:
        return jsonify({"error": "status inválido"}), 400
    if status == appointment.status:
        return jsonify({"id": appointment.id, "status": appointment.status})
    transitions = {
        AppointmentStatus.PENDING: {
            AppointmentStatus.CONFIRMED, AppointmentStatus.CANCELLED,
        },
        AppointmentStatus.CONFIRMED: {
            AppointmentStatus.COMPLETED, AppointmentStatus.CANCELLED,
        },
    }
    if status not in transitions.get(appointment.status, set()):
        return jsonify({"error": "transição de status inválida"}), 409
    appointment.status = status
    if status == AppointmentStatus.COMPLETED \
            and g.current_user.provider_profile:
        g.current_user.provider_profile.completed_jobs += 1
    db.session.commit()
    return jsonify({"id": appointment.id, "status": appointment.status})



# ═══════════════════════════════════════════════════════════════
# BIBLIOTECA DE MÍDIA — URLs centralizadas e reutilizáveis
# ═══════════════════════════════════════════════════════════════

def _media_slug(value):
    import re
    import unicodedata
    value = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-")[:180] or "imagem"


def _media_url_is_valid(value):
    from urllib.parse import urlparse
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme == "https" and bool(parsed.netloc) and not any(c.isspace() for c in str(value))


@api_bp.get("/admin/media-assets")
@require_roles(UserRole.ADMIN)
def admin_media_assets():
    query = MediaAsset.query
    category = request.args.get("category", "").strip().lower()
    search = request.args.get("q", "").strip()
    active = request.args.get("active", "true").strip().lower()
    if category:
        query = query.filter(func.lower(MediaAsset.category) == category)
    if active in {"true", "1", "yes"}:
        query = query.filter_by(active=True)
    elif active in {"false", "0", "no"}:
        query = query.filter_by(active=False)
    if search:
        term = f"%{search[:100]}%"
        query = query.filter(or_(MediaAsset.name.ilike(term), MediaAsset.slug.ilike(term), MediaAsset.category.ilike(term)))
    rows = query.order_by(MediaAsset.is_default.desc(), MediaAsset.created_at.desc()).limit(250).all()
    return jsonify({"items": [item.to_dict() for item in rows], "total": len(rows)})


@api_bp.post("/admin/media-assets")
@require_roles(UserRole.ADMIN)
def admin_create_media_asset():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    image_url = str(data.get("image_url", "")).strip()
    category = str(data.get("category", "geral")).strip().lower()
    if not name or len(name) > 160:
        return jsonify({"error": "Informe um nome de até 160 caracteres."}), 400
    if not _media_url_is_valid(image_url) or len(image_url) > 2048:
        return jsonify({"error": "Informe uma URL HTTPS válida para a imagem."}), 400
    if not category or len(category) > 80:
        return jsonify({"error": "Categoria inválida."}), 400
    tags = data.get("tags") or []
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        return jsonify({"error": "tags deve ser uma lista de textos."}), 400
    tags = list(dict.fromkeys(tag.strip().lower()[:40] for tag in tags if tag.strip()))[:20]
    slug = _media_slug(data.get("slug") or name)
    base_slug, suffix = slug, 2
    while MediaAsset.query.filter_by(slug=slug).first():
        slug = f"{base_slug[:170]}-{suffix}"
        suffix += 1
    asset = MediaAsset(
        name=name, slug=slug, image_url=image_url, category=category,
        tags=tags, alt_text=str(data.get("alt_text") or name).strip()[:300] or None,
        source=str(data.get("source") or "external").strip()[:80],
        license=str(data.get("license") or "").strip()[:160] or None,
        is_default=bool(data.get("is_default", False)), active=True,
        created_by=g.current_user.id,
    )
    if asset.is_default:
        MediaAsset.query.filter_by(category=category, is_default=True).update({"is_default": False})
    db.session.add(asset)
    db.session.commit()
    return jsonify(asset.to_dict()), 201





@api_bp.post("/admin/media-assets/<int:asset_id>/usages")
@require_roles(UserRole.ADMIN)
def admin_link_media_asset_usage(asset_id):
    asset = db.session.get(MediaAsset, asset_id)
    if not asset or not asset.active:
        return jsonify({"error": "Imagem ativa não encontrada."}), 404
    data = request.get_json(silent=True) or {}
    entity_type = str(data.get("entity_type", "")).strip().lower()
    try:
        entity_id = int(data.get("entity_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Informe o ID numérico do registro."}), 400
    if entity_type not in {"product", "service", "store"}:
        return jsonify({"error": "Tipo deve ser product, service ou store."}), 400
    model = {"product": Product, "service": Service, "store": Store}[entity_type]
    entity = db.session.get(model, entity_id)
    if not entity:
        return jsonify({"error": "Registro de destino não encontrado."}), 404
    role = str(data.get("role", "primary")).strip().lower() or "primary"
    if role not in {"primary", "thumbnail", "banner"}:
        return jsonify({"error": "Papel de imagem inválido."}), 400
    usage = MediaAssetUsage.query.filter_by(entity_type=entity_type, entity_id=entity_id, role=role).first()
    if usage:
        usage.asset_id = asset.id
    else:
        usage = MediaAssetUsage(asset_id=asset.id, entity_type=entity_type, entity_id=entity_id, role=role)
        db.session.add(usage)
    # Compatibilidade: telas/API atuais continuam lendo image_url.
    if role == "primary":
        entity.image_url = asset.image_url
    db.session.commit()
    return jsonify({"usage_id": usage.id, "asset_id": asset.id, "entity_type": entity_type, "entity_id": entity_id, "role": role, "image_url": asset.image_url}), 201


def _sync_media_asset_usages(asset):
    for usage in asset.usages:
        if usage.role != "primary":
            continue
        model = {"product": Product, "service": Service, "store": Store}.get(usage.entity_type)
        entity = db.session.get(model, usage.entity_id) if model else None
        if entity:
            entity.image_url = asset.image_url


@api_bp.patch("/admin/media-assets/<int:asset_id>")
@require_roles(UserRole.ADMIN)
def admin_update_media_asset(asset_id):
    asset = db.session.get(MediaAsset, asset_id)
    if not asset:
        return jsonify({"error": "Imagem não encontrada."}), 404
    data = request.get_json(silent=True) or {}
    if "name" in data:
        name = str(data["name"]).strip()
        if not name or len(name) > 160:
            return jsonify({"error": "Nome inválido."}), 400
        asset.name = name
    if "image_url" in data:
        url = str(data["image_url"]).strip()
        if not _media_url_is_valid(url) or len(url) > 2048:
            return jsonify({"error": "Informe uma URL HTTPS válida para a imagem."}), 400
        asset.image_url = url
        _sync_media_asset_usages(asset)
    if "category" in data:
        category = str(data["category"]).strip().lower()
        if not category or len(category) > 80:
            return jsonify({"error": "Categoria inválida."}), 400
        asset.category = category
    if "tags" in data:
        if not isinstance(data["tags"], list) or any(not isinstance(tag, str) for tag in data["tags"]):
            return jsonify({"error": "tags deve ser uma lista de textos."}), 400
        asset.tags = list(dict.fromkeys(tag.strip().lower()[:40] for tag in data["tags"] if tag.strip()))[:20]
    for key, max_len in (("alt_text", 300), ("source", 80), ("license", 160)):
        if key in data:
            setattr(asset, key, str(data[key] or "").strip()[:max_len] or None)
    if "active" in data:
        asset.active = as_bool(data["active"])
    if "is_default" in data:
        if as_bool(data["is_default"]):
            MediaAsset.query.filter(MediaAsset.category == asset.category, MediaAsset.id != asset.id).update({"is_default": False})
            asset.is_default = True
        else:
            asset.is_default = False
    db.session.commit()
    return jsonify(asset.to_dict())


@api_bp.delete("/admin/media-assets/<int:asset_id>")
@require_roles(UserRole.ADMIN)
def admin_archive_media_asset(asset_id):
    asset = db.session.get(MediaAsset, asset_id)
    if not asset:
        return jsonify({"error": "Imagem não encontrada."}), 404
    asset.active = False
    asset.is_default = False
    db.session.commit()
    return jsonify({"id": asset.id, "active": False, "message": "Imagem arquivada."})


# Endpoints Asaas registrados no MESMO blueprint /api.
from ..payments import register_payment_routes  # noqa: E402
register_payment_routes(api_bp)
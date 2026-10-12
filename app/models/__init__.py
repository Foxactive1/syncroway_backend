"""
SyncroWay — modelos de domínio.
Suporta produtos (e-commerce) e serviços (vertical nativa transacional).
"""
from datetime import datetime, timezone
from decimal import Decimal

from werkzeug.security import check_password_hash

from ..extensions import db


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ═══════════════════════════════════════════════════════════════
# MIXINS
# ═══════════════════════════════════════════════════════════════

class TimestampMixin:
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class SoftDeleteMixin:
    deleted_at = db.Column(db.DateTime, nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def soft_delete(self):
        self.deleted_at = utcnow()

    def restore(self):
        self.deleted_at = None


# ═══════════════════════════════════════════════════════════════
# CONSTANTES
# ═══════════════════════════════════════════════════════════════

class UserRole:
    ADMIN    = "admin"
    MERCHANT = "merchant"
    STORE    = MERCHANT        # alias retrocompatível
    DRIVER   = "driver"
    CUSTOMER = "customer"
    PROVIDER = "provider"


class CategoryKind:
    PRODUCT = "product"
    SERVICE = "service"
    BOTH    = "both"


class OrderType:
    PRODUCT = "product"
    SERVICE = "service"


class OrderStatus:
    PENDING          = "pending"
    CONFIRMED        = "confirmed"
    CANCELLED        = "cancelled"
    PREPARING        = "preparing"
    READY            = "ready"
    PICKED_UP        = "picked_up"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED        = "delivered"
    SCHEDULED        = "scheduled"
    IN_PROGRESS      = "in_progress"
    COMPLETED        = "completed"
    UNDER_REVIEW     = "under_review"


class PaymentStatus:
    PENDING  = "pending"
    PAID     = "paid"
    HELD     = "held"
    REFUNDED = "refunded"
    FAILED   = "failed"


class PaymentMethod:
    CASH        = "cash"
    PIX         = "pix"
    CARD_ONLINE = "card_online"


class ServicePricingModel:
    FIXED    = "fixed"
    HOURLY   = "hourly"
    ON_QUOTE = "on_quote"


class ServiceModality:
    IN_PERSON = "in_person"
    REMOTE    = "remote"
    AT_SHOP   = "at_shop"


class VerificationStatus:
    UNVERIFIED = "unverified"
    PENDING    = "pending"
    VERIFIED   = "verified"
    PREMIUM    = "premium"
    SUSPENDED  = "suspended"


class QuoteStatus:
    DRAFT     = "draft"
    SENT      = "sent"
    ACCEPTED  = "accepted"
    REJECTED  = "rejected"
    EXPIRED   = "expired"
    WITHDRAWN = "withdrawn"


class ServiceRequestStatus:
    OPEN      = "open"
    MATCHED   = "matched"
    CONVERTED = "converted"
    CANCELLED = "cancelled"
    EXPIRED   = "expired"


class ScheduleSlotStatus:
    FREE      = "free"
    RESERVED  = "reserved"
    BLOCKED   = "blocked"


class AppointmentStatus:
    PENDING   = "pending"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class DeliveryStatus:
    AWAITING_DRIVER = "awaiting_driver"
    ASSIGNED        = "assigned"
    ACCEPTED        = "accepted"
    PICKED_UP       = "picked_up"
    DELIVERED       = "delivered"
    CANCELLED       = "cancelled"


class TransactionType:
    COMMISSION        = "commission"
    STORE_RECEIVABLE  = "store_receivable"
    DELIVERY_FEE      = "delivery_fee"
    PROVIDER_RECEIVABLE = "provider_receivable"
    REFUND            = "refund"


class TransactionStatus:
    PENDING  = "pending"
    SETTLED  = "settled"
    REFUNDED = "refunded"
    FAILED   = "failed"

class StockMovementType:
    """Tipos formais de movimento de estoque.

    `initial` e `sale` são controlados pelo sistema (cadastro / checkout).
    Os demais são lançados manualmente pelo lojista via API.
    """
    INITIAL       = "initial"
    PURCHASE      = "purchase"
    SALE          = "sale"
    RETURN_IN     = "return_in"
    RETURN_OUT    = "return_out"
    LOSS          = "loss"
    INTERNAL_USE  = "internal_use"
    ADJUSTMENT    = "adjustment"

    # Aceitos em POST /movements. `initial`/`sale` são automáticos;
    # `adjustment` só entra via /inventory/count (aceita delta sinalizado).
    MANUAL_TYPES = {PURCHASE, RETURN_IN, RETURN_OUT, LOSS, INTERNAL_USE}

    # +1 soma ao estoque; -1 subtrai. `adjustment` fica fora — recebe delta.
    DIRECTIONS = {
        INITIAL:      +1,
        PURCHASE:     +1,
        SALE:         -1,
        RETURN_IN:    +1,
        RETURN_OUT:   -1,
        LOSS:         -1,
        INTERNAL_USE: -1,
    }

    # Motivo obrigatório — sem isso o ledger vira lixo em 3 meses.
    REQUIRE_REASON = {LOSS, INTERNAL_USE, RETURN_OUT, RETURN_IN}

    # Todos os valores válidos (para filtros e validação).
    ALL = set(DIRECTIONS) | {ADJUSTMENT}


# ═══════════════════════════════════════════════════════════════
# USERS
# ═══════════════════════════════════════════════════════════════

class User(TimestampMixin, SoftDeleteMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(180), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(30))
    document = db.Column(db.String(20), unique=True)
    role = db.Column(db.String(30), default=UserRole.CUSTOMER,
                     nullable=False, index=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    email_verified_at = db.Column(db.DateTime)
    last_login_at = db.Column(db.DateTime)

    # ─── Relacionamentos ───────────────────────────────
    stores = db.relationship("Store", back_populates="owner", lazy=True)
    orders = db.relationship(
        "Order", back_populates="customer",
        foreign_keys="Order.customer_id", lazy=True,
    )
    cart = db.relationship("Cart", back_populates="customer",
                           uselist=False, cascade="all, delete-orphan")
    appointments = db.relationship(
        "Appointment", back_populates="customer",
        foreign_keys="Appointment.customer_id", lazy=True,
    )
    refresh_tokens = db.relationship(
        "RefreshToken", back_populates="user",
        cascade="all, delete-orphan", lazy=True,
    )
    driver_profile = db.relationship(
        "DriverProfile", back_populates="user",
        uselist=False, cascade="all, delete-orphan",
    )
    deliveries = db.relationship("Delivery", back_populates="driver", lazy=True)

    provider_profile = db.relationship(
        "ServiceProviderProfile", back_populates="user",
        uselist=False, cascade="all, delete-orphan",
    )
    services_offered = db.relationship(
        "Service", back_populates="provider",
        foreign_keys="Service.provider_id", lazy=True,
    )
    service_requests = db.relationship(
        "ServiceRequest", back_populates="customer",
        foreign_keys="ServiceRequest.customer_id", lazy=True,
    )
    quotes_sent = db.relationship(
        "ServiceQuote", back_populates="provider",
        foreign_keys="ServiceQuote.provider_id", lazy=True,
    )
    schedule_slots = db.relationship(
        "ServiceScheduleSlot", back_populates="provider",
        cascade="all, delete-orphan", lazy=True,
    )
    reviews_written = db.relationship(
        "Review", back_populates="author",
        foreign_keys="Review.author_id", lazy=True,
    )
    orders_as_provider = db.relationship(
        "Order", back_populates="provider",
        foreign_keys="Order.provider_id", lazy=True,
    )

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_provider(self) -> bool:
        return self.role == UserRole.PROVIDER

    @property
    def is_store(self) -> bool:
        return self.role == UserRole.MERCHANT

    def __repr__(self):
        # Sem e-mail — evita PII em logs.
        return f"<User id={self.id} role={self.role}>"


# ═══════════════════════════════════════════════════════════════
# CATEGORIA
# ═══════════════════════════════════════════════════════════════

class Category(TimestampMixin, db.Model):
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    slug = db.Column(db.String(120), unique=True, nullable=False, index=True)
    kind = db.Column(db.String(20), default=CategoryKind.BOTH,
                     nullable=False, index=True)
    icon = db.Column(db.String(80))
    description = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True, nullable=False)

    stores = db.relationship("Store", back_populates="category", lazy=True)
    products = db.relationship("Product", back_populates="category", lazy=True)
    services = db.relationship("Service", back_populates="category", lazy=True)
    service_requests = db.relationship(
        "ServiceRequest", back_populates="category", lazy=True,
    )

    def __repr__(self):
        return f"<Category {self.slug} kind={self.kind}>"


# ═══════════════════════════════════════════════════════════════
# STORE
# ═══════════════════════════════════════════════════════════════

class Store(TimestampMixin, db.Model):
    __tablename__ = "stores"

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                         nullable=False, index=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"),
                            nullable=False, index=True)
    name = db.Column(db.String(140), nullable=False, index=True)
    slug = db.Column(db.String(160), unique=True, index=True)
    description = db.Column(db.Text)
    image_url = db.Column(db.Text)

    address = db.Column(db.String(255))
    city = db.Column(db.String(100), index=True)
    state = db.Column(db.String(2))
    zip_code = db.Column(db.String(20))
    latitude = db.Column(db.Numeric(10, 7))
    longitude = db.Column(db.Numeric(10, 7))
    delivery_radius_km = db.Column(db.Numeric(5, 2), default=Decimal("5.00"))

    commission_rate = db.Column(db.Numeric(5, 4),
                                default=Decimal("0.1000"), nullable=False)

    active = db.Column(db.Boolean, default=True, nullable=False)
    accepts_orders = db.Column(db.Boolean, default=True, nullable=False)
    accepts_appointments = db.Column(db.Boolean, default=False, nullable=False)
    accepts_services = db.Column(db.Boolean, default=False, nullable=False)

    owner = db.relationship("User", back_populates="stores")
    category = db.relationship("Category", back_populates="stores")
    # Sem cascade de delete: produtos/serviços não podem ser apagados por tabela
    # pois order_items.service_id/product_id referenciam historicamente.
    # O fluxo correto é arquivar via active=False (ver rotas admin).
    products = db.relationship("Product", back_populates="store", lazy=True)
    services = db.relationship("Service", back_populates="store", lazy=True)

    def __repr__(self):
        return f"<Store id={self.id} name={self.name!r}>"


# ═══════════════════════════════════════════════════════════════
# PRODUCT
# ═══════════════════════════════════════════════════════════════

class Product(TimestampMixin, db.Model):
    __tablename__ = "products"
    __table_args__ = (
        db.CheckConstraint("price >= 0", name="ck_product_price_nonnegative"),
        db.CheckConstraint("stock >= 0", name="ck_product_stock_nonnegative"),
    )

    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey("stores.id"),
                         nullable=False, index=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"),
                            nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False, index=True)
    description = db.Column(db.Text)
    sku = db.Column(db.String(80), unique=True, index=True)
    price = db.Column(db.Numeric(12, 2), nullable=False)
    stock = db.Column(db.Integer, default=0, nullable=False)
    minimum_stock = db.Column(db.Integer, default=5, nullable=False)
    image_url = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True, nullable=False)

    store = db.relationship("Store", back_populates="products")
    category = db.relationship("Category", back_populates="products")
    stock_movements = db.relationship(
        "StockMovement", back_populates="product",
        cascade="all, delete-orphan", lazy=True,
    )

    def __repr__(self):
        return f"<Product id={self.id} name={self.name!r}>"


class StockMovement(TimestampMixin, db.Model):
    """Ledger de estoque: cada entrada/saída preserva saldo anterior e novo."""

    __tablename__ = "stock_movements"
    __table_args__ = (
        db.Index("ix_stock_movement_product_created", "product_id", "created_at"),
        db.Index("ix_stock_movement_actor_created", "actor_id", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(
        db.Integer, db.ForeignKey("products.id"), nullable=False, index=True,
    )
    actor_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True, index=True,
    )
    type = db.Column(db.String(30), nullable=False, index=True)
    quantity = db.Column(db.Integer, nullable=False)
    stock_before = db.Column(db.Integer, nullable=False)
    stock_after = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(255))
    reference_type = db.Column(db.String(30))
    reference_id = db.Column(db.Integer, index=True)

    product = db.relationship("Product", back_populates="stock_movements")
    actor = db.relationship("User", foreign_keys=[actor_id])


# ═══════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════

class Service(TimestampMixin, db.Model):
    __tablename__ = "services"
    __table_args__ = (
        db.CheckConstraint(
            "(price IS NULL OR price >= 0) AND "
            "(hourly_rate IS NULL OR hourly_rate >= 0)",
            name="ck_service_price_nonnegative",
        ),
        db.Index("ix_service_provider_active", "provider_id", "active"),
        db.Index("ix_service_category_active", "category_id", "active"),
    )

    id = db.Column(db.Integer, primary_key=True)

    provider_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                            nullable=False, index=True)
    store_id = db.Column(db.Integer, db.ForeignKey("stores.id"),
                         nullable=True, index=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"),
                            nullable=False, index=True)

    title = db.Column(db.String(160), nullable=False, index=True)
    description = db.Column(db.Text)
    image_url = db.Column(db.Text)

    pricing_model = db.Column(db.String(20),
                              default=ServicePricingModel.ON_QUOTE,
                              nullable=False, index=True)
    price = db.Column(db.Numeric(12, 2))
    hourly_rate = db.Column(db.Numeric(12, 2))
    min_hours = db.Column(db.Integer)
    visit_fee = db.Column(db.Numeric(12, 2),
                          default=Decimal("0.00"), nullable=False)
    duration_minutes = db.Column(db.Integer)

    modality = db.Column(db.String(20),
                         default=ServiceModality.IN_PERSON,
                         nullable=False, index=True)

    rating_avg = db.Column(db.Numeric(3, 2),
                           default=Decimal("0.00"), nullable=False)
    rating_count = db.Column(db.Integer, default=0, nullable=False)

    active = db.Column(db.Boolean, default=True, nullable=False)

    provider = db.relationship(
        "User", back_populates="services_offered",
        foreign_keys=[provider_id],
    )
    store = db.relationship("Store", back_populates="services")
    category = db.relationship("Category", back_populates="services")
    quotes = db.relationship("ServiceQuote", back_populates="service", lazy=True)

    def __repr__(self):
        return f"<Service id={self.id} title={self.title!r}>"


# ═══════════════════════════════════════════════════════════════
# SERVICE PROVIDER PROFILE
# ═══════════════════════════════════════════════════════════════

class ServiceProviderProfile(TimestampMixin, db.Model):
    __tablename__ = "service_provider_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        unique=True, nullable=False, index=True)

    headline = db.Column(db.String(120))
    bio = db.Column(db.Text)
    years_experience = db.Column(db.Integer)

    offers_remote = db.Column(db.Boolean, default=False, nullable=False)
    offers_in_person = db.Column(db.Boolean, default=True, nullable=False)
    service_radius_km = db.Column(db.Numeric(5, 2), default=Decimal("10.00"))
    city = db.Column(db.String(100), index=True)
    state = db.Column(db.String(2))
    latitude = db.Column(db.Numeric(10, 7))
    longitude = db.Column(db.Numeric(10, 7))

    verification_status = db.Column(
        db.String(20), default=VerificationStatus.UNVERIFIED,
        nullable=False, index=True,
    )
    verified_at = db.Column(db.DateTime)
    document_front_url = db.Column(db.Text)
    document_back_url = db.Column(db.Text)
    selfie_url = db.Column(db.Text)
    certification_urls = db.Column(db.JSON)

    is_available = db.Column(db.Boolean, default=False,
                             nullable=False, index=True)

    rating_avg = db.Column(db.Numeric(3, 2),
                           default=Decimal("0.00"), nullable=False)
    rating_count = db.Column(db.Integer, default=0, nullable=False)
    completed_jobs = db.Column(db.Integer, default=0, nullable=False)

    user = db.relationship("User", back_populates="provider_profile")

    def __repr__(self):
        return f"<ServiceProviderProfile user_id={self.user_id}>"


# ═══════════════════════════════════════════════════════════════
# SERVICE REQUEST + QUOTE
# ═══════════════════════════════════════════════════════════════

class ServiceRequest(TimestampMixin, db.Model):
    __tablename__ = "service_requests"
    __table_args__ = (
        db.Index("ix_svc_req_status_created", "status", "created_at"),
        db.Index("ix_svc_req_customer_status", "customer_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                            nullable=False, index=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"),
                            nullable=False, index=True)

    title = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, nullable=False)

    modality = db.Column(db.String(20), default=ServiceModality.IN_PERSON,
                         nullable=False, index=True)
    address = db.Column(db.String(255))
    city = db.Column(db.String(100), index=True)
    state = db.Column(db.String(2))
    latitude = db.Column(db.Numeric(10, 7))
    longitude = db.Column(db.Numeric(10, 7))
    desired_at = db.Column(db.DateTime, index=True)

    budget_max = db.Column(db.Numeric(12, 2))
    quotes_count = db.Column(db.Integer, default=0, nullable=False)

    status = db.Column(db.String(20),
                       default=ServiceRequestStatus.OPEN,
                       nullable=False, index=True)
    expires_at = db.Column(db.DateTime)

    customer = db.relationship(
        "User", back_populates="service_requests",
        foreign_keys=[customer_id],
    )
    category = db.relationship("Category", back_populates="service_requests")
    quotes = db.relationship("ServiceQuote", back_populates="request",
                             cascade="all, delete-orphan", lazy=True)
    orders = db.relationship("Order", back_populates="service_request", lazy=True)

    def __repr__(self):
        return f"<ServiceRequest id={self.id} status={self.status}>"


class ServiceQuote(TimestampMixin, db.Model):
    __tablename__ = "service_quotes"
    __table_args__ = (
        db.UniqueConstraint("request_id", "provider_id",
                            name="uq_quote_request_provider"),
        db.CheckConstraint("amount >= 0", name="ck_quote_amount_nonnegative"),
    )

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer,
                           db.ForeignKey("service_requests.id"),
                           nullable=False, index=True)
    provider_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                            nullable=False, index=True)
    service_id = db.Column(db.Integer, db.ForeignKey("services.id"),
                           nullable=True, index=True)

    amount = db.Column(db.Numeric(12, 2), nullable=False)
    visit_fee = db.Column(db.Numeric(12, 2),
                          default=Decimal("0.00"), nullable=False)
    estimated_duration_min = db.Column(db.Integer)
    message = db.Column(db.Text)

    status = db.Column(db.String(20), default=QuoteStatus.SENT,
                       nullable=False, index=True)
    expires_at = db.Column(db.DateTime)
    accepted_at = db.Column(db.DateTime)
    rejected_at = db.Column(db.DateTime)

    request = db.relationship("ServiceRequest", back_populates="quotes")
    provider = db.relationship(
        "User", back_populates="quotes_sent",
        foreign_keys=[provider_id],
    )
    service = db.relationship("Service", back_populates="quotes")

    def __repr__(self):
        return f"<ServiceQuote id={self.id} status={self.status}>"


# ═══════════════════════════════════════════════════════════════
# SCHEDULE SLOTS
# ═══════════════════════════════════════════════════════════════

class ServiceScheduleSlot(TimestampMixin, db.Model):
    __tablename__ = "service_schedule_slots"
    __table_args__ = (
        db.Index("ix_slot_provider_start", "provider_id", "start_at"),
        db.CheckConstraint("end_at > start_at", name="ck_slot_time_order"),
    )

    id = db.Column(db.Integer, primary_key=True)
    provider_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                            nullable=False, index=True)
    start_at = db.Column(db.DateTime, nullable=False, index=True)
    end_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default=ScheduleSlotStatus.FREE,
                       nullable=False, index=True)
    order_id = db.Column(db.Integer,
                         db.ForeignKey("orders.id", ondelete="SET NULL"),
                         nullable=True, index=True)

    provider = db.relationship("User", back_populates="schedule_slots")
    order = db.relationship("Order", foreign_keys=[order_id])


# ═══════════════════════════════════════════════════════════════
# CART
# ═══════════════════════════════════════════════════════════════

class Cart(TimestampMixin, db.Model):
    __tablename__ = "carts"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                            unique=True, nullable=False, index=True)

    customer = db.relationship("User", back_populates="cart")
    items = db.relationship("CartItem", back_populates="cart",
                            cascade="all, delete-orphan", lazy=True)


class CartItem(TimestampMixin, db.Model):
    __tablename__ = "cart_items"
    __table_args__ = (
        db.CheckConstraint("quantity > 0", name="ck_cart_item_qty_positive"),
        db.CheckConstraint(
            "(product_id IS NOT NULL AND service_id IS NULL) OR "
            "(product_id IS NULL AND service_id IS NOT NULL)",
            name="ck_cart_item_exactly_one_subject",
        ),
        db.UniqueConstraint("cart_id", "product_id", name="uq_cart_product"),
        db.UniqueConstraint("cart_id", "service_id", name="uq_cart_service"),
    )

    id = db.Column(db.Integer, primary_key=True)
    cart_id = db.Column(db.Integer, db.ForeignKey("carts.id"),
                        nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"),
                           nullable=True, index=True)
    service_id = db.Column(db.Integer, db.ForeignKey("services.id"),
                           nullable=True, index=True)
    quantity = db.Column(db.Integer, default=1, nullable=False)

    cart = db.relationship("Cart", back_populates="items")
    product = db.relationship("Product")
    service = db.relationship("Service")


# ═══════════════════════════════════════════════════════════════
# ORDER
# ═══════════════════════════════════════════════════════════════

class Order(TimestampMixin, db.Model):
    __tablename__ = "orders"
    __table_args__ = (
        db.Index("ix_order_type_status_created", "type", "status", "created_at"),
        db.Index("ix_order_provider_status", "provider_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, index=True)

    customer_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                            nullable=False, index=True)
    type = db.Column(db.String(20), default=OrderType.PRODUCT,
                     nullable=False, index=True)

    provider_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                            nullable=True, index=True)
    service_request_id = db.Column(
        db.Integer, db.ForeignKey("service_requests.id"),
        nullable=True, index=True,
    )

    status = db.Column(db.String(30), default=OrderStatus.PENDING,
                       nullable=False, index=True)
    payment_status = db.Column(db.String(30), default=PaymentStatus.PENDING,
                               nullable=False, index=True)
    payment_method = db.Column(db.String(40))

    subtotal = db.Column(db.Numeric(12, 2),
                         default=Decimal("0.00"), nullable=False)
    delivery_fee = db.Column(db.Numeric(12, 2),
                             default=Decimal("0.00"), nullable=False)
    visit_fee = db.Column(db.Numeric(12, 2),
                          default=Decimal("0.00"), nullable=False)
    total_amount = db.Column(db.Numeric(12, 2),
                             default=Decimal("0.00"), nullable=False)
    commission_value = db.Column(db.Numeric(12, 2),
                                 default=Decimal("0.00"), nullable=False)

    address = db.Column(db.String(255))
    notes = db.Column(db.Text)

    scheduled_for = db.Column(db.DateTime, index=True)
    estimated_duration_min = db.Column(db.Integer)
    payment_release_at = db.Column(db.DateTime)

    ready_at = db.Column(db.DateTime)
    picked_up_at = db.Column(db.DateTime)
    delivered_at = db.Column(db.DateTime)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    cancelled_at = db.Column(db.DateTime)
    cancellation_reason = db.Column(db.Text)

    customer = db.relationship(
        "User", back_populates="orders", foreign_keys=[customer_id],
    )
    provider = db.relationship(
        "User", back_populates="orders_as_provider",
        foreign_keys=[provider_id],
    )
    items = db.relationship("OrderItem", back_populates="order",
                            cascade="all, delete-orphan", lazy=True)
    delivery = db.relationship("Delivery", back_populates="order",
                               uselist=False, cascade="all, delete-orphan")
    service_request = db.relationship("ServiceRequest", back_populates="orders")
    reviews = db.relationship("Review", back_populates="order", lazy=True)
    transactions = db.relationship("Transaction", back_populates="order", lazy=True)

    @property
    def is_product_order(self) -> bool:
        return self.type == OrderType.PRODUCT

    @property
    def is_service_order(self) -> bool:
        return self.type == OrderType.SERVICE

    def __repr__(self):
        return f"<Order id={self.id} type={self.type} status={self.status}>"


class OrderItem(TimestampMixin, db.Model):
    __tablename__ = "order_items"
    __table_args__ = (
        db.CheckConstraint("quantity > 0", name="ck_order_item_qty_positive"),
        db.CheckConstraint(
            "(product_id IS NOT NULL AND service_id IS NULL) OR "
            "(product_id IS NULL AND service_id IS NOT NULL)",
            name="ck_order_item_exactly_one_subject",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"),
                         nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"),
                           nullable=True, index=True)
    service_id = db.Column(db.Integer, db.ForeignKey("services.id"),
                           nullable=True, index=True)

    item_name = db.Column(db.String(160), nullable=False)
    item_type = db.Column(db.String(20), nullable=False)

    quantity = db.Column(db.Integer, default=1, nullable=False)
    unit_price = db.Column(db.Numeric(12, 2), nullable=False)
    total_price = db.Column(db.Numeric(12, 2), nullable=False)

    order = db.relationship("Order", back_populates="items")
    product = db.relationship("Product")
    service = db.relationship("Service")


# ═══════════════════════════════════════════════════════════════
# APPOINTMENT
# ═══════════════════════════════════════════════════════════════

class Appointment(TimestampMixin, db.Model):
    __tablename__ = "appointments"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                            nullable=False, index=True)
    service_id = db.Column(db.Integer, db.ForeignKey("services.id"),
                           nullable=False, index=True)
    provider_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                            nullable=False, index=True)
    order_id = db.Column(db.Integer,
                         db.ForeignKey("orders.id", ondelete="SET NULL"),
                         nullable=True, index=True)
    scheduled_at = db.Column(db.DateTime, nullable=False, index=True)
    status = db.Column(db.String(30), default=AppointmentStatus.PENDING,
                       nullable=False, index=True)
    notes = db.Column(db.Text)

    customer = db.relationship(
        "User", back_populates="appointments",
        foreign_keys=[customer_id],
    )
    provider = db.relationship("User", foreign_keys=[provider_id])
    service = db.relationship("Service")
    order = db.relationship("Order", foreign_keys=[order_id])


# ═══════════════════════════════════════════════════════════════
# DRIVER / DELIVERY
# ═══════════════════════════════════════════════════════════════

class DriverProfile(TimestampMixin, db.Model):
    __tablename__ = "driver_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        unique=True, nullable=False, index=True)
    document = db.Column(db.String(30), unique=True, nullable=False)
    license_number = db.Column(db.String(40), unique=True, nullable=False)
    vehicle_type = db.Column(db.String(30), nullable=False)
    vehicle_plate = db.Column(db.String(12), unique=True, nullable=False)
    available = db.Column(db.Boolean, default=False, nullable=False, index=True)
    approved = db.Column(db.Boolean, default=False, nullable=False, index=True)
    current_latitude = db.Column(db.Numeric(10, 7))
    current_longitude = db.Column(db.Numeric(10, 7))
    city = db.Column(db.String(100), index=True)

    user = db.relationship("User", back_populates="driver_profile")


class Delivery(TimestampMixin, db.Model):
    __tablename__ = "deliveries"
    __table_args__ = (
        db.CheckConstraint("delivery_fee >= 0", name="ck_delivery_fee_nonnegative"),
    )

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"),
                         unique=True, nullable=False, index=True)
    driver_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                          nullable=True, index=True)
    status = db.Column(db.String(30), default=DeliveryStatus.AWAITING_DRIVER,
                       nullable=False, index=True)
    pickup_address = db.Column(db.String(255))
    delivery_address = db.Column(db.String(255), nullable=False)
    delivery_fee = db.Column(db.Numeric(12, 2),
                             default=Decimal("0.00"), nullable=False)
    distance_km = db.Column(db.Numeric(6, 2))
    accepted_at = db.Column(db.DateTime)
    picked_up_at = db.Column(db.DateTime)
    delivered_at = db.Column(db.DateTime)
    cancelled_at = db.Column(db.DateTime)

    order = db.relationship("Order", back_populates="delivery")
    driver = db.relationship("User", back_populates="deliveries")


# ═══════════════════════════════════════════════════════════════
# REVIEW
# ═══════════════════════════════════════════════════════════════

class Review(TimestampMixin, db.Model):
    __tablename__ = "reviews"
    __table_args__ = (
        db.UniqueConstraint("order_id", "author_id", "target_type",
                            name="uq_review_per_author_target"),
        db.CheckConstraint("score BETWEEN 1 AND 5", name="ck_review_score"),
        db.Index("ix_review_target", "target_type", "target_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"),
                         nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                          nullable=False, index=True)
    target_type = db.Column(db.String(20), nullable=False, index=True)
    target_id = db.Column(db.Integer, nullable=False, index=True)
    score = db.Column(db.SmallInteger, nullable=False)
    comment = db.Column(db.Text)

    order = db.relationship("Order", back_populates="reviews")
    author = db.relationship(
        "User", back_populates="reviews_written",
        foreign_keys=[author_id],
    )


# ═══════════════════════════════════════════════════════════════
# TRANSACTION / PAYOUT / INCIDENT / AUDIT
# ═══════════════════════════════════════════════════════════════

class Transaction(TimestampMixin, db.Model):
    __tablename__ = "transactions"
    __table_args__ = (
        db.Index("ix_tx_beneficiary", "beneficiary", "beneficiary_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"),
                         nullable=True, index=True)
    type = db.Column(db.String(30), nullable=False, index=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    beneficiary = db.Column(db.String(20), nullable=False, index=True)
    beneficiary_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                               nullable=True, index=True)
    status = db.Column(db.String(20), default=TransactionStatus.PENDING,
                       nullable=False, index=True)
    settled_at = db.Column(db.DateTime)

    order = db.relationship("Order", back_populates="transactions")


class Payout(TimestampMixin, db.Model):
    __tablename__ = "payouts"

    id = db.Column(db.Integer, primary_key=True)
    beneficiary = db.Column(db.String(20), nullable=False, index=True)
    beneficiary_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                               nullable=False, index=True)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    gross_amount = db.Column(db.Numeric(12, 2), nullable=False)
    fees = db.Column(db.Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    net_amount = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(20), default="open", nullable=False, index=True)
    paid_at = db.Column(db.DateTime)
    receipt_url = db.Column(db.Text)


class Incident(TimestampMixin, db.Model):
    __tablename__ = "incidents"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"),
                         nullable=True, index=True)
    reported_by = db.Column(db.Integer, db.ForeignKey("users.id"),
                            nullable=False, index=True)
    type = db.Column(db.String(40), nullable=False, index=True)
    status = db.Column(db.String(20), default="open",
                       nullable=False, index=True)
    description = db.Column(db.Text, nullable=False)
    resolution = db.Column(db.Text)
    resolved_at = db.Column(db.DateTime)


class AuditLog(db.Model):
    __tablename__ = "audit_log"
    __table_args__ = (
        db.Index("ix_audit_actor_created", "actor_id", "created_at"),
        db.Index("ix_audit_entity", "entity", "entity_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                         nullable=True, index=True)
    action = db.Column(db.String(60), nullable=False, index=True)
    entity = db.Column(db.String(60), nullable=False, index=True)
    entity_id = db.Column(db.Integer, index=True)
    diff = db.Column(db.JSON)
    ip = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, default=utcnow,
                           nullable=False, index=True)


# ═══════════════════════════════════════════════════════════════
# REFRESH TOKEN
# ═══════════════════════════════════════════════════════════════

class RefreshToken(TimestampMixin, db.Model):
    __tablename__ = "refresh_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False, index=True)
    # 1024 chars comporta JWT HS256 com claims extras (aud/iss/scope) sem truncar.
    token = db.Column(db.String(1024), unique=True, nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked = db.Column(db.Boolean, default=False, nullable=False)

    user = db.relationship("User", back_populates="refresh_tokens")

# ═══════════════════════════════════════════════════════════════
# ASAAS (integração de pagamentos)
# ═══════════════════════════════════════════════════════════════

class AsaasRecipientWallet(TimestampMixin, db.Model):
    __tablename__ = "asaas_recipient_wallets"
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey("stores.id"),
                         unique=True, nullable=False)
    wallet_id = db.Column(db.String(100), unique=True, nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=False)


class AsaasCustomer(TimestampMixin, db.Model):
    __tablename__ = "asaas_customers"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        unique=True, nullable=False)
    external_id = db.Column(db.String(100), unique=True, nullable=False)


class AsaasPayment(TimestampMixin, db.Model):
    __tablename__ = "asaas_payments"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"),
                         unique=True, nullable=False)
    external_id = db.Column(db.String(100), unique=True, nullable=True)
    state = db.Column(db.String(30), nullable=False,
                      default="creating", index=True)
    billing_type = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    invoice_url = db.Column(db.Text)
    split_wallet_id = db.Column(db.String(100), nullable=False)
    split_percent = db.Column(db.Numeric(8, 4), nullable=False)
    error = db.Column(db.Text)


class AsaasWebhookEvent(db.Model):
    __tablename__ = "asaas_webhook_events"
    id = db.Column(db.String(160), primary_key=True)
    event_type = db.Column(db.String(100), nullable=False)
    payment_id = db.Column(db.String(100), index=True)
    payload = db.Column(db.JSON, nullable=False)
    processed_at = db.Column(db.DateTime, default=utcnow, nullable=False)
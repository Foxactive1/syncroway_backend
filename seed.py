"""Seed de desenvolvimento.

Segurança:
- RESET_DB_ON_SEED=true é **proibido** quando APP_ENV=production.
- SEED_DEFAULT_PASSWORD é obrigatório em produção; em dev cai em "Mudar@123".
- SEED_ASAAS_WALLETS=true cria carteiras Asaas **desabilitadas** para as lojas
  seedadas. O operador deve verificar e ativar manualmente em produção.

Idempotência:
- Categorias por slug, usuários por e-mail, catálogo por SKU/título.
- Reexecuções parciais não deixam o banco em estado inconsistente.
"""
import os
from decimal import Decimal

from werkzeug.security import generate_password_hash

from app import create_app
from app.extensions import db
from app.models import (
    Cart,
    Category,
    DriverProfile,
    Product,
    Service,
    ServiceModality,
    ServicePricingModel,
    ServiceProviderProfile,
    StockMovement,
    Store,
    User,
    UserRole,
)

app = create_app()

IMG = "https://placehold.co/600x400/eeeeee/999999?text="

CATEGORY_DATA = [
    ("Alimentação", "alimentacao", "product", "bi-egg-fried"),
    ("Veterinária & Pet", "veterinaria-pet", "both", "bi-heart-pulse"),
    ("Eletrônicos", "eletronicos", "product", "bi-phone"),
    ("Mercado", "mercado", "product", "bi-cart4"),
    ("Serviços", "servicos", "service", "bi-tools"),
    ("Casa & Manutenção", "casa-manutencao", "both", "bi-house"),
    ("Automotivo", "automotivo", "both", "bi-car-front"),
    ("Moda & Vestuário", "moda", "product", "bi-bag-heart"),
    ("Beleza & Bem-estar", "beleza", "service", "bi-scissors"),
]


# ═══════════════════════════════════════════════════════════════
# GUARDAS DE SEGURANÇA
# ═══════════════════════════════════════════════════════════════

def _resolve_default_password() -> str:
    """Senha explícita em produção; fallback apenas em dev."""
    explicit = os.getenv("SEED_DEFAULT_PASSWORD")
    if explicit:
        return explicit
    app_env = os.getenv("APP_ENV", "development").strip().lower()
    if app_env == "production":
        raise RuntimeError(
            "SEED_DEFAULT_PASSWORD é obrigatório quando APP_ENV=production"
        )
    return "Mudar@123"


def _guard_production_reset() -> bool:
    """Autoriza drop_all só fora de produção."""
    wants_reset = os.getenv("RESET_DB_ON_SEED", "false").lower() == "true"
    if not wants_reset:
        return False
    app_env = os.getenv("APP_ENV", "development").strip().lower()
    if app_env == "production":
        raise RuntimeError(
            "RESET_DB_ON_SEED=true é proibido com APP_ENV=production"
        )
    return True


# ═══════════════════════════════════════════════════════════════
# SEEDS
# ═══════════════════════════════════════════════════════════════

def _seed_categories() -> dict[str, Category]:
    """Idempotente por slug."""
    for name, slug, kind, icon in CATEGORY_DATA:
        if not Category.query.filter_by(slug=slug).first():
            db.session.add(Category(name=name, slug=slug, kind=kind, icon=icon))
    db.session.commit()
    slugs = [slug for _, slug, _, _ in CATEGORY_DATA]
    return {c.slug: c for c in Category.query.filter(Category.slug.in_(slugs)).all()}


def _seed_users(default_password: str) -> dict[str, User]:
    """Idempotente por e-mail."""
    pwd = generate_password_hash(default_password)
    result: dict[str, User] = {}

    admin = User.query.filter_by(email="admin@syncroway.local").first()
    if not admin:
        admin = User(name="Admin SyncroWay", email="admin@syncroway.local",
                     password_hash=pwd, role=UserRole.ADMIN)
        db.session.add(admin)
    result["admin"] = admin

    merchant = User.query.filter_by(email="loja@syncroway.local").first()
    if not merchant:
        merchant = User(name="Lojista Parceiro", email="loja@syncroway.local",
                        password_hash=pwd, role=UserRole.MERCHANT)
        db.session.add(merchant)
    result["merchant"] = merchant

    customer = User.query.filter_by(email="cliente@syncroway.local").first()
    if not customer:
        customer = User(name="Cliente Francano", email="cliente@syncroway.local",
                        password_hash=pwd, role=UserRole.CUSTOMER)
        db.session.add(customer)
        db.session.flush()
        db.session.add(Cart(customer_id=customer.id))
    result["customer"] = customer

    driver = User.query.filter_by(email="motorista@syncroway.local").first()
    if not driver:
        driver = User(name="Motorista Parceiro",
                      email="motorista@syncroway.local",
                      password_hash=pwd, role=UserRole.DRIVER)
        db.session.add(driver)
        db.session.flush()
        db.session.add(DriverProfile(
            user_id=driver.id,
            document="00000000000",
            license_number="CNH-DEMO-001",
            vehicle_type="motorcycle",
            vehicle_plate="SYN0W01",
            approved=True,
            available=True,
        ))
    result["driver"] = driver

    provider = User.query.filter_by(email="prestador@syncroway.local").first()
    if not provider:
        provider = User(name="Prestador SyncroWay",
                        email="prestador@syncroway.local",
                        password_hash=pwd, role=UserRole.PROVIDER)
        db.session.add(provider)
        db.session.flush()
        db.session.add(ServiceProviderProfile(
            user_id=provider.id,
            headline="Profissional parceiro",
            bio="Prestador independente da rede SyncroWay.",
            city="Franca",
            state="SP",
            is_available=True,
        ))
    result["provider"] = provider

    db.session.commit()
    return result


def _seed_catalog(cats: dict[str, Category], merchant: User) -> None:
    """Catálogo inteiro em UMA transação — falha não deixa meio-criado.

    Guarda por agregado: só pula se já existem produtos E serviços.
    Se um seed anterior crashou depois das lojas, ainda completa produtos/serviços.
    """
    if Product.query.count() > 0 and Service.query.count() > 0:
        return

    try:
        stores = [
            Store(owner_id=merchant.id, category_id=cats["alimentacao"].id,
                  name="Burger House",
                  description="Lanches artesanais, combos e bebidas",
                  city="Franca", state="SP", accepts_orders=True),
            Store(owner_id=merchant.id, category_id=cats["alimentacao"].id,
                  name="Pizzaria Napoli",
                  description="Pizzas assadas em forno a lenha",
                  city="Franca", state="SP", accepts_orders=True),
            Store(owner_id=merchant.id, category_id=cats["veterinaria-pet"].id,
                  name="Pet Care Franca",
                  description="Clínica Veterinária e Banho/Tosa",
                  city="Franca", state="SP",
                  accepts_orders=True, accepts_appointments=True),
            Store(owner_id=merchant.id, category_id=cats["eletronicos"].id,
                  name="Tech Store",
                  description="Celulares, informática e acessórios",
                  city="Franca", state="SP", accepts_orders=True),
            Store(owner_id=merchant.id, category_id=cats["mercado"].id,
                  name="Super Fácil",
                  description="Mercado, açougue e hortifrúti",
                  city="Franca", state="SP", accepts_orders=True),
            Store(owner_id=merchant.id, category_id=cats["servicos"].id,
                  name="Instalação & Manutenção",
                  description="Elétrica, hidráulica e pequenos reparos rápidos",
                  city="Franca", state="SP",
                  accepts_appointments=True, accepts_orders=False),
            Store(owner_id=merchant.id, category_id=cats["automotivo"].id,
                  name="Oficina Rápida V8",
                  description="Mecânica em geral e troca de óleo",
                  city="Franca", state="SP",
                  accepts_orders=True, accepts_appointments=True),
            Store(owner_id=merchant.id, category_id=cats["moda"].id,
                  name="Boutique Elegance",
                  description="Roupas femininas e acessórios de moda",
                  city="Franca", state="SP", accepts_orders=True),
            Store(owner_id=merchant.id, category_id=cats["beleza"].id,
                  name="Studio Hair & Beleza",
                  description="Salão de beleza completo",
                  city="Franca", state="SP",
                  accepts_appointments=True, accepts_orders=False),
        ]
        db.session.add_all(stores)
        db.session.flush()
        by_name = {s.name: s for s in stores}

        products = [
            # Burger House
            Product(store_id=by_name["Burger House"].id, category_id=cats["alimentacao"].id,
                    name="Hambúrguer Clássico", sku="BH-001",
                    price=Decimal("29.90"), stock=100,
                    image_url=f"{IMG}Hamburguer"),
            Product(store_id=by_name["Burger House"].id, category_id=cats["alimentacao"].id,
                    name="Batata Frita Média", sku="BH-002",
                    price=Decimal("12.90"), stock=200,
                    image_url=f"{IMG}Batata+Frita"),
            Product(store_id=by_name["Burger House"].id, category_id=cats["alimentacao"].id,
                    name="Refrigerante Lata", sku="BH-003",
                    price=Decimal("6.50"), stock=500,
                    image_url=f"{IMG}Refrigerante"),
            # Pizzaria
            Product(store_id=by_name["Pizzaria Napoli"].id, category_id=cats["alimentacao"].id,
                    name="Pizza Margherita Grande", sku="PZ-001",
                    price=Decimal("65.00"), stock=50,
                    image_url=f"{IMG}Pizza+Margherita"),
            Product(store_id=by_name["Pizzaria Napoli"].id, category_id=cats["alimentacao"].id,
                    name="Pizza Calabresa Grande", sku="PZ-002",
                    price=Decimal("59.90"), stock=50,
                    image_url=f"{IMG}Pizza+Calabresa"),
            # Tech Store
            Product(store_id=by_name["Tech Store"].id, category_id=cats["eletronicos"].id,
                    name="Fone Bluetooth Pro", sku="TS-001",
                    price=Decimal("149.90"), stock=25,
                    image_url=f"{IMG}Fone+Bluetooth"),
            Product(store_id=by_name["Tech Store"].id, category_id=cats["eletronicos"].id,
                    name="Carregador Turbo", sku="TS-002",
                    price=Decimal("89.90"), stock=80,
                    image_url=f"{IMG}Carregador+Turbo"),
            Product(store_id=by_name["Tech Store"].id, category_id=cats["eletronicos"].id,
                    name="Mouse Sem Fio", sku="TS-003",
                    price=Decimal("55.00"), stock=40,
                    image_url=f"{IMG}Mouse+Sem+Fio"),
            # Super Fácil
            Product(store_id=by_name["Super Fácil"].id, category_id=cats["mercado"].id,
                    name="Cesta Básica Completa", sku="SF-001",
                    price=Decimal("129.90"), stock=50,
                    image_url=f"{IMG}Cesta+Basica"),
            Product(store_id=by_name["Super Fácil"].id, category_id=cats["mercado"].id,
                    name="Arroz 5kg", sku="SF-002",
                    price=Decimal("28.90"), stock=150,
                    image_url=f"{IMG}Arroz+5kg"),
            Product(store_id=by_name["Super Fácil"].id, category_id=cats["mercado"].id,
                    name="Feijão Carioca 1kg", sku="SF-003",
                    price=Decimal("8.50"), stock=200,
                    image_url=f"{IMG}Feijao"),
            # Oficina
            Product(store_id=by_name["Oficina Rápida V8"].id, category_id=cats["automotivo"].id,
                    name="Óleo Sintético 5W30 (1L)", sku="OF-001",
                    price=Decimal("45.00"), stock=60,
                    image_url=f"{IMG}Oleo+Motor"),
            Product(store_id=by_name["Oficina Rápida V8"].id, category_id=cats["automotivo"].id,
                    name="Filtro de Óleo", sku="OF-002",
                    price=Decimal("25.00"), stock=40,
                    image_url=f"{IMG}Filtro+Oleo"),
            # Boutique
            Product(store_id=by_name["Boutique Elegance"].id, category_id=cats["moda"].id,
                    name="Vestido de Verão", sku="BT-001",
                    price=Decimal("120.00"), stock=15,
                    image_url=f"{IMG}Vestido"),
            Product(store_id=by_name["Boutique Elegance"].id, category_id=cats["moda"].id,
                    name="Bolsa de Couro Sintético", sku="BT-002",
                    price=Decimal("180.00"), stock=10,
                    image_url=f"{IMG}Bolsa"),
        ]
        db.session.add_all(products)
        db.session.flush()

        for product in products:
            db.session.add(StockMovement(
                product_id=product.id,
                actor_id=merchant.id,
                type="initial",
                quantity=product.stock,
                stock_before=0,
                stock_after=product.stock,
                reason="Saldo inicial do seed",
            ))

        services = [
            Service(provider_id=merchant.id, store_id=by_name["Pet Care Franca"].id,
                    category_id=cats["veterinaria-pet"].id,
                    title="Consulta Veterinária",
                    description="Atendimento clínico geral",
                    pricing_model=ServicePricingModel.FIXED,
                    modality=ServiceModality.AT_SHOP,
                    duration_minutes=40, price=Decimal("120.00")),
            Service(provider_id=merchant.id, store_id=by_name["Pet Care Franca"].id,
                    category_id=cats["veterinaria-pet"].id,
                    title="Banho e Tosa (Porte Pequeno)",
                    description="Estética animal completa",
                    pricing_model=ServicePricingModel.FIXED,
                    modality=ServiceModality.AT_SHOP,
                    duration_minutes=90, price=Decimal("85.00")),
            Service(provider_id=merchant.id, store_id=by_name["Instalação & Manutenção"].id,
                    category_id=cats["servicos"].id,
                    title="Visita Técnica Residencial",
                    description="Avaliação de elétrica/hidráulica",
                    pricing_model=ServicePricingModel.FIXED,
                    modality=ServiceModality.IN_PERSON,
                    duration_minutes=60, price=Decimal("100.00")),
            Service(provider_id=merchant.id, store_id=by_name["Instalação & Manutenção"].id,
                    category_id=cats["servicos"].id,
                    title="Instalação de Chuveiro",
                    description="Substituição e instalação de ducha",
                    pricing_model=ServicePricingModel.FIXED,
                    modality=ServiceModality.IN_PERSON,
                    duration_minutes=45, price=Decimal("80.00")),
            Service(provider_id=merchant.id, store_id=by_name["Oficina Rápida V8"].id,
                    category_id=cats["automotivo"].id,
                    title="Alinhamento e Balanceamento",
                    description="Serviço para 4 rodas",
                    pricing_model=ServicePricingModel.FIXED,
                    modality=ServiceModality.AT_SHOP,
                    duration_minutes=50, price=Decimal("140.00")),
            Service(provider_id=merchant.id, store_id=by_name["Studio Hair & Beleza"].id,
                    category_id=cats["beleza"].id,
                    title="Corte Feminino",
                    description="Lavagem, corte e secagem",
                    pricing_model=ServicePricingModel.FIXED,
                    modality=ServiceModality.AT_SHOP,
                    duration_minutes=60, price=Decimal("90.00")),
            Service(provider_id=merchant.id, store_id=by_name["Studio Hair & Beleza"].id,
                    category_id=cats["beleza"].id,
                    title="Manicure e Pedicure",
                    description="Serviço completo",
                    pricing_model=ServicePricingModel.FIXED,
                    modality=ServiceModality.AT_SHOP,
                    duration_minutes=90, price=Decimal("70.00")),
        ]
        db.session.add_all(services)

        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def _seed_asaas_wallets(store_names: list[str]) -> None:
    """Opcional: cria carteiras Asaas DESABILITADAS para lojas seedadas.

    Controlado por SEED_ASAAS_WALLETS=true. O operador precisa verificar
    o dono da wallet no painel Asaas antes de ativar `enabled=True`.
    """
    if os.getenv("SEED_ASAAS_WALLETS", "false").lower() != "true":
        return

    try:
        from app.models import AsaasRecipientWallet
    except ImportError:
        print("⚠️  Modelo AsaasRecipientWallet indisponível; pulando seed de carteiras.")
        return

    created = 0
    for name in store_names:
        store = Store.query.filter_by(name=name).first()
        if not store:
            continue
        if AsaasRecipientWallet.query.filter_by(store_id=store.id).first():
            continue
        db.session.add(AsaasRecipientWallet(
            store_id=store.id,
            wallet_id=f"seed-wallet-{store.id}",
            enabled=False,
        ))
        created += 1
    db.session.commit()
    if created:
        print(f"⚠️  {created} carteira(s) Asaas criada(s) DESABILITADA(S).")
        print("   Verifique na Asaas e ative manualmente antes de usar em produção.")


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    default_password = _resolve_default_password()

    with app.app_context():
        try:
            if _guard_production_reset():
                db.drop_all()
            db.create_all()

            cats = _seed_categories()
            users = _seed_users(default_password)
            _seed_catalog(cats, users["merchant"])
            _seed_asaas_wallets([
                "Burger House",
                "Pizzaria Napoli",
                "Tech Store",
                "Super Fácil",
            ])

            print("--- Seed Expandido Concluído ---")
            print(f"Senha padrão: {default_password}")
            if default_password == "Mudar@123":
                print("⚠️  Esta é a senha de DESENVOLVIMENTO. Não use em produção.")
            print(" 1. Admin:      admin@syncroway.local")
            print(" 2. Lojista:    loja@syncroway.local")
            print(" 3. Cliente:    cliente@syncroway.local")
            print(" 4. Motorista:  motorista@syncroway.local")
            print(" 5. Prestador:  prestador@syncroway.local")
        except Exception:
            db.session.rollback()
            raise


if __name__ == "__main__":
    main()
"""Testes de aceitação da Sprint 2 + regressões críticas.

Cobre:
- Inventário e financeiro por loja
- Checkout multi-loja
- Permissões de role e loja indisponível
- Validação numérica em /checkout
- Rotação de refresh token (single-use)
- Estoque preservado quando checkout rejeita por validação
- Webhook Asaas idempotente e com autenticação
- Endpoint público /public-config
"""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from werkzeug.security import generate_password_hash

from app import create_app
from app.config import Config
from app.extensions import db
from app.models import (
    Cart,
    Category,
    Delivery,
    DriverProfile,
    Order,
    Product,
    Store,
    Transaction,
    User,
)


class Sprint2TestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()

        class TestConfig(Config):
            TESTING = True
            RATELIMIT_ENABLED = False
            DELIVERY_FEE_FROM_CLIENT = True
            SQLALCHEMY_DATABASE_URI = (
                f"sqlite:///{Path(self.tempdir.name) / 'test.db'}"
            )
            SECRET_KEY = "test-secret-key-with-at-least-32-bytes"
            ASAAS_WEBHOOK_TOKEN = "test-webhook-token"
            ASAAS_ENABLED = False

        self.app = create_app(TestConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            db.create_all()

            category = Category(name="Mercado", slug="mercado", kind="product")
            merchant = User(
                name="Loja", email="loja@test.local",
                password_hash=generate_password_hash("Senha@123"),
                role="merchant",
            )
            customer = User(
                name="Cliente", email="cliente@test.local",
                password_hash=generate_password_hash("Senha@123"),
                role="customer",
            )
            driver = User(
                name="Motorista", email="motorista@test.local",
                password_hash=generate_password_hash("Senha@123"),
                role="driver",
            )
            admin = User(
                name="Admin", email="admin@test.local",
                password_hash=generate_password_hash("Senha@123"),
                role="admin",
            )
            driver.driver_profile = DriverProfile(
                document="111", license_number="CNH111",
                vehicle_type="motorcycle", vehicle_plate="TST1A11",
                approved=True, available=True,
            )
            db.session.add_all([category, merchant, customer, driver, admin])
            db.session.flush()

            # IDs estáveis para uso nos testes — nunca hardcode 1, 2, ...
            self.category_id = category.id
            self.merchant_id = merchant.id
            self.customer_id = customer.id
            self.driver_id = driver.id
            self.admin_id = admin.id

            db.session.add(Cart(customer_id=customer.id))
            store = Store(
                owner_id=merchant.id,
                category_id=category.id,
                name="Loja Teste",
                address="Rua da Loja, 1",
                commission_rate=Decimal("0.10"),
            )
            db.session.add(store)
            db.session.flush()
            self.store_id = store.id

            product = Product(
                store_id=store.id,
                category_id=category.id,
                name="Produto",
                sku="TST-1",
                price=Decimal("100"),
                stock=10,
                minimum_stock=3,
            )
            db.session.add(product)
            db.session.flush()
            self.product_id = product.id

            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()   # fecha o pool antes de remover o arquivo SQLite
        self.tempdir.cleanup()

    def auth(self, email: str) -> dict:
        response = self.client.post(
            "/api/auth/login",
            json={"email": email, "password": "Senha@123"},
        )
        self.assertEqual(response.status_code, 200)
        return {"Authorization": f"Bearer {response.get_json()['access_token']}"}

    # ─── Sprint 2 (originais, agora com IDs estáveis) ────────

    def test_inventory_finance_privacy_and_driver_earnings(self):
        customer = self.auth("cliente@test.local")
        self.assertEqual(
            self.client.post(
                "/api/cart/items",
                json={"product_id": self.product_id, "quantity": 1},
                headers=customer,
            ).status_code,
            201,
        )
        checkout = self.client.post(
            "/api/checkout",
            json={
                "address": "Rua Cliente, 10",
                "delivery_fee": 7,
                "payment_method": "cash",
            },
            headers=customer,
        )
        self.assertEqual(checkout.status_code, 201)

        merchant = self.auth("loja@test.local")
        inventory = self.client.get(
            "/api/merchant/inventory", headers=merchant
        ).get_json()
        self.assertEqual(inventory["items"][0]["stock"], 9)

        movements = self.client.get(
            "/api/merchant/stock-movements", headers=merchant
        ).get_json()
        self.assertEqual(movements[0]["type"], "sale")

        finance = self.client.get(
            "/api/merchant/dashboard?period=30d", headers=merchant
        ).get_json()["finance"]
        self.assertEqual(finance["gross_revenue"], 100.0)
        self.assertEqual(finance["net_revenue"], 90.0)

        driver = self.auth("motorista@test.local")
        available = self.client.get(
            "/api/driver/dashboard?period=30d", headers=driver
        ).get_json()["available_deliveries"][0]
        self.assertIsNone(available["pickup_address"])
        self.assertIsNone(available["delivery_address"])
        delivery_id = available["id"]

        self.assertEqual(
            self.client.post(
                f"/api/driver/deliveries/{delivery_id}/accept", headers=driver
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/driver/deliveries/{delivery_id}",
                json={"status": "picked_up"},
                headers=driver,
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/driver/deliveries/{delivery_id}",
                json={"status": "delivered"},
                headers=driver,
            ).status_code,
            200,
        )
        dashboard = self.client.get(
            "/api/driver/dashboard?period=30d", headers=driver
        ).get_json()
        self.assertEqual(dashboard["earnings"]["period"], 7.0)
        self.assertEqual(
            dashboard["history"][0]["delivery_address"], "Rua Cliente, 10"
        )

    def test_checkout_multistore_creates_independent_orders_and_deliveries(self):
        with self.app.app_context():
            category = Category.query.filter_by(slug="mercado").one()
            merchant = User.query.filter_by(email="loja@test.local").one()
            second_store = Store(
                owner_id=merchant.id,
                category_id=category.id,
                name="Loja Dois",
                address="Rua da Loja Dois, 2",
                commission_rate=Decimal("0.15"),
            )
            db.session.add(second_store)
            db.session.flush()
            second_product = Product(
                store_id=second_store.id,
                category_id=category.id,
                name="Produto Dois",
                sku="TST-2",
                price=Decimal("50"),
                stock=5,
            )
            db.session.add(second_product)
            db.session.commit()
            second_product_id = second_product.id
            second_store_id = second_store.id

        customer = self.auth("cliente@test.local")
        self.assertEqual(
            self.client.post(
                "/api/cart/items",
                json={"product_id": self.product_id, "quantity": 2},
                headers=customer,
            ).status_code,
            201,
        )
        self.assertEqual(
            self.client.post(
                "/api/cart/items",
                json={"product_id": second_product_id, "quantity": 1},
                headers=customer,
            ).status_code,
            201,
        )
        response = self.client.post(
            "/api/checkout",
            json={
                "address": "Rua Cliente, 10",
                "payment_method": "cash",
                "delivery_fee": 6,
                "delivery_fees": {str(second_store_id): 9},
            },
            headers=customer,
        )
        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertEqual(payload["order_count"], 2)
        self.assertEqual(payload["subtotal"], 250.0)
        self.assertEqual(payload["delivery_fee"], 15.0)
        self.assertEqual(payload["total"], 265.0)

        with self.app.app_context():
            self.assertEqual(Order.query.count(), 2)
            self.assertEqual(Delivery.query.count(), 2)
            self.assertEqual(Transaction.query.count(), 6)
            self.assertEqual(
                {len(order.items) for order in Order.query.all()},
                {1},
            )
            self.assertEqual(
                {d.pickup_address for d in Delivery.query.all()},
                {"Rua da Loja, 1", "Rua da Loja Dois, 2"},
            )

    def test_customer_permissions_and_unavailable_store(self):
        merchant = self.auth("loja@test.local")
        forbidden = self.client.post(
            "/api/cart/items",
            json={"product_id": self.product_id, "quantity": 1},
            headers=merchant,
        )
        self.assertEqual(forbidden.status_code, 403)

        with self.app.app_context():
            store = Store.query.filter_by(name="Loja Teste").one()
            store.accepts_orders = False
            db.session.commit()

        customer = self.auth("cliente@test.local")
        unavailable = self.client.post(
            "/api/cart/items",
            json={"product_id": self.product_id, "quantity": 1},
            headers=customer,
        )
        self.assertEqual(unavailable.status_code, 409)

    def test_finance_routes_and_numeric_validation(self):
        customer = self.auth("cliente@test.local")
        self.assertEqual(
            self.client.post(
                "/api/cart/items",
                json={"product_id": self.product_id, "quantity": 1},
                headers=customer,
            ).status_code,
            201,
        )

        # Cobre entradas não-finitas e malformadas: todas devem virar 400,
        # nunca 500. `NaN`/`Infinity` passam pelo construtor Decimal mas
        # quebram comparações e quantize — parse_decimal barra explicitamente.
        for bad_value in ["NaN", "Infinity", "-Infinity", "1e999999", "abc", ""]:
            invalid = self.client.post(
                "/api/checkout",
                json={"address": "Rua Cliente, 10", "delivery_fee": bad_value},
                headers=customer,
            )
            self.assertEqual(
                invalid.status_code,
                400,
                f"delivery_fee={bad_value!r} deveria retornar 400, "
                f"veio {invalid.status_code}",
            )

        checkout = self.client.post(
            "/api/checkout",
            json={"address": "Rua Cliente, 10", "delivery_fee": 7},
            headers=customer,
        )
        self.assertEqual(checkout.status_code, 201)
        with self.app.app_context():
            order = Order.query.one()
            order.payment_status = "paid"
            db.session.commit()

        admin = self.auth("admin@test.local")
        finance = self.client.get(
            "/api/admin/finance?period=30d", headers=admin
        )
        self.assertEqual(finance.status_code, 200)
        self.assertEqual(finance.get_json()["gmv"], 107.0)
        transactions = self.client.get("/api/admin/transactions", headers=admin)
        self.assertEqual(transactions.status_code, 200)
        self.assertEqual(len(transactions.get_json()), 3)

    # ─── Regressões críticas ─────────────────────────────────

    def test_refresh_token_rotation_is_single_use(self):
        """Refresh rotaciona uma vez; reuso do token antigo falha com 401."""
        login = self.client.post(
            "/api/auth/login",
            json={"email": "cliente@test.local", "password": "Senha@123"},
        ).get_json()
        original_refresh = login["refresh_token"]

        first = self.client.post(
            "/api/auth/refresh",
            json={"refresh_token": original_refresh},
        )
        self.assertEqual(first.status_code, 200)
        new_tokens = first.get_json()
        self.assertIn("access_token", new_tokens)
        self.assertIn("refresh_token", new_tokens)
        self.assertNotEqual(new_tokens["refresh_token"], original_refresh)

        # Reuso: o token antigo deve estar revogado.
        reuse = self.client.post(
            "/api/auth/refresh",
            json={"refresh_token": original_refresh},
        )
        self.assertEqual(reuse.status_code, 401)

        # O novo refresh continua funcionando.
        second = self.client.post(
            "/api/auth/refresh",
            json={"refresh_token": new_tokens["refresh_token"]},
        )
        self.assertEqual(second.status_code, 200)

    def test_stock_preserved_when_checkout_rejects_payment_method(self):
        """Validação de pagamento ocorre antes de qualquer mutação de estoque."""
        customer = self.auth("cliente@test.local")
        self.assertEqual(
            self.client.post(
                "/api/cart/items",
                json={"product_id": self.product_id, "quantity": 1},
                headers=customer,
            ).status_code,
            201,
        )

        bad = self.client.post(
            "/api/checkout",
            json={"address": "Rua X", "payment_method": "bitcoin"},
            headers=customer,
        )
        self.assertEqual(bad.status_code, 400)

        with self.app.app_context():
            product = db.session.get(Product, self.product_id)
            self.assertEqual(product.stock, 10)

    def test_asaas_webhook_is_idempotent(self):
        """Eventos com o mesmo id não são processados duas vezes."""
        payload = {
            "id": "evt_test_dup_001",
            "event": "PAYMENT_RECEIVED",
            "payment": {"id": "pay_unknown"},
        }
        headers = {"asaas-access-token": "test-webhook-token"}

        first = self.client.post(
            "/api/webhooks/asaas", json=payload, headers=headers
        )
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.get_json().get("duplicate", False))

        second = self.client.post(
            "/api/webhooks/asaas", json=payload, headers=headers
        )
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json().get("duplicate"))

    def test_asaas_webhook_rejects_invalid_token(self):
        response = self.client.post(
            "/api/webhooks/asaas",
            json={"id": "evt_x", "event": "PAYMENT_RECEIVED"},
            headers={"asaas-access-token": "wrong"},
        )
        self.assertEqual(response.status_code, 401)

    def test_public_config_exposes_only_non_sensitive_flags(self):
        response = self.client.get("/api/public-config")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("asaas_enabled", payload)
        self.assertIn("delivery_fee_per_store", payload)
        self.assertIn("api_prefix", payload)

        # Nunca vaza chaves ou tokens.
        for key in payload:
            self.assertNotIn("key", key.lower())
            self.assertNotIn("secret", key.lower())
            self.assertNotIn("token", key.lower())


if __name__ == "__main__":
    unittest.main()
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from werkzeug.security import generate_password_hash

from app import create_app
from app.config import Config
from app.extensions import db
from app.models import Cart, Category, DriverProfile, Product, Store, User


class Sprint2TestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()

        class TestConfig(Config):
            TESTING = True
            RATELIMIT_ENABLED = False
            DELIVERY_FEE_FROM_CLIENT = True
            SQLALCHEMY_DATABASE_URI = f"sqlite:///{Path(self.tempdir.name) / 'test.db'}"
            SECRET_KEY = "test-secret-key-with-at-least-32-bytes"

        self.app = create_app(TestConfig)
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            category = Category(name="Mercado", slug="mercado", kind="product")
            merchant = User(name="Loja", email="loja@test.local", password_hash=generate_password_hash("Senha@123"), role="merchant")
            customer = User(name="Cliente", email="cliente@test.local", password_hash=generate_password_hash("Senha@123"), role="customer")
            driver = User(name="Motorista", email="motorista@test.local", password_hash=generate_password_hash("Senha@123"), role="driver")
            driver.driver_profile = DriverProfile(document="111", license_number="CNH111", vehicle_type="motorcycle", vehicle_plate="TST1A11", approved=True, available=True)
            db.session.add_all([category, merchant, customer, driver])
            db.session.flush()
            db.session.add(Cart(customer_id=customer.id))
            store = Store(owner_id=merchant.id, category_id=category.id, name="Loja Teste", address="Rua da Loja, 1", commission_rate=Decimal("0.10"))
            db.session.add(store)
            db.session.flush()
            db.session.add(Product(store_id=store.id, category_id=category.id, name="Produto", sku="TST-1", price=Decimal("100"), stock=10, minimum_stock=3))
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
        self.tempdir.cleanup()

    def auth(self, email):
        response = self.client.post("/api/auth/login", json={"email": email, "password": "Senha@123"})
        self.assertEqual(response.status_code, 200)
        return {"Authorization": f"Bearer {response.get_json()['access_token']}"}

    def test_inventory_finance_privacy_and_driver_earnings(self):
        customer = self.auth("cliente@test.local")
        self.assertEqual(self.client.post("/api/cart/items", json={"product_id": 1, "quantity": 1}, headers=customer).status_code, 201)
        checkout = self.client.post("/api/checkout", json={"address": "Rua Cliente, 10", "delivery_fee": 7, "payment_method": "cash"}, headers=customer)
        self.assertEqual(checkout.status_code, 201)

        merchant = self.auth("loja@test.local")
        inventory = self.client.get("/api/merchant/inventory", headers=merchant).get_json()
        self.assertEqual(inventory["items"][0]["stock"], 9)
        movements = self.client.get("/api/merchant/stock-movements", headers=merchant).get_json()
        self.assertEqual(movements[0]["type"], "sale")
        finance = self.client.get("/api/merchant/dashboard?period=30d", headers=merchant).get_json()["finance"]
        self.assertEqual(finance["gross_revenue"], 100.0)
        self.assertEqual(finance["net_revenue"], 90.0)

        driver = self.auth("motorista@test.local")
        available = self.client.get("/api/driver/dashboard?period=30d", headers=driver).get_json()["available_deliveries"][0]
        self.assertIsNone(available["pickup_address"])
        self.assertIsNone(available["delivery_address"])
        delivery_id = available["id"]
        self.assertEqual(self.client.post(f"/api/driver/deliveries/{delivery_id}/accept", headers=driver).status_code, 200)
        self.assertEqual(self.client.patch(f"/api/driver/deliveries/{delivery_id}", json={"status": "picked_up"}, headers=driver).status_code, 200)
        self.assertEqual(self.client.patch(f"/api/driver/deliveries/{delivery_id}", json={"status": "delivered"}, headers=driver).status_code, 200)
        dashboard = self.client.get("/api/driver/dashboard?period=30d", headers=driver).get_json()
        self.assertEqual(dashboard["earnings"]["period"], 7.0)
        self.assertEqual(dashboard["history"][0]["delivery_address"], "Rua Cliente, 10")


if __name__ == "__main__":
    unittest.main()

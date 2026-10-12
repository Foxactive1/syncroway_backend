from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import pytest
from flask_migrate import upgrade
from sqlalchemy import inspect
import test_sprint2 as fixture_module
from app.models import Order, Product, AsaasPayment, AsaasRecipientWallet, AsaasCustomer, AsaasWebhookEvent, StockMovement
from app.extensions import db
from app.utils import utcnow

@pytest.fixture
def case():
    c = fixture_module.Sprint2TestCase(methodName='runTest'); c.setUp()
    c.app.config['DELIVERY_FEE_FROM_CLIENT'] = False
    c.app.config['DELIVERY_FEE_PER_STORE'] = '7'
    yield c
    c.tearDown()

def checkout(c, online=False):
    c.app.config['ASAAS_ENABLED'] = online
    headers = c.auth('cliente@test.local')
    c.client.post('/api/cart/items', headers=headers, json={'product_id': c.product_id, 'quantity': 1})
    result = c.client.post('/api/checkout', headers=headers, json={'address': 'Rua teste', 'payment_method': 'pix' if online else 'cash', 'delivery_fees': {str(c.store_id): 0}, 'delivery_fee': 0})
    assert result.status_code == 201
    return headers, result.get_json()['id']

def marker(c, oid, state='pending'):
    with c.app.app_context():
        db.session.add(AsaasPayment(order_id=oid, external_id='pay_test', state=state, billing_type='PIX', amount=107, split_wallet_id='wallet_test', split_percent=90))
        db.session.commit()

def event(c, event_name, eid='evt_1'):
    return c.client.post('/api/webhooks/asaas', headers={'asaas-access-token': 'test-webhook-token'}, json={'id': eid, 'event': event_name, 'payment': {'id': 'pay_test'}})

def test_freight_is_server_owned_and_online_is_disabled(case):
    headers, oid = checkout(case)
    with case.app.app_context():
        assert db.session.get(Order, oid).delivery_fee == Decimal('7')
    response = case.client.post('/api/checkout', headers=headers, json={'address': 'Rua', 'payment_method':'pix'})
    assert response.status_code == 409

def test_cancel_restores_stock_once(case):
    headers, oid = checkout(case)
    for _ in range(2): assert case.client.post(f'/api/orders/{oid}/cancel', headers=headers).status_code == 200
    with case.app.app_context():
        assert db.session.get(Product, case.product_id).stock == 10
        assert StockMovement.query.filter_by(type='return_in').count() == 1

def test_expiry_never_releases_stock_with_remote_charge(case):
    _, oid = checkout(case, True)
    with case.app.app_context():
        db.session.get(Order, oid).payment_release_at = utcnow() - timedelta(minutes=1)
        db.session.commit()
    marker(case, oid)
    assert case.app.test_cli_runner().invoke(args=['expire-orders']).exit_code == 0
    with case.app.app_context(): assert db.session.get(Product, case.product_id).stock == 9

def test_expiry_releases_unbilled_online_reservation(case):
    _, oid = checkout(case, True)
    with case.app.app_context():
        db.session.get(Order, oid).payment_release_at = utcnow() - timedelta(minutes=1)
        db.session.commit()
    result = case.app.test_cli_runner().invoke(args=['expire-orders'])
    assert result.exit_code == 0, result.output
    with case.app.app_context(): assert db.session.get(Product, case.product_id).stock == 10

def test_overdue_keeps_stock_deleted_restores_it(case):
    _, oid = checkout(case, True); marker(case, oid)
    assert event(case, 'PAYMENT_OVERDUE').status_code == 200
    with case.app.app_context(): assert db.session.get(Product, case.product_id).stock == 9
    assert event(case, 'PAYMENT_DELETED', 'evt_2').status_code == 200
    assert event(case, 'PAYMENT_DELETED', 'evt_2').get_json()['duplicate'] is True
    with case.app.app_context(): assert db.session.get(Product, case.product_id).stock == 10
    assert event(case, 'PAYMENT_RECEIVED', 'evt_3').status_code == 200
    with case.app.app_context():
        assert db.session.get(Order, oid).status == 'cancelled'
        assert AsaasPayment.query.one().state == 'paid_after_cancel_review'

def test_unmatched_syncroway_webhook_retries(case):
    response = case.client.post('/api/webhooks/asaas', headers={'asaas-access-token':'test-webhook-token'}, json={'id':'evt_race','event':'PAYMENT_RECEIVED','payment':{'id':'pay_unknown','externalReference':'sw-order-1'}})
    assert response.status_code == 503
    with case.app.app_context(): assert db.session.get(AsaasWebhookEvent, 'evt_race') is None

def test_payment_creation_and_timeout_are_durable(case):
    headers, oid = checkout(case, True)
    with case.app.app_context():
        db.session.add(AsaasRecipientWallet(store_id=case.store_id, wallet_id='wallet_test', enabled=True))
        db.session.add(AsaasCustomer(user_id=case.customer_id, external_id='cus_test'))
        db.session.commit()
    from app.payments import GatewayError
    with patch('app.payments._api', side_effect=GatewayError('timeout')):
        assert case.client.post(f'/api/payments/orders/{oid}', headers=headers, json={}).status_code == 502
    assert case.client.post(f'/api/payments/orders/{oid}', headers=headers, json={}).status_code == 409
    with case.app.app_context(): assert AsaasPayment.query.one().state == 'unknown'

def test_payment_creation_pix_and_paid_webhook(case):
    headers, oid = checkout(case, True)
    with case.app.app_context():
        db.session.add(AsaasRecipientWallet(store_id=case.store_id, wallet_id='wallet_test', enabled=True))
        db.session.add(AsaasCustomer(user_id=case.customer_id, external_id='cus_test'))
        db.session.commit()
    with patch('app.payments._api', return_value={'id':'pay_test','invoiceUrl':'https://sandbox.asaas.com/i/test'}) as api:
        assert case.client.post(f'/api/payments/orders/{oid}', headers=headers, json={}).status_code == 201
        assert case.client.post(f'/api/payments/orders/{oid}', headers=headers, json={}).status_code == 200
        assert api.call_count == 1
    assert event(case,'PAYMENT_RECEIVED').status_code == 200
    assert case.client.get(f'/api/payments/orders/{oid}', headers=headers).get_json()['order_payment_status'] == 'paid'

def test_offline_payment_requires_evidence_and_audit(case):
    _, oid = checkout(case)
    admin = case.auth('admin@test.local')
    endpoint = f'/api/admin/orders/{oid}/confirm-offline-payment'
    assert case.client.post(endpoint, headers=admin, json={}).status_code == 400
    assert case.client.post(endpoint, headers=admin, json={'reference':'receipt-123'}).status_code == 200
    with case.app.app_context():
        from app.models import AuditLog
        assert AuditLog.query.filter_by(action='offline_payment').count() == 1

def test_migrations_install_and_match_models(tmp_path):
    from app import create_app
    from app.config import Config
    class C(Config):
        SQLALCHEMY_DATABASE_URI = f'sqlite:///{tmp_path / "migration.db"}'
        RATELIMIT_ENABLED = False
    app = create_app(C)
    with app.app_context():
        upgrade(directory=str(Path(__file__).resolve().parents[1] / 'migrations'))
        from alembic.migration import MigrationContext
        from alembic.autogenerate import compare_metadata
        with db.engine.connect() as conn:
            assert compare_metadata(MigrationContext.configure(conn), db.metadata) == []
        db.session.remove(); db.engine.dispose()

def test_paid_order_cannot_cancel_or_release_stock(case):
    headers, oid = checkout(case, True); marker(case, oid)
    assert event(case, 'PAYMENT_RECEIVED').status_code == 200
    assert case.client.post(f'/api/orders/{oid}/cancel', headers=headers).status_code == 409
    with case.app.app_context(): assert db.session.get(Product, case.product_id).stock == 9

def test_reconcile_rejects_wrong_order_reference(case):
    _, oid = checkout(case, True); marker(case, oid, 'unknown')
    admin = case.auth('admin@test.local')
    with patch('app.payments._api', return_value={'externalReference':'sw-order-other','value':107,'status':'RECEIVED'}):
        assert case.client.post(f'/api/admin/payments/orders/{oid}/reconcile', headers=admin, json={}).status_code == 409
    with case.app.app_context(): assert db.session.get(Order, oid).payment_status == 'pending'

def test_invalid_json_and_login_rate_limit(case):
    assert case.client.post('/api/auth/login', json=['bad']).status_code == 400
    from app import create_app
    from app.config import Config
    class LimitedConfig(Config):
        SQLALCHEMY_DATABASE_URI = case.app.config['SQLALCHEMY_DATABASE_URI']
        RATELIMIT_ENABLED = True
        TESTING = True
    limited = create_app(LimitedConfig).test_client()
    statuses = [limited.post('/api/auth/login', json={'email':'invalid@example.com','password':'wrong'}).status_code for _ in range(11)]
    assert statuses[-1] == 429

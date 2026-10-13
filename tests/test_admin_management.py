import pytest
import test_sprint2 as fixtures
from app.extensions import db
from app.models import User, Store, DriverProfile, AuditLog

@pytest.fixture
def case():
    c=fixtures.Sprint2TestCase(methodName='runTest'); c.setUp()
    yield c
    c.tearDown()

BASE='/api/admin/management/'

def test_roles_and_list(case):
    assert case.client.get(BASE+'users').status_code == 401
    assert case.client.get(BASE+'users',headers=case.auth('cliente@test.local')).status_code == 403
    a=case.auth('admin@test.local')
    for kind in ['users','drivers','stores']:
        r=case.client.get(BASE+kind,headers=a)
        assert r.status_code == 200
        assert isinstance(r.get_json(),list)
        assert 'password_hash' not in r.get_data(as_text=True)

def test_create_update_and_deactivate_merchant(case):
    a=case.auth('admin@test.local')
    data=dict(name='New Merchant',email='merchant@example.test',password='Test@12345',role='merchant',active=True)
    r=case.client.post(BASE+'users',json=data,headers=a);assert r.status_code==201,r.get_json()
    uid=r.get_json()['id']
    r=case.client.patch(BASE+f'users/{uid}',json={'name':'Updated','active':False},headers=a)
    assert r.status_code==200
    assert r.get_json()['active'] is False
    with case.app.app_context():
        assert db.session.get(User,uid).check_password('Test@12345')
        logs=AuditLog.query.filter_by(entity='users',entity_id=uid).all()
        assert len(logs)==2
        assert 'Test@12345' not in str([l.diff for l in logs])

def test_self_admin_cannot_disable_or_demote(case):
    a=case.auth('admin@test.local')
    for data in [{'active':False},{'role':'customer'}]:
        assert case.client.patch(BASE+f'users/{case.admin_id}',json=data,headers=a).status_code==400
    with case.app.app_context():assert db.session.get(User,case.admin_id).active

def test_store_commission_owner_and_archive(case):
    a=case.auth('admin@test.local')
    r=case.client.patch(BASE+f'stores/{case.store_id}',json={'commission_percent':'12.5','owner_id':case.admin_id},headers=a)
    assert r.status_code==200,r.get_json()
    with case.app.app_context():
        s=db.session.get(Store,case.store_id)
        assert float(s.commission_rate)==0.125
        assert s.owner_id==case.admin_id
    assert case.client.patch(BASE+f'stores/{case.store_id}',json={'active':False},headers=a).status_code==200
    with case.app.app_context():assert db.session.get(Store,case.store_id) is not None

def test_invalid_store_edit_is_atomic(case):
    a=case.auth('admin@test.local')
    r=case.client.patch(BASE+f'stores/{case.store_id}',json={'name':'Should roll back','commission_percent':'101'},headers=a)
    assert r.status_code==400
    with case.app.app_context():assert db.session.get(Store,case.store_id).name=='Loja Teste'

def test_driver_full_profile_create_edit(case):
    a=case.auth('admin@test.local')
    data=dict(name='Driver',email='driver@example.test',password='Test@12345',document='doc-test',license_number='cnh-test',vehicle_type='motorcycle',vehicle_plate='DEMO-NEW',approved=True,available=True,active=True,city='Franca')
    r=case.client.post(BASE+'drivers',json=data,headers=a)
    assert r.status_code==201,r.get_json()
    uid=r.get_json()['id'];assert r.get_json()['approved'] is True
    r=case.client.patch(BASE+f'drivers/{uid}',json={'vehicle_plate':'DEMO-EDIT','available':False},headers=a)
    assert r.status_code==200,r.get_json()
    assert r.get_json()['vehicle_plate']=='DEMO-EDIT'
    assert r.get_json()['available'] is False

def test_existing_demo_email_can_be_kept(case):
    a=case.auth('admin@test.local')
    r=case.client.patch(BASE+f'users/{case.merchant_id}',json={'email':'loja@test.local','name':'Loja atualizada'},headers=a)
    assert r.status_code==200,r.get_json()

def test_cannot_change_role_of_store_owner(case):
    a=case.auth('admin@test.local')
    assert case.client.patch(BASE+f'users/{case.merchant_id}',json={'role':'customer'},headers=a).status_code==400

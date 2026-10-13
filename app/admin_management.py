"""Administrative CRUD for the native client; archives instead of deleting history."""
from decimal import Decimal
from flask import g, jsonify, request
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash
from email_validator import validate_email, EmailNotValidError
from .auth import require_roles
from .extensions import db
from .models import (User, UserRole, Store, Category, DriverProfile, Cart,
                     ServiceProviderProfile, Delivery, DeliveryStatus, AuditLog)
from .utils import parse_decimal

ROLES = {'customer', 'merchant', 'provider', 'driver', 'admin'}


def serialize_user(u):
    return dict(id=u.id, name=u.name, email=u.email, phone=u.phone,
                role=u.role, active=u.active)


def serialize_store(s):
    return dict(id=s.id, name=s.name, owner_id=s.owner_id,
                category_id=s.category_id, description=s.description,
                address=s.address, city=s.city, state=s.state,
                zip_code=s.zip_code, slug=s.slug, active=s.active,
                accepts_orders=s.accepts_orders,
                accepts_appointments=s.accepts_appointments,
                accepts_services=s.accepts_services,
                commission_percent=str(s.commission_rate * 100))


def serialize_driver(u):
    result = serialize_user(u)
    p = u.driver_profile
    for field in ('document', 'license_number', 'vehicle_type', 'vehicle_plate',
                  'approved', 'available', 'city'):
        result[field] = getattr(p, field) if p else None
    return result


def text_field(data, key, limit, required=False):
    value = data.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f'{key} deve ser texto')
    value = value.strip()
    if required and not value:
        raise ValueError(f'{key} é obrigatório')
    if len(value) > limit:
        raise ValueError(f'{key} excede {limit} caracteres')
    return value or None


def flag(data, key, default):
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f'{key} deve ser booleano')
    return value


def apply_user(u, data, creating=False, driver=False):
    for key, limit in [('name', 120), ('email', 180), ('phone', 30)]:
        if creating or key in data:
            value = text_field(data, key, limit, key in {'name', 'email'})
            if key == 'email' and (creating or value.lower() != (u.email or '').lower()):
                try:
                    value = validate_email(value, check_deliverability=False,
                                           test_environment=True).normalized.lower()
                except EmailNotValidError:
                    raise ValueError('e-mail inválido')
            setattr(u, key, value)
    role = 'driver' if driver else data.get('role', u.role or 'customer')
    if role not in ROLES:
        raise ValueError('perfil inválido')
    if not driver and role == 'driver' and (creating or u.role != 'driver'):
        raise ValueError('Use Motoristas para criar um perfil de motorista completo')
    active = flag(data, 'active', True if creating else u.active)
    if not creating:
        if u.id == g.current_user.id and (not active or role != 'admin'):
            raise ValueError('Você não pode desativar ou remover seu próprio perfil administrativo')
        if u.role == 'admin' and u.active and (not active or role != 'admin'):
            if User.query.filter_by(role='admin', active=True).count() <= 1:
                raise ValueError('É necessário manter um administrador ativo')
        if u.stores and role not in {'merchant', 'admin'}:
            raise ValueError('Transfira as lojas antes de alterar o perfil do proprietário')
        if u.role == 'driver' and role != 'driver':
            raise ValueError('Motorista com perfil deve ser desativado, sem alterar o papel')
        if role != u.role and (u.provider_profile or u.services_offered):
            raise ValueError('Perfil prestador possui vínculos; desative em vez de alterar papel')
        if not active and u.role == 'driver' and Delivery.query.filter(
            Delivery.driver_id == u.id,
            Delivery.status.in_(['assigned', 'accepted', 'picked_up']),
        ).first():
            raise ValueError('Motorista possui entrega em andamento')
    password = data.get('password')
    if creating or password:
        if not isinstance(password, str) or not 8 <= len(password) <= 256:
            raise ValueError('Senha deve possuir de 8 a 256 caracteres')
        u.password_hash = generate_password_hash(password)
    u.role, u.active = role, active


def register_admin_management(bp):
    @bp.get('/admin/management/<kind>')
    @require_roles(UserRole.ADMIN)
    def list_records(kind):
        if kind == 'users':
            rows = User.query.order_by(User.id).limit(500).all()
            return jsonify([serialize_user(u) for u in rows])
        if kind == 'drivers':
            rows = User.query.filter_by(role='driver').order_by(User.id).limit(500).all()
            return jsonify([serialize_driver(u) for u in rows])
        if kind == 'stores':
            return jsonify([serialize_store(s) for s in Store.query.order_by(Store.id).limit(500).all()])
        return jsonify({'error': 'recurso inválido'}), 404

    @bp.post('/admin/management/<kind>')
    @bp.patch('/admin/management/<kind>/<int:record_id>')
    @require_roles(UserRole.ADMIN)
    def save_record(kind, record_id=None):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'corpo JSON deve ser objeto'}), 400
        if kind not in {'users', 'drivers', 'stores'}:
            return jsonify({'error': 'recurso inválido'}), 404
        creating = record_id is None
        try:
            # Serialize user administration, protecting the last admin and owners.
            User.query.order_by(User.id).with_for_update().all()
            if kind in {'users', 'drivers'}:
                row = User() if creating else User.query.filter_by(id=record_id).with_for_update().first()
                if row is None or (kind == 'drivers' and not creating and row.role != 'driver'):
                    return jsonify({'error': 'usuário não encontrado'}), 404
                before = {} if creating else serialize_driver(row) if kind == 'drivers' else serialize_user(row)
                apply_user(row, data, creating, kind == 'drivers')
                if creating:
                    db.session.add(row)
                    db.session.flush()
                if row.role == 'customer' and not row.cart:
                    db.session.add(Cart(customer_id=row.id))
                if row.role == 'provider' and not row.provider_profile:
                    db.session.add(ServiceProviderProfile(user_id=row.id))
                if kind == 'drivers':
                    profile = row.driver_profile
                    if profile is None:
                        profile = DriverProfile(user_id=row.id)
                        db.session.add(profile)
                    for field, limit in [('document', 30), ('license_number', 40),
                                         ('vehicle_type', 30), ('vehicle_plate', 12), ('city', 100)]:
                        if creating or field in data or getattr(profile, field) is None:
                            value = text_field(data, field, limit, field != 'city')
                            if field == 'vehicle_plate': value = value.upper()
                            if field == 'vehicle_type': value = value.lower()
                            setattr(profile, field, value)
                    approved = flag(data, 'approved', False if creating else profile.approved)
                    available = flag(data, 'available', False if creating else profile.available)
                    if (not approved or available) and Delivery.query.filter(
                        Delivery.driver_id == row.id, Delivery.status.in_(['assigned', 'accepted', 'picked_up']),
                    ).first():
                        raise ValueError('Motorista possui entrega em andamento; não altere aprovação/disponibilidade')
                    profile.approved, profile.available = approved, available
                db.session.flush()
                after = serialize_driver(row) if kind == 'drivers' else serialize_user(row)
                # Flush relationship before serializer for newly created profile.
                if kind == 'drivers':
                    db.session.expire(row, ['driver_profile'])
                    after = serialize_driver(row)
            else:
                row = Store() if creating else Store.query.filter_by(id=record_id).with_for_update().first()
                if row is None: return jsonify({'error': 'loja não encontrada'}), 404
                before = {} if creating else serialize_store(row)
                for key, limit in [('name', 140), ('slug', 160), ('description', 2000),
                                   ('address', 255), ('city', 100), ('state', 2), ('zip_code', 20)]:
                    if creating or key in data:
                        setattr(row, key, text_field(data, key, limit, key == 'name'))
                if row.state: row.state = row.state.upper()
                for key in ('owner_id', 'category_id'):
                    if creating or key in data:
                        value = data.get(key)
                        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                            raise ValueError(f'{key} deve ser ID inteiro positivo')
                        if key == 'owner_id':
                            owner = db.session.get(User, value)
                            if not owner or not owner.active or owner.role not in {'merchant', 'admin'}:
                                raise ValueError('Proprietário deve ser lojista/admin ativo')
                        else:
                            category = db.session.get(Category, value)
                            if not category or not category.active:
                                raise ValueError('Categoria inválida/inativa')
                        setattr(row, key, value)
                if creating or 'commission_percent' in data:
                    row.commission_rate = parse_decimal(data.get('commission_percent', '12.5'),
                        'commission_percent', maximum=Decimal('100')) / 100
                for key, default in [('active', True), ('accepts_orders', True),
                                     ('accepts_services', False), ('accepts_appointments', False)]:
                    setattr(row, key, flag(data, key, default if creating else getattr(row, key)))
                if creating: db.session.add(row)
                db.session.flush()
                after = serialize_store(row)
            db.session.add(AuditLog(actor_id=g.current_user.id,
                action='admin_create' if creating else 'admin_update', entity=kind,
                entity_id=row.id, diff={'before': before, 'after': after,
                                        'password_changed': bool(data.get('password'))},
                ip=request.remote_addr))
            db.session.commit()
            return jsonify(after), 201 if creating else 200
        except ValueError as error:
            db.session.rollback()
            return jsonify({'error': str(error)}), 400
        except IntegrityError:
            db.session.rollback()
            return jsonify({'error': 'E-mail, documento, CNH, placa ou slug já cadastrado'}), 409

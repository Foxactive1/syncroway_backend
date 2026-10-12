from datetime import datetime, timedelta, timezone
from functools import wraps
from uuid import uuid4

import jwt
from flask import current_app, g, jsonify, request

from .extensions import db
from .models import RefreshToken, User
from .utils import naive_utc, utcnow


def create_tokens(user: User, *, commit: bool = True) -> dict:
    """
    Emite par access+refresh. Persiste refresh para permitir revogação.

    commit=True (default): comportamento legado — comita a transação inteira.
    commit=False: apenas flush; útil quando o caller tem mudanças pendentes
    (ex.: user.last_login_at) que devem ser comitadas atomicamente.
    """
    now = datetime.now(timezone.utc)
    access_minutes = int(current_app.config["JWT_ACCESS_MINUTES"])
    refresh_days = int(current_app.config["JWT_REFRESH_DAYS"])

    access_payload = {
        "sub": str(user.id),
        "role": user.role,
        "type": "access",
        "jti": uuid4().hex,
        "iat": now,
        "exp": now + timedelta(minutes=access_minutes),
    }
    access_token = jwt.encode(
        access_payload, current_app.config["SECRET_KEY"], algorithm="HS256"
    )

    expires_at = now + timedelta(days=refresh_days)
    refresh_payload = {
        "sub": str(user.id),
        "type": "refresh",
        "jti": uuid4().hex,
        "iat": now,
        "exp": expires_at,
    }
    refresh_token = jwt.encode(
        refresh_payload, current_app.config["SECRET_KEY"], algorithm="HS256"
    )

    # Housekeeping: remove refresh tokens expirados desse usuário.
    RefreshToken.query.filter(
        RefreshToken.user_id == user.id,
        RefreshToken.expires_at < utcnow(),
    ).delete(synchronize_session=False)

    db.session.add(RefreshToken(
        user_id=user.id,
        token=refresh_token,
        expires_at=naive_utc(expires_at),
    ))
    if commit:
        db.session.commit()
    else:
        db.session.flush()

    return {"access_token": access_token, "refresh_token": refresh_token}


def _decode_token(token: str, expected_type: str = "access"):
    try:
        payload = jwt.decode(
            token, current_app.config["SECRET_KEY"], algorithms=["HS256"]
        )
        if payload.get("type") != expected_type:
            return None
        return payload
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, TypeError, ValueError):
        return None


def get_current_user():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    payload = _decode_token(auth[7:].strip(), expected_type="access")
    if not payload:
        return None
    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        return None
    user = db.session.get(User, user_id)
    if not user or not user.active:
        return None
    return user


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"error": "autenticação necessária ou token expirado"}), 401
        g.current_user = user
        return fn(*args, **kwargs)
    return wrapper


def require_roles(*roles):
    def decorator(fn):
        @wraps(fn)
        @require_auth
        def wrapper(*args, **kwargs):
            if g.current_user.role not in roles:
                return jsonify({"error": "permissão insuficiente"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator
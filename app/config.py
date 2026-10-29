import os
from pathlib import Path

from dotenv import load_dotenv
from .utils import as_bool, parse_decimal

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def normalize_database_url(url: str | None) -> str:
    if not url:
        return f"sqlite:///{BASE_DIR / 'instance' / 'syncroway.db'}"
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


_DATABASE_URL = normalize_database_url(os.getenv("DATABASE_URL"))
_IS_SQLITE = _DATABASE_URL.startswith("sqlite")


class Config:
    APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
    SQLALCHEMY_DATABASE_URI = _DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # SQLite (dev/Termux) usa SingletonThreadPool e ignora essas opções.
    # Postgres precisa de pool_pre_ping para sobreviver a restarts/rede móvel.
    SQLALCHEMY_ENGINE_OPTIONS = (
        {} if _IS_SQLITE else {"pool_pre_ping": True, "pool_recycle": 280}
    )
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me-use-32-bytes-minimum")
    JWT_ACCESS_MINUTES = int(os.getenv("JWT_ACCESS_MINUTES", "15"))
    JWT_REFRESH_DAYS = int(os.getenv("JWT_REFRESH_DAYS", "7"))
    CORS_ORIGINS = [
    value.strip()
    for value in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5500,http://localhost:8000,http://localhost:8080,"
        "http://127.0.0.1:5500,http://127.0.0.1:8000,http://127.0.0.1:8080,"
        "http://localhost:5173,","https://syncroway.com.br/","https://www.syncroway.com.br"
        "https://syncroway-marketplace.innovaideia2023.chatgpt.site",
    ).split(",")
    if value.strip()
]
    API_PREFIX = os.getenv("API_PREFIX", "/api")
    DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "America/Sao_Paulo")
    ASAAS_ENABLED = as_bool(os.getenv("ASAAS_ENABLED"))
    ASAAS_ENV = os.getenv("ASAAS_ENV", "sandbox").strip().lower()
    ASAAS_API_KEY = os.getenv("ASAAS_API_KEY", "")
    ASAAS_WEBHOOK_TOKEN = os.getenv("ASAAS_WEBHOOK_TOKEN", "")
    DELIVERY_FEE_PER_STORE = str(parse_decimal(os.getenv("DELIVERY_FEE_PER_STORE", "0"), "frete", maximum=9999999999))
    # Legacy local demos only. Never honored in production.
    DELIVERY_FEE_FROM_CLIENT = False
    ORDER_PAYMENT_MINUTES = int(os.getenv("ORDER_PAYMENT_MINUTES", "30"))
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
    PROXY_HOPS = int(os.getenv("PROXY_HOPS", "0"))
    RATELIMIT_ENABLED = True
    JSON_SORT_KEYS = False
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", str(2 * 1024 * 1024)))

    @classmethod
    def validate(cls):
        if cls.APP_ENV == "production" and cls.SECRET_KEY == "dev-only-change-me-use-32-bytes-minimum":
            raise RuntimeError("SECRET_KEY deve ser configurada em produção")
        if len(cls.SECRET_KEY) < 32:
            raise RuntimeError("SECRET_KEY deve possuir ao menos 32 caracteres")
        if cls.APP_ENV == "production" and cls.SQLALCHEMY_DATABASE_URI.startswith("sqlite"):
            raise RuntimeError("Produção exige PostgreSQL; configure DATABASE_URL")
        if cls.APP_ENV == "production" and cls.RATELIMIT_STORAGE_URI == "memory://":
            raise RuntimeError("Configure RATELIMIT_STORAGE_URI com Redis em produção")
        if cls.PROXY_HOPS not in (0, 1):
            raise RuntimeError("PROXY_HOPS deve ser 0 ou 1")
        if cls.ORDER_PAYMENT_MINUTES < 1:
            raise RuntimeError("ORDER_PAYMENT_MINUTES deve ser positivo")
        if cls.ASAAS_ENABLED:
            if cls.ASAAS_ENV not in {"sandbox", "production"}:
                raise RuntimeError("ASAAS_ENV inválido")
            if not cls.ASAAS_API_KEY or len(cls.ASAAS_WEBHOOK_TOKEN) < 32:
                raise RuntimeError("Configure ASAAS_API_KEY e ASAAS_WEBHOOK_TOKEN (32+ caracteres)")

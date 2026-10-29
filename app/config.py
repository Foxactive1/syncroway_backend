import os
from pathlib import Path

from dotenv import load_dotenv

from .utils import as_bool, parse_decimal

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DEV_SECRET = "dev-only-change-me-use-32-bytes-minimum"


def normalize_database_url(url: str | None) -> str:
    if not url:
        return f"sqlite:///{BASE_DIR / 'instance' / 'syncroway.db'}"
    for old in ("postgres://", "postgresql://"):
        if url.startswith(old):
            return url.replace(old, "postgresql+psycopg://", 1)
    return url


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} deve ser um inteiro (recebido: {raw!r})")


def parse_origins(raw: str) -> list[str]:
    """Separa por vírgula, remove espaços e barra final (CORS exige origem exata)."""
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


_APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
_IS_PROD = _APP_ENV == "production"
_DATABASE_URL = normalize_database_url(os.getenv("DATABASE_URL"))
_IS_SQLITE = _DATABASE_URL.startswith("sqlite")

_PROD_ORIGINS = (
    "https://syncroway.com.br,"
    "https://www.syncroway.com.br,"
    "https://syncroway-marketplace.innovaideia2023.chatgpt.site"
)
_DEV_ORIGINS = (
    "http://localhost:5500,http://localhost:8000,http://localhost:8080,"
    "http://localhost:5173,"
    "http://127.0.0.1:5500,http://127.0.0.1:8000,http://127.0.0.1:8080"
)


class Config:
    APP_ENV = _APP_ENV
    SQLALCHEMY_DATABASE_URI = _DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # SQLite (dev/Termux) usa SingletonThreadPool e ignora essas opções.
    # Postgres precisa de pool_pre_ping para sobreviver a restarts/rede móvel.
    SQLALCHEMY_ENGINE_OPTIONS = (
        {} if _IS_SQLITE else {"pool_pre_ping": True, "pool_recycle": 280}
    )

    SECRET_KEY = os.getenv("SECRET_KEY", DEV_SECRET)
    JWT_ALGORITHM = "HS256"
    JWT_ACCESS_MINUTES = env_int("JWT_ACCESS_MINUTES", 15)
    JWT_REFRESH_DAYS = env_int("JWT_REFRESH_DAYS", 7)

    # Em produção, só origens oficiais; localhost apenas em desenvolvimento.
    CORS_ORIGINS = parse_origins(
        os.getenv("CORS_ORIGINS", _PROD_ORIGINS if _IS_PROD else f"{_DEV_ORIGINS},{_PROD_ORIGINS}")
    )

    API_PREFIX = os.getenv("API_PREFIX", "/api")
    DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "America/Sao_Paulo")

    ASAAS_ENABLED = as_bool(os.getenv("ASAAS_ENABLED"))
    ASAAS_ENV = os.getenv("ASAAS_ENV", "sandbox").strip().lower()
    ASAAS_API_KEY = os.getenv("ASAAS_API_KEY", "")
    ASAAS_WEBHOOK_TOKEN = os.getenv("ASAAS_WEBHOOK_TOKEN", "")

    DELIVERY_FEE_PER_STORE = str(
        parse_decimal(os.getenv("DELIVERY_FEE_PER_STORE", "0"), "frete", maximum=9999999999)
    )
    # Somente demos locais; nunca honrado em produção.
    DELIVERY_FEE_FROM_CLIENT = False

    ORDER_PAYMENT_MINUTES = env_int("ORDER_PAYMENT_MINUTES", 30)
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_ENABLED = True
    PROXY_HOPS = env_int("PROXY_HOPS", 0)
    JSON_SORT_KEYS = False
    MAX_CONTENT_LENGTH = env_int("MAX_CONTENT_LENGTH", 2 * 1024 * 1024)

    @classmethod
    def validate(cls):
        if cls.APP_ENV == "production":
            if cls.SECRET_KEY == DEV_SECRET:
                raise RuntimeError("SECRET_KEY deve ser configurada em produção")
            if cls.SQLALCHEMY_DATABASE_URI.startswith("sqlite"):
                raise RuntimeError("Produção exige PostgreSQL; configure DATABASE_URL")
            if cls.RATELIMIT_STORAGE_URI == "memory://":
                raise RuntimeError("Configure RATELIMIT_STORAGE_URI com Redis em produção")
            if any(o.startswith("http://") for o in cls.CORS_ORIGINS):
                raise RuntimeError("CORS_ORIGINS em produção deve usar apenas https://")
        if len(cls.SECRET_KEY) < 32:
            raise RuntimeError("SECRET_KEY deve possuir ao menos 32 caracteres")
        if cls.PROXY_HOPS not in (0, 1):
            raise RuntimeError("PROXY_HOPS deve ser 0 ou 1")
        if cls.ORDER_PAYMENT_MINUTES < 1:
            raise RuntimeError("ORDER_PAYMENT_MINUTES deve ser positivo")
        if cls.ASAAS_ENABLED:
            if cls.ASAAS_ENV not in {"sandbox", "production"}:
                raise RuntimeError("ASAAS_ENV inválido")
            if not cls.ASAAS_API_KEY or len(cls.ASAAS_WEBHOOK_TOKEN) < 32:
                raise RuntimeError(
                    "Configure ASAAS_API_KEY e ASAAS_WEBHOOK_TOKEN (32+ caracteres)"
                )

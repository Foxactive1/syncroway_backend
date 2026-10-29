import os
from pathlib import Path

from dotenv import load_dotenv

from .utils import as_bool, parse_decimal

BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(parents=True, exist_ok=True)

# Âncora em BASE_DIR (A) — funciona independente do cwd.
load_dotenv(BASE_DIR / ".env")

DEV_SECRET = "dev-only-change-me-use-32-bytes-minimum"


def normalize_database_url(url: str | None) -> str:
    """Normaliza DATABASE_URL e ancora SQLite relativo em instance/ (A)."""
    if not url:
        return f"sqlite:///{INSTANCE_DIR / 'syncroway.db'}"

    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)

    if url.startswith("sqlite:///"):
        if url.startswith("sqlite:////"):       # absoluto
            return url
        rest = url[len("sqlite:///"):]
        if rest.startswith(":memory:"):
            return url
        return f"sqlite:///{INSTANCE_DIR / rest}"

    return url


def env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} deve ser um inteiro (recebido: {raw!r})")


def parse_origins(raw: str) -> list[str]:
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
    # ─── App ─────────────────────────────────────────────
    APP_ENV = _APP_ENV
    SECRET_KEY = os.getenv("SECRET_KEY", DEV_SECRET)

    # ─── Banco de dados ──────────────────────────────────
    SQLALCHEMY_DATABASE_URI = _DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # SQLite (dev/reloader) precisa de check_same_thread=False;
    # Postgres precisa de pool_pre_ping para sobreviver a restarts.
    SQLALCHEMY_ENGINE_OPTIONS = (
        {"connect_args": {"check_same_thread": False}}
        if _IS_SQLITE
        else {"pool_pre_ping": True, "pool_recycle": 280}
    )

    # ─── Auth ────────────────────────────────────────────
    JWT_ALGORITHM = "HS256"
    JWT_ACCESS_MINUTES = env_int("JWT_ACCESS_MINUTES", 15)
    JWT_REFRESH_DAYS = env_int("JWT_REFRESH_DAYS", 7)

    # ─── HTTP / API ──────────────────────────────────────
    # Em produção: só domínios oficiais. Em dev: dev + prod.
    CORS_ORIGINS = parse_origins(
        os.getenv(
            "CORS_ORIGINS",
            _PROD_ORIGINS if _IS_PROD else f"{_DEV_ORIGINS},{_PROD_ORIGINS}",
        )
    )
    API_PREFIX = os.getenv("API_PREFIX", "/api")
    DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "America/Sao_Paulo")
    JSON_SORT_KEYS = False
    MAX_CONTENT_LENGTH = env_int("MAX_CONTENT_LENGTH", 2 * 1024 * 1024)

    # ─── Rate limit / proxy ──────────────────────────────
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_ENABLED = True
    PROXY_HOPS = env_int("PROXY_HOPS", 0)

    # ─── Negócio ─────────────────────────────────────────
    DELIVERY_FEE_PER_STORE = str(
        parse_decimal(os.getenv("DELIVERY_FEE_PER_STORE", "0"), "frete", maximum=9999999999)
    )
    DELIVERY_FEE_FROM_CLIENT = False  # só demo; ignorado em prod
    ORDER_PAYMENT_MINUTES = env_int("ORDER_PAYMENT_MINUTES", 30)
    PAYMENT_RELEASE_DAYS = env_int("PAYMENT_RELEASE_DAYS", 2)

    # ─── Asaas ───────────────────────────────────────────
    ASAAS_ENABLED = as_bool(os.getenv("ASAAS_ENABLED"))
    ASAAS_ENV = os.getenv("ASAAS_ENV", "sandbox").strip().lower()
    ASAAS_API_KEY = (os.getenv("ASAAS_API_KEY") or "").strip()
    ASAAS_WEBHOOK_TOKEN = (os.getenv("ASAAS_WEBHOOK_TOKEN") or "").strip()
    ASAAS_SPLIT_ENABLED = as_bool(os.getenv("ASAAS_SPLIT_ENABLED"))
    MANUAL_SETTLE_ALLOWED = as_bool(os.getenv("MANUAL_SETTLE_ALLOWED"))

    @classmethod
    def validate(cls):
        # ─── App ─────────────────────────────────────────
        if len(cls.SECRET_KEY) < 32:
            raise RuntimeError("SECRET_KEY deve possuir ao menos 32 caracteres")

        if cls.APP_ENV == "production":
            if cls.SECRET_KEY == DEV_SECRET:
                raise RuntimeError("SECRET_KEY deve ser configurada em produção")
            if cls.SQLALCHEMY_DATABASE_URI.startswith("sqlite"):
                raise RuntimeError("Produção exige PostgreSQL; configure DATABASE_URL")
            if cls.RATELIMIT_STORAGE_URI == "memory://":
                raise RuntimeError("Configure RATELIMIT_STORAGE_URI (ex.: Redis) em produção")
            if any(o.startswith("http://") for o in cls.CORS_ORIGINS):
                raise RuntimeError("CORS_ORIGINS em produção deve usar apenas https://")

        # ─── Invariantes ─────────────────────────────────
        if cls.PROXY_HOPS not in (0, 1):
            raise RuntimeError("PROXY_HOPS deve ser 0 ou 1")
        if cls.ORDER_PAYMENT_MINUTES < 1:
            raise RuntimeError("ORDER_PAYMENT_MINUTES deve ser positivo")
        if cls.PAYMENT_RELEASE_DAYS < 0:
            raise RuntimeError("PAYMENT_RELEASE_DAYS não pode ser negativo")

        # ─── Asaas ───────────────────────────────────────
        if not cls.ASAAS_ENABLED:
            if cls.ASAAS_SPLIT_ENABLED:
                raise RuntimeError("ASAAS_SPLIT_ENABLED=true exige ASAAS_ENABLED=true")
            return

        if cls.ASAAS_ENV not in {"sandbox", "production"}:
            raise RuntimeError("ASAAS_ENV deve ser 'sandbox' ou 'production'")
        if not cls.ASAAS_API_KEY:
            raise RuntimeError("ASAAS_API_KEY é obrigatória quando ASAAS_ENABLED=true")
        if len(cls.ASAAS_WEBHOOK_TOKEN) < 32:
            raise RuntimeError(
                "ASAAS_WEBHOOK_TOKEN (≥32 caracteres) é obrigatório quando ASAAS_ENABLED=true"
            )

        # Guard-rails contra trocar a chave e esquecer o env.
        if cls.ASAAS_ENV == "production" and cls.ASAAS_API_KEY.startswith("$aact_hmlg_"):
            raise RuntimeError(
                "ASAAS_ENV=production mas a chave parece ser sandbox ($aact_hmlg_). "
                "Use a chave de produção ($aact_prod_)."
            )
        if cls.ASAAS_ENV == "sandbox" and cls.ASAAS_API_KEY.startswith("$aact_prod_"):
            raise RuntimeError(
                "ASAAS_ENV=sandbox mas a chave parece ser produção ($aact_prod_). "
                "Use a chave de sandbox ($aact_hmlg_)."
            )

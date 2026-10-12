"""Helpers compartilhados entre rotas, auth e serializers."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


def utcnow():
    """Retorna datetime *naive* em UTC (padrão do banco)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def naive_utc(value):
    """Normaliza qualquer datetime para naive-UTC.

    - None → None
    - naive → assume que já é UTC, devolve igual
    - aware → converte para UTC e remove tzinfo

    Usado em auth.py para comparar expiração de tokens com `utcnow()`.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def iso_utc(value):
    """Serializa datetime naive-UTC (ou aware) com offset explícito.

    Sem isso, o JS interpreta a string como local time e desloca o fuso
    do usuário — ex.: pedido às 10:30 em SP aparece como 13:30.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def money(value):
    return float(value or Decimal("0"))


def as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "sim", "on"}


def parse_decimal(value, field, *,
                  minimum=Decimal("0"),
                  maximum=None):
    """Converte entrada para Decimal rejeitando não-finitos e fora de faixa.

    Além de NaN/Infinity, rejeita valores com expoente bruto acima de 15
    (usando `.adjusted()`), o que pega `1e999999` mesmo se uma comparação
    com `Decimal("1e15")` fosse otimizada de forma inesperada pelo
    interpretador.
    """
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} inválido") from exc

    if not parsed.is_finite():
        raise ValueError(f"{field} inválido")

    # `adjusted()` = expoente do dígito mais significativo.
    # 15 = teto absoluto (10^15) — bem acima de NUMERIC(12,2).
    if parsed.adjusted() > 15 or parsed.adjusted() < -15:
        raise ValueError(f"{field} fora do intervalo permitido")

    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field} não pode ser menor que {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{field} não pode ser maior que {maximum}")

    return parsed
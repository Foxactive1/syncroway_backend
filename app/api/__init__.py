"""Rotas HTTP da API SyncroWay."""
from .routes import api_bp  # noqa: F401

__all__ = ["api_bp"]
from ..admin_management import register_admin_management
register_admin_management(api_bp)

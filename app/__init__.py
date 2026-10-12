"""Application factory do backend SyncroWay."""
import sqlite3
from pathlib import Path
from sqlalchemy.exc import DBAPIError, OperationalError
from flask import Flask, jsonify, request
from sqlalchemy import event
from sqlalchemy.engine import Engine

from .config import Config
from .extensions import cors, db, migrate, limiter


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    """Mantém o SQLite com as mesmas garantias referenciais do PostgreSQL."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_app(config_object=Config):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object)

    validator = getattr(config_object, "validate", None)
    if callable(validator):
        validator()

    if app.config.get("PROXY_HOPS"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    limiter.init_app(app)
    db.init_app(app)
    migrate.init_app(app, db)
    cors.init_app(
        app,
        resources={
            rf"{app.config['API_PREFIX']}/*": {
                "origins": app.config["CORS_ORIGINS"],
            }
        },
    )

    @app.before_request
    def validate_json_object():
        if request.is_json and not isinstance(request.get_json(silent=True), dict):
            return jsonify({"error": "O corpo JSON deve ser um objeto válido"}), 400

    from .api import api_bp

    app.register_blueprint(api_bp, url_prefix=app.config["API_PREFIX"])

    @app.get("/")
    def index():
        return jsonify({
            "service": "SyncroWay API",
            "status": "online",
            "health": f"{app.config['API_PREFIX']}/health",
        })

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({"error": "recurso não encontrado"}), 404

    @app.errorhandler(405)
    def method_not_allowed(_error):
        return jsonify({"error": "método não permitido"}), 405

    from .operations import register_operations
    register_operations(app)

    @app.errorhandler(429)
    def rate_limited(_error):
        return jsonify({"error": "Muitas tentativas. Aguarde antes de tentar novamente."}), 429

    @app.errorhandler(500)
    def internal_error(_error):
        db.session.rollback()
        return jsonify({"error": "Falha interna. Tente novamente ou contate o suporte."}), 500

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if app.config["APP_ENV"] == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.path.startswith(app.config["API_PREFIX"]):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app
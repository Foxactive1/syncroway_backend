"""Application factory do backend SyncroWay."""
import sqlite3
from pathlib import Path
from sqlalchemy.exc import DBAPIError, OperationalError
from flask import Flask, jsonify
from sqlalchemy import event
from sqlalchemy.engine import Engine

from .config import Config
from .extensions import cors, db, migrate


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

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

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

    return app
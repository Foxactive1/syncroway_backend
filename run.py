"""Ponto de entrada para desenvolvimento local.

Em produção use gunicorn/waitress — este arquivo é apenas para `python run.py`.
"""
import logging
import os

from app import create_app
from app.utils import as_bool

app = create_app()


if __name__ == "__main__":
    # Logs estruturados aparecem no stdout durante desenvolvimento.
    # Em produção (gunicorn), deixe o logging do servidor dominar.
    logging.basicConfig(
        level=(
            logging.DEBUG
            if as_bool(os.getenv("FLASK_DEBUG"), default=False)
            else logging.INFO
        ),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 127.0.0.1 por padrão: expor em todas as interfaces (0.0.0.0) é opt-in.
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
        debug=as_bool(os.getenv("FLASK_DEBUG"), default=False),
    )
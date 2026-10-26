"""Entry point for gunicorn: gunicorn furnace.wsgi:app"""

import logging

from .api import create_app

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = create_app()

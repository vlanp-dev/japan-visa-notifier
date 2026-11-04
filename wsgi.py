"""WSGI entry point for a production server (gunicorn, uWSGI, mod_wsgi, ...).

Example::

    gunicorn wsgi:application

The module builds the application at import time so WSGI servers can use the
``application`` callable directly.
"""

from app.config import load_config
from app.logging_config import configure_logging
from app.web import create_app

_config = load_config()
configure_logging(_config.log_level)
application = create_app(_config)

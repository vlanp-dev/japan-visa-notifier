"""Episode alert service.

The package is intentionally built only on the Python standard library so the
service can run in a minimal environment without installing dependencies.
"""

__version__ = "0.1.0"

from .config import Config, ConfigError, load_config  # noqa: E402
from .web import create_app  # noqa: E402

__all__ = ["Config", "ConfigError", "__version__", "create_app", "load_config"]

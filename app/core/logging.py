"""Centralised logging setup.

Adopted pattern from SmartSuggest's ``app_factory.setup_logging`` — one place that
configures the root logger, level driven by ``settings.log_level``. Kept to the
stdlib (no Loki/vendor lock); structured shipping (Loki/CloudWatch) is an ops
concern layered on top of stdout JSON later.
"""

import logging
from logging.config import dictConfig

from app.core.config import settings

_configured = False


def setup_logging() -> logging.Logger:
    """Configure root logging once and return the app logger.

    Idempotent: safe to call from ``create_app`` and from scripts.

    Returns:
        logging.Logger: Logger named ``evix``.
    """
    global _configured
    if not _configured:
        dictConfig(
            {
                "version": 1,
                "disable_existing_loggers": False,
                "formatters": {
                    "default": {
                        "format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                        "datefmt": "%Y-%m-%dT%H:%M:%S",
                    },
                },
                "handlers": {
                    "console": {
                        "class": "logging.StreamHandler",
                        "formatter": "default",
                        "stream": "ext://sys.stdout",
                    },
                },
                "root": {
                    "level": settings.log_level.upper(),
                    "handlers": ["console"],
                },
            }
        )
        _configured = True
    return logging.getLogger("evix")

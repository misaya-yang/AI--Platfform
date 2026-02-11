import logging
from logging.config import dictConfig

from .settings import Settings


def configure_logging(settings: Settings) -> None:
    level = "INFO"
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"default": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "level": level,
                }
            },
            "root": {"handlers": ["console"], "level": level},
        }
    )

    logging.getLogger("httpx").setLevel("WARNING")

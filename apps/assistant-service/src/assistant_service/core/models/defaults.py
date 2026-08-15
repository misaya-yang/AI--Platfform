"""Deployment-wide default model for the assistant service.

``DEFAULT_MODEL`` (unprefixed env var) is the single deployment default for
chat-model selection. SDKs, the console, and the gateway omit a concrete
model ID unless one is explicitly requested; the assistant service applies
this value whenever a caller does not supply one.

The gateway mirrors this value in ``src/config/settings.py`` as
``default_model`` (``validation_alias="DEFAULT_MODEL"``); keep both in sync.
"""

import os

DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "qwen3.7-plus")

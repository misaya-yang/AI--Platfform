"""Backward-compatible import path for document parsing."""

from __future__ import annotations

import sys

from assistant_service.core.files import document_parser as _document_parser

sys.modules[__name__] = _document_parser

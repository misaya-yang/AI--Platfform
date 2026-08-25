from __future__ import annotations

import io
import zipfile

import pytest

from src.api.internal.attachment_capabilities import _extract_content, _safe_zip_members


def test_text_attachment_is_bounded_and_truncates() -> None:
    metadata, content, truncated = _extract_content(b"hello world", "note.txt", "text/plain", 5)
    assert metadata["format"] == "txt"
    assert content == "hello"
    assert truncated is True


def test_zip_traversal_is_rejected() -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("../escape.xml", b"bad")
    with pytest.raises(ValueError, match="unsafe"):
        _safe_zip_members(stream.getvalue())

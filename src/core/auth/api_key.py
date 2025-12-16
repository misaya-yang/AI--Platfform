from __future__ import annotations

from typing import List

from ..exceptions import AuthError


def verify_api_key(key: str, valid_keys: List[str]) -> None:
    if key not in valid_keys:
        raise AuthError("Invalid API key")

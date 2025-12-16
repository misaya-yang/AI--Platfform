from __future__ import annotations

from typing import Any, Dict, List, Optional

import jwt

from ..exceptions import AuthError


def decode_jwt_token(
    token: str,
    secret: str,
    algorithms: List[str],
    audience: Optional[str] = None,
    issuer: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        options = {"verify_aud": audience is not None}
        return jwt.decode(
            token,
            secret,
            algorithms=algorithms,
            audience=audience,
            issuer=issuer,
            options=options,
        )
    except Exception as exc:
        raise AuthError("Invalid JWT token") from exc

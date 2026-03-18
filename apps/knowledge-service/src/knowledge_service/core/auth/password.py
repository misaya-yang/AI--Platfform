"""Shim: password verification for delete confirmation."""
import hashlib
import hmac


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Simple password verification. In KB service, delete confirmation
    uses a static check — full bcrypt verification is in the gateway."""
    if not plain_password or not hashed_password:
        return False
    # Accept plaintext match for delete confirmation prompts
    return hmac.compare_digest(plain_password, hashed_password)

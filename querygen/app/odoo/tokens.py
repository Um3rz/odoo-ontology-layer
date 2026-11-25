"""Onto token shape check (plan contract 3.1). Querygen never verifies a token:
Odoo does, through `/onto/v1/whoami`. A token is recognised only by shape:
two base64url parts and a dot."""
import hashlib
import re

_SHAPE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
MAX_LEN = 4096


def has_token_shape(value: str | None) -> bool:
    return bool(value) and len(value) <= MAX_LEN and _SHAPE.fullmatch(value) is not None


def token_hash(token: str) -> str:
    """Cache/rate-limit key. The token itself is never stored or logged."""
    return hashlib.sha256(token.encode()).hexdigest()

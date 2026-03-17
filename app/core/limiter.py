"""
Rate limiter — shared slowapi Limiter instance.

Default key: remote IP address (for auth + invite endpoints).
User key: SHA-256 hash of bearer token (for per-user limits on messages + follows).
"""

import hashlib

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def _get_user_key(request: Request) -> str:
    """Hash the full bearer token to produce a unique per-user rate limit key."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and len(auth) > 20:
        token_hash = hashlib.sha256(auth[7:].encode()).hexdigest()[:16]
        return f"user:{token_hash}"
    return get_remote_address(request)


limiter = Limiter(key_func=get_remote_address)

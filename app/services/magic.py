"""
Magic Link service layer.

Handles DID token verification for user authentication.
The iOS app sends a short-lived DID token after Magic login.
We verify it here, extract the user's EOA address and email,
then discard the token — it is never stored.
"""

import logging
from dataclasses import dataclass

from magic_admin import Magic
from magic_admin.error import DIDTokenExpired, DIDTokenInvalid, DIDTokenMalformed, RequestError

from app.core.config import MAGIC_SECRET_KEY

logger = logging.getLogger(__name__)

_magic: Magic | None = None


def _get_magic() -> Magic:
    """Singleton Magic client."""
    global _magic
    if _magic is None:
        _magic = Magic(api_secret_key=MAGIC_SECRET_KEY)
    return _magic


@dataclass
class MagicUserInfo:
    eoa_address: str  # 0x... — permanent identity, stored in DB
    email: str


def verify_did_token(did_token: str) -> MagicUserInfo:
    """
    Verify a Magic DID token sent from the iOS app.

    Validates the token signature and expiry against Magic's API,
    then extracts the user's EOA address and email.

    Args:
        did_token: Short-lived token from Magic SDK on iOS (expires in 15 min)

    Returns:
        MagicUserInfo with eoa_address and email

    Raises:
        ValueError: If token is invalid, expired, or Magic API call fails
    """
    magic = _get_magic()

    try:
        magic.Token.validate(did_token)
    except (DIDTokenExpired, DIDTokenInvalid, DIDTokenMalformed) as e:
        logger.warning("Invalid DID token: %s", e)
        raise ValueError(f"Invalid or expired DID token: {e}")

    try:
        issuer = magic.Token.get_issuer(did_token)
        metadata = magic.User.get_metadata_by_issuer(issuer)
    except RequestError as e:
        logger.error("Magic API request failed: %s", e)
        raise ValueError(f"Failed to fetch user metadata from Magic: {e}")

    eoa_address = metadata.data.get("public_address")
    email = metadata.data.get("email")

    if not eoa_address:
        raise ValueError("Magic token missing public_address")

    return MagicUserInfo(eoa_address=eoa_address, email=email or "")

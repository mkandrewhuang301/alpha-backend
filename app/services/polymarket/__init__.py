"""
Polymarket service package.

Re-exports all symbols from wallet.py for backwards compatibility —
existing `from app.services.polymarket import X` imports continue to work.
"""

from app.services.polymarket.wallet import (  # noqa: F401
    # Constants
    SAFE_FACTORY,
    SAFE_FACTORY_NAME,
    SAFE_INIT_CODE_HASH,
    PROXY_FACTORY,
    PROXY_INIT_CODE_HASH,
    ZERO_ADDRESS,
    POLYGON_CHAIN_ID,
    CLOB_AUTH_MESSAGE,
    # Wallet derivation
    derive_proxy_wallet,
    derive_safe,
    # Safe deployment
    SafeDeployPayload,
    SafeDeployResult,
    get_safe_deploy_payload,
    deploy_safe,
    # CLOB credentials
    ClobSigningMessage,
    ClobCredentials,
    get_clob_signing_message,
    derive_clob_credentials,
    encrypt_credential,
    decrypt_credential,
    # CLOB auth headers
    build_user_clob_headers,
    get_user_clob_credentials,
    # Internal but imported by other services
    _build_hmac_signature,
)

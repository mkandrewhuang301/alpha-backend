import httpx
import base64
import time
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from app.core.config import KALSHI_API_KEY, KALSHI_PRIVATE_KEY, KALSHI_BASE_URL


def _get_auth_headers(method: str, path: str) -> dict:
    timestamp_ms = str(int(time.time() * 1000))
    message = timestamp_ms + method.upper() + path
    private_key = serialization.load_pem_private_key(
        KALSHI_PRIVATE_KEY.encode(), password=None
    )
    signature = private_key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = base64.b64encode(signature).decode()
    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
        "Content-Type": "application/json",
    }


async def get_markets(status: str = None, series_ticker: str = None, event_ticker: str = None, limit: int = None):
    path = "/trade-api/v2/markets"
    headers = _get_auth_headers("GET", path)
    url = f"{KALSHI_BASE_URL}/markets"
    params = {}
    if status:
        params["status"] = status
    if series_ticker:
        params["series_ticker"] = series_ticker
    if event_ticker:
        params["event_ticker"] = event_ticker
    if limit:
        params["limit"] = limit
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers, params=params)
        return response.json()
    
async def get_series(category: str = None):
    path = "/trade-api/v2/series"
    headers = _get_auth_headers("GET", path)
    url = f"{KALSHI_BASE_URL}/series"
    params = {}
    if category:
        params["category"] = category
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers, params=params)
        return response.json()


#get specific market  by ticker
async def get_market(ticker: str):
    path = f"/trade-api/v2/markets/{ticker}"
    headers = _get_auth_headers("GET", path)
    url = f"{KALSHI_BASE_URL}/markets/{ticker}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers)
        return response.json()

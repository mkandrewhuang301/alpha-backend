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


def _handle_response(response: httpx.Response) -> dict:
    if response.status_code != 200:
        return {"error": True, "status_code": response.status_code, "detail": response.text}
    if not response.content:
        return {"error": True, "status_code": response.status_code, "detail": "Empty response"}
    return response.json()


async def get_markets(status: str = None, series_ticker: str = None, event_ticker: str = None, limit: int = None, cursor: str = None):
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
    if cursor:
        params["cursor"] = cursor
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers, params=params)
        return _handle_response(response)
    
async def get_series_list(
    category: str = None,
    tags: str = None,
    include_product_metadata: bool = False,
    include_volume: bool = False,
):
    path = "/trade-api/v2/series"
    headers = _get_auth_headers("GET", path)
    url = f"{KALSHI_BASE_URL}/series"
    params = {}
    if category:
        params["category"] = category
    if tags:
        params["tags"] = tags
    if include_product_metadata:
        params["include_product_metadata"] = "true"
    if include_volume:
        params["include_volume"] = "true"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers, params=params)
        return _handle_response(response)


async def get_series_by_ticker(series_ticker: str, include_volume: bool = False):
    path = f"/trade-api/v2/series/{series_ticker}"
    headers = _get_auth_headers("GET", path)
    url = f"{KALSHI_BASE_URL}/series/{series_ticker}"
    params = {}
    if include_volume:
        params["include_volume"] = "true"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers, params=params)
        return _handle_response(response)


#get specific market  by ticker
async def get_market(ticker: str):
    path = f"/trade-api/v2/markets/{ticker}"
    headers = _get_auth_headers("GET", path)
    url = f"{KALSHI_BASE_URL}/markets/{ticker}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers)
        return _handle_response(response)


async def get_events(
    limit: int = 200,
    cursor: str = None,
    with_nested_markets: bool = False,
    with_milestones: bool = False,
    status: str = None,
    series_ticker: str = None,
):
    path = "/trade-api/v2/events"
    headers = _get_auth_headers("GET", path)
    url = f"{KALSHI_BASE_URL}/events"
    params = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    if with_nested_markets:
        params["with_nested_markets"] = "true"
    if with_milestones:
        params["with_milestones"] = "true"
    if status:
        params["status"] = status
    if series_ticker:
        params["series_ticker"] = series_ticker
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers, params=params)
        return _handle_response(response)


async def get_event(event_ticker: str, with_nested_markets: bool = False):
    path = f"/trade-api/v2/events/{event_ticker}"
    headers = _get_auth_headers("GET", path)
    url = f"{KALSHI_BASE_URL}/events/{event_ticker}"
    params = {}
    if with_nested_markets:
        params["with_nested_markets"] = "true"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers, params=params)
        return _handle_response(response)

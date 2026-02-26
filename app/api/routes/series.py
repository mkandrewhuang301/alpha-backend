from fastapi import APIRouter
from app.services.kalshi import get_series

router = APIRouter()

@router.get("/")
async def list_series(category: str = None):
    data = await get_series(category=category)
    return data

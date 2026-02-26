# In app/main.py
from fastapi import FastAPI
from app.api.routes import markets, series

app = FastAPI()

app.include_router(markets.router, prefix="/markets")
app.include_router(series.router, prefix="/series")
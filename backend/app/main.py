import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.core.config import settings
from app.core.database import connect_to_mongo, close_mongo_connection
from app.services.dhan_websocket import dhan_ws_client
from app.services.eod_scheduler import eod_auto_square_off_loop
from app.services.strategy_engine import strategy_engine_loop
from app.services.strategies import batch1, batch2, batch3, batch4  # noqa: F401 — import registers strategies
from app.api.v1.endpoints import signals, market_data, auth
from app.api.v1.endpoints import paper_trade
from app.api.v1.endpoints.market_data import ws_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("OptionSaathi-Main")


async def global_broadcast(message: Dict[str, Any]):
    for idx_key in settings.INDICES_CONFIG.keys():
        await ws_manager.broadcast_to_index(idx_key, message)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Starting OptionSaathi Python Multi-Index AI Engine...")

    await connect_to_mongo()

    ws_task = asyncio.create_task(dhan_ws_client.connect_and_listen(broadcast_callback=global_broadcast))
    logger.info("📡 Dhan Live WebSocket Feed Task Initialized in Background.")

    eod_task = asyncio.create_task(eod_auto_square_off_loop())
    logger.info("⏱️ EOD Auto Square-Off Scheduler Task Initialized in Background.")

    # NOTE: Global Market Scanner has been retired — ORB Breaker now lives in the
    # unified Strategy Engine registry (strategies/batch4.py) alongside the other
    # 11 strategies, avoiding duplicate signals under two different tags.
    strategy_task = asyncio.create_task(strategy_engine_loop(broadcast_callback=global_broadcast))
    logger.info("🎯 Strategy Engine Task Initialized in Background (12 strategies, including ORB Breaker).")

    yield

    logger.info("🛑 Shutting down OptionSaathi Engine...")
    ws_task.cancel()
    eod_task.cancel()
    strategy_task.cancel()
    await close_mongo_connection()
    logger.info("✅ Graceful Shutdown Complete.")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="2.0.0",
    description="Multi-Index AI Confluence SaaS Signal Engine for Nifty, BankNifty, Sensex & FinNifty",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if hasattr(auth, 'router'):
    app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["Authentication"])

app.include_router(signals.router, prefix=f"{settings.API_V1_STR}/signals", tags=["Signals Engine"])
app.include_router(market_data.router, prefix=f"{settings.API_V1_STR}/market", tags=["Market Data & WebSockets"])
app.include_router(paper_trade.router, prefix="/api/v1/paper", tags=["paper-trading"])


@app.get("/", tags=["Health Check"])
async def root_health_check():
    return {
        "status": "ONLINE",
        "engine": settings.PROJECT_NAME,
        "dhan_ws_connected": dhan_ws_client.is_connected,
        "version": "2.0.0"
    }


@app.get("/api/v1/maintenance-status", tags=["Health Check"])
async def get_maintenance_status():
    import os
    is_maintenance = os.environ.get("MAINTENANCE_MODE", "false").strip().lower() in ("true", "1", "yes")
    return {"maintenance": is_maintenance}


class DhanCredentialsUpdate(BaseModel):
    client_id: str
    access_token: str
    admin_key: str


@app.post("/api/v1/admin/update-dhan-credentials", tags=["Admin"])
async def update_dhan_credentials_endpoint(payload: DhanCredentialsUpdate):
    """
    Updates the Dhan client_id/access_token stored in MongoDB — takes effect
    immediately (next WebSocket reconnect / next API call), NO redeploy needed.
    Protected by admin_key matching SECRET_KEY (basic protection since this is
    a sensitive credential-write endpoint).
    """
    if payload.admin_key != settings.SECRET_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key")

    from app.services.dhan_credentials import update_dhan_credentials
    await update_dhan_credentials(payload.client_id, payload.access_token)

    return {"success": True, "message": "Dhan credentials updated — active immediately, no redeploy needed."}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
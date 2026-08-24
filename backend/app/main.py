import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Dict, Any
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import connect_to_mongo, close_mongo_connection
from app.services.dhan_websocket import dhan_ws_client
from app.services.eod_scheduler import eod_auto_square_off_loop
from app.services.market_scanner import global_market_scanner_loop
from app.api.v1.endpoints import signals, market_data, auth
from app.api.v1.endpoints import paper_trade
from app.api.v1.endpoints.market_data import ws_manager

# Logger Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("OptionSaathi-Main")


async def global_broadcast(message: Dict[str, Any]):
    """
    Fan-out a message (SIGNAL_STATUS_UPDATE, PAPER_TRADE_AUTO_CLOSED,
    GLOBAL_SIGNAL_UPDATE) to every connected frontend client across all 4 index
    WebSocket channels. The frontend listens for message.type regardless of which
    channel delivered it, so broadcasting on every channel guarantees delivery.

    NOTE: This was previously never wired up — dhan_ws_client.connect_and_listen()
    was called with no broadcast_callback, so these push-notifications silently
    never reached the frontend at all. Fixed here.
    """
    for idx_key in settings.INDICES_CONFIG.keys():
        await ws_manager.broadcast_to_index(idx_key, message)


# ----------------------------------------------------------------------------
# LIFESPAN MANAGER (FastAPI Startup & Shutdown Handler)
# ----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Startup Actions
    logger.info("🚀 Starting OptionSaathi Python Multi-Index AI Engine...")

    # Initialize Async MongoDB Driver
    await connect_to_mongo()

    # Spawn Dhan WebSocket Persistent Listener in Background Task
    ws_task = asyncio.create_task(dhan_ws_client.connect_and_listen(broadcast_callback=global_broadcast))
    logger.info("📡 Dhan Live WebSocket Feed Task Initialized in Background.")

    # Spawn EOD Auto Square-Off Scheduler (mimics real broker MIS square-off behaviour)
    eod_task = asyncio.create_task(eod_auto_square_off_loop())
    logger.info("⏱️ EOD Auto Square-Off Scheduler Task Initialized in Background.")

    # Spawn Global Market Scanner (continuous confluence scan + shared option-chain cache)
    scanner_task = asyncio.create_task(global_market_scanner_loop(broadcast_callback=global_broadcast))
    logger.info("🌐 Global Market Scanner Task Initialized in Background.")

    yield  # Application Runs Here

    # 2. Shutdown Actions
    logger.info("🛑 Shutting down OptionSaathi Engine...")
    ws_task.cancel()
    eod_task.cancel()
    scanner_task.cancel()
    await close_mongo_connection()
    logger.info("✅ Graceful Shutdown Complete.")


# ----------------------------------------------------------------------------
# FASTAPI APP INSTANTIATION
# ----------------------------------------------------------------------------
app = FastAPI(
    title=settings.PROJECT_NAME,
    version="2.0.0",
    description="Multi-Index AI Confluence SaaS Signal Engine for Nifty, BankNifty, Sensex & FinNifty",
    lifespan=lifespan
)

# ----------------------------------------------------------------------------
# CORS MIDDLEWARE SETUP
# ----------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Production me exact frontend domain se replace karein
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------------------
# API ROUTER MOUNTING
# ----------------------------------------------------------------------------
# 1. Auth Endpoint Routes
if hasattr(auth, 'router'):
    app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["Authentication"])

# 2. Signals Generator Routes (/decode, /decode-force, /automated-signals-log, /live-ltp-batch)
app.include_router(signals.router, prefix=f"{settings.API_V1_STR}/signals", tags=["Signals Engine"])

# 3. Market Data & Real-Time WebSockets Routes (/mood-live, /ws/{index_name})
app.include_router(market_data.router, prefix=f"{settings.API_V1_STR}/market", tags=["Market Data & WebSockets"])
app.include_router(paper_trade.router, prefix="/api/v1/paper", tags=["paper-trading"])


# ----------------------------------------------------------------------------
# ROOT HEALTH CHECK ENDPOINT
# ----------------------------------------------------------------------------
@app.get("/", tags=["Health Check"])
async def root_health_check():
    return {
        "status": "ONLINE",
        "engine": settings.PROJECT_NAME,
        "dhan_ws_connected": dhan_ws_client.is_connected,
        "version": "2.0.0"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
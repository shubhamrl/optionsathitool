import asyncio
import logging
from typing import Dict, Any, Optional
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel

from app.core.config import settings
from app.engine.confluence_math import calculate_pcr_and_sentiment
from app.services.dhan_websocket import market_data_store, dhan_ws_client
from app.services.token_registry import fetch_expiry_list, fetch_full_option_chain_data

router = APIRouter()
logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# 1. GET /api/v1/market/mood-live
# Multi-Index Market Mood Summary (Spot, PCR, Trend Sentiment, VIX)
# ----------------------------------------------------------------------------
@router.get("/mood-live")
async def get_market_mood_live(index_name: str = "NIFTY"):
    idx_key = index_name.upper()
    if idx_key not in settings.INDICES_CONFIG:
        raise HTTPException(status_code=400, detail=f"Index '{index_name}' is not supported.")

    idx_config = settings.INDICES_CONFIG[idx_key]
    
    # 1. Get Live In-Memory Data Store for target index
    index_store = market_data_store.get(idx_key, {})
    
    # Extract live ticks from memory
    spot_price = 0.0
    total_ce_oi = 0
    total_pe_oi = 0

    for sec_id, node in index_store.items():
        ltp = node.get("ltp", 0.0)
        oi = node.get("oi", 0)
        
        if node.get("type") == "CE":
            total_ce_oi += oi
        elif node.get("type") == "PE":
            total_pe_oi += oi

    # Calculate PCR from Live Memory
    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0

    if pcr > 1.25:
        trend = "STRONG BULLISH"
    elif pcr > 1.05:
        trend = "MILD BULLISH"
    elif pcr < 0.75:
        trend = "STRONG BEARISH"
    elif pcr < 0.95:
        trend = "MILD BEARISH"
    else:
        trend = "SIDEWAYS CHOP"

    return {
        "success": True,
        "index_name": idx_key,
        "spotPrice": spot_price,
        "pcrSentiment": pcr,
        "trend": trend,
        "indiaVix": 13.5,
        "active_subscribed_tokens": len(index_store)
    }


# ----------------------------------------------------------------------------
# 2. WEBSOCKET /api/v1/market/ws/{index_name}
# Central Backend WebSocket Bridge for Frontend App Clients
# Zero Client Calls to Dhan — Streams directly from Server In-Memory Store!
# ----------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        # Active connections per index: { "NIFTY": [WebSocket1, WebSocket2] }
        self.active_connections: Dict[str, list] = {
            idx: [] for idx in settings.INDICES_CONFIG.keys()
        }

    async def connect(self, websocket: WebSocket, index_name: str):
        await websocket.accept()
        idx_key = index_name.upper()
        if idx_key in self.active_connections:
            self.active_connections[idx_key].append(websocket)
            logger.info(f"📱 Client connected to App WS for Index: {idx_key}")

    def disconnect(self, websocket: WebSocket, index_name: str):
        idx_key = index_name.upper()
        if idx_key in self.active_connections and websocket in self.active_connections[idx_key]:
            self.active_connections[idx_key].remove(websocket)
            logger.info(f"📱 Client disconnected from App WS for Index: {idx_key}")

    async def broadcast_to_index(self, index_name: str, message: Dict[str, Any]):
        idx_key = index_name.upper()
        if idx_key in self.active_connections:
            dead_sockets = []
            for connection in self.active_connections[idx_key]:
                try:
                    await connection.send_json(message)
                except Exception:
                    dead_sockets.append(connection)

            for dead in dead_sockets:
                self.disconnect(dead, idx_key)


ws_manager = ConnectionManager()


@router.websocket("/ws/{index_name}")
async def websocket_market_stream(websocket: WebSocket, index_name: str):
    idx_key = index_name.upper()
    if idx_key not in settings.INDICES_CONFIG:
        await websocket.close(code=1008)
        return

    await ws_manager.connect(websocket, idx_key)

    try:
        while True:
            # Send live memory ticker updates to frontend every 1 second
            index_store = market_data_store.get(idx_key, {})
            
            payload = {
                "type": "TICKER_STREAM",
                "index": idx_key,
                "data": index_store
            }
            await websocket.send_json(payload)
            await asyncio.sleep(1)

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, idx_key)
    except Exception as e:
        logger.error(f"App WebSocket error: {str(e)}")
        ws_manager.disconnect(websocket, idx_key)
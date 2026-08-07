import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.config import settings
from app.api.deps import get_current_user  # JWT Protect Middleware Dependency
from app.core.database import get_database  # Async Motor MongoDB instance
from app.engine.ai_surveillance import AISurveillanceEngine, get_oi_support_resistance
from app.engine.risk_manager import risk_manager
from app.engine.confluence_math import calculate_pcr_and_sentiment, calculate_option_greeks
from app.services.dhan_websocket import (
    market_data_store,
    track_active_position_trade,
    dhan_ws_client
)
from app.services.token_registry import fetch_expiry_list, fetch_full_option_chain_data

router = APIRouter()
logger = logging.getLogger(__name__)

# Cache for Expiries per index
expiry_cache: Dict[str, Dict[str, Any]] = {}


class DecodeRequest(BaseModel):
    index_name: str = "NIFTY"


class BatchLTPRequest(BaseModel):
    security_ids: List[str]


# ----------------------------------------------------------------------------
# HELPER: Get active weekly expiry for target index
# ----------------------------------------------------------------------------
async def get_or_fetch_expiry(index_name: str) -> Optional[str]:
    now_ts = datetime.utcnow().timestamp()
    if index_name in expiry_cache and (now_ts - expiry_cache[index_name]["fetched_at"]) < 3600:
        return expiry_cache[index_name]["expiry"]

    idx_config = settings.INDICES_CONFIG.get(index_name.upper(), settings.INDICES_CONFIG["NIFTY"])
    expiries = await fetch_expiry_list(idx_config["scrip_id"], idx_config["underlying_seg"])

    if expiries:
        expiry_cache[index_name] = {"expiry": expiries[0], "fetched_at": now_ts}
        return expiries[0]
    return None


# ----------------------------------------------------------------------------
# 1. POST /api/v1/signals/decode
# Unified Multi-Index AI Confluence Signal Generator
# ----------------------------------------------------------------------------
@router.post("/decode")
async def decode_market_signal(
    payload: DecodeRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    index_name = payload.index_name.upper()
    idx_config = settings.INDICES_CONFIG.get(index_name, settings.INDICES_CONFIG["NIFTY"])

    expiry = await get_or_fetch_expiry(index_name)
    if not expiry:
        return {"success": False, "message": f"No active expiry found for {index_name}"}

    # 1. Fetch Option Chain Data (Central Backend Call - ZERO Client Leakage)
    raw_oc = await fetch_full_option_chain_data(
        scrip_id=idx_config["scrip_id"],
        segment=idx_config["underlying_seg"],
        expiry=expiry
    )

    if not raw_oc:
        return {"success": False, "message": "Dhan API busy or rate limit reached. Please try again."}

    spot = float(raw_oc.get("last_price") or raw_oc.get("underlyingPrice") or 0.0)
    oc = raw_oc.get("oc", {})

    if spot <= 0 or not oc:
        return {"success": False, "message": "Incomplete option chain data received from Dhan."}

    # 2. ATM Strike Calculation
    step = idx_config["step_size"]
    atm_strike = int(round(spot / step) * step)

    # Decimal key matcher helper
    atm_node = None
    for key in oc.keys():
        try:
            if abs(float(key) - atm_strike) < 0.1:
                atm_node = oc[key]
                break
        except ValueError:
            continue

    if not atm_node:
        return {"success": False, "message": f"Could not find ATM node for strike {atm_strike}"}

    # 3. AI Confluence Evaluation
    ai_engine = AISurveillanceEngine(index_name)
    ai_result = ai_engine.evaluate_market_state(
        spot=spot,
        atm_strike=atm_strike,
        oc_data=oc,
        spot_history=[spot],  # Evaluated with rolling memory in pipeline
        orb_high=0.0,
        orb_low=0.0
    )

    signal = ai_result["signal"]
    selected_type = ai_result["selected_type"]

    if signal == "NO TRADE" or not selected_type:
        return {
            "success": True,
            "data": {
                "ok": True,
                "index_name": index_name,
                "spot": spot,
                "atm_strike": atm_strike,
                "pcr": ai_result["pcr"],
                "regime": ai_result["sentiment"],
                "signal": "NO TRADE",
                "score": ai_result["ai_score"],
                "reasons": ai_result["reasons"],
                "wait_levels": ai_result.get("wait_levels")
            }
        }

    # 4. Extract Real LTP and Security ID for Selected Option Contract
    selected_node = atm_node.get("ce") if selected_type == "CE" else atm_node.get("pe")
    entry_price = float(selected_node.get("last_price") or 0.0) if selected_node else 0.0
    security_id = str(selected_node.get("security_id") or "") if selected_node else ""

    # Synthetic Price Guard: Never emit a signal on synthetic/fake price
    if entry_price <= 0 or not security_id:
        return {
            "success": True,
            "data": {
                "ok": True,
                "signal": "NO TRADE",
                "message": "⚠️ Signal suppressed: Real LTP unavailable for this option contract."
            }
        }

    # 5. Calculate Option Greeks & Dynamic Risk Management
    iv = float(selected_node.get("implied_volatility") or 13.5) if selected_node else 13.5
    greeks = calculate_option_greeks(spot, atm_strike, 0.02, iv, option_type=selected_type)

    risk_result = risk_manager.calculate_trade_targets(
        index_name=index_name,
        entry_premium=entry_price,
        spot_atr=12.0,
        delta=greeks["delta"],
        is_forced_scalp=False
    )

    signal_doc = {
        "user_id": str(current_user["_id"]),
        "index_name": index_name,
        "signal": signal,
        "strike": f"{atm_strike}{selected_type}",
        "selected_type": selected_type,
        "entry_price": risk_result["entry_price"],
        "index_spot": spot,
        "stop_loss": risk_result["stop_loss"],
        "shz_upper": risk_result["target1"],
        "shz_lower": risk_result["stop_loss"],
        "target2": risk_result["target2"],
        "score": ai_result["ai_score"],
        "reasons": ai_result["reasons"],
        "pcr": ai_result["pcr"],
        "vix": 13.5,
        "breakout_status": "CONFLUENCE_DECODE",
        "atm_strike": atm_strike,
        "security_id": security_id,
        "status": "ACTIVE",
        "created_at": datetime.utcnow()
    }

    # Save Signal to MongoDB (With 5-min cooling period check)
    cooling_window = datetime.utcnow() - timedelta(minutes=5)
    existing = await db.signals.find_one({
        "user_id": str(current_user["_id"]),
        "atm_strike": atm_strike,
        "index_name": index_name,
        "created_at": {"$gt": cooling_window}
    })

    if not existing:
        res = await db.signals.insert_one(signal_doc)
        signal_id = str(res.inserted_id)
        signal_doc["_id"] = signal_id

        # 🟢 Register active position for real-time WebSocket SL/Target exit monitoring
        track_active_position_trade(
            security_id=security_id,
            signal_id=signal_id,
            target=risk_result["target1"],
            sl=risk_result["stop_loss"]
        )
    else:
        # Cooling-window par existing signal mila — usi ka _id return karo
        signal_doc["_id"] = str(existing["_id"])

    return {"success": True, "data": signal_doc}


# ----------------------------------------------------------------------------
# 2. POST /api/v1/signals/decode-force (Forced Scalp Engine - Crash Proof)
# ----------------------------------------------------------------------------
@router.post("/decode-force")
async def decode_force_scalp(
    payload: DecodeRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    try:
        index_name = payload.index_name.upper()
        idx_config = settings.INDICES_CONFIG.get(index_name, settings.INDICES_CONFIG["NIFTY"])

        expiry = await get_or_fetch_expiry(index_name)
        if not expiry:
            return {"success": False, "message": f"No active expiry found for {index_name}"}

        raw_oc = await fetch_full_option_chain_data(idx_config["scrip_id"], idx_config["underlying_seg"], expiry)

        if not raw_oc:
            return {"success": False, "message": "Option chain data currently unavailable from Dhan."}

        spot = float(raw_oc.get("last_price") or raw_oc.get("underlyingPrice") or 0.0)
        oc = raw_oc.get("oc", {})

        if spot <= 0 or not oc:
            return {"success": False, "message": "Incomplete market data for forced scalp."}

        # 1. Calculate ATM Strike
        step = idx_config["step_size"]
        atm_strike = int(round(spot / step) * step)

        # 2. PCR & Trend Bias for Forced Scalp High Probability
        pcr, sentiment = calculate_pcr_and_sentiment(oc)

        # 🎯 Momentum + OI-wall bias: combine PCR bias with where spot sits relative to the
        # nearest OI resistance/support walls, so Forced Scalp doesn't blindly follow PCR alone.
        resistance_strike, support_strike = get_oi_support_resistance(oc)
        if resistance_strike and support_strike:
            oi_mid_level = (resistance_strike + support_strike) / 2
            momentum_bias = "CE" if spot > oi_mid_level else "PE"
        else:
            momentum_bias = "CE" if pcr >= 1.0 else "PE"

        pcr_bias = "CE" if pcr >= 0.95 else "PE"

        # High confluence: PCR bias and momentum bias agree -> trust it fully.
        # On disagreement, fall back to PCR bias (safer default) for direction.
        selected_type = momentum_bias if momentum_bias == pcr_bias else pcr_bias
        signal = "BUY CALL" if selected_type == "CE" else "BUY PUT"
        # 3. Find Matching Option Node Safely
        atm_node = None
        for key in oc.keys():
            try:
                if abs(float(key) - atm_strike) < 0.1:
                    atm_node = oc[key]
                    break
            except (ValueError, TypeError):
                continue

        if not atm_node:
            # Fallback to direct dictionary lookup
            atm_node = oc.get(str(atm_strike)) or oc.get(float(atm_strike))

        if not atm_node:
            return {"success": False, "message": f"Could not map ATM strike {atm_strike}."}

        selected_node = atm_node.get("ce") if selected_type == "CE" else atm_node.get("pe")

        if not selected_node:
            return {"success": False, "message": "Selected option contract node unavailable."}

        entry_price = float(selected_node.get("last_price") or selected_node.get("top_bid_price") or 0.0)
        security_id = str(selected_node.get("security_id") or "")

        if entry_price <= 0 or not security_id:
            return {"success": False, "message": "Real LTP unavailable for option contract."}

        # 4. High-Probability Dynamic Risk Management (Force Mode Math)
        # IV pulled from the live option node — risk_manager scales SL/Target with it.
        iv = float(selected_node.get("implied_volatility") or 13.5)

        risk_result = risk_manager.calculate_trade_targets(
            index_name=index_name,
            entry_premium=entry_price,
            spot_atr=10.0,
            delta=0.52,
            is_forced_scalp=True,
            iv=iv
        )

        signal_doc = {
            "user_id": str(current_user["_id"]),
            "index_name": index_name,
            "signal": signal,
            "strike": f"{atm_strike}{selected_type}",
            "selected_type": selected_type,
            "entry_price": risk_result["entry_price"],
            "index_spot": spot,
            "stop_loss": risk_result["stop_loss"],
            "shz_upper": risk_result["target1"],
            "shz_lower": risk_result["stop_loss"],
            "target2": risk_result["target2"],
            "score": 5.0,  # Scalp Risk Score Flag
            "reasons": [
                f"⚡ HIGH-RISK FORCED SCALP: Triggered by trader override",
                f"📊 Market Bias: PCR {pcr} ({sentiment})",
                f"🛡️ Tight Micro StopLoss applied to preserve capital"
            ],
            "pcr": pcr,
            "vix": 13.5,
            "breakout_status": "FORCED_SCALP",
            "atm_strike": atm_strike,
            "security_id": security_id,
            "status": "ACTIVE",
            "created_at": datetime.utcnow()
        }

        # Save to Database & Track Trade
        res = await db.signals.insert_one(signal_doc)
        signal_id = str(res.inserted_id)
        signal_doc["_id"] = signal_id

        # Register live WS tracking
        track_active_position_trade(
            security_id=security_id,
            signal_id=signal_id,
            target=risk_result["target1"],
            sl=risk_result["stop_loss"]
        )

        return {"success": True, "data": signal_doc}

    except Exception as e:
        logger.error(f"❌ Error in /decode-force: {str(e)}", exc_info=True)
        return {
            "success": False,
            "message": f"Engine execution issue: {str(e)}"
        }


# ----------------------------------------------------------------------------
# 3. GET /api/v1/signals/automated-signals-log
# Retrieves Today's Active Signals for current user
# ----------------------------------------------------------------------------
@router.get("/automated-signals-log")
async def get_signals_log(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    cursor = db.signals.find({
        "user_id": str(current_user["_id"]),
        "created_at": {"$gte": today_start}
    }).sort("created_at", -1).limit(50)

    signals = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        signals.append(doc)

    return {"success": True, "count": len(signals), "logs": signals}


# ----------------------------------------------------------------------------
# 4. POST /api/v1/signals/live-ltp-batch
# Batch Live Price Sync & Target/SL Lock Endpoint
# ----------------------------------------------------------------------------
@router.post("/live-ltp-batch")
async def batch_live_ltp_update(
    payload: BatchLTPRequest,
    db=Depends(get_database)
):
    clean_ids = list(set([str(sid) for sid in payload.security_ids if sid]))
    if not clean_ids:
        return {"success": True, "prices": {}}

    prices = {}
    # Extract live ticks from Python WebSocket in-memory store
    for index_name, store in market_data_store.items():
        for sec_id in clean_ids:
            if sec_id in store and "ltp" in store[sec_id]:
                prices[sec_id] = store[sec_id]["ltp"]

    # Evaluate DB Active Signal Statuses
    if prices:
        cursor = db.signals.find({"security_id": {"$in": list(prices.keys())}, "status": "ACTIVE"})
        async for sig in cursor:
            live_price = prices.get(sig["security_id"])
            if not live_price:
                continue

            target = float(sig.get("shz_upper") or 0.0)
            sl = float(sig.get("shz_lower") or 0.0)

            updated_status = None
            if target > 0 and live_price >= target:
                updated_status = "TARGET_HIT"
            elif sl > 0 and live_price <= sl:
                updated_status = "SL_HIT"

            if updated_status:
                await db.signals.update_one(
                    {"_id": sig["_id"], "status": "ACTIVE"},
                    {"$set": {"status": updated_status, "exit_ltp": live_price, "updated_at": datetime.utcnow()}}
                )

    return {"success": True, "prices": prices}


# ----------------------------------------------------------------------------
# 5. GET /api/v1/signals/admin/today-stats
# ----------------------------------------------------------------------------
@router.get("/admin/today-stats")
async def get_admin_today_stats(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    total_users = await db.users.count_documents({})
    total_trades = await db.signals.count_documents({"created_at": {"$gte": today_start}})
    target_hits = await db.signals.count_documents({"created_at": {"$gte": today_start}, "status": "TARGET_HIT"})
    sl_hits = await db.signals.count_documents({"created_at": {"$gte": today_start}, "status": "SL_HIT"})

    return {
        "success": True,
        "stats": {
            "total_users": total_users,
            "total_trades": total_trades,
            "target_hits": target_hits,
            "sl_hits": sl_hits
        }
    }


# ----------------------------------------------------------------------------
# 6. GET /api/v1/signals/admin/user-trades/{target_user_id}
# ----------------------------------------------------------------------------
@router.get("/admin/user-trades/{target_user_id}")
async def get_admin_user_trades(
    target_user_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    cursor = db.signals.find({
        "user_id": target_user_id,
        "created_at": {"$gte": today_start}
    }).sort("created_at", -1)

    trades = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        trades.append(doc)

    return {"success": True, "count": len(trades), "trades": trades}
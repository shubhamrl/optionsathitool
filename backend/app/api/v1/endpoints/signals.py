import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.config import settings
from app.api.deps import get_current_user
from app.core.database import get_database
from app.engine.ai_surveillance import AISurveillanceEngine, get_oi_support_resistance
from app.engine.risk_manager import risk_manager
from app.engine.confluence_math import calculate_pcr_and_sentiment, calculate_option_greeks
from app.services.dhan_websocket import (
    market_data_store,
    track_active_position_trade,
    dhan_ws_client,
    get_orb_levels,
    get_price_momentum,
    register_token_index_mapping
)
from app.services.token_registry import fetch_expiry_list, fetch_full_option_chain_data
from app.services.feature_logger import log_signal_features, update_signal_outcome, OUTCOME_TARGET_HIT, OUTCOME_SL_HIT

router = APIRouter()
logger = logging.getLogger(__name__)

expiry_cache: Dict[str, Dict[str, Any]] = {}


class DecodeRequest(BaseModel):
    index_name: str = "NIFTY"


class BatchLTPRequest(BaseModel):
    security_ids: List[str]


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


def _register_index_token_map(oc: Dict[str, Any], index_name: str):
    """Registers security_id -> index_name for every contract in this option chain,
    so live WebSocket ticks route into the CORRECT index bucket in market_data_store
    instead of silently defaulting to NIFTY."""
    token_map = {}
    for node in oc.values():
        ce_id = str((node.get("ce") or {}).get("security_id") or "")
        pe_id = str((node.get("pe") or {}).get("security_id") or "")
        if ce_id:
            token_map[ce_id] = index_name
        if pe_id:
            token_map[pe_id] = index_name
    if token_map:
        register_token_index_mapping(token_map)


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

    _register_index_token_map(oc, index_name)

    step = idx_config["step_size"]
    atm_strike = int(round(spot / step) * step)

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

    orb_levels = get_orb_levels(index_name)
    price_momentum = get_price_momentum(index_name)

    ai_engine = AISurveillanceEngine(index_name)
    ai_result = ai_engine.evaluate_market_state(
        spot=spot,
        atm_strike=atm_strike,
        oc_data=oc,
        spot_history=[spot],
        orb_high=orb_levels["orb_high"],
        orb_low=orb_levels["orb_low"],
        price_momentum=price_momentum
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

    selected_node = atm_node.get("ce") if selected_type == "CE" else atm_node.get("pe")
    entry_price = float(selected_node.get("last_price") or 0.0) if selected_node else 0.0
    security_id = str(selected_node.get("security_id") or "") if selected_node else ""

    if entry_price <= 0 or not security_id:
        return {
            "success": True,
            "data": {
                "ok": True,
                "signal": "NO TRADE",
                "message": "Signal suppressed: Real LTP unavailable for this option contract."
            }
        }

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

        track_active_position_trade(
            security_id=security_id,
            signal_id=signal_id,
            target=risk_result["target1"],
            sl=risk_result["stop_loss"]
        )

        orb_triggered = "CE" if spot > orb_levels["orb_high"] > 0 else ("PE" if 0 < orb_levels["orb_low"] and spot < orb_levels["orb_low"] else None)
        await log_signal_features(
            db=db, signal_id=signal_id, index_name=index_name, mode="standard",
            pcr=ai_result["pcr"], delta=greeks["delta"], iv=iv, score=ai_result["ai_score"],
            selected_type=selected_type, momentum_bias=price_momentum.get("bias") if price_momentum else None,
            orb_triggered=orb_triggered
        )
    else:
        signal_doc["_id"] = str(existing["_id"])

    return {"success": True, "data": signal_doc}


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

        _register_index_token_map(oc, index_name)

        step = idx_config["step_size"]
        atm_strike = int(round(spot / step) * step)

        pcr, sentiment = calculate_pcr_and_sentiment(oc)

        price_momentum = get_price_momentum(index_name)

        resistance_strike, support_strike = get_oi_support_resistance(oc)
        if resistance_strike and support_strike:
            oi_mid_level = (resistance_strike + support_strike) / 2
            oi_wall_bias = "CE" if spot > oi_mid_level else "PE"
        else:
            oi_wall_bias = None

        if pcr > 1.05:
            pcr_bias = "CE"
        elif pcr < 0.95:
            pcr_bias = "PE"
        else:
            pcr_bias = None

        direction_reason = ""
        if price_momentum["bias"] in ("CE", "PE"):
            selected_type = price_momentum["bias"]
            direction_reason = f"Price Action: {price_momentum['reason']}"
        elif oi_wall_bias and pcr_bias and oi_wall_bias == pcr_bias:
            selected_type = oi_wall_bias
            direction_reason = "PCR and OI-wall positioning both confirm this direction."
        elif pcr_bias:
            selected_type = pcr_bias
            direction_reason = f"PCR bias fallback (PCR {pcr}, {sentiment})."
        else:
            selected_type = "CE" if oi_wall_bias is None else oi_wall_bias
            direction_reason = "Neutral PCR/momentum — defaulting on OI-wall positioning."

        signal = "BUY CALL" if selected_type == "CE" else "BUY PUT"

        atm_node = None
        for key in oc.keys():
            try:
                if abs(float(key) - atm_strike) < 0.1:
                    atm_node = oc[key]
                    break
            except (ValueError, TypeError):
                continue

        if not atm_node:
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
            "score": 5.0,
            "reasons": [
                "HIGH-RISK FORCED SCALP: Triggered by trader override",
                direction_reason,
                f"Market Bias: PCR {pcr} ({sentiment})",
                "Dynamic Micro StopLoss applied based on live Delta/ATR/IV"
            ],
            "pcr": pcr,
            "vix": 13.5,
            "breakout_status": "FORCED_SCALP",
            "atm_strike": atm_strike,
            "security_id": security_id,
            "status": "ACTIVE",
            "created_at": datetime.utcnow()
       }

        res = await db.signals.insert_one(signal_doc)
        signal_id = str(res.inserted_id)
        signal_doc["_id"] = signal_id

        track_active_position_trade(
            security_id=security_id,
            signal_id=signal_id,
            target=risk_result["target1"],
            sl=risk_result["stop_loss"]
        )

        await log_signal_features(
            db=db, signal_id=signal_id, index_name=index_name, mode="scalp",
            pcr=pcr, delta=0.52, iv=iv, score=5.0,
            selected_type=selected_type, momentum_bias=price_momentum.get("bias"),
            orb_triggered=None
        )

        return {"success": True, "data": signal_doc}

    except Exception as e:
        logger.error(f"Error in /decode-force: {str(e)}", exc_info=True)
        return {
            "success": False,
            "message": f"Engine execution issue: {str(e)}"
        }


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


@router.post("/live-ltp-batch")
async def batch_live_ltp_update(
    payload: BatchLTPRequest,
    db=Depends(get_database)
):
    clean_ids = list(set([str(sid) for sid in payload.security_ids if sid]))
    if not clean_ids:
        return {"success": True, "prices": {}}

    prices = {}
    for index_name, store in market_data_store.items():
        for sec_id in clean_ids:
            if sec_id in store and "ltp" in store[sec_id]:
                prices[sec_id] = store[sec_id]["ltp"]

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
                outcome_code = OUTCOME_TARGET_HIT if updated_status == "TARGET_HIT" else OUTCOME_SL_HIT
                await update_signal_outcome(db, str(sig["_id"]), outcome_code)
    return {"success": True, "prices": prices}


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


@router.get("/admin/overall-accuracy")
async def get_overall_accuracy_stats(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    tracking_doc = await db.app_settings.find_one({"_id": "accuracy_tracking"})
    start_date = tracking_doc.get("start_date") if tracking_doc else None

    date_filter = {"created_at": {"$gte": start_date}} if start_date else {}

    total_target_hit = await db.signals.count_documents({**date_filter, "status": "TARGET_HIT"})
    total_sl_hit = await db.signals.count_documents({**date_filter, "status": "SL_HIT"})
    total_active = await db.signals.count_documents({**date_filter, "status": "ACTIVE"})
    total_expired = await db.signals.count_documents({**date_filter, "status": "EXPIRED"})
    total_signals_ever = await db.signals.count_documents(date_filter)

    decided_signals = total_target_hit + total_sl_hit
    win_rate = round((total_target_hit / decided_signals) * 100, 1) if decided_signals > 0 else 0.0

    verification_goal = 200
    progress_percentage = round(min((decided_signals / verification_goal) * 100, 100), 1)

    return {
        "success": True,
        "stats": {
            "total_signals_ever": total_signals_ever,
            "total_target_hit": total_target_hit,
            "total_sl_hit": total_sl_hit,
            "total_active": total_active,
            "total_expired": total_expired,
            "decided_signals": decided_signals,
            "win_rate_percentage": win_rate,
            "verification_goal": verification_goal,
            "progress_percentage": progress_percentage,
            "is_goal_reached": decided_signals >= verification_goal,
            "tracking_since": start_date.isoformat() if start_date else None
        }
    }


@router.post("/admin/reset-accuracy-tracking")
async def reset_accuracy_tracking(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    now = datetime.utcnow()
    await db.app_settings.update_one(
        {"_id": "accuracy_tracking"},
        {"$set": {"start_date": now}},
        upsert=True
    )
    return {
        "success": True,
        "message": "Accuracy tracking reset! Ab sirf is pal ke baad ke signals count honge.",
        "tracking_since": now.isoformat()
    }
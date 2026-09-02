import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.config import settings
from app.api.deps import get_current_user
from app.core.database import get_database
from app.engine.risk_manager import risk_manager
from app.engine.confluence_math import calculate_option_greeks
from app.services.dhan_websocket import (
    market_data_store,
    track_active_position_trade,
    register_token_index_mapping
)
from app.services.feature_logger import log_signal_features, update_signal_outcome, OUTCOME_TARGET_HIT, OUTCOME_SL_HIT
from app.core.market_hours import get_market_status

router = APIRouter()
logger = logging.getLogger(__name__)


class DecodeRequest(BaseModel):
    index_name: str = "NIFTY"


class BatchLTPRequest(BaseModel):
    security_ids: List[str]


def _register_index_token_map(oc: Dict[str, Any], index_name: str):
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


def _ist_days_ago_start_utc(days: int) -> datetime:
    ist_offset = timedelta(hours=5, minutes=30)
    ist_now = datetime.utcnow() + ist_offset
    start = datetime(ist_now.year, ist_now.month, ist_now.day) - timedelta(days=days - 1)
    return start - ist_offset


def _ist_single_day_bounds(days_ago: int):
    ist_offset = timedelta(hours=5, minutes=30)
    ist_now = datetime.utcnow() + ist_offset
    day = datetime(ist_now.year, ist_now.month, ist_now.day) - timedelta(days=days_ago)
    start_utc = day - ist_offset
    end_utc = start_utc + timedelta(days=1)
    label = day.strftime("%d %b %Y")
    return start_utc, end_utc, label


async def _run_on_demand_strategy_scan(db, index_name: str, current_user_id: str) -> Dict[str, Any]:
    """
    Runs ALL registered strategies (strategy_engine.STRATEGIES — same registry
    the background Strategy Scanner uses) against this index's current shared
    snapshot, on demand. Returns the FIRST strategy that finds a genuine setup.
    Used by both /decode and /decode-force — per user request, both buttons now
    scan the same 12 strategies and return whichever setup exists.
    """
    from app.services.strategy_engine import STRATEGIES, _build_context

    context = await _build_context(db, index_name)
    if not context:
        return {"ok": True, "signal": "NO TRADE", "index_name": index_name,
                "message": "Market data temporarily unavailable. Please try again."}

    checked_names = []
    idx_config = settings.INDICES_CONFIG.get(index_name, settings.INDICES_CONFIG["NIFTY"])
    spot = context["spot"]
    oc = context["oc"]
    _register_index_token_map(oc, index_name)

    for strat in STRATEGIES:
        try:
            result = await strat["detect_fn"](index_name, context)
        except Exception as e:
            logger.error(f"On-demand scan: strategy '{strat['key']}' failed: {str(e)}")
            continue

        checked_names.append(strat["nickname"])
        if not result:
            continue

        selected_type = result["bias"]
        reason_lines = result.get("reasons", [result.get("reason", "")])
        risk_mode = result.get("risk_mode", "standard")

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
            continue

        selected_node = atm_node.get("ce") if selected_type == "CE" else atm_node.get("pe")
        entry_price = float(selected_node.get("last_price") or 0.0) if selected_node else 0.0
        security_id = str(selected_node.get("security_id") or "") if selected_node else ""
        if entry_price <= 0 or not security_id:
            continue

        breakout_status = f"STRAT_{strat['key']}"

        cooling_window = datetime.utcnow() - timedelta(minutes=5)
        existing = await db.signals.find_one({
            "user_id": current_user_id,
            "atm_strike": atm_strike,
            "index_name": index_name,
            "breakout_status": breakout_status,
            "created_at": {"$gt": cooling_window}
        })
        if existing:
            existing["_id"] = str(existing["_id"])
            existing["ok"] = True
            return existing

        iv = float(selected_node.get("implied_volatility") or 13.5) if selected_node else 13.5
        greeks = calculate_option_greeks(spot, atm_strike, 0.02, iv, option_type=selected_type)
        risk_result = risk_manager.calculate_trade_targets(
            index_name=index_name, entry_premium=entry_price, spot_atr=12.0,
            delta=greeks["delta"], is_forced_scalp=(risk_mode == "tight"), iv=iv
        )

        signal = "BUY CALL" if selected_type == "CE" else "BUY PUT"
        signal_doc = {
            "user_id": current_user_id,
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
            "score": 6.0,
            "reasons": [f"STRATEGY: {strat['nickname']}"] + reason_lines,
            "pcr": 0.0,
            "vix": 13.5,
            "breakout_status": breakout_status,
            "atm_strike": atm_strike,
            "security_id": security_id,
            "status": "ACTIVE",
            "created_at": datetime.utcnow()
        }
        res = await db.signals.insert_one(signal_doc)
        signal_id = str(res.inserted_id)
        signal_doc["_id"] = signal_id
        signal_doc["ok"] = True

        track_active_position_trade(
            security_id=security_id, signal_id=signal_id,
            target=risk_result["target1"], sl=risk_result["stop_loss"]
        )
        await log_signal_features(
            db=db, signal_id=signal_id, index_name=index_name, mode=f"strategy_{strat['key'].lower()}",
            pcr=0.0, delta=greeks["delta"], iv=iv, score=6.0,
            selected_type=selected_type, momentum_bias=selected_type, orb_triggered=None
        )
        return signal_doc

    checked_text = ", ".join(checked_names) if checked_names else "koi strategy ready nahi hai"
    return {
        "ok": True,
        "signal": "NO TRADE",
        "index_name": index_name,
        "reasons": [f"{len(checked_names)} strategies check hui ({checked_text}) — abhi koi setup nahi bana."]
    }


@router.post("/decode")
async def decode_market_signal(
    payload: DecodeRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    index_name = payload.index_name.upper()

    market_status = get_market_status()
    if not market_status["is_open"]:
        return {
            "success": True,
            "data": {
                "ok": True, "signal": "MARKET_CLOSED",
                "message": market_status["message"], "next_open": market_status["next_open"]
            }
        }

    result = await _run_on_demand_strategy_scan(db, index_name, str(current_user["_id"]))
    return {"success": True, "data": result}


@router.post("/decode-force")
async def decode_force_scalp(
    payload: DecodeRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    try:
        index_name = payload.index_name.upper()

        market_status = get_market_status()
        if not market_status["is_open"]:
            return {
                "success": True,
                "data": {
                    "ok": True, "signal": "MARKET_CLOSED",
                    "message": market_status["message"], "next_open": market_status["next_open"]
                }
            }

        result = await _run_on_demand_strategy_scan(db, index_name, str(current_user["_id"]))
        return {"success": True, "data": result}

    except Exception as e:
        logger.error(f"Error in /decode-force: {str(e)}", exc_info=True)
        return {"success": False, "message": f"Engine execution issue: {str(e)}"}


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


@router.get("/global-signals-log")
async def get_global_signals_log(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    """Legacy feed — GLOBAL_SCAN no longer generates new signals (ORB Breaker was
    merged into the strategy registry), so this will stay empty going forward.
    Kept so old historical data remains viewable."""
    market_status = get_market_status()
    window_start = _ist_days_ago_start_utc(7)

    cursor = db.signals.find({
        "user_id": "SYSTEM_GLOBAL_SCANNER",
        "created_at": {"$gte": window_start}
    }).sort("created_at", -1).limit(150)

    logs = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        if doc.get("created_at"):
            doc["created_at"] = doc["created_at"].isoformat()
        logs.append(doc)

    return {"success": True, "is_market_open": market_status["is_open"], "logs": logs}


@router.get("/strategy-signals-log")
async def get_strategy_signals_log(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    """Feed of the last 7 days of signals from the multi-strategy engine —
    now includes ORB Breaker too, since it's just another registered strategy."""
    market_status = get_market_status()
    window_start = _ist_days_ago_start_utc(7)

    cursor = db.signals.find({
        "user_id": "SYSTEM_STRATEGY_ENGINE",
        "created_at": {"$gte": window_start}
    }).sort("created_at", -1).limit(200)

    logs = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        if doc.get("created_at"):
            doc["created_at"] = doc["created_at"].isoformat()
        logs.append(doc)

    return {"success": True, "is_market_open": market_status["is_open"], "logs": logs}


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

    async def _mode_stats(breakout_status: str):
        t_hit = await db.signals.count_documents({**date_filter, "status": "TARGET_HIT", "breakout_status": breakout_status})
        s_hit = await db.signals.count_documents({**date_filter, "status": "SL_HIT", "breakout_status": breakout_status})
        decided = t_hit + s_hit
        wr = round((t_hit / decided) * 100, 1) if decided > 0 else 0.0
        return {"target_hit": t_hit, "sl_hit": s_hit, "decided": decided, "win_rate_percentage": wr}

    standard_stats = await _mode_stats("CONFLUENCE_DECODE")
    scalp_stats = await _mode_stats("FORCED_SCALP")

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
            "tracking_since": start_date.isoformat() if start_date else None,
            "standard_signal": standard_stats,
            "forced_scalp": scalp_stats
        }
    }


STRATEGY_NICKNAMES = {
    "GLOBAL_SCAN": "Global AI Scanner (legacy)",
    "FORCED_SCALP": "Forced Scalp (legacy)",
    "CONFLUENCE_DECODE": "Standard Signal (legacy)",
    "CANDLE_SCALP": "Candle Scalp",
    "STRAT_ORB_BREAKER": "ORB Breaker",
    "STRAT_VWAP_BOUNCER": "VWAP Bouncer",
    "STRAT_BOLLINGER_SQUEEZE": "Bollinger Squeeze",
    "STRAT_RSI_REVERSAL": "RSI Reversal",
    "STRAT_SR_BOUNCE": "Support-Resistance Bounce",
    "STRAT_GAP_FILL_FADER": "Gap-Fill Fader",
    "STRAT_MA_CROSSOVER": "MA Crossover",
    "STRAT_OI_BUILDUP": "OI Buildup Tracker",
    "STRAT_VOLUME_SPIKE": "Volume Spike Momentum",
    "STRAT_DOJI_REVERSAL": "Doji Reversal",
    "STRAT_MULTI_TIMEFRAME": "Multi-Timeframe Confluence",
    "STRAT_IV_CONTRACTION": "IV Contraction Entry",
}


@router.get("/admin/strategy-leaderboard")
async def get_strategy_leaderboard(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    statuses = await db.signals.distinct("breakout_status")
    results = []
    for status in statuses:
        if not status:
            continue
        t_hit = await db.signals.count_documents({"breakout_status": status, "status": "TARGET_HIT"})
        s_hit = await db.signals.count_documents({"breakout_status": status, "status": "SL_HIT"})
        decided = t_hit + s_hit
        win_rate = round((t_hit / decided) * 100, 1) if decided > 0 else 0.0
        results.append({
            "key": status,
            "nickname": STRATEGY_NICKNAMES.get(status, status.replace("STRAT_", "").replace("_", " ").title()),
            "target_hit": t_hit,
            "sl_hit": s_hit,
            "decided": decided,
            "win_rate_percentage": win_rate
        })

    results.sort(key=lambda r: (-r["decided"], -r["win_rate_percentage"]))
    return {"success": True, "strategies": results}


@router.get("/admin/strategy-leaderboard-daily")
async def get_strategy_leaderboard_daily(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    """Per-day (last 7 IST days) breakdown of each strategy's win-rate — so you
    can see 'aaj kis strategy ne kaisa perform kiya' separately from the
    all-time cumulative leaderboard."""
    days_data = []
    for days_ago in range(7):
        start_utc, end_utc, label = _ist_single_day_bounds(days_ago)

        pipeline = [
            {"$match": {
                "created_at": {"$gte": start_utc, "$lt": end_utc},
                "status": {"$in": ["TARGET_HIT", "SL_HIT"]},
                "breakout_status": {"$ne": None}
            }},
            {"$group": {
                "_id": {"strategy": "$breakout_status", "status": "$status"},
                "count": {"$sum": 1}
            }}
        ]
        rows = await db.signals.aggregate(pipeline).to_list(length=200)

        per_strategy: Dict[str, Dict[str, int]] = {}
        for r in rows:
            key = r["_id"]["strategy"]
            stat = r["_id"]["status"]
            per_strategy.setdefault(key, {"target_hit": 0, "sl_hit": 0})
            if stat == "TARGET_HIT":
                per_strategy[key]["target_hit"] = r["count"]
            else:
                per_strategy[key]["sl_hit"] = r["count"]

        strategies = []
        for key, counts in per_strategy.items():
            decided = counts["target_hit"] + counts["sl_hit"]
            wr = round((counts["target_hit"] / decided) * 100, 1) if decided > 0 else 0.0
            strategies.append({
                "key": key,
                "nickname": STRATEGY_NICKNAMES.get(key, key.replace("STRAT_", "").replace("_", " ").title()),
                "target_hit": counts["target_hit"],
                "sl_hit": counts["sl_hit"],
                "decided": decided,
                "win_rate_percentage": wr
            })
        strategies.sort(key=lambda s: (-s["decided"], -s["win_rate_percentage"]))
        days_data.append({"date": label, "strategies": strategies})

    return {"success": True, "days": days_data}


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
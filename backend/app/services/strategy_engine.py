import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Callable

from app.core.database import get_database
from app.core.config import settings
from app.core.market_hours import is_market_open
from app.engine.confluence_math import calculate_option_greeks
from app.engine.risk_manager import risk_manager
from app.services.feature_logger import log_signal_features
from app.services.auto_trader import auto_execute_for_all_users
from app.services.token_registry import fetch_expiry_list, fetch_full_option_chain_data

logger = logging.getLogger(__name__)

SCAN_INTERVAL_SECONDS = 25
_expiry_cache: Dict[str, Dict[str, Any]] = {}


async def _get_cached_expiry(index_name: str):
    idx_config = settings.INDICES_CONFIG.get(index_name, settings.INDICES_CONFIG["NIFTY"])
    now_ts = datetime.utcnow().timestamp()
    cached = _expiry_cache.get(index_name)
    if cached and (now_ts - cached["fetched_at"]) < 3600:
        return cached["expiry"]
    expiries = await fetch_expiry_list(idx_config["scrip_id"], idx_config["underlying_seg"])
    if expiries:
        _expiry_cache[index_name] = {"expiry": expiries[0], "fetched_at": now_ts}
        return expiries[0]
    return None


async def _refresh_market_snapshot(db, index_name: str):
    """
    🔴 CRITICAL: This duty used to belong to the now-retired standalone
    market_scanner.py loop. After ORB Breaker was merged into this registry,
    NOTHING was fetching fresh option-chain data anymore — market_snapshots
    was going stale/empty, silently starving every strategy (and the DECODE/
    FORCED SCALP buttons, which also read market_snapshots) of data. Restored
    here so the Strategy Engine is fully self-sufficient again.
    """
    existing = await db.market_snapshots.find_one({"index_name": index_name})
    if existing:
        age_seconds = (datetime.utcnow() - existing.get("updated_at", datetime.min)).total_seconds()
        if age_seconds < 18:
            return  # still fresh enough, skip re-fetch this cycle

    idx_config = settings.INDICES_CONFIG.get(index_name, settings.INDICES_CONFIG["NIFTY"])
    expiry = await _get_cached_expiry(index_name)
    if not expiry:
        return

    raw_oc = await fetch_full_option_chain_data(
        scrip_id=idx_config["scrip_id"], segment=idx_config["underlying_seg"], expiry=expiry
    )
    if not raw_oc:
        return

    spot = float(raw_oc.get("last_price") or raw_oc.get("underlyingPrice") or 0.0)
    oc = raw_oc.get("oc", {})
    if spot <= 0 or not oc:
        return

    await db.market_snapshots.update_one(
        {"index_name": index_name},
        {"$set": {"index_name": index_name, "expiry": expiry, "spot": spot, "oc": oc, "updated_at": datetime.utcnow()}},
        upsert=True
    )
STRATEGY_INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY"]
STRATEGY_SIGNAL_USER_ID = "SYSTEM_STRATEGY_ENGINE"
COOLDOWN_MINUTES = 5

# ----------------------------------------------------------------------------
# STRATEGY REGISTRY — each entry is one pluggable strategy. `detect_fn` receives
# (index_name, context) and must return None (no setup) or a dict:
#   {"bias": "CE"|"PE", "reason": str, "risk_mode": "standard"|"tight"}
# `context` carries shared per-scan data (candles, spot, option-chain snapshot)
# so multiple strategies don't each re-fetch the same thing.
# Strategies are added here in later steps — this file stays the single place
# to add/remove a strategy without touching the engine loop itself.
# ----------------------------------------------------------------------------
STRATEGIES: List[Dict[str, Any]] = []

_toggle_cache: Dict[str, bool] = {}
_toggle_cache_time: Optional[datetime] = None
TOGGLE_CACHE_TTL_SECONDS = 20


async def get_strategy_toggles(db) -> Dict[str, bool]:
    """Cached (20s) map of strategy_key -> enabled. Missing keys default to
    enabled=True (a strategy is ON unless explicitly turned off in ADMIN)."""
    global _toggle_cache, _toggle_cache_time
    now = datetime.utcnow()
    if _toggle_cache_time and (now - _toggle_cache_time).total_seconds() < TOGGLE_CACHE_TTL_SECONDS:
        return _toggle_cache
    toggles = {}
    cursor = db.strategy_settings.find({})
    async for doc in cursor:
        toggles[doc["_id"]] = doc.get("enabled", True)
    _toggle_cache = toggles
    _toggle_cache_time = now
    return toggles


def _ist_today_bounds():
    ist_offset = timedelta(hours=5, minutes=30)
    ist_now = datetime.utcnow() + ist_offset
    start = datetime(ist_now.year, ist_now.month, ist_now.day) - ist_offset
    return start, start + timedelta(days=1)


async def get_today_win_rate(db, breakout_status: str) -> Optional[float]:
    """Returns today's live win-rate for a strategy, or None if fewer than 3
    trades decided today (not enough sample to judge yet)."""
    start, end = _ist_today_bounds()
    t_hit = await db.signals.count_documents({
        "breakout_status": breakout_status, "status": "TARGET_HIT",
        "created_at": {"$gte": start, "$lt": end}
    })
    s_hit = await db.signals.count_documents({
        "breakout_status": breakout_status, "status": "SL_HIT",
        "created_at": {"$gte": start, "$lt": end}
    })
    decided = t_hit + s_hit
    if decided < 3:
        return None
    return (t_hit / decided) * 100


async def should_strategy_fire(db, strategy_key: str) -> bool:
    """A strategy fires normally if it's ON. If turned OFF in ADMIN, it still
    keeps scanning (data-read continues) but only fires when today's live
    win-rate has proven itself at 80%+ — everyday 'random' signals stay
    suppressed while it's off."""
    toggles = await get_strategy_toggles(db)
    if toggles.get(strategy_key, True):
        return True
    today_wr = await get_today_win_rate(db, f"STRAT_{strategy_key}")
    return today_wr is not None and today_wr >= 80.0 


def register_strategy(key: str, nickname: str, detect_fn: Callable):
    """Adds one strategy to the registry. Called at import-time by strategy
    definition files (kept separate from the engine for readability)."""
    STRATEGIES.append({"key": key, "nickname": nickname, "detect_fn": detect_fn})


def log_registered_strategies():
    logger.info("🎯 Strategy Engine Initializing...")
    if not STRATEGIES:
        logger.info("   ⚠️ No strategies registered yet.")
        return
    for s in STRATEGIES:
        logger.info(f"   ✅ {s['nickname']} — ONLINE")
    logger.info(f"   {len(STRATEGIES)}/{len(STRATEGIES)} strategies active. Scanning {', '.join(STRATEGY_INDICES)} every {SCAN_INTERVAL_SECONDS}s.")


async def _build_context(db, index_name: str) -> Optional[Dict[str, Any]]:
    """Gathers everything strategies commonly need for one index, once per scan
    cycle, so each strategy's detect_fn doesn't redundantly re-fetch the same
    snapshot/candles."""
    from app.services.dhan_websocket import index_candle_store, market_data_store
    from app.services.candle_storage import get_today_candles

    snapshot = await db.market_snapshots.find_one({"index_name": index_name})
    if not snapshot or not snapshot.get("oc") or snapshot.get("spot", 0) <= 0:
        return None

    return {
        "spot": snapshot["spot"],
        "oc": snapshot["oc"],
        "live_spot": market_data_store.get(index_name, {}).get("spot", 0.0),
        "recent_candles": index_candle_store.get(index_name, []),  # rolling ~30 min, in-memory
        "today_candles": None,  # lazy-loaded below only if a strategy needs it
        "_db": db,
        "_index_name": index_name,
    }


async def _get_today_candles_lazy(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Full-day candle history is only fetched from MongoDB if a strategy
    actually asks for it (via this helper), and cached per-scan-cycle so
    multiple strategies checking the same index share one fetch."""
    if context.get("today_candles") is None:
        from app.services.candle_storage import get_today_candles
        context["today_candles"] = await get_today_candles(context["_index_name"])
    return context["today_candles"]


async def _execute_strategy_signal(
    db, index_name: str, strategy_key: str, strategy_nickname: str,
    selected_type: str, reason_lines: List[str], risk_mode: str,
    context: Dict[str, Any], broadcast_callback=None
):
    """Generic signal creation shared by every strategy — resolves the ATM
    contract from the shared snapshot, computes risk via the existing
    risk_manager, saves the signal (tagged with this strategy's key), tracks it
    for auto SL/Target monitoring, logs features, and broadcasts it."""
    idx_config = settings.INDICES_CONFIG.get(index_name, settings.INDICES_CONFIG["NIFTY"])
    spot = context["spot"]
    oc = context["oc"]
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
        return

    selected_node = atm_node.get("ce") if selected_type == "CE" else atm_node.get("pe")
    entry_price = float(selected_node.get("last_price") or 0.0) if selected_node else 0.0
    security_id = str(selected_node.get("security_id") or "") if selected_node else ""
    if entry_price <= 0 or not security_id:
        return

    breakout_status = f"STRAT_{strategy_key}"

    cooling_window = datetime.utcnow() - timedelta(minutes=COOLDOWN_MINUTES)
    recent = await db.signals.find_one({
        "user_id": STRATEGY_SIGNAL_USER_ID,
        "atm_strike": atm_strike,
        "index_name": index_name,
        "breakout_status": breakout_status,
        "created_at": {"$gt": cooling_window}
    })
    if recent:
        return

    iv = float(selected_node.get("implied_volatility") or 13.5) if selected_node else 13.5
    greeks = calculate_option_greeks(spot, atm_strike, 0.02, iv, option_type=selected_type)

    risk_result = risk_manager.calculate_trade_targets(
        index_name=index_name, entry_premium=entry_price, spot_atr=12.0,
        delta=greeks["delta"], is_forced_scalp=(risk_mode == "tight"), iv=iv
    )

    signal = "BUY CALL" if selected_type == "CE" else "BUY PUT"
    from app.services.dhan_websocket import track_active_position_trade

    signal_doc = {
        "user_id": STRATEGY_SIGNAL_USER_ID,
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
        "reasons": [f"STRATEGY: {strategy_nickname}"] + reason_lines,
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

    track_active_position_trade(
        security_id=security_id, signal_id=signal_id,
        target=risk_result["target1"], sl=risk_result["stop_loss"]
    )

    await log_signal_features(
        db=db, signal_id=signal_id, index_name=index_name, mode=f"strategy_{strategy_key.lower()}",
        pcr=0.0, delta=greeks["delta"], iv=iv, score=6.0,
        selected_type=selected_type, momentum_bias=selected_type, orb_triggered=None
    )

    logger.info(f"🎯 [{strategy_nickname}] {index_name} {signal} {atm_strike}{selected_type} @ ₹{entry_price}")

    await auto_execute_for_all_users(db, signal_doc, source=breakout_status)

    if broadcast_callback:
        signal_doc["created_at"] = signal_doc["created_at"].isoformat()
        await broadcast_callback({"type": "STRATEGY_SIGNAL_UPDATE", "signal": signal_doc})


async def strategy_engine_loop(broadcast_callback=None):
    """
    Runs every registered strategy against every tracked index, every scan
    cycle. Adding a new strategy = one `register_strategy(...)` call elsewhere;
    this loop itself never needs to change.
    """
    log_registered_strategies()
    while True:
        try:
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)
            if not is_market_open() or not STRATEGIES:
                continue
            db = await get_database()

            for index_name in STRATEGY_INDICES:
                await _refresh_market_snapshot(db, index_name)
                context = await _build_context(db, index_name)
                if not context:
                    continue

                for strat in STRATEGIES:
                    try:
                        result = await strat["detect_fn"](index_name, context)
                        if result:
                            if not await should_strategy_fire(db, strat["key"]):
                                continue  # OFF and hasn't proven 80%+ today — data-read only, no signal
                            await _execute_strategy_signal(
                                db, index_name, strat["key"], strat["nickname"],
                                result["bias"], result.get("reasons", [result.get("reason", "")]),
                                result.get("risk_mode", "standard"), context, broadcast_callback
                            )
                    except Exception as e:
                        logger.error(f"Strategy '{strat['key']}' failed on {index_name}: {str(e)}")
                        continue

        except asyncio.CancelledError:
            logger.info("🎯 Strategy Engine stopped.")
            break
        except Exception as e:
            logger.error(f"Strategy Engine loop error: {str(e)}", exc_info=True)
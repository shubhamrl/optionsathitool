import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from app.core.database import get_database
from app.core.config import settings
from app.core.market_hours import is_market_open
from app.services.dhan_websocket import index_candle_store, market_data_store, track_active_position_trade
from app.engine.confluence_math import calculate_option_greeks
from app.services.feature_logger import log_signal_features
from app.services.auto_trader import auto_execute_for_all_users

logger = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)
SCAN_INTERVAL_SECONDS = 30
SCALP_INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY"]
SCALP_SIGNAL_USER_ID = "SYSTEM_CANDLE_SCALP"
COOLDOWN_MINUTES = 5

# ⏱️ True "scalp" behaviour — if a trade hasn't hit SL/Target within this window,
# force-close it at the live price instead of letting it drift for 30-60+ minutes.
MAX_HOLD_MINUTES = 10


def _now_ist() -> datetime:
    return datetime.utcnow() + IST_OFFSET


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(min(val, hi), lo)


def _get_completed_5min_candles(index_name: str, ist_now: datetime) -> List[Dict[str, Any]]:
    """
    Aggregates the rolling 1-minute candle store (built by dhan_websocket.py) into
    completed 5-minute buckets. The currently-forming bucket is always excluded,
    since it isn't a finished candle yet.
    """
    candles = index_candle_store.get(index_name, [])
    if len(candles) < 6:
        return []

    current_bucket = f"{ist_now.hour:02d}:{(ist_now.minute // 5) * 5:02d}"
    buckets: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    for c in candles:
        try:
            hh, mm = c["minute"].split(":")
        except Exception:
            continue
        bucket_key = f"{hh}:{(int(mm) // 5) * 5:02d}"
        if bucket_key == current_bucket:
            continue  # still forming — not a completed candle yet
        if bucket_key not in buckets:
            buckets[bucket_key] = {"open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"]}
            order.append(bucket_key)
        else:
            b = buckets[bucket_key]
            b["high"] = max(b["high"], c["high"])
            b["low"] = min(b["low"], c["low"])
            b["close"] = c["close"]

    return [buckets[k] for k in order]


def _detect_candle_scalp_setup(index_name: str, ist_now: datetime, min_sl_points: float) -> Optional[Dict[str, Any]]:
    """
    Reads the last completed 5-minute candle (plus the one before it) and looks for
    ONE of two objective, rule-based patterns:
      1. Momentum Continuation — a strong-bodied candle whose body dominates its
         range (small opposite wick) => trade WITH that direction.
      2. Rejection/Reversal — a long wick opposite the previous candle's direction
         => trade AGAINST the prior move (reversal).
    Returns {"bias": "CE"|"PE", "reason": str, "candle": {...}} or None.
    """
    candles_5m = _get_completed_5min_candles(index_name, ist_now)
    if len(candles_5m) < 2:
        return None

    last = candles_5m[-1]
    prev = candles_5m[-2]

    rng = last["high"] - last["low"]
    if rng <= 0:
        return None

    body = last["close"] - last["open"]
    upper_wick = last["high"] - max(last["open"], last["close"])
    lower_wick = min(last["open"], last["close"]) - last["low"]
    threshold = max(min_sl_points * 0.5, 3.0)

    if body > 0 and body >= rng * 0.6 and body >= threshold:
        return {"bias": "CE", "reason": f"Strong bullish 5-min candle (body {round(body, 1)} pts) — momentum continuation.", "candle": last}
    if body < 0 and abs(body) >= rng * 0.6 and abs(body) >= threshold:
        return {"bias": "PE", "reason": f"Strong bearish 5-min candle (body {round(abs(body), 1)} pts) — momentum continuation.", "candle": last}

    prev_body = prev["close"] - prev["open"]
    if prev_body < 0 and lower_wick >= rng * 0.5 and lower_wick >= threshold:
        return {"bias": "CE", "reason": "Long lower wick after a down-candle — rejection/reversal signal.", "candle": last}
    if prev_body > 0 and upper_wick >= rng * 0.5 and upper_wick >= threshold:
        return {"bias": "PE", "reason": "Long upper wick after an up-candle — rejection/reversal signal.", "candle": last}

    return None


async def _scan_one_index_scalp(db, index_name: str, broadcast_callback=None):
    ist_now = _now_ist()
    idx_config = settings.INDICES_CONFIG.get(index_name, settings.INDICES_CONFIG["NIFTY"])

    setup = _detect_candle_scalp_setup(index_name, ist_now, idx_config.get("min_sl_points", 8.0))
    if not setup:
        return

    selected_type = setup["bias"]
    candle = setup["candle"]
    candle_range = max(candle["high"] - candle["low"], 1.0)

    # 🔎 Live confirmation — require the CURRENT live spot to still be moving in the
    # signalled direction relative to the candle's close, so we don't fire on a
    # setup that has already faded/reversed by the time we act on it.
    live_spot = market_data_store.get(index_name, {}).get("spot", 0.0)
    if live_spot > 0:
        confirm_buffer = candle_range * 0.15
        if selected_type == "CE" and live_spot < candle["close"] - confirm_buffer:
            return
        if selected_type == "PE" and live_spot > candle["close"] + confirm_buffer:
            return

    snapshot = await db.market_snapshots.find_one({"index_name": index_name})
    if not snapshot or not snapshot.get("oc") or snapshot.get("spot", 0) <= 0:
        return

    spot = snapshot["spot"]
    oc = snapshot["oc"]
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

    cooling_window = datetime.utcnow() - timedelta(minutes=COOLDOWN_MINUTES)
    recent = await db.signals.find_one({
        "user_id": SCALP_SIGNAL_USER_ID,
        "atm_strike": atm_strike,
        "index_name": index_name,
        "created_at": {"$gt": cooling_window}
    })
    if recent:
        return

    iv = float(selected_node.get("implied_volatility") or 13.5)
    greeks = calculate_option_greeks(spot, atm_strike, 0.02, iv, option_type=selected_type)
    abs_delta = abs(greeks.get("delta") or 0.5)

    # 🕯️ Genuinely tight "scalp" sizing — small enough that it resolves fast on its
    # own; combined with the 10-min hard time-exit below (not just size alone).
    min_sl = idx_config.get("min_sl_points", 8.0)
    max_sl = idx_config.get("max_sl_points", 22.0)
    premium_risk_points = round(_clamp(candle_range * abs_delta * 0.5, min_sl * 0.15, max_sl * 0.22), 1)

    stop_loss = max(1.0, round(entry_price - premium_risk_points, 1))
    target1 = round(entry_price + (premium_risk_points * 1.5), 1)
    target2 = round(entry_price + (premium_risk_points * 2.0), 1)

    signal = "BUY CALL" if selected_type == "CE" else "BUY PUT"
    signal_doc = {
        "user_id": SCALP_SIGNAL_USER_ID,
        "index_name": index_name,
        "signal": signal,
        "strike": f"{atm_strike}{selected_type}",
        "selected_type": selected_type,
        "entry_price": entry_price,
        "index_spot": spot,
        "stop_loss": stop_loss,
        "shz_upper": target1,
        "shz_lower": stop_loss,
        "target2": target2,
        "score": 6.0,
        "reasons": [
            "CANDLE SCALP: 5-minute structure-based micro trade",
            setup["reason"],
            f"Max hold: {MAX_HOLD_MINUTES} min — auto-exits at live price if SL/Target not hit"
        ],
        "pcr": 0.0,
        "vix": 13.5,
        "breakout_status": "CANDLE_SCALP",
        "atm_strike": atm_strike,
        "security_id": security_id,
        "status": "ACTIVE",
        "created_at": datetime.utcnow()
    }

    res = await db.signals.insert_one(signal_doc)
    signal_id = str(res.inserted_id)
    signal_doc["_id"] = signal_id

    track_active_position_trade(
        security_id=security_id, signal_id=signal_id, target=target1, sl=stop_loss
    )

    await log_signal_features(
        db=db, signal_id=signal_id, index_name=index_name, mode="candle_scalp",
        pcr=0.0, delta=abs_delta, iv=iv, score=6.0,
        selected_type=selected_type, momentum_bias=selected_type, orb_triggered=None
    )

    logger.info(f"🕯️ [CANDLE SCALP] {index_name} {signal} {atm_strike}{selected_type} @ ₹{entry_price} | SL {stop_loss} | T {target1}")

    await auto_execute_for_all_users(db, signal_doc, source="CANDLE_SCALP")

    if broadcast_callback:
        signal_doc["created_at"] = signal_doc["created_at"].isoformat()
        await broadcast_callback({"type": "CANDLE_SCALP_SIGNAL_UPDATE", "signal": signal_doc})


async def _close_candle_scalp_trade(db, signal_id: str, exit_ltp: float, exit_reason: str, broadcast_callback=None):
    """Force-closes a Candle Scalp signal (and any linked OPEN paper trades) at the
    current live price. Reused both for the 10-min time-exit and could be reused
    for other early-exit triggers in future."""
    from bson import ObjectId
    from app.models.paper_trading import calculate_indian_option_charges
    from app.services.feature_logger import update_signal_outcome, OUTCOME_EXPIRED_PROFIT, OUTCOME_EXPIRED_LOSS

    sig = await db.signals.find_one_and_update(
        {"_id": ObjectId(signal_id), "status": "ACTIVE"},
        {"$set": {
            "status": "EXPIRED",
            "exit_ltp": exit_ltp,
            "exit_reason": exit_reason,
            "expired_at": datetime.utcnow()
        }}
    )
    if not sig:
        return

    entry = float(sig.get("entry_price", 0.0))
    outcome_code = OUTCOME_EXPIRED_PROFIT if exit_ltp >= entry else OUTCOME_EXPIRED_LOSS
    await update_signal_outcome(db, signal_id, outcome_code)

    cursor = db.paper_trades.find({"signal_id": signal_id, "status": "OPEN"})
    trades = await cursor.to_list(length=1000)

    for trade in trades:
        trade_id = trade["_id"]
        claimed = await db.paper_trades.find_one_and_update(
            {"_id": trade_id, "status": "OPEN"},
            {"$set": {"status": "CLOSING"}}
        )
        if not claimed:
            continue

        user_id = claimed["user_id"]
        buy_price = claimed["buy_price"]
        quantity = claimed["quantity"]
        margin_used = claimed["margin_used"]

        charges = calculate_indian_option_charges(buy_price, exit_ltp, quantity)
        net_pnl = charges["net_pnl"]
        total_taxes = charges["total_taxes"]

        wallet = await db.paper_wallets.find_one({"user_id": user_id})
        current_balance = wallet.get("balance", 0.0) if wallet else 0.0
        current_realized_pnl = wallet.get("realized_pnl", 0.0) if wallet else 0.0
        current_taxes = wallet.get("total_taxes_paid", 0.0) if wallet else 0.0

        await db.paper_wallets.update_one(
            {"user_id": user_id},
            {"$set": {
                "balance": round(current_balance + margin_used + net_pnl, 2),
                "realized_pnl": round(current_realized_pnl + net_pnl, 2),
                "total_taxes_paid": round(current_taxes + total_taxes, 2)
            }}
        )

        await db.paper_trades.update_one(
            {"_id": trade_id},
            {"$set": {
                "status": "SQUARED_OFF",
                "sell_price": exit_ltp,
                "net_pnl": net_pnl,
                "charges": charges,
                "exit_reason": exit_reason,
                "closed_at": datetime.utcnow(),
                "closed_by": "CANDLE_SCALP_TIME_EXIT"
            }}
        )

    logger.info(f"⏱️ [CANDLE SCALP TIME-EXIT] Signal {signal_id} closed @ ₹{exit_ltp} | {exit_reason}")

    if broadcast_callback:
        await broadcast_callback({
            "type": "SIGNAL_STATUS_UPDATE",
            "signal_id": signal_id,
            "status": "EXPIRED",
            "exit_ltp": exit_ltp
        })


async def _time_based_exit_scan(db, broadcast_callback=None):
    cutoff = datetime.utcnow() - timedelta(minutes=MAX_HOLD_MINUTES)
    cursor = db.signals.find({
        "user_id": SCALP_SIGNAL_USER_ID,
        "status": "ACTIVE",
        "created_at": {"$lte": cutoff}
    })
    sigs = await cursor.to_list(length=200)

    for sig in sigs:
        index_name = sig.get("index_name", "NIFTY")
        security_id = sig.get("security_id", "")
        entry = float(sig.get("entry_price", 0.0))

        store = market_data_store.get(index_name, {})
        node = store.get(security_id)
        exit_ltp = float(node["ltp"]) if node and node.get("ltp", 0) > 0 else entry

        outcome_word = "Profit" if exit_ltp >= entry else "Loss"
        exit_reason = f"Time-Exit ({MAX_HOLD_MINUTES} min) — Closed in {outcome_word} at live price"

        await _close_candle_scalp_trade(db, str(sig["_id"]), exit_ltp, exit_reason, broadcast_callback)


async def candle_scalp_scanner_loop(broadcast_callback=None):
    """
    Continuously scans NIFTY/BANKNIFTY/FINNIFTY 5-minute candles for structure-based
    scalp setups. Two things make this a genuine "scalp" (not just Standard Signal
    with smaller numbers): (1) tight, structure-derived SL/Target, and (2) a hard
    10-minute max-hold time-exit — if a trade hasn't resolved naturally by then, it
    is force-closed at the live price so capital and attention move to the next
    setup instead of drifting for 30-60+ minutes.
    """
    logger.info("🕯️ Candle Scalp Scanner started.")
    while True:
        try:
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)
            if not is_market_open():
                continue
            db = await get_database()
            await _time_based_exit_scan(db, broadcast_callback)
            for index_name in SCALP_INDICES:
                await _scan_one_index_scalp(db, index_name, broadcast_callback)
        except asyncio.CancelledError:
            logger.info("🕯️ Candle Scalp Scanner stopped.")
            break
        except Exception as e:
            logger.error(f"Candle Scalp Scanner error: {str(e)}", exc_info=True)
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any

from app.models.paper_trading import calculate_indian_option_charges
from app.services.feature_logger import (
    update_signal_outcome, OUTCOME_EXPIRED_PROFIT, OUTCOME_EXPIRED_LOSS,
    OUTCOME_TARGET_HIT, OUTCOME_SL_HIT
)

logger = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)
EOD_HOUR = 15
EOD_MINUTE = 15
NEAR_MISS_THRESHOLD = 0.7

_last_daily_cleanup_date = None


def _now_ist() -> datetime:
    return datetime.utcnow() + IST_OFFSET


def _determine_exit_reason(entry: float, target: float, sl: float, exit_ltp: float) -> str:
    if exit_ltp >= entry:
        distance_to_target = target - entry
        progress = (exit_ltp - entry) / distance_to_target if distance_to_target > 0 else 0.0
        if progress >= NEAR_MISS_THRESHOLD:
            return "Target Missed — Closed Near Target"
        return "Closed in Profit at Market Close"
    else:
        distance_to_sl = entry - sl
        progress = (entry - exit_ltp) / distance_to_sl if distance_to_sl > 0 else 0.0
        if progress >= NEAR_MISS_THRESHOLD:
            return "Stop-Loss Narrowly Avoided"
        return "Closed in Loss at Market Close"


async def _get_live_ltp(index_name: str, security_id: str, fallback: float) -> float:
    from app.services.dhan_websocket import market_data_store
    store = market_data_store.get(index_name, {})
    node = store.get(security_id)
    if node and node.get("ltp", 0) > 0:
        return float(node["ltp"])
    return fallback


async def _close_single_paper_trade(db, trade_id, exit_ltp: float, exit_reason: str):
    """Atomically closes ONE paper trade by its _id — used by the reconciliation
    safety-net, found via signal_id (stable DB field), not the in-memory tracker."""
    claimed = await db.paper_trades.find_one_and_update(
        {"_id": trade_id, "status": "OPEN"},
        {"$set": {"status": "CLOSING"}}
    )
    if not claimed:
        return

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
            "closed_by": "RECONCILE_SAFETY_NET"
        }}
    )
    logger.info(f"🛡️ [RECONCILE] Paper trade {trade_id} closed: {exit_reason} | Net PnL ₹{net_pnl:.2f}")


async def _reconcile_active_signals(db, broadcast_callback=None):
    """
    🛡️ SAFETY NET — runs every 60s, INDEPENDENT of the tick-triggered in-memory
    active_positions_tracker (dhan_websocket.py). Directly compares every
    ACTIVE signal's target/SL against live LTP in market_data_store. Catches
    any signal that the primary tick-based path missed for any reason, within
    at most ~60 seconds — so nothing stays stuck ACTIVE despite already having
    hit its target or SL. Uses signal_id (stable DB field) to find and close
    linked OPEN paper trades, not the fragile in-memory tracker.
    """
    from app.services.dhan_websocket import market_data_store

    cursor = db.signals.find({"status": "ACTIVE"})
    active_sigs = await cursor.to_list(length=500)

    for sig in active_sigs:
        index_name = sig.get("index_name", "NIFTY")
        security_id = sig.get("security_id", "")
        target = float(sig.get("shz_upper", 0.0))
        sl = float(sig.get("stop_loss", 0.0))
        if not security_id:
            continue

        store = market_data_store.get(index_name, {})
        node = store.get(security_id)
        if not node or node.get("ltp", 0) <= 0:
            continue
        live_ltp = float(node["ltp"])

        status_update = None
        if target > 0 and live_ltp >= target:
            status_update = "TARGET_HIT"
        elif sl > 0 and live_ltp <= sl:
            status_update = "SL_HIT"
        if not status_update:
            continue

        claimed = await db.signals.find_one_and_update(
            {"_id": sig["_id"], "status": "ACTIVE"},
            {"$set": {"status": status_update, "exit_ltp": live_ltp}}
        )
        if not claimed:
            continue  # already closed via the normal tick-based path meanwhile

        outcome_code = OUTCOME_TARGET_HIT if status_update == "TARGET_HIT" else OUTCOME_SL_HIT
        await update_signal_outcome(db, str(sig["_id"]), outcome_code)

        logger.warning(
            f"🛡️ [RECONCILE SAFETY-NET] Signal {sig['_id']} was stuck ACTIVE despite "
            f"LTP already at ₹{live_ltp} — force-closed as {status_update}."
        )

        trade_cursor = db.paper_trades.find({"signal_id": str(sig["_id"]), "status": "OPEN"})
        trades = await trade_cursor.to_list(length=50)
        for trade in trades:
            reason = "Target Hit (Safety-Net)" if status_update == "TARGET_HIT" else "Stop-Loss Hit (Safety-Net)"
            await _close_single_paper_trade(db, trade["_id"], live_ltp, reason)

        if broadcast_callback:
            await broadcast_callback({
                "type": "SIGNAL_STATUS_UPDATE",
                "signal_id": str(sig["_id"]),
                "status": status_update,
                "exit_ltp": live_ltp
            })


async def _eod_close_paper_trades(db, query_filter: Dict[str, Any]) -> int:
    cursor = db.paper_trades.find({**query_filter, "status": "OPEN"})
    trades = await cursor.to_list(length=1000)
    if not trades:
        return 0

    closed_count = 0
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
        target = float(claimed.get("target1", 0.0))
        sl = float(claimed.get("stop_loss", 0.0))
        security_id = claimed.get("security_id", "")
        index_name = claimed.get("index_name", "NIFTY")

        exit_ltp = await _get_live_ltp(index_name, security_id, buy_price)
        exit_reason = _determine_exit_reason(buy_price, target, sl, exit_ltp)

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
                "closed_by": "EOD_AUTO_SQUARE_OFF"
            }}
        )

        closed_count += 1

    logger.info(f"📤 [EOD AUTO SQUARE-OFF] Closed {closed_count} paper trade(s).")
    return closed_count


async def _eod_close_signals(db, query_filter: Dict[str, Any]) -> int:
    cursor = db.signals.find({**query_filter, "status": "ACTIVE"})
    sigs = await cursor.to_list(length=1000)
    if not sigs:
        return 0

    for sig in sigs:
        entry = float(sig.get("entry_price", 0.0))
        target = float(sig.get("shz_upper", 0.0))
        sl = float(sig.get("stop_loss") or sig.get("shz_lower") or 0.0)
        security_id = sig.get("security_id", "")
        index_name = sig.get("index_name", "NIFTY")

        exit_ltp = await _get_live_ltp(index_name, security_id, entry)
        exit_reason = _determine_exit_reason(entry, target, sl, exit_ltp)

        await db.signals.update_one(
            {"_id": sig["_id"]},
            {"$set": {
                "status": "EXPIRED",
                "exit_ltp": exit_ltp,
                "exit_reason": exit_reason,
                "expired_at": datetime.utcnow()
            }}
        )

        outcome_code = OUTCOME_EXPIRED_PROFIT if exit_ltp >= entry else OUTCOME_EXPIRED_LOSS
        await update_signal_outcome(db, str(sig["_id"]), outcome_code)

    logger.info(f"📤 [EOD AUTO EXPIRE] Marked {len(sigs)} signal(s) as EXPIRED.")
    return len(sigs)


async def eod_auto_square_off_loop(broadcast_callback=None):
    """
    Har 60 second check karta hai. Real broker MIS auto-square-off jaisa behavior:
      1. SAFETY NET (stale-day): purane IST-day se bacha hua OPEN/ACTIVE record turant close.
      2. 🛡️ RECONCILIATION SAFETY NET (every cycle): independently re-checks har
         ACTIVE signal ko live LTP ke against — primary tick-based tracker jo miss
         kare, use max ~60 sec me pakad leta hai.
      3. SCHEDULED: aaj ke OPEN/ACTIVE records sirf 3:15 PM IST ke baad close hote hain.
    """
    from app.core.database import get_database

    logger.info("⏱️ EOD Auto Square-Off Scheduler started.")

    while True:
        try:
            await asyncio.sleep(60)
            db = await get_database()
            ist_now = _now_ist()

            ist_today_midnight_utc = datetime(ist_now.year, ist_now.month, ist_now.day) - IST_OFFSET

            stale_filter = {"created_at": {"$lt": ist_today_midnight_utc}}
            await _eod_close_paper_trades(db, stale_filter)
            await _eod_close_signals(db, stale_filter)

            # 🛡️ Runs EVERY cycle — this is the fix for signals getting stuck ACTIVE.
            await _reconcile_active_signals(db, broadcast_callback)

            is_scheduled_time = (ist_now.hour, ist_now.minute) >= (EOD_HOUR, EOD_MINUTE)
            is_weekday = ist_now.weekday() < 5

            if is_scheduled_time and is_weekday:
                today_filter = {"created_at": {"$gte": ist_today_midnight_utc}}
                await _eod_close_paper_trades(db, today_filter)
                await _eod_close_signals(db, today_filter)

                from app.services.candle_storage import snapshot_previous_close, cleanup_old_candles
                await snapshot_previous_close(db)
                await cleanup_old_candles(db)

                global _last_daily_cleanup_date
                today_str = ist_now.strftime("%Y-%m-%d")
                if _last_daily_cleanup_date != today_str:
                    from app.services.dhan_websocket import reset_daily_state
                    reset_daily_state()
                    _last_daily_cleanup_date = today_str

        except asyncio.CancelledError:
            logger.info("⏱️ EOD Auto Square-Off Scheduler stopped.")
            break
        except Exception as e:
            logger.error(f"EOD Scheduler error: {str(e)}", exc_info=True)
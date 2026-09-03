import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any

from app.models.paper_trading import calculate_indian_option_charges
from app.services.feature_logger import update_signal_outcome, OUTCOME_EXPIRED_PROFIT, OUTCOME_EXPIRED_LOSS

logger = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)
EOD_HOUR = 15
EOD_MINUTE = 15
NEAR_MISS_THRESHOLD = 0.7  # 70% distance covered towards target/SL counts as "near miss"

_last_daily_cleanup_date = None


def _now_ist() -> datetime:
    return datetime.utcnow() + IST_OFFSET


def _determine_exit_reason(entry: float, target: float, sl: float, exit_ltp: float) -> str:
    """
    Bilkul broker jaisa: market close ke waqt jo bhi real LTP hai usi par close hota hai.
    Ye function sirf ek DESCRIPTIVE label deta hai ki trade kitna target/SL ke kareeb tha —
    exit price hamesha real exit_ltp hi hota hai, kabhi target/SL ka number nahi.
    """
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


async def eod_auto_square_off_loop():
    """
    Har 60 second check karta hai. Real broker MIS auto-square-off jaisa behavior:
      1. SAFETY NET: kisi bhi previous IST-day se bacha hua OPEN/ACTIVE record turant
         close ho jaata hai, chahe abhi kitna bhi time ho (server-downtime cover karta hai).
      2. SCHEDULED: aaj (IST) ke OPEN/ACTIVE records sirf 3:15 PM IST ke baad close hote hain,
         weekdays par — bilkul jaisa Groww/Upstox MIS square-off karte hain.
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

            is_scheduled_time = (ist_now.hour, ist_now.minute) >= (EOD_HOUR, EOD_MINUTE)
            is_weekday = ist_now.weekday() < 5

            if is_scheduled_time and is_weekday:
                today_filter = {"created_at": {"$gte": ist_today_midnight_utc}}
                await _eod_close_paper_trades(db, today_filter)
                await _eod_close_signals(db, today_filter)

               # 🧹 Clear cached option-chain snapshots after market close — next
                # day's data is completely different (new strikes/expiry context),
                # so yesterday's snapshot is worthless and just wastes storage.
                await db.market_snapshots.delete_many({})

                  # 📸 Snapshot today's final candle close (per index) BEFORE cleanup —
                # tomorrow's Gap-Fill Fader strategy needs this to compute the gap.
                from app.services.candle_storage import snapshot_previous_close, cleanup_old_candles
                await snapshot_previous_close(db)

                # 🧹 Clear old intraday candle history — only today's is ever needed.
                await cleanup_old_candles(db)

                # 🧹 CRITICAL: reset in-memory market_data_store / subscribed_security_ids /
                # active_positions_tracker — these leak memory forever otherwise (see
                # reset_daily_state() docstring). Guarded to run only ONCE per day even
                # though this branch executes every 60s after 3:15 PM.
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
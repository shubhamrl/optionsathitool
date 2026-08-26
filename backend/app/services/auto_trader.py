import logging
from datetime import datetime, timedelta
from typing import Dict, Any

from app.models.paper_trading import LOT_SIZES

logger = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)
DEFAULT_VIRTUAL_FUNDS = 100000.0


def _ist_today_start_utc() -> datetime:
    ist_now = datetime.utcnow() + IST_OFFSET
    return datetime(ist_now.year, ist_now.month, ist_now.day) - IST_OFFSET


async def auto_execute_for_all_users(db, signal_doc: Dict[str, Any], source: str):
    """
    Called right after Global Scan / Candle Scalp inserts a signal. Checks every
    user who has opted in (auto_trade_settings collection) for this specific
    source, applies their personal risk controls (daily loss cap, daily trade
    cap, lot size), and — if everything passes — auto-places a paper trade for
    them, exactly like clicking 'EXECUTE PAPER TRADE' manually. Runs silently:
    a user does not need to be online for this to work, and failures for one
    user never block others (wrapped per-user in try/except).

    source: "GLOBAL_SCAN" or "CANDLE_SCALP" — each has its own independent
    enable-toggle, lot size, daily-loss cap and daily-trade cap per user.
    """
    field_prefix = "global_scan" if source == "GLOBAL_SCAN" else "candle_scalp"
    enabled_field = f"{field_prefix}_enabled"

    security_id = signal_doc.get("security_id", "")
    index_name = signal_doc.get("index_name", "NIFTY")
    entry_price = float(signal_doc.get("entry_price", 0.0))
    stop_loss = float(signal_doc.get("stop_loss", 0.0))
    target1 = float(signal_doc.get("shz_upper", 0.0))
    signal_id = str(signal_doc.get("_id", ""))
    signal_type = signal_doc.get("signal", "BUY CALL")
    strike = signal_doc.get("strike", "")

    if not security_id or entry_price <= 0 or not signal_id:
        return

    today_start = _ist_today_start_utc()

    cursor = db.auto_trade_settings.find({enabled_field: True})
    async for cfg in cursor:
        user_id = cfg.get("user_id")
        if not user_id:
            continue

        try:
            lot_size = int(cfg.get(f"{field_prefix}_lot_size", 1))
            max_daily_loss = cfg.get(f"{field_prefix}_max_daily_loss")
            max_daily_trades = cfg.get(f"{field_prefix}_max_daily_trades")

            # 1. Daily trade-count cap
            if max_daily_trades:
                trades_today = await db.paper_trades.count_documents({
                    "user_id": user_id, "source": source, "auto_executed": True,
                    "created_at": {"$gte": today_start}
                })
                if trades_today >= int(max_daily_trades):
                    continue

            # 2. Daily loss cap (only realized/closed auto-trades count towards it)
            if max_daily_loss:
                closed_cursor = db.paper_trades.find({
                    "user_id": user_id, "source": source, "auto_executed": True,
                    "status": "SQUARED_OFF", "created_at": {"$gte": today_start}
                })
                closed_today = await closed_cursor.to_list(length=1000)
                pnl_today = sum(float(t.get("net_pnl", 0.0)) for t in closed_today)
                if pnl_today < 0 and abs(pnl_today) >= float(max_daily_loss):
                    continue

            # 3. Don't double-up on the same contract for this user
            existing_open = await db.paper_trades.find_one({
                "user_id": user_id, "security_id": security_id, "status": "OPEN"
            })
            if existing_open:
                continue

            # 4. Wallet balance check
            quantity = lot_size * LOT_SIZES.get(index_name, 25)
            required_margin = entry_price * quantity

            wallet = await db.paper_wallets.find_one({"user_id": user_id})
            current_balance = wallet.get("balance", DEFAULT_VIRTUAL_FUNDS) if wallet else DEFAULT_VIRTUAL_FUNDS
            if current_balance < required_margin:
                continue

            # 5. Place the trade
            new_balance = current_balance - required_margin
            await db.paper_wallets.update_one(
                {"user_id": user_id},
                {"$set": {"balance": new_balance}},
                upsert=True
            )

            paper_trade = {
                "user_id": user_id,
                "index_name": index_name,
                "signal": signal_type,
                "strike": strike,
                "security_id": security_id,
                "signal_id": signal_id,
                "buy_price": entry_price,
                "sell_price": 0.0,
                "quantity": quantity,
                "lots": lot_size,
                "stop_loss": stop_loss,
                "target1": target1,
                "status": "OPEN",
                "margin_used": required_margin,
                "auto_executed": True,
                "source": source,
                "created_at": datetime.utcnow()
            }
            result = await db.paper_trades.insert_one(paper_trade)
            trade_id = str(result.inserted_id)

            from app.services.dhan_websocket import link_paper_trade_to_position
            link_paper_trade_to_position(security_id, signal_id, trade_id)

            logger.info(f"🤖 [AUTO-TRADE] {source} → user {user_id}: {strike} {lot_size}L @ ₹{entry_price}")

        except Exception as e:
            logger.error(f"Auto-trade failed for user {user_id} ({source}): {str(e)}")
            continue
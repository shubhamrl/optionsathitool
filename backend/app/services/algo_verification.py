import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

from app.models.paper_trading import LOT_SIZES

logger = logging.getLogger(__name__)

VERIFICATION_WINDOW_SECONDS = 30
CHECK_INTERVAL_SECONDS = 5
DEFAULT_VIRTUAL_FUNDS = 100000.0


async def queue_for_verification(db, signal_doc: Dict[str, Any], strategy_key: str):
    """
    Called right after a strategy fires a normal signal (unchanged existing
    flow — this is purely ADDITIVE). If (strategy_key, index_name) is on the
    admin's algo_whitelist, queues it for a 30-second live-price verification
    instead of immediately treating it as an 'algo' signal. This never touches
    or delays the original signal's normal lifecycle (SL/Target tracking, EOD,
    leaderboard) — it's a separate, secondary check layered on top.
    """
    index_name = signal_doc["index_name"]
    whitelist_entry = await db.algo_whitelist.find_one({
        "strategy_key": strategy_key, "index_name": index_name, "enabled": True
    })
    if not whitelist_entry:
        return

    await db.algo_pending_verification.insert_one({
        "signal_id": str(signal_doc["_id"]),
        "strategy_key": strategy_key,
        "index_name": index_name,
        "security_id": signal_doc["security_id"],
        "entry_price": signal_doc["entry_price"],
        "selected_type": signal_doc["selected_type"],
        "queued_at": datetime.utcnow(),
        "resolved": False
    })
    logger.info(f"🕐 [ALGO QUEUE] {strategy_key} on {index_name} queued for 30s verification.")


async def _auto_execute_algo_trade(db, signal_doc: Dict[str, Any], strategy_key: str):
    """
    For each user who has opted IN (via user_algo_settings) to auto-paper-trade
    this exact (strategy_key, index_name) combo, places a paper trade for them —
    works even if they're offline, same principle as the earlier Global-Scan/
    Candle-Scalp auto-trader, but keyed per (user, strategy, index) since a user
    might want ON for one combo and OFF for another.
    """
    index_name = signal_doc["index_name"]
    security_id = signal_doc["security_id"]
    entry_price = float(signal_doc["entry_price"])
    stop_loss = float(signal_doc["stop_loss"])
    target1 = float(signal_doc["shz_upper"])
    signal_id = str(signal_doc["_id"])
    signal_type = signal_doc["signal"]
    strike = signal_doc["strike"]

    cursor = db.user_algo_settings.find({
        "strategy_key": strategy_key, "index_name": index_name, "auto_paper_trade": True
    })
    async for cfg in cursor:
        user_id = cfg["user_id"]
        try:
            lot_size = int(cfg.get("lot_size", 1))

            existing_open = await db.paper_trades.find_one({
                "user_id": user_id, "security_id": security_id, "status": "OPEN"
            })
            if existing_open:
                continue

            quantity = lot_size * LOT_SIZES.get(index_name, 25)
            required_margin = entry_price * quantity

            wallet = await db.paper_wallets.find_one({"user_id": user_id})
            current_balance = wallet.get("balance", DEFAULT_VIRTUAL_FUNDS) if wallet else DEFAULT_VIRTUAL_FUNDS
            if current_balance < required_margin:
                continue

            await db.paper_wallets.update_one(
                {"user_id": user_id},
                {"$set": {"balance": current_balance - required_margin}},
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
                "source": "ALGO_VERIFIED",
                "created_at": datetime.utcnow()
            }
            result = await db.paper_trades.insert_one(paper_trade)
            trade_id = str(result.inserted_id)

            from app.services.dhan_websocket import link_paper_trade_to_position
            link_paper_trade_to_position(security_id, signal_id, trade_id)

            logger.info(f"🤖 [ALGO AUTO-TRADE] user {user_id}: {strike} {lot_size}L @ ₹{entry_price}")
        except Exception as e:
            logger.error(f"Algo auto-trade failed for user {user_id}: {str(e)}")
            continue


async def _resolve_pending(db, pending: Dict[str, Any], broadcast_callback=None):
    from app.services.dhan_websocket import market_data_store

    index_name = pending["index_name"]
    security_id = pending["security_id"]
    entry_price = pending["entry_price"]

    store = market_data_store.get(index_name, {})
    node = store.get(security_id)
    live_ltp = float(node["ltp"]) if node and node.get("ltp", 0) > 0 else None

    # Favorable = premium hasn't dropped meaningfully below entry (small 3% noise
    # tolerance) — since every signal here is an option BUY (CE or PE), "still
    # favorable" means the same thing in both directions: price didn't reverse.
    is_confirmed = live_ltp is not None and live_ltp >= entry_price * 0.97

    await db.algo_pending_verification.update_one(
        {"_id": pending["_id"]},
        {"$set": {
            "resolved": True,
            "confirmed": is_confirmed,
            "live_ltp_at_resolution": live_ltp,
            "resolved_at": datetime.utcnow()
        }}
    )

    if not is_confirmed:
        logger.info(f"❌ [ALGO REJECTED] {pending['strategy_key']} on {index_name} — price reversed during verification window.")
        return

    signal_doc = await db.signals.find_one({"_id": __import__("bson").ObjectId(pending["signal_id"])})
    if not signal_doc:
        return
    signal_doc["_id"] = pending["signal_id"]

    await db.algo_confirmed_signals.insert_one({
        "signal_id": pending["signal_id"],
        "strategy_key": pending["strategy_key"],
        "index_name": index_name,
        "confirmed_at": datetime.utcnow()
    })

    logger.info(f"✅ [ALGO CONFIRMED] {pending['strategy_key']} on {index_name} — verified after {VERIFICATION_WINDOW_SECONDS}s.")

    await _auto_execute_algo_trade(db, signal_doc, pending["strategy_key"])

    if broadcast_callback:
        signal_doc["created_at"] = signal_doc["created_at"].isoformat() if isinstance(signal_doc.get("created_at"), datetime) else signal_doc.get("created_at")
        await broadcast_callback({"type": "ALGO_SIGNAL_CONFIRMED", "signal": signal_doc})


async def algo_verification_loop(broadcast_callback=None):
    """Checks every 5s for pending verifications whose 30-second window has
    elapsed, and resolves them (confirm or reject) based on live price."""
    logger.info("🤖 Algo Verification Engine started.")
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            from app.core.database import get_database
            db = await get_database()

            cutoff = datetime.utcnow() - timedelta(seconds=VERIFICATION_WINDOW_SECONDS)
            cursor = db.algo_pending_verification.find({"resolved": False, "queued_at": {"$lte": cutoff}})
            pending_list = await cursor.to_list(length=100)

            for pending in pending_list:
                await _resolve_pending(db, pending, broadcast_callback)

        except asyncio.CancelledError:
            logger.info("🤖 Algo Verification Engine stopped.")
            break
        except Exception as e:
            logger.error(f"Algo Verification Engine error: {str(e)}", exc_info=True)
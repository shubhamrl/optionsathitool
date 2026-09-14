import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

from app.models.paper_trading import LOT_SIZES

logger = logging.getLogger(__name__)

VERIFICATION_WINDOW_SECONDS = 30
CHECK_INTERVAL_SECONDS = 5
DEFAULT_VIRTUAL_FUNDS = 100000.0

# 🎯 Accuracy improvement — a signal is rejected EARLY (fail-fast) if price ever
# drops this much below entry DURING the 30s window, instead of only checking
# the final snapshot. Previously a signal that dipped hard and merely
# "recovered" back to -3% by the 30s mark still got confirmed — a common
# false-confirmation pattern that likely contributed to today's SL hits.
EARLY_REJECT_DROP_PCT = 0.02  # 2%


async def queue_for_verification(db, signal_doc: Dict[str, Any], strategy_key: str):
    """
    Called right after a strategy fires a normal signal (purely additive — does
    not affect the signal's normal lifecycle). If (strategy_key, index_name) is
    on the admin's algo_whitelist, queues it for 30-second live-price
    verification before treating it as a confirmed algo signal.
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
        "min_ltp_seen": signal_doc["entry_price"],
        "resolved": False,
        "rejected_early": False
    })
    logger.info(f"🕐 [ALGO QUEUE] {strategy_key} on {index_name} queued for {VERIFICATION_WINDOW_SECONDS}s verification.")


async def get_master_toggle(db, user_id: str) -> bool:
    """Single master ON/OFF switch per user for algo auto-paper-trading —
    default OFF. When ON, every algo-CONFIRMED signal auto-executes for this
    user regardless of which whitelisted strategy/index it came from — no
    per-combo setup needed, so nothing is ever missed even if the user isn't
    online when a signal confirms."""
    doc = await db.user_algo_master_settings.find_one({"_id": user_id})
    return bool(doc.get("enabled", False)) if doc else False


async def _auto_execute_algo_trade(db, signal_doc: Dict[str, Any], strategy_key: str):
    index_name = signal_doc["index_name"]
    security_id = signal_doc["security_id"]
    entry_price = float(signal_doc["entry_price"])
    stop_loss = float(signal_doc["stop_loss"])
    target1 = float(signal_doc["shz_upper"])
    signal_id = str(signal_doc["_id"])
    signal_type = signal_doc["signal"]
    strike = signal_doc["strike"]

    cursor = db.user_algo_master_settings.find({"enabled": True})
    async for cfg in cursor:
        user_id = cfg["_id"]
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


async def _monitor_active_pending(db):
    """
    Runs every cycle (5s) against still-open (unresolved, un-rejected) pending
    verifications — tracks the minimum LTP seen so far, and REJECTS EARLY the
    moment price drops too far below entry, rather than waiting the full 30s.
    """
    from app.services.dhan_websocket import market_data_store

    cursor = db.algo_pending_verification.find({"resolved": False, "rejected_early": False})
    active_pending = await cursor.to_list(length=200)

    for pending in active_pending:
        index_name = pending["index_name"]
        security_id = pending["security_id"]
        entry_price = pending["entry_price"]

        store = market_data_store.get(index_name, {})
        node = store.get(security_id)
        if not node or node.get("ltp", 0) <= 0:
            continue
        live_ltp = float(node["ltp"])

        new_min = min(pending.get("min_ltp_seen", entry_price), live_ltp)
        update_fields = {"min_ltp_seen": new_min}

        drop_pct = (entry_price - new_min) / entry_price if entry_price > 0 else 0
        if drop_pct >= EARLY_REJECT_DROP_PCT:
            update_fields["rejected_early"] = True
            update_fields["resolved"] = True
            update_fields["confirmed"] = False
            update_fields["resolved_at"] = datetime.utcnow()
            logger.info(f"❌ [ALGO EARLY-REJECT] {pending['strategy_key']} on {index_name} — price dropped {round(drop_pct*100,1)}% during verification window.")

        await db.algo_pending_verification.update_one({"_id": pending["_id"]}, {"$set": update_fields})


async def _resolve_pending(db, pending: Dict[str, Any], broadcast_callback=None):
    """Final resolution once the 30s window has elapsed (for pendings that
    weren't already early-rejected). Requires BOTH: live price at/above entry
    (no tolerance for being down), AND current price-momentum still favors the
    signal's direction — a much stricter bar than the old '-3% tolerance'
    single-point check."""
    from app.services.dhan_websocket import market_data_store, get_price_momentum

    index_name = pending["index_name"]
    security_id = pending["security_id"]
    entry_price = pending["entry_price"]
    selected_type = pending["selected_type"]

    store = market_data_store.get(index_name, {})
    node = store.get(security_id)
    live_ltp = float(node["ltp"]) if node and node.get("ltp", 0) > 0 else None

    price_ok = live_ltp is not None and live_ltp >= entry_price
    momentum = get_price_momentum(index_name)
    momentum_ok = momentum.get("bias") == selected_type

    is_confirmed = price_ok and momentum_ok

    await db.algo_pending_verification.update_one(
        {"_id": pending["_id"]},
        {"$set": {
            "resolved": True,
            "confirmed": is_confirmed,
            "live_ltp_at_resolution": live_ltp,
            "momentum_at_resolution": momentum.get("bias"),
            "resolved_at": datetime.utcnow()
        }}
    )

    if not is_confirmed:
        logger.info(f"❌ [ALGO REJECTED] {pending['strategy_key']} on {index_name} — price_ok={price_ok}, momentum_ok={momentum_ok}.")
        return

    from bson import ObjectId
    signal_doc = await db.signals.find_one({"_id": ObjectId(pending["signal_id"])})
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
    logger.info("🤖 Algo Verification Engine started.")
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            from app.core.database import get_database
            db = await get_database()

            # 🎯 Fail-fast monitoring (every cycle)
            await _monitor_active_pending(db)

            # Final resolution for windows that have fully elapsed
            cutoff = datetime.utcnow() - timedelta(seconds=VERIFICATION_WINDOW_SECONDS)
            cursor = db.algo_pending_verification.find({
                "resolved": False, "rejected_early": False, "queued_at": {"$lte": cutoff}
            })
            pending_list = await cursor.to_list(length=100)
            for pending in pending_list:
                await _resolve_pending(db, pending, broadcast_callback)

        except asyncio.CancelledError:
            logger.info("🤖 Algo Verification Engine stopped.")
            break
        except Exception as e:
            logger.error(f"Algo Verification Engine error: {str(e)}", exc_info=True)
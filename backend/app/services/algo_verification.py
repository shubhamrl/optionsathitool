import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple

from bson import ObjectId

from app.models.paper_trading import LOT_SIZES

logger = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)

VERIFICATION_WINDOW_SECONDS = 30
CHECK_INTERVAL_SECONDS = 5
EARLY_REJECT_DROP_PCT = 0.02  # 2% drop below entry during window => fail-fast reject

# Used ONLY when creating a brand-new paper wallet for a user who has none yet.
# Existing wallets always use their real stored balance.
NEW_WALLET_STARTING_CAPITAL = 100000.0

# ---------------- 5-layer scoring config (all tunable) ----------------
W_MARKET = 0.35
W_TECHNICAL = 0.50
W_ML = 0.15              # kept low: ML shadow-table is still flat (see ADMIN > ML Readiness)
ML_BASELINE_WIN_RATE = 0.40
ML_SCALE = 0.20

ENABLE_INSTANT_CONFIRM = True
INSTANT_CONFIRM_SCORE = 0.60   # very strong agreement => confirm immediately, skip 30s wait
WINDOW_CONFIRM_SCORE = 0.30    # minimum composite after the 30s window
TECHNICAL_VETO_SCORE = -0.60   # technical strongly against => reject whatever else says
MIN_LIVE_RR = 1.0              # reward/risk measured from LIVE price must be >= this
MAX_CHASE_PROGRESS = 0.5       # already ran >50% of entry->target => don't chase
NO_NEW_ALGO_AFTER_HM = (15, 0) # IST; EOD square-off is 3:15 PM

_vix_history: deque = deque(maxlen=60)
_broadcast_cb = None


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _now_ist() -> datetime:
    return datetime.utcnow() + IST_OFFSET


def _get_live_ltp(index_name: str, security_id: str) -> Optional[float]:
    from app.services.dhan_websocket import market_data_store
    node = market_data_store.get(index_name, {}).get(security_id)
    if node and node.get("ltp", 0) > 0:
        return float(node["ltp"])
    return None


def _record_vix_sample():
    from app.services.dhan_websocket import get_live_vix
    _vix_history.append(get_live_vix())


def _vix_trend_pct() -> Optional[float]:
    """Recent-vs-older VIX change. None until ~60s of samples exist."""
    if len(_vix_history) < 12:
        return None
    samples = list(_vix_history)
    recent = sum(samples[-6:]) / 6
    older = sum(samples[:-6]) / len(samples[:-6])
    if older <= 0:
        return None
    return (recent - older) / older


# ---------------- Layer 2: Market Analyst ----------------
async def _market_analyst(signal_doc: Dict[str, Any], strategy_key: str) -> Tuple[float, Dict[str, Any]]:
    from app.services.dhan_websocket import get_live_vix
    from app.services.candle_storage import get_today_candles
    from app.services.regime_indicators import get_adx_5m
    from app.services.strategy_engine import STRATEGY_REGIME_CATEGORY

    index_name = signal_doc["index_name"]
    direction = 1.0 if signal_doc["selected_type"] == "CE" else -1.0

    # PCR alignment (live PCR stored on the signal at creation time)
    pcr = float(signal_doc.get("pcr") or 0.0)
    pcr_score = 0.0 if pcr <= 0 else _clamp((pcr - 1.0) / 0.25) * direction

    # VIX: rising VIX favours option buyers, falling VIX = theta bleed; very high VIX = expensive
    vix = get_live_vix()
    trend = _vix_trend_pct()
    vix_score = 0.0
    if trend is not None:
        vix_score += _clamp(trend / 0.03) * 0.7
    if vix > 22:
        vix_score -= 0.3
    vix_score = _clamp(vix_score)

    # Regime strength for this strategy's category
    adx = await get_adx_5m(index_name)
    category = STRATEGY_REGIME_CATEGORY.get(strategy_key, "neutral")
    if adx is None or category == "neutral":
        regime_score = 0.0
    elif category == "trend":
        regime_score = _clamp((adx - 18.0) / 12.0)
    else:
        regime_score = _clamp((22.0 - adx) / 12.0)

    score = 0.40 * pcr_score + 0.30 * vix_score + 0.30 * regime_score
    return _clamp(score), {
        "pcr": pcr, "pcr_score": round(pcr_score, 3),
        "vix": vix, "vix_trend": None if trend is None else round(trend, 4), "vix_score": round(vix_score, 3),
        "adx": adx, "regime_score": round(regime_score, 3),
    }


# ---------------- Layer 3: Technical Analyst ----------------
def _technical_analyst(signal_doc: Dict[str, Any], live_ltp: Optional[float]) -> Tuple[float, Dict[str, Any]]:
    from app.services.dhan_websocket import get_price_momentum, index_candle_store

    index_name = signal_doc["index_name"]
    selected_type = signal_doc["selected_type"]
    direction = 1.0 if selected_type == "CE" else -1.0

    bias = get_price_momentum(index_name).get("bias")
    if bias == selected_type:
        mom_score = 1.0
    elif bias in ("CE", "PE"):
        mom_score = -1.0
    else:
        mom_score = 0.0

    candle_score = 0.0
    candles = index_candle_store.get(index_name, [])
    if candles:
        last = candles[-1]
        rng = last["high"] - last["low"]
        if rng > 0:
            candle_score = _clamp((last["close"] - last["open"]) / rng) * direction

    entry = float(signal_doc["entry_price"])
    follow_score = 0.0
    if live_ltp and entry > 0:
        follow_score = _clamp(((live_ltp - entry) / entry) / 0.02)

    score = 0.45 * mom_score + 0.25 * candle_score + 0.30 * follow_score
    return _clamp(score), {
        "mom_score": mom_score, "candle_score": round(candle_score, 3), "follow_score": round(follow_score, 3),
    }


# ---------------- Layer 4: Researcher (ML historical-pattern model) ----------------
def _researcher_score(signal_doc: Dict[str, Any]) -> Optional[float]:
    conf = signal_doc.get("predicted_confidence")
    if conf is None:
        return None
    return _clamp((float(conf) - ML_BASELINE_WIN_RATE) / ML_SCALE)


def _composite(market: float, technical: float, ml: Optional[float]) -> float:
    total_w = W_MARKET + W_TECHNICAL
    weighted = W_MARKET * market + W_TECHNICAL * technical
    if ml is not None:
        total_w += W_ML
        weighted += W_ML * ml
    return weighted / total_w


# ---------------- Layer 5: Profit Check ----------------
def _profit_check(signal_doc: Dict[str, Any], live_ltp: Optional[float]) -> Tuple[bool, str]:
    if live_ltp is None:
        return False, "no live price"
    entry = float(signal_doc["entry_price"])
    target = float(signal_doc["shz_upper"])
    sl = float(signal_doc["stop_loss"])
    reward = target - live_ltp
    risk = live_ltp - sl
    if reward <= 0 or risk <= 0:
        return False, "live price already beyond target or SL"
    if reward / risk < MIN_LIVE_RR:
        return False, f"live RR {round(reward / risk, 2)} < {MIN_LIVE_RR}"
    span = target - entry
    if span > 0 and (live_ltp - entry) / span > MAX_CHASE_PROGRESS:
        return False, "price already ran too far towards target (chasing)"
    return True, "ok"


async def _evaluate_layers(signal_doc: Dict[str, Any], strategy_key: str) -> Dict[str, Any]:
    live_ltp = _get_live_ltp(signal_doc["index_name"], signal_doc["security_id"])
    market, market_detail = await _market_analyst(signal_doc, strategy_key)
    technical, tech_detail = _technical_analyst(signal_doc, live_ltp)
    ml = _researcher_score(signal_doc)
    composite = _composite(market, technical, ml)
    profit_ok, profit_reason = _profit_check(signal_doc, live_ltp)
    return {
        "market": market, "technical": technical, "ml": ml, "composite": composite,
        "profit_ok": profit_ok, "profit_reason": profit_reason, "live_ltp": live_ltp,
        "detail": {**market_detail, **tech_detail},
    }


def _score_fields(e: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "score_market": round(e["market"], 3),
        "score_technical": round(e["technical"], 3),
        "score_ml": None if e["ml"] is None else round(e["ml"], 3),
        "score_composite": round(e["composite"], 3),
        "profit_ok": e["profit_ok"],
        "profit_reason": e["profit_reason"],
        "live_ltp_at_eval": e["live_ltp"],
        "layer_detail": e["detail"],
    }


# ---------------- Wallet + auto paper trade ----------------
async def _get_or_create_wallet(db, user_id: str) -> Dict[str, Any]:
    wallet = await db.paper_wallets.find_one({"user_id": user_id})
    if wallet:
        return wallet
    await db.paper_wallets.update_one(
        {"user_id": user_id},
        {"$setOnInsert": {
            "balance": NEW_WALLET_STARTING_CAPITAL,
            "initial_capital": NEW_WALLET_STARTING_CAPITAL,
            "realized_pnl": 0.0,
            "total_taxes_paid": 0.0,
            "total_trades": 0,
            "created_at": datetime.utcnow(),
        }},
        upsert=True
    )
    return await db.paper_wallets.find_one({"user_id": user_id})


async def get_master_toggle(db, user_id: str) -> bool:
    doc = await db.user_algo_master_settings.find_one({"_id": user_id})
    return bool(doc.get("enabled", False)) if doc else False


async def _auto_execute_algo_trade(db, signal_doc: Dict[str, Any], strategy_key: str, exec_price: Optional[float] = None):
    index_name = signal_doc["index_name"]
    security_id = signal_doc["security_id"]
    signal_entry = float(signal_doc["entry_price"])
    stop_loss = float(signal_doc["stop_loss"])
    target1 = float(signal_doc["shz_upper"])
    signal_id = str(signal_doc["_id"])

    # Real-trading behaviour: fill at the CURRENT live price, SL/Target stay as signalled.
    fill_price = float(exec_price) if exec_price and exec_price > 0 else signal_entry

    cursor = db.user_algo_master_settings.find({"enabled": True})
    async for cfg in cursor:
        user_id = cfg["_id"]
        try:
            lot_size = int(cfg.get("lot_size", 1))

            existing_open = await db.paper_trades.find_one({
                "user_id": user_id, "security_id": security_id, "status": "OPEN"
            })
            if existing_open:
                logger.info(f"🤖 [ALGO SKIP] user {user_id}: already has an OPEN position on {security_id}.")
                continue

            quantity = lot_size * LOT_SIZES.get(index_name, 25)
            required_margin = round(fill_price * quantity, 2)

            wallet = await _get_or_create_wallet(db, user_id)

            # Atomic: deducts only if the REAL current balance covers it.
            deduct = await db.paper_wallets.update_one(
                {"user_id": user_id, "balance": {"$gte": required_margin}},
                {"$inc": {"balance": -required_margin}}
            )
            if deduct.modified_count == 0:
                logger.info(f"🤖 [ALGO SKIP] user {user_id}: insufficient balance (₹{wallet.get('balance', 0):.2f} < required ₹{required_margin:.2f}).")
                continue

            paper_trade = {
                "user_id": user_id,
                "index_name": index_name,
                "signal": signal_doc["signal"],
                "strike": signal_doc["strike"],
                "security_id": security_id,
                "signal_id": signal_id,
                "buy_price": fill_price,
                "signal_entry_price": signal_entry,
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

            logger.info(f"🤖 [ALGO AUTO-TRADE] user {user_id}: {signal_doc['strike']} {lot_size}L @ ₹{fill_price} (signal entry ₹{signal_entry})")
        except Exception as e:
            logger.error(f"Algo auto-trade failed for user {user_id}: {str(e)}")
            continue


async def _broadcast_confirmed(signal_doc: Dict[str, Any]):
    if not _broadcast_cb:
        return
    payload = dict(signal_doc)
    for k, v in list(payload.items()):
        if isinstance(v, datetime):
            payload[k] = v.isoformat()
    await _broadcast_cb({"type": "ALGO_SIGNAL_CONFIRMED", "signal": payload})


async def _finalize_confirmed(db, signal_doc: Dict[str, Any], strategy_key: str, evald: Dict[str, Any], path: str):
    await db.algo_confirmed_signals.insert_one({
        "signal_id": str(signal_doc["_id"]),
        "strategy_key": strategy_key,
        "index_name": signal_doc["index_name"],
        "confirmed_at": datetime.utcnow(),
        "confirm_path": path,
        "composite_score": round(evald["composite"], 3),
    })
    logger.info(f"✅ [ALGO CONFIRMED/{path}] {strategy_key} on {signal_doc['index_name']} | composite {round(evald['composite'], 3)}")
    await _auto_execute_algo_trade(db, signal_doc, strategy_key, exec_price=evald["live_ltp"])
    await _broadcast_confirmed(signal_doc)


# ---------------- Queue (called right after a strategy fires) ----------------
async def queue_for_verification(db, signal_doc: Dict[str, Any], strategy_key: str):
    index_name = signal_doc["index_name"]
    whitelist_entry = await db.algo_whitelist.find_one({
        "strategy_key": strategy_key, "index_name": index_name, "enabled": True
    })
    if not whitelist_entry:
        return

    ist = _now_ist()
    if (ist.hour, ist.minute) >= NO_NEW_ALGO_AFTER_HM:
        logger.info(f"⏰ [ALGO SKIP] {strategy_key} on {index_name}: too close to EOD square-off.")
        return

    insert_result = await db.algo_pending_verification.insert_one({
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

    if not ENABLE_INSTANT_CONFIRM:
        return

    try:
        fresh = await db.signals.find_one({"_id": ObjectId(str(signal_doc["_id"]))})
        if not fresh:
            return
        evald = await _evaluate_layers(fresh, strategy_key)
        await db.algo_pending_verification.update_one(
            {"_id": insert_result.inserted_id}, {"$set": _score_fields(evald)}
        )
        strong = (
            evald["profit_ok"]
            and evald["technical"] > TECHNICAL_VETO_SCORE
            and evald["composite"] >= INSTANT_CONFIRM_SCORE
        )
        if strong:
            claim = await db.algo_pending_verification.update_one(
                {"_id": insert_result.inserted_id, "resolved": False},
                {"$set": {"resolved": True, "confirmed": True, "confirm_path": "instant", "resolved_at": datetime.utcnow()}}
            )
            if claim.modified_count == 1:
                fresh["_id"] = str(fresh["_id"])
                await _finalize_confirmed(db, fresh, strategy_key, evald, "instant")
    except Exception as e:
        logger.warning(f"⚠️ Instant-confirm evaluation failed (falls back to 30s window): {str(e)}")


# ---------------- Fail-fast monitor ----------------
async def _monitor_active_pending(db):
    from app.services.dhan_websocket import market_data_store

    cursor = db.algo_pending_verification.find({"resolved": False, "rejected_early": False})
    active_pending = await cursor.to_list(length=200)

    for pending in active_pending:
        index_name = pending["index_name"]
        security_id = pending["security_id"]
        entry_price = pending["entry_price"]

        node = market_data_store.get(index_name, {}).get(security_id)
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
            logger.info(f"❌ [ALGO EARLY-REJECT] {pending['strategy_key']} on {index_name} — dropped {round(drop_pct * 100, 1)}% in window.")

        await db.algo_pending_verification.update_one(
            {"_id": pending["_id"], "resolved": False}, {"$set": update_fields}
        )


# ---------------- Final resolution after the 30s window ----------------
async def _resolve_pending(db, pending: Dict[str, Any]):
    from app.services.dhan_websocket import get_price_momentum

    index_name = pending["index_name"]
    security_id = pending["security_id"]
    entry_price = pending["entry_price"]
    selected_type = pending["selected_type"]
    strategy_key = pending["strategy_key"]

    live_ltp = _get_live_ltp(index_name, security_id)
    price_ok = live_ltp is not None and live_ltp >= entry_price
    momentum = get_price_momentum(index_name)
    momentum_ok = momentum.get("bias") == selected_type

    signal_doc = await db.signals.find_one({"_id": ObjectId(pending["signal_id"])})
    if not signal_doc:
        await db.algo_pending_verification.update_one(
            {"_id": pending["_id"], "resolved": False},
            {"$set": {"resolved": True, "confirmed": False, "resolved_at": datetime.utcnow()}}
        )
        return

    if signal_doc.get("status") != "ACTIVE":
        await db.algo_pending_verification.update_one(
            {"_id": pending["_id"], "resolved": False},
            {"$set": {
                "resolved": True, "confirmed": False, "resolved_at": datetime.utcnow(),
                "profit_reason": f"signal already {signal_doc.get('status')} before confirmation",
            }}
        )
        logger.info(f"❌ [ALGO REJECTED] {strategy_key} on {index_name} — signal already {signal_doc.get('status')} before the {VERIFICATION_WINDOW_SECONDS}s window ended.")
        return

    evald = None
    try:
        evald = await _evaluate_layers(signal_doc, strategy_key)
    except Exception as e:
        logger.warning(f"⚠️ 5-layer evaluation failed, using legacy price+momentum rule only: {str(e)}")

    if evald is None:
        layers_ok = True
    else:
        layers_ok = (
            evald["profit_ok"]
            and evald["technical"] > TECHNICAL_VETO_SCORE
            and evald["composite"] >= WINDOW_CONFIRM_SCORE
        )

    is_confirmed = price_ok and momentum_ok and layers_ok

    fields = {
        "resolved": True,
        "confirmed": is_confirmed,
        "live_ltp_at_resolution": live_ltp,
        "momentum_at_resolution": momentum.get("bias"),
        "resolved_at": datetime.utcnow(),
    }
    if evald is not None:
        fields.update(_score_fields(evald))
    if is_confirmed:
        fields["confirm_path"] = "window"

    claim = await db.algo_pending_verification.update_one(
        {"_id": pending["_id"], "resolved": False}, {"$set": fields}
    )
    if claim.modified_count == 0:
        return  # already resolved elsewhere (early-reject / instant path)

    if not is_confirmed:
        reason = evald["profit_reason"] if evald else "-"
        comp = round(evald["composite"], 3) if evald else None
        logger.info(f"❌ [ALGO REJECTED] {strategy_key} on {index_name} — price_ok={price_ok}, momentum_ok={momentum_ok}, composite={comp}, profit={reason}.")
        return

    signal_doc["_id"] = str(signal_doc["_id"])
    final_eval = evald if evald is not None else {"composite": 0.0, "live_ltp": live_ltp}
    await _finalize_confirmed(db, signal_doc, strategy_key, final_eval, "window")


async def algo_verification_loop(broadcast_callback=None):
    global _broadcast_cb
    _broadcast_cb = broadcast_callback
    logger.info("🤖 Algo Verification Engine (5-layer) started.")
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            _record_vix_sample()

            from app.core.database import get_database
            db = await get_database()

            await _monitor_active_pending(db)

            cutoff = datetime.utcnow() - timedelta(seconds=VERIFICATION_WINDOW_SECONDS)
            cursor = db.algo_pending_verification.find({
                "resolved": False, "rejected_early": False, "queued_at": {"$lte": cutoff}
            })
            pending_list = await cursor.to_list(length=100)
            for pending in pending_list:
                await _resolve_pending(db, pending)

        except asyncio.CancelledError:
            logger.info("🤖 Algo Verification Engine stopped.")
            break
        except Exception as e:
            logger.error(f"Algo Verification Engine error: {str(e)}", exc_info=True)
import asyncio
import logging
from datetime import datetime
from typing import Dict, Any

from app.core.database import get_database
from app.core.config import settings
from app.core.market_hours import is_market_open
from app.services.token_registry import fetch_expiry_list, fetch_full_option_chain_data
from app.services.dhan_websocket import (
    register_token_index_mapping,
    get_orb_levels,
    get_price_momentum,
    track_active_position_trade,
)
from app.engine.ai_surveillance import AISurveillanceEngine
from app.engine.risk_manager import risk_manager
from app.engine.confluence_math import calculate_option_greeks
from app.services.feature_logger import log_signal_features

logger = logging.getLogger(__name__)

SCAN_INTERVAL_SECONDS = 20
SCANNED_INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY"]
GLOBAL_SIGNAL_USER_ID = "SYSTEM_GLOBAL_SCANNER"
COOLDOWN_MINUTES = 5

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


def _register_tokens(oc: Dict[str, Any], index_name: str):
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


async def _scan_one_index(db, index_name: str, broadcast_callback=None):
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

    _register_tokens(oc, index_name)

    # 💾 Save the latest snapshot — DECODE/FORCED SCALP endpoints will read from
    # this instead of hitting Dhan on every single click.
    await db.market_snapshots.update_one(
        {"index_name": index_name},
        {"$set": {
            "index_name": index_name,
            "expiry": expiry,
            "spot": spot,
            "oc": oc,
            "updated_at": datetime.utcnow()
        }},
        upsert=True
    )

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

    orb_levels = get_orb_levels(index_name)
    price_momentum = get_price_momentum(index_name)

    ai_engine = AISurveillanceEngine(index_name)
    ai_result = ai_engine.evaluate_market_state(
        spot=spot, atm_strike=atm_strike, oc_data=oc, spot_history=[spot],
        orb_high=orb_levels["orb_high"], orb_low=orb_levels["orb_low"],
        price_momentum=price_momentum
    )

    signal = ai_result["signal"]
    selected_type = ai_result["selected_type"]
    if signal == "NO TRADE" or not selected_type:
        return

    selected_node = atm_node.get("ce") if selected_type == "CE" else atm_node.get("pe")
    entry_price = float(selected_node.get("last_price") or 0.0) if selected_node else 0.0
    security_id = str(selected_node.get("security_id") or "") if selected_node else ""
    if entry_price <= 0 or not security_id:
        return

    # 🔁 Cooldown — don't republish the same setup every 20 seconds
    cooling_window = datetime.utcnow().timestamp() - (COOLDOWN_MINUTES * 60)
    recent = await db.signals.find_one({
        "user_id": GLOBAL_SIGNAL_USER_ID,
        "atm_strike": atm_strike,
        "index_name": index_name,
        "created_at": {"$gt": datetime.utcfromtimestamp(cooling_window)}
    })
    if recent:
        return

    iv = float(selected_node.get("implied_volatility") or 13.5) if selected_node else 13.5
    greeks = calculate_option_greeks(spot, atm_strike, 0.02, iv, option_type=selected_type)
    risk_result = risk_manager.calculate_trade_targets(
        index_name=index_name, entry_premium=entry_price, spot_atr=12.0,
        delta=greeks["delta"], is_forced_scalp=False
    )

    signal_doc = {
        "user_id": GLOBAL_SIGNAL_USER_ID,
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
        "breakout_status": "GLOBAL_SCAN",
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

    orb_triggered = "CE" if spot > orb_levels["orb_high"] > 0 else ("PE" if 0 < orb_levels["orb_low"] and spot < orb_levels["orb_low"] else None)
    await log_signal_features(
        db=db, signal_id=signal_id, index_name=index_name, mode="standard",
        pcr=ai_result["pcr"], delta=greeks["delta"], iv=iv, score=ai_result["ai_score"],
        selected_type=selected_type, momentum_bias=price_momentum.get("bias") if price_momentum else None,
        orb_triggered=orb_triggered
    )

    logger.info(f"🌐 [GLOBAL SIGNAL] {index_name} {signal} {atm_strike}{selected_type} @ ₹{entry_price}")

    if broadcast_callback:
        signal_doc["created_at"] = signal_doc["created_at"].isoformat()
        await broadcast_callback({
            "type": "GLOBAL_SIGNAL_UPDATE",
            "signal": signal_doc
        })


async def global_market_scanner_loop(broadcast_callback=None):
    """
    Continuously scans all 4 indices during market hours using the SAME confluence
    logic as the Standard 'DECODE SIGNAL' button (ORB + PCR + real price-momentum),
    and pushes any genuine signal to every connected user automatically. Also caches
    each index's latest option-chain snapshot in MongoDB so per-click DECODE/FORCED
    SCALP requests no longer need a fresh Dhan API call every time.
    """
    logger.info("🌐 Global Market Scanner started.")
    while True:
        try:
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)
            if not is_market_open():
                continue
            db = await get_database()
            for index_name in SCANNED_INDICES:
                await _scan_one_index(db, index_name, broadcast_callback)
        except asyncio.CancelledError:
            logger.info("🌐 Global Market Scanner stopped.")
            break
        except Exception as e:
            logger.error(f"Global Market Scanner error: {str(e)}", exc_info=True)
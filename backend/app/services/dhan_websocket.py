import asyncio
import json
import logging
import struct
import websockets
from datetime import datetime, timedelta
from typing import Dict, Any, List, Set, Optional

from app.core.config import settings
from app.engine.ai_surveillance import AISurveillanceEngine
from app.services.dhan_binary_parser import parse_dhan_binary_feed
from app.services.feature_logger import update_signal_outcome, OUTCOME_TARGET_HIT, OUTCOME_SL_HIT

logger = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)
MARKET_OPEN_HOUR, MARKET_OPEN_MIN = 9, 15
ORB_CLOSE_HOUR, ORB_CLOSE_MIN = 9, 30
MAX_CANDLES_STORED = 30

# Central Spot Index Tokens Config
INDEX_SPOT_TOKENS = {
    "13": "NIFTY",
    "25": "BANKNIFTY",
    "51": "SENSEX",
    "27": "FINNIFTY"
}

# Live Market State Memory
market_data_store: Dict[str, Dict[str, Dict[str, Any]]] = {
    "NIFTY": {"spot": 0.0, "pcr": 1.0, "trend": "NEUTRAL"},
    "BANKNIFTY": {"spot": 0.0, "pcr": 1.0, "trend": "NEUTRAL"},
    "SENSEX": {"spot": 0.0, "pcr": 1.0, "trend": "NEUTRAL"},
    "FINNIFTY": {"spot": 0.0, "pcr": 1.0, "trend": "NEUTRAL"}
}

security_id_to_index_map: Dict[str, str] = {}
active_positions_tracker: Dict[str, List[Dict[str, Any]]] = {}
subscribed_security_ids: Set[str] = set()

# 🕯️ Rolling 1-minute candle store per index (built live from spot ticks) — used to
# detect real price-action momentum (e.g. "big green candle followed by a red candle
# means booking/selling pressure"), which pure PCR/OI snapshots can't see.
index_candle_store: Dict[str, List[Dict[str, Any]]] = {idx: [] for idx in INDEX_SPOT_TOKENS.values()}
_current_candle: Dict[str, Dict[str, Any]] = {}

# 📐 Real Opening Range (9:15–9:30 AM IST) high/low per index, per day — replaces the
# old hardcoded orb_high=0.0 / orb_low=0.0 that made the Standard Signal's ORB trigger
# permanently dead.
index_orb_store: Dict[str, Dict[str, Any]] = {
    idx: {"date": None, "orb_high": 0.0, "orb_low": 0.0, "locked": False}
    for idx in INDEX_SPOT_TOKENS.values()
}


def _now_ist() -> datetime:
    return datetime.utcnow() + IST_OFFSET


def _update_candle_and_orb(index_name: str, ltp: float, ist_now: datetime):
    minute_key = ist_now.strftime("%H:%M")
    today_str = ist_now.strftime("%Y-%m-%d")

    cur = _current_candle.get(index_name)
    if cur is None or cur["minute"] != minute_key:
        if cur is not None:
            finalized_candle = {
                "minute": cur["minute"], "open": cur["open"],
                "high": cur["high"], "low": cur["low"], "close": cur["close"]
            }
            index_candle_store[index_name].append(finalized_candle)
            if len(index_candle_store[index_name]) > MAX_CANDLES_STORED:
                index_candle_store[index_name] = index_candle_store[index_name][-MAX_CANDLES_STORED:]

            # 💾 Also persist to MongoDB (full-day history) — needed by strategies
            # like VWAP/Bollinger Bands that need more than the 30-min rolling window.
            import asyncio
            from app.services.candle_storage import persist_finalized_candle
            asyncio.create_task(persist_finalized_candle(index_name, finalized_candle))

        _current_candle[index_name] = {"minute": minute_key, "open": ltp, "high": ltp, "low": ltp, "close": ltp}
    else:
        cur["high"] = max(cur["high"], ltp)
        cur["low"] = min(cur["low"], ltp)
        cur["close"] = ltp

    orb = index_orb_store[index_name]
    if orb["date"] != today_str:
        orb["date"] = today_str
        orb["orb_high"] = 0.0
        orb["orb_low"] = 0.0
        orb["locked"] = False

    within_orb_window = (
        (ist_now.hour, ist_now.minute) >= (MARKET_OPEN_HOUR, MARKET_OPEN_MIN)
        and (ist_now.hour, ist_now.minute) < (ORB_CLOSE_HOUR, ORB_CLOSE_MIN)
    )
    if within_orb_window and not orb["locked"]:
        orb["orb_high"] = ltp if orb["orb_high"] == 0.0 else max(orb["orb_high"], ltp)
        orb["orb_low"] = ltp if orb["orb_low"] == 0.0 else min(orb["orb_low"], ltp)
    elif (ist_now.hour, ist_now.minute) >= (ORB_CLOSE_HOUR, ORB_CLOSE_MIN):
        orb["locked"] = True


def get_orb_levels(index_name: str) -> Dict[str, float]:
    """Real Opening Range high/low for today, or 0.0/0.0 if not yet built (e.g. before
    9:30 AM, or on a day the server started after the opening window)."""
    orb = index_orb_store.get(index_name, {})
    return {"orb_high": orb.get("orb_high", 0.0), "orb_low": orb.get("orb_low", 0.0)}


def get_price_momentum(index_name: str) -> Dict[str, Any]:
    """
    Reads the rolling 1-minute candle history to detect real price-action momentum:
      1. Reversal pattern: a strong-bodied candle followed by an opposite-colour
         candle (e.g. big green then red = likely profit-booking/selling pressure).
      2. Fallback: short-term directional slope over the last few candles.
    Returns {"bias": "CE"|"PE"|"NEUTRAL", "reason": str}.
    """
    candles = index_candle_store.get(index_name, [])
    if len(candles) < 2:
        return {"bias": "NEUTRAL", "reason": "Not enough live candle history yet."}

    idx_config = settings.INDICES_CONFIG.get(index_name.upper(), settings.INDICES_CONFIG["NIFTY"])
    threshold = idx_config.get("min_sl_points", 8.0) * 0.6

    last = candles[-1]
    prev = candles[-2]
    last_body = last["close"] - last["open"]
    prev_body = prev["close"] - prev["open"]

    if prev_body >= threshold and last_body < 0:
        return {"bias": "PE", "reason": "Strong up-candle followed by a red candle — likely profit-booking/selling pressure."}
    if prev_body <= -threshold and last_body > 0:
        return {"bias": "CE", "reason": "Strong down-candle followed by a green candle — likely short-covering/buying pressure."}

    recent = candles[-4:] if len(candles) >= 4 else candles
    net_change = recent[-1]["close"] - recent[0]["open"]
    if net_change >= threshold * 0.5:
        return {"bias": "CE", "reason": f"Price grinding higher over the last {len(recent)} minute(s)."}
    if net_change <= -threshold * 0.5:
        return {"bias": "PE", "reason": f"Price grinding lower over the last {len(recent)} minute(s)."}

    return {"bias": "NEUTRAL", "reason": "No clear directional momentum in recent price action."}


class DhanWebSocketClient:
    def __init__(self):
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.is_connected: bool = False
        self.reconnect_delay: int = 3

    async def connect_and_listen(self, broadcast_callback=None):
        while True:
            if not settings.DHAN_ACCESS_TOKEN or not settings.DHAN_CLIENT_ID:
                await asyncio.sleep(10)
                continue

            ws_url = (
                f"wss://api-feed.dhan.co?version=2&"
                f"token={settings.DHAN_ACCESS_TOKEN}&"
                f"clientId={settings.DHAN_CLIENT_ID}&"
                f"authType=2"
            )

            try:
                logger.info("🔌 Connecting to Dhan Live Marketfeed...")
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                    self.ws = ws
                    self.is_connected = True
                    logger.info("✅ Dhan Live Marketfeed Connected!")

                    await self._subscribe_index_spots()

                    if subscribed_security_ids:
                        await self.subscribe_symbols(list(subscribed_security_ids))

                    while True:
                        raw_msg = await ws.recv()
                        if isinstance(raw_msg, bytes):
                            parsed_tick = parse_dhan_binary_feed(raw_msg)
                            if parsed_tick and "security_id" in parsed_tick:
                                await self._handle_tick_update(parsed_tick, broadcast_callback)

            except Exception as e:
                self.is_connected = False
                logger.warning(f"⚠️ WS Disconnected ({str(e)}). Reconnecting in {self.reconnect_delay}s...")
                await asyncio.sleep(self.reconnect_delay)

    async def _subscribe_index_spots(self):
        spot_instruments = [
            {"ExchangeSegment": "IDX_I", "SecurityId": "13"},
            {"ExchangeSegment": "IDX_I", "SecurityId": "25"},
            {"ExchangeSegment": "IDX_I", "SecurityId": "27"},
            {"ExchangeSegment": "BSE_FNO", "SecurityId": "51"}
        ]
        payload = {
            "RequestCode": 15,
            "InstrumentCount": len(spot_instruments),
            "InstrumentList": spot_instruments
        }
        try:
            await self.ws.send(json.dumps(payload))
            logger.info("🟢 Main Index Spot Tokens Subscribed to Live Stream!")
        except Exception as e:
            logger.error(f"Failed to subscribe index spots: {str(e)}")

    async def subscribe_symbols(self, security_ids: List[str], exchange_segment: str = "NSE_FNO"):
        if not self.ws or not self.is_connected:
            subscribed_security_ids.update(security_ids)
            return

        clean_ids = [str(sid) for sid in security_ids if str(sid) not in subscribed_security_ids]
        if not clean_ids:
            return

        instruments = [{"ExchangeSegment": exchange_segment, "SecurityId": sid} for sid in clean_ids]
        payload = {"RequestCode": 17, "InstrumentCount": len(instruments), "InstrumentList": instruments}
        try:
            await self.ws.send(json.dumps(payload))
            subscribed_security_ids.update(clean_ids)
            logger.info(f"📡 Subscribed {len(clean_ids)} option contracts.")
        except Exception as e:
            logger.error(f"Failed to subscribe options: {str(e)}")

    async def _handle_tick_update(self, tick: Dict[str, Any], broadcast_callback=None):
        sec_id = tick["security_id"]
        ltp = tick.get("ltp", 0.0)

        if sec_id in INDEX_SPOT_TOKENS and ltp > 0:
            idx_name = INDEX_SPOT_TOKENS[sec_id]
            market_data_store[idx_name]["spot"] = round(ltp, 2)
            _update_candle_and_orb(idx_name, ltp, _now_ist())
            return

        index_name = security_id_to_index_map.get(sec_id, "NIFTY")
        if index_name in market_data_store:
            if sec_id not in market_data_store[index_name]:
                market_data_store[index_name][sec_id] = {}
            if ltp > 0:
                market_data_store[index_name][sec_id]["ltp"] = round(ltp, 2)

        if sec_id in active_positions_tracker and ltp > 0:
            asyncio.create_task(self._evaluate_active_position_exit(sec_id, ltp, broadcast_callback))

    async def _evaluate_active_position_exit(self, sec_id: str, current_ltp: float, broadcast_callback=None):
        positions = active_positions_tracker.get(sec_id, [])
        remaining_positions = []

        for pos in positions:
            target = pos.get("target", 0.0)
            sl = pos.get("sl", 0.0)
            signal_id = pos.get("signal_id")
            trade_id = pos.get("trade_id")

            status_update = None
            if target > 0 and current_ltp >= target:
                status_update = "TARGET_HIT"
            elif sl > 0 and current_ltp <= sl:
                status_update = "SL_HIT"

            if status_update:
                logger.info(f"🎯 [SIGNAL CLOSED] Signal: {signal_id} | Status: {status_update} @ ₹{current_ltp}")
                try:
                    from app.core.database import get_database
                    from bson import ObjectId
                    db = await get_database()

                    await db.signals.update_one(
                        {"_id": ObjectId(signal_id)},
                        {"$set": {"status": status_update, "exit_ltp": current_ltp}}
                    )

                    outcome_code = OUTCOME_TARGET_HIT if status_update == "TARGET_HIT" else OUTCOME_SL_HIT
                    await update_signal_outcome(db, signal_id, outcome_code)

                    if trade_id:
                        await self._auto_square_off_paper_trade(
                            db, trade_id, current_ltp, status_update, broadcast_callback
                        )

                except Exception as e:
                    logger.error(f"Error updating DB status: {str(e)}")

                if broadcast_callback:
                    await broadcast_callback({
                        "type": "SIGNAL_STATUS_UPDATE",
                        "signal_id": signal_id,
                        "status": status_update,
                        "exit_ltp": current_ltp
                    })
            else:
                remaining_positions.append(pos)

        if remaining_positions:
            active_positions_tracker[sec_id] = remaining_positions
        else:
            active_positions_tracker.pop(sec_id, None)

    async def _auto_square_off_paper_trade(self, db, trade_id: str, exit_ltp: float, exit_reason: str, broadcast_callback=None):
        from bson import ObjectId
        from app.models.paper_trading import calculate_indian_option_charges

        # 🔒 Atomically CLAIM this trade — only proceeds if still OPEN at this exact
        # instant, preventing double wallet-credit if this races with a manual
        # square-off or the EOD scheduler closing the same trade.
        trade = await db.paper_trades.find_one_and_update(
            {"_id": ObjectId(trade_id), "status": "OPEN"},
            {"$set": {"status": "CLOSING"}}
        )
        if not trade:
            return  # Already closed by another path

        user_id = trade["user_id"]
        buy_price = trade["buy_price"]
        quantity = trade["quantity"]
        margin_used = trade["margin_used"]

        charges = calculate_indian_option_charges(buy_price, exit_ltp, quantity)
        net_pnl = charges["net_pnl"]
        total_taxes = charges["total_taxes"]

        wallet = await db.paper_wallets.find_one({"user_id": user_id})
        current_balance = wallet.get("balance", 0.0) if wallet else 0.0
        current_realized_pnl = wallet.get("realized_pnl", 0.0) if wallet else 0.0
        current_taxes = wallet.get("total_taxes_paid", 0.0) if wallet else 0.0

        updated_balance = current_balance + margin_used + net_pnl
        updated_realized_pnl = current_realized_pnl + net_pnl
        updated_taxes = current_taxes + total_taxes

        await db.paper_wallets.update_one(
            {"user_id": user_id},
            {"$set": {
                "balance": round(updated_balance, 2),
                "realized_pnl": round(updated_realized_pnl, 2),
                "total_taxes_paid": round(updated_taxes, 2)
            }}
        )

        await db.paper_trades.update_one(
            {"_id": ObjectId(trade_id)},
            {"$set": {
                "status": "SQUARED_OFF",
                "sell_price": exit_ltp,
                "net_pnl": net_pnl,
                "charges": charges,
                "exit_reason": exit_reason,
                "closed_at": datetime.utcnow()
            }}
        )

        logger.info(f"💰 [AUTO SQUARE-OFF] Trade: {trade_id} | Reason: {exit_reason} | Net PnL: ₹{net_pnl:.2f}")

        if broadcast_callback:
            await broadcast_callback({
                "type": "PAPER_TRADE_AUTO_CLOSED",
                "trade_id": trade_id,
                "status": "SQUARED_OFF",
                "exit_price": exit_ltp,
                "exit_reason": exit_reason,
                "net_pnl": net_pnl
            })


dhan_ws_client = DhanWebSocketClient()


def register_token_index_mapping(token_map: Dict[str, str]):
    security_id_to_index_map.update(token_map)


def track_active_position_trade(security_id: str, signal_id: str, target: float, sl: float, trade_id: Optional[str] = None):
    sec_id_str = str(security_id)
    if sec_id_str not in active_positions_tracker:
        active_positions_tracker[sec_id_str] = []

    active_positions_tracker[sec_id_str].append({
        "signal_id": str(signal_id),
        "target": float(target),
        "sl": float(sl),
        "trade_id": str(trade_id) if trade_id else None
    })

    if sec_id_str not in subscribed_security_ids:
        asyncio.create_task(dhan_ws_client.subscribe_symbols([sec_id_str]))


def link_paper_trade_to_position(security_id: str, signal_id: str, trade_id: str):
    sec_id_str = str(security_id)
    positions = active_positions_tracker.get(sec_id_str, [])
    found = False
    for pos in positions:
        if pos.get("signal_id") == str(signal_id):
            pos["trade_id"] = str(trade_id)
            found = True

    if not found:
        logger.warning(
            f"⚠️ link_paper_trade_to_position: signal {signal_id} security {sec_id_str} par "
            f"tracked nahi mila. Trade {trade_id} SL/Target par auto-close NAHI hoga."
        )

    if sec_id_str not in subscribed_security_ids:
        asyncio.create_task(dhan_ws_client.subscribe_symbols([sec_id_str]))
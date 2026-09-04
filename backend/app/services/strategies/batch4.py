"""
Batch 4 — ORB Breaker (migrated from the old standalone Global Market Scanner)

Same logic as before: Opening-Range Breakout confirmed by PCR + real price
momentum via AISurveillanceEngine. Now just one more entry in the shared
strategy registry — no special treatment, same generic execution pipeline as
every other strategy.
"""
import logging
from typing import Dict, Any, Optional

from app.core.config import settings
from app.services.strategy_engine import register_strategy
from app.services.dhan_websocket import get_orb_levels, get_price_momentum
from app.engine.ai_surveillance import AISurveillanceEngine

logger = logging.getLogger(__name__)


async def detect_orb_breaker(index_name: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    spot = context["spot"]
    oc = context["oc"]

    idx_config = settings.INDICES_CONFIG.get(index_name, settings.INDICES_CONFIG["NIFTY"])
    step = idx_config["step_size"]
    atm_strike = int(round(spot / step) * step)

    orb_levels = get_orb_levels(index_name)
    price_momentum = get_price_momentum(index_name)

    ai_engine = AISurveillanceEngine(index_name)
    ai_result = ai_engine.evaluate_market_state(
        spot=spot, atm_strike=atm_strike, oc_data=oc, spot_history=[spot],
        orb_high=orb_levels["orb_high"], orb_low=orb_levels["orb_low"],
        price_momentum=price_momentum
    )

    if ai_result["signal"] == "NO TRADE" or not ai_result["selected_type"]:
        return None

    return {"bias": ai_result["selected_type"], "reasons": ai_result["reasons"]}


def _aggregate_1min_to_15min(candles_1m):
    """Aggregates one day's 1-minute candles into 15-minute buckets (chronological
    order preserved since a single day's candles are already time-ordered)."""
    buckets = {}
    order = []
    for c in candles_1m:
        try:
            hh, mm = c["m"].split(":")
        except Exception:
            continue
        bucket_min = (int(mm) // 15) * 15
        key = f"{hh}:{bucket_min:02d}"
        if key not in buckets:
            buckets[key] = {"open": c["o"], "high": c["h"], "low": c["l"], "close": c["c"]}
            order.append(key)
        else:
            b = buckets[key]
            b["high"] = max(b["high"], c["h"])
            b["low"] = min(b["low"], c["l"])
            b["close"] = c["c"]
    return [buckets[k] for k in order]


def _ema_series(closes, period):
    """Standard EMA: seeded with SMA of the first `period` values, then smoothed."""
    if len(closes) < period:
        return []
    multiplier = 2 / (period + 1)
    ema_vals = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema_vals.append((price - ema_vals[-1]) * multiplier + ema_vals[-1])
    return ema_vals


async def detect_ema_trend_breakout(index_name: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    15-minute candle EMA(9/20/50) cross-breakout: fires when a 15-min candle
    CLOSES beyond one of these EMAs after being on the other side previously —
    a standard trend-confirmation breakout pattern. EMA-50 needs ~2+ trading
    days of 15-min history, so it naturally stays quiet until enough days of
    candle history have accumulated (see candle_storage.RETAIN_DAYS).
    """
    from app.services.candle_storage import get_recent_days_candles

    day_lists = await get_recent_days_candles(index_name, days=5)
    if not day_lists:
        return None

    candles_15m = []
    for day_candles in day_lists:
        candles_15m.extend(_aggregate_1min_to_15min(day_candles))

    if len(candles_15m) < 11:  # need at least EMA-9 + 1 prior point
        return None

    closes = [c["close"] for c in candles_15m]

    for period, label in ((50, "EMA-50"), (20, "EMA-20"), (9, "EMA-9")):
        ema_vals = _ema_series(closes, period)
        if len(ema_vals) < 2:
            continue

        prev_close = closes[-2]
        prev_ema = ema_vals[-2]
        last_close = closes[-1]
        last_ema = ema_vals[-1]

        crossed_up = prev_close <= prev_ema and last_close > last_ema
        crossed_down = prev_close >= prev_ema and last_close < last_ema

        if crossed_up:
            return {"bias": "CE", "reasons": [f"15-min candle closed above {label} ({round(last_ema,1)}) — bullish breakout confirmed on close."]}
        if crossed_down:
            return {"bias": "PE", "reasons": [f"15-min candle closed below {label} ({round(last_ema,1)}) — bearish breakdown confirmed on close."]}

    return None


register_strategy("ORB_BREAKER", "ORB Breaker", detect_orb_breaker)
register_strategy("EMA_TREND_BREAK", "EMA Trend Breakout", detect_ema_trend_breakout)
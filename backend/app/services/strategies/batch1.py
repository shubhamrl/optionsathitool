"""
Batch 1 — VWAP Bouncer, Bollinger Squeeze, RSI Reversal, Support-Resistance Bounce

All logic below uses standard, publicly-documented technical-analysis concepts
(VWAP, Bollinger Bands, RSI, pivot-based S/R) — not sourced from any paid book.
"""
import logging
from typing import Dict, Any, List, Optional

from app.services.strategy_engine import register_strategy, _get_today_candles_lazy
from app.engine.ai_surveillance import get_oi_support_resistance

logger = logging.getLogger(__name__)


def _sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _stdev(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    variance = sum((v - mean) ** 2 for v in window) / period
    return variance ** 0.5


def _rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ----------------------------------------------------------------------------
# 1. VWAP Bouncer — price crosses session VWAP and holds beyond it (trend-follow)
# ----------------------------------------------------------------------------
async def detect_vwap_bouncer(index_name: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    candles = await _get_today_candles_lazy(context)
    if len(candles) < 15:
        return None

    total_pv, total_vol_proxy = 0.0, 0.0
    for c in candles:
        typical_price = (c["h"] + c["l"] + c["c"]) / 3
        # No real volume feed on spot index — use candle range as a stable proxy
        # weight so VWAP still reflects where more price-time was spent.
        weight = max(c["h"] - c["l"], 0.1)
        total_pv += typical_price * weight
        total_vol_proxy += weight

    if total_vol_proxy <= 0:
        return None
    vwap = total_pv / total_vol_proxy

    live_spot = context["live_spot"]
    if live_spot <= 0:
        return None

    last_3 = candles[-3:]
    buffer = max((last_3[-1]["h"] - last_3[-1]["l"]) * 0.3, 3.0)

    all_above = all(c["c"] > vwap for c in last_3)
    all_below = all(c["c"] < vwap for c in last_3)

    if all_above and live_spot > vwap + buffer:
        return {"bias": "CE", "reasons": [f"Price holding above session VWAP ({round(vwap,1)}) for 3+ candles — trend-following continuation."]}
    if all_below and live_spot < vwap - buffer:
        return {"bias": "PE", "reasons": [f"Price holding below session VWAP ({round(vwap,1)}) for 3+ candles — trend-following continuation."]}
    return None


# ----------------------------------------------------------------------------
# 2. Bollinger Squeeze — bands compress tightly then price breaks out of them
# ----------------------------------------------------------------------------
async def detect_bollinger_squeeze(index_name: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    candles = await _get_today_candles_lazy(context)
    if len(candles) < 25:
        return None

    closes = [c["c"] for c in candles]
    period = 20

    sma = _sma(closes, period)
    sd = _stdev(closes, period)
    if sma is None or sd is None or sd <= 0:
        return None

    upper_band = sma + (2 * sd)
    lower_band = sma - (2 * sd)
    band_width = upper_band - lower_band

    # "Squeeze" check: was the band unusually tight over the last 10 candles
    # relative to itself (using a rolling band-width proxy), then price pokes out?
    prior_closes = closes[-30:-10] if len(closes) >= 30 else closes[:-10]
    prior_sd = _stdev(prior_closes, min(period, len(prior_closes))) if len(prior_closes) >= 5 else None
    was_squeezed = prior_sd is not None and sd < prior_sd * 0.75

    live_spot = context["live_spot"]
    if live_spot <= 0 or not was_squeezed:
        return None

    if live_spot > upper_band:
        return {"bias": "CE", "reasons": [f"Bollinger Band squeeze released — price broke above upper band ({round(upper_band,1)})."]}
    if live_spot < lower_band:
        return {"bias": "PE", "reasons": [f"Bollinger Band squeeze released — price broke below lower band ({round(lower_band,1)})."]}
    return None


# ----------------------------------------------------------------------------
# 3. RSI Reversal — RSI at an extreme + price/RSI divergence => mean-reversion
# ----------------------------------------------------------------------------
async def detect_rsi_reversal(index_name: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    candles = await _get_today_candles_lazy(context)
    if len(candles) < 20:
        return None

    closes = [c["c"] for c in candles]
    current_rsi = _rsi(closes, 14)
    prior_rsi = _rsi(closes[:-3], 14)  # RSI a few candles ago, for divergence check
    if current_rsi is None or prior_rsi is None:
        return None

    recent_high = max(c["h"] for c in candles[-8:])
    recent_low = min(c["l"] for c in candles[-8:])
    live_spot = context["live_spot"]
    if live_spot <= 0:
        return None

    # Oversold + bullish divergence (price near recent low, RSI turning up from <35)
    if current_rsi < 35 and current_rsi > prior_rsi and live_spot <= recent_low * 1.001:
        return {"bias": "CE", "reasons": [f"RSI oversold ({round(current_rsi,1)}) and turning up near session low — mean-reversion bounce."]}
    # Overbought + bearish divergence
    if current_rsi > 65 and current_rsi < prior_rsi and live_spot >= recent_high * 0.999:
        return {"bias": "PE", "reasons": [f"RSI overbought ({round(current_rsi,1)}) and turning down near session high — mean-reversion pullback."]}
    return None


# ----------------------------------------------------------------------------
# 4. Support-Resistance Bounce — price touches an OI-derived wall and rejects it
# ----------------------------------------------------------------------------
async def detect_support_resistance_bounce(index_name: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    oc = context["oc"]
    live_spot = context["live_spot"]
    recent_candles = context.get("recent_candles", [])
    if live_spot <= 0 or len(recent_candles) < 3:
        return None

    resistance_strike, support_strike = get_oi_support_resistance(oc)
    if not resistance_strike or not support_strike:
        return None

    last = recent_candles[-1]
    touch_buffer = max((last["high"] - last["low"]) * 0.5, 4.0)

    # Price touched support and is bouncing up (last candle closed green, near support)
    if abs(live_spot - support_strike) <= touch_buffer and last["close"] > last["open"]:
        return {"bias": "CE", "reasons": [f"Price bounced off Put-OI support wall near {support_strike}."]}
    # Price touched resistance and is rejecting down
    if abs(live_spot - resistance_strike) <= touch_buffer and last["close"] < last["open"]:
        return {"bias": "PE", "reasons": [f"Price rejected at Call-OI resistance wall near {resistance_strike}."]}
    return None


register_strategy("VWAP_BOUNCER", "VWAP Bouncer", detect_vwap_bouncer)
register_strategy("BOLLINGER_SQUEEZE", "Bollinger Squeeze", detect_bollinger_squeeze)
register_strategy("RSI_REVERSAL", "RSI Reversal", detect_rsi_reversal)
register_strategy("SR_BOUNCE", "Support-Resistance Bounce", detect_support_resistance_bounce)
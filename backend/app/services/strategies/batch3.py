"""
Batch 3 — Doji Reversal, Multi-Timeframe Confluence, IV Contraction Entry

Standard, publicly-documented technical-analysis concepts. One honest note:
"IV Contraction Entry" is an adaptation, not the classic IV-Crush strategy —
classic IV Crush is an option-SELLING strategy (short premium), which doesn't
fit this engine's directional-buying-only architecture. This version instead
looks for low/falling IV (cheap premium) combined with directional momentum —
a legitimate but different setup, disclosed here rather than silently reframed.
"""
import logging
from typing import Dict, Any, List, Optional

from app.services.strategy_engine import register_strategy, _get_today_candles_lazy

logger = logging.getLogger(__name__)

# Rolling ATM-IV history per index (in-memory, resets on restart — only needs
# recent readings to judge "falling" IV, not full-day history).
_iv_history: Dict[str, List[float]] = {}
MAX_IV_HISTORY = 20


def _aggregate_candles(candles: List[Dict[str, Any]], bucket_minutes: int) -> List[Dict[str, Any]]:
    """Aggregates 1-minute candles into N-minute buckets (e.g. 5 or 15 min)."""
    buckets: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for c in candles:
        try:
            hh, mm = c["m"].split(":")
        except Exception:
            continue
        bucket_min = (int(mm) // bucket_minutes) * bucket_minutes
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


# ----------------------------------------------------------------------------
# 9. Doji Reversal — small-bodied indecision candle after a clear directional run
# ----------------------------------------------------------------------------
async def detect_doji_reversal(index_name: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    candles = await _get_today_candles_lazy(context)
    if len(candles) < 6:
        return None

    last = candles[-1]
    rng = last["h"] - last["l"]
    if rng <= 0:
        return None
    body = abs(last["c"] - last["o"])

    # Doji: body is a small fraction of the total range (indecision)
    if body > rng * 0.25:
        return None

    prior = candles[-5:-1]
    prior_trend = prior[-1]["c"] - prior[0]["o"]
    trend_size = abs(prior_trend)
    idx_threshold = 4.0  # minimum points to call it a "clear" prior run

    if trend_size < idx_threshold:
        return None

    if prior_trend > 0:
        return {"bias": "PE", "reasons": [f"Doji (indecision) candle after a {round(trend_size,1)}-pt up-run — possible reversal."]}
    else:
        return {"bias": "CE", "reasons": [f"Doji (indecision) candle after a {round(trend_size,1)}-pt down-run — possible reversal."]}


# ----------------------------------------------------------------------------
# 10. Multi-Timeframe Confluence — 5-min AND 15-min trend agree (higher-confidence)
# ----------------------------------------------------------------------------
async def detect_multi_timeframe_confluence(index_name: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    candles = await _get_today_candles_lazy(context)
    if len(candles) < 30:
        return None

    candles_5m = _aggregate_candles(candles, 5)
    candles_15m = _aggregate_candles(candles, 15)
    if len(candles_5m) < 4 or len(candles_15m) < 2:
        return None

    def _trend(bucket_list, n):
        recent = bucket_list[-n:]
        if len(recent) < n:
            return 0.0
        return recent[-1]["close"] - recent[0]["open"]

    trend_5m = _trend(candles_5m, 3)
    trend_15m = _trend(candles_15m, 2)

    min_5m_pts = 6.0
    min_15m_pts = 10.0

    if trend_5m > min_5m_pts and trend_15m > min_15m_pts:
        return {"bias": "CE", "reasons": [f"5-min trend (+{round(trend_5m,1)} pts) AND 15-min trend (+{round(trend_15m,1)} pts) both bullish — multi-timeframe confluence."]}
    if trend_5m < -min_5m_pts and trend_15m < -min_15m_pts:
        return {"bias": "PE", "reasons": [f"5-min trend ({round(trend_5m,1)} pts) AND 15-min trend ({round(trend_15m,1)} pts) both bearish — multi-timeframe confluence."]}
    return None


# ----------------------------------------------------------------------------
# 11. IV Contraction Entry (adapted from classic IV-Crush — see module docstring)
# ----------------------------------------------------------------------------
async def detect_iv_contraction_entry(index_name: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    oc = context["oc"]
    spot = context["spot"]

    # Find ATM IV from either side of the chain (best-effort average of CE/PE at ATM)
    closest_strike, closest_diff = None, None
    for strike_key in oc.keys():
        try:
            sv = float(strike_key)
        except ValueError:
            continue
        diff = abs(sv - spot)
        if closest_diff is None or diff < closest_diff:
            closest_diff = diff
            closest_strike = strike_key

    if closest_strike is None:
        return None

    node = oc[closest_strike]
    ce_iv = float((node.get("ce") or {}).get("implied_volatility") or 0)
    pe_iv = float((node.get("pe") or {}).get("implied_volatility") or 0)
    ivs = [v for v in (ce_iv, pe_iv) if v > 0]
    if not ivs:
        return None
    atm_iv = sum(ivs) / len(ivs)

    history = _iv_history.setdefault(index_name, [])
    history.append(atm_iv)
    if len(history) > MAX_IV_HISTORY:
        _iv_history[index_name] = history[-MAX_IV_HISTORY:]
        history = _iv_history[index_name]

    if len(history) < 8:
        return None

    baseline_iv = sum(history[:-3]) / len(history[:-3])
    is_falling = atm_iv < baseline_iv * 0.92  # IV has meaningfully contracted

    if not is_falling:
        return None

    # Combine with short-term momentum — cheap premium + a real move starting
    recent_candles = context.get("recent_candles", [])
    if len(recent_candles) < 3:
        return None
    last3 = recent_candles[-3:]
    move = last3[-1]["close"] - last3[0]["open"]
    move_threshold = 5.0

    if move > move_threshold:
        return {"bias": "CE", "reasons": [f"ATM IV contracted ({round(atm_iv,1)} vs baseline {round(baseline_iv,1)}) — cheaper premium, entering on emerging up-move."]}
    if move < -move_threshold:
        return {"bias": "PE", "reasons": [f"ATM IV contracted ({round(atm_iv,1)} vs baseline {round(baseline_iv,1)}) — cheaper premium, entering on emerging down-move."]}
    return None


register_strategy("DOJI_REVERSAL", "Doji Reversal", detect_doji_reversal)
register_strategy("MULTI_TIMEFRAME", "Multi-Timeframe Confluence", detect_multi_timeframe_confluence)
register_strategy("IV_CONTRACTION", "IV Contraction Entry", detect_iv_contraction_entry)
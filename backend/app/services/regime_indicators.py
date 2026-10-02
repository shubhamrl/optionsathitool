import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 22.0
ADX_RANGE_THRESHOLD = 18.0


def _wilder_smooth(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    smoothed = [sum(values[:period])]
    for v in values[period:]:
        smoothed.append(smoothed[-1] - (smoothed[-1] / period) + v)
    return smoothed


def calculate_adx(candles: List[Dict[str, Any]], period: int = ADX_PERIOD) -> Optional[float]:
    """
    Standard Wilder's ADX(14) on whatever candle-resolution is passed in
    (works on the 1-minute candles candle_storage.py already persists —
    aggregating to 5-min isn't required for a usable regime-read, and keeps
    this cheap to compute every scan cycle).
    Returns None if not enough candles yet.
    """
    if len(candles) < period * 2:
        return None

    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    closes = [c["c"] for c in candles]

    plus_dm, minus_dm, tr = [], [], []
    for i in range(1, len(candles)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))

    smoothed_tr = _wilder_smooth(tr, period)
    smoothed_plus_dm = _wilder_smooth(plus_dm, period)
    smoothed_minus_dm = _wilder_smooth(minus_dm, period)
    if not smoothed_tr or not smoothed_plus_dm or not smoothed_minus_dm:
        return None

    dx_values = []
    for i in range(len(smoothed_tr)):
        if smoothed_tr[i] == 0:
            continue
        plus_di = 100 * (smoothed_plus_dm[i] / smoothed_tr[i])
        minus_di = 100 * (smoothed_minus_dm[i] / smoothed_tr[i])
        di_sum = plus_di + minus_di
        dx = 100 * (abs(plus_di - minus_di) / di_sum) if di_sum > 0 else 0.0
        dx_values.append(dx)

    if len(dx_values) < period:
        return None
    return round(sum(dx_values[-period:]) / period, 2)


def get_regime(adx_value: Optional[float]) -> str:
    """Returns 'trend', 'range', or 'neutral' (not enough data / mid-zone)."""
    if adx_value is None:
        return "neutral"
    if adx_value > ADX_TREND_THRESHOLD:
        return "trend"
    if adx_value < ADX_RANGE_THRESHOLD:
        return "range"
    return "neutral"


def is_strategy_allowed_in_regime(strategy_category: str, regime: str) -> bool:
    """
    strategy_category: 'trend' | 'mean_reversion' | 'neutral'
    regime: 'trend' | 'range' | 'neutral'

    - 'neutral' strategies (event/volatility-driven, not candle-pattern-based)
      always fire — regime doesn't meaningfully help or hurt them.
    - In a 'neutral' (mid-ADX) regime, everything is allowed — only the clear
      extremes (strong-trend / clear-range) actively filter anything out.
    """
    if strategy_category == "neutral" or regime == "neutral":
        return True
    if regime == "trend":
        return strategy_category == "trend"
    if regime == "range":
        return strategy_category == "mean_reversion"
    return True
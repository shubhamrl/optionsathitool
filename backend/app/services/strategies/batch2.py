"""
Batch 2 — Gap-Fill Fader, MA Crossover, OI Buildup Tracker, Volume Spike Momentum

Standard, publicly-documented technical-analysis concepts. Two honest data-notes:
  - Gap-Fill Fader needs yesterday's close (persisted via candle_storage.py's
    snapshot_previous_close, captured at EOD) — inactive on the very first day
    this runs, active from the next trading day onward.
  - Volume Spike Momentum: Dhan's index-spot feed carries no traded-volume field
    (indices aren't directly traded), so candle RANGE is used as an activity
    proxy instead of true volume — this is disclosed, not silently assumed.
"""
import logging
from typing import Dict, Any, List, Optional

from app.services.strategy_engine import register_strategy, _get_today_candles_lazy
from app.services.candle_storage import get_previous_close

logger = logging.getLogger(__name__)

# In-memory OI snapshot from the previous scan cycle, per index+strike — used to
# detect a genuine BUILDUP (change), not just a static OI level. Resets on
# restart, which is acceptable since it only needs the last cycle to compare.
_prev_oi_snapshot: Dict[str, Dict[str, float]] = {}


def _sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


# ----------------------------------------------------------------------------
# 5. Gap-Fill Fader — big opening gap tends to partially/fully "fill" (mean-revert)
# ----------------------------------------------------------------------------
async def detect_gap_fill_fader(index_name: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    prev_close = await get_previous_close(index_name)
    if prev_close is None or prev_close <= 0:
        return None  # No prior-day data captured yet — inactive until tomorrow

    candles = await _get_today_candles_lazy(context)
    if len(candles) < 3:
        return None

    day_open = candles[0]["o"]
    gap_points = day_open - prev_close
    gap_pct = abs(gap_points) / prev_close * 100

    if gap_pct < 0.3:  # too small a gap to be meaningful
        return None

    live_spot = context["live_spot"]
    if live_spot <= 0:
        return None

    last = candles[-1]
    fading_started = None
    if gap_points > 0:  # gapped up — fade means price drifting back down towards prev_close
        fading_started = last["c"] < day_open and live_spot < day_open
    else:  # gapped down — fade means price drifting back up towards prev_close
        fading_started = last["c"] > day_open and live_spot > day_open

    if not fading_started:
        return None

    if gap_points > 0:
        return {"bias": "PE", "reasons": [f"Gap-up of {round(gap_points,1)} pts ({round(gap_pct,2)}%) showing early fade — mean-reversion towards yesterday's close."]}
    else:
        return {"bias": "CE", "reasons": [f"Gap-down of {round(abs(gap_points),1)} pts ({round(gap_pct,2)}%) showing early fade — mean-reversion towards yesterday's close."]}


# ----------------------------------------------------------------------------
# 6. Moving Average Crossover — fast SMA crosses slow SMA (classic trend signal)
# ----------------------------------------------------------------------------
async def detect_ma_crossover(index_name: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    candles = await _get_today_candles_lazy(context)
    if len(candles) < 23:
        return None

    closes = [c["c"] for c in candles]

    fast_now = _sma(closes, 9)
    slow_now = _sma(closes, 21)
    fast_prev = _sma(closes[:-1], 9)
    slow_prev = _sma(closes[:-1], 21)

    if None in (fast_now, slow_now, fast_prev, slow_prev):
        return None

    crossed_up = fast_prev <= slow_prev and fast_now > slow_now
    crossed_down = fast_prev >= slow_prev and fast_now < slow_now

    if crossed_up:
        return {"bias": "CE", "reasons": [f"9-period SMA crossed above 21-period SMA ({round(fast_now,1)} > {round(slow_now,1)}) — trend turning bullish."]}
    if crossed_down:
        return {"bias": "PE", "reasons": [f"9-period SMA crossed below 21-period SMA ({round(fast_now,1)} < {round(slow_now,1)}) — trend turning bearish."]}
    return None


# ----------------------------------------------------------------------------
# 7. OI Buildup Tracker — sudden fresh OI increase at a strike + price direction
#    => "smart money" positioning footprint (Long Buildup / Short Buildup)
# ----------------------------------------------------------------------------
async def detect_oi_buildup(index_name: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    oc = context["oc"]
    spot = context["spot"]

    current_snapshot: Dict[str, float] = {}
    for strike_key, node in oc.items():
        ce_oi = float((node.get("ce") or {}).get("oi") or 0)
        pe_oi = float((node.get("pe") or {}).get("oi") or 0)
        current_snapshot[f"{strike_key}_CE"] = ce_oi
        current_snapshot[f"{strike_key}_PE"] = pe_oi

    prev_snapshot = _prev_oi_snapshot.get(index_name)
    _prev_oi_snapshot[index_name] = current_snapshot

    if not prev_snapshot:
        return None  # first cycle — nothing to compare yet

    best_buildup = None
    best_pct_change = 0.0

    for key, current_oi in current_snapshot.items():
        prev_oi = prev_snapshot.get(key, 0)
        if prev_oi < 500 or current_oi <= prev_oi:
            continue
        pct_change = (current_oi - prev_oi) / prev_oi * 100
        if pct_change > best_pct_change and pct_change >= 8:  # meaningful jump threshold
            best_pct_change = pct_change
            best_buildup = key

    if not best_buildup:
        return None

    strike_str, opt_type = best_buildup.rsplit("_", 1)
    try:
        strike_val = float(strike_str)
    except ValueError:
        return None

    # Long Buildup interpretation: fresh CE OI addition ABOVE spot (resistance
    # building, bearish for that level) vs BELOW/AT spot with price rising (bullish
    # conviction). Kept simple and directionally conservative:
    if opt_type == "CE" and strike_val <= spot:
        return {"bias": "CE", "reasons": [f"Fresh Call OI buildup (+{round(best_pct_change,1)}%) near/below spot at {strike_val} — bullish positioning."]}
    if opt_type == "PE" and strike_val >= spot:
        return {"bias": "PE", "reasons": [f"Fresh Put OI buildup (+{round(best_pct_change,1)}%) near/above spot at {strike_val} — bearish positioning."]}
    return None


# ----------------------------------------------------------------------------
# 8. Volume Spike Momentum — unusually large candle RANGE (activity proxy, since
#    true traded volume isn't available on this index-spot feed) + directional close
# ----------------------------------------------------------------------------
async def detect_volume_spike_momentum(index_name: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    candles = await _get_today_candles_lazy(context)
    if len(candles) < 12:
        return None

    ranges = [c["h"] - c["l"] for c in candles]
    avg_range = _sma(ranges[:-1], 10)
    if avg_range is None or avg_range <= 0:
        return None

    last = candles[-1]
    last_range = last["h"] - last["l"]

    if last_range < avg_range * 2.2:  # needs to be a clear spike, not just noise
        return None

    body = last["c"] - last["o"]
    if abs(body) < last_range * 0.5:  # spike but indecisive candle — skip
        return None

    if body > 0:
        return {"bias": "CE", "reasons": [f"Sudden activity spike — candle range {round(last_range,1)} pts vs avg {round(avg_range,1)} pts, closed strongly bullish."]}
    else:
        return {"bias": "PE", "reasons": [f"Sudden activity spike — candle range {round(last_range,1)} pts vs avg {round(avg_range,1)} pts, closed strongly bearish."]}


register_strategy("GAP_FILL_FADER", "Gap-Fill Fader", detect_gap_fill_fader)
register_strategy("MA_CROSSOVER", "MA Crossover", detect_ma_crossover)
register_strategy("OI_BUILDUP", "OI Buildup Tracker", detect_oi_buildup)
register_strategy("VOLUME_SPIKE", "Volume Spike Momentum", detect_volume_spike_momentum)
"""
Signal gates + speed helpers for the Strategy Engine.

Gates (pure in-memory, synchronous, microseconds):
  1) Confluence: fire only when >= MIN_AGREE DIFFERENT strategies agree (same index/direction)
     within CONFLUENCE_WINDOW_MINUTES, and nobody live disagrees.
  2) Midday sleeping window (IST): scanning never stops, but inside the window a stricter bar
     applies (3 strategies, or just 2 when the market is genuinely trending, ADX > 22).
  3) SOLO_FAST_LANE: combos you trust never wait for a second strategy.
Speed helpers: live_option_ltp() and prewarm_atm_ticks().
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)

# ---------------- switches / tunables ----------------
CONFLUENCE_ENABLED = True
SLEEP_WINDOW_ENABLED = True        # both False => gate allows everything (instant rollback)

CONFLUENCE_WINDOW_MINUTES = 5      # how long a strategy's vote stays valid
MIN_AGREE = 2
BLOCK_ON_OPPOSITE = True           # a live opposite-direction vote cancels the agreement

# "STRATEGY_KEY:INDEX" combos that never wait for a second strategy (remove to go pure 2-agree)
SOLO_FAST_LANE = {"MULTI_TIMEFRAME:FINNIFTY"}

# Instant "second opinion": live price-momentum counts as one extra agreeing vote.
MOMENTUM_COSIGN = False

SLEEP_START = (11, 0)              # IST, inclusive
SLEEP_END = (14, 0)                # IST, exclusive
SLEEP_MIN_AGREE = 3
SLEEP_MIN_AGREE_IF_TREND = 2       # regime == "trend" (ADX > 22)

SUMMARY_EVERY_MINUTES = 10
LIVE_PRICE_MAX_DEVIATION = 0.15    # ignore a live tick that differs >15% from snapshot price

_candidates: Dict[str, Dict[str, Dict[str, Any]]] = {}   # index -> strategy_key -> vote
_stats = {"allowed": 0, "solo": 0, "no_confluence": 0, "window": 0, "opposite": 0}
_last_summary = datetime.utcnow()


def in_sleep_window(ist: datetime) -> bool:
    return SLEEP_START <= (ist.hour, ist.minute) < SLEEP_END


def _maybe_log_summary(now: datetime):
    global _last_summary
    if (now - _last_summary).total_seconds() < SUMMARY_EVERY_MINUTES * 60:
        return
    logger.info(
        f"🚦 [GATE SUMMARY] last {SUMMARY_EVERY_MINUTES} min → passed={_stats['allowed']} "
        f"(solo={_stats['solo']}), waiting-for-confirmation={_stats['no_confluence']}, "
        f"held-by-sleep-window={_stats['window']}, blocked-opposite={_stats['opposite']}"
    )
    for k in _stats:
        _stats[k] = 0
    _last_summary = now


def evaluate_gate(index_name: str, strategy_key: str, nickname: str, bias: str, regime: str) -> Dict[str, Any]:
    now = datetime.utcnow()
    ist = now + IST_OFFSET
    window = SLEEP_WINDOW_ENABLED and in_sleep_window(ist)
    _maybe_log_summary(now)

    if not CONFLUENCE_ENABLED and not window:
        _stats["allowed"] += 1
        return {"allow": True, "count": 1, "with": [], "names": [nickname], "in_window": False, "need": 1}

    book = _candidates.setdefault(index_name, {})
    cutoff = now - timedelta(minutes=CONFLUENCE_WINDOW_MINUTES)
    for k in [k for k, v in book.items() if v["ts"] < cutoff]:
        del book[k]
    book[strategy_key] = {"bias": bias, "nickname": nickname, "ts": now}   # latest vote wins

    agree = [k for k, v in book.items() if v["bias"] == bias]
    oppose = [k for k, v in book.items() if v["bias"] != bias]
    names = [book[k]["nickname"] for k in agree]
    count = len(agree)

    if MOMENTUM_COSIGN:
        try:
            from app.services.dhan_websocket import get_price_momentum
            if get_price_momentum(index_name).get("bias") == bias:
                count += 1
                names.append("Live Momentum")
        except Exception:
            pass

    solo = f"{strategy_key}:{index_name}" in SOLO_FAST_LANE
    base_need = 1 if (solo or not CONFLUENCE_ENABLED) else MIN_AGREE
    need = base_need
    if window:
        window_need = SLEEP_MIN_AGREE_IF_TREND if regime == "trend" else SLEEP_MIN_AGREE
        if solo and regime == "trend":
            window_need = 1
        need = max(base_need, window_need)

    allow = count >= need
    if allow and BLOCK_ON_OPPOSITE and oppose:
        allow = False
        _stats["opposite"] += 1
    elif not allow:
        _stats["window" if (window and count >= base_need) else "no_confluence"] += 1

    result = {
        "allow": allow, "count": count,
        "with": [k for k in agree if k != strategy_key],
        "names": names, "in_window": window, "need": need,
    }
    if allow:
        for k in agree:                      # consume votes so the same pair doesn't re-fire
            book.pop(k, None)
        _stats["allowed"] += 1
        if solo and count < MIN_AGREE:
            _stats["solo"] += 1
        logger.info(
            f"🚦 [GATE PASS] {index_name} {bias} | {count} agree: {', '.join(names)} "
            f"| solo-lane={solo and count < MIN_AGREE} | sleep-window={window} (needed {need})"
        )
    return result


# ---------------- speed helpers ----------------
def live_option_ltp(index_name: str, security_id: str) -> Optional[float]:
    """Latest WebSocket LTP for an option contract (searches every bucket, so it also
    works if token->index routing hasn't been registered)."""
    from app.services.dhan_websocket import market_data_store
    node = (market_data_store.get(index_name) or {}).get(security_id)
    if isinstance(node, dict) and node.get("ltp", 0) > 0:
        return float(node["ltp"])
    for bucket in market_data_store.values():
        node = bucket.get(security_id) if isinstance(bucket, dict) else None
        if isinstance(node, dict) and node.get("ltp", 0) > 0:
            return float(node["ltp"])
    return None


def prewarm_atm_ticks(index_name: str, spot: float, oc: Dict[str, Any]):
    """Subscribe ATM and the strikes just above/below (CE+PE) so a live price is already
    streaming when a signal needs it, and register token->index routing for them."""
    try:
        from app.core.config import settings
        from app.services.dhan_websocket import (
            register_token_index_mapping, dhan_ws_client, subscribed_security_ids,
        )
        step = settings.INDICES_CONFIG[index_name]["step_size"]
        atm = round(spot / step) * step
        wanted: Dict[str, str] = {}
        for key, node in oc.items():
            try:
                strike = float(key)
            except (ValueError, TypeError):
                continue
            if abs(strike - atm) <= step + 0.1:
                for side in ("ce", "pe"):
                    sid = str((node.get(side) or {}).get("security_id") or "")
                    if sid:
                        wanted[sid] = index_name
        if not wanted:
            return
        register_token_index_mapping(wanted)
        new_ids = [s for s in wanted if s not in subscribed_security_ids]
        if new_ids:
            asyncio.create_task(dhan_ws_client.subscribe_symbols(new_ids))
    except Exception as e:
        logger.warning(f"⚠️ prewarm_atm_ticks skipped: {type(e).__name__}: {e}")
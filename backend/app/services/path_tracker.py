"""
Signal price-path tracker: research data for the RR / SL what-if report.

For every scanner signal it records, from entry until ~15:25 IST (it does NOT stop at T1 or SL),
the time (seconds after entry) at which the option premium first reached each R-level:
  up   : +0.5R +1.0R +1.3R +1.6R +2.0R +2.5R +3.0R      (1R = entry - SL, the signal's own risk)
  down : -0.5R -1.0R -1.5R -2.0R
plus price snapshots at 5/10/15/30/60 minutes, max favourable / adverse excursion and the last price.
With that, ANY (target, stop, time-stop) combination can be replayed on the same signals later.

Hot path (on_tick) is pure in-memory dict work: no DB, no network. A background task flushes to Mongo
(`signal_paths`) only when something happened, plus every 5 min for MFE/MAE. Exception-safe by design.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)

UP_LEVELS = (0.5, 1.0, 1.3, 1.6, 2.0, 2.5, 3.0)
DOWN_LEVELS = (0.5, 1.0, 1.5, 2.0)
SNAPSHOT_MINUTES = (5, 10, 15, 30, 60)

FLUSH_INTERVAL_SECONDS = 10
PERIODIC_FLUSH_SECONDS = 300
FINALIZE_AFTER_IST = (15, 25)
MAX_TRACK_AGE_HOURS = 20
BAD_TICK_LOW, BAD_TICK_HIGH = 0.3, 3.0      # ignore ticks outside 0.3x..3x of entry (garbage prints)
RETENTION_DAYS = 45

_enabled = False
_by_sec: Dict[str, List["_Track"]] = {}
_all: Dict[str, "_Track"] = {}


def _lv(x: float) -> str:
    return str(int(round(x * 10)))      # 1.3 -> "13" (Mongo-safe key, no dots)


class _Track:
    __slots__ = ("signal_id", "index_name", "security_id", "entry", "risk", "t1_r", "t2_r", "t0",
                 "up", "down", "snap", "mfe", "mae", "last", "bad_ticks", "dirty", "moved", "last_flush")

    def __init__(self, signal_id, index_name, security_id, entry, risk, t1_r, t2_r):
        now = datetime.utcnow()
        self.signal_id, self.index_name, self.security_id = signal_id, index_name, security_id
        self.entry, self.risk, self.t1_r, self.t2_r = entry, risk, t1_r, t2_r
        self.t0 = now
        self.up: Dict[str, int] = {}
        self.down: Dict[str, int] = {}
        self.snap: Dict[int, float] = {}
        self.mfe = self.mae = self.last = entry
        self.bad_ticks = 0
        self.dirty = True            # write the initial doc on the first flush
        self.moved = False
        self.last_flush = now


def register_signal(signal_doc: Dict[str, Any]):
    """Called right after a scanner signal is created. No-op if the tracker is not running."""
    if not _enabled:
        return
    try:
        entry = float(signal_doc["entry_price"])
        sl = float(signal_doc["stop_loss"])
        risk = entry - sl
        if entry <= 0 or risk <= 0:
            return
        signal_id = str(signal_doc["_id"])
        if signal_id in _all:
            return
        t1 = float(signal_doc.get("shz_upper") or 0.0)
        t2 = float(signal_doc.get("target2") or 0.0)
        tr = _Track(
            signal_id, signal_doc["index_name"], str(signal_doc["security_id"]), entry, risk,
            round((t1 - entry) / risk, 2) if t1 > 0 else 0.0,
            round((t2 - entry) / risk, 2) if t2 > 0 else 0.0,
        )
        _all[signal_id] = tr
        _by_sec.setdefault(tr.security_id, []).append(tr)
    except Exception as e:
        logger.warning(f"🧭 path register skipped: {type(e).__name__}: {e}")


def on_tick(security_id: str, ltp: float):
    """Called for every option tick. Must stay O(few tracks) and never raise."""
    tracks = _by_sec.get(security_id)
    if not tracks:
        return
    try:
        now = datetime.utcnow()
        for tr in tracks:
            if ltp < tr.entry * BAD_TICK_LOW or ltp > tr.entry * BAD_TICK_HIGH:
                tr.bad_ticks += 1
                continue
            el = (now - tr.t0).total_seconds()
            if el < 0:
                continue
            tr.last = ltp
            if ltp > tr.mfe:
                tr.mfe = ltp
                tr.moved = True
            if ltp < tr.mae:
                tr.mae = ltp
                tr.moved = True
            move = (ltp - tr.entry) / tr.risk
            if move > 0:
                for k in UP_LEVELS:
                    key = _lv(k)
                    if key not in tr.up and move >= k:
                        tr.up[key] = int(el)
                        tr.dirty = True
            elif move < 0:
                for k in DOWN_LEVELS:
                    key = _lv(k)
                    if key not in tr.down and -move >= k:
                        tr.down[key] = int(el)
                        tr.dirty = True
            for m in SNAPSHOT_MINUTES:
                if m not in tr.snap and el >= m * 60:
                    tr.snap[m] = round(ltp, 2)
                    tr.dirty = True
    except Exception:
        pass


def _doc(tr: _Track, complete: bool) -> Dict[str, Any]:
    return {
        "signal_id": tr.signal_id, "index_name": tr.index_name, "security_id": tr.security_id,
        "entry": tr.entry, "risk": round(tr.risk, 4), "t1_r": tr.t1_r, "t2_r": tr.t2_r,
        "up": dict(tr.up), "down": dict(tr.down),
        "snap": {str(k): v for k, v in tr.snap.items()},
        "mfe_r": round((tr.mfe - tr.entry) / tr.risk, 3),
        "mae_r": round((tr.mae - tr.entry) / tr.risk, 3),
        "last": tr.last, "bad_ticks": tr.bad_ticks,
        "complete": complete, "updated_at": datetime.utcnow(),
    }


def _drop(tr: _Track):
    _all.pop(tr.signal_id, None)
    lst = _by_sec.get(tr.security_id)
    if lst and tr in lst:
        lst.remove(tr)
        if not lst:
            _by_sec.pop(tr.security_id, None)


async def _flush_loop():
    from app.core.database import get_database
    try:
        db = await get_database()
        await db.signal_paths.create_index("signal_id", unique=True)
        await db.signal_paths.create_index("created_at", expireAfterSeconds=RETENTION_DAYS * 86400)
    except Exception as e:
        logger.warning(f"🧭 signal_paths index setup skipped: {type(e).__name__}")

    while True:
        try:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            if not _all:
                continue
            db = await get_database()
            now = datetime.utcnow()
            ist = now + IST_OFFSET
            finalize = (ist.hour, ist.minute) >= FINALIZE_AFTER_IST
            for tr in list(_all.values()):
                too_old = (now - tr.t0).total_seconds() > MAX_TRACK_AGE_HOURS * 3600
                if too_old:
                    _drop(tr)       # leaves complete=False in DB; excluded from analysis
                    continue
                due = tr.dirty or (tr.moved and (now - tr.last_flush).total_seconds() >= PERIODIC_FLUSH_SECONDS)
                if not finalize and not due:
                    continue
                await db.signal_paths.update_one(
                    {"signal_id": tr.signal_id},
                    {"$set": _doc(tr, complete=finalize), "$setOnInsert": {"created_at": tr.t0}},
                    upsert=True
                )
                tr.dirty = False
                tr.moved = False
                tr.last_flush = now
                if finalize:
                    _drop(tr)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"🧭 path flush error: {type(e).__name__}: {e}")


def start_path_tracker() -> List[asyncio.Task]:
    global _enabled
    if os.environ.get("PATH_TRACKER_ENABLED", "true").strip().lower() not in ("true", "1", "yes"):
        logger.info("🧭 Path tracker disabled (PATH_TRACKER_ENABLED=false).")
        return []
    _enabled = True
    logger.info("🧭 Path tracker started.")
    return [asyncio.create_task(_flush_loop())]
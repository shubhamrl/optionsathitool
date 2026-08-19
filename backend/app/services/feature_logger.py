"""
Lightweight, storage-safe signal-feature logger for future AI training.

Uses a MongoDB CAPPED collection (fixed max size in bytes) so total storage can
NEVER grow unbounded — once full, MongoDB automatically deletes the oldest records
to make room for new ones. This makes it safe on a 512MB free Atlas tier no matter
how many signals get logged over weeks/months.

Only compact, short-keyed, mostly-numeric fields are stored (no option-chain dumps,
no long text) — each document is roughly 150-250 bytes.
"""
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

FEATURE_COLLECTION = "signal_features"
CAPPED_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB hard cap — self-evicting, will NEVER grow past this
CAPPED_MAX_DOCS = 100000

# Outcome codes stored as small ints (not text) to keep documents tiny
OUTCOME_PENDING = 0
OUTCOME_TARGET_HIT = 1
OUTCOME_SL_HIT = 2
OUTCOME_EXPIRED_PROFIT = 3
OUTCOME_EXPIRED_LOSS = 4

_collection_ready = False
_logging_disabled = False


async def _ensure_collection(db):
    global _collection_ready, _logging_disabled
    if _collection_ready or _logging_disabled:
        return
    try:
        existing = await db.list_collection_names()
        if FEATURE_COLLECTION not in existing:
            await db.create_collection(
                FEATURE_COLLECTION, capped=True, size=CAPPED_SIZE_BYTES, max=CAPPED_MAX_DOCS
            )
            await db[FEATURE_COLLECTION].create_index("sig_id")
            logger.info(f"📦 Created capped collection '{FEATURE_COLLECTION}' (hard-capped at {CAPPED_SIZE_BYTES // (1024*1024)}MB)")
        _collection_ready = True
    except Exception as e:
        logger.warning(f"⚠️ Could not set up feature-logging collection — disabling feature logging (non-critical): {str(e)}")
        _logging_disabled = True


def _momentum_code(bias: Optional[str]) -> int:
    if bias == "CE":
        return 1
    if bias == "PE":
        return -1
    return 0


async def log_signal_features(
    db,
    signal_id: str,
    index_name: str,
    mode: str,          # "standard" or "scalp"
    pcr: float,
    delta: float,
    iv: float,
    score: float,
    selected_type: str,
    momentum_bias: Optional[str] = None,
    orb_triggered: Optional[str] = None,   # "CE", "PE", or None
):
    """Logs a compact feature snapshot at the moment a signal is generated.
    Wrapped in try/except so a logging failure can NEVER break signal generation."""
    if _logging_disabled:
        return
    try:
        await _ensure_collection(db)
        if _logging_disabled:
            return
        now = datetime.utcnow()
        doc = {
            "sig_id": signal_id,
            "t": now,
            "idx": index_name[:2].upper(),
            "mode": 0 if mode == "standard" else 1,
            "dir": 1 if selected_type == "CE" else -1,
            "pcr": round(float(pcr), 2),
            "delta": round(float(delta), 2),
            "iv": round(float(iv), 1),
            "score": round(float(score), 1),
            "mom": _momentum_code(momentum_bias),
            "orb": 1 if orb_triggered == "CE" else (-1 if orb_triggered == "PE" else 0),
            "hm": now.hour * 100 + now.minute,  # e.g. 931 = 9:31 AM, compact time-of-day
            "out": OUTCOME_PENDING,
        }
        await db[FEATURE_COLLECTION].insert_one(doc)
    except Exception as e:
        logger.warning(f"⚠️ Feature logging failed (non-critical, trade unaffected): {str(e)}")


async def update_signal_outcome(db, signal_id: str, outcome_code: int):
    """Updates the outcome of a previously logged signal. Safe no-op if logging is
    disabled or the record doesn't exist — never blocks the trade-closing flow."""
    if _logging_disabled:
        return
    try:
        await _ensure_collection(db)
        if _logging_disabled:
            return
        await db[FEATURE_COLLECTION].update_one(
            {"sig_id": signal_id},
            {"$set": {"out": outcome_code}}
        )
    except Exception as e:
        logger.warning(f"⚠️ Feature outcome update failed (non-critical): {str(e)}")
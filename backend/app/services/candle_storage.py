import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)
COLLECTION = "intraday_candles"

_db_ref = None  # cached db handle, set on first use


async def _get_db():
    global _db_ref
    if _db_ref is None:
        from app.core.database import get_database
        _db_ref = await get_database()
    return _db_ref


async def persist_finalized_candle(index_name: str, candle: Dict[str, Any]):
    """
    Called once per index, once per minute (when dhan_websocket.py finalizes a
    1-minute candle) — appends it to today's persistent record in MongoDB. Wrapped
    in try/except so a storage hiccup never breaks live tick processing.
    """
    try:
        db = await _get_db()
        ist_now = datetime.utcnow() + IST_OFFSET
        date_key = ist_now.strftime("%Y-%m-%d")

        compact = {
            "m": candle["minute"],  # "HH:MM"
            "o": round(candle["open"], 2),
            "h": round(candle["high"], 2),
            "l": round(candle["low"], 2),
            "c": round(candle["close"], 2),
        }

        await db[COLLECTION].update_one(
            {"index_name": index_name, "date": date_key},
            {"$push": {"candles": compact}, "$set": {"updated_at": datetime.utcnow()}},
            upsert=True
        )
    except Exception as e:
        logger.warning(f"⚠️ Candle persist failed for {index_name} (non-critical): {str(e)}")


async def get_today_candles(index_name: str) -> List[Dict[str, Any]]:
    """Returns today's full list of 1-minute candles for an index, oldest first.
    Used by strategies that need more history than the small in-memory rolling
    window (e.g. VWAP, Bollinger Bands need the whole session)."""
    try:
        db = await _get_db()
        ist_now = datetime.utcnow() + IST_OFFSET
        date_key = ist_now.strftime("%Y-%m-%d")

        doc = await db[COLLECTION].find_one({"index_name": index_name, "date": date_key})
        if not doc:
            return []
        return doc.get("candles", [])
    except Exception as e:
        logger.warning(f"⚠️ Candle fetch failed for {index_name} (non-critical): {str(e)}")
        return []


PREV_CLOSE_COLLECTION = "prev_day_close"


async def snapshot_previous_close(db):
    """
    Called once at EOD (before/around the 3:15 PM square-off) — saves today's
    final candle close per index into a small, NEVER-auto-deleted collection, so
    tomorrow's Gap-Fill strategy has something to compare against. Only 4 tiny
    documents total, negligible storage.
    """
    ist_now = datetime.utcnow() + IST_OFFSET
    today_key = ist_now.strftime("%Y-%m-%d")

    for index_name in ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY"):
        doc = await db[COLLECTION].find_one({"index_name": index_name, "date": today_key})
        candles = doc.get("candles", []) if doc else []
        if not candles:
            continue
        last_close = candles[-1]["c"]
        await db[PREV_CLOSE_COLLECTION].update_one(
            {"index_name": index_name},
            {"$set": {"close": last_close, "date": today_key, "updated_at": datetime.utcnow()}},
            upsert=True
        )


async def get_previous_close(index_name: str) -> Optional[float]:
    """Returns the last saved end-of-day close for an index, or None if not
    yet captured (e.g. very first day this feature has been running)."""
    try:
        db = await _get_db()
        doc = await db[PREV_CLOSE_COLLECTION].find_one({"index_name": index_name})
        return doc.get("close") if doc else None
    except Exception as e:
        logger.warning(f"⚠️ Previous-close fetch failed for {index_name} (non-critical): {str(e)}")
        return None


RETAIN_DAYS = 5  # Kept multi-day (not just today) so 15-min EMA-50 (needs ~750 min
# = ~2 trading days of history) has enough data. Storage impact is negligible
# (a few thousand small candle records total).


async def cleanup_old_candles(db):
    """Deletes candle records older than RETAIN_DAYS — called by the EOD scheduler."""
    ist_now = datetime.utcnow() + IST_OFFSET
    cutoff_date = (ist_now - timedelta(days=RETAIN_DAYS - 1)).strftime("%Y-%m-%d")
    result = await db[COLLECTION].delete_many({"date": {"$lt": cutoff_date}})
    if result.deleted_count:
        logger.info(f"🧹 Cleaned up {result.deleted_count} old intraday_candles document(s) (kept last {RETAIN_DAYS} days).")


async def get_recent_days_candles(index_name: str, days: int = RETAIN_DAYS) -> List[List[Dict[str, Any]]]:
    """Returns a list of day-wise 1-minute candle lists (oldest day first, today
    last), for strategies (like EMA breakout) that need multi-day history that
    a single trading day can't provide."""
    try:
        db = await _get_db()
        cursor = db[COLLECTION].find({"index_name": index_name}).sort("date", -1).limit(days)
        docs = await cursor.to_list(length=days)
        docs.reverse()  # oldest first
        return [d.get("candles", []) for d in docs if d.get("candles")]
    except Exception as e:
        logger.warning(f"⚠️ Multi-day candle fetch failed for {index_name} (non-critical): {str(e)}")
        return []
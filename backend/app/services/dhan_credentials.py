import logging
from datetime import datetime
from typing import Tuple, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

CREDENTIALS_DOC_ID = "dhan_credentials"
CACHE_TTL_SECONDS = 30

_cached_client_id: Optional[str] = None
_cached_access_token: Optional[str] = None
_cache_loaded_at: Optional[datetime] = None


async def get_dhan_credentials() -> Tuple[str, str]:
    """
    Returns (client_id, access_token) — read from MongoDB (app_settings
    collection) once set via update_dhan_credentials(), with a short in-memory
    cache so this doesn't hit the DB on every single API call. Falls back to
    the DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN environment variables if nothing
    has been saved in the DB yet, so nothing breaks on first deploy.
    """
    global _cached_client_id, _cached_access_token, _cache_loaded_at

    now = datetime.utcnow()
    if _cache_loaded_at and (now - _cache_loaded_at).total_seconds() < CACHE_TTL_SECONDS:
        if _cached_access_token:
            return _cached_client_id, _cached_access_token

    try:
        from app.core.database import get_database
        db = await get_database()
        doc = await db.app_settings.find_one({"_id": CREDENTIALS_DOC_ID})
        if doc and doc.get("access_token"):
            _cached_client_id = doc.get("client_id") or settings.DHAN_CLIENT_ID
            _cached_access_token = doc["access_token"]
            _cache_loaded_at = now
            return _cached_client_id, _cached_access_token
    except Exception as e:
        logger.warning(f"⚠️ Could not read Dhan credentials from DB, falling back to env vars: {str(e)}")

    _cached_client_id = settings.DHAN_CLIENT_ID
    _cached_access_token = settings.DHAN_ACCESS_TOKEN
    _cache_loaded_at = now
    return _cached_client_id, _cached_access_token


async def update_dhan_credentials(client_id: str, access_token: str):
    """Saves a fresh token/client_id to MongoDB and updates the in-memory cache
    immediately — the very next API call or WebSocket reconnect attempt uses
    it. No redeploy needed."""
    global _cached_client_id, _cached_access_token, _cache_loaded_at

    from app.core.database import get_database
    db = await get_database()
    await db.app_settings.update_one(
        {"_id": CREDENTIALS_DOC_ID},
        {"$set": {
            "client_id": client_id.strip(),
            "access_token": access_token.strip(),
            "updated_at": datetime.utcnow()
        }},
        upsert=True
    )

    _cached_client_id = client_id.strip()
    _cached_access_token = access_token.strip()
    _cache_loaded_at = datetime.utcnow()
    logger.info("🔑 Dhan credentials updated in DB — active immediately, no redeploy needed.")
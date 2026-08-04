import logging
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


class Database:
    client: Optional[AsyncIOMotorClient] = None
    db: Optional[AsyncIOMotorDatabase] = None


db_instance = Database()


async def connect_to_mongo():
    """
    Connects to MongoDB using Async Motor Driver on FastAPI Startup
    """
    logger.info("🍃 Connecting to MongoDB Database...")
    db_instance.client = AsyncIOMotorClient(
        settings.MONGODB_URL,
        maxPoolSize=50,
        minPoolSize=10,
        serverSelectionTimeoutMS=5000
    )
    
    # Extract database name from connection URL or default to 'optionsaathi'
    db_name = settings.MONGODB_URL.split("/")[-1].split("?")[0] or "optionsaathi"
    db_instance.db = db_instance.client[db_name]
    logger.info(f"✅ Connected to MongoDB Database: '{db_name}' successfully!")


async def close_mongo_connection():
    """
    Closes MongoDB Connection Pool on FastAPI Shutdown
    """
    logger.info("🍃 Closing MongoDB Database Connection...")
    if db_instance.client:
        db_instance.client.close()
        logger.info("✅ MongoDB Database Connection Closed.")


async def get_database() -> AsyncIOMotorDatabase:
    """
    FastAPI Dependency Injector for MongoDB Instance
    """
    if db_instance.db is None:
        raise RuntimeError("Database connection is not initialized.")
    return db_instance.db
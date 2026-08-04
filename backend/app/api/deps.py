import logging
from typing import Dict, Any, Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from bson import ObjectId

from app.core.config import settings
from app.core.database import get_database

logger = logging.getLogger(__name__)

# OAuth2 Scheme for Bearer Token Extraction from HTTP Headers
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db = Depends(get_database)
) -> Dict[str, Any]:
    """
    FastAPI Security Dependency:
    1. Extracts JWT Bearer Token from HTTP Request Header
    2. Decodes & Verifies Secret Key
    3. Fetches User Document from MongoDB
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials or token expired",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        # Decode JWT Token Payload
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=["HS256"]
        )
        
        user_id: Optional[str] = payload.get("sub") or payload.get("id") or payload.get("userId")
        if user_id is None:
            raise credentials_exception

    except JWTError as e:
        logger.error(f"JWT Verification Failed: {str(e)}")
        raise credentials_exception

    # Query User from MongoDB
    try:
        # Convert string ID to ObjectId if valid
        query_id = ObjectId(user_id) if ObjectId.is_valid(user_id) else user_id
        user = await db.users.find_one({"_id": query_id})
        
        if user is None:
            # Fallback search by string id
            user = await db.users.find_one({"id": str(user_id)})
            if user is None:
                raise credentials_exception

        # Standardize _id to string for application-wide use
        user["_id"] = str(user["_id"])
        return user

    except Exception as e:
        logger.error(f"Error retrieving authenticated user: {str(e)}")
        raise credentials_exception
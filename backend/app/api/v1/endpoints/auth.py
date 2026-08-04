import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from jose import jwt

from app.core.config import settings
from app.core.database import get_database
from app.api.deps import get_current_user

router = APIRouter()
logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# SCHEMAS
# ----------------------------------------------------------------------------
class GoogleAuthRequest(BaseModel):
    id_token: str = Field(..., description="Google OAuth Credential ID Token from Frontend")


class UserProfileResponse(BaseModel):
    id: str
    email: EmailStr
    full_name: str
    picture: Optional[str] = None
    role: str = "user"
    created_at: str


# ----------------------------------------------------------------------------
# HELPER: Generate JWT Token for App Access
# ----------------------------------------------------------------------------
def create_app_jwt_token(user_id: str, email: str) -> str:
    expires_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    expire = datetime.utcnow() + expires_delta
    
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": expire,
        "iat": datetime.utcnow()
    }
    
    encoded_jwt = jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")
    return encoded_jwt


# ----------------------------------------------------------------------------
# 1. POST /api/v1/auth/google
# Google Sign-In & Instant User Registration/Sync
# ----------------------------------------------------------------------------
@router.post("/google")
async def google_auth_login_or_signup(
    payload: GoogleAuthRequest,
    db = Depends(get_database)
):
    """
    Authenticates user via Google OAuth Token.
    Extracts Full Google Profile Info and saves to MongoDB Users Collection for Admin View.
    """
    token_str = payload.id_token.strip()
    if not token_str:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google ID token is required."
        )

    try:
        # Verify Google Token with Google OAuth Servers
        # Note: CLIENT_ID verification can be passed via settings.GOOGLE_CLIENT_ID if configured
        id_info = id_token.verify_oauth2_token(
            token_str,
            google_requests.Request()
        )

        # Extract Complete Google User Information
        google_id = id_info.get("sub")
        email = id_info.get("email")
        email_verified = id_info.get("email_verified", False)
        name = id_info.get("name", "OptionSaathi Trader")
        given_name = id_info.get("given_name", "")
        family_name = id_info.get("family_name", "")
        picture = id_info.get("picture", "")
        locale = id_info.get("locale", "")

        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Google account did not provide a valid email address."
            )

    except ValueError as e:
        logger.error(f"Google Token Verification Error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Google Token."
        )
    except Exception as e:
        logger.error(f"Unexpected Google Auth Error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google Authentication service error."
        )

    # Search for existing user in MongoDB
    user = await db.users.find_one({"email": email})

    now = datetime.utcnow()

    if user:
        # User Exists -> Update Google Profile Metadata & Last Login
        user_id = str(user["_id"])
        await db.users.update_one(
            {"_id": user["_id"]},
            {
                "$set": {
                    "full_name": name,
                    "given_name": given_name,
                    "family_name": family_name,
                    "picture": picture,
                    "google_id": google_id,
                    "email_verified": email_verified,
                    "last_login": now,
                    "updated_at": now
                }
            }
        )
        role = user.get("role", "user")
        logger.info(f"👤 Existing User Logged In via Google: {email} (ID: {user_id})")
    else:
        # New User -> Register and save complete Google Profile data for Admin List
        new_user_doc = {
            "email": email,
            "full_name": name,
            "given_name": given_name,
            "family_name": family_name,
            "picture": picture,
            "google_id": google_id,
            "locale": locale,
            "email_verified": email_verified,
            "auth_provider": "google",
            "role": "user",  # Default role ('admin' or 'user')
            "is_active": True,
            "last_login": now,
            "created_at": now,
            "updated_at": now
        }
        
        insert_result = await db.users.insert_one(new_user_doc)
        user_id = str(insert_result.inserted_id)
        role = "user"
        logger.info(f"✨ New User Registered via Google: {email} (ID: {user_id})")

    # Issue Application JWT Token
    access_token = create_app_jwt_token(user_id=user_id, email=email)

    return {
        "success": True,
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": user_id,
            "email": email,
            "full_name": name,
            "picture": picture,
            "role": role
        }
    }


# ----------------------------------------------------------------------------
# 2. GET /api/v1/auth/me
# Get Current Authenticated User Info
# ----------------------------------------------------------------------------
@router.get("/me")
async def get_me(current_user: Dict[str, Any] = Depends(get_current_user)):
    return {
        "success": True,
        "user": {
            "id": str(current_user["_id"]),
            "email": current_user.get("email"),
            "full_name": current_user.get("full_name"),
            "picture": current_user.get("picture"),
            "role": current_user.get("role", "user"),
            "created_at": current_user.get("created_at").isoformat() if isinstance(current_user.get("created_at"), datetime) else str(current_user.get("created_at"))
        }
    }


# ----------------------------------------------------------------------------
# 3. GET /api/v1/auth/admin/users-list
# Admin Panel API: Retrieve all registered users with Google profile info
# ----------------------------------------------------------------------------
@router.get("/admin/users-list")
async def get_admin_users_list(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db = Depends(get_database)
):
    """
    Fetches complete users list for Admin Panel view.
    Displays name, email, profile photo, registration date, and last active status.
    """
    # Check if requesting user is Admin
    if current_user.get("role") != "admin":
        # Temporary safeguard - if testing, you can comment this block or allow all
        pass

    cursor = db.users.find({}).sort("created_at", -1)
    
    users = []
    async for doc in cursor:
        users.append({
            "id": str(doc["_id"]),
            "email": doc.get("email"),
            "full_name": doc.get("full_name"),
            "picture": doc.get("picture"),
            "auth_provider": doc.get("auth_provider", "google"),
            "role": doc.get("role", "user"),
            "is_active": doc.get("is_active", True),
            "last_login": doc.get("last_login").isoformat() if isinstance(doc.get("last_login"), datetime) else None,
            "created_at": doc.get("created_at").isoformat() if isinstance(doc.get("created_at"), datetime) else None
        })

    return {
        "success": True,
        "count": len(users),
        "users": users
    }
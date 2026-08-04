import os
from typing import Dict, Any
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App General Config
    PROJECT_NAME: str = "OptionSaathi AI Confluence Engine"
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str = os.getenv("SECRET_KEY", "YOUR_SUPER_SECRET_JWT_KEY_CHANGE_IN_PRODUCTION_32_BYTES")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 Days

    # Database Settings
    MONGODB_URL: str = os.getenv("MONGODB_URL", "mongodb://localhost:27017/optionsaathi")

    # Dhan API Credentials
    DHAN_CLIENT_ID: str = os.getenv("DHAN_CLIENT_ID", "")
    DHAN_ACCESS_TOKEN: str = os.getenv("DHAN_ACCESS_TOKEN", "")
    DHAN_BASE_URL: str = "https://api.dhan.co/v2"
    DHAN_WS_URL: str = "wss://api-feed.dhan.co"

    # 🟢 ADD THIS LINE: Google Auth Client ID
    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")

    # Multi-Index Specifications
    INDICES_CONFIG: Dict[str, Dict[str, Any]] = {
        "NIFTY": {
            "scrip_id": 13,
            "underlying_seg": "IDX_I",
            "fno_seg": "NSE_FNO",
            "step_size": 50,
            "strike_range": 10,
            "default_lot_size": 25,
            "max_sl_points": 22.0,
            "min_sl_points": 8.0,
        },
        "BANKNIFTY": {
            "scrip_id": 25,
            "underlying_seg": "IDX_I",
            "fno_seg": "NSE_FNO",
            "step_size": 100,
            "strike_range": 10,
            "default_lot_size": 15,
            "max_sl_points": 45.0,
            "min_sl_points": 18.0,
        },
        "SENSEX": {
            "scrip_id": 51,
            "underlying_seg": "IDX_I",
            "fno_seg": "BSE_FNO",
            "step_size": 100,
            "strike_range": 10,
            "default_lot_size": 10,
            "max_sl_points": 60.0,
            "min_sl_points": 25.0,
        },
        "FINNIFTY": {
            "scrip_id": 27,
            "underlying_seg": "IDX_I",
            "fno_seg": "NSE_FNO",
            "step_size": 50,
            "strike_range": 8,
            "default_lot_size": 40,
            "max_sl_points": 20.0,
            "min_sl_points": 8.0,
        },
    }

    class Config:
        case_sensitive = True
        env_file = ".env"
        # 🟢 Optional: allow extra fields from env so it never crashes if new env vars are added
        extra = "ignore" 


# Global Settings Instance
settings = Settings()
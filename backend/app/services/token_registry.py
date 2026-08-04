import httpx
import logging
from typing import Dict, Any, List
from app.core.config import settings

logger = logging.getLogger(__name__)

DHAN_BASE_URL = "https://api.dhan.co/v2"

def get_dhan_headers() -> Dict[str, str]:
    return {
        "access-token": settings.DHAN_ACCESS_TOKEN,
        "client-id": settings.DHAN_CLIENT_ID,
        "Content-Type": "application/json"
    }

async def fetch_expiry_list(scrip_id: int, segment: str = "IDX_I") -> List[str]:
    """
    Fetch active weekly expiries for Nifty, BankNifty, Sensex
    """
    url = f"{DHAN_BASE_URL}/optionchain/expirylist"
    payload = {"UnderlyingScrip": scrip_id, "UnderlyingSeg": segment}
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(url, json=payload, headers=get_dhan_headers())
            data = response.json()
            expiries = data.get("data", [])
            if expiries:
                expiries.sort()
                return expiries
            return []
        except Exception as e:
            logger.error(f"Error fetching expiry list for Scrip {scrip_id}: {str(e)}")
            return []

async def fetch_full_option_chain_data(scrip_id: int, segment: str, expiry: str) -> Dict[str, Any]:
    """
    Fetches Full Option Chain Raw Data (Spot, Strikes, OI, IV, Option Prices)
    Central Backend Call - ZERO User Side Leakage!
    """
    url = f"{DHAN_BASE_URL}/optionchain"
    payload = {
        "UnderlyingScrip": scrip_id,
        "UnderlyingSeg": segment,
        "Expiry": expiry
    }
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(url, json=payload, headers=get_dhan_headers())
            if response.status_code == 200:
                res_data = response.json()
                return res_data.get("data", {})
            elif response.status_code == 429:
                logger.error("⚠️ 429 Rate Limit hit on Dhan Option Chain fetch!")
                return {}
            return {}
        except Exception as e:
            logger.error(f"Error fetching option chain: {str(e)}")
            return {}
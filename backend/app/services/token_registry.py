import httpx
import logging
from typing import Dict, Any, List
from app.core.config import settings

logger = logging.getLogger(__name__)

DHAN_BASE_URL = "https://api.dhan.co/v2"


async def get_dhan_headers() -> Dict[str, str]:
    from app.services.dhan_credentials import get_dhan_credentials
    client_id, access_token = await get_dhan_credentials()
    return {
        "access-token": access_token,
        "client-id": client_id,
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
            headers = await get_dhan_headers()
            response = await client.post(url, json=payload, headers=headers)

            if response.status_code == 401:
                logger.error(
                    f"🔴 Dhan REST API returned 401 Unauthorized for Scrip {scrip_id} — "
                    f"the current token is invalid/expired. Response: {response.text[:300]}"
                )
                return []

            if response.status_code != 200:
                logger.error(
                    f"Dhan expiry-list request failed for Scrip {scrip_id}: "
                    f"HTTP {response.status_code} — {response.text[:300]}"
                )
                return []

            data = response.json()
            expiries = data.get("data", [])

            if not isinstance(expiries, list):
                logger.error(
                    f"Unexpected Dhan expiry-list response shape for Scrip {scrip_id}: {data}"
                )
                return []

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
            headers = await get_dhan_headers()
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code == 200:
                res_data = response.json()
                return res_data.get("data", {})
            elif response.status_code == 429:
                logger.error("⚠️ 429 Rate Limit hit on Dhan Option Chain fetch!")
                return {}
            elif response.status_code == 401:
                logger.error(f"🔴 Dhan REST API 401 Unauthorized on option-chain fetch: {response.text[:300]}")
                return {}
            return {}
        except Exception as e:
            logger.error(f"Error fetching option chain: {str(e)}")
            return {}
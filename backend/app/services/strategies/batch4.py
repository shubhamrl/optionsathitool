"""
Batch 4 — ORB Breaker (migrated from the old standalone Global Market Scanner)

Same logic as before: Opening-Range Breakout confirmed by PCR + real price
momentum via AISurveillanceEngine. Now just one more entry in the shared
strategy registry — no special treatment, same generic execution pipeline as
every other strategy.
"""
import logging
from typing import Dict, Any, Optional

from app.core.config import settings
from app.services.strategy_engine import register_strategy
from app.services.dhan_websocket import get_orb_levels, get_price_momentum
from app.engine.ai_surveillance import AISurveillanceEngine

logger = logging.getLogger(__name__)


async def detect_orb_breaker(index_name: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    spot = context["spot"]
    oc = context["oc"]

    idx_config = settings.INDICES_CONFIG.get(index_name, settings.INDICES_CONFIG["NIFTY"])
    step = idx_config["step_size"]
    atm_strike = int(round(spot / step) * step)

    orb_levels = get_orb_levels(index_name)
    price_momentum = get_price_momentum(index_name)

    ai_engine = AISurveillanceEngine(index_name)
    ai_result = ai_engine.evaluate_market_state(
        spot=spot, atm_strike=atm_strike, oc_data=oc, spot_history=[spot],
        orb_high=orb_levels["orb_high"], orb_low=orb_levels["orb_low"],
        price_momentum=price_momentum
    )

    if ai_result["signal"] == "NO TRADE" or not ai_result["selected_type"]:
        return None

    return {"bias": ai_result["selected_type"], "reasons": ai_result["reasons"]}


register_strategy("ORB_BREAKER", "ORB Breaker", detect_orb_breaker)
import logging
from typing import Dict, Any, Optional
from app.core.config import settings

logger = logging.getLogger(__name__)


def clamp(val: float, min_val: float, max_val: float) -> float:
    """Clamps a numeric value between min_val and max_val."""
    return max(min(val, max_val), min_val)


class DynamicRiskManager:
    """
    Precision Risk-Reward Engine.
    Computes Option Premium Risk Points based on Black-Scholes Delta, ATR, and Index Volatility bounds.
    """

    @staticmethod
    def calculate_trade_targets(
        index_name: str,
        entry_premium: float,
        spot_atr: float,
        delta: float,
        is_forced_scalp: bool = False
    ) -> Dict[str, Any]:
        """
        Calculates StopLoss, Target 1 (1:1.6 RR), and Target 2 (1:2.8 RR) for an option contract.
        
        :param index_name: NIFTY, BANKNIFTY, SENSEX, FINNIFTY
        :param entry_premium: Real LTP option entry price
        :param spot_atr: Spot Index ATR / Volatility points
        :param delta: Calculated Option Delta (0.1 to 0.9)
        :param is_forced_scalp: If true, applies tight scalp risk parameters
        :return: Dict containing entry, stop_loss, target1, target2, risk_points, and risk_reward_ratio
        """
        if entry_premium <= 0:
            return {
                "entry_price": 0.0,
                "stop_loss": 0.0,
                "target1": 0.0,
                "target2": 0.0,
                "risk_points": 0.0,
                "is_valid": False
            }

        idx_config = settings.INDICES_CONFIG.get(index_name.upper(), settings.INDICES_CONFIG["NIFTY"])
        min_sl = idx_config.get("min_sl_points", 8.0)
        max_sl = idx_config.get("max_sl_points", 22.0)

        # Delta & ATR risk math adjustment
        abs_delta = abs(delta) if delta else 0.5
        effective_atr = max(spot_atr, 8.0)
        spot_risk_points = effective_atr * 0.8

        if is_forced_scalp:
            # Scalp Mode: Tight SL bounds for quick entries
            premium_risk_points = clamp(8.0 * abs_delta + 2.0, min_sl, max_sl * 0.7)
            t1_multiplier = 1.5
            t2_multiplier = 2.5
        else:
            # Standard Confluence Signal (6.0+ Score): Optimal RR
            premium_risk_points = clamp(spot_risk_points * abs_delta + 2.0, min_sl, max_sl)
            t1_multiplier = 1.6
            t2_multiplier = 2.8

        premium_risk_points = round(premium_risk_points, 1)

        stop_loss = round(entry_premium - premium_risk_points, 1)
        # Ensure SL doesn't go below zero or negative
        stop_loss = max(1.0, stop_loss)

        target1 = round(entry_premium + (premium_risk_points * t1_multiplier), 1)
        target2 = round(entry_premium + (premium_risk_points * t2_multiplier), 1)

        return {
            "entry_price": round(entry_premium, 1),
            "stop_loss": stop_loss,
            "target1": target1,
            "target2": target2,
            "risk_points": premium_risk_points,
            "reward_points_t1": round(target1 - entry_premium, 1),
            "reward_points_t2": round(target2 - entry_premium, 1),
            "risk_reward_ratio": f"1:{t1_multiplier}",
            "is_valid": True
        }


# Singleton Risk Manager
risk_manager = DynamicRiskManager()
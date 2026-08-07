import logging
from typing import Dict, Any, Optional, Tuple
from app.engine.confluence_math import calculate_option_greeks, calculate_pcr_and_sentiment

logger = logging.getLogger(__name__)


def get_oi_support_resistance(oc_data: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """
    Scans the full option chain and finds:
      - resistance_strike: strike with the highest CE Open Interest (call writers' wall)
      - support_strike: strike with the highest PE Open Interest (put writers' wall)
    These act as the nearest 'wait for breakout' levels when the AI has no active trigger.
    """
    max_ce_oi = 0.0
    resistance_strike: Optional[float] = None
    max_pe_oi = 0.0
    support_strike: Optional[float] = None

    for key, node in oc_data.items():
        try:
            strike = float(key)
        except (TypeError, ValueError):
            continue

        ce_node = node.get("ce") or {}
        pe_node = node.get("pe") or {}
        ce_oi = float(ce_node.get("oi") or 0)
        pe_oi = float(pe_node.get("oi") or 0)

        if ce_oi > max_ce_oi:
            max_ce_oi = ce_oi
            resistance_strike = strike

        if pe_oi > max_pe_oi:
            max_pe_oi = pe_oi
            support_strike = strike

    return resistance_strike, support_strike


class AISurveillanceEngine:
    def __init__(self, index_name: str):
        self.index_name = index_name
        self.min_ai_confidence_score = 6.0

    def evaluate_market_state(
        self,
        spot: float,
        atm_strike: float,
        oc_data: Dict[str, Any],
        spot_history: list,
        orb_high: float,
        orb_low: float
    ) -> Dict[str, Any]:
        """
        Internal AI Brain: Scans IV, Greeks, PCR, OI, Range Breakouts in real-time
        """
        pcr, pcr_sentiment = calculate_pcr_and_sentiment(oc_data)

        ce_score = 0.0
        pe_score = 0.0
        ai_reasons = []

        # 1. ORB Breakout Check
        primary_ce_trigger = False
        primary_pe_trigger = False

        if orb_high > 0 and spot > orb_high:
            primary_ce_trigger = True
            ce_score += 3.0
            ai_reasons.append("AI_DETECTED: Opening Range High Upper Breakout")
        elif orb_low > 0 and spot < orb_low:
            primary_pe_trigger = True
            pe_score += 3.0
            ai_reasons.append("AI_DETECTED: Opening Range Low Lower Breakdown")

        # 2. PCR Buildup Analysis
        if pcr > 1.10:
            ce_score += 2.0
            ai_reasons.append(f"AI_PCR_CONFIRMATION: Bullish Put Writing Buildup (PCR {pcr})")
        elif pcr < 0.90:
            pe_score += 2.0
            ai_reasons.append(f"AI_PCR_CONFIRMATION: Bearish Call Writing Buildup (PCR {pcr})")

        # 3. Decision Matrix
        signal = "NO TRADE"
        selected_type = None
        final_score = 0.0

        if primary_ce_trigger and ce_score >= self.min_ai_confidence_score and ce_score > pe_score:
            signal = "BUY CALL"
            selected_type = "CE"
            final_score = ce_score
        elif primary_pe_trigger and pe_score >= self.min_ai_confidence_score and pe_score > ce_score:
            signal = "BUY PUT"
            selected_type = "PE"
            final_score = pe_score

        # 4. OI-based Support/Resistance — used as "wait for these levels" guidance
        resistance_strike, support_strike = get_oi_support_resistance(oc_data)
        level_buffer = round(max(spot * 0.001, 5.0), 1)  # min 5-point buffer, scales with spot

        upper_trigger = round(resistance_strike + level_buffer, 1) if resistance_strike else round(spot + 50, 1)
        lower_trigger = round(support_strike - level_buffer, 1) if support_strike else round(spot - 50, 1)

        wait_levels = {
            "upper_trigger": upper_trigger,
            "lower_trigger": lower_trigger,
            "resistance_strike": resistance_strike,
            "support_strike": support_strike,
            "message": (
                f"Wait for {self.index_name} Spot > {upper_trigger} for BUY CALL, "
                f"or < {lower_trigger} for BUY PUT."
            )
        }

        # 5. Detailed NO TRADE reasoning (only meaningful when no signal fired)
        no_trade_reasons = []
        if signal == "NO TRADE":
            if 0.90 <= pcr <= 1.10:
                no_trade_reasons.append(
                    f"Market is range-bound — PCR {pcr} shows no strong Call/Put writing bias yet."
                )
            else:
                no_trade_reasons.append(
                    f"PCR bias present ({pcr}, {pcr_sentiment}) but price hasn't confirmed a breakout trigger yet."
                )

            if resistance_strike and support_strike:
                no_trade_reasons.append(
                    f"OI Wall Check: Resistance building near {resistance_strike} (Max CE OI), "
                    f"Support building near {support_strike} (Max PE OI) — spot is trapped between these levels."
                )

            if not primary_ce_trigger and not primary_pe_trigger:
                no_trade_reasons.append(
                    "Low volatility / no Opening-Range breakout detected — AI is avoiding a blind entry."
                )

            no_trade_reasons.append(
                f"AI Confidence Score {round(max(ce_score, pe_score), 1)} is below the "
                f"minimum threshold of {self.min_ai_confidence_score} required to trigger a trade."
            )

        combined_reasons = ai_reasons + no_trade_reasons if signal == "NO TRADE" else ai_reasons

        return {
            "index": self.index_name,
            "spot": spot,
            "atm_strike": atm_strike,
            "pcr": pcr,
            "sentiment": pcr_sentiment,
            "signal": signal,
            "selected_type": selected_type,
            "ai_score": round(final_score, 1),
            "reasons": combined_reasons,
            "wait_levels": wait_levels
        }
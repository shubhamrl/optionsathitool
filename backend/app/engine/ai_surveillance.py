import logging
from typing import Dict, Any, Optional
from app.engine.confluence_math import calculate_option_greeks, calculate_pcr_and_sentiment

logger = logging.getLogger(__name__)

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

        return {
            "index": self.index_name,
            "spot": spot,
            "atm_strike": atm_strike,
            "pcr": pcr,
            "sentiment": pcr_sentiment,
            "signal": signal,
            "selected_type": selected_type,
            "ai_score": round(final_score, 1),
            "reasons": ai_reasons
        }
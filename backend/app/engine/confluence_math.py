import math
from typing import Dict, Any, Tuple

# Black-Scholes Cumulative Standard Normal Distribution
def norm_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def calculate_option_greeks(
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    iv_percent: float,
    risk_free_rate: float = 0.07,
    option_type: str = "CE"
) -> Dict[str, float]:
    """
    Calculates Real-Time Option Greeks: Delta, Gamma, Theta, Vega
    """
    if spot <= 0 or strike <= 0 or iv_percent <= 0:
        return {"delta": 0.5 if option_type == "CE" else -0.5, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    # Minimum time buffer (avoid division by zero on expiry date)
    t = max(time_to_expiry_years, 0.001)
    sigma = iv_percent / 100.0
    r = risk_free_rate

    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t) / (sigma * math.sqrt(t))
        d2 = d1 - sigma * math.sqrt(t)

        if option_type == "CE":
            delta = norm_cdf(d1)
            theta = (- (spot * norm_pdf(d1) * sigma) / (2 * math.sqrt(t)) 
                     - r * strike * math.exp(-r * t) * norm_cdf(d2)) / 365.0
        else:
            delta = norm_cdf(d1) - 1.0
            theta = (- (spot * norm_pdf(d1) * sigma) / (2 * math.sqrt(t)) 
                     + r * strike * math.exp(-r * t) * norm_cdf(-d2)) / 365.0

        gamma = norm_pdf(d1) / (spot * sigma * math.sqrt(t))
        vega = (spot * math.sqrt(t) * norm_pdf(d1)) / 100.0

        return {
            "delta": round(delta, 3),
            "gamma": round(gamma, 5),
            "theta": round(theta, 2),
            "vega": round(vega, 2)
        }
    except Exception:
        return {"delta": 0.5 if option_type == "CE" else -0.5, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

def calculate_pcr_and_sentiment(oc_data: Dict[str, Any]) -> Tuple[float, str]:
    """
    Extracts Total CE vs PE Open Interest and calculates Global PCR
    """
    total_ce_oi = 0
    total_pe_oi = 0

    for strike_key, node in oc_data.items():
        ce_node = node.get("ce", {})
        pe_node = node.get("pe", {})
        
        total_ce_oi += int(ce_node.get("oi", 0) or 0)
        total_pe_oi += int(pe_node.get("oi", 0) or 0)

    if total_ce_oi == 0:
        return 1.0, "NEUTRAL"

    pcr = round(total_pe_oi / total_ce_oi, 2)
    
    if pcr > 1.25:
        sentiment = "STRONG BULLISH"
    elif pcr > 1.05:
        sentiment = "MILD BULLISH"
    elif pcr < 0.75:
        sentiment = "STRONG BEARISH"
    elif pcr < 0.95:
        sentiment = "MILD BEARISH"
    else:
        sentiment = "SIDEWAYS CHOP"

    return pcr, sentiment
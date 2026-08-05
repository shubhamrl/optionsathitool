from datetime import datetime
from typing import Optional, Dict, Any

# Updated Official Lot Sizes
LOT_SIZES = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "FINNIFTY": 60,
    "MIDCPNIFTY": 120,
    "SENSEX": 20
}

def calculate_indian_option_charges(
    buy_price: float,
    sell_price: float,
    quantity: int
) -> Dict[str, float]:
    """
    Calculates exact Indian Brokerage and Taxes for F&O Options Trading.
    """
    buy_turnover = buy_price * quantity
    sell_turnover = sell_price * quantity
    total_turnover = buy_turnover + sell_turnover

    # 1. Brokerage: Flat ₹20 per executed order (Buy + Sell = ₹40)
    brokerage = 40.0

    # 2. STT/CTT: 0.125% on Sell side premium turnover
    stt = sell_turnover * 0.00125

    # 3. Exchange Turnover Charge: ~0.05% of Total Turnover
    exchange_charge = total_turnover * 0.0005

    # 4. GST: 18% on (Brokerage + Exchange Charges)
    gst = (brokerage + exchange_charge) * 0.18

    # 5. Stamp Duty: 0.003% on Buy side turnover
    stamp_duty = buy_turnover * 0.00003

    # Total Tax deductions
    total_taxes = round(brokerage + stt + exchange_charge + gst + stamp_duty, 2)
    gross_pnl = round(sell_turnover - buy_turnover, 2)
    net_pnl = round(gross_pnl - total_taxes, 2)

    return {
        "buy_turnover": round(buy_turnover, 2),
        "sell_turnover": round(sell_turnover, 2),
        "gross_pnl": gross_pnl,
        "brokerage": brokerage,
        "stt": round(stt, 2),
        "exchange_charge": round(exchange_charge, 2),
        "gst": round(gst, 2),
        "stamp_duty": round(stamp_duty, 2),
        "total_taxes": total_taxes,
        "net_pnl": net_pnl
    }
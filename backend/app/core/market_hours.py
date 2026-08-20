"""
Static NSE market-hours + holiday calendar (Dhan API doesn't expose a holiday
endpoint, so this is maintained manually — verify/update yearly against
https://www.nseindia.com/resources/exchange-communication-holidays).

⚠️ IMPORTANT: The 2026 list below is NOT guaranteed complete — please cross-check
against the official NSE page and add any missing dates.
"""
from datetime import datetime, timedelta
from typing import Dict, Any

IST_OFFSET = timedelta(hours=5, minutes=30)
MARKET_OPEN_HOUR, MARKET_OPEN_MIN = 9, 15
MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN = 15, 30

# Confirmed 2026 NSE trading holidays found — VERIFY AND COMPLETE this list from
# the official NSE holiday page before relying on it fully.
NSE_HOLIDAYS_2026 = {
    "2026-06-26",  # Muharram
    "2026-09-14",  # Ganesh Chaturthi
    "2026-10-02",  # Mahatma Gandhi Jayanti
    # 🔴 ADD REMAINING 2026 HOLIDAYS HERE (Republic Day, Holi, Good Friday,
    # Ambedkar Jayanti, Independence Day, Dussehra, Diwali, Christmas, etc.)
}


def _now_ist() -> datetime:
    return datetime.utcnow() + IST_OFFSET


def is_market_open(ist_now: datetime = None) -> bool:
    ist_now = ist_now or _now_ist()
    if ist_now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    if ist_now.strftime("%Y-%m-%d") in NSE_HOLIDAYS_2026:
        return False
    current_minutes = ist_now.hour * 60 + ist_now.minute
    open_minutes = MARKET_OPEN_HOUR * 60 + MARKET_OPEN_MIN
    close_minutes = MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN
    return open_minutes <= current_minutes <= close_minutes


def _next_open_display(ist_now: datetime) -> str:
    candidate = ist_now
    for _ in range(10):
        candidate = candidate + timedelta(days=1) if (
            candidate.hour * 60 + candidate.minute > MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN
            or candidate.date() != ist_now.date()
        ) else candidate
        if candidate.weekday() < 5 and candidate.strftime("%Y-%m-%d") not in NSE_HOLIDAYS_2026:
            return candidate.strftime("%A, %d %B") + " at 9:15 AM"
        candidate = candidate + timedelta(days=1)
    return "soon"


def get_market_status(ist_now: datetime = None) -> Dict[str, Any]:
    ist_now = ist_now or _now_ist()
    open_now = is_market_open(ist_now)

    if open_now:
        return {"is_open": True, "message": "Market is open.", "next_open": None}

    is_holiday = ist_now.strftime("%Y-%m-%d") in NSE_HOLIDAYS_2026
    is_weekend = ist_now.weekday() >= 5

    if is_holiday:
        reason = "Today is an NSE trading holiday."
    elif is_weekend:
        reason = "Markets are closed on weekends."
    elif ist_now.hour * 60 + ist_now.minute < MARKET_OPEN_HOUR * 60 + MARKET_OPEN_MIN:
        reason = "Market hasn't opened yet today."
    else:
        reason = "Market has closed for the day."

    next_open = _next_open_display(ist_now)
    return {
        "is_open": False,
        "message": f"{reason} Trading hours are 9:15 AM – 3:30 PM IST, Monday to Friday.",
        "next_open": next_open
    }
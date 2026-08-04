from datetime import datetime
from typing import List, Optional, Any
from pydantic import BaseModel, Field, ConfigDict


class SignalBase(BaseModel):
    user_id: str = Field(..., description="ID of the user who requested or owns this signal")
    index_name: str = Field(default="NIFTY", description="Target Index: NIFTY, BANKNIFTY, SENSEX, FINNIFTY")
    signal: str = Field(..., description="BUY CALL, BUY PUT, or NO TRADE")
    strike: str = Field(..., description="e.g. 24200CE or 48500PE")
    selected_type: Optional[str] = Field(None, description="CE or PE")
    
    # Prices
    entry_price: float = Field(..., description="Option premium price at entry (e.g. 135.5)")
    index_spot: float = Field(..., description="Underlying Index Spot price at entry (e.g. 24250.2)")
    stop_loss: float = Field(..., description="Option Premium StopLoss level")
    shz_upper: float = Field(..., description="Target 1 Option Premium Level")
    shz_lower: float = Field(..., description="StopLoss Option Premium Level (Duplicate for UI compatibility)")
    target2: Optional[float] = Field(None, description="Target 2 Option Premium Level")
    
    # Analytics
    score: float = Field(..., description="AI Confluence Score (0.0 to 10.0)")
    reasons: List[str] = Field(default_factory=list, description="List of AI Confluence triggers")
    pcr: float = Field(default=1.0, description="Global Put Call Ratio")
    vix: float = Field(default=13.5, description="India VIX value")
    
    # Execution Metadata
    breakout_status: str = Field(default="CONFLUENCE_DECODE", description="CONFLUENCE_DECODE or FORCED_SCALP")
    atm_strike: int = Field(..., description="ATM Strike Price Number")
    security_id: str = Field(..., description="Dhan Option Security ID for WS live tracking")
    
    # Position Tracking Status
    status: str = Field(default="ACTIVE", description="ACTIVE, TARGET_HIT, SL_HIT, CLOSED")
    exit_ltp: Optional[float] = Field(None, description="LTP price when Target/SL was locked")


class SignalCreate(SignalBase):
    pass


class SignalInDB(SignalBase):
    id: str = Field(..., alias="_id", description="MongoDB ObjectId string")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={datetime: lambda dt: dt.isoformat()}
    )


class SignalResponse(BaseModel):
    success: bool
    data: Optional[Dict[str, Any]] = None
    message: Optional[str] = None
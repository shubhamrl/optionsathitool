from fastapi import APIRouter, Depends, HTTPException, status
from typing import Dict, Any, List
from datetime import datetime
from bson import ObjectId

from app.api.v1.endpoints.auth import get_current_user, get_database
from app.models.paper_trading import calculate_indian_option_charges, LOT_SIZES

router = APIRouter()

DEFAULT_VIRTUAL_FUNDS = 100000.0  # ₹1 Lakh Virtual Capital

# ----------------------------------------------------------------------------
# 1. GET /api/v1/paper/wallet - Fetch or Initialize User Virtual Wallet
# ----------------------------------------------------------------------------
@router.get("/wallet")
async def get_paper_wallet(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    user_id = str(current_user["_id"])
    wallet = await db.paper_wallets.find_one({"user_id": user_id})

    if not wallet:
        new_wallet = {
            "user_id": user_id,
            "balance": DEFAULT_VIRTUAL_FUNDS,
            "initial_capital": DEFAULT_VIRTUAL_FUNDS,
            "realized_pnl": 0.0,
            "total_taxes_paid": 0.0,
            "total_trades": 0,
            "created_at": datetime.utcnow()
        }
        await db.paper_wallets.insert_one(new_wallet)
        wallet = new_wallet

    wallet["_id"] = str(wallet.get("_id", ""))
    return {"success": True, "wallet": wallet}


# ----------------------------------------------------------------------------
# 2. POST /api/v1/paper/place-trade - Execute Virtual Order from Signal
# ----------------------------------------------------------------------------
@router.post("/place-trade")
async def place_paper_trade(
    trade_data: Dict[str, Any],
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    user_id = str(current_user["_id"])
    index_name = trade_data.get("index_name", "NIFTY")
    signal_type = trade_data.get("signal", "BUY CALL")
    strike = trade_data.get("strike", "")
    entry_price = float(trade_data.get("entry_price", 0.0))
    stop_loss = float(trade_data.get("stop_loss", 0.0))
    target1 = float(trade_data.get("target1", 0.0))

    # Required for live LTP streaming + auto SL/Target square-off linking
    security_id = str(trade_data.get("security_id", "")).strip()
    signal_id = trade_data.get("signal_id")

    if not security_id:
        raise HTTPException(
            status_code=400,
            detail="security_id is required to place a paper trade (needed for live LTP tracking)."
        )

    lots = int(trade_data.get("lots", 1))
    lot_size = LOT_SIZES.get(index_name, 25)
    quantity = lots * lot_size
    required_margin = entry_price * quantity

    # Check Wallet Balance
    wallet = await db.paper_wallets.find_one({"user_id": user_id})
    current_balance = wallet.get("balance", DEFAULT_VIRTUAL_FUNDS) if wallet else DEFAULT_VIRTUAL_FUNDS

    if current_balance < required_margin:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient Virtual Balance! Required: ₹{required_margin:.2f}, Available: ₹{current_balance:.2f}"
        )

    # Deduct Margin & Create Open Paper Trade
    new_balance = current_balance - required_margin
    await db.paper_wallets.update_one(
        {"user_id": user_id},
        {"$set": {"balance": new_balance}}
    )

    paper_trade = {
        "user_id": user_id,
        "index_name": index_name,
        "signal": signal_type,
        "strike": strike,
        "security_id": security_id,
        "signal_id": str(signal_id) if signal_id else None,
        "buy_price": entry_price,
        "sell_price": 0.0,
        "quantity": quantity,
        "lots": lots,
        "stop_loss": stop_loss,
        "target1": target1,
        "status": "OPEN",
        "margin_used": required_margin,
        "created_at": datetime.utcnow()
    }

    result = await db.paper_trades.insert_one(paper_trade)
    paper_trade["_id"] = str(result.inserted_id)

    # Link this trade into the active SL/Target monitoring tracker so
    # dhan_websocket.py can auto square-off this exact trade on hit.
    if signal_id:
        from app.services.dhan_websocket import link_paper_trade_to_position
        link_paper_trade_to_position(security_id, signal_id, str(result.inserted_id))

    return {
        "success": True,
        "message": "Virtual Paper Trade Executed!",
        "trade": paper_trade,
        "remaining_balance": new_balance
    }


# ----------------------------------------------------------------------------
# 3. GET /api/v1/paper/positions - Fetch Active & Closed Paper Trades
# ----------------------------------------------------------------------------
@router.get("/positions")
async def get_paper_positions(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    user_id = str(current_user["_id"])

    cursor = db.paper_trades.find({"user_id": user_id}).sort("created_at", -1)

    positions = []
    async for doc in cursor:
        positions.append({
            "_id": str(doc["_id"]),
            "user_id": str(doc.get("user_id", "")),
            "index_name": doc.get("index_name", "NIFTY"),
            "signal": doc.get("signal", "BUY CALL"),
            "strike": doc.get("strike", ""),
            "security_id": doc.get("security_id", ""),
            "signal_id": doc.get("signal_id", ""),
            "buy_price": float(doc.get("buy_price", 0.0)),
            "sell_price": float(doc.get("sell_price", 0.0)),
            "quantity": int(doc.get("quantity", 0)),
            "lots": int(doc.get("lots", 1)),
            "stop_loss": float(doc.get("stop_loss", 0.0)),
            "target1": float(doc.get("target1", 0.0)),
            "margin_used": float(doc.get("margin_used", 0.0)),
            "net_pnl": float(doc.get("net_pnl", 0.0)),
            "status": doc.get("status", "OPEN"),
            "exit_reason": doc.get("exit_reason", ""),
            "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else ""
        })

    return {"success": True, "positions": positions}


# ----------------------------------------------------------------------------
# 4. POST /api/v1/paper/square-off/{trade_id} - Close Paper Position
# ----------------------------------------------------------------------------
@router.post("/square-off/{trade_id}")
async def square_off_paper_trade(
    trade_id: str,
    exit_data: Dict[str, Any],
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    user_id = str(current_user["_id"])
    exit_price = float(exit_data.get("exit_price", 0.0))

    # 🔒 Atomically CLAIM this trade — only proceeds if it's still OPEN at this exact
    # instant. Prevents double-crediting the wallet if this manual exit races with
    # the auto SL/Target monitor or the EOD scheduler closing the same trade.
    trade = await db.paper_trades.find_one_and_update(
        {"_id": ObjectId(trade_id), "user_id": user_id, "status": "OPEN"},
        {"$set": {"status": "CLOSING"}}
    )
    if not trade:
        raise HTTPException(status_code=400, detail="Trade not found or already closed!")

    buy_price = trade["buy_price"]
    quantity = trade["quantity"]
    margin_used = trade["margin_used"]

    # Calculate Charges & PnL
    charges = calculate_indian_option_charges(buy_price, exit_price, quantity)
    net_pnl = charges["net_pnl"]
    total_taxes = charges["total_taxes"]

    # Refund Margin + Add Net PnL to Wallet
    wallet = await db.paper_wallets.find_one({"user_id": user_id})
    current_balance = wallet.get("balance", DEFAULT_VIRTUAL_FUNDS)
    current_realized_pnl = wallet.get("realized_pnl", 0.0)
    current_taxes = wallet.get("total_taxes_paid", 0.0)

    updated_balance = current_balance + margin_used + net_pnl
    updated_realized_pnl = current_realized_pnl + net_pnl
    updated_taxes = current_taxes + total_taxes

    await db.paper_wallets.update_one(
        {"user_id": user_id},
        {"$set": {
            "balance": round(updated_balance, 2),
            "realized_pnl": round(updated_realized_pnl, 2),
            "total_taxes_paid": round(updated_taxes, 2)
        }}
    )

    # Update Paper Trade Status
    await db.paper_trades.update_one(
        {"_id": ObjectId(trade_id)},
        {"$set": {
            "status": "SQUARED_OFF",
            "sell_price": exit_price,
            "net_pnl": net_pnl,
            "charges": charges,
            "closed_at": datetime.utcnow()
        }}
    )

    return {"success": True, "message": "Position Squared Off!", "net_pnl": net_pnl}


# ----------------------------------------------------------------------------
# 5. POST /api/v1/paper/square-off-all - Close All Open Paper Positions
# ----------------------------------------------------------------------------
@router.post("/square-off-all")
async def square_off_all_paper_trades(
    exit_data: Dict[str, Any],
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    user_id = str(current_user["_id"])
    prices_map = exit_data.get("prices_map", {})  # { trade_id: current_ltp }

    # Fetch all open positions for the user
    cursor = db.paper_trades.find({"user_id": user_id, "status": "OPEN"})
    open_trades = await cursor.to_list(length=100)

    if not open_trades:
        return {"success": True, "message": "No open positions to close!", "closed_count": 0}

    wallet = await db.paper_wallets.find_one({"user_id": user_id})
    current_balance = wallet.get("balance", DEFAULT_VIRTUAL_FUNDS) if wallet else DEFAULT_VIRTUAL_FUNDS
    current_realized_pnl = wallet.get("realized_pnl", 0.0) if wallet else 0.0
    current_taxes = wallet.get("total_taxes_paid", 0.0) if wallet else 0.0

    total_refund_margin = 0.0
    total_net_pnl = 0.0
    total_taxes = 0.0
    closed_count = 0

    for trade in open_trades:
        trade_id = trade["_id"]

        # 🔒 Atomically claim each trade — skip if the auto SL/Target monitor or EOD
        # scheduler already closed it between the initial fetch above and now.
        claimed = await db.paper_trades.find_one_and_update(
            {"_id": trade_id, "status": "OPEN"},
            {"$set": {"status": "CLOSING"}}
        )
        if not claimed:
            continue

        buy_price = claimed["buy_price"]
        quantity = claimed["quantity"]
        margin_used = claimed["margin_used"]

        exit_price = float(prices_map.get(str(trade_id), buy_price))
        if exit_price <= 0:
            exit_price = buy_price

        charges = calculate_indian_option_charges(buy_price, exit_price, quantity)
        net_pnl = charges["net_pnl"]
        taxes = charges["total_taxes"]

        total_refund_margin += margin_used
        total_net_pnl += net_pnl
        total_taxes += taxes
        closed_count += 1

        await db.paper_trades.update_one(
            {"_id": trade_id},
            {"$set": {
                "status": "SQUARED_OFF",
                "sell_price": exit_price,
                "net_pnl": net_pnl,
                "charges": charges,
                "closed_at": datetime.utcnow()
            }}
        )

    # Update Wallet Balance, PnL and Taxes in one atomic query
    updated_balance = current_balance + total_refund_margin + total_net_pnl
    updated_realized_pnl = current_realized_pnl + total_net_pnl
    updated_taxes = current_taxes + total_taxes

    await db.paper_wallets.update_one(
        {"user_id": user_id},
        {"$set": {
            "balance": round(updated_balance, 2),
            "realized_pnl": round(updated_realized_pnl, 2),
            "total_taxes_paid": round(updated_taxes, 2)
        }}
   )

    return {
        "success": True,
        "message": f"Successfully closed {closed_count} open position(s)!",
        "closed_count": closed_count,
        "total_net_pnl": round(total_net_pnl, 2)
    }


# ----------------------------------------------------------------------------
# 6. POST /api/v1/paper/admin/recalculate-wallets - Fix any wallet corruption
# Rebuilds every user's wallet balance from scratch using their actual
# paper_trades records as source of truth. Also un-sticks any trade left in
# "CLOSING" state by a crashed process, so it can be retried.
# ----------------------------------------------------------------------------
@router.post("/admin/recalculate-wallets")
async def recalculate_all_wallets(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db=Depends(get_database)
):
    # Un-stick any trade stranded in "CLOSING" (e.g. server crashed mid-close)
    stuck_result = await db.paper_trades.update_many(
        {"status": "CLOSING"},
        {"$set": {"status": "OPEN"}}
    )

    user_ids = await db.paper_trades.distinct("user_id")
    results = []

    for user_id in user_ids:
        closed_cursor = db.paper_trades.find({"user_id": user_id, "status": "SQUARED_OFF"})
        closed_trades = await closed_cursor.to_list(length=10000)

        open_cursor = db.paper_trades.find({"user_id": user_id, "status": "OPEN"})
        open_trades = await open_cursor.to_list(length=10000)

        realized_pnl = round(sum(float(t.get("net_pnl", 0.0)) for t in closed_trades), 2)
        total_taxes_paid = round(sum(float((t.get("charges") or {}).get("total_taxes", 0.0)) for t in closed_trades), 2)
        margin_locked_in_open = round(sum(float(t.get("margin_used", 0.0)) for t in open_trades), 2)

        wallet = await db.paper_wallets.find_one({"user_id": user_id})
        initial_capital = wallet.get("initial_capital", DEFAULT_VIRTUAL_FUNDS) if wallet else DEFAULT_VIRTUAL_FUNDS

        correct_balance = round(initial_capital + realized_pnl - margin_locked_in_open, 2)

        await db.paper_wallets.update_one(
            {"user_id": user_id},
            {"$set": {
                "balance": correct_balance,
                "realized_pnl": realized_pnl,
                "total_taxes_paid": total_taxes_paid
            }},
            upsert=True
        )

        results.append({
            "user_id": user_id,
            "corrected_balance": correct_balance,
            "realized_pnl": realized_pnl,
            "open_positions_margin_locked": margin_locked_in_open
        })

    return {
        "success": True,
        "message": f"Recalculated {len(results)} wallet(s). Un-stuck {stuck_result.modified_count} stranded trade(s).",
        "wallets": results
    }
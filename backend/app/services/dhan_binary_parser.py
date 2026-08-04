import struct
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# ============================================================================
# DHAN V2 BINARY RESPONSE CODES (Per Official Dhan API Specification)
# ============================================================================
RESPONSE_CODE_TICKER = 2      # Ticker Packet (LTP + Last Traded Time)
RESPONSE_CODE_QUOTE = 4       # Quote Packet (LTP, Volume, OI, Avg Price)
RESPONSE_CODE_FULL = 8        # Full Packet (Market Depth + L1 Data)
RESPONSE_CODE_PREVIOUS_CLOSE = 6


def parse_dhan_binary_feed(binary_data: bytes) -> Optional[Dict[str, Any]]:
    """
    Parses Little-Endian Binary Packets from Dhan Market Feed WebSocket.
    
    Header Format (8 Bytes):
    - ResponseCode : 1 byte  (unsigned char -> 'B')
    - MessageLength: 2 bytes (unsigned short -> 'H')
    - ExchangeSeg  : 1 byte  (unsigned char -> 'B')
    - SecurityId   : 4 bytes (unsigned int -> 'I')
    """
    if not binary_data or len(binary_data) < 8:
        return None

    try:
        # Unpack Common Header (First 8 Bytes)
        response_code, msg_length, exchange_seg, security_id = struct.unpack('<BHBI', binary_data[:8])
        
        parsed_tick: Dict[str, Any] = {
            "response_code": response_code,
            "security_id": str(security_id),
            "exchange_segment": exchange_seg
        }

        # --------------------------------------------------------------------
        # 1. RESPONSE CODE 2: TICKER DATA PACKET (Minimum 16 Bytes)
        # Offset 8..12 -> LTP (float, 4 bytes)
        # Offset 12..16 -> Last Traded Time (uint32, 4 bytes)
        # --------------------------------------------------------------------
        if response_code == RESPONSE_CODE_TICKER and len(binary_data) >= 16:
            ltp, ltt = struct.unpack('<fI', binary_data[8:16])
            
            parsed_tick.update({
                "ltp": round(float(ltp), 2),
                "ltt": ltt
            })
            return parsed_tick

        # --------------------------------------------------------------------
        # 2. RESPONSE CODE 4: QUOTE DATA PACKET (Minimum 38 Bytes)
        # Contains: LTP, Last Qty, LTT, Avg Price, Volume, Buy/Sell Qty, Open Interest
        # --------------------------------------------------------------------
        elif response_code == RESPONSE_CODE_QUOTE and len(binary_data) >= 38:
            # Unpack payload following the 8-byte header:
            # f = LTP (float)
            # H = Last Quantity (uint16)
            # I = Last Traded Time (uint32)
            # f = Average Price (float)
            # I = Volume (uint32)
            # I = Total Buy Qty (uint32)
            # I = Total Sell Qty (uint32)
            # I = Open Interest (uint32)
            ltp, last_qty, ltt, avg_price, volume, total_buy_qty, total_sell_qty, oi = struct.unpack(
                '<fHIfIIII', binary_data[8:38]
            )

            parsed_tick.update({
                "ltp": round(float(ltp), 2),
                "last_qty": last_qty,
                "ltt": ltt,
                "avg_price": round(float(avg_price), 2),
                "volume": volume,
                "buy_qty": total_buy_qty,
                "sell_qty": total_sell_qty,
                "oi": oi  # 🟢 Live Open Interest Update
            })
            return parsed_tick

        # --------------------------------------------------------------------
        # 3. RESPONSE CODE 8: FULL DATA PACKET WITH PREVIOUS CLOSE
        # --------------------------------------------------------------------
        elif response_code == RESPONSE_CODE_FULL and len(binary_data) >= 42:
            ltp, last_qty, ltt, avg_price, volume, total_buy_qty, total_sell_qty, oi, prev_close = struct.unpack(
                '<fHIfIIIIf', binary_data[8:42]
            )

            parsed_tick.update({
                "ltp": round(float(ltp), 2),
                "last_qty": last_qty,
                "ltt": ltt,
                "avg_price": round(float(avg_price), 2),
                "volume": volume,
                "oi": oi,
                "prev_close": round(float(prev_close), 2)
            })
            return parsed_tick

        # Unknown or heartbeat packets - return basic header
        return parsed_tick

    except struct.error as e:
        logger.debug(f"Binary unpacking error for frame length {len(binary_data)}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Unexpected binary parser error: {str(e)}")
        return None
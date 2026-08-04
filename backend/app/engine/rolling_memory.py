import logging
from collections import deque
from datetime import datetime
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

MAX_MEMORY_SIZE = 60  # Rolling 60 snapshots (5-minute memory at 5-sec intervals or tick snapshots)


class RollingIndexMemory:
    """
    In-Memory Rolling Window Store for a single Index (Nifty, BankNifty, Sensex, etc.)
    Maintains time-series snapshots for indicators (EMA, VWAP, ATR, PCR).
    """

    def __init__(self, index_name: str, max_size: int = MAX_MEMORY_SIZE):
        self.index_name = index_name
        self.max_size = max_size
        # O(1) rolling FIFO queue
        self._snapshots: deque = deque(maxlen=max_size)

    def add_snapshot(self, spot: float, pcr: float = 1.0, volume: int = 0, extra: Optional[Dict[str, Any]] = None):
        """
        Adds a new market tick snapshot. Automatically purges the oldest snapshot when max_size is exceeded.
        """
        if spot is None or spot <= 0:
            return

        snapshot = {
            "spot": float(spot),
            "pcr": float(pcr) if pcr is not None else 1.0,
            "volume": int(volume) if volume is not None else 0,
            "timestamp": datetime.utcnow().isoformat(),
        }

        if extra and isinstance(extra, dict):
            snapshot.update(extra)

        self._snapshots.append(snapshot)

    def get_all(self) -> List[Dict[str, Any]]:
        """Returns all stored snapshots as a list."""
        return list(self._snapshots)

    def get_last_n(self, n: int) -> List[Dict[str, Any]]:
        """Returns the last N snapshots."""
        if n <= 0:
            return []
        snapshots_list = list(self._snapshots)
        return snapshots_list[-n:]

    def get_last(self) -> Optional[Dict[str, Any]]:
        """Returns the most recent snapshot."""
        return self._snapshots[-1] if self._snapshots else None

    def get_spot_prices((self) -> List[float]:
        """Extracts valid spot prices array for EMA/VWAP/ATR calculations."""
        return [s["spot"] for s in self._snapshots if "spot" in s and s["spot"] > 0]

    def get_pcr_history(self) -> List[float]:
        """Extracts PCR history array for sentiment trend analysis."""
        return [s["pcr"] for s in self._snapshots if "pcr" in s]

    def get_volume_history(self) -> List[int]:
        """Extracts Volume history array for volume spike calculations."""
        return [s["volume"] for s in self._snapshots if "volume" in s]

    def size(self) -> int:
        """Returns current snapshot count."""
        return len(self._snapshots)

    def clear(self):
        """Flushes the rolling memory."""
        self._snapshots.clear()


class MultiIndexMemoryRegistry:
    """
    Central Manager for Multi-Index Rolling Memory Stores.
    Isolated memory pools for NIFTY, BANKNIFTY, SENSEX, FINNIFTY.
    """

    def __init__(self):
        self._registry: Dict[str, RollingIndexMemory] = {
            "NIFTY": RollingIndexMemory("NIFTY"),
            "BANKNIFTY": RollingIndexMemory("BANKNIFTY"),
            "SENSEX": RollingIndexMemory("SENSEX"),
            "FINNIFTY": RollingIndexMemory("FINNIFTY"),
        }

    def get_memory(self, index_name: str) -> RollingIndexMemory:
        """Retrieves or creates rolling memory store for a specific index."""
        idx_key = index_name.upper()
        if idx_key not in self._registry:
            self._registry[idx_key] = RollingIndexMemory(idx_key)
        return self._registry[idx_key]

    def add_spot_tick(self, index_name: str, spot: float, pcr: float = 1.0, volume: int = 0):
        """Helper to quickly append spot tick to target index memory."""
        mem = self.get_memory(index_name)
        mem.add_snapshot(spot=spot, pcr=pcr, volume=volume)


# Global Singleton Manager for Multi-Index Memory
multi_index_memory = MultiIndexMemoryRegistry()
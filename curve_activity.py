"""Bounded, read-only PumpPortal trade observations; not proof of executable liquidity."""
from collections import defaultdict, deque
import threading
import time

class CurveActivity:
    def __init__(self, window_seconds=300, max_mints=200):
        self.window_seconds = window_seconds
        self.max_mints = max_mints
        self._trades = defaultdict(deque)
        self._lock = threading.Lock()

    def record(self, event, now=None):
        if not isinstance(event, dict):
            return False
        mint = event.get('mint')
        kind = event.get('txType')
        sol = event.get('solAmount')
        if not isinstance(mint, str) or kind not in ('buy', 'sell') or isinstance(sol, bool):
            return False
        try:
            sol = float(sol)
        except (TypeError, ValueError):
            return False
        if not (0 < sol < 1e7):
            return False
        timestamp = time.monotonic() if now is None else now
        with self._lock:
            if mint not in self._trades and len(self._trades) >= self.max_mints:
                self._trades.pop(next(iter(self._trades)))
            q = self._trades[mint]
            q.append((timestamp, kind, sol))
            self._prune(q, timestamp)
        return True

    def _prune(self, q, now):
        while q and q[0][0] < now - self.window_seconds:
            q.popleft()

    def summary(self, mint, now=None):
        timestamp = time.monotonic() if now is None else now
        with self._lock:
            q = self._trades.get(mint)
            if q is None:
                return {'trades': 0, 'buys': 0, 'sells': 0, 'volume_sol': 0.0}
            self._prune(q, timestamp)
            buys = sum(kind == 'buy' for _, kind, _ in q)
            sells = len(q) - buys
            return {'trades': len(q), 'buys': buys, 'sells': sells,
                    'volume_sol': round(sum(sol for _, _, sol in q), 6)}

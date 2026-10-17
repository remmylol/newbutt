"""v8.2 independent discovery-only scheduling/diagnostics (no trade approval)."""
from __future__ import annotations
from collections import deque
from threading import Lock
import math
import time


class SharedGeckoBudget:
    """Per-process rolling request limit shared by discovery and diagnostics.

    Does not reserve/modify trading positions. The upstream provider may enforce
    tighter shared-IP limits, so a real 429 wins and pauses this whole source.
    """
    def __init__(self, max_per_minute: int = 6):
        self.limit = max(1, min(20, int(max_per_minute)))
        self.requests = deque()
        self.backoff_until = 0.0
        self.lock = Lock()

    def acquire(self, *, now: float | None = None) -> tuple[bool, str, float]:
        stamp = time.monotonic() if now is None else float(now)
        with self.lock:
            while self.requests and self.requests[0] <= stamp - 60:
                self.requests.popleft()
            if stamp < self.backoff_until:
                return False, 'provider_backoff', self.backoff_until - stamp
            if len(self.requests) >= self.limit:
                return False, 'minute_budget', max(0, self.requests[0] + 60 - stamp)
            self.requests.append(stamp)
            return True, 'ok', 0.0

    def note_429(self, retry_after: int | float = 180, *, now: float | None = None) -> None:
        stamp = time.monotonic() if now is None else float(now)
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = 180.0
        if not math.isfinite(delay):
            delay = 180.0
        with self.lock:
            self.backoff_until = max(self.backoff_until, stamp + max(60.0, min(900.0, delay)))

    def status(self, *, now: float | None = None) -> dict:
        stamp = time.monotonic() if now is None else float(now)
        with self.lock:
            used = sum(t > stamp - 60 for t in self.requests)
            return {'minute_used': used, 'minute_max': self.limit,
                    'backoff_s': round(max(0.0, self.backoff_until - stamp), 1)}


def primary_m5_activity(pair: dict) -> str:
    """Classify absent 5m data separately from explicit reported zero.

    This is telemetry ONLY. Missing is not treated as 0 momentum or buying.
    Neither status qualifies a token for entry without normal market checks.
    """
    if not isinstance(pair, dict):
        return 'missing_market_pair'
    volume = pair.get('volume')
    changes = pair.get('priceChange')
    tx = (pair.get('txns') or {}).get('m5') if isinstance(pair.get('txns'), dict) else None
    if not isinstance(volume, dict) or 'm5' not in volume or volume['m5'] is None:
        return 'volume_missing'
    try:
        vol = float(volume['m5'])
    except (TypeError, ValueError, OverflowError):
        return 'volume_invalid'
    if not math.isfinite(vol) or vol < 0:
        return 'volume_invalid'
    if not isinstance(changes, dict) or changes.get('m5') is None:
        return 'trend_missing'
    if not isinstance(tx, dict) or tx.get('buys') is None or tx.get('sells') is None:
        return 'trades_missing'
    try:
        buys, sells = float(tx['buys']), float(tx['sells'])
    except (ValueError, TypeError, OverflowError):
        return 'trades_invalid'
    if not all(math.isfinite(n) and n >= 0 for n in (buys, sells)):
        return 'trades_invalid'
    return 'reported_zero_volume' if vol == 0 and buys == 0 and sells == 0 else 'reported_activity'

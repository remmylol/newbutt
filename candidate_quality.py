"""Persistent, conservative scheduling hints for established Solana paper scans.

A route/liquidity failure postpones the next *entry discovery lookup*; a confirmed
liquid route or filtered momentum candidate clears the structural quarantine.
Quality rankings ONLY choose which token to examine, never authorize a trade.
Held positions are never governed by this module and remain managed independently.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

from rolling_discovery import MINT_PATTERN

# Different failure classes are intentionally treated differently. Missing
# liquidity is NOT assumed to mean a zero-liquidity pool or a confirmed rug.
RETRY_POLICY = {
    "supported_pair_liquidity_unavailable": (600, 3600),
    "supported_pair_missing_price": (600, 3600),
    "pair_not_indexed": (300, 2400),
    "supported_pair_low_liquidity": (1200, 7200),
    "eligible_route_liquidity": (1200, 7200),
    "no_sol_quoted_pair": (3600, 21600),
    "unsupported_sol_route": (7200, 86400),
    "unsupported_route": (7200, 86400),
}


class CandidateQualityLedger:
    def __init__(self, path: Path, *, limit: int = 2500, ttl_seconds: int = 7 * 86400):
        self.path = Path(path)
        self.limit = max(100, int(limit))
        self.ttl_seconds = max(3600, int(ttl_seconds))
        self.records: dict[str, dict] = {}
        self.loaded = False
        self.read_only = False
        self.last_saved = 0.0

    def load(self, *, now: float | None = None) -> None:
        if self.loaded:
            return
        stamp = time.time() if now is None else now
        try:
            data = json.loads(self.path.read_text(encoding="utf8"))
        except FileNotFoundError:
            self.loaded = True
            return
        except (OSError, ValueError, TypeError):
            self.loaded = self.read_only = True
            return
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("records"), dict):
            self.loaded = self.read_only = True
            return
        self.loaded = True
        for mint, rec in data["records"].items():
            if not isinstance(mint, str) or not MINT_PATTERN.fullmatch(mint) or not isinstance(rec, dict):
                continue
            try:
                last_seen = float(rec["last_seen"])
                retry_until = float(rec.get("retry_until", 0))
                verified_at = float(rec.get("verified_at", 0))
                momentum_at = float(rec.get("momentum_at", 0))
                momentum_score = float(rec.get("momentum_score", 0))
                failures = int(rec.get("failures", 0))
                reason = str(rec.get("reason", ""))
            except (ValueError, TypeError, OverflowError, KeyError):
                continue
            if (not all(math.isfinite(x) and x >= 0 for x in
                        (last_seen, retry_until, verified_at, momentum_at, momentum_score))
                    or not 0 <= failures <= 100 or not 0 <= stamp - last_seen < self.ttl_seconds
                    or (reason and reason not in RETRY_POLICY)):
                continue
            self.records[mint] = dict(last_seen=last_seen, retry_until=retry_until,
                                      verified_at=verified_at, momentum_at=momentum_at,
                                      momentum_score=momentum_score, failures=failures, reason=reason)
        self.prune(now=stamp)

    def _record(self, mint: str, stamp: float) -> dict | None:
        if not isinstance(mint, str) or not MINT_PATTERN.fullmatch(mint):
            return None
        self.load(now=stamp)
        if mint not in self.records:
            self.records[mint] = dict(last_seen=stamp, retry_until=0.0,
                                      verified_at=0.0, momentum_at=0.0,
                                      momentum_score=0.0, failures=0, reason="")
        return self.records[mint]

    def fail(self, mint: str, reason: str, *, now: float | None = None) -> float:
        """Escalating backoff for repeated structural failures; zero for other reasons."""
        if reason not in RETRY_POLICY:
            return 0.0
        stamp = time.time() if now is None else now
        rec = self._record(mint, stamp)
        if rec is None:
            return 0.0
        # Reset streak when a different error occurs or hours have passed.
        consecutive = rec["reason"] == reason and stamp - rec["last_seen"] < 6 * 3600
        rec["failures"] = min(100, rec["failures"] + 1) if consecutive else 1
        base, ceiling = RETRY_POLICY[reason]
        ttl = min(ceiling, base * (2 ** min(rec["failures"] - 1, 8)))
        rec.update(last_seen=stamp, retry_until=stamp + ttl, reason=reason,
                   # Structural failure invalidates previous rank boost.
                   momentum_at=0.0, momentum_score=0.0)
        self.prune(now=stamp)
        return ttl

    def note_verified(self, mint: str, *, score: float | None = None,
                      now: float | None = None) -> bool:
        """Called ONLY after a supported and sufficiently liquid pool is revalidated."""
        stamp = time.time() if now is None else now
        rec = self._record(mint, stamp)
        if rec is None:
            return False
        recovered = rec["retry_until"] > 0 and rec["reason"] in RETRY_POLICY
        rec.update(last_seen=stamp, retry_until=0.0, reason="", failures=0,
                   verified_at=stamp)
        if score is None:
            # Previous promising momentum is no longer fresh if this scan
            # validated liquidity but failed the current momentum filters.
            rec["momentum_at"] = 0.0
            rec["momentum_score"] = 0.0
        else:
            try:
                val = float(score)
                if math.isfinite(val) and val > 0:
                    rec["momentum_score"] = min(100.0, val)
                    rec["momentum_at"] = stamp
            except (ValueError, TypeError, OverflowError):
                pass
        return recovered

    def blocked(self, mint: str, *, now: float | None = None) -> bool:
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        return self.records.get(mint, {}).get("retry_until", 0) > stamp

    def momentum_rank(self, mint: str, *, now: float | None = None) -> float:
        """Scheduling score decays fast; cannot substitute for current entry data."""
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        rec = self.records.get(mint)
        if not rec or rec["retry_until"] > stamp:
            return 0.0
        age = stamp - rec["momentum_at"]
        verified_age = stamp - rec["verified_at"]
        if not 0 <= age <= 300 or not 0 <= verified_age <= 300:
            return 0.0
        return rec["momentum_score"] * (1 - age / 330.0)

    def prioritise(self, mints: list[str], *, now: float | None = None) -> list[str]:
        """Keep first-seen order among ties; lift RECENT verified momentum."""
        stamp = time.time() if now is None else now
        distinct = list(dict.fromkeys(mints))
        return sorted((m for m in distinct if not self.blocked(m, now=stamp)),
                      key=lambda m: -self.momentum_rank(m, now=stamp))

    def stats(self, *, now: float | None = None) -> dict:
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        return {"records": len(self.records),
                "quarantined": sum(r["retry_until"] > stamp for r in self.records.values()),
                "fresh_momentum": sum(self.momentum_rank(m, now=stamp) > 0 for m in self.records)}

    def prune(self, *, now: float | None = None) -> None:
        stamp = time.time() if now is None else now
        self.records = {m: r for m, r in self.records.items()
                        if 0 <= stamp - r["last_seen"] < self.ttl_seconds}
        if len(self.records) > self.limit:
            for mint in sorted(self.records, key=lambda m: self.records[m]["last_seen"])[:len(self.records)-self.limit]:
                self.records.pop(mint, None)

    def save(self, *, now: float | None = None, force: bool = False) -> bool:
        stamp = time.time() if now is None else now
        if not self.loaded or self.read_only or (not force and stamp - self.last_saved < 30):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "records": self.records}, separators=(",", ":")), encoding="utf8")
        tmp.replace(self.path)
        self.last_saved = stamp
        return True

"""Bounded, persisted discovery queue for Solana PAPER momentum scans.

The records are *hints*, never permission to enter a trade. Actual price,
route, liquidity, age, and momentum checks remain in bot.py.
"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Iterable

MINT_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def rotating_pool_sources(round_number: int, *, per_round: int = 5) -> list[tuple[str, dict]]:
    """Walk different Solana rankings/pages rather than permanent first pages.

    GeckoTerminal documents network trending, top pools, and top-by-DEX.
    Public API access is limited; 5 calls per >=90s is ~3.3/minute.
    """
    n = max(0, int(round_number))
    durations = ("5m", "1h", "6h", "24h")
    dexes = ("raydium", "orca", "meteora", "pumpswap")
    options = [
        ("trending_pools", {"duration": durations[n % len(durations)], "page": 1 + (n // 4) % 5}),
        ("pools", {"page": 1 + n % 8, "order": "h24_volume_usd_desc"}),
    ]
    # Alternate DEX rankings and deep pages across refreshes. A DEX endpoint
    # returning 404 is backed off by the caller, not interpreted as market failure.
    for i in range(3):
        dex = dexes[(n * 3 + i) % len(dexes)]
        page = 1 + ((n + i * 2) // 2) % 6
        options.append((f"dexes/{dex}/pools", {"page": page}))
    return options[:max(0, min(per_round, 5))]


class RollingWatchlist:
    def __init__(self, path: Path, *, limit: int = 750, ttl_seconds: int = 21600):
        self.path = Path(path)
        self.limit = max(50, int(limit))
        self.ttl_seconds = max(3600, int(ttl_seconds))
        self.records: dict[str, dict] = {}
        self.loaded = False
        self.last_saved = 0.0
        self.last_refresh_new = 0
        self.source_round = 0
        self.read_only = False

    def load(self, *, now: float | None = None) -> None:
        if self.loaded:
            return
        stamp = time.time() if now is None else now
        try:
            data = json.loads(self.path.read_text(encoding="utf8"))
        except FileNotFoundError:
            self.loaded = True
            return
        except (ValueError, OSError, TypeError):
            # Keep monitoring positions even if optional discovery metadata is
            # corrupted. Preserve that file unchanged for manual recovery.
            self.loaded, self.read_only = True, True
            return
        if not isinstance(data, dict) or not isinstance(data.get("records"), dict):
            self.loaded, self.read_only = True, True
            return
        self.source_round = max(0, min(10000000, int(data.get("source_round", 0)))) if str(data.get("source_round", 0)).isdigit() else 0
        self.loaded = True
        for mint, record in data["records"].items():
            if not isinstance(mint, str) or not MINT_PATTERN.fullmatch(mint) or not isinstance(record, dict):
                continue
            try:
                seen = float(record["last_seen"])
                first = float(record["first_seen"])
                scanned = max(0.0, float(record.get("last_scanned", 0)))
                score = max(0.0, float(record.get("score", 0)))
            except (ValueError, TypeError, KeyError):
                continue
            if not all(math.isfinite(v) for v in (seen, first, scanned, score)):
                continue
            if 0 <= stamp - seen < self.ttl_seconds:
                self.records[mint] = {"first_seen": first, "last_seen": seen,
                                      "last_scanned": scanned, "score": score}
        self.prune(now=stamp)

    def prune(self, *, now: float | None = None) -> None:
        stamp = time.time() if now is None else now
        self.records = {k: v for k, v in self.records.items()
                        if 0 <= stamp - v["last_seen"] < self.ttl_seconds}
        if len(self.records) > self.limit:
            # Preserve genuinely new and recently sourced pools; least-recently
            # seen pools are the first to expire. Never controls held positions.
            evict = sorted(self.records, key=lambda k: self.records[k]["last_seen"])
            for mint in evict[:len(self.records) - self.limit]:
                self.records.pop(mint, None)

    def update(self, mints: Iterable[str], scores: dict[str, float] | None = None,
               *, now: float | None = None) -> int:
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        fresh = 0
        for mint in dict.fromkeys(mints):
            if not isinstance(mint, str) or not MINT_PATTERN.fullmatch(mint):
                continue
            if mint not in self.records:
                self.records[mint] = {"first_seen": stamp, "last_seen": stamp,
                                      "last_scanned": 0.0, "score": 0.0}
                fresh += 1
            rec = self.records[mint]
            rec["last_seen"] = stamp
            if scores:
                val = scores.get(mint, 0)
                try:
                    quality = float(val)
                    if math.isfinite(quality) and quality > 0:
                        rec["score"] = max(rec["score"], quality)
                except (TypeError, ValueError):
                    pass
        self.prune(now=stamp)
        self.last_refresh_new = fresh
        return fresh

    def priority_mints(self, *, limit: int | None = None) -> list[str]:
        # First visit genuinely new mints, then oldest last-scanned. The score
        # breaks ties but is never used to approve trading.
        ordered = sorted(self.records, key=lambda k: (
            self.records[k]["last_scanned"] > 0,
            self.records[k]["last_scanned"],
            -self.records[k]["first_seen"] if self.records[k]["last_scanned"] == 0 else 0,
            -self.records[k]["score"], k))
        return ordered if limit is None else ordered[:max(0, limit)]

    def mark_scanned(self, mints: Iterable[str], *, now: float | None = None) -> None:
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        for mint in mints:
            if mint in self.records:
                self.records[mint]["last_scanned"] = stamp

    def scan_counts(self, mints: Iterable[str]) -> tuple[int, int]:
        new = sum(self.records.get(m, {}).get("last_scanned", 1) == 0 for m in mints)
        return new, sum(m in self.records for m in mints)

    def save(self, *, now: float | None = None, force: bool = False) -> bool:
        stamp = time.time() if now is None else now
        if not self.loaded or self.read_only or (not force and stamp - self.last_saved < 45):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "source_round": self.source_round, "records": self.records}, separators=(",", ":")), encoding="utf8")
        tmp.replace(self.path)
        self.last_saved = stamp
        return True

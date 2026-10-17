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


def rotating_pool_sources(round_number: int, *, per_round: int = 3) -> list[tuple[str, dict]]:
    """Dedicated fast-mover feeds first, plus one varying broader market page.

    GeckoTerminal has duration-ranked 5m/1h trending pools. Give each a slot
    every refresh, and rotate a third slot through longer momentum windows,
    top-pool and DEX pages. Never silently use paid/scraped website data.
    """
    n = max(0, int(round_number))
    # Frequently check top pages (where fast movers surface), occasionally
    # explore pages 2-4 to discover a different established cohort.
    page5 = 1 if n % 4 else 1 + ((n // 4) % 4)
    page1 = 1 if n % 3 else 1 + ((n // 3) % 4)
    dexes = ("raydium", "orca", "meteora", "pumpswap")
    deep_page = 1 + ((n // 5) % 8)
    # Third request exposes a distinct type of discovery each round. New
    # pools become candidates only if the ordinary age/liquidity checks pass.
    extra = (
        ("new_pools", {"page":2 + (n // 5) % 4}),
        ("trending_pools", {"duration": "6h", "page": 1 + n % 3}),
        ("pools", {"page": deep_page}),
        (f"dexes/{dexes[(n // 5) % len(dexes)]}/pools", {"page": deep_page}),
        ("trending_pools", {"duration": "24h", "page": 1 + n % 3}),
    )[n % 5]
    sources = [
        ("trending_pools", {"duration":"5m", "page":page5}),
        ("trending_pools", {"duration":"1h", "page":page1}),
        extra,
    ]
    return sources[:max(0, min(per_round, len(sources)))]


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
        self.search_cursor = 0
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
        cursor = data.get("search_cursor", 0)
        self.search_cursor = max(0, min(10000000, int(cursor))) if str(cursor).isdigit() else 0
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
        tmp.write_text(json.dumps({"version": 2, "source_round": self.source_round, "search_cursor": self.search_cursor, "records": self.records}, separators=(",", ":")), encoding="utf8")
        tmp.replace(self.path)
        self.last_saved = stamp
        return True

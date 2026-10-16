"""Diversified, conservative discovery hints; NEVER an entry or exit decision.

DexScreener search is a second discovery lane while GeckoTerminal pages are
rate limited.  The final entry path independently rechecks fresh market data.
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter
from typing import Any

# Rotating broad and narrative-specific terms; nothing here implies quality.
DEFAULT_SEARCH_TERMS = (
    "solana", "bonk", "pepe", "ai", "meme", "cat", "dog", "frog", "inu",
    "moon", "pump", "degen", "agent", "gaming", "trump", "wojak", "coin",
    "sol", "orca", "raydium", "meteora", "memecoin", "shib", "based",
    "sui", "bitcoin", "chad", "doge", "terminal", "viral", "robot", "bull",
)
MINT_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def rotating_search_terms(terms: tuple[str, ...], cursor: int, quota: int) -> tuple[list[str], int]:
    clean = list(dict.fromkeys(t.lower().strip() for t in terms if t and t.strip()))
    if not clean or quota <= 0:
        return [], max(0, cursor)
    count = min(quota, len(clean))
    start = max(0, cursor) % len(clean)
    selected = [clean[(start + i) % len(clean)] for i in range(count)]
    return selected, (start + count) % len(clean)


def search_pool_hints(pairs: Any, *, sol_mint: str, supported_dexes: frozenset[str],
                      min_liquidity_usd: float, min_pair_age_minutes: float,
                      min_cap_usd: float, max_cap_usd: float,
                      now_ms: float | None = None,
                      rejections: Counter | None = None) -> dict[str, float]:
    """Prefilter SOL-quoted markets for the watchlist, never approve trades.

    Unknown cap is allowed as a *discovery hint* and must be checked by bot.py;
    unknown pool age and liquidity fail closed to avoid flooding with new pools.
    """
    stamp = time.time() * 1000 if now_ms is None else now_ms
    hints: dict[str, float] = {}

    def reject(reason: str) -> None:
        if rejections is not None:
            rejections[reason] += 1

    if not isinstance(pairs, list):
        return hints
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("chainId") != "solana":
            reject("other_chain")
            continue
        base = (pair.get("baseToken") or {}).get("address")
        quote = (pair.get("quoteToken") or {}).get("address")
        dex = str(pair.get("dexId") or "").lower().replace("-", "").replace("_", "")
        if (not isinstance(base, str) or not MINT_PATTERN.fullmatch(base)
                or base == sol_mint or quote != sol_mint or dex not in supported_dexes):
            reject("not_supported_sol_pair")
            continue
        try:
            liq = float((pair.get("liquidity") or {}).get("usd"))
            created = float(pair.get("pairCreatedAt"))
        except (ValueError, TypeError):
            reject("missing_liquidity_or_age")
            continue
        if not math.isfinite(liq) or liq < min_liquidity_usd:
            reject("below_discovery_liquidity")
            continue
        # Reject future timestamps and pairs too young for the established strategy.
        age = (stamp - created) / 60000.0
        if not math.isfinite(age) or age < min_pair_age_minutes or age > 3650 * 24 * 60:
            reject("new_or_invalid_age")
            continue
        try:
            value = pair.get("marketCap")
            if value is None:
                value = pair.get("fdv")
            cap = float(value) if value is not None else None
        except (TypeError, ValueError):
            cap = None
        if cap is not None and (not math.isfinite(cap) or cap < min_cap_usd or cap > max_cap_usd):
            reject("cap_outside_range")
            continue
        volume = pair.get("volume") or {}
        try:
            v1h = float(volume.get("h1") or 0)
        except (TypeError, ValueError):
            v1h = 0
        if not math.isfinite(v1h) or v1h < 0:
            v1h = 0
        # Ranking only. Final market and momentum requirements stay untouched.
        score = min(liq, 250000) + min(v1h, 250000) * 2
        hints[base] = max(score, hints.get(base, 0))
    return hints

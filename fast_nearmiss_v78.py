"""Strict same-pool eligibility for bounded PAPER-only near-miss rechecks.

This helper does not authorize a buy, relax entry conditions, or estimate
profit. It is called only AFTER a fresh primary SOL-pool fetch fails final
momentum screening. No stale or promotional-only token can qualify.
"""
from __future__ import annotations
import math
import time

MOMENTUM_NEAR_REASONS = frozenset({
    'momentum_price_change', 'momentum_volume', 'momentum_volume_1h',
    'fast_price_momentum', 'fast_volume_depth', 'fast_volume_pace',
    'fast_buy_pressure', 'momentum_buy_sell_ratio',
    'momentum_volume_liquidity_ratio',
})

def _n(v, default=0.0):
    try: x=float(v)
    except (ValueError,TypeError,OverflowError): return default
    return x if math.isfinite(x) else default

def is_verified_fast_nearmiss(pair: dict, reason: str, *, sol_mint: str,
                              dexes: set[str], minimum_age_minutes: float = 30,
                              minimum_liquidity: float = 15000,
                              min_cap: float = 100000,
                              max_cap: float = 250000000,
                              now_ms: float | None = None) -> bool:
    """Return True ONLY for recent activity on an individually safe SOL pool.

    Favors established-cap near-misses; smaller $100K-$500K pools retain
    their normal safety backoff because their bespoke rules are more strict.
    """
    if reason not in MOMENTUM_NEAR_REASONS or not isinstance(pair,dict):
        return False
    if pair.get('chainId')!='solana' or (pair.get('quoteToken') or {}).get('address')!=sol_mint:
        return False
    if (pair.get('dexId') or '').lower() not in {x.lower() for x in dexes}:
        return False
    mint=(pair.get('baseToken') or {}).get('address')
    if not mint or mint==sol_mint or _n(pair.get('priceUsd'))<=0:
        return False
    liq=_n((pair.get('liquidity') or {}).get('usd'))
    cap=_n(pair.get('marketCap'),-1)
    # Only stronger, adequately capitalized near misses are fast-retried.
    if liq<max(25000,minimum_liquidity) or cap<max(500000,min_cap) or cap>max_cap:
        return False
    created=_n(pair.get('pairCreatedAt'),-1)
    moment_ms = time.time()*1000 if now_ms is None else float(now_ms)
    age_minutes = (moment_ms-created)/60000
    if created<=0 or not minimum_age_minutes <= age_minutes < 24*60*365:
        return False
    # Independently refuses young/future pools, even if the caller errs.
    ch=pair.get('priceChange') or {}
    tx=(pair.get('txns') or {}).get('m5') or {}
    vol=pair.get('volume') or {}
    ch5=_n(ch.get('m5'),float('nan'))
    ch1=_n(ch.get('h1'),float('nan'))
    v5=_n(vol.get('m5'))
    h1=_n(vol.get('h1'))
    buys=_n(tx.get('buys'))
    sells=_n(tx.get('sells'))
    return (0.5<=ch5<=16 and ch1>=2 and v5>=750 and h1>=6000
            and buys>=8 and sells>=1 and buys/sells>=1.08
            and buys+sells>=12 and v5/liq>=0.015)

"""Fast, bounded re-observation of a VERIFIED supported SOL-pool 5m near miss.

No one-hour requirement. Never an entry authorization. The full market and
pre-buy filters independently run again on every rescan.
"""
from __future__ import annotations
import math
import time

REASONS=frozenset({'momentum_volume','momentum_price_change',
                   'momentum_buy_sell_ratio','momentum_volume_liquidity_ratio',
                   'fast_volume_depth','fast_price_momentum'})

def _n(v, default=float('nan')):
    try: x=float(v)
    except (TypeError,ValueError,OverflowError): return default
    return x if math.isfinite(x) else default

def short_flow_nearmiss(pair: dict, reason: str, *, sol_mint: str,
                        dexes: set[str], now_ms: float | None = None,
                        min_age_minutes: float = 12) -> bool:
    if reason not in REASONS or not isinstance(pair,dict):return False
    if pair.get('chainId')!='solana' or (pair.get('quoteToken') or {}).get('address')!=sol_mint:return False
    if str(pair.get('dexId') or '').lower() not in {str(x).lower() for x in dexes}:return False
    if _n(pair.get('priceUsd'))<=0 or not (pair.get('baseToken') or {}).get('address'):return False
    stamp = time.time()*1000 if now_ms is None else now_ms
    age=(stamp-_n(pair.get('pairCreatedAt')))/60000
    cap=_n(pair.get('marketCap'));liq=_n((pair.get('liquidity') or {}).get('usd'))
    if not (min_age_minutes<=age<525600 and 100000<=cap<=250000000 and liq>=25000):return False
    v5=_n((pair.get('volume') or {}).get('m5'))
    m5=_n((pair.get('priceChange') or {}).get('m5'))
    tx=(pair.get('txns') or {}).get('m5') or {}
    buys=_n(tx.get('buys'));sells=_n(tx.get('sells'))
    return (500<=v5 and .5<=m5<=13 and buys>=8 and sells>=3 and
            buys+sells>=14 and buys/sells>=1.16 and v5/liq>=.008)

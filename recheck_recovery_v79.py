"""v7.9 bounded PAPER-only follow-up eligibility on a single fresh SOL pool.

A follow-up is strictly an additional market-data observation. No function
here may approve an entry or override the final market or trade safeguards.
"""
from __future__ import annotations
import math
import time

DATA_REASONS = frozenset({'trend_m5_missing','trend_h1_missing',
                          'trend_h6_missing','trend_h24_missing'})
NEAR_REASONS = frozenset({
    'momentum_price_change','momentum_volume','momentum_volume_1h',
    'momentum_buy_sell_ratio','momentum_volume_liquidity_ratio',
    'fast_price_momentum','fast_volume_depth','fast_volume_pace','fast_buy_pressure',
})

def number(v,default=0.0):
    try: x=float(v)
    except (TypeError,ValueError,OverflowError): return default
    return x if math.isfinite(x) else default

def followup_decision(pair: dict, reason: str, *, sol_mint: str, dexes,
                      min_age_minutes=30, min_liquidity=15000,
                      min_cap=100000, max_cap=250000000,
                      now_ms=None, require_hour: bool = True) -> tuple[str,str]:
    """Return ('data'|'hot'|'none', diagnostic). Always require a *same-pool* check."""
    if reason not in DATA_REASONS | NEAR_REASONS:
        return 'none','reason_not_retriable'
    if not isinstance(pair,dict): return 'none','missing_primary_pool'
    if pair.get('chainId')!='solana' or (pair.get('quoteToken') or {}).get('address')!=sol_mint:
        return 'none','not_sol_quoted_primary_pool'
    if (pair.get('dexId') or '').lower() not in {str(d).lower() for d in dexes}:
        return 'none','unsupported_route'
    if not (pair.get('baseToken') or {}).get('address') or number(pair.get('priceUsd'))<=0:
        return 'none','unpriced_primary_pool'
    age=(float(time.time()*1000 if now_ms is None else now_ms) - number(pair.get('pairCreatedAt'),-1))/60000
    if not min_age_minutes<=age<525600:
        return 'none','age_not_verified'
    liq=number((pair.get('liquidity') or {}).get('usd'))
    cap=number(pair.get('marketCap'),-1)
    if cap<=0 or cap<min_cap or cap>max_cap:
        return 'none','market_cap_not_verified'
    if liq<min_liquidity:
        return 'none','insufficient_same_pool_liquidity'
    if reason in NEAR_REASONS and liq<max(20000,min_liquidity):
        return 'none','insufficient_nearmiss_liquidity'
    # Never accelerate FDV-only and extra-fragile microcap pools.
    if cap<350000 or (cap<500000 and liq<25000):
        return 'none','small_cap_followup_safety'
    tx=(pair.get('txns') or {}).get('m5') or {}
    vols=pair.get('volume') or {}; changes=pair.get('priceChange') or {}
    buys=number(tx.get('buys')); sells=number(tx.get('sells'))
    v5=number(vols.get('m5')); v1=number(vols.get('h1'))
    ch5=number(changes.get('m5'),float('nan'))
    ch1=number(changes.get('h1'),float('nan'))
    if (sells<1 or buys<7 or buys+sells<11 or buys/sells<1.08):
        return 'none','weak_buying_activity'
    if (v5<650 or (require_hour and v1<5000) or v5/liq<0.012):
        return 'none','weak_volume_activity'
    if reason in DATA_REASONS:
        # Only the named trend field may be absent; at least one distinct
        # short-term change observation must demonstrate positive direction.
        key=reason[len('trend_'):-len('_missing')]
        if changes.get(key) is not None and math.isfinite(number(changes.get(key),float('nan'))):
            return 'none','missing_field_already_present'
        if reason=='trend_m5_missing':
            if not math.isfinite(ch1) or ch1<2 or ch1>100:
                return 'none','no_verified_positive_hour_trend'
        else:
            if not math.isfinite(ch5) or not .5<=ch5<=12:
                return 'none','no_verified_positive_five_minute_trend'
        return 'data','missing_trend_recovery'
    if not math.isfinite(ch5) or (require_hour and not math.isfinite(ch1)):
        return 'none','required_trend_missing'
    if not 0.35<=ch5<=13 or (require_hour and not 1.5<=ch1<=100):
        return 'none','not_active_but_safe_nearmiss'
    return 'hot','slightly_below_fast_entry_threshold'

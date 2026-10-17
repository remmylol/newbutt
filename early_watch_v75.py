"""Cross-feed early observation of liquid young/near-cap Solana pools.

Hints from GeckoTerminal or DexScreener search/profile are never entry
permission. A due watch must re-fetch its pair and satisfy ALL existing entry
checks, including the 30-minute age and verified market-cap requirements.
"""
from __future__ import annotations
from datetime import datetime
import math
import time
from rolling_discovery import MINT_PATTERN


def _num(v, fallback=-1.0):
    try:
        n = float(v)
        return n if math.isfinite(n) else fallback
    except (ValueError, TypeError, OverflowError):
        return fallback


def _offer(result, mint, *, age, cap, liquidity, change5, change1, min_age, min_cap, max_cap, now):
    if not isinstance(mint, str) or not MINT_PATTERN.fullmatch(mint):
        return
    # Require actual observable SOL pool, positive momentum and bounded age.
    if not (5 <= age <= 180 and liquidity >= 25000 and
            0 < change5 <= 30 and change1 > 0 and
            0.6 * min_cap <= cap <= max_cap):
        return
    young = age < min_age
    near_cap = cap < min_cap
    if not (young or near_cap):
        return
    wait = max(0, min_age - age) * 60 + 15 if young else 600
    hint = {'due':now+wait, 'reason':'age_watch' if young else 'cap_watch',
            'age_minutes':round(age,2), 'indicative_cap_usd':cap}
    previous = result.get(mint)
    if previous is None or hint['due'] < previous['due']:
        result[mint] = hint


def from_dex_pairs(pairs, *, sol_mint, supported_dexes, min_age, min_cap, max_cap,
                   now=None):
    stamp = time.time() if now is None else now
    result = {}
    if not isinstance(pairs, list):
        return result
    routes = {d.lower().replace('-', '').replace('_', '') for d in supported_dexes}
    for p in pairs[:200]:
        if not isinstance(p, dict) or p.get('chainId') != 'solana':
            continue
        base = (p.get('baseToken') or {}).get('address')
        quote = (p.get('quoteToken') or {}).get('address')
        dex = str(p.get('dexId') or '').lower().replace('-', '').replace('_', '')
        if base == sol_mint or quote != sol_mint or dex not in routes:
            continue
        created = _num(p.get('pairCreatedAt'))
        liq = _num((p.get('liquidity') or {}).get('usd'))
        cap = _num(p.get('marketCap'))
        if cap <= 0:
            cap = _num(p.get('fdv'))  # observation only; final buy must have marketCap
        changes = p.get('priceChange') or {}
        if _num(p.get('priceUsd')) <= 0 or created <= 0 or created > stamp*1000:
            continue
        _offer(result, base, age=(stamp*1000-created)/60000, cap=cap,
               liquidity=liq, change5=_num(changes.get('m5')),
               change1=_num(changes.get('h1')),
               min_age=min_age, min_cap=min_cap, max_cap=max_cap, now=stamp)
    return result


def from_gecko_pools(payload, *, sol_mint, supported_dexes, min_age, min_cap, max_cap,
                     now=None):
    stamp = time.time() if now is None else now
    result = {}
    if not isinstance(payload, dict):
        return result
    included = {item.get('id'):item for item in payload.get('included', [])
                if isinstance(item, dict) and isinstance(item.get('id'), str)}
    routes = {d.lower().replace('-', '').replace('_', '') for d in supported_dexes}
    def address(relationship):
        raw = ((relationship or {}).get('data') or {}).get('id', '')
        entry = included.get(raw) or {}
        return (entry.get('attributes') or {}).get('address') or (raw[7:] if raw.startswith('solana_') else '')
    for pool in payload.get('data', [])[:100]:
        if not isinstance(pool, dict):
            continue
        attributes = pool.get('attributes') or {}
        rel = pool.get('relationships') or {}
        dex = str(((rel.get('dex') or {}).get('data') or {}).get('id') or '').lower()
        if dex.startswith('solana_'):
            dex = dex[7:]
        if dex.replace('-', '').replace('_', '') not in routes:
            continue
        base = address(rel.get('base_token'))
        if address(rel.get('quote_token')) != sol_mint or base == sol_mint:
            continue
        try:
            created = datetime.fromisoformat(str(attributes['pool_created_at']).replace('Z', '+00:00'))
            if created.tzinfo is None:
                continue
            age = (stamp-created.timestamp())/60
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
        change = attributes.get('price_change_percentage') or {}
        cap = _num(attributes.get('market_cap_usd'))
        if cap <= 0:
            cap = _num(attributes.get('fdv_usd'))
        if _num(attributes.get('base_token_price_usd')) <= 0:
            continue
        _offer(result, base, age=age, cap=cap,
               liquidity=_num(attributes.get('reserve_in_usd')),
               change5=_num(change.get('m5')), change1=_num(change.get('h1')),
               min_age=min_age, min_cap=min_cap, max_cap=max_cap, now=stamp)
    return result

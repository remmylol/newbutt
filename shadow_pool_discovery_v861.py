"""Recent GeckoTerminal pool hints for the active 5-12 minute shadow strategy.

These are untrusted discovery hints ONLY; DexScreener same-pool quotes decide buys.
"""
from __future__ import annotations
from datetime import datetime, timezone
import math
from rolling_discovery import MINT_PATTERN
from quality_pool_discovery import _token_address


def early_pool_hints(payload: dict, *, sol_mint: str, supported_dexes, now: float | None = None) -> dict[str, float]:
    """Mint -> pool birth epoch seconds, restricted to recent supported SOL pools.

    Accept 3-12 minute-old pools to allow an early observation before the 5m
    entry boundary; NEVER infer an absent pool creation timestamp.
    """
    if not isinstance(payload, dict):
        return {}
    stamp = datetime.now(timezone.utc).timestamp() if now is None else now
    included = {v.get('id'):v for v in payload.get('included',[]) if isinstance(v,dict)}
    supported = {str(d).lower().replace('_','').replace('-','') for d in supported_dexes}
    result = {}
    for pool in payload.get('data',[]):
        if not isinstance(pool,dict): continue
        a=pool.get('attributes') or {}
        rel=pool.get('relationships') or {}
        dex=str(((rel.get('dex') or {}).get('data') or {}).get('id','')).lower()
        if dex.startswith('solana_'): dex=dex[7:]
        if dex.replace('_','').replace('-','') not in supported: continue
        base=_token_address(rel.get('base_token'),included)
        quote=_token_address(rel.get('quote_token'),included)
        if not isinstance(base,str) or not MINT_PATTERN.fullmatch(base) or base==sol_mint or quote!=sol_mint: continue
        try:
            created=datetime.fromisoformat(str(a.get('pool_created_at')).replace('Z','+00:00'))
            born=created.timestamp()
            liquidity=float(a.get('reserve_in_usd') or 0)
            price=float(a.get('base_token_price_usd') or 0)
        except (ValueError,TypeError,OverflowError): continue
        if not (math.isfinite(born) and math.isfinite(liquidity) and math.isfinite(price)): continue
        age=(stamp-born)/60
        if not (3 <= age < 12 and liquidity >= 15000 and price>0): continue
        cap_raw=a.get('market_cap_usd') or a.get('fdv_usd')
        try: cap=float(cap_raw) if cap_raw is not None else None
        except (ValueError,TypeError,OverflowError): cap=None
        if cap is not None and math.isfinite(cap) and 0<cap<35000: continue
        result[base]=max(result.get(base,0),born)
    return result

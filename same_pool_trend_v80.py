"""Read-only secondary trend observation, NEVER substitute for actual entry data."""
import math
from datetime import datetime, timezone

def number(v):
    try: x=float(v)
    except (TypeError, ValueError, OverflowError): return None
    return x if math.isfinite(x) else None

def gecko_same_pool_observation(pair, payload, *, sol_mint):
    """Crosscheck pool identity, mint orientation, quote and price.

    Only used to decide observation scheduling. Never merged into an entry
    candidate; primary DexScreener must independently supply full trade data.
    """
    if not isinstance(payload,dict) or not isinstance(pair,dict):return None,'invalid_data'
    pool=pair.get('pairAddress');mint=(pair.get('baseToken') or {}).get('address')
    if not pool or not mint or (pair.get('quoteToken') or {}).get('address')!=sol_mint:return None,'invalid_primary_pool'
    item=payload.get('data') or {};attr=item.get('attributes') or {};rel=item.get('relationships') or {}
    base=((rel.get('base_token') or {}).get('data') or {}).get('id')
    quote=((rel.get('quote_token') or {}).get('data') or {}).get('id')
    if (item.get('id')!='solana_'+pool or attr.get('address')!=pool or
        base!='solana_'+mint or quote!='solana_'+sol_mint):return None,'pool_or_mint_mismatch'
    p1=number(pair.get('priceUsd'));p2=number(attr.get('base_token_price_usd'))
    if p1 is None or p2 is None or p1<=0 or p2<=0 or abs(p2/p1-1)>.03:return None,'price_disagreement'
    created_ms=None
    created=attr.get('pool_created_at')
    if isinstance(created,str):
        try:
            created_ms=int(datetime.fromisoformat(created.replace('Z','+00:00')).timestamp()*1000)
        except (ValueError,OverflowError):pass
    changes=attr.get('price_change_percentage') or {}
    m5=number(changes.get('m5'));h1=number(changes.get('h1'))
    v5=number((attr.get('volume_usd') or {}).get('m5'))
    tx=(attr.get('transactions') or {}).get('m5') or {}
    buys=number(tx.get('buys'));sells=number(tx.get('sells'))
    if m5 is None and h1 is None and created_ms is None:return None,'secondary_trend_and_age_unavailable'
    return {'m5':m5,'h1':h1,'created_ms':created_ms,'v5':v5,
            'buys':buys,'sells':sells,'pool':pool},'independent_observation_only'

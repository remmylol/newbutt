"""v8.1 paper-only small-cap fast-momentum qualification and audit helpers.

These are market-data heuristics, not evidence of executable liquidity or guaranteed profit.
"""
from __future__ import annotations
import math

VERSION = 'v8.1'


def n(value, default=0.0):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (ValueError, TypeError, OverflowError):
        return default


def early_volume_exception(pair: dict, *, age_minutes: float, cap_usd: float, cap_source: str) -> tuple[bool, str]:
    """Use verified SAME-POOL 5m flow in lieu of a mature 1h-volume FLOOR.

    NEVER ignores the observed hourly data entirely, and never approves a
    candidate by itself. Caller must still pass route, current liquidity,
    price/age, trend, prebuy snapshot, sellability and account gates.
    """
    if cap_source != 'marketCap' or not 35000 <= cap_usd < 250000 or not 12 <= age_minutes < 60:
        return False, 'tier_or_age_outside_exception'
    q = pair.get('quoteToken') or {}
    # Same SOL quote is checked again by the caller's route verification.
    if str(q.get('address', '')) != 'So11111111111111111111111111111111111111112':
        return False, 'non_sol_quote'
    liq = n((pair.get('liquidity') or {}).get('usd'))
    vol = pair.get('volume') or {}
    v5,v1 = n(vol.get('m5')),n(vol.get('h1'))
    txn = (pair.get('txns') or {}).get('m5') or {}
    buys,sells = n(txn.get('buys')),n(txn.get('sells'))
    chg = pair.get('priceChange') or {}
    c5,c1 = n(chg.get('m5'),-999),n(chg.get('h1'),-999)
    micro = cap_usd < 100000
    if (liq < (18000 if micro else 22000) or
        v5 < (1800 if micro else 1450) or
        v1 < max(5000 if micro else 4500, v5 * 1.10) or
        buys+sells < (20 if micro else 16) or sells < 3 or
        buys/sells < (1.30 if micro else 1.22) or
        not 0.8 <= c5 <= 14 or c1 < 5.5 or c1 > 100 or v5/liq > 3.5):
        return False, 'insufficient_recent_flow_or_safety'
    return True, 'verified_5m_flow_hourly_volume_exception'


def confirmed_profit_lock(observations: list, *, now: float, opened_at: float,
                          arm_net_pct: float=3.5, giveback_pct: float=1.5,
                          floor_net_pct: float=0.75, confirm_seconds: float=15.0,
                          max_quote_age_seconds: float=25.0) -> tuple[bool, dict]:
    """Primary-pool-only; two distinct falling observations after an earlier peak."""
    from fast_momentum_v76 import _valid_observations
    valid = _valid_observations(observations, now, opened_at, window=3600)
    if now-opened_at < 60 or len(valid)<3 or now-valid[-1][0]>max_quote_age_seconds:
        return False, {'status':'insufficient_primary_observations'}
    peak=max(x[1] for x in valid)
    if peak < arm_net_pct:
        return False, {'status':'not_armed', 'peak_net_pct':round(peak,2)}
    idx=max(i for i,x in enumerate(valid) if x[1]==peak)
    if len(valid[idx+1:])<2:
        return False, {'status':'waiting_confirmations', 'peak_net_pct':round(peak,2)}
    a,b=valid[-2:]
    target=peak-giveback_pct
    confirmed=(b[1]>=floor_net_pct and b[1]<=target and a[1]<=target and
               b[0]-a[0]>=confirm_seconds and b[2]<a[2] and
               (a[2]-b[2])/a[2]>=0.0002 and idx < len(valid)-2)
    return confirmed, {'status':'confirmed' if confirmed else 'waiting_confirmations',
                       'peak_net_pct':round(peak,2), 'target_net_pct':round(target,2),
                       'current_net_pct':round(b[1],2)}


def update_exit_shadows(metrics: dict, observations: list, *, now: float, opened_at: float) -> dict:
    """Counterfactual signal timestamps only; does not make or assume a trade."""
    shadows=metrics.setdefault('v81_exit_shadows', {
        'quick_4pct': {'status':'open'}, 'trail_3p5_1p5': {'status':'open'}})
    from fast_momentum_v76 import _valid_observations
    valid=_valid_observations(observations,now,opened_at,window=3600)
    if not valid or now-valid[-1][0]>25:
        return shadows
    latest=valid[-1]
    quick=shadows.setdefault('quick_4pct',{'status':'open'})
    if quick.get('status')=='open':
        first_hit=next((point for point in valid if point[1]>=4.0),None)
        if first_hit is not None:
            quick.update(status='signal_observed', net_pct=round(first_hit[1],3),
                         elapsed_s=round(first_hit[0]-opened_at,1))
    trailing=shadows.setdefault('trail_3p5_1p5',{'status':'open'})
    if trailing.get('status')=='open':
        ok,info=confirmed_profit_lock(valid,now=now,opened_at=opened_at)
        if ok:
            trailing.update(status='signal_observed',net_pct=round(latest[1],3),elapsed_s=round(now-opened_at,1))
    return shadows


def summarize_version_trades(trades: list, *, version='v8.1', limit=100) -> dict:
    """Only actually CLOSED trades with explicitly recorded version are counted."""
    eligible=[t for t in trades if (t.get('paper_metrics') or {}).get('strategy_version')==version
              and t.get('execution') in ('paper_estimate_only','paper_quote_backed_estimate','paper_writeoff_no_sale')]
    eligible=eligible[-limit:]
    profits=[n(t.get('pnl_usd')) for t in eligible]
    winners=[p for p in profits if p>0]
    losses=[p for p in profits if p<=0]
    from collections import Counter
    return {'version':version,'trades':len(eligible),'wins':len(winners),'losses':len(losses),
            'win_rate_pct':round(100*len(winners)/len(eligible),1) if eligible else None,
            'net_pnl_usd':round(sum(profits),3),
            'avg_win_usd':round(sum(winners)/len(winners),3) if winners else None,
            'avg_loss_usd':round(sum(losses)/len(losses),3) if losses else None,
            'expectancy_usd':round(sum(profits)/len(profits),3) if profits else None,
            'reasons':dict(Counter(str(t.get('reason','unknown')) for t in eligible)),
            'by_tier':dict(Counter(str((t.get('paper_metrics') or {}).get('entry_cap_tier','unknown')) for t in eligible))}

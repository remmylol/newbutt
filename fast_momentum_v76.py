"""v7.6 paper-only fast-momentum selection and confirmed risk exits.

Source data is indicative DexScreener/GeckoTerminal market metadata, never a
transaction quote. No claim of predicting profitable price movement.
"""
from __future__ import annotations
import math


def _n(value, default=0.0):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (ValueError, TypeError, OverflowError):
        return default


def fast_mover_gate(candidate: dict, *, min_change_5m=0.8, min_change_1h=4.0,
                    min_volume_5m=1400.0, min_volume_liq=0.02,
                    min_hour_pace=0.055, min_buy_sell_ratio=1.14,
                    min_transactions_5m=14) -> tuple[bool, str, float]:
    """Checks sustained participation instead of relying on price % alone.

    5m/hour pace measures current five-minute volume against preceding hour,
    not historical acceleration. Call it an activity proxy, not a trend model.
    """
    c5, c1 = _n(candidate.get('change_m5')), _n(candidate.get('change_h1'))
    v5, v1 = _n(candidate.get('m5_volume')), _n(candidate.get('h1_volume'))
    liq = _n(candidate.get('liquidity'))
    buys, sells = _n(candidate.get('m5_buys')), _n(candidate.get('m5_sells'))
    if c5 < min_change_5m or c1 < min_change_1h:
        return False, 'fast_price_momentum', 0.0
    if v5 < min_volume_5m or liq <= 0 or v5 / liq < min_volume_liq:
        return False, 'fast_volume_depth', 0.0
    if v1 <= 0 or v5 / v1 < min_hour_pace:
        return False, 'fast_volume_pace', 0.0
    if buys+sells < min_transactions_5m or sells <= 0 or buys / sells < min_buy_sell_ratio:
        return False, 'fast_buy_pressure', 0.0
    if c5 > 16 or c1 > 100 or v5 / liq > 5.0:
        return False, 'fast_overheated_or_anomalous', 0.0
    # Bounded preference, not trade approval; prevents a massive volume outlier
    # or liquidity alone from monopolizing the candidate ordering.
    rank = (min(40, c5 * 5) + min(20, c1 / 2) +
            min(25, 200 * (v5 / liq)) + min(15, 30 * max(0, buys/(buys+sells)-0.5)))
    return True, 'fast_qualified', round(rank, 2)


def confirmed_profit_trail(observations: list, *, now: float, opened_at: float,
                           arm_net_pct=5.0, giveback_pct=2.5,
                           min_hold_seconds=45.0, confirm_seconds=15.0,
                           max_quote_age_seconds=25.0) -> tuple[bool, dict]:
    """Verify two distinct primary-pool prices below a profit peak's trail.

    Observations: [timestamp, net_return_pct, primary_market_price]. Requires a
    peak before both confirmations; fallback prices must never be included.
    """
    valid = _valid_observations(observations, now, opened_at, window=3600)
    if now - opened_at < min_hold_seconds or len(valid) < 3 or now-valid[-1][0] > max_quote_age_seconds:
        return False, {'status':'insufficient_fresh_primary_observations'}
    peak = max(x[1] for x in valid)
    if peak < arm_net_pct:
        return False, {'status':'not_armed', 'peak_net_pct':round(peak,2)}
    peak_idx = max(i for i,x in enumerate(valid) if x[1] == peak)
    floor = peak-giveback_pct
    below = [x for x in valid[peak_idx+1:] if x[1] <= floor]
    if len(below) < 2:
        return False, {'status':'trail_waiting_confirmation','peak_net_pct':round(peak,2),'floor_net_pct':round(floor,2)}
    first, last = below[-2], below[-1]
    confirmed = (last == valid[-1] and last[0]-first[0] >= confirm_seconds
                 and abs(last[2]-first[2]) / max(abs(first[2]),1e-12) >= 0.0002)
    return confirmed, {'status':'confirmed' if confirmed else 'trail_waiting_confirmation',
                       'peak_net_pct':round(peak,2),'current_net_pct':round(last[1],2),
                       'floor_net_pct':round(floor,2),'confirmation_seconds':round(last[0]-first[0],1)}


def confirmed_loss_circuit(observations: list, *, now: float, opened_at: float,
                           entry_market_price: float, net_loss_pct=7.0,
                           min_market_drop_pct=2.5, min_hold_seconds=90.0,
                           confirm_seconds=25.0, max_quote_age_seconds=25.0) -> tuple[bool, dict]:
    """Early damage-control for persistently losing positions, not a one-tick SL."""
    valid = _valid_observations(observations, now, opened_at, window=300)
    if now-opened_at < min_hold_seconds or len(valid)<2 or now-valid[-1][0]>max_quote_age_seconds:
        return False, {'status':'insufficient_fresh_primary_observations'}
    if entry_market_price <= 0:
        return False, {'status':'entry_price_unavailable'}
    drop = 100*(1-valid[-1][2]/entry_market_price)
    losing = [x for x in valid if x[1] <= -net_loss_pct and
              100*(1-x[2]/entry_market_price) >= min_market_drop_pct]
    if len(losing)<2 or losing[-1] != valid[-1]:
        return False, {'status':'loss_not_confirmed', 'entry_drop_pct':round(drop,2)}
    a,b=losing[-2:]
    confirmed = b[0]-a[0]>=confirm_seconds and abs(b[2]-a[2])/max(abs(a[2]),1e-12)>=0.0002
    return confirmed, {'status':'confirmed' if confirmed else 'loss_waiting_confirmation',
                       'entry_drop_pct':round(drop,2),'current_net_pct':round(b[1],2),
                       'confirmation_seconds':round(b[0]-a[0],1)}


def _valid_observations(observations, now, opened_at, *, window):
    out=[]
    for r in observations[-240:]:
        if not isinstance(r,(list,tuple)) or len(r)!=3:
            continue
        t,net,price=(_n(r[0], float('nan')),_n(r[1],float('nan')),_n(r[2],float('nan')))
        if all(math.isfinite(x) for x in (t,net,price)) and price>0 and max(opened_at,now-window)<=t<=now:
            if out and t<=out[-1][0]:
                continue
            out.append([t,net,price])
    return out

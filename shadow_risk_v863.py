"""Read-only shadow-paper liquidity risk calculations (never real execution)."""
from __future__ import annotations
from collections import defaultdict
import math


def prebuy_depth_check(*, price: float, liquidity: float, stake: float = 2.0,
                       round_trip_cost_pct: float = 6.0, max_impact_pct: float = 2.0) -> tuple[bool, str, float]:
    """Model sell impact for a full $stake buy. A mathematical estimate, NOT a swap quote."""
    if not all(math.isfinite(x) for x in (price, liquidity, stake)) or min(price, liquidity, stake) <= 0:
        return False, 'missing_primary_price_or_sell_depth', 0.0
    if liquidity < 15_000:
        return False, 'primary_sell_pool_below_15k', 0.0
    gross = stake / (1 + round_trip_cost_pct / 200.0)
    quote_depth = liquidity * 0.5
    impact = 100 * gross / (gross + quote_depth)
    if impact > max_impact_pct:
        return False, 'modeled_full_stake_sell_impact_high', impact
    return True, 'indicative_full_stake_depth_pass', impact


def pool_risk_signal(entry_liq: float, peak_liq: float, current_liq: float,
                     *, min_liq: float = 15_000.0, drop_pct: float = 40.0,
                     warning_multiplier: float = 1.2) -> tuple[bool, str, float]:
    """Only invoke with a fresh, same-pool primary price AND sellable quote."""
    if not math.isfinite(current_liq) or current_liq <= 0:
        return False, 'liquidity_unverified_no_exit', 0.0
    baseline = max(entry_liq, peak_liq, current_liq)
    decline = max(0., (baseline-current_liq) / baseline * 100) if baseline > 0 else 0.
    if current_liq < min_liq * warning_multiplier:
        return True, 'liquidity_near_minimum', decline
    if decline >= drop_pct:
        return True, 'confirmed_pool_liquidity_drop', decline
    return False, 'liquidity_healthy', decline


def classify_unavailable_exit(reason: str, evidence: dict | None = None) -> str:
    """Don't label provider outages, stale prices, or missing routes as proven rugs."""
    evidence = evidence or {}
    tag = str(evidence.get('reason') or '')
    if tag in ('pinned_pool_zero_liquidity','pinned_pool_near_zero_liquidity'):
        return 'observed_pinned_pool_liquidity_depleted'
    if tag == 'pinned_pool_missing_no_liquid_alternative':
        return 'pinned_pool_missing_from_provider_unconfirmed'
    if 'fallback' in reason or 'missing' in reason or 'stale' in reason:
        return 'quote_or_market_data_unavailable'
    if 'impact' in reason or 'depth' in reason:
        return 'modeled_exit_depth_unavailable'
    return 'unverified_exit_other'


def liquidity_trade_breakdown(trades: list[dict], *, version: str = 'v8.6.3_shadow') -> dict:
    """Summarize CLOSED new-version trades in entry liquidity and flow tiers."""
    groups = defaultdict(list)
    for trade in trades:
        metrics = trade.get('paper_metrics') or {}
        if metrics.get('strategy_version') != version:
            continue
        liq = float(metrics.get('entry_pool_liquidity_usd') or 0)
        vol = float(metrics.get('entry_5m_volume_usd') or 0)
        ltier = 'unknown' if liq <= 0 else '<25k' if liq < 25000 else '25-50k' if liq < 50000 else '50-100k' if liq < 100000 else '100k+'
        vtier = 'unknown' if vol <= 0 else '<5k' if vol < 5000 else '5-25k' if vol < 25000 else '25k+'
        groups[f'liquidity:{ltier}'].append(trade)
        groups[f'volume5:{vtier}'].append(trade)
    summary = {}
    for category, rows in sorted(groups.items()):
        summary[category] = {'trades':len(rows), 'net_pnl_usd':round(sum(float(x.get('pnl_usd') or 0) for x in rows),3),
            'writeoffs':sum(x.get('execution') == 'paper_writeoff_no_sale' for x in rows),
            'wins':sum(float(x.get('pnl_usd') or 0) > 0 for x in rows)}
    return summary

"""Conservative, offline-testable PAPER-only risk decisions.

Quotes are indicative provider snapshots, never proof of executable routing or
independent on-chain prices. This module cannot send or authorize live orders.
"""
from __future__ import annotations

import math
from statistics import median


def sustained_loss_exit(history: list, *, now: float, opened_at: float,
                        entry_market_price: float, net_return_pct: float,
                        min_hold_seconds: float = 120.0,
                        min_net_loss_pct: float = 8.5,
                        min_entry_market_drop_pct: float = 4.5,
                        confirmation_seconds: float = 60.0,
                        lookback_seconds: float = 300.0,
                        max_quote_age_seconds: float = 20.0,
                        min_observations: int = 3,
                        min_distinct_change_pct: float = 0.05) -> tuple[bool, dict]:
    """Confirm a *persistent* underwater price, not only a recent sharp drop.

    Does not trigger on modeled transaction costs alone. Requires multiple
    time-separated prices from the primary pool, including distinct values:
    repeatedly returned identical/cached quotes are NOT independent updates.
    Historical point timestamps mark LOCAL receipt times, not source freshness.
    """
    details = {"status": "insufficient_history", "net_pct": round(net_return_pct, 2),
               "entry_market_drop_pct": 0.0, "underwater_seconds": 0.0,
               "observations": 0, "distinct_price_moves": 0}
    if not math.isfinite(entry_market_price) or entry_market_price <= 0:
        details["status"] = "missing_entry_market_price"
        return False, details
    if not math.isfinite(net_return_pct):
        details["status"] = "invalid_net_return"
        return False, details
    if now - opened_at < min_hold_seconds:
        details["status"] = "min_hold"
        return False, details
    if net_return_pct > -min_net_loss_pct:
        details["status"] = "net_loss_not_severe"
        return False, details
    samples = []
    for item in history:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            continue
        try:
            timestamp, price = float(item[0]), float(item[1])
        except (ValueError, TypeError):
            continue
        if (math.isfinite(timestamp) and math.isfinite(price) and price > 0
                and opened_at <= timestamp <= now and now - timestamp <= lookback_seconds):
            samples.append((timestamp, price))
    samples.sort()
    details["observations"] = len(samples)
    if len(samples) < min_observations:
        return False, details
    if now - samples[-1][0] > max_quote_age_seconds:
        details["status"] = "stale_primary_quote"
        return False, details
    limit = entry_market_price * (1 - min_entry_market_drop_pct / 100)
    details["entry_market_drop_pct"] = round((1 - samples[-1][1] / entry_market_price) * 100, 3)
    if samples[-1][1] > limit:
        details["status"] = "entry_market_drop_below_trigger"
        return False, details
    # Latest continuous underwater streak. A recovery above the threshold
    # resets the confirmation clock rather than letting old losses accumulate.
    underwater = []
    for item in reversed(samples):
        if item[1] > limit:
            break
        underwater.append(item)
    underwater.reverse()
    details["underwater_seconds"] = round(underwater[-1][0] - underwater[0][0], 2)
    if len(underwater) < min_observations or details["underwater_seconds"] < confirmation_seconds:
        details["status"] = "underwater_duration_unconfirmed"
        return False, details
    # A string of identical API quotes must not be treated as fresh evidence.
    changed = sum(abs((b[1] / a[1] - 1) * 100) >= min_distinct_change_pct
                  for a, b in zip(underwater, underwater[1:]))
    details["distinct_price_moves"] = changed
    if changed < 2:
        details["status"] = "cached_or_unchanged_price"
        return False, details
    details["status"] = "confirmed_sustained_loss"
    return True, details


def paper_trade_scorecard(trades: list, *, last_n: int = 30) -> dict:
    """Observed closed-paper trade outcomes. Descriptive only; never optimize
    parameters automatically from a tiny history or claim execution quality.
    """
    eligible = []
    for trade in trades:
        if not isinstance(trade, dict) or trade.get("execution") not in (
                "paper_estimate_only", "paper_writeoff_no_sale"):
            continue
        try:
            pnl = float(trade.get("pnl_usd"))
            invested = float(trade.get("invested_usd"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(pnl) and math.isfinite(invested) and invested > 0:
            eligible.append((pnl, invested))
    sample = eligible[-max(1, last_n):]
    if not sample:
        return {"trades": 0, "wins": 0, "win_rate_pct": None,
                "net_pnl_usd": 0.0, "median_return_pct": None}
    wins = sum(pnl > 0 for pnl, _ in sample)
    return {"trades": len(sample), "wins": wins,
            "win_rate_pct": round(100 * wins / len(sample), 1),
            "net_pnl_usd": round(sum(pnl for pnl, _ in sample), 2),
            "median_return_pct": round(median(100 * pnl / invested for pnl, invested in sample), 2)}

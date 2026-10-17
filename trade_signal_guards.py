"""Pure, offline-testable entry/exit guards for indicative PAPER trading.

These are heuristics applied to public prices, NOT expected returns or executable
swap quotes. Do not use them to authorize real trades.
"""
from __future__ import annotations

import math


def entry_cost_headroom(candidate: dict, *, round_trip_cost_pct: float,
                        minimum_buffer_pct: float = 2.0,
                        minimum_5m_change_pct: float = 0.5, require_1h: bool = True) -> tuple[bool, dict]:
    """Demand current momentum evidence beyond modeled fees, not a profit forecast."""
    h1 = float(candidate.get("change_h1", float("nan")))
    m5 = float(candidate.get("change_m5", float("nan")))
    threshold = max(0.0, round_trip_cost_pct) + max(0.0, minimum_buffer_pct)
    headroom = (h1 if require_1h else m5) - max(0.0, round_trip_cost_pct)
    if not math.isfinite(m5) or (require_1h and not math.isfinite(h1)):
        reason = "momentum_missing"
    elif require_1h and h1 < threshold:
        reason = "cost_headroom_insufficient"
    elif m5 < minimum_5m_change_pct:
        reason = "weak_immediate_momentum"
    else:
        reason = "passed"
    return reason == "passed", {"reason": reason, "h1_pct": h1, "m5_pct": m5,
                                 "cost_pct": round_trip_cost_pct, "required_h1_pct": threshold,
                                 "signal_headroom_pct": headroom, "h1_required": require_1h}


def fresh_pool_snapshot(initial: dict, checked: dict | None, *, elapsed_s: float,
                        max_age_s: float = 12.0, max_drop_pct: float = 0.15,
                        max_rise_pct: float = 2.0) -> tuple[bool, dict]:
    """Compare two provider snapshots of the SAME pool; no source timestamp claim.

    Freshness only measures local elapsed time since the provider HTTP response.
    An API can still return cached or delayed market data.
    """
    if checked is None or not isinstance(checked, dict):
        return False, {"reason": "no_fresh_same_pool_snapshot"}
    if elapsed_s < 0 or elapsed_s > max_age_s:
        return False, {"reason": "prebuy_snapshot_expired", "age_s": round(elapsed_s, 2)}
    previous = float(initial.get("price", 0) or 0)
    latest = float(checked.get("price", 0) or 0)
    if not all(math.isfinite(x) and x > 0 for x in (previous, latest)):
        return False, {"reason": "invalid_fresh_price"}
    if checked.get("address") != initial.get("address") or checked.get("pool_address") != initial.get("pool_address"):
        return False, {"reason": "different_pool_or_mint"}
    drift = (latest / previous - 1) * 100
    if drift < -abs(max_drop_pct):
        reason = "price_reversed_during_verification"
    elif drift > max_rise_pct:
        reason = "price_spiked_during_verification"
    else:
        reason = "passed"
    return reason == "passed", {"reason": reason, "drift_pct": round(drift, 4),
                                 "age_s": round(elapsed_s, 2), "checked_price": latest}


def early_loss_reversal(history: list, *, now: float, opened_at: float,
                        net_return_pct: float, min_hold_seconds: float = 30.0,
                        min_net_loss_pct: float = 7.0, min_market_drop_pct: float = 2.0,
                        min_observations: int = 3, lookback_seconds: float = 120.0,
                        max_quote_age_seconds: float = 20.0) -> tuple[bool, dict]:
    """Require material *net* loss AND repeat primary-pool price deterioration.

    A 4% entry/exit haircut alone never triggers this signal. Missing, stale or
    fallback price observations never constitute a confirmed market sell.
    """
    window = sorted((float(t), float(p)) for t, p in history
                    if isinstance(t, (float, int)) and isinstance(p, (float, int))
                    and math.isfinite(p) and p > 0 and 0 <= now - t <= lookback_seconds)
    details = {"status": "insufficient_history", "observations": len(window),
               "net_pct": round(net_return_pct, 2), "market_drop_pct": 0.0}
    if now - opened_at < min_hold_seconds:
        details["status"] = "min_hold"
        return False, details
    if len(window) < min_observations:
        return False, details
    if now - window[-1][0] > max_quote_age_seconds:
        details["status"] = "stale_quote"
        return False, details
    # Check falling price from a pre-existing high AND a recent lower quote.
    peak_i = max(range(len(window) - 1), key=lambda i: window[i][1])
    post = window[peak_i + 1:]
    peak = window[peak_i][1]
    drop = (1 - window[-1][1] / peak) * 100
    details["market_drop_pct"] = round(drop, 3)
    if net_return_pct > -min_net_loss_pct:
        details["status"] = "net_loss_not_severe"
        return False, details
    if drop < min_market_drop_pct:
        details["status"] = "market_drop_below_trigger"
        return False, details
    if len(post) < min_observations - 1:
        details["status"] = "insufficient_post_peak_confirmation"
        return False, details
    # Allow minor quote noise but require a material decline across the most
    # recent observations; a single large outlier is not enough.
    recent = post[-(min_observations - 1):]
    if any(recent[i][1] > recent[i - 1][1] * 1.0025 for i in range(1, len(recent))):
        details["status"] = "not_sustained"
        return False, details
    if recent[-1][1] >= window[peak_i][1]:
        details["status"] = "recovered"
        return False, details
    # Two repeated cached low prices alone are not independent confirmation.
    if recent[-1][1] >= recent[0][1] * (1 - 0.0005):
        details["status"] = "no_recent_continuation"
        return False, details
    details["status"] = "confirmed_loss_reversal"
    return True, details

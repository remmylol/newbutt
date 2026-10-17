"""Bounded OBSERVATION retries for fresh launches awaiting an indexed SOL pool.

No market-cap, liquidity, price, age or trading approval is derived here. The
normal scanner must retrieve and validate all primary market data on each retry.
"""
from __future__ import annotations

VERSION = 'v8.4'
# A PUMP bonding-curve quote isn't a DEX SOL quote. It may migrate later.
POOL_WAIT_REASONS = frozenset({
    'pair_not_indexed', 'supported_pair_liquidity_unavailable',
    'supported_pair_missing_price', 'no_sol_quoted_pair',
    'supported_pair_low_liquidity',
})
MAX_POOL_RECHECKS = 4
# Start near 2m after first eligible DEX lookup, then widen to manage requests.
RETRY_DELAYS_SECONDS = (120, 180, 240, 360)


def followup_delay(reason: str, prior_attempts: int, *, feed_origin: bool) -> int | None:
    """Return a *future observation* delay, never a paper entry signal."""
    if not feed_origin or reason not in POOL_WAIT_REASONS:
        return None
    if not isinstance(prior_attempts, int) or not 0 <= prior_attempts < MAX_POOL_RECHECKS:
        return None
    # When a supported SOL route exists but price/liquidity isn't indexed yet,
    # revisit faster. A different quote asset or absent index has lower
    # near-term probability of becoming eligible; migrations can still jump
    # this queue immediately via the separate event lane.
    kind_bias = {
        'supported_pair_missing_price': 0.65,
        'supported_pair_liquidity_unavailable': 0.80,
        'supported_pair_low_liquidity': 1.00,
        'pair_not_indexed': 1.25,
        'no_sol_quoted_pair': 1.50,
    }
    return max(60,int(RETRY_DELAYS_SECONDS[prior_attempts] * kind_bias[reason]))

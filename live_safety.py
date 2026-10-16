"""Fail-closed, read-only checks for a future supervised live execution path.

This module does not place trades or load private keys.
"""
from __future__ import annotations
import math
from typing import Any

class SafetyError(ValueError):
    pass

MAX_TRADE_USD = 10.0
MAX_POSITIONS = 2
MIN_FEE_RESERVE_USD = 5.0


def checked_sol_price(payload: dict[str, Any]) -> float:
    try:
        price = float(payload['solana']['usd'])
    except (KeyError, TypeError, ValueError) as exc:
        raise SafetyError('missing SOL/USD price; fail closed') from exc
    if not math.isfinite(price) or price <= 0:
        raise SafetyError('invalid SOL/USD price; fail closed')
    return price


def buy_preflight(*, requested_usd: float, sol_usd: float, balance_sol: float,
                  open_positions: int, pending_orders: int, wallet_verified: bool,
                  quote_age_seconds: float, max_quote_age_seconds: float = 30) -> float:
    """Return SOL quantity if eligible, otherwise refuse. No network calls."""
    values = (requested_usd, sol_usd, balance_sol, quote_age_seconds)
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise SafetyError('invalid or missing numerical input')
    if not wallet_verified:
        raise SafetyError('wallet verification required')
    if requested_usd <= 0 or requested_usd > MAX_TRADE_USD:
        raise SafetyError('buy exceeds $10 USD limit or is nonpositive')
    if sol_usd <= 0 or balance_sol < 0:
        raise SafetyError('invalid price or balance')
    if quote_age_seconds < 0 or quote_age_seconds > max_quote_age_seconds:
        raise SafetyError('stale SOL/USD price')
    if not isinstance(open_positions, int) or not 0 <= open_positions < MAX_POSITIONS:
        raise SafetyError('maximum 2 positions reached or invalid position count')
    if pending_orders != 0:
        raise SafetyError('pending or uncertain order; refuse another buy')
    if balance_sol * sol_usd - requested_usd < MIN_FEE_RESERVE_USD:
        raise SafetyError('insufficient SOL after reserving $5 equivalent for fees')
    return requested_usd / sol_usd

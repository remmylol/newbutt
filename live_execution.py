"""Fail-closed bridge between strategy signals, safety policy and journal.

This release is PREVIEW ONLY. It never imports a signer or broadcasts a trade.
No private keys are needed. All live order states are left unchanged.
"""
from __future__ import annotations
from dataclasses import dataclass
from live_safety import buy_preflight, SafetyError
from order_journal import Journal, JournalError

class ExecutionBlocked(RuntimeError):
    pass

@dataclass(frozen=True)
class Preview:
    action: str
    mint: str
    usd: float
    estimated_sol: float
    note: str = 'PREVIEW ONLY — no transaction signed or broadcast'


def preview_buy(*, mint: str, requested_usd: float, sol_usd: float,
                balance_sol: float, open_positions: int, wallet_verified: bool,
                quote_age_seconds: float, journal_dir: str = '/data/live_orders') -> Preview:
    """Validate a hypothetical order against risk limits and unresolved journal records."""
    if not isinstance(mint, str) or not mint.strip():
        raise ExecutionBlocked('missing mint')
    try:
        alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
        value = 0
        for char in mint.strip():
            if char not in alphabet:
                raise ValueError('invalid base58 mint')
            value = value * 58 + alphabet.index(char)
        raw = value.to_bytes((value.bit_length() + 7) // 8, 'big')
        raw = b'\x00' * (len(mint.strip()) - len(mint.strip().lstrip('1'))) + raw
        if len(raw) != 32:
            raise ValueError('mint must decode to 32 bytes')
        pending = Journal(journal_dir).status()['unresolved']
        sol_amount = buy_preflight(requested_usd=requested_usd, sol_usd=sol_usd,
            balance_sol=balance_sol, open_positions=open_positions,
            pending_orders=pending, wallet_verified=wallet_verified,
            quote_age_seconds=quote_age_seconds)
    except (SafetyError, JournalError, ValueError) as exc:
        raise ExecutionBlocked(str(exc)) from exc
    return Preview('buy', mint.strip(), requested_usd, sol_amount)


def execute_buy(*args, **kwargs):
    raise ExecutionBlocked('Live submission intentionally disabled: signed-tx persistence and reconciliation not implemented')


def execute_sell(*args, **kwargs):
    raise ExecutionBlocked('Live selling intentionally disabled: token reconciliation and recovery not implemented')

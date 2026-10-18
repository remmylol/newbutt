"""Fail-closed, read-only Solana sell-route verification for PAPER experiments.

Checks real PumpSwap pool/vault accounts via RPC and requests an exact-pool
Jupiter sell QUOTE. Never signs, sends, simulates, or executes transactions.
A quote is not a sale and not a guaranteed fill. Other DEX layouts unsupported.
"""
from __future__ import annotations

import base64
import logging
import math
from dataclasses import dataclass
from typing import Any

import requests

SOL = 'So11111111111111111111111111111111111111112'
PUMP_AMM_PROGRAM = 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA'
JUPITER_QUOTE_URL = 'https://api.jup.ag/swap/v1/quote'
ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
# Anchor discriminator: sha256(b'account:Pool')[:8]. Do not parse arbitrary
# PumpSwap-program-owned accounts as Pool structs.
PUMP_POOL_DISCRIMINATOR = bytes((241, 154, 109, 4, 17, 177, 109, 188))
log = logging.getLogger(__name__)


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, 'big')
    result = ''
    while n:
        n, remainder = divmod(n, 58)
        result = ALPHABET[remainder] + result
    return ALPHABET[0] * (len(data) - len(data.lstrip(b'\x00'))) + (result or '' if any(data) else '')


def valid_pubkey(value: str) -> bool:
    if not isinstance(value, str) or not 32 <= len(value) <= 44:
        return False
    try:
        n = 0
        for c in value:
            n = n * 58 + ALPHABET.index(c)
        leading = len(value) - len(value.lstrip('1'))
        return leading + (n.bit_length() + 7) // 8 == 32
    except ValueError:
        return False


@dataclass(frozen=True)
class PoolSnapshot:
    pool: str
    mint: str
    base_vault: str
    quote_vault: str
    base_raw: int
    quote_raw: int
    base_decimals: int
    quote_decimals: int
    virtual_quote_raw: int
    slot: int

    @property
    def effective_quote_raw(self) -> int:
        return self.quote_raw + self.virtual_quote_raw


def _rpc(url: str, method: str, params: list, *, session: Any = requests, timeout: float = 4.) -> dict:
    if not url.startswith('https://'):
        raise ValueError('rpc_https_required')
    response = session.post(url, json={'jsonrpc':'2.0','id':1,'method':method,'params':params}, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get('error') or not isinstance(data.get('result'), dict):
        raise ValueError('rpc_invalid_response')
    return data['result']


def _token_balance(url: str, address: str, session: Any) -> tuple[int, int, int]:
    resp = _rpc(url, 'getTokenAccountBalance', [address, {'commitment':'confirmed'}], session=session)
    raw = resp.get('value') or {}
    amount = int(raw['amount'])
    decimals = int(raw['decimals'])
    slot = int((resp.get('context') or {}).get('slot') or 0)
    if not 0 <= decimals <= 18 or amount < 0 or slot < 1:
        raise ValueError('invalid_vault_balance')
    return amount, decimals, slot


def pump_pool_snapshot(rpc_url: str, pool: str, mint: str, *, session: Any = requests) -> PoolSnapshot:
    """Verify actual PumpSwap program ownership, mint pairing and vault balances.

    Pool struct offsets are per PumpSwap Pool layout (Anchor 8-byte prefix).
    Appended virtual_quote_reserves is signed i128; absent legacy field = 0.
    """
    if not valid_pubkey(pool) or not valid_pubkey(mint):
        raise ValueError('invalid_pool_or_mint_pubkey')
    result = _rpc(rpc_url, 'getAccountInfo', [pool, {'encoding':'base64','commitment':'confirmed'}], session=session)
    acct = result.get('value')
    if not isinstance(acct, dict):
        raise ValueError('pool_account_missing')
    if acct.get('owner') != PUMP_AMM_PROGRAM:
        raise ValueError('pool_not_pumpswap_program_owned')
    payload = acct.get('data')
    if not isinstance(payload, list) or len(payload) < 2 or payload[1] != 'base64':
        raise ValueError('pool_encoding_unsupported')
    data = base64.b64decode(payload[0], validate=True)
    if len(data) < 211:
        raise ValueError('pool_account_too_short')
    if data[:8] != PUMP_POOL_DISCRIMINATOR:
        # Wrong account type or unknown layout. Never guess alternative offsets.
        log.warning(
            'PAPER SHADOW POOL ACCOUNT REJECT pool=%s expected_mint=%s '
            'reason=pool_account_discriminator_mismatch account_len=%d discriminator_hex=%s '
            'no_paper_buy=True',
            pool, mint, len(data), data[:8].hex())
        raise ValueError('pool_account_discriminator_mismatch')
    # Offsets below are fixed by the PumpSwap Anchor Pool account layout:
    # 8-byte discriminator, u8 bump, u16 index, 32-byte creator,
    # 32-byte base mint [43:75], 32-byte quote mint [75:107].
    onchain_base_mint = b58encode(data[43:75])
    onchain_quote_mint = b58encode(data[75:107])
    if onchain_base_mint != mint or onchain_quote_mint != SOL:
        # This is not a reason to remap the token or try a different pool.
        # Log the actual pubkeys so a genuine provider/pool mismatch can be
        # distinguished from a decoding issue without exposing RPC/API secrets.
        log.warning(
            'PAPER SHADOW POOL MINT MISMATCH pool=%s expected_base_mint=%s '
            'onchain_base_mint=%s expected_quote_mint=%s onchain_quote_mint=%s '
            'rpc_slot=%s account_len=%d no_paper_buy=True',
            pool, mint, onchain_base_mint, SOL, onchain_quote_mint,
            (result.get('context') or {}).get('slot'), len(data))
        raise ValueError('pool_mint_identity_mismatch')
    base_vault = b58encode(data[139:171])
    quote_vault = b58encode(data[171:203])
    if not valid_pubkey(base_vault) or not valid_pubkey(quote_vault):
        raise ValueError('invalid_pool_vaults')
    virtual_raw = int.from_bytes(data[245:261], 'little', signed=True) if len(data) >= 261 else 0
    base_raw, base_decimals, base_slot = _token_balance(rpc_url, base_vault, session)
    quote_raw, quote_decimals, quote_slot = _token_balance(rpc_url, quote_vault, session)
    pool_slot = int((result.get('context') or {}).get('slot') or 0)
    if not pool_slot or max(pool_slot, base_slot, quote_slot) - min(pool_slot, base_slot, quote_slot) > 150:
        raise ValueError('rpc_slot_gap_too_large')
    if quote_decimals != 9 or quote_raw + virtual_raw < 0:
        raise ValueError('invalid_quote_vault_state')
    return PoolSnapshot(pool,mint,base_vault,quote_vault,base_raw,quote_raw,
                        base_decimals,quote_decimals,virtual_raw,max(pool_slot,base_slot,quote_slot))


def pinned_sell_quote(api_key: str, mint: str, pool: str, amount_raw: int,
                      *, session: Any = requests) -> dict:
    if not api_key:
        raise ValueError('jupiter_api_key_missing')
    if not valid_pubkey(pool) or not valid_pubkey(mint) or amount_raw <= 0:
        raise ValueError('invalid_quote_request')
    response = session.get(JUPITER_QUOTE_URL,
        headers={'x-api-key':api_key},
        params={'inputMint':mint,'outputMint':SOL,'amount':str(amount_raw),
                'slippageBps':'150','swapMode':'ExactIn','onlyDirectRoutes':'true',
                'instructionVersion':'V2'}, timeout=4.)
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or result.get('inputMint') != mint or result.get('outputMint') != SOL:
        raise ValueError('quote_mint_mismatch')
    if result.get('inAmount') != str(amount_raw):
        raise ValueError('quote_amount_mismatch')
    route = result.get('routePlan')
    if not isinstance(route,list) or len(route) != 1 or not isinstance(route[0],dict):
        raise ValueError('quote_not_single_pool')
    swap = route[0].get('swapInfo') or {}
    if (swap.get('ammKey') != pool or swap.get('inputMint') != mint or
            swap.get('outputMint') != SOL):
        raise ValueError('quote_not_pinned_pool')
    if route[0].get('percent') not in (None, 100) or route[0].get('bps') not in (None, 10000):
        raise ValueError('quote_partial_route')
    out = int(result.get('outAmount') or 0)
    minimum = int(result.get('otherAmountThreshold') or 0)
    if not 0 < minimum <= out:
        raise ValueError('quote_invalid_proceeds')
    impact = float(result.get('priceImpactPct', 'nan'))
    if not math.isfinite(impact) or impact < 0 or impact > 0.02:
        raise ValueError('quote_excessive_impact')
    return {'out_lamports':out, 'min_lamports':minimum,
            'impact_fraction':impact, 'amm_key':pool,
            'context_slot':int(result.get('contextSlot') or 0)}


def verify_paper_exit_route(rpc_url: str, jupiter_key: str, *, mint: str, pool: str,
                            dex: str, price_usd: float, size_usd: float = 2.,
                            session: Any = requests) -> dict:
    """Same-pool RPC reserves + read-only quote. Unsupported/unknown -> no buy."""
    if dex != 'pumpswap':
        raise ValueError('onchain_adapter_unsupported_dex')
    if not math.isfinite(price_usd) or price_usd <= 0:
        raise ValueError('invalid_entry_price')
    snap = pump_pool_snapshot(rpc_url, pool, mint, session=session)
    if snap.base_raw <= 0 or snap.effective_quote_raw < 250_000_000:
        raise ValueError('onchain_pool_insufficient_reserves')
    # DexScreener entry haircut is 3% of price; use that same token quantity.
    raw_amount = int(size_usd / (price_usd * 1.03) * 10 ** snap.base_decimals)
    if raw_amount <= 0 or raw_amount > snap.base_raw // 10:
        raise ValueError('position_too_large_for_pool')
    # Read-only quote from routing engine must match the EXACT verified pool.
    quote = pinned_sell_quote(jupiter_key,mint,pool,raw_amount,session=session)
    # Compare to pool's onchain reserve-implied SOL value at prebuy time.
    implied_lamports = raw_amount * snap.effective_quote_raw / snap.base_raw
    if quote['min_lamports'] < implied_lamports * .65:
        raise ValueError('sell_quote_below_onchain_price')
    return {'status':'pinned_pool_rpc_and_router_quote','pool':pool,'rpc_slot':snap.slot,
            'onchain_base_raw':snap.base_raw,'onchain_quote_raw':snap.effective_quote_raw,
            'mint_decimals':snap.base_decimals,'token_amount_raw':raw_amount,
            'quote_min_sol_lamports':quote['min_lamports'],
            'quote_impact_pct':round(quote['impact_fraction']*100,4),
            'executable_swap_verified':False,
            'read_only_route_quote_verified':True}

"""Read-only Pump.fun SOL bonding-curve inspection; never fabricates DEX liquidity.

The virtual reserves determine a spot quote, NOT executable pool liquidity.
Real SOL reserves are separately reported and are not substituted for the
DexScreener liquidity requirement. No transactions or private keys are used.
"""
import base64
import struct
from typing import Any
import requests

PUMP_PROGRAM_ID = '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'


def decode_curve_account(data: bytes) -> dict[str, Any]:
    if len(data) < 49:
        raise ValueError('Bonding curve account too short')
    # Anchor account discriminator, followed by five u64 reserves/supply and bool.
    import hashlib
    expected = hashlib.sha256(b'account:BondingCurve').digest()[:8]
    if data[:8] != expected:
        raise ValueError('Not a Pump.fun BondingCurve account')
    virtual_token, virtual_sol, real_token, real_sol, supply = struct.unpack_from('<5Q', data, 8)
    complete = data[48]
    if complete not in (0, 1):
        raise ValueError('Invalid completion flag')
    if not virtual_token or not virtual_sol or not supply:
        raise ValueError('Invalid virtual reserves')
    return {'virtual_token_raw': virtual_token, 'virtual_sol_lamports': virtual_sol,
            'real_token_raw': real_token, 'real_sol_lamports': real_sol,
            'supply_raw': supply, 'complete': bool(complete),
            'spot_sol_per_raw_token': virtual_sol / virtual_token / 1e9,
            'market_cap_sol': virtual_sol * supply / virtual_token / 1e9,
            'real_sol_reserve': real_sol / 1e9}


def lookup_curve(mint: str, rpc_url: str, *, timeout: int = 8) -> dict[str, Any]:
    if not rpc_url:
        raise ValueError('SOLANA_RPC_URL not configured')
    from solders.pubkey import Pubkey
    program = Pubkey.from_string(PUMP_PROGRAM_ID)
    address, _ = Pubkey.find_program_address([b'bonding-curve', bytes(Pubkey.from_string(mint))], program)
    response = requests.post(rpc_url, json={'jsonrpc':'2.0','id':1,'method':'getAccountInfo',
        'params':[str(address), {'encoding':'base64','commitment':'confirmed'}]}, timeout=timeout)
    response.raise_for_status()
    result = response.json()
    if result.get('error'):
        raise ValueError('RPC error: ' + str(result['error'].get('message', 'unknown')))
    value = (result.get('result') or {}).get('value')
    if not value:
        return {'status': 'curve_account_missing'}
    if value.get('owner') != PUMP_PROGRAM_ID:
        return {'status': 'wrong_account_owner'}
    encoded = value.get('data')
    if not isinstance(encoded, list) or len(encoded) < 2 or encoded[1] != 'base64':
        return {'status': 'invalid_account_encoding'}
    info = decode_curve_account(base64.b64decode(encoded[0], validate=True))
    return {'status': 'graduated' if info['complete'] else 'active_bonding_curve', **info}

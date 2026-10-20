"""Minimal, dependency-free Solana PDA checks for *read-only* PumpSwap research.

Offsets/seeds from pump-fun/pump-public-docs. Never signs or sends trades.
Canonical classification requires ALL of: correct Pool PDA and LP mint PDA,
index 0, base=meme mint, quote=WSOL, creator=Pump migration authority PDA.
Being canonical is not by itself proof of unwithdrawable liquidity.
"""
from __future__ import annotations
import hashlib

from read_only_exit_v872 import ALPHABET, b58encode, valid_pubkey, PUMP_AMM_PROGRAM, SOL

PUMP_BONDING_PROGRAM = '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P-2, _P)) % _P

def b58decode_key(value):
    if not valid_pubkey(value):
        raise ValueError('invalid_pubkey_for_pda')
    n=0
    for ch in value:
        n=n*58+ALPHABET.index(ch)
    data=n.to_bytes(32,'big')
    if b58encode(data)!=value:
        raise ValueError('noncanonical_pubkey')
    return data

def _is_ed25519_point(data):
    """Match Ed25519 compressed-y curve-point decompression used by Solana PDA.

    A valid on-curve compressed point cannot be a program-derived address.
    """
    if len(data)!=32:return False
    yfull=int.from_bytes(data,'little')
    sign=yfull>>255
    y=yfull & ((1<<255)-1)
    if y>=_P:return False
    y2=(y*y)%_P
    den=(_D*y2+1)%_P
    if den==0:return False
    x2=((y2-1)*pow(den,_P-2,_P))%_P
    if x2==0:return sign==0
    return pow(x2,(_P-1)//2,_P)==1

def find_program_address(seeds, program):
    if len(seeds)>=16 or any(len(s)>32 for s in seeds):
        raise ValueError('invalid_pda_seeds')
    program_bytes=b58decode_key(program)
    for bump in range(255,-1,-1):
        digest=hashlib.sha256(b''.join(seeds)+bytes([bump])+program_bytes+b'ProgramDerivedAddress').digest()
        if not _is_ed25519_point(digest):
            return b58encode(digest),bump
    raise ValueError('pda_derivation_failed')

def classify_pool(pool,raw):
    if len(raw)<211:
        raise ValueError('unverified_pool_layout')
    pool_bump=raw[8]
    index=int.from_bytes(raw[9:11],'little')
    creator=b58encode(raw[11:43]);base=b58encode(raw[43:75]);quote=b58encode(raw[75:107])
    lp_mint=b58encode(raw[107:139])
    derived,bump=find_program_address([b'pool',index.to_bytes(2,'little'),
                                      b58decode_key(creator),b58decode_key(base),
                                      b58decode_key(quote)],PUMP_AMM_PROGRAM)
    if derived!=pool or bump!=pool_bump:
        raise ValueError('unverified_pool_pda')
    derived_lp,_=find_program_address([b'pool_lp_mint',b58decode_key(pool)],PUMP_AMM_PROGRAM)
    if lp_mint!=derived_lp:
        raise ValueError('unverified_lp_mint_pda')
    if index==0 and quote==SOL and base!=SOL:
        migration_creator,_=find_program_address([b'pool-authority',b58decode_key(base)],
                                                 PUMP_BONDING_PROGRAM)
        if creator==migration_creator:
            return {'pool_type':'canonical_migration','pool_index':index,
                    'pda_verified':True,'lp_mint_pda_verified':True,
                    'migration_authority_verified':True}
    return {'pool_type':'permissionless_or_noncanonical','pool_index':index,
            'pda_verified':True,'lp_mint_pda_verified':True,
            'migration_authority_verified':False}

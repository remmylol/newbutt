"""Regression for official PumpSwap migration authority and LP-withdrawal evidence.

All fixtures are synthetic, except fixed PDA vectors documented by pump.fun.
Never uses RPC outside monkeypatched fakes, and never executes a swap.
"""
import base64
from copy import deepcopy
import pytest
from read_only_exit_v872 import SOL, PUMP_AMM_PROGRAM, PUMP_POOL_DISCRIMINATOR, b58encode
from pumpswap_pda_v901 import (
    b58decode_key,find_program_address,classify_pool,PUMP_BONDING_PROGRAM)
import micro_rug_guard_v895 as rug

SAMPLE_POOL='GseMAnNDvntR5uFePZ51yZBXzNSn7GdFPkfHwfr6d77J'
SAMPLE_MINT='7LSsEoJGhLeZzGvDofTdNg7M3JttxQqGWNLo6vWMpump'
SAMPLE_AUTHORITY='9XDYTfQKwW8sHPqnFdUreMmtmffmkHVPGTNV2e3LKxNW'


def key(i):
    return b58encode(bytes([i])*32)


def make_pool(*,canonical,lp_supply,lp_balance,index=None,reversed_orientation=False,coin_creator=True):
    mint=SAMPLE_MINT
    base=SOL if reversed_orientation else mint
    quote=mint if reversed_orientation else SOL
    i=(0 if canonical else 1) if index is None else index
    creator=(find_program_address([b'pool-authority',b58decode_key(mint)],PUMP_BONDING_PROGRAM)[0]
             if canonical else key(3))
    seeds=[b'pool',i.to_bytes(2,'little'),b58decode_key(creator),
           b58decode_key(base),b58decode_key(quote)]
    pool,bump=find_program_address(seeds,PUMP_AMM_PROGRAM)
    lp,_=find_program_address([b'pool_lp_mint',b58decode_key(pool)],PUMP_AMM_PROGRAM)
    raw=bytearray(287)
    raw[:8]=PUMP_POOL_DISCRIMINATOR
    raw[8]=bump;raw[9:11]=i.to_bytes(2,'little')
    for a,b,data in [(11,43,creator),(43,75,base),(75,107,quote),(107,139,lp),
                     (139,171,key(9)),(171,203,key(10))]:raw[a:b]=b58decode_key(data)
    raw[203:211]=lp_supply.to_bytes(8,'little')
    if coin_creator:raw[211:243]=b58decode_key(key(11))
    return mint,pool,lp,bytes(raw),lp_balance


def run_security(monkeypatch,fixture,*,creator_balance=0):
    mint,pool,lp,raw,outstanding=fixture
    large_acc=key(16)
    def rpc(_,method,params):
        if method=='getAccountInfo':
            assert params[0]==pool
            return {'value':{'owner':PUMP_AMM_PROGRAM,'data':[base64.b64encode(raw).decode(),'base64']}}
        if method=='getTokenSupply':
            if params[0]==lp:return {'value':{'amount':str(outstanding)}}
            if params[0]==mint:return {'value':{'amount':'1000000'}}
            raise AssertionError(params)
        if method=='getTokenLargestAccounts':
            return {'value':[{'address':large_acc}]}
        if method=='getMultipleAccounts':
            assert params[0]==[large_acc]
            return {'value':[{'data':{'parsed':{'type':'account','info':{
                'owner':key(11),'tokenAmount':{'amount':str(creator_balance)}}}}}]}
        raise AssertionError(method)
    monkeypatch.setattr(rug,'_rpc',rpc)
    g=rug.MicroRugGuard('fake:')
    g._executor.shutdown(wait=False)
    try:
        return g._security(mint,pool,'pumpswap',{'base_vault':key(10) if b58encode(raw[43:75])==SOL else key(9)})
    finally:
        pass


def test_official_pump_documented_pool_derivation_vector():
    authority,bump=find_program_address([b'pool-authority',b58decode_key(SAMPLE_MINT)],PUMP_BONDING_PROGRAM)
    assert authority==SAMPLE_AUTHORITY and 0<=bump<=255
    pool,pb=find_program_address([b'pool',(0).to_bytes(2,'little'),
                                b58decode_key(authority),b58decode_key(SAMPLE_MINT),
                                b58decode_key(SOL)],PUMP_AMM_PROGRAM)
    assert pool==SAMPLE_POOL and pb==254


def test_true_migration_zero_supply_is_eligible_after_holder_screen(monkeypatch):
    fixture=make_pool(canonical=True,lp_supply=0,lp_balance=0)
    classification=classify_pool(fixture[1],fixture[3])
    assert classification['pool_type']=='canonical_migration'
    evidence=run_security(monkeypatch,fixture)
    assert evidence['status']=='verified_burned_lp_and_screened_holders'
    assert evidence['pool_type']=='canonical_migration'
    assert evidence['withdrawable_lp_pct_upper_bound']==0
    assert evidence['coin_creator_identity_available']


def test_custom_zero_supply_cannot_impersonate_migration(monkeypatch):
    fixture=make_pool(canonical=False,lp_supply=0,lp_balance=0,index=0)
    assert classify_pool(fixture[1],fixture[3])['pool_type']=='permissionless_or_noncanonical'
    evidence=run_security(monkeypatch,fixture)
    assert evidence['status']=='unverified_lp_withdrawal_control'
    assert evidence['reason']=='custom_zero_supply_not_a_verified_migration'


def test_custom_99pct_burned_passes_but_98pct_fails(monkeypatch):
    fixture=make_pool(canonical=False,lp_supply=100000,lp_balance=1000)
    evidence=run_security(monkeypatch,fixture)
    assert evidence['status']=='verified_burned_lp_and_screened_holders'
    assert evidence['withdrawable_lp_pct_upper_bound']==1.
    fixture_bad=make_pool(canonical=False,lp_supply=100000,lp_balance=1001)
    bad=run_security(monkeypatch,fixture_bad)
    assert bad['status']=='unverified_lp_withdrawal_control'
    assert bad['withdrawable_lp_pct_upper_bound']==1.001


def test_canonical_added_liquidity_above_one_pct_still_blocks(monkeypatch):
    fixture=make_pool(canonical=True,lp_supply=10000,lp_balance=1200)
    evidence=run_security(monkeypatch,fixture)
    assert evidence['status']=='unverified_lp_withdrawal_control'
    assert evidence['pool_type']=='canonical_migration'
    assert evidence['withdrawable_lp_pct_upper_bound']==12.


def test_invalid_pool_account_pda_and_wrong_lp_mint_rejected():
    mint,pool,lp,raw,_=make_pool(canonical=True,lp_supply=0,lp_balance=0)
    with pytest.raises(ValueError,match='unverified_pool_pda'):
        classify_pool(key(20),raw)
    replaced=bytearray(raw);replaced[107:139]=b58decode_key(key(23))
    with pytest.raises(ValueError,match='unverified_lp_mint_pda'):
        classify_pool(pool,bytes(replaced))
    replaced=bytearray(raw);replaced[8]=0 if raw[8]!=0 else 1
    with pytest.raises(ValueError,match='unverified_pool_pda'):
        classify_pool(pool,bytes(replaced))


def test_reversed_sol_pool_not_misclassified_as_migration(monkeypatch):
    fixture=make_pool(canonical=False,lp_supply=100000,lp_balance=500,reversed_orientation=True)
    assert classify_pool(fixture[1],fixture[3])['pool_type']=='permissionless_or_noncanonical'
    assert run_security(monkeypatch,fixture)['status']=='verified_burned_lp_and_screened_holders'


def test_known_coin_creator_concentration_blocked(monkeypatch):
    fixture=make_pool(canonical=True,lp_supply=0,lp_balance=0)
    evidence=run_security(monkeypatch,fixture,creator_balance=120000)
    assert evidence['status']=='concentrated_wallet_supply'
    assert evidence['coin_creator_identity_available']
    assert evidence['creator_wallet_pct']==12.0


def test_no_coin_creator_identifier_is_explicitly_reported(monkeypatch):
    fixture=make_pool(canonical=True,lp_supply=0,lp_balance=0,coin_creator=False)
    evidence=run_security(monkeypatch,fixture)
    assert evidence['status']=='verified_burned_lp_and_screened_holders'
    assert evidence['coin_creator_identity_available'] is False


def test_no_negative_lp_supply_or_supply_above_history_accepted(monkeypatch):
    fixture=make_pool(canonical=True,lp_supply=0,lp_balance=5)
    with pytest.raises(ValueError,match='unverified_lp_supply_consistency'):
        run_security(monkeypatch,fixture)


def test_no_real_orders_in_verification_source():
    import inspect
    src=inspect.getsource(rug.MicroRugGuard._security)
    for name in ('sendTransaction','simulateTransaction','sign_and_send','broadcast_swap'):
        assert name not in src

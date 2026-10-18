"""v8.6.3 paper-only sellability, liquidity deterioration, and five slots."""
from __future__ import annotations
import time
import pytest
import bot
from test_shadow_primary_v86 import pair, ledger, MINT, POOL
from shadow_risk_v863 import (prebuy_depth_check,pool_risk_signal,
    classify_unavailable_exit,liquidity_trade_breakdown)


def test_full_two_dollar_sell_model_blocks_unknown_depth_and_impact():
    assert prebuy_depth_check(price=.001,liquidity=50_000)[0]
    assert not prebuy_depth_check(price=0,liquidity=50_000)[0]
    assert not prebuy_depth_check(price=.001,liquidity=14_999)[0]
    assert not prebuy_depth_check(price=.001,liquidity=15_000,stake=5_000,max_impact_pct=2)[0]


def test_original_shadow_entry_never_requires_independent_crosscheck(monkeypatch):
    monkeypatch.setattr(bot,'PAPER_SHADOW_ACTIVE',True)
    monkeypatch.setattr(bot,'SHADOW_REQUIRE_INDEPENDENT_POOL',True)
    monkeypatch.setattr(bot,'MAX_POSITIONS',5)
    monkeypatch.setattr(bot,'MAX_SHADOW_POSITIONS',5)
    def forbidden(*args,**kwargs):
        raise AssertionError('original shadow cannot fetch extra entry data')
    monkeypatch.setattr(bot,'held_pool_fallback',forbidden)
    state=ledger()
    assert bot.shadow_paper_entry(state,pair())==(True,'paper_shadow_bought')
    assert state['paper_trade_metrics'][MINT]['entry_crosscheck']=='original_shadow_single_observation'


def test_five_two_dollar_shadow_positions_are_allowed_sixth_rejected(monkeypatch):
    monkeypatch.setattr(bot,'PAPER_SHADOW_ACTIVE',True)
    monkeypatch.setattr(bot,'SHADOW_REQUIRE_INDEPENDENT_POOL',False)
    monkeypatch.setattr(bot,'MAX_POSITIONS',5)
    monkeypatch.setattr(bot,'MAX_SHADOW_POSITIONS',5)
    state=ledger()
    for idx in range(6):
        p=pair()
        p['baseToken']={'address':chr(65+idx)*44,'symbol':f'T{idx}','name':f'T{idx}'}
        p['pairAddress']=chr(70+idx)*44
        result=bot.shadow_paper_entry(state,p)
        assert result == ((True,'paper_shadow_bought') if idx < 5 else (False,'position_limit'))
    assert len(state['positions'])==5
    assert state['cash_usd']==pytest.approx(26)
    assert {item['amount_usd'] for item in state['positions'].values()}=={2}


def test_restored_shadow_does_not_sell_early_on_liquidity_deterioration(monkeypatch):
    monkeypatch.setattr(bot,'PAPER_SHADOW_ACTIVE',True)
    state=ledger()
    assert bot.shadow_paper_entry(state,pair(liquidity=60_000))[0]
    met=state['paper_trade_metrics'][MINT]
    met['liquidity_risk_next_at']=time.time()-1
    assert met['shadow_next_sample_at']>time.time()
    primary={MINT:{'price':.000095,'liquidity':33_000,'quote_source':'dexscreener',
        'pool_address':POOL,'shadow_market_cap':200_000,'shadow_age_minutes':8}}
    bot.manage_positions(state,primary)
    assert MINT in state['positions']
    assert state['closed_trades']==[]
    assert met.get('liquidity_guard_trigger_count',0)==0


def test_unverified_primary_never_triggers_protective_exit(monkeypatch):
    monkeypatch.setattr(bot,'PAPER_SHADOW_ACTIVE',True)
    monkeypatch.setattr(bot,'SHADOW_REQUIRE_INDEPENDENT_POOL',False)
    state=ledger()
    assert bot.shadow_paper_entry(state,pair(liquidity=60_000))[0]
    met=state['paper_trade_metrics'][MINT]
    met['liquidity_risk_next_at']=time.time()-1
    bot.manage_positions(state,{MINT:{'price':.0001,'liquidity':16_000,
        'quote_source':'geckoterminal_indicative','pool_address':POOL}})
    assert MINT in state['positions'] and not state['closed_trades']
    assert MINT in state['exit_issues']
    assert state.get('halted') is False


def test_no_stake_sale_if_original_pool_is_below_exit_depth(monkeypatch):
    monkeypatch.setattr(bot,'PAPER_SHADOW_ACTIVE',True)
    monkeypatch.setattr(bot,'SHADOW_REQUIRE_INDEPENDENT_POOL',False)
    state=ledger()
    assert bot.shadow_paper_entry(state,pair())[0]
    met=state['paper_trade_metrics'][MINT]
    met['liquidity_risk_next_at']=time.time()-1
    bot.manage_positions(state,{MINT:{'price':.0001,'liquidity':0,'quote_source':'dexscreener','pool_address':POOL}})
    assert MINT in state['positions']
    assert not state['closed_trades']
    assert MINT in state['exit_issues']
    assert state.get('halted') is False


def test_loss_classification_distinguishes_provider_failure_from_real_depletion():
    assert classify_unavailable_exit('missing_price',{'reason':'pool_status_unverified'})=='quote_or_market_data_unavailable'
    assert classify_unavailable_exit('missing_price',{'reason':'pinned_pool_zero_liquidity'})=='observed_pinned_pool_liquidity_depleted'
    assert classify_unavailable_exit('x',{'reason':'pinned_pool_missing_no_liquid_alternative'})=='pinned_pool_missing_from_provider_unconfirmed'


def test_breakdown_version_specific_liquidity_and_five_minute_flow():
    trades=[{'pnl_usd':-.5,'execution':'paper_estimate_only',
             'paper_metrics':{'strategy_version':'v8.6.3_shadow','entry_pool_liquidity_usd':42_000,'entry_5m_volume_usd':7_000}},
            {'pnl_usd':-2,'execution':'paper_writeoff_no_sale',
             'paper_metrics':{'strategy_version':'v8.6.3_shadow','entry_pool_liquidity_usd':42_000,'entry_5m_volume_usd':7_000}},
            {'pnl_usd':100,'paper_metrics':{'strategy_version':'v8.6_shadow','entry_pool_liquidity_usd':42_000}}]
    result=liquidity_trade_breakdown(trades)
    assert result['liquidity:25-50k']=={'trades':2,'net_pnl_usd':-2.5,'writeoffs':1,'wins':0}
    assert result['volume5:5-25k']['trades']==2


def test_risk_monitor_only_triggers_with_meaningful_liquidity_drop():
    assert pool_risk_signal(60_000,60_000,55_000)[0] is False
    assert pool_risk_signal(60_000,60_000,30_000)[0] is True
    assert pool_risk_signal(60_000,60_000,16_000)[1]=='liquidity_near_minimum'

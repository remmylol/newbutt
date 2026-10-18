"""v7.6 offline fast-volume / confirmed-exit protection regression."""
import math
import time
from collections import Counter
from dataclasses import asdict

import pytest
import bot
import clean_boot
from fast_momentum_v76 import fast_mover_gate, confirmed_profit_trail, confirmed_loss_circuit
from rolling_discovery import rotating_pool_sources
from independent_movers_v73 import MODES
from test_candidate_pipeline_v74 import eligible_pair


def good_fast(**kwargs):
    c = dict(change_m5=2.5, change_h1=8., m5_volume=6000.,
             h1_volume=60000., liquidity=80000.,m5_buys=38,m5_sells=20)
    c.update(kwargs)
    return c


def test_launch_is_paper_fast_with_original_age_and_cap_safety():
    assert clean_boot.PAPER_PRESET['PAPER_MODE']=='true'
    assert clean_boot.PAPER_PRESET['LIVE_TRADING_ENABLED']=='false'
    assert clean_boot.PAPER_PRESET['PAPER_FAST_ONLY']=='true'
    assert clean_boot.PAPER_PRESET['PAPER_FAST_TP_NET_PCT']=='8'
    assert clean_boot.PAPER_PRESET['MOMENTUM_MARKET_CAP_MIN_USD']=='35000'
    assert clean_boot.PAPER_PRESET['MOMENTUM_MIN_PAIR_AGE_MINUTES']=='12'


def test_fast_pool_passes_ranked_and_slow_pool_refused():
    ok,reason,rank=fast_mover_gate(good_fast())
    assert ok and reason=='fast_qualified' and 0 < rank < 101
    ok,reason,_=fast_mover_gate(good_fast(change_m5=.3))
    assert not ok and reason=='fast_price_momentum'
    ok,reason,_=fast_mover_gate(good_fast(m5_volume=1200))
    assert not ok and reason=='fast_volume_depth'
    ok,reason,_=fast_mover_gate(good_fast(h1_volume=120000))
    assert not ok and reason=='fast_volume_pace'
    ok,reason,_=fast_mover_gate(good_fast(m5_buys=13,m5_sells=18))
    assert not ok and reason=='fast_buy_pressure'


def test_fast_gate_rejects_vertical_spike_and_invalid_data():
    assert fast_mover_gate(good_fast(change_m5=22))[1]=='fast_overheated_or_anomalous'
    assert not fast_mover_gate(good_fast(m5_volume=float('nan')))[0]
    assert not fast_mover_gate(good_fast(m5_sells=0))[0]


def test_new_fast_entry_passes_complete_market_filters(monkeypatch):
    monkeypatch.setattr(bot,'PAPER_FAST_ONLY',True)
    p=eligible_pair(cap=1_100_000,liquidity=70000,m5_volume=6200,
                    h1_volume=62000,buys=42,sells=18,change_h1=10)
    c=bot.analyze_pair(p)
    assert c is not None and c['fast_mover_rank']>0
    p['priceChange']={**p['priceChange'],'m5':0.3}
    reasons=Counter()
    assert bot.analyze_pair(p,reasons) is None
    assert reasons['fast_price_momentum']==1


def test_micro_cap_safety_not_bypassed_by_fast_qualifier(monkeypatch):
    monkeypatch.setattr(bot,'PAPER_FAST_ONLY',True)
    p=eligible_pair(cap=150000,liquidity=24000,m5_volume=6000,
                    h1_volume=35000,buys=44,sells=17,change_h1=10)
    reasons=Counter()
    assert bot.analyze_pair(p,reasons) is None
    assert reasons['micro_cap_extra_safety']==1


def test_fast_profit_trail_confirms_two_distinct_primary_prices():
    now=1000.
    hist=[[940.,7.,1.07],[955.,3.9,1.039],[985.,3.6,1.036]]
    fired,info=confirmed_profit_trail(hist,now=now,opened_at=920)
    assert fired and info['status']=='confirmed' and info['peak_net_pct']==7


def test_profit_trail_never_fires_on_single_stale_or_flat_quote():
    now=1000.
    assert not confirmed_profit_trail([[940.,7,1.07],[985.,3,1.03]],now=now,opened_at=920)[0]
    assert not confirmed_profit_trail([[900.,7,1.07],[920.,3,1.03],[930.,2,1.02]],now=now,opened_at=890)[0]
    assert not confirmed_profit_trail([[940.,7,1.07],[970.,3.6,1.036],[985.,3.6,1.036]],now=now,opened_at=920)[0]
    assert not confirmed_profit_trail([[940.,3,1.03],[970.,2.1,1.021],[985.,2.0,1.02]],now=now,opened_at=920)[0]


def test_loss_circuit_confirms_two_distinct_prices_and_time():
    now=1000.
    history=[[925.,-7.8,.970],[960.,-8.4,.965],[993.,-8.9,.956]]
    fired,info=confirmed_loss_circuit(history,now=now,opened_at=880,entry_market_price=1.)
    assert fired and info['entry_drop_pct']>4
    assert not confirmed_loss_circuit(history,now=now,opened_at=960,entry_market_price=1.)[0]
    assert not confirmed_loss_circuit(history,now=now,opened_at=880,entry_market_price=0)[0]


def test_loss_circuit_doesnt_fire_on_flat_or_short_confirmation():
    hist=[[920.,-8,.96],[980.,-8,.96],[994.,-8,.96]]
    assert not confirmed_loss_circuit(hist,now=1000,opened_at=870,entry_market_price=1.)[0]
    hist=[[960.,-8,.961],[975.,-8.2,.96],[994.,-8.3,.958]]
    assert not confirmed_loss_circuit(hist,now=1000,opened_at=880,entry_market_price=1.)[0]


def test_take_profit_is_8pct_for_new_fast_paper_fills(monkeypatch,tmp_path):
    monkeypatch.setattr(bot,'STATE_FILE',tmp_path/'ledger.json')
    monkeypatch.setattr(bot,'PAPER_FAST_ONLY',True)
    state=bot.load_state()
    state['cash_usd']=30
    candidate=dict(address='A'*44,symbol='FAST',name='Fast',price=.01,
                   pool_address='B'*44,pool_dex='raydium',score=93,
                   fast_mover_rank=72.,age_hours=2.,market_cap_usd=1_000_000,
                   m5_buys=40,m5_sells=20)
    bot.paper_buy(state,candidate)
    assert state['paper_trade_metrics'][candidate['address']]['take_profit_net_pct']==8
    assert state['positions'][candidate['address']]['amount_usd']==10
    sold=[]
    monkeypatch.setattr(bot,'close_position',lambda s,m,p,r,l,q:sold.append(r))
    bot.manage_positions(state,{candidate['address']:{'price':.0115,'liquidity':100000,
                          'quote_source':'dexscreener','pool_address':'B'*44}})
    assert sold==['fast_take_profit']


def test_default_strategy_limits_remain_active():
    assert any(m=='price_change_percentage_5m' for m in MODES)
    assert MODES.count('price_change_percentage_5m') >= 2
    assert all(len(rotating_pool_sources(n))==3 for n in range(15))
    assert bot.MAX_POSITIONS==5  # shadow-only v8.6.3 capacity
    assert bot.SMALL_CAP_PAPER_SIZE_USD==5
    assert bot.PAPER_FAST_ONLY


def test_fast_rejection_queue_reoffers_after_five_minutes(tmp_path):
    from fresh_batch_discovery import FreshBatchQueue
    m='A'*44
    q=FreshBatchQueue(tmp_path/'queue.json')
    assert q.offer({m:800},now=1000)['new']==1
    assert q.discard(m,'fast_price_momentum',now=1001)
    assert q.blocked(m,now=1299)
    assert not q.blocked(m,now=1301)
    assert q.offer({m:950},now=1302)['new']==1
    assert q.due(now=1302)==[m]


def test_unsafe_structural_rejection_retains_long_backoff(tmp_path):
    from fresh_batch_discovery import FreshBatchQueue
    m='A'*44
    q=FreshBatchQueue(tmp_path/'queue.json')
    q.offer({m:800},now=1000)
    q.discard(m,'market_cap_below_range',now=1001)
    assert q.blocked(m,now=2000)
    assert not q.blocked(m,now=8300)


def test_fast_rejections_are_short_term_price_rejection_not_structural():
    for reason in ('fast_price_momentum','fast_volume_depth','fast_volume_pace',
                   'fast_buy_pressure','fast_overheated_or_anomalous'):
        assert reason in bot._MOMENTUM_RETRY_REASONS


def _held_state_for_exit(monkeypatch, tmp_path, *, price=.01, age=180):
    monkeypatch.setattr(bot,'STATE_FILE',tmp_path/'ledger.json')
    monkeypatch.setattr(bot,'PAPER_REVERSAL_EXIT_ENABLED',False)
    monkeypatch.setattr(bot,'PAPER_EARLY_LOSS_REVERSAL_ENABLED',False)
    monkeypatch.setattr(bot,'PAPER_SUSTAINED_LOSS_ENABLED',False)
    stamp=time.time()
    mint='A'*44
    state=bot.load_state()
    state['positions']={mint:asdict(bot.Position(token=mint,symbol='TEST',name='Test',
          entry_price=.01, amount_usd=10, token_units=1000, opened_at=stamp-age,
          peak_price=.0112,last_price=price,last_price_at=stamp-5,
          score=88,entry_pool_address='P'*44,entry_pool_dex='raydium'))}
    state['paper_trade_metrics']={mint:{'entry_market_price':.01,
                                        'take_profit_net_pct':8,
                                        'primary_net_observations':[]}}
    return stamp,mint,state


def test_integration_loss_circuit_paper_exit_verified_primary(monkeypatch,tmp_path):
    stamp,mint,state=_held_state_for_exit(monkeypatch,tmp_path)
    state['paper_trade_metrics'][mint]['primary_net_observations']=[
        [stamp-66,-8.,.0096],[stamp-40,-8.5,.0095]]
    sold=[]
    monkeypatch.setattr(bot,'close_position',lambda s,m,p,r,l,q:sold.append(r))
    bot.manage_positions(state,{mint:{'price':.00935,'liquidity':100000,
                   'quote_source':'dexscreener','pool_address':'P'*44}})
    assert sold==['confirmed_loss_circuit']


def test_integration_profit_trail_paper_exit_verified_primary(monkeypatch,tmp_path):
    stamp,mint,state=_held_state_for_exit(monkeypatch,tmp_path)
    state['paper_trade_metrics'][mint]['primary_net_observations']=[
        [stamp-70,7.5,.0112],[stamp-40,3.9,.0107]]
    sold=[]
    monkeypatch.setattr(bot,'close_position',lambda s,m,p,r,l,q:sold.append(r))
    bot.manage_positions(state,{mint:{'price':.0106,'liquidity':100000,
                   'quote_source':'dexscreener','pool_address':'P'*44}})
    assert sold==['confirmed_profit_trail']


def test_integration_fallback_quote_does_not_trigger_fast_exit(monkeypatch,tmp_path):
    stamp,mint,state=_held_state_for_exit(monkeypatch,tmp_path)
    monkeypatch.setattr(bot,'PAPER_EXIT_REQUIRE_PRIMARY_QUOTE',False)
    state['paper_trade_metrics'][mint]['primary_net_observations']=[
        [stamp-70,-8,.0096],[stamp-40,-8.5,.0095]]
    sold=[]
    monkeypatch.setattr(bot,'close_position',lambda s,m,p,r,l,q:sold.append(r))
    bot.manage_positions(state,{mint:{'price':.00935,'liquidity':100000,
                   'quote_source':'geckoterminal_indicative','pool_address':'P'*44}})
    assert sold==[]
    assert len(state['paper_trade_metrics'][mint]['primary_net_observations'])==2

"""v8.7.5 offline-only risk-screen and read-only monitoring regressions."""
import copy
import time
import pytest
import bot
from test_shadow_exit_integrity_v874 import MINT, POOL, opening, primary, ledger


def signal(vol, change):
    return {'address':MINT,'symbol':'TEST','name':'Test','price':.0001,'score':0,
            'pool_address':POOL,'pool_dex':'pumpswap','liquidity':51000,
            'market_cap_usd':377000,'m5_volume':vol,'m5_buys':25,'m5_sells':12,
            'change_m5':change,'age_hours':.1}


def test_historical_claude_type_thin_surge_rejected(monkeypatch):
    monkeypatch.setattr(bot,'SHADOW_UNHEALTHY_ENTRY_GUARD',True)
    monkeypatch.setattr(bot,'SHADOW_SURGE_CHANGE_5M_PCT',13.5)
    monkeypatch.setattr(bot,'SHADOW_SURGE_MIN_5M_VOLUME_USD',2500.)
    assert bot.shadow_unhealthy_entry_reason(signal(1421.23,14.76)) == 'unhealthy_high_surge_thin_volume'
    assert bot.shadow_unhealthy_entry_reason(signal(1900.88,15.09)) == 'unhealthy_high_surge_thin_volume'
    assert bot.shadow_unhealthy_entry_reason(signal(1602.,15.19)) == 'unhealthy_high_surge_thin_volume'


def test_healthy_flow_not_indiscriminately_blocked(monkeypatch):
    monkeypatch.setattr(bot,'SHADOW_UNHEALTHY_ENTRY_GUARD',True)
    monkeypatch.setattr(bot,'SHADOW_SURGE_CHANGE_5M_PCT',13.5)
    monkeypatch.setattr(bot,'SHADOW_SURGE_MIN_5M_VOLUME_USD',2500.)
    assert bot.shadow_unhealthy_entry_reason(signal(7263.,15.65)) is None
    assert bot.shadow_unhealthy_entry_reason(signal(1600.,10.5)) is None
    assert bot.shadow_unhealthy_entry_reason(signal(2499.,13.5)) == 'unhealthy_high_surge_thin_volume'
    assert bot.shadow_unhealthy_entry_reason(signal(2500.,13.5)) is None


def test_shadow_order_blocked_prior_to_prebuy_quote(monkeypatch):
    monkeypatch.setattr(bot,'PAPER_SHADOW_ACTIVE',True)
    monkeypatch.setattr(bot,'PAPER_MODE',True)
    monkeypatch.setattr(bot,'SHADOW_UNHEALTHY_ENTRY_GUARD',True)
    monkeypatch.setattr(bot,'SHADOW_SAFETY_PREBUY_VERIFICATION',True)
    candidate=signal(1421.23,14.76)
    pair={'baseToken':{'address':MINT},'pairAddress':POOL}
    def must_not_call(*args,**kwargs):
        raise AssertionError('unhealthy signal should not trigger prebuy external calls')
    monkeypatch.setattr(bot,'shadow_prebuy_safety',must_not_call)
    state=ledger()
    ok,reason=bot.shadow_paper_entry(state,pair,signal_candidate=candidate)
    assert not ok and reason == 'unhealthy_high_surge_thin_volume'
    assert not state['positions'] and not state['closed_trades']
    assert state['cash_usd'] == 36


def test_routed_drop_can_trigger_before_indexer_stop(monkeypatch):
    state=opening(monkeypatch)
    monkeypatch.setattr(bot,'SHADOW_ROUTE_MONITOR_ENABLED',True)
    monkeypatch.setattr(bot,'SHADOW_EXIT_ROUTE_QUOTE_REQUIRED',True)
    monkeypatch.setattr(bot,'SHADOW_ROUTE_MONITOR_NET_STOP_PCT',-9.)
    monkeypatch.setattr(bot,'SHADOW_ROUTE_MONITOR_INTERVAL_SECONDS',15.)
    bot._shadow_route_probe_times.clear()
    metrics=state['paper_trade_metrics'][MINT]
    metrics['prebuy_safety']={'readonly_route':{
        'mint_decimals':6,'token_amount_raw':20_000_000_000,
        'quote_min_sol_lamports':20_000_000}}
    calls=[]
    def fetch(state_arg,mint,pos):
        calls.append(mint)
        return True,'same_pinned_pool_readonly_quote',{
            'min_lamports':14_000_000,'out_lamports':14_500_000,
            'in_amount_raw':20_000_000_000}
    monkeypatch.setattr(bot,'shadow_exit_route_check',fetch)
    bot.manage_positions(state,primary(price=.000102))
    assert calls == [MINT], 'do not double-call sell route in same tick'
    assert MINT not in state['positions']
    closed=state['closed_trades'][0]
    assert closed['reason']=='shadow_routed_downside_stop'
    assert closed['execution']=='paper_quote_backed_estimate'
    assert closed['proceeds_usd'] <= 1.4 + 1e-5
    assert state['cash_usd'] == pytest.approx(34 + closed['proceeds_usd'])
    assert len(closed['paper_metrics']['shadow_primary_quote_trace']) == 1
    assert not state['halted'], 'a -30% loss does not reach the -50% catastrophic halt'


def test_probe_skipped_without_entry_anchor_and_never_fabricates_sale(monkeypatch):
    state=opening(monkeypatch)
    monkeypatch.setattr(bot,'SHADOW_ROUTE_MONITOR_ENABLED',True)
    monkeypatch.setattr(bot,'SHADOW_EXIT_ROUTE_QUOTE_REQUIRED',True)
    monkeypatch.setattr(bot,'SHADOW_ROUTE_MONITOR_INTERVAL_SECONDS',15.)
    calls=[]
    monkeypatch.setattr(bot,'shadow_exit_route_check',lambda *a:(calls.append(True), None, None))
    bot._shadow_route_probe_times.clear()
    bot.manage_positions(state,primary(price=.000102))
    assert calls == [] and not state['closed_trades']
    assert MINT in state['positions']
    assert state['paper_trade_metrics'][MINT]['shadow_route_probe_last_status']=='missing_comparable_entry_anchor'


def test_quote_monitor_global_budget_and_interval(monkeypatch):
    state=opening(monkeypatch)
    monkeypatch.setattr(bot,'SHADOW_ROUTE_MONITOR_ENABLED',True)
    monkeypatch.setattr(bot,'SHADOW_EXIT_ROUTE_QUOTE_REQUIRED',True)
    monkeypatch.setattr(bot,'SHADOW_ROUTE_MONITOR_INTERVAL_SECONDS',15.)
    monkeypatch.setattr(bot,'SHADOW_ROUTE_MONITOR_MAX_PER_MINUTE',1)
    monkeypatch.setattr(bot,'SHADOW_ROUTE_MONITOR_NET_STOP_PCT',-9.)
    bot._shadow_route_probe_times.clear()
    metrics=state['paper_trade_metrics'][MINT]
    metrics['prebuy_safety']={'readonly_route':{'token_amount_raw':20_000_000_000,'quote_min_sol_lamports':20_000_000}}
    calls=[]
    def fetch(*a):
        calls.append(1)
        return True,'same_pinned_pool_readonly_quote',{'min_lamports':21_000_000,'in_amount_raw':20_000_000_000}
    monkeypatch.setattr(bot,'shadow_exit_route_check',fetch)
    bot.manage_positions(state,primary(price=.000105))
    bot.manage_positions(state,primary(price=.000105))
    assert len(calls)==1
    assert MINT in state['positions'] and len(metrics['shadow_primary_quote_trace'])==2
    assert metrics['shadow_route_probe_last_status']=='ok'


def test_indexer_protective_stop_gets_priority_over_monitor(monkeypatch):
    state=opening(monkeypatch)
    monkeypatch.setattr(bot,'SHADOW_ROUTE_MONITOR_ENABLED',True)
    monkeypatch.setattr(bot,'SHADOW_EXIT_ROUTE_QUOTE_REQUIRED',True)
    bot._shadow_route_probe_times.clear()
    metrics=state['paper_trade_metrics'][MINT]
    metrics['prebuy_safety']={'readonly_route':{'token_amount_raw':20_000_000_000,'quote_min_sol_lamports':20_000_000}}
    calls=[]
    def fetch(*a):
        calls.append(1)
        return True,'same_pinned_pool_readonly_quote',{'min_lamports':13_000_000,'in_amount_raw':20_000_000_000}
    monkeypatch.setattr(bot,'shadow_exit_route_check',fetch)
    bot.manage_positions(state,primary(price=.000075))
    assert calls==[1]
    assert state['closed_trades'][0]['reason']=='shadow_protective_stop'
    assert MINT not in state['positions']
    assert not metrics.get('shadow_route_probe_last_at')


def test_unavailable_primary_not_a_sale_even_when_monitor_enabled(monkeypatch):
    state=opening(monkeypatch,age_seconds=46*60)
    monkeypatch.setattr(bot,'SHADOW_ROUTE_MONITOR_ENABLED',True)
    monkeypatch.setattr(bot,'SHADOW_EXIT_ROUTE_QUOTE_REQUIRED',True)
    bot.manage_positions(state,{})
    assert MINT in state['positions'] and not state['closed_trades']
    assert state['paper_trade_metrics'][MINT]['pending_exit_reason']=='shadow_45m_expiry'


def test_recent_successful_health_probe_does_not_delay_emergency_exit(monkeypatch):
    state=opening(monkeypatch)
    monkeypatch.setattr(bot,'SHADOW_EXIT_ROUTE_QUOTE_REQUIRED',True)
    monkeypatch.setattr(bot,'SOLANA_RPC_URL','https://example.invalid')
    monkeypatch.setattr(bot,'JUPITER_API_KEY','offline-only')
    metrics=state['paper_trade_metrics'][MINT]
    metrics['prebuy_safety']={'readonly_route':{'mint_decimals':6,
        'token_amount_raw':20_000_000_000,'quote_min_sol_lamports':20_000_000}}
    t=time.time()-4
    metrics['exit_route_last_attempt_at']=t
    metrics['exit_route_last_success_at']=t
    calls=[]
    def quote(key, mint, pool, raw):
        calls.append(1)
        return {'min_lamports':14_000_000,'out_lamports':14_500_000}
    monkeypatch.setattr(bot,'pinned_sell_quote',quote)
    bot.manage_positions(state,primary(price=.000075))
    assert calls == [1]
    assert state['closed_trades'][0]['reason']=='shadow_protective_stop'


def test_failed_quote_is_still_throttled_and_position_is_retained(monkeypatch):
    state=opening(monkeypatch)
    monkeypatch.setattr(bot,'SHADOW_EXIT_ROUTE_QUOTE_REQUIRED',True)
    monkeypatch.setattr(bot,'SOLANA_RPC_URL','https://example.invalid')
    monkeypatch.setattr(bot,'JUPITER_API_KEY','offline-only')
    metrics=state['paper_trade_metrics'][MINT]
    metrics['prebuy_safety']={'readonly_route':{'mint_decimals':6}}
    metrics['exit_route_last_attempt_at']=time.time()-2
    metrics['exit_route_last_success_at']=0
    def must_not_call(*args):
        raise AssertionError('failed route cooldown should prevent API request')
    monkeypatch.setattr(bot,'pinned_sell_quote',must_not_call)
    bot.manage_positions(state,primary(price=.000075))
    assert MINT in state['positions'] and not state['closed_trades']
    assert metrics['pending_exit_reason']=='shadow_protective_stop'


def test_report_preserves_quote_health_and_never_claims_real_fills(monkeypatch):
    import portfolio_report
    state=opening(monkeypatch)
    met=state['paper_trade_metrics'][MINT]
    met['shadow_route_probe_last_status']='ok'
    met['shadow_route_probe_last_net_pct']=-15.4
    met['max_primary_observation_gap_s']=13.1
    report=portfolio_report.summarize(state)
    assert report['open_risk_observations'][0]['route_probe_last_change_pct']==-15.4
    assert report['open_risk_observations'][0]['primary_quote_max_gap_seconds']==13.1
    assert report['open_positions']==1 and report['closed_trades']==0

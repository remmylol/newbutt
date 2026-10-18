"""Offline regression tests: no network, no real fills or modified portfolio files."""
import json
import time
import pytest
import bot

MINT = 'A' * 44
POOL = 'B' * 44


def ledger():
    return {
        'cash_usd': 36., 'positions': {}, 'closed_trades': [],
        'day_start_equity': 36., 'day': '2026-10-18',
        'halted': False, 'exit_issues': {}, 'seen_tokens': [],
        'paper_trade_metrics': {}, 'rugged_mints': [],
    }


def opening(monkeypatch, *, age_seconds=0, entry_liq=20000):
    monkeypatch.setattr(bot, 'PAPER_SHADOW_ACTIVE', True)
    monkeypatch.setattr(bot, 'PAPER_MODE', True)
    monkeypatch.setattr(bot, 'SHADOW_PROTECTIVE_EXIT_ENABLED', True)
    monkeypatch.setattr(bot, 'SHADOW_EXIT_ROUTE_QUOTE_REQUIRED', False)
    monkeypatch.setattr(bot, 'SHADOW_LIQUIDITY_EXIT_DROP_PCT', 40.)
    state = ledger()
    candidate = {'address': MINT, 'symbol': 'TEST', 'name': 'Test',
                 'price': .0001, 'score': 0, 'pool_address': POOL,
                 'pool_dex': 'pumpswap', 'liquidity': entry_liq,
                 'market_cap_usd': 100000, 'm5_volume': 5000,
                 'm5_buys': 15, 'm5_sells': 10, 'age_hours': .1}
    bot.paper_buy(state, candidate, size_usd=2., entry_profile='early_shadow')
    assert MINT in state['positions']
    state['positions'][MINT]['opened_at'] -= age_seconds
    return state


def primary(*, price=.0001, liquidity=20000):
    return {MINT: {'price': price, 'liquidity': liquidity,
            'quote_source': 'dexscreener', 'pool_address': POOL,
            'shadow_market_cap': 100000., 'shadow_age_minutes': 7.}}


def test_shadow_expiry_checked_each_tick_even_if_sample_not_due(monkeypatch):
    state = opening(monkeypatch, age_seconds=47*60)
    state['paper_trade_metrics'][MINT]['shadow_next_sample_at'] = time.time()+180
    bot.manage_positions(state, primary())
    assert MINT not in state['positions']
    assert state['closed_trades'][0]['reason']=='shadow_45m_expiry'
    assert state['closed_trades'][0]['execution']=='paper_estimate_only'


def test_expired_position_with_no_price_is_pending_not_filled(monkeypatch):
    state = opening(monkeypatch, age_seconds=47*60)
    bot.manage_positions(state, {})
    assert MINT in state['positions']
    assert state['closed_trades']==[]
    met=state['paper_trade_metrics'][MINT]
    assert met['pending_exit_reason']=='shadow_45m_expiry'
    saved=json.loads(json.dumps(state))
    bot.manage_positions(saved, primary(price=.000107))
    assert MINT not in saved['positions']
    assert saved['closed_trades'][0]['reason']=='shadow_45m_expiry'
    assert saved['closed_trades'][0]['paper_metrics']['exit_intent_latency_seconds'] >= 0


def test_missing_primary_quote_never_creates_sale(monkeypatch):
    state = opening(monkeypatch, age_seconds=47*60)
    bot.manage_positions(state, {MINT: dict(primary()[MINT], quote_source='geckoterminal_indicative')})
    assert MINT in state['positions']
    assert not state['closed_trades']
    assert state['paper_trade_metrics'][MINT]['pending_exit_reason']=='shadow_45m_expiry'
    assert MINT in state['exit_issues']


def test_low_liquidity_does_not_suppress_emergency_stop(monkeypatch):
    state = opening(monkeypatch, entry_liq=20000)
    bot.manage_positions(state, primary(price=.00008, liquidity=8500))
    assert MINT not in state['positions']
    assert state['closed_trades'][0]['reason']=='shadow_protective_stop'


def test_no_sale_if_router_cannot_prove_same_pinned_pool(monkeypatch):
    state = opening(monkeypatch, age_seconds=47*60)
    monkeypatch.setattr(bot,'SHADOW_EXIT_ROUTE_QUOTE_REQUIRED', True)
    monkeypatch.setattr(bot,'SOLANA_RPC_URL','https://example.invalid')
    monkeypatch.setattr(bot,'JUPITER_API_KEY','dummy')
    state['paper_trade_metrics'][MINT]['prebuy_safety']={'readonly_route':{'mint_decimals':6}}
    def fail(*args,**kwargs): raise ValueError('quote_not_pinned_pool')
    monkeypatch.setattr(bot, 'pinned_sell_quote', fail)
    bot.manage_positions(state, primary())
    assert MINT in state['positions']
    assert not state['closed_trades']
    assert state['paper_trade_metrics'][MINT]['pending_exit_reason']=='shadow_45m_expiry'
    assert state['exit_issues'][MINT]['reason']=='router_quote_quote_not_pinned_pool'
    assert state['cash_usd']==pytest.approx(34)


def test_exit_quote_with_pinned_pool_is_still_only_paper_estimate(monkeypatch):
    state = opening(monkeypatch, age_seconds=47*60)
    monkeypatch.setattr(bot,'SHADOW_EXIT_ROUTE_QUOTE_REQUIRED', True)
    monkeypatch.setattr(bot,'SOLANA_RPC_URL','https://example.invalid')
    monkeypatch.setattr(bot,'JUPITER_API_KEY','dummy')
    state['paper_trade_metrics'][MINT]['prebuy_safety']={'readonly_route':{
        'mint_decimals':6,'token_amount_raw':20_000_000_000,
        'quote_min_sol_lamports':20_000_000}}
    def quote(key,mint,pool,amount):
        assert mint==MINT and pool==POOL and amount>0
        return {'min_lamports':17_000_000,'out_lamports':18_000_000}
    monkeypatch.setattr(bot,'pinned_sell_quote',quote)
    bot.manage_positions(state, primary(price=.00011))
    assert MINT not in state['positions']
    trade=state['closed_trades'][0]
    assert trade['execution']=='paper_quote_backed_estimate'
    assert trade['exit_readonly_route']['minimum_sol_lamports']==17_000_000
    assert trade['proceeds_usd'] < 2.0  # conservative SOL quote anchor vs optimistic indexer


def test_pending_expiry_halts_new_entries_after_timeout(monkeypatch):
    state=opening(monkeypatch,age_seconds=47*60)
    bot.manage_positions(state,{})
    stamp=state['paper_trade_metrics'][MINT]['pending_exit_first_at']
    bot.shadow_exit_risk_guard(state,now=stamp+91)
    assert state['shadow_exit_safety_halt'] is True
    assert state['halted'] is True
    assert not state['closed_trades']
    assert bot.should_halt(state)


def test_severe_open_position_loss_halts_entries(monkeypatch):
    state=opening(monkeypatch)
    state['paper_trade_metrics'][MINT]['max_adverse_net_pct']=-93.5
    bot.shadow_risk_snapshot(state, interval=0)
    assert state['halted'] is True
    assert state['shadow_exit_safety_reason']=='severe_open_position_loss'
    assert MINT in state['positions']


def test_catastrophic_closed_shadow_trade_halts_new_entries(monkeypatch):
    state=opening(monkeypatch)
    bot.manage_positions(state,primary(price=.000015,liquidity=20000))
    assert state['closed_trades'][0]['pnl_pct'] < -50
    assert state['halted'] is True
    assert state['shadow_exit_safety_reason']=='catastrophic_closed_shadow_loss'


def test_high_price_fallback_is_not_an_exit(monkeypatch):
    state=opening(monkeypatch,age_seconds=47*60)
    bot.manage_positions(state,{MINT:{**primary(price=.00022)[MINT],
               'quote_source':'dexscreener_price_geckoterminal_depth_indicative'}})
    assert MINT in state['positions'] and state['cash_usd']==pytest.approx(34)
    assert not state['closed_trades']

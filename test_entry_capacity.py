"""Regression: at the slot limit discovery and exits continue, but entry work stops."""
import time
from dataclasses import asdict

import bot
from candidate_quality import CandidateQualityLedger
from rolling_discovery import RollingWatchlist


def mint(i):
    return chr(65 + i) * 44


def held(m):
    return asdict(bot.Position(token=m, symbol='HELD', name='Held',
        entry_price=0.001, amount_usd=10, token_units=10000,
        opened_at=time.time()-100, peak_price=0.001,
        last_price=0.001, last_price_at=time.time(), score=80,
        entry_pool_address='P'*44, entry_pool_dex='raydium'))


def wire_scan(monkeypatch, tmp_path):
    token = mint(2)
    pair = {
        'chainId': 'solana', 'dexId': 'raydium', 'pairAddress': 'B'*44,
        'baseToken': {'address': token, 'symbol': 'TEST', 'name': 'Test'},
        'quoteToken': {'address': bot.SOL_MINT},
        'priceUsd': '0.001', 'liquidity': {'usd': 100000},
        'marketCap': 2000000,
        'pairCreatedAt': int((time.time() - 86400)*1000),
        'volume': {'m5': 40000, 'h1': 100000},
        'priceChange': {'m5': 3, 'h1': 8, 'h6': 8, 'h24': 15},
        'txns': {'m5': {'buys': 35, 'sells': 20}},
        'url': 'https://example.test/pool'
    }
    calls = []
    def fake_get_json(url, *args, **kwargs):
        calls.append(url)
        return [pair]
    monkeypatch.setattr(bot, 'get_json', fake_get_json)
    monkeypatch.setattr(bot, 'latest_candidate_addresses', lambda: [token])
    monkeypatch.setattr(bot, 'select_entry_mints', lambda addresses, fresh_due, **kwargs: ([token], 0, 0))
    monkeypatch.setattr(bot, 'fill_sparse_momentum_scan', lambda addresses, selected, **kwargs: (selected, []))
    monkeypatch.setattr(bot, 'STATE_FILE', tmp_path/'clean_paper_state.json')
    monkeypatch.setattr(bot, '_quality_ledger', CandidateQualityLedger(tmp_path/'candidate_quality_state.json'))
    watch=RollingWatchlist(tmp_path/'rolling_discovery_state.json')
    watch.update([token])
    monkeypatch.setattr(bot, '_rolling_watch', watch)
    monkeypatch.setattr(bot, 'CANDIDATE_QUALITY_ENABLED', True)
    monkeypatch.setattr(bot, 'ROLLING_DISCOVERY_ENABLED', True)
    monkeypatch.setattr(bot, 'PAPER_MODE', True)
    monkeypatch.setattr(bot, 'MAX_POSITIONS', 2)  # legacy two-slot capacity regression
    # Focus v4 capacity tests on slot scheduling. v5 snapshot tests live elsewhere.
    monkeypatch.setattr(bot, 'PAPER_FINAL_SNAPSHOT_ENABLED', False)
    monkeypatch.setattr(bot, 'MOMENTUM_SWING_MODE', True)
    monkeypatch.setattr(bot, 'SNIPER_MODE', False)
    monkeypatch.setattr(bot, 'PAPER_SECONDARY_LIQUIDITY_ENABLED', False)
    monkeypatch.setattr(bot, 'PUMP_CURVE_LOOKUP_ENABLED', False)
    monkeypatch.setattr(bot, '_quality_pool_mints', [])
    monkeypatch.setattr(bot, '_verified_dex_mints', bot.OrderedDict())
    monkeypatch.setattr(bot, '_pending_entry_mints', bot.OrderedDict())
    monkeypatch.setattr(bot, '_entry_reject_until', bot.OrderedDict())
    monkeypatch.setattr(bot, '_first_discovered_at', bot.OrderedDict())
    monkeypatch.setattr(bot, '_entry_scan_next_at', 0.0)
    monkeypatch.setattr(bot, '_api_cooldown_until', 0.0)
    monkeypatch.setattr(bot, 'token_pairs', lambda m: [])
    monkeypatch.setattr(bot, 'pinned_pool_liquidity_evidence', lambda *a,**kw: {'verified': True})
    monkeypatch.setattr(bot, 'held_pool_fallback', lambda *a,**kw: None)
    monkeypatch.setattr(bot, 'should_halt', lambda s: False)
    return token, calls


def test_full_portfolio_still_scans_and_saves_but_skips_expensive_entry_checks(monkeypatch, tmp_path):
    records=[]
    monkeypatch.setattr(bot.log, 'info', lambda fmt, *args: records.append(fmt % args if args else fmt))
    token, calls = wire_scan(monkeypatch,tmp_path)
    state = bot.load_state()
    state['positions'] = {mint(0): held(mint(0)), mint(1): held(mint(1))}
    state['cash_usd'] = 16
    monkeypatch.setattr(bot, 'manage_positions', lambda *args,**kwargs: None)
    def fail(*args,**kwargs):
        raise AssertionError('full portfolio must not run reentry or prebuy entry checks')
    monkeypatch.setattr(bot, 'paper_reentry_watch_mints', fail)
    monkeypatch.setattr(bot, 'candidate_momentum_confirm', fail)
    monkeypatch.setattr(bot, 'paper_prebuy_sellability', fail)
    monkeypatch.setattr(bot, 'paper_buy', fail)
    bot._pending_entry_mints[token] = (time.time(), 'pending')
    bot.scan_once(state)
    assert calls, 'normal market scan should still run'
    assert len(state['positions']) == 2
    assert len(bot._pending_entry_mints) == 0
    assert bot.STATE_FILE.exists()
    assert any('PAPER ENTRY CAPACITY reason=position_limit' in msg for msg in records)
    assert any('discovery_active=True exits_active=True' in msg for msg in records)


def test_slot_freed_by_exit_can_fill_on_same_scan(monkeypatch, tmp_path):
    records=[]
    monkeypatch.setattr(bot.log, 'info', lambda fmt, *args: records.append(fmt % args if args else fmt))
    token, calls = wire_scan(monkeypatch,tmp_path)
    state = bot.load_state()
    state['positions'] = {mint(0): held(mint(0)), mint(1): held(mint(1))}
    state['cash_usd'] = 16
    managed=[]
    def simulate_exit(s, market, evidence):
        managed.append(True)
        s['positions'].pop(mint(0))
        s['cash_usd'] += 10
    monkeypatch.setattr(bot, 'manage_positions', simulate_exit)
    def fail(*args, **kwargs):
        raise AssertionError('full portfolio must not schedule reentries before exit')
    monkeypatch.setattr(bot, 'paper_reentry_watch_mints', fail)
    checks=[]
    def trend(*args, **kwargs):
        checks.append('trend')
        return True, 'confirmed:0.35pct'
    def prebuy(*args, **kwargs):
        checks.append('prebuy')
        return True, 'indicative'
    monkeypatch.setattr(bot, 'candidate_momentum_confirm', trend)
    monkeypatch.setattr(bot, 'paper_prebuy_sellability', prebuy)
    bot.scan_once(state)
    assert managed
    assert checks == ['trend', 'prebuy']
    assert token in state['positions']
    assert len(state['positions']) == 2
    assert any('PAPER BUY TEST' in msg for msg in records)
    assert bot.STATE_FILE.exists()


def test_paused_entry_resumes_next_scan_after_slot_frees(monkeypatch, tmp_path):
    token, calls = wire_scan(monkeypatch, tmp_path)
    state = bot.load_state()
    state['positions'] = {mint(0): held(mint(0)), mint(1): held(mint(1))}
    state['cash_usd'] = 16
    monkeypatch.setattr(bot, 'manage_positions', lambda *args, **kwargs: None)
    monkeypatch.setattr(bot, 'paper_reentry_watch_mints', lambda *args,**kwargs: [])
    checks=[]
    monkeypatch.setattr(bot, 'candidate_momentum_confirm',
                        lambda *a,**kw: (checks.append('trend') or True, 'confirmed:0.30pct'))
    monkeypatch.setattr(bot, 'paper_prebuy_sellability',
                        lambda *a,**kw: (checks.append('prebuy') or True, 'indicative'))
    bot.scan_once(state)
    assert checks == []
    assert token in bot._rolling_watch.records
    # On the next 15-second entry scan a closed position frees exactly one slot.
    state['positions'].pop(mint(0))
    state['cash_usd'] += 10
    monkeypatch.setattr(bot, '_entry_scan_next_at', 0.0)
    bot.scan_once(state)
    assert checks == ['trend', 'prebuy']
    assert token in state['positions']
    assert len(state['positions']) == 2

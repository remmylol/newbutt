"""No-network tests for varied Solana discovery and PAPER-only scan fairness."""
from pathlib import Path

import bot
from rolling_discovery import RollingWatchlist, rotating_pool_sources


def m(i):
    return 'A' * 41 + 'BCDEFGHJKLMNPQRSTUVWXYZ'[i]


def test_source_rotation_explores_multiple_pages_and_dexes():
    plans = [rotating_pool_sources(i) for i in range(20)]
    assert all(len(p) == 5 for p in plans)
    assert all(any(endpoint.startswith('dexes/') for endpoint, _ in p) for p in plans)
    distinct = {(endpoint, tuple(sorted(params.items()))) for p in plans for endpoint, params in p}
    assert len(distinct) >= 35, len(distinct)
    assert any(params.get('page', 1) > 3 for p in plans for _, params in p)
    assert len({params.get('duration') for p in plans for _, params in p if 'duration' in params}) == 4


def test_watchlist_roundtrip_persist_and_oldest_unscanned_priority(tmp_path):
    path = tmp_path / 'data' / 'rolling_discovery_state.json'
    watch = RollingWatchlist(path, limit=100, ttl_seconds=3600)
    assert watch.update([m(0), m(1), m(2)], {m(0): 123}, now=1000) == 3
    watch.mark_scanned([m(0)], now=1010)
    assert watch.priority_mints()[:2] == [m(1), m(2)]
    watch.source_round = 16
    assert watch.save(now=1011, force=True)
    restored = RollingWatchlist(path, limit=100, ttl_seconds=3600)
    restored.load(now=1020)
    assert restored.source_round == 16
    assert restored.priority_mints()[:2] == [m(1), m(2)]
    assert restored.update([m(2), m(3)], now=1050) == 1
    assert restored.priority_mints()[0] == m(3)
    restored.prune(now=4700)
    assert not restored.records


def test_corrupt_optional_watchlist_never_overwritten_and_does_not_block(tmp_path):
    path = tmp_path / 'rolling_discovery_state.json'
    path.write_text('{broken', encoding='utf8')
    watch = RollingWatchlist(path)
    watch.update([m(0)], now=1000)
    assert watch.read_only
    assert not watch.save(now=1100, force=True)
    assert path.read_text() == '{broken'


def test_bounded_records_reject_invalid_and_duplicates(tmp_path):
    watch = RollingWatchlist(tmp_path / 'watch.json', limit=50)
    assert watch.update([m(1), m(1), 'INVALID', 'So11111111111111111111111111111111111111112'], now=1000) == 2
    # SOL must be excluded by the bot rather than by this generic storage helper.
    assert 'INVALID' not in watch.records
    for i in range(21):
        token = 'B'*42 + 'ABCDEFGHJKLMNPQRSTUVWXYZ'[i]
        watch.update([token], now=1000 + i)
    assert watch.scan_counts([m(1), 'none'])[0] == 1


def test_rolling_pool_candidates_rotate_actual_endpoint_requests(monkeypatch, tmp_path):
    class Response:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {'data': [], 'included': []}
    calls = []
    def fake_get(url, **kwargs):
        calls.append((url, kwargs['params']))
        return Response()
    monkeypatch.setattr(bot, 'ROLLING_DISCOVERY_ENABLED', True)
    monkeypatch.setattr(bot, 'MOMENTUM_SWING_MODE', True)
    monkeypatch.setattr(bot, '_quality_source_round', 0)
    monkeypatch.setattr(bot, '_quality_source_backoff', {})
    monkeypatch.setattr(bot, '_quality_cache', {'until':0, 'scores':{}, 'mints':[]})
    monkeypatch.setattr(bot, '_rolling_watch', RollingWatchlist(tmp_path / 'watch.json'))
    monkeypatch.setattr(bot.session, 'get', fake_get)
    monkeypatch.setattr(bot, 'QUALITY_POOL_CACHE_SECONDS', 0)
    bot.quality_pool_candidates()
    first = list(calls)
    monkeypatch.setattr(bot, '_quality_cache', {'until':0, 'scores':{}, 'mints':[]})
    bot.quality_pool_candidates()
    assert len(calls) == 10
    assert set(map(str, first)) != set(map(str, calls[5:]))


def test_new_watchlist_priority_preserves_pending_and_skips_cooldowns(monkeypatch,tmp_path):
    watch = RollingWatchlist(tmp_path / 'watch.json')
    a,b,c,d = (m(i) for i in range(4))
    watch.update([a,b,c], now=1000)
    watch.mark_scanned([a], now=1001)
    monkeypatch.setattr(bot, '_rolling_watch',watch)
    monkeypatch.setattr(bot, 'ROLLING_DISCOVERY_ENABLED', True)
    monkeypatch.setattr(bot, 'MOMENTUM_SWING_MODE', True)
    monkeypatch.setattr(bot, 'SNIPER_MODE', False)
    monkeypatch.setattr(bot, '_quality_pool_mints', [])
    monkeypatch.setattr(bot, '_verified_dex_mints',bot.OrderedDict())
    monkeypatch.setattr(bot, '_pending_entry_mints',bot.OrderedDict())
    monkeypatch.setattr(bot, '_entry_reject_until',bot.OrderedDict([(b, float('inf'))]))
    monkeypatch.setattr(bot, '_discovery_priority',{})
    monkeypatch.setattr(bot, 'verified_dex_due', lambda limit: [])
    monkeypatch.setattr(bot, 'paper_reentry_watch_mints', lambda state: [])
    monkeypatch.setattr(bot, 'ROLLING_DISCOVERY_FRESH_SCAN_QUOTA', 24)
    selected,_,_ = bot.select_entry_mints([a,b,c,d],[],count=4,cursor=0,reentry_due=[])
    assert c in selected
    assert b not in selected


def test_discovery_retains_distinct_validated_mints_across_refreshes(monkeypatch, tmp_path):
    """Different API snapshots should accumulate into a rotating watchlist."""
    watch = RollingWatchlist(tmp_path / 'data' / 'rolling_discovery_state.json')
    phase = [0]
    class Response:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            address = m(phase[0])
            return {
                'data': [{
                    'relationships': {
                        'base_token': {'data': {'id': 'solana_' + address}},
                        'quote_token': {'data': {'id': 'solana_' + bot.SOL_MINT}},
                        'dex': {'data': {'id': 'solana_raydium'}},
                    },
                    'attributes': {
                        'reserve_in_usd': '55000', 'base_token_price_usd': '0.1',
                        'market_cap_usd': '5000000', 'volume_usd': {'h1': '30000'},
                        'price_change_percentage': {'m5': '1.1','h1':'3.0','h6':'4.0','h24':'6.0'},
                    },
                }], 'included': []
            }
    monkeypatch.setattr(bot.session, 'get', lambda *args, **kwargs: Response())
    monkeypatch.setattr(bot, 'get_json', lambda *args, **kwargs: [])
    monkeypatch.setattr(bot, 'dex_request', lambda *args, **kwargs: {'pairs': []})
    monkeypatch.setattr(bot, 'fomo_board_candidates', lambda: {'graduated': [], 'trending': []})
    monkeypatch.setattr(bot, 'pumpfun_board_candidates', lambda: {'movers': [], 'trending': []})
    monkeypatch.setattr(bot, 'fresh_launch_snapshot', lambda: [])
    monkeypatch.setattr(bot, '_rolling_watch', watch)
    monkeypatch.setattr(bot, '_quality_source_round', 0)
    monkeypatch.setattr(bot, '_quality_source_backoff', {})
    monkeypatch.setattr(bot, '_discovery_round', 0)
    monkeypatch.setattr(bot, '_discovery_seen', set())
    monkeypatch.setattr(bot, '_discovery_cache', {'until': 0, 'addresses': []})
    monkeypatch.setattr(bot, '_quality_cache', {'until': 0, 'mints': [], 'scores': {}})
    monkeypatch.setattr(bot, 'QUALITY_POOL_CACHE_SECONDS', 0)
    monkeypatch.setattr(bot, 'DISCOVERY_CACHE_SECONDS', 0)
    monkeypatch.setattr(bot, 'ROLLING_DISCOVERY_ENABLED', True)
    monkeypatch.setattr(bot, 'MOMENTUM_SWING_MODE', True)
    monkeypatch.setattr(bot, 'SNIPER_MODE', False)
    monkeypatch.setattr(bot, 'DISCOVERY_SEARCH_TERMS', ())
    one = bot.latest_candidate_addresses()
    assert m(0) in one
    phase[0] = 1
    bot._discovery_cache['until'] = 0
    bot._quality_cache['until'] = 0
    two = bot.latest_candidate_addresses()
    assert m(0) in two and m(1) in two
    assert len(watch.records) == 2
    assert watch.source_round == 2
    watch.save(force=True)
    assert (tmp_path / 'data' / 'rolling_discovery_state.json').exists()

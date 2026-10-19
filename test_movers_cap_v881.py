"""v8.8.1: Movers are discovery-only; strict sub-$250k VERIFIED cap before real buy.
All tests offline; no RPC, website, or transaction broadcast.
"""
import copy
import logging
from pathlib import Path
import requests
import pytest
import bot
from clean_boot import apply_clean_profile
from fresh_batch_discovery import FreshBatchQueue
from pumpfun_boards import board_mints
from shadow_age_tracks_v876 import AgeTrack
from live_age_execution_v879 import LiveAgeTrack
from test_live_primary_v879 import FakeBroker, MINT, SOL, ROUTES, NOW, pair


LIMIT = 250_000.


def make_track(tmp_path):
    return AgeTrack(name='0_60', min_age=0., path=tmp_path/'paper.json',
                    halts_enabled=False, take_profit_net_pct=25.,
                    exit_on_profitable_reversal=True, max_entry_market_cap_usd=LIMIT)


@pytest.mark.parametrize('cap,expected',[(100_000.,'opened'),
                                         (249_999.99,'opened'),
                                         (250_000.,'market_cap_at_or_above_limit'),
                                         (250_000.01,'market_cap_at_or_above_limit'),
                                         (5_000_000.,'market_cap_at_or_above_limit')])
def test_primary_never_buys_at_or_above_limit(tmp_path,cap,expected):
    t=make_track(tmp_path)
    result=t.observe(pair(age=6.,cap=cap),sol_mint=SOL,dexes=ROUTES,now=NOW)
    assert result==expected
    assert bool(t.positions) == (expected=='opened')
    if expected=='opened':
        assert t.positions[MINT]['entry_market_cap_usd']==cap
        assert t.positions[MINT]['entry_volume5_usd']==5000.
        assert t.positions[MINT]['entry_buys5']==36
        assert t.positions[MINT]['entry_sells5']==15
    else:
        assert not t.seen and not t.closed and t.open_count==0


def test_missing_actual_marketcap_does_not_fallback_to_fdv(tmp_path):
    t=make_track(tmp_path)
    p=pair(age=6.,cap=100_000.);p.pop('marketCap');p['fdv']=100_000
    assert t.observe(p,sol_mint=SOL,dexes=ROUTES,now=NOW)=='unverified_pool'
    assert not t.positions


def test_cap_does_not_block_sell_when_token_market_cap_goes_above_limit(tmp_path):
    t=make_track(tmp_path)
    assert t.observe(pair(age=6.,cap=249_900.),sol_mint=SOL,dexes=ROUTES,now=NOW)=='opened'
    # SELLING always takes precedence over the market-cap entry ceiling.
    assert t.observe(pair(age=6.5,cap=500_000.,price=.0002),sol_mint=SOL,
                     dexes=ROUTES,now=NOW+30)=='closed:protective_net_stop'
    assert len(t.closed)==1 and t.closed[0]['entry_market_cap_usd']==249_900.
    assert not t.positions


def test_live_entry_ceiling_blocks_transaction_and_held_position_can_exit(tmp_path):
    broker=FakeBroker(tmp_path/'journal')
    track=LiveAgeTrack(broker=broker,name='0_60',min_age=0.,path=tmp_path/'live.json',
                       size=2.,max_positions=5,starting_capital=36.,halts_enabled=False,
                       take_profit_net_pct=25.,exit_on_profitable_reversal=True,
                       allow_new_buys=True,max_entry_market_cap_usd=LIMIT)
    assert track.observe(pair(age=8.,cap=250_000.),sol_mint=SOL,dexes=ROUTES,
                         now=NOW)=='market_cap_at_or_above_limit'
    assert broker.calls==[]
    assert track.observe(pair(age=8.,cap=249_900.),sol_mint=SOL,dexes=ROUTES,now=NOW+1)=='opened'
    assert len(broker.calls)==1 and broker.calls[0][0]=='buy'
    broker.exit_net_pct=-15.
    assert track.observe(pair(age=9.,cap=900_000.),sol_mint=SOL,dexes=ROUTES,
                         now=NOW+10)=='closed:protective_net_stop'
    assert [action for action,_,_ in broker.calls]==['buy','sell']
    assert track.closed[-1]['entry_market_cap_usd']==249_900.
    assert track.closed[-1]['entry_volume5_usd']==5000.


def test_launcher_cap_is_hard_ceiling_even_with_historical_railway_override():
    d={'CLEAN_MARKET_CAP_MAX_USD':'5000000',
       'CLEAN_MARKET_CAP_MIN_USD':'35000',
       'CLEAN_PUMPFUN_BOARD_JWT':'JWT_SECRET',
       'CLEAN_PUMPFUN_BOARD_SESSION_ID':'SID_SECRET',
       'CLEAN_PUMPFUN_BOARD_USER_ID':'UID_SECRET'}
    apply_clean_profile(d)
    assert d['MOMENTUM_MARKET_CAP_MAX_USD']=='250000'
    assert d['PUMPFUN_BOARDS_ENABLED']=='true'
    assert d['PUMPFUN_MOVERS_ENABLED']=='true'
    assert d['PUMPFUN_BOARD_JWT']=='JWT_SECRET'
    assert d['PUMPFUN_BOARD_SESSION_ID']=='SID_SECRET'
    assert d['PUMPFUN_BOARD_USER_ID']=='UID_SECRET'


def test_lower_optional_cap_works_but_inverted_bounds_reject():
    d={'CLEAN_MARKET_CAP_MAX_USD':'200000'}
    apply_clean_profile(d)
    assert d['MOMENTUM_MARKET_CAP_MAX_USD']=='200000'
    with pytest.raises(ValueError,match='cannot exceed'):
        apply_clean_profile({'CLEAN_MARKET_CAP_MIN_USD':'250000'})


def test_board_parser_supports_nested_movers_payload_and_filters_duplicates():
    a='A'*44;b='B'*44
    payload={'data':{'movers':[{'mint':a},{'token':{'address':b}},
                               {'mint':a},{'mint':'bad'}]}}
    assert board_mints(payload,limit=30)==[a,b]


def board_ready(monkeypatch):
    monkeypatch.setattr(bot,'PUMPFUN_BOARDS_ENABLED',True)
    monkeypatch.setattr(bot,'PUMPFUN_MOVERS_ENABLED',True)
    monkeypatch.setattr(bot,'PUMPFUN_BOARD_JWT','SENSITIVE_JWT')
    monkeypatch.setattr(bot,'PUMPFUN_BOARD_SESSION_ID','SENSITIVE_SESSION')
    monkeypatch.setattr(bot,'PUMPFUN_BOARD_USER_ID','SENSITIVE_USER')
    monkeypatch.setattr(bot,'PUMPFUN_BOARD_TIER','1')
    monkeypatch.setattr(bot,'PUMPFUN_BOARDS_LIMIT',30)
    monkeypatch.setattr(bot,'PUMPFUN_BOARDS_CACHE_SECONDS',60)
    monkeypatch.setattr(bot,'_board_cache',{
        'until':0.,'movers':[],'trending':[],'warn_at':0.,
        'status':'not_polled','last_success':0.,'failures':0})


def test_authenticated_movers_poll_bounded_and_cached(monkeypatch):
    board_ready(monkeypatch)
    calls=[]
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'data':{'movers':[{'mint':'A'*44},{'mint':'B'*44}]}}
    def get(url,**kw):
        calls.append((url,kw));return Response()
    monkeypatch.setattr(bot.session,'get',get)
    result=bot.pumpfun_board_candidates()
    assert result['movers']==['A'*44,'B'*44]
    assert len(calls)==1
    url,kw=calls[0]
    assert url=='https://advanced-api-v2.pump.fun/boards/movers'
    assert kw['timeout']==8
    assert kw['params']['limit']==30
    assert kw['headers']['Authorization']=='Bearer SENSITIVE_JWT'
    assert bot.pumpfun_board_candidates()==result
    assert len(calls)==1 # no extra HTTP during 60s cache period


def test_missing_auth_is_safe_and_never_calls_remote(monkeypatch):
    board_ready(monkeypatch)
    monkeypatch.setattr(bot,'PUMPFUN_BOARD_JWT','')
    monkeypatch.setattr(bot.session,'get',lambda *a,**k:pytest.fail('must not request without auth'))
    assert bot.pumpfun_board_candidates()=={'movers':[],'trending':[]}
    assert bot._board_cache['status']=='missing_auth'


def test_auth_error_does_not_expose_secrets_or_stop_discovery(monkeypatch,caplog):
    board_ready(monkeypatch)
    class Response:
        status_code=401
        headers={}
        def raise_for_status(self):raise requests.HTTPError('401',response=self)
    monkeypatch.setattr(bot.session,'get',lambda *a,**k:Response())
    with caplog.at_level(logging.WARNING):
        assert bot.pumpfun_board_candidates()=={'movers':[],'trending':[]}
    assert bot._board_cache['status']=='http_401'
    assert bot._board_cache['until']>bot._board_cache['last_success']
    assert 'SENSITIVE_' not in caplog.text


def test_movers_enter_bounded_priority_queue_not_auto_buy(tmp_path):
    q=FreshBatchQueue(tmp_path/'queue.json')
    movers=['A'*44,'B'*44]
    ordinary=['C'*44,'D'*44]
    q.offer({movers[0]:180,movers[1]:170,ordinary[0]:200,ordinary[1]:190},
            feed_mints=set(movers),pool_hints=set(ordinary),now=NOW)
    assert not any(r.get('shadow_created_at') for r in q.active.values())
    due=q.due_shadow(now=NOW,limit=2,priority_hint_mints=set(movers),priority_hint_quota=1)
    assert len(due)==2 and due[0]==movers[0]
    assert all(q.active[m]['checked']==0 for m in due)

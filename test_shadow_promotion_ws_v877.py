"""No live Solana or market calls. Promotion preserves original 0-60 accounting."""
import json
import threading
import time
from pathlib import Path

import bot
from clean_boot import PAPER_PRESET, apply_clean_profile
from shadow_age_tracks_v876 import BackgroundAgeStudy
from solana_age_ws_v877 import SolanaPoolWatcher, ws_url_for_rpc

SOL=bot.SOL_MINT
MINT='A'*44
POOL='B'*44
POOL2='C'*44


def pair(now, *, age=3., price=.0004, liq=50000., pool=POOL, mint=MINT):
    return {'chainId':'solana','dexId':'pumpswap','pairAddress':pool,
            'baseToken':{'address':mint,'symbol':'TEST'},'quoteToken':{'address':SOL},
            'pairCreatedAt':(now-age*60)*1000,'priceUsd':str(price),
            'marketCap':400000,'liquidity':{'usd':liq},
            'volume':{'m5':5000},'txns':{'m5':{'buys':36,'sells':15}},
            'priceChange':{'m5':9}}


def test_clean_boot_promotes_original_age_track_only():
    assert PAPER_PRESET['AGE_0_60_PRIMARY']=='true'
    assert PAPER_PRESET['AGE_0_60_WS_ENABLED']=='true'
    assert PAPER_PRESET['LIVE_TRADING_ENABLED']=='false'
    assert PAPER_PRESET['PAPER_MODE']=='true'
    from shadow_age_tracks_v876 import AgeTrack
    assert not hasattr(AgeTrack,'onchain_new_trading_rules')


def test_clean_boot_wss_rpc_override_keeps_secrets_out_of_logs():
    env={'CLEAN_STATE_FILE':'/data/example.json', 'CLEAN_SOLANA_RPC_URL':'https://private.rpc/x',
         'CLEAN_SOLANA_WS_URL':'wss://private.rpc/custom?q=SECRET', 'PAPER_MODE':'false'}
    _, state=apply_clean_profile(env)
    assert state=='/data/example.json'
    assert env['AGE_0_60_PRIMARY']=='true' and env['PAPER_MODE']=='true'
    assert env['LIVE_TRADING_ENABLED']=='false'
    assert env['AGE_0_60_WS_RPC_URL']=='wss://private.rpc/custom?q=SECRET'


def test_ws_endpoint_derivation():
    assert ws_url_for_rpc('https://abc.example/path?q=1')=='wss://abc.example/path?q=1'
    assert ws_url_for_rpc('https://abc.example', 'wss://rpc.other/path')=='wss://rpc.other/path'


def test_ws_does_not_modify_5_60_or_legacy_portfolio(monkeypatch, tmp_path):
    stamp=time.time()
    base=tmp_path/'historical.json'
    original='{"positions":{},"halted":true,"cash_usd":34.33}'
    base.write_text(original)
    study=BackgroundAgeStudy(state_file=base)
    observed=pair(stamp)
    study.pair_observed(observed,sol_mint=SOL,dexes={'pumpswap'},now=stamp)
    study.save()
    before=study.tracks[1].stats()
    monkeypatch.setattr(bot,'_age_study',study)
    monkeypatch.setattr(bot,'AGE_0_60_PRIMARY',True)
    monkeypatch.setattr(bot,'AGE_0_60_WS_ENABLED',True)
    monkeypatch.setattr(bot,'_age_ws_last_fetch_at',0.)
    monkeypatch.setattr(bot,'_age_ws_backoff_until',0.)
    bot._age_ws_last_per_mint.clear()
    fetched=[]
    class Result:
        status_code=200
        def raise_for_status(self):pass
        def json(self):return [pair(stamp,price=.00048)]
    def fake_get(url,**kw):
        fetched.append(url)
        return Result()
    monkeypatch.setattr(bot.requests,'get',fake_get)
    bot.promoted_ws_observation(MINT,POOL)
    assert len(fetched)==1
    assert len(study.tracks[0].closed)==1
    assert study.tracks[0].closed[-1]['reason']=='take_profit_8pct'
    assert study.tracks[1].stats()==before
    assert base.read_text()==original
    # A WS repeat cannot reenter a token or sell twice.
    bot._age_ws_last_per_mint.clear()
    bot._age_ws_last_fetch_at=0.
    bot.promoted_ws_observation(MINT,POOL)
    assert len(study.tracks[0].closed)==1 and len(fetched)==1


def test_ws_halt_persists_and_same_rules_apply(monkeypatch,tmp_path):
    stamp=time.time()
    study=BackgroundAgeStudy(state_file=tmp_path/'historical.json')
    study.pair_observed(pair(stamp),sol_mint=SOL,dexes={'pumpswap'},now=stamp)
    # A 75% crash triggers original -9% stop and original safety halt.
    study.tracks[0].observe(pair(stamp,age=3.1,price=.0001),sol_mint=SOL,
                            dexes={'pumpswap'},now=stamp+10)
    study.save()
    assert study.tracks[0].halted and study.tracks[0].halt_reason=='catastrophic_closed_virtual_loss'
    fresh=BackgroundAgeStudy(state_file=tmp_path/'historical.json')
    fresh.tracks[0].load()
    assert fresh.tracks[0].halted and fresh.tracks[0].halt_reason=='catastrophic_closed_virtual_loss'
    assert fresh.tracks[0].observe(pair(stamp,mint='D'*44,pool='E'*44),
         sol_mint=SOL,dexes={'pumpswap'},now=stamp+12)=='track_safety_halt'
    assert not fresh.tracks[0].positions


def test_ws_subscribe_notifications_only_from_held_pools(monkeypatch):
    from types import SimpleNamespace
    watcher = None
    fired=[]
    class Timeout(Exception): pass
    class FakeWS:
        def __init__(self):self.q=[];self.closed=False
        def settimeout(self,timeout):pass
        def send(self,raw):
            request=json.loads(raw)
            assert request['method']=='accountSubscribe'
            assert request['params'][0]==POOL
            self.q.append(json.dumps({'id':request['id'],'result':7}))
            self.q.append(json.dumps({'method':'accountNotification','params':{'subscription':8}}))
            self.q.append(json.dumps({'method':'accountNotification','params':{'subscription':7}}))
        def recv(self):
            if self.q:return self.q.pop(0)
            raise Timeout()
        def close(self):self.closed=True
    fake=SimpleNamespace(create_connection=lambda *a,**kw:FakeWS(),
                         WebSocketTimeoutException=Timeout)
    monkeypatch.setitem(__import__('sys').modules,'websocket',fake)
    def wake(mint,pool):
        fired.append((mint,pool)); watcher.stop()
    watcher=SolanaPoolWatcher(rpc_ws_url='wss://unused.example',
            held_pools=lambda:{MINT:POOL},on_change=wake)
    watcher._run()
    assert fired==[(MINT,POOL)]
    assert watcher.notifications==1


def test_promoted_old_halt_is_nonblocking_history_is_preserved(tmp_path):
    """A v8.7.6 catastrophic-loss ledger remains intact after promotion."""
    base=tmp_path/'historical.json'
    path=tmp_path/'age_study_0_60_historical.json'
    original={'version':1,'name':'0_60','cash':33.4847009072,
        'positions':{},'closed':[{'pnl_usd':-1.514368,'symbol':'Snlsiren',
                'reason':'protective_net_stop'}],
        'seen':[MINT], 'halted':True,
        'halt_reason':'catastrophic_closed_virtual_loss'}
    path.write_text(json.dumps(original))
    study=BackgroundAgeStudy(state_file=base,promoted_0_60_no_halts=True)
    track=study.tracks[0]
    assert track.path==path
    track.load()
    assert track.halted is False
    assert track.halt_reason is None
    assert track.halts_enabled is False
    assert track.historical_halt_reason=='catastrophic_closed_virtual_loss'
    assert track.closed==original['closed']
    assert track.cash==original['cash']
    assert track.positions==original['positions']
    assert study.tracks[1].halts_enabled is True
    assert study.tracks[1].cash == 36.
    assert track.save() is True
    after=json.loads(path.read_text())
    assert after['halted'] is False
    assert after['historical_halt_reason']=='catastrophic_closed_virtual_loss'
    assert after['closed']==original['closed']
    assert after['cash']==original['cash']
    assert after['positions']==original['positions']
    assert after['seen']==original['seen']
    # A second startup cannot erase the migrated ledger.
    again=BackgroundAgeStudy(state_file=base,promoted_0_60_no_halts=True)
    again.tracks[0].load()
    assert again.tracks[0].historical_halt_reason=='catastrophic_closed_virtual_loss'
    assert again.tracks[0].cash==original['cash']


def test_promoted_stop_and_liquidity_exit_without_any_loss_halt(tmp_path):
    stamp=time.time()
    study=BackgroundAgeStudy(state_file=tmp_path/'historical.json',promoted_0_60_no_halts=True)
    track=study.tracks[0]
    assert track.observe(pair(stamp),sol_mint=SOL,dexes={'pumpswap'},now=stamp)=='opened'
    assert track.observe(pair(stamp,age=3.1,price=.00008),sol_mint=SOL,
                         dexes={'pumpswap'},now=stamp+15)=='closed:protective_net_stop'
    assert track.closed[-1]['net_pct']<-50
    assert not track.halted and not track.halts_enabled
    # A catastrophic loss must not disable new 0-60 entries; different mint.
    mint2='D'*44; pool2='E'*44
    assert track.observe(pair(stamp+30,mint=mint2,pool=pool2),sol_mint=SOL,
                         dexes={'pumpswap'},now=stamp+30)=='opened'
    assert track.observe(pair(stamp+45,mint=mint2,pool=pool2,liq=27000.),
                         sol_mint=SOL,dexes={'pumpswap'},now=stamp+45)=='closed:liquidity_drop_40pct'
    assert not track.halted
    assert track.closed[-1]['reason']=='liquidity_drop_40pct'
    assert not study.tracks[1].halted


def test_original_background_mode_still_halts_at_catastrophic_loss(tmp_path):
    stamp=time.time()
    study=BackgroundAgeStudy(state_file=tmp_path/'historical.json')
    t=study.tracks[0]
    assert t.halts_enabled is True
    assert t.observe(pair(stamp),sol_mint=SOL,dexes={'pumpswap'},now=stamp)=='opened'
    assert t.observe(pair(stamp,age=3.1,price=.00008),sol_mint=SOL,
                     dexes={'pumpswap'},now=stamp+15)=='closed:protective_net_stop'
    assert t.halted and t.halt_reason=='catastrophic_closed_virtual_loss'

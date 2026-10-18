"""Historical signal handoff regressions updated for restored v8.6.4 snapshot parity."""
from __future__ import annotations
import time
from collections import OrderedDict
import pytest
import bot
from early_watch_shadow_v80 import shadow_entry_diagnostic
from fresh_batch_discovery import FreshBatchQueue
from candidate_pipeline_v74 import CandidatePipeline
from test_shadow_primary_v86 import pair,ledger,MINT,POOL
from shadow_pool_discovery_v861 import early_pool_hints
from datetime import datetime,timezone


def test_original_shadow_signal_uses_original_observed_price(monkeypatch):
    monkeypatch.setattr(bot,'PAPER_SHADOW_ACTIVE',True)
    first=pair(age=11.8, price=.001,change=10,volume=7000,buys=40,sells=15)
    snapshot=bot.shadow_paper_candidate(first)
    assert snapshot
    # In the intervening seconds the 5m sample flips, and the pool ages beyond
    # 12m. Neither change undoes the legitimate first-snapshot signal.
    second=pair(age=12.1,price=.0012,change=-20,volume=100,buys=1,sells=9)
    # Same pool identity/birth; only age in the provided fixture changes.
    state=ledger()
    assert bot.shadow_paper_entry(state,second,signal_candidate=snapshot) == (True,'paper_shadow_bought')
    assert state['positions'][MINT]['amount_usd']==2
    assert state['paper_trade_metrics'][MINT]['entry_market_price']==pytest.approx(.001)
    assert state['paper_trade_metrics'][MINT]['entry_5m_change_pct']==10
    assert state['paper_trade_metrics'][MINT]['shadow_signal_snapshot_locked'] is True
    assert state['paper_trade_metrics'][MINT]['shadow_execution_age_minutes'] < 12


@pytest.mark.parametrize('modified',[
    {'pairAddress':'B'*44}, {'baseToken':{'address':'C'*44}},
])
def test_original_signal_cannot_be_redirected_to_other_pool(monkeypatch, modified):
    monkeypatch.setattr(bot,'PAPER_SHADOW_ACTIVE',True)
    original=pair()
    signal=bot.shadow_paper_candidate(original)
    updated={**original,**modified}
    state=ledger()
    assert bot.shadow_paper_entry(state,updated,signal_candidate=signal)==(False,'shadow_signal_snapshot_mismatch')
    assert state['cash_usd']==36 and state['positions']=={}


def test_original_signal_does_not_require_second_price_or_liquidity(monkeypatch):
    monkeypatch.setattr(bot,'PAPER_SHADOW_ACTIVE',True)
    original=pair()
    signal=bot.shadow_paper_candidate(original)
    # The approved original observation is unchanged. There is no second
    # market lookup in the restored strategy, unlike the v8.6.2 model.
    updated={**original,'priceUsd':'0','liquidity':{'usd':0}}
    state=ledger()
    assert bot.shadow_paper_entry(state,updated,signal_candidate=signal)==(True,'paper_shadow_bought')
    assert state['paper_trade_metrics'][MINT]['entry_market_price']==pytest.approx(.0001)


def test_one_minute_old_pool_is_watched_just_before_five_minutes(tmp_path):
    stamp=time.time()
    one=pair(age=1,volume=10000)
    reason,watch,e=shadow_entry_diagnostic(one,sol_mint=bot.SOL_MINT,dexes=bot.active_entry_routes())
    assert reason=='shadow_wait_age_5m' and watch and e['age']<2
    q=FreshBatchQueue(tmp_path/'q.json')
    q.offer({MINT:20},now=stamp)
    born=one['pairCreatedAt']/1000
    assert q.schedule_shadow_recheck(MINT,reason,created_at=born,now=stamp,delay_seconds=45)
    due=q.rejected[MINT]['until']-stamp
    assert 220<=due<=240, 'avoid exhausting attempts before the 5m entry window'
    assert q.resume_shadow_due(now=stamp+due+1)==[MINT]
    assert q.due_shadow(now=stamp+due+1)[0]==MINT


def test_gecko_discovers_sub_three_minute_pools_for_early_watching():
    now=time.time()
    payload={'data':[{'attributes':{'pool_created_at':datetime.fromtimestamp(now-75,timezone.utc).isoformat(),
        'reserve_in_usd':'30000','base_token_price_usd':'0.01'},
        'relationships':{'base_token':{'data':{'id':'solana_'+MINT}},
        'quote_token':{'data':{'id':'solana_'+bot.SOL_MINT}},
        'dex':{'data':{'id':'solana_pump_swap'}}}}]}
    found=early_pool_hints(payload,sol_mint=bot.SOL_MINT,supported_dexes=('pumpswap','raydium'),now=now)
    assert MINT in found


def test_full_scanner_uses_first_shadow_snapshot_even_if_later_price_falls(monkeypatch,tmp_path):
    from early_watch_shadow_v80 import EarlyShadow
    monkeypatch.setattr(bot,'PAPER_SHADOW_ACTIVE',True)
    q=FreshBatchQueue(tmp_path/'queue.json');q.offer({MINT:900},novel_mints={MINT})
    pipeline=CandidatePipeline(tmp_path/'pipeline.json');pipeline.register_sources({'geckoterminal':[MINT]})
    first=pair(age=7,price=.001,change=5.0,volume=5000,buys=20,sells=5)
    second=pair(age=7,price=.0011,change=-7.0,volume=20,buys=0,sells=4)
    monkeypatch.setattr(bot,'get_json',lambda url,*a,**kw:[first] if '/tokens/v1/solana/' in url else [])
    monkeypatch.setattr(bot,'latest_candidate_addresses',lambda:[MINT])
    monkeypatch.setattr(bot,'_fresh_batch',q);monkeypatch.setattr(bot,'_early_shadow_v80',EarlyShadow(tmp_path/'shadow.json'))
    monkeypatch.setattr(bot,'_candidate_pipeline',pipeline)
    def fresh_pairs(mint):
        bot._token_pairs_healthy[mint]=True
        return [second]
    monkeypatch.setattr(bot,'token_pairs',fresh_pairs)
    for field,val in (
        ('FRESH_BATCH_DISCOVERY_ENABLED',True),('CANDIDATE_QUALITY_ENABLED',False),
        ('ROLLING_DISCOVERY_ENABLED',False),('FRESH_LAUNCH_FEED_ENABLED',False),
        ('PAPER_SECONDARY_LIQUIDITY_ENABLED',False),('PUMP_CURVE_LOOKUP_ENABLED',False),
        ('_verified_dex_mints',OrderedDict()),('_pending_entry_mints',OrderedDict()),
        ('_entry_reject_until',OrderedDict()),('_entry_reject_reason',{}),
        ('_entry_scan_next_at',0.),('_api_cooldown_until',0.),
        ('STATE_FILE',tmp_path/'ledger.json')):
        monkeypatch.setattr(bot,field,val)
    monkeypatch.setattr(bot,'paper_reentry_watch_mints',lambda *a,**kw:[])
    monkeypatch.setattr(bot,'should_halt',lambda *a,**kw:False)
    state=ledger();bot.scan_once(state)
    assert MINT in state['positions']
    assert state['paper_trade_metrics'][MINT]['entry_market_price']==pytest.approx(.001)
    assert state['paper_trade_metrics'][MINT]['entry_5m_change_pct']==5.0

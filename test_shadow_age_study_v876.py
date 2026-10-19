"""Isolated 0-60 and 5-60 age-study research portfolios (no primary trades)."""
from __future__ import annotations
import json
from pathlib import Path
import pytest
import bot
from shadow_age_tracks_v876 import AgeTrack, BackgroundAgeStudy
from shadow_pool_discovery_v861 import early_pool_hints

SOL=bot.SOL_MINT
MINT='A'*44
POOL='B'*44
POOL2='C'*44
NOW=1_800_000_000.
ROUTES={'pumpswap'}


def pair(*,age=3.,mint=MINT,pool=POOL,price=.0004,liq=50_000.,cap=400_000.,
         volume=5000.,buys=36,sells=15,change=9.,born=None):
    return {'chainId':'solana','dexId':'pumpswap','pairAddress':pool,
            'baseToken':{'address':mint,'symbol':'TEST'},
            'quoteToken':{'address':SOL},'pairCreatedAt':(NOW-age*60 if born is None else born)*1000,
            'priceUsd':str(price),'marketCap':cap,'liquidity':{'usd':liq},
            'volume':{'m5':volume},'txns':{'m5':{'buys':buys,'sells':sells}},
            'priceChange':{'m5':change}}


def study(tmp_path):
    return BackgroundAgeStudy(state_file=tmp_path/'primary.json',starting_capital=36)


def test_two_independent_portfolios_and_windows(tmp_path):
    s=study(tmp_path)
    events=s.pair_observed(pair(age=3),sol_mint=SOL,dexes=ROUTES,now=NOW)
    assert events=={'0_60':'opened'}
    assert s.tracks[0].cash==34 and s.tracks[1].cash==36
    assert MINT in s.tracks[0].positions and MINT not in s.tracks[1].positions
    events=s.pair_observed(pair(age=6,price=.000405),sol_mint=SOL,dexes=ROUTES,now=NOW+180)
    assert events=={'5_60':'opened'}
    assert s.tracks[0].cash==34 and s.tracks[1].cash==34
    assert s.tracks[0].positions[MINT]['entry_5m_partial'] is True
    assert s.tracks[1].positions[MINT]['entry_5m_partial'] is False


def test_late_age_qualified_entry_uses_both_tracks(tmp_path):
    s=study(tmp_path)
    events=s.pair_observed(pair(age=42),sol_mint=SOL,dexes=ROUTES,now=NOW)
    assert events=={'0_60':'opened','5_60':'opened'}
    assert all(t.positions[MINT]['entry_age_minutes']==42 for t in s.tracks)


def test_zero_to_sixty_has_no_age_minimum(tmp_path):
    s=study(tmp_path)
    events=s.pair_observed(pair(age=.03),sol_mint=SOL,dexes=ROUTES,now=NOW)
    assert events=={'0_60':'opened'}
    assert s.tracks[0].positions[MINT]['entry_5m_partial'] is True


def test_reject_after_sixty_min_even_if_other_filters_qualify(tmp_path):
    s=study(tmp_path)
    assert s.pair_observed(pair(age=60),sol_mint=SOL,dexes=ROUTES,now=NOW)=={}
    assert all(not t.positions for t in s.tracks)


def test_original_main_shadow_remains_five_to_twelve(tmp_path):
    # New virtual 42m permission MUST NOT alter the original primary signal.
    p=pair(age=42)
    assert bot.shadow_paper_candidate(p,now=NOW) is None
    p=pair(age=8)
    assert bot.shadow_paper_candidate(p,now=NOW) is not None


def test_unhealthy_thin_volume_surge_rejected_both(tmp_path):
    s=study(tmp_path)
    p=pair(age=6,volume=1421.23,change=14.76)
    assert s.pair_observed(p,sol_mint=SOL,dexes=ROUTES,now=NOW)=={}
    assert all(not t.positions for t in s.tracks)


def test_no_entry_without_valid_full_flow_fields(tmp_path):
    s=study(tmp_path)
    p=pair(age=8)
    p['txns']['m5'].pop('sells')
    assert s.pair_observed(p,sol_mint=SOL,dexes=ROUTES,now=NOW)=={}
    assert all(not t.positions for t in s.tracks)


def test_no_cross_pool_mark_or_exit(tmp_path):
    s=study(tmp_path)
    s.pair_observed(pair(age=8),sol_mint=SOL,dexes=ROUTES,now=NOW)
    event=s.pair_observed(pair(age=8.2,pool=POOL2,price=.0009),sol_mint=SOL,dexes=ROUTES,now=NOW+12)
    assert event=={}
    assert all(t.cash==34 and t.positions[MINT]['entry_price']==.0004 for t in s.tracks)


def test_exit_on_liquidity_collapse_below_entry_floor(tmp_path):
    s=study(tmp_path)
    s.pair_observed(pair(age=8),sol_mint=SOL,dexes=ROUTES,now=NOW)
    events=s.pair_observed(pair(age=8.2,price=.00025,liq=5_000,cap=20_000),
                           sol_mint=SOL,dexes=ROUTES,now=NOW+12)
    assert all(ev=='closed:protective_net_stop' for ev in events.values())
    assert all(len(t.closed)==1 and not t.positions for t in s.tracks)
    assert all(t.closed[0]['execution']=='indicative_pinned_pool_only_not_executable' for t in s.tracks)


def test_expiry_after_pool_ages_past_sixty(tmp_path):
    s=study(tmp_path)
    s.pair_observed(pair(age=50),sol_mint=SOL,dexes=ROUTES,now=NOW)
    events=s.pair_observed(pair(age=95.2,price=.000405),sol_mint=SOL,dexes=ROUTES,now=NOW+2712)
    assert events=={'0_60':'closed:expiry_45m','5_60':'closed:expiry_45m'}


def test_missing_quotes_do_not_invent_proceeds_or_writeoff(tmp_path):
    s=study(tmp_path)
    s.pair_observed(pair(age=8),sol_mint=SOL,dexes=ROUTES,now=NOW)
    s.missing(MINT,now=NOW+2800)
    for t in s.tracks:
        assert MINT in t.positions and t.closed==[] and t.cash==34
        assert t.positions[MINT]['quote_unavailable_checks']==1


def test_independent_sidecars_survive_restart_without_touching_primary(tmp_path):
    primary=tmp_path/'primary.json'
    primary.write_text('{"cash_usd":36,"positions":{},"sentinel":true}')
    s=study(tmp_path)
    s.pair_observed(pair(age=3),sol_mint=SOL,dexes=ROUTES,now=NOW)
    s.save()
    assert primary.read_text()=='{"cash_usd":36,"positions":{},"sentinel":true}'
    assert s.tracks[0].path!=s.tracks[1].path
    assert all(t.path.exists() for t in s.tracks)
    loaded=study(tmp_path)
    loaded.tracks[0].load();loaded.tracks[1].load()
    assert loaded.tracks[0].cash==34 and MINT in loaded.tracks[0].positions
    assert loaded.tracks[1].cash==36 and not loaded.tracks[1].positions


def test_corrupt_ledger_fails_closed_without_overwriting(tmp_path):
    s=study(tmp_path)
    path=s.tracks[0].path;path.write_text('{broken')
    s.pair_observed(pair(age=7),sol_mint=SOL,dexes=ROUTES,now=NOW)
    s.save()
    assert path.read_text()=='{broken'
    assert s.tracks[0].read_only and not s.tracks[1].read_only


def test_no_duplicate_virtual_entries_for_same_mint(tmp_path):
    s=study(tmp_path)
    s.pair_observed(pair(age=8),sol_mint=SOL,dexes=ROUTES,now=NOW)
    s.pair_observed(pair(age=8.5,price=.00048),sol_mint=SOL,dexes=ROUTES,now=NOW+30)
    assert all(t.closed and not t.positions for t in s.tracks)
    s.pair_observed(pair(age=9,price=.00042),sol_mint=SOL,dexes=ROUTES,now=NOW+60)
    assert all(t.open_count==1 for t in s.tracks)


def test_due_prioritizes_each_age_lane_and_active_after_sixty(tmp_path):
    s=study(tmp_path)
    stamps=[(MINT,NOW-120),('C'*44,NOW-400),('D'*44,NOW-1800)]
    for mint,b in stamps:s.hint(mint,b,now=NOW)
    due=s.due(now=NOW,limit=3)
    assert set(due)=={m for m,_ in stamps}
    s.tracks[0].positions['E'*44]={'pool':'F'*44,'entry_price':.1,'opened_at':NOW-4000,
        'entry_liquidity':50000,'last_net_pct':-6}
    due=s.due(now=NOW,limit=2)
    assert 'E'*44 in due


def test_gecko_hints_extended_without_changing_original_window():
    from datetime import datetime, timezone
    now=NOW
    born=datetime.fromtimestamp(NOW-40*60,tz=timezone.utc).isoformat()
    pool={'attributes':{'pool_created_at':born,'reserve_in_usd':'50000',
                         'base_token_price_usd':'0.01','market_cap_usd':'500000'},
          'relationships':{'dex':{'data':{'id':'solana_pumpswap'}},
             'base_token':{'data':{'id':'solana_'+MINT}},
             'quote_token':{'data':{'id':'solana_'+SOL}}}}
    payload={'data':[pool],'included':[]}
    result=early_pool_hints(payload,sol_mint=SOL,supported_dexes=ROUTES,now=now,max_age_minutes=60)
    assert MINT in result
    assert MINT not in early_pool_hints(payload,sol_mint=SOL,supported_dexes=ROUTES,now=now)


def test_clean_boot_enables_independent_tracks():
    from clean_boot import PAPER_PRESET
    assert PAPER_PRESET['SHADOW_AGE_STUDY_ENABLED']=='true'
    assert PAPER_PRESET['PAPER_SHADOW_ACTIVE']=='true'
    assert PAPER_PRESET['SHADOW_AGE_STUDY_FETCH_PER_SCAN']=='3'


def test_catastrophic_loss_halts_only_affected_virtual_track(tmp_path):
    s=study(tmp_path)
    s.pair_observed(pair(age=3),sol_mint=SOL,dexes=ROUTES,now=NOW)
    # Only 0-60 entered before five minutes, so the second track remains free.
    event=s.pair_observed(pair(age=3.3,price=.00004,liq=4000),
                          sol_mint=SOL,dexes=ROUTES,now=NOW+18)
    assert event=={'0_60':'closed:protective_net_stop'}
    assert s.tracks[0].halted is True
    assert s.tracks[1].halted is False
    fresh=pair(age=7,mint='G'*44,pool='H'*44)
    event=s.pair_observed(fresh,sol_mint=SOL,dexes=ROUTES,now=NOW+240)
    assert event=={'5_60':'opened'}
    s.save()
    newer=study(tmp_path)
    newer.tracks[0].load();newer.tracks[1].load()
    assert newer.tracks[0].halted and not newer.tracks[1].halted


def test_portfolio_report_reads_separate_age_studies_without_mutation(tmp_path):
    from portfolio_report import background_age_reports
    s=study(tmp_path)
    s.pair_observed(pair(age=8),sol_mint=SOL,dexes=ROUTES,now=NOW)
    s.pair_observed(pair(age=9,price=.00047),sol_mint=SOL,dexes=ROUTES,now=NOW+60)
    s.save()
    files=[t.path for t in s.tracks]
    before=[p.read_bytes() for p in files]
    report=background_age_reports(tmp_path/'primary.json')
    assert [r['track'] for r in report]==['0_60','5_60']
    assert all(r['status']=='indicative_paper_only' and r['closed_trades']==1
               and r['realized_pnl_usd']>0 and not r['executable_quote_verified'] for r in report)
    assert before==[p.read_bytes() for p in files]

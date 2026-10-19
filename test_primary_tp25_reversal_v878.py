"""Offline tests for v8.7.8 promoted-paper primary's changed exit settings.

No real provider calls or trade execution. Background track must remain v8.7.6.
"""
import json

import pytest
from shadow_age_tracks_v876 import BackgroundAgeStudy
from test_shadow_age_study_v876 import NOW, SOL, MINT, POOL, ROUTES, pair


def promoted(tmp_path):
    return BackgroundAgeStudy(state_file=tmp_path/'historical.json',
                              promoted_0_60_no_halts=True)


def observe(track, *, price, age=8., stamp=NOW, liq=50_000., pool=POOL, mint=MINT):
    return track.observe(pair(age=age,price=price,liq=liq,pool=pool,mint=mint),
                         sol_mint=SOL,dexes=ROUTES,now=stamp)


def test_promoted_primary_tp25_exact_trigger_and_no_tp8(tmp_path):
    study=promoted(tmp_path)
    track=study.tracks[0]
    assert track.take_profit_net_pct==25.
    assert track.exit_on_profitable_reversal and not track.halts_enabled
    assert observe(track,price=.0004)=='opened'
    assert observe(track,price=.00048,stamp=NOW+10)=='observed'  # +14% net
    assert observe(track,price=.000523,stamp=NOW+20)=='observed'  # +24.75% net
    assert observe(track,price=.000524,stamp=NOW+25)=='closed:take_profit_25pct'
    assert track.closed[0]['net_pct']==pytest.approx(25.)


def test_profitable_reversal_exits_at_first_observed_pullback(tmp_path):
    track=promoted(tmp_path).tracks[0]
    assert observe(track,price=.0004)=='opened'
    assert observe(track,price=.00044,stamp=NOW+5)=='observed'  # +4% net
    assert observe(track,price=.00045,stamp=NOW+10)=='observed' # +6.5%
    assert observe(track,price=.0004499,stamp=NOW+12)=='closed:profitable_reversal'
    closed=track.closed[-1]
    assert closed['pnl_usd']>0
    assert closed['prior_peak_net_pct']==pytest.approx(6.5)
    assert closed['peak_to_exit_drop_pct_points']>0
    assert track.cash>36 and track.positions=={}


def test_unchanged_reading_doesnt_count_as_reversal(tmp_path):
    track=promoted(tmp_path).tracks[0]
    assert observe(track,price=.0004)=='opened'
    for t in (5,7,9):
        assert observe(track,price=.00044,stamp=NOW+t)=='observed'
    assert MINT in track.positions
    assert observe(track,price=.0004399,stamp=NOW+11)=='closed:profitable_reversal'


def test_gross_positive_but_net_negative_doesnt_arm_reversal(tmp_path):
    track=promoted(tmp_path).tracks[0]
    assert observe(track,price=.0004)=='opened'
    assert observe(track,price=.00042,stamp=NOW+5)=='observed' # +5% gross, -1% net
    assert observe(track,price=.000418,stamp=NOW+8)=='observed'
    assert not track.closed
    # Only a measured price >+6% gross is net-profitable.
    assert observe(track,price=.000426,stamp=NOW+10)=='observed'
    assert observe(track,price=.000425,stamp=NOW+12)=='closed:profitable_reversal'


def test_gap_from_profit_into_loss_does_not_invent_profitable_sale(tmp_path):
    track=promoted(tmp_path).tracks[0]
    assert observe(track,price=.0004)=='opened'
    assert observe(track,price=.00043,stamp=NOW+5)=='observed' # +1.5 net
    assert observe(track,price=.000419,stamp=NOW+8)=='closed:profitable_reversal'
    assert track.closed[-1]['pnl_usd']<0
    assert track.closed[-1]['net_pct']==pytest.approx(-1.25)


def test_original_stop_preempts_reversal_when_jump_is_severe(tmp_path):
    track=promoted(tmp_path).tracks[0]
    assert observe(track,price=.0004)=='opened'
    assert observe(track,price=.00043,stamp=NOW+5)=='observed'
    assert observe(track,price=.0003,stamp=NOW+8)=='closed:protective_net_stop'
    assert track.closed[0]['net_pct']==pytest.approx(-31.)
    assert not track.halted


def test_liquidity_drop_original_precedence_over_reversal(tmp_path):
    track=promoted(tmp_path).tracks[0]
    assert observe(track,price=.0004)=='opened'
    assert observe(track,price=.00045,stamp=NOW+5)=='observed'
    assert observe(track,price=.00044,liq=25_000.,stamp=NOW+8)=='closed:liquidity_drop_40pct'


def test_45m_expiry_remains_in_place_when_no_reversal(tmp_path):
    track=promoted(tmp_path).tracks[0]
    assert observe(track,price=.0004)=='opened'
    # Never positive, never below -9, and no liquidity collapse.
    assert observe(track,price=.000405,stamp=NOW+2701)=='closed:expiry_45m'


def test_5_60_is_original_tp8_without_reversal(tmp_path):
    study=promoted(tmp_path)
    original=study.tracks[1]
    assert original.take_profit_net_pct==8.
    assert original.exit_on_profitable_reversal is False
    assert original.halts_enabled
    assert observe(original,price=.0004)=='opened'
    assert observe(original,price=.000444,stamp=NOW+4)=='observed' # +5 net
    assert observe(original,price=.00044,stamp=NOW+6)=='observed' # down but no reversal exit
    assert observe(original,price=.000457,stamp=NOW+10)=='closed:take_profit_8pct'


def test_previous_unpromoted_0_60_preserves_v876_exit_rules(tmp_path):
    study=BackgroundAgeStudy(state_file=tmp_path/'historical.json')
    original=study.tracks[0]
    assert original.take_profit_net_pct==8.
    assert original.exit_on_profitable_reversal is False
    assert observe(original,price=.0004)=='opened'
    assert observe(original,price=.000457,stamp=NOW+5)=='closed:take_profit_8pct'


def test_reload_old_open_ledger_preserves_peak_and_cash(tmp_path):
    study=promoted(tmp_path)
    track=study.tracks[0]
    assert observe(track,price=.0004)=='opened'
    assert observe(track,price=.000444,stamp=NOW+7)=='observed' # +5 net peak
    study.save()
    raw=track.path.read_bytes()
    again=promoted(tmp_path)
    again.tracks[0].load()
    assert again.tracks[0].cash==34.
    assert again.tracks[0].positions[MINT]['peak_net_pct']==pytest.approx(5.)
    assert again.tracks[0].path.read_bytes()==raw
    assert observe(again.tracks[0],price=.0004439,stamp=NOW+30)=='closed:profitable_reversal'
    again.save()
    ledger=json.loads(again.tracks[0].path.read_text())
    assert len(ledger['closed'])==1 and len(ledger['positions'])==0
    assert ledger['closed'][0]['reason']=='profitable_reversal'
    assert ledger['halted'] is False


def test_non_matching_pool_cannot_trigger_reversal(tmp_path):
    track=promoted(tmp_path).tracks[0]
    assert observe(track,price=.0004)=='opened'
    assert observe(track,price=.000444,stamp=NOW+2)=='observed'
    assert observe(track,price=.00043,stamp=NOW+3,pool='C'*44)=='different_pool'
    assert not track.closed


def test_no_fabricated_exit_when_price_missing(tmp_path):
    track=promoted(tmp_path).tracks[0]
    assert observe(track,price=.0004)=='opened'
    assert observe(track,price=.000444,stamp=NOW+2)=='observed'
    track.note_missing_quote(MINT,now=NOW+3)
    assert not track.closed
    assert track.positions[MINT]['quote_unavailable_checks']==1

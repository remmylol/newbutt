"""Read-only local PAPER portfolio report for Railway Console or offline use.

Examples:
    python portfolio_report.py --state /data/clean_paper_state_fresh_36_oct18_v2.json
    python portfolio_report.py --state /data/clean_paper_state_fresh_36_oct18_v2.json --json

This never signs transactions, contacts providers, or writes the state file.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path


def summarize(state: dict) -> dict:
    trades = state.get('closed_trades', [])
    positions = state.get('positions', {})
    issues = state.get('exit_issues', {})
    def number(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0
    results = {
        'starting_day_equity_usd': round(number(state.get('day_start_equity')), 4),
        'cash_usd': round(number(state.get('cash_usd')), 4),
        'open_positions': len(positions),
        'impaired_positions': sum(issues.get(m, {}).get('status') == 'IMPAIRED_EXIT_UNAVAILABLE'
                                  for m in positions),
        'closed_trades': len(trades),
        'winning_trades': sum(number(t.get('pnl_usd')) > 0 for t in trades),
        'losing_trades': sum(number(t.get('pnl_usd')) < 0 for t in trades),
        'writeoffs': sum(t.get('execution') == 'paper_writeoff_no_sale' for t in trades),
        'writeoff_pnl_usd': round(sum(number(t.get('pnl_usd')) for t in trades
                 if t.get('execution') == 'paper_writeoff_no_sale'),4),
        'realized_pnl_usd': round(sum(number(t.get('pnl_usd')) for t in trades),4),
        'halted': bool(state.get('halted')),
        'halt_daily_loss': bool(state.get('daily_loss_halt')),
        'halt_writeoff_circuit': bool(state.get('shadow_writeoff_halt')),
        'halt_exit_safety': bool(state.get('shadow_exit_safety_halt')),
        'halt_exit_safety_reason': state.get('shadow_exit_safety_reason'),
        'severe_open_positions': [m for m in positions if
           number(state.get('paper_trade_metrics',{}).get(m,{}).get('max_adverse_net_pct')) <= -50
           and state.get('paper_trade_metrics',{}).get(m,{}).get('max_adverse_net_pct') is not None],
        'pending_exit_positions': [m for m in positions
                if state.get('paper_trade_metrics',{}).get(m,{}).get('pending_exit_reason')],
        'open_mints': list(positions),
        'open_risk_observations': [{
            'mint': mint,
            'route_probe_status': state.get('paper_trade_metrics',{}).get(mint,{}).get('shadow_route_probe_last_status'),
            'route_probe_last_change_pct': state.get('paper_trade_metrics',{}).get(mint,{}).get('shadow_route_probe_last_net_pct'),
            'route_probe_worst_change_pct': state.get('paper_trade_metrics',{}).get(mint,{}).get('shadow_route_probe_worst_net_pct'),
            'primary_quote_max_gap_seconds': state.get('paper_trade_metrics',{}).get(mint,{}).get('max_primary_observation_gap_s'),
            'pending_exit_reason': state.get('paper_trade_metrics',{}).get(mint,{}).get('pending_exit_reason'),
        } for mint in positions],
        'recent_closed_trades': [{
            'symbol': t.get('symbol',''),
            'net_pnl_usd': round(number(t.get('pnl_usd')),4),
            'reason':t.get('reason',''),
            'execution':t.get('execution',''),
            'exit_intent_latency_seconds': (t.get('paper_metrics') or {}).get('exit_intent_latency_seconds'),
            'primary_quote_max_gap_seconds': (t.get('paper_metrics') or {}).get('max_primary_observation_gap_s'),
            'route_probe_worst_change_pct': (t.get('paper_metrics') or {}).get('shadow_route_probe_worst_net_pct'),
        } for t in trades[-10:]],
    }
    # Do NOT claim open inventory has executable resale value: impaired marks
    # are zero for risk, other marks are only indicative.
    risk_value = number(state.get('cash_usd'))
    for mint, pos in positions.items():
        if issues.get(mint, {}).get('status') == 'IMPAIRED_EXIT_UNAVAILABLE':
            continue
        risk_value += number(pos.get('token_units')) * number(pos.get('last_price'))
    results['risk_marked_equity_usd'] = round(risk_value,4)
    if trades:
        results['win_rate_including_writeoffs_pct'] = round(100 * results['winning_trades']/len(trades),2)
    else:
        results['win_rate_including_writeoffs_pct'] = None
    return results



def background_age_reports(primary_path: str | Path) -> list[dict]:
    """Read-only, intentionally separate from the primary portfolio totals."""
    base=Path(primary_path)
    reports=[]
    for track in ('0_60','5_60'):
        file=base.with_name(f'age_study_{track}_{base.stem}.json')
        if not file.exists():
            reports.append({'track':track,'status':'not_started','path':str(file)})
            continue
        try:
            obj=json.loads(file.read_text(encoding='utf8'))
            if (obj.get('version')!=1 or obj.get('name')!=track or
                    not isinstance(obj.get('closed'),list) or
                    not isinstance(obj.get('positions'),dict)):
                raise ValueError('invalid_study_ledger')
            closed=obj['closed']
            reports.append({'track':track,'status':'indicative_paper_only',
                            'path':str(file),'cash_usd':round(float(obj['cash']),4),
                            'closed_trades':len(closed),
                            'wins':sum(float(r.get('pnl_usd',0))>0 for r in closed),
                            'realized_pnl_usd':round(sum(float(r.get('pnl_usd',0)) for r in closed),4),
                            'open_positions':len(obj['positions']),
                            'halted':bool(obj.get('halted',False)),
                            'halt_reason':obj.get('halt_reason'),
                            'historical_halt_reason':obj.get('historical_halt_reason'),
                            'executable_quote_verified':False})
        except (OSError,TypeError,ValueError,KeyError):
            reports.append({'track':track,'status':'unreadable_unchanged','path':str(file)})
    return reports


def main() -> None:
    parser = argparse.ArgumentParser(description='Read-only PAPER portfolio performance report')
    parser.add_argument('--state', default=os.getenv('CLEAN_STATE_FILE') or os.getenv('STATE_FILE') or '/data/clean_paper_state.json')
    parser.add_argument('--json', action='store_true', help='Print machine-readable report')
    args = parser.parse_args()
    data = json.loads(Path(args.state).read_text(encoding='utf-8'))
    summary = summarize(data)
    summary['background_age_tracks'] = background_age_reports(args.state)
    primary_0_60 = next((rec for rec in summary['background_age_tracks']
                        if rec.get('track')=='0_60'),None)
    # The promoted ledger is the original v8.7.6 age-study file. Do not
    # recast the legacy 5-12 JSON as the currently active paper portfolio.
    summary['active_paper_primary'] = '0_60_v878_tp25_profit_reversal'
    summary['promoted_0_60_primary'] = primary_0_60
    summary['legacy_5_12_portfolio_historical'] = True
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print('PAPER PRIMARY 0-60 (v8.7.8 +25% net TP and profitable-reversal exit; indicative/no real fills)')
        print(' ',summary['promoted_0_60_primary'])
        print('LEGACY 5-12 STATE (historical only; no new entries)')
        for k, v in summary.items():
            if k not in ('open_mints','recent_closed_trades','open_risk_observations','background_age_tracks'):
                print(f'{k}: {v}')
        if summary['open_risk_observations']:
            print('Open-position route and quote health (indicative only):')
            for item in summary['open_risk_observations']:
                print(' ', item)
        print('Background age studies (indicative, independent, not actual fills):')
        for rec in summary['background_age_tracks']:
            print(' ', rec)
        print('Latest closed trades:')
        for t in summary['recent_closed_trades']:
            print(f"  {t['symbol']}: ${t['net_pnl_usd']:+.4f} / {t['reason']} / {t['execution']}")

if __name__ == '__main__':
    main()

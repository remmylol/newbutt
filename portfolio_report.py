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
        'open_mints': list(positions),
        'recent_closed_trades': [{
            'symbol': t.get('symbol',''),
            'net_pnl_usd': round(number(t.get('pnl_usd')),4),
            'reason':t.get('reason',''),
            'execution':t.get('execution',''),
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


def main() -> None:
    parser = argparse.ArgumentParser(description='Read-only PAPER portfolio performance report')
    parser.add_argument('--state', default=os.getenv('CLEAN_STATE_FILE') or os.getenv('STATE_FILE') or '/data/clean_paper_state.json')
    parser.add_argument('--json', action='store_true', help='Print machine-readable report')
    args = parser.parse_args()
    data = json.loads(Path(args.state).read_text(encoding='utf-8'))
    summary = summarize(data)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print('PAPER SHADOW PORTFOLIO REPORT (no actual swap-fill verification)')
        for k, v in summary.items():
            if k not in ('open_mints','recent_closed_trades'):
                print(f'{k}: {v}')
        print('Latest closed trades:')
        for t in summary['recent_closed_trades']:
            print(f"  {t['symbol']}: ${t['net_pnl_usd']:+.4f} / {t['reason']} / {t['execution']}")

if __name__ == '__main__':
    main()

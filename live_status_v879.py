"""Read-only local report for v8.8.0 live ledger; never imports signer."""
from __future__ import annotations
import argparse,json
from pathlib import Path

def status(state_file):
    root=Path(state_file)
    live=root.with_name('live_age_0_60_'+root.stem+'.json')
    journal_dir=root.with_name('live_order_journal_v879')
    if not live.exists():
        return {'live_ledger':str(live),'created':False,'orders':0}
    d=json.loads(live.read_text())
    closed=d.get('closed',[])
    actual=[x for x in closed if x.get('execution')=='jupiter_swap_v2_confirmed']
    jrows=[]
    if journal_dir.exists():
        for f in sorted(journal_dir.glob('*.json')):
            jrows.append(json.loads(f.read_text()))
    fee_known=[x for x in actual if 'wallet_cash_flow_pnl_usd' in x]
    conflict=[x for x in actual if x.get('modeled_exit_net_pct') is not None
              and (float(x.get('pnl_usd',0))>0)!=(float(x['modeled_exit_net_pct'])>0)]
    catastrophic=[x for x in actual if float(x.get('pnl_usd',0)) <= -float(x.get('invested_usd',2))*.5]
    return {'live_ledger':str(live),'created':True,'open':len(d.get('positions',{})),
            'closed_real':len(actual),
            'real_pnl_usd_before_network_fees':round(sum(float(x.get('real_pnl_usd_before_network_fees',0)) for x in actual),6),
            'cash_estimate_usd':d.get('cash'),
            'cash_estimate_is_not_profit':True,
            'real_pnl_inclusive_wallet_cashflow_usd_verified_subset':round(sum(
                 float(x['wallet_cash_flow_pnl_usd']) for x in fee_known),6),
            'wallet_cashflow_verified_closed':len(fee_known),
            'closed_missing_onchain_fee_or_rent_data':len(actual)-len(fee_known),
            'indicative_vs_actual_sign_disagreements':len(conflict),
            'catastrophic_50pct_or_worse':len(catastrophic),
            'pending_orders':[{'id':r.get('id'),'status':r.get('status'),'action':r.get('action'),
                               'mint':r.get('mint'),'signature':r.get('signature')}
                              for r in jrows if r.get('status') not in {'applied','failed'}],
            'confirmed_orders_applied':sum(r.get('status')=='applied' for r in jrows)}

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--state-file',required=True,help='existing /data/CLEAN_STATE_FILE path')
    args=p.parse_args()
    print(json.dumps(status(args.state_file),indent=2))

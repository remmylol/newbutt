"""Read-only local report for v8.7.9 live ledger; never imports signer."""
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
    return {'live_ledger':str(live),'created':True,'open':len(d.get('positions',{})),
            'closed_real':len(actual),
            'real_pnl_usd_before_network_fees':round(sum(float(x.get('real_pnl_usd_before_network_fees',0)) for x in actual),6),
            'cash_estimate_usd':d.get('cash'),
            'pending_orders':[{'id':r.get('id'),'status':r.get('status'),'action':r.get('action'),
                               'mint':r.get('mint'),'signature':r.get('signature')}
                              for r in jrows if r.get('status') not in {'applied','failed'}],
            'confirmed_orders_applied':sum(r.get('status')=='applied' for r in jrows)}

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--state-file',required=True,help='existing /data/CLEAN_STATE_FILE path')
    args=p.parse_args()
    print(json.dumps(status(args.state_file),indent=2))

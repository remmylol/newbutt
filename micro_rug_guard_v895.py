"""Read-only PumpSwap-specific micro pool risk evidence.

Only the on-chain PumpSwap Pool layout is supported. Unknown DEX/pool,
LP-control, holder owners or reserve history fail closed for *micro_0_60*.
No real orders, no invented safe/locked LP status. Zero LP token supply
alone is evidence of burned LP tokens, not a guarantee against market dumps.
"""
from __future__ import annotations

from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
import logging
import math
import time

from read_only_exit_v872 import pump_pool_snapshot, _rpc, b58encode, valid_pubkey, PUMP_AMM_PROGRAM

log=logging.getLogger(__name__)

class MicroRugGuard:
    def __init__(self,rpc_url,*,snapshot_loader=None,security_loader=None,
                 max_pool_checks_per_minute=6):
        self.rpc_url=rpc_url
        self.snapshot_loader=snapshot_loader or self._snapshot
        self.security_loader=security_loader or self._security
        self._executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='micro_rug')
        self._pending={};self._last={};self._security_cache={};self._security_pending={}
        self.history={};self.reasons=Counter()
        self.max_pool_checks_per_minute=max(1,int(max_pool_checks_per_minute))
        self._checks=deque()

    def _snapshot(self,mint,pool,dex):
        if str(dex).lower()!='pumpswap':raise ValueError('unsupported_pool_adapter')
        snap=pump_pool_snapshot(self.rpc_url,pool,mint)
        return {'pool':pool,'mint':mint,'sol_vault_raw':snap.quote_raw,
                'token_vault_raw':snap.base_raw,'slot':snap.slot,
                'base_vault':snap.base_vault,'quote_vault':snap.quote_vault}

    def _security(self,mint,pool,dex,snapshot):
        if str(dex).lower()!='pumpswap':raise ValueError('unsupported_pool_adapter')
        info=_rpc(self.rpc_url,'getAccountInfo',[pool,{'encoding':'base64','commitment':'confirmed'}])['value']
        # Parsed and validated PumpSwap Pool layout already checked in snapshot.
        if not isinstance(info,dict) or info.get('owner')!=PUMP_AMM_PROGRAM:
            raise ValueError('unverified_pool_owner')
        import base64
        raw=base64.b64decode(info['data'][0],validate=True)
        if len(raw)<235:raise ValueError('unverified_lp_mint_layout')
        lp_mint=b58encode(raw[107:139])
        creator=b58encode(raw[11:43])
        if not valid_pubkey(lp_mint) or not valid_pubkey(creator):
            raise ValueError('unverified_pool_control_address')
        # Only positively proven all-burned LP is accepted; a locked LP
        # position requires a protocol-specific locker decoder not present.
        lp_supply=int(_rpc(self.rpc_url,'getTokenSupply',[lp_mint,{'commitment':'confirmed'}])['value']['amount'])
        if lp_supply!=0:
            return {'status':'unverified_lp_withdrawal_control','pool_creator':creator,
                    'lp_mint':lp_mint,'lp_supply_raw':lp_supply,
                    'reason':'nonzero_lp_requires_verified_lock_owner'}
        token_supply=int(_rpc(self.rpc_url,'getTokenSupply',[mint,{'commitment':'confirmed'}])['value']['amount'])
        if token_supply<=0:raise ValueError('unverified_mint_supply')
        top=_rpc(self.rpc_url,'getTokenLargestAccounts',[mint,{'commitment':'confirmed'}])['value']
        if not isinstance(top,list) or not top:raise ValueError('unverified_holders')
        accounts=[r.get('address') for r in top if isinstance(r,dict) and valid_pubkey(r.get('address'))]
        if not accounts:raise ValueError('unverified_holders')
        details=_rpc(self.rpc_url,'getMultipleAccounts',[accounts,{'encoding':'jsonParsed','commitment':'confirmed'}])['value']
        if not isinstance(details,list) or len(details)!=len(accounts):raise ValueError('unverified_holders')
        owners=Counter()
        for account,detail in zip(accounts,details):
            if account==snapshot['base_vault']:continue  # independently verified AMM vault
            meta=((detail or {}).get('data') or {}).get('parsed') or {}
            info=meta.get('info') or {}
            if meta.get('type')!='account' or not valid_pubkey(info.get('owner')):
                raise ValueError('unverified_holder_owner')
            quantity=int((info.get('tokenAmount') or {}).get('amount') or 0)
            if quantity<0:raise ValueError('unverified_holder_balance')
            owners[info['owner']]+=quantity
        largest=max(owners.values(),default=0)/token_supply
        top_five=sum(v for _,v in owners.most_common(5))/token_supply
        creator_share=owners[creator]/token_supply
        if largest>=.20 or top_five>=.50 or creator_share>=.10:
            return {'status':'concentrated_wallet_supply','pool_creator':creator,
                    'largest_owner_pct':round(largest*100,2),
                    'top5_owner_pct':round(top_five*100,2),
                    'creator_wallet_pct':round(creator_share*100,2),
                    'warning':'related_wallets_not_fully_identifiable'}
        return {'status':'verified_burned_lp_and_screened_holders',
                'pool_creator':creator,'lp_mint':lp_mint,
                'largest_owner_pct':round(largest*100,2),
                'top5_owner_pct':round(top_five*100,2),
                'creator_wallet_pct':round(creator_share*100,2),
                'warning':'not_proof_of_absent_related_wallets_or_safe_sell'}

    def _security_check(self,mint,pool,dex,snap):
        try:return self.security_loader(mint,pool,dex,snap)
        except Exception as exc:
            return {'status':'unverified_lp_or_holder_security','detail':type(exc).__name__}

    def _refresh(self,mint,pool,dex):
        try:
            snap=self.snapshot_loader(mint,pool,dex)
            if (snap['mint']!=mint or snap['pool']!=pool or
                    int(snap['sol_vault_raw'])<=0 or int(snap['token_vault_raw'])<=0):
                return {'status':'unverified_pool_reserves'}
            return {'status':'snapshot_ok','snapshot':snap}
        except Exception as exc:
            return {'status':'unverified_pool_security','detail':type(exc).__name__}

    def check(self,pair,now):
        mint=(pair.get('baseToken') or {}).get('address')
        pool=pair.get('pairAddress');dex=pair.get('dexId')
        if not valid_pubkey(mint) or not valid_pubkey(pool):
            return False,'unverified_pool_identity',{}
        key=(mint,pool)
        task=self._pending.get(key)
        if task is None:
            last=self._last.get(key,0)
            if now-last<5:
                return False,'unverified_pool_check_cooldown',{}
            while self._checks and now-self._checks[0]>=60:
                self._checks.popleft()
            if len(self._checks)>=self.max_pool_checks_per_minute:
                return False,'unverified_pool_security_budget',{}
            self._checks.append(now)
            self._last[key]=now
            self._pending[key]=self._executor.submit(self._refresh,mint,pool,dex)
            return False,'unverified_pool_check_pending',{}
        if not task.done():return False,'unverified_pool_check_pending',{}
        self._pending.pop(key,None)
        result=task.result()
        if result.get('status')!='snapshot_ok':
            why=result.get('status','unverified_pool_security')
            self.reasons[why]+=1
            return False,why,result
        snap=result['snapshot']
        hist=self.history.setdefault(key,deque(maxlen=32))
        if not hist or now-hist[-1][0]>=5:hist.append((now,int(snap['sol_vault_raw']),int(snap['token_vault_raw'])))
        prior=[row for row in hist if 60<=now-row[0]<=130]
        if not prior:
            self.reasons['unverified_60s_reserve_history']+=1
            return False,'unverified_60s_reserve_history',result
        older=max(prior,key=lambda row:row[0])
        quote_decline=1.-int(snap['sol_vault_raw'])/max(1,older[1])
        result['sol_reserve_decline_pct']=round(100*quote_decline,3)
        if quote_decline>=.10:
            log.warning('MICRO RUG SOL VAULT DECLINE mint=%s pool=%s decline_pct=%.2f warning_at_10pct=True',
                        mint,pool,100*quote_decline)
        if quote_decline>=.20:
            self.reasons['verified_sol_reserve_decline_20pct']+=1
            return False,'verified_sol_reserve_decline_20pct',result
        cache=self._security_cache.get(key)
        if cache and now-cache[0]<30:
            security=cache[1]
        else:
            fut=self._security_pending.get(key)
            if fut is None:
                self._security_pending[key]=self._executor.submit(
                    self._security_check,mint,pool,dex,snap)
                return False,'unverified_lp_security_pending',result
            if not fut.done():
                return False,'unverified_lp_security_pending',result
            self._security_pending.pop(key,None)
            try:security=fut.result()
            except Exception as exc:
                security={'status':'unverified_lp_or_holder_security','detail':type(exc).__name__}
            self._security_cache[key]=(now,security)
        result['security']=security
        status=security.get('status')
        if status!='verified_burned_lp_and_screened_holders':
            self.reasons[status or 'unverified_lp_or_holder_security']+=1
            return False,status or 'unverified_lp_or_holder_security',result
        return True,'verified_micro_rug_evidence_not_rug_proof',result

    def completed(self,mint):
        return (any(m==mint and future.done() for (m,_),future in self._pending.items()) or
                any(m==mint and future.done() for (m,_),future in self._security_pending.items()))

    def stats(self):
        return {'rejections':dict(self.reasons),'pools_seen':len(self.history),
                'pending_pool_checks':len(self._pending),
                'pending_lp_holder_checks':len(self._security_pending),
                'pool_checks_per_minute_limit':self.max_pool_checks_per_minute,
                'locked_lp_adapters_not_supported':'only_burned_lp_pumpswap_verified'}

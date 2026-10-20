"""Read-only PumpSwap-specific micro pool risk evidence.

Only the on-chain PumpSwap Pool layout is supported. Unknown DEX/pool,
LP-control, holder owners or reserve history fail closed for *micro_0_60*.
No real orders, no invented safe/locked LP status. Canonical migration pools are verified against Pump program PDAs. At least
99% of historical LP must be burned in other pools; unverifiable lockers
remain blocked. No guarantee of preventing price-collapse losses.
"""
from __future__ import annotations

from collections import Counter, deque
import base64
from concurrent.futures import ThreadPoolExecutor
import logging
import math
import time
import requests

from read_only_exit_v872 import (_rpc, b58encode, valid_pubkey, PUMP_AMM_PROGRAM,
    PUMP_POOL_DISCRIMINATOR, SOL)
from pumpswap_pda_v901 import classify_pool

log=logging.getLogger(__name__)

class MicroRugGuard:
    def __init__(self,rpc_url,*,snapshot_loader=None,security_loader=None,
                 max_pool_checks_per_minute=24):
        self.rpc_url=rpc_url
        self.snapshot_loader=snapshot_loader or self._snapshot
        self.security_loader=security_loader or self._security
        self._executor=ThreadPoolExecutor(max_workers=4,thread_name_prefix='micro_rug')
        self._pending={};self._last={};self._security_cache={};self._security_pending={}
        self.history={};self.reasons=Counter();self._snapshot_cache={}
        self.max_pool_checks_per_minute=max(1,int(max_pool_checks_per_minute))
        self._checks=deque()
        self.prefetch_attempts=0;self.prefetch_submitted=0
        # In-memory telemetry: unique pools versus repeated poll attempts.
        self._unique_statuses={};self._reported_stages={}
        self.security_negative_cache_hits=0
        self.completed_futures_pruned=0
        self._last_prune=0.
        self._snapshot_failures={}
        self.callbacks_consumed_snapshot=0
        self.callbacks_consumed_lp=0
        self.transient_provider_failures=0

    def _snapshot(self,mint,pool,dex):
        """Normalize SOL/token by *mint identity*, not base/quote order.

        Require PumpSwap account owner+discriminator, exact token pairing,
        and independently parsed vault mint identities. Unsupported layouts
        and non-WSOL pairs remain denied.
        """
        if str(dex).lower()!='pumpswap':raise ValueError('unsupported_pool_adapter')
        result=_rpc(self.rpc_url,'getAccountInfo',[pool,{'encoding':'base64','commitment':'confirmed'}])
        acct=result.get('value')
        if not isinstance(acct,dict) or acct.get('owner')!=PUMP_AMM_PROGRAM:
            raise ValueError('unverified_pool_owner')
        encoded=acct.get('data') or []
        if not isinstance(encoded,list) or len(encoded)<2 or encoded[1]!='base64':
            raise ValueError('unverified_pool_encoding')
        raw=base64.b64decode(encoded[0],validate=True)
        if len(raw)<211 or raw[:8]!=PUMP_POOL_DISCRIMINATOR:
            raise ValueError('unverified_pool_layout')
        bm,qm=b58encode(raw[43:75]),b58encode(raw[75:107])
        if (bm,qm)==(mint,SOL):reversed_pair=False
        elif (bm,qm)==(SOL,mint):reversed_pair=True
        else:raise ValueError('pool_mint_identity_mismatch')
        a,b=b58encode(raw[139:171]),b58encode(raw[171:203])
        if a==b or not valid_pubkey(a) or not valid_pubkey(b):
            raise ValueError('unverified_vault_addresses')
        balances=_rpc(self.rpc_url,'getMultipleAccounts',[[a,b],{
            'encoding':'jsonParsed','commitment':'confirmed'}])
        values=balances.get('value')
        if not isinstance(values,list) or len(values)!=2:
            raise ValueError('unverified_vault_accounts')
        amounts=[]
        for v,expected in zip(values,(bm,qm)):
            if not isinstance(v,dict):raise ValueError('unverified_vault_account')
            parsed=((v.get('data') or {}).get('parsed') or {})
            info=parsed.get('info') or {}
            if parsed.get('type')!='account' or info.get('mint')!=expected:
                raise ValueError('pool_vault_mint_mismatch')
            if v.get('owner') not in ('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
                                     'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'):
                raise ValueError('unverified_vault_token_program')
            amount=int((info.get('tokenAmount') or {})['amount'])
            if amount<0:raise ValueError('unverified_vault_balance')
            amounts.append(amount)
        pool_slot=int((result.get('context') or {}).get('slot') or 0)
        vault_slot=int((balances.get('context') or {}).get('slot') or 0)
        if pool_slot<=0 or vault_slot<=0 or abs(pool_slot-vault_slot)>150:
            raise ValueError('unverified_rpc_slot_gap')
        return {'pool':pool,'mint':mint,'sol_vault_raw':amounts[0 if reversed_pair else 1],
                'token_vault_raw':amounts[1 if reversed_pair else 0],
                'slot':max(pool_slot,vault_slot),
                'base_vault':b if reversed_pair else a,  # semantic TOKEN vault
                'quote_vault':a if reversed_pair else b, # semantic WSOL vault
                'reversed_pool_orientation':reversed_pair}

    def _security(self,mint,pool,dex,snapshot):
        if str(dex).lower()!='pumpswap':raise ValueError('unsupported_pool_adapter')
        info=_rpc(self.rpc_url,'getAccountInfo',[pool,{'encoding':'base64','commitment':'confirmed'}])['value']
        # Parsed and validated PumpSwap Pool layout already checked in snapshot.
        if not isinstance(info,dict) or info.get('owner')!=PUMP_AMM_PROGRAM:
            raise ValueError('unverified_pool_owner')
        raw=base64.b64decode(info['data'][0],validate=True)
        if raw[:8]!=PUMP_POOL_DISCRIMINATOR:raise ValueError('unverified_pool_layout')
        bm,qm=b58encode(raw[43:75]),b58encode(raw[75:107])
        if not ((bm,qm)==(mint,SOL) or (bm,qm)==(SOL,mint)):
            raise ValueError('pool_mint_identity_mismatch')
        if len(raw)<211:raise ValueError('unverified_lp_mint_layout')
        classification=classify_pool(pool,raw)
        # Independently match the fresh snapshot's verified TOKEN vault to
        # the same program-owned Pool account used by the LP check.
        expected_token_vault=b58encode(raw[139:171] if bm==mint else raw[171:203])
        if snapshot.get('base_vault')!=expected_token_vault:
            raise ValueError('pool_snapshot_security_vault_mismatch')
        lp_mint=b58encode(raw[107:139])
        creator=b58encode(raw[11:43])
        # `creator` is the *pool authority* on migrated pools, not the
        # original coin developer. Newer Pool layouts store coin_creator.
        zero_key='11111111111111111111111111111111'
        coin_creator=b58encode(raw[211:243]) if len(raw)>=243 else zero_key
        known_coin_creator=coin_creator!=zero_key and valid_pubkey(coin_creator)
        # Never mislabel the Pump migration PDA as the coin's developer.
        creator_to_screen=coin_creator if known_coin_creator else creator
        if not valid_pubkey(lp_mint) or not valid_pubkey(creator):
            raise ValueError('unverified_pool_control_address')
        # PumpSwap Pool.lp_supply may be ZERO for an official migrated pool:
        # burned migration LP can leave no withdrawable supply. A custom
        # zero/zero pool NEVER inherits that exemption without canonical PDA
        # verification and a correctly-derived LP mint. Do not trust index=0
        # or the pool's own creator string without independent PDA derivation.
        historical=int.from_bytes(raw[203:211],'little')
        outstanding=int(_rpc(self.rpc_url,'getTokenSupply',[
            lp_mint,{'commitment':'confirmed'}])['value']['amount'])
        if historical<0 or outstanding<0 or outstanding>historical:
            raise ValueError('unverified_lp_supply_consistency')
        if historical==0 and classification['pool_type']!='canonical_migration':
            log.info('V901 PUMPSWAP LP REJECT pool=%s classification=%s reason=custom_zero_supply_not_verified_migration',
                     pool,classification['pool_type'])
            return {'status':'unverified_lp_withdrawal_control',
                    **classification,'pool_creator':creator,'lp_mint':lp_mint,
                    'lp_supply_raw':outstanding,'historical_lp_supply_raw':historical,
                    'reason':'custom_zero_supply_not_a_verified_migration'}
        burned_pct=100*(1-outstanding/historical) if historical else 100.
        # Burned LP is irredeemable. All *outstanding* LP is conservatively
        # counted as withdrawable, including alleged third-party locks.
        # Enforce <=1% outstanding using integer comparisons (no rounding).
        if historical and outstanding*100>historical:
            log.info('V901 PUMPSWAP LP REJECT pool=%s classification=%s burned_lp_pct=%.3f outstanding_lp_pct=%.3f reason=withdrawable_exposure',
                     pool,classification['pool_type'],burned_pct,100*outstanding/historical)
            return {'status':'unverified_lp_withdrawal_control',
                    **classification,'pool_creator':creator,'lp_mint':lp_mint,
                    'lp_supply_raw':outstanding,'historical_lp_supply_raw':historical,
                    'burned_lp_pct':round(burned_pct,3),
                    'withdrawable_lp_pct_upper_bound':round(100* outstanding/historical,3),
                    'reason':'withdrawable_lp_exposure_exceeds_1pct_or_lock_unverified'}
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
        creator_share=owners[creator_to_screen]/token_supply
        if largest>=.20 or top_five>=.50 or creator_share>=.10:
            return {'status':'concentrated_wallet_supply',**classification,'pool_creator':creator,
                    'largest_owner_pct':round(largest*100,2),
                    'top5_owner_pct':round(top_five*100,2),
                    'creator_wallet_pct':round(creator_share*100,2),
                    'coin_creator_identity_available':known_coin_creator,
                    'warning':'related_wallets_not_fully_identifiable'}
        log.info('V901 PUMPSWAP LP VERIFIED pool=%s classification=%s burned_lp_pct=%.3f outstanding_lp_pct=%.3f creator_identity_known=%s read_only=True',
                 pool,classification['pool_type'],burned_pct,(100*outstanding/historical if historical else 0.),known_coin_creator)
        return {'status':'verified_burned_lp_and_screened_holders',
                **classification,'pool_creator':creator,'lp_mint':lp_mint,
                'lp_supply_raw':outstanding,'historical_lp_supply_raw':historical,
                'withdrawable_lp_pct_upper_bound':round(100*outstanding/historical,3) if historical else 0.,
                'burned_lp_pct':round(burned_pct,3),
                'unburned_lp_pct':round(100-burned_pct,3),
                'largest_owner_pct':round(largest*100,2),
                'top5_owner_pct':round(top_five*100,2),
                'creator_wallet_pct':round(creator_share*100,2),
                'coin_creator_identity_available':known_coin_creator,
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
        except requests.RequestException as exc:
            return {'status':'unverified_pool_rpc_unavailable','detail':type(exc).__name__}
        except ValueError as exc:
            # A malformed provider response is transient. A verified bad pool
            # owner, discriminator or mint remains a strict security failure.
            detail=str(exc)[:96]
            status=('unverified_pool_rpc_unavailable' if detail in
                    ('rpc_invalid_response','rpc_https_required') else
                    'unverified_pool_security')
            return {'status':status,'detail':detail}
        except Exception as exc:
            return {'status':'unverified_pool_security','detail':type(exc).__name__}

    @staticmethod
    def _security_ttl(status):
        if status=='verified_burned_lp_and_screened_holders':return 120.
        # Permanent judgment is inappropriate: LP can be burned later.
        # Recheck negative evidence after 90 seconds, but do not repeatedly
        # fetch identical unchanged LP supply with every candidate poll.
        if status in ('unverified_lp_withdrawal_control',
                      'concentrated_wallet_supply'):return 90.
        return 20.  # RPC/provider errors are transient, never proof of safety

    def _negative_cached(self,key,now):
        entry=self._security_cache.get(key)
        if not entry:return None
        seen,verdict=entry
        status=verdict.get('status','unverified_lp_or_holder_security')
        if (status!='verified_burned_lp_and_screened_holders' and
                0<=now-seen<self._security_ttl(status)):
            return verdict
        return None

    def _note_unique(self,key,status):
        if status and key[0] and key[1]:
            self._unique_statuses.setdefault(key,set()).add(status)

    def drain_completed(self,now):
        """Read-only callback drain; call from scanner even without market results."""
        self._harvest_finished(now)

    def _harvest_finished(self,now):
        """Consume completed RPC futures independent of candidate re-scans.

        Submitted snapshots are timestamped at request START, not at callback
        consumption; a late result cannot be laundered into a fresh sample.
        Successful LP evidence is cached but still requires new reserve data,
        mint checks, age checks, momentum and Jupiter quotes at entry.
        """
        for key,task in list(self._pending.items())[:32]:
            if not task.done():
                continue
            self._pending.pop(key,None)
            self.callbacks_consumed_snapshot+=1
            start=self._last.get(key,now)
            try:result=task.result()
            except Exception as exc:
                result={'status':'unverified_pool_rpc_unavailable',
                        'detail':type(exc).__name__}
            status=result.get('status','unverified_pool_security')
            if status=='snapshot_ok':
                snap=result.get('snapshot') or {}
                if (0<=now-start<=18 and snap.get('pool')==key[1] and
                        snap.get('mint')==key[0] and
                        int(snap.get('sol_vault_raw') or 0)>0 and
                        int(snap.get('token_vault_raw') or 0)>0):
                    self._snapshot_cache[key]=(start,snap)
                    self._snapshot_failures.pop(key,None)
                    hist=self.history.setdefault(key,deque(maxlen=32))
                    if not hist or start-hist[-1][0]>=5:
                        hist.append((start,int(snap['sol_vault_raw']),
                                     int(snap['token_vault_raw'])))
                # A delayed response is NOT fresh; queue a new on-chain read.
            else:
                self._snapshot_failures[key]=(now,result)
                self._note_unique(key,status)
                self.reasons[status]+=1
                if status=='unverified_pool_rpc_unavailable':
                    self.transient_provider_failures+=1
            log.info('V902 MICRO SECURITY CALLBACK pool=%s mint=%s stage=snapshot status=%s',
                     key[1],key[0],status)
        for key,task in list(self._security_pending.items())[:32]:
            if not task.done():
                continue
            self._security_pending.pop(key,None)
            self.callbacks_consumed_lp+=1
            # LP input was a verified snapshot, so the verdict cannot be
            # considered newer than that snapshot or its RPC lookup.
            start=self._snapshot_cache.get(key,(now,))[0]
            try:verdict=task.result()
            except Exception as exc:
                verdict={'status':'unverified_lp_or_holder_security',
                         'detail':type(exc).__name__}
            if not isinstance(verdict,dict):
                verdict={'status':'unverified_lp_or_holder_security'}
            status=verdict.get('status','unverified_lp_or_holder_security')
            if 0<=now-start<=120:
                self._security_cache[key]=(start,verdict)
            self._note_unique(key,status)
            log.info('V902 MICRO SECURITY CALLBACK pool=%s mint=%s stage=lp_holders status=%s',
                     key[1],key[0],status)

    def prioritized_verified_mints(self,now):
        """Scheduling hints, never an entry authorization."""
        found={}
        for (mint,pool),(seen,record) in self._security_cache.items():
            if (record.get('status')=='verified_burned_lp_and_screened_holders'
                    and 0<=now-seen<=120):
                found[mint]=max(seen,found.get(mint,float('-inf')))
        return [mint for mint,_ in sorted(found.items(),key=lambda item:-item[1])]

    def _prune_futures(self,now):
        # A completed result for a candidate no longer revisited should not
        # inflate pending counts indefinitely. Do not cancel active requests.
        if now-self._last_prune<20:return
        self._last_prune=now
        for key,future in list(self._pending.items()):
            if (future.done() and now-self._last.get(key,0)>90):
                self._pending.pop(key,None)
                self.completed_futures_pruned+=1
        for key,future in list(self._security_pending.items()):
            latest=self._snapshot_cache.get(key,(0,))[0]
            if future.done() and now-latest>90:
                self._security_pending.pop(key,None)
                self.completed_futures_pruned+=1

    def prefetch(self,pair,now):
        """Prewarm verification; skip stable negative pools without RPC work.

        Negative evidence expires and will be checked again. A cache hit
        never authorizes an entry; `check` must still approve it.
        """
        self.prefetch_attempts+=1
        key=((pair.get('baseToken') or {}).get('address'),pair.get('pairAddress'))
        self._harvest_finished(now)
        self._prune_futures(now)
        verdict=self._negative_cached(key,now)
        if verdict:
            self.security_negative_cache_hits+=1
            return False,verdict.get('status','unverified_lp_or_holder_security'),{
                'security':verdict,'cache_status':'negative_expiring'}
        before=len(self._checks)
        result=self.check(pair,now)
        if len(self._checks)>before:self.prefetch_submitted+=1
        return result

    def check(self,pair,now):
        mint=(pair.get('baseToken') or {}).get('address')
        pool=pair.get('pairAddress');dex=pair.get('dexId')
        if not valid_pubkey(mint) or not valid_pubkey(pool):
            return False,'unverified_pool_identity',{}
        key=(mint,pool)
        self._harvest_finished(now)
        self._prune_futures(now)
        # Also cover entry-side check() calls (not only prefetch). A known
        # unverified locker may never be interpreted as a positive LP pass.
        verdict=self._negative_cached(key,now)
        if verdict:
            self.security_negative_cache_hits+=1
            self._note_unique(key,verdict.get('status'))
            return False,verdict.get('status','unverified_lp_or_holder_security'),{
                'security':verdict,'cache_status':'negative_expiring'}
        task=self._pending.get(key)
        if task is not None and task.done():
            self._pending.pop(key,None)
            try:result=task.result()
            except Exception:result={'status':'unverified_pool_security'}
            if result.get('status')!='snapshot_ok':
                why=result.get('status','unverified_pool_security')
                self.reasons[why]+=1
                self._note_unique(key,why)
                return False,why,result
            self._snapshot_cache[key]=(now,result['snapshot'])
            if len(self._snapshot_cache)>1600:
                for old_key,_ in sorted(self._snapshot_cache.items(),key=lambda item:item[1][0])[:200]:
                    self._snapshot_cache.pop(old_key,None)
                    self.history.pop(old_key,None)
                    self._security_cache.pop(old_key,None)
                    self._unique_statuses.pop(old_key,None)
                    self._last.pop(old_key,None)
        transient=self._snapshot_failures.get(key)
        if transient:
            seen,verdict=transient
            failure=verdict.get('status','unverified_pool_security')
            ttl=8 if failure=='unverified_pool_rpc_unavailable' else 45
            if 0<=now-seen<ttl:
                return False,failure,{'pool_security':verdict,
                                      'retry_after':seen+ttl}
            self._snapshot_failures.pop(key,None)
        cached=self._snapshot_cache.get(key)
        # Consume completed results immediately and use a fresh snapshot for
        # LP checks instead of re-queueing an RPC request each observation.
        fresh=bool(cached and 0<=now-cached[0]<=18)
        if task is not None and not task.done() and not fresh:
            return False,'unverified_pool_check_pending',{}
        if not fresh:
            if task is None or task.done():
                last=self._last.get(key,0)
                if now-last<12:return False,'unverified_pool_check_cooldown',{}
                while self._checks and now-self._checks[0]>=60:
                    self._checks.popleft()
                if len(self._checks)>=self.max_pool_checks_per_minute:
                    return False,'unverified_pool_security_budget',{}
                self._checks.append(now);self._last[key]=now
                self._pending[key]=self._executor.submit(self._refresh,mint,pool,dex)
            return False,'unverified_pool_check_pending',{}
        snapshot_time,snap=cached
        hist=self.history.setdefault(key,deque(maxlen=32))
        if not hist or snapshot_time-hist[-1][0]>=5:
            hist.append((snapshot_time,int(snap['sol_vault_raw']),int(snap['token_vault_raw'])))
        # Run LP/holder checks as soon as the first independently verified
        # snapshot exists; never serialize them BEHIND the 60-second history
        # wait. A completed verdict is reused while the reserve path matures.
        result={'snapshot':snap,'status':'snapshot_ok'}
        cache=self._security_cache.get(key)
        if cache and 0<=now-cache[0]<self._security_ttl(cache[1].get('status')):
            security=cache[1]
        else:
            fut=self._security_pending.get(key)
            if fut is None:
                self._security_pending[key]=self._executor.submit(self._security_check,mint,pool,dex,snap)
                return False,'unverified_lp_security_pending',result
            if not fut.done():return False,'unverified_lp_security_pending',result
            self._security_pending.pop(key,None)
            try:security=fut.result()
            except Exception as exc:security={'status':'unverified_lp_or_holder_security','detail':type(exc).__name__}
            self._security_cache[key]=(now,security)
        result['security']=security
        status=security.get('status')
        if status!='verified_burned_lp_and_screened_holders':
            self.reasons[status or 'unverified_lp_or_holder_security']+=1
            self._note_unique(key,status or 'unverified_lp_or_holder_security')
            return False,status or 'unverified_lp_or_holder_security',result
        self._note_unique(key,'lp_and_holder_passed')
        prior=[row for row in hist if 60<=snapshot_time-row[0]<=130]
        if not prior:
            self.reasons['unverified_60s_reserve_history']+=1
            self._note_unique(key,'unverified_60s_reserve_history')
            return False,'unverified_60s_reserve_history',result
        older=max(prior,key=lambda row:row[0])
        decline=1.-int(snap['sol_vault_raw'])/max(1,older[1])
        result['sol_reserve_decline_pct']=round(100*decline,3)
        if decline>=.10:
            log.warning('MICRO RUG SOL VAULT DECLINE mint=%s pool=%s decline_pct=%.2f warning_at_10pct=True',
                        mint,pool,100*decline)
        if decline>=.20:
            self.reasons['verified_sol_reserve_decline_20pct']+=1
            self._note_unique(key,'verified_sol_reserve_decline_20pct')
            return False,'verified_sol_reserve_decline_20pct',result
        self._note_unique(key,'pool_rug_evidence_passed')
        return True,'verified_micro_rug_evidence_not_rug_proof',result

    def negative_pool_deferral_until(self,mint,now):
        """Delay redundant micro-only lookups when ALL known mint pools fail.

        This is an optimization, not a permanent blacklist. A newly
        discovered pool may still enter verification independently. Never
        suppress a pending verification or a pool with a positive verdict.
        """
        known=[(key,ts,verdict) for key,(ts,verdict) in self._security_cache.items()
               if key[0]==mint]
        if not known:return None
        negative_keys={key for key,_,_ in known}
        # A newly discovered second pool for the same mint must always get a
        # chance to complete verification; suppress ONLY known-negative pools.
        active_keys=set(self._snapshot_cache)|set(self._pending)|set(self._security_pending)
        if any(key[0]==mint and key not in negative_keys for key in active_keys):
            return None
        for key,_,verdict in known:
            if verdict.get('status')=='verified_burned_lp_and_screened_holders':
                return None
            if (key in self._pending and not self._pending[key].done()) or (
                key in self._security_pending and not self._security_pending[key].done()):
                return None
        deadlines=[ts+self._security_ttl(v.get('status')) for _,ts,v in known]
        if any(until<=now for until in deadlines):return None
        return min(deadlines)

    def completed(self,mint):
        return (any(m==mint and future.done() for (m,_),future in self._pending.items()) or
                any(m==mint and future.done() for (m,_),future in self._security_pending.items()))

    def stats(self):
        unique=Counter(stage for stages in self._unique_statuses.values() for stage in stages)
        cached=Counter(verdict.get('status','unknown') for _,verdict in self._security_cache.values())
        return {'rejections':dict(self.reasons),'pools_seen':len(self.history),
                'unique_pools_by_security_stage_since_restart':dict(unique),
                'security_cache_by_latest_status':dict(cached),
                'negative_security_cache_hits':self.security_negative_cache_hits,
                'completed_futures_pruned':self.completed_futures_pruned,
                'pending_pool_checks':sum(not f.done() for f in self._pending.values()),
                'completed_pool_checks_waiting':sum(f.done() for f in self._pending.values()),
                'pending_lp_holder_checks':sum(not f.done() for f in self._security_pending.values()),
                'completed_lp_checks_waiting':sum(f.done() for f in self._security_pending.values()),
                'pool_checks_per_minute_limit':self.max_pool_checks_per_minute,
                'canonical_pool_verification':'Pump PDA+index+pool PDA+LP mint PDA',
                'lock_contract_adapter':'unsupported lockers fail closed; >=99pct burned or verified canonical zero-LP',
                'pool_type_counts':dict(Counter(v.get('pool_type','unclassified') for _,v in self._security_cache.values())),
                'fresh_snapshot_cache_size':len(self._snapshot_cache),
                'prefetch_attempts':self.prefetch_attempts,
                'prefetch_submitted':self.prefetch_submitted,
                'security_results_cached':len(self._security_cache),
                'v902_callbacks_consumed_snapshot':self.callbacks_consumed_snapshot,
                'v902_callbacks_consumed_lp':self.callbacks_consumed_lp,
                'v902_transient_provider_failures':self.transient_provider_failures,
                'v902_cached_snapshot_failures':len(self._snapshot_failures)}

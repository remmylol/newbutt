"""Strict, bounded read-only research checks for NEW v8.9.3 paper portfolios.

No wallet, signer or transaction capability. Risk screening is NOT a rug guarantee.
Unknown RPC state fails closed; top-holder statistics are advisory because AMM
vaults can be large holders and their identities need independent verification.
"""
from __future__ import annotations
import collections
import json
import logging
import math
import time
import requests
from concurrent.futures import ThreadPoolExecutor

audit_log=logging.getLogger(__name__)

TOKEN_PROGRAM='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
# Official Solana Token-2022 program ID (the previous string was misspelled).
TOKEN_2022='TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
# Only information-only mint extensions are accepted for PAPER research.
# Transfer hooks, fees, delegates, non-transferable/paused states and unknown
# extension types remain blocked. This check is NOT proof of sellability.
TOKEN_2022_INFORMATION_ONLY_EXTENSIONS=frozenset({
    'metadataPointer', 'tokenMetadata', 'groupPointer',
    'groupMemberPointer', 'tokenGroup', 'tokenGroupMember',
})
VERIFIED_MINT_STATUSES=frozenset({'verified_standard_spl','verified_token2022_basic'})


def _verify_token2022_mint(info):
    """Conservatively assess a jsonParsed Token-2022 mint (paper-only)."""
    data=info.get('data')
    parsed=(data.get('parsed') if isinstance(data,dict) else None)
    if not isinstance(parsed,dict) or parsed.get('type')!='mint':
        return {'status':'unverified_mint_parse','owner_program':TOKEN_2022}
    meta=parsed.get('info')
    if not isinstance(meta,dict) or not isinstance(meta.get('decimals'),int) or isinstance(meta.get('decimals'),bool):
        return {'status':'unverified_mint_parse','owner_program':TOKEN_2022}
    # Token-2022 may have optional TLV extensions after its 82-byte base mint.
    # Never accept a non-base-size account if the provider failed to disclose
    # extension details; an unseen transfer hook or fee could block a sale.
    extensions=meta.get('extensions')
    if extensions is None:
        try: space=int(info.get('space') or data.get('space') or 0)
        except (TypeError,ValueError,OverflowError): space=0
        if space!=82:
            return {'status':'unverified_token2022_extensions_unparsed',
                    'owner_program':TOKEN_2022, 'mint_space':space}
        extensions=[]
    if not isinstance(extensions,list):
        return {'status':'unverified_token2022_extensions_unparsed','owner_program':TOKEN_2022}
    extension_names=[]
    for extension in extensions:
        if not isinstance(extension,dict) or not isinstance(extension.get('extension'),str):
            return {'status':'unverified_token2022_extensions_unparsed','owner_program':TOKEN_2022}
        extension_names.append(extension['extension'])
    blocked=sorted(set(extension_names)-TOKEN_2022_INFORMATION_ONLY_EXTENSIONS)
    if blocked:
        return {'status':'unverified_token2022_extensions',
                'owner_program':TOKEN_2022, 'blocked_extensions':blocked}
    return {'status':'verified_token2022_basic','owner_program':TOKEN_2022,
            'mint_authority_active':meta.get('mintAuthority') is not None,
            'freeze_authority_active':meta.get('freezeAuthority') is not None,
            'decimals':meta['decimals'],
            'information_only_extensions':sorted(set(extension_names))}



def _n(x):
    try:
        v=float(x);return v if math.isfinite(v) else 0.
    except (ValueError,TypeError,OverflowError): return 0.

class MomentumHistory:
    """Recent same-pool price path, not an entry signal or executable quote."""
    def __init__(self, max_mints=1600):
        self.points={};self.max_mints=max_mints
        self.reasons=collections.Counter()
    def observe(self,pair,now):
        mint=(pair.get('baseToken') or {}).get('address')
        pool=pair.get('pairAddress');price=_n(pair.get('priceUsd'))
        change=_n((pair.get('priceChange') or {}).get('m5'))
        liquidity=_n((pair.get('liquidity') or {}).get('usd'))
        buys=_n(((pair.get('txns') or {}).get('m5') or {}).get('buys'))
        sells=_n(((pair.get('txns') or {}).get('m5') or {}).get('sells'))
        if not mint or not pool or price<=0:return
        prev=[p for p in self.points.get(mint,[]) if now-p[0]<300 and p[1]==pool and now>=p[0]]
        if not prev or now-prev[-1][0]>=4:
            prev.append((now,pool,price,change,buys,sells,liquidity))
        self.points[mint]=prev[-32:]
        if len(self.points)>self.max_mints:
            for k in sorted(self.points,key=lambda m:self.points[m][-1][0])[:len(self.points)-self.max_mints]:
                self.points.pop(k,None)
    def gate(self,pair,now):
        mint=(pair.get('baseToken') or {}).get('address')
        pool=pair.get('pairAddress');price=_n(pair.get('priceUsd'))
        vals=[p for p in self.points.get(mint,[]) if p[1]==pool and now-p[0]<300]
        if len(vals)<2:return True,'insufficient_history_not_disqualifying'
        previous=vals[:-1] if vals[-1][0]>=now-1 else vals
        if not previous:return True,'insufficient_history_not_disqualifying'
        fresh_liq=_n((pair.get('liquidity') or {}).get('usd'))
        historical_liq=max((p[6] for p in previous),default=0)
        if historical_liq>0 and fresh_liq<historical_liq*.75:
            self.reasons['same_pool_liquidity_drop_25pct']+=1
            return False,'same_pool_liquidity_drop_25pct'
        peak=max(p[2] for p in previous)
        # A sharply declining chart is not made safe by the 5m change
        # falling back inside the old 16% upper bound.
        if peak>0 and price<peak*.88:
            self.reasons['recent_peak_drawdown_12pct']+=1
            return False,'recent_peak_drawdown_12pct'
        old_high=any(p[3]>=40 for p in previous)
        change=_n((pair.get('priceChange') or {}).get('m5'))
        if old_high and change<16 and peak>price*1.035:
            self.reasons['fading_from_large_pump']+=1
            return False,'fading_from_large_pump'
        buys=_n(((pair.get('txns') or {}).get('m5') or {}).get('buys'))
        sells=_n(((pair.get('txns') or {}).get('m5') or {}).get('sells'))
        if len(vals)>=3 and buys<sells and price<vals[-2][2]:
            self.reasons['selling_dominance_and_decline']+=1
            return False,'selling_dominance_and_decline'
        return True,'observed_price_trend_pass'

class ResearchRiskGate:
    def __init__(self,rpc_url='',*,strict=True,max_rpc_per_minute=4,micro_rug_guard=None):
        self.rpc_url=rpc_url.strip();self.strict=bool(strict)
        self.max_rpc_per_minute=max(1,int(max_rpc_per_minute))
        self.calls=collections.deque();self.cache={}
        # V907: four bounded workers prevent 9-12 mint checks from queuing behind
        # two slow requests. Existing rolling calls/minute still caps throughput.
        self._executor=ThreadPoolExecutor(max_workers=4,thread_name_prefix='v893_rpc')
        self._pending={}
        self._pending_started={}
        self._v907_completed_rpc={}  # scheduling hints, NOT authorization
        self.v907_prefetch_calls=0;self.v907_prefetch_submitted=0
        self.v907_mint_callbacks_consumed=0
        self.v907_full_risk_approvals=0
        self.v907_pending_mint_after_pool_ready=0
        self._v907_logged_stage={}
        self._v907_approved_mint_pool=set()
        self.reasons=collections.Counter();self.momentum=MomentumHistory()
        self.micro_rug_guard=micro_rug_guard
    def observe(self,pair,now):
        self.momentum.observe(pair,now)
    def _rpc(self,method,params):
        response=requests.post(self.rpc_url,json={'jsonrpc':'2.0','id':1,'method':method,'params':params},timeout=3)
        response.raise_for_status()
        result=response.json()
        if result.get('error'):raise ValueError('rpc_error')
        return result['result']
    def _authority_lookup(self,mint):
        """Runs off the scanner thread; maximum two bounded RPC calls."""
        try:
            info=self._rpc('getAccountInfo',[mint,{'encoding':'jsonParsed','commitment':'confirmed'}]).get('value')
            if not isinstance(info,dict):result={'status':'unverified_missing_mint_account'}
            elif info.get('owner')==TOKEN_2022:
                result=_verify_token2022_mint(info)
            elif info.get('owner')!=TOKEN_PROGRAM:
                # A non-standard owner is NOT an SPL mint. Do not guess or
                # treat it as safe; expose the public owner program so an
                # operator can distinguish a genuine incompatible mint from
                # a discovery/provider identity mismatch.
                parsed_data=(info.get('data') or {}).get('parsed') or {}
                result={'status':'unverified_nonstandard_mint_owner',
                        'owner_program':str(info.get('owner') or '')[:64],
                        'parsed_type':str(parsed_data.get('type') or '')[:40],
                        'account_executable':bool(info.get('executable',False))}
            else:
                parsed=(info.get('data') or {}).get('parsed') or {}
                meta=parsed.get('info') or {}
                if parsed.get('type')!='mint' or not isinstance(meta.get('decimals'),int):
                    result={'status':'unverified_mint_parse'}
                else:
                    result={'status':'verified_standard_spl',
                            'mint_authority_active':meta.get('mintAuthority') is not None,
                            'freeze_authority_active':meta.get('freezeAuthority') is not None,
                            'decimals':meta['decimals']}
            if result.get('status') in VERIFIED_MINT_STATUSES:
                # Largest token accounts are not reliably beneficial-owner
                # concentration, since AMM LP vaults can own many tokens.
                # Record advisory only; never label an LP vault as a rug.
                try:
                    accounts=self._rpc('getTokenLargestAccounts',[mint,{'commitment':'confirmed'}]).get('value') or []
                    supply=int(((info.get('data') or {}).get('parsed') or {}).get('info',{}).get('supply') or 0)
                    if supply>0 and accounts:
                        largest=max(int(r.get('amount') or 0) for r in accounts)
                        result['largest_token_account_pct_advisory']=round(100*largest/supply,2)
                        result['holder_owner_classification']='unknown_lp_vault_not_excluded'
                except (requests.RequestException,ValueError,KeyError,TypeError):
                    result['holder_owner_classification']='unverified_rpc_unavailable'
            return result
        except (requests.RequestException,ValueError,KeyError,TypeError):
            return {'status':'unverified_rpc_failure'}
    def drain_completed(self,now):
        """Harvest finished mint checks without requiring a repeat candidate scan.

        The completion only caches read-only mint evidence and wakes a lookup.
        It never becomes entry authorization without the normal allow_micro().
        Preserve RPC-request time for cache age: delayed responses cannot be
        artificially made newer by callback collection.
        """
        for mint,future in list(self._pending.items())[:32]:
            if not future.done():
                continue
            self._pending.pop(mint,None)
            started=self._pending_started.pop(mint,now)
            try: result=future.result()
            except Exception:result={'status':'unverified_rpc_failure'}
            if not isinstance(result,dict):
                result={'status':'unverified_rpc_failure'}
            self.cache[mint]=(min(now,started),result)
            self._v907_completed_rpc[mint]=now
            self.v907_mint_callbacks_consumed+=1
            if result.get('status') not in VERIFIED_MINT_STATUSES:
                audit_log.warning(
                    'V907 MICRO MINT CALLBACK mint=%s status=%s '
                    'owner_program=%s security_not_bypassed=True',
                    mint,result.get('status','unknown'),
                    result.get('owner_program','unknown'))

    def recently_completed_mints(self,now):
        """Short-lived wake signals; NEVER claim a safety approval."""
        for mint,completed in tuple(self._v907_completed_rpc.items()):
            if not 0<=now-completed<=15:
                self._v907_completed_rpc.pop(mint,None)
        return {mint for mint in self._v907_completed_rpc
                if self.cache.get(mint,({},{}))[1].get('status') in
                VERIFIED_MINT_STATUSES}

    def prefetch_micro_mint(self,mint,now):
        """Start mint verification alongside a plausible pool's first LP scan."""
        self.v907_prefetch_calls+=1
        already_pending=mint in self._pending
        result=self.authority(mint,now,retry_transient=True)
        if (not already_pending and mint in self._pending and
                result.get('status')=='unverified_rpc_pending'):
            self.v907_prefetch_submitted+=1
        return result

    def authority(self,mint,now,*,retry_transient=False):
        self.drain_completed(now)
        cached=self.cache.get(mint)
        if cached:
            transient=cached[1].get('status') in (
                'unverified_rpc_failure','unverified_missing_mint_account')
            ttl=15 if (transient and retry_transient) else 900
            if 0<=now-cached[0]<ttl:return cached[1]
        pending=self._pending.get(mint)
        if pending is not None:
            return {'status':'unverified_rpc_pending'}
        while self.calls and now-self.calls[0]>=60:self.calls.popleft()
        if not self.rpc_url.startswith('https://'):
            return {'status':'unverified_no_https_rpc'}
        if len(self.calls)>=self.max_rpc_per_minute:
            return {'status':'unverified_rpc_budget'}
        self.calls.append(now)
        self._pending_started[mint]=now
        self._pending[mint]=self._executor.submit(self._authority_lookup,mint)
        return {'status':'unverified_rpc_pending'}

    def _v907_stage(self,mint,pool,mint_status,pool_status,reason,now):
        """Per-pool transition log; no presumption that any proof is timeless."""
        key=(mint,pool)
        stage=(mint_status,pool_status,reason)
        prior=self._v907_logged_stage.get(key)
        if prior is None or stage!=prior[1] or now-prior[0]>=45:
            audit_log.info(
                'V907 MICRO RESEARCH JOIN mint=%s pool=%s mint_status=%s '
                'lp_cache_status=%s final_reason=%s security_and_quote_required=True '
                'paper_only=True',mint,pool,mint_status,pool_status,reason)
            self._v907_logged_stage[key]=(now,stage)
        if len(self._v907_logged_stage)>2500:
            for old,_ in sorted(self._v907_logged_stage.items(),
                    key=lambda entry: entry[1][0])[:300]:
                self._v907_logged_stage.pop(old,None)

    def allow_micro(self,pair,e,now,*,min_cap_usd=3000.):
        # V907: If an on-chain LP/holder check has ALREADY proved the pool
        # unsuitable and its negative cache is still valid, don't waste
        # scarce mint RPC work on it. Expired evidence is always rechecked.
        # Unknown or pending LP evidence never becomes safety approval.
        guard=self.micro_rug_guard
        pool=pair.get('pairAddress') or ''
        key=(e['mint'],pool)
        if guard is not None and hasattr(guard,'_negative_cached'):
            known_unsafe=guard._negative_cached(key,now)
            if known_unsafe is not None and known_unsafe.get('status') in (
                    'unverified_lp_withdrawal_control',
                    'concentrated_wallet_supply'):
                reason=known_unsafe['status']
                self.reasons[reason]+=1
                self._v907_stage(e['mint'],pool,'not_requested_unsafe_lp',
                    reason,reason,now)
                return False,reason,{'micro_pool_evidence':{
                    'security':known_unsafe,'cache_status':'negative_expiring'}}
        ok,reason,details=self.allow(pair,e,now,min_cap_usd=min_cap_usd,
            require_volume5=False,micro_fast_retry=True)
        pool=(pair.get('pairAddress') or '')
        guard=self.micro_rug_guard
        entry=(getattr(guard,'_security_cache',{}) or {}).get((e['mint'],pool))
        pool_status=((entry[1] or {}).get('status') if entry else 'not_lp_verified')
        if not ok:
            if (guard is not None and
                    reason in ('unverified_rpc_pending','unverified_rpc_budget') and
                    pool_status=='verified_burned_lp_and_screened_holders'):
                self.v907_pending_mint_after_pool_ready+=1
            self._v907_stage(e['mint'],pool,details.get('status','unknown'),
                             pool_status,reason,now)
            return ok,reason,details
        if guard is None:
            self._v907_stage(e['mint'],pool,details.get('status','unknown'),
                             pool_status,'unverified_micro_rug_guard_not_installed',now)
            return False,'unverified_micro_rug_guard_not_installed',details
        ok,reason,pool_details=guard.check(pair,now)
        if not ok:
            self.reasons[reason]+=1
            self._v907_stage(e['mint'],pool,details.get('status','unknown'),
                             pool_status,reason,now)
            return False,reason,dict(details,micro_pool_evidence=pool_details)
        self.v907_full_risk_approvals+=1
        self._v907_approved_mint_pool.add((e['mint'],pool))
        self._v907_stage(e['mint'],pool,details.get('status','unknown'),
                         'verified_burned_lp_and_screened_holders',
                         'research_risk_pass',now)
        return True,'research_risk_pass',dict(details,micro_pool_evidence=pool_details)

    def allow(self,pair,e,now,*,min_cap_usd=5000.,require_volume5=True,
              micro_fast_retry=False):
        mint=e['mint']
        if micro_fast_retry:
            result=self.authority(mint,now,retry_transient=True)
        else:
            result=self.authority(mint,now)
        status=result.get('status','unknown')
        if status not in VERIFIED_MINT_STATUSES:
            self.reasons[status]+=1
            if self.strict:return False,status,result
        if result.get('mint_authority_active'):
            self.reasons['mint_authority_active']+=1
            return False,'mint_authority_active',result
        if result.get('freeze_authority_active'):
            self.reasons['freeze_authority_active']+=1
            return False,'freeze_authority_active',result
        if e['liquidity']<1000 or e['cap']<min_cap_usd:
            self.reasons['below_experimental_floor']+=1
            return False,'below_experimental_floor',result
        if (require_volume5 and not e.get('v5')) or not e.get('buys') or not e.get('sells'):
            self.reasons['missing_two_sided_activity']+=1
            return False,'missing_two_sided_activity',result
        if e['v5']/max(e['liquidity'],1)>300:
            self.reasons['anomalous_volume_depth_ratio']+=1
            return False,'anomalous_volume_depth_ratio',result
        ok,why=self.momentum.gate(pair,now)
        if not ok:
            self.reasons[why]+=1
            return False,why,result
        return True,'research_risk_pass',result
    def summary(self):
        return {'cached_rpc_mints':len(self.cache),'denials':dict(self.reasons),
                'momentum_rejections':dict(self.momentum.reasons),
                'micro_rug_guard':(self.micro_rug_guard.stats() if self.micro_rug_guard else None),
                'rpc_budget_remaining':max(0,self.max_rpc_per_minute-len(self.calls)),
                'strict_missing_security':self.strict,'pending_rpc':len(self._pending),
                'v907_prefetch_calls':self.v907_prefetch_calls,
                'v907_prefetch_submitted_approx':self.v907_prefetch_submitted,
                'v907_mint_callbacks_consumed':self.v907_mint_callbacks_consumed,
                'v907_full_risk_approvals':self.v907_full_risk_approvals,
                'v907_unique_risk_approved_pools':len(self._v907_approved_mint_pool),
                'v907_pending_mint_after_lp_verified':self.v907_pending_mint_after_pool_ready}

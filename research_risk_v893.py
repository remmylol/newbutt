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
    def __init__(self,rpc_url='',*,strict=True,max_rpc_per_minute=4):
        self.rpc_url=rpc_url.strip();self.strict=bool(strict)
        self.max_rpc_per_minute=max(1,int(max_rpc_per_minute))
        self.calls=collections.deque();self.cache={}
        self._executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='v893_rpc')
        self._pending={}
        self.reasons=collections.Counter();self.momentum=MomentumHistory()
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
    def authority(self,mint,now,*,retry_transient=False):
        cached=self.cache.get(mint)
        if cached:
            # Do not cache temporary RPC/provider failures for fifteen minutes.
            # Never retry until the brief cooldown has passed; completed
            # futures are consumed once, and the original per-minute RPC
            # budget remains in force.
            transient=cached[1].get('status') in (
                'unverified_rpc_failure','unverified_missing_mint_account')
            ttl=15 if (transient and retry_transient) else 900
            if 0<=now-cached[0]<ttl:return cached[1]
        pending=self._pending.get(mint)
        if pending is not None:
            if not pending.done():return {'status':'unverified_rpc_pending'}
            self._pending.pop(mint,None)
            try: result=pending.result()
            except Exception:result={'status':'unverified_rpc_failure'}
            self.cache[mint]=(now,result)
            # Emit this once when the completed asynchronous RPC response is
            # collected, NOT on every repeat candidate evaluation. This log
            # includes public addresses only: never the RPC URL or API key.
            status=result.get('status','unknown')
            if status not in VERIFIED_MINT_STATUSES:
                audit_log.warning(
                    'V893 RESEARCH RPC MINT AUDIT mint=%s status=%s '
                    'owner_program=%s parsed_type=%s account_executable=%s '
                    'blocked_extensions=%s paper_entry_denied=%s',
                    mint,status,result.get('owner_program','unknown'),
                    result.get('parsed_type','unknown'),
                    result.get('account_executable','unknown'),
                    result.get('blocked_extensions','none'),self.strict)
            return result
        while self.calls and now-self.calls[0]>=60:self.calls.popleft()
        if not self.rpc_url.startswith('https://'):
            return {'status':'unverified_no_https_rpc'}
        if len(self.calls)>=self.max_rpc_per_minute:
            return {'status':'unverified_rpc_budget'}
        self.calls.append(now)
        self._pending[mint]=self._executor.submit(self._authority_lookup,mint)
        return {'status':'unverified_rpc_pending'}
    def allow_micro(self,pair,e,now,*,min_cap_usd=3000.):
        return self.allow(pair,e,now,min_cap_usd=min_cap_usd,require_volume5=False,
                          micro_fast_retry=True)

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
                'rpc_budget_remaining':max(0,self.max_rpc_per_minute-len(self.calls)),
                'strict_missing_security':self.strict,'pending_rpc':len(self._pending)}

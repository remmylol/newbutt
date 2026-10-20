"""Optional read-only Jupiter sell quote for research paper exits.

Not a simulated live fill: entries are NOT confirmed buy swaps, raw units
come from the paper price, and network fees/priority costs remain unverified.
Never submit transactions; do not use the price result in paper accounting.
"""
from __future__ import annotations
from collections import deque
import math
import time
import requests
from concurrent.futures import ThreadPoolExecutor

SOL_MINT='So11111111111111111111111111111111111111112'
class QuoteObserver:
    def __init__(self,api_key='',*,max_calls_per_minute=3):
        self.api_key=api_key.strip();self.max_calls_per_minute=max_calls_per_minute
        self.calls=deque();self.entry_cache={}
        self._executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='v893_jupiter')
        self._pending={};self.totals={'attempts':0,'quoted':0,'unverified':0}
    def _roundtrip_network(self,mint,size,amount,sol_usd):
        """Off-thread two-sided read-only quote lookup."""
        try:
            headers={'x-api-key':self.api_key}
            base='https://api.jup.ag/swap/v1/quote'
            qbuy=requests.get(base,params={'inputMint':SOL_MINT,'outputMint':mint,
                'amount':amount,'slippageBps':200},headers=headers,timeout=3)
            qbuy.raise_for_status();buy=qbuy.json()
            out_token=int(buy['outAmount'])
            if out_token<=0:raise ValueError('zero_tokens')
            qsell=requests.get(base,params={'inputMint':mint,'outputMint':SOL_MINT,
                'amount':out_token,'slippageBps':200},headers=headers,timeout=3)
            qsell.raise_for_status();sell=qsell.json()
            sol_back=int(sell['outAmount'])/1e9
            expected_usd=sol_back*sol_usd
            max_impact=max(abs(float(buy.get('priceImpactPct') or 0)),
                           abs(float(sell.get('priceImpactPct') or 0)))
            if expected_usd<size*.92 or max_impact>5:
                result={'status':'rejected_unfavorable_roundtrip_quote',
                        'modeled_gross_return_usd':round(expected_usd,5),
                        'max_quote_impact_pct':round(max_impact,3)}
            else:
                result={'status':'two_sided_jupiter_quotes_not_fills',
                        'buy_token_raw_out':out_token,
                        'gross_roundtrip_return_usd':round(expected_usd,5),
                        'max_quote_impact_pct':round(max_impact,3),
                        'warning':'quotes_not_fills_network_fees_and_slippage_still_apply'}
            return result
        except (requests.RequestException,ValueError,KeyError,TypeError,OverflowError):
            return {'status':'unverified_roundtrip_quote'}

    def probe_micro_roundtrip(self,mint,size,decimals,pair,*,now=None):
        """Bounded transient quote retries for micro_0_60 only."""
        return self.probe_roundtrip(mint,size,decimals,pair,now=now,micro_fast_retry=True)

    def probe_roundtrip(self,mint,size,decimals,pair,*,now=None,micro_fast_retry=False):
        stamp=time.time() if now is None else now
        cached=self.entry_cache.get(mint)
        if cached:
            # Recheck temporary quote-provider failures after bounded delay;
            # successful quotes retain their original 20-second TTL.
            transient=cached[1].get('status')=='unverified_roundtrip_quote'
            ttl=15 if (transient and micro_fast_retry) else 20
            if 0<=stamp-cached[0]<ttl:return cached[1]
        pending=self._pending.get(mint)
        if pending:
            start,future=pending
            if not future.done():return {'status':'unverified_roundtrip_quote_pending'}
            self._pending.pop(mint,None)
            try:result=future.result()
            except Exception:result={'status':'unverified_roundtrip_quote'}
            if 0<=stamp-start<=25:
                self.entry_cache[mint]=(stamp,result)
                return result
        if not self.api_key:return {'status':'unverified_no_jupiter_key'}
        if not isinstance(decimals,int) or not 0<=decimals<=18:
            return {'status':'unverified_token_decimals'}
        try:
            token_usd=float(pair.get('priceUsd') or 0)
            token_sol=float(pair.get('priceNative') or 0)
            sol_usd=token_usd/token_sol if token_sol>0 else 0.
            amount=int(float(size)/sol_usd*1e9)
            if not 0<sol_usd<100000 or amount<=0:raise ValueError('invalid_sol_usd')
        except (ValueError,TypeError,OverflowError,ZeroDivisionError):
            return {'status':'unverified_sol_price'}
        while self.calls and stamp-self.calls[0]>=60:self.calls.popleft()
        if len(self.calls)>=self.max_calls_per_minute:return {'status':'unverified_quote_budget'}
        self.calls.append(stamp)
        self._pending[mint]=(stamp,self._executor.submit(self._roundtrip_network,mint,size,amount,sol_usd))
        return {'status':'unverified_roundtrip_quote_pending'}

    def probe_sell(self,mint,entry_price,exit_price,size,decimals,pair,*,now=None):
        stamp=time.time() if now is None else now
        self.totals['attempts']+=1
        if not self.api_key:return {'status':'unverified_no_jupiter_key'}
        if not isinstance(decimals,int) or not 0<=decimals<=18:
            self.totals['unverified']+=1;return {'status':'unverified_token_decimals'}
        try:
            native=float(pair.get('priceNative') or 0)
            usd=float(pair.get('priceUsd') or 0)
            sol_usd=usd/native if native>0 else 0
            quantity=int((float(size)/float(entry_price))*10**decimals)
            if quantity<=0 or not math.isfinite(sol_usd) or not 0<sol_usd<100000:
                raise ValueError('bad_estimated_amount')
        except (ValueError,TypeError,OverflowError,ZeroDivisionError):
            self.totals['unverified']+=1;return {'status':'unverified_price_native_or_amount'}
        while self.calls and stamp-self.calls[0]>=60:self.calls.popleft()
        if len(self.calls)>=self.max_calls_per_minute:
            self.totals['unverified']+=1;return {'status':'unverified_quote_budget'}
        self.calls.append(stamp)
        try:
            r=requests.get('https://api.jup.ag/swap/v1/quote',params={
                'inputMint':mint,'outputMint':SOL_MINT,'amount':quantity,'slippageBps':200},
                headers={'x-api-key':self.api_key},timeout=3)
            r.raise_for_status();payload=r.json()
            out=int(payload['outAmount'])/1e9
            usd_out=out*sol_usd
            self.totals['quoted']+=1
            return {'status':'jupiter_read_only_sell_quote_not_fill',
                'gross_sol_out':round(out,9),'gross_usd_out_estimate':round(usd_out,5),
                'gross_usd_profit_before_network_fees':round(usd_out-size,5),
                'price_impact_pct':payload.get('priceImpactPct'),
                'estimated_token_input_raw':quantity,
                'sol_usd_from_same_pair':round(sol_usd,4),
                'warning':'paper_entry_units_are_indicative_not_confirmed'}
        except (requests.RequestException,ValueError,KeyError,TypeError,OverflowError):
            self.totals['unverified']+=1
            return {'status':'unverified_quote_unavailable'}

    def _micro_sell_network(self,mint,pool,amount,sol_usd):
        """A fresh exact-pool sell quote; never an executed or guaranteed swap."""
        try:
            r=requests.get('https://api.jup.ag/swap/v1/quote',params={
                'inputMint':mint,'outputMint':SOL_MINT,'amount':str(amount),
                'slippageBps':'150','swapMode':'ExactIn','onlyDirectRoutes':'true'},
                headers={'x-api-key':self.api_key},timeout=3)
            r.raise_for_status();payload=r.json()
            route=payload.get('routePlan') or []
            if (payload.get('inputMint')!=mint or payload.get('outputMint')!=SOL_MINT or
                    int(payload.get('inAmount') or -1)!=amount or len(route)!=1 or
                    (route[0].get('swapInfo') or {}).get('ammKey')!=pool or
                    route[0].get('percent') not in (None,100)):
                return {'status':'unverified_pinned_pool_sell_route'}
            raw=int(payload.get('outAmount') or 0)
            minimum=int(payload.get('otherAmountThreshold') or 0)
            if not 0<minimum<=raw:return {'status':'unverified_minimum_sell_proceeds'}
            impact=float(payload.get('priceImpactPct') or 'nan')
            if not math.isfinite(impact) or impact<0 or impact>.10:
                return {'status':'unverified_or_excessive_sell_impact'}
            return {'status':'pinned_pool_sell_quote_not_fill',
                    'minimum_usd_out_estimate':round(minimum/1e9*sol_usd,8),
                    'minimum_sol_out_lamports':minimum,'quoted_sol_out_lamports':raw,
                    'token_amount_raw':amount,'pool':pool,
                    'price_impact_pct':impact*100,
                    'warning':'USD_conversion_and_paper_inventory_are_estimated;not_a_fill'}
        except (requests.RequestException,ValueError,TypeError,KeyError,OverflowError,IndexError):
            return {'status':'unverified_sell_quote_unavailable'}

    def probe_micro_sell(self,mint,rec,pair,*,now=None):
        """Bounded nonblocking micro-only held-position exit quote polling."""
        stamp=time.time() if now is None else now
        pool=rec.get('pool')
        buy=(rec.get('entry_route_quote') or
             (rec.get('research_risk_evidence') or {}).get('entry_route_quote') or {})
        amount=buy.get('buy_token_raw_out')
        if not isinstance(amount,int) or amount<=0:
            return {'status':'unverified_paper_entry_token_quantity'}
        try:
            native=float(pair.get('priceNative') or 0)
            usd=float(pair.get('priceUsd') or 0)
            sol_usd=usd/native if native>0 else 0.
            if not math.isfinite(sol_usd) or not 0<sol_usd<100000:
                raise ValueError('invalid_native_quote')
        except (TypeError,ValueError,OverflowError,ZeroDivisionError):
            return {'status':'unverified_sol_usd_conversion'}
        if not self.api_key:return {'status':'unverified_no_jupiter_key'}
        if not hasattr(self,'_micro_exit_pending'):
            self._micro_exit_pending={};self._micro_exit_cache={}
            self._micro_exit_calls=deque()
        key=(mint,pool,amount)
        fut=self._micro_exit_pending.get(key)
        if fut is not None:
            if not fut.done():return {'status':'unverified_sell_quote_pending'}
            self._micro_exit_pending.pop(key,None)
            try:result=fut.result()
            except Exception:result={'status':'unverified_sell_quote_unavailable'}
            self._micro_exit_cache[key]=(stamp,result)
            return result
        cached=self._micro_exit_cache.get(key)
        if cached and stamp-cached[0]<3:
            return cached[1]
        while self._micro_exit_calls and stamp-self._micro_exit_calls[0]>=60:
            self._micro_exit_calls.popleft()
        # Reserve four calls/min for mandatory age-five and risk-triggered
        # exits; routine quote monitoring cannot consume that headroom.
        urgent=bool(rec.get('age_five_exit_prearmed') or rec.get('pending_exit_reason') or
                    float(rec.get('last_liquidity',1e100))<=
                    .60*float(rec.get('entry_liquidity',0)))
        if len(self._micro_exit_calls)>=(12 if urgent else 8):
            return {'status':'unverified_micro_sell_quote_budget'}
        self._micro_exit_calls.append(stamp)
        self._micro_exit_pending[key]=self._executor.submit(
            self._micro_sell_network,mint,pool,amount,sol_usd)
        return {'status':'unverified_sell_quote_pending'}

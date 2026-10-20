"""Optional read-only Jupiter sell quote for research paper exits.

Not a simulated live fill: entries are NOT confirmed buy swaps. Legacy
research may use indicative price-based inventory; micro positions instead
use minimum tokens from the verified same-pool Jupiter buy quote. The latter
is still NOT confirmed inventory, and network/priority costs are unverified.
Never submit transactions or treat quotes as guaranteed executed proceeds.
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
        self.api_key=api_key.strip();self.max_calls_per_minute=max(1,int(max_calls_per_minute))
        self.calls=deque();self.entry_cache={}
        self._executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='v893_jupiter')
        # Micro exits are urgent: entry roundtrip work can take two network
        # requests and must not occupy the workers reserved for exits.
        self._micro_exit_executor=ThreadPoolExecutor(max_workers=2,
            thread_name_prefix='v898_micro_exit')
        self._pending={};self.totals={'attempts':0,'quoted':0,'unverified':0}
    def entry_stats(self,now=None):
        """Non-sensitive read-only quote-budget diagnostics for micro audits."""
        stamp=time.time() if now is None else now
        return {'max_roundtrips_per_minute':self.max_calls_per_minute,
                'used_last_60s':sum(0<=stamp-t<60 for t in self.calls),
                'pending_roundtrips':sum(not item[1].done() for item in self._pending.values()),
                'cached_mints':len(self.entry_cache)}

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

    @staticmethod
    def _pinned_direct_route(quote, input_mint, output_mint, amount, pool):
        """Validate actual token directions, input units and ONE explicit AMM."""
        try:
            plan=quote.get('routePlan') or []
            return (isinstance(pool,str) and bool(pool) and
                    quote.get('inputMint')==input_mint and
                    quote.get('outputMint')==output_mint and
                    int(quote.get('inAmount') or -1)==int(amount) and
                    len(plan)==1 and
                    (plan[0].get('swapInfo') or {}).get('ammKey')==pool and
                    plan[0].get('percent') in (None,100))
        except (TypeError,ValueError,OverflowError,AttributeError,IndexError):
            return False

    def _micro_roundtrip_network(self,mint,size,amount,sol_usd,pool,decimals,spot_usd):
        """Micro-only exact-pool entry/exit check; never makes a swap."""
        try:
            headers={'x-api-key':self.api_key}
            url='https://api.jup.ag/swap/v1/quote'
            buy_response=requests.get(url,params={
                'inputMint':SOL_MINT,'outputMint':mint,'amount':str(amount),
                'slippageBps':'150','swapMode':'ExactIn','onlyDirectRoutes':'true'},
                headers=headers,timeout=3)
            buy_response.raise_for_status();buy=buy_response.json()
            if not self._pinned_direct_route(buy,SOL_MINT,mint,amount,pool):
                return {'status':'rejected_unpinned_micro_entry_route'}
            gross_tokens=int(buy.get('outAmount') or 0)
            minimum_tokens=int(buy.get('otherAmountThreshold') or 0)
            if not 0<minimum_tokens<=gross_tokens:
                return {'status':'unverified_minimum_entry_inventory'}
            # Reject wildly inconsistent same-pool USD market marks. These
            # are a reason to pause, not a licence to guess an entry fill.
            expected_spot_usd=minimum_tokens/(10**decimals)*spot_usd
            if (not math.isfinite(expected_spot_usd) or
                    not size*.80<=expected_spot_usd<=size*1.20):
                return {'status':'rejected_micro_entry_spot_quote_mismatch',
                    'entry_min_inventory_spot_usd':round(expected_spot_usd,6)}
            buy_impact=float(buy.get('priceImpactPct') if buy.get('priceImpactPct') is not None else 'nan')
            if not math.isfinite(buy_impact) or buy_impact<0 or buy_impact>.03:
                return {'status':'rejected_excessive_micro_buy_impact'}
            # Conservative inventory: the minimum tokens that an entry could
            # produce within slippage tolerance, NOT the optimistic quote output.
            sell_response=requests.get(url,params={
                'inputMint':mint,'outputMint':SOL_MINT,'amount':str(minimum_tokens),
                'slippageBps':'150','swapMode':'ExactIn','onlyDirectRoutes':'true'},
                headers=headers,timeout=3)
            sell_response.raise_for_status();sell=sell_response.json()
            if not self._pinned_direct_route(sell,mint,SOL_MINT,minimum_tokens,pool):
                return {'status':'rejected_unpinned_micro_sell_route'}
            sol_out=int(sell.get('outAmount') or 0)
            sol_min=int(sell.get('otherAmountThreshold') or 0)
            impact=float(sell.get('priceImpactPct') if sell.get('priceImpactPct') is not None else 'nan')
            if not 0<sol_min<=sol_out or not math.isfinite(impact) or impact<0:
                return {'status':'unverified_micro_sell_minimum_or_impact'}
            if impact>.03:
                return {'status':'rejected_excessive_micro_sell_impact'}
            conservative_usd=sol_min/1e9*sol_usd
            if not math.isfinite(conservative_usd) or conservative_usd<size*.92:
                return {'status':'rejected_unfavorable_micro_minimum_roundtrip',
                    'minimum_roundtrip_usd':round(conservative_usd,6)}
            return {'status':'two_sided_jupiter_quotes_not_fills',
                'pool':pool,'buy_token_raw_out':gross_tokens,
                'buy_min_token_raw_out':minimum_tokens,
                'minimum_roundtrip_usd':round(conservative_usd,6),
                'gross_roundtrip_return_usd':round(sol_out/1e9*sol_usd,6),
                'max_quote_impact_pct':round(max(buy_impact,impact)*100,4),
                'entry_inventory_basis':'buy_minimum_quote_raw_not_fill',
                'warning':'pinned_pool_read_only_quotes;minimum_inventory_and_sale_are_estimates_not_fills'}
        except (requests.RequestException,ValueError,KeyError,TypeError,OverflowError,AttributeError):
            return {'status':'unverified_micro_pinned_roundtrip'}

    def probe_micro_roundtrip(self,mint,size,decimals,pair,*,now=None):
        """Bounded transient quote retries for micro_0_60 only."""
        return self.probe_roundtrip(mint,size,decimals,pair,now=now,micro_fast_retry=True)

    def probe_roundtrip(self,mint,size,decimals,pair,*,now=None,micro_fast_retry=False):
        stamp=time.time() if now is None else now
        pool=pair.get('pairAddress') if micro_fast_retry and isinstance(pair,dict) else None
        cache_key=(mint,pool) if micro_fast_retry else mint
        cached=self.entry_cache.get(cache_key)
        if cached:
            # Recheck temporary quote-provider failures after bounded delay;
            # successful quotes retain their original 20-second TTL.
            transient=cached[1].get('status')=='unverified_roundtrip_quote'
            ttl=(7 if transient else 10) if micro_fast_retry else 20
            if 0<=stamp-cached[0]<ttl:return cached[1]
        pending=self._pending.get(cache_key)
        if pending:
            start,future=pending
            if not future.done():return {'status':'unverified_roundtrip_quote_pending'}
            self._pending.pop(cache_key,None)
            try:result=future.result()
            except Exception:result={'status':'unverified_roundtrip_quote'}
            if 0<=stamp-start<=(10 if micro_fast_retry else 25):
                # Age quotes from request start, not from delayed polling.
                self.entry_cache[cache_key]=(start,result)
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
        if micro_fast_retry:
            if not isinstance(pool,str) or not pool:
                return {'status':'unverified_micro_pool_address'}
            work=self._executor.submit(self._micro_roundtrip_network,
                                       mint,size,amount,sol_usd,pool,decimals,token_usd)
        else:
            work=self._executor.submit(self._roundtrip_network,mint,size,amount,sol_usd)
        self._pending[cache_key]=(stamp,work)
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
            if not self._pinned_direct_route(payload,mint,SOL_MINT,amount,pool):
                return {'status':'unverified_pinned_pool_sell_route'}
            raw=int(payload.get('outAmount') or 0)
            minimum=int(payload.get('otherAmountThreshold') or 0)
            if not 0<minimum<=raw:return {'status':'unverified_minimum_sell_proceeds'}
            impact=float(payload.get('priceImpactPct') if payload.get('priceImpactPct') is not None else 'nan')
            # A severe IMPACT is precisely when a held token may need an
            # emergency exit. Don't turn a verified tiny sale quote into a
            # fictitious pending position merely because the impact is large.
            # Still reject nonsensical / unverified impact data.
            if not math.isfinite(impact) or impact<0 or impact>1.:
                return {'status':'unverified_or_invalid_sell_impact'}
            return {'status':'pinned_pool_sell_quote_not_fill',
                    'minimum_usd_out_estimate':round(minimum/1e9*sol_usd,8),
                    'minimum_sol_out_lamports':minimum,'quoted_sol_out_lamports':raw,
                    'token_amount_raw':amount,'pool':pool,
                    'price_impact_pct':impact*100,
                    'severe_sell_impact_warning':impact>=.10,
                    'warning':'USD_conversion_and_paper_inventory_are_estimated;not_a_fill'}
        except (requests.RequestException,ValueError,TypeError,KeyError,OverflowError,IndexError):
            return {'status':'unverified_sell_quote_unavailable'}

    def probe_micro_sell(self,mint,rec,pair,*,now=None):
        """Bounded nonblocking micro-only held-position exit quote polling."""
        stamp=time.time() if now is None else now
        pool=rec.get('pool')
        buy=(rec.get('entry_route_quote') or
             (rec.get('research_risk_evidence') or {}).get('entry_route_quote') or {})
        amount=buy.get('buy_min_token_raw_out') or buy.get('buy_token_raw_out')
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
        pending=self._micro_exit_pending.get(key)
        if pending is not None:
            start,fut=pending
            if not fut.done():return {'status':'unverified_sell_quote_pending'}
            self._micro_exit_pending.pop(key,None)
            try:result=fut.result()
            except Exception:result={'status':'unverified_sell_quote_unavailable'}
            # Do not claim an old network quote is fresh simply because its
            # Future was collected now; restart the request on next poll.
            if not 0<=stamp-start<=7:
                self._micro_exit_cache.pop(key,None)
                return {'status':'unverified_stale_sell_quote'}
            self._micro_exit_cache[key]=(start,result)
            return dict(result,quote_age_seconds=round(stamp-start,3),
                        quote_request_epoch=start)
        cached=self._micro_exit_cache.get(key)
        if cached and 0<=stamp-cached[0]<=5:
            return dict(cached[1],quote_age_seconds=round(stamp-cached[0],3),
                        quote_request_epoch=cached[0])
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
        self._micro_exit_pending[key]=(stamp,self._micro_exit_executor.submit(
            self._micro_sell_network,mint,pool,amount,sol_usd))
        return {'status':'unverified_sell_quote_pending'}

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
import logging
import time
import requests
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

v902_log=logging.getLogger(__name__)
SOL_MINT='So11111111111111111111111111111111111111112'
class QuoteObserver:
    def __init__(self,api_key='',*,max_calls_per_minute=3,micro_join_wait_seconds=0.0):
        self.api_key=api_key.strip();self.max_calls_per_minute=max(1,int(max_calls_per_minute))
        self.calls=deque();self.entry_cache={}
        self._executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='v893_jupiter')
        # Micro exits are urgent: entry roundtrip work can take two network
        # requests and must not occupy the workers reserved for exits.
        self._micro_exit_executor=ThreadPoolExecutor(max_workers=2,
            thread_name_prefix='v898_micro_exit')
        self._pending={};self.micro_result_statuses={};self.totals={'attempts':0,'quoted':0,'unverified':0}
        self.micro_stale_completed_quotes=0;self.micro_valid_quote_reuses=0
        # v904: bounded inline consumption only AFTER normal micro risk gates.
        # Off by default for standalone/library callers; the paper boot opts in.
        self.micro_join_wait_seconds=max(0.0,min(4.0,float(micro_join_wait_seconds)))
        self.micro_inline_join_successes=0
        self.micro_inline_join_timeouts=0
        self.micro_inline_join_rejections=0
        # v906: urgent held exits use a separate executor, bounded inline wait
        # and a same-cycle replacement when a completed result has expired.
        self.micro_urgent_exit_inline_successes=0
        self.micro_urgent_exit_inline_timeouts=0
        self.micro_exit_stale_resubmits=0
        self.micro_urgent_exit_last_status={}
    def entry_stats(self,now=None):
        """Non-sensitive read-only quote-budget diagnostics for micro audits."""
        stamp=time.time() if now is None else now
        return {'max_roundtrips_per_minute':self.max_calls_per_minute,
                'used_last_60s':sum(0<=stamp-t<60 for t in self.calls),
                'pending_roundtrips':sum(not item[1].done() for item in self._pending.values()),
                'ready_roundtrips':sum(item[1].done() for item in self._pending.values()),
                'completed_micro_quote_statuses':dict(self.micro_result_statuses),
                'cached_mints':len(self.entry_cache),
                'v903_stale_completed_quotes':self.micro_stale_completed_quotes,
                'v903_fresh_quote_reuses':self.micro_valid_quote_reuses,
                'v904_inline_join_successes':self.micro_inline_join_successes,
                'v904_inline_join_timeouts':self.micro_inline_join_timeouts,
                'v904_inline_join_rejections':self.micro_inline_join_rejections,
                'v904_inline_wait_seconds':self.micro_join_wait_seconds,
                'v906_urgent_exit_inline_successes':self.micro_urgent_exit_inline_successes,
                'v906_urgent_exit_inline_timeouts':self.micro_urgent_exit_inline_timeouts,
                'v906_stale_exit_resubmits':self.micro_exit_stale_resubmits,
                'v906_urgent_exit_last_status':dict(self.micro_urgent_exit_last_status)}

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
            # The BUY is the older of the two quote responses. Timestamp it
            # here so a 7-second TTL bounds the age of BOTH directions.
            quote_observed_at=time.time()
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
                    'pool':pool, 'spot_price_usd':round(spot_usd,12),
                    'token_decimals':decimals,
                    'expected_spot_value_usd':round(expected_spot_usd,6),
                    'nominal_entry_size_usd':round(size,6),
                    'spot_value_to_entry_ratio':round(expected_spot_usd/size,5),
                    'allowed_ratio_range':[.80,1.20],
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
                'pool':pool,'quote_observed_at':quote_observed_at,
                'buy_token_raw_out':gross_tokens,
                'buy_min_token_raw_out':minimum_tokens,
                'minimum_roundtrip_usd':round(conservative_usd,6),
                'gross_roundtrip_return_usd':round(sol_out/1e9*sol_usd,6),
                'max_quote_impact_pct':round(max(buy_impact,impact)*100,4),
                'entry_inventory_basis':'buy_minimum_quote_raw_not_fill',
                'warning':'pinned_pool_read_only_quotes;minimum_inventory_and_sale_are_estimates_not_fills'}
        except (requests.RequestException,ValueError,KeyError,TypeError,OverflowError,AttributeError):
            return {'status':'unverified_micro_pinned_roundtrip'}

    @staticmethod
    def _micro_quote_time(start,result,stamp):
        # Untrusted/missing worker timestamps NEVER make a quote newer than
        # the actual request start. Only a worker-issued timestamp within the
        # request/observation interval may refine freshness.
        recorded=result.get('quote_observed_at') if isinstance(result,dict) else None
        if (isinstance(recorded,(int,float)) and math.isfinite(recorded)
                and start<=recorded<=stamp+0.5):
            return float(recorded)
        if (isinstance(recorded,(int,float)) and math.isfinite(recorded)
                and recorded<start):
            # A claimed response older than its own request is impossible.
            # Do not substitute the newer request start to make it fresh.
            return float('-inf')
        # Missing/untrusted/future worker timestamps are conservatively
        # dated from request start; a later clock cannot refresh an old quote.
        return start

    def ready_micro_mints(self,*,now=None):
        """Hint callback scheduler about fresh, finished same-pool quotes.

        Does not authorize any entry. Only the normal probe/observe path may
        consume a quote after re-verifying the pool, mint and market gates.
        """
        stamp=time.time() if now is None else now
        ready=set()
        for key,(start,future) in tuple(self._pending.items()):
            if not (isinstance(key,tuple) and len(key)==2 and future.done()):
                continue
            try:result=future.result()
            except Exception:continue
            observed=self._micro_quote_time(start,result,stamp)
            if (result.get('status')=='two_sided_jupiter_quotes_not_fills'
                    and result.get('pool')==key[1] and 0<=stamp-observed<=7):
                ready.add(key[0])
        for key,(observed,result) in tuple(self.entry_cache.items()):
            if (isinstance(key,tuple) and len(key)==2 and
                    result.get('status')=='two_sided_jupiter_quotes_not_fills'
                    and result.get('pool')==key[1] and 0<=stamp-observed<=7):
                ready.add(key[0])
        return ready

    def quote_inflight_or_fresh(self,mint,*,now=None):
        stamp=time.time() if now is None else now
        return (mint in self.ready_micro_mints(now=stamp) or
                any(isinstance(key,tuple) and key[0]==mint and not future.done()
                    for key,(start,future) in self._pending.items()
                    if 0<=stamp-start<=12))

    def probe_micro_roundtrip(self,mint,size,decimals,pair,*,now=None):
        """Bounded transient quote retries for micro_0_60 only."""
        return self.probe_roundtrip(mint,size,decimals,pair,now=now,micro_fast_retry=True)

    def probe_micro_roundtrip_join(self,mint,size,decimals,pair,*,now=None):
        """Join a *new* short-lived quote while a candidate's other gates pass.

        The regular entry scanner may only revisit a mint every 5-15 seconds;
        with a seven-second TTL it repeatedly arrived after the quote expired.
        Instead, after on-chain/market gates pass, wait at most four seconds
        for the already submitted read-only worker and consume its result in
        this SAME observe() invocation. This is not a transaction or bypass:
        `probe_micro_roundtrip` still checks actual pool, quoted minimums,
        network response age and the rate limit; failures remain fail-closed.
        """
        result=self.probe_micro_roundtrip(mint,size,decimals,pair,now=now)
        if (self.micro_join_wait_seconds<=0 or
                result.get('status')!='unverified_roundtrip_quote_pending'):
            return result
        pool=pair.get('pairAddress') if isinstance(pair,dict) else None
        if not pool:return result
        work=self._pending.get((mint,pool))
        if work is None:return result
        _started,future=work
        try:
            future.result(timeout=self.micro_join_wait_seconds)
        except FutureTimeout:
            self.micro_inline_join_timeouts+=1
            return result
        except Exception:
            self.micro_inline_join_rejections+=1
            return {'status':'unverified_micro_pinned_roundtrip'}
        # Never evaluate freshness using the original scan timestamp: the
        # worker may have finished seconds after scan time. Fetch the result
        # through the normal quote cache/validation with wall-clock NOW.
        completed_at=time.time()
        confirmed=self.probe_micro_roundtrip(mint,size,decimals,pair,now=completed_at)
        if (confirmed.get('status')=='two_sided_jupiter_quotes_not_fills'
                and confirmed.get('pool')==pool):
            self.micro_inline_join_successes+=1
            v902_log.info('V904 MICRO INLINE QUOTE JOIN mint=%s pool=%s '
                'quote_age_s=%.3f market_security_checked=True paper_only=True',
                mint,pool,max(0.0,completed_at-float(confirmed.get('quote_observed_at',completed_at))))
        else:
            self.micro_inline_join_rejections+=1
        return confirmed

    def probe_roundtrip(self,mint,size,decimals,pair,*,now=None,micro_fast_retry=False):
        stamp=time.time() if now is None else now
        pool=pair.get('pairAddress') if micro_fast_retry and isinstance(pair,dict) else None
        cache_key=(mint,pool) if micro_fast_retry else mint
        cached=self.entry_cache.get(cache_key)
        if cached:
            # A successful micro quote may be used only within 7 seconds of
            # the older Jupiter response, never 10 seconds of polling.
            transient=cached[1].get('status')=='unverified_roundtrip_quote'
            if micro_fast_retry:
                ttl=(7 if cached[1].get('status')=='two_sided_jupiter_quotes_not_fills'
                     else 7 if transient else 10)
            else:
                ttl=20
            if 0<=stamp-cached[0]<=ttl:
                if micro_fast_retry and cached[1].get('status')=='two_sided_jupiter_quotes_not_fills':
                    self.micro_valid_quote_reuses+=1
                return cached[1]
            self.entry_cache.pop(cache_key,None)
        pending=self._pending.get(cache_key)
        if pending:
            start,future=pending
            if not future.done():return {'status':'unverified_roundtrip_quote_pending'}
            self._pending.pop(cache_key,None)
            try:result=future.result()
            except Exception:result={'status':'unverified_roundtrip_quote'}
            if micro_fast_retry:
                status=str(result.get('status','unknown'))
                self.micro_result_statuses[status]=self.micro_result_statuses.get(status,0)+1
                if status=='rejected_micro_entry_spot_quote_mismatch':
                    v902_log.warning('V902 MICRO ENTRY QUOTE MISMATCH pool=%s mint=%s '
                        'nominal_usd=%s spot_min_value_usd=%s ratio=%s '
                        'limit_low=0.80 limit_high=1.20 no_paper_buy=True',
                        pool,mint,result.get('nominal_entry_size_usd'),
                        result.get('entry_min_inventory_spot_usd'),
                        result.get('spot_value_to_entry_ratio'))
            observed=(self._micro_quote_time(start,result,stamp)
                      if micro_fast_retry and result.get('status')=='two_sided_jupiter_quotes_not_fills'
                      else start)
            ttl=(7 if micro_fast_retry and result.get('status')=='two_sided_jupiter_quotes_not_fills'
                 else 10 if micro_fast_retry else 25)
            if 0<=stamp-observed<=ttl:
                # Retain the older provider response time for a valid
                # two-sided micro quote; failed quotes use request start.
                self.entry_cache[cache_key]=(observed,result)
                return result
            if micro_fast_retry and result.get('status')=='two_sided_jupiter_quotes_not_fills':
                self.micro_stale_completed_quotes+=1
                v902_log.info('V903 MICRO EXPIRED QUOTE mint=%s pool=%s quote_age_s=%.3f '
                    'will_requote=True no_entry_from_stale_result=True',mint,pool,stamp-observed)
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

    def _micro_exit_result(self, result, pool, start, stamp):
        """Never promote a stale/wrong-pool/non-finite network quote to a sale."""
        elapsed=stamp-start
        if not 0 <= elapsed <= 7:
            return {'status':'unverified_stale_sell_quote'}
        response=dict(result)
        response['quote_age_seconds']=round(elapsed,3)
        response['quote_request_epoch']=start
        # Only a successful exact-pool Jupiter response can ever be credited.
        if response.get('status')=='pinned_pool_sell_quote_not_fill':
            try:
                amount=float(response['minimum_usd_out_estimate'])
            except (KeyError,TypeError,ValueError,OverflowError):
                return {'status':'unverified_sell_quote_metadata'}
            if (response.get('pool')!=pool or not math.isfinite(amount) or
                    amount<0):
                return {'status':'unverified_sell_quote_metadata'}
        return response

    def probe_micro_sell(self,mint,rec,pair,*,now=None,urgent=False,
                         wait_seconds=0.0):
        """Quote a HELD paper position. An urgent check can synchronously join
        a fresh request (max 2.75 s); all quote/route/age requirements stay.

        Nonurgent callers remain fully nonblocking and retain their historical
        stale-future behavior. No network request is made on behalf of a real
        wallet, and an unavailable quote never creates fictitious proceeds.
        """
        stamp=time.time() if now is None else float(now)
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
        def present_time():
            # Production normally passes a snapshot time captured moments ago;
            # tests often use a fixed synthetic clock. Preserve both without
            # treating a several-second inline wait as if zero time elapsed.
            actual=time.time()
            return actual if abs(actual-stamp)<60 else stamp

        def take_result(start,fut,*,clock):
            try:result=fut.result()
            except Exception:result={'status':'unverified_sell_quote_unavailable'}
            verified=self._micro_exit_result(result,pool,start,clock)
            if verified.get('status')!='unverified_stale_sell_quote':
                self._micro_exit_cache[key]=(start,result)
            else:
                self._micro_exit_cache.pop(key,None)
            return verified

        pending=self._micro_exit_pending.get(key)
        if pending is not None:
            start,fut=pending
            clock=present_time()
            if fut.done():
                self._micro_exit_pending.pop(key,None)
                response=take_result(start,fut,clock=clock)
                if response['status']!='unverified_stale_sell_quote' or not urgent:
                    return response
                # Before v906 an expired finished future COST ANOTHER scan to
                # start a new request. An urgent holder cannot afford that.
                self.micro_exit_stale_resubmits+=1
                v902_log.info('V906 MICRO EXIT STALE REQUOTE mint=%s pool=%s '
                              'old_age_s=%.3f new_quote_same_cycle=True '
                              'no_credit_from_stale=True',mint,pool,clock-start)
            elif clock-start>7 and urgent:
                # Stuck provider future: unlink but don't use its response.
                self._micro_exit_pending.pop(key,None)
                self._micro_exit_cache.pop(key,None)
                self.micro_exit_stale_resubmits+=1
            elif urgent and wait_seconds>0:
                from concurrent.futures import TimeoutError as _WaitTimeout
                try:
                    fut.result(timeout=max(0.,min(float(wait_seconds),2.75)))
                except _WaitTimeout:
                    self.micro_urgent_exit_inline_timeouts+=1
                    return {'status':'unverified_sell_quote_pending'}
                except Exception:
                    pass
                self._micro_exit_pending.pop(key,None)
                response=take_result(start,fut,clock=present_time())
                if response['status']!='unverified_stale_sell_quote':
                    if response['status']=='pinned_pool_sell_quote_not_fill':
                        self.micro_urgent_exit_inline_successes+=1
                    return response
                self.micro_exit_stale_resubmits+=1
            else:
                return {'status':'unverified_sell_quote_pending'}

        clock=present_time()
        cached=self._micro_exit_cache.get(key)
        if cached and 0<=clock-cached[0]<=5:
            return self._micro_exit_result(cached[1],pool,cached[0],clock)
        if cached:self._micro_exit_cache.pop(key,None)
        while self._micro_exit_calls and clock-self._micro_exit_calls[0]>=60:
            self._micro_exit_calls.popleft()
        priority=bool(urgent or rec.get('age_five_exit_prearmed') or
                      rec.get('pending_exit_reason') or
                      float(rec.get('last_liquidity',1e100))<=
                      .60*float(rec.get('entry_liquidity',0)))
        if len(self._micro_exit_calls)>=(12 if priority else 8):
            if priority:self.micro_urgent_exit_last_status[mint]='unverified_micro_sell_quote_budget'
            return {'status':'unverified_micro_sell_quote_budget'}
        self._micro_exit_calls.append(clock)
        future=self._micro_exit_executor.submit(self._micro_sell_network,
                                                 mint,pool,amount,sol_usd)
        self._micro_exit_pending[key]=(clock,future)
        if not urgent or wait_seconds<=0:
            return {'status':'unverified_sell_quote_pending'}
        from concurrent.futures import TimeoutError as _WaitTimeout
        try:
            future.result(timeout=max(0.,min(float(wait_seconds),2.75)))
        except _WaitTimeout:
            self.micro_urgent_exit_inline_timeouts+=1
            self.micro_urgent_exit_last_status[mint]='unverified_sell_quote_pending'
            v902_log.info('V906 MICRO URGENT EXIT mint=%s pool=%s '
                          'status=awaiting_fresh_sell_quote no_paper_sale=True',mint,pool)
            return {'status':'unverified_sell_quote_pending'}
        except Exception:
            pass
        self._micro_exit_pending.pop(key,None)
        result=take_result(clock,future,clock=present_time())
        self.micro_urgent_exit_last_status[mint]=result['status']
        if result['status']=='pinned_pool_sell_quote_not_fill':
            self.micro_urgent_exit_inline_successes+=1
        v902_log.info('V906 MICRO URGENT EXIT mint=%s pool=%s status=%s '
                      'quote_age_s=%s same_pool_required=True paper_only=True',
                      mint,pool,result['status'],result.get('quote_age_seconds'))
        return result

    def probe_micro_sell_urgent(self,mint,rec,pair,*,now=None):
        """Bounded same-cycle held-exit path; never authorizes a live fill."""
        return self.probe_micro_sell(mint,rec,pair,now=now,urgent=True,
                                     wait_seconds=2.75)

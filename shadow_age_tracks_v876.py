"""Two independent, *indicative* background paper portfolios for age-window research.

The original portfolios trade separately from the legacy 5-12-minute paper
ledger; v8.7.7 can promote the original 0_60 portfolio without changing its
entry/exit thresholds, while disabling its automatic trading halts if requested.
Legacy quoted prices come from pinned DexScreener pools. The micro paper
track uses exact-pool Jupiter minimum-out quotes but those are NOT fills or
guaranteed executable amounts. No fresh quote => no invented exit.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any

from early_watch_shadow_v80 import (pool_evidence, SHADOW_MIN_CAP_USD,
    SHADOW_MIN_5M_VOLUME_USD, SHADOW_MIN_5M_BUYS, SHADOW_MIN_5M_SELLS,
    SHADOW_MIN_BUY_SELL_RATIO, SHADOW_MIN_5M_CHANGE_PCT,
    SHADOW_MAX_5M_CHANGE_PCT, SHADOW_ESTIMATED_ROUND_TRIP_COST_PCT)
from rolling_discovery import MINT_PATTERN

v902_log=logging.getLogger(__name__)


def flow_filter_reasons(pair: dict, e: dict, *, surge_change: float=13.5,
                        surge_volume: float=2500.,
                        min_cap: float=SHADOW_MIN_CAP_USD,
                        min_volume5_usd: float | None=SHADOW_MIN_5M_VOLUME_USD,
                        max_change: float | None=SHADOW_MAX_5M_CHANGE_PCT,
                        min_buys5: int=SHADOW_MIN_5M_BUYS,
                        min_sells5: int=SHADOW_MIN_5M_SELLS,
                        min_buy_sell_ratio: float=SHADOW_MIN_BUY_SELL_RATIO,
                        min_change5: float=SHADOW_MIN_5M_CHANGE_PCT) -> list[str]:
    """Reasons for age-study entry rejection; does not alter other strategies.

    A None 5m volume minimum (micro_0_60 only) also disables the
    surge-volume gate, but retains m5 price movement and 5m buys/sells.
    """
    vol=pair.get('volume') or {}
    tx=(pair.get('txns') or {}).get('m5') or {}
    change=pair.get('priceChange') or {}
    if not all(isinstance(d,dict) for d in (vol,tx,change)):
        return ['invalid_m5_market_data']
    reasons=[]
    if min_volume5_usd is not None and 'm5' not in vol:
        reasons.append('missing_volume5')
    if 'buys' not in tx or 'sells' not in tx:
        reasons.append('missing_transactions5')
    if 'm5' not in change or not math.isfinite(e['change5']):
        reasons.append('missing_price_change5')
    if e['cap']<min_cap:reasons.append('market_cap_below_floor')
    if min_volume5_usd is not None and e['v5']<min_volume5_usd:
        reasons.append('volume5_below_floor')
    if e['buys']<min_buys5:reasons.append('buys5_below_floor')
    if e['sells']<min_sells5:reasons.append('sells5_below_floor')
    if e['sells']>0 and e['buys']/e['sells']<min_buy_sell_ratio:
        reasons.append('buy_sell_ratio_below_floor')
    if math.isfinite(e['change5']):
        if e['change5']<min_change5:
            reasons.append('price_change5_below_floor')
        if max_change is not None and e['change5']>max_change:
            reasons.append('price_change5_above_ceiling')
        if (min_volume5_usd is not None and
                e['change5']>=surge_change and e['v5']<surge_volume):
            reasons.append('thin_volume_surge')
    return reasons


def valid_flow(pair: dict, e: dict, *, surge_change: float=13.5,
               surge_volume: float=2500., min_cap: float=SHADOW_MIN_CAP_USD,
               max_change: float | None=SHADOW_MAX_5M_CHANGE_PCT,
               min_volume5_usd: float | None=SHADOW_MIN_5M_VOLUME_USD) -> bool:
    return not flow_filter_reasons(pair,e,surge_change=surge_change,
        surge_volume=surge_volume,min_cap=min_cap,max_change=max_change,
        min_volume5_usd=min_volume5_usd)


class AgeTrack:
    VERSION=1
    def __init__(self, *, name: str, min_age: float, path: Path,
                 max_age: float=60., starting_capital: float=36.,
                 size: float=2., max_positions: int=5,
                 halts_enabled: bool=True,
                 take_profit_net_pct: float=8.,
                 exit_on_profitable_reversal: bool=False,
                 max_entry_market_cap_usd: float | None = None,
                 trailing_activation_net_pct: float=0.,
                 trailing_drawdown_pct_points: float=0.,
                 take_profit_enabled: bool=True,
                 min_entry_market_cap_usd: float=SHADOW_MIN_CAP_USD,
                 min_entry_liquidity_usd: float=15000.,
                 min_entry_volume5_usd: float | None=SHADOW_MIN_5M_VOLUME_USD,
                 max_entry_change5_pct: float | None=SHADOW_MAX_5M_CHANGE_PCT,
                 research_risk_gate=None,
                 research_quote_probe=None):
        if not 0 <= min_age < max_age <= 60: raise ValueError('bad_age_window')
        self.name=name; self.min_age=min_age; self.max_age=max_age
        self.path=Path(path); self.starting_capital=float(starting_capital)
        self.size=float(size); self.max_positions=max_positions
        # The promoted 0_60 paper trader can explicitly disable loss-based
        # trading halts without changing ANY entry/exit thresholds.
        self.halts_enabled=bool(halts_enabled)
        if not math.isfinite(float(take_profit_net_pct)) or take_profit_net_pct<=0:
            raise ValueError('invalid_take_profit_net_pct')
        self.take_profit_net_pct=float(take_profit_net_pct)
        self.exit_on_profitable_reversal=bool(exit_on_profitable_reversal)
        if not all(math.isfinite(float(x)) and float(x)>=0 for x in
                   (trailing_activation_net_pct,trailing_drawdown_pct_points)):
            raise ValueError('invalid_trailing_configuration')
        self.trailing_activation_net_pct=float(trailing_activation_net_pct)
        self.trailing_drawdown_pct_points=float(trailing_drawdown_pct_points)
        self.take_profit_enabled=bool(take_profit_enabled)
        self.min_entry_market_cap_usd=float(min_entry_market_cap_usd)
        self.min_entry_liquidity_usd=float(min_entry_liquidity_usd)
        self.min_entry_volume5_usd=(None if min_entry_volume5_usd is None
                                    else float(min_entry_volume5_usd))
        self.max_entry_change5_pct=max_entry_change5_pct
        # V905: Independently tunable MICRO momentum filters. All other
        # portfolio rules, LP withdrawal, holder, reserve and Jupiter gates
        # remain unchanged. Values stay within tested conservative bands.
        self.micro_momentum_profile='v905_balanced_more_candidates' if name=='micro_0_60' else 'legacy_unchanged'
        if name=='micro_0_60':
            def micro_num(key,default,lo,hi):
                try:
                    value=float(os.getenv(key,str(default)))
                    if not math.isfinite(value):raise ValueError(key)
                    return max(lo,min(hi,value))
                except (ValueError,TypeError,OverflowError):
                    return float(default)
            self.min_buys5=int(micro_num('MICRO_MIN_BUYS5',6,3,10))
            self.min_sells5=SHADOW_MIN_5M_SELLS
            self.min_buy_sell_ratio=micro_num('MICRO_MIN_BUY_SELL_RATIO',1.10,1.0,1.25)
            self.min_change5=micro_num('MICRO_MIN_CHANGE5_PCT',0.30,0.0,0.8)
        else:
            self.min_buys5=SHADOW_MIN_5M_BUYS
            self.min_sells5=SHADOW_MIN_5M_SELLS
            self.min_buy_sell_ratio=SHADOW_MIN_BUY_SELL_RATIO
            self.min_change5=SHADOW_MIN_5M_CHANGE_PCT
        if not (math.isfinite(self.min_entry_market_cap_usd) and self.min_entry_market_cap_usd>0 and
                math.isfinite(self.min_entry_liquidity_usd) and self.min_entry_liquidity_usd>0):
            raise ValueError('invalid_research_threshold')
        self.research_risk_gate=research_risk_gate
        self.research_quote_probe=research_quote_probe
        self.research_rejections={}
        self.entry_filter_rejections={}
        self.legacy_volume_observations={}
        # In-memory, never represents a verified route or entry approval.
        self.pending_followups={}
        self.retry_after={}
        # Per-process UNIQUE pool funnel; old persisted ledger is untouched.
        # A pool may appear in several stages over time (e.g. pending -> pass).
        self._micro_unique_funnel={}

        if max_entry_market_cap_usd is not None:
            max_entry_market_cap_usd=float(max_entry_market_cap_usd)
            if not math.isfinite(max_entry_market_cap_usd) or max_entry_market_cap_usd<=0:
                raise ValueError('invalid_max_entry_market_cap_usd')
        self.max_entry_market_cap_usd=max_entry_market_cap_usd
        self.historical_halt_reason=None
        self.cash=self.starting_capital; self.positions={};self.closed=[]
        self.seen=set();self.reentered_after8=set();self.healthy_samples=0;self.open_count=0
        self.missed_quotes=0;self.halted=False;self.halt_reason=None;self.read_only=False;self.loaded=False;self.changed=False
        self.error=None

    def load(self):
        if self.loaded:return
        self.loaded=True
        try: data=json.loads(self.path.read_text(encoding='utf8'))
        except FileNotFoundError:
            self.changed=True
            return
        except (OSError,ValueError,TypeError) as exc:
            self.read_only=True;self.error=type(exc).__name__;return
        if not isinstance(data,dict) or data.get('version') != self.VERSION or data.get('name')!=self.name:
            self.read_only=True;self.error='invalid_version_or_track';return
        if (not isinstance(data.get('positions'),dict) or not isinstance(data.get('closed'),list)
                or not isinstance(data.get('seen'),list)):
            self.read_only=True;self.error='invalid_ledger';return
        cash=data.get('cash')
        if not isinstance(cash,(int,float)) or not math.isfinite(cash) or cash < -0.001:
            self.read_only=True;self.error='invalid_cash';return
        self.cash=float(cash)
        positions={}
        for k,v in data['positions'].items():
            if not isinstance(k,str) or not MINT_PATTERN.fullmatch(k) or not isinstance(v,dict):
                self.read_only=True;self.error='invalid_position';return
            try:
                price=float(v.get('entry_price'))
                size=float(v.get('entry_liquidity'))
                opened=float(v.get('opened_at'))
            except (ValueError,TypeError,OverflowError):
                self.read_only=True;self.error='invalid_position';return
            if (not all(math.isfinite(x) for x in (price,size,opened)) or price<=0
                    or size<=0 or opened<=0 or not isinstance(v.get('pool'),str)):
                self.read_only=True;self.error='invalid_position';return
            positions[k]=v
        self.positions=positions
        self.closed=[r for r in data['closed'][-1000:] if isinstance(r,dict)]
        self.seen={m for m in data['seen'][-4000:] if isinstance(m,str) and MINT_PATTERN.fullmatch(m)}
        if self.name=='micro_0_60':
            self.reentered_after8={m for m in data.get('reentered_after8',[])
                if isinstance(m,str) and MINT_PATTERN.fullmatch(m)}
            # Recover one-time re-entry use from older ledgers when applicable.
            self.reentered_after8.update(str(r['mint']) for r in data['closed']
                if isinstance(r,dict) and isinstance(r.get('entry_age_minutes'),(int,float))
                and r['entry_age_minutes']>=8 and isinstance(r.get('mint'),str))
            self.reentered_after8.update(m for m,r in positions.items()
                if isinstance(r.get('entry_age_minutes'),(int,float))
                and r['entry_age_minutes']>=8)
        self.healthy_samples=int(data.get('healthy_samples',0))
        self.open_count=int(data.get('open_count',len(self.closed)+len(self.positions)))
        self.missed_quotes=int(data.get('missed_quotes',0))
        if self.name=='micro_0_60':
            self.entry_filter_rejections={str(k):int(v) for k,v in
                (data.get('entry_filter_rejections') or {}).items()
                if isinstance(v,int) and v>=0}
            self.legacy_volume_observations={str(k):int(v) for k,v in
                (data.get('legacy_volume_observations') or {}).items()
                if isinstance(v,int) and v>=0}
        if self.research_risk_gate is not None:
            self.research_rejections={str(k):int(v) for k,v in
                (data.get('research_rejections') or {}).items()
                if isinstance(v,int) and v>=0}
        if self.halts_enabled:
            self.halted=bool(data.get('halted',False))
            self.halt_reason=data.get('halt_reason')
        else:
            # Preserve the historical halt cause for audit; it is no longer
            # an automatic entry restriction on the promoted 0_60 track.
            self.historical_halt_reason=(data.get('historical_halt_reason') or
                data.get('halt_reason'))
            self.halted=False;self.halt_reason=None
            if data.get('halted') or data.get('halt_reason'):
                self.changed=True

    def _micro_funnel_note(self,pool,stage,now,*,reason=None,mint=None,age=None):
        if self.name!='micro_0_60' or not pool:return
        rec=self._micro_unique_funnel.setdefault(pool,{'last':now,'stages':set()})
        rec['last']=now
        previous_stage=rec.get('latest_stage')
        previous_reason=rec.get('latest_reason')
        rec['stages'].add(stage)
        rec['latest_stage']=stage
        if reason is not None:rec['latest_reason']=reason
        # Stage transitions are logged once, not on every market polling tick.
        if (stage in ('research_waiting','research_security_blocked',
                      'research_security_passed','jupiter_quote_waiting',
                      'jupiter_quote_blocked','jupiter_roundtrip_passed',
                      'paper_entry_opened') and
                (stage!=previous_stage or (reason and reason!=previous_reason))):
            v902_log.info('V902 MICRO CANDIDATE TRACE pool=%s mint=%s '
                          'age_min=%s stage=%s reason=%s read_only=True',
                          pool,mint or 'unknown',
                          ('%.3f'%age if age is not None else 'unknown'),
                          stage,reason or 'none')
        # In-memory diagnostics for *this deployment*, not a historical
        # unique-trade metric. Bound memory without erasing persisted trades.
        if len(self._micro_unique_funnel)>1800:
            oldest=sorted(self._micro_unique_funnel,
                key=lambda k:self._micro_unique_funnel[k]['last'])[:200]
            for k in oldest:self._micro_unique_funnel.pop(k,None)

    def _micro_funnel_stats(self):
        counts={}
        for rec in self._micro_unique_funnel.values():
            for stage in rec['stages']:
                counts[stage]=counts.get(stage,0)+1
        return {'scope':'unique_pool_addresses_since_process_start',
                'pools':len(self._micro_unique_funnel),
                'stages_not_mutually_exclusive':counts,
                'latest_stage_counts':dict(__import__('collections').Counter(
                    rec.get('latest_stage','unknown')
                    for rec in self._micro_unique_funnel.values()))}

    def entry_age_allowed(self, age: float) -> bool:
        """Micro 0–5 minutes and 8–60 minutes; exclude [5,8)."""
        return self.min_age <= age < self.max_age and not (
            self.name=='micro_0_60' and 5. <= age < 8.)

    def eligible_new_mint(self, mint: str, age: float) -> bool:
        if not self.entry_age_allowed(age) or mint in self.positions:
            return False
        if mint not in self.seen:
            return True
        return (self.name=='micro_0_60' and age>=8. and
                mint not in self.reentered_after8 and
                any(c.get('mint')==mint and float(c.get('entry_age_minutes',8.))<5.
                    for c in self.closed))

    def _research_reject(self,reason,mint=None,now=None):
        if self.name=='micro_0_60' and mint:
            if reason in ('research_risk:unverified_rpc_pending',
                          'research_route:unverified_roundtrip_quote_pending'):
                self.pending_followups[mint]=time.time() if now is None else now
            elif (reason in ('research_risk:unverified_pool_check_pending',
                             'research_risk:unverified_pool_check_cooldown',
                             'research_risk:unverified_lp_security_pending') and
                  self.research_quote_probe is not None and
                  hasattr(self.research_quote_probe,'quote_inflight_or_fresh') and
                  self.research_quote_probe.quote_inflight_or_fresh(mint,now=now)):
                # A completed Jupiter quote must not lose its urgent callback
                # just because the next on-chain snapshot is still pending.
                # This schedules a RECHECK, not a trade or stale approval.
                self.pending_followups[mint]=time.time() if now is None else now
                self.retry_after[mint]=(time.time() if now is None else now)+3
            elif reason in ('research_risk:unverified_rpc_failure',
                            'research_route:unverified_roundtrip_quote',
                          'research_route:unverified_micro_pinned_roundtrip',
                          'research_route:unverified_quote_budget',
                          'research_route:unverified_micro_pool_address',
                          'research_route:unverified_minimum_entry_inventory',
                          'research_route:unverified_micro_sell_minimum_or_impact',
                            'research_risk:unverified_missing_mint_account',
                          'research_risk:unverified_pool_check_pending',
                          'research_risk:unverified_pool_check_cooldown',
                          'research_risk:unverified_pool_security_budget',
                          'research_risk:unverified_60s_reserve_history',
                          'research_risk:unverified_lp_security_pending',
                          'research_risk:unverified_pool_rpc_unavailable',
                          'research_risk:unverified_pool_security',
                          'research_risk:unverified_lp_or_holder_security'):
                self.pending_followups.pop(mint,None)
                self.retry_after[mint]=(time.time() if now is None else now)+15
            elif reason in ('research_route:rejected_unpinned_micro_entry_route',
                            'research_route:rejected_unpinned_micro_sell_route',
                            'research_route:rejected_micro_entry_spot_quote_mismatch'):
                # Jupiter onlyDirectRoutes is not a pool pin. Retry later in
                # case the best direct AMM changes, but NEVER accept a route
                # through a pool that has not passed the same safety checks.
                self.pending_followups.pop(mint,None)
                self.retry_after[mint]=(time.time() if now is None else now)+30
            else:
                self.pending_followups.pop(mint,None)
                self.retry_after.pop(mint,None)
        self.research_rejections[reason]=self.research_rejections.get(reason,0)+1
        self.changed=True
        return reason

    def observe(self, pair: dict, *, sol_mint: str, dexes, now: float | None=None,
                surge_change: float=13.5, surge_volume: float=2500.) -> str:
        self.load()
        if self.read_only:return 'read_only'
        stamp=time.time() if now is None else now
        mint=(pair.get('baseToken') or {}).get('address') if isinstance(pair,dict) else None
        rec=self.positions.get(mint)
        e=pool_evidence(pair,sol_mint,dexes,now_ms=stamp*1000,
                        min_liquidity=self.min_entry_liquidity_usd)
        if rec is not None and e is None:
            # Holding exits MUST work after liquidity falls below entry floor,
            # cap collapses, or the position ages past the original entry window.
            try:
                price=float(pair.get('priceUsd',0))
                liq=float((pair.get('liquidity') or {}).get('usd',0))
            except (TypeError,ValueError,OverflowError):price=liq=float('nan')
            if (pair.get('chainId')=='solana' and
                (pair.get('quoteToken') or {}).get('address')==sol_mint and
                (pair.get('dexId') or '').lower() in {d.lower() for d in dexes} and
                pair.get('pairAddress')==rec['pool'] and
                math.isfinite(price) and price>0 and math.isfinite(liq) and liq>=0):
                e={'mint':mint,'pool':rec['pool'],'price':price,'liquidity':liq}
        if e is None:return 'unverified_pool'
        if rec is not None:
            if e['pool'] != rec['pool']:return 'different_pool'
            price=e['price']
            elapsed=max(0.,stamp-rec['opened_at'])
            net_pct=((price/rec['entry_price'])-1)*100-SHADOW_ESTIMATED_ROUND_TRIP_COST_PCT
            # Observe an actual decline from a PREVIOUSLY observed positive
            # modeled-net peak. Never treat an unchanged price as a reversal.
            # In particular, an entry starts at -6% after modeled costs,
            # so a gross price rise is not necessarily a profitable trade.
            prior_peak_net_pct=float(rec.get('peak_net_pct',rec.get('last_net_pct',-6.)))
            profitable_reversal=(self.exit_on_profitable_reversal and
                prior_peak_net_pct>0. and
                prior_peak_net_pct>=self.trailing_activation_net_pct and
                net_pct<=prior_peak_net_pct-self.trailing_drawdown_pct_points and
                net_pct<prior_peak_net_pct)
            rec.pop('missing_since',None)
            rec['observed_at']=stamp;rec['last_price']=price
            rec['last_net_pct']=round(net_pct,5)
            rec['peak_net_pct']=max(float(rec.get('peak_net_pct',-100)),net_pct)
            rec['observations']=int(rec.get('observations',0))+1
            rec['last_liquidity']=e['liquidity'];self.changed=True
            if self.halts_enabled and net_pct<=-50. and not self.halted:
                self.halted=True;self.halt_reason='catastrophic_observed_virtual_loss'
            # A micro holding opened before age five has a hard exit intent at
            # five minutes. Never manufacture proceeds if pool or sell route
            # data becomes unavailable. Initiate read-only quotes in advance.
            token_age=self.positions[mint]['entry_age_minutes']+elapsed/60.
            micro_age_exit=(self.name=='micro_0_60' and
                rec['entry_age_minutes']<5. and token_age>=5.)
            # The $2 micro position is modeled using the conservative buy
            # minimum and a freshly verified exact-pool sell minimum. Dex
            # price-only returns are retained solely for comparison.
            token_quote=None
            if (self.name=='micro_0_60' and self.research_quote_probe is not None and
                    hasattr(self.research_quote_probe,'probe_micro_sell')):
                # Pre-arm at 4m15s, not 4m45s; near the boundary the caller
                # schedules frequent fresh-pool checks. No fabricated fills.
                urgent_age=rec['entry_age_minutes']<5. and token_age>=4.25
                if urgent_age:
                    rec['age_five_exit_prearmed']=True
                last_poll=float(rec.get('last_micro_sell_quote_check_at',0))
                urgent_risk=(bool(rec.get('pending_exit_reason')) or
                             micro_age_exit or net_pct<=-9. or
                             e['liquidity']<=rec['entry_liquidity']*.60)
                quote_due=(urgent_age or stamp-last_poll>=12 or urgent_risk or
                           e['liquidity']<=rec['entry_liquidity']*.90)
                if quote_due:
                    rec['last_micro_sell_quote_check_at']=stamp
                    # v906: Keep routine observation nonblocking, but once a
                    # risk/age exit is due, JOIN a fresh sell quote promptly.
                    # Legacy/mock observers retain their existing interface.
                    urgent_probe=getattr(self.research_quote_probe,
                                         'probe_micro_sell_urgent',None)
                    token_quote=(urgent_probe(mint,rec,pair,now=stamp)
                                 if urgent_risk and callable(urgent_probe)
                                 else self.research_quote_probe.probe_micro_sell(
                                     mint,rec,pair,now=stamp))
                    rec['held_sell_quote_status']=token_quote.get('status')
                    if token_quote.get('status')=='pinned_pool_sell_quote_not_fill':
                        try:
                            minimum=float(token_quote['minimum_usd_out_estimate'])
                        except (TypeError,ValueError,KeyError,OverflowError):
                            minimum=float('nan')
                        age_seconds=float(token_quote.get('quote_age_seconds',float('inf')))
                        if (math.isfinite(minimum) and minimum>=0 and
                                0<=age_seconds<=7 and
                                token_quote.get('pool')==rec['pool']):
                            rec['held_sell_min_usd_estimate']=minimum
                            rec['held_sell_min_quote_at']=stamp-age_seconds
                            rec['held_sell_quote_return_pct']=round(
                                (minimum/self.size-1)*100,4)
                        else:
                            rec['held_sell_quote_status']='unverified_sell_quote_metadata'
                            token_quote=None
            # On micro, exit thresholds use the verified sale minimum when
            # available. The old Dex spot estimate is informational, not an
            # exit-authorizing profit signal. Missing quotes may set a pending
            # protective stop, never a virtual fill.
            quoted_stop=False
            quote_take_profit=False
            quoted_reversal=False
            prior_quote_peak=float(rec.get('peak_quoted_return_pct',float('-inf')))
            recent_minimum=None
            if self.name=='micro_0_60':
                quote_time=float(rec.get('held_sell_min_quote_at',float('-inf')))
                if 0<=stamp-quote_time<=7:
                    quote_min=rec.get('held_sell_min_usd_estimate')
                    if (isinstance(quote_min,(int,float)) and
                            math.isfinite(quote_min) and quote_min>=0):
                        recent_minimum=float(quote_min)
                        pct=(recent_minimum/self.size-1)*100
                        prev_peak=prior_quote_peak
                        quoted_stop=pct<=-9.
                        quote_take_profit=self.take_profit_enabled and pct>=self.take_profit_net_pct
                        quoted_reversal=(self.exit_on_profitable_reversal and
                            prev_peak>0 and prev_peak>=self.trailing_activation_net_pct and
                            pct<=prev_peak-self.trailing_drawdown_pct_points and pct<prev_peak)
                        rec['peak_quoted_return_pct']=max(prev_peak,pct)
            # A price-only crash without a usable quote still triggers
            # protective exit INTENT, but needs an actual quote before credit.
            missing_quote_stop=(self.name=='micro_0_60' and
                                recent_minimum is None and net_pct<=-9.)
            stop=net_pct<=-9. if self.name!='micro_0_60' else False
            liquidity_loss=e['liquidity'] <= rec['entry_liquidity'] * .60
            micro_profit=quote_take_profit if self.name=='micro_0_60' else (
                self.take_profit_enabled and net_pct>=self.take_profit_net_pct)
            micro_reversal=quoted_reversal if self.name=='micro_0_60' else profitable_reversal
            reason=('age_5m_mandatory_exit' if micro_age_exit else
                    'protective_quoted_net_stop' if quoted_stop else
                    'protective_net_stop' if stop else
                    'protective_unverified_spot_stop' if missing_quote_stop else
                    'liquidity_drop_40pct' if liquidity_loss else
                    f'take_profit_{self.take_profit_net_pct:g}pct' if micro_profit else
                    'profitable_reversal' if micro_reversal else
                    'expiry_45m' if elapsed>=2700 else None)
            # Two separate confirmed snapshots showing a >20% on-chain SOL
            # reserve drawdown prompt a proactive exit intent. USD pool
            # liquidity is not used as a proxy for withdrawn SOL.
            if self.name=='micro_0_60' and self.research_risk_gate is not None:
                guard=getattr(self.research_risk_gate,'micro_rug_guard',None)
                if guard is not None and hasattr(guard,'check'):
                    safe,guard_reason,_=guard.check(pair,stamp)
                    if guard_reason=='verified_sol_reserve_decline_20pct':
                        rec['reserve_drop_checks']=int(rec.get('reserve_drop_checks',0))+1
                    elif safe:
                        rec['reserve_drop_checks']=0
                    if rec.get('reserve_drop_checks',0)>=2:
                        reason=reason or 'onchain_sol_reserve_drop_20pct'
            reason=rec.get('pending_exit_reason') or reason
            if reason:
                quote=None
                if self.name=='micro_0_60':
                    if not self.research_quote_probe or not hasattr(self.research_quote_probe,'probe_micro_sell'):
                        rec['pending_exit_reason']=reason
                        rec['exit_quote_status']='unverified_micro_sell_probe_not_installed'
                        self.changed=True
                        return 'pending_exit:'+reason
                    urgent_probe=getattr(self.research_quote_probe,
                                         'probe_micro_sell_urgent',None)
                    quote=(token_quote if token_quote is not None else
                           urgent_probe(mint,rec,pair,now=stamp)
                           if callable(urgent_probe) else
                           self.research_quote_probe.probe_micro_sell(mint,rec,pair,now=stamp))
                    # Also reject incomplete, stale and wrong-pool estimates.
                    quote_age=quote.get('quote_age_seconds',float('inf'))
                    try:
                        min_quote=float(quote.get('minimum_usd_out_estimate'))
                        quote_age=float(quote_age)
                    except (TypeError,ValueError,OverflowError):
                        min_quote=float('nan');quote_age=float('inf')
                    if (quote.get('status')!='pinned_pool_sell_quote_not_fill' or
                            quote.get('pool')!=rec['pool'] or
                            not math.isfinite(min_quote) or min_quote<0 or
                            not 0<=quote_age<=7):
                        rec['pending_exit_reason']=reason
                        rec['exit_quote_status']=quote.get('status','unverified_sell_quote')
                        rec['last_exit_attempt_at']=stamp
                        rec['unverified_exit_attempts']=int(rec.get('unverified_exit_attempts',0))+1
                        if rec['unverified_exit_attempts'] in (1,5,15,30):
                            v902_log.warning('V906 MICRO EXIT STILL PENDING mint=%s pool=%s '
                                'reason=%s quote_status=%s attempts=%d spot_return_pct=%.2f '
                                'pool_liquidity_usd=%.2f no_fictitious_sale=True',
                                mint,rec['pool'],reason,rec['exit_quote_status'],
                                rec['unverified_exit_attempts'],net_pct,e['liquidity'])
                        self.changed=True
                        return 'pending_exit:'+reason
                    # Min quote out is still only an indicative estimate, not
                    # a fill guarantee. Never use Dex spot as sale proceeds.
                    proceeds=max(0.,min_quote)
                else:
                    proceeds=max(0.,self.size*(1+net_pct/100))
                self.cash+=proceeds
                # For micro, the reported net percentage must correspond to
                # the amount actually credited to *paper* cash, not Dex spot.
                credited_return_pct=(proceeds/self.size-1)*100
                closed_trade={'mint':mint,'symbol':rec.get('symbol',''),
                    'pool':rec['pool'],'opened_at':rec['opened_at'],'closed_at':stamp,
                    'entry_age_minutes':rec['entry_age_minutes'],
                    'entry_market_cap_usd':rec.get('entry_market_cap_usd'),
                    'entry_volume5_usd':rec.get('entry_volume5_usd'),
                    'entry_buys5':rec.get('entry_buys5'),
                    'entry_sells5':rec.get('entry_sells5'),
                    'entry_change5_pct':rec.get('entry_change5_pct'),
                    'entry_5m_partial':rec['entry_5m_partial'],
                    'minutes_held':round(elapsed/60,3),'reason':reason,
                    'invested_usd':self.size,'proceeds_usd':round(proceeds,6),
                    'pnl_usd':round(proceeds-self.size,6),
                    'net_pct':round(credited_return_pct if self.name=='micro_0_60'
                                    else net_pct,4),
                    'observations':rec['observations'],
                    'execution':('jupiter_pinned_pool_min_quote_paper_only_no_fill'
                                 if self.name=='micro_0_60' else
                                 'indicative_pinned_pool_only_not_executable')}
                if quote is not None:
                    closed_trade['execution_probe']=quote
                    closed_trade['price_only_modeled_net_pct']=round(net_pct,4)
                    closed_trade['quote_based_estimated_return_pct']=round(credited_return_pct,4)
                    closed_trade['quote_age_seconds']=round(float(quote.get('quote_age_seconds',float('inf'))),3)
                    closed_trade['accounting_basis']='pinned_pool_minimum_quote_paper_no_fill'
                    closed_trade['paper_accounting_revision']='v898'
                    closed_trade['exit_signal_basis']='quoted_minimum_or_fallback_risk_intent'
                    closed_trade['estimated_network_costs_usd']=None
                    closed_trade['paper_entry_inventory_basis']=((rec.get('entry_route_quote') or
                        (rec.get('research_risk_evidence') or {}).get('entry_route_quote') or
                        {}).get('entry_inventory_basis','legacy_unverified_inventory'))
                if reason=='profitable_reversal':
                    if self.name=='micro_0_60':
                        if math.isfinite(prior_quote_peak):
                            closed_trade['prior_peak_quoted_return_pct']=round(prior_quote_peak,5)
                            closed_trade['peak_to_exit_quote_drop_pct_points']=round(
                                prior_quote_peak-credited_return_pct,5)
                    else:
                        closed_trade['prior_peak_net_pct']=round(prior_peak_net_pct,5)
                        closed_trade['peak_to_exit_drop_pct_points']=round(prior_peak_net_pct-net_pct,5)
                if self.name!='micro_0_60' and self.research_quote_probe is not None:
                    closed_trade['execution_probe']=self.research_quote_probe.probe_sell(
                        mint,rec['entry_price'],price,self.size,rec.get('token_decimals'),
                        pair,now=stamp)
                self.closed.append(closed_trade)
                self.closed=self.closed[-1000:];self.positions.pop(mint,None)
                if self.halts_enabled and net_pct<=-50.:
                    self.halted=True;self.halt_reason='catastrophic_closed_virtual_loss'
                if self.halts_enabled and self.cash+sum(self.size for _ in self.positions) <= self.starting_capital*.85:
                    self.halted=True;self.halt_reason=self.halt_reason or 'daily_virtual_loss_limit'
                return 'closed:'+reason
            return 'observed'
        if self.name=='micro_0_60':
            self._micro_funnel_note(e['pool'],'market_pool_observed',stamp)
        # Prefetch on-chain security AND reserve history as soon as a
        # plausible micro pool is seen. Starting only after mint authority and
        # momentum verification wastes most of the 0-5 minute age window.
        # Ages 7-8 are PREWARM-ONLY; buys remain banned until age eight.
        if (self.name=='micro_0_60' and self.research_risk_gate is not None and
                0<=e['age']<60. and not 5.<=e['age']<7. and
                e['cap']>=self.min_entry_market_cap_usd and
                e['liquidity']>=self.min_entry_liquidity_usd and
                (self.max_entry_market_cap_usd is None or
                 e['cap']<self.max_entry_market_cap_usd)):
            guard=getattr(self.research_risk_gate,'micro_rug_guard',None)
            pool_prefetch_reason=None
            if guard is not None and hasattr(guard,'prefetch'):
                _,pool_prefetch_reason,_=guard.prefetch(pair,stamp)
            # V907: concurrently begin mint verification for *flow-eligible*
            # micro pools rather than waiting until LP and history checks
            # finish. The existing RPC/minute budget and mint security remain
            # fully enforced. No quote or paper buy is authorized here.
            if (hasattr(self.research_risk_gate,'prefetch_micro_mint') and
                    self.eligible_new_mint(mint,max(8.,e['age'])
                        if 7.<=e['age']<8. else e['age']) and
                    pool_prefetch_reason not in (
                        'unverified_lp_withdrawal_control',
                        'concentrated_wallet_supply',
                        'verified_sol_reserve_decline_20pct') and
                    not flow_filter_reasons(pair,e,surge_change=surge_change,
                        surge_volume=surge_volume,
                        min_cap=self.min_entry_market_cap_usd,
                        min_volume5_usd=self.min_entry_volume5_usd,
                        max_change=self.max_entry_change5_pct,
                        min_buys5=self.min_buys5,
                        min_sells5=self.min_sells5,
                        min_buy_sell_ratio=self.min_buy_sell_ratio,
                        min_change5=self.min_change5)):
                self.research_risk_gate.prefetch_micro_mint(mint,stamp)
        if not self.entry_age_allowed(e['age']):
            if self.name=='micro_0_60' and 5.<=e['age']<8.:
                self.entry_filter_rejections['blocked_age_5_to_8m']=self.entry_filter_rejections.get('blocked_age_5_to_8m',0)+1
                self._micro_funnel_note(e['pool'],'age_5_to_8_block',stamp)
                self.changed=True
                return 'blocked_age_5_to_8m'
            return 'outside_window'
        # Apply to NEW entries only; exits remain free to proceed even after a
        # cap spike, price crash, or liquidity loss. pool_evidence requires the
        # real DexScreener marketCap (no FDV-based size substitution).
        if (self.max_entry_market_cap_usd is not None and
                e['cap'] >= self.max_entry_market_cap_usd):
            return 'market_cap_at_or_above_limit' 
        if not self.eligible_new_mint(mint,e['age']):return 'already_seen'
        if self.halts_enabled and self.halted:return 'track_safety_halt'
        if len(self.positions)>=self.max_positions or self.cash<self.size:return 'capacity'
        if self.name=='micro_0_60':
            # Original five-minute volume cutoff is informative ONLY; never
            # an entry rejection for this micro research track.
            raw_volume=pair.get('volume') or {}
            if 'm5' not in raw_volume:
                volume_reason='original_volume5_missing_would_block'
            elif e['v5']<SHADOW_MIN_5M_VOLUME_USD:
                volume_reason='original_volume5_below_1400_would_block'
            elif e['change5']>=surge_change and e['v5']<surge_volume:
                volume_reason='original_thin_volume_surge_would_block'
            else:
                volume_reason=None
            if volume_reason:
                self.legacy_volume_observations[volume_reason]=(self.legacy_volume_observations.get(volume_reason,0)+1)
                self.changed=True
        filter_reasons=flow_filter_reasons(pair,e,surge_change=surge_change,
            surge_volume=surge_volume,min_cap=self.min_entry_market_cap_usd,
            max_change=self.max_entry_change5_pct,
            min_volume5_usd=self.min_entry_volume5_usd,
            min_buys5=self.min_buys5,min_sells5=self.min_sells5,
            min_buy_sell_ratio=self.min_buy_sell_ratio,
            min_change5=self.min_change5)
        if filter_reasons:
            if self.name=='micro_0_60':
                self._micro_funnel_note(e['pool'],'momentum_filter_block',stamp)
                for reason in filter_reasons:
                    self.entry_filter_rejections[reason]=self.entry_filter_rejections.get(reason,0)+1
                self.changed=True
            self.pending_followups.pop(mint,None)
            return 'entry_filters'
        if self.name=='micro_0_60':
            self._micro_funnel_note(e['pool'],'age_and_flow_passed',stamp)
        if self.research_risk_gate is not None:
            if self.name=='micro_0_60' and hasattr(self.research_risk_gate,'allow_micro'):
                safe,reason,details=self.research_risk_gate.allow_micro(
                    pair,e,stamp,min_cap_usd=self.min_entry_market_cap_usd)
            else:
                safe,reason,details=self.research_risk_gate.allow(pair,e,stamp)
            if not safe:
                if self.name=='micro_0_60':
                    pending=('pending' in str(reason) or 'budget' in str(reason) or
                             'unverified_60s_reserve_history' in str(reason))
                    self._micro_funnel_note(e['pool'],
                        'research_waiting' if pending else 'research_security_blocked',
                        stamp,reason=str(reason),mint=mint,age=e['age'])
                return self._research_reject('research_risk:'+reason,mint,stamp)
        else:
            details={}
        if self.name=='micro_0_60':
            self._micro_funnel_note(e['pool'],'research_security_passed',stamp,mint=mint,age=e['age'])
        if self.research_quote_probe is not None:
            quote_probe=(self.research_quote_probe.probe_micro_roundtrip_join
                if self.name=='micro_0_60' and hasattr(self.research_quote_probe,'probe_micro_roundtrip_join')
                else self.research_quote_probe.probe_micro_roundtrip
                if self.name=='micro_0_60' and hasattr(self.research_quote_probe,'probe_micro_roundtrip')
                else self.research_quote_probe.probe_roundtrip)
            route=quote_probe(mint,self.size,details.get('decimals'),pair,now=stamp)
            # Defense in depth: a 'successful' status returned by a corrupted
            # callback/cache cannot substitute a different unverified pool.
            if (self.name=='micro_0_60' and
                    route.get('status')=='two_sided_jupiter_quotes_not_fills' and
                    route.get('pool')!=e['pool']):
                route={'status':'rejected_unpinned_micro_entry_route'}
            if route.get('status')!='two_sided_jupiter_quotes_not_fills':
                if self.name=='micro_0_60':
                    status=str(route.get('status'))
                    waiting=('pending' in status or 'budget' in status)
                    self._micro_funnel_note(e['pool'],
                        'jupiter_quote_waiting' if waiting else 'jupiter_quote_blocked',
                        stamp,reason=status,mint=mint,age=e['age'])
                return self._research_reject('research_route:'+str(route.get('status')),mint,stamp)
            if self.name=='micro_0_60':
                self._micro_funnel_note(e['pool'],'jupiter_roundtrip_passed',stamp,mint=mint,age=e['age'])
                quote_seen=route.get('quote_observed_at')
                quote_age=(round(stamp-quote_seen,3) if isinstance(quote_seen,(int,float))
                           and math.isfinite(quote_seen) and 0<=stamp-quote_seen<=7
                           else 'request_start_or_mock_verified_by_probe')
                v902_log.info('V903 MICRO ENTRY JOIN pool=%s mint=%s age_min=%.3f '
                    'security_passed=True quote_passed=True quote_age_s=%s '
                    'same_pool=%s age_allowed=%s entry=paper_only',
                    e['pool'],mint,e['age'],quote_age,route.get('pool')==e['pool'],
                    self.entry_age_allowed(e['age']))
            details=dict(details,entry_route_quote=route)
        self.pending_followups.pop(mint,None)
        self.retry_after.pop(mint,None)
        self.positions[mint]={'pool':e['pool'],'symbol':str((pair.get('baseToken') or {}).get('symbol',''))[:30],
            'entry_price':e['price'],'entry_age_minutes':round(e['age'],3),
            'entry_5m_partial':e['age']<5.,'opened_at':stamp,
            'entry_market_cap_usd':e['cap'],
            'entry_volume5_usd':e['v5'],
            'entry_buys5':e['buys'],'entry_sells5':e['sells'],
            'entry_change5_pct':e['change5'],
            'entry_liquidity':e['liquidity'],'last_liquidity':e['liquidity'],
            'last_price':e['price'],'observed_at':stamp,'last_net_pct':-6.,
            'peak_net_pct':-6.,'observations':1,
            'research_risk_evidence':details,
            'entry_route_quote':details.get('entry_route_quote'),
            'entry_quote_pool_verified':(details.get('entry_route_quote') or {}).get('pool')==e['pool'],
            'token_decimals':details.get('decimals')}
        if self.name=='micro_0_60' and e['age']>=8. and mint in self.seen:
            self.reentered_after8.add(mint)
        self.cash-=self.size;self.seen.add(mint);self.open_count+=1
        if self.name=='micro_0_60':
            self._micro_funnel_note(e['pool'],'paper_entry_opened',stamp,mint=mint,age=e['age'])
        self.changed=True
        return 'opened'

    def note_missing_quote(self, mint: str, *, now:float | None=None):
        rec=self.positions.get(mint)
        if rec is not None:
            stamp=time.time() if now is None else now
            if (self.name=='micro_0_60' and rec.get('entry_age_minutes',60)<5. and
                    rec['entry_age_minutes']+(stamp-rec['opened_at'])/60.>=5.):
                rec['pending_exit_reason']='age_5m_mandatory_exit'
                rec['exit_quote_status']='unverified_fresh_pool_unavailable'
            rec['missing_since']=rec.get('missing_since') or stamp
            rec['quote_unavailable_checks']=int(rec.get('quote_unavailable_checks',0))+1
            self.missed_quotes+=1;self.changed=True

    def stats(self):
        self.load()
        wins=sum(1 for r in self.closed if r.get('pnl_usd',0)>0)
        realized=sum(float(r.get('pnl_usd',0)) for r in self.closed)
        # Stale/unverified prices are NOT counted as recoverable sale proceeds.
        # Provide cash + stale indicative marks only in an explicitly labeled field.
        marked=sum(max(0.,self.size*(1+float(r.get('last_net_pct',-100))/100))
                   for r in self.positions.values())
        return {'track':self.name,'cash_usd':round(self.cash,4),'closed':len(self.closed),
                'wins':wins,'losses':len(self.closed)-wins,
                'realized_pnl_usd':round(realized,4),'open':len(self.positions),
                'indicative_stale_marked_equity_usd':round(self.cash+marked,4),
                'missing_quotes':self.missed_quotes,'read_only':self.read_only,
                'halted':self.halted,'halt_reason':self.halt_reason,
                'automatic_loss_halts_enabled':self.halts_enabled,
                'historical_halt_reason':self.historical_halt_reason,
                'take_profit_net_pct':self.take_profit_net_pct,
                'entry_min_market_cap_usd':self.min_entry_market_cap_usd,
                'entry_min_liquidity_usd':self.min_entry_liquidity_usd,
                'max_change5_pct':self.max_entry_change5_pct,
                'research_rejections':dict(self.research_rejections),
                'entry_filter_rejections':dict(self.entry_filter_rejections),
                'micro_unique_funnel':self._micro_funnel_stats() if self.name=='micro_0_60' else {},
                'legacy_volume_observations':dict(self.legacy_volume_observations),
                'entry_min_volume5_usd':self.min_entry_volume5_usd,
                'v905_momentum_profile':self.micro_momentum_profile,
                'v905_min_buys5':self.min_buys5,
                'v905_min_sells5':self.min_sells5,
                'v905_min_buy_sell_ratio':self.min_buy_sell_ratio,
                'v905_min_change5_pct':self.min_change5,
                'blocked_age_band':'[5,8) minutes' if self.name=='micro_0_60' else None,
                'micro_reentries_after8':len(self.reentered_after8),
                'pending_micro_exits':sum(bool(v.get('pending_exit_reason')) for v in self.positions.values()) if self.name=='micro_0_60' else 0,
                'v898_quote_accounted_closes':sum(r.get('paper_accounting_revision')=='v898' for r in self.closed) if self.name=='micro_0_60' else 0,
                'v898_quote_stop_closes':sum(r.get('paper_accounting_revision')=='v898' and r.get('reason')=='protective_quoted_net_stop' for r in self.closed) if self.name=='micro_0_60' else 0,
                'v898_pending_exit_statuses':{str(r.get('exit_quote_status','unspecified')):sum(1 for q in self.positions.values() if q.get('pending_exit_reason') and str(q.get('exit_quote_status','unspecified'))==str(r.get('exit_quote_status','unspecified'))) for r in self.positions.values() if r.get('pending_exit_reason')} if self.name=='micro_0_60' else {},
                'exit_on_profitable_reversal':self.exit_on_profitable_reversal}

    def save(self):
        self.load()
        if self.read_only or not self.changed:return False
        payload={'version':self.VERSION,'name':self.name,'cash':self.cash,
                 'positions':self.positions,'closed':self.closed,
                 'seen':sorted(self.seen)[-4000:],'healthy_samples':self.healthy_samples,
                 'open_count':self.open_count,'missed_quotes':self.missed_quotes,
                 'halted':self.halted,'halt_reason':self.halt_reason,
                 'historical_halt_reason':self.historical_halt_reason}
        if self.research_risk_gate is not None:
            payload['research_rejections']=dict(self.research_rejections)
        if self.name=='micro_0_60':
            payload['entry_filter_rejections']=dict(self.entry_filter_rejections)
            payload['legacy_volume_observations']=dict(self.legacy_volume_observations)
            payload['reentered_after8']=sorted(self.reentered_after8)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temp=self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(payload,separators=(',',':')),encoding='utf8')
        temp.replace(self.path);self.changed=False
        return True


class BackgroundAgeStudy:
    """Two isolated tracks and one bounded 0-60m mint queue; no primary state refs."""
    def __init__(self, *, state_file: Path, starting_capital:float=36., max_candidates:int=320,
                 promoted_0_60_no_halts:bool=False,
                 max_entry_market_cap_usd:float | None=None,
                 enable_v893_research: bool=False, research_guard=None, research_quote_probe=None):
        base=Path(state_file)
        # v8.9.6 fresh micro-only experiment. Distinct name means old stuck
        # inventory/closed trades are NOT imported or falsely realized.
        # Re-deploys of the same run retain this new ledger without resetting.
        micro_run_id=os.getenv('MICRO_0_60_RUN_ID','v896_fresh36_run001')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',micro_run_id):
            raise ValueError('invalid_micro_0_60_run_id')
        self.micro_run_id=micro_run_id
        self.tracks=[AgeTrack(name=name,min_age=min_age,
            path=base.with_name(f'age_study_{name}_{base.stem}.json'),
            starting_capital=starting_capital,
            max_entry_market_cap_usd=max_entry_market_cap_usd,
            halts_enabled=not(promoted_0_60_no_halts and name=='0_60'),
            # Only the PROMOTED primary 0_60 switches to the requested exit
            # settings. Independent 5_60 and unpromoted study remain v8.7.6.
            take_profit_net_pct=25. if promoted_0_60_no_halts and name=='0_60' else 8.,
            exit_on_profitable_reversal=bool(promoted_0_60_no_halts and name=='0_60'))
            for name,min_age in (('0_60',0.),('5_60',5.))]
        if enable_v893_research:
            # Two additional, independent paper portfolios; never modify the
            # historical 0_60 and 5_60 ledgers or their eligibility settings.
            for name,cap,liquidity,change_max in (
                    ('adaptive_0_60',35000.,15000.,16.),
                    ('micro_0_60',3000.,1000.,None)):
                self.tracks.append(AgeTrack(name=name,min_age=0.,
                    path=base.with_name(f'age_study_{name}_{base.stem}__{micro_run_id}.json')
                         if name=='micro_0_60' else base.with_name(f'age_study_{name}_{base.stem}.json'),
                    starting_capital=36. if name=='micro_0_60' else starting_capital,halts_enabled=False,
                    take_profit_net_pct=25.,exit_on_profitable_reversal=True,
                    max_entry_market_cap_usd=max_entry_market_cap_usd,
                    min_entry_market_cap_usd=cap,
                    min_entry_liquidity_usd=liquidity,
                    min_entry_volume5_usd=(None if name=='micro_0_60' else SHADOW_MIN_5M_VOLUME_USD),
                    max_entry_change5_pct=change_max,
                    research_risk_gate=research_guard,
                    research_quote_probe=research_quote_probe))
        self.research_guard=research_guard if enable_v893_research else None
        self.hints={};self.last_lookup={};self.micro_last_lookup={}
        self.max_candidates=max_candidates
        self.observation_counts={};self.last_stats_at=0.

    def hint(self,mint:str,born:float,*,now:float|None=None):
        stamp=time.time() if now is None else now
        if not isinstance(mint,str) or not MINT_PATTERN.fullmatch(mint):return
        if not math.isfinite(born) or not 0 <= stamp-born < 3600:return
        self.hints[mint]=born
        if len(self.hints)>self.max_candidates:
            for key,_ in sorted(self.hints.items(), key=lambda row:row[1])[:len(self.hints)-self.max_candidates]:
                del self.hints[key]

    def pair_observed(self,pair:dict,*,sol_mint:str,dexes,now:float|None=None,
                      surge_change:float=13.5,surge_volume:float=2500.,
                      micro_only:bool=False):
        stamp=time.time() if now is None else now
        mint=(pair.get('baseToken') or {}).get('address') if isinstance(pair,dict) else None
        held=any(mint in track.positions for track in self.tracks)
        if self.research_guard is not None and isinstance(pair,dict):
            self.research_guard.observe(pair,stamp)
        e=pool_evidence(pair,sol_mint,dexes,now_ms=stamp*1000,
                        min_liquidity=1000. if len(self.tracks)>2 else 15000.)
        if e is None and not held:return {}
        if e is not None and e['age'] <60:
            self.hint(e['mint'],stamp-e['age']*60,now=stamp)
        results={}
        valid=False
        for track in self.tracks:
            if micro_only and track.name!='micro_0_60':continue
            result=track.observe(pair,sol_mint=sol_mint,dexes=dexes,now=stamp,
                                 surge_change=surge_change,surge_volume=surge_volume)
            if result not in ('unverified_pool','different_pool','read_only'):
                valid=True
            if result=='opened' or result.startswith('closed:'):
                results[track.name]=result
        if valid and mint:
            (self.micro_last_lookup if micro_only else self.last_lookup)[mint]=stamp
        return results

    def due(self,*,now:float|None=None,limit:int=4):
        stamp=time.time() if now is None else now
        active={m for track in self.tracks for m in track.positions}
        # Any held virtual token is watched even if the pool is now >60m old.
        position_due=sorted((m for m in active if stamp-self.last_lookup.get(m,0)>=15),
                            key=lambda m:self.last_lookup.get(m,0))
        eligible=[m for m,b in self.hints.items() if 0 <= stamp-b <3600 and
                  m not in active and stamp-self.last_lookup.get(m,0)>=
                  (12 if self.research_guard is not None else 45) and
                  any((not track.halts_enabled or not track.halted) and
                      track.eligible_new_mint(m,(stamp-b)/60.) and len(track.positions)<track.max_positions
                      and track.cash>=track.size and stamp-b >= track.min_age*60
                      for track in self.tracks)]
        # Reserve opportunity for both very young and late-age pools. Reusing
        # the primary 12m queue here would silently defeat the 60m experiment.
        early=sorted((m for m in eligible if stamp-self.hints[m]<300),
                     key=lambda m:self.last_lookup.get(m,0))
        middle=sorted((m for m in eligible if 300<=stamp-self.hints[m]<720),
                      key=lambda m:self.last_lookup.get(m,0))
        late=sorted((m for m in eligible if 720<=stamp-self.hints[m]<3600),
                    key=lambda m:self.last_lookup.get(m,0))
        selected=position_due[:max(0,limit)]
        lanes=[early,middle,late]
        for lane in lanes:
            if len(selected)>=limit:break
            if lane and lane[0] not in selected:selected.append(lane.pop(0))
        while len(selected)<limit and any(lanes):
            for lane in lanes:
                if len(selected)>=limit:break
                if lane:
                    m=lane.pop(0)
                    if m not in selected:selected.append(m)
        return selected

    def micro_ready_followups(self, *, now:float|None=None, limit:int=2, exclude=()):
        """Ready async RPC/quote callbacks get a fresh-pool retry, not a stale fill."""
        stamp=time.time() if now is None else now
        micro=next((t for t in self.tracks if t.name=='micro_0_60'),None)
        if micro is None:return []
        micro.load()
        if micro.read_only or len(micro.positions)>=micro.max_positions or micro.cash<micro.size:
            return []
        ready=[]; excluded=set(exclude)
        other_held={m for t in self.tracks if t is not micro for m in t.positions}
        risk_pending=getattr(self.research_guard,'_pending',{}) or {}
        quote_pending=getattr(micro.research_quote_probe,'_pending',{}) or {}
        rug_guard=getattr(self.research_guard,'micro_rug_guard',None)
        quote_observer=micro.research_quote_probe
        quote_ready=(quote_observer.ready_micro_mints(now=stamp)
                     if quote_observer is not None and
                     hasattr(quote_observer,'ready_micro_mints') else set())
        pool_ready=(rug_guard.recently_completed_mints(stamp)
                    if rug_guard is not None and
                    hasattr(rug_guard,'recently_completed_mints') else set())
        full_pool_ready=(rug_guard.recently_fully_verified_mints(stamp)
                    if rug_guard is not None and
                    hasattr(rug_guard,'recently_fully_verified_mints') else set())
        mint_ready=(self.research_guard.recently_completed_mints(stamp)
                    if self.research_guard is not None and
                    hasattr(self.research_guard,'recently_completed_mints') else set())
        # Do not rely solely on pending_followups: a temporary security
        # rejection may have removed it while the quote completed. Inspect
        # both callback systems independently and deduplicate by mint.
        candidates=set(micro.pending_followups)|quote_ready|pool_ready|full_pool_ready|mint_ready
        for mint in candidates:
            started=micro.pending_followups.get(mint,stamp)
            if (rug_guard is not None and
                hasattr(rug_guard,'negative_pool_deferral_until') and
                rug_guard.negative_pool_deferral_until(mint,stamp)):
                continue
            born=self.hints.get(mint)
            if (mint in excluded or mint in other_held or born is None or
                    not 0<=stamp-born<3600 or
                    not micro.eligible_new_mint(mint,(stamp-born)/60.)):
                micro.pending_followups.pop(mint,None)
                continue
            rpc=risk_pending.get(mint)
            # v8.9.8 keys quotes by (mint, selected_pool), whereas the old
            # callback scheduler looked up mint alone, preventing rapid reuse
            # of finished quotes until they had already expired. A completed
            # future for this mint merely SCHEDULES a retry: the entry quote
            # is still revalidated against the exact selected pool in observe.
            ready_quote=(mint in quote_ready or any(
                (key==mint or (isinstance(key,tuple) and len(key)>=1 and key[0]==mint))
                and pending[1].done() and 0<=stamp-pending[0]<=7
                for key,pending in quote_pending.items()))
            # Only completed futures are urgent; avoid hammering incomplete ones.
            completed=(rpc is not None and rpc.done()) or ready_quote or (
                mint in pool_ready) or (mint in full_pool_ready) or (mint in mint_ready) or (rug_guard is not None and rug_guard.completed(mint))
            due_retry=stamp>=micro.retry_after.get(mint,float('inf'))
            last=max(self.last_lookup.get(mint,0),self.micro_last_lookup.get(mint,0))
            if (completed or due_retry) and stamp-last>=3:
                # Finished two-sided quotes take priority over bare pool
                # callbacks, but this only schedules a fresh candidate scan.
                priority=(0 if ready_quote else 1 if mint in full_pool_ready else 2 if mint in mint_ready else 3 if mint in pool_ready else 4)
                ready.append((priority,started,mint))
                if priority<4:
                    v902_log.info('V903 MICRO CALLBACK WAKE mint=%s quote_fresh=%s '
                        'pool_callback_recent=%s next_action=fresh_pair_and_all_gates '
                        'paper_only=True',mint,bool(ready_quote),mint in pool_ready)
        for mint,retry_at in tuple(micro.retry_after.items()):
            if (rug_guard is not None and
                hasattr(rug_guard,'negative_pool_deferral_until') and
                rug_guard.negative_pool_deferral_until(mint,stamp)):
                continue
            born=self.hints.get(mint)
            if born is None or not 0<=stamp-born<3600 or not micro.eligible_new_mint(mint,(stamp-born)/60.):
                micro.retry_after.pop(mint,None)
                continue
            last=max(self.last_lookup.get(mint,0),self.micro_last_lookup.get(mint,0))
            if (stamp>=retry_at and stamp-last>=3 and
                mint not in excluded and mint not in other_held):
                ready.append((2,retry_at,mint))
        return list(dict.fromkeys(mint for _,_,mint in sorted(ready)))[:limit]

    def micro_extra_due(self, *, now:float|None=None, limit:int=3, exclude=()):
        """Extra micro-only queue, activated by scanner backlog in bot.py."""
        stamp=time.time() if now is None else now
        micro=next((t for t in self.tracks if t.name=='micro_0_60'),None)
        if micro is None:return []
        micro.load()
        if micro.read_only or len(micro.positions)>=micro.max_positions or micro.cash<micro.size:
            return []
        excluded=set(exclude)
        other_held={m for t in self.tracks if t is not micro for m in t.positions}
        ready=self.micro_ready_followups(now=stamp,limit=limit,exclude=excluded)
        excluded.update(ready)
        fresh=[]; older=[]
        guard=getattr(self.research_guard,'micro_rug_guard',None)
        for mint,born in self.hints.items():
            if (guard is not None and hasattr(guard,'negative_pool_deferral_until') and
                guard.negative_pool_deferral_until(mint,stamp)):
                continue
            last=max(self.last_lookup.get(mint,0),self.micro_last_lookup.get(mint,0))
            if (mint in excluded or mint in other_held or
                    mint in micro.positions or not 0<=stamp-born<3600 or
                    not micro.eligible_new_mint(mint,(stamp-born)/60.) or stamp-last<12):
                continue
            item=(last,stamp-born,mint)
            (fresh if last==0 else older).append(item)
        # Prefer already LP/holder-verified candidates; they still undergo
        # FRESH reserve-history, age, momentum, token authority and Jupiter
        # checks in observe(). Cached LP evidence never constitutes approval.
        verified=[]
        if guard is not None and hasattr(guard,'prioritized_verified_mints'):
            full_ready=(guard.recently_fully_verified_mints(stamp)
                if hasattr(guard,'recently_fully_verified_mints') else set())
            for mint in sorted(guard.prioritized_verified_mints(stamp),
                               key=lambda m: (m not in full_ready)):

                born=self.hints.get(mint)
                last=max(self.last_lookup.get(mint,0),self.micro_last_lookup.get(mint,0))
                if (mint not in excluded and mint not in other_held and
                        mint not in micro.positions and born is not None and
                        0<=stamp-born<3600 and
                        micro.eligible_new_mint(mint,(stamp-born)/60.) and
                        stamp-last>=3):
                    verified.append(mint)
                    excluded.add(mint)
                    if len(verified)+len(ready)>=limit:break
        # V905 fair lookups: ready callbacks and LP-verified candidates stay
        # first, but when there are at least two available lookup slots,
        # reserve ONE slot for a never-before-seen mint. Without this, the
        # older/recheck queues can monopolize the small per-scan budget and
        # new tokens miss the [0,5) research window entirely. Each selected
        # mint still goes through every fresh market, security and quote gate.
        fresh.sort()
        # Fresh completed quotes/callbacks are genuinely time critical and
        # must NEVER be dropped to make space for brand-new discovery.
        candidates=ready[:limit]
        reserve_fresh=(limit>=2 and bool(fresh) and len(candidates)<limit)
        verified_budget=max(0,limit-len(candidates)-int(reserve_fresh))
        candidates.extend(verified[:verified_budget])
        if reserve_fresh:
            new_mint=fresh.pop(0)[2]
            if new_mint not in candidates:
                candidates.append(new_mint)
        # Reoffer unselected verified candidates in a later cycle (they are
        # not permanently removed from the external guard's verified set).
        for _,_,mint in sorted(older)+fresh:
            if len(candidates)>=limit:break
            if mint not in excluded and mint not in candidates:candidates.append(mint)
        return candidates[:limit]

    def micro_urgent_held(self, *, now:float|None=None, limit:int=2):
        stamp=time.time() if now is None else now
        micro=next((t for t in self.tracks if t.name=='micro_0_60'),None)
        if micro is None:return []
        micro.load()
        pending=[]
        for mint,rec in micro.positions.items():
            age=float(rec.get('entry_age_minutes',60))+(stamp-float(rec['opened_at']))/60.
            pending_exit=bool(rec.get('pending_exit_reason'))
            age_exit=(float(rec.get('entry_age_minutes',60))<5. and age>=4.25)
            last=max(self.last_lookup.get(mint,0),self.micro_last_lookup.get(mint,0))
            # v906: all held micro positions get a dedicated observation about
            # every 8 seconds, not only after a stop has ALREADY been noticed.
            # Otherwise a fast rug can occur between 15+ second checks. The
            # fast scheduler remains bounded and gives pending exits priority.
            if stamp-last>=(3 if pending_exit or age_exit else 8):
                priority=0 if pending_exit else 1 if age_exit else 2
                pending.append((priority,(5.-age if age<5. else 0),last,mint))
        return [mint for *_,mint in sorted(pending)[:limit]]

    def missing(self,mint:str,*,now:float|None=None,micro_only:bool=False):
        stamp=time.time() if now is None else now
        (self.micro_last_lookup if micro_only else self.last_lookup)[mint]=stamp
        for track in self.tracks:
            if not micro_only or track.name=='micro_0_60':
                track.note_missing_quote(mint,now=stamp)

    def save(self):
        for track in self.tracks:track.save()

    def status(self):
        return [t.stats() for t in self.tracks]

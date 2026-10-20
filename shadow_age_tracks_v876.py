"""Two independent, *indicative* background paper portfolios for age-window research.

The original portfolios trade separately from the legacy 5-12-minute paper
ledger; v8.7.7 can promote the original 0_60 portfolio without changing its
entry/exit thresholds, while disabling its automatic trading halts if requested.
Quotes come from fresh, pinned DexScreener pools and are NOT verified
executable prices. No quote => no invented exit.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from early_watch_shadow_v80 import (pool_evidence, SHADOW_MIN_CAP_USD,
    SHADOW_MIN_5M_VOLUME_USD, SHADOW_MIN_5M_BUYS, SHADOW_MIN_5M_SELLS,
    SHADOW_MIN_BUY_SELL_RATIO, SHADOW_MIN_5M_CHANGE_PCT,
    SHADOW_MAX_5M_CHANGE_PCT, SHADOW_ESTIMATED_ROUND_TRIP_COST_PCT)
from rolling_discovery import MINT_PATTERN


def valid_flow(pair: dict, e: dict, *, surge_change: float=13.5, surge_volume: float=2500.,
               min_cap: float=SHADOW_MIN_CAP_USD,
               max_change: float | None=SHADOW_MAX_5M_CHANGE_PCT) -> bool:
    """Use the original measured flow thresholds, plus v8.7.5 health gate.

    For age <5 minutes the vendor's 'm5' window is PARTIAL history; we label
    these entries as such and never pretend a complete 5-minute sample exists.
    """
    vol = pair.get('volume') or {}
    tx = (pair.get('txns') or {}).get('m5') or {}
    change = pair.get('priceChange') or {}
    if not all((isinstance(d,dict) for d in (vol,tx,change))): return False
    if ('m5' not in vol or 'buys' not in tx or 'sells' not in tx or 'm5' not in change): return False
    if not math.isfinite(e['change5']): return False
    if (e['cap'] < min_cap or e['v5'] < SHADOW_MIN_5M_VOLUME_USD or
        e['buys'] < SHADOW_MIN_5M_BUYS or e['sells'] < SHADOW_MIN_5M_SELLS or
        e['buys']/e['sells'] < SHADOW_MIN_BUY_SELL_RATIO or
        (e['change5'] < SHADOW_MIN_5M_CHANGE_PCT or (max_change is not None and e['change5'] > max_change))): return False
    if e['change5'] >= surge_change and e['v5'] < surge_volume: return False
    return True


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
        self.max_entry_change5_pct=max_entry_change5_pct
        if not (math.isfinite(self.min_entry_market_cap_usd) and self.min_entry_market_cap_usd>0 and
                math.isfinite(self.min_entry_liquidity_usd) and self.min_entry_liquidity_usd>0):
            raise ValueError('invalid_research_threshold')
        self.research_risk_gate=research_risk_gate
        self.research_quote_probe=research_quote_probe
        self.research_rejections={}

        if max_entry_market_cap_usd is not None:
            max_entry_market_cap_usd=float(max_entry_market_cap_usd)
            if not math.isfinite(max_entry_market_cap_usd) or max_entry_market_cap_usd<=0:
                raise ValueError('invalid_max_entry_market_cap_usd')
        self.max_entry_market_cap_usd=max_entry_market_cap_usd
        self.historical_halt_reason=None
        self.cash=self.starting_capital; self.positions={};self.closed=[]
        self.seen=set();self.healthy_samples=0;self.open_count=0
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
        self.healthy_samples=int(data.get('healthy_samples',0))
        self.open_count=int(data.get('open_count',len(self.closed)+len(self.positions)))
        self.missed_quotes=int(data.get('missed_quotes',0))
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

    def _research_reject(self,reason):
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
            stop=net_pct<=-9.
            liquidity_loss=e['liquidity'] <= rec['entry_liquidity'] * .60
            reason=('protective_net_stop' if stop else
                    'liquidity_drop_40pct' if liquidity_loss else
                    f'take_profit_{self.take_profit_net_pct:g}pct' if self.take_profit_enabled and net_pct>=self.take_profit_net_pct else
                    'profitable_reversal' if profitable_reversal else
                    'expiry_45m' if elapsed>=2700 else None)
            if reason:
                proceeds=max(0.,self.size*(1+net_pct/100))
                self.cash+=proceeds
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
                    'pnl_usd':round(proceeds-self.size,6),'net_pct':round(net_pct,4),
                    'observations':rec['observations'],
                    'execution':'indicative_pinned_pool_only_not_executable'}
                if reason=='profitable_reversal':
                    closed_trade['prior_peak_net_pct']=round(prior_peak_net_pct,5)
                    closed_trade['peak_to_exit_drop_pct_points']=round(prior_peak_net_pct-net_pct,5)
                if self.research_quote_probe is not None:
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
        if not self.min_age <= e['age'] < self.max_age:return 'outside_window'
        # Apply to NEW entries only; exits remain free to proceed even after a
        # cap spike, price crash, or liquidity loss. pool_evidence requires the
        # real DexScreener marketCap (no FDV-based size substitution).
        if (self.max_entry_market_cap_usd is not None and
                e['cap'] >= self.max_entry_market_cap_usd):
            return 'market_cap_at_or_above_limit' 
        if mint in self.seen:return 'already_seen'
        if self.halts_enabled and self.halted:return 'track_safety_halt'
        if len(self.positions)>=self.max_positions or self.cash<self.size:return 'capacity'
        if not valid_flow(pair,e,surge_change=surge_change,surge_volume=surge_volume,
                          min_cap=self.min_entry_market_cap_usd,
                          max_change=self.max_entry_change5_pct):return 'entry_filters'
        if self.research_risk_gate is not None:
            safe,reason,details=self.research_risk_gate.allow(pair,e,stamp)
            if not safe: return self._research_reject('research_risk:'+reason)
        else:
            details={}
        if self.research_quote_probe is not None:
            route=self.research_quote_probe.probe_roundtrip(
                mint,self.size,details.get('decimals'),pair,now=stamp)
            if route.get('status')!='two_sided_jupiter_quotes_not_fills':
                return self._research_reject('research_route:'+str(route.get('status')))
            details=dict(details,entry_route_quote=route)
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
            'token_decimals':details.get('decimals')}
        self.cash-=self.size;self.seen.add(mint);self.open_count+=1
        self.changed=True
        return 'opened'

    def note_missing_quote(self, mint: str, *, now:float | None=None):
        rec=self.positions.get(mint)
        if rec is not None:
            stamp=time.time() if now is None else now
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
                    ('micro_0_60',5000.,1000.,None)):
                self.tracks.append(AgeTrack(name=name,min_age=0.,
                    path=base.with_name(f'age_study_{name}_{base.stem}.json'),
                    starting_capital=starting_capital,halts_enabled=True,
                    take_profit_net_pct=25.,exit_on_profitable_reversal=True,
                    max_entry_market_cap_usd=max_entry_market_cap_usd,
                    min_entry_market_cap_usd=cap,
                    min_entry_liquidity_usd=liquidity,
                    max_entry_change5_pct=change_max,
                    research_risk_gate=research_guard,
                    research_quote_probe=research_quote_probe))
        self.research_guard=research_guard if enable_v893_research else None
        self.hints={};self.last_lookup={};self.max_candidates=max_candidates
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
                      surge_change:float=13.5,surge_volume:float=2500.):
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
            result=track.observe(pair,sol_mint=sol_mint,dexes=dexes,now=stamp,
                                 surge_change=surge_change,surge_volume=surge_volume)
            if result not in ('unverified_pool','different_pool','read_only'):
                valid=True
            if result=='opened' or result.startswith('closed:'):
                results[track.name]=result
        if valid and mint:
            self.last_lookup[mint]=stamp
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
                  any((not track.halts_enabled or not track.halted) and m not in track.seen and len(track.positions)<track.max_positions
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

    def missing(self,mint:str,*,now:float|None=None):
        stamp=time.time() if now is None else now
        self.last_lookup[mint]=stamp
        for track in self.tracks:track.note_missing_quote(mint,now=stamp)

    def save(self):
        for track in self.tracks:track.save()

    def status(self):
        return [t.stats() for t in self.tracks]

"""Two independent, *indicative* background paper portfolios for age-window research.

Neither portfolio has authority to enter the primary trader, place orders, or
change risk halts. Quotes are from fresh, pinned DexScreener pools; outcomes
are NOT verified executable prices. No quote => no invented exit.
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


def valid_flow(pair: dict, e: dict, *, surge_change: float=13.5, surge_volume: float=2500.) -> bool:
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
    if (e['cap'] < SHADOW_MIN_CAP_USD or e['v5'] < SHADOW_MIN_5M_VOLUME_USD or
        e['buys'] < SHADOW_MIN_5M_BUYS or e['sells'] < SHADOW_MIN_5M_SELLS or
        e['buys']/e['sells'] < SHADOW_MIN_BUY_SELL_RATIO or
        not SHADOW_MIN_5M_CHANGE_PCT <= e['change5'] <= SHADOW_MAX_5M_CHANGE_PCT): return False
    if e['change5'] >= surge_change and e['v5'] < surge_volume: return False
    return True


class AgeTrack:
    VERSION=1
    def __init__(self, *, name: str, min_age: float, path: Path,
                 max_age: float=60., starting_capital: float=36.,
                 size: float=2., max_positions: int=5):
        if not 0 <= min_age < max_age <= 60: raise ValueError('bad_age_window')
        self.name=name; self.min_age=min_age; self.max_age=max_age
        self.path=Path(path); self.starting_capital=float(starting_capital)
        self.size=float(size); self.max_positions=max_positions
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
        self.halted=bool(data.get('halted',False))
        self.halt_reason=data.get('halt_reason')

    def observe(self, pair: dict, *, sol_mint: str, dexes, now: float | None=None,
                surge_change: float=13.5, surge_volume: float=2500.) -> str:
        self.load()
        if self.read_only:return 'read_only'
        stamp=time.time() if now is None else now
        mint=(pair.get('baseToken') or {}).get('address') if isinstance(pair,dict) else None
        rec=self.positions.get(mint)
        e=pool_evidence(pair,sol_mint,dexes,now_ms=stamp*1000)
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
            rec.pop('missing_since',None)
            rec['observed_at']=stamp;rec['last_price']=price
            rec['last_net_pct']=round(net_pct,5)
            rec['peak_net_pct']=max(float(rec.get('peak_net_pct',-100)),net_pct)
            rec['observations']=int(rec.get('observations',0))+1
            rec['last_liquidity']=e['liquidity'];self.changed=True
            if net_pct<=-50. and not self.halted:
                self.halted=True;self.halt_reason='catastrophic_observed_virtual_loss'
            stop=net_pct<=-9.
            liquidity_loss=e['liquidity'] <= rec['entry_liquidity'] * .60
            reason=('protective_net_stop' if stop else
                    'liquidity_drop_40pct' if liquidity_loss else
                    'take_profit_8pct' if net_pct>=8 else
                    'expiry_45m' if elapsed>=2700 else None)
            if reason:
                proceeds=max(0.,self.size*(1+net_pct/100))
                self.cash+=proceeds
                self.closed.append({'mint':mint,'symbol':rec.get('symbol',''),
                    'pool':rec['pool'],'opened_at':rec['opened_at'],'closed_at':stamp,
                    'entry_age_minutes':rec['entry_age_minutes'],
                    'entry_5m_partial':rec['entry_5m_partial'],
                    'minutes_held':round(elapsed/60,3),'reason':reason,
                    'invested_usd':self.size,'proceeds_usd':round(proceeds,6),
                    'pnl_usd':round(proceeds-self.size,6),'net_pct':round(net_pct,4),
                    'observations':rec['observations'],
                    'execution':'indicative_pinned_pool_only_not_executable'})
                self.closed=self.closed[-1000:];self.positions.pop(mint,None)
                if net_pct<=-50.:
                    self.halted=True;self.halt_reason='catastrophic_closed_virtual_loss'
                if self.cash+sum(self.size for _ in self.positions) <= self.starting_capital*.85:
                    self.halted=True;self.halt_reason=self.halt_reason or 'daily_virtual_loss_limit'
                return 'closed:'+reason
            return 'observed'
        if not self.min_age <= e['age'] < self.max_age:return 'outside_window'
        if mint in self.seen:return 'already_seen'
        if self.halted:return 'track_safety_halt'
        if len(self.positions)>=self.max_positions or self.cash<self.size:return 'capacity'
        if not valid_flow(pair,e,surge_change=surge_change,surge_volume=surge_volume):return 'entry_filters'
        self.positions[mint]={'pool':e['pool'],'symbol':str((pair.get('baseToken') or {}).get('symbol',''))[:30],
            'entry_price':e['price'],'entry_age_minutes':round(e['age'],3),
            'entry_5m_partial':e['age']<5.,'opened_at':stamp,
            'entry_liquidity':e['liquidity'],'last_liquidity':e['liquidity'],
            'last_price':e['price'],'observed_at':stamp,'last_net_pct':-6.,
            'peak_net_pct':-6.,'observations':1}
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
                'halted':self.halted,'halt_reason':self.halt_reason}

    def save(self):
        self.load()
        if self.read_only or not self.changed:return False
        payload={'version':self.VERSION,'name':self.name,'cash':self.cash,
                 'positions':self.positions,'closed':self.closed,
                 'seen':sorted(self.seen)[-4000:],'healthy_samples':self.healthy_samples,
                 'open_count':self.open_count,'missed_quotes':self.missed_quotes,
                 'halted':self.halted,'halt_reason':self.halt_reason}
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temp=self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(payload,separators=(',',':')),encoding='utf8')
        temp.replace(self.path);self.changed=False
        return True


class BackgroundAgeStudy:
    """Two isolated tracks and one bounded 0-60m mint queue; no primary state refs."""
    def __init__(self, *, state_file: Path, starting_capital:float=36., max_candidates:int=320):
        base=Path(state_file)
        self.tracks=[AgeTrack(name=name,min_age=min_age,
            path=base.with_name(f'age_study_{name}_{base.stem}.json'),
            starting_capital=starting_capital)
            for name,min_age in (('0_60',0.),('5_60',5.))]
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
        e=pool_evidence(pair,sol_mint,dexes,now_ms=stamp*1000)
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
                  m not in active and stamp-self.last_lookup.get(m,0)>=45 and
                  any(not track.halted and m not in track.seen and len(track.positions)<track.max_positions
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

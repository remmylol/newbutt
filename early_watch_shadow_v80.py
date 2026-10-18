"""v8.0 early pool watch and indicative shadow-only counterfactuals.

Watch decisions do not authorize entries. Shadow results are sampled price-path
observations, not executable fills; missing quotes do not invent P&L.
"""
from __future__ import annotations
import json, math, time
from pathlib import Path
from rolling_discovery import MINT_PATTERN

def num(value, default=0.0):
    try: val=float(value)
    except (TypeError,ValueError,OverflowError): return default
    return val if math.isfinite(val) else default

def pool_evidence(pair, sol_mint, dexes, now_ms=None):
    if not isinstance(pair,dict) or pair.get('chainId')!='solana': return None
    mint=(pair.get('baseToken') or {}).get('address')
    quote=(pair.get('quoteToken') or {}).get('address')
    if not mint or not MINT_PATTERN.fullmatch(mint) or mint==sol_mint or quote!=sol_mint: return None
    if (pair.get('dexId') or '').lower() not in {str(d).lower() for d in dexes}: return None
    pool=pair.get('pairAddress')
    if not isinstance(pool,str) or not (32<=len(pool)<=64): return None
    price=num(pair.get('priceUsd')); liq=num((pair.get('liquidity') or {}).get('usd'))
    cap=num(pair.get('marketCap'),-1); created=num(pair.get('pairCreatedAt'),-1)
    if created<=0 or price<=0 or liq<15000 or cap<=0: return None
    age=(num(time.time()*1000 if now_ms is None else now_ms)-created)/60000
    if not 0<=age<525600: return None
    volume=pair.get('volume') or {}; buys=num(((pair.get('txns') or {}).get('m5') or {}).get('buys'))
    sells=num(((pair.get('txns') or {}).get('m5') or {}).get('sells'))
    change=num((pair.get('priceChange') or {}).get('m5'),float('nan'))
    return {'mint':mint,'pool':pool,'price':price,'liquidity':liq,'cap':cap,'age':age,
            'v5':num(volume.get('m5')),'buys':buys,'sells':sells,'change5':change}

def threshold_watch(pair,reason,*,sol_mint,dexes,min_cap=35000,min_age=12,max_cap=250000000,now_ms=None):
    """Eligibility watch on bounded, independently verified same-pool activity."""
    if reason not in ('too_new_for_established_strategy','market_cap_below_range'): return 'none',0,'not_threshold_reason'
    e=pool_evidence(pair,sol_mint,dexes,now_ms)
    if not e: return 'none',0,'primary_sol_pool_unverified'
    if e['cap']>max_cap or e['cap']<25000 or e['liquidity']<15000:
        return 'none',0,'cap_or_liquidity'
    if e['v5']<900 or e['buys']<8 or e['sells']<1 or e['buys']/e['sells']<1.18:
        return 'none',0,'weak_verified_activity'
    if not math.isfinite(e['change5']) or not .5<=e['change5']<=16:
        return 'none',0,'no_positive_five_minute_trend'
    if reason=='too_new_for_established_strategy' and 5<=e['age']<min_age and e['cap']>=min_cap:
        return 'age',max(75,int((min_age-e['age'])*60)+10),'approaching_minimum_age'
    if reason=='market_cap_below_range' and e['age']>=min_age and .72*min_cap<=e['cap']<min_cap:
        return 'cap',120,'approaching_minimum_cap'
    return 'none',0,'not_near_threshold'

# Shared entry and exit definitions: the observational study and the active
# paper portfolio intentionally use precisely the same shadow signal.
SHADOW_MIN_AGE_MINUTES = 5.0
SHADOW_MAX_AGE_MINUTES = 12.0
SHADOW_MIN_CAP_USD = 35000.0
SHADOW_MIN_LIQUIDITY_USD = 15000.0
SHADOW_MIN_5M_VOLUME_USD = 1400.0
SHADOW_MIN_5M_BUYS = 10
SHADOW_MIN_5M_SELLS = 1
SHADOW_MIN_BUY_SELL_RATIO = 1.25
SHADOW_MIN_5M_CHANGE_PCT = 0.8
SHADOW_MAX_5M_CHANGE_PCT = 16.0
SHADOW_ESTIMATED_ROUND_TRIP_COST_PCT = 6.0
SHADOW_TP_NET_PCT = 8.0
SHADOW_SL_NET_PCT = -12.0
SHADOW_EXPIRY_SECONDS = 45 * 60
SHADOW_SAMPLE_INTERVAL_SECONDS = 180


def shadow_entry_evidence(pair, *, sol_mint, dexes, now_ms=None):
    """Exactly the same market and flow acceptance criteria as EarlyShadow."""
    e = pool_evidence(pair, sol_mint, dexes, now_ms=now_ms)
    if not e or not (SHADOW_MIN_AGE_MINUTES <= e['age'] < SHADOW_MAX_AGE_MINUTES
                     and e['cap'] >= SHADOW_MIN_CAP_USD):
        return None
    if (e['v5'] < SHADOW_MIN_5M_VOLUME_USD or e['buys'] < SHADOW_MIN_5M_BUYS
            or e['sells'] < SHADOW_MIN_5M_SELLS
            or e['buys'] / e['sells'] < SHADOW_MIN_BUY_SELL_RATIO
            or not math.isfinite(e['change5'])
            or not SHADOW_MIN_5M_CHANGE_PCT <= e['change5'] <= SHADOW_MAX_5M_CHANGE_PCT):
        return None
    return e



def shadow_entry_diagnostic(pair, *, sol_mint, dexes, now_ms=None):
    """Reasons for ACTIVE shadow entry, never interpret missing M5 as zero flow.

    Watchability means only that the exact primary SOL pool is eligible for a
    bounded DATA recheck before age 12; it is not trade authorization.
    """
    e=pool_evidence(pair, sol_mint, dexes, now_ms=now_ms)
    if e is None:
        return 'primary_sol_pool_unverified',False,{}
    age=e['age']
    watchable=0 <= age < SHADOW_MAX_AGE_MINUTES and e['liquidity']>=SHADOW_MIN_LIQUIDITY_USD
    if age>=SHADOW_MAX_AGE_MINUTES: return 'shadow_age_expired',False,e
    if age<SHADOW_MIN_AGE_MINUTES: return 'shadow_wait_age_5m',watchable,e
    if e['cap']<SHADOW_MIN_CAP_USD: return 'shadow_cap_below_35k',watchable,e
    m5=pair.get('volume') or {}
    tx=(pair.get('txns') or {}).get('m5') or {}
    change=(pair.get('priceChange') or {})
    if ('m5' not in m5 or 'buys' not in tx or 'sells' not in tx or
            'm5' not in change or not math.isfinite(e['change5'])):
        return 'shadow_m5_data_missing',watchable,e
    if e['v5']<SHADOW_MIN_5M_VOLUME_USD:return 'shadow_volume5_low',watchable,e
    if e['buys']<SHADOW_MIN_5M_BUYS:return 'shadow_buys5_low',watchable,e
    if e['sells']<SHADOW_MIN_5M_SELLS:return 'shadow_sells5_low',watchable,e
    if e['buys']/e['sells']<SHADOW_MIN_BUY_SELL_RATIO:return 'shadow_buy_sell_ratio_low',watchable,e
    if e['change5']<SHADOW_MIN_5M_CHANGE_PCT:return 'shadow_price_change_low',watchable,e
    if e['change5']>SHADOW_MAX_5M_CHANGE_PCT:return 'shadow_price_change_high',watchable,e
    return 'eligible_shadow_signal',False,e


def shadow_exit_signal(entry_market_price, current_market_price, elapsed_seconds):
    """Return the exact sampled price-path decision from the original shadow.

    The portfolio's indicative fill calculation additionally models pool
    impact; neither this signal nor its sample-return is a real swap quote.
    """
    entry, current = num(entry_market_price), num(current_market_price)
    if entry <= 0 or current <= 0:
        return None, None
    net_pct = (current / entry - 1) * 100 - SHADOW_ESTIMATED_ROUND_TRIP_COST_PCT
    reason = ('shadow_tp_8' if net_pct >= SHADOW_TP_NET_PCT else
              'shadow_loss_12' if net_pct <= SHADOW_SL_NET_PCT else
              'shadow_45m_expiry' if elapsed_seconds >= SHADOW_EXPIRY_SECONDS else None)
    return reason, net_pct


class EarlyShadow:
    """Observe simulated 5-12 minute entries; no actual paper portfolio fills."""
    def __init__(self,path,*,max_active=8):
        self.path=Path(path);self.max_active=max_active;self.active={};self.completed=[]
        self.loaded=False;self.read_only=False
    def load(self,now=None):
        if self.loaded:return
        self.loaded=True;stamp=time.time() if now is None else now
        try: obj=json.loads(self.path.read_text())
        except FileNotFoundError:return
        except (OSError,ValueError,TypeError):self.read_only=True;return
        if not isinstance(obj,dict) or obj.get('version')!=1:self.read_only=True;return
        if not isinstance(obj.get('active'),dict) or not isinstance(obj.get('completed'),list):self.read_only=True;return
        for mint,r in obj['active'].items():
            if (MINT_PATTERN.fullmatch(mint) and isinstance(r,dict) and num(r.get('entry_price'))>0
                    and 0<=stamp-num(r.get('entered_at'))<3600 and isinstance(r.get('pool'),str)):
                self.active[mint]=r
        self.completed=[r for r in obj['completed'][-150:] if isinstance(r,dict)]
    def due(self,now=None,limit=4):
        stamp=time.time() if now is None else now;self.load(stamp)
        return sorted((m for m,r in self.active.items() if stamp>=num(r.get('due'))),
                      key=lambda m:num(self.active[m].get('due')))[:max(0,limit)]
    def observe(self,pair,*,sol_mint,dexes,now=None,allow_open=True):
        stamp=time.time() if now is None else now;self.load(stamp)
        e=pool_evidence(pair,sol_mint,dexes,now_ms=stamp*1000)
        if not e:return 'invalid_pool'
        mint=e['mint'];rec=self.active.get(mint)
        if rec is None:
            if not allow_open or len(self.active)>=self.max_active or not (SHADOW_MIN_AGE_MINUTES<=e['age']<SHADOW_MAX_AGE_MINUTES and e['cap']>=SHADOW_MIN_CAP_USD):return 'not_shadow_entry'
            if shadow_entry_evidence(pair, sol_mint=sol_mint, dexes=dexes, now_ms=stamp*1000) is None:return 'insufficient_shadow_activity'
            self.active[mint]={'pool':e['pool'],'entry_price':e['price'],'entered_at':stamp,
                 'due':stamp+SHADOW_SAMPLE_INTERVAL_SECONDS,'observations':1,'age_at_entry':round(e['age'],2),
                 'market_cap_usd':e['cap'],'liquidity_usd':e['liquidity']}
            return 'shadow_opened'
        if e['pool']!=rec['pool']:return 'shadow_pool_mismatch'
        # Conservatively reserve 6% modeled round-trip costs; no live quotes.
        reason,net_pct=shadow_exit_signal(rec['entry_price'],e['price'],stamp-num(rec['entered_at']))
        rec['observations']=min(100,int(rec.get('observations',0))+1)
        rec['due']=stamp+SHADOW_SAMPLE_INTERVAL_SECONDS
        elapsed=stamp-num(rec['entered_at'])
        reason=reason or ''
        if reason:
            self.completed.append({'mint':mint,'result':reason,'entry_age':rec['age_at_entry'],
                 'net_pct':round(net_pct,2),'minutes':round(elapsed/60,1),
                 'observations':rec['observations'],'closed_at':stamp})
            self.completed=self.completed[-150:];self.active.pop(mint,None)
            return reason
        return 'shadow_observed'
    def save(self):
        self.load()
        if self.read_only:return False
        self.path.parent.mkdir(parents=True,exist_ok=True)
        path=self.path.with_suffix('.tmp')
        path.write_text(json.dumps({'version':1,'active':self.active,'completed':self.completed},separators=(',',':')))
        path.replace(self.path)
        return True
    def stats(self):
        wins=sum(r['net_pct']>0 for r in self.completed)
        return {'open':len(self.active),'completed':len(self.completed),'wins':wins,
                'net_pct_sum':round(sum(num(r.get('net_pct')) for r in self.completed),2)}

"""Persisted fresh-batch candidate scheduler; discovery hints, NEVER trade authorization.

A rejected token is dropped from the ACTIVE queue, not forgotten forever.
Fresh source evidence can reintroduce it after a bounded wait; held positions
are always monitored separately by bot.py.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from rolling_discovery import MINT_PATTERN

MOMENTUM_REASONS = frozenset({
    'momentum_price_change', 'momentum_volume', 'momentum_volume_1h',
    'momentum_buy_sell_ratio', 'momentum_volume_liquidity_ratio',
    'downtrend_multi_timeframe', 'extreme_price_spike', 'trend_m5_missing',
    'trend_h1_missing', 'trend_h6_missing', 'trend_h24_missing',
    'minimum_volume',
})
FAST_MOMENTUM_REASONS = frozenset({
    'fast_price_momentum', 'fast_volume_depth', 'fast_volume_pace',
    'fast_buy_pressure', 'fast_overheated_or_anomalous',
})
STRUCTURAL_REASONS = frozenset({
    'no_sol_quoted_pair', 'unsupported_sol_route', 'supported_pair_low_liquidity',
    'eligible_route_liquidity', 'market_cap_below_range', 'market_cap_above_range',
    'too_new_for_established_strategy',
})

def _bounded_launch_attempts(value) -> int:
    try: return max(0, min(4, int(value)))
    except (ValueError, TypeError, OverflowError): return 0


def _launch_fields(rec: dict) -> dict:
    return {'launch_attempts':_bounded_launch_attempts(rec.get('launch_attempts',0))}


def _bounded_hot_attempts(value) -> int:
    try:
        return max(0, min(2, int(value)))
    except (ValueError, TypeError, OverflowError):
        return 0


def _bounded_shadow_attempts(value) -> int:
    try:return max(0,min(12,int(value)))
    except (ValueError,TypeError,OverflowError):return 0


def _shadow_fields(rec:dict) -> dict:
    try: born=float(rec.get('shadow_created_at') or 0)
    except (TypeError,ValueError,OverflowError):born=0.
    return {'shadow_created_at':born if math.isfinite(born) and born>0 else 0.,
            'shadow_attempts':_bounded_shadow_attempts(rec.get('shadow_attempts',0)),
            'shadow_event_pending':bool(rec.get('shadow_event_pending',False))}


def _bounded_watch_attempts(value) -> int:
    try: return max(0,min(4,int(value)))
    except (TypeError,ValueError,OverflowError): return 0


def _bounded_recovery_attempts(value) -> int:
    try: return max(0,min(3,int(value)))
    except (ValueError,TypeError,OverflowError): return 0


class FreshBatchQueue:
    def __init__(self, path: Path, *, capacity: int = 240, ttl_seconds: int = 1800):
        self.path = Path(path)
        self.capacity = max(25, int(capacity))
        self.ttl_seconds = max(300, int(ttl_seconds))
        self.active: dict[str, dict] = {}
        self.rejected: dict[str, dict] = {}
        self.loaded = False
        self.read_only = False
        self.last_saved = 0.0
        self.last_offered = 0
        self.last_rejected = 0
        self.last_reactivated = 0

    def load(self, *, now: float | None = None) -> None:
        if self.loaded:
            return
        stamp = time.time() if now is None else now
        try:
            obj = json.loads(self.path.read_text(encoding='utf8'))
        except FileNotFoundError:
            self.loaded = True
            return
        except (OSError, ValueError, TypeError):
            self.loaded = self.read_only = True
            return
        if not isinstance(obj, dict) or obj.get('version') != 1 or not isinstance(obj.get('active'), dict) or not isinstance(obj.get('rejected'), dict):
            self.loaded = self.read_only = True
            return
        self.loaded = True
        for mint, rec in obj['active'].items():
            if not isinstance(mint, str) or not MINT_PATTERN.fullmatch(mint) or not isinstance(rec, dict):
                continue
            try:
                seen = float(rec['seen']); score = float(rec.get('score', 0)); checked = float(rec.get('checked', 0))
            except (KeyError, ValueError, TypeError):
                continue
            if all(math.isfinite(x) and x >= 0 for x in (seen, score, checked)) and 0 <= stamp - seen < self.ttl_seconds:
                self.active[mint] = {'seen':seen,'score':score,'checked':checked,
                                     **_launch_fields(rec), **_shadow_fields(rec),
                                     'hot_attempts':_bounded_hot_attempts(rec.get('hot_attempts',0)),
                                     'recovery_attempts':_bounded_recovery_attempts(rec.get('recovery_attempts',0)),
                                     'watch_attempts':_bounded_watch_attempts(rec.get('watch_attempts',0)),
                                     'novel': bool(rec.get('novel',False)),
                                     'feed_only': bool(rec.get('feed_only',False)),
                                     'pool_hint_at':float(rec.get('pool_hint_at',0) or 0),
                                     'event_pending':bool(rec.get('event_pending',False))}
        for mint, rec in obj['rejected'].items():
            if not isinstance(mint, str) or not MINT_PATTERN.fullmatch(mint) or not isinstance(rec, dict):
                continue
            try:
                until = float(rec['until']); seen = float(rec['seen']); reason = str(rec.get('reason',''))[:80]
            except (KeyError, ValueError, TypeError):
                continue
            if math.isfinite(until) and math.isfinite(seen) and 0 <= stamp - seen < 86400 and until > 0:
                self.rejected[mint] = {'until':until,'seen':seen,'reason':reason,
                                       **_launch_fields(rec), **_shadow_fields(rec),
                                     'hot_attempts':_bounded_hot_attempts(rec.get('hot_attempts',0)),
                                     'recovery_attempts':_bounded_recovery_attempts(rec.get('recovery_attempts',0)),
                                     'watch_attempts':_bounded_watch_attempts(rec.get('watch_attempts',0))}
        self.prune(now=stamp)

    def prune(self, *, now: float | None = None) -> None:
        stamp = time.time() if now is None else now
        self.active = {k:v for k,v in self.active.items() if 0 <= stamp-v['seen'] < self.ttl_seconds}
        self.rejected = {k:v for k,v in self.rejected.items() if 0 <= stamp-v['seen'] < 86400}
        if len(self.active) > self.capacity:
            evict = sorted(self.active, key=lambda m:(self.active[m]['checked'] == 0, self.active[m]['seen'], self.active[m]['score']))
            for mint in evict[:len(self.active)-self.capacity]:
                self.active.pop(mint, None)
        if len(self.rejected) > 5000:
            evict = sorted(self.rejected, key=lambda m:self.rejected[m]['until'])
            for mint in evict[:len(self.rejected)-5000]:
                self.rejected.pop(mint, None)

    def offer(self, scores: dict[str,float], *, now: float | None = None,
              novel_mints: set[str] | None = None,
              feed_mints: set[str] | None = None,
              pool_hints: set[str] | None = None) -> dict[str,int]:
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        self.prune(now=stamp)
        new = blocked = reactivated = 0
        genuinely_new = novel_mints or set()
        feed_mints = feed_mints or set()
        pool_hints = pool_hints or set()
        for mint, quality in scores.items():
            if not isinstance(mint, str) or not MINT_PATTERN.fullmatch(mint):
                continue
            ban = self.rejected.get(mint)
            if ban and ban['until'] > stamp:
                blocked += 1
                continue
            if ban:
                self.rejected.pop(mint, None)
                reactivated += 1
            try:
                score = float(quality)
            except (TypeError, ValueError):
                score = 0
            if not math.isfinite(score) or score < 0:
                score = 0
            if mint not in self.active:
                self.active[mint] = {'seen':stamp,'score':score,'checked':0.0,
                                     'launch_attempts':_bounded_launch_attempts(ban.get('launch_attempts',0)) if ban else 0,
                                     'hot_attempts':_bounded_hot_attempts(ban.get('hot_attempts',0)) if ban else 0,
                                     'recovery_attempts':_bounded_recovery_attempts(ban.get('recovery_attempts',0)) if ban else 0,
                                     'watch_attempts':_bounded_watch_attempts(ban.get('watch_attempts',0)) if ban else 0,
                                     **_shadow_fields(ban or {}),
                                     'novel': mint in genuinely_new and ban is None,
                                     'feed_only':mint in feed_mints and mint not in pool_hints,
                                     'pool_hint_at':stamp if mint in pool_hints else 0.0,
                                     'event_pending':False}
                new += 1
            else:
                self.active[mint]['seen'] = stamp
                self.active[mint]['score'] = score  # fresh source evidence, not historical max
                self.active[mint]['novel'] = bool(self.active[mint].get('novel',False) or mint in genuinely_new)
                if mint in pool_hints:
                    self.active[mint]['pool_hint_at'] = stamp
                    self.active[mint]['feed_only'] = False
                elif mint in feed_mints and not self.active[mint].get('pool_hint_at'):
                    self.active[mint]['feed_only'] = True
        self.prune(now=stamp)
        self.last_offered = new
        self.last_reactivated = reactivated
        return {'new':new,'blocked':blocked,'reactivated':reactivated,'active':len(self.active)}

    def promote_pool_event(self, mint: str, *, now: float | None = None) -> bool:
        """New migration signal permits one immediate FULL verification, never a buy."""
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        if not isinstance(mint, str) or not MINT_PATTERN.fullmatch(mint):
            return False
        if mint in self.active:
            if self.active[mint].get('shadow_created_at') and stamp-float(self.active[mint]['shadow_created_at'])>=12*60:
                self.active[mint]['shadow_created_at']=0.
            self.active[mint]['checked']=0.0
            self.active[mint]['score']=max(900.0,self.active[mint]['score'])
            self.active[mint]['event_pending']=True
            self.active[mint]['shadow_event_pending']=True
            return True
        old=self.rejected.pop(mint,None) or {}
        self.active[mint]={'seen':stamp,'score':900.0,'checked':0.0,
                           'launch_attempts':_bounded_launch_attempts(old.get('launch_attempts',0)),
                           'hot_attempts':_bounded_hot_attempts(old.get('hot_attempts',0)),
                           'recovery_attempts':_bounded_recovery_attempts(old.get('recovery_attempts',0)),
                           'watch_attempts':_bounded_watch_attempts(old.get('watch_attempts',0)),
                           **_shadow_fields(old),
                           'novel':False, 'feed_only':False, 'pool_hint_at':0.0,
                           'event_pending':True}
        self.prune(now=stamp)
        return mint in self.active

    def discard(self, mint: str, reason: str, *, now: float | None = None) -> bool:
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        if mint not in self.active:
            return False
        self.active.pop(mint)
        # Treat unavailable upstream market data as inconclusive; retry later.
        delay = (7200 if reason in STRUCTURAL_REASONS else
                 300 if reason in FAST_MOMENTUM_REASONS else
                 1800 if reason in MOMENTUM_REASONS else 900)
        self.rejected[mint] = {'until':stamp+delay,'seen':stamp,'reason':str(reason)[:80]}
        self.last_rejected += 1
        self.prune(now=stamp)
        return True

    def launch_attempts(self, mint: str, *, now: float | None = None) -> int:
        self.load(now=now)
        rec = self.active.get(mint) or self.rejected.get(mint) or {}
        return _bounded_launch_attempts(rec.get('launch_attempts', 0))

    def schedule_launch_pool_recheck(self, mint: str, reason: str, *,
                                    now: float | None = None,
                                    delay_seconds: int = 120, max_attempts: int = 4) -> bool:
        """Re-observe absent/immature liquidity, NEVER authorize a trade.

        Persisted in the normal v1 queue for crash-safe resumption. Pool quotes,
        cap, activity and sellability are revalidated by the entry scanner.
        """
        from launch_pool_followup_v83 import POOL_WAIT_REASONS
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        if reason not in POOL_WAIT_REASONS: return False
        rec = self.active.get(mint)
        if rec is None: return False
        tries = _bounded_launch_attempts(rec.get('launch_attempts', 0))
        if tries >= max(1, min(4, int(max_attempts))): return False
        self.active.pop(mint)
        self.rejected[mint] = {
            'until':stamp+max(60,int(delay_seconds)), 'seen':stamp,
            'reason':'launch_pool:'+reason, 'launch_attempts':tries+1,
            'watch_attempts':_bounded_watch_attempts(rec.get('watch_attempts',0)),
            'hot_attempts':_bounded_hot_attempts(rec.get('hot_attempts',0)),
            'recovery_attempts':_bounded_recovery_attempts(rec.get('recovery_attempts',0)),
        }
        self.prune(now=stamp)
        return True

    def resume_launch_pool_due(self, *, now: float | None = None,
                               limit: int = 6) -> list[str]:
        """Convert due observation watches into fresh pool market lookups."""
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        self.prune(now=stamp)
        eligible = sorted((m for m,r in self.rejected.items()
                           if str(r.get('reason','')).startswith('launch_pool:')
                           and 1 <= _bounded_launch_attempts(r.get('launch_attempts',0)) <= 4
                           and r['until'] <= stamp and stamp-r['seen'] <= 3600),
                          key=lambda m: (self.rejected[m]['until'],m))
        moved = []
        for mint in eligible[:max(0,min(8,int(limit)))]:
            rec = self.rejected.pop(mint)
            self.active[mint] = {
                'seen':stamp, 'score':650.0, 'checked':0.0,
                'launch_attempts':_bounded_launch_attempts(rec.get('launch_attempts',0)),
                'watch_attempts':_bounded_watch_attempts(rec.get('watch_attempts',0)),
                'hot_attempts':_bounded_hot_attempts(rec.get('hot_attempts',0)),
                'recovery_attempts':_bounded_recovery_attempts(rec.get('recovery_attempts',0)),
                'novel':False,'feed_only':True,
                'pool_hint_at':0.0,'event_pending':False,
            }
            moved.append(mint)
        self.prune(now=stamp)
        return moved

    def schedule_hot_recheck(self, mint: str, reason: str, *, now: float | None = None,
                             delay_seconds: int = 90, max_attempts: int = 2) -> bool:
        """Limited market RECHECK, never entry approval or structural-filter bypass.

        Only the caller can certify that this mint still has fresh same-pool
        activity and a narrowly missed momentum threshold. An already scanned
        token gets at most two rechecks before normal rejection quarantine.
        """
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        if reason not in MOMENTUM_REASONS | FAST_MOMENTUM_REASONS:
            return False
        rec = self.active.get(mint)
        if not rec:
            return False
        attempts = _bounded_hot_attempts(rec.get('hot_attempts', 0))
        if attempts >= max(1, max_attempts):
            return False
        self.active.pop(mint)
        self.rejected[mint] = {
            'until':stamp+max(75,int(delay_seconds)), 'seen':stamp,
            'reason':reason, 'hot_attempts':attempts+1,
            'recovery_attempts':_bounded_recovery_attempts(rec.get('recovery_attempts',0)),
        }
        self.prune(now=stamp)
        return True

    def resume_hot_due(self, *, now: float | None = None, limit: int = 4) -> list[str]:
        """Promote previously verified near-misses to fresh full market checks.

        The normal bot quality and entry cool-down checks still apply. Never
        converts a rejected token into a trade signal by itself.
        """
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        self.prune(now=stamp)
        eligible = sorted((m for m,r in self.rejected.items()
                           if 1 <= r.get('hot_attempts',0) <= 2
                           and r['reason'] not in {'trend_m5_missing','trend_h1_missing','trend_h6_missing','trend_h24_missing'}
                           and r['until'] <= stamp and stamp-r['seen'] <= 900),
                          key=lambda m:(self.rejected[m]['until'],m))
        moved=[]
        for mint in eligible[:max(0,min(limit,8))]:
            rec=self.rejected.pop(mint)
            self.active[mint] = {'seen':stamp, 'score':500.0, 'checked':0.0,
                                 'hot_attempts':rec['hot_attempts'],
                                 'recovery_attempts':_bounded_recovery_attempts(rec.get('recovery_attempts',0))}
            moved.append(mint)
        self.prune(now=stamp)
        return moved

    def schedule_data_recovery(self, mint: str, reason: str, *, now: float | None = None,
                               delay_seconds: int = 105, max_attempts: int = 3) -> bool:
        """Schedule a fresh data-only market fetch; never assume missing trend is bullish."""
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        if reason not in {'trend_m5_missing','trend_h1_missing','trend_h6_missing','trend_h24_missing'}:
            return False
        rec = self.active.get(mint)
        if not rec: return False
        attempts = _bounded_recovery_attempts(rec.get('recovery_attempts',0))
        if attempts >= max(1,min(3,int(max_attempts))): return False
        self.active.pop(mint)
        self.rejected[mint] = {
            'until':stamp+max(60,int(delay_seconds)), 'seen':stamp,
            'reason':reason, **_launch_fields(rec),
                                     'hot_attempts':_bounded_hot_attempts(rec.get('hot_attempts',0)),
            'recovery_attempts':attempts+1,
        }
        self.prune(now=stamp)
        return True

    def resume_data_due(self, *, now: float | None = None, limit: int = 4) -> list[str]:
        """Independently schedule fresh pool observations, even absent discovery feeds."""
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        self.prune(now=stamp)
        eligible = sorted((m for m,r in self.rejected.items()
                           if 1<=r.get('recovery_attempts',0)<=3
                           and r['reason'] in {'trend_m5_missing','trend_h1_missing','trend_h6_missing','trend_h24_missing'}
                           and r['until']<=stamp and stamp-r['seen']<=900),
                          key=lambda m:(self.rejected[m]['until'],m))
        moved=[]
        for mint in eligible[:max(0,min(limit,8))]:
            rec=self.rejected.pop(mint)
            self.active[mint] = {
                'seen':stamp,'score':550.0,'checked':0.0,
                **_launch_fields(rec),
                                     'hot_attempts':_bounded_hot_attempts(rec.get('hot_attempts',0)),
                'recovery_attempts':_bounded_recovery_attempts(rec['recovery_attempts']),
            }
            moved.append(mint)
        self.prune(now=stamp)
        return moved

    def schedule_threshold_watch(self, mint: str, kind: str, *, now: float | None = None,
                                 delay_seconds: int = 120, max_attempts: int = 4) -> bool:
        """Independent observation-only recheck for near-age or near-cap pools.

        This stores no trade authorization. Watch acceptance is always decided
        separately from an actual fresh verified SOL pool by bot.py.
        """
        stamp=time.time() if now is None else now
        self.load(now=stamp)
        if kind not in ('age','cap','provider'):return False
        rec=self.active.get(mint)
        if rec is None:return False
        attempts=_bounded_watch_attempts(rec.get('watch_attempts',0))
        if attempts>=min(4,max_attempts):return False
        self.active.pop(mint)
        self.rejected[mint]={'until':stamp+max(60,int(delay_seconds)), 'seen':stamp,
            'reason':'threshold_watch_'+kind,'watch_attempts':attempts+1,
            **_launch_fields(rec),
                                     'hot_attempts':_bounded_hot_attempts(rec.get('hot_attempts',0)),
            'recovery_attempts':_bounded_recovery_attempts(rec.get('recovery_attempts',0)),
                                     'feed_only':bool(rec.get('feed_only',False)),
                                     'pool_hint_at':float(rec.get('pool_hint_at',0) or 0)}
        self.prune(now=stamp)
        return True

    def resume_threshold_due(self, *, now: float | None = None, limit: int = 4) -> list[str]:
        stamp=time.time() if now is None else now
        self.load(now=stamp);self.prune(now=stamp)
        eligible=sorted((m for m,r in self.rejected.items()
            if r.get('reason') in ('threshold_watch_age','threshold_watch_cap','threshold_watch_provider')
            and 1<=_bounded_watch_attempts(r.get('watch_attempts',0))<=4
            and r['until']<=stamp and stamp-r['seen']<=1200),
            key=lambda m:(self.rejected[m]['until'],m))
        moved=[]
        for mint in eligible[:max(0,min(8,limit))]:
            rec=self.rejected.pop(mint)
            self.active[mint]={'seen':stamp,'score':700.0,'checked':0.0,
               'watch_attempts':_bounded_watch_attempts(rec.get('watch_attempts',0)),
               **_launch_fields(rec),
                                     'hot_attempts':_bounded_hot_attempts(rec.get('hot_attempts',0)),
               'recovery_attempts':_bounded_recovery_attempts(rec.get('recovery_attempts',0))}
            moved.append(mint)
        self.prune(now=stamp)
        return moved

    def defer_blocked(self, mint: str, reason: str, *, now: float | None = None,
                      delay_seconds: int = 1800) -> bool:
        """Remove an unscannable legacy/quality-cooled hint from the ACTIVE batch.

        A different subsystem may already have a longer backoff. Re-discovery
        after this bounded wait never overrides that subsystem's own check.
        This is queue hygiene only; no paper positions or market data change.
        """
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        if mint not in self.active:
            return False
        self.active.pop(mint)
        previous = self.rejected.get(mint, {})
        until = max(float(previous.get('until', 0.0)), stamp + max(60, int(delay_seconds)))
        self.rejected[mint] = {'until':until, 'seen':stamp, 'reason':str(reason)[:80]}
        self.prune(now=stamp)
        return True

    def blocked(self, mint: str, *, now: float | None = None) -> bool:
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        return self.rejected.get(mint, {}).get('until', 0) > stamp

    def due(self, *, limit: int = 30, now: float | None = None) -> list[str]:
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        self.prune(now=stamp)
        return sorted(self.active, key=lambda m:(
            self.active[m]['checked'] > 0,
            not self.active[m].get('novel',False),
            self.active[m]['checked'], -self.active[m]['score'], m))[:max(0, limit)]


    def offer_shadow_pool_hints(self, hints: dict[str,float], *, now: float | None=None) -> int:
        """Recent Gecko independent pool birth hints: one fresh Dex lookup, not an entry.

        Only this independently observed supported 3-12m SOL pool may revive
        a legacy cooldown. Never use age/liquidity from hints for buying.
        """
        stamp=time.time() if now is None else now
        self.load(now=stamp)
        accepted=0
        for mint,created in hints.items():
            if not isinstance(mint,str) or not MINT_PATTERN.fullmatch(mint):continue
            try:birth=float(created)
            except (ValueError,TypeError,OverflowError):continue
            if not math.isfinite(birth) or not 3*60<=stamp-birth<12*60:continue
            old=self.active.get(mint) or self.rejected.get(mint) or {}
            prior_birth=float(old.get('shadow_created_at') or 0)
            # Same exhausted pool must not be reactivated every refresh.
            if abs(prior_birth-birth)<3 and _bounded_shadow_attempts(old.get('shadow_attempts',0))>=12:
                continue
            rec=self.active.get(mint)
            if rec is None:
                self.rejected.pop(mint,None)
                rec={'seen':stamp,'score':1_000_000.,'checked':0.,
                     'novel':False,'feed_only':False,'pool_hint_at':stamp,
                     'event_pending':False,'launch_attempts':0,
                     'hot_attempts':0,'watch_attempts':0,'recovery_attempts':0}
                self.active[mint]=rec
            rec['seen']=stamp
            rec['score']=max(float(rec.get('score') or 0),1_000_000.)
            rec['feed_only']=False
            rec['pool_hint_at']=stamp
            # Avoid perpetual immediate rechecks from provider's same pool.
            if abs(prior_birth-birth)>=3:
                rec['checked']=0.
                rec['shadow_attempts']=0
                rec['shadow_event_pending']=True
            else:
                rec['shadow_attempts']=_bounded_shadow_attempts(old.get('shadow_attempts',0))
                rec['shadow_event_pending']=bool(rec.get('shadow_event_pending',False))
            rec['shadow_created_at']=birth
            accepted+=1
        self.prune(now=stamp)
        return accepted

    def schedule_shadow_recheck(self,mint:str,reason:str,*,created_at:float,
                                delay_seconds:int=45,now:float|None=None)->bool:
        """A primary-priced 3-12m pool gets a bounded fresh observation.

        Never reuses cached prices/transaction counts for shadow entry.
        """
        stamp=time.time() if now is None else now
        self.load(now=stamp)
        rec=self.active.get(mint)
        if rec is None:return False
        try:birth=float(created_at)
        except (TypeError,ValueError,OverflowError):return False
        if not (math.isfinite(birth) and 3*60<=stamp-birth<12*60):return False
        tries=_bounded_shadow_attempts(rec.get('shadow_attempts',0))
        if tries>=12:return False
        delay=max(30,min(90,int(delay_seconds)))
        if stamp+delay >= birth+12*60:return False
        self.active.pop(mint)
        self.rejected[mint]={'until':stamp+delay,'seen':stamp,'reason':'shadow_wait:'+str(reason)[:65],
                             'shadow_created_at':birth,'shadow_attempts':tries+1}
        self.prune(now=stamp)
        return True

    def resume_shadow_due(self,*,now:float|None=None,limit:int=12)->list[str]:
        stamp=time.time() if now is None else now
        self.load(now=stamp)
        self.prune(now=stamp)
        due=sorted((m for m,r in self.rejected.items()
                    if str(r.get('reason','')).startswith('shadow_wait:')
                    and r['until']<=stamp
                    and 3*60<=stamp-float(r.get('shadow_created_at') or 0)<12*60
                    and 1<=_bounded_shadow_attempts(r.get('shadow_attempts'))<=12),
                   key=lambda m:(self.rejected[m]['until'],m))
        moved=[]
        for mint in due[:max(0,min(24,int(limit)))]:
            old=self.rejected.pop(mint)
            self.active[mint]={'seen':stamp,'score':1_000_000.,'checked':0.,
                               'novel':False,'feed_only':False,'pool_hint_at':stamp,
                               'event_pending':False,'shadow_event_pending':True,
                               'shadow_created_at':old['shadow_created_at'],
                               'shadow_attempts':old['shadow_attempts']}
            moved.append(mint)
        self.prune(now=stamp)
        return moved

    def due_shadow(self, *, limit:int=40,launch_quota:int=2,now:float|None=None)->list[str]:
        """Strict early-pool first, migration next, bounded launch diagnostics."""
        stamp=time.time() if now is None else now
        self.load(now=stamp)
        self.prune(now=stamp)
        cap=max(0,min(self.capacity,int(limit)))
        if cap==0:return []
        pool_early=[];events=[];other=[];launches=[]
        for mint,r in self.active.items():
            born=float(r.get('shadow_created_at') or 0)
            if born:
                if not (3*60<=stamp-born<12*60):continue
                # Same pool recently checked has to wait for recheck scheduling.
                if r.get('checked',0)>0 and not r.get('shadow_event_pending'):continue
                pool_early.append(mint)
            elif r.get('event_pending'):events.append(mint)
            elif r.get('feed_only'):launches.append(mint)
            elif r.get('checked',0)<=0:other.append(mint)
        pool_early.sort(key=lambda m:(self.active[m].get('checked',0)>0,
             abs((stamp-self.active[m]['shadow_created_at'])/60-6.5), m))
        events.sort(key=lambda m:self.active[m].get('checked',0))
        other.sort(key=lambda m:(-self.active[m].get('pool_hint_at',0),-self.active[m].get('score',0)))
        launches.sort(key=lambda m:(-self.active[m].get('score',0),self.active[m].get('seen',0)))
        result=[]
        for mint in pool_early+events+other[:8]:
            if len(result)>=cap:break
            if mint not in result:result.append(mint)
        for mint in launches[:max(0,min(int(launch_quota),cap-len(result)))]:
            if mint not in result:result.append(mint)
        return result

    def shadow_due_counts(self, mints:list[str],*,now:float|None=None)->dict[str,int]:
        stamp=time.time() if now is None else now
        counts={'early_pool':0,'migration':0,'launch_only':0,'other_pool':0,'expired_shadow':0}
        for mint,r in self.active.items():
            if r.get('shadow_created_at') and stamp-float(r['shadow_created_at'])>=12*60:
                counts['expired_shadow']+=1
        for mint in mints:
            r=self.active.get(mint,{})
            if r.get('shadow_created_at'):counts['early_pool']+=1
            elif r.get('event_pending'):counts['migration']+=1
            elif r.get('feed_only'):counts['launch_only']+=1
            else:counts['other_pool']+=1
        return counts

    def due_v85(self, *, limit: int = 40, launch_quota: int = 6,
                migration_quota: int = 6, now: float | None = None) -> list[str]:
        """Reserve most lookups for current DEX-pool leads, not raw launches.

        Historical quotes and source hints are never used for BUY approval.
        Launch-only leads have a small bounded observation lane; genuine
        migration events have a separate one-time fast-market-lookup lane.
        """
        stamp = time.time() if now is None else now
        cap=max(0,min(self.capacity,int(limit)))
        if not cap: return []
        ready=self.due(limit=self.capacity,now=stamp)
        events=[m for m in ready if self.active[m].get('event_pending')]
        hints=[m for m in ready if m not in events and
               0 <= stamp-float(self.active[m].get('pool_hint_at') or 0) <= 240 and
               float(self.active[m].get('pool_hint_at') or 0)>0]
        other=[m for m in ready if m not in events and m not in hints and
               not self.active[m].get('feed_only',False)]
        launches=[m for m in ready if self.active[m].get('feed_only',False) and m not in events and m not in hints]
        selected=events[:max(0,min(cap,int(migration_quota)))]
        reserve=min(max(0,int(launch_quota)), max(0, cap-len(selected)),len(launches))
        for mint in hints+other:
            if len(selected)>=cap-reserve: break
            if mint not in selected: selected.append(mint)
        for mint in launches[:reserve]:
            if mint not in selected: selected.append(mint)
        # No artificial starvation if only live launch hints are available;
        # still limit that expensive, low-yield lane per scheduled scan.
        if len(selected)<cap:
            for mint in events+hints+other:
                if len(selected)>=cap:break
                if mint not in selected:selected.append(mint)
        return selected

    def due_v85_counts(self, mints: list[str]) -> dict[str,int]:
        return {
            'migration':sum(bool(self.active.get(m,{}).get('event_pending')) for m in mints),
            'liquid_source_hints':sum(bool(self.active.get(m,{}).get('pool_hint_at'))
                         and not self.active.get(m,{}).get('event_pending') for m in mints),
            'launch_only':sum(bool(self.active.get(m,{}).get('feed_only'))
                       and not self.active.get(m,{}).get('event_pending') for m in mints),
        }

    def mark_scanned(self, mints: list[str], *, now: float | None = None) -> None:
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        for mint in mints:
            if mint in self.active:
                self.active[mint]['checked'] = stamp
                self.active[mint]['event_pending'] = False
                self.active[mint]['shadow_event_pending'] = False

    def stats(self, *, now: float | None = None) -> dict:
        self.load(now=now)
        self.prune(now=now)
        return {'active':len(self.active), 'novel_unscanned':sum(r['checked']==0 and r.get('novel',False) for r in self.active.values()),
                'unscanned':sum(r['checked']==0 for r in self.active.values()),
                'quarantined':sum(r['until'] > (time.time() if now is None else now) for r in self.rejected.values())}

    def save(self, *, now: float | None = None, force: bool = False) -> bool:
        stamp = time.time() if now is None else now
        if not self.loaded or self.read_only or (not force and stamp-self.last_saved < 45):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps({'version':1,'active':self.active,'rejected':self.rejected}, separators=(',',':')), encoding='utf8')
        tmp.replace(self.path)
        self.last_saved = stamp
        return True

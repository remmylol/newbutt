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
                                     **_launch_fields(rec),
                                     'hot_attempts':_bounded_hot_attempts(rec.get('hot_attempts',0)),
                                     'recovery_attempts':_bounded_recovery_attempts(rec.get('recovery_attempts',0)),
                                     'watch_attempts':_bounded_watch_attempts(rec.get('watch_attempts',0)),
                                     'novel': bool(rec.get('novel',False))}
        for mint, rec in obj['rejected'].items():
            if not isinstance(mint, str) or not MINT_PATTERN.fullmatch(mint) or not isinstance(rec, dict):
                continue
            try:
                until = float(rec['until']); seen = float(rec['seen']); reason = str(rec.get('reason',''))[:80]
            except (KeyError, ValueError, TypeError):
                continue
            if math.isfinite(until) and math.isfinite(seen) and 0 <= stamp - seen < 86400 and until > 0:
                self.rejected[mint] = {'until':until,'seen':seen,'reason':reason,
                                       **_launch_fields(rec),
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
              novel_mints: set[str] | None = None) -> dict[str,int]:
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        self.prune(now=stamp)
        new = blocked = reactivated = 0
        genuinely_new = novel_mints or set()
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
                                     'novel': mint in genuinely_new and ban is None}
                new += 1
            else:
                self.active[mint]['seen'] = stamp
                self.active[mint]['score'] = score  # fresh source evidence, not historical max
                self.active[mint]['novel'] = bool(self.active[mint].get('novel',False) or mint in genuinely_new)
        self.prune(now=stamp)
        self.last_offered = new
        self.last_reactivated = reactivated
        return {'new':new,'blocked':blocked,'reactivated':reactivated,'active':len(self.active)}

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
                'novel':False,
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
            'recovery_attempts':_bounded_recovery_attempts(rec.get('recovery_attempts',0))}
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

    def mark_scanned(self, mints: list[str], *, now: float | None = None) -> None:
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        for mint in mints:
            if mint in self.active:
                self.active[mint]['checked'] = stamp

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

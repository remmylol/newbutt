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

def _bounded_hot_attempts(value) -> int:
    try:
        return max(0, min(2, int(value)))
    except (ValueError, TypeError, OverflowError):
        return 0


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
                                     'hot_attempts':_bounded_hot_attempts(rec.get('hot_attempts',0))}
        for mint, rec in obj['rejected'].items():
            if not isinstance(mint, str) or not MINT_PATTERN.fullmatch(mint) or not isinstance(rec, dict):
                continue
            try:
                until = float(rec['until']); seen = float(rec['seen']); reason = str(rec.get('reason',''))[:80]
            except (KeyError, ValueError, TypeError):
                continue
            if math.isfinite(until) and math.isfinite(seen) and 0 <= stamp - seen < 86400 and until > 0:
                self.rejected[mint] = {'until':until,'seen':seen,'reason':reason,
                                       'hot_attempts':_bounded_hot_attempts(rec.get('hot_attempts',0))}
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

    def offer(self, scores: dict[str,float], *, now: float | None = None) -> dict[str,int]:
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        self.prune(now=stamp)
        new = blocked = reactivated = 0
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
                                     'hot_attempts':_bounded_hot_attempts(ban.get('hot_attempts',0)) if ban else 0}
                new += 1
            else:
                self.active[mint]['seen'] = stamp
                self.active[mint]['score'] = score  # fresh source evidence, not historical max
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
                           and r['until'] <= stamp and stamp-r['seen'] <= 900),
                          key=lambda m:(self.rejected[m]['until'],m))
        moved=[]
        for mint in eligible[:max(0,min(limit,8))]:
            rec=self.rejected.pop(mint)
            self.active[mint] = {'seen':stamp, 'score':500.0, 'checked':0.0,
                                 'hot_attempts':rec['hot_attempts']}
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
        return {'active':len(self.active), 'unscanned':sum(r['checked']==0 for r in self.active.values()),
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

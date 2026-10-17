"""Persisted PAPER-only provenance, screening outcomes and near-eligible watches.

Discovery is an untrusted hint. Nothing here authorizes an entry, overrides a
cooldown, or modifies the portfolio. Fail-safe when the sidecar is unreadable.
"""
from __future__ import annotations

from collections import Counter
import json
import math
import time
from pathlib import Path
from rolling_discovery import MINT_PATTERN

SOURCE_NAMES = ('geckoterminal', 'dexpaprika', 'recent_profiles', 'dex_search', 'near_watch', 'jupiter', 'birdeye', 'raydium_volume', 'dex_promotion_verified', 'fresh_launch_feed')


def safe_nonnegative_number(value, default=0.0):
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return default
    return number if math.isfinite(number) and number >= 0 else default


def safe_bounded_int(value, maximum=10000):
    try:
        number = int(value)
    except (ValueError, TypeError, OverflowError):
        return 0
    return max(0,min(maximum,number))


class CandidatePipeline:
    def __init__(self, path: Path, *, limit: int = 2500):
        self.path = Path(path)
        self.limit = max(100, limit)
        self.loaded = False
        self.read_only = False
        self.records: dict[str, dict] = {}
        self.near: dict[str, dict] = {}
        self.last_saved = 0.0

    def load(self, *, now: float | None = None):
        if self.loaded:
            return
        self.loaded = True
        stamp = time.time() if now is None else now
        try:
            data = json.loads(self.path.read_text(encoding='utf8'))
        except FileNotFoundError:
            return
        except (ValueError, OSError, TypeError):
            self.read_only = True
            return
        if not isinstance(data, dict) or data.get('version') != 1:
            self.read_only = True
            return
        if not isinstance(data.get('records'), dict) or not isinstance(data.get('near'), dict):
            self.read_only = True
            return
        for mint, rec in data['records'].items():
            if not isinstance(mint, str) or not MINT_PATTERN.fullmatch(mint) or not isinstance(rec, dict):
                continue
            try:
                last = float(rec['last'])
            except (ValueError, TypeError, KeyError):
                continue
            if not math.isfinite(last) or not 0 <= stamp - last < 86400:
                continue
            sources = sorted(set(rec.get('sources', [])) & set(SOURCE_NAMES)) if isinstance(rec.get('sources'), list) else []
            self.records[mint] = {
                'first': safe_nonnegative_number(rec.get('first', last), last), 'last':last, 'sources': sources,
                'checks': max(0, min(10000, int(rec.get('checks', 0)))),
                'passes':max(0, min(10000, int(rec.get('passes', 0)))),
                'status':str(rec.get('status', 'discovered'))[:60],
                'reason':str(rec.get('reason', ''))[:80],
                'market_cap':safe_nonnegative_number(rec.get('market_cap', 0)),
                'last_volume':safe_nonnegative_number(rec.get('last_volume', 0)),
                'last_flow':safe_nonnegative_number(rec.get('last_flow', 0)),
                'last_market_at':safe_nonnegative_number(rec.get('last_market_at', 0)),
                'accelerating':bool(rec.get('accelerating', False)),
                'observations':safe_bounded_int(rec.get('observations',0)),
                'last_change_5m':safe_nonnegative_number(rec.get('last_change_5m',0)),
                'last_liquidity':safe_nonnegative_number(rec.get('last_liquidity',0)),
                'paper_entries':safe_bounded_int(rec.get('paper_entries',0)),
            }
        for mint, rec in data['near'].items():
            if mint not in self.records or not isinstance(rec, dict):
                continue
            try:
                due, until = float(rec['due']), float(rec['until'])
                tries = int(rec.get('tries', 0))
            except (ValueError, TypeError, KeyError):
                continue
            if all(math.isfinite(x) for x in (due, until)) and until > stamp and 0 <= tries <= 4:
                self.near[mint] = {'due':due,'until':until,'tries':tries,'reason':str(rec.get('reason',''))[:45]}
        self.prune(now=stamp)

    def prune(self, *, now: float | None = None):
        stamp = time.time() if now is None else now
        self.records = {m:r for m,r in self.records.items() if 0 <= stamp-r['last'] < 86400}
        if len(self.records) > self.limit:
            evict = sorted(self.records, key=lambda m:(self.records[m]['last'], m))
            for m in evict[:len(self.records)-self.limit]:
                self.records.pop(m, None)
        self.near = {m:r for m,r in self.near.items() if m in self.records and r['until'] > stamp and r['tries'] < 4}
        if len(self.near) > 500:
            for m in sorted(self.near, key=lambda k:self.near[k]['until'])[:len(self.near)-500]:
                self.near.pop(m, None)

    def register_sources(self, sources: dict[str, object], *, now: float | None = None):
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        for source, mints in sources.items():
            if source not in SOURCE_NAMES:
                continue
            for mint in mints:
                if not isinstance(mint,str) or not MINT_PATTERN.fullmatch(mint):
                    continue
                rec = self.records.setdefault(mint, {'first':stamp,'last':stamp,'sources':[],
                    'checks':0,'passes':0,'status':'discovered','reason':'','market_cap':0.0,
                    'last_volume':0.0,'last_flow':0.0,'last_market_at':0.0,'accelerating':False,
                    'observations':0,'last_change_5m':0.0,'last_liquidity':0.0,'paper_entries':0})
                rec['last'] = stamp
                if source not in rec['sources']:
                    rec['sources'].append(source)
        self.prune(now=stamp)

    def rank(self, priority: dict[str, float]) -> dict[str, float]:
        """Bound heterogeneous source scores so one provider can't monopolize scans."""
        result = {}
        for mint, raw in priority.items():
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = 0.0
            if not math.isfinite(value):
                value = 0.0
            rec = self.records.get(mint, {})
            overlap = len(rec.get('sources', []))
            already_checked = bool(rec.get('checks', 0))
            # Never change eligibility or final strategy score, only queue order.
            result[mint] = (50 * math.log1p(min(1_000_000, max(0, value)))
                            + 65 * min(3, overlap) + (90 if not already_checked else 0)
                            + (50 if rec.get('accelerating') else 0))
        return result

    def offer_near(self, hints: dict[str, dict], *, now: float | None = None) -> int:
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        count = 0
        self.register_sources({'near_watch':hints}, now=stamp)
        for mint, hint in hints.items():
            if mint not in self.records or not isinstance(hint,dict):
                continue
            old = self.near.get(mint)
            if old and old['until'] > stamp:
                # A fresh API observation may accelerate maturation but never
                # clears the retry backoff of an already attempted token.
                old['due'] = max(old['due'] if old['tries'] else stamp, min(old['due'], float(hint['due'])))
                continue
            due = float(hint.get('due',stamp+600))
            if not math.isfinite(due):
                continue
            self.near[mint] = {'due':max(stamp, due), 'until':stamp+10800,'tries':0,
                               'reason':str(hint.get('reason', 'near_threshold'))[:45]}
            count += 1
        self.prune(now=stamp)
        return count

    def matured_due(self, *, now: float | None = None, limit: int = 12) -> dict[str, float]:
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        due = sorted((m for m,r in self.near.items() if r['due'] <= stamp and r['tries'] < 4),
                     key=lambda m:(self.near[m]['due'],m))[:max(0,limit)]
        for mint in due:
            rec = self.near[mint]
            rec['tries'] += 1
            rec['due'] = stamp + 900  # 15-minute bounded recheck; no forced buys
        self.prune(now=stamp)
        return {m:250.0 for m in due}

    def observe_market(self, mint: str, pair: dict, *, now: float | None = None) -> bool:
        """Record ACTUAL same-pool activity even if a final market filter fails.

        This changes diagnostic rankings only; it never grants market eligibility.
        Missing/unverified metrics do not become zero or positive evidence.
        """
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        rec = self.records.get(mint)
        if rec is None or not isinstance(pair,dict): return False
        vol = pair.get('volume') or {}
        txns = (pair.get('txns') or {}).get('m5') or {}
        ch = pair.get('priceChange') or {}
        v5=safe_nonnegative_number(vol.get('m5'))
        buys=safe_nonnegative_number(txns.get('buys'))
        sells=safe_nonnegative_number(txns.get('sells'))
        change=safe_nonnegative_number(ch.get('m5'))
        liq=safe_nonnegative_number((pair.get('liquidity') or {}).get('usd'))
        if v5<=0 or buys+sells<=0 or liq<=0:
            return False
        flow=buys/(buys+sells)
        dt=stamp-rec.get('last_market_at',0)
        previous=rec.get('last_volume',0)
        increasing=(30<=dt<=600 and previous>0 and v5>=1.15*previous
                    and flow>=rec.get('last_flow',0) and flow>=0.52)
        rec['accelerating']=bool(increasing)
        rec['last_volume']=v5
        rec['last_flow']=flow
        rec['last_change_5m']=change
        rec['last_liquidity']=liq
        rec['last_market_at']=stamp
        rec['observations']=min(10000,rec.get('observations',0)+1)
        rec['last']=stamp
        return increasing

    def record_scan(self, checked, passed: dict, rejected: dict[str,str], *, now: float | None = None):
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        by_source = {s:{'checked':0,'passed':0} for s in SOURCE_NAMES}
        unique_checked = 0
        unique_passes = 0
        for mint in checked:
            rec = self.records.get(mint)
            if rec is None:
                continue
            first_check = rec['checks'] == 0
            first_pass = rec['passes'] == 0 and mint in passed
            rec['checks'] += 1
            rec['last'] = stamp
            if mint in passed:
                rec['passes'] += 1
                rec['status'] = 'market_pass'
                rec['reason'] = ''
                c = passed[mint]
                rec['market_cap'] = float(c.get('market_cap_usd') or 0)
                volume = max(0.0, float(c.get('m5_volume') or 0))
                buys = max(0.0, float(c.get('m5_buys') or 0))
                sells = max(0.0, float(c.get('m5_sells') or 0))
                flow = buys / max(1.0, buys+sells)
                if not rec.get('observations',0):
                    rec['accelerating'] = bool(
                        30 <= stamp-rec.get('last_market_at', 0) <= 600
                        and rec.get('last_volume', 0) > 0
                        and volume >= 1.15*rec['last_volume']
                        and flow >= rec.get('last_flow', 0))
                    rec['last_volume'] = volume
                    rec['last_flow'] = flow
                    rec['last_market_at'] = stamp
                self.near.pop(mint,None)
            else:
                rec['status'] = 'rejected' if mint in rejected else 'unresolved'
                rec['reason'] = rejected.get(mint, 'no_verified_pass')[:80]
            unique_checked += int(first_check)
            unique_passes += int(first_pass)
            for src in rec['sources']:
                by_source[src]['checked'] += int(first_check)
                by_source[src]['passed'] += int(first_pass)
        self.prune(now=stamp)
        return {'first_checks':unique_checked,'first_passes':unique_passes,
                'by_source':{s:v for s,v in by_source.items() if v['checked'] or v['passed']},
                'near_watched':len(self.near),'records':len(self.records)}

    def record_paper_entry(self, mint: str, *, now: float | None = None) -> list[str]:
        """Attribute a completed paper entry to previously recorded discovery sources."""
        stamp = time.time() if now is None else now
        self.load(now=stamp)
        rec = self.records.get(mint)
        if rec is None: return []
        rec['paper_entries']=min(10000,rec.get('paper_entries',0)+1)
        rec['last']=stamp
        return list(rec.get('sources',[]))

    def source_metrics(self) -> dict:
        """Unique tracked mints per source, not total repeated scan events."""
        self.load()
        data = {}
        for source in SOURCE_NAMES:
            relevant = [r for r in self.records.values() if source in r['sources']]
            data[source] = {'hinted':len(relevant),
                            'first_checked':sum(r['checks'] > 0 for r in relevant),
                            'ever_market_passed':sum(r['passes'] > 0 for r in relevant),
                            'accelerating_recent':sum(r.get('accelerating', False) and 0<=time.time()-r.get('last_market_at',0)<=600 for r in relevant),
                            'screened_pool_observations':sum(r.get('observations',0) for r in relevant),
                            'paper_entries':sum(r.get('paper_entries',0) for r in relevant),
                            'last_rejections':dict(Counter(r['reason'] for r in relevant if r['status']=='rejected' and r['reason']).most_common(4))}
        return data

    def save(self, *, now: float | None = None, force: bool = False):
        stamp = time.time() if now is None else now
        if not self.loaded or self.read_only or (not force and stamp-self.last_saved < 45):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps({'version':1,'records':self.records,'near':self.near},separators=(',',':')),encoding='utf8')
        tmp.replace(self.path)
        self.last_saved = stamp
        return True

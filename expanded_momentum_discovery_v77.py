"""v7.7 additional independent Solana momentum discovery, PAPER hints ONLY.

Official feeds:
  Jupiter Tokens V2 (optional API key): toptraded/toptrending/toporganicscore
  Birdeye token list V3 (optional paid-plan API key): volume-ranked
  Raydium public API v3 (no key): highest-volume pools across ranked pages
  DexScreener (public): boosted/community lists; explicitly *promotional leads*
All feed mints are checked against DexScreener's current actual SOL-quoted
liquidity pool before they enter discovery. Final trading checks remain in bot.py.
No signatures, trades, execution permission or wallet access.
"""
from __future__ import annotations
from collections import Counter
import json
import math
from pathlib import Path
import time
from typing import Any, Callable
from rolling_discovery import MINT_PATTERN
from discovery_expansion import search_pool_hints
from early_watch_v75 import from_dex_pairs

DEX_BASE = 'https://api.dexscreener.com'
JUP_BASE = 'https://api.jup.ag/tokens/v2'
BIRD_BASE = 'https://public-api.birdeye.so/defi/v3/token/list'
RAY_BASE = 'https://api-v3.raydium.io/pools/info/list'
FEED_TIMINGS = {'jupiter':120, 'birdeye':360, 'raydium_volume':240, 'dex_promotion_verified':180}
JUP_MODES = ('toptraded/5m','toptrending/5m','toptraded/5m','toporganicscore/1h', 'toptrending/5m','toptraded/1h')
BIRD_MODES = ('volume_1h_usd', 'volume_1h_change_percent', 'trade_1h_count')
DEX_MODES = ('token-boosts/latest/v1', 'token-boosts/top/v1', 'community-takeovers/latest/v1')


def _number(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return default
    return value if math.isfinite(value) else default


def _rank_jupiter(rows: Any, *, min_cap: float, max_cap: float) -> tuple[list[str], dict]:
    if not isinstance(rows, list):
        raise ValueError('unexpected_jupiter_schema')
    score = {}
    rejected = Counter()
    for row in rows[:120]:
        if not isinstance(row, dict):
            rejected['invalid_row'] += 1
            continue
        mint = row.get('id')
        if not isinstance(mint, str) or not MINT_PATTERN.fullmatch(mint):
            rejected['invalid_mint'] += 1
            continue
        cap = _number(row.get('mcap'), -1)
        liq = _number(row.get('liquidity'), -1)
        if 0 < cap < min_cap or cap > max_cap or (0 <= liq < 12000):
            rejected['cap_or_liquidity'] += 1
            continue
        stats = row.get('stats5m') or {}
        if not isinstance(stats, dict):
            stats = {}
        v5 = _number(stats.get('buyVolume')) + _number(stats.get('sellVolume'))
        buys = _number(stats.get('numBuys'))
        sells = _number(stats.get('numSells'))
        price5 = _number(stats.get('priceChange'))
        if price5 < 0.5 or v5 < 700 or buys < 8 or buys < sells * 1.05:
            rejected['not_fast_and_bought'] += 1
            continue
        organic = _number(row.get('organicScore'))
        score[mint] = max(score.get(mint, 0), 2000*min(price5, 15) + v5*1.5 +
                          200*min(buys, 300) + organic * 90)
    return sorted(score, key=score.get, reverse=True)[:30], dict(rejected)


def _rank_birdeye(payload: Any, *, min_cap: float, max_cap: float) -> tuple[list[str],dict]:
    data = payload.get('data') if isinstance(payload, dict) else None
    rows = (data.get('items', data.get('tokens')) if isinstance(data,dict) else None)
    if not isinstance(rows, list):
        raise ValueError('unexpected_birdeye_schema')
    score = {}
    rejected = Counter()
    for row in rows[:100]:
        if not isinstance(row, dict):
            rejected['invalid_row'] += 1
            continue
        mint = row.get('address')
        if not isinstance(mint, str) or not MINT_PATTERN.fullmatch(mint):
            rejected['invalid_mint'] += 1
            continue
        liq = _number(row.get('liquidity'), -1)
        cap = _number(row.get('market_cap'), -1)
        v1 = _number(row.get('volume_1h_usd'))
        change1 = _number(row.get('price_change_1h_percent'))
        if liq >= 0 and liq < 15000 or cap > 0 and not min_cap <= cap <= max_cap:
            rejected['cap_or_liquidity'] += 1
            continue
        if v1 > 0 and v1 < 6000 or change1 < -3:
            rejected['low_volume_or_falling'] += 1
            continue
        score[mint] = v1 + min(max(0, change1), 40)*2000 + max(0, liq)*0.2
    return sorted(score, key=score.get, reverse=True)[:30], dict(rejected)


def _rank_raydium(payload: Any, *, sol_mint: str, min_liquidity: float) -> tuple[list[str],dict]:
    if not isinstance(payload, dict) or payload.get('success') is not True:
        raise ValueError('unexpected_raydium_status')
    data = payload.get('data')
    rows = data.get('data') if isinstance(data,dict) else None
    if not isinstance(rows, list):
        raise ValueError('unexpected_raydium_schema')
    candidates = {}
    rejected = Counter()
    for row in rows[:100]:
        if not isinstance(row,dict):
            rejected['invalid_row'] += 1
            continue
        a=(row.get('mintA') or {}).get('address')
        b=(row.get('mintB') or {}).get('address')
        if a==sol_mint and isinstance(b,str):
            mint=b
        elif b==sol_mint and isinstance(a,str):
            mint=a
        else:
            rejected['not_exact_sol_pair'] += 1
            continue
        if not MINT_PATTERN.fullmatch(mint) or mint==sol_mint:
            rejected['invalid_mint'] += 1
            continue
        liq = _number(row.get('tvl'), -1)
        volume = _number((row.get('day') or {}).get('volume'))
        if liq < min_liquidity or volume < 12000:
            rejected['too_small_or_inactive'] += 1
            continue
        # 24h volume is NOT 5m momentum; DexScreener must verify it.
        candidates[mint] = max(candidates.get(mint, 0), volume + min(liq,250000)*0.5)
    return sorted(candidates, key=candidates.get, reverse=True)[:30],dict(rejected)


def _rank_promotions(rows: Any) -> tuple[list[str],dict]:
    if not isinstance(rows, list):
        raise ValueError('unexpected_dex_promotion_schema')
    mints = []
    counts = Counter()
    for row in rows:
        if not isinstance(row, dict) or row.get('chainId') != 'solana':
            counts['other_chain'] += 1
            continue
        mint = row.get('tokenAddress')
        if isinstance(mint, str) and MINT_PATTERN.fullmatch(mint) and mint not in mints:
            mints.append(mint)
        else:
            counts['invalid_or_repeat'] += 1
        if len(mints) >= 30:
            break
    return mints, dict(counts)


def verify_sol_momentum(pairs: Any, *, sol_mint: str, supported_dexes: set[str],
                        min_liquidity: float, min_age: float, min_cap: float,
                        max_cap: float, now: float) -> tuple[dict[str,float],dict[str,dict],dict]:
    """Fail-closed pair check; don't assume Jupiter/Birdeye/promotions are executable.

    A deliberately lightweight *discovery* momentum test is separate from the
    more stringent final fast-entry gate. Age-ineligible coins may be observed.
    """
    if not isinstance(pairs,list):
        raise ValueError('unexpected_dex_batch_schema')
    normalized_routes = frozenset(d.lower().replace('-', '').replace('_', '')
                                  for d in supported_dexes)
    rejects = Counter()
    near = from_dex_pairs(pairs, sol_mint=sol_mint, supported_dexes=supported_dexes,
                          min_age=min_age, min_cap=min_cap, max_cap=max_cap, now=now)
    # Verify each fast-moving pair INDIVIDUALLY. A liquid but slow Raydium
    # pool may not make a separate illiquid fast pool eligible for the mint.
    scores = {}
    for pair in pairs:
        if not isinstance(pair,dict):
            rejects['invalid_market_pair'] += 1
            continue
        pool_hints = search_pool_hints(
            [pair], sol_mint=sol_mint, supported_dexes=normalized_routes,
            min_liquidity_usd=min_liquidity, min_pair_age_minutes=min_age,
            min_cap_usd=min_cap, max_cap_usd=max_cap,
            now_ms=now*1000, rejections=rejects)
        base = (pair.get('baseToken') or {}).get('address')
        if base not in pool_hints:
            continue
        v5 = _number((pair.get('volume') or {}).get('m5'))
        change5 = _number((pair.get('priceChange') or {}).get('m5'))
        tx = (pair.get('txns') or {}).get('m5') or {}
        buys = _number(tx.get('buys'))
        sells = _number(tx.get('sells'))
        if not (0.6 <= change5 <= 22 and v5 >= 800 and buys >= 9 and buys >= sells*1.05):
            rejects['not_live_fast_momentum'] += 1
            continue
        score = pool_hints[base] + min(v5,200000) * 2 + min(change5, 15)*12000 + buys*60
        scores[base] = max(score, scores.get(base,0))
    return scores, near, dict(rejects)


class ExpandedMomentumDiscovery:
    """Bounded, persisted and key-aware independent discovery poller."""
    def __init__(self, path: Path, *, enabled=True, jupiter_key='', birdeye_key=''):
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.keys = {'jupiter':str(jupiter_key).strip(), 'birdeye':str(birdeye_key).strip()}
        self.loaded = False
        self.read_only = False
        self.rounds = {source:0 for source in FEED_TIMINGS}
        self.next_at = {source:0.0 for source in FEED_TIMINGS}
        self.backoff = {source:0.0 for source in FEED_TIMINGS}

    def load(self, now=None):
        if self.loaded:
            return
        self.loaded = True
        try:
            data = json.loads(self.path.read_text(encoding='utf8'))
        except FileNotFoundError:
            return
        except (ValueError, OSError, TypeError):
            self.read_only = True
            return
        if not isinstance(data,dict) or data.get('version') != 1:
            self.read_only = True
            return
        stamp = time.time() if now is None else now
        for source in FEED_TIMINGS:
            self.rounds[source] = max(0, min(1_000_000, int(_number((data.get('rounds') or {}).get(source)))))
            self.next_at[source] = max(0.0, min(stamp + FEED_TIMINGS[source], _number((data.get('next_at') or {}).get(source))))
            self.backoff[source] = max(0.0, min(stamp+86400, _number((data.get('backoff') or {}).get(source))))

    def save(self):
        if self.read_only:
            return
        self.path.parent.mkdir(parents=True,exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix+'.tmp')
        tmp.write_text(json.dumps({'version':1, 'rounds':self.rounds, 'next_at':self.next_at,
                                   'backoff':self.backoff},separators=(',',':')),encoding='utf8')
        tmp.replace(self.path)

    def poll(self, session, dex_get: Callable, *, sol_mint: str, supported_dexes: set[str],
             min_liquidity: float, min_age: float, min_cap: float, max_cap: float,
             now=None) -> tuple[dict[str,dict[str,float]],dict[str,dict[str,dict]],dict[str,dict]]:
        stamp = time.time() if now is None else now
        self.load(stamp)
        scores_by = {}
        near_by = {}
        health = {}
        for source in FEED_TIMINGS:
            scores_by[source] = {}
            near_by[source] = {}
            if not self.enabled:
                health[source] = {'status':'disabled'}
                continue
            if source in ('jupiter','birdeye') and not self.keys[source]:
                health[source] = {'status':'no_key_optional'}
                continue
            wait = max(self.next_at[source],self.backoff[source])-stamp
            if wait > 0:
                health[source] = {'status':'waiting','next_in_s':int(wait)}
                continue
            self.next_at[source] = stamp + FEED_TIMINGS[source]
            ranking = ''
            rejected = {}
            try:
                if source == 'jupiter':
                    ranking = JUP_MODES[self.rounds[source] % len(JUP_MODES)]
                    response = session.get(f'{JUP_BASE}/{ranking}',
                        params={'limit':100},headers={'x-api-key':self.keys[source], 'Accept':'application/json'},timeout=12)
                    response.raise_for_status()
                    mints,rejected = _rank_jupiter(response.json(),min_cap=min_cap,max_cap=max_cap)
                elif source == 'birdeye':
                    ranking = BIRD_MODES[self.rounds[source] % len(BIRD_MODES)]
                    response = session.get(BIRD_BASE,params={
                        'sort_by':ranking, 'sort_type':'desc', 'min_liquidity':min_liquidity,
                        'min_market_cap':min_cap,'max_market_cap':max_cap,'limit':100,
                        'offset':(self.rounds[source]//len(BIRD_MODES)%3)*100},
                        headers={'X-API-KEY':self.keys[source],'x-chain':'solana','Accept':'application/json'},timeout=12)
                    response.raise_for_status()
                    mints,rejected = _rank_birdeye(response.json(),min_cap=min_cap,max_cap=max_cap)
                elif source == 'raydium_volume':
                    page = 1 + (self.rounds[source] % 6)
                    ranking = f'volume24h_page_{page}'
                    response = session.get(RAY_BASE, params={
                        'poolType':'all','poolSortField':'volume24h','sortType':'desc',
                        'pageSize':100,'page':page}, headers={'Accept':'application/json'}, timeout=12)
                    response.raise_for_status()
                    mints,rejected = _rank_raydium(response.json(),sol_mint=sol_mint,
                                                   min_liquidity=min_liquidity)
                else:
                    ranking = DEX_MODES[self.rounds[source] % len(DEX_MODES)]
                    mints,rejected = _rank_promotions(dex_get(f'{DEX_BASE}/{ranking}'))
                # A discovery feed is never eligible by itself. Verify an actual
                # indexed, supported SOL pool in one bounded DexScreener batch.
                if mints:
                    pairs = dex_get(f'{DEX_BASE}/tokens/v1/solana/{",".join(mints[:30])}')
                    scores,near,market_rejected = verify_sol_momentum(
                        pairs, sol_mint=sol_mint, supported_dexes=supported_dexes,
                        min_liquidity=min_liquidity,min_age=min_age,min_cap=min_cap,
                        max_cap=max_cap,now=stamp)
                    scores_by[source] = scores
                    near_by[source] = near
                    rejected.update(market_rejected)
                self.rounds[source] += 1
                self.backoff[source] = 0.0
                health[source] = {'status':'ok','ranking':ranking,'raw_mints':len(mints),
                                  'verified_fast':len(scores_by[source]),
                                  'observation_only':len(near_by[source]),
                                  'rejected':rejected,
                                  'promotion_not_momentum':source=='dex_promotion_verified'}
            except Exception as exc:
                status_code = getattr(getattr(exc,'response',None),'status_code',None)
                if status_code in (401,402,403):
                    delay = 21600  # no repeated rejected-key charges
                elif status_code == 429 or '429' in str(exc) or 'cooldown' in str(exc).lower():
                    delay = 900
                else:
                    delay = 240
                self.backoff[source] = stamp + delay
                health[source] = {'status':'http_'+str(status_code) if status_code else type(exc).__name__,
                                  'retry_s':delay,'ranking':ranking}
            self.save()
        return scores_by, near_by, health

"""Independent DexPaprika Solana fast-mover discovery (PAPER hints only).

Free public API: https://api.dexpaprika.com/networks/solana/pools/search
Keyless quota is rolling 30 days. Poll no more often than every six minutes
by default (~7,200 calls/30 days); optional free key supports 90s refresh.
No quote, ranking, volume or token mint from this module authorizes a buy.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from rolling_discovery import MINT_PATTERN
import json
import math
import time

API = "https://api.dexpaprika.com/networks/solana/pools/search"
MODES = ("price_change_percentage_5m", "price_change_percentage_1h",
         "txns_24h", "volume_usd_24h", "created_at")


def number(value, default=0.0):
    try:
        x = float(value)
    except (ValueError, TypeError):
        return default
    return x if math.isfinite(x) else default


def extract_movers(payload, *, sol_mint: str, min_liquidity: float,
                   min_age_minutes: float, min_cap: float, max_cap: float,
                   now: float | None = None) -> tuple[dict[str, float], dict[str, int]]:
    """Reject malformed/mismatched records. Exact SOL token pair is required."""
    stamp = time.time() if now is None else now
    scores: dict[str, float] = {}
    rejections: dict[str, int] = {}

    def reject(reason):
        rejections[reason] = rejections.get(reason, 0) + 1

    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("unexpected_dexpaprika_schema")
    for pool in payload["results"][:100]:
        if not isinstance(pool, dict) or pool.get("chain") != "solana":
            reject("wrong_chain_or_schema")
            continue
        tokens = pool.get("tokens")
        if not isinstance(tokens, list) or len(tokens) != 2 or not all(isinstance(t, dict) for t in tokens):
            reject("missing_pair_tokens")
            continue
        ids = [str(t.get("id") or "") for t in tokens]
        if ids.count(sol_mint) != 1:
            reject("not_sol_quoted_pool")
            continue
        other_idx = 1 - ids.index(sol_mint)
        mint = ids[other_idx]
        if not MINT_PATTERN.fullmatch(mint):
            reject("invalid_mint")
            continue
        liq = number(pool.get("liquidity_usd"), -1)
        if liq < min_liquidity:
            reject("low_liquidity")
            continue
        created = pool.get("created_at")
        if not isinstance(created, str):
            reject("unknown_pool_age")
            continue
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if created_dt.tzinfo is None:
                raise ValueError("missing timezone")
            age_minutes = (stamp - created_dt.timestamp()) / 60
        except (ValueError, OverflowError):
            reject("invalid_pool_age")
            continue
        if age_minutes < min_age_minutes or age_minutes > 5 * 365 * 24 * 60:
            reject("too_young_or_invalid")
            continue
        # FDV can differ significantly from verified circulating market cap.
        # Use only as an early exclusion when supplied; DexScreener verifies
        # actual market cap and supported pool before any paper entry.
        fdv = number(tokens[other_idx].get("fdv"), -1)
        if fdv > 0 and not min_cap <= fdv <= max_cap:
            reject("fdv_outside_range")
            continue
        change5 = number(pool.get("price_change_percentage_5m"), -999.0)
        change1 = number(pool.get("price_change_percentage_1h"), -999.0)
        if change5 <= 0 and change1 <= 0:
            reject("not_rising")
            continue
        vol = max(0.0, number(pool.get("volume_usd_24h")))
        tx = max(0.0, number(pool.get("transactions_24h")))
        # Discovery ranking only; large outliers cannot dominate indefinitely.
        score = (min(liq, 250_000) + min(vol / 24, 300_000) * 2
                 + min(tx / 24, 4000) * 12
                 + min(max(0, change5), 15) * 2500
                 + min(max(0, change1), 40) * 700)
        scores[mint] = max(scores.get(mint, 0.0), score)
    return scores, rejections


def extract_near_eligible(payload, *, sol_mint: str, min_liquidity: float,
                          min_age_minutes: float, min_cap: float, max_cap: float,
                          now: float | None = None) -> dict[str, dict]:
    """Early *observation only*: young or near-min-cap liquid SOL pools.

    These mints are NEVER directly entered into the fresh trading batch until
    the scheduled retry, where all normal independent DexScreener gates apply.
    """
    stamp = time.time() if now is None else now
    result = {}
    if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
        return result
    for pool in payload['results'][:100]:
        if not isinstance(pool, dict) or pool.get('chain') != 'solana':
            continue
        tokens = pool.get('tokens')
        if not isinstance(tokens, list) or len(tokens) != 2 or not all(isinstance(t,dict) for t in tokens):
            continue
        ids = [str(t.get('id') or '') for t in tokens]
        if ids.count(sol_mint) != 1:
            continue
        other = tokens[1-ids.index(sol_mint)]
        mint = str(other.get('id') or '')
        if not MINT_PATTERN.fullmatch(mint):
            continue
        liquidity = number(pool.get('liquidity_usd'),-1)
        fdv = number(other.get('fdv'), -1)
        if liquidity < max(min_liquidity, 25000.0) or fdv < max(100000.0, min_cap*0.6) or fdv > max_cap:
            continue
        try:
            created = datetime.fromisoformat(str(pool['created_at']).replace('Z','+00:00'))
            if created.tzinfo is None:
                continue
            age = (stamp-created.timestamp())/60
        except (ValueError, KeyError, TypeError, OverflowError):
            continue
        if not 20 <= age <= 180:
            continue
        if number(pool.get('price_change_percentage_5m')) <= 0 and number(pool.get('price_change_percentage_1h')) <= 0:
            continue
        young = age < min_age_minutes
        near_cap = fdv < min_cap
        if not (young or near_cap):
            continue
        due = (stamp + max(0, min_age_minutes-age)*60 + 15) if young else stamp+600
        result[mint] = {'due':due, 'reason':('age_watch' if young else 'cap_watch'),
                        'age_minutes':round(age,1), 'indicative_fdv_usd':fdv}
    return result


class IndependentMovers:
    """Persisted rotating ranking searches with rolling-quota safeguards."""
    def __init__(self, path: Path, *, enabled: bool = True, api_key: str = ""):
        self.path = Path(path)
        self.enabled = enabled
        self.api_key = api_key.strip()
        self.interval = 90 if self.api_key else 360
        self.loaded = False
        self.read_only = False
        self.round = 0
        self.last_near: dict[str, dict] = {}
        self.next_at = 0.0
        self.backoff_until = 0.0
        self.cursor_by_mode: dict[str, str] = {}

    def load(self):
        if self.loaded:
            return
        self.loaded = True
        try:
            data = json.loads(self.path.read_text(encoding="utf8"))
        except FileNotFoundError:
            return
        except (ValueError, TypeError, OSError):
            self.read_only = True
            return
        if not isinstance(data, dict) or data.get("version") != 1:
            self.read_only = True
            return
        self.round = min(10000000, max(0, int(number(data.get("round")))))
        now = time.time()
        self.next_at = max(0, min(now + self.interval, number(data.get("next_at"))))
        self.backoff_until = max(0, min(now + 86400 * 7, number(data.get("backoff_until"))))
        cursors = data.get("cursors", {})
        if isinstance(cursors, dict):
            self.cursor_by_mode = {k: v for k, v in cursors.items()
                                   if k in MODES and isinstance(v, str) and 0 < len(v) <= 1024}

    def save(self):
        if self.read_only:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps({"version": 1, "round": self.round,
                                    "next_at": self.next_at, "backoff_until": self.backoff_until,
                                    "cursors": self.cursor_by_mode}, separators=(",", ":")), encoding="utf8")
        temp.replace(self.path)

    def poll(self, session, *, sol_mint: str, min_liquidity: float,
             min_age_minutes: float, min_cap: float, max_cap: float,
             now: float | None = None) -> tuple[dict[str, float], dict]:
        stamp = time.time() if now is None else now
        self.load()
        if not self.enabled:
            return {}, {"status": "disabled", "new": 0}
        if stamp < max(self.next_at, self.backoff_until):
            return {}, {"status": "waiting", "wait_s": int(max(self.next_at, self.backoff_until) - stamp)}

        mode = MODES[self.round % len(MODES)]
        params = {"order_by": mode, "sort": "desc", "limit": 100,
                  "liquidity_usd_min": min_liquidity}
        if self.cursor_by_mode.get(mode):
            params["cursor"] = self.cursor_by_mode[mode]
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = self.api_key  # never log this header
        self.next_at = stamp + self.interval  # fail closed on exceptions
        try:
            response = session.get(API, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            payload = response.json()
            self.last_near = extract_near_eligible(payload, sol_mint=sol_mint,
                min_liquidity=min_liquidity, min_age_minutes=min_age_minutes,
                min_cap=min_cap, max_cap=max_cap, now=stamp)
            scores, rejected = extract_movers(payload, sol_mint=sol_mint,
                min_liquidity=min_liquidity, min_age_minutes=min_age_minutes,
                min_cap=min_cap, max_cap=max_cap, now=stamp)
            next_cursor = payload.get("next_cursor") if payload.get("has_next_page") else None
            if isinstance(next_cursor, str) and 0 < len(next_cursor) <= 1024:
                self.cursor_by_mode[mode] = next_cursor
            else:
                self.cursor_by_mode.pop(mode, None)
            self.round += 1
            self.save()
            return scores, {"status": "ok", "mode": mode, "count": len(scores),
                            "near":len(self.last_near),
                            "rejected": rejected, "cursor_advanced": bool(next_cursor)}
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", 0)
            if status == 429:
                raw = getattr(getattr(exc, "response", None), "headers", {}).get("Retry-After", "360")
                self.backoff_until = stamp + max(60, min(3600, int(number(raw, 360))))
            elif status == 402:
                self.backoff_until = stamp + 86400 * 7  # exhausted rolling credits: stop repeatedly retrying
            else:
                self.backoff_until = stamp + 300
            self.round += 1
            self.save()
            return {}, {"status": "backoff", "error": f"HTTP{status}" if status else type(exc).__name__,
                        "retry_s": int(self.backoff_until - stamp)}

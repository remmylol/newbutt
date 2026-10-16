"""Conservative, independent GeckoTerminal Solana pool discovery.

Discovery hints only: execution eligibility is ALWAYS rechecked with DexScreener.
"""
from __future__ import annotations
import math
from collections import Counter
from datetime import datetime, timezone
from typing import Any


def _number(value: Any) -> float:
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _token_address(rel: dict, included: dict[str, dict]) -> str:
    record = (rel or {}).get("data") or {}
    token = included.get(record.get("id"), {})
    address = (token.get("attributes") or {}).get("address")
    if isinstance(address, str) and address:
        return address
    identifier = record.get("id", "")
    return identifier[len("solana_"):] if isinstance(identifier, str) and identifier.startswith("solana_") else ""


def extract_quality_pool_mints(payload: dict, *, sol_mint: str,
                               supported_dexes: frozenset[str],
                               min_liquidity_usd: float,
                               min_volume_1h_usd: float,
                               sniper_max_age_minutes: float | None = None,
                               sniper_min_volume_5m_usd: float | None = None,
                               sniper_min_change_5m_pct: float | None = None,
                               sniper_min_buy_sell_ratio: float = 1.3,
                               sniper_min_txns_5m: int = 12,
                               sniper_max_change_5m_pct: float = 80.0,
                               sniper_min_age_minutes: float = 1.0,
                               discovery_stage: str = "strict",
                               discovery_min_market_cap_usd: float = 0.0,
                               rejection_counts: Counter | None = None) -> dict[str, float]:
    """Select SOL-quoted, supported-DEX, priced liquid pools, by base mint.

    Never assume symbols are authoritative; compare the exact SOL mint.
    Some GeckoTerminal DEX ids have a `solana_` prefix. Unknowns fail closed.
    """
    if not isinstance(payload, dict):
        return {}
    included = {i.get("id"): i for i in payload.get("included", [])
                if isinstance(i, dict) and isinstance(i.get("id"), str)}
    results: dict[str, float] = {}
    def reject(reason: str) -> None:
        if rejection_counts is not None:
            rejection_counts[reason] += 1
    for pool in payload.get("data", []):
        if not isinstance(pool, dict):
            reject("invalid_pool_record")
            continue
        attr = pool.get("attributes") or {}
        rel = pool.get("relationships") or {}
        dex_rec = ((rel.get("dex") or {}).get("data") or {})
        dex = str(dex_rec.get("id", "")).lower()
        if dex.startswith("solana_"):
            dex = dex[7:]
        # GeckoTerminal may use e.g. pump_swap; normalize punctuation only.
        dex = dex.replace("_", "").replace("-", "")
        if dex not in supported_dexes:
            reject("unsupported_dex")
            continue
        base = _token_address(rel.get("base_token"), included)
        quote = _token_address(rel.get("quote_token"), included)
        if not base or base == sol_mint or quote != sol_mint:
            reject("not_base_token_sol_quote")
            continue
        liq = _number(attr.get("reserve_in_usd"))
        v1h = _number((attr.get("volume_usd") or {}).get("h1"))
        price = _number(attr.get("base_token_price_usd"))
        if liq < min_liquidity_usd:
            reject("below_minimum_liquidity")
            continue
        if v1h < min_volume_1h_usd:
            reject("below_minimum_hour_volume")
            continue
        if price <= 0:
            reject("missing_price")
            continue
        if discovery_stage == "continuation" and discovery_min_market_cap_usd > 0:
            # GeckoTerminal sometimes supplies market-cap/FDV directly. When
            # present, avoid spending scarce market lookups on tiny tokens.
            # Never infer missing capitalization from liquidity or price.
            cap = _number(attr.get("market_cap_usd")) or _number(attr.get("fdv_usd"))
            if cap > 0 and cap < discovery_min_market_cap_usd:
                reject("below_established_cap")
                continue
        if sniper_max_age_minutes is not None:
            # Only prioritize pools for which the upstream age is known.
            created = attr.get("pool_created_at")
            try:
                created_at = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                age_minutes = (datetime.now(timezone.utc) - created_at).total_seconds() / 60.0
            except (TypeError, ValueError, OverflowError):
                reject("missing_or_invalid_pool_age")
                continue
            if age_minutes < sniper_min_age_minutes:
                reject("too_new")
                continue
            if age_minutes > sniper_max_age_minutes:
                reject("too_old")
                continue
            v5m = _number((attr.get("volume_usd") or {}).get("m5"))
            if discovery_stage != "watch" and v5m < (sniper_min_volume_5m_usd or 0):
                reject("low_5m_volume")
                continue
            change_5m = attr.get("price_change_percentage") or {}
            try:
                momentum = float(change_5m.get("m5"))
            except (TypeError, ValueError):
                if discovery_stage != "watch":
                    reject("missing_momentum")
                    continue
                momentum = 0.0
            if discovery_stage != "watch" and (not math.isfinite(momentum) or not (sniper_min_change_5m_pct or 0) <= momentum <= sniper_max_change_5m_pct):
                reject("momentum_out_of_range")
                continue
            if not math.isfinite(momentum):
                momentum = 0.0
            txns = ((attr.get("transactions") or {}).get("m5") or {})
            buys = _number(txns.get("buys"))
            sells = _number(txns.get("sells"))
            # No inferred activity: require reported buys AND sells for a meaningful ratio.
            if discovery_stage != "watch" and (buys + sells < sniper_min_txns_5m or sells <= 0 or buys / sells < sniper_min_buy_sell_ratio):
                reject("trade_flow_insufficient")
                continue
        score = min(liq, 250000.0) + 2.0 * min(v1h, 250000.0)
        if discovery_stage == "continuation":
            # Discovery PRIORITY only. Final entry independently re-fetches
            # DexScreener market cap, trend, same-pool liquidity and sell depth.
            # GeckoTerminal's price-change fields are optional; missing values
            # never become permission to trade.
            changes = attr.get("price_change_percentage") or {}
            def change_value(key: str) -> float | None:
                try:
                    v = float(changes.get(key))
                    return v if math.isfinite(v) else None
                except (TypeError, ValueError):
                    return None
            m5, h1, h6, h24 = (change_value(k) for k in ("m5", "h1", "h6", "h24"))
            # These are PRIORITY hints only. The entry filter still enforces
            # the complete trend and price/volume rules from fresh market data.
            if h1 is not None and h6 is not None:
                if h1 < 0 or h6 < 0 or (h24 is not None and h24 < 0):
                    score *= 0.1
                else:
                    score += 8500.0 * min(h1, 15.0) + 3000.0 * min(h6, 25.0)
            if m5 is not None:
                score += 4000.0 * max(-2.0, min(m5, 5.0))
            score += 14000.0 * min(v1h / max(liq, 1.0), 4.0)
        if sniper_max_age_minutes is not None:
            score += 10.0 * min(v5m, 100000.0)
            score += 2500.0 * min(buys / max(sells, 1.0), 4.0)
            score += 1000.0 * max(0.0, min(momentum, 25.0))
            score += max(0.0, sniper_max_age_minutes - age_minutes) * 500.0
        results[base] = max(score, results.get(base, 0.0))
    return results

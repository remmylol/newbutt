"""Read-only discovery hints from Pump.fun board feeds.

These endpoints are reverse engineered, can require a JWT, and may change.
Never use a board response for pricing, market validation or order execution.
"""
from urllib.parse import urlparse

ALLOWED_HOSTS = {"advanced-api-v2.pump.fun", "frontend-api-v3.pump.fun"}
MOVERS_URL = "https://advanced-api-v2.pump.fun/boards/movers"
# This is a top-runners proxy, not a verified exact replica of the UI's Trending tab.
TRENDING_PROXY_URL = "https://frontend-api-v3.pump.fun/coins/top-runners"


def valid_pumpfun_endpoint(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme == "https" and p.hostname in ALLOWED_HOSTS and not p.username and not p.password
    except (ValueError, TypeError):
        return False


def board_mints(payload: object, *, limit: int = 50) -> list[str]:
    """Fail closed on unfamiliar shapes; never reinterpret arbitrary strings as mints."""
    if limit <= 0:
        return []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = None
        for key in ("coins", "tokens", "results", "items", "data", "movers", "trending"):
            value = payload.get(key)
            if isinstance(value, list):
                items = value
                break
            if isinstance(value, dict):
                for nested in ("coins", "tokens", "items", "results", "movers", "trending"):
                    if isinstance(value.get(nested), list):
                        items = value[nested]
                        break
                if items is not None:
                    break
        if items is None:
            return []
    else:
        return []
    seen = set()
    result = []
    alphabet = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    for item in items:
        if not isinstance(item, dict):
            continue
        nested = item.get("coin") or item.get("token") or {}
        if not isinstance(nested, dict):
            nested = {}
        mint = item.get("mint") or item.get("mintAddress") or item.get("tokenAddress") or nested.get("mint") or nested.get("address")
        if not isinstance(mint, str) or not (32 <= len(mint) <= 44) or not set(mint) <= alphabet or mint in seen:
            continue
        seen.add(mint)
        result.append(mint)
        if len(result) >= limit:
            break
    return result

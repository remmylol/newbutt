"""Read-only discovery hints from Pump.fun board feeds.

Unofficial endpoint; response shape and authentication may change.
Returned mints are untrusted hints, never trading signals.
"""
from __future__ import annotations

from collections import deque
from urllib.parse import urlparse

ALLOWED_HOSTS = {"advanced-indexer.pump.fun", "frontend-api-v3.pump.fun"}
MOVERS_URL = "https://advanced-indexer.pump.fun/boards/movers"
TRENDING_PROXY_URL = "https://frontend-api-v3.pump.fun/coins/top-runners"

# Only exact field names for token mints. Never treat arbitrary string values,
# wallet addresses, or social/user identifiers as coin addresses.
_MINT_FIELDS = ("mint", "mintAddress", "tokenAddress", "token_mint",
                "tokenMint", "coinMint", "coin_mint", "baseMint", "base_mint")
_TOKEN_NESTED_FIELDS = ("coin", "token", "baseToken", "base_token", "asset")
_LIST_OF_MINTS_FIELDS = {"mints", "mintAddresses", "tokenMints"}
_SAFE_SCHEMA_KEYS = frozenset({
    "board", "version", "state", "snapshot", "data", "results", "items",
    "coins", "tokens", "entries", "rows", "movers", "trending", "payload",
    "updates", "changes", "added", "removed", "order", "ids", "mints",
    "mintAddresses", "tokenMints", "error", "success", "status", "message",
})
_ALPHABET = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
_MAX_NODES = 4000
_MAX_DEPTH = 9


def valid_pumpfun_endpoint(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme == "https" and p.hostname in ALLOWED_HOSTS and not p.username and not p.password
    except (ValueError, TypeError):
        return False


def _valid_mint(value: object) -> bool:
    return isinstance(value, str) and 32 <= len(value) <= 44 and all(ch in _ALPHABET for ch in value)


def _tokens_in_tree(payload: object, *, limit: int = 50) -> tuple[list[str], dict[str, int]]:
    """Bounded traversal; extract ONLY explicit mint/token fields.

    Supports nested board snapshots, updates and key-indexed objects without
    accepting arbitrary user/wallet addresses from response text. Never logs
    or displays the coin payload itself.
    """
    stats = dict(objects=0, arrays=0, nodes=0, mint_fields=0, valid_mint_fields=0,
                 direct_mint_lists=0, truncated=0)
    if limit <= 0:
        return [], stats
    seen: set[str] = set()
    result: list[str] = []
    to_visit = deque([(payload, 0, None)])
    while to_visit and stats['nodes'] < _MAX_NODES:
        node, depth, source = to_visit.popleft()
        stats['nodes'] += 1
        if depth > _MAX_DEPTH:
            stats['truncated'] += 1
            continue
        if isinstance(node, dict):
            stats['objects'] += 1
            for field in _MINT_FIELDS:
                if field in node:
                    stats['mint_fields'] += 1
                    mint = node[field]
                    if _valid_mint(mint):
                        stats['valid_mint_fields'] += 1
                        if mint not in seen:
                            seen.add(mint)
                            result.append(mint)
            if source in _TOKEN_NESTED_FIELDS:
                mint = node.get('address')
                if _valid_mint(mint) and mint not in seen:
                    seen.add(mint)
                    result.append(mint)
            if len(result) >= limit:
                return result[:limit], stats
            if depth < _MAX_DEPTH:
                for key, val in node.items():
                    if isinstance(val, (dict, list)):
                        to_visit.append((val, depth+1, key))
                    elif key in _LIST_OF_MINTS_FIELDS and _valid_mint(val):
                        stats['direct_mint_lists'] += 1
                        if val not in seen:
                            seen.add(val)
                            result.append(val)
        elif isinstance(node, list):
            stats['arrays'] += 1
            if source in _LIST_OF_MINTS_FIELDS:
                for mint in node[:100]:
                    if _valid_mint(mint) and mint not in seen:
                        seen.add(mint)
                        result.append(mint)
                        stats['direct_mint_lists'] += 1
                        if len(result) >= limit:
                            return result[:limit], stats
            if depth < _MAX_DEPTH:
                for val in node[:500]:
                    if isinstance(val, (dict, list)):
                        to_visit.append((val, depth+1, source))
        if len(result) >= limit:
            return result[:limit], stats
    if to_visit:
        stats['truncated'] += 1
    return result[:limit], stats


def board_mints(payload: object, *, limit: int = 50) -> list[str]:
    return _tokens_in_tree(payload, limit=limit)[0]


def safe_board_shape(payload: object, *, limit: int = 50) -> dict[str, object]:
    """Non-sensitive diagnostics only; never includes payload values or IDs."""
    _, stats = _tokens_in_tree(payload, limit=limit)
    top_keys = ()
    if isinstance(payload, dict):
        top_keys = tuple(sorted(k for k in payload if isinstance(k, str) and k in _SAFE_SCHEMA_KEYS))
    state_kind = 'absent'
    if isinstance(payload, dict) and 'state' in payload:
        val = payload['state']
        state_kind = 'dict' if isinstance(val, dict) else 'list' if isinstance(val, list) else 'scalar'
    return {
        'top_type': 'dict' if isinstance(payload, dict) else 'list' if isinstance(payload, list) else 'scalar',
        'recognized_top_keys': ','.join(top_keys) or 'none',
        'state_type': state_kind,
        **stats,
    }

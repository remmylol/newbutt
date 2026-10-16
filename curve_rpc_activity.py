"""Read-only, rate-bounded Pump.fun bonding-curve trade observations via Solana JSON-RPC.

No PumpPortal paid trade subscription, wallet signer, or transaction submission.
Uses getSignaturesForAddress(curve PDA) then getTransaction to decode Anchor's
TradeEvent (self-CPI and legacy Program data logs). Incomplete RPC history
is diagnosed; observed activity is NOT an exhaustive on-chain trade tape.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import struct
import threading
import time
from collections import OrderedDict
from typing import Callable

import requests

from pump_curve_market import PUMP_PROGRAM_ID

LOG = logging.getLogger("meme-bot")
TRADE_DISC = hashlib.sha256(b"event:TradeEvent").digest()[:8]
SELF_CPI_TAG = bytes.fromhex("e445a52e51cb9a1d")
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58_decode(data: str) -> bytes:
    """Decode the base58 inner instruction data with no additional dependency."""
    n = 0
    for character in data:
        n = n * 58 + BASE58_ALPHABET.index(character)
    zeros = len(data) - len(data.lstrip("1"))
    return b"\x00" * zeros + (n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b"")


def b58_encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    chars = []
    while n:
        n, rem = divmod(n, 58)
        chars.append(BASE58_ALPHABET[rem])
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + "".join(reversed(chars))


def decode_trade_event(raw: bytes, *, expected_mint: str) -> dict | None:
    """Decode immutable prefix of Pump.fun TradeEvent; ignore append-only fields.

    Only exact Anchor discriminators are accepted. SOL volume must be verified
    against SOL-quoted markets before interpreting it as SOL for exotic quotes.
    """
    if raw.startswith(SELF_CPI_TAG + TRADE_DISC):
        payload = raw[16:]
    elif raw.startswith(TRADE_DISC):
        payload = raw[8:]
    else:
        return None
    # mint[32], quote amount[u64], token amount[u64], buy[bool], user[32]
    if len(payload) < 81 or payload[48] not in (0, 1):
        return None
    if b58_encode(payload[:32]) != expected_mint:
        return None
    quote_amount, token_amount = struct.unpack_from("<QQ", payload, 32)
    if quote_amount <= 0 or token_amount <= 0:
        return None
    # Pump.fun legacy SOL-quoted event field. In the V2/V3 quote-token era,
    # this can be non-SOL quote units. Never use this measurement to unlock
    # entries without separately establishing quote_mint == wrapped SOL.
    return {"mint": expected_mint, "txType": "buy" if payload[48] == 1 else "sell",
            "solAmount": quote_amount / 1_000_000_000,
            "quote_amount_raw": quote_amount,
            "token_amount_raw": token_amount}


def _program_keys(tx: dict) -> list[str]:
    message = ((tx.get("transaction") or {}).get("message") or {})
    meta = tx.get("meta") or {}
    keys = [key.get("pubkey") if isinstance(key, dict) else key
            for key in (message.get("accountKeys") or [])]
    loaded = meta.get("loadedAddresses") or {}
    keys.extend(loaded.get("writable") or [])
    keys.extend(loaded.get("readonly") or [])
    return keys


def extract_curve_trades(tx: dict | None, mint: str) -> list[dict]:
    """Extract real successful Pump TradeEvents, preferring CPI to log copies.

    Both CPI and Program data events may appear for the same trade; using CPI
    as primary and logs only if absent prevents double-counting.
    """
    if not isinstance(tx, dict):
        return []
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    keys = _program_keys(tx)
    if PUMP_PROGRAM_ID not in keys:
        return []
    trades = []
    for block in meta.get("innerInstructions") or []:
        for ix in block.get("instructions") or []:
            program_id = ix.get("programId")
            if not program_id and isinstance(ix.get("programIdIndex"), int):
                ix_idx = ix["programIdIndex"]
                program_id = keys[ix_idx] if 0 <= ix_idx < len(keys) else None
            if program_id != PUMP_PROGRAM_ID:
                continue
            raw_b58 = ix.get("data")
            if not isinstance(raw_b58, str):
                continue
            try:
                event = decode_trade_event(b58_decode(raw_b58), expected_mint=mint)
            except (ValueError, OverflowError):
                continue
            if event:
                trades.append(event)
    if trades:
        return trades

    # Legacy emit! events: only consider logs emitted while Pump is active.
    # The stack tracks nested program invocations so unrelated logs can't spoof it.
    program_stack = []
    for line in meta.get("logMessages") or []:
        if not isinstance(line, str):
            continue
        if line.startswith("Program ") and " invoke [" in line:
            program_stack.append(line.split(" ", 2)[1])
        elif line.startswith("Program ") and (line.endswith(" success") or " failed:" in line):
            if program_stack:
                program_stack.pop()
        elif line.startswith("Program data: ") and program_stack and program_stack[-1] == PUMP_PROGRAM_ID:
            try:
                decoded = base64.b64decode(line[len("Program data: "):], validate=True)
            except (ValueError, base64.binascii.Error):
                continue
            event = decode_trade_event(decoded, expected_mint=mint)
            if event:
                trades.append(event)
    return trades


class RpcTradeError(RuntimeError):
    def __init__(self, code: int | str, message: str):
        super().__init__(message)
        self.code = code


class ReadOnlySolanaRPC:
    """Whitelisted read-only RPC methods, never logging URLs/API secrets."""

    ALLOWED_METHODS = {"getSignaturesForAddress", "getTransaction"}

    def __init__(self, rpc_url: str, timeout: float = 9):
        if not rpc_url.startswith(("https://", "http://")):
            raise ValueError("A valid SOLANA_RPC_URL is required")
        self.url = rpc_url
        self.timeout = timeout
        self.session = requests.Session()

    def __call__(self, method: str, params: list):
        if method not in self.ALLOWED_METHODS:
            raise ValueError("RPC method is not allowed for activity observer")
        response = self.session.post(self.url, json={"jsonrpc": "2.0", "id": 1,
                                                     "method": method, "params": params}, timeout=self.timeout)
        if response.status_code == 429:
            raise RpcTradeError(429, "RPC rate limited")
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            err = payload["error"]
            raise RpcTradeError(err.get("code", "unknown"), str(err.get("message", "RPC error"))[:120])
        return payload.get("result")


def bonding_curve_address(mint: str) -> str:
    from solders.pubkey import Pubkey
    program = Pubkey.from_string(PUMP_PROGRAM_ID)
    addr, _ = Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))], program)
    return str(addr)


class RpcCurveTradeTracker:
    """Incremental polling with bounded work, skipped-history diagnostics and dedup.

    Each poll fetches a limited slice of newest signatures per selected mint,
    then processes a bounded number of queued transactions. When activity is
    extremely heavy, missing signatures are possible and reported as gaps;
    trade counts must be interpreted as observed minima rather than complete.
    """

    def __init__(self, rpc: Callable, activity, *, address_fn: Callable = bonding_curve_address,
                 signature_limit: int = 12, transaction_limit: int = 5, lookback_seconds: int = 300,
                 max_pending: int = 160):
        self.rpc = rpc
        self.activity = activity
        self.address_fn = address_fn
        self.signature_limit = max(2, min(50, int(signature_limit)))
        self.transaction_limit = max(1, min(25, int(transaction_limit)))
        self.lookback_seconds = min(300, max(60, int(lookback_seconds)))
        self.max_pending = max(16, int(max_pending))
        self.cursor: OrderedDict[str, str] = OrderedDict()
        self.pending: OrderedDict[str, tuple[str, int]] = OrderedDict()
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.retry: dict[str, tuple[int, float]] = {}
        self.stats = {"poll_cycles": 0, "signature_scans": 0, "tx_fetched": 0, "valid_trades": 0,
                      "rpc_errors": 0, "rate_limits": 0, "history_gaps": 0,
                      "stale_trades": 0, "ignored_events": 0, "successful_scans": 0,
                      "tx_retries": 0, "tx_dropped": 0, "tx_null": 0,
                      "backpressure_cycles": 0, "signature_scans_deferred": 0,
                      "pending_peak": 0, "queue_overflow_drops": 0,
                      "rpc_gap_signatures": 0, "rpc_gap_overflow": 0,
                      "tx_error_codes": {},
                      "ignored_reasons": {}}
        self._scan_cursor = 0
        self.last_trade_monotonic = None
        self.last_success_monotonic = None
        self.cooldown_until = 0.
        self.last_error = None
        self._lock = threading.Lock()

    def _rpc(self, method: str, params: list):
        try:
            return self.rpc(method, params)
        except Exception as exc:
            self.stats["rpc_errors"] += 1
            code = getattr(exc, "code", None)
            if code == 429 or "429" in str(exc):
                self.stats["rate_limits"] += 1
                self.cooldown_until = time.monotonic() + 120
            # Only report numeric RPC codes and controlled labels; never leak endpoint URLs or keys.
            safe_code = code if isinstance(code, int) else "unknown"
            self.last_error = f"{method}:{type(exc).__name__}:code={safe_code}"
            codes = self.stats["tx_error_codes"]
            if method == "getTransaction":
                label = str(safe_code)
                codes[label] = codes.get(label, 0) + 1
            raise

    def poll(self, mints: list[str], *, now_wall: float | None = None, now_mono: float | None = None) -> None:
        wall = time.time() if now_wall is None else float(now_wall)
        mono = time.monotonic() if now_mono is None else float(now_mono)
        with self._lock:
            if mono < self.cooldown_until:
                return
            self.stats["poll_cycles"] += 1
            # Drop expired jobs before accepting fresh signatures. Exceeding a
            # five-minute activity window is a data coverage loss, not a zero.
            for signature, (_, block_time) in list(self.pending.items()):
                if block_time < wall - self.lookback_seconds or block_time > wall + 30:
                    self.pending.pop(signature, None)
                    self.retry.pop(signature, None)
                    self.stats["stale_trades"] += 1
            self.stats["pending_peak"] = max(self.stats["pending_peak"], len(self.pending))
            unique_mints = list(dict.fromkeys(mints))
            # Keep ingestion sustainable: no more than ~2x the transaction
            # processing budget and pause ingestion when queue is saturated.
            max_scans = max(1, self.transaction_limit * 2 // self.signature_limit)
            scan_slots = min(len(unique_mints), max_scans)
            if len(self.pending) >= int(self.max_pending * .75):
                self.stats["backpressure_cycles"] += 1
                scan_slots = 0
            if unique_mints:
                begin = self._scan_cursor % len(unique_mints)
                rotated = unique_mints[begin:] + unique_mints[:begin]
                self._scan_cursor = (begin + scan_slots) % len(unique_mints)
            else:
                rotated = []
            self.stats["signature_scans_deferred"] += len(rotated) - scan_slots
            for mint in rotated[:scan_slots]:
                try:
                    addr = self.address_fn(mint)
                    sigs = self._rpc("getSignaturesForAddress", [addr,
                                      {"limit": self.signature_limit, "commitment": "confirmed"}]) or []
                    self.stats["signature_scans"] += 1
                    self.stats["successful_scans"] += 1
                    self.last_success_monotonic = mono
                except Exception:
                    continue
                if not isinstance(sigs, list):
                    continue
                prev = self.cursor.get(mint)
                latest = next((x.get("signature") for x in sigs if isinstance(x, dict) and x.get("signature")), None)
                if latest:
                    self.cursor[mint] = latest
                    self.cursor.move_to_end(mint)
                # If the previous high-water mark fell out of the returned
                # limited history, this poll might have missed trades.
                returned = [row.get("signature") for row in sigs if isinstance(row, dict)]
                if prev and prev not in returned and len(sigs) >= self.signature_limit:
                    self.stats["history_gaps"] += 1
                    self.stats["rpc_gap_signatures"] += 1
                fresh = []
                for row in sigs:
                    if not isinstance(row, dict):
                        continue
                    signature = row.get("signature")
                    if signature == prev:
                        break
                    block_time = row.get("blockTime")
                    # Require authenticated timestamps and successful signatures.
                    if not isinstance(signature, str) or not isinstance(block_time, int):
                        continue
                    if row.get("err") is not None or block_time < wall - self.lookback_seconds or block_time > wall + 30:
                        continue
                    if signature not in self.seen and signature not in self.pending:
                        fresh.append((signature, block_time))
                for signature, block_time in reversed(fresh):
                    if len(self.pending) >= self.max_pending:
                        dropped, _ = self.pending.popitem(last=False)
                        self.retry.pop(dropped, None)
                        self.stats["history_gaps"] += 1
                        self.stats["rpc_gap_overflow"] += 1
                        self.stats["queue_overflow_drops"] += 1
                    self.pending[signature] = (mint, block_time)
                    self.stats["pending_peak"] = max(self.stats["pending_peak"], len(self.pending))
            # After a 429, do not make additional RPC calls during cooldown.
            if mono < self.cooldown_until:
                return
            # Newest-first prevents a flood of old pending signatures from
            # turning otherwise valid five-minute trades into stale observations.
            # A transient getTransaction error must not stall all other mints.
            tx_budget = self.transaction_limit
            for signature in list(reversed(self.pending)):
                if tx_budget <= 0 or mono < self.cooldown_until:
                    break
                mint, block_time = self.pending.get(signature, (None, None))
                if mint is None:
                    continue
                if block_time < wall - self.lookback_seconds or block_time > wall + 30:
                    self.pending.pop(signature, None)
                    self.retry.pop(signature, None)
                    self.stats["stale_trades"] += 1
                    continue
                attempts, retry_at = self.retry.get(signature, (0, 0.))
                if mono < retry_at:
                    continue
                tx_budget -= 1
                try:
                    tx = self._rpc("getTransaction", [signature, {"encoding": "json",
                                  "commitment": "confirmed", "maxSupportedTransactionVersion": 1}])
                except Exception as exc:
                    attempts += 1
                    if attempts >= 3:
                        self.pending.pop(signature, None)
                        self.retry.pop(signature, None)
                        self.stats["tx_dropped"] += 1
                        self.stats["history_gaps"] += 1
                    else:
                        self.retry[signature] = (attempts, mono + min(90., 10. * (2 ** (attempts - 1))))
                        self.stats["tx_retries"] += 1
                    continue
                if tx is None:
                    self.stats["tx_null"] += 1
                    attempts += 1
                    if attempts >= 3:
                        self.pending.pop(signature, None)
                        self.retry.pop(signature, None)
                        self.stats["tx_dropped"] += 1
                    else:
                        self.retry[signature] = (attempts, mono + min(90., 10. * (2 ** (attempts - 1))))
                    continue
                self.pending.pop(signature, None)
                self.retry.pop(signature, None)
                self.stats["tx_fetched"] += 1
                self.seen[signature] = None
                if len(self.seen) > 2000:
                    self.seen.popitem(last=False)
                tx_time = tx.get("blockTime") if isinstance(tx, dict) else None
                event_time = tx_time if isinstance(tx_time, int) else block_time
                age = wall - event_time
                if age > self.lookback_seconds or age < -30:
                    self.stats["stale_trades"] += 1
                    continue
                observed_at = mono - max(0., age)
                events = extract_curve_trades(tx, mint)
                if not events:
                    self.stats["ignored_events"] += 1
                    reason = "failed_transaction" if isinstance(tx, dict) and (tx.get("meta") or {}).get("err") is not None else ("missing_pump_program" if isinstance(tx, dict) and PUMP_PROGRAM_ID not in _program_keys(tx) else "no_matching_trade_event")
                    reasons = self.stats["ignored_reasons"]
                    reasons[reason] = reasons.get(reason, 0) + 1
                for event in events:
                    if self.activity.record(event, now=observed_at):
                        self.stats["valid_trades"] += 1
                        self.last_trade_monotonic = mono
            while len(self.cursor) > 250:
                self.cursor.popitem(last=False)

    def diagnostics(self) -> dict:
        with self._lock:
            age = (None if self.last_trade_monotonic is None else
                   round(time.monotonic() - self.last_trade_monotonic, 1))
            scan_age = (None if self.last_success_monotonic is None else
                        round(time.monotonic() - self.last_success_monotonic, 1))
            return {"source": "solana_rpc", **self.stats,
                    "pending_signatures": len(self.pending),
                    "retry_waiting": len(self.retry),
                    "tracked_cursors": len(self.cursor),
                    "last_trade_age_seconds": age,
                    "last_successful_scan_age_seconds": scan_age,
                    "last_error": self.last_error}

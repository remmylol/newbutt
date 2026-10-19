"""Read-only, reconnecting Solana accountSubscribe watcher for HELD age-track pools.

A WebSocket account notification is NOT a trade/price. This module only sends
an exact held-pool wake-up to a callback. The existing v8.7.6 AgeTrack.observe
is the ONLY code deciding exits using a freshly fetched pinned DexScreener pair.
No new filters, swaps, on-chain writes, or wallet credentials are supported.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger('meme-bot')


def ws_url_for_rpc(https_url: str, override: str = '') -> str:
    url = (override or https_url).strip()
    p = urlsplit(url)
    if p.scheme not in ('https', 'wss') or not p.netloc:
        raise ValueError('RPC endpoint must be HTTPS or WSS; custom WebSocket must be WSS')
    return urlunsplit(('wss', p.netloc, p.path, p.query, p.fragment))


class SolanaPoolWatcher:
    """One worker thread; callback receives mint/pool only from owned subscriptions."""
    def __init__(self, *, rpc_ws_url: str, held_pools, on_change,
                 reconnect_max_s: float = 30., refresh_s: float = 1.,
                 async_callback: bool = False):
        if not rpc_ws_url.startswith('wss://'):
            raise ValueError('WSS is required')
        self.url = rpc_ws_url
        self.held_pools = held_pools
        self.on_change = on_change
        self.reconnect_max_s = reconnect_max_s
        self.refresh_s = refresh_s
        self.async_callback=bool(async_callback)
        self._callback_lock=threading.Lock()
        self._callback_inflight=set()
        self.max_callbacks=5  # no unbounded worker spawning
        self._stop = threading.Event()
        self.thread = None
        self.notifications = 0
        self.reconnects = 0

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name='solana-held-pool-ws', daemon=True)
            self.thread.start()

    def stop(self):
        self._stop.set()

    def _deliver(self,mint,pool):
        if not self.async_callback:
            self.on_change(mint,pool)
            return
        key=(mint,pool)
        with self._callback_lock:
            if key in self._callback_inflight or len(self._callback_inflight)>=self.max_callbacks:
                return
            self._callback_inflight.add(key)
        def worker():
            try:
                self.on_change(mint,pool)
            except Exception:
                log.exception('LIVE 0_60 FAST WS wake_callback_error mint=%s',mint)
            finally:
                with self._callback_lock:self._callback_inflight.discard(key)
        threading.Thread(target=worker,name='live-held-ws-quote',daemon=True).start()

    def _run(self):
        try:
            import websocket
        except ImportError:
            log.error('PAPER PRIMARY WS disabled reason=websocket_client_missing; REST monitoring remains')
            return
        backoff = 1.
        while not self._stop.is_set():
            try:
                # Never print URL, which could contain a private RPC API token.
                ws = websocket.create_connection(self.url, timeout=5)
                try:
                    ws.settimeout(1.)
                    backoff = 1.
                    subscriptions = {}      # pool -> (request_id, mint)
                    subscription_ids = {}   # subscription_id -> (mint, pool)
                    pending = {}            # request_id -> (mint, pool)
                    next_id = 100
                    last_refresh = 0.
                    log.info('PAPER PRIMARY WS connected held_pool_watching=True wallet_readonly=True')
                    while not self._stop.is_set():
                        stamp = time.monotonic()
                        if stamp-last_refresh >= self.refresh_s:
                            last_refresh = stamp
                            desired = self.held_pools()  # mint -> pinned pool
                            wanted = {pool: mint for mint,pool in desired.items() if pool}
                            # Subscribe only to held pools. Reconnect on changes
                            # to keep map/subscription IDs unambiguous and simple.
                            if any(pool not in wanted or mint != wanted[pool]
                                   for pool,(_,mint) in subscriptions.items()):
                                break
                            for pool,mint in wanted.items():
                                if pool in subscriptions: continue
                                next_id += 1
                                pending[next_id] = (mint,pool)
                                subscriptions[pool] = (next_id,mint)
                                ws.send(json.dumps({'jsonrpc':'2.0','id':next_id,
                                     'method':'accountSubscribe',
                                     'params':[pool,{'encoding':'base64','commitment':'processed'}]}))
                        try:
                            raw = ws.recv()
                        except websocket.WebSocketTimeoutException:
                            continue
                        if not raw:
                            raise RuntimeError('Solana WS closed')
                        msg = json.loads(raw)
                        if not isinstance(msg, dict):continue
                        request_id = msg.get('id')
                        if request_id in pending:
                            mint,pool = pending.pop(request_id)
                            sid = msg.get('result')
                            if isinstance(sid,int):
                                subscription_ids[sid]=(mint,pool)
                            else:
                                log.warning('PAPER PRIMARY WS subscribe_unavailable pool=%s fallback_rest=True',pool)
                                subscriptions.pop(pool,None)
                            continue
                        if msg.get('method') == 'accountNotification':
                            sid = (msg.get('params') or {}).get('subscription')
                            match = subscription_ids.get(sid)
                            if match:
                                mint,pool = match
                                # The callback rechecks whether it is still held.
                                self.notifications += 1
                                try:self._deliver(mint,pool)
                                except Exception:
                                    log.exception('PAPER PRIMARY WS wake_callback_error mint=%s',mint)
                finally:
                    ws.close()
            except Exception as exc:
                log.warning('PAPER PRIMARY WS disconnected error_type=%s REST_fallback_active=True',type(exc).__name__)
            if not self._stop.is_set():
                self.reconnects += 1
                self._stop.wait(backoff)
                backoff = min(self.reconnect_max_s,backoff*2)

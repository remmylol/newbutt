# Clean Solana momentum paper bot — Railway setup

## What this package does

- **PAPER ONLY:** no wallet key, no real swaps, no account funding required.
- Reuses the most recent bot's discovery, entry, exit, reversal and confirmed-rug paper accounting logic.
- `clean_boot.py` forces a reviewed established-momentum profile **before importing** `bot.py`. Legacy Railway bot variables (including stale `MIN_SCORE`, entry limits, old `STATE_FILE` and live mode) are ignored.
- Uses older Solana pairs with ~$500K-$250M market cap / FDV proxy and reported SOL-quoted liquidity >= $15K. Requires positive multitimeframe momentum and fresh sellability checks. Successful historical backtests or profit are **not** implied.
- Unlike a real swap, a paper quote remains indicative; there is no verified on-chain execution and no guaranteed exit fill.

## Fresh install (recommended: brand-new Railway **service/project**)

1. Back up any current Railway state file and logs you want to keep. Stop/pause the old service so it doesn't keep scanning or writing to shared state. Do not delete it yet.
2. Create a **new GitHub repository** if you'd like a clean history (optional, but easiest if you want one obvious version). Unzip `boy_clean_railway_deploy.zip` and upload the **contents** of the folder at the repository root. Do **not** upload the zip as a single GitHub file.
3. In Railway, click **New Project > Deploy from GitHub Repo** and connect the new repository. Confirm the new service points at the new repository and **root directory** `/`.
4. Under **Service > Settings > Deploy**, set **Custom Start Command** to `python clean_boot.py` (no quotes). Railway uses Railpack to detect Python and install `requirements.txt` automatically. If your interface has different labels, look for the service's *Start Command* setting.
5. **Variables**: you do **not** have to import any of the old 111 variables. `clean_boot.py` pins the strategy settings. You may start with **zero strategy variables**. (If you add a persistent volume, the volume mount path is automatically exposed at runtime.)
6. **Strongly recommended:** add a Railway volume to this new service, mounted at `/data`. By default the launcher uses the mount location to keep `/data/clean_paper_state.json`. Confirm `CLEAN BOOT ... volume_mounted=True` in the logs. Without a volume, state may reset after redeploys.
7. Set the service to **one running replica**. Do not run two copies against the same state file. Deploy and open **Deploy Logs**.

## First five minutes: validation checklist

1. Find `CLEAN BOOT PROFILE=established_momentum_v1 PAPER_MODE=true LIVE_TRADING_ENABLED=false`.
2. Find `CLEAN BOOT STATE_FILE=... volume_mounted=True` (if volume added).
3. Find `ESTABLISHED MOMENTUM PAPER MODE cap=$500000-$250000000`.
4. Find `QUALITY POOLS discovered=... errors=...` and `DISCOVERY sources=...`. Zero pools or sustained errors points to upstream/network/DEX issues, *not* a missing market opportunity.
5. Find `ENTRY SCAN tokens=...`, `OPPORTUNITY FUNNEL ... dex_checked=... dex_eligible=...`, `FILTER SUMMARY...`, and the new `SCANNER HEALTH status=...`.
6. If `dex_eligible=0`, read `momentum_rejections` and `market_data_rejections`. Do **not** relax the safety or sellability checks just to force trades.
7. If `dex_eligible>0` but no paper buy, check `PAPER ENTRY DECISIONS`, `PAPER ENTRY SKIP`, `PAPER ENTRY REJECT`, including **entry trend confirmation**, **min score**, **independent same-pool depth**, cash and paper loss limits. An `eligible` market still isn't necessarily a buy.
8. If a qualified coin finally passes the second observation and same-pool independent checks, expect `PAPER SELLABILITY CHECK` then `PAPER BUY`, followed by position monitoring. If none appear, continue monitoring/filter diagnostics; no trades may be the correct outcome.

`SCANNER HEALTH` interpretation:

| Status | Meaning |
|---|---|
| `no_candidates_selected_or_cooldown` | Candidate list empty or all currently cooling; bounded cooldown probes still apply |
| `no_indexed_markets` | Discovery supplied mints but DexScreener did not return matching indexed pools |
| `market_data_or_pool_route_blocked` | Mostly missing/unusable price, same-pool liquidity, or SOL-route data |
| `momentum_filters_no_match` | Quotes received but cap/volume/change/flow didn't qualify |
| `candidates_passed_filters_pending_entry_checks` | Some passed market filters; second observation, score and paper sellability still apply |
| `token_lookup_unavailable` | Market-data API lookup failures, not evidence that pools have zero liquidity |

## Optional variables (new namespace only)

Do not bring over old variables such as `MIN_SCORE`, `MOMENTUM_*`, `PAPER_MODE`, API keys, `STATE_FILE` or any private wallet key. The clean launcher deliberately ignores them.

| New variable | Default | Reason |
|---|---|---|
| `CLEAN_STARTING_CAPITAL_USD` | 36 | New **paper** account initial balance, only if no saved state |
| `CLEAN_POSITION_SIZE_USD` | 10 | Simulated entry size, minimum $5 |
| `CLEAN_MARKET_CAP_MIN_USD` | 500000 | Established coin market cap minimum |
| `CLEAN_MARKET_CAP_MAX_USD` | 250000000 | Established coin market cap maximum |
| `CLEAN_STATE_FILE` | Derived from Railway volume path, otherwise local file | Optional custom **paper** state location |

A `CLEAN_*` value outside safe bounds produces an explicit startup error. `LIVE_TRADING_ENABLED=true` is always overridden to `false`.

## Local validation (optional)

```bash
python -m pip install -r requirements.txt
python clean_boot.py --self-test
# Expected: CLEAN SELF TEST PASS ... NO NETWORK REQUESTS, NO REAL TRADES
```

`--self-test` uses invented token/pool data, verifies a qualified synthetic example, downtrend and missing-depth rejections, an indicative same-pool sellability check, and a $10 paper entry without saving anything. **This test does not verify live API availability, fillability, profitability, or Railway operation.**

## If a new deployment still never trades

Send the first `CLEAN BOOT` lines plus a recent `QUALITY POOLS`, `ENTRY SCAN`, `FILTER SUMMARY`, `SCANNER HEALTH`, and any `PAPER ENTRY REJECT` / `PAPER ENTRY DECISIONS`. Those distinguish source problems from legitimate trade filters and independent exit-depth failures. Do not send API keys or private wallet information.

## State migration / safety

This new bot deliberately starts an **independent** paper portfolio by default. Preserve the old persistent state as a backup instead of copying a potentially halted or impaired position into the new service. If you wish to migrate trade history, that requires a deliberate, audited import with both bots stopped; no automatic state migration is attempted. Never treat a paper rug write-off as a successful sale. No real funds are involved in this package.

## Sparse cooldown scan fix (2026-10-16)

- Normal scan candidates and pending reentries are still preferred; no entry/risk filters were loosened.
- Previously, cooldown probing happened **only** when the regular selector returned zero tokens. One due reentry could suppress all probes despite 97/99 mints cooling.
- Now, when at least 75% of currently discovered mints are cooling and fewer than eight normal candidates were chosen, a sparse scan may include up to four timed, rotating cooldown rechecks (subject to `MOMENTUM_COOLDOWN_PROBE_GAP_SECONDS`). Those rechecks must pass all normal market and paper sellability filters.
- Look for `ENTRY SCAN ... cooldown_probes=1` or higher during sparse scans. `cooldown_probes=0` can still be normal if probe limits or per-mint timers have not elapsed.
- `cooling_total` reflects the broader rejection ledger, including older mints no longer in the current discovery pool. Compare `cooling_discovery` with `discovery_total` to measure current discovery starvation.
- `PAPER REENTRY DUE` only means a token is eligible to be re-screened, not that it should be bought again.
- Run `python -m pytest -q test_sparse_cooldown.py test_clean_boot.py` for offline tests and `python clean_boot.py --self-test` for a synthetic paper entry rehearsal.


### Self-test with custom market-cap settings

`python clean_boot.py --self-test` uses a synthetic token cap **inside** the resolved
`CLEAN_MARKET_CAP_MIN_USD` / `CLEAN_MARKET_CAP_MAX_USD` range. This fixes a
false test failure with valid non-default ranges. The start-up log prints the
resolved range; neither the fixture nor the self-test authorizes live trades.

### Persistent paper history in Railway

A clean launch can start with `/app/clean_paper_state.json` if there is no volume.
That is **ephemeral** across deployments. In Railway, create/attach a persistent
volume for the service, mount it at `/data`, then set
`CLEAN_STATE_FILE=/data/clean_paper_state.json` (or let the launcher derive the
path from `RAILWAY_VOLUME_MOUNT_PATH`). The `railway_volume_env_present` log
shows only whether Railway exposed a volume-path variable; it does not verify
that the storage is mounted. Back up/migrate existing state before changing
paths, and run only one bot instance against a state file.


## Rolling established-coin discovery (2026-10-16)

The `clean_boot.py` profile now enables **rolling established-market discovery** while keeping PAPER mode, reversal exits, the same $15K verified SOL pool liquidity floor, trend confirmation, route checking and indicative sellability requirements.

- Instead of scraping the same first trending/top-pool pages on every refresh, `rolling_discovery.py` rotates a **five-request discovery batch** through different trending durations and pages, Solana top pools, and top-by-DEX pool pages (Raydium, Orca, Meteora and PumpSwap). A source returning HTTP 400/404 is paused for 15 minutes; a rate-limited source pauses and the batch stops. Network errors don't prove a token has no liquidity.
- Public GeckoTerminal API is approximately **10 calls/minute** and may vary. Default discovery cache is 90 seconds = roughly **3.3 rotating pool requests/minute**, excluding the separately budgeted DexScreener lookups. This is a conservative limit, not a service guarantee. DEX paths must be valid for that public API; errors are logged rather than bypassed.
- Successful, supported SOL-quoted/liquid GeckoTerminal discoveries and qualifying DexScreener SOL-quoted search hits enter a separate persisted watchlist, default **750** mints retained up to **6 hours** after last observed. A newly returned mint is not automatically an eligible trade: every scan fetches and verifies fresh market conditions.
- Every **15-second entry scan** reserves up to **24 of 60 slots** for never- or least-recently checked watchlist mints (as slots allow), while preserving priority for pending confirmation/reentry candidates, verified pools, quality pools and the existing bounded cooldown probes.
- The watchlist and rotating source position persist in `rolling_discovery_state.json` in the **same folder as** `STATE_FILE` (normally `/data/rolling_discovery_state.json`). It does **not overwrite** `/data/clean_paper_state.json` or change existing paper positions. Existing paper-trade records are not migrated or reset. If the watchlist file is corrupted, it is kept untouched and the scanner falls back to in-memory discovery.
- The number of truly fresh, qualifying market tokens on Solana cannot be guaranteed. Some refreshes may produce no new mints, and the same token may continue to appear while held or awaiting recheck. Avoid artificially relaxing quality filters to force fills.

On Railway upload the **entire ZIP's contents** to your GitHub repository root, including `rolling_discovery.py`, `bot.py` and `clean_boot.py`. Deploy the latest commit. Keep the Start Command `python clean_boot.py`, keep your existing `/data` volume and `CLEAN_STATE_FILE`, and **do not delete your persistent paper file**. There is no need to create a new Railway service or add new Variables.

Verify logs include:

```
CLEAN BOOT ROLLING_DISCOVERY=enabled watchlist=/data/rolling_discovery_state.json rotating_source_requests=5 refresh_seconds=90
DISCOVERY ROTATION round=... source_pages=... errors=...
DISCOVERY WATCHLIST new_mints=... watchlist_total=... unscanned=...
ENTRY ROTATION never_scanned=... rolling_selected=... watchlist_total=...
ENTRY SCAN tokens=... cooldown_probes=...
```

If `new_mints=0`, the upstream sources simply may have repeated old markets; check `DISCOVERY ROTATION` errors and whether source/page values change across subsequent 90-second refreshes. Also check `PAPER BUY`/`PAPER SELL` and `FILTER SUMMARY` to distinguish discovery from market suitability.

Offline tests (no real trades): `python -m pytest -q test_rolling_discovery.py test_sparse_cooldown.py test_clean_boot.py`. Existing clean `--self-test` remains available. These tests use synthetic API payloads; they cannot certify live API connectivity or profitability.

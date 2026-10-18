> **Current: v8.6.4 original-shadow restore.** Follow `ORIGINAL_SHADOW_RESTORE_V8_6_4_NOTES.md` for the current paper-only config and startup messages. The v1 established-momentum instructions below are historical. Keep your saved Railway `/data` state.

> **CURRENT RELEASE v8.6.3 — SHADOW PAPER LIQUIDITY PROTECTION.** Five total and five young-pool positions (each young-pool position $2), independent same-pool indicative depth corroboration before new shadow entries, 20-second liquidity-risk monitoring, protective paper exits only with usable original-pool estimates, and entry-depth/volume outcome reporting. Live trading is disabled. **This is not an executable sell quote or a guarantee against rug pulls.** See [`SHADOW_LIQUIDITY_PROTECTION_V8_6_3_NOTES.md`](SHADOW_LIQUIDITY_PROTECTION_V8_6_3_NOTES.md). All older release headings below are historical.

---

> **Latest release: v8.6.2.** See `SHADOW_SIGNAL_HANDOFF_V8_6_2_NOTES.md` for the shadow signal handoff fix, sub-five-minute observation repair, and unchanged deployment settings.

# CURRENT RELEASE: v8.6.1 shadow-first pool discovery

**The active strategy is EarlyShadow**, not the old established-momentum strategy.
Discovery now prioritizes newly indexed 3–12-minute SOL pools, re-verifies
market data for 5–12-minute shadow buys, and follows young supported pools
at bounded 45-second intervals. Position sizes, limits, exits, `/data` state
and existing Railway credentials are unchanged. Full notes:
`SHADOW_FIRST_DISCOVERY_V8_6_1_NOTES.md`.

Only new strategy BUY signals use EarlyShadow. All wallet execution is disabled;
all results are indicative PAPER simulations.

---

> **Current release: v8.6 SHADOW PRIMARY — PAPER ONLY.** The original 5–12-minute shadow-study signals now make actual paper-portfolio entries, and shadow +8% / −12% / 45-minute sampling makes new-position exit decisions. Existing capital, saved portfolio, $10 default allocation, $2 early allocation and two-position maximum remain unchanged. See [ACTIVE_EARLY_SHADOW_V8_6_NOTES.md](ACTIVE_EARLY_SHADOW_V8_6_NOTES.md). Older descriptions below are historical and do not govern NEW shadow entries.

# v8.5 update

For an existing v8.4.1 bot, replace the six runtime files listed in `LIQUID_FIRST_DISCOVERY_V8_5_NOTES.md`. Do not recreate or erase `/data`; retain `CLEAN_PUMPPORTAL_API_KEY`. Search Railway logs for `CLEAN BOOT MIGRATION_DISCOVERY=v8.5` and `PAPER V85 QUEUE`. Launch-only candidates have no more than six observation lookups per scheduled scan. These are market checks, not trades.

# Current deployment: v8.4

See `MIGRATION_DISCOVERY_NO_HOUR_V8_4_NOTES.md`. Preserve the `/data` volume, the `CLEAN_PUMPPORTAL_API_KEY` Railway secret, and `python clean_boot.py`. The pinned minimum age is 12 minutes and one-hour volume/history is no longer a hard entry requirement. Migration events are observation hints, not buys. Older sections below refer to previous versions.

# v8.3 live-launch update

Start Command remains `python clean_boot.py`. Keep `CLEAN_PUMPPORTAL_API_KEY` as a **Railway secret**, all existing credentials and the `/data` volume. The pinned minimum age is now 12 minutes. Replace the seven runtime Python files named in `LAUNCH_TO_LIQUIDITY_V8_3_NOTES.md`, or deploy the complete ZIP. No state reset.

## v8.2 Discovery reliability and fast-mover coverage

The latest upgrade keeps the v8.1 paper-only trading/exit filters, while prioritizing GeckoTerminal 5m page-one movers and additional new pools, enabling an observation-only early 5m volume discovery lane when hourly history is thin, revisiting Jupiter rankings every 120s, sharing a rolling Gecko diagnostic/discovery request budget, prioritizing genuinely new watchlist mints, and logging missing-vs-zero 5m data. Full explanation and GitHub replacement files: `FAST_DISCOVERY_V8_2_NOTES.md`.

## Current release: v8.1 — Small-cap flow and profit lock

v8.1 relaxes **only** the $35K–$250K / 15–60-minute small-cap tier's hourly-volume requirement when strong, verified, same-pool five-minute trading flow exists; reduces selected small-cap/young-pool thresholds; preserves $2 speculative paper sizing, primary-pool liquidity checks, age/cap floors and existing risk exits. New v8.1 paper trades have a confirmation-based profit lock (arm at +3.5% net, 1.5-point giveback, current net at least +0.75%), a separate **observation-only** +4% quick-target shadow, and version-specific trade-performance reports. Details in `SMALL_CAP_FLOW_AND_PROFIT_LOCK_V8_1_NOTES.md`. Deploy by replacing `bot.py` and `clean_boot.py` and adding `microcap_strategy_v81.py` to v8.0. Keep the `/data` volume and Jupiter key. All trading remains PAPER ONLY.

---

> **Current v8.0 release:** See [EARLY_MICRO_WATCH_V8_0_NOTES.md](EARLY_MICRO_WATCH_V8_0_NOTES.md). Minimum $35K reported market cap, 15-minute pool age, extra $2 micro/young-pool paper tiers, persisted age/cap/provider watches, exact-pool independent diagnostics and 5–15-minute shadow comparisons. Current config overrides earlier-version settings; old sections below are historical.

> **v7.9 upgrade:** See [RECOVERY_AND_BALANCED_FAST_MOMENTUM_V7_9_NOTES.md](RECOVERY_AND_BALANCED_FAST_MOMENTUM_V7_9_NOTES.md). For existing v7.8, replace five runtime files and add `recheck_recovery_v79.py`. Keep `python clean_boot.py`, your Jupiter API variable and existing `/data` state.

> **New v7.7 expanded discovery network:** see [EXPANDED_NETWORK_V7_7_NOTES.md](EXPANDED_NETWORK_V7_7_NOTES.md). Free Raydium volume pools and verified DEX Screener promotional/community leads, plus optional Jupiter/Birdeye API-key feeds. All PAPER-only, trade gates unchanged.

> **v7.6 deployment:** [FAST_MOMENTUM_EXIT_V7_6_NOTES.md](FAST_MOMENTUM_EXIT_V7_6_NOTES.md). Keep `python clean_boot.py` and preserve `/data`; replace six runtime Python files and add `fast_momentum_v76.py`; no additional Railway variables needed.

> v7.5 deployment notes: [EARLY_WATCH_100K_V7_5_NOTES.md](EARLY_WATCH_100K_V7_5_NOTES.md).

> **v7.4 update:** Minimum cap is now $250K, with stricter gates and a $5 paper size for $250K-$500K tokens. See [CANDIDATE_PIPELINE_V7_4_NOTES.md](CANDIDATE_PIPELINE_V7_4_NOTES.md). If `CLEAN_MARKET_CAP_MIN_USD=500000` remains set in Railway, that explicit override keeps the old floor.

> **v7 upgrade:** include `fresh_batch_discovery.py` and `rolling_discovery.py` along with updated `bot.py` and `clean_boot.py`. The fresh batch sidecar is `/data/fresh_batch_state.json`. See [FRESH_BATCH_V7_NOTES.md](FRESH_BATCH_V7_NOTES.md).

**v6 update:** See [RISK_V6_NOTES.md](RISK_V6_NOTES.md). Keep the existing `/data` volume and `python clean_boot.py` start command.

## New in v5 — cost-aware entry and confirmed-loss protection

See [ENTRY_EXIT_V5_NOTES.md](ENTRY_EXIT_V5_NOTES.md). The bot still runs **paper-only** with the same Railway volume and position limits. Replace `bot.py`, `clean_boot.py` and add `trade_signal_guards.py` together. The new entry check uses recent observed momentum as a fee-cost *proxy*, not an expected-return prediction. Final entries are revalidated against the latest same-pool provider response; early confirmed-loss exits require repeated primary price deterioration and verified indicative exit data.

# Clean Solana momentum paper bot — Railway setup

## What this package does

- **PAPER ONLY:** no wallet key, no real swaps, no account funding required.
- Reuses the most recent bot's discovery, entry, exit, reversal and confirmed-rug paper accounting logic.
- `clean_boot.py` forces a reviewed established-momentum profile **before importing** `bot.py`. Legacy Railway bot variables (including stale `MIN_SCORE`, entry limits, old `STATE_FILE` and live mode) are ignored.
- Uses older Solana pairs with ~$250K-$250M market cap / FDV proxy (with extra gates below $500K) and reported SOL-quoted liquidity >= $15K. Requires positive multitimeframe momentum and fresh sellability checks. Successful historical backtests or profit are **not** implied.
- Unlike a real swap, a paper quote remains indicative; there is no verified on-chain execution and no guaranteed exit fill.

## Fresh install (recommended: brand-new Railway **service/project**)

1. Back up any current Railway state file and logs you want to keep. Stop/pause the old service so it doesn't keep scanning or writing to shared state. Do not delete it yet.
2. Create a **new GitHub repository** if you'd like a clean history (optional, but easiest if you want one obvious version). Unzip the downloaded bot release and upload the **contents** of the folder at the repository root. Do **not** upload the zip as a single GitHub file.
3. In Railway, click **New Project > Deploy from GitHub Repo** and connect the new repository. Confirm the new service points at the new repository and **root directory** `/`.
4. Under **Service > Settings > Deploy**, set **Custom Start Command** to `python clean_boot.py` (no quotes). Railway uses Railpack to detect Python and install `requirements.txt` automatically. If your interface has different labels, look for the service's *Start Command* setting.
5. **Variables**: you do **not** have to import any of the old 111 variables. `clean_boot.py` pins the strategy settings. You may start with **zero strategy variables**. (If you add a persistent volume, the volume mount path is automatically exposed at runtime.)
6. **Strongly recommended:** add a Railway volume to this new service, mounted at `/data`. By default the launcher uses the mount location to keep `/data/clean_paper_state.json`. Confirm `CLEAN BOOT ... volume_mounted=True` in the logs. Without a volume, state may reset after redeploys.
7. Set the service to **one running replica**. Do not run two copies against the same state file. Deploy and open **Deploy Logs**.

## First five minutes: validation checklist

1. Find `CLEAN BOOT PROFILE=established_momentum_v1 PAPER_MODE=true LIVE_TRADING_ENABLED=false`.
2. Find `CLEAN BOOT STATE_FILE=... volume_mounted=True` (if volume added).
3. Find `ESTABLISHED MOMENTUM PAPER MODE cap=$35000-$250000000`.
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
| `CLEAN_MARKET_CAP_MIN_USD` | 35000 | Established coin market cap minimum |
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

- Instead of scraping the same first trending/top-pool pages on every refresh, `rolling_discovery.py` rotates a **three-request discovery batch** through different trending durations and pages, Solana top pools, and top-by-DEX pool pages (Raydium, Orca, Meteora and PumpSwap). HTTP 400/404 backs off that particular page for 30 minutes; an HTTP 429 backs off all GeckoTerminal calls for at least 180 seconds. Network errors don't prove a token has no liquidity.
- Public GeckoTerminal API is approximately **10 calls/minute** and may vary. Default discovery cache is 90 seconds = roughly **2 rotating pool requests/minute**, excluding the separately budgeted DexScreener lookups. This is a conservative limit, not a service guarantee. DEX paths must be valid for that public API; errors are logged rather than bypassed.
- Successful, supported SOL-quoted/liquid GeckoTerminal discoveries and qualifying DexScreener SOL-quoted search hits enter a separate persisted watchlist, default **750** mints retained up to **6 hours** after last observed. A newly returned mint is not automatically an eligible trade: every scan fetches and verifies fresh market conditions.
- Every **15-second entry scan** reserves up to **24 of 60 slots** for never- or least-recently checked watchlist mints (as slots allow), while preserving priority for pending confirmation/reentry candidates, verified pools, quality pools and the existing bounded cooldown probes.
- The watchlist and rotating source position persist in `rolling_discovery_state.json` in the **same folder as** `STATE_FILE` (normally `/data/rolling_discovery_state.json`). It does **not overwrite** `/data/clean_paper_state.json` or change existing paper positions. Existing paper-trade records are not migrated or reset. If the watchlist file is corrupted, it is kept untouched and the scanner falls back to in-memory discovery.
- The number of truly fresh, qualifying market tokens on Solana cannot be guaranteed. Some refreshes may produce no new mints, and the same token may continue to appear while held or awaiting recheck. Avoid artificially relaxing quality filters to force fills.

On Railway upload the **entire ZIP's contents** to your GitHub repository root, including `rolling_discovery.py`, `discovery_expansion.py`, `bot.py` and `clean_boot.py`. Deploy the latest commit. Keep the Start Command `python clean_boot.py`, keep your existing `/data` volume and `CLEAN_STATE_FILE`, and **do not delete your persistent paper file**. There is no need to create a new Railway service or add new Variables.

Verify logs include:

```
CLEAN BOOT ROLLING_DISCOVERY=enabled watchlist=/data/rolling_discovery_state.json rotating_source_requests=3 search_terms_per_refresh=3 refresh_seconds=90
DISCOVERY ROTATION round=... requests=... successes=... skipped_backoff=... global_backoff_s=...
DISCOVERY SEARCH terms=... requests_ok=... unique_hints=... next_cursor=...
DISCOVERY WATCHLIST new_mints=... watchlist_total=... unscanned=...
ENTRY ROTATION never_scanned=... rolling_selected=... watchlist_total=...
ENTRY SCAN tokens=... cooldown_probes=...
```

If `new_mints=0`, the upstream sources simply may have repeated old markets; check `DISCOVERY ROTATION` errors and whether source/page values change across subsequent 90-second refreshes. Also check `PAPER BUY`/`PAPER SELL` and `FILTER SUMMARY` to distinguish discovery from market suitability.

Offline tests (no real trades): `python -m pytest -q`. Existing clean `--self-test` remains available. These tests use synthetic API payloads; they cannot certify live API connectivity or profitability.


## Wider discovery expansion — update existing Railway service

**This is an in-place update, not a new wallet, service, or paper portfolio.**
Keep `CLEAN_STATE_FILE=/data/clean_paper_state.json`, your mounted `/data` volume,
and the existing `python clean_boot.py` start command. Back up the two state
files under `/data` before replacing code. Run only one instance against them.

**New:** `discovery_expansion.py` and `test_discovery_expansion.py`.
**Updated:** `bot.py`, `rolling_discovery.py`, `clean_boot.py`,
`test_rolling_discovery.py`, `README.md` and `CLEAN_RAILWAY_SETUP.md`.
Other Python files are preserved unchanged. Upload files at the GitHub
repository root, in one commit, and let Railway deploy that exact commit.

- 32 search terms rotate at a budget of 3 DexScreener market-search requests
  per discovery refresh (typically every 60 seconds). The cursor persists
  within `/data/rolling_discovery_state.json` across restarts.
- Search pairs are **discovery hints** only. They must have a supported
  SOL-quoted DEX route, at least $15K *reported* pool liquidity, at least
  60 minutes known age and an in-range reported capitalization if present.
  Even a prefiltered mint still faces the bot's independent, fresh,
  multi-timeframe momentum, depth and prebuy checks.
- GeckoTerminal source rotation uses 3 calls per ~90 seconds instead of 5,
  with page-level HTTP 404 backoff, shared-source HTTP 429 cooldown, and
  logged request/success/error counts. DexScreener searches continue even
  if GeckoTerminal is unavailable. No fee-based API key is required.
- The watchlist holds up to 750 recently seen plausible mints and retains
  the saved source and search cursors. The existing paper positions are not
  migrated, closed, or rewritten by discovery changes.
- Successful broader discovery depends on provider coverage and market
  activity; neither a fresh mint nor a positive trade is guaranteed per scan.

New lines to track after deployment:

```
DISCOVERY ROTATION round=... planned=3 requests=... successes=... skipped_backoff=... global_backoff_s=...
DISCOVERY SEARCH terms=['...', '...', '...'] requests_ok=3 unique_hints=... rejected=... next_cursor=...
DISCOVERY WATCHLIST new_mints=... watchlist_total=... unscanned=...
ENTRY ROTATION never_scanned=... rolling_selected=...
```

If the watchlist fails to expand, inspect `requests_ok`, GeckoTerminal
`global_backoff_s` and the search rejection categories before changing any
market or exit filters. The tests are offline synthetic checks and cannot
establish the actual live discovery rate or strategy profitability.

## Candidate-quality scheduling (latest update)

`candidate_quality.py` introduces a conservative, persistent candidate-quality
sidecar that postpones **new entry scans** for repeatedly unindexed, invalid
quote-route, or missing/insufficient SOL liquidity tokens. Waits increase on
repeated failures, are capped, and expire; these tokens are never permanently
banned. Read-only or invalid quality metadata is preserved rather than silently
overwritten. Good, recently revalidated markets with passing momentum get
priority for a few minutes, while brand-new watchlist discoveries retain
reserved entry-scan capacity. Held positions are managed independently.

New logs: `CLEAN BOOT CANDIDATE_QUALITY`, `CANDIDATE QUALITY`, and
`PAPER ENTRY TREND CONFIRMED`. This only changes discovery selection order
and diagnostics; all buy/sell filters and the paper ledger are unchanged.
See `UPGRADE_NOTES.md` for file replacements and Railway instructions.

## v4 — Entry scheduling while the two paper slots are full

This package includes an early, post-exit-management position-capacity guard.
The bot keeps discovery, scoring, held-position monitoring, and paper state
persistence active, but skips futile reentry confirmations and prebuy
cross-provider checks until a paper slot is available. Once capacity returns,
all ordinary entry requirements still apply. On deployment keep your existing
`/data` volume and `python clean_boot.py` start command. See
`UPGRADE_NOTES.md` for changed files and log verification.

### v7.1 clean deploy
Upload `bot.py`, `clean_boot.py`, `fresh_batch_discovery.py`, and `rolling_discovery.py` from the same ZIP. Preserve the `/data` volume and existing paper state. Verify `CLEAN BOOT FRESH_BATCH=v7.1 enabled` and `FRESH BATCH DEFERRED` when applicable. See `FRESH_BATCH_V7_1_PATCH_NOTES.md`.

## v7.2 novelty-aware discovery patch

See `FRESH_BATCH_V7_2_NOTES.md`. When fresh watchlist additions stop for repeated discovery rounds, the bot alternates top GeckoTerminal trending pages with deeper 5-minute/1-hour rankings using the same three-request API budget. Freshness diagnostics distinguish genuinely new watchlist mints from per-process counts. All paper-only and entry-risk checks remain unchanged.


## v7.3 — faster, independent Solana discovery

The complete source-and-test ZIP adds `independent_movers_v73.py` and `test_independent_movers_v73.py`, updates `bot.py` and `clean_boot.py`, and documents the change in `FAST_MOVER_V7_3_NOTES.md`. GeckoTerminal rotates every 60 seconds with the existing three-request per-refresh budget. DexPaprika is an independent, rate-limited SOL-pool mover feed (360-second keyless polling; optional free key stored as `CLEAN_DEXPAPRIKA_API_KEY` for 90-second polling). Recent DexScreener token profiles are considered only after an actual qualified SOL-pool lookup: profile placement is not momentum verification. All discovery hints still run through the existing paper-only market, entry, risk and sellability checks. See `FAST_MOVER_V7_3_NOTES.md` for logs, limitations and the exact update procedure.

v7.8: watch for `CLEAN BOOT HOT_NEARMISS=v7.8` and, when applicable, `PAPER FAST NEAR MISS` followed by `PAPER HOT RECHECK`. Existing `/data` remains unchanged except for compatible optional retry metadata in `fresh_batch_state.json`. Existing Railway Jupiter key is reused; no additional key is required.

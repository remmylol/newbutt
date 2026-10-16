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

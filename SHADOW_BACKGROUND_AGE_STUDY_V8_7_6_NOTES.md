# Shadow Primary v8.7.6 — independent 0–60 / 5–60 minute paper studies

**PAPER ONLY / RESEARCH.** The v8.7.5 health-protected **original 5–12-minute** shadow remains the **only primary trader**. This patch does **not** widen that strategy's entry age, change the primary portfolio ledger, loosen its filters, clear a primary halt, or enable live trades.

## Three distinct tracks

| Track | Entry age | Role | Saved ledger |
| --- | --- | --- | --- |
| Current primary | 5–12 minutes | Exclusive existing paper trader, v8.7.5 safety and on-chain/Jupiter read-only verification unchanged | Your existing `CLEAN_STATE_FILE` |
| Background A | 0 to under 60 minutes | Independent virtual $2 position simulator, no minimum age | `age_study_0_60_<primary_state_stem>.json` |
| Background B | 5 to under 60 minutes | Independent virtual $2 position simulator | `age_study_5_60_<primary_state_stem>.json` |

All three ledgers start at the configured paper starting capital (default $36) **independently**. Their cash/P&L **must not be added together**: both experimental tracks can simulate the same token and are not real executions. Each background portfolio allows five concurrent $2 virtual entries. Each has its own durable severe-loss/daily-loss safety halt and keeps scanning open virtual positions while halted. The primary halt is unaffected.

### How the background entries are evaluated

- GeckoTerminal pages already consulted by the primary strategy now also supply discovery hints for 0–60-minute eligible Solana SOL-quoted pools. This second hint collection **does not** feed back into the primary 12-minute scheduler. The experiment runs up to **three extra DexScreener token-pair lookups per primary entry scan** (nominally every 15 seconds), giving held virtual positions priority, then making room for the 0–5, 5–12 and 12–60-minute age buckets. Source/API rate limits can reduce this coverage. Not every pool launched in an hour will be observed.
- Each virtual entry must pass the same verified market data, initial pool-liquidity floor ($15K), cap floor ($35K), 5m volume >=$1,400, buys >=10, sells >=1, buy/sell >=1.25, 5m change 0.8–16%, plus v8.7.5's high-surge/thin-volume screen. The **0–5-minute** candidate's provider `m5` metric represents **partial** five-minute history; ledger records `entry_5m_partial=true`. This is an experiment, not a claim that the original full-five-minute signal can be evaluated at launch.
- Positions monitor the **same original pool** with repeated fresh data even after entry pool age exceeds 60 minutes or liquidity drops beneath $15K. A virtual +8% net take-profit, −9% observed protective stop, 40% liquidity-loss trigger, and 45-minute expiry are considered at the next *usable* quote. No usable quote means no invented sale; counts are tracked as missed quotes. A catastrophic observed/closed 50% loss halts only that experimental track.
- Outcomes use a conservative **6% modeled round-trip haircut** and actual observed primary-pool prices, but are **indicative snapshots only**. The background tracks do **not** require the primary strategy's independent on-chain/Jupiter prebuy validation and **do not** obtain executable sale fills; therefore they can materially overestimate performance, especially in the first five minutes. Do not compare raw 'win rate' as if it measured real profitability.

## Files

Modified: `bot.py`, `clean_boot.py`, `shadow_pool_discovery_v861.py`, `portfolio_report.py`.

Added: `shadow_age_tracks_v876.py`, `test_shadow_age_study_v876.py`, this document.

All other files (including `early_watch_shadow_v80.py`, the read-only Solana verifier and the actual paper portfolio/exit rules) remain unchanged from v8.7.5.

## Railway deployment

1. Back up the current `/data` and primary state JSON. **Do not reset or replace your existing `CLEAN_STATE_FILE`**. Upload the ZIP contents to GitHub and redeploy with the same start command: `python clean_boot.py`.
2. Keep the existing read-only RPC/Jupiter credentials and attached `/data` volume. `clean_boot.py` pins `SHADOW_AGE_STUDY_ENABLED=true` and `SHADOW_AGE_STUDY_FETCH_PER_SCAN=3`. No new Railway variables are needed.
3. Verify `CLEAN BOOT BACKGROUND_AGE_STUDY=v8.7.6 ...` and `PAPER AGE STUDY ACTIVE version=v8.7.6 ...` at startup, followed by per-minute `PAPER AGE STUDY AUDIT track=0_60 ...` and `track=5_60 ...`. Individual hypothetical openings/closings log as `PAPER AGE STUDY EVENT ...`.
4. On Railway, the background files will be named based on the **current primary `CLEAN_STATE_FILE` basename**, and saved beside it on `/data`. Example, if `CLEAN_STATE_FILE=/data/shadow_v875_fresh36_run001.json`, they are `/data/age_study_0_60_shadow_v875_fresh36_run001.json` and `/data/age_study_5_60_shadow_v875_fresh36_run001.json`. Switching the main filename starts *new separate* studies without deleting existing studies.
5. To compare all three portfolios offline or in the Railway console (read-only): `python portfolio_report.py --state /data/shadow_v875_fresh36_run001.json --json`. The report includes `background_age_tracks`; background outcomes are always labeled **not executable**.

## Tests and limitations

`python -m pytest -q` exercises the old protections and new sidecar isolation, source-hint age bounds, age-bucket eligibility, entry health gate, low-liquidity protective exits, pinned pools, 45-minute expiry, missing-quote retention, separate per-track safety halts, reload and reporting.

The primary trader is unchanged, but its implementation was not validated against Railway's live providers as part of this offline delivery. The experiment makes additional API calls subject to possible 429 rate limits, observation delay and survivorship/discovery bias; no synthetic test proves real-world safety or expected profitability. Keep all portfolios paper-only.

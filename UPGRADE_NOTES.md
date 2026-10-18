# Current release v8.6.4: original shadow active paper + write-offs

See `ORIGINAL_SHADOW_RESTORE_V8_6_4_NOTES.md`. This supersedes the v8.6.3 independent prebuy verification and fast liquidity exit logic. All older notes below are historical.

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

# Latest upgrade: v8.5 liquid-first scan prioritization

See `LIQUID_FIRST_DISCOVERY_V8_5_NOTES.md`. Deploy from v8.4.1 by replacing `bot.py`, `clean_boot.py`, `fresh_batch_discovery.py`, `launch_pool_followup_v83.py` and `recheck_recovery_v79.py`; add `fast_liquid_recheck_v85.py`. Preserve Railway `/data`, `CLEAN_PUMPPORTAL_API_KEY`, other API keys, and `python clean_boot.py`. The 12-minute age, $35K cap, no-one-hour paper profile and normal entry checks are retained.

# v8.4 upgrade: migrations + no mandatory 1h window

See `MIGRATION_DISCOVERY_NO_HOUR_V8_4_NOTES.md` for the complete v8.3→v8.4 runtime-file list and verification. Preserve the Railway `/data` volume, `CLEAN_PUMPPORTAL_API_KEY` secret and `python clean_boot.py` Start Command. This is paper-only.

# v8.3 — Launch-to-liquidity watch + 12m minimum pool age

See `LAUNCH_TO_LIQUIDITY_V8_3_NOTES.md` for the full v8.2.1 → v8.3 diff and Railway instructions. Maintains the connected PumpPortal stream; raises mature launch intake to 32; adds persisted pool-availability follow-ups and age 12 minutes without new age-specific restrictions; all entry validation and trading remain paper-only.

## v8.2 Discovery reliability and fast-mover coverage

The latest upgrade keeps the v8.1 paper-only trading/exit filters, while prioritizing GeckoTerminal 5m page-one movers and additional new pools, enabling an observation-only early 5m volume discovery lane when hourly history is thin, revisiting Jupiter rankings every 120s, sharing a rolling Gecko diagnostic/discovery request budget, prioritizing genuinely new watchlist mints, and logging missing-vs-zero 5m data. Full explanation and GitHub replacement files: `FAST_DISCOVERY_V8_2_NOTES.md`.

## Current release: v8.1 — Small-cap flow and profit lock

v8.1 relaxes **only** the $35K–$250K / 15–60-minute small-cap tier's hourly-volume requirement when strong, verified, same-pool five-minute trading flow exists; reduces selected small-cap/young-pool thresholds; preserves $2 speculative paper sizing, primary-pool liquidity checks, age/cap floors and existing risk exits. New v8.1 paper trades have a confirmation-based profit lock (arm at +3.5% net, 1.5-point giveback, current net at least +0.75%), a separate **observation-only** +4% quick-target shadow, and version-specific trade-performance reports. Details in `SMALL_CAP_FLOW_AND_PROFIT_LOCK_V8_1_NOTES.md`. Deploy by replacing `bot.py` and `clean_boot.py` and adding `microcap_strategy_v81.py` to v8.0. Keep the `/data` volume and Jupiter key. All trading remains PAPER ONLY.

---

> **Current v8.0 release:** See [EARLY_MICRO_WATCH_V8_0_NOTES.md](EARLY_MICRO_WATCH_V8_0_NOTES.md). Minimum $35K reported market cap, 15-minute pool age, extra $2 micro/young-pool paper tiers, persisted age/cap/provider watches, exact-pool independent diagnostics and 5–15-minute shadow comparisons. Current config overrides earlier-version settings; old sections below are historical.

## Current upgrade: v7.9

See [RECOVERY_AND_BALANCED_FAST_MOMENTUM_V7_9_NOTES.md](RECOVERY_AND_BALANCED_FAST_MOMENTUM_V7_9_NOTES.md). From v7.8 replace `bot.py`, `clean_boot.py`, `candidate_pipeline_v74.py`, `fast_momentum_v76.py`, `fresh_batch_discovery.py` and add `recheck_recovery_v79.py`. No Railway variable changes; keep the same `/data` volume, Jupiter key and `python clean_boot.py` startup. All trade execution remains paper-only.

> **New v7.7 expanded discovery network:** see [EXPANDED_NETWORK_V7_7_NOTES.md](EXPANDED_NETWORK_V7_7_NOTES.md). Free Raydium volume pools and verified DEX Screener promotional/community leads, plus optional Jupiter/Birdeye API-key feeds. All PAPER-only, trade gates unchanged.

**Latest v7.6:** [FAST_MOMENTUM_EXIT_V7_6_NOTES.md](FAST_MOMENTUM_EXIT_V7_6_NOTES.md). Paper fast-mover gating/ranking; 8% net TP on *new* fast trades, confirmed primary-pool trailing protection, earlier confirmed loss circuit, shorter bounded retry after temporary momentum misses. Update six existing Python files, add `fast_momentum_v76.py`.

**Latest version v7.5:** [EARLY_WATCH_100K_V7_5_NOTES.md](EARLY_WATCH_100K_V7_5_NOTES.md). $100K guarded min cap, 30m min pool age and cross-source observation-only young pool tracking; no real swaps.

**v7.4 candidate conversion and $250K guarded cap tier:** [CANDIDATE_PIPELINE_V7_4_NOTES.md](CANDIDATE_PIPELINE_V7_4_NOTES.md).

## New in v5 — cost-aware entry and confirmed-loss protection

See [ENTRY_EXIT_V5_NOTES.md](ENTRY_EXIT_V5_NOTES.md). The bot still runs **paper-only** with the same Railway volume and position limits. Replace `bot.py`, `clean_boot.py` and add `trade_signal_guards.py` together. The new entry check uses recent observed momentum as a fee-cost *proxy*, not an expected-return prediction. Final entries are revalidated against the latest same-pool provider response; early confirmed-loss exits require repeated primary price deterioration and verified indicative exit data.

# Capacity-Aware Paper Entry Scheduling — v4

This release is a focused patch **on top of Candidate Quality Expansion v3**.
It does **not** change market-cap, liquidity, momentum, sellability, reversal,
stop-loss, take-profit, position-size, maximum-open-positions, or daily-loss
thresholds. The bot remains indicative **PAPER ONLY**, not a live swap engine.

## What changed

- **`bot.py` updated**: A full paper portfolio no longer schedules closed-trade
  reentry watch lookups or calls the fresh second-price trend confirmation,
  cross-provider same-pool prebuy verification, and paper buy attempts for
  candidates that cannot be bought due to the two-position limit.
- The bot **continues discovering, scanning, and ranking** new candidate
  markets while its portfolio is full. Position exit monitoring still runs on
  every regular portfolio tick, using the entry pool of each position.
- Entry capacity is checked **after** managing exits. If an exit frees a slot
  during the scan, a newly eligible market can still progress through the full
  price-trend and prebuy verification checks before an indicative paper buy.
- Pending entry recheck markers are cleared only while the portfolio remains
  full, preventing stale pending work. Discovery records, quarantine/retry data,
  and closed-trade histories are not cleared.
- New `PAPER REENTRY PAUSED` and `PAPER ENTRY CAPACITY` log lines show when
  capacity is limiting entries and how many checks were deferred.
- **`test_entry_capacity.py` new**: Three deterministic scan-level regression
  tests exercise a full portfolio, an exit on the same scan, and an exit before
  the next scan.

## GitHub deployment from existing v3

For minimum code changes: **replace only `bot.py`**, and optionally add
`test_entry_capacity.py` (tests). You may also replace `UPGRADE_NOTES.md` and
other docs. All remaining operational Python modules in the ZIP are identical
in content to v3, including `clean_boot.py` and `candidate_quality.py`.

Keep the existing Railway Start Command `python clean_boot.py` and the
existing volume and `CLEAN_STATE_FILE=/data/clean_paper_state.json`. Preserve
`/data/clean_paper_state.json`, `/data/rolling_discovery_state.json`, and
`/data/candidate_quality_state.json`. Deploy the new GitHub commit, do not
run two concurrent bot instances on the same state file, and back up the
volume files if past simulated trades matter.

## Verification

While the two slots are occupied, expect logs similar to:

    PAPER REENTRY PAUSED reason=position_limit positions=2 max=2; discovery_and_exit_monitoring=active
    PAPER ENTRY CAPACITY reason=position_limit positions=2 max=2 held_candidates=0 deferred_candidates=1 prebuy_checks_skipped=1 discovery_active=True exits_active=True future_entry_requires_fresh_market_check=True

`PAPER ENTRY CAPACITY` is a scheduler decision, not a failed trade. Normal
`DISCOVERY SEARCH`, `DISCOVERY WATCHLIST`, `ENTRY ROTATION`, `FILTER SUMMARY`,
and the held-position `Equity...` logs continue.

Run `python -m pytest -q` and `python clean_boot.py --self-test` in an
isolated test environment. Neither proves live market liquidity or real
executability; those still require a separately validated swap adapter.

## v7.2

Added adaptive deeper GeckoTerminal fast-mover page rotation after persistent zero-new discovery refreshes, with historical novelty logs and backward-compatible persistence. See `FRESH_BATCH_V7_2_NOTES.md`.


## v7.3 — faster, independent Solana discovery

The complete source-and-test ZIP adds `independent_movers_v73.py` and `test_independent_movers_v73.py`, updates `bot.py` and `clean_boot.py`, and documents the change in `FAST_MOVER_V7_3_NOTES.md`. GeckoTerminal rotates every 60 seconds with the existing three-request per-refresh budget. DexPaprika is an independent, rate-limited SOL-pool mover feed (360-second keyless polling; optional free key stored as `CLEAN_DEXPAPRIKA_API_KEY` for 90-second polling). Recent DexScreener token profiles are considered only after an actual qualified SOL-pool lookup: profile placement is not momentum verification. All discovery hints still run through the existing paper-only market, entry, risk and sellability checks. See `FAST_MOVER_V7_3_NOTES.md` for logs, limitations and the exact update procedure.

## v7.8 bounded fast near-miss retry
See `FAST_RECHECK_V7_8_NOTES.md`. From v7.7 replace `bot.py`, `clean_boot.py`, `fresh_batch_discovery.py` and add `fast_nearmiss_v78.py`. All entry and exit thresholds remain the same, PAPER mode only; retry up to twice after 90s for a narrowly missed fresh supported pool, no market-cap or structural shortcuts.

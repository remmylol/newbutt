# v8.6.4 — Original shadow as primary paper strategy + evidence-based write-offs

**Paper-only research implementation.** This release deliberately restores the **original v8.5 early-shadow observational entry and exit decisions** in the funded paper ledger, with **one separate conservative write-off process** for positions that appear unsellable. It does not implement live trades, wallet signing, executable DEX sell quotes, or guaranteed fills.

## What is restored

- **Single observational entry**: the very first verified DexScreener same-pool SOL pair snapshot that meets the early-shadow criteria supplies both the decision **and observed paper entry price**. No second entry snapshot, no independent GeckoTerminal pool crosscheck, no extra prebuy sell-depth gate, and no legacy momentum/score veto. A token must still be supported, same-pool, SOL-quoted, and valid *on its original signal observation*.
- **Original thresholds unchanged from the high-win-rate v8.5 study**: 5 <= age < 12 minutes, >=$35K reported market cap, >=$15K SOL pool liquidity, >=$1,400 five-minute volume, >=10 five-minute buys, >=1 sell, buy/sell >=1.25, five-minute price change +0.8% to +16%.
- **Original observation timing and exits**: evaluate on the original 180-second sampled price path; indicative (market price / entry market price - 1)*100 - 6 percentage points; +8% modeled net TP, -12% modeled net stop, or 45-minute expiry. No 20-second liquidity drop sale, legacy fast-profit stop, reversal exit, or profit trails for these positions.
- **Original re-observation semantics**: a closed non-rugged token can qualify again *within the age window* if rediscovered. Legacy post-trade reentry cooldowns are not part of the shadow buy decision.
- **Current user-requested account configuration retained**: five maximum positions, five maximum shadow positions, $2 per shadow trade, $10 default for other old portfolio profiles, current saved cash/positions/trade history and Railway state volume. Old non-shadow holdings retain their own established exit profiles.
- **Discovery remains the v8.6.1/8.6.3 shadow-first pool discovery system**, to supply timely snapshots. Its launch and migration feeds do **not** authorize a buy without DexScreener pool evidence. This is a deliberate infrastructure choice; the original study was observation-only and did not have an independent funded order engine.

## The one retained safety/accounting addition: unsellable paper write-off

- Original-pool missing price, unavailable quote, or fallback-only market data **never becomes a simulated sale**. Such holdings remain open and retain their original stake until sufficient evidence exists.
- Only a successful fresh pool response that independently shows the pinned supported SOL pool missing or near-zero liquidity **and no viable alternate supported indexed SOL pool**, repeatedly verified over >=300 seconds and >=3 checks, triggers **conservative zero-recovery accounting**. This is a *write-off, not a sale*, and does **not** prove that no future recovery is possible or that a deliberate rug occurred.
- Temporary market-data failures without reliable pool evidence **do not qualify** for an automatic write-off. The position remains open and may be conservatively marked impaired. Missing quotes no longer automatically halt new entries, but **manual/risk/daily loss halts still apply**. Legacy exit-only halts are cleared only when the old saved state explicitly proves no halt existed beforehand.
- Rug-written-off mints remain blacklisted. Account P&L includes the full $2 zero-recovery assumption where write-off evidence meets the threshold, rather than silently omitting failed exits from the win-rate denominator.

## Why paper returns may still differ from the earlier observational study

The original study summed completed sampled percentage results and did not count unresolved positions as closed losses. The funded paper portfolio uses the same price-path signal, but debits $2 cash at entry, estimates 3% buying and 3% selling costs plus pool impact, and can book confirmed-evidence *accounting write-offs*. This is **not a claim that the original 80%+ completed-observation win rate was a real or repeatable portfolio return**.

## Deployment / rollback

1. Back up your persistent Railway `/data/clean_paper_state.json` and previous code release if possible. **Do not delete or reset the persistent `/data` volume** to make the bot trade. Deploy only one running replica.
2. Replace the root GitHub repository with the **contents** of this release ZIP. If patching v8.6.3, replace **`bot.py` and `clean_boot.py`**; include the tests and release notes. Keep the existing `early_watch_shadow_v80.py`, `shadow_pool_discovery_v861.py`, other modules and `requirements.txt`.
3. Keep Railway Start Command **`python clean_boot.py`**, API keys in Railway Variables, and `/data` volume mounted.
4. Confirm `CLEAN BOOT PROFILE=shadow_primary_v8.6.4_original_restore`, `PAPER PRIMARY STRATEGY=v8.6.4 ORIGINAL_SHADOW_ACTIVE`, and `max_positions=5`. Look for `PAPER SHADOW ORIGINAL BUY`, `PAPER SHADOW ACTIVE SAMPLE`, `PAPER SHADOW EXIT UNAVAILABLE`, and `PAPER RUG WRITE-OFF`.
5. Run `python clean_boot.py --self-test` and `pytest -q` to verify locally; all tests use synthetic data/offline fixtures, not representative market performance.

This version is a clean behavioral baseline for the **original shadow signal with recorded losses**; avoid extrapolating profitability from fewer than several dozen (preferably hundreds of) resolved observations with unresolved outcomes also tracked.

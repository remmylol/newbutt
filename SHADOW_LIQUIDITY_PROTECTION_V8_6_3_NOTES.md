# v8.6.3 — Shadow liquidity protection & five-position paper account

**PAPER ONLY.** Does not sign transactions or produce on-chain executable swap quotations. All simulated fills use *indicative* DEX price/liquidity and cost estimates; simulation cannot prevent real rug pulls.

## Changes from v8.6.2

- **Five total open positions and five shadow/young open positions**, up to five separate **$2 each** (maximum $10 young-pool nominal allocation if cash and portfolio risk rules allow). Default established-profile position size stays **$10**. Saved state stays `/data/clean_paper_state.json`; no automatic reset.
- **Shadow prebuy full-stake SELL depth model**: on the *fresh exact same SOL pool*, calculate a hypothetical full $2 liquidation using 50% reported USD pool liquidity as quote-side depth and the 6% round-trip cost model; veto missing/nonpositive price, reported liquidity under $15K, or modeled exit impact above 2%. These are estimates, **NOT executable sell quotes**.
- **Independent pinned pool corroboration required in the Clean Railway profile**: GeckoTerminal's public exact-pool endpoint must return the same base mint, at least $15K reported liquidity and >=70% of primary liquidity, and a price within 12% of the same-pool primary price. If provider evidence is missing, stale, rate-limited, or disagreeing, the bot **does not buy**. This can reduce trade counts. Strict validation is intentional for a strategy that saw frequent full-liquidity-loss paper write-offs.
- **Fast paper liquidity guard**: every 20 seconds per open shadow position, check that the fresh primary pool has positive, *simulatable sell* depth. If verified same-pool liquidity drops >=40% versus the higher of entry or observed peak, or dips below $18K while still sellable, initiate an **early paper risk exit**. Existing shadow +8%/-12%/45m price-path exit sampling remains every 180 seconds. A failed primary quote or liquidity=0 must never count as a sell; existing impaired/write-off accounting and entry halt still apply.
- **More accurate unavailable-exit classifications**: missing original-pool data, observed depletion, and insufficient modeled quote depth are distinguished in write-off evidence. If the secondary exact-pool provider reports meaningful positive liquidity, a missing primary listing alone does not establish a zero-liquidity write-off. No assertion that a rug pull was intentionally committed or irreversible.
- **Versioned performance**: new paper entries have `strategy_version=v8.6.3_shadow`. Closed trade reports include a `PAPER SHADOW LIQUIDITY PERFORMANCE` summary stratified by **entry liquidity** and **entry 5-minute volume**, with wins, write-offs, and paper P&L. Old trades retain their version labels and saved data.

## Trading configuration retained

- Entry: 5–12-minute original-pool shadow signal; >=$35K reported cap, >=$15K SOL pool liquidity, >=$1,400 five-minute volume, >=10 buys, >=1 sell, buy/sell ratio >=1.25, +0.8% to +16% five-minute change.
- Price-based shadow exits: +8% or -12% modeled net at 180-second samples or 45-minute expiry. New liquidity-protection exits are an intentionally **additional** risk safeguard independent of that timing.
- $2 new shadow entries, $10 default allocation for other/legacy profiles, 50% daily-loss limit, paper-only, no on-chain execution.
- Existing open early-shadow positions receive the new liquidity monitor once the redeploy starts, with missing entry liquidity metrics populated on the first verified primary observation. Existing positions keep their entry cost and sampled shadow exit logic; legacy positions preserve legacy exits.

## Deployment (existing Railway)

1. Upload the **contents** of the full ZIP to the GitHub repo root (do not upload the ZIP as a single file). Or, from v8.6.2, replace **`bot.py`**, **`clean_boot.py`**, and add **`shadow_risk_v863.py`**. The new test file is optional in Railway but should remain in GitHub for regression checks.
2. **Keep** the `/data` Railway volume mounted, existing `CLEAN_*` API keys, and Start Command `python clean_boot.py`. Do not upload a local `.env`, token secrets, or existing state files from your device. Use a single Railway replica.
3. Verify `CLEAN BOOT PROFILE=shadow_primary_v8.6.3` and `max_positions=5`. Look for `PAPER SHADOW ACTIVE BUY`, `PAPER SHADOW ENTRY VETO reason=shadow_independent_pinned_pool_unavailable`, `PAPER SHADOW LIQUIDITY ALERT`, `PAPER SELL reason=shadow_liquidity_protection_*`, `PAPER SHADOW LIQUIDITY PERFORMANCE`.
4. **Normal:** some formerly qualifying shadow signals will now be vetoed when independent data cannot confirm the same pool. Zero positions can be appropriate; don't disable the guard solely to force more simulated trades.

Test locally with `python clean_boot.py --self-test` (synthetic, no network) and `pytest -q`.

## Remaining limitations

- The entry sellability model is an *estimated* amount using public pool data; it does **not** call Jupiter for an authenticated, token-decimal-aware executable sell quote. A simulated protective sale cannot guarantee a real exit, especially during sudden liquidity removal or provider lag.
- The scanner still performs regular held-token lookups about every 5 seconds while liquidity *risk actions* are rate-limited to ~20 seconds. API failures, data lag, network congestion and liquidity changes can create significant divergence between paper returns and realized returns.
- Five simultaneous shadow positions increase maximum capital exposed to young pools compared with v8.6.2. No claim of positive expected return is supported by the historical small sample.

# Shadow Primary v8.7.5 — additional unhealthy-entry and loss-observation controls

**PAPER ONLY.** This is a conservative iteration of the user-supplied v8.7.4 ZIP, not a proven profitable strategy. No wallet signing, broadcasting, real-order execution or guaranteed stops. Keep `python clean_boot.py` as the Railway start command.

## Why this patch

The retained Oct 18 v8.7.4 Claude AI paper ledger shows a $2 entry, 5.26-minute pool age, 5m change +14.76% and 5m volume $1,421.23; a read-only, same-pool quote supported an estimated exit 83 seconds later at −84.14%, and the persistent circuit breaker correctly halted new paper entries. The available evidence does **not** establish when price first crossed the −9% stop or that a real swap would have filled. In prior simulations two other −85% losses also occurred on high 5m price changes and low 5m volume. These are *in-sample observations*, not predictive validation.

## Changes from v8.7.4

1. `bot.py`: preserve original 5–12m / +0.8–16% / $1,400 / ratio 1.25 **shadow candidate signal**. Add a **separate prebuy safety veto** when 5m price appreciation is **>=13.5%** AND 5m USD volume is **<$2,500**. Configurable and applied only after a valid original shadow signal, before expensive independent/RPC/Jupiter entry verification. Example: the observed Claude AI, TikTok US, and Mr Beast signals would be rejected by this additional rule, while the $7,263 / +15.65% YouTube observation would not be vetoed. This is a retrospective comparison, not a verified improvement in win rate or expectancy.
2. `bot.py`: budgeted proactive, read-only, exact-pool Jupiter *sell quote* for already-held shadow positions, nominal **15 seconds** per position and **12 monitoring quotes per rolling minute globally**. Compare SOL-denominated output with the size-adjusted entry quote. When deterioration crosses **−9%**, record an exit intention, then require both a contemporaneous same-pool primary price/positive liquidity and that newly pinned route quote before recording a **conservative paper estimate**. Still not a transaction or guaranteed fill; SOL/USD movement can affect comparisons.
3. Exit-priority repair: on indexer-confirmed stop, exit intent, sudden liquidity decline, or expiry, skip the optional probe and use the normal emergency route. Routine successful probes no longer block a new emergency quote for twelve seconds. Failed quotes remain throttled to avoid API hammering. A same-tick successful probe can be reused for an exit; a prior-tick quote is never reused as a fill.
4. Persistent bounded price observations (last 80) and max interval between usable primary quotes, plus most recent/worst routed quote changes, saved in the *existing paper metrics*. These improve root cause analysis, **not** actual quote speed. The bot already refreshes open positions on each 5-second main loop and requests indexer data; this upgrade adds independent route observations subject to budget/provider latency.
5. `portfolio_report.py`: report open-position route health, primary quote-gap metrics and diagnostics in recent paper closes.
6. `clean_boot.py`: pins new paper-only profile settings and logs `CLEAN BOOT HEALTH_PROTECTION=v8.7.5`.

## New pinned Railway profile (set automatically by `clean_boot.py`)

```
SHADOW_UNHEALTHY_ENTRY_GUARD=true
SHADOW_SURGE_CHANGE_5M_PCT=13.5
SHADOW_SURGE_MIN_5M_VOLUME_USD=2500
SHADOW_ROUTE_MONITOR_ENABLED=true
SHADOW_ROUTE_MONITOR_INTERVAL_SECONDS=15
SHADOW_ROUTE_MONITOR_MAX_PER_MINUTE=12
SHADOW_ROUTE_MONITOR_NET_STOP_PCT=-9
```

Original entry signal, $2 paper sizing, five position cap, v8.7.4 exit-route requirement, missing-route handling and catastrophic-loss / writeoff halts remain enabled. `clean_boot.py` pins these values; arbitrary Railway variables with the same names do not override the pinned profile when using that launcher. Do **not** disable safety circuits to increase activity.

## Deployment

1. Stop new trading during deployment; make a backup of the Railway `/data` volume and its currently selected `CLEAN_STATE_FILE` JSON. Never delete or overwrite portfolio files.
2. Replace the repository-root source files with **the contents of this ZIP**, commit/push and redeploy the Railway service. Keep your existing Solana RPC URL, Jupiter API key, volume mount `/data`, `CLEAN_STATE_FILE`, `CLEAN_STARTING_CAPITAL_USD=36` and start command `python clean_boot.py`.
3. **Important:** existing `/data/shadow_v874_fresh36_run001.json` from the Claude AI loss contains `halted=true` and `shadow_exit_safety_halt=true`. v8.7.5 **preserves this halt**. Re-deploying does not re-enable entries. Keep this historic ledger as-is. For a strictly separate paper test after reviewing that halt, select an unused state path such as `/data/shadow_v875_test36_run001.json`; doing so creates a **new simulation**, not an authorized clearing of the historical loss.
4. Verify `CLEAN BOOT HEALTH_PROTECTION=v8.7.5`, `PAPER SHADOW HEALTH PROTECTION v8.7.5`, `old_strategy_entries_disabled=True`, the correct state-file path, and the `PAPER SHADOW RISK AUDIT`. For suppressed entries look for `PAPER SHADOW UNHEALTHY ENTRY SKIP`; for held exits, `PAPER SHADOW ROUTED DOWNSIDE OBSERVED`. Never claim actual swaps or live profitability from paper results.
5. Monitor for Jupiter rate limits and `PAPER SHADOW HELD ROUTE PROBE ...` messages. The read-only check may add provider calls. Missing quotes do **not** mean the token was sold; protect against write-off/accounting illusions by preserving pending-exit records and circuit breakers.

## Tests/limitations

- `python clean_boot.py --self-test`: passed with synthetic candidates, no network or real orders.
- `python -m py_compile *.py`: passed.
- `pytest -q`: **107 passed** offline, including new targeted tests for unhealthy entries, independent quote stopping, quote throttling/priority, fail-closed missing routes, and portfolio reporting.
- Read-only routing was tested with mocked responses; no live Solana or Railway/Jupiter execution validation. 15-second observations are neither real-time fills nor protection from sudden instantaneous crashes. This rule was chosen after observing three losses; requires out-of-sample validation before any claims of improved profitability.

Files modified: `bot.py`, `clean_boot.py`, `portfolio_report.py`. New: `test_shadow_health_protection_v875.py`, this notes file. All other shipping files are retained from v8.7.4.

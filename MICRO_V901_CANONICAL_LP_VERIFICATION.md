# v9.0.1 — Verified PumpSwap migration pools / LP-withdrawal evidence (paper-only)

## What to deploy

Starting from **your current v9.0.0 GitHub repository**, replace exactly one existing file and add one new module in the repository root:

- **Replace** `micro_rug_guard_v895.py` (the original import filename remains unchanged).
- **Add** `pumpswap_pda_v901.py` (new dependency-free, read-only Solana PDA derivation module).

The archive contains these files, this guide, and optional tests. **Do not replace `bot.py`, `shadow_age_tracks_v876.py`, `quote_observer_v893.py`, `research_risk_v893.py`, or your Railway startup script** for this update.

Deploy to Railway normally and retain:

```
MICRO_0_60_RUN_ID=v896_fresh36_run001
```

Keep the existing `CLEAN_STATE_FILE`, `/data` persistent volume, and `python paper_only_boot.py` startup command. Do not delete or rename any prior state files. The existing paper ledger and positions are left untouched by this code release.

## Correcting the LP security classifier

PumpSwap Pool account creator, index, LP mint, vaults, and `lp_supply` fields are checked against on-chain account data. The new `pumpswap_pda_v901.py` module deterministically derives and checks:

1. The Pool address from the **PumpSwap** program `['pool', index_u16, creator, base_mint, quote_mint]` seeds, including the canonical bump.
2. The Pool LP mint from `['pool_lp_mint', pool]` under the **PumpSwap** program.
3. For canonical migration classification, `index == 0`, `base_mint == target token`, `quote_mint == WSOL`, and the **Pump bonding program** migration creator PDA derived from `['pool-authority', base_mint]`.
4. Pool account ownership, Anchor discriminator, token mint pairing, and confirmed, independently identified vaults continue to be mandatory.

The verifier correctly handles an official migration pool that has `lp_supply == 0` **and** `getTokenSupply(lp_mint) == 0`. This case was previously rejected just because historical LP supply was zero. Zero/zero LP alone is **never sufficient** for a custom or unverified pool.

For every pool with a positive historical LP supply, the verifier calculates **maximum LP-withdrawal exposure** as outstanding LP supply / historical LP supply, using exact integers at the 1% threshold. Custom pools and official migration pools with additions still require **at least 99% provably burned**. A pool with 98.999% burned stays blocked; unverified custodial or third-party LP locks **are counted as potentially withdrawable**.

The newest PumpSwap layouts can report `coin_creator` separately from the pool `creator`. The holder check now uses `coin_creator` when provided and explicitly reports `coin_creator_identity_available=False` when it isn't. It does not claim to identify undisclosed related wallets.

New audit events:

```
V901 PUMPSWAP LP VERIFIED ... classification=canonical_migration ...
V901 PUMPSWAP LP REJECT ... reason=withdrawable_exposure ...
V901 PUMPSWAP LP REJECT ... reason=custom_zero_supply_not_verified_migration ...
```

The existing `V895 MICRO AGE+RUG AUDIT` / `V897 MICRO THROUGHPUT AUDIT` now include `canonical_pool_verification` and `pool_type_counts` among pool guard diagnostics. **To evaluate effectiveness, compare unique verified canonical pools, LP-withdrawal rejections, candidate-to-Jupiter progression, and new trades.** Rejection counts represent repeated attempts, not distinct unsafe tokens.

## What is *not* claimed

- No on-chain validation against *your actual candidate pools* was possible in the offline test environment. The implementation uses synthetic RPC fixtures and published PumpSwap pool/PDA examples; inspect Railway logs and actual Solana provider data before relying on the new verdicts.
- **No third-party locker adapter has been added.** There is no verified locker program address, interface layout, lock beneficiary, or unlock-time guarantee in the provided project. Metadata claiming "locked" is not proof. Those pools remain ineligible unless their LP is provably burned.
- PDA verification establishes pool authenticity, **not** immunity from price crashes, wash trading, or token holders selling. LP tokens could be minted by additional pool deposits later; cached security conclusions expire and the independent SOL vault reserve monitor remains active.
- Paper Jupiter quotes are not live trade fills. Stop losses and liquidity exits can slip or fail if real liquidity vanishes.
- This release affects the paper-only `micro_0_60` gate, does not enable live trades, does not weaken the 5–8-minute entry ban, age-five exit intent, 0–60 minute bounds, and does not change the $2 paper position size.

## Testing

From the same directory as the source modules:

```
python -m pytest -q
python paper_only_boot.py --self-test
```

Recorded in this preparation: **306 tests passed**, plus the offline paper-only self-test. Added 11 v9.0.1 tests covering published creator/pool PDA address vectors, official 0/0 LP, fake migration indices, fake LP mint PDA, pool PDA mismatch, 99%-burn boundary, large outstanding LP, reversed SOL pairs, `coin_creator` concentration screening, and LP-supply inconsistency.

## Published references

- https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_SWAP_README.md
- https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_SWAP_CREATOR_FEE_README.md

Official documentation distinguishes the migration authority (`pool-authority`, Pump program) and canonical index `0` from an arbitrary `create_pool` pool. The on-chain LP mint supply is the actual outstanding balance; `Pool.lp_supply` is the historical counter used to distinguish direct burns.

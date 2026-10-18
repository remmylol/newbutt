"""Clean Railway launch for the established-token PAPER simulator.

Resets legacy strategy variables to a pinned, auditable preset. This is
intentionally not a live trading launcher, and never imports wallet signers
in its preflight. The --self-test mode uses SYNTHETIC data without network I/O.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import runpy
import sys
import time
from pathlib import Path
from typing import MutableMapping

ROOT = Path(__file__).resolve().parent

# Keep a small, documented strategy profile. Edit this file and redeploy to
# change filters instead of carrying over years of Railway variables.
PAPER_PRESET = {
    "PAPER_MODE": "true",
    "PAPER_SHADOW_ACTIVE": "true",
    "LIVE_TRADING_ENABLED": "false",
    "MOMENTUM_SWING_MODE": "true",
    "SNIPER_MODE": "false",
    "PAPER_EXPLORATION_ENABLED": "false",
    "FRESH_LAUNCH_FEED_ENABLED": "true",
    "FRESH_LAUNCH_POOL_LIMIT": "10000",
    "PUMP_CURVE_LOOKUP_ENABLED": "false",
    "FOMO_BOARDS_ENABLED": "false",
    "PUMPFUN_BOARDS_ENABLED": "false",
    "MOMENTUM_MARKET_CAP_MIN_USD": "35000",
    "MOMENTUM_MARKET_CAP_MAX_USD": "250000000",
    "MOMENTUM_ALLOW_FDV_FALLBACK": "true",
    "MOMENTUM_MIN_PAIR_AGE_MINUTES": "12",
    "MOMENTUM_MAX_PAIR_AGE_HOURS": "0",
    "MOMENTUM_MIN_LIQUIDITY_USD": "15000",
    "MOMENTUM_MIN_VOLUME_5M_USD": "800",
    "MOMENTUM_MIN_VOLUME_1H_USD": "0",
    "PAPER_NO_1H_REQUIREMENT": "true",
    "MOMENTUM_MIN_TXNS_5M": "8",
    "MOMENTUM_MIN_BUY_SELL_RATIO": "1.05",
    "MOMENTUM_MIN_VOLUME_LIQUIDITY_RATIO": "0.005",
    "MOMENTUM_MIN_CHANGE_5M_PCT": "0.2",
    "MOMENTUM_MAX_CHANGE_5M_PCT": "18",
    "MOMENTUM_MIN_CHANGE_1H_PCT": "1.5",
    "MOMENTUM_MIN_CHANGE_6H_PCT": "0",
    "MOMENTUM_MIN_CHANGE_24H_PCT": "-1",
    "MOMENTUM_PAPER_DEXES": "pumpswap,raydium,orca,meteora",
    "MOMENTUM_TRENDING_POOL_PAGES": "5",
    "MOMENTUM_TOP_POOL_PAGES": "2",
    "QUALITY_POOL_DISCOVERY_ENABLED": "true",
    "QUALITY_POOL_CACHE_SECONDS": "60",
    "INDEPENDENT_MOVERS_ENABLED": "true",
    "EXPANDED_MOMENTUM_DISCOVERY_ENABLED": "true",
    "RECENT_PROFILE_DISCOVERY_ENABLED": "true",
    "QUALITY_POOL_SCAN_QUOTA": "22",
    "QUALITY_POOL_MIN_VOLUME_1H_USD": "0",
    "ROLLING_DISCOVERY_ENABLED": "true",
    "CANDIDATE_QUALITY_ENABLED": "true",
    "FRESH_BATCH_DISCOVERY_ENABLED": "true",
    "FRESH_BATCH_ACTIVE_LIMIT": "40",
    "ROLLING_DISCOVERY_WATCHLIST_SIZE": "750",
    "ROLLING_DISCOVERY_WATCHLIST_TTL_SECONDS": "21600",
    "ROLLING_DISCOVERY_FRESH_SCAN_QUOTA": "24",
    "DISCOVERY_SEARCH_TERMS": "solana,bonk,pepe,ai,meme,cat,dog,frog,inu,moon,pump,degen,agent,gaming,trump,wojak,coin,sol,orca,raydium,meteora,memecoin,shib,based,sui,bitcoin,chad,doge,terminal,viral,robot,bull",
    "DISCOVERY_SEARCHES_PER_SCAN": "3",
    "MAX_ENTRY_TOKENS_PER_SCAN": "60",
    "ENTRY_SCAN_INTERVAL_SECONDS": "15",
    "MARKET_REQUEST_SPACING_SECONDS": "1.5",
    "MIN_SCORE": "62",
    "POSITION_SIZE_USD": "10",
    "MAX_POSITIONS": "5",
    "MAX_SHADOW_POSITIONS": "5",
    "SHADOW_REQUIRE_INDEPENDENT_POOL": "false",
    "SHADOW_LIQUIDITY_MONITOR_SECONDS": "180",
    "SHADOW_LIQUIDITY_EXIT_DROP_PCT": "40",
    "MAX_HOLD_MINUTES": "45",
    "PAPER_FAST_ONLY": "true",
    "PAPER_FAST_TP_NET_PCT": "8",
    "PAPER_PROFIT_TRAIL_ENABLED": "true",
    "PAPER_PROFIT_TRAIL_ARM_NET_PCT": "5",
    "PAPER_PROFIT_TRAIL_GIVEBACK_PCT": "2.5",
    "PAPER_LOSS_CIRCUIT_ENABLED": "true",
    "PAPER_PROFIT_LOCK_V81": "true",
    "SCAN_INTERVAL_SECONDS": "5",
    "MAX_DAILY_LOSS_FRACTION": "0.5",
    "ENTRY_TREND_CONFIRM_ENABLED": "true",
    "PAPER_PREBUY_SELL_CHECK_ENABLED": "true",
    "PAPER_PREBUY_INDEPENDENT_POOL_CHECK": "true",
    "PAPER_REVERSAL_EXIT_ENABLED": "true",
    "PAPER_COST_ENTRY_ENABLED": "true",
    "PAPER_COST_ENTRY_BUFFER_PCT": "1.5",
    "PAPER_COST_ENTRY_MIN_5M_PCT": "0.5",
    "PAPER_FINAL_SNAPSHOT_ENABLED": "true",
    "PAPER_FINAL_SNAPSHOT_MAX_AGE_SECONDS": "12",
    "PAPER_FINAL_SNAPSHOT_MAX_DROP_PCT": "0.15",
    "PAPER_FINAL_SNAPSHOT_MAX_RISE_PCT": "2",
    "PAPER_EARLY_LOSS_REVERSAL_ENABLED": "true",
    "PAPER_EARLY_LOSS_MIN_HOLD_SECONDS": "30",
    "PAPER_EARLY_LOSS_NET_PCT": "7",
    "PAPER_EARLY_LOSS_MARKET_DROP_PCT": "2",
    "PAPER_SUSTAINED_LOSS_ENABLED": "true",
    "PAPER_SUSTAINED_LOSS_NET_PCT": "8.5",
    "PAPER_SUSTAINED_LOSS_MARKET_DROP_PCT": "4.5",
    "PAPER_SUSTAINED_LOSS_MIN_HOLD_SECONDS": "120",
    "PAPER_SUSTAINED_LOSS_CONFIRM_SECONDS": "60",
    "PAPER_SUSTAINED_LOSS_LOOKBACK_SECONDS": "300",
    "PAPER_LOSS_REENTRY_COOLDOWN_SECONDS": "3600",
    "PAPER_SUSTAINED_LOSS_REENTRY_COOLDOWN_SECONDS": "3600",
    "PAPER_LOSS_REENTRY_NET_PCT": "7",
    "STOP_LOSS_PCT": "0.15",
    "PAPER_EXIT_SIGNAL_LOG_INTERVAL_SECONDS": "30",
    "PAPER_RUG_WRITEOFF_ENABLED": "true",
    "PAPER_RUG_WRITEOFF_AFTER_SECONDS": "300",
    "PAPER_RUG_WRITEOFF_MIN_CHECKS": "3",
    "MOMENTUM_REJECT_COOLDOWN_SECONDS": "60",
    "MOMENTUM_COOLDOWN_PROBE_QUOTA": "4",
    "MOMENTUM_COOLDOWN_PROBE_GAP_SECONDS": "90",
    "PAPER_REENTRY_ENABLED": "false",
}

# Explicitly support only these few fresh-install customizations. All other
# old bot settings, especially old MIN_SCORE and liquidity thresholds, are
# erased before bot.py imports its module-level constants.
CUSTOM_OVERRIDES = {
    "CLEAN_STARTING_CAPITAL_USD": ("STARTING_CAPITAL_USD", 5.0, 100000.0),
    "CLEAN_POSITION_SIZE_USD": ("POSITION_SIZE_USD", 5.0, 10000.0),
    "CLEAN_MARKET_CAP_MIN_USD": ("MOMENTUM_MARKET_CAP_MIN_USD", 35000.0, 250000000.0),
    "CLEAN_MARKET_CAP_MAX_USD": ("MOMENTUM_MARKET_CAP_MAX_USD", 500000.0, 1000000000.0),
}


def bot_environment_keys() -> set[str]:
    """Find the bot's env reader keys; keep framework/OS variables intact."""
    result = set(PAPER_PRESET)
    for file in ROOT.glob("*.py"):
        source = file.read_text(encoding="utf8")
        if file.name == "clean_boot.py":
            continue
        result.update(re.findall(r'os\.getenv\([\'\"]([A-Z][A-Z0-9_]*)[\'\"]', source))
        result.update(re.findall(r'os\.environ\.get\([\'\"]([A-Z][A-Z0-9_]*)[\'\"]', source))
    return result


def apply_clean_profile(env: MutableMapping[str, str]) -> tuple[list[str], str]:
    overrides: dict[str, str] = {}
    # Optional free DexPaprika key is separate from the pinned strategy profile.
    # Preserve only this specifically named credential; never log its contents.
    paprika_key = env.get("CLEAN_DEXPAPRIKA_API_KEY", "").strip()
    jupiter_key = env.get("CLEAN_JUPITER_API_KEY", "").strip()
    birdeye_key = env.get("CLEAN_BIRDEYE_API_KEY", "").strip()
    pumpportal_key = env.get("CLEAN_PUMPPORTAL_API_KEY", "").strip()
    for external, (internal, minimum, maximum) in CUSTOM_OVERRIDES.items():
        if external in env and env[external].strip():
            value = float(env[external])
            if not minimum <= value <= maximum:
                raise ValueError(f"{external} outside allowed range [{minimum}, {maximum}]")
            overrides[internal] = format(value, "g")
    cap_min = float(overrides.get("MOMENTUM_MARKET_CAP_MIN_USD", PAPER_PRESET["MOMENTUM_MARKET_CAP_MIN_USD"]))
    cap_max = float(overrides.get("MOMENTUM_MARKET_CAP_MAX_USD", PAPER_PRESET["MOMENTUM_MARKET_CAP_MAX_USD"]))
    if cap_min > cap_max:
        raise ValueError("CLEAN_MARKET_CAP_MIN_USD cannot exceed CLEAN_MARKET_CAP_MAX_USD")
    ignored = sorted(k for k in bot_environment_keys() if k in env and k not in ("STATE_FILE", "STARTING_CAPITAL_USD"))
    # STATE_FILE is also ignored. Only CLEAN_STATE_FILE can select the paper ledger,
    # so a stale Railway setting won't accidentally restore a halted portfolio.
    for key in bot_environment_keys() | {"STATE_FILE", "STARTING_CAPITAL_USD"}:
        env.pop(key, None)
    env.update(PAPER_PRESET)
    env.update(overrides)
    if paprika_key:
        env["DEXPAPRIKA_API_KEY"] = paprika_key
    if jupiter_key:
        env["JUPITER_API_KEY"] = jupiter_key
    if birdeye_key:
        env["BIRDEYE_API_KEY"] = birdeye_key
    if pumpportal_key:
        env["PUMPPORTAL_API_KEY"] = pumpportal_key
    env["STARTING_CAPITAL_USD"] = overrides.get("STARTING_CAPITAL_USD", "36")
    mount = env.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    path = env.get("CLEAN_STATE_FILE", "").strip()
    if not path:
        path = str(Path(mount) / "clean_paper_state.json") if mount else str(ROOT / "clean_paper_state.json")
    env["STATE_FILE"] = path
    # Fail-closed regardless of any host configuration, both when testing and running.
    env["PAPER_MODE"] = "true"
    env["LIVE_TRADING_ENABLED"] = "false"
    return ignored, path


def offline_self_test() -> None:
    """Exercise discovery->entry eligibility->sellability->paper accounting offline."""
    import bot
    from datetime import date
    from collections import Counter
    mint = "A" * 44
    now = int(time.time() * 1000)
    good = {
        "chainId": "solana", "dexId": "raydium", "pairAddress": "B" * 44,
        "baseToken": {"address": mint, "symbol": "TEST", "name": "Synthetic test"},
        "quoteToken": {"address": bot.SOL_MINT, "symbol": "SOL"},
        # A synthetic $5M cap is NOT valid for every allowed CLEAN_* cap range.
        # Keep the fixture inside the ACTIVE profile's configured bounds.
        "priceUsd": "0.01", "marketCap": max(bot.MOMENTUM_MARKET_CAP_MIN_USD,
                                                min(bot.MOMENTUM_MARKET_CAP_MAX_USD, 5_000_000)),
        "liquidity": {"usd": 120_000}, "pairCreatedAt": now - 7 * 60 * 1000,
        "volume": {"m5": 4500, "h1": 55000, "h6": 200000, "h24": 500000},
        "priceChange": {"m5": 1.3, "h1": 5, "h6": 8, "h24": 12},
        "txns": {"m5": {"buys": 24, "sells": 15}},
    }
    c = bot.shadow_paper_candidate(good)
    assert c is not None, 'healthy 7-minute shadow fixture was not eligible'
    bad_downtrend = {**good, 'priceChange': {'m5': -4.0, 'h1': -8.0}}
    assert bot.shadow_paper_candidate(bad_downtrend) is None, 'negative 5m trend not rejected'
    missing_hour = {**good, 'volume': {'m5': 4500},
                    'priceChange': {'m5': 1.3}}
    assert bot.shadow_paper_candidate(missing_hour) is not None, 'shadow requires hourly data'
    old_pool = {**good, 'pairCreatedAt': now - 13*60*1000}
    assert bot.shadow_paper_candidate(old_pool) is None, '12+ minute pool allowed'
    illiquid = {**good, 'liquidity': {'usd': None}}
    assert bot.shadow_paper_candidate(illiquid) is None, 'unverified liquidity accepted'
    state = {'cash_usd': bot.STARTING_CAPITAL, 'positions': {}, 'closed_trades': [],
             'rugged_mints': [], 'seen_tokens': [], 'day': str(date.today()),
             'day_start_equity': bot.STARTING_CAPITAL, 'halted': False}
    assert bot.PAPER_SHADOW_ACTIVE, 'exclusive shadow profile disabled'
    original_queue_state = bot.FRESH_BATCH_DISCOVERY_ENABLED
    original_fallback = bot.held_pool_fallback
    bot.held_pool_fallback = lambda pool, mint: {"pool_address": pool, "price": .01, "liquidity": 120_000}  # synthetic independent provider
    bot.FRESH_BATCH_DISCOVERY_ENABLED = False  # strictly no persistent files on self-test
    try:
        ok, reason = bot.shadow_paper_entry(state, good)
    finally:
        bot.FRESH_BATCH_DISCOVERY_ENABLED = original_queue_state
    assert ok, f'shadow paper entry blocked: {reason}'
    assert abs(state['positions'][mint]['amount_usd'] - bot.MICRO_35K_PAPER_SIZE_USD) < 0.00001
    assert abs(state['cash_usd']-(bot.STARTING_CAPITAL-bot.MICRO_35K_PAPER_SIZE_USD)) < 0.00001
    assert state['paper_trade_metrics'][mint]['strategy']=='early_shadow_active'
    assert bot.shadow_exit_signal(.01,.0115,180)[0]=='shadow_tp_8'
    assert bot.shadow_exit_signal(.01,.009,180)[0]=='shadow_loss_12'
    assert bot.shadow_exit_signal(.01,.01,45*60)[0]=='shadow_45m_expiry'
    # Original shadow observation is the entry signal AND observed entry price.
    # Later market snapshots must not change an earlier recorded paper buy.
    signal = bot.shadow_paper_candidate(good)
    changed = {**good, 'priceUsd':'0.011', 'priceChange':{'m5':-15},
               'volume':{'m5':1}, 'txns':{'m5':{'buys':0,'sells':10}}}
    second_state = {'cash_usd':bot.STARTING_CAPITAL, 'positions':{}, 'closed_trades':[],
                    'rugged_mints':[], 'seen_tokens':[], 'day':str(date.today()),
                    'day_start_equity':bot.STARTING_CAPITAL, 'halted':False}
    bot.FRESH_BATCH_DISCOVERY_ENABLED = False
    try:
        locked_ok, locked_reason = bot.shadow_paper_entry(second_state, changed, signal_candidate=signal)
    finally:
        bot.FRESH_BATCH_DISCOVERY_ENABLED = original_queue_state
    assert locked_ok, f'valid original shadow signal was re-filtered: {locked_reason}'
    assert abs(second_state['paper_trade_metrics'][mint]['entry_market_price']-.01)<1e-9
    assert second_state['paper_trade_metrics'][mint]['shadow_signal_snapshot_locked'] is True
    bot.held_pool_fallback = original_fallback
    print('CLEAN SELF TEST PASS: original single-snapshot entry, no-hour data, 5-12m entry, $2 young position, 8/-12/45 exits, persistent-ledger-compatible paper buy',flush=True)
    print("NO NETWORK REQUESTS, NO REAL TRADES, NO PERSISTENT STATE WRITTEN", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pinned PAPER-ONLY momentum launch")
    parser.add_argument("--self-test", action="store_true", help="Offline synthetic eligibility/entry rehearsal; exit afterwards")
    args = parser.parse_args(argv)
    try:
        ignored, state_path = apply_clean_profile(os.environ)
    except (ValueError, OSError) as exc:
        print(f"CLEAN BOOT CONFIG ERROR: {exc}", file=sys.stderr, flush=True)
        return 2
    print("CLEAN BOOT PROFILE=shadow_primary_v8.6.4_original_restore PAPER_MODE=true LIVE_TRADING_ENABLED=false", flush=True)
    print(f"CLEAN BOOT SHADOW_PRIMARY=v8.6.4_original_restore active=true exclusive_new_entries=true age=5-12m liquidity_min_usd=15000 cap_min_usd=35000 vol5_min_usd=1400 buys_min=10 buy_sell_min_ratio=1.25 exits=8pct_net_tp,-12pct_net_sl,45m sampled_every_180s cost=6pct position_usd=2.00 max_positions={os.environ['MAX_POSITIONS']} default_position_size_usd={os.environ['POSITION_SIZE_USD']} preserved_state=true paper_only=true", flush=True)
    print("CLEAN BOOT EXISTING_POSITIONS legacy_exit_rules_preserved=true new_shadow_entries_use_original_shadow_rules=true", flush=True)
    print("CLEAN BOOT SHADOW_DISCOVERY=v8.6.4_original_restore new_pool_gecko_pages=1,2,3 early_pool_hint=0_to_12m shadow_entry=5_to_12m pool_first_queue=true shadow_recheck_s=45 max_observations=12 raw_launch_probe_quota=2 single_original_observation_entry=true first_shadow_signal_locked=true paper_only=true",flush=True)
    print("CLEAN BOOT DATA_RECOVERY=v8.1 retries=3 interval_s=105 hot_nearmiss_retry_s=75 paper_only=true same_pool_verified=true", flush=True)
    print("CLEAN BOOT LEGACY_FAST_MOMENTUM=prior_saved_positions_only new_entry_filters_replaced_by_shadow=true paper_only=true", flush=True)
    print("CLEAN BOOT EARLY_MICRO=v8.6.4_original_restore min_cap_usd=35000 min_age_minutes=5 max_age_minutes_exclusive=12 micro_paper_size_usd=2.00 shadow_primary_active=true paper_only=true", flush=True)
    print("CLEAN BOOT FRESH_BATCH=v7.7 enabled sources=geckoterminal_fast_movers,dexpaprika_independent_movers,dexscreener_recent_profiles,dexscreener_search,raydium_volume,dexscreener_promotion_verified,jupiter_optional,birdeye_optional adaptive_deep_pages=enabled stale_queue_defer=enabled paper_only=True", flush=True)
    print(f"CLEAN BOOT LEGACY_CONFIG_IGNORED count={len(ignored)} keys={','.join(k for k in ignored if 'KEY' not in k and 'TOKEN' not in k and 'SECRET' not in k)}", flush=True)
    print(f"CLEAN BOOT INDEPENDENT_MOVERS=enabled key_configured={bool(os.environ.get('DEXPAPRIKA_API_KEY'))} "
          f"interval_s={90 if os.environ.get('DEXPAPRIKA_API_KEY') else 360} "
          f"state={str(Path(state_path).with_name('independent_movers_v73_state.json'))}", flush=True)
    print("CLEAN BOOT EXPANDED_DISCOVERY=v7.7 raydium_volume_public=true dex_promoted_verified=true "
          f"jupiter_key_configured={bool(os.environ.get('JUPITER_API_KEY'))} "
          f"birdeye_key_configured={bool(os.environ.get('BIRDEYE_API_KEY'))} "
          "paid_rankings_optional=true discovery_not_entry_approval=true paper_only=true",flush=True)
    print("CLEAN BOOT CANDIDATE_PIPELINE=v8.6.4_original_restore shadow_primary_entry_only=true verified_liquid_pool_required=true paper_only=true", flush=True)
    print("CLEAN BOOT LEGACY_MICROCAP_FLOW=old_saved_positions_only shadow_entry_requires_no_hour=true new_trades_5_to_12m=true paper_only=true", flush=True)
    print("CLEAN BOOT FAST_DISCOVERY=v8.2 gecko_shared_limit=6/min new_pools_pages=1,2,3_each_refresh gecko_early_5m_flow_hints=enabled jupiter_interval_s=120 primary_missing_vs_zero=logged paper_only=true", flush=True)
    print("CLEAN BOOT MIGRATION_DISCOVERY=v8.6.4_original_restore subscription=subscribeMigration same_socket=true event_recheck=enabled no_hour_required=true shadow_age_5_to_12m=true paper_only=true", flush=True)
    print("CLEAN BOOT LAUNCH_POOL_WATCH=v8.3 max_rechecks=4 attempts_bounded=true missing_dex_pool_retry=true entry_filters_unchanged=true paper_only=true", flush=True)
    print(f"CLEAN BOOT FRESH_FEED=v8.2.1 enabled=true pumpportal_key_configured={bool(os.environ.get('PUMPPORTAL_API_KEY'))} stream=new_tokens_and_migrations queue=bounded_age_qualified paper_only=true", flush=True)
    print(f"CLEAN BOOT MARKET_CAP_RANGE=${float(os.environ['MOMENTUM_MARKET_CAP_MIN_USD']):,.0f}-${float(os.environ['MOMENTUM_MARKET_CAP_MAX_USD']):,.0f}", flush=True)
    print(f"CLEAN BOOT STATE_FILE={state_path} railway_volume_env_present={bool(os.environ.get('RAILWAY_VOLUME_MOUNT_PATH'))}", flush=True)
    if os.environ.get("CANDIDATE_QUALITY_ENABLED") == "true":
        print(f"CLEAN BOOT CANDIDATE_QUALITY=enabled state={str(Path(state_path).with_name('candidate_quality_state.json'))} paper_only=True", flush=True)
    if os.environ.get("ROLLING_DISCOVERY_ENABLED") == "true":
        print(f"CLEAN BOOT ROLLING_DISCOVERY=enabled watchlist={str(Path(state_path).with_name('rolling_discovery_state.json'))} rotating_source_requests=3 search_terms_per_refresh=3 refresh_seconds={os.environ.get('QUALITY_POOL_CACHE_SECONDS', '90')}", flush=True)
    if not os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") and not args.self_test:
        print("CLEAN BOOT WARNING: no Railway volume detected; paper state may reset on deploy", flush=True)
    if args.self_test:
        offline_self_test()
        return 0
    # Behaves identically to `python bot.py` after safe config is applied.
    runpy.run_module("bot", run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

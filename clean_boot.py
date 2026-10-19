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
    "PUMPFUN_BOARDS_ENABLED": "true",
    "PUMPFUN_MOVERS_ENABLED": "true",
    "PUMPFUN_TRENDING_ENABLED": "false",
    "PUMPFUN_BOARDS_CACHE_SECONDS": "60",
    "PUMPFUN_BOARDS_LIMIT": "30",
    "MOMENTUM_MARKET_CAP_MIN_USD": "35000",
    "MOMENTUM_MARKET_CAP_MAX_USD": "250000",
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
    "SHADOW_REQUIRE_INDEPENDENT_POOL": "true",
    "SHADOW_SAFETY_PREBUY_VERIFICATION": "true",
    "SHADOW_REQUIRE_READONLY_EXIT_ROUTE": "true",
    "SHADOW_PREBUY_MAX_PRICE_DRIFT_PCT": "8",
    "SHADOW_PREBUY_MIN_LIQ_RATIO": "0.6",
    "SHADOW_PREBUY_MAX_LIQ_RATIO": "2.0",
    "SHADOW_PREBUY_MAX_IMPACT_PCT": "2",
    "SHADOW_PROTECTIVE_EXIT_ENABLED": "true",
    "SHADOW_PROTECTIVE_NET_STOP_PCT": "-9",
    "SHADOW_PROTECTIVE_PEAK_ARM_PCT": "12",
    "SHADOW_PROTECTIVE_PEAK_DROP_PCT": "7",
    "SHADOW_WRITEOFF_CIRCUIT_ENABLED": "true",
    "SHADOW_MAX_DAILY_WRITEOFFS": "1",
    "SHADOW_EXIT_ROUTE_QUOTE_REQUIRED": "true",
    "SHADOW_EXIT_ROUTE_RETRY_SECONDS": "12",
    "SHADOW_EXIT_UNAVAILABLE_HALT_SECONDS": "90",
    "SHADOW_SEVERE_POSITION_LOSS_HALT_PCT": "50",
    "SHADOW_CATASTROPHIC_CLOSED_LOSS_HALT_PCT": "50",
    "SHADOW_LIQUIDITY_MONITOR_SECONDS": "180",
    "SHADOW_UNHEALTHY_ENTRY_GUARD": "true",
    "SHADOW_SURGE_CHANGE_5M_PCT": "13.5",
    "SHADOW_SURGE_MIN_5M_VOLUME_USD": "2500",
    "SHADOW_AGE_STUDY_ENABLED": "true",
    "SHADOW_AGE_STUDY_FETCH_PER_SCAN": "3",
    # Promote the v8.7.6 0_60 entries; v8.7.8 updates primary exits only; WebSocket wakes
    # the very same observe() function and does not add trading restrictions.
    "AGE_0_60_PRIMARY": "true",
    "AGE_0_60_WS_ENABLED": "true",
    "SHADOW_ROUTE_MONITOR_ENABLED": "true",
    "SHADOW_ROUTE_MONITOR_INTERVAL_SECONDS": "15",
    "SHADOW_ROUTE_MONITOR_MAX_PER_MINUTE": "12",
    "SHADOW_ROUTE_MONITOR_NET_STOP_PCT": "-9",
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
    "MAX_DAILY_LOSS_FRACTION": "0.15",
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
    "CLEAN_MARKET_CAP_MAX_USD": ("MOMENTUM_MARKET_CAP_MAX_USD", 35001.0, 250000.0),
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
    movers_jwt = env.get("CLEAN_PUMPFUN_BOARD_JWT", "").strip()
    movers_session = env.get("CLEAN_PUMPFUN_BOARD_SESSION_ID", "").strip()
    movers_user = env.get("CLEAN_PUMPFUN_BOARD_USER_ID", "").strip()
    movers_tier = env.get("CLEAN_PUMPFUN_BOARD_TIER", "web").strip()
    if not movers_tier or not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", movers_tier):
        raise ValueError("CLEAN_PUMPFUN_BOARD_TIER must be a short tier label")
    solana_rpc = env.get("CLEAN_SOLANA_RPC_URL", "").strip()
    solana_ws = env.get("CLEAN_SOLANA_WS_URL", "").strip()
    live_request = env.get('CLEAN_LIVE_0_60_ENABLED','false').lower()=='true'
    new_buys_request=env.get('CLEAN_LIVE_NEW_BUYS_ENABLED','false').lower()=='true'
    if new_buys_request and not live_request:
        raise ValueError('new buys require live executor enabled (to maintain exit monitoring)')
    live_arm = env.get('CLEAN_LIVE_0_60_ARM','')
    live_secret = env.get('CLEAN_LIVE_SECRET_KEY_B58','')
    live_wallet = env.get('CLEAN_LIVE_PUBLIC_ADDRESS','')
    if live_request and (live_arm!='I_ACCEPT_AUTOMATIC_LIVE_TRADING' or not live_secret):
        raise ValueError('live enabled without exact opt-in or wallet secret')

    for external, (internal, minimum, maximum) in CUSTOM_OVERRIDES.items():
        if external in env and env[external].strip():
            value = float(env[external])
            if external == "CLEAN_MARKET_CAP_MAX_USD" and value > maximum:
                # Historical Railway overrides could otherwise silently re-enable
                # large-cap live purchases. Clamp explicitly to the hard ceiling.
                value = maximum
            if not minimum <= value <= maximum:
                raise ValueError(f"{external} outside allowed range [{minimum}, {maximum}]")
            overrides[internal] = format(value, "g")
    cap_min = float(overrides.get("MOMENTUM_MARKET_CAP_MIN_USD", PAPER_PRESET["MOMENTUM_MARKET_CAP_MIN_USD"]))
    cap_max = float(overrides.get("MOMENTUM_MARKET_CAP_MAX_USD", PAPER_PRESET["MOMENTUM_MARKET_CAP_MAX_USD"]))
    if cap_min >= cap_max:
        raise ValueError("CLEAN_MARKET_CAP_MIN_USD cannot exceed CLEAN_MARKET_CAP_MAX_USD")
    # Some optional maintenance scripts (e.g. portfolio_report.py) read CLEAN_*
    # variables. The dynamic legacy-variable scanner therefore sees them too.
    # Preserve launcher-owned CLEAN_* settings when clearing old bot settings;
    # otherwise CLEAN_STATE_FILE silently disappears before the path is read.
    launcher_keys = {"CLEAN_STATE_FILE", *CUSTOM_OVERRIDES,
                     "CLEAN_DEXPAPRIKA_API_KEY", "CLEAN_JUPITER_API_KEY",
                     "CLEAN_BIRDEYE_API_KEY", "CLEAN_PUMPPORTAL_API_KEY",
                     "CLEAN_SOLANA_RPC_URL", "CLEAN_SOLANA_WS_URL",
                     "CLEAN_LIVE_0_60_ENABLED", "CLEAN_LIVE_0_60_ARM",
                     "CLEAN_LIVE_SECRET_KEY_B58", "CLEAN_LIVE_PUBLIC_ADDRESS",
                     "CLEAN_LIVE_NEW_BUYS_ENABLED", "CLEAN_LIVE_MAX_ROUNDTRIP_LOSS_PCT",
                     "CLEAN_LIVE_ESTIMATED_EXIT_FEE_SOL",
                     "CLEAN_PUMPFUN_BOARD_JWT", "CLEAN_PUMPFUN_BOARD_SESSION_ID",
                     "CLEAN_PUMPFUN_BOARD_USER_ID", "CLEAN_PUMPFUN_BOARD_TIER"}
    to_clear = (bot_environment_keys() | {"STATE_FILE", "STARTING_CAPITAL_USD"}) - launcher_keys
    ignored = sorted(k for k in to_clear if k in env)
    for key in to_clear:
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
    # JWT is optional for the new browser-observed indexer. User/session
    # parameters are independent Railway secrets. Never send the wallet key.
    for internal, value in (("PUMPFUN_BOARD_JWT", movers_jwt),
                            ("PUMPFUN_BOARD_SESSION_ID", movers_session),
                            ("PUMPFUN_BOARD_USER_ID", movers_user)):
        if value:
            env[internal] = value
        else:
            env.pop(internal, None)
    env["PUMPFUN_BOARD_TIER"] = movers_tier
    if solana_rpc:
        if not solana_rpc.startswith("https://"):
            raise ValueError("CLEAN_SOLANA_RPC_URL must be HTTPS")
        env["SOLANA_RPC_URL"] = solana_rpc
    if solana_ws:
        if not solana_ws.startswith("wss://"):
            raise ValueError("CLEAN_SOLANA_WS_URL must use wss://")
        env["AGE_0_60_WS_RPC_URL"] = solana_ws
    env["STARTING_CAPITAL_USD"] = overrides.get("STARTING_CAPITAL_USD", "36")
    mount = env.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    path = env.get("CLEAN_STATE_FILE", "").strip()
    if not path:
        path = str(Path(mount) / "clean_paper_state.json") if mount else str(ROOT / "clean_paper_state.json")
    env["STATE_FILE"] = path
    # PAPER_MODE remains true to preserve the original discovery/exits/profile;
    # only the promoted 0_60 AgeTrack is swapped for a LIVE transaction engine.
    env['PAPER_MODE']='true'
    env['LIVE_TRADING_ENABLED']='true' if live_request else 'false'
    env['LIVE_0_60_ENABLED']='true' if live_request else 'false'
    env['LIVE_0_60_NEW_BUYS_ENABLED']='true' if live_request and new_buys_request else 'false'
    if live_request:
        if not solana_rpc or not jupiter_key:
            raise ValueError('live requires CLEAN_SOLANA_RPC_URL and CLEAN_JUPITER_API_KEY')
        if not mount or not Path(mount).is_absolute():
            raise ValueError('live requires mounted persistent Railway volume')
        if not Path(path).is_absolute() or not Path(path).is_relative_to(Path(mount)):
            raise ValueError('live STATE_FILE must be inside persistent volume')
        env['LIVE_0_60_ARM']=live_arm
        env['LIVE_0_60_SECRET_KEY_B58']=live_secret
        if live_wallet:env['SOLANA_WALLET_ADDRESS']=live_wallet

    return ignored, path


def offline_self_test() -> None:
    """Exercise discovery->entry eligibility->sellability->paper accounting offline."""
    import bot
    from datetime import date
    from collections import Counter
    mint = "A" * 44
    now = int(time.time() * 1000)
    good = {
        "chainId": "solana", "dexId": "pumpswap", "pairAddress": "B" * 44,
        "baseToken": {"address": mint, "symbol": "TEST", "name": "Synthetic test"},
        "quoteToken": {"address": bot.SOL_MINT, "symbol": "SOL"},
        # A synthetic $5M cap is NOT valid for every allowed CLEAN_* cap range.
        # Keep the fixture inside the ACTIVE profile's configured bounds.
        "priceUsd": "0.01", "marketCap": max(bot.MOMENTUM_MARKET_CAP_MIN_USD,
                                                min(bot.MOMENTUM_MARKET_CAP_MAX_USD - 1, 200_000)),
        "liquidity": {"usd": 120_000}, "pairCreatedAt": now - 7 * 60 * 1000,
        "volume": {"m5": 4500, "h1": 55000, "h6": 200000, "h24": 500000},
        "priceChange": {"m5": 1.3, "h1": 5, "h6": 8, "h24": 12},
        "txns": {"m5": {"buys": 24, "sells": 15}},
    }
    # Synthetic depth scales with arbitrary cap overrides in offline tests.
    # This exercises the route gate without being blocked by its 1% risk rule.
    good['liquidity']['usd'] = max(120_000, float(good['marketCap']) * .02)
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
    original_pairs = bot.token_pairs
    original_pair_health = dict(bot._token_pairs_healthy)
    bot.token_pairs = lambda mint: [good]  # synthetic primary response, no HTTP
    bot._token_pairs_healthy[mint] = True
    bot.held_pool_fallback = lambda pool, mint: {"pool_address": pool, "price": .01, "liquidity": good["liquidity"]["usd"]}  # synthetic independent provider
    import read_only_exit_v872
    original_route_verify = bot.verify_paper_exit_route
    bot.verify_paper_exit_route = lambda *args, **kwargs: {'rpc_slot':123,'quote_min_sol_lamports':1000000,'read_only_route_quote_verified':True}
    bot.SOLANA_RPC_URL = "https://example.invalid"
    bot.JUPITER_API_KEY = "test-only"
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
    bot.verify_paper_exit_route = original_route_verify
    bot.held_pool_fallback = original_fallback
    bot.token_pairs = original_pairs
    bot._token_pairs_healthy.clear()
    bot._token_pairs_healthy.update(original_pair_health)
    print('CLEAN SELF TEST PASS: original shadow signal, new prebuy two-provider indicative verification, no-hour data, 5-12m entry, $2 position, 8/-12/45 baseline, paper-only protective exits, persistent-ledger-compatible buy',flush=True)
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
    print(f"CLEAN BOOT PROFILE=v8.8.2 promoted_0_60_ws_tp25_reversal LIVE_0_60_ENABLED={os.getenv('LIVE_0_60_ENABLED')=='true'} LIVE_0_60_NEW_BUYS_ENABLED={os.getenv('LIVE_0_60_NEW_BUYS_ENABLED')=='true'} no_manual_order_approval=True",flush=True)
    promoted_ledger = Path(state_path).with_name(f'age_study_0_60_{Path(state_path).stem}.json')
    print(f"CLEAN BOOT PRIMARY_0_60=v8.7.8 original_v876_entry_rules_unchanged=true "
          f"entry_age=0_to_under_60m tp_net_pct=25 profit_reversal=first_observed_decline_from_positive_net_peak stop_net_pct=-9 liquidity_loss_pct=40 max_hold_minutes=45 position_usd=2.00 "
          f"websocket_wakeup=true live_quote_safety_prebuy_sell_route=true live_new_buys_pause_supported=true automatic_loss_halts_enabled=false historical_trade_losses_preserved=true "
          f"ledger={promoted_ledger} live_primary_separate={os.getenv('LIVE_0_60_ENABLED')=='true'}",flush=True)
    print(f"CLEAN BOOT SOLANA_POOL_WS=wss_only accountSubscribe=held_exact_pools "
          f"rpc_https_configured={bool(os.environ.get('SOLANA_RPC_URL'))} "
          f"custom_wss_configured={bool(os.environ.get('AGE_0_60_WS_RPC_URL'))} "
          f"dex_price_on_wakeup=true no_unverified_sale=true",flush=True)
    print("CLEAN BOOT LEGACY_5_12=historical_ledger_retained new_entries_disabled=true paper_only=true",flush=True)
    print("CLEAN BOOT LEGACY_5_12_SAFETY=v8.7.2 two_provider=true onchain_pumpswap_vaults=true pinned_router_sell_quote_required=true unsupported_dex_entries_blocked=true writeoff_onchain_confirmed_only=true all_quotes_readonly=true protective_stop_net_pct=-9 daily_loss_halt_pct=15 first_writeoff_circuit_halt=true preserve_existing_halts=true",flush=True)
    print("CLEAN BOOT LEGACY_5_12_SHADOW_SCHEDULER=v8.7.3 5m_boundary_watch_preserved=true 5_to_12m_first=true cooldown_backfill=true expired_queue_retirement=true no_signal_or_safety_filter_changes=true",flush=True)
    print("CLEAN BOOT LEGACY_5_12_EXIT_INTEGRITY=v8.7.4 fresh_pinned_readonly_sell_quote_required=true "
          "shadow_expiry_checked_each_tick=true pending_exit_persisted=true "
          "severe_position_loss_circuit=true impaired_exit_halt=true catastrophic_closed_loss_halt=true "
          "entry_signals_unchanged=true no_real_fills=true", flush=True)
    print("CLEAN BOOT LEGACY_5_12_HEALTH_PROTECTION=v8.7.5 risk_screen=high_surge_thin_volume "
          "same_pool_router_probe=enabled bounded=12_per_minute "
          "quote_trace_bounded=true original_shadow_signal_unchanged=true "
          "risk_entries_additional_screen=true persistent_halts_preserved=true paper_only=true", flush=True)
    print("CLEAN BOOT AGE_TRACKS=v8.7.8 primary_0_to_60=true background_5_to_60=true "
          "original_flow_filters_same_v876=true market_cap_ceiling_added=true primary_exit_tp25_reversal=true background_5_60_original_exits=true promoted_loss_halt_disabled=true distinct_ledgers=true indicative_paper_only=true",flush=True)
    print("CLEAN BOOT EXISTING_POSITIONS legacy_open_positions_block_promotion=true "
          "age_0_60_prior_halt_nonblocking=true positions_and_history_persist=true",flush=True)
    print("CLEAN BOOT LEGACY_5_12_SHADOW_DISCOVERY=v8.6.4_original_restore new_pool_gecko_pages=1,2,3 early_pool_hint=0_to_12m shadow_entry=5_to_12m pool_first_queue=true shadow_recheck_s=45 max_observations=12 raw_launch_probe_quota=2 single_original_signal=true extra_prebuy_same_pool_sources=2 fresh_pool_identity_required=true paper_only=true",flush=True)
    print("CLEAN BOOT DATA_RECOVERY=v8.1 retries=3 interval_s=105 hot_nearmiss_retry_s=75 paper_only=true same_pool_verified=true", flush=True)
    print("CLEAN BOOT LEGACY_FAST_MOMENTUM=prior_saved_positions_only new_entry_filters_replaced_by_shadow=true paper_only=true", flush=True)
    print("CLEAN BOOT EARLY_MICRO=legacy_profile_not_main min_age_minutes=0 max_age_minutes_exclusive=60 position_size_usd=2.00 paper_only=true",flush=True)
    print("CLEAN BOOT FRESH_BATCH=v7.7 enabled sources=geckoterminal_fast_movers,dexpaprika_independent_movers,dexscreener_recent_profiles,dexscreener_search,raydium_volume,dexscreener_promotion_verified,jupiter_optional,birdeye_optional adaptive_deep_pages=enabled stale_queue_defer=enabled paper_only=True", flush=True)
    print(f"CLEAN BOOT LEGACY_CONFIG_IGNORED count={len(ignored)} keys={','.join(k for k in ignored if 'KEY' not in k and 'TOKEN' not in k and 'SECRET' not in k)}", flush=True)
    print(f"CLEAN BOOT INDEPENDENT_MOVERS=enabled key_configured={bool(os.environ.get('DEXPAPRIKA_API_KEY'))} "
          f"interval_s={90 if os.environ.get('DEXPAPRIKA_API_KEY') else 360} "
          f"state={str(Path(state_path).with_name('independent_movers_v73_state.json'))}", flush=True)
    print(f"CLEAN BOOT PRIMARY_MARKET_CAP=verified_marketCap_only max_usd_exclusive={float(os.environ['MOMENTUM_MARKET_CAP_MAX_USD']):g} exits_ignore_cap=True", flush=True)
    print(f"CLEAN BOOT PUMPFUN_MOVERS=enabled params_configured={bool(os.environ.get('PUMPFUN_BOARD_SESSION_ID') and os.environ.get('PUMPFUN_BOARD_USER_ID'))} optional_bearer_present={bool(os.environ.get('PUMPFUN_BOARD_JWT'))} tier={os.environ.get('PUMPFUN_BOARD_TIER', 'web')} interval_s=60 max_results=30 priority_scans_per_tick=4 discovery_only=True", flush=True)
    print("CLEAN BOOT EXPANDED_DISCOVERY=v7.7 raydium_volume_public=true dex_promoted_verified=true "
          f"jupiter_key_configured={bool(os.environ.get('JUPITER_API_KEY'))} "
          f"birdeye_key_configured={bool(os.environ.get('BIRDEYE_API_KEY'))} "
          "paid_rankings_optional=true discovery_not_entry_approval=true paper_only=true",flush=True)
    print("CLEAN BOOT CANDIDATE_PIPELINE=original_discovery_preserved legacy_5_12_entries_disabled=true paper_only=true",flush=True)
    print("CLEAN BOOT LEGACY_MICROCAP_FLOW=historical_only main_entry_age=0_to_60m paper_only=true",flush=True)
    print("CLEAN BOOT FAST_DISCOVERY=v8.2 gecko_shared_limit=6/min new_pools_pages=1,2,3_each_refresh gecko_early_5m_flow_hints=enabled jupiter_interval_s=120 primary_missing_vs_zero=logged paper_only=true", flush=True)
    print("CLEAN BOOT MIGRATION_DISCOVERY=v8.6.4_original_restore subscription=subscribeMigration same_socket=true event_recheck=enabled no_hour_required=true shadow_age_5_to_12m=true paper_only=true", flush=True)
    print("CLEAN BOOT LAUNCH_POOL_WATCH=v8.3 max_rechecks=4 attempts_bounded=true missing_dex_pool_retry=true entry_filters_unchanged=true paper_only=true", flush=True)
    print(f"CLEAN BOOT FRESH_FEED=v8.2.1 enabled=true pumpportal_key_configured={bool(os.environ.get('PUMPPORTAL_API_KEY'))} stream=new_tokens_and_migrations queue=bounded_age_qualified paper_only=true", flush=True)
    print(f"CLEAN BOOT MARKET_CAP_RANGE=${float(os.environ['MOMENTUM_MARKET_CAP_MIN_USD']):,.0f}-${float(os.environ['MOMENTUM_MARKET_CAP_MAX_USD']):,.0f}", flush=True)
    print(f"CLEAN BOOT STATE_FILE={state_path} railway_volume_env_present={bool(os.environ.get('RAILWAY_VOLUME_MOUNT_PATH'))}", flush=True)
    print("CLEAN BOOT STATE_PATH_FIX=v8.7.2 clean_state_file_preserved=True never_overwrite_existing_ledger=True", flush=True)
    if os.environ.get("CANDIDATE_QUALITY_ENABLED") == "true":
        print(f"CLEAN BOOT CANDIDATE_QUALITY=enabled state={str(Path(state_path).with_name('candidate_quality_state.json'))} paper_only=True", flush=True)
    if os.environ.get("ROLLING_DISCOVERY_ENABLED") == "true":
        print(f"CLEAN BOOT ROLLING_DISCOVERY=enabled watchlist={str(Path(state_path).with_name('rolling_discovery_state.json'))} rotating_source_requests=3 search_terms_per_refresh=3 refresh_seconds={os.environ.get('QUALITY_POOL_CACHE_SECONDS', '90')}", flush=True)
    if not os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") and not args.self_test:
        print("CLEAN BOOT WARNING: no Railway volume detected; paper state may reset on deploy", flush=True)
    if args.self_test and os.environ.get('LIVE_0_60_ENABLED')=='true':
        print('CLEAN BOOT SELF TEST is offline only; disable CLEAN_LIVE_0_60_ENABLED',file=sys.stderr)
        return 2
    if args.self_test:
        offline_self_test()
        return 0
    # Behaves identically to `python bot.py` after safe config is applied.
    runpy.run_module("bot", run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

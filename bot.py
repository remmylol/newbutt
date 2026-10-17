import json
import math
import re
import logging
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any
from collections import Counter, OrderedDict
from urllib.parse import urlencode
import threading
from pump_curve_market import lookup_curve
from curve_paper import buy_quote, sell_quote
from curve_activity import CurveActivity
from curve_rpc_activity import RpcCurveTradeTracker, ReadOnlySolanaRPC
from quality_pool_discovery import extract_quality_pool_mints
from rolling_discovery import RollingWatchlist, rotating_pool_sources, MINT_PATTERN
from independent_movers_v73 import IndependentMovers
from expanded_momentum_discovery_v77 import ExpandedMomentumDiscovery
from candidate_pipeline_v74 import CandidatePipeline
from fresh_batch_discovery import FreshBatchQueue
from candidate_quality import CandidateQualityLedger
from discovery_expansion import rotating_search_terms, search_pool_hints, DEFAULT_SEARCH_TERMS
from trade_signal_guards import entry_cost_headroom, fresh_pool_snapshot, early_loss_reversal
from risk_guards_v6 import sustained_loss_exit, paper_trade_scorecard
from pumpfun_boards import board_mints, valid_pumpfun_endpoint, MOVERS_URL, TRENDING_PROXY_URL
from fomo_boards import extract_fomo_mints, inspect_fomo_board, network_distribution, BASE_URL as FOMO_API_BASE_URL
from live_execution import execute_buy, execute_sell
print("EXECUTION BRIDGE LOADED - preview only, live trading disabled", flush=True)

import requests
from dotenv import load_dotenv
from early_watch_v75 import from_dex_pairs as early_dex_pairs, from_gecko_pools as early_gecko_pools
from fast_momentum_v76 import fast_mover_gate, confirmed_profit_trail, confirmed_loss_circuit
from fast_nearmiss_v78 import is_verified_fast_nearmiss

load_dotenv()

# ---- Configuration ----
STARTING_CAPITAL = float(os.getenv("STARTING_CAPITAL_USD", "36"))
PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() == "true"
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_SECONDS", "5"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "2"))
POSITION_SIZE_USD = float(os.getenv("POSITION_SIZE_USD", "10"))  # Fixed paper position size in USD
MAX_DAILY_LOSS_FRACTION = float(os.getenv("MAX_DAILY_LOSS_FRACTION", "0.5"))
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT_PCT", "0.20"))
STOP_LOSS = float(os.getenv("STOP_LOSS_PCT", "0.35"))
TRAILING_STOP_ENABLED = os.getenv("TRAILING_STOP_ENABLED", "false").lower() == "true"
TRAILING_STOP = float(os.getenv("TRAILING_STOP_PCT", "0.15"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "62" if PAPER_MODE and os.getenv("MOMENTUM_SWING_MODE", "true").lower() == "true" else "68" if PAPER_MODE else "70"))
# Experimental fills are PAPER-only, separately tracked, and never bypass
# verified-route, liquidity, buy-pressure, trend or prebuy sellability gates.
PAPER_EXPLORATION_ENABLED = PAPER_MODE and os.getenv("PAPER_EXPLORATION_ENABLED", "true").lower() == "true"
PAPER_EXPLORATION_MIN_SCORE = max(0., min(MIN_SCORE, float(os.getenv("PAPER_EXPLORATION_MIN_SCORE", "60"))))
PAPER_EXPLORATION_DAILY_LIMIT = max(0, min(10, int(os.getenv("PAPER_EXPLORATION_DAILY_LIMIT", "2"))))
PAPER_EXPLORATION_POSITION_SIZE_USD = min(POSITION_SIZE_USD, max(5., float(os.getenv("PAPER_EXPLORATION_POSITION_SIZE_USD", "5"))))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "5000"))
MIN_SOL_PAIR_LIQUIDITY_USD = max(5000.0, float(os.getenv("MIN_SOL_PAIR_LIQUIDITY_USD", "5000")))
MIN_VOLUME_5M_USD = float(os.getenv("MIN_VOLUME_5M_USD", "1000"))
MAX_PAIR_AGE_HOURS = float(os.getenv("MAX_PAIR_AGE_HOURS", "0.5"))
MIN_PAIR_AGE_MINUTES = float(os.getenv("MIN_PAIR_AGE_MINUTES", "1"))
FAST_MOMENTUM_ENABLED = os.getenv("FAST_MOMENTUM_ENABLED", "true").lower() == "true"
# Default to established-token momentum rather than fresh-launch sniping.
# Explicit momentum selection takes precedence over a stale Railway SNIPER_MODE=true.
MOMENTUM_SWING_MODE = os.getenv("MOMENTUM_SWING_MODE", "true").lower() == "true"
SNIPER_MODE = (not MOMENTUM_SWING_MODE and os.getenv("SNIPER_MODE", "false").lower() == "true")
MOMENTUM_MARKET_CAP_MIN_USD = max(0.0, float(os.getenv("MOMENTUM_MARKET_CAP_MIN_USD", "100000")))
SMALL_CAP_TIER_MAX_USD = 500000.0  # extra paper gates below $500K; stricter below $250K
SMALL_CAP_PAPER_SIZE_USD = 5.0  # half-size paper exposure in speculative tier
MOMENTUM_MARKET_CAP_MAX_USD = max(MOMENTUM_MARKET_CAP_MIN_USD, float(os.getenv("MOMENTUM_MARKET_CAP_MAX_USD", "250000000")))
MOMENTUM_ALLOW_FDV_FALLBACK = os.getenv("MOMENTUM_ALLOW_FDV_FALLBACK", "true").lower() == "true"
MOMENTUM_MIN_LIQUIDITY_USD = max(5000.0, float(os.getenv("MOMENTUM_MIN_LIQUIDITY_USD", "15000")))
MOMENTUM_MIN_PAIR_AGE_MINUTES = max(0.0, float(os.getenv("MOMENTUM_MIN_PAIR_AGE_MINUTES", "30")))
# Zero permits any age, including months and years old.
MOMENTUM_MAX_PAIR_AGE_HOURS = max(0.0, float(os.getenv("MOMENTUM_MAX_PAIR_AGE_HOURS", "0")))
MOMENTUM_MIN_VOLUME_1H_USD = max(0.0, float(os.getenv("MOMENTUM_MIN_VOLUME_1H_USD", "12000")))
MOMENTUM_MIN_CHANGE_1H_PCT = float(os.getenv("MOMENTUM_MIN_CHANGE_1H_PCT", "2"))
MOMENTUM_MIN_CHANGE_6H_PCT = float(os.getenv("MOMENTUM_MIN_CHANGE_6H_PCT", "0.5"))
MOMENTUM_MIN_CHANGE_24H_PCT = float(os.getenv("MOMENTUM_MIN_CHANGE_24H_PCT", "0"))
MOMENTUM_TRENDING_POOL_PAGES = max(1, min(5, int(os.getenv("MOMENTUM_TRENDING_POOL_PAGES", "5"))))
# Complement trending pools with older high-volume Solana markets.
# 5 trending + 2 top-pool queries once per 60s stays under the public
# GeckoTerminal approximate 10-requests/minute limit for this source.
MOMENTUM_TOP_POOL_PAGES = max(0, min(2, int(os.getenv("MOMENTUM_TOP_POOL_PAGES", "2"))))
# These DEX ids are used ONLY for indicative paper simulations, not PumpPortal live swaps.
MOMENTUM_PAPER_DEXES = frozenset(x.strip().lower() for x in os.getenv(
    "MOMENTUM_PAPER_DEXES", "pumpswap,raydium,orca,meteora").split(",") if x.strip())
SNIPER_MAX_ENTRY_IMPACT_PCT = float(os.getenv("SNIPER_MAX_ENTRY_IMPACT_PCT", "2"))
SNIPER_MAX_AGE_MINUTES = float(os.getenv("SNIPER_MAX_AGE_MINUTES", "45"))
SNIPER_MIN_VOLUME_5M_USD = float(os.getenv("SNIPER_MIN_VOLUME_5M_USD", "3000"))
SNIPER_MIN_BUY_SELL_RATIO = float(os.getenv("SNIPER_MIN_BUY_SELL_RATIO", "1.2"))
SNIPER_MIN_PRICE_CHANGE_5M_PCT = float(os.getenv("SNIPER_MIN_PRICE_CHANGE_5M_PCT", "3"))
SNIPER_MIN_TXNS_5M = int(os.getenv("SNIPER_MIN_TXNS_5M", "10"))
SNIPER_MAX_PRICE_CHANGE_5M_PCT = float(os.getenv("SNIPER_MAX_PRICE_CHANGE_5M_PCT", "60"))
MOMENTUM_MIN_VOLUME_5M_USD = float(os.getenv("MOMENTUM_MIN_VOLUME_5M_USD", "800"))
MOMENTUM_MIN_CHANGE_5M_PCT = float(os.getenv("MOMENTUM_MIN_CHANGE_5M_PCT", "0.2"))
MOMENTUM_MAX_CHANGE_5M_PCT = float(os.getenv("MOMENTUM_MAX_CHANGE_5M_PCT", "18"))
MOMENTUM_MIN_TXNS_5M = int(os.getenv("MOMENTUM_MIN_TXNS_5M", "8"))
MOMENTUM_MIN_BUY_SELL_RATIO = float(os.getenv("MOMENTUM_MIN_BUY_SELL_RATIO", "1.05"))
MOMENTUM_MIN_VOLUME_LIQUIDITY_RATIO = float(os.getenv("MOMENTUM_MIN_VOLUME_LIQUIDITY_RATIO", "0.005"))
MAX_HOLD_MINUTES = float(os.getenv("MAX_HOLD_MINUTES", "45" if MOMENTUM_SWING_MODE else "5"))
# v7.6 fast-moving established pools, no weak/slow filler buys. All PAPER.
PAPER_FAST_ONLY = PAPER_MODE and MOMENTUM_SWING_MODE and os.getenv("PAPER_FAST_ONLY", "true").lower() == "true"
PAPER_FAST_TP_NET_PCT = max(5.0, float(os.getenv("PAPER_FAST_TP_NET_PCT", "8")))
PAPER_PROFIT_TRAIL_ENABLED = PAPER_MODE and os.getenv("PAPER_PROFIT_TRAIL_ENABLED", "true").lower() == "true"
PAPER_PROFIT_TRAIL_ARM_NET_PCT = max(4.0, float(os.getenv("PAPER_PROFIT_TRAIL_ARM_NET_PCT", "5")))
PAPER_PROFIT_TRAIL_GIVEBACK_PCT = max(1.0, float(os.getenv("PAPER_PROFIT_TRAIL_GIVEBACK_PCT", "2.5")))
PAPER_LOSS_CIRCUIT_ENABLED = PAPER_MODE and os.getenv("PAPER_LOSS_CIRCUIT_ENABLED", "true").lower() == "true"
# Price-history confirmation uses already observed quotes; no extra paid API calls.
ENTRY_TREND_CONFIRM_ENABLED = os.getenv("ENTRY_TREND_CONFIRM_ENABLED", "true").lower() == "true"
ENTRY_TREND_MIN_OBSERVATION_SECONDS = max(5.0, float(os.getenv("ENTRY_TREND_MIN_OBSERVATION_SECONDS", "10")))
ENTRY_TREND_MAX_OBSERVATION_SECONDS = max(ENTRY_TREND_MIN_OBSERVATION_SECONDS, float(os.getenv("ENTRY_TREND_MAX_OBSERVATION_SECONDS", "120")))
ENTRY_TREND_MIN_CHANGE_PCT = float(os.getenv("ENTRY_TREND_MIN_CHANGE_PCT", "0.2"))
ENTRY_TREND_MAX_BUY_PRESSURE_DROP = max(0.0, float(os.getenv("ENTRY_TREND_MAX_BUY_PRESSURE_DROP", "0.03")))
PAPER_REVERSAL_EXIT_ENABLED = os.getenv("PAPER_REVERSAL_EXIT_ENABLED", "true").lower() == "true"
PAPER_REVERSAL_MIN_HOLD_SECONDS = max(0.0, float(os.getenv("PAPER_REVERSAL_MIN_HOLD_SECONDS", "20")))
PAPER_REVERSAL_LOOKBACK_SECONDS = max(10.0, float(os.getenv("PAPER_REVERSAL_LOOKBACK_SECONDS", "45")))
PAPER_REVERSAL_MIN_DROP_PCT = max(0.1, float(os.getenv("PAPER_REVERSAL_MIN_DROP_PCT", "4")))
PAPER_REVERSAL_CONFIRM_OBSERVATIONS = max(2, int(os.getenv("PAPER_REVERSAL_CONFIRM_OBSERVATIONS", "3")))
PAPER_REVERSAL_MAX_QUOTE_AGE_SECONDS = max(5.0, float(os.getenv("PAPER_REVERSAL_MAX_QUOTE_AGE_SECONDS", "20")))
# v5: conservative cost/quote freshness guard and confirmed deterioration exit.
PAPER_COST_ENTRY_ENABLED = PAPER_MODE and os.getenv("PAPER_COST_ENTRY_ENABLED", "true").lower() == "true"
PAPER_COST_ENTRY_BUFFER_PCT = max(0.0, float(os.getenv("PAPER_COST_ENTRY_BUFFER_PCT", "2")))
PAPER_COST_ENTRY_MIN_5M_PCT = max(0.0, float(os.getenv("PAPER_COST_ENTRY_MIN_5M_PCT", "0.5")))
PAPER_FINAL_SNAPSHOT_ENABLED = PAPER_MODE and os.getenv("PAPER_FINAL_SNAPSHOT_ENABLED", "true").lower() == "true"
PAPER_FINAL_SNAPSHOT_MAX_AGE_SECONDS = max(1.0, float(os.getenv("PAPER_FINAL_SNAPSHOT_MAX_AGE_SECONDS", "12")))
PAPER_FINAL_SNAPSHOT_MAX_DROP_PCT = max(0.0, float(os.getenv("PAPER_FINAL_SNAPSHOT_MAX_DROP_PCT", "0.15")))
PAPER_FINAL_SNAPSHOT_MAX_RISE_PCT = max(0.0, float(os.getenv("PAPER_FINAL_SNAPSHOT_MAX_RISE_PCT", "2")))
PAPER_EARLY_LOSS_REVERSAL_ENABLED = PAPER_MODE and os.getenv("PAPER_EARLY_LOSS_REVERSAL_ENABLED", "true").lower() == "true"
PAPER_EARLY_LOSS_MIN_HOLD_SECONDS = max(10.0, float(os.getenv("PAPER_EARLY_LOSS_MIN_HOLD_SECONDS", "30")))
PAPER_EARLY_LOSS_NET_PCT = max(2.0, float(os.getenv("PAPER_EARLY_LOSS_NET_PCT", "7")))
PAPER_EARLY_LOSS_MARKET_DROP_PCT = max(0.5, float(os.getenv("PAPER_EARLY_LOSS_MARKET_DROP_PCT", "2")))
# v6: separate persistent drawdown exit from v5 short-window reversal.
PAPER_SUSTAINED_LOSS_ENABLED = PAPER_MODE and os.getenv("PAPER_SUSTAINED_LOSS_ENABLED", "true").lower() == "true"
PAPER_SUSTAINED_LOSS_NET_PCT = max(6.0, float(os.getenv("PAPER_SUSTAINED_LOSS_NET_PCT", "8.5")))
PAPER_SUSTAINED_LOSS_MARKET_DROP_PCT = max(2.0, float(os.getenv("PAPER_SUSTAINED_LOSS_MARKET_DROP_PCT", "4.5")))
PAPER_SUSTAINED_LOSS_MIN_HOLD_SECONDS = max(60.0, float(os.getenv("PAPER_SUSTAINED_LOSS_MIN_HOLD_SECONDS", "120")))
PAPER_SUSTAINED_LOSS_CONFIRM_SECONDS = max(30.0, float(os.getenv("PAPER_SUSTAINED_LOSS_CONFIRM_SECONDS", "60")))
PAPER_SUSTAINED_LOSS_LOOKBACK_SECONDS = max(180.0, float(os.getenv("PAPER_SUSTAINED_LOSS_LOOKBACK_SECONDS", "300")))
PAPER_EXIT_SIGNAL_LOG_INTERVAL_SECONDS = max(10.0, float(os.getenv("PAPER_EXIT_SIGNAL_LOG_INTERVAL_SECONDS", "30")))
SHADOW_STRATEGY_C_TP = float(os.getenv("SHADOW_STRATEGY_C_TP", "0.10"))
SHADOW_STRATEGY_C_SL = float(os.getenv("SHADOW_STRATEGY_C_SL", "0.15"))
# Smart PAPER reentry is based on persisted closed trades, not seen_tokens.
PAPER_REENTRY_ENABLED = PAPER_MODE and os.getenv("PAPER_REENTRY_ENABLED", "true").lower() == "true"
PAPER_REENTRY_COOLDOWN_SECONDS = max(60, int(os.getenv("PAPER_REENTRY_COOLDOWN_SECONDS", "180")))
PAPER_REVERSAL_REENTRY_COOLDOWN_SECONDS = max(60, int(os.getenv("PAPER_REVERSAL_REENTRY_COOLDOWN_SECONDS", "300")))
PAPER_IMPAIRED_REENTRY_COOLDOWN_SECONDS = max(300, int(os.getenv("PAPER_IMPAIRED_REENTRY_COOLDOWN_SECONDS", "3600")))
PAPER_REENTRY_LOOKBACK_SECONDS = max(600, int(os.getenv("PAPER_REENTRY_LOOKBACK_SECONDS", "3600")))
# Older pools may requalify on fresh momentum *only after a real paper exit*.
PAPER_REENTRY_MAX_POOL_AGE_MINUTES = max(SNIPER_MAX_AGE_MINUTES, min(360, float(os.getenv("PAPER_REENTRY_MAX_POOL_AGE_MINUTES", "180"))))
PAPER_REENTRY_SCAN_QUOTA = max(0, min(8, int(os.getenv("PAPER_REENTRY_SCAN_QUOTA", "4"))))
PAPER_REENTRY_MAX_PER_WINDOW = max(0, min(10, int(os.getenv("PAPER_REENTRY_MAX_PER_WINDOW", "2"))))
PAPER_REENTRY_MAX_LOSS_USD = max(0.0, float(os.getenv("PAPER_REENTRY_MAX_LOSS_USD", "2.00")))
PAPER_LOSS_REENTRY_COOLDOWN_SECONDS = max(600, int(os.getenv("PAPER_LOSS_REENTRY_COOLDOWN_SECONDS", "1800")))
PAPER_SUSTAINED_LOSS_REENTRY_COOLDOWN_SECONDS = max(1800, int(os.getenv("PAPER_SUSTAINED_LOSS_REENTRY_COOLDOWN_SECONDS", "3600")))
PAPER_LOSS_REENTRY_NET_PCT = max(3.0, float(os.getenv("PAPER_LOSS_REENTRY_NET_PCT", "7")))

MAX_SLIPPAGE_BPS = int(os.getenv("MAX_SLIPPAGE_BPS", "150"))
MAX_STALE_PRICE_SECONDS = int(os.getenv("MAX_STALE_PRICE_SECONDS", "60"))
# Fee/slippage haircut used for paper-trade estimates. Actual costs vary.
ESTIMATED_ROUND_TRIP_COST = float(os.getenv("ESTIMATED_ROUND_TRIP_COST", "0.04"))
PAPER_MAX_EXIT_IMPACT = float(os.getenv("PAPER_MAX_EXIT_IMPACT", "0.10"))
PAPER_EXIT_LIQUIDITY_FRACTION = float(os.getenv("PAPER_EXIT_LIQUIDITY_FRACTION", "0.5"))
PAPER_EXIT_WARNING_INTERVAL_SECONDS = max(30, int(os.getenv("PAPER_EXIT_WARNING_INTERVAL_SECONDS", "60")))
PAPER_POOL_FALLBACK_SECONDS = max(10, int(os.getenv("PAPER_POOL_FALLBACK_SECONDS", "30")))
PAPER_IMPAIRED_AFTER_SECONDS = max(30, int(os.getenv("PAPER_IMPAIRED_AFTER_SECONDS", "60")))
PAPER_IMPAIRED_AFTER_CHECKS = max(2, int(os.getenv("PAPER_IMPAIRED_AFTER_CHECKS", "3")))
# PAPER ONLY: book a zero-recovery accounting loss after prolonged confirmed
# disappearance of usable pool liquidity. An API outage or a mere poor quote
# never establishes a rug pull. This is NOT a sell transaction.
PAPER_RUG_WRITEOFF_ENABLED = PAPER_MODE and os.getenv("PAPER_RUG_WRITEOFF_ENABLED", "true").lower() == "true"
PAPER_RUG_WRITEOFF_AFTER_SECONDS = max(60, int(os.getenv("PAPER_RUG_WRITEOFF_AFTER_SECONDS", "300")))
PAPER_RUG_WRITEOFF_MIN_CHECKS = max(2, int(os.getenv("PAPER_RUG_WRITEOFF_MIN_CHECKS", "3")))
PAPER_RUG_DUST_LIQUIDITY_USD = max(0.0, float(os.getenv("PAPER_RUG_DUST_LIQUIDITY_USD", "10")))
PAPER_PREBUY_SELL_CHECK_ENABLED = os.getenv("PAPER_PREBUY_SELL_CHECK_ENABLED", "true").lower() == "true"
# Independent, pinned-pool read-only cross-check for new entries (fails closed).
PAPER_PREBUY_INDEPENDENT_POOL_CHECK = os.getenv("PAPER_PREBUY_INDEPENDENT_POOL_CHECK", "true").lower() == "true"
PAPER_SECONDARY_LIQUIDITY_ENABLED = PAPER_MODE and os.getenv("PAPER_SECONDARY_LIQUIDITY_ENABLED", "true").lower() == "true"
PAPER_SECONDARY_LIQUIDITY_MAX_PER_MINUTE = max(0, min(12, int(os.getenv("PAPER_SECONDARY_LIQUIDITY_MAX_PER_MINUTE", "4"))))
PAPER_SECONDARY_LIQUIDITY_PER_SCAN = max(0, min(5, int(os.getenv("PAPER_SECONDARY_LIQUIDITY_PER_SCAN", "2"))))
PAPER_SECONDARY_LIQUIDITY_CACHE_SECONDS = max(15, int(os.getenv("PAPER_SECONDARY_LIQUIDITY_CACHE_SECONDS", "45")))
PAPER_SECONDARY_DEFERRED_QUOTA = max(0, min(5, int(os.getenv("PAPER_SECONDARY_DEFERRED_QUOTA", "2"))))
PAPER_SECONDARY_DEFERRED_TTL_SECONDS = max(60, min(900, int(os.getenv("PAPER_SECONDARY_DEFERRED_TTL_SECONDS", "360"))))
PAPER_PREBUY_MIN_CROSSCHECK_LIQUIDITY_RATIO = max(0.1, min(1.0, float(os.getenv("PAPER_PREBUY_MIN_CROSSCHECK_LIQUIDITY_RATIO", "0.70"))))
PAPER_PREBUY_MAX_CROSSCHECK_PRICE_DEVIATION_PCT = max(0.0, float(os.getenv("PAPER_PREBUY_MAX_CROSSCHECK_PRICE_DEVIATION_PCT", "12")))
PAPER_EXIT_REQUIRE_PRIMARY_QUOTE = os.getenv("PAPER_EXIT_REQUIRE_PRIMARY_QUOTE", "true").lower() == "true"
PAPER_FALLBACK_LOG_INTERVAL_SECONDS = max(15, int(os.getenv("PAPER_FALLBACK_LOG_INTERVAL_SECONDS", "60")))
SNIPER_MIN_ENTRY_LIQUIDITY_MULTIPLIER = max(1.0, float(os.getenv("SNIPER_MIN_ENTRY_LIQUIDITY_MULTIPLIER", "1.5")))

PUMP_CURVE_LOOKUP_ENABLED = not MOMENTUM_SWING_MODE and os.getenv("PUMP_CURVE_LOOKUP_ENABLED", "true").lower() == "true"
PUMP_CURVE_LOOKUPS_PER_SCAN = max(0, min(10, int(os.getenv("PUMP_CURVE_LOOKUPS_PER_SCAN", "4"))))
FRESH_LAUNCH_FEED_ENABLED = not MOMENTUM_SWING_MODE and os.getenv("FRESH_LAUNCH_FEED_ENABLED", "true").lower() == "true"
# Optional account key: never logged; the new-token stream is best effort.
PUMPPORTAL_API_KEY = os.getenv("PUMPPORTAL_API_KEY", "").strip()
FRESH_LAUNCH_TTL_SECONDS = max(300, int(os.getenv("FRESH_LAUNCH_TTL_SECONDS", "3600")))
FRESH_LAUNCH_POOL_LIMIT = max(100, int(os.getenv("FRESH_LAUNCH_POOL_LIMIT", "1500")))
FRESH_LAUNCH_SCAN_QUOTA = max(0, int(os.getenv("FRESH_LAUNCH_SCAN_QUOTA", "24")))
VERIFIED_DEX_RECHECK_QUOTA = max(0, int(os.getenv("VERIFIED_DEX_RECHECK_QUOTA", "18")))
VERIFIED_DEX_TTL_SECONDS = max(60, int(os.getenv("VERIFIED_DEX_TTL_SECONDS", "900")))
STRUCTURAL_REJECT_COOLDOWN_SECONDS = max(60, int(os.getenv("STRUCTURAL_REJECT_COOLDOWN_SECONDS", "300")))
STALE_POOL_RECHECK_SECONDS = max(300, int(os.getenv("STALE_POOL_RECHECK_SECONDS", "1800")))
MISSING_DATA_COOLDOWN_SECONDS = max(30, int(os.getenv("MISSING_DATA_COOLDOWN_SECONDS", "120")))
# Momentum can change in one minute; do not park all established markets for
# the structural (default five-minute) cooldown after ordinary weak momentum.
MOMENTUM_REJECT_COOLDOWN_SECONDS = max(20, int(os.getenv("MOMENTUM_REJECT_COOLDOWN_SECONDS", "60")))
# When *every* discovered mint is cooling, run a handful of bounded, rotating
# discovery rechecks. This is ONLY for fresh market evaluation, not permission
# to trade or to bypass any of the paper pre-buy safety checks.
MOMENTUM_COOLDOWN_PROBE_QUOTA = max(0, min(8, int(os.getenv("MOMENTUM_COOLDOWN_PROBE_QUOTA", "4"))))
MOMENTUM_COOLDOWN_PROBE_GAP_SECONDS = max(30, int(os.getenv("MOMENTUM_COOLDOWN_PROBE_GAP_SECONDS", "90")))
QUALITY_POOL_DISCOVERY_ENABLED = os.getenv("QUALITY_POOL_DISCOVERY_ENABLED", "true").lower() == "true"
QUALITY_POOL_CACHE_SECONDS = max(60, int(os.getenv("QUALITY_POOL_CACHE_SECONDS", "60")))
QUALITY_POOL_SCAN_QUOTA = max(0, int(os.getenv("QUALITY_POOL_SCAN_QUOTA", "18")))
RECENT_PROFILE_DISCOVERY_ENABLED = PAPER_MODE and MOMENTUM_SWING_MODE and os.getenv("RECENT_PROFILE_DISCOVERY_ENABLED", "true").lower() == "true"
INDEPENDENT_MOVERS_ENABLED = PAPER_MODE and MOMENTUM_SWING_MODE and os.getenv("INDEPENDENT_MOVERS_ENABLED", "true").lower() == "true"
DEXPAPRIKA_API_KEY = os.getenv("DEXPAPRIKA_API_KEY", "").strip()
EXPANDED_MOMENTUM_DISCOVERY_ENABLED = PAPER_MODE and MOMENTUM_SWING_MODE and os.getenv("EXPANDED_MOMENTUM_DISCOVERY_ENABLED", "true").lower() == "true"
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "").strip()
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "").strip()
# Optional unofficial Pump.fun board sources. Missing auth must not break discovery.
PUMPFUN_BOARDS_ENABLED = os.getenv("PUMPFUN_BOARDS_ENABLED", "false").lower() == "true"
PUMPFUN_BOARDS_CACHE_SECONDS = max(60, int(os.getenv("PUMPFUN_BOARDS_CACHE_SECONDS", "60")))
PUMPFUN_BOARDS_LIMIT = max(1, min(50, int(os.getenv("PUMPFUN_BOARDS_LIMIT", "50"))))
PUMPFUN_MOVERS_ENABLED = os.getenv("PUMPFUN_MOVERS_ENABLED", "true").lower() == "true"
PUMPFUN_TRENDING_ENABLED = os.getenv("PUMPFUN_TRENDING_ENABLED", "true").lower() == "true"
PUMPFUN_BOARD_JWT = os.getenv("PUMPFUN_BOARD_JWT", "").strip()
PUMPFUN_BOARD_SESSION_ID = os.getenv("PUMPFUN_BOARD_SESSION_ID", "").strip()
PUMPFUN_BOARD_USER_ID = os.getenv("PUMPFUN_BOARD_USER_ID", "").strip()
PUMPFUN_BOARD_TIER = os.getenv("PUMPFUN_BOARD_TIER", "1").strip()
PUMPFUN_TRENDING_ENDPOINT = os.getenv("PUMPFUN_TRENDING_ENDPOINT", TRENDING_PROXY_URL).strip()

# FOMO Family unofficial token boards. A key is required for any request.
# Both boards cost 250 credits per call. 2 feeds / 2h ~= 180k credits/month;
# the documented free plan provides 250k credits/month.
FOMO_BOARDS_ENABLED = os.getenv("FOMO_BOARDS_ENABLED", "true").lower() == "true"
FOMO_API_KEY = os.getenv("FOMO_API_KEY", "").strip()
FOMO_GRADUATED_ENABLED = os.getenv("FOMO_GRADUATED_ENABLED", "true").lower() == "true"
FOMO_TRENDING_ENABLED = os.getenv("FOMO_TRENDING_ENABLED", "true").lower() == "true"
FOMO_BOARDS_CACHE_SECONDS = max(300, int(os.getenv("FOMO_BOARDS_CACHE_SECONDS", "7200")))
FOMO_BOARDS_LIMIT = max(1, min(150, int(os.getenv("FOMO_BOARDS_LIMIT", "50"))))
SNIPER_NEW_POOL_PAGES = max(1, min(5, int(os.getenv("SNIPER_NEW_POOL_PAGES", "5"))))
SNIPER_TRENDING_POOL_PAGES = max(0, min(3, int(os.getenv("SNIPER_TRENDING_POOL_PAGES", "0"))))
SNIPER_WATCHLIST_LIMIT = max(20, int(os.getenv("SNIPER_WATCHLIST_LIMIT", "250")))
SNIPER_WATCHLIST_RECHECK_QUOTA = max(0, int(os.getenv("SNIPER_WATCHLIST_RECHECK_QUOTA", "36")))
SNIPER_WATCHLIST_TTL_SECONDS = max(60, int(os.getenv("SNIPER_WATCHLIST_TTL_SECONDS", "1800")))
SNIPER_VERIFIED_RECHECK_QUOTA = max(0, int(os.getenv("SNIPER_VERIFIED_RECHECK_QUOTA", "12")))
SNIPER_LAUNCH_PROBE_QUOTA = max(0, int(os.getenv("SNIPER_LAUNCH_PROBE_QUOTA", "6")))
ROLLING_DISCOVERY_ENABLED = os.getenv("ROLLING_DISCOVERY_ENABLED", "false").lower() == "true"
FRESH_BATCH_DISCOVERY_ENABLED = PAPER_MODE and MOMENTUM_SWING_MODE and os.getenv("FRESH_BATCH_DISCOVERY_ENABLED", "false").lower() == "true"
FRESH_BATCH_ACTIVE_LIMIT = max(12, min(120, int(os.getenv("FRESH_BATCH_ACTIVE_LIMIT", "40"))))
CANDIDATE_QUALITY_ENABLED = os.getenv("CANDIDATE_QUALITY_ENABLED", "false").lower() == "true"
ROLLING_DISCOVERY_WATCHLIST_SIZE = max(100, min(2000, int(os.getenv("ROLLING_DISCOVERY_WATCHLIST_SIZE", "750"))))
ROLLING_DISCOVERY_WATCHLIST_TTL_SECONDS = max(3600, int(os.getenv("ROLLING_DISCOVERY_WATCHLIST_TTL_SECONDS", "21600")))
ROLLING_DISCOVERY_FRESH_SCAN_QUOTA = max(0, min(40, int(os.getenv("ROLLING_DISCOVERY_FRESH_SCAN_QUOTA", "24"))))
QUALITY_POOL_MIN_VOLUME_1H_USD = max(0.0, float(os.getenv("QUALITY_POOL_MIN_VOLUME_1H_USD", "10000")))
CURVE_MIN_REAL_SOL_FOR_OBSERVATION = max(0.0, float(os.getenv("CURVE_MIN_REAL_SOL_FOR_OBSERVATION", "1.0")))
FRESH_RECHECK_BASE_SECONDS = max(60, int(os.getenv("FRESH_RECHECK_BASE_SECONDS", "60")))
FRESH_RECHECK_MAX_SECONDS = max(FRESH_RECHECK_BASE_SECONDS, int(os.getenv("FRESH_RECHECK_MAX_SECONDS", "300")))
DISCOVERY_SEARCH_TERMS = tuple(x.strip() for x in os.getenv("DISCOVERY_SEARCH_TERMS", ",".join(DEFAULT_SEARCH_TERMS) if MOMENTUM_SWING_MODE else "pump").split(",") if x.strip())
DISCOVERY_SEARCHES_PER_SCAN = max(0, min(5, int(os.getenv("DISCOVERY_SEARCHES_PER_SCAN", "3"))))
DISCOVERY_CACHE_SECONDS = max(60, int(os.getenv("DISCOVERY_CACHE_SECONDS", "60")))
ENTRY_SCAN_INTERVAL_SECONDS = max(10, int(os.getenv("ENTRY_SCAN_INTERVAL_SECONDS", "15")))
ENTRY_BATCH_SIZE = max(1, min(30, int(os.getenv("ENTRY_BATCH_SIZE", "30"))))
MAX_ENTRY_TOKENS_PER_SCAN = max(1, int(os.getenv("MAX_ENTRY_TOKENS_PER_SCAN", "60")))
MARKET_REQUEST_SPACING_SECONDS = max(0.0, float(os.getenv("MARKET_REQUEST_SPACING_SECONDS", "1.5")))
RATE_LIMIT_COOLDOWN_SECONDS = max(30, int(os.getenv("RATE_LIMIT_COOLDOWN_SECONDS", "120")))
BASE = "https://api.dexscreener.com"
SOL_MINT = "So11111111111111111111111111111111111111112"
STATE_FILE = Path(os.getenv("STATE_FILE", "bot_state_36.json"))
PUBLIC_WALLET_ADDRESS = os.getenv("SOLANA_WALLET_ADDRESS", "").strip()
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "").strip()
LOG_FILE = Path("bot.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")]
)
log = logging.getLogger("meme-bot")
session = requests.Session()
session.headers.update({"User-Agent": "SolanaMemeRiskBot/0.1"})


@dataclass
class Position:
    token: str
    symbol: str
    name: str
    entry_price: float
    amount_usd: float
    token_units: float
    opened_at: float
    peak_price: float
    last_price: float
    score: float
    last_price_at: float = 0.0
    entry_pool_address: str = ""
    entry_pool_dex: str = ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_api_cooldown_until = 0.0
_last_market_request_at = 0.0


class DexRateLimited(RuntimeError):
    pass


def api_cooldown_remaining() -> float:
    return max(0.0, _api_cooldown_until - time.monotonic())


def dex_request(url: str, *, params: dict | None = None, timeout: int = 12) -> Any:
    """Shared DexScreener budget; a 429 pauses ALL DexScreener calls."""
    global _api_cooldown_until, _last_market_request_at
    if api_cooldown_remaining() > 0:
        raise DexRateLimited(f"cooldown {api_cooldown_remaining():.0f}s")
    wait = MARKET_REQUEST_SPACING_SECONDS - (time.monotonic() - _last_market_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_market_request_at = time.monotonic()
    response = session.get(url, params=params, timeout=timeout)
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "")
        try:
            delay = float(retry_after)
        except (ValueError, TypeError):
            delay = RATE_LIMIT_COOLDOWN_SECONDS
        delay = max(RATE_LIMIT_COOLDOWN_SECONDS, min(delay, 900))
        _api_cooldown_until = time.monotonic() + delay
        log.warning("DexScreener HTTP 429: pausing API requests for %.0fs", delay)
        raise DexRateLimited(f"HTTP 429; cooldown {delay:.0f}s")
    response.raise_for_status()
    return response.json()


def get_json(url: str, timeout: int = 12) -> Any:
    return dex_request(url, timeout=timeout)


def log_real_wallet_balance() -> None:
    """Read-only Solana balance; never loads a secret or signs a transaction."""
    if not PUBLIC_WALLET_ADDRESS or not SOLANA_RPC_URL:
        log.warning("REAL WALLET: unavailable; set SOLANA_WALLET_ADDRESS (public) and SOLANA_RPC_URL in Railway Variables.")
        return
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", PUBLIC_WALLET_ADDRESS):
        log.error("REAL WALLET: invalid public address format; no RPC request made.")
        return
    if not SOLANA_RPC_URL.startswith("https://"):
        log.error("REAL WALLET: SOLANA_RPC_URL must use HTTPS.")
        return
    try:
        response = session.post(SOLANA_RPC_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance",
            "params": [PUBLIC_WALLET_ADDRESS, {"commitment": "confirmed"}],
        }, timeout=12)
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise ValueError("RPC returned an error")
        lamports = payload["result"]["value"]
        if not isinstance(lamports, int) or lamports < 0:
            raise ValueError("RPC returned invalid balance")
        sol = lamports / 1_000_000_000
        try:
            price_response = session.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "solana", "vs_currencies": "usd"}, timeout=10)
            price_response.raise_for_status()
            usd_price = float(price_response.json()["solana"]["usd"])
            if usd_price <= 0:
                raise ValueError("Invalid SOL/USD price")
            log.info("REAL WALLET (read-only): %.6f SOL (~$%.2f USD); PAPER portfolio is separate.", sol, sol * usd_price)
        except (requests.RequestException, ValueError, KeyError, TypeError):
            log.warning("REAL WALLET (read-only): %.6f SOL; USD estimate unavailable (price feed error).", sol)
    except (requests.RequestException, ValueError, KeyError, TypeError):
        log.exception("REAL WALLET: unable to fetch balance; paper trading continues independently.")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            log.exception("State file is unreadable; refusing to overwrite it.")
            raise
    return {
        "cash_usd": STARTING_CAPITAL,
        "positions": {},
        "closed_trades": [],
        "day": str(date.today()),
        "day_start_equity": STARTING_CAPITAL,
        "halted": False,
        "seen_tokens": [],
        "exit_issues": {},
        "rugged_mints": [],
        "paper_trade_metrics": {},
    }


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


_discovery_cache: dict[str, Any] = {"until": 0.0, "addresses": []}
_discovery_round = 0
_discovery_seen: set[str] = set()
_discovery_priority: dict[str, float] = {}
_quality_pool_mints: list[str] = []
_rolling_watch = RollingWatchlist(STATE_FILE.with_name("rolling_discovery_state.json"),
                                 limit=ROLLING_DISCOVERY_WATCHLIST_SIZE,
                                 ttl_seconds=ROLLING_DISCOVERY_WATCHLIST_TTL_SECONDS)
_quality_ledger = CandidateQualityLedger(STATE_FILE.with_name("candidate_quality_state.json"))
_fresh_batch = FreshBatchQueue(STATE_FILE.with_name("fresh_batch_state.json"))
_candidate_pipeline = CandidatePipeline(STATE_FILE.with_name("candidate_pipeline_v74_state.json"))
_independent_movers = IndependentMovers(STATE_FILE.with_name("independent_movers_v73_state.json"),
    enabled=INDEPENDENT_MOVERS_ENABLED, api_key=DEXPAPRIKA_API_KEY)
_expanded_movers = ExpandedMomentumDiscovery(STATE_FILE.with_name("expanded_momentum_v77_state.json"),
    enabled=EXPANDED_MOMENTUM_DISCOVERY_ENABLED, jupiter_key=JUPITER_API_KEY,
    birdeye_key=BIRDEYE_API_KEY)
_quality_source_round = 0
_quality_source_backoff: dict[str, float] = {}
_quality_global_backoff_until = 0.0
_quality_cache: dict[str, Any] = {"until": 0.0, "mints": [], "scores": {}}
_board_cache: dict[str, Any] = {"until": 0.0, "movers": [], "trending": [], "warn_at": 0.0}
_fomo_cache: dict[str, Any] = {"until": 0.0, "graduated": [], "trending": [], "warn_at": 0.0, "disabled": set()}

# Stage 1: supported, priced and liquid newly-created pools (NO momentum gate).
# Stage 2: scheduled fresh DexScreener rechecks; analyze_pair applies ALL entry gates.
_sniper_watchlist: OrderedDict[str, tuple[float, float]] = OrderedDict()
_sniper_watch_cursor = 0

# PumpPortal's public subscribeNewToken feed is discovery-only. Every mint must
# still pass DexScreener's SOL-pool, liquidity, age and momentum checks.
_fresh_launches: OrderedDict[str, float] = OrderedDict()
# Mint -> (next check monotonic timestamp, attempts, last outcome).
# Entries are bounded by the feed TTL and pool size.
_fresh_checks: dict[str, tuple[float, int, str]] = {}
_fresh_lock = threading.Lock()
_feed_started = False
_feed_connected = False
_feed_status = "not_started"
_feed_messages = 0
_feed_reconnects = 0
_curve_activity = CurveActivity()
_curve_subscriptions: OrderedDict[str, float] = OrderedDict()
_curve_verified: OrderedDict[str, float] = OrderedDict()
_curve_sub_lock = threading.Lock()
_curve_rpc_tracker: RpcCurveTradeTracker | None = None
_curve_poll_worker_running = False
_curve_poll_cursor = 0
# Last validated on-chain curve reserves; diagnostics and prioritization only.
_curve_real_sol: dict[str, tuple[float, float]] = {}
CURVE_ACTIVITY_ENABLED = not MOMENTUM_SWING_MODE and os.getenv("CURVE_ACTIVITY_ENABLED", "true").lower() == "true"
CURVE_ACTIVITY_MAX_SUBSCRIPTIONS = max(1, min(100, int(os.getenv("CURVE_ACTIVITY_MAX_SUBSCRIPTIONS", "40"))))
CURVE_ACTIVITY_MIN_TRADES = max(1, int(os.getenv("CURVE_ACTIVITY_MIN_TRADES", "20")))
CURVE_ACTIVITY_MIN_VOLUME_SOL = max(0., float(os.getenv("CURVE_ACTIVITY_MIN_VOLUME_SOL", "2")))
RPC_TRADE_POLL_INTERVAL_SECONDS = max(15, int(os.getenv("RPC_TRADE_POLL_INTERVAL_SECONDS", "30")))
RPC_TRADE_MINTS_PER_POLL = max(1, min(12, int(os.getenv("RPC_TRADE_MINTS_PER_POLL", "4"))))
RPC_TRADE_SIGNATURE_LIMIT = max(2, min(50, int(os.getenv("RPC_TRADE_SIGNATURE_LIMIT", "12"))))
RPC_TRADE_TRANSACTIONS_PER_POLL = max(1, min(20, int(os.getenv("RPC_TRADE_TRANSACTIONS_PER_POLL", "8"))))
RPC_TRADE_MAX_PENDING = max(32, min(500, int(os.getenv("RPC_TRADE_MAX_PENDING", "160"))))


def _curve_poll_mints() -> list[str]:
    """Prioritize confirmed active curves, while rotating across fresh launches.

    The feed can arrive faster than an affordable RPC polling rate, so the
    observed 5-minute volume is a *lower bound*, not a complete trade tape.
    """
    global _curve_poll_cursor
    with _curve_sub_lock:
        threshold = time.monotonic() - 900
        for mint, when in list(_curve_verified.items()):
            if when < threshold:
                del _curve_verified[mint]
                _curve_real_sol.pop(mint, None)
        # Prefer recently observed, reserve-backed curves, and rotate the rest.
        # This only prioritizes read-only trade observations; it never enables entries.
        verified = sorted(_curve_verified, key=lambda mint: (
            _curve_real_sol.get(mint, (0.0, 0.0))[0] >= CURVE_MIN_REAL_SOL_FOR_OBSERVATION,
            _curve_activity.summary(mint)['trades'],
            _curve_verified[mint]), reverse=True)[:max(1, RPC_TRADE_MINTS_PER_POLL // 2)]
        others = [mint for mint in _curve_subscriptions if mint not in verified]
        slots = max(0, RPC_TRADE_MINTS_PER_POLL - len(verified))
        rotating = []
        if others and slots:
            start = _curve_poll_cursor % len(others)
            rotating = [others[(start + i) % len(others)] for i in range(min(slots, len(others)))]
            _curve_poll_cursor += len(rotating)
        return verified + rotating


def _curve_rpc_worker() -> None:
    global _curve_rpc_tracker, _curve_poll_worker_running
    if not SOLANA_RPC_URL:
        log.warning("CURVE RPC activity unavailable: SOLANA_RPC_URL not configured")
        return
    try:
        tracker = RpcCurveTradeTracker(ReadOnlySolanaRPC(SOLANA_RPC_URL), _curve_activity,
                                       signature_limit=RPC_TRADE_SIGNATURE_LIMIT,
                                       transaction_limit=RPC_TRADE_TRANSACTIONS_PER_POLL,
                                       max_pending=RPC_TRADE_MAX_PENDING)
    except ValueError as exc:
        log.warning("CURVE RPC activity unavailable: %s", type(exc).__name__)
        return
    _curve_rpc_tracker = tracker
    _curve_poll_worker_running = True
    log.info("CURVE RPC ACTIVITY started (read-only Solana RPC; no paid trade subscription) poll=%ds mints=%d tx_limit=%d",
             RPC_TRADE_POLL_INTERVAL_SECONDS, RPC_TRADE_MINTS_PER_POLL,
             RPC_TRADE_TRANSACTIONS_PER_POLL)
    try:
        while True:
            started = time.monotonic()
            mints = _curve_poll_mints()
            try:
                tracker.poll(mints)
            except Exception as exc:
                log.warning("CURVE RPC ACTIVITY worker error=%s", type(exc).__name__)
            diag = tracker.diagnostics()
            if mints and (diag['poll_cycles'] <= 3 or diag['poll_cycles'] % 5 == 0):
                log.info("CURVE RPC ACTIVITY watched=%d polled=%d signature_scans=%d tx_fetched=%d valid_trades=%d rpc_errors=%d history_gaps=%d pending=%d",
                         len(_curve_subscriptions), len(mints), diag['signature_scans'],
                         diag['tx_fetched'], diag['valid_trades'], diag['rpc_errors'],
                         diag['history_gaps'], diag['pending_signatures'])
            time.sleep(max(1, RPC_TRADE_POLL_INTERVAL_SECONDS - (time.monotonic() - started)))
    finally:
        _curve_poll_worker_running = False


def start_curve_activity() -> None:
    if CURVE_ACTIVITY_ENABLED:
        threading.Thread(target=_curve_rpc_worker, daemon=True, name="curve-rpc-activity").start()


def update_curve_subscriptions(mints: list[str]) -> None:
    """Keep a bounded launch watchlist for read-only RPC polling.

    This no longer sends any paid PumpPortal subscribeTokenTrade requests.
    """
    if not CURVE_ACTIVITY_ENABLED:
        return
    now = time.monotonic()
    with _curve_sub_lock:
        for mint in mints:
            if not isinstance(mint, str) or not _MINT_RE.fullmatch(mint):
                continue
            if mint not in _curve_subscriptions:
                _curve_subscriptions[mint] = now
        cutoff = now - min(FRESH_LAUNCH_TTL_SECONDS, 1800)
        for mint, timestamp in list(_curve_subscriptions.items()):
            if timestamp < cutoff:
                del _curve_subscriptions[mint]
        while len(_curve_subscriptions) > CURVE_ACTIVITY_MAX_SUBSCRIPTIONS:
            _curve_subscriptions.popitem(last=False)


def note_active_curve(mint: str, real_sol: float | None = None) -> None:
    """Prioritize mints with verified curve accounts for RPC activity polls."""
    if not CURVE_ACTIVITY_ENABLED:
        return
    with _curve_sub_lock:
        timestamp = time.monotonic()
        _curve_verified[mint] = timestamp
        if real_sol is not None:
            _curve_real_sol[mint] = (max(0.0, real_sol), timestamp)
        _curve_verified.move_to_end(mint)
        while len(_curve_verified) > 20:
            old, _ = _curve_verified.popitem(last=False)
            _curve_real_sol.pop(old, None)


def curve_activity_diagnostics() -> dict[str, Any]:
    with _curve_sub_lock:
        watched = len(_curve_subscriptions)
        verified = len(_curve_verified)
    tracker = _curve_rpc_tracker
    details = tracker.diagnostics() if tracker else {"source": "solana_rpc", "valid_trades": 0,
                                                     "signature_scans": 0, "tx_fetched": 0}
    return {"watched": watched, "verified_curves": verified, "worker_running": _curve_poll_worker_running,
            **details}

_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def record_fresh_launch(mint: str, *, seen_at: float | None = None) -> bool:
    if not isinstance(mint, str) or not _MINT_RE.fullmatch(mint) or mint == SOL_MINT:
        return False
    timestamp = time.monotonic() if seen_at is None else seen_at
    with _fresh_lock:
        _fresh_launches.pop(mint, None)
        _fresh_launches[mint] = timestamp
        _fresh_checks.setdefault(mint, (timestamp, 0, "new"))
        while len(_fresh_launches) > FRESH_LAUNCH_POOL_LIMIT:
            old_mint, _ = _fresh_launches.popitem(last=False)
            _fresh_checks.pop(old_mint, None)
    update_curve_subscriptions([mint])
    return True


def fresh_launch_snapshot() -> list[str]:
    cutoff = time.monotonic() - FRESH_LAUNCH_TTL_SECONDS
    with _fresh_lock:
        for mint, timestamp in list(_fresh_launches.items()):
            if timestamp < cutoff:
                _fresh_launches.pop(mint, None)
                _fresh_checks.pop(mint, None)
        return list(reversed(_fresh_launches))



def due_fresh_launches(*, now: float | None = None, limit: int | None = None) -> list[str]:
    """Oldest-due first: avoid permanently starving launches behind newer ones."""
    current = time.monotonic() if now is None else now
    with _fresh_lock:
        due = [(checks[0], _fresh_launches[mint], mint)
               for mint, checks in _fresh_checks.items()
               if mint in _fresh_launches and checks[0] <= current]
    due.sort()
    return [mint for _, _, mint in due[:limit]]


def mark_fresh_checked(mint: str, outcome: str, *, now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    with _fresh_lock:
        if mint not in _fresh_launches:
            return
        _, attempts, _ = _fresh_checks.get(mint, (current, 0, "new"))
        attempts += 1
        # Missing market data is rechecked regularly as the token matures.
        # Longer intervals prevent hammering DexScreener for unindexed launches.
        delay = min(FRESH_RECHECK_MAX_SECONDS, FRESH_RECHECK_BASE_SECONDS * (2 ** min(attempts - 1, 8)))
        _fresh_checks[mint] = (current + delay, attempts, outcome)


def fresh_check_counts() -> dict[str, int]:
    with _fresh_lock:
        counts = Counter(outcome for _, _, outcome in _fresh_checks.values())
    return dict(counts)


def _fresh_launch_worker() -> None:
    global _feed_connected, _feed_status, _feed_messages, _feed_reconnects
    if not PUMPPORTAL_API_KEY:
        _feed_status = "missing_api_key"
        log.warning("FRESH FEED disabled reason=missing_api_key; PumpPortal requires PUMPPORTAL_API_KEY for data WebSocket; GeckoTerminal polling still works")
        return
    try:
        import websocket
    except ImportError:
        _feed_status = "websocket_client_missing"
        log.warning("Fresh-launch feed unavailable: install websocket-client")
        return
    backoff = 3
    while True:
        ws = None
        try:
            feed_url = "wss://pumpportal.fun/api/data"
            if PUMPPORTAL_API_KEY:
                feed_url += "?" + urlencode({"api-key": PUMPPORTAL_API_KEY})
            ws = websocket.create_connection(feed_url, timeout=20)
            ws.settimeout(30)
            ws.send(json.dumps({"method": "subscribeNewToken"}))
            _feed_connected = True
            _feed_status = "connected"
            log.info("FRESH FEED connected (PumpPortal new-token stream)")
            backoff = 3
            while True:
                message = json.loads(ws.recv())
                if not isinstance(message, dict):
                    continue
                mint = message.get("mint")
                if record_fresh_launch(mint):
                    _feed_messages += 1
        except Exception as exc:
            _feed_reconnects += 1
            # WebSocketBadStatusException can embed full HTML and headers.
            # Report only the error class/status, never the response body or key.
            status = getattr(exc, "status_code", None)
            if status is None:
                match = re.search(r"Handshake status (\d{3})", str(exc))
                status = int(match.group(1)) if match else None
            _feed_status = f"HTTP{status}" if status else type(exc).__name__
            if status in (502, 503, 504):
                backoff = max(60, backoff)  # upstream fault, don't thrash
            log.warning("FRESH FEED unavailable reason=%s%s retry_in=%ds; "
                        "GeckoTerminal new-pool polling remains active (discovery only)",
                        type(exc).__name__, f":HTTP{status}" if status else "", backoff)
        finally:
            _feed_connected = False
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
        time.sleep(backoff)
        backoff = min(60, backoff * 2)


def start_fresh_launch_feed() -> None:
    global _feed_started
    if not FRESH_LAUNCH_FEED_ENABLED or _feed_started:
        return
    _feed_started = True
    threading.Thread(target=_fresh_launch_worker, daemon=True, name="fresh-launch-feed").start()



_gecko_near_hints: dict[str, dict] = {}
_profile_near_hints: dict[str, dict] = {}


def quality_pool_candidates() -> tuple[list[str], dict[str, float]]:
    """Public GeckoTerminal hints, independently reverified on DexScreener.

    A 3-request/90s budget complements rotating DexScreener searches. 429
    throttles the whole GeckoTerminal source, never held-position monitoring.
    """
    now = time.monotonic()
    if not QUALITY_POOL_DISCOVERY_ENABLED:
        return [], {}
    if now < _quality_cache["until"]:
        return list(_quality_cache["mints"]), dict(_quality_cache["scores"])
    scores: dict[str, float] = {}
    _gecko_near_hints.clear()
    accepted = 0
    errors = 0
    pool_rejects = Counter()
    global _quality_source_round, _quality_global_backoff_until
    # A fixed first-page sweep repeatedly supplies the same 90-120 mints.
    # Rotating discovery instead walks different DEX-specific rankings,
    # momentum windows, and deeper pages with a five-request budget per 90s.
    if ROLLING_DISCOVERY_ENABLED and MOMENTUM_SWING_MODE:
        _rolling_watch.load()
        if _quality_source_round == 0:
            _quality_source_round = _rolling_watch.source_round
        endpoints = rotating_pool_sources(_quality_source_round, novelty_drought=_rolling_watch.novelty_drought)
        _quality_source_round += 1
        _rolling_watch.source_round = _quality_source_round
    else:
        endpoints = ([ ("new_pools", {"page": p}) for p in range(1, SNIPER_NEW_POOL_PAGES + 1)] +
                     [("trending_pools", {"page": p}) for p in range(1, SNIPER_TRENDING_POOL_PAGES + 1)]) if SNIPER_MODE else (
                     [("trending_pools", {"page": p}) for p in range(1, MOMENTUM_TRENDING_POOL_PAGES + 1)] +
                     [("pools", {"page": p, "order": "h24_volume_usd_desc"}) for p in range(1, MOMENTUM_TOP_POOL_PAGES + 1)]
                     if MOMENTUM_SWING_MODE else [("trending_pools", {"page": 1}), ("new_pools", {"page": 1})])
    source_report = []
    attempted = 0
    skipped_backoff = 0
    global_pause = max(0, int(_quality_global_backoff_until - now))
    for endpoint, params in endpoints:
        # Source-level 429 means no more calls to ANY GeckoTerminal endpoint.
        # A missing deep page (404) only disables that exact URL/page.
        backoff_key = (endpoint, tuple(sorted(params.items())))
        if now < _quality_global_backoff_until or now < _quality_source_backoff.get(backoff_key, 0.0):
            skipped_backoff += 1
            continue
        page = params.get("page", 1)
        attempted += 1
        try:
            response = session.get(
                f"https://api.geckoterminal.com/api/v2/networks/solana/{endpoint}",
                params={"include": "base_token,quote_token", **params},
                headers={"Accept": "application/json;version=20230203"}, timeout=12)
            response.raise_for_status()
            payload = response.json()
            if MOMENTUM_SWING_MODE:
                _gecko_near_hints.update(early_gecko_pools(payload,
                    sol_mint=SOL_MINT, supported_dexes=active_entry_routes(),
                    min_age=MOMENTUM_MIN_PAIR_AGE_MINUTES,
                    min_cap=MOMENTUM_MARKET_CAP_MIN_USD,
                    max_cap=MOMENTUM_MARKET_CAP_MAX_USD))
            candidates = extract_quality_pool_mints(payload,
                sol_mint=SOL_MINT,
                supported_dexes=frozenset(d.replace("_", "").replace("-", "") for d in active_entry_routes()),
                min_liquidity_usd=active_entry_liquidity_min(),
                min_volume_1h_usd=(0.0 if SNIPER_MODE else QUALITY_POOL_MIN_VOLUME_1H_USD),
                sniper_max_age_minutes=(SNIPER_MAX_AGE_MINUTES if SNIPER_MODE else None),
                sniper_min_volume_5m_usd=(SNIPER_MIN_VOLUME_5M_USD if SNIPER_MODE else None),
                sniper_min_change_5m_pct=(SNIPER_MIN_PRICE_CHANGE_5M_PCT if SNIPER_MODE else None),
                sniper_min_buy_sell_ratio=SNIPER_MIN_BUY_SELL_RATIO,
                sniper_min_txns_5m=SNIPER_MIN_TXNS_5M,
                sniper_max_change_5m_pct=SNIPER_MAX_PRICE_CHANGE_5M_PCT,
                sniper_min_age_minutes=MIN_PAIR_AGE_MINUTES,
                discovery_stage=("watch" if SNIPER_MODE else "continuation" if MOMENTUM_SWING_MODE else "strict"),
                discovery_min_market_cap_usd=(MOMENTUM_MARKET_CAP_MIN_USD if MOMENTUM_SWING_MODE else 0),
                rejection_counts=pool_rejects)
            accepted += len(candidates)
            source_report.append(f"{endpoint}:{params.get('duration', 'ranked')}:p{page}:{len(candidates)}")
            for mint, score in candidates.items():
                scores[mint] = max(scores.get(mint, 0), score)
        except requests.HTTPError as exc:
            errors += 1
            status = exc.response.status_code if exc.response is not None else 0
            if status == 429:
                try:
                    retry_seconds = min(600, max(180, int(exc.response.headers.get("Retry-After", "180"))))
                except (AttributeError, TypeError, ValueError):
                    retry_seconds = 180
                _quality_global_backoff_until = now + retry_seconds
                # Don't hammer separate pages while provider throttles the IP.
                log.warning("DISCOVERY API BACKOFF source=geckoterminal HTTP=429 pause_s=%d", retry_seconds)
                break
            if status in (400, 404):
                _quality_source_backoff[backoff_key] = now + 1800
            elif status >= 500:
                _quality_source_backoff[backoff_key] = now + 300
            log.warning("QUALITY POOLS source=%s page=%d HTTP=%s", endpoint, page, status)
        except (requests.RequestException, ValueError, TypeError) as exc:
            errors += 1
            log.warning("QUALITY POOLS source=%s page=%d failed: %s", endpoint, page, type(exc).__name__)
    if ROLLING_DISCOVERY_ENABLED and MOMENTUM_SWING_MODE:
        log.info("DISCOVERY ROTATION round=%d planned=%d requests=%d successes=%d skipped_backoff=%d global_backoff_s=%d source_pages=%s errors=%d novelty_drought=%d exploration=%s",
                 _quality_source_round, len(endpoints), attempted, len(source_report), skipped_backoff,
                 max(0, int(_quality_global_backoff_until - now)), source_report, errors,
                 _rolling_watch.novelty_drought, "deep_pages" if _rolling_watch.novelty_drought >= 2 else "normal")
    # Preserve previous healthy sample during transient upstream failures.
    if not scores and errors:
        scores = dict(_quality_cache["scores"])
    ordered = sorted(scores, key=scores.get, reverse=True)[:SNIPER_WATCHLIST_LIMIT if SNIPER_MODE else 200]
    _quality_cache.update(until=now + QUALITY_POOL_CACHE_SECONDS,
                          mints=ordered, scores={m: scores[m] for m in ordered})
    log.info("SNIPER POOLS stage1_discovered=%d stage1_unique=%d pages=%d errors=%d rejected=%s cache_seconds=%d source=geckoterminal_poll fallback_when_ws_down=%s", accepted, len(ordered), len(endpoints), errors, dict(pool_rejects), QUALITY_POOL_CACHE_SECONDS, not _feed_connected) if SNIPER_MODE else log.info("QUALITY POOLS discovered=%d unique=%d errors=%d rejected=%s", accepted, len(ordered), errors, dict(pool_rejects))
    return ordered, dict(_quality_cache["scores"])


def pumpfun_board_candidates() -> dict[str, list[str]]:
    """Collect board mint hints once per minute; allow complete failure without stalling scans.

    Pump.fun UI boards are undocumented. Movers requires credentials; trending
    uses a configurable top-runners proxy, not a guaranteed UI mirror.
    """
    if not PUMPFUN_BOARDS_ENABLED:
        return {"movers": [], "trending": []}
    stamp = time.monotonic()
    if stamp < _board_cache["until"]:
        return {k: list(_board_cache[k]) for k in ("movers", "trending")}
    result = {"movers": [], "trending": []}
    auth = {"Authorization": f"Bearer {PUMPFUN_BOARD_JWT}"} if PUMPFUN_BOARD_JWT else {}
    requests_to_make = []
    if PUMPFUN_MOVERS_ENABLED and PUMPFUN_BOARD_JWT and PUMPFUN_BOARD_SESSION_ID and PUMPFUN_BOARD_USER_ID:
        requests_to_make.append(("movers", MOVERS_URL, {"limit": PUMPFUN_BOARDS_LIMIT, "session_id": PUMPFUN_BOARD_SESSION_ID, "tier": PUMPFUN_BOARD_TIER, "userId": PUMPFUN_BOARD_USER_ID}))
    if PUMPFUN_TRENDING_ENABLED:
        requests_to_make.append(("trending", PUMPFUN_TRENDING_ENDPOINT, {"limit": PUMPFUN_BOARDS_LIMIT}))
    failures = []
    for name, url, params in requests_to_make:
        if not valid_pumpfun_endpoint(url):
            failures.append(f"{name}:invalid_endpoint")
            continue
        try:
            response = session.get(url, params=params, headers={"Accept": "application/json", **auth}, timeout=8)
            response.raise_for_status()
            result[name] = board_mints(response.json(), limit=PUMPFUN_BOARDS_LIMIT)
        except (requests.RequestException, ValueError, TypeError) as exc:
            failures.append(f"{name}:{type(exc).__name__}")
    _board_cache.update(until=stamp + PUMPFUN_BOARDS_CACHE_SECONDS, **result)
    # Avoid producing 502/403 warning spam every minute. No credentials logged.
    if failures and stamp >= _board_cache["warn_at"]:
        log.warning("PUMPFUN BOARDS unavailable=%s; discovery continues with GeckoTerminal and fresh launch feed", ",".join(failures))
        _board_cache["warn_at"] = stamp + 600
    log.info("PUMPFUN BOARDS movers=%d trending_proxy=%d auth_movers=%s interval=%ds",
             len(result["movers"]), len(result["trending"]), bool(PUMPFUN_BOARD_JWT and PUMPFUN_BOARD_SESSION_ID and PUMPFUN_BOARD_USER_ID), PUMPFUN_BOARDS_CACHE_SECONDS)
    return result


def fomo_board_candidates() -> dict[str, list[str]]:
    """Fetch FOMO graduated/trending hints at a credit-conscious interval.

    Missing credentials, stale/unavailable boards, exhausted credits and HTTP
    failures NEVER authorize entries or prevent other discovery sources.
    Auth/quota failures disable the affected board until process restart.
    """
    empty = {"graduated": [], "trending": []}
    if not FOMO_BOARDS_ENABLED or not FOMO_API_KEY:
        return empty
    stamp = time.monotonic()
    if stamp < _fomo_cache["until"]:
        return {key: list(_fomo_cache[key]) for key in empty}
    results = {key: [] for key in empty}
    errors = []
    for board, enabled in (("graduated", FOMO_GRADUATED_ENABLED), ("trending", FOMO_TRENDING_ENABLED)):
        if not enabled or board in _fomo_cache["disabled"]:
            continue
        try:
            response = session.get(
                f"{FOMO_API_BASE_URL}/v2/leaderboard/tokens/{board}",
                params={"limit": FOMO_BOARDS_LIMIT},
                headers={"Authorization": f"Bearer {FOMO_API_KEY}", "Accept": "application/json"},
                timeout=10,
            )
            if response.status_code in (401, 402, 403, 404):
                _fomo_cache["disabled"].add(board)
                errors.append(f"{board}:HTTP{response.status_code}:disabled")
                log.warning("FOMO BOARD DIAGNOSTICS board=%s status=HTTP%d disabled=True", board, response.status_code)
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("available") is False:
                errors.append(f"{board}:unavailable")
                log.warning("FOMO BOARD DIAGNOSTICS board=%s status=unavailable_or_invalid_root", board)
                continue
            if payload.get("stale") is True or payload.get("source") == "captured":
                errors.append(f"{board}:stale_or_captured")
                stale_chains = network_distribution(payload)
                log.warning("FOMO NETWORKS board=%s solana=%d other=%d unknown=%d excluded=stale_or_captured",
                            board, stale_chains["solana"], stale_chains["other"], stale_chains["unknown"])
                log.warning("FOMO BOARD DIAGNOSTICS board=%s status=stale_or_captured source=%s age_hours=%s capture_present=%s",
                            board, "captured" if payload.get("source") == "captured" else "other",
                            str(payload.get("ageHours"))[:16] if isinstance(payload.get("ageHours"), (int, float)) else "unknown",
                            isinstance(payload.get("capturedAt"), str))
                continue
            results[board], diagnostics = inspect_fomo_board(payload, limit=FOMO_BOARDS_LIMIT)
            chain_counts = network_distribution(payload)
            log.info("FOMO NETWORKS board=%s solana=%d other=%d unknown=%d", board,
                     chain_counts["solana"], chain_counts["other"], chain_counts["unknown"])
            log.info("FOMO BOARD DIAGNOSTICS board=%s status=%s records=%d solana_rows=%d valid_mints=%d non_solana=%d missing_network=%d missing_address=%d invalid_address=%d duplicates=%d malformed_rows=%d",
                     board, diagnostics["status"], diagnostics["records"], diagnostics["solana_rows"],
                     diagnostics["valid_mints"], diagnostics["non_solana"], diagnostics["missing_network"],
                     diagnostics["missing_address"], diagnostics["invalid_address"], diagnostics["duplicates"],
                     diagnostics["malformed_rows"])
        except (requests.RequestException, ValueError, TypeError) as exc:
            errors.append(f"{board}:{type(exc).__name__}")
            log.warning("FOMO BOARD DIAGNOSTICS board=%s status=%s", board, type(exc).__name__)
    _fomo_cache.update(until=stamp + FOMO_BOARDS_CACHE_SECONDS, **results)
    if errors and stamp >= _fomo_cache["warn_at"]:
        log.warning("FOMO BOARDS unavailable=%s; GeckoTerminal discovery continues", ",".join(errors))
        _fomo_cache["warn_at"] = stamp + 1800
    log.info("FOMO BOARDS graduated=%d trending=%d enabled=%s interval=%ds",
             len(results["graduated"]), len(results["trending"]), bool(FOMO_API_KEY), FOMO_BOARDS_CACHE_SECONDS)
    return results


def recent_profile_pool_hints() -> tuple[dict[str, float], dict]:
    """Recent listing profiles are *advertisements*, not momentum evidence.

    Take only unseen Solana mints, then validate their actual indexed SOL
    pools in a single capped DEX Screener token-batch request. This is a
    discovery-only lane; it never skips stage-2 eligibility or liquidity
    verification. A failure of either request never blocks other feeds.
    """
    _profile_near_hints.clear()
    if not RECENT_PROFILE_DISCOVERY_ENABLED:
        return {}, {"status": "disabled"}
    try:
        rows = get_json(f"{BASE}/token-profiles/latest/v1")
        if not isinstance(rows, list):
            return {}, {"status": "invalid_profiles"}
        _rolling_watch.load()
        seen = set(_rolling_watch.records)
        mints = []
        for row in rows:
            if not isinstance(row, dict) or row.get("chainId") != "solana":
                continue
            mint = row.get("tokenAddress")
            if (isinstance(mint, str) and MINT_PATTERN.fullmatch(mint)
                    and mint != SOL_MINT and mint not in seen and mint not in mints):
                mints.append(mint)
            if len(mints) == 30:
                break
        if not mints:
            return {}, {"status": "no_unseen_profiles", "unseen": 0}
        pairs = dex_request(f"{BASE}/tokens/v1/solana/{','.join(mints)}")
        _profile_near_hints.update(early_dex_pairs(pairs,
            sol_mint=SOL_MINT, supported_dexes=active_entry_routes(),
            min_age=MOMENTUM_MIN_PAIR_AGE_MINUTES, min_cap=MOMENTUM_MARKET_CAP_MIN_USD,
            max_cap=MOMENTUM_MARKET_CAP_MAX_USD))
        rejects = Counter()
        scores = search_pool_hints(
            pairs, sol_mint=SOL_MINT,
            supported_dexes=frozenset(d.replace("-", "").replace("_", "")
                                      for d in active_entry_routes()),
            min_liquidity_usd=active_entry_liquidity_min(),
            min_pair_age_minutes=MOMENTUM_MIN_PAIR_AGE_MINUTES,
            min_cap_usd=MOMENTUM_MARKET_CAP_MIN_USD,
            max_cap_usd=MOMENTUM_MARKET_CAP_MAX_USD,
            rejections=rejects)
        return scores, {"status": "ok", "unseen": len(mints), "qualified_hints": len(scores),
                        "rejects": dict(rejects)}
    except DexRateLimited:
        return {}, {"status": "rate_limited"}
    except (requests.RequestException, ValueError, TypeError) as exc:
        return {}, {"status": type(exc).__name__}


def latest_candidate_addresses() -> list[str]:
    """Discover Solana mints from profiles, boosts and rotating DEX search queries.

    Discovery data is cached briefly to avoid hammering public APIs every five seconds.
    Pair quotes are still refreshed on each scan. Search results are *not* trusted
    as executable routes: entry_pair_reason validates the actual SOL pool.
    """
    global _discovery_round
    now = time.monotonic()
    if now < _discovery_cache["until"]:
        return list(_discovery_cache["addresses"])

    addresses: list[str] = []
    priority: dict[str, float] = {}
    sources: dict[str, int] = {}
    _search_near_hints: dict[str, dict] = {}
    # Promotion/boost feeds are not organic fast movers; skip them in v7.
    endpoints = [] if FRESH_BATCH_DISCOVERY_ENABLED else [
        f"{BASE}/token-profiles/latest/v1",
        f"{BASE}/token-boosts/latest/v1",
        f"{BASE}/token-boosts/top/v1",
    ]
    for url in endpoints:
        try:
            data = get_json(url)
            matches = [item["tokenAddress"] for item in data
                       if isinstance(item, dict) and item.get("chainId") == "solana"
                       and item.get("tokenAddress")] if isinstance(data, list) else []
            addresses.extend(mint for mint in matches if mint != SOL_MINT)
            sources[url.rsplit('/', 3)[-3]] = len(matches)
        except DexRateLimited:
            break
        except Exception as exc:
            log.warning("Discovery endpoint failed (%s): %s", url, exc)

    if DISCOVERY_SEARCH_TERMS:
        if ROLLING_DISCOVERY_ENABLED and MOMENTUM_SWING_MODE:
            _rolling_watch.load()
            # Saved cursor makes restart continue the rotating search cycle.
            _discovery_round = _rolling_watch.search_cursor
        terms, next_search_round = rotating_search_terms(
            DISCOVERY_SEARCH_TERMS, _discovery_round, DISCOVERY_SEARCHES_PER_SCAN)
        search_valid = set()
        search_rejects = Counter()
        completed = 0
        for term in terms:
            try:
                pairs = dex_request(f"{BASE}/latest/dex/search", params={"q": term}).get("pairs") or []
                if MOMENTUM_SWING_MODE:
                    _search_near_hints.update(early_dex_pairs(pairs,
                        sol_mint=SOL_MINT, supported_dexes=active_entry_routes(),
                        min_age=MOMENTUM_MIN_PAIR_AGE_MINUTES,
                        min_cap=MOMENTUM_MARKET_CAP_MIN_USD,
                        max_cap=MOMENTUM_MARKET_CAP_MAX_USD))
                if ROLLING_DISCOVERY_ENABLED and MOMENTUM_SWING_MODE:
                    hints = search_pool_hints(
                        pairs, sol_mint=SOL_MINT,
                        supported_dexes=frozenset(d.replace("-", "").replace("_", "")
                                                  for d in active_entry_routes()),
                        min_liquidity_usd=active_entry_liquidity_min(),
                        min_pair_age_minutes=MOMENTUM_MIN_PAIR_AGE_MINUTES,
                        min_cap_usd=MOMENTUM_MARKET_CAP_MIN_USD,
                        max_cap_usd=MOMENTUM_MARKET_CAP_MAX_USD,
                        rejections=search_rejects)
                    matches = list(hints)
                    for mint, score in hints.items():
                        priority[mint] = max(priority.get(mint, 0), score)
                else:
                    matches = []
                    for pair in pairs:
                        if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                            continue
                        mint = (pair.get("baseToken") or {}).get("address")
                        if (not mint or mint == SOL_MINT or
                                (pair.get("quoteToken") or {}).get("address") != SOL_MINT or
                                (pair.get("dexId") or "").lower() not in active_entry_routes()):
                            continue
                        matches.append(mint)
                        liquidity = as_float((pair.get("liquidity") or {}).get("usd"))
                        if liquidity >= active_entry_liquidity_min():
                            priority[mint] = max(priority.get(mint, 0), liquidity)
                addresses.extend(matches)
                search_valid.update(matches)
                sources[f"search:{term}"] = len(set(matches))
                completed += 1
            except DexRateLimited:
                break
            except Exception as exc:
                log.warning("Discovery search failed (%s): %s", term, exc)
        # When throttled, retry the unfinished term on the next refresh.
        if completed == len(terms):
            _discovery_round = next_search_round
        else:
            _discovery_round = (_discovery_round + completed) % len(DISCOVERY_SEARCH_TERMS)
        if ROLLING_DISCOVERY_ENABLED and MOMENTUM_SWING_MODE:
            _rolling_watch.search_cursor = _discovery_round
            log.info("DISCOVERY SEARCH terms=%s requests_ok=%d unique_hints=%d rejected=%s next_cursor=%d",
                     terms, completed, len(search_valid), dict(search_rejects), _discovery_round)

    fomo_results = fomo_board_candidates()
    fomo_mints = list(dict.fromkeys(fomo_results["graduated"] + fomo_results["trending"]))
    if SNIPER_MODE:
        update_sniper_watchlist({mint: 1.0 for mint in fomo_mints})
    sources["fomo_graduated"] = len(fomo_results["graduated"])
    sources["fomo_trending"] = len(fomo_results["trending"])

    board_results = pumpfun_board_candidates()
    board_mint_list = list(dict.fromkeys(board_results["movers"] + board_results["trending"]))
    # Board rankings only influence what to inspect, not what is eligible.
    # Stage 2 independently validates SOL pool, age, momentum and sellability.
    if SNIPER_MODE:
        update_sniper_watchlist({mint: 1.0 for mint in board_mint_list})
    sources["pumpfun_movers"] = len(board_results["movers"])
    sources["pumpfun_trending_proxy"] = len(board_results["trending"])

    quality_mints, quality_scores = quality_pool_candidates()
    _quality_pool_mints[:] = quality_mints
    if SNIPER_MODE:
        update_sniper_watchlist(quality_scores)
    for mint, score in quality_scores.items():
        priority[mint] = max(priority.get(mint, 0.0), score)
    sources["quality_pools"] = len(quality_mints)

    profile_scores, profile_health = recent_profile_pool_hints()
    for mint, score in profile_scores.items():
        priority[mint] = max(priority.get(mint, 0.0), score)
    sources["recent_profile_verified_pools"] = len(profile_scores)
    log.info("DISCOVERY RECENT PROFILE status=%s unseen=%s verified_sol_pool_hints=%d "
             "rejects=%s promotion_not_momentum=True",
             profile_health.get("status"), profile_health.get("unseen", 0),
             len(profile_scores), profile_health.get("rejects", {}))


    # v7.3 independent fast-mover search: one bounded API request when due.
    # Mint candidates are hints only; primary market/route, cost, trend,
    # fresh price and sellability checks below are unchanged.
    independent_scores, independent_health = _independent_movers.poll(
        session, sol_mint=SOL_MINT, min_liquidity=active_entry_liquidity_min(),
        min_age_minutes=MOMENTUM_MIN_PAIR_AGE_MINUTES,
        min_cap=MOMENTUM_MARKET_CAP_MIN_USD, max_cap=MOMENTUM_MARKET_CAP_MAX_USD)
    for mint, score in independent_scores.items():
        priority[mint] = max(priority.get(mint, 0.0), score)
    sources["dexpaprika_movers"] = len(independent_scores)
    # v7.7 extra independent rankings: Jupiter (optional key), Birdeye
    # (optional paid key), and verified DexScreener promoted/community leads.
    # Each feed is separately rate-bounded; every hint is screened against
    # real, SOL-quoted DexScreener pools before reaching the existing batch.
    extra_sources, extra_near, extra_health = _expanded_movers.poll(
        session, dex_request, sol_mint=SOL_MINT, supported_dexes=set(active_entry_routes()),
        min_liquidity=active_entry_liquidity_min(), min_age=MOMENTUM_MIN_PAIR_AGE_MINUTES,
        min_cap=MOMENTUM_MARKET_CAP_MIN_USD, max_cap=MOMENTUM_MARKET_CAP_MAX_USD)
    for name, scores in extra_sources.items():
        sources[name+'_fast_hints'] = len(scores)
        for mint, score in scores.items():
            priority[mint] = max(priority.get(mint, 0.0), score)
    for name, status in extra_health.items():
        if status.get('status') not in ('waiting', 'no_key_optional'):
            log.info("DISCOVERY EXPANDED source=%s status=%s mode=%s raw=%d verified_fast=%d "
                     "near=%d rejects=%s retry_s=%d promotional_only=%s",
                     name, status.get('status'), status.get('ranking', 'none'),
                     status.get('raw_mints', 0), len(extra_sources.get(name, {})),
                     len(extra_near.get(name, {})), status.get('rejected', {}),
                     status.get('retry_s', 0), name == 'dex_promotion_verified')
    # Observe promising immature pools from EVERY discovery lane, without
    # adding them to the active tradable batch before the scheduled maturity
    # check. All hints are structural and can be invalidated at any later time.
    near_by_source = {
        'geckoterminal':dict(_gecko_near_hints),
        'recent_profiles':dict(_profile_near_hints),
        'dex_search':_search_near_hints,
        'dexpaprika':(getattr(_independent_movers, 'last_near', {})
                      if independent_health.get('status') == 'ok'
                      and isinstance(getattr(_independent_movers, 'last_near', {}), dict) else {}),
        **extra_near,
    }
    for name, hints in near_by_source.items():
        if hints:
            _candidate_pipeline.register_sources({name:hints})
    near_hints = {}
    for hints in near_by_source.values():
        for mint, hint in hints.items():
            if mint not in near_hints or hint['due'] < near_hints[mint]['due']:
                near_hints[mint] = hint
    early_found = _candidate_pipeline.offer_near(near_hints)
    if near_hints:
        log.info('DISCOVERY EARLY WATCH SOURCES hints=%s offered_unique=%d observation_only=True',
                 {name:len(items) for name,items in near_by_source.items()}, len(near_hints))
    near_due = _candidate_pipeline.matured_due(limit=12)
    for mint, score in near_due.items():
        priority[mint] = max(priority.get(mint, 0.0), score)
    if early_found or near_due:
        log.info("DISCOVERY EARLY WATCH new=%d due_for_fresh_market_check=%d watched=%d paper_entry_requires_normal_filters=True",
                 early_found, len(near_due), len(_candidate_pipeline.near))
    if independent_health.get("status") != "waiting":
        log.info("DISCOVERY INDEPENDENT source=dexpaprika status=%s ranking=%s hints=%d rejected=%s "
                 "cursor_advanced=%s retry_s=%s interval_s=%d",
                 independent_health.get("status"), independent_health.get("mode", "none"),
                 len(independent_scores), independent_health.get("rejected", {}),
                 independent_health.get("cursor_advanced", False),
                 independent_health.get("retry_s", 0), _independent_movers.interval)


    rolling_mints: list[str] = []
    if ROLLING_DISCOVERY_ENABLED and MOMENTUM_SWING_MODE:
        if _rolling_watch.read_only:
            log.warning("DISCOVERY WATCHLIST persisted sidecar unreadable; leaving original file unchanged and using in-memory rotation")
        # Record only structurally plausible candidates: quality pools plus
        # supported/liquid SOL-quoted search hits, not paid-boost spam.
        # Source-yield telemetry is computed against the persisted historical
        # universe BEFORE update: reactivated old mints are never "truly new".
        historical = set(_rolling_watch.records)
        source_yields = {
            "geckoterminal": (len(set(quality_mints)), len(set(quality_mints) - historical)),
            "dexpaprika": (len(independent_scores), len(set(independent_scores) - historical)),
            "recent_profiles": (len(profile_scores), len(set(profile_scores) - historical)),
            "dex_search": (len(search_valid) if DISCOVERY_SEARCH_TERMS else 0,
                           len(set(search_valid) - historical) if DISCOVERY_SEARCH_TERMS else 0),
            **{name:(len(scores),len(set(scores)-historical))
               for name,scores in extra_sources.items()},
        }
        fresh = _rolling_watch.update(list(quality_mints) + list(priority), priority)
        log.info("DISCOVERY SOURCE YIELD %s unique_this_refresh=%d source_overlaps_possible=True",
                 source_yields, len(priority))
        drought = _rolling_watch.note_discovery_yield(fresh)
        rolling_mints = _rolling_watch.priority_mints()
        log.info("DISCOVERY NOVELTY truly_new_watchlist=%d source_hints=%d prior_known=%d zero_new_refreshes=%d next_mode=%s", 
                 fresh, len(priority), max(0, len(priority) - fresh), drought,
                 "deep_pages" if drought >= 2 else "normal")
        log.info("DISCOVERY WATCHLIST new_mints=%d watchlist_total=%d unscanned=%d rotating_sources=%d state=%s",
                 fresh, len(rolling_mints),
                 sum(_rolling_watch.records[m]["last_scanned"] == 0 for m in rolling_mints),
                 _quality_source_round, _rolling_watch.path)
        _rolling_watch.save()

    if FRESH_BATCH_DISCOVERY_ENABLED:
        # SOURCE hints must be from *this* refresh, not the 6h historical
        # watchlist. Rejected mints are removed from the active batch and
        # cannot be offered again until their bounded quarantine expires.
        _candidate_pipeline.register_sources({
            "geckoterminal":quality_mints,
            "dexpaprika":independent_scores,
            "recent_profiles":profile_scores,
            "dex_search":search_valid if DISCOVERY_SEARCH_TERMS else (),
            "near_watch":near_due,
            **extra_sources,
        })
        # Normalize incomparable provider priority scales; record source
        # attribution for later first-pass conversion telemetry.
        offered = _fresh_batch.offer(_candidate_pipeline.rank(priority))
        active_stats = _fresh_batch.stats()
        log.info("FRESH BATCH REFILL discovered=%d active=%d unscanned=%d blocked_repeat=%d reactivated=%d quarantined=%d state=%s",
                 offered["new"], active_stats["active"], active_stats["unscanned"],
                 offered["blocked"], offered["reactivated"], active_stats["quarantined"], _fresh_batch.path)
        _fresh_batch.save()
        log.info("CANDIDATE SOURCE CONVERSION lifetime_24h=%s first_market_pass_not_paper_buy=True", _candidate_pipeline.source_metrics())
        _candidate_pipeline.save()

    # Feed mints are added even when DexScreener profiles/boosts are unchanged.
    # A newly launched token may have no indexed SOL pair yet; that is normal.
    stream_mints = fresh_launch_snapshot()
    sources["fresh_launch_feed"] = len(stream_mints)
    unique = ([m for m in _fresh_batch.due(limit=FRESH_BATCH_ACTIVE_LIMIT) if m != SOL_MINT]
              if FRESH_BATCH_DISCOVERY_ENABLED else
              [mint for mint in dict.fromkeys(rolling_mints + quality_mints + addresses + fomo_mints + board_mint_list + stream_mints) if mint != SOL_MINT])
    # Preserve rotating coverage of all addresses; prioritize known viable search
    # hits, but do not permanently starve the remainder of the pool.
    _discovery_priority.clear()
    _discovery_priority.update(priority)
    fresh = sum(mint not in _discovery_seen for mint in unique)
    _discovery_seen.update(unique)
    log.info("DISCOVERY sources=%s raw=%d unique=%d new_since_start=%d supported_liquid_search_mints=%d; new_since_start_is_process_only=True", sources, len(addresses), len(unique), fresh, len(priority))
    # Keep the last successful token list during rate limits or temporary outages.
    if unique:
        _discovery_cache.update(until=time.monotonic() + DISCOVERY_CACHE_SECONDS, addresses=unique)
        return unique
    _discovery_cache["until"] = time.monotonic() + DISCOVERY_CACHE_SECONDS
    return list(_discovery_cache["addresses"])


# Fresh status of the most recent token-pairs lookup; absence/HTTP failures are
# NOT reliable proof of drained liquidity. Held-position scans consume this.
_token_pairs_healthy: dict[str, bool] = {}


def token_pairs(address: str) -> list[dict]:
    _token_pairs_healthy[address] = False
    try:
        data = get_json(f"{BASE}/token-pairs/v1/solana/{address}")
        if isinstance(data, list):
            _token_pairs_healthy[address] = True
            return data
        return []
    except DexRateLimited:
        return []
    except Exception as exc:
        log.debug("Pair lookup failed for %s: %s", address, exc)
        return []


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def age_hours(pair: dict) -> float:
    created = pair.get("pairCreatedAt")
    if not created:
        return 1e9
    try:
        created_ms = float(created)
    except (TypeError, ValueError, OverflowError):
        return 1e9
    if not (0 < created_ms <= time.time() * 1000 + 60000):
        return 1e9
    return max(0.0, (time.time() * 1000 - created_ms) / 3_600_000)


def best_pair(pairs: list[dict]) -> dict | None:
    # Pick the pair with the deepest liquidity, not necessarily the highest FDV.
    sol_pairs = [p for p in pairs if p.get("chainId") == "solana" and p.get("priceUsd")]
    if not sol_pairs:
        return None
    return max(sol_pairs, key=lambda p: as_float((p.get("liquidity") or {}).get("usd")))


# Entry eligibility is distinct from price discovery for held positions.
# Only these pool identifiers have an explicit PumpPortal route in our staging flow.
SUPPORTED_SOL_ROUTES = {"pumpfun": "pump", "pumpswap": "pump-amm"}


def active_entry_routes() -> frozenset[str]:
    # Paper analysis of older pools is possible without authorizing live swaps.
    if MOMENTUM_SWING_MODE and not SNIPER_MODE:
        return frozenset(SUPPORTED_SOL_ROUTES) | MOMENTUM_PAPER_DEXES
    return frozenset(SUPPORTED_SOL_ROUTES)


def active_entry_liquidity_min() -> float:
    return max(MIN_SOL_PAIR_LIQUIDITY_USD, MOMENTUM_MIN_LIQUIDITY_USD) if MOMENTUM_SWING_MODE and not SNIPER_MODE else MIN_SOL_PAIR_LIQUIDITY_USD


def momentum_market_cap(pair: dict) -> tuple[float | None, str]:
    # FDV estimates fully diluted supply, NOT circulating market cap.
    cap = as_float(pair.get("marketCap"), -1.0)
    if math.isfinite(cap) and cap > 0:
        return cap, "marketCap"
    fdv = as_float(pair.get("fdv"), -1.0)
    if MOMENTUM_ALLOW_FDV_FALLBACK and math.isfinite(fdv) and fdv > 0:
        return fdv, "fdv_proxy"
    return None, "missing_market_cap"


def momentum_trend_rejection(pair: dict) -> str | None:
    """Require fresh short-term lift AND aligned 1h/6h/24h windows.

    Absent data is not interpreted as zero or green. This is a coarse API trend
    proxy, NOT a measured moving-average slope or candle confirmation.
    """
    volume = pair.get("volume") or {}
    change = pair.get("priceChange") or {}
    txns = (pair.get("txns") or {}).get("m5") or {}
    for window in ("m5", "h1", "h6", "h24"):
        if change.get(window) is None:
            return f"trend_{window}_missing"
        if not math.isfinite(as_float(change[window], float("nan"))):
            return f"trend_{window}_invalid"
    if as_float(volume.get("m5")) < MOMENTUM_MIN_VOLUME_5M_USD:
        return "momentum_volume"
    if as_float(volume.get("h1")) < MOMENTUM_MIN_VOLUME_1H_USD:
        return "momentum_volume_1h"
    ch5, ch1, ch6, ch24 = [as_float(change.get(k)) for k in ("m5", "h1", "h6", "h24")]
    if not MOMENTUM_MIN_CHANGE_5M_PCT <= ch5 <= MOMENTUM_MAX_CHANGE_5M_PCT:
        return "momentum_price_change"
    if ch1 < MOMENTUM_MIN_CHANGE_1H_PCT or ch6 < MOMENTUM_MIN_CHANGE_6H_PCT or ch24 < MOMENTUM_MIN_CHANGE_24H_PCT:
        return "downtrend_multi_timeframe"
    if ch1 > 120 or ch6 > 300 or ch24 > 800:
        return "extreme_price_spike"
    buys, sells = as_float(txns.get("buys")), as_float(txns.get("sells"))
    if buys + sells < MOMENTUM_MIN_TXNS_5M or sells <= 0 or buys / sells < MOMENTUM_MIN_BUY_SELL_RATIO:
        return "momentum_buy_sell_ratio"
    liq = as_float((pair.get("liquidity") or {}).get("usd"))
    if liq > 0 and as_float(volume.get("m5")) / liq < MOMENTUM_MIN_VOLUME_LIQUIDITY_RATIO:
        return "momentum_volume_liquidity_ratio"
    return None


def entry_pair_reason(pair: dict) -> str | None:
    if pair.get("chainId") != "solana":
        return "not a Solana pair"
    quote = pair.get("quoteToken") or {}
    if quote.get("address") != SOL_MINT:
        return f"unsupported quote mint {quote.get('symbol') or quote.get('address') or 'unknown'}"
    dex = (pair.get("dexId") or "").lower()
    if dex not in active_entry_routes():
        return f"unsupported {'PumpPortal' if SNIPER_MODE else 'paper-market'} route {dex or 'unknown'}"
    liq = as_float((pair.get("liquidity") or {}).get("usd"))
    if liq < active_entry_liquidity_min():
        return f"SOL-pair liquidity ${liq:.0f} below ${active_entry_liquidity_min():.0f}"
    return None


def entry_pair_diagnostic(pairs: list[dict]) -> tuple[str, dict | None]:
    """Report the actual blocking stage, without treating absent liquidity as zero.

    A token may have a liquid USDC/Raydium pool and an unindexed Pump.fun
    bonding curve. Reporting the deepest unrelated pool as a SOL liquidity
    failure obscures that distinction.
    """
    solana = [p for p in pairs if p.get("chainId") == "solana"]
    sol_quoted = [p for p in solana if (p.get("quoteToken") or {}).get("address") == SOL_MINT]
    supported = [p for p in sol_quoted if (p.get("dexId") or "").lower() in active_entry_routes()]
    priced = [p for p in supported if as_float(p.get("priceUsd")) > 0]
    if not solana:
        return "no_solana_pair", None
    if not sol_quoted:
        return "no_sol_quoted_pair", max(solana, key=lambda p: as_float((p.get("liquidity") or {}).get("usd")))
    if not supported:
        return "unsupported_sol_route", max(sol_quoted, key=lambda p: as_float((p.get("liquidity") or {}).get("usd")))
    if not priced:
        return "supported_pair_missing_price", supported[0]
    with_liquidity = [p for p in priced if (p.get("liquidity") or {}).get("usd") is not None]
    if not with_liquidity:
        return "supported_pair_liquidity_unavailable", priced[0]
    deepest = max(with_liquidity, key=lambda p: as_float((p.get("liquidity") or {}).get("usd")))
    if as_float((deepest.get("liquidity") or {}).get("usd")) < MIN_SOL_PAIR_LIQUIDITY_USD:
        return "supported_pair_low_liquidity", deepest
    return "eligible_route_liquidity", deepest


# GeckoTerminal's public endpoint is cached, rate limited, and indicative.
# Only use it when DexScreener has NO liquidity number (not when it reports
# a genuine low value). Prices, pool ID, SOL quote and mint must all agree.
_secondary_liquidity_cache: dict[tuple[str, str], tuple[float, dict | None, str]] = {}
_secondary_request_times: list[float] = []
_secondary_paused_until = 0.0



# These are discovery hints, not buy authorizations. Every deferred mint must
# be re-fetched and pass all normal entry and prebuy gates before a paper fill.
_secondary_deferred: OrderedDict[str, tuple[float, float, float]] = OrderedDict()


def secondary_candidate_priority(pair: dict, *, allow_reentry_age: bool = False) -> float | None:
    """Reject obviously non-tradeable candidates BEFORE spending scarce HTTP checks.

    Mirrors liquidity-independent hard momentum criteria. It intentionally
    cannot establish sufficient liquidity, route execution, or sellability.
    """
    if not PAPER_SECONDARY_LIQUIDITY_ENABLED or (pair.get("liquidity") or {}).get("usd") is not None:
        return None
    if pair.get("chainId") != "solana" or (pair.get("dexId") or "").lower() not in active_entry_routes():
        return None
    if (pair.get("quoteToken") or {}).get("address") != SOL_MINT or not pair.get("pairAddress"):
        return None
    mint = (pair.get("baseToken") or {}).get("address")
    if not mint or as_float(pair.get("priceUsd")) <= 0:
        return None
    age = age_hours(pair)
    permitted = (PAPER_REENTRY_MAX_POOL_AGE_MINUTES if allow_reentry_age else SNIPER_MAX_AGE_MINUTES) if SNIPER_MODE else ((MOMENTUM_MAX_PAIR_AGE_HOURS * 60 if MOMENTUM_MAX_PAIR_AGE_HOURS else float("inf")) if MOMENTUM_SWING_MODE else MAX_PAIR_AGE_HOURS * 60)
    if not math.isfinite(age) or age >= 1e9 or not (MOMENTUM_MIN_PAIR_AGE_MINUTES if MOMENTUM_SWING_MODE and not SNIPER_MODE else MIN_PAIR_AGE_MINUTES) <= age * 60 <= permitted:
        return None
    m5_volume = as_float((pair.get("volume") or {}).get("m5"))
    buys = as_float(((pair.get("txns") or {}).get("m5") or {}).get("buys"))
    sells = as_float(((pair.get("txns") or {}).get("m5") or {}).get("sells"))
    change = as_float((pair.get("priceChange") or {}).get("m5"))
    change_h1 = as_float((pair.get("priceChange") or {}).get("h1"))
    if m5_volume < (MOMENTUM_MIN_VOLUME_5M_USD if MOMENTUM_SWING_MODE else MIN_VOLUME_5M_USD) or buys + sells < 8 or sells <= 0:
        return None
    if change > 80 or change_h1 > 250 or change_h1 < -45 or buys / sells < 1:
        return None
    if SNIPER_MODE or FAST_MOMENTUM_ENABLED:
        if m5_volume < (SNIPER_MIN_VOLUME_5M_USD if SNIPER_MODE else MOMENTUM_MIN_VOLUME_5M_USD):
            return None
        if not (SNIPER_MIN_PRICE_CHANGE_5M_PCT if SNIPER_MODE else MOMENTUM_MIN_CHANGE_5M_PCT) <= change <= (SNIPER_MAX_PRICE_CHANGE_5M_PCT if SNIPER_MODE else MOMENTUM_MAX_CHANGE_5M_PCT):
            return None
        if buys + sells < (SNIPER_MIN_TXNS_5M if SNIPER_MODE else MOMENTUM_MIN_TXNS_5M):
            return None
        if buys / sells < (SNIPER_MIN_BUY_SELL_RATIO if SNIPER_MODE else MOMENTUM_MIN_BUY_SELL_RATIO):
            return None
    if MOMENTUM_SWING_MODE and not SNIPER_MODE:
        cap, _ = momentum_market_cap(pair)
        if cap is None or not MOMENTUM_MARKET_CAP_MIN_USD <= cap <= MOMENTUM_MARKET_CAP_MAX_USD:
            return None
        if momentum_trend_rejection(pair) is not None:
            return None
    # Highest evidence of tradeability goes first. Limited influence of spikes.
    return min(m5_volume, 20000) / 1000 + min(buys + sells, 100) / 10 + min(change, 20) / 5


def secondary_defer(mint: str, priority: float, *, retry_at: float | None = None, now: float | None = None) -> None:
    stamp = time.monotonic() if now is None else now
    if not mint or PAPER_SECONDARY_DEFERRED_QUOTA <= 0:
        return
    until = stamp + PAPER_SECONDARY_DEFERRED_TTL_SECONDS
    _secondary_deferred[mint] = (until, retry_at if retry_at is not None else stamp + 60, priority)
    _secondary_deferred.move_to_end(mint)
    while len(_secondary_deferred) > 100:
        _secondary_deferred.popitem(last=False)


def secondary_due_mints(*, now: float | None = None) -> list[str]:
    stamp = time.monotonic() if now is None else now
    for mint, (expires, _, _) in list(_secondary_deferred.items()):
        if expires < stamp:
            _secondary_deferred.pop(mint, None)
    ranked = sorted(((mint, priority) for mint, (_, due, priority) in _secondary_deferred.items()
                     if stamp >= due), key=lambda row: row[1], reverse=True)
    return [mint for mint, _ in ranked[:PAPER_SECONDARY_DEFERRED_QUOTA]]


def independently_verify_missing_liquidity(pair: dict) -> tuple[dict | None, str]:
    global _secondary_paused_until
    if not PAPER_SECONDARY_LIQUIDITY_ENABLED:
        return None, "disabled"
    if (pair.get("liquidity") or {}).get("usd") is not None:
        return None, "primary_liquidity_present"
    if pair.get("chainId") != "solana" or (pair.get("dexId") or "").lower() not in active_entry_routes():
        return None, "unsupported_route"
    mint = (pair.get("baseToken") or {}).get("address", "")
    pool = pair.get("pairAddress") or ""
    if (pair.get("quoteToken") or {}).get("address") != SOL_MINT or not mint or not pool or not (32 <= len(pool) <= 64):
        return None, "unsupported_pair_orientation_or_id"
    price = as_float(pair.get("priceUsd"))
    if not math.isfinite(price) or price <= 0:
        return None, "primary_price_missing"
    now = time.monotonic()
    key = (pool, mint)
    cached = _secondary_liquidity_cache.get(key)
    # A recent independently verified pool may be reused during an API outage,
    # but a cached failure/429 must never masquerade as successful evidence.
    if now < _secondary_paused_until and not (cached and now < cached[0] and cached[1] is not None):
        return None, "rate_limit_cooldown"
    if cached and now < cached[0]:
        evidence, status = cached[1], cached[2]
    else:
        if now < _secondary_paused_until:
            return None, "rate_limit_cooldown"
        _secondary_request_times[:] = [t for t in _secondary_request_times if now - t < 60]
        if len(_secondary_request_times) >= PAPER_SECONDARY_LIQUIDITY_MAX_PER_MINUTE:
            return None, "minute_request_budget"
        _secondary_request_times.append(now)
        evidence = None
        status = "secondary_unavailable"
        try:
            response = session.get(
                f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}",
                timeout=8, headers={"Accept": "application/json;version=20230203"})
            if response.status_code == 429:
                _secondary_paused_until = now + 120
                status = "secondary_rate_limited"
            else:
                response.raise_for_status()
                item = response.json().get("data") or {}
                relationships = item.get("relationships") or {}
                base = ((relationships.get("base_token") or {}).get("data") or {}).get("id")
                quote = ((relationships.get("quote_token") or {}).get("data") or {}).get("id")
                attributes = item.get("attributes") or {}
                if item.get("id") != "solana_" + pool or attributes.get("address") != pool:
                    status = "secondary_wrong_pool"
                elif base != "solana_" + mint or quote != "solana_" + SOL_MINT:
                    status = "secondary_wrong_token_or_quote"
                else:
                    depth = as_float(attributes.get("reserve_in_usd"))
                    other_price = as_float(attributes.get("base_token_price_usd"))
                    required = max(MIN_SOL_PAIR_LIQUIDITY_USD, MIN_LIQUIDITY_USD) * (
                        SNIPER_MIN_ENTRY_LIQUIDITY_MULTIPLIER if SNIPER_MODE else 1)
                    if not all(math.isfinite(v) for v in (depth, other_price)) or depth < required or other_price <= 0:
                        status = "secondary_insufficient_depth_or_price"
                    elif abs(other_price / price - 1) * 100 > PAPER_PREBUY_MAX_CROSSCHECK_PRICE_DEVIATION_PCT:
                        status = "secondary_price_disagreement"
                    else:
                        evidence = {"price": other_price, "liquidity": depth}
                        status = "verified_indicative_only"
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            status = "secondary_request_failed"
        ttl = max(90, PAPER_SECONDARY_LIQUIDITY_CACHE_SECONDS) if status in (
            "secondary_insufficient_depth_or_price", "secondary_wrong_pool",
            "secondary_wrong_token_or_quote") else PAPER_SECONDARY_LIQUIDITY_CACHE_SECONDS
        _secondary_liquidity_cache[key] = (now + ttl, evidence, status)
        if len(_secondary_liquidity_cache) > 500:
            for old in list(_secondary_liquidity_cache)[:100]:
                _secondary_liquidity_cache.pop(old, None)
    if evidence is None:
        return None, status
    # Price tolerance must also be checked when reusing a cached observation.
    if abs(evidence["price"] / price - 1) * 100 > PAPER_PREBUY_MAX_CROSSCHECK_PRICE_DEVIATION_PCT:
        return None, "secondary_price_disagreement"
    recovered = dict(pair)
    recovered["liquidity"] = dict(pair.get("liquidity") or {}, usd=evidence["liquidity"])
    recovered["liquidity_data_source"] = "geckoterminal_indicative"
    return recovered, "verified_indicative_only"


def best_entry_pair(pairs: list[dict]) -> dict | None:
    """Select the deepest *age-eligible* supported SOL pool when sniping.

    Tokens can have multiple pools. A deep older pool must not hide a newly
    deployed, sufficiently liquid, independent pool for the same base mint.
    No route, liquidity, price or entry filter is relaxed here.
    """
    eligible = [p for p in pairs if entry_pair_reason(p) is None and as_float(p.get("priceUsd")) > 0]
    if SNIPER_MODE:
        fresh = [p for p in eligible
                 if MIN_PAIR_AGE_MINUTES <= age_hours(p) * 60 <= SNIPER_MAX_AGE_MINUTES]
        if fresh:
            eligible = fresh
    elif MOMENTUM_SWING_MODE:
        aligned = [p for p in eligible if
                   age_hours(p) * 60 >= MOMENTUM_MIN_PAIR_AGE_MINUTES and
                   (not MOMENTUM_MAX_PAIR_AGE_HOURS or age_hours(p) <= MOMENTUM_MAX_PAIR_AGE_HOURS) and
                   momentum_trend_rejection(p) is None and
                   (lambda cap: cap is not None and MOMENTUM_MARKET_CAP_MIN_USD <= cap <= MOMENTUM_MARKET_CAP_MAX_USD)(momentum_market_cap(p)[0])]
        if aligned:
            eligible = aligned
    return max(eligible, key=lambda p: as_float((p.get("liquidity") or {}).get("usd")), default=None)


def analyze_pair(pair: dict, reject_reasons: Counter | None = None, *, allow_reentry_age: bool = False) -> dict | None:
    token = pair.get("baseToken") or {}
    address = token.get("address")
    price = as_float(pair.get("priceUsd"))
    liquidity = as_float((pair.get("liquidity") or {}).get("usd"))
    volume = pair.get("volume") or {}
    txns = pair.get("txns") or {}
    change = pair.get("priceChange") or {}
    m5_volume = as_float(volume.get("m5"))
    h1_volume = as_float(volume.get("h1"))
    h24_volume = as_float(volume.get("h24"))
    m5_buys = as_float((txns.get("m5") or {}).get("buys"))
    m5_sells = as_float((txns.get("m5") or {}).get("sells"))
    ch5 = as_float(change.get("m5"))
    ch1 = as_float(change.get("h1"))
    age = age_hours(pair)

    def reject(reason: str) -> None:
        if reject_reasons is not None:
            reject_reasons[reason] += 1

    if not address or price <= 0:
        reject("missing_price_or_mint")
        return None
    if (SNIPER_MODE or MOMENTUM_SWING_MODE) and entry_pair_reason(pair) is not None:
        reject("sniper_unsupported_or_illiquid_pair")
        return None

    # Hard safety/liquidity filters. Public DEX data is not a honeypot audit.
    if liquidity < MIN_LIQUIDITY_USD:
        reject("minimum_liquidity")
        return None
    if not MOMENTUM_SWING_MODE and m5_volume < MIN_VOLUME_5M_USD:
        reject("minimum_volume")
        return None
    if age >= 1e9 and (pair.get("pairCreatedAt") is None or MOMENTUM_SWING_MODE):
        reject("missing_pair_age" if pair.get("pairCreatedAt") is None else "invalid_pair_age")
        return None
    permitted_age = ((PAPER_REENTRY_MAX_POOL_AGE_MINUTES if allow_reentry_age else SNIPER_MAX_AGE_MINUTES) / 60
                     if SNIPER_MODE else (MOMENTUM_MAX_PAIR_AGE_HOURS or float("inf")) if MOMENTUM_SWING_MODE else MAX_PAIR_AGE_HOURS)
    if age > permitted_age:
        reject("too_old")
        return None
    if MOMENTUM_SWING_MODE and not SNIPER_MODE:
        if age * 60 < MOMENTUM_MIN_PAIR_AGE_MINUTES:
            reject("too_new_for_established_strategy")
            return None
        cap, cap_source = momentum_market_cap(pair)
        if cap is None:
            reject("market_cap_unavailable")
            return None
        if cap < MOMENTUM_MARKET_CAP_MIN_USD:
            reject("market_cap_below_range")
            return None
        if cap > MOMENTUM_MARKET_CAP_MAX_USD:
            reject("market_cap_above_range")
            return None
        trend_reason = momentum_trend_rejection(pair)
        if trend_reason:
            reject(trend_reason)
            return None
        # New $100K-$250K tier is PAPER-ONLY and requires materially more
        # liquidity and activity than the established strategy. Missing or
        # fully diluted proxy market cap never qualifies a sub-$500K token.
        if cap < SMALL_CAP_TIER_MAX_USD:
            if cap_source != "marketCap":
                reject("small_cap_requires_market_cap")
                return None
            if cap < 250000:
                if (liquidity < 35000 or m5_volume < 1800 or h1_volume < 25000
                        or m5_buys + m5_sells < 20 or m5_sells <= 0
                        or m5_buys / m5_sells < 1.35 or ch1 < 6
                        or not 0.75 <= ch5 <= 12):
                    reject("micro_cap_extra_safety")
                    return None
            elif (liquidity < 25000 or m5_volume < 1200 or h1_volume < 18000
                    or m5_buys + m5_sells < 12 or m5_sells <= 0
                    or m5_buys / m5_sells < 1.20 or ch1 < 4):
                reject("small_cap_extra_safety")
                return None
        # At 30-60 minutes even a larger cap has limited observed history.
        if 30 <= age * 60 < 60 and (liquidity < 25000 or m5_volume < 1500
                or h1_volume < 18000 or m5_buys + m5_sells < 16
                or m5_sells <= 0 or m5_buys / m5_sells < 1.25 or ch1 < 5):
            reject("young_pool_extra_safety")
            return None
        if PAPER_FAST_ONLY:
            quick = {"change_m5":ch5,"change_h1":ch1,"m5_volume":m5_volume,
                     "h1_volume":h1_volume,"liquidity":liquidity,
                     "m5_buys":m5_buys,"m5_sells":m5_sells}
            fast_ok, fast_reason, fast_rank = fast_mover_gate(quick)
            if not fast_ok:
                reject(fast_reason)
                return None
    if FAST_MOMENTUM_ENABLED or SNIPER_MODE or MOMENTUM_SWING_MODE:
        if age * 60 < MIN_PAIR_AGE_MINUTES:
            reject("too_new")
            return None
        if m5_volume < (SNIPER_MIN_VOLUME_5M_USD if SNIPER_MODE else MOMENTUM_MIN_VOLUME_5M_USD):
            reject("momentum_volume")
            return None
        if not (SNIPER_MIN_PRICE_CHANGE_5M_PCT if SNIPER_MODE else MOMENTUM_MIN_CHANGE_5M_PCT) <= ch5 <= (SNIPER_MAX_PRICE_CHANGE_5M_PCT if SNIPER_MODE else MOMENTUM_MAX_CHANGE_5M_PCT):
            reject("momentum_price_change")
            return None
        if m5_buys + m5_sells < (SNIPER_MIN_TXNS_5M if SNIPER_MODE else MOMENTUM_MIN_TXNS_5M):
            reject("momentum_transactions")
            return None
        if m5_sells <= 0 or m5_buys / m5_sells < (SNIPER_MIN_BUY_SELL_RATIO if SNIPER_MODE else MOMENTUM_MIN_BUY_SELL_RATIO):
            reject("momentum_buy_sell_ratio")
            return None
        if m5_volume / max(liquidity, 1) < MOMENTUM_MIN_VOLUME_LIQUIDITY_RATIO:
            reject("momentum_volume_liquidity_ratio")
            return None
    if SNIPER_MODE and liquidity < MIN_SOL_PAIR_LIQUIDITY_USD * SNIPER_MIN_ENTRY_LIQUIDITY_MULTIPLIER:
        reject("sniper_liquidity_buffer")
        return None
    if SNIPER_MODE:
        # Illustrative constant-product estimate, NOT a live swap quote.
        # Conservatively assume only half of reported USD pool liquidity is
        # quote-side depth. Never approve a candidate with unavailable depth.
        quote_depth_usd = liquidity / 2.0
        entry_impact_pct = 100.0 * POSITION_SIZE_USD / (quote_depth_usd + POSITION_SIZE_USD) if quote_depth_usd > 0 else float("inf")
        if entry_impact_pct > SNIPER_MAX_ENTRY_IMPACT_PCT:
            reject("sniper_entry_impact")
            return None
    else:
        entry_impact_pct = 0.0

    if m5_sells == 0 and m5_buys >= 8:
        # One-sided flow is often suspicious; avoid rather than assume it's bullish.
        reject("one_sided_flow")
        return None
    if m5_buys + m5_sells < 8:
        reject("insufficient_transactions")
        return None
    if ch5 > 80 or ch1 > 250:
        # Avoid chasing extreme vertical moves.
        reject("extreme_price_spike")
        return None
    if ch5 < -25 or ch1 < -45:
        reject("price_collapse")
        return None

    # Heuristic score, not a predictive model. Each component is capped.
    liquidity_score = min(25.0, liquidity / 2000.0)
    volume_score = min(20.0, (m5_volume / 1000.0) * 5.0)
    activity_score = min(15.0, (m5_buys + m5_sells) / 2.0)
    buy_pressure = m5_buys / max(1.0, m5_buys + m5_sells)
    flow_score = max(0.0, min(15.0, (buy_pressure - 0.45) * 50.0 + 7.5))
    momentum_score = 0.0
    if 0 < ch5 <= 15:
        momentum_score += 8
    elif -5 <= ch5 <= 0:
        momentum_score += 4
    if 0 < ch1 <= 35:
        momentum_score += 7
    elif -10 <= ch1 <= 0:
        momentum_score += 3
    age_score = (10.0 * max(0.0, 1.0 - age / max(SNIPER_MAX_AGE_MINUTES / 60, 0.001))
                 if SNIPER_MODE else
                 (10.0 if 0.25 <= age <= 24 else (6.0 if age <= 72 else 2.0)))
    score = min(100.0, liquidity_score + volume_score + activity_score +
                flow_score + momentum_score + age_score)
    if MOMENTUM_SWING_MODE and not SNIPER_MODE:
        # Positive 6h/24h alignment boosts established tokens without
        # penalizing them merely for being days or months old.
        age_score = 10.0
        score = min(100.0, liquidity_score + volume_score + activity_score + flow_score +
                    min(15.0, 5.0 + max(0.0, ch5) / 3.0 + max(0.0, ch1) / 8.0) + age_score)

    return {
        "address": address,
        "symbol": str(token.get("symbol") or "???")[:20],
        "name": str(token.get("name") or "Unknown")[:50],
        "price": price,
        "liquidity": liquidity,
        "m5_volume": m5_volume,
        "h1_volume": h1_volume,
        "h24_volume": h24_volume,
        "m5_buys": int(m5_buys),
        "m5_sells": int(m5_sells),
        "change_m5": ch5,
        "change_h1": ch1,
        "change_h6": as_float(change.get("h6")),
        "change_h24": as_float(change.get("h24")),
        "market_cap_usd": momentum_market_cap(pair)[0] if MOMENTUM_SWING_MODE and not SNIPER_MODE else as_float(pair.get("marketCap")),
        "market_cap_source": momentum_market_cap(pair)[1] if MOMENTUM_SWING_MODE and not SNIPER_MODE else "marketCap",
        "age_hours": age,
        "score": score,
        "estimated_entry_impact_pct": entry_impact_pct,
        "url": pair.get("url", ""),
        "pool_address": pair.get("pairAddress", ""),
        "pool_dex": (pair.get("dexId") or "").lower(),
        "fast_mover_rank": (fast_rank if MOMENTUM_SWING_MODE and PAPER_FAST_ONLY else 0.0),
    }


def equity(state: dict, current_prices: dict[str, float] | None = None) -> float:
    current_prices = current_prices or {}
    value = as_float(state["cash_usd"])
    for mint, raw in state["positions"].items():
        pos = Position(**raw)
        issue = state.get("exit_issues", {}).get(mint, {})
        if issue.get("status") == "IMPAIRED_EXIT_UNAVAILABLE":
            # Conservative risk-value, NOT a realized sale or proof of zero recoverability.
            continue
        p = current_prices.get(mint, pos.last_price)
        value += pos.token_units * p
    return value


def roll_day_if_needed(state: dict) -> None:
    today = str(date.today())
    if state.get("day") != today:
        state["day"] = today
        state["day_start_equity"] = equity(state)
        # Unresolved safety events survive day rollover and deploys.
        state["halted"] = bool(state.get("exit_issues")) or bool(state.get("halted"))
        log.info("New day. Daily equity baseline reset to $%.2f", state["day_start_equity"])


def should_halt(state: dict) -> bool:
    baseline = as_float(state.get("day_start_equity"), STARTING_CAPITAL)
    current = equity(state)
    loss_fraction = (baseline - current) / max(1.0, baseline)
    if loss_fraction >= MAX_DAILY_LOSS_FRACTION:
        state["daily_loss_halt"] = True
        state["halted"] = True
        log.error("DAILY LOSS LIMIT HIT: %.2f%%. New entries halted.", loss_fraction * 100)
    return bool(state.get("halted"))


def paper_prebuy_sellability(candidate: dict, pairs: list[dict] | None = None) -> tuple[bool, str]:
    """Fresh read-only same-pool sell *estimate*, not executable swap simulation.

    Reject if exact pair, orientation, route, price, reserves, or hypothetical
    full-position liquidation depth cannot be verified. Fail closed on API errors.
    """
    mint = candidate.get("address", "")
    pool = candidate.get("pool_address", "")
    if not mint or not pool:
        return False, "missing_pinned_entry_pool"
    try:
        observed = token_pairs(mint) if pairs is None else pairs
    except Exception:
        return False, "pool_recheck_unavailable"
    matching = [p for p in observed if p.get("pairAddress") == pool
                and p.get("chainId") == "solana"
                and (p.get("baseToken") or {}).get("address") == mint
                and (p.get("quoteToken") or {}).get("address") == SOL_MINT
                and (p.get("dexId") or "").lower() in active_entry_routes()]
    if not matching:
        return False, "entry_pool_missing_or_unsupported"
    pair = matching[0]
    observed_price = as_float(pair.get("priceUsd"))
    depth = as_float((pair.get("liquidity") or {}).get("usd"))
    using_secondary_depth = candidate.get("liquidity_data_source") == "geckoterminal_indicative"
    if using_secondary_depth:
        if (pair.get("liquidity") or {}).get("usd") is not None:
            # Real primary data has returned; use the primary number exclusively.
            using_secondary_depth = False
        else:
            recovered, outcome = independently_verify_missing_liquidity(pair)
            if recovered is None:
                return False, "secondary_liquidity_recheck_" + outcome
            depth = as_float(recovered["liquidity"]["usd"])
    size = POSITION_SIZE_USD
    if observed_price <= 0 or depth < active_entry_liquidity_min() * (SNIPER_MIN_ENTRY_LIQUIDITY_MULTIPLIER if SNIPER_MODE else 1):
        return False, "entry_pool_price_or_liquidity_unverified"
    candidate_price = as_float(candidate.get("price"))
    if candidate_price <= 0 or abs(observed_price / candidate_price - 1) > 0.10:
        return False, "entry_pool_price_changed"
    # Hypothetical full-position liquidation after modeled entry cost.
    position = Position(token=mint, symbol=str(candidate.get("symbol", "?")),
                        name=str(candidate.get("name", "?")), entry_price=observed_price * (1 + ESTIMATED_ROUND_TRIP_COST / 2),
                        amount_usd=size, token_units=size / (observed_price * (1 + ESTIMATED_ROUND_TRIP_COST / 2)),
                        opened_at=time.time(), peak_price=observed_price, last_price=observed_price,
                        score=as_float(candidate.get("score")))
    quote = paper_exit_quote(position, observed_price, depth)
    if quote is None or quote["impact_pct"] > SNIPER_MAX_ENTRY_IMPACT_PCT:
        return False, "same_pool_sell_impact_exceeds_limit"
    # Injectable pairs make offline unit tests deterministic; actual candidates
    # require a separate fresh data provider, never another token's pool.
    if pairs is None and PAPER_PREBUY_INDEPENDENT_POOL_CHECK and not using_secondary_depth:
        alternate = held_pool_fallback(pool, mint)
        if alternate is None:
            return False, "independent_same_pool_unavailable"
        independent_liq = as_float(alternate.get("liquidity"))
        independent_price = as_float(alternate.get("price"))
        if independent_liq < depth * PAPER_PREBUY_MIN_CROSSCHECK_LIQUIDITY_RATIO or independent_liq < active_entry_liquidity_min():
            return False, "independent_same_pool_liquidity_disagreement"
        if independent_price <= 0 or abs(independent_price / observed_price - 1) * 100 > PAPER_PREBUY_MAX_CROSSCHECK_PRICE_DEVIATION_PCT:
            return False, "independent_same_pool_price_disagreement"
        log.info("PAPER ENTRY POOL VERIFIED mint=%s pool=%s primary_liq=$%.0f secondary_liq=$%.0f primary_price=%.8g secondary_price=%.8g independent=indicative_only",
                 mint, pool, depth, independent_liq, observed_price, independent_price)
    if using_secondary_depth:
        log.warning("PAPER ENTRY SECONDARY DEPTH mint=%s pool=%s secondary_liq=$%.0f "
                    "primary_depth=missing same_pool_sol_quote_verified=True estimate_only=True", mint, pool, depth)
    # v5: save the actual fresh same-pool provider response. This is only a
    # LOCAL receipt time; it does not imply that DexScreener data is real-time.
    candidate["_prebuy_verified_pair"] = pair
    candidate["_prebuy_verified_at"] = time.time()
    return True, "indicative_same_pool_sell_depth_verified"


def paper_reentry_decision(state: dict, mint: str, *, now: float | None = None) -> tuple[bool, str, bool]:
    """A previously traded mint can return after a safe, bounded cooldown.

    Uses saved closed-trade timestamps (UTC), so a redeploy cannot clear the
    reversal cooldown, loss budget, or hourly reentry cap. Missing/malformed
    historical timestamps fail closed for a previously traded token.
    """
    if mint in state.get("positions", {}):
        return False, "already_held", False
    if mint in state.get("rugged_mints", []) or any(
        t.get("token") == mint and t.get("execution") == "paper_writeoff_no_sale"
        for t in state.get("closed_trades", [])):
        return False, "rug_writeoff_blacklisted", True
    traded = mint in state.get("seen_tokens", []) or any(
        t.get("token") == mint for t in state.get("closed_trades", []))
    if not traded:
        return True, "first_entry", False
    if not PAPER_REENTRY_ENABLED:
        return False, "reentry_disabled", True
    if state.get("exit_issues", {}).get(mint):
        return False, "unresolved_exit_issue", True
    trades = [t for t in state.get("closed_trades", []) if t.get("token") == mint]
    if not trades:
        return False, "legacy_trade_no_exit_history", True
    stamp = time.time() if now is None else now
    timed: list[tuple[float, dict]] = []
    for trade in trades:
        try:
            dt = datetime.fromisoformat(str(trade["closed_at"]).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return False, "exit_timestamp_unverified", True
            closed = dt.timestamp()
        except (TypeError, ValueError, KeyError, OverflowError):
            return False, "exit_timestamp_unverified", True
        if closed > stamp + 30:
            return False, "exit_timestamp_in_future", True
        timed.append((closed, trade))
    if not timed:
        return False, "missing_exit_history", True
    last_closed, last_trade = max(timed, key=lambda item: item[0])
    reason = str(last_trade.get("reason") or "")
    # v6/v7.6: do not churn back into a coin shortly after a protective paper sale
    # or a substantial realized loss, even if momentum briefly recovers.
    last_loss_pct = as_float(last_trade.get("pnl_pct"))
    cooldown = (PAPER_SUSTAINED_LOSS_REENTRY_COOLDOWN_SECONDS if reason == "confirmed_sustained_loss" else
                PAPER_IMPAIRED_REENTRY_COOLDOWN_SECONDS if "liquidity" in reason or "impair" in reason or "sellability" in reason else
                max(PAPER_REVERSAL_REENTRY_COOLDOWN_SECONDS, PAPER_LOSS_REENTRY_COOLDOWN_SECONDS)
                if "reversal" in reason else
                PAPER_LOSS_REENTRY_COOLDOWN_SECONDS if last_loss_pct <= -PAPER_LOSS_REENTRY_NET_PCT else
                max(PAPER_REENTRY_COOLDOWN_SECONDS, 900) if last_loss_pct < 0 else
                PAPER_REENTRY_COOLDOWN_SECONDS)
    if stamp - last_closed < cooldown:
        return False, f"reentry_cooldown:{max(1, int(cooldown-(stamp-last_closed)))}s:last={reason}", True
    recent = [t for ts, t in timed if 0 <= stamp - ts < PAPER_REENTRY_LOOKBACK_SECONDS]
    reentries = sum(bool(t.get("paper_metrics", {}).get("is_reentry")) for t in recent)
    if reentries >= PAPER_REENTRY_MAX_PER_WINDOW:
        return False, "reentry_hourly_limit", True
    realized_losses = sum(max(0.0, -as_float(t.get("pnl_usd"))) for t in recent)
    if realized_losses >= PAPER_REENTRY_MAX_LOSS_USD:
        return False, f"reentry_loss_budget:${realized_losses:.2f}", True
    return True, "reentry_approved", True


def paper_reentry_watch_mints(state: dict, *, now: float | None = None) -> list[str]:
    """Include eligible recent exits in stage 2 even after discovery rotation.

    Rechecks are never buys: each must still pass fresh SOL-pool route, real
    liquidity data, 5m momentum, score, independent sellability and two prices.
    """
    if not PAPER_REENTRY_ENABLED or not PAPER_REENTRY_SCAN_QUOTA:
        return []
    stamp = time.time() if now is None else now
    seen: set[str] = set()
    selected: list[str] = []
    for trade in reversed(state.get("closed_trades", [])):
        mint = trade.get("token")
        if not isinstance(mint, str) or mint in seen or mint in state.get("positions", {}):
            continue
        seen.add(mint)
        # An old scanner rejection is not an exit cooldown. On reentry we
        # *always* fetch fresh market data and re-run every hard gate; a stale
        # structural cooldown must not silently extend a 5-minute exit wait.
        try:
            closed_at = datetime.fromisoformat(str(trade["closed_at"]).replace("Z", "+00:00")).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        if stamp - closed_at > PAPER_REENTRY_MAX_POOL_AGE_MINUTES * 60 or stamp < closed_at:
            continue
        allowed, _, is_reentry = paper_reentry_decision(state, mint, now=stamp)
        if allowed and is_reentry:
            selected.append(mint)
            log.info("PAPER REENTRY DUE mint=%s source=closed_trade fresh_market_check=required", mint)
        if len(selected) >= PAPER_REENTRY_SCAN_QUOTA:
            break
    return selected


def paper_entry_profile(state: dict, score: float) -> tuple[str | None, str]:
    """Standard scoring unchanged; experimental paper candidates use a capped cohort."""
    if score >= MIN_SCORE:
        return "standard", "approved"
    if not PAPER_MODE or not PAPER_EXPLORATION_ENABLED or score < PAPER_EXPLORATION_MIN_SCORE:
        return None, "low_score"
    if state.get("paper_exploration_day") != str(date.today()):
        # Previous day's counter expires but actual open positions remain tracked.
        state["paper_exploration_day"] = str(date.today())
        state["paper_exploration_fills_today"] = 0
    if state.get("paper_exploration_fills_today", 0) >= PAPER_EXPLORATION_DAILY_LIMIT:
        return None, "paper_exploration_daily_limit"
    for mint in state.get("positions", {}):
        if state.get("paper_trade_metrics", {}).get(mint, {}).get("entry_profile") == "exploration":
            return None, "paper_exploration_position_limit"
    return "exploration", "approved"


def paper_buy(state: dict, c: dict, *, size_usd: float | None = None, entry_profile: str = "standard") -> None:
    if c["address"] in state["positions"]:
        return
    if c["address"] in state.get("rugged_mints", []) or any(
        t.get("token") == c["address"] and t.get("execution") == "paper_writeoff_no_sale"
        for t in state.get("closed_trades", [])):
        log.warning("PAPER ENTRY BLOCKED mint=%s reason=rug_writeoff_blacklisted", c["address"])
        return
    if len(state["positions"]) >= MAX_POSITIONS:
        return
    # Fixed position allocation, capped by available cash. This is position size,
    # not the amount guaranteed to be lost; stop-losses can slip in volatile markets.
    size = min(state["cash_usd"], POSITION_SIZE_USD if size_usd is None else max(0., size_usd))
    if size < 5:
        return
    # Conservative paper entry haircut for spread/impact. Live costs can be worse.
    entry = c["price"] * (1 + ESTIMATED_ROUND_TRIP_COST / 2)
    units = size / entry
    state["cash_usd"] -= size
    state["positions"][c["address"]] = asdict(Position(
        token=c["address"], symbol=c["symbol"], name=c["name"],
        entry_price=entry, amount_usd=size, token_units=units,
        opened_at=time.time(), peak_price=entry, last_price=entry, score=c["score"],
        last_price_at=time.time(),
        entry_pool_address=c.get("pool_address", ""),
        entry_pool_dex=c.get("pool_dex", "")
    ))
    is_reentry = c["address"] in state.get("seen_tokens", []) or any(
        t.get("token") == c["address"] for t in state.get("closed_trades", []))
    state.setdefault("paper_trade_metrics", {})[c["address"]] = {
        "is_reentry": is_reentry,
        "discovered_at": _first_discovered_at.get(c["address"]),
        "entry_delay_seconds": (max(0.0, time.time() - _first_discovered_at[c["address"]])
                                if c["address"] in _first_discovered_at else None),
        "entry_score": c.get("score"), "entry_profile": entry_profile,
        "entry_5m_change_pct": c.get("change_m5"),
        "fast_mover_rank": c.get("fast_mover_rank", 0),
        "take_profit_net_pct": (PAPER_FAST_TP_NET_PCT if PAPER_FAST_ONLY and c.get("fast_mover_rank",0)>0 else TAKE_PROFIT*100),
        "primary_net_observations": [],
        "strategy": "established_momentum" if MOMENTUM_SWING_MODE else "fresh_sniper" if SNIPER_MODE else "legacy",
        "entry_market_price": c["price"],  # Before the modeled buy haircut.
        "entry_market_cap_usd": c.get("market_cap_usd"),
        "entry_age_minutes": c.get("age_hours", 0)*60,
        "entry_market_cap_source": c.get("market_cap_source"),
        "entry_1h_change_pct": c.get("change_h1"),
        "entry_6h_change_pct": c.get("change_h6"),
        "entry_24h_change_pct": c.get("change_h24"),
        "entry_buy_pressure": c.get("m5_buys", 0) / max(1, c.get("m5_buys", 0) + c.get("m5_sells", 0)),
        "entry_5m_volume_usd": c.get("m5_volume"),
        "max_favorable_net_pct": None, "max_adverse_net_pct": None,
        "hit_take_profit_observed": False, "seconds_to_first_take_profit_observed": None, "observed_points": {},
        "last_net_pct": None, "fallback_only_observations": 0, "unverified_exit_observations": 0,
        "last_quote_source": "", "estimated_round_trip_cost_pct": ESTIMATED_ROUND_TRIP_COST * 100,
        "recent_primary_prices": [[time.time(), c["price"]]], "reversal_exit_attempts": 0,
        "shadow_c": {"status": "open", "tp_pct": SHADOW_STRATEGY_C_TP * 100,
                     "sl_pct": SHADOW_STRATEGY_C_SL * 100, "exit_reason": None,
                     "exit_net_pct": None, "exit_elapsed_seconds": None},
    }
    log.info("PAPER ENTRY METRICS %s discovery_to_entry_s=%s fee_haircut_est_pct=%.2f",
             c["symbol"], state["paper_trade_metrics"][c["address"]]["entry_delay_seconds"], ESTIMATED_ROUND_TRIP_COST * 100)
    state["seen_tokens"] = (state.get("seen_tokens", []) + [c["address"]])[-2000:]
    log.info("PAPER BUY %s ($%.2f) score=%.1f price=%g", c["symbol"], size, c["score"], entry)


def paper_exit_quote(pos: Position, price: float, liquidity_usd: float) -> dict | None:
    """Conservative indicative proceeds, NOT a DEX executable quote.

    A constant-product-style impact approximation assumes half of reported
    pool USD liquidity is quote-side depth. Reject unknown/insufficient depth.
    """
    if price <= 0 or liquidity_usd <= 0 or pos.token_units <= 0:
        return None
    gross = pos.token_units * price
    quote_depth = liquidity_usd * PAPER_EXIT_LIQUIDITY_FRACTION
    if quote_depth <= 0:
        return None
    impact = gross / (quote_depth + gross)
    if impact > PAPER_MAX_EXIT_IMPACT:
        return None
    cost = max(0.0, min(0.99, ESTIMATED_ROUND_TRIP_COST / 2))
    proceeds = gross * (1 - impact) * (1 - cost)
    return {"proceeds_usd": proceeds, "impact_pct": impact * 100,
            "exit_price": proceeds / pos.token_units, "liquidity_usd": liquidity_usd}


def paper_exit_failure_reason(pos: Position, price: float, liquidity_usd: float, quote_source: str = "") -> str | None:
    """Explain why an indicative exit can't be validated; never fabricate a fill."""
    if quote_source == "geckoterminal_indicative" and PAPER_EXIT_REQUIRE_PRIMARY_QUOTE:
        return "primary_pool_quote_missing_fallback_valuation_only"
    if price <= 0:
        return "missing_exit_price"
    if liquidity_usd <= 0:
        return "missing_exit_liquidity"
    if pos.token_units <= 0:
        return "invalid_position_units"
    gross = pos.token_units * price
    depth = liquidity_usd * PAPER_EXIT_LIQUIDITY_FRACTION
    if depth <= 0:
        return "missing_quote_side_depth"
    impact = gross / (depth + gross)
    if impact > PAPER_MAX_EXIT_IMPACT:
        return "excessive_estimated_exit_impact"
    return None


def note_unavailable_exit(state: dict, mint: str, symbol: str, reason: str) -> None:
    """Persist an unresolved PAPER exit and rate-limit errors per mint.

    This is NOT a sell, realized loss, or executable quote. The original
    position and invested cash remain unchanged and new entries stay halted.
    """
    now = time.time()
    issues = state.setdefault("exit_issues", {})
    if "halted_before_exit_issues" not in state:
        # Older persisted state with an already-unresolved exit has no halt
        # provenance: it was halted by the old exit handler. Daily-loss checks
        # still independently prohibit a resume.
        state["halted_before_exit_issues"] = bool(state.get("halted")) and not bool(issues)
    item = issues.get(mint)
    if not isinstance(item, dict):
        item = {"status": "EXIT_UNAVAILABLE", "first_seen_at": now,
                "last_logged_at": 0.0, "checks": 0}
        issues[mint] = item
    age = now - float(item.get("first_seen_at", now))
    next_checks = int(item.get("checks", 0)) + 1
    if age >= PAPER_IMPAIRED_AFTER_SECONDS and next_checks >= PAPER_IMPAIRED_AFTER_CHECKS:
        if item.get("status") != "IMPAIRED_EXIT_UNAVAILABLE":
            log.warning("PAPER POSITION IMPAIRED %s mint=%s cost_retained_not_realized=$%.2f conservative_value=$0; exit still monitored", symbol, mint,
                        as_float(state.get("positions", {}).get(mint, {}).get("amount_usd")))
        item["status"] = "IMPAIRED_EXIT_UNAVAILABLE"
    else:
        item["status"] = "EXIT_UNAVAILABLE"
    item["reason"] = reason
    item["last_checked_at"] = now
    item["checks"] = int(item.get("checks", 0)) + 1
    state["halted"] = True
    last_log = float(item.get("last_logged_at", 0))
    if last_log <= 0 or now - last_log >= PAPER_EXIT_WARNING_INTERVAL_SECONDS:
        log.error("PAPER EXIT UNAVAILABLE %s mint=%s reason=%s checks=%d; "
                  "position retained, no sale assumed, new entries halted; rechecking every cycle",
                  symbol, mint, reason, item["checks"])
        item["last_logged_at"] = now


def pinned_pool_liquidity_evidence(pos: Position, pairs: list[dict], *, primary_healthy: bool) -> dict:
    """Interpret a *successful fresh* pool response, not a cached price.

    Missing original pool / explicit zero liquidity is evidence of a missing
    exit, but a usable alternative SOL pair rules out automatic write-off.
    A provider timeout, unsupported schema, or an excessive-impact quote by
    itself is never sufficient to declare the tokens worthless.
    """
    if not primary_healthy or not pos.entry_pool_address:
        return {"verified": False, "reason": "pool_status_unverified"}
    supported = [p for p in pairs if p.get("chainId") == "solana"
                 and (p.get("baseToken") or {}).get("address") == pos.token
                 and (p.get("quoteToken") or {}).get("address") == SOL_MINT
                 and (p.get("dexId") or "").lower() in active_entry_routes()]
    # Do not write off tokens when even an alternative known SOL pool can
    # support an ordinary paper-size exit; independently re-validate that pool
    # before any future simulated sale (not in this function).
    alt_viable = any(p.get("pairAddress") != pos.entry_pool_address
                     and paper_exit_quote(pos, as_float(p.get("priceUsd")),
                                          as_float((p.get("liquidity") or {}).get("usd"))) is not None
                     for p in supported)
    pinned = next((p for p in supported if p.get("pairAddress") == pos.entry_pool_address), None)
    if alt_viable:
        return {"verified": False, "reason": "alternative_liquid_sol_pool_found"}
    if pinned is None:
        return {"verified": True, "reason": "pinned_pool_missing_no_liquid_alternative"}
    liq_field = (pinned.get("liquidity") or {}).get("usd")
    if liq_field is not None and as_float(liq_field) <= PAPER_RUG_DUST_LIQUIDITY_USD:
        reason = ("pinned_pool_zero_liquidity" if as_float(liq_field) <= 0 else
                  "pinned_pool_near_zero_liquidity")
        return {"verified": True, "reason": reason}
    return {"verified": False, "reason": "pinned_pool_nonzero_or_liquidity_unverified"}


def release_halt_after_rug_writeoff(state: dict) -> None:
    """Unblock only an exit-related halt, never daily-loss or manual halts."""
    if state.get("exit_issues"):
        return
    prior_halt = bool(state.pop("halted_before_exit_issues", False))
    baseline = as_float(state.get("day_start_equity"), STARTING_CAPITAL)
    hit_daily_limit = ((baseline - equity(state)) / max(1.0, baseline)
                       >= MAX_DAILY_LOSS_FRACTION)
    if prior_halt or state.get("daily_loss_halt") or state.get("exit_recovery_review_required") or hit_daily_limit:
        if hit_daily_limit:
            state["daily_loss_halt"] = True
        state["halted"] = True
        log.warning("PAPER RUG WRITE-OFF closed but entry halt retained due to another risk/control")
    else:
        state["halted"] = False
        log.info("PAPER ENTRY HALT CLEARED after final rug write-off; daily-loss rules still apply")


def write_off_rugged_paper_position(state: dict, mint: str, evidence: dict) -> bool:
    """Record a zero-recovery paper accounting loss; NEVER simulate a sell."""
    if not PAPER_MODE or mint not in state.get("positions", {}):
        return False
    pos = Position(**state["positions"][mint])
    issue = state.get("exit_issues", {}).get(mint, {}).copy()
    metrics = state.setdefault("paper_trade_metrics", {}).pop(mint, {})
    metrics["last_net_pct"] = -100.0
    metrics["max_adverse_net_pct"] = min(-100.0, as_float(metrics.get("max_adverse_net_pct"), 0))
    metrics["unrecoverable_exit_issue"] = issue
    shadow = metrics.setdefault("shadow_c", {})
    if shadow.get("status") == "open":
        shadow.update(status="closed_at_paper_writeoff", exit_reason="rug_writeoff_no_sale",
                      exit_net_pct=-100.0,
                      exit_elapsed_seconds=round(max(0, time.time() - pos.opened_at), 2))
    state.setdefault("closed_trades", []).append({
        "closed_at": now_iso(), "token": mint, "symbol": pos.symbol,
        "entry_price": pos.entry_price, "exit_price": 0.0,
        "invested_usd": pos.amount_usd, "proceeds_usd": 0.0,
        "pnl_usd": -pos.amount_usd, "pnl_pct": -100.0,
        "reason": "rug_writeoff_no_sellable_liquidity",
        "paper_metrics": metrics, "estimated_impact_pct": None,
        "pool_liquidity_usd": None, "quote_source": "no_verified_exit",
        "execution": "paper_writeoff_no_sale", "status": "closed_writeoff",
        "loss_recognition": "conservative_paper_zero_recovery_assumption",
        "rug_evidence": evidence, "entry_pool_address": pos.entry_pool_address,
    })
    state["closed_trades"] = state["closed_trades"][-1000:]
    rugged = state.setdefault("rugged_mints", [])
    if mint not in rugged:
        rugged.append(mint)
    del state["positions"][mint]
    state.setdefault("exit_issues", {}).pop(mint, None)
    _candidate_price_history.pop(mint, None)
    _pending_entry_mints.pop(mint, None)
    log.error("PAPER RUG WRITE-OFF %s mint=%s pool=%s lost=$%.2f pnl=-100%% recovered=$0 "
              "execution=paper_writeoff_no_sale evidence=%s reentry=blacklisted",
              pos.symbol, mint, pos.entry_pool_address, pos.amount_usd, evidence)
    release_halt_after_rug_writeoff(state)
    return True


def maybe_write_off_rugged_position(state: dict, mint: str, evidence: dict | None) -> bool:
    if not PAPER_RUG_WRITEOFF_ENABLED or not evidence or not evidence.get("verified"):
        return False
    issue = state.get("exit_issues", {}).get(mint)
    if not issue or mint not in state.get("positions", {}):
        return False
    # Only successful, *independent in time* healthy primary observations count.
    now = time.time()
    prev = float(issue.get("last_rug_confirmed_at", -1))
    if now - prev >= 1.0:
        issue["rug_confirmed_checks"] = int(issue.get("rug_confirmed_checks", 0)) + 1
        issue["last_rug_confirmed_at"] = now
    age = max(0.0, now - float(issue.get("first_seen_at", now)))
    checks = int(issue.get("rug_confirmed_checks", 0))
    if age >= PAPER_RUG_WRITEOFF_AFTER_SECONDS and checks >= PAPER_RUG_WRITEOFF_MIN_CHECKS:
        return write_off_rugged_paper_position(state, mint, {
            "reason": evidence.get("reason"), "unavailable_seconds": round(age, 1),
            "healthy_pool_checks": checks, "original_failure": issue.get("reason"),
            "note": "primary pool not sellable; no viable alternate indexed SOL route; permanence not provable",
        })
    if checks <= PAPER_RUG_WRITEOFF_MIN_CHECKS:
        log.info("PAPER RUG WATCH mint=%s primary_evidence=%s age=%.0fs confirmed_checks=%d/%d writeoff_after=%ds",
                 mint, evidence.get("reason"), age, checks, PAPER_RUG_WRITEOFF_MIN_CHECKS,
                 PAPER_RUG_WRITEOFF_AFTER_SECONDS)
    return False


def clear_exit_issue(state: dict, mint: str, symbol: str) -> None:
    """Clear a recovered quote alert; never automatically remove safety halt."""
    item = state.get("exit_issues", {}).pop(mint, None)
    if item:
        state["exit_recovery_review_required"] = True
        log.info("PAPER EXIT QUOTE RECOVERED %s mint=%s; previous unresolved status cleared; "
                 "safety halt remains until operator review", symbol, mint)


def close_position(state: dict, mint: str, price: float, reason: str, liquidity_usd: float | None = None, quote_source: str = "") -> bool:
    raw = state["positions"].get(mint)
    if not raw:
        return False
    pos = Position(**raw)
    failure = paper_exit_failure_reason(pos, price, liquidity_usd or 0, quote_source)
    quote = None if failure else paper_exit_quote(pos, price, liquidity_usd or 0)
    if quote is None:
        note_unavailable_exit(state, mint, pos.symbol, failure or "exit_quote_unavailable")
        return False
    clear_exit_issue(state, mint, pos.symbol)
    exit_price = quote["exit_price"]
    proceeds = quote["proceeds_usd"]
    pnl = proceeds - pos.amount_usd
    state["cash_usd"] += proceeds
    metrics = state.setdefault("paper_trade_metrics", {}).pop(mint, {})
    if metrics.get("max_favorable_net_pct") is not None:
        metrics["hit_take_profit_observed"] = bool(metrics.get("hit_take_profit_observed") or metrics["max_favorable_net_pct"] >= as_float(metrics.get("take_profit_net_pct"), TAKE_PROFIT*100))
    shadow = metrics.setdefault("shadow_c", {})
    if shadow.get("status") == "open":
        shadow.update(status="closed_at_actual_exit", exit_reason="actual_trade_closed_first",
                      exit_net_pct=round(pnl / max(pos.amount_usd, 1e-15) * 100, 3),
                      exit_elapsed_seconds=round(max(0, time.time()-pos.opened_at), 2))
    log.info("PAPER SHADOW C %s status=%s reason=%s net_pct=%s seconds=%s [paper_estimate_only]",
             pos.symbol, shadow.get("status"), shadow.get("exit_reason"), shadow.get("exit_net_pct"), shadow.get("exit_elapsed_seconds"))
    log.info("PAPER TRADE METRICS %s entry_delay_s=%s mfe_net_pct=%s mae_net_pct=%s hit_tp_observed=%s seconds_to_tp=%s points=%s round_trip_cost_assumption_pct=%.2f exit_impact_est_pct=%.3f",
             pos.symbol, metrics.get("entry_delay_seconds"), metrics.get("max_favorable_net_pct"),
             metrics.get("max_adverse_net_pct"), metrics.get("hit_take_profit_observed"),
             metrics.get("seconds_to_first_take_profit_observed"), metrics.get("observed_points", {}), ESTIMATED_ROUND_TRIP_COST * 100, quote["impact_pct"])
    state["closed_trades"].append({
        "closed_at": now_iso(), "token": mint, "symbol": pos.symbol,
        "entry_price": pos.entry_price, "exit_price": exit_price,
        "invested_usd": pos.amount_usd, "proceeds_usd": proceeds,
        "pnl_usd": pnl, "pnl_pct": pnl / max(1.0, pos.amount_usd) * 100,
        "reason": reason, "paper_metrics": metrics, "estimated_impact_pct": quote["impact_pct"],
        "pool_liquidity_usd": quote["liquidity_usd"], "quote_source": quote_source or "primary_pool_indicative", "execution": "paper_estimate_only"
    })
    state["closed_trades"] = state["closed_trades"][-1000:]
    if PAPER_MODE:
        report = paper_trade_scorecard(state["closed_trades"])
        log.info("PAPER PERFORMANCE last_%d_trades=%d wins=%d win_rate_pct=%s net_pnl_usd=%.2f median_net_pct=%s indicative_only=True",
                 30, report["trades"], report["wins"], report["win_rate_pct"],
                 report["net_pnl_usd"], report["median_return_pct"])
    del state["positions"][mint]
    # Price observations from the prior holding must never confirm a new buy.
    _candidate_price_history.pop(mint, None)
    _pending_entry_mints.pop(mint, None)
    log.info("PAPER SELL %s reason=%s PnL=$%.2f (%.1f%%) impact≈%.2f%% [estimate only]", pos.symbol, reason, pnl,
             pnl / max(1.0, pos.amount_usd) * 100, quote["impact_pct"])
    return True


_pool_fallback_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}


def held_pool_fallback(pool_address: str, mint: str = "") -> dict | None:
    """Secondary, pool-pinned read-only observation. Never infer liquidity.

    GeckoTerminal reserve_in_usd and base_token_price_usd can be stale or
    inaccurate. This is an indicative fallback for PAPER estimates only.
    """
    if not pool_address or not (32 <= len(pool_address) <= 64):
        return None
    now = time.monotonic()
    key = (pool_address, mint)
    old = _pool_fallback_cache.get(key)
    if old and now < old[0]:
        return old[1]
    outcome = None
    try:
        response = session.get(
            f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool_address}",
            timeout=8, headers={"Accept": "application/json;version=20230203"})
        response.raise_for_status()
        pool_data = response.json().get("data") or {}
        base_id = (((pool_data.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
        if mint and base_id != "solana_" + mint:
            raise ValueError("fallback pool base token does not match held mint")
        attributes = pool_data.get("attributes") or {}
        price = as_float(attributes.get("base_token_price_usd"))
        liquidity = as_float(attributes.get("reserve_in_usd"))
        if price > 0 and liquidity > 0:
            outcome = {"price": price, "liquidity": liquidity,
                       "pool_address": pool_address, "quote_source": "geckoterminal_indicative"}
    except Exception as exc:
        log.debug("Secondary pool lookup failed pool=%s: %s", pool_address, type(exc).__name__)
    _pool_fallback_cache[key] = (now + PAPER_POOL_FALLBACK_SECONDS, outcome)
    if len(_pool_fallback_cache) > 500:
        for key in list(_pool_fallback_cache)[:100]:
            _pool_fallback_cache.pop(key, None)
    return outcome


def manage_positions(state: dict, market: dict[str, dict],
                     exit_evidence: dict[str, dict] | None = None) -> None:
    exit_evidence = exit_evidence or {}
    for mint in list(state["positions"].keys()):
        raw = state["positions"][mint]
        pos = Position(**raw)
        c = market.get(mint)
        if not c or as_float(c.get("price")) <= 0:
            # Never invent a price. Halt new entries if a held position has no
            # usable quote for too long; its mark-to-market value is uncertain.
            last_valid = pos.last_price_at or pos.opened_at
            stale_for = max(0.0, time.time() - last_valid)
            if stale_for >= MAX_STALE_PRICE_SECONDS:
                note_unavailable_exit(state, mint, pos.symbol, "stale_or_missing_price")
                maybe_write_off_rugged_position(state, mint, exit_evidence.get(mint))
            continue
        price = c["price"]
        pos.last_price = price
        pos.last_price_at = time.time()
        pos.peak_price = max(pos.peak_price, price)
        raw.update(asdict(pos))
        # Evaluate TP/SL against estimated net proceeds, including the paper
        # execution haircut, rather than the raw market-price change alone.
        liquidity = as_float(c.get("liquidity"))
        source = str(c.get("quote_source", "dexscreener"))
        metrics = state.setdefault("paper_trade_metrics", {}).setdefault(mint, {})
        metrics["last_quote_source"] = source
        if source == "geckoterminal_indicative":
            metrics["fallback_only_observations"] = int(metrics.get("fallback_only_observations", 0)) + 1
        failure = paper_exit_failure_reason(pos, price, liquidity, source)
        quote = None if failure else paper_exit_quote(pos, price, liquidity)
        if quote is None:
            metrics["unverified_exit_observations"] = int(metrics.get("unverified_exit_observations", 0)) + 1
            note_unavailable_exit(state, mint, pos.symbol, failure or "exit_quote_unavailable")
            maybe_write_off_rugged_position(state, mint, exit_evidence.get(mint))
            continue
        clear_exit_issue(state, mint, pos.symbol)
        net_return = quote["proceeds_usd"] / max(pos.amount_usd, 1e-15) - 1
        metrics = state.setdefault("paper_trade_metrics", {}).setdefault(mint, {})
        net_pct = net_return * 100
        old_max = metrics.get("max_favorable_net_pct")
        old_min = metrics.get("max_adverse_net_pct")
        metrics["max_favorable_net_pct"] = max(old_max, net_pct) if old_max is not None else net_pct
        metrics["max_adverse_net_pct"] = min(old_min, net_pct) if old_min is not None else net_pct
        metrics["last_net_pct"] = net_pct
        active_target = as_float(metrics.get("take_profit_net_pct"), TAKE_PROFIT*100) / 100
        if net_return >= active_target and metrics.get("seconds_to_first_take_profit_observed") is None:
            metrics["seconds_to_first_take_profit_observed"] = round(max(0.0, time.time() - pos.opened_at), 2)
        metrics["hit_take_profit_observed"] = bool(metrics.get("hit_take_profit_observed") or net_return >= active_target)
        # Record virtual Strategy C without changing this position's real paper rules.
        shadow = metrics.setdefault("shadow_c", {"status": "open"})
        if shadow.get("status") == "open":
            shadow_reason = ("take_profit" if net_return >= SHADOW_STRATEGY_C_TP else
                             "stop_loss" if net_return <= -SHADOW_STRATEGY_C_SL else None)
            if shadow_reason:
                shadow.update(status="closed", exit_reason=shadow_reason,
                              exit_net_pct=round(net_pct, 3),
                              exit_elapsed_seconds=round(max(0, time.time()-pos.opened_at), 2))
                log.info("PAPER SHADOW C %s reason=%s estimated_net_pct=%.2f elapsed_s=%.1f [no simulated fill guarantee]",
                         pos.symbol, shadow_reason, net_pct, shadow["exit_elapsed_seconds"])
        # Feed both detectors only valid primary-pool quotes, never fallback
        # valuations. Save enough history for confirmed loss deterioration.
        reversal_signal = False
        reversal_details = {"status": "disabled_or_fallback"}
        loss_signal = False
        loss_details = {"status": "disabled_or_fallback"}
        sustained_signal = False
        sustained_details = {"status": "disabled_or_fallback"}
        # Only the pinned entry pool's primary indicative price is eligible.
        # A missing-pool marker, cross-pool quote or recovered secondary depth
        # cannot certify sustained price deterioration.
        primary_eligible = (source == "dexscreener" and
                            bool(pos.entry_pool_address) and
                            c.get("pool_address") == pos.entry_pool_address)
        if source != "geckoterminal_indicative" and (PAPER_REVERSAL_EXIT_ENABLED or PAPER_EARLY_LOSS_REVERSAL_ENABLED or (PAPER_SUSTAINED_LOSS_ENABLED and primary_eligible)):
            primary_prices = metrics.setdefault("recent_primary_prices", [])
            primary_prices.append([time.time(), price])
            primary_prices[:] = [[t,p] for t,p in primary_prices if 0 <= time.time()-t <= max(PAPER_REVERSAL_LOOKBACK_SECONDS, 120.0, PAPER_SUSTAINED_LOSS_LOOKBACK_SECONDS)]
            if len(primary_prices) > 120:
                primary_prices[:] = primary_prices[-120:]
            if PAPER_REVERSAL_EXIT_ENABLED:
                reversal_signal, reversal_details = detect_immediate_reversal(primary_prices, now=time.time(), opened_at=pos.opened_at)
            if PAPER_EARLY_LOSS_REVERSAL_ENABLED:
                loss_signal, loss_details = early_loss_reversal(
                    primary_prices, now=time.time(), opened_at=pos.opened_at,
                    net_return_pct=net_pct, min_hold_seconds=PAPER_EARLY_LOSS_MIN_HOLD_SECONDS,
                    min_net_loss_pct=PAPER_EARLY_LOSS_NET_PCT,
                    min_market_drop_pct=PAPER_EARLY_LOSS_MARKET_DROP_PCT,
                    max_quote_age_seconds=PAPER_REVERSAL_MAX_QUOTE_AGE_SECONDS)
            if PAPER_SUSTAINED_LOSS_ENABLED and primary_eligible:
                entry_market = as_float(metrics.get("entry_market_price")) or (
                    pos.entry_price / (1.0 + ESTIMATED_ROUND_TRIP_COST / 2.0))
                sustained_signal, sustained_details = sustained_loss_exit(
                    primary_prices, now=time.time(), opened_at=pos.opened_at,
                    entry_market_price=entry_market, net_return_pct=net_pct,
                    min_hold_seconds=PAPER_SUSTAINED_LOSS_MIN_HOLD_SECONDS,
                    min_net_loss_pct=PAPER_SUSTAINED_LOSS_NET_PCT,
                    min_entry_market_drop_pct=PAPER_SUSTAINED_LOSS_MARKET_DROP_PCT,
                    confirmation_seconds=PAPER_SUSTAINED_LOSS_CONFIRM_SECONDS,
                    lookback_seconds=PAPER_SUSTAINED_LOSS_LOOKBACK_SECONDS,
                    max_quote_age_seconds=PAPER_REVERSAL_MAX_QUOTE_AGE_SECONDS)
        # v7.6: a *separate* confirmed primary-pool net history. Fallback
        # quotes and another pool's price can never trigger these exits.
        fast_trail, fast_trail_diag = False, {"status":"not_primary"}
        fast_loss, fast_loss_diag = False, {"status":"not_primary"}
        if primary_eligible:
            primary_net = metrics.setdefault("primary_net_observations", [])
            primary_net.append([time.time(), net_pct, price])
            primary_net[:] = [x for x in primary_net if 0 <= time.time()-x[0] <= 3600][-150:]
            if PAPER_PROFIT_TRAIL_ENABLED:
                fast_trail, fast_trail_diag = confirmed_profit_trail(
                    primary_net, now=time.time(), opened_at=pos.opened_at,
                    arm_net_pct=PAPER_PROFIT_TRAIL_ARM_NET_PCT,
                    giveback_pct=PAPER_PROFIT_TRAIL_GIVEBACK_PCT)
            if PAPER_LOSS_CIRCUIT_ENABLED:
                fast_loss, fast_loss_diag = confirmed_loss_circuit(
                    primary_net, now=time.time(), opened_at=pos.opened_at,
                    entry_market_price=as_float(metrics.get("entry_market_price")))
        if PAPER_MODE and time.time() - as_float(metrics.get("last_v76_diag_at")) >= PAPER_EXIT_SIGNAL_LOG_INTERVAL_SECONDS:
            metrics["last_v76_diag_at"] = time.time()
            log.info("PAPER FAST EXIT HEALTH %s net_pct=%.2f primary=%s peak_net_pct=%s trailing=%s loss_circuit=%s tp_net_pct=%.1f indicative_only=True",
                     pos.symbol, net_pct, primary_eligible, fast_trail_diag.get("peak_net_pct"),
                     fast_trail_diag.get("status"), fast_loss_diag.get("status"),
                     as_float(metrics.get("take_profit_net_pct"), TAKE_PROFIT*100))
        if PAPER_MODE and time.time() - as_float(metrics.get("last_reversal_diag_at")) >= PAPER_EXIT_SIGNAL_LOG_INTERVAL_SECONDS:
            metrics["last_reversal_diag_at"] = time.time()
            log.info("PAPER EXIT SIGNAL HEALTH %s net_pct=%.2f age_s=%.0f quote_source=%s primary_points=%d reversal=%s reversal_drop=%s loss_guard=%s loss_market_drop=%s sustained_guard=%s entry_drop=%s underwater_s=%s distinct_moves=%s cost_assumption=%.2f%%",
                     pos.symbol, net_pct, time.time()-pos.opened_at, source,
                     len(metrics.get("recent_primary_prices", [])), reversal_details.get("status"),
                     reversal_details.get("drop_pct"), loss_details.get("status"),
                     loss_details.get("market_drop_pct"), sustained_details.get("status"),
                     sustained_details.get("entry_market_drop_pct"), sustained_details.get("underwater_seconds"),
                     sustained_details.get("distinct_price_moves"), ESTIMATED_ROUND_TRIP_COST*100)
        held_seconds = max(0.0, time.time() - pos.opened_at)
        points = metrics.setdefault("observed_points", {})
        for seconds in (30, 60, 120):
            if held_seconds >= seconds and str(seconds) not in points:
                points[str(seconds)] = {"elapsed_seconds": round(held_seconds, 1), "net_return_pct": round(net_pct, 3)}
                log.info("PAPER MILESTONE %s target=%ds observed_at=%.1fs estimated_net_pct=%.2f", pos.symbol, seconds, held_seconds, net_pct)
        peak_return = price / max(pos.peak_price, 1e-15) - 1
        effective_tp = as_float(metrics.get("take_profit_net_pct"), TAKE_PROFIT*100) / 100
        if net_return >= effective_tp:
            close_position(state, mint, price, "fast_take_profit" if effective_tp < TAKE_PROFIT else "take_profit", liquidity, source)
        elif net_return <= -STOP_LOSS:
            close_position(state, mint, price, "stop_loss", liquidity, source)
        elif reversal_signal:
            metrics["reversal_exit_attempts"] = int(metrics.get("reversal_exit_attempts", 0)) + 1
            log.warning("PAPER REVERSAL EXIT SIGNAL %s decline=%.2f%% observations=%d source=%s; checking verified indicative exit",
                        pos.symbol, reversal_details["drop_pct"], reversal_details["observations"], source)
            close_position(state, mint, price, "immediate_reversal", liquidity, source)
        elif loss_signal:
            metrics["early_loss_reversal_exit_attempts"] = int(metrics.get("early_loss_reversal_exit_attempts", 0)) + 1
            log.warning("PAPER CONFIRMED LOSS REVERSAL %s net_pct=%.2f market_drop_pct=%.2f observations=%d source=%s; verified indicative exit only",
                        pos.symbol, net_pct, loss_details["market_drop_pct"], loss_details["observations"], source)
            close_position(state, mint, price, "confirmed_loss_reversal", liquidity, source)
        elif fast_loss:
            log.warning("PAPER FAST LOSS CIRCUIT %s net_pct=%.2f%% entry_drop=%.2f%% confirmation_s=%s indicative_only=True",
                        pos.symbol, net_pct, fast_loss_diag.get("entry_drop_pct",0),
                        fast_loss_diag.get("confirmation_seconds"))
            close_position(state, mint, price, "confirmed_loss_circuit", liquidity, source)
        elif fast_trail:
            log.info("PAPER FAST PROFIT TRAIL %s net_pct=%.2f%% observed_peak=%.2f%% floor=%.2f%% indicative_only=True",
                     pos.symbol, net_pct, fast_trail_diag.get("peak_net_pct",0),
                     fast_trail_diag.get("floor_net_pct",0))
            close_position(state, mint, price, "confirmed_profit_trail", liquidity, source)
        elif sustained_signal:
            metrics["sustained_loss_exit_attempts"] = int(metrics.get("sustained_loss_exit_attempts", 0)) + 1
            log.warning("PAPER SUSTAINED LOSS EXIT %s net_pct=%.2f%% entry_drop=%.2f%% underwater_s=%.0f distinct_moves=%d pool=%s source=%s indicative_only=True",
                        pos.symbol, net_pct, sustained_details["entry_market_drop_pct"],
                        sustained_details["underwater_seconds"], sustained_details["distinct_price_moves"],
                        pos.entry_pool_address, source)
            close_position(state, mint, price, "confirmed_sustained_loss", liquidity, source)
        elif MAX_HOLD_MINUTES > 0 and time.time() - pos.opened_at >= MAX_HOLD_MINUTES * 60:
            close_position(state, mint, price, "time_limit", liquidity, source)
        elif TRAILING_STOP_ENABLED and pos.peak_price > pos.entry_price and peak_return <= -TRAILING_STOP:
            close_position(state, mint, price, "trailing_stop", liquidity, source)
        elif c["liquidity"] < MIN_LIQUIDITY_USD * 0.65:
            close_position(state, mint, price, "liquidity_deterioration", liquidity, source)


_candidate_price_history: dict[str, list[tuple[float, float, float | None]]] = {}
# Pending mints are reverified from fresh API quotes before any entry. Never
# treat their old score, price, or liquidity as valid for an order.
_pending_entry_mints: OrderedDict[str, float] = OrderedDict()
PENDING_ENTRY_TTL_SECONDS = max(30.0, float(os.getenv("PENDING_ENTRY_TTL_SECONDS", "90")))
PENDING_ENTRY_SCAN_QUOTA = max(0, min(15, int(os.getenv("PENDING_ENTRY_SCAN_QUOTA", "8"))))


def pending_entry_due(*, now: float | None = None) -> list[str]:
    stamp = time.time() if now is None else now
    for mint, first_at in list(_pending_entry_mints.items()):
        if stamp - first_at > PENDING_ENTRY_TTL_SECONDS:
            _pending_entry_mints.pop(mint, None)
    return [mint for mint in _pending_entry_mints if not mint_cooling_down(mint)][:PENDING_ENTRY_SCAN_QUOTA]


def watch_pending_entry(mint: str, detail: str, *, now: float | None = None) -> None:
    if detail != "awaiting_second_price_observation":
        _pending_entry_mints.pop(mint, None)
        return
    _pending_entry_mints.setdefault(mint, time.time() if now is None else now)
    _pending_entry_mints.move_to_end(mint)
    while len(_pending_entry_mints) > 250:
        _pending_entry_mints.popitem(last=False)



def candidate_momentum_confirm(mint: str, price: float, *, now: float | None = None, buy_pressure: float | None = None, force_confirmation: bool = False) -> tuple[bool, str]:
    """Two independent observations from entry scans; never assume unseen momentum.

    First sightings are watched, not instantly purchased. A short-term price
    decline is never interpreted as upward confirmation.
    """
    now = time.time() if now is None else now
    if not ENTRY_TREND_CONFIRM_ENABLED and not force_confirmation:
        return True, "disabled"
    if price <= 0:
        return False, "no_current_price"
    history = _candidate_price_history.setdefault(mint, [])
    history[:] = [(t, p, b) for t, p, b in history if 0 <= now-t <= ENTRY_TREND_MAX_OBSERVATION_SECONDS]
    baseline = next(((t, p, b) for t, p, b in history if now-t >= ENTRY_TREND_MIN_OBSERVATION_SECONDS), None)
    history.append((now, price, buy_pressure))
    if len(_candidate_price_history) > 2500:
        for old in list(_candidate_price_history)[:250]:
            _candidate_price_history.pop(old, None)
    if baseline is None:
        return False, "awaiting_second_price_observation"
    change = (price / baseline[1] - 1) * 100
    if change < ENTRY_TREND_MIN_CHANGE_PCT:
        return False, f"short_momentum_not_confirmed:{change:.2f}pct"
    if buy_pressure is not None and baseline[2] is not None and buy_pressure < baseline[2] - ENTRY_TREND_MAX_BUY_PRESSURE_DROP:
        return False, f"buy_pressure_fading:{buy_pressure:.3f}_vs_{baseline[2]:.3f}"
    return True, f"confirmed:{change:.2f}pct"


def detect_immediate_reversal(history: list, *, now: float, opened_at: float) -> tuple[bool, dict]:
    """Confirmed recent fall from a recent high; use quote values only, no fills."""
    window = [(float(t), float(p)) for t, p in history
              if isinstance(t, (int, float)) and isinstance(p, (int, float))
              and p > 0 and 0 <= now - t <= PAPER_REVERSAL_LOOKBACK_SECONDS]
    if now - opened_at < PAPER_REVERSAL_MIN_HOLD_SECONDS or len(window) < PAPER_REVERSAL_CONFIRM_OBSERVATIONS:
        return False, {"status": "insufficient_recent_history"}
    window.sort()
    # Require a distinct prior high and multiple observations after that high.
    peak_index = max(range(len(window)), key=lambda i: window[i][1])
    confirmations = window[peak_index+1:]
    if len(confirmations) < PAPER_REVERSAL_CONFIRM_OBSERVATIONS - 1:
        return False, {"status": "insufficient_post_peak_confirmation"}
    peak = window[peak_index][1]
    drop_pct = (1 - window[-1][1] / peak) * 100
    # Consecutively deteriorating prices, not a single volatile quote.
    declining = all(confirmations[i][1] <= confirmations[i-1][1] for i in range(1, len(confirmations)))
    recent = now-window[-1][0] <= PAPER_REVERSAL_MAX_QUOTE_AGE_SECONDS
    result = {"status": "reversal" if (declining and recent and drop_pct >= PAPER_REVERSAL_MIN_DROP_PCT) else "no_reversal",
              "drop_pct": round(drop_pct, 2), "peak_price": peak, "observations": len(window)}
    return result["status"] == "reversal", result

_first_discovered_at: OrderedDict[str, float] = OrderedDict()
_entry_scan_next_at = 0.0
_entry_scan_cursor = 0
# Revisit previously confirmed SOL-quoted, supported, sufficiently liquid DEX
# markets. These are candidates for momentum re-evaluation, NOT auto-buys.
_verified_dex_mints: OrderedDict[str, float] = OrderedDict()
_verified_dex_scan_cursor = 0
_quality_pool_scan_cursor = 0
_entry_reject_until: OrderedDict[str, float] = OrderedDict()
_entry_reject_reason: dict[str, str] = {}
_momentum_cooldown_probe_after: OrderedDict[str, float] = OrderedDict()
_momentum_cooldown_probe_cursor = 0

# These are poor *momentum* readings, not intrinsically broken routes/pools.
# Structural and absent-market-data failures keep their longer cooldown.
_MOMENTUM_RETRY_REASONS = frozenset((
    "minimum_volume", "momentum_volume", "momentum_volume_1h",
    "momentum_price_change", "momentum_buy_sell_ratio",
    "momentum_volume_liquidity_ratio", "downtrend_multi_timeframe",
    "trend_m5_missing", "trend_h1_missing", "trend_h6_missing",
    "trend_h24_missing", "extreme_price_spike",
    "fast_price_momentum", "fast_volume_depth", "fast_volume_pace",
    "fast_buy_pressure", "fast_overheated_or_anomalous",
))

def note_entry_rejection(mint: str, reason: str, *, now: float | None = None) -> None:
    """Temporarily deprioritize unsupported or unindexed mints, never ban permanently."""
    stamp = time.monotonic() if now is None else now
    ttl = (STALE_POOL_RECHECK_SECONDS if reason == "too_old" else
           MISSING_DATA_COOLDOWN_SECONDS if reason in ("pair_not_indexed", "supported_pair_liquidity_unavailable", "supported_pair_missing_price", "missing_pair_age") else
           MOMENTUM_REJECT_COOLDOWN_SECONDS if MOMENTUM_SWING_MODE and reason in _MOMENTUM_RETRY_REASONS else
           STRUCTURAL_REJECT_COOLDOWN_SECONDS)
    _entry_reject_until[mint] = stamp + ttl
    _entry_reject_until.move_to_end(mint)
    _entry_reject_reason[mint] = reason
    while len(_entry_reject_until) > 2500:
        old, _ = _entry_reject_until.popitem(last=False)
        _entry_reject_reason.pop(old, None)

def mint_cooling_down(mint: str, *, now: float | None = None) -> bool:
    stamp = time.monotonic() if now is None else now
    until = _entry_reject_until.get(mint, 0.0)
    if until <= stamp:
        _entry_reject_until.pop(mint, None)
        _entry_reject_reason.pop(mint, None)
        return False
    return True


def quality_quarantined(mint: str) -> bool:
    """Quality backoff never applies to held positions/exit price monitoring."""
    return CANDIDATE_QUALITY_ENABLED and _quality_ledger.blocked(mint)


def entry_lookup_blocked(mint: str) -> bool:
    return (mint_cooling_down(mint) or quality_quarantined(mint) or
            (FRESH_BATCH_DISCOVERY_ENABLED and _fresh_batch.blocked(mint)))


def momentum_cooldown_probes(addresses: list[str], *, now: float | None = None,
                             limit: int | None = None) -> list[str]:
    """Bounded market-data rechecks from cooled mints during sparse scans.

    Cooldown probes must never turn into blanket bypasses: they are bounded per
    scan and per mint, re-fetch market data, and pass the normal entry filters.
    In particular missing liquidity is NOT inferred to be zero or sufficient.
    """
    global _momentum_cooldown_probe_cursor
    if not MOMENTUM_SWING_MODE or SNIPER_MODE or FRESH_BATCH_DISCOVERY_ENABLED:
        # A rejected v7 batch must not reappear through the old cooldown
        # probe escape hatch. It can be freshly sourced after quarantine.
        return []
    stamp = time.monotonic() if now is None else now
    cap = MOMENTUM_COOLDOWN_PROBE_QUOTA if limit is None else min(MOMENTUM_COOLDOWN_PROBE_QUOTA, max(0, limit))
    if cap <= 0:
        return []
    # Keep a compact ledger; a discovered mint can disappear from the feed.
    for mint, retry_after in list(_momentum_cooldown_probe_after.items()):
        if retry_after + 3600 < stamp:
            _momentum_cooldown_probe_after.pop(mint, None)
    pool = list(dict.fromkeys(addresses))
    if not pool:
        return []
    start = _momentum_cooldown_probe_cursor % len(pool)
    chosen = []
    traversed = 0
    while traversed < len(pool) and len(chosen) < cap:
        mint = pool[(start + traversed) % len(pool)]
        traversed += 1
        if (mint_cooling_down(mint, now=stamp) and not quality_quarantined(mint) and
                _momentum_cooldown_probe_after.get(mint, 0.0) <= stamp):
            chosen.append(mint)
            _momentum_cooldown_probe_after[mint] = stamp + MOMENTUM_COOLDOWN_PROBE_GAP_SECONDS
            _momentum_cooldown_probe_after.move_to_end(mint)
    _momentum_cooldown_probe_cursor = (start + traversed) % len(pool)
    while len(_momentum_cooldown_probe_after) > 2500:
        _momentum_cooldown_probe_after.popitem(last=False)
    return chosen


def fill_sparse_momentum_scan(addresses: list[str], selected: list[str], *, count: int,
                              cooling_before: int, now: float | None = None) -> tuple[list[str], list[str]]:
    """Keep sparse established-market scans alive without bypassing entry gates.

    A due reentry or one verified token used to suppress *all* cooldown probes,
    even if nearly 100% of discovered mints were cooling. Supplement a nearly
    empty scan with at most the existing per-mint rate-limited probe quota.
    Rechecks are ordinary market fetches: no cached quote, liquidity or trend
    reading is granted authority to buy.
    """
    if not MOMENTUM_SWING_MODE or SNIPER_MODE or FRESH_BATCH_DISCOVERY_ENABLED or not addresses or count <= 0:
        return selected, []
    if selected and cooling_before * 4 < len(addresses) * 3:
        return selected, []  # normal discovery still has sufficient coverage
    if len(selected) >= min(count, 8):
        return selected, []
    room = min(max(0, count - len(selected)), MOMENTUM_COOLDOWN_PROBE_QUOTA)
    if room <= 0:
        return selected, []
    already_selected = set(selected)
    possible = [m for m in dict.fromkeys(addresses) if m not in already_selected]
    probes = momentum_cooldown_probes(possible, now=now, limit=room)
    return selected + probes, probes


def retain_fresh_verified_pool(mint: str, pair: dict, *, allow_reentry_age: bool = False) -> bool:
    """Exclude age-expired sniper pools from repeated scans, not from all discovery.

    Retains a recheck after a bounded cooldown in case a mint later opens a
    distinct eligible pool. This check cannot authorize a trade.
    """
    if SNIPER_MODE and age_hours(pair) >= 1e9:
        _verified_dex_mints.pop(mint, None)
        note_entry_rejection(mint, "missing_pair_age")
        return False
    age_limit = PAPER_REENTRY_MAX_POOL_AGE_MINUTES if allow_reentry_age else SNIPER_MAX_AGE_MINUTES
    if SNIPER_MODE and age_hours(pair) * 60 > age_limit:
        _verified_dex_mints.pop(mint, None)
        _sniper_watchlist.pop(mint, None)
        note_entry_rejection(mint, "too_old")
        return False
    note_verified_dex_mint(mint)
    return True


def note_verified_dex_mint(mint: str, *, now: float | None = None) -> None:
    if not mint or mint == SOL_MINT:
        return
    stamp = time.monotonic() if now is None else now
    # In established mode, verified liquidity alone does not cancel a
    # recent momentum rejection. Otherwise old non-trending pools monopolize
    # the reserved watchlist slots every 15 seconds.
    if not MOMENTUM_SWING_MODE:
        _entry_reject_until.pop(mint, None)
        _entry_reject_reason.pop(mint, None)
    _verified_dex_mints[mint] = stamp + VERIFIED_DEX_TTL_SECONDS
    _verified_dex_mints.move_to_end(mint)
    while len(_verified_dex_mints) > 250:
        _verified_dex_mints.popitem(last=False)


def verified_dex_due(*, now: float | None = None, limit: int | None = None) -> list[str]:
    stamp = time.monotonic() if now is None else now
    for mint, expires_at in list(_verified_dex_mints.items()):
        if expires_at <= stamp:
            _verified_dex_mints.pop(mint, None)
    cap = VERIFIED_DEX_RECHECK_QUOTA if limit is None else limit
    if not MOMENTUM_SWING_MODE:
        return list(reversed(_verified_dex_mints))[:max(0, cap)]
    # Round-robin older verified pools, rather than always returning the most
    # recently touched tokens and starving the rest of the watchlist.
    global _verified_dex_scan_cursor
    pool = list(_verified_dex_mints)
    if not pool or cap <= 0:
        return []
    start = _verified_dex_scan_cursor % len(pool)
    chosen: list[str] = []
    if CANDIDATE_QUALITY_ENABLED:
        # Reserve at most half the verified-market quota for genuinely recent,
        # independently revalidated momentum. Keep the other half round-robin.
        hot = sorted((m for m in pool if not mint_cooling_down(m, now=stamp)
                      and not quality_quarantined(m)
                      and _quality_ledger.momentum_rank(m) > 0),
                     key=lambda m: _quality_ledger.momentum_rank(m), reverse=True)
        chosen = hot[:max(1, cap // 2)]
    visited = 0
    while visited < len(pool) and len(chosen) < cap:
        mint = pool[(start + visited) % len(pool)]
        visited += 1
        if mint not in chosen and not mint_cooling_down(mint, now=stamp) and not quality_quarantined(mint):
            chosen.append(mint)
    _verified_dex_scan_cursor = (start + visited) % len(pool)
    return chosen


def update_sniper_watchlist(scores: dict[str, float], *, now: float | None = None) -> None:
    """Store Stage-1 hints for repeat evaluation, bounded by both TTL and size.

    This is discovery-only: no score or cached quote ever authorizes a buy.
    """
    stamp = time.monotonic() if now is None else now
    for mint, (expiry, _) in list(_sniper_watchlist.items()):
        if expiry <= stamp:
            _sniper_watchlist.pop(mint, None)
    for mint, score in scores.items():
        if not mint or mint == SOL_MINT:
            continue
        _sniper_watchlist[mint] = (stamp + SNIPER_WATCHLIST_TTL_SECONDS, score)
        _sniper_watchlist.move_to_end(mint)
    while len(_sniper_watchlist) > SNIPER_WATCHLIST_LIMIT:
        _sniper_watchlist.popitem(last=False)


def sniper_watchlist_due(*, now: float | None = None, limit: int | None = None) -> list[str]:
    """Rotate across Stage-1 markets so older watched pools are not starved."""
    global _sniper_watch_cursor
    stamp = time.monotonic() if now is None else now
    update_sniper_watchlist({}, now=stamp)
    pool = [mint for mint in _sniper_watchlist if not mint_cooling_down(mint, now=stamp)]
    if not pool:
        return []
    n = min(len(pool), SNIPER_WATCHLIST_RECHECK_QUOTA if limit is None else max(0, limit))
    start = _sniper_watch_cursor % len(pool)
    chosen = [pool[(start + i) % len(pool)] for i in range(n)]
    _sniper_watch_cursor = (start + n) % len(pool)
    return chosen


def defer_unavailable_fresh_batch_mints(*, held_mints: set[str] | None = None) -> dict[str, int]:
    """Stop earlier screening backoffs from masquerading as unscanned work.

    No backoff is bypassed. This only removes blocked mints from the active
    entry queue; positions continue to be valued and managed separately.
    """
    if not FRESH_BATCH_DISCOVERY_ENABLED:
        return {"quality":0, "cooldown":0}
    held = held_mints or set()
    removed = {"quality":0, "cooldown":0}
    for mint in list(_fresh_batch.active):
        if mint in held:
            continue
        if quality_quarantined(mint):
            if _fresh_batch.defer_blocked(mint, "existing_quality_quarantine"):
                removed["quality"] += 1
        elif mint_cooling_down(mint):
            if _fresh_batch.defer_blocked(mint, "existing_entry_cooldown"):
                removed["cooldown"] += 1
    if any(removed.values()):
        st = _fresh_batch.stats()
        log.info("FRESH BATCH DEFERRED quality=%d entry_cooldown=%d remaining_active=%d remaining_unscanned=%d quarantined=%d reason=existing_backoff_not_scannable",
                 removed['quality'], removed['cooldown'], st['active'], st['unscanned'], st['quarantined'])
        _fresh_batch.save()
    return removed


def select_entry_mints(addresses: list[str], fresh_due: list[str], *, count: int, cursor: int,
                       reentry_due: list[str] | None = None,
                       held_mints: set[str] | None = None) -> tuple[list[str], int, int]:
    """Reserve slots for verified pools; share the rest with fresh and rotating discovery.

    This only selects mints for new API lookups. No cached price, liquidity or
    momentum score is trusted for an actual trade.
    """
    cap = max(0, count)
    held = held_mints or set()
    def entry_allowed(mint: str) -> bool:
        return mint not in held and not entry_lookup_blocked(mint)
    if SNIPER_MODE:
        selected = [m for m in dict.fromkeys(pending_entry_due() + (reentry_due or [])) if m not in held and not quality_quarantined(m)][:cap]
        # Preserve first slots for freshly discovered, prequalified pools.
        # Broad FOMO/boosted watchlists often contain expired markets; they
        # should never crowd these more recent observations out.
        for mint in _quality_pool_mints[:min(QUALITY_POOL_SCAN_QUOTA, cap)]:
            if len(selected) >= cap:
                break
            if mint not in selected and entry_allowed(mint):
                selected.append(mint)
        for mint in secondary_due_mints():
            if len(selected) >= cap:
                break
            if mint not in selected:
                selected.append(mint)
        for mint in verified_dex_due(limit=min(SNIPER_VERIFIED_RECHECK_QUOTA, max(0, cap - len(selected)))):
            if len(selected) >= cap:
                break
            if mint not in selected and entry_allowed(mint):
                selected.append(mint)
        fresh_n = 0
        for mint in fresh_due:
            if len(selected) >= cap or fresh_n >= min(SNIPER_LAUNCH_PROBE_QUOTA, FRESH_LAUNCH_SCAN_QUOTA):
                break
            if mint not in selected and entry_allowed(mint):
                selected.append(mint)
                fresh_n += 1
        # Stage 2: rotate remaining watched pools at the end, ensuring that
        # pending, fresh and verified candidate slots are never starved.
        for mint in sniper_watchlist_due(limit=min(SNIPER_WATCHLIST_RECHECK_QUOTA, cap)):
            if len(selected) >= cap:
                break
            if mint not in selected:
                selected.append(mint)
        return selected, cursor, fresh_n
    if FRESH_BATCH_DISCOVERY_ENABLED:
        # The ACTIVE cohort is finite: never rehydrate rejected tokens from
        # the historical watchlist, verified-market cache or boosted feeds.
        # Entries with pending second observations stay in the active batch.
        pending = [m for m in dict.fromkeys(pending_entry_due() + (reentry_due or []))
                   if m in _fresh_batch.active and m not in held and not entry_lookup_blocked(m)]
        selected = pending[:cap]
        # Fill primarily from the fresh source cohort. Do not re-add stale
        # verified markets to fill an arbitrary 60-token quota.
        for mint in _fresh_batch.due(limit=FRESH_BATCH_ACTIVE_LIMIT):
            if len(selected) >= cap:
                break
            if mint not in selected and entry_allowed(mint):
                selected.append(mint)
        return selected, cursor, 0
    # Cooldowns apply only to new entry lookups; verified pools are rechecked.
    # In established mode reserve discovery space instead of endlessly checking
    # the same verified or top-of-quality-list mints. Recent verified entry
    # candidates keep priority via pending_entry_due().
    verified_budget = min(VERIFIED_DEX_RECHECK_QUOTA, max(4, cap // 3)) if MOMENTUM_SWING_MODE else VERIFIED_DEX_RECHECK_QUOTA
    selected = [m for m in dict.fromkeys(pending_entry_due() + (reentry_due or []) + verified_dex_due(limit=min(verified_budget, cap))) if m not in held and not quality_quarantined(m)][:cap]
    selected = selected[:cap]
    if ROLLING_DISCOVERY_ENABLED and MOMENTUM_SWING_MODE:
        # Reserve up to ~40% of entry slots for never/least-recently checked
        # independent pool discoveries. Never override a rejection cooldown.
        rolling_cap = min(ROLLING_DISCOVERY_FRESH_SCAN_QUOTA, max(0, cap * 2 // 5))
        rolling_count = 0
        rolling_order = _rolling_watch.priority_mints()
        if CANDIDATE_QUALITY_ENABLED:
            unseen = [m for m in rolling_order if _rolling_watch.records[m]["last_scanned"] == 0]
            seen = [m for m in rolling_order if _rolling_watch.records[m]["last_scanned"] > 0]
            # Half the watchlist quota is reserved for genuinely unscanned pools.
            # Recent VERIFIED momentum gets priority within the remainder.
            recent = [m for m in _quality_ledger.prioritise(seen)
                      if _quality_ledger.momentum_rank(m) > 0]
            rolling_order = unseen[:max(1, rolling_cap // 2)] + recent + unseen[max(1, rolling_cap // 2):] + seen
        for mint in rolling_order:
            if rolling_count >= rolling_cap or len(selected) >= cap:
                break
            if mint not in selected and entry_allowed(mint):
                selected.append(mint)
                rolling_count += 1
    quality_selected = 0
    global _quality_pool_scan_cursor
    quality_pool = _quality_pool_mints
    if MOMENTUM_SWING_MODE and quality_pool:
        start = _quality_pool_scan_cursor % len(quality_pool)
        quality_pool = [quality_pool[(start + i) % len(quality_pool)] for i in range(len(quality_pool))]
        _quality_pool_scan_cursor = (start + min(len(quality_pool), QUALITY_POOL_SCAN_QUOTA)) % len(quality_pool)
    for mint in quality_pool:
        if quality_selected >= QUALITY_POOL_SCAN_QUOTA or len(selected) >= cap:
            break
        if mint not in selected and entry_allowed(mint):
            selected.append(mint)
            quality_selected += 1
    fresh_cap = min(FRESH_LAUNCH_SCAN_QUOTA, cap - len(selected))
    prioritized_fresh = []
    for mint in fresh_due:
        if mint not in selected and entry_allowed(mint) and len(prioritized_fresh) < fresh_cap:
            prioritized_fresh.append(mint)
    selected.extend(prioritized_fresh)
    # Older indexed pools discovered through search are also valuable.
    search = sorted((m for m in addresses if m in _discovery_priority and m not in selected and m not in held and not entry_lookup_blocked(m)),
                    key=lambda m: (_quality_ledger.momentum_rank(m) if CANDIDATE_QUALITY_ENABLED else 0, _discovery_priority[m]), reverse=True)
    for mint in search[:max(0, (cap - len(selected)) // 2)]:
        selected.append(mint)
    rotating = [m for m in dict.fromkeys(addresses) if m not in selected and m not in held and not entry_lookup_blocked(m)]
    remaining = cap - len(selected)
    chosen = [rotating[(cursor + i) % len(rotating)] for i in range(min(remaining, len(rotating)))] if rotating else []
    selected.extend(chosen)
    return selected, (cursor + len(chosen)) % max(1, len(rotating)), len(prioritized_fresh)



def scan_once(state: dict) -> None:
    global _entry_scan_next_at, _entry_scan_cursor
    # Always manage existing positions; discovery is a separate, slower job.
    now = time.monotonic()
    scan_entries = now >= _entry_scan_next_at and api_cooldown_remaining() <= 0
    if scan_entries:
        _entry_scan_next_at = now + ENTRY_SCAN_INTERVAL_SECONDS
    addresses = latest_candidate_addresses() if scan_entries else []
    if scan_entries:
        if FRESH_BATCH_DISCOVERY_ENABLED:
            # Trust the *current* active queue, not a 60s discovery snapshot
            # that can still contain rejected or previously cooled candidates.
            revived = _fresh_batch.resume_hot_due(limit=4)
            if revived:
                log.info("PAPER HOT RECHECK due=%d bounded_attempts=2 fresh_primary_market_check=required paper_only=True", len(revived))
            defer_unavailable_fresh_batch_mints(held_mints=set(state.get("positions", {})))
            addresses = _fresh_batch.due(limit=FRESH_BATCH_ACTIVE_LIMIT)
            if not addresses:
                log.info("FRESH BATCH WAIT reason=no_ready_active_candidates active=%d "
                         "quarantined=%d next_discovery_in_s=%.0f entries_paused_no_forced_buy=True",
                         _fresh_batch.stats()['active'], _fresh_batch.stats()['quarantined'],
                         max(0.0, _discovery_cache['until'] - time.monotonic()))
        else:
            addresses = list(dict.fromkeys(addresses + secondary_due_mints()))
    # A full paper portfolio still scans and ranks the market, but there is no
    # reason to schedule CLOSED-trade reentries until an exit frees a slot.
    # Positions are always monitored and can be closed later in this scan.
    entry_slots_at_scan_start = max(0, MAX_POSITIONS - len(state.get("positions", {})))
    reentry_due = (paper_reentry_watch_mints(state)
                   if scan_entries and entry_slots_at_scan_start > 0 else [])
    if scan_entries and PAPER_MODE and not entry_slots_at_scan_start:
        log.info("PAPER REENTRY PAUSED reason=position_limit positions=%d max=%d; "
                 "discovery_and_exit_monitoring=active",
                 len(state.get("positions", {})), MAX_POSITIONS)
    if scan_entries and reentry_due:
        if FRESH_BATCH_DISCOVERY_ENABLED:
            reentry_due = [m for m in reentry_due if m in _fresh_batch.active and not _fresh_batch.blocked(m)]
        addresses = list(dict.fromkeys(reentry_due + addresses))
        log.info("PAPER REENTRY WATCH eligible_due=%d cooldown=5m_reversal/3m_other scan_quota=%d",
                 len(reentry_due), PAPER_REENTRY_SCAN_QUOTA)
    if addresses:
        for mint in addresses:
            _first_discovered_at.setdefault(mint, time.time())
        while len(_first_discovered_at) > 3000:
            _first_discovered_at.popitem(last=False)
        count = (min(MAX_ENTRY_TOKENS_PER_SCAN, len(addresses)) if FRESH_BATCH_DISCOVERY_ENABLED else
                 min(MAX_ENTRY_TOKENS_PER_SCAN, max(len(addresses), len(_verified_dex_mints))))
        discovery_total = len(addresses)
        cooling_before = sum(mint_cooling_down(m) for m in addresses)
        stream_set = set(fresh_launch_snapshot())
        stream_priority = due_fresh_launches(limit=min(count, FRESH_LAUNCH_SCAN_QUOTA))
        quality_quarantine_skips = sum(quality_quarantined(m) for m in addresses) if CANDIDATE_QUALITY_ENABLED else 0
        selected_addresses, _entry_scan_cursor, fresh_selected = select_entry_mints(
            addresses, stream_priority, count=count, cursor=_entry_scan_cursor, reentry_due=reentry_due,
            held_mints=set(state.get("positions", {})) if CANDIDATE_QUALITY_ENABLED else None)
        # Normal candidates retain priority. When most discovery mints are
        # cooling and only a handful of regular entries remain, supplement
        # them with rate-limited probes rather than waiting for exactly zero.
        addresses, cooldown_probes = fill_sparse_momentum_scan(
            [m for m in addresses if m not in state.get("positions", {})] if CANDIDATE_QUALITY_ENABLED else addresses,
            selected_addresses, count=count, cooling_before=cooling_before)
        if ROLLING_DISCOVERY_ENABLED and MOMENTUM_SWING_MODE:
            unseen, watched = _rolling_watch.scan_counts(addresses)
            log.info("ENTRY ROTATION never_scanned=%d rolling_selected=%d watchlist_total=%d total_checked_history=%d",
                     unseen, watched, len(_rolling_watch.records),
                     sum(r["last_scanned"] > 0 for r in _rolling_watch.records.values()))
            _rolling_watch.mark_scanned(addresses)
            _rolling_watch.save()
        if FRESH_BATCH_DISCOVERY_ENABLED:
            _fresh_batch.mark_scanned(addresses)
            batch_stats = _fresh_batch.stats()
            log.info("FRESH BATCH SCAN active=%d remaining_unscanned=%d quarantined=%d selected=%d",
                     batch_stats["active"], batch_stats["unscanned"], batch_stats["quarantined"], len(addresses))
        log.info("ENTRY SCAN tokens=%d discovery_total=%d cooling_discovery=%d cooldown_probes=%d fresh_due=%d verified_dex_selected=%d verified_dex_watched=%d quality_pool_selected=%d quality_pool_watched=%d feed_connected=%s feed_messages=%d feed_reconnects=%d next_in=%ds",
                 len(addresses), discovery_total, cooling_before, len(cooldown_probes), fresh_selected, len([m for m in addresses if m in _verified_dex_mints]),
                 len(_verified_dex_mints), len([m for m in addresses if m in _quality_pool_mints]), len(_quality_pool_mints), _feed_connected, _feed_messages, _feed_reconnects, ENTRY_SCAN_INTERVAL_SECONDS)
        if FRESH_LAUNCH_FEED_ENABLED and not _feed_connected:
            log.info("FEED FALLBACK mode=polling_geckoterminal_new_pools reason=%s fresh_ws_events=0; "
                     "no_curve_only_entries supported_route_and_liquidity_checks_required", _feed_status)
        log.info("ENTRY PENDING rechecks_selected=%d waiting=%d fresh_feed_enabled=%s action=next_entry_scan_not_5s_portfolio_tick",
                 sum(m in _pending_entry_mints for m in addresses), len(_pending_entry_mints), FRESH_LAUNCH_FEED_ENABLED)
        if SNIPER_MODE:
            log.info("SNIPER STAGES watchlist=%d checked=%d fresh_probes=%d stage2_entry_requires=verified_dex_momentum", len(_sniper_watchlist), len([m for m in addresses if m in _sniper_watchlist]), fresh_selected)
    elif api_cooldown_remaining() > 0:
        log.info("ENTRY SCAN paused: DexScreener cooldown %.0fs", api_cooldown_remaining())
    if not addresses:
        quality_quarantine_skips = 0
    market: dict[str, dict] = {}
    batch_rejected: dict[str, str] = {}
    hot_nearmisses: set[str] = set()
    rejection_counts: Counter = Counter()
    route_rejections: Counter = Counter()
    evaluated = 0
    missing_pairs = 0
    batch_lookup_failures = 0
    market_data_rejections: Counter = Counter()
    secondary_outcomes: Counter = Counter()
    secondary_probes = 0
    verified_market_this_scan = 0
    eligible_verified = 0
    eligible_search = 0
    checked_mints: set[str] = set()
    curve_checks = 0
    curve_outcomes: Counter = Counter()
    curve_pending: list[str] = []
    curve_quote_ready = 0
    curve_activity_ready = 0
    curve_activity_observed_threshold_met = 0
    curve_reserve_observation_ready = 0
    for start in range(0, len(addresses), ENTRY_BATCH_SIZE):
        batch = addresses[start:start + ENTRY_BATCH_SIZE]
        if not batch:
            continue
        try:
            data = get_json(f"{BASE}/tokens/v1/solana/{','.join(batch)}")
            pairs = data if isinstance(data, list) else []
            # Select eligible SOL-quoted PumpPortal routes for new entries only.
            grouped: dict[str, list[dict]] = {}
            for p in pairs:
                mint = (p.get("baseToken") or {}).get("address")
                if mint:
                    grouped.setdefault(mint, []).append(p)
            for mint in batch:
                checked_mints.add(mint)
                if mint not in grouped:
                    missing_pairs += 1
                    market_data_rejections["pair_not_indexed"] += 1
                    batch_rejected[mint] = "pair_not_indexed"
                    if CANDIDATE_QUALITY_ENABLED:
                        _quality_ledger.fail(mint, "pair_not_indexed")
                    # A missing index response is not evidence that yesterday's
                    # verified pool disappeared. Retain it until its TTL expires.
                    mark_fresh_checked(mint, "pair_not_indexed")
                    if mint not in _verified_dex_mints:
                        note_entry_rejection(mint, "pair_not_indexed")
                    if mint in stream_set:
                        curve_pending.append(mint)
            # Give the scarce independent-liquidity calls to candidates that
            # already satisfy observable momentum/age prerequisites.
            ordered_mints = sorted(grouped.items(), key=lambda row: max(
                (secondary_candidate_priority(p, allow_reentry_age=paper_reentry_decision(state, row[0])[0]
                    and paper_reentry_decision(state, row[0])[2]) or -1 for p in row[1]), default=-1), reverse=True)
            for mint, mint_pairs in ordered_mints:
                evaluated += 1
                pair = best_entry_pair(mint_pairs)
                if pair is None and PAPER_SECONDARY_LIQUIDITY_ENABLED:
                    # A confirmed low value is never overridden. Prioritize
                    # missing-data SOL pools only if cheap momentum filters pass.
                    can_reenter, _, previously_held = paper_reentry_decision(state, mint)
                    eligible_missing = [(secondary_candidate_priority(p, allow_reentry_age=can_reenter and previously_held), p)
                                        for p in mint_pairs if (p.get("liquidity") or {}).get("usd") is None]
                    eligible_missing = [(rank, p) for rank, p in eligible_missing if rank is not None]
                    eligible_missing.sort(key=lambda item: item[0], reverse=True)
                    if not eligible_missing and any(
                            p.get("chainId") == "solana" and
                            (p.get("quoteToken") or {}).get("address") == SOL_MINT and
                            (p.get("dexId") or "").lower() in active_entry_routes() and
                            (p.get("liquidity") or {}).get("usd") is None for p in mint_pairs):
                        secondary_outcomes["unqualified_for_secondary_probe"] += 1
                    if eligible_missing:
                        priority, missing_pair = eligible_missing[0]
                        now_secondary = time.monotonic()
                        held_slot = _secondary_deferred.get(mint)
                        if held_slot and held_slot[1] > now_secondary:
                            secondary_outcomes["queued_for_budget"] += 1
                        elif secondary_probes >= PAPER_SECONDARY_LIQUIDITY_PER_SCAN:
                            secondary_defer(mint, priority, retry_at=now_secondary + ENTRY_SCAN_INTERVAL_SECONDS)
                            secondary_outcomes["queued_for_scan_budget"] += 1
                        else:
                            secondary_probes += 1
                            recovered, outcome = independently_verify_missing_liquidity(missing_pair)
                            secondary_outcomes[outcome] += 1
                            if recovered is not None:
                                _secondary_deferred.pop(mint, None)
                                pair = recovered
                                log.info("SECONDARY LIQUIDITY VERIFIED %s mint=%s pool=%s liq=$%.0f source=geckoterminal_indicative paper_only=True",
                                         (recovered.get("baseToken") or {}).get("symbol", mint), mint,
                                         recovered.get("pairAddress"), as_float(recovered["liquidity"]["usd"]))
                            else:
                                if outcome in ("minute_request_budget", "rate_limit_cooldown", "secondary_rate_limited", "secondary_request_failed"):
                                    _secondary_request_times[:] = [t for t in _secondary_request_times if now_secondary - t < 60]
                                    budget_reset = (_secondary_request_times[0] + 60 if _secondary_request_times else now_secondary + 30)
                                    retry_at = max(now_secondary + 15, budget_reset, _secondary_paused_until)
                                    secondary_defer(mint, priority, retry_at=retry_at)
                                else:
                                    _secondary_deferred.pop(mint, None)
                                log.info("SECONDARY LIQUIDITY UNVERIFIED mint=%s outcome=%s entry=blocked", mint, outcome)
                if pair is None:
                    # Log a useful reason for the deepest pool rather than scoring it.
                    diagnostic, representative = entry_pair_diagnostic(mint_pairs)
                    market_data_rejections[diagnostic] += 1
                    batch_rejected[mint] = diagnostic
                    if CANDIDATE_QUALITY_ENABLED:
                        _quality_ledger.fail(mint, diagnostic)
                    if representative:
                        reason = entry_pair_reason(representative) or diagnostic
                        route_rejections[reason.split(" ")[0]] += 1
                        log.info("REJECT %s: %s (market_data=%s)", (representative.get("baseToken") or {}).get("symbol", mint), reason, diagnostic)
                    else:
                        log.info("REJECT %s: %s", mint, diagnostic)
                    if diagnostic not in ("supported_pair_liquidity_unavailable", "supported_pair_missing_price"):
                        # Confirmed low liquidity / unsupported route invalidates
                        # the verified entry, unlike transient missing data.
                        _verified_dex_mints.pop(mint, None)
                    mark_fresh_checked(mint, diagnostic)
                    if mint not in _verified_dex_mints:
                        note_entry_rejection(mint, diagnostic)
                    if mint in stream_set and diagnostic in ("supported_pair_liquidity_unavailable", "supported_pair_missing_price", "no_sol_quoted_pair"):
                        curve_pending.append(mint)
                    continue
                # Record only markets re-verified from fresh pair data.
                was_verified = mint in _verified_dex_mints
                verified_market_this_scan += 1
                can_reenter, _, was_traded = paper_reentry_decision(state, mint)
                reentry_age_allowed = can_reenter and was_traded
                retain_fresh_verified_pool(mint, pair, allow_reentry_age=reentry_age_allowed)
                local_rejections: Counter = Counter()
                c = analyze_pair(pair, local_rejections, allow_reentry_age=reentry_age_allowed)
                if CANDIDATE_QUALITY_ENABLED:
                    _quality_ledger.note_verified(mint, score=c.get("score") if c else None)
                rejection_counts.update(local_rejections)
                if c is None and local_rejections:
                    reason = next(iter(local_rejections))
                    batch_rejected[mint] = reason
                    if (age_hours(pair)*60 >= MOMENTUM_MIN_PAIR_AGE_MINUTES
                            and is_verified_fast_nearmiss(pair, reason, sol_mint=SOL_MINT,
                              dexes=set(active_entry_routes()),
                              minimum_liquidity=active_entry_liquidity_min(),
                              min_cap=MOMENTUM_MARKET_CAP_MIN_USD,
                              max_cap=MOMENTUM_MARKET_CAP_MAX_USD)):
                        hot_nearmisses.add(mint)
                mark_fresh_checked(mint, "eligible" if c else "momentum_rejected")
                if MOMENTUM_SWING_MODE and not SNIPER_MODE:
                    if c is not None:
                        # Genuine momentum requalification clears the previous
                        # cooldown, never merely receiving another pool quote.
                        _entry_reject_until.pop(mint, None)
                        _entry_reject_reason.pop(mint, None)
                    elif local_rejections:
                        note_entry_rejection(mint, next(iter(local_rejections)))
                if c:
                    eligible_verified += int(was_verified)
                    eligible_search += int(mint in _discovery_priority)
                    c["pumpportal_pool"] = SUPPORTED_SOL_ROUTES.get(pair["dexId"].lower(), "paper_only_no_pumpportal_route")
                    if pair.get("liquidity_data_source"):
                        c["liquidity_data_source"] = pair["liquidity_data_source"]
                    market[mint] = c
        except DexRateLimited:
            break
        except Exception as exc:
            batch_lookup_failures += 1
            log.warning("Batch token lookup failed: %s", exc)

    if scan_entries and FRESH_BATCH_DISCOVERY_ENABLED:
        pipeline_result = _candidate_pipeline.record_scan(checked_mints, market, batch_rejected)
        if addresses:
            log.info("CANDIDATE CONVERSION checked=%d first_checks=%d market_passed=%d first_passes=%d by_source=%s near_watched=%d tracked=%d; market_pass_not_trade_approval=True",
                     len(checked_mints), pipeline_result['first_checks'], len(market),
                     pipeline_result['first_passes'], pipeline_result['by_source'],
                     pipeline_result['near_watched'], pipeline_result['records'])
        _candidate_pipeline.save()
        removed = 0
        near_retries = 0
        hot_retries = 0
        for mint, reason in batch_rejected.items():
            if mint in hot_nearmisses and _fresh_batch.schedule_hot_recheck(mint, reason, delay_seconds=90):
                hot_retries += 1
                removed += 1
                continue
            if (mint in _candidate_pipeline.near and
                    reason in ('too_new_for_established_strategy', 'market_cap_below_range')):
                if _fresh_batch.defer_blocked(mint, 'near_eligible_retry', delay_seconds=900):
                    near_retries += 1
                    removed += 1
            else:
                removed += int(_fresh_batch.discard(mint, reason))
        if hot_retries or (addresses and batch_rejected):
            log.info("PAPER FAST NEAR MISS checked=%d eligible_for_bounded_recheck=%d scheduled=%d cap_min=$500000 retry_s=90 max_attempts=2 no_entry_override=True",
                     len(checked_mints), len(hot_nearmisses), hot_retries)
        if near_retries:
            log.info("DISCOVERY NEAR RETRY deferred=%d min_retry_s=900 normal_entry_safety_unchanged=True", near_retries)
        current_stats = _fresh_batch.stats()
        if removed or addresses:
            log.info("FRESH BATCH RESULT checked=%d market_passed=%d discarded=%d active_remaining=%d unscanned=%d quarantined=%d next_refill=discovery_refresh",
                     len(checked_mints), len(market), removed, current_stats["active"],
                     current_stats["unscanned"], current_stats["quarantined"])
        _fresh_batch.save()

    # Read-only on-chain fallback for fresh launches. This supplies genuine
    # bonding-curve status/reserves, NOT a substitute DEX liquidity number.
    if scan_entries and PUMP_CURVE_LOOKUP_ENABLED and SOLANA_RPC_URL:
        for mint in dict.fromkeys(curve_pending):
            if curve_checks >= PUMP_CURVE_LOOKUPS_PER_SCAN:
                break
            curve_checks += 1
            try:
                info = lookup_curve(mint, SOLANA_RPC_URL)
                status = info["status"]
                curve_outcomes[status] += 1
                if status in ("active_bonding_curve", "graduated"):
                    if status == "active_bonding_curve":
                        note_active_curve(mint, info.get("real_sol_reserve"))
                    # A 0.1 SOL quote checks reserve arithmetic without treating
                    # virtual reserves as verified liquidity or enabling entries.
                    indicative = buy_quote(info, 0.1)
                    if indicative:
                        curve_quote_ready += 1
                        activity = _curve_activity.summary(mint)
                        # Trade activity is diagnostic only; for modern quote-token
                        # curves the event's quote amount isn't necessarily SOL.
                        # Never make entry eligibility depend on assumed quote units.
                        reserve_observation_ready = info["real_sol_reserve"] >= CURVE_MIN_REAL_SOL_FOR_OBSERVATION
                        curve_reserve_observation_ready += int(reserve_observation_ready)
                        observed_threshold = activity['trades'] >= CURVE_ACTIVITY_MIN_TRADES
                        curve_activity_observed_threshold_met += int(observed_threshold)
                        ready = False  # quote-mint/fee/exits not yet validated for curve entries
                        curve_activity_ready += int(ready)
                        log.info("CURVE ACTIVITY mint=%s source=solana_rpc trades5m=%d buys=%d sells=%d unverified_quote_amount_as_sol=%.4f observed_trade_threshold=%s reserve_observation_ready=%s activity_ready=%s entry=blocked_pending_execution_validation",
                                 mint, activity['trades'], activity['buys'], activity['sells'], activity['volume_sol'], observed_threshold, reserve_observation_ready, ready)
                        log.info("CURVE QUOTE mint=%s buy_sol=0.1 token_raw=%.0f impact=%.3f%% real_sol=%.4f entry=blocked_pending_execution_validation",
                                 mint, indicative["token_raw"], indicative["impact"] * 100, info["real_sol_reserve"])
                    else:
                        log.info("CURVE DATA mint=%s status=%s real_sol=%.4f quote=unavailable entry=blocked",
                                 mint, status, info["real_sol_reserve"])
                else:
                    log.info("CURVE DATA mint=%s status=%s entry=blocked", mint, status)
            except Exception as exc:
                curve_outcomes["lookup_error"] += 1
                log.warning("CURVE LOOKUP failed mint=%s error=%s", mint, type(exc).__name__)

    if scan_entries and CURVE_ACTIVITY_ENABLED:
        # Subscribe to a rotating bounded selection of fresh launches, including
        # mints awaiting their first DexScreener pair. This is observation only.
        # Watchlist is populated on launch arrival; never replace it with scan subset.
        pass
    if scan_entries:
        backlog_diag = curve_activity_diagnostics()
        log.info("OPPORTUNITY FUNNEL dex_checked=%d dex_eligible=%d dex_missing_liquidity=%d dex_unsupported_route=%d curve_checked=%d curve_active=%d curve_quotes=%d curve_executable=0 curve_reason=pending_execution_and_sellability_validation rpc_pending=%s rpc_history_gaps=%s secondary_waiting=%d secondary_due=%d",
                 evaluated, len(market), market_data_rejections.get("supported_pair_liquidity_unavailable", 0),
                 market_data_rejections.get("unsupported_sol_route", 0) + market_data_rejections.get("no_sol_quoted_pair", 0),
                 curve_checks, curve_outcomes.get("active_bonding_curve", 0), curve_quote_ready,
                 backlog_diag.get("pending_signatures"), backlog_diag.get("history_gaps"),
                 len(_secondary_deferred), len(secondary_due_mints()))
        log.info("LIQUIDITY SECONDARY probes=%d outcomes=%s max_per_minute=%d paper_only=True", secondary_probes, dict(secondary_outcomes), PAPER_SECONDARY_LIQUIDITY_MAX_PER_MINUTE)
        log.info("FILTER SUMMARY evaluated=%d eligible=%d pair_not_indexed=%d route_rejections=%s momentum_rejections=%s market_data_rejections=%s fresh_check_outcomes=%s curve_checks=%d curve_outcomes=%s curve_quote_ready=%d curve_reserve_observation_ready=%d curve_activity_observed_threshold_met=%d curve_activity_ready=%d curve_activity_source=solana_rpc verified_market_this_scan=%d eligible_verified=%d eligible_search=%d cooling_total=%d curve_trade_diagnostics=%s",
                 evaluated, len(market), missing_pairs, dict(route_rejections), dict(rejection_counts), dict(market_data_rejections), fresh_check_counts(), curve_checks, dict(curve_outcomes), curve_quote_ready, curve_reserve_observation_ready, curve_activity_observed_threshold_met, curve_activity_ready, verified_market_this_scan, eligible_verified, eligible_search, len(_entry_reject_until), curve_activity_diagnostics())
        if CANDIDATE_QUALITY_ENABLED:
            _quality_ledger.save()
            quality_stats = _quality_ledger.stats()
            log.info("CANDIDATE QUALITY records=%d quarantined=%d recent_verified_momentum=%d entry_skips=%d state=%s",
                     quality_stats["records"], quality_stats["quarantined"],
                     quality_stats["fresh_momentum"], quality_quarantine_skips, _quality_ledger.path)
        if batch_lookup_failures and not evaluated:
            scanner_status = "token_lookup_unavailable"
        elif not addresses and not evaluated:
            scanner_status = "no_candidates_selected_or_cooldown"
        elif not evaluated:
            scanner_status = "no_indexed_markets"
        elif market:
            scanner_status = "candidates_passed_filters_pending_entry_checks"
        elif market_data_rejections and sum(market_data_rejections.values()) >= evaluated * 0.7:
            scanner_status = "market_data_or_pool_route_blocked"
        else:
            scanner_status = "momentum_filters_no_match"
        # The funnel counts market-data eligibility only. No paper position is
        # authorized until independent same-pool sellability and trend checks.
        log.info("SCANNER HEALTH status=%s checked=%d accepted_market=%d api_batch_errors=%d missing_index=%d cooling=%d observed_candidates=%d; market_pass_does_not_equal_paper_buy",
                 scanner_status, evaluated, len(market), batch_lookup_failures,
                 missing_pairs, len(_entry_reject_until), len(addresses))

    # Held positions must follow the IDENTICAL pool used at entry, never the
    # deepest or currently trending pool for the same token mint.
    for mint, raw in state["positions"].items():
        pool_id = raw.get("entry_pool_address") or ""
        if pool_id and mint in market and market[mint].get("pool_address") != pool_id:
            market.pop(mint, None)

    # Gather independently checked primary-pool evidence for conservative
    # *accounting write-offs*, never to claim that a sell was executed.
    exit_evidence: dict[str, dict] = {}
    # Fetch quotes for held positions separately from candidate screening.
    # A held token may fail entry filters (e.g. momentum/volume) but still need
    # to be priced so exits and portfolio valuation can be managed.
    for mint in list(state["positions"].keys()):
        if mint in market:
            continue
        _token_pairs_healthy.pop(mint, None)
        pairs = token_pairs(mint)
        evidence = pinned_pool_liquidity_evidence(Position(**state["positions"][mint]), pairs,
                                                  primary_healthy=_token_pairs_healthy.get(mint, False))
        exit_evidence[mint] = evidence
        if not evidence.get("verified"):
            state.get("exit_issues", {}).get(mint, {}).pop("rug_confirmed_checks", None)
        eligible_quote_pairs = [p for p in pairs if p.get("chainId") == "solana"
                                and (p.get("quoteToken") or {}).get("address") == SOL_MINT
                                and (p.get("dexId") or "").lower() in active_entry_routes()]
        pinned = state["positions"][mint].get("entry_pool_address") or ""
        pair = (next((p for p in eligible_quote_pairs if p.get("pairAddress") == pinned), None)
                if pinned else best_pair(eligible_quote_pairs))
        if pair:
            price = as_float(pair.get("priceUsd"))
            liquidity = as_float((pair.get("liquidity") or {}).get("usd"))
            source = "dexscreener"
            if price > 0 and (pair.get("liquidity") or {}).get("usd") is None and PAPER_SECONDARY_LIQUIDITY_ENABLED:
                # Held positions also need verified depth for PAPER exits. An
                # independent stale price alone must never trigger a paper sale.
                recovered, outcome = independently_verify_missing_liquidity(pair)
                if recovered is not None:
                    liquidity = as_float(recovered["liquidity"]["usd"])
                    source = "dexscreener_price_geckoterminal_depth_indicative"
                else:
                    log.info("PAPER EXIT SECONDARY DEPTH UNAVAILABLE mint=%s reason=%s; no fabricated exit", mint, outcome)
            if price > 0:
                market[mint] = {"price": price, "liquidity": liquidity,
                                "quote_source": source, "pool_address": pair.get("pairAddress", "")}

    # If DexScreener has dropped the precise entry pool, check an independent
    # GeckoTerminal pool endpoint. Do not infer a sell price from another pool.
    # API readings remain INDICATIVE, not verified executable swaps.
    for mint, raw in state["positions"].items():
        if not raw.get("entry_pool_address") or (mint in market and
            as_float(market[mint].get("price")) > 0 and
            as_float(market[mint].get("liquidity")) > 0):
            continue
        fallback = held_pool_fallback(raw["entry_pool_address"], mint)
        if fallback:
            market[mint] = fallback
            fallback_logged = state.setdefault("fallback_log_times", {})
            now_log = time.time()
            if now_log - float(fallback_logged.get(mint, 0)) >= PAPER_FALLBACK_LOG_INTERVAL_SECONDS:
                log.warning("PAPER POOL FALLBACK mint=%s pool=%s source=geckoterminal valuation_only=no_verified_exit",
                            mint, raw["entry_pool_address"])
                fallback_logged[mint] = now_log

    manage_positions(state, market, exit_evidence)
    roll_day_if_needed(state)
    if should_halt(state):
        save_state(state)
        return

    # Price-only quotes for held positions have no score and are not entry candidates.
    candidates = sorted(
        (c for c in market.values()
         if isinstance(c.get("score"), (int, float))
         and isinstance(c.get("address"), str)),
        key=lambda c: c["score"],
        reverse=True,
    )
    skipped_entries: Counter = Counter()
    # Manage positions BEFORE this guard: positions may be sold in the current
    # cycle, and the newly freed slot must be usable immediately. A portfolio
    # that remains full cannot place any paper buy, so skip expensive secondary
    # pool/sellability verification and trend-confirmation entry work entirely.
    # Do NOT bypass market discovery, candidate ranking or held-position exits.
    if PAPER_MODE and len(state["positions"]) >= MAX_POSITIONS:
        deferred = sum(c["address"] not in state["positions"] for c in candidates)
        if scan_entries:
            log.info("PAPER ENTRY CAPACITY reason=position_limit positions=%d max=%d "
                     "held_candidates=%d deferred_candidates=%d "
                     "prebuy_checks_skipped=%d discovery_active=True exits_active=True "
                     "future_entry_requires_fresh_market_check=True",
                     len(state["positions"]), MAX_POSITIONS,
                     len(candidates) - deferred, deferred, deferred)
        # Pending confirmation snapshots must not survive indefinitely while
        # no capacity exists; new entry candidates get a new quote-based check.
        _pending_entry_mints.clear()
        save_state(state)
        log.info("Equity≈$%.2f cash=$%.2f positions=%d candidates=%d",
                 equity(state), state["cash_usd"], len(state["positions"]), len(market))
        return
    if PAPER_FAST_ONLY and MOMENTUM_SWING_MODE:
        candidates.sort(key=lambda c: (as_float(c.get("fast_mover_rank")), as_float(c.get("score"))), reverse=True)
        if candidates:
            log.info("PAPER FAST CANDIDATES selected=%d top=%s fast_m5_min=1.0%% activity=volume_liquidity_hour_pace buy_pressure=verified paper_only=True",
                     len(candidates), [(c.get("symbol"),c.get("fast_mover_rank")) for c in candidates[:5]])
    for c in candidates:
        if c["address"] in state["positions"]:
            skipped_entries["already_held"] += 1
            log.info("PAPER ENTRY SKIP %s reason=already_held", c["symbol"])
            continue
        allowed, reentry_reason, is_reentry = paper_reentry_decision(state, c["address"])
        if not allowed:
            _pending_entry_mints.pop(c["address"], None)
            skipped_entries[reentry_reason.split(":", 1)[0]] += 1
            log.info("PAPER ENTRY SKIP %s reason=%s", c["symbol"], reentry_reason)
            continue
        profile, reason = paper_entry_profile(state, c["score"])
        if profile is None:
            _pending_entry_mints.pop(c["address"], None)
            skipped_entries[reason] += 1
            log.info("PAPER ENTRY SKIP %s reason=%s score=%.1f standard_threshold=%.1f exploration_floor=%.1f", c["symbol"], reason, c["score"], MIN_SCORE, PAPER_EXPLORATION_MIN_SCORE)
            continue
        if profile == "exploration":
            log.info("PAPER EXPLORATION SCREEN %s score=%.1f floor=%.1f standard=%.1f fills_today=%d/%d size=$%.2f (no live order)",
                     c["symbol"], c["score"], PAPER_EXPLORATION_MIN_SCORE, MIN_SCORE,
                     state.get("paper_exploration_fills_today", 0), PAPER_EXPLORATION_DAILY_LIMIT, PAPER_EXPLORATION_POSITION_SIZE_USD)
        if MOMENTUM_SWING_MODE and PAPER_COST_ENTRY_ENABLED:
            cost_ok, cost_diag = entry_cost_headroom(
                c, round_trip_cost_pct=ESTIMATED_ROUND_TRIP_COST * 100,
                minimum_buffer_pct=PAPER_COST_ENTRY_BUFFER_PCT,
                minimum_5m_change_pct=PAPER_COST_ENTRY_MIN_5M_PCT)
            if not cost_ok:
                _pending_entry_mints.pop(c["address"], None)
                skipped_entries["entry_cost_headroom"] += 1
                log.info("PAPER ENTRY SKIP %s reason=entry_cost_headroom detail=%s h1=%.2f%% m5=%.2f%% cost=%.2f%% required_h1=%.2f%%",
                         c["symbol"], cost_diag["reason"], cost_diag["h1_pct"],
                         cost_diag["m5_pct"], cost_diag["cost_pct"], cost_diag["required_h1_pct"])
                continue
        flow_buys = as_float(c.get("m5_buys"))
        flow_sells = as_float(c.get("m5_sells"))
        flow = flow_buys / (flow_buys+flow_sells) if flow_buys+flow_sells > 0 else None
        confirmed, trend_detail = candidate_momentum_confirm(c["address"], as_float(c.get("price")), buy_pressure=flow, force_confirmation=is_reentry)
        if not confirmed:
            watch_pending_entry(c["address"], trend_detail)
            skipped_entries["entry_short_trend"] += 1
            log.info("PAPER ENTRY SKIP %s reason=entry_short_trend detail=%s", c["symbol"], trend_detail)
            continue
        _pending_entry_mints.pop(c["address"], None)
        if CANDIDATE_QUALITY_ENABLED:
            log.info("PAPER ENTRY TREND CONFIRMED %s mint=%s detail=%s score=%.1f fresh_observation_required=True",
                     c["symbol"], c["address"], trend_detail, c["score"])
        if (SNIPER_MODE or MOMENTUM_SWING_MODE) and PAPER_MODE and PAPER_PREBUY_SELL_CHECK_ENABLED:
            ok, reason = paper_prebuy_sellability(c)
            if not ok:
                skipped_entries["prebuy_sellability"] += 1
                log.info("PAPER ENTRY REJECT %s mint=%s reason=%s", c["symbol"], c["address"], reason)
                continue
            log.info("PAPER SELLABILITY CHECK %s pool=%s result=indicative_only", c["symbol"], c.get("pool_address"))
            if PAPER_FINAL_SNAPSHOT_ENABLED:
                raw_fresh = c.pop("_prebuy_verified_pair", None)
                checked_at = c.pop("_prebuy_verified_at", 0)
                # A true final check must still pass the complete current market
                # filters; a successful indicative sellability test isn't enough.
                fresh = analyze_pair(raw_fresh) if isinstance(raw_fresh, dict) else None
                if fresh is None:
                    skipped_entries["final_snapshot"] += 1
                    log.info("PAPER ENTRY SKIP %s reason=final_snapshot detail=latest_momentum_or_market_filter_failed", c["symbol"])
                    continue
                fresh_ok, fresh_diag = fresh_pool_snapshot(
                    c, fresh, elapsed_s=time.time() - checked_at,
                    max_age_s=PAPER_FINAL_SNAPSHOT_MAX_AGE_SECONDS,
                    max_drop_pct=PAPER_FINAL_SNAPSHOT_MAX_DROP_PCT,
                    max_rise_pct=PAPER_FINAL_SNAPSHOT_MAX_RISE_PCT)
                if not fresh_ok:
                    skipped_entries["final_snapshot"] += 1
                    log.info("PAPER ENTRY SKIP %s reason=final_snapshot detail=%s drift_pct=%s age_s=%s",
                             c["symbol"], fresh_diag["reason"], fresh_diag.get("drift_pct"), fresh_diag.get("age_s"))
                    continue
                if PAPER_COST_ENTRY_ENABLED and MOMENTUM_SWING_MODE:
                    new_cost_ok, new_cost_diag = entry_cost_headroom(
                        fresh, round_trip_cost_pct=ESTIMATED_ROUND_TRIP_COST * 100,
                        minimum_buffer_pct=PAPER_COST_ENTRY_BUFFER_PCT,
                        minimum_5m_change_pct=PAPER_COST_ENTRY_MIN_5M_PCT)
                    if not new_cost_ok:
                        skipped_entries["final_cost_headroom"] += 1
                        log.info("PAPER ENTRY SKIP %s reason=final_cost_headroom detail=%s", c["symbol"], new_cost_diag["reason"])
                        continue
                previous_price = c["price"]
                c.update(fresh)  # Includes the final pool price used by paper_buy.
                log.info("PAPER ENTRY FINAL SNAPSHOT %s price=%g previous=%g drift=%.3f%% age_s=%.2f "
                         "signal_headroom_h1=%.2f%% cost_assumption=%.2f%% source=dexscreener_provider_snapshot indicative_only=True",
                         c["symbol"], c["price"], previous_price, fresh_diag["drift_pct"],
                         fresh_diag["age_s"], c["change_h1"] - ESTIMATED_ROUND_TRIP_COST * 100,
                         ESTIMATED_ROUND_TRIP_COST * 100)
        if is_reentry:
            log.info("PAPER REENTRY APPROVED %s reason=%s score=%.1f second_price_confirmed=True", c["symbol"], reentry_reason, c["score"])
        log.info("CANDIDATE %s score=%.1f liq=$%.0f vol5m=$%.0f age=%.1fh cap=$%.0f source=%s trend5m=%.1f%% trend1h=%.1f%% trend6h=%.1f%% trend24h=%.1f%% %s",
                 c["symbol"], c["score"], c["liquidity"], c["m5_volume"], c["age_hours"],
                 c.get("market_cap_usd") or 0, c.get("market_cap_source", "unknown"), c["change_m5"], c["change_h1"],
                 c.get("change_h6", 0), c.get("change_h24", 0), c["url"])
        if PAPER_MODE:
            is_small_cap = (MOMENTUM_SWING_MODE and
                            0 < as_float(c.get("market_cap_usd")) < SMALL_CAP_TIER_MAX_USD)
            is_young = MOMENTUM_SWING_MODE and 30 <= as_float(c.get("age_hours"))*60 < 60
            if (is_small_cap or is_young) and any(
                    (0 < as_float(state.get("paper_trade_metrics", {}).get(m, {}).get("entry_market_cap_usd")) < SMALL_CAP_TIER_MAX_USD
                     or 30 <= as_float(state.get("paper_trade_metrics", {}).get(m, {}).get("entry_age_minutes"))*1 < 60)
                    for m in state["positions"]):
                skipped_entries["small_cap_position_limit"] += 1
                log.info("PAPER ENTRY SKIP %s reason=small_cap_position_limit maximum_open_small_cap=1", c["symbol"])
                continue
            proposed_size = (PAPER_EXPLORATION_POSITION_SIZE_USD if profile == "exploration" else None)
            if is_small_cap or is_young:
                proposed_size = min(SMALL_CAP_PAPER_SIZE_USD, proposed_size) if proposed_size is not None else SMALL_CAP_PAPER_SIZE_USD
                log.info("PAPER SMALL CAP SIZING %s cap=$%.0f age_minutes=%.1f size=$%.2f tier_under=$%.0f max_concurrent_speculative=1 paper_only=True",
                         c["symbol"], c["market_cap_usd"], c["age_hours"]*60, proposed_size, SMALL_CAP_TIER_MAX_USD)
            before = len(state["positions"])
            paper_buy(state, c, size_usd=proposed_size,
                      entry_profile=("small_cap" if is_small_cap and profile == "standard" else profile))
            if len(state["positions"]) > before and profile == "exploration":
                state["paper_exploration_fills_today"] = state.get("paper_exploration_fills_today", 0) + 1
                log.info("PAPER EXPLORATION BUY %s small_paper_only=$%.2f daily_count=%d/%d",
                         c["symbol"], state["positions"][c["address"]]["amount_usd"],
                         state["paper_exploration_fills_today"], PAPER_EXPLORATION_DAILY_LIMIT)
            if len(state["positions"]) == before:
                skipped_entries["buy_not_placed"] += 1
                log.info("PAPER ENTRY SKIP %s reason=buy_not_placed cash=%.2f slots=%d", c["symbol"], state["cash_usd"], MAX_POSITIONS - before)
        else:
            # Live execution is deliberately fail-closed until a tested swap adapter
            # is installed. Never silently pretend a live order was sent.
            log.error("LIVE MODE REQUESTED BUT NO VERIFIED EXECUTION ADAPTER IS CONFIGURED. No order sent.")
            state["halted"] = True
            break
        if len(state["positions"]) >= MAX_POSITIONS:
            skipped_entries["position_limit_reached"] += max(0, len(candidates) - candidates.index(c) - 1)
            log.info("PAPER ENTRY CAPACITY positions=%d max=%d remaining_candidates=%d", len(state["positions"]), MAX_POSITIONS, len(candidates) - candidates.index(c) - 1)
            break
    if candidates or _pending_entry_mints:
        log.info("PAPER ENTRY DECISIONS screened=%d skips=%s pending_recheck=%d pending_due=%d paper_threshold=%.1f exploration_enabled=%s exploration_floor=%.1f exploration_fills=%d/%d", len(candidates), dict(skipped_entries), len(_pending_entry_mints), len(pending_entry_due()), MIN_SCORE, PAPER_EXPLORATION_ENABLED, PAPER_EXPLORATION_MIN_SCORE, state.get("paper_exploration_fills_today", 0), PAPER_EXPLORATION_DAILY_LIMIT)
    save_state(state)
    log.info("Equity≈$%.2f cash=$%.2f positions=%d candidates=%d",
             equity(state), state["cash_usd"], len(state["positions"]), len(market))


def main() -> None:
    if MOMENTUM_SWING_MODE:
        log.info("CANDIDATE PIPELINE v7.5 cap_floor=$%.0f small_cap_tier=under_$%.0f extra_liquidity_volume_flow_and_verified_cap=True near_watch=observation_only", MOMENTUM_MARKET_CAP_MIN_USD, SMALL_CAP_TIER_MAX_USD)
        log.info("ESTABLISHED MOMENTUM PAPER MODE cap=$%.0f-$%.0f min_age=%.0fm max_age=%s min_liq=$%.0f trend5m>=%.1f%% 1h>=%.1f%% 6h>=%.1f%% 24h>=%.1f%% dexes=%s; all trades INDICATIVE paper simulations, not executable swap quotes",
                 MOMENTUM_MARKET_CAP_MIN_USD, MOMENTUM_MARKET_CAP_MAX_USD,
                 MOMENTUM_MIN_PAIR_AGE_MINUTES, MOMENTUM_MAX_PAIR_AGE_HOURS or "unlimited",
                 active_entry_liquidity_min(), MOMENTUM_MIN_CHANGE_5M_PCT, MOMENTUM_MIN_CHANGE_1H_PCT,
                 MOMENTUM_MIN_CHANGE_6H_PCT, MOMENTUM_MIN_CHANGE_24H_PCT, sorted(active_entry_routes()))
    if PAPER_FAST_ONLY:
        log.info("PAPER FAST V7.6 active=True min_m5=1.0%% min_h1=5.0%% min_vol5m=$1800 min_volume_liquidity=2.5%% min_flow=1.18 net_take_profit=%.1f%% trail_arm=%.1f%% trail_giveback=%.1f%% loss_circuit=enabled only_verified_primary=True",
                 PAPER_FAST_TP_NET_PCT, PAPER_PROFIT_TRAIL_ARM_NET_PCT, PAPER_PROFIT_TRAIL_GIVEBACK_PCT)
    if SNIPER_MODE:
        log.info("SNIPER MODE DEX paper only: max_age=%.0fm volume5m>=$%.0f buys/sells>=%.2f price5m>=%.1f%% estimated_entry_impact<=%.1f%%; bonding-curve entries blocked",
                 SNIPER_MAX_AGE_MINUTES, SNIPER_MIN_VOLUME_5M_USD, SNIPER_MIN_BUY_SELL_RATIO, SNIPER_MIN_PRICE_CHANGE_5M_PCT, SNIPER_MAX_ENTRY_IMPACT_PCT)
    # Explicit import verifies the strategy and execution bridge are deployed together.
    # Live transaction calls remain disabled until recovery and reconciliation are reviewed.
    from live_execution import execute_buy, execute_sell  # noqa: F401
    if os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true":
        log.critical("LIVE_TRADING_ENABLED must remain false for this staged strategy release. Refusing to start.")
        raise SystemExit(2)
    if not PAPER_MODE:
        log.critical("PAPER_MODE=false, but this release has no live swap signer. Refusing to trade live.")
        log.critical("Set PAPER_MODE=true. Live mode requires a separately reviewed Jupiter swap adapter.")
        raise SystemExit(2)
    state = load_state()
    start_fresh_launch_feed()
    if not FRESH_LAUNCH_FEED_ENABLED and not MOMENTUM_SWING_MODE:
        log.warning("FRESH FEED disabled by configuration: FRESH_LAUNCH_FEED_ENABLED=false; new launches only from polling sources")
    start_curve_activity()
    log.info("Paper state file: %s (existing state, if present, takes precedence over starting capital)", STATE_FILE)
    log_real_wallet_balance()
    log.info("Starting in PAPER MODE. Capital=$%.2f; position size=$%.2f; max positions=%d; stop-loss=%.0f%%; take-profit=%.0f%%; trailing stop=%s; min score=%.1f",
             STARTING_CAPITAL, POSITION_SIZE_USD, MAX_POSITIONS, STOP_LOSS * 100, TAKE_PROFIT * 100,
             (f"enabled ({TRAILING_STOP:.0%})" if TRAILING_STOP_ENABLED else "disabled"), MIN_SCORE)
    log.info("This is not a live trading bot. No wallet key is read and no transaction is signed.")
    last_wallet_check = time.monotonic()
    while True:
        try:
            if time.monotonic() - last_wallet_check >= 300:
                log_real_wallet_balance()
                last_wallet_check = time.monotonic()
            scan_once(state)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            save_state(state)
            break
        except Exception:
            log.exception("Unexpected scan error; will retry next cycle.")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()

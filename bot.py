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
from pumpfun_boards import board_mints, safe_board_shape, valid_pumpfun_endpoint, MOVERS_URL, TRENDING_PROXY_URL
from fomo_boards import extract_fomo_mints, inspect_fomo_board, network_distribution, BASE_URL as FOMO_API_BASE_URL
from live_execution import execute_buy, execute_sell
print("EXECUTION BRIDGE LOADED - preview only, live trading disabled", flush=True)

import requests
from dotenv import load_dotenv
from early_watch_v75 import from_dex_pairs as early_dex_pairs, from_gecko_pools as early_gecko_pools
from fast_momentum_v76 import fast_mover_gate, confirmed_profit_trail, confirmed_loss_circuit
from recheck_recovery_v79 import followup_decision
from fast_liquid_recheck_v85 import short_flow_nearmiss
from shadow_pool_discovery_v861 import early_pool_hints
from shadow_age_tracks_v876 import BackgroundAgeStudy
from research_risk_v893 import ResearchRiskGate
from micro_rug_guard_v895 import MicroRugGuard
from quote_observer_v893 import QuoteObserver
from solana_age_ws_v877 import SolanaPoolWatcher, ws_url_for_rpc
from early_watch_shadow_v80 import (threshold_watch, EarlyShadow, pool_evidence, shadow_entry_evidence,
    shadow_exit_signal, shadow_entry_diagnostic, SHADOW_MIN_LIQUIDITY_USD, SHADOW_ESTIMATED_ROUND_TRIP_COST_PCT,
    SHADOW_SAMPLE_INTERVAL_SECONDS, SHADOW_MIN_AGE_MINUTES, SHADOW_MAX_AGE_MINUTES)
from same_pool_trend_v80 import gecko_same_pool_observation
from shadow_risk_v863 import (prebuy_depth_check, pool_risk_signal, classify_unavailable_exit, liquidity_trade_breakdown)
from fast_discovery_v82 import SharedGeckoBudget, primary_m5_activity
from launch_pool_followup_v83 import followup_delay, POOL_WAIT_REASONS, MAX_POOL_RECHECKS
from read_only_exit_v872 import verify_paper_exit_route, pump_pool_snapshot, pinned_sell_quote
from microcap_strategy_v81 import (early_volume_exception, confirmed_profit_lock,
                                    update_exit_shadows, summarize_version_trades)

load_dotenv()

# ---- Configuration ----
STARTING_CAPITAL = float(os.getenv("STARTING_CAPITAL_USD", "36"))
PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() == "true"
# v8.6: exclusive new-entry and new-position exit strategy. Old saved
# positions are never silently converted to the new rules.
PAPER_SHADOW_ACTIVE = PAPER_MODE and os.getenv("PAPER_SHADOW_ACTIVE", "false").lower() == "true"
SHADOW_AGE_STUDY_ENABLED = PAPER_SHADOW_ACTIVE and os.getenv("SHADOW_AGE_STUDY_ENABLED", "false").lower() == "true"
# Promoted track retains v8.7.6 entry/exit thresholds. At the user's request,
# automatic LOSS halts are disabled for 0_60 only; historical losses remain.
LIVE_0_60_ENABLED = os.getenv('LIVE_0_60_ENABLED','false').lower() == 'true'
AGE_0_60_PRIMARY = PAPER_MODE and SHADOW_AGE_STUDY_ENABLED and os.getenv('AGE_0_60_PRIMARY', 'false').lower() == 'true'
AGE_0_60_WS_ENABLED = AGE_0_60_PRIMARY and os.getenv('AGE_0_60_WS_ENABLED', 'false').lower() == 'true'
AGE_0_60_WS_RPC_URL = os.getenv('AGE_0_60_WS_RPC_URL', '').strip()
# v8.9.3 instruments paper-only independent research portfolios.
V893_RESEARCH_ENABLED = SHADOW_AGE_STUDY_ENABLED and PAPER_MODE and os.getenv('SHADOW_V893_RESEARCH_ENABLED','true').lower() == 'true'
V893_ADAPTIVE_SCAN = PAPER_SHADOW_ACTIVE and PAPER_MODE and os.getenv('SHADOW_V893_ADAPTIVE_SCAN','true').lower() == 'true'
SHADOW_AGE_STUDY_FETCH_PER_SCAN = max(1,min(6,int(os.getenv("SHADOW_AGE_STUDY_FETCH_PER_SCAN", "3"))))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_SECONDS", "5"))
MAX_POSITIONS = max(1, min(10, int(os.getenv("MAX_POSITIONS", "5"))))
MAX_SHADOW_POSITIONS = max(1, min(MAX_POSITIONS, int(os.getenv("MAX_SHADOW_POSITIONS", "5"))))
SHADOW_LIQUIDITY_MONITOR_SECONDS = max(10., float(os.getenv("SHADOW_LIQUIDITY_MONITOR_SECONDS", "20")))
SHADOW_LIQUIDITY_EXIT_DROP_PCT = max(20., float(os.getenv("SHADOW_LIQUIDITY_EXIT_DROP_PCT", "40")))
SHADOW_REQUIRE_INDEPENDENT_POOL = os.getenv("SHADOW_REQUIRE_INDEPENDENT_POOL", "false").lower() == "true"
# v8.7 PAPER-ONLY protections. clean_boot enables these as a pinned profile;
# direct module users retain legacy defaults for controlled comparison.
SHADOW_SAFETY_PREBUY_VERIFICATION = os.getenv("SHADOW_SAFETY_PREBUY_VERIFICATION", "false").lower() == "true"
SHADOW_REQUIRE_READONLY_EXIT_ROUTE = os.getenv("SHADOW_REQUIRE_READONLY_EXIT_ROUTE", "false").lower() == "true"
SHADOW_PREBUY_MAX_PRICE_DRIFT_PCT = max(0., float(os.getenv("SHADOW_PREBUY_MAX_PRICE_DRIFT_PCT", "8")))
SHADOW_PREBUY_MIN_LIQ_RATIO = max(0.1, min(1., float(os.getenv("SHADOW_PREBUY_MIN_LIQ_RATIO", "0.6"))))
SHADOW_PREBUY_MAX_LIQ_RATIO = max(1., float(os.getenv("SHADOW_PREBUY_MAX_LIQ_RATIO", "2.0")))
SHADOW_PREBUY_MAX_IMPACT_PCT = max(0.1, float(os.getenv("SHADOW_PREBUY_MAX_IMPACT_PCT", "2")))
SHADOW_PROTECTIVE_EXIT_ENABLED = os.getenv("SHADOW_PROTECTIVE_EXIT_ENABLED", "false").lower() == "true"
SHADOW_PROTECTIVE_NET_STOP_PCT = min(-1., float(os.getenv("SHADOW_PROTECTIVE_NET_STOP_PCT", "-9")))
SHADOW_PROTECTIVE_PEAK_ARM_PCT = max(1., float(os.getenv("SHADOW_PROTECTIVE_PEAK_ARM_PCT", "12")))
SHADOW_PROTECTIVE_PEAK_DROP_PCT = max(1., float(os.getenv("SHADOW_PROTECTIVE_PEAK_DROP_PCT", "7")))
SHADOW_WRITEOFF_CIRCUIT_ENABLED = os.getenv("SHADOW_WRITEOFF_CIRCUIT_ENABLED", "false").lower() == "true"
SHADOW_MAX_DAILY_WRITEOFFS = max(1, int(os.getenv("SHADOW_MAX_DAILY_WRITEOFFS", "1")))
# v8.7.4 paper-only EXIT rules. The entry strategy is unchanged.
SHADOW_EXIT_ROUTE_QUOTE_REQUIRED = os.getenv("SHADOW_EXIT_ROUTE_QUOTE_REQUIRED", "false").lower() == "true"
SHADOW_EXIT_ROUTE_RETRY_SECONDS = max(5.0, float(os.getenv("SHADOW_EXIT_ROUTE_RETRY_SECONDS", "12")))
SHADOW_EXIT_UNAVAILABLE_HALT_SECONDS = max(30.0, float(os.getenv("SHADOW_EXIT_UNAVAILABLE_HALT_SECONDS", "90")))
SHADOW_SEVERE_POSITION_LOSS_HALT_PCT = max(20., float(os.getenv("SHADOW_SEVERE_POSITION_LOSS_HALT_PCT", "50")))
SHADOW_CATASTROPHIC_CLOSED_LOSS_HALT_PCT = max(20., float(os.getenv("SHADOW_CATASTROPHIC_CLOSED_LOSS_HALT_PCT", "50")))
# v8.7.5: Extra PAPER-only risk controls, layered AFTER the unchanged original
# shadow signal. These are experiments; no observed backtest establishes efficacy.
SHADOW_UNHEALTHY_ENTRY_GUARD = os.getenv('SHADOW_UNHEALTHY_ENTRY_GUARD', 'false').lower() == 'true'
SHADOW_SURGE_CHANGE_5M_PCT = max(5., float(os.getenv('SHADOW_SURGE_CHANGE_5M_PCT', '13.5')))
SHADOW_SURGE_MIN_5M_VOLUME_USD = max(1400., float(os.getenv('SHADOW_SURGE_MIN_5M_VOLUME_USD', '2500')))
SHADOW_ROUTE_MONITOR_ENABLED = os.getenv('SHADOW_ROUTE_MONITOR_ENABLED', 'false').lower() == 'true'
SHADOW_ROUTE_MONITOR_INTERVAL_SECONDS = max(10., float(os.getenv('SHADOW_ROUTE_MONITOR_INTERVAL_SECONDS', '15')))
SHADOW_ROUTE_MONITOR_MAX_PER_MINUTE = max(1, min(30, int(os.getenv('SHADOW_ROUTE_MONITOR_MAX_PER_MINUTE', '12'))))
SHADOW_ROUTE_MONITOR_NET_STOP_PCT = min(-1., float(os.getenv('SHADOW_ROUTE_MONITOR_NET_STOP_PCT', '-9')))
_shadow_route_probe_times: list[float] = []
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
MOMENTUM_MARKET_CAP_MIN_USD = max(0.0, float(os.getenv("MOMENTUM_MARKET_CAP_MIN_USD", "35000")))
SMALL_CAP_TIER_MAX_USD = 500000.0  # extra paper gates below $500K; stricter below $250K
SMALL_CAP_PAPER_SIZE_USD = 5.0  # paper speculative exposure cap
MICRO_35K_PAPER_SIZE_USD = 2.0  # strictly smaller paper size below $100k
MOMENTUM_MARKET_CAP_MAX_USD = max(MOMENTUM_MARKET_CAP_MIN_USD, float(os.getenv("MOMENTUM_MARKET_CAP_MAX_USD", "250000000")))
MOMENTUM_ALLOW_FDV_FALLBACK = os.getenv("MOMENTUM_ALLOW_FDV_FALLBACK", "true").lower() == "true"
MOMENTUM_MIN_LIQUIDITY_USD = max(5000.0, float(os.getenv("MOMENTUM_MIN_LIQUIDITY_USD", "15000")))
MOMENTUM_MIN_PAIR_AGE_MINUTES = max(0.0, float(os.getenv("MOMENTUM_MIN_PAIR_AGE_MINUTES", "12")))
# Zero permits any age, including months and years old.
MOMENTUM_MAX_PAIR_AGE_HOURS = max(0.0, float(os.getenv("MOMENTUM_MAX_PAIR_AGE_HOURS", "0")))
MOMENTUM_MIN_VOLUME_1H_USD = max(0.0, float(os.getenv("MOMENTUM_MIN_VOLUME_1H_USD", "10000")))
MOMENTUM_MIN_CHANGE_1H_PCT = float(os.getenv("MOMENTUM_MIN_CHANGE_1H_PCT", "1.5"))
MOMENTUM_MIN_CHANGE_6H_PCT = float(os.getenv("MOMENTUM_MIN_CHANGE_6H_PCT", "0"))
MOMENTUM_MIN_CHANGE_24H_PCT = float(os.getenv("MOMENTUM_MIN_CHANGE_24H_PCT", "-1"))
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
PAPER_NO_1H_REQUIREMENT = PAPER_MODE and os.getenv("PAPER_NO_1H_REQUIREMENT", "false").lower() == "true"
PAPER_FAST_ONLY = PAPER_MODE and MOMENTUM_SWING_MODE and os.getenv("PAPER_FAST_ONLY", "true").lower() == "true"
PAPER_FAST_TP_NET_PCT = max(5.0, float(os.getenv("PAPER_FAST_TP_NET_PCT", "8")))
PAPER_PROFIT_TRAIL_ENABLED = PAPER_MODE and os.getenv("PAPER_PROFIT_TRAIL_ENABLED", "true").lower() == "true"
PAPER_PROFIT_TRAIL_ARM_NET_PCT = max(4.0, float(os.getenv("PAPER_PROFIT_TRAIL_ARM_NET_PCT", "5")))
PAPER_PROFIT_TRAIL_GIVEBACK_PCT = max(1.0, float(os.getenv("PAPER_PROFIT_TRAIL_GIVEBACK_PCT", "2.5")))
PAPER_LOSS_CIRCUIT_ENABLED = PAPER_MODE and os.getenv("PAPER_LOSS_CIRCUIT_ENABLED", "true").lower() == "true"
PAPER_PROFIT_LOCK_V81 = PAPER_MODE and os.getenv("PAPER_PROFIT_LOCK_V81", "true").lower() == "true"
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
PAPER_COST_ENTRY_BUFFER_PCT = max(0.0, float(os.getenv("PAPER_COST_ENTRY_BUFFER_PCT", "1.5")))
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
FRESH_LAUNCH_FEED_ENABLED = PAPER_MODE and os.getenv("FRESH_LAUNCH_FEED_ENABLED", "false").lower() == "true"
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
PUMPFUN_TRENDING_ENABLED = os.getenv("PUMPFUN_TRENDING_ENABLED", "false").lower() == "true"
PUMPFUN_BOARD_JWT = os.getenv("PUMPFUN_BOARD_JWT", "").strip()
PUMPFUN_BOARD_SESSION_ID = os.getenv("PUMPFUN_BOARD_SESSION_ID", "").strip()
PUMPFUN_BOARD_USER_ID = os.getenv("PUMPFUN_BOARD_USER_ID", "").strip()
PUMPFUN_BOARD_TIER = os.getenv("PUMPFUN_BOARD_TIER", "web").strip()
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
_early_shadow_v80 = EarlyShadow(STATE_FILE.with_name("early_shadow_v80_state.json"))
# v8.9.7: only throughput budgets change here; mint/LP/holder entry
# authorization still fails closed. Provider-specific limits are configurable.
_micro_v895_rug = MicroRugGuard(SOLANA_RPC_URL,
    max_pool_checks_per_minute=max(1,min(60,int(os.getenv('MICRO_POOL_CHECKS_PER_MINUTE','24'))))) if V893_RESEARCH_ENABLED else None
_v893_risk = ResearchRiskGate(rpc_url=SOLANA_RPC_URL,
    strict=os.getenv('SHADOW_V893_REQUIRE_RPC_RISK','true').lower()=='true',
    max_rpc_per_minute=max(1,min(30,int(os.getenv('RESEARCH_MINT_CHECKS_PER_MINUTE','12')))),
    micro_rug_guard=_micro_v895_rug) if V893_RESEARCH_ENABLED else None
_v893_quotes = QuoteObserver(api_key=JUPITER_API_KEY,
    max_calls_per_minute=max(1,min(30,int(os.getenv('RESEARCH_JUPITER_ROUNDTRIPS_PER_MINUTE','10')))),
    micro_join_wait_seconds=max(0.0,min(4.0,float(os.getenv('MICRO_INLINE_QUOTE_WAIT_SECONDS','3.5'))))) if V893_RESEARCH_ENABLED else None
_v893_last_audit_at = 0.0
_age_study = BackgroundAgeStudy(state_file=STATE_FILE, starting_capital=STARTING_CAPITAL,
    promoted_0_60_no_halts=AGE_0_60_PRIMARY,
    max_entry_market_cap_usd=MOMENTUM_MARKET_CAP_MAX_USD,
    enable_v893_research=V893_RESEARCH_ENABLED,
    research_guard=_v893_risk, research_quote_probe=_v893_quotes)
# Paper-only shadow studies must not inherit an old loss-based halt from any
# separately persisted study (including the historical 5_60 track). Apply the
# policy BEFORE their ledgers are loaded. AgeTrack.load() preserves the past
# reason in historical_halt_reason, clears the active flag on next save, and
# preserves cash, positions, closed trades, and seen mints without reset.
# This does NOT bypass the invalid-ledger read-only guard, the position limit,
# negative cash checks, quote verification, or *live* execution freezes.
if PAPER_MODE and AGE_0_60_PRIMARY and not LIVE_0_60_ENABLED:
    for _paper_age_track in _age_study.tracks:
        _paper_age_track.halts_enabled = False
# The LIVE version replaces only the promoted 0_60 track with an isolated
# real-execution ledger. All discovery + entry/exit decision code remains the
# existing v8.7.8 AgeTrack.observe; background 5_60 stays indicative paper.
if LIVE_0_60_ENABLED:
    if not AGE_0_60_PRIMARY:
        raise RuntimeError('live mode requires promoted 0_60 strategy')
    from live_age_execution_v879 import JupiterAutoBroker, LiveAgeTrack
    _live_file = STATE_FILE.with_name('live_age_0_60_'+STATE_FILE.stem+'.json')
    _broker = JupiterAutoBroker(
        key_b58=os.getenv('LIVE_0_60_SECRET_KEY_B58',''),
        rpc_url=os.getenv('SOLANA_RPC_URL',''),
        jupiter_key=os.getenv('JUPITER_API_KEY',''),
        journal_dir=STATE_FILE.with_name('live_order_journal_v879'))
    _age_study.tracks[0] = LiveAgeTrack(
        name='0_60',min_age=0.,path=_live_file,starting_capital=STARTING_CAPITAL,
        size=2.,max_positions=5,halts_enabled=False,take_profit_net_pct=40.,
        max_entry_market_cap_usd=MOMENTUM_MARKET_CAP_MAX_USD,
        exit_on_profitable_reversal=True,broker=_broker,
        allow_new_buys=os.getenv('LIVE_0_60_NEW_BUYS_ENABLED','false').lower()=='true',
        acknowledge_zero_balance_mints=tuple(filter(None,os.getenv('LIVE_0_60_ACK_ZERO_BALANCE_MINTS','').split(','))),
        recover_failed_buy_order_id=os.getenv('LIVE_0_60_FAILED_BUY_RECOVER_ORDER_ID',''))
    log.warning('LIVE PRIMARY CONFIGURED wallet=%s path=%s signer_loaded=True no_tx_sent_yet=True',
                _broker.wallet,_live_file)
_age_study_last_stats_at = 0.0
_micro_fast_followup_window: list[float] = []  # max 24 shared micro followups/min
# Up to 12 additional urgent held-position observations/min only when the
# shared callback budget is exhausted; NEVER applies to entry research.
_micro_urgent_reserve_window: list[float] = []
_age_study_lock = threading.RLock()
_age_ws = None
_age_ws_fetch_guard = threading.Lock()
_age_ws_last_fetch_at = 0.
_age_ws_last_per_mint: dict[str,float] = {}
_age_ws_backoff_until = 0.
_live_ws_last_quote_at: dict[str,float] = {}
_live_ws_last_global_quote_at = 0.
_live_ws_last_telemetry_at: dict[str,float] = {}

_secondary_trend_check_times: list[float] = []
_gecko_shared_budget = SharedGeckoBudget(6)
_independent_movers = IndependentMovers(STATE_FILE.with_name("independent_movers_v73_state.json"),
    enabled=INDEPENDENT_MOVERS_ENABLED, api_key=DEXPAPRIKA_API_KEY)
_expanded_movers = ExpandedMomentumDiscovery(STATE_FILE.with_name("expanded_momentum_v77_state.json"),
    enabled=EXPANDED_MOMENTUM_DISCOVERY_ENABLED, jupiter_key=JUPITER_API_KEY,
    birdeye_key=BIRDEYE_API_KEY)
_quality_source_round = 0
_quality_source_backoff: dict[str, float] = {}
_quality_global_backoff_until = 0.0
_quality_cache: dict[str, Any] = {"until": 0.0, "mints": [], "scores": {}}
_board_cache: dict[str, Any] = {"until": 0.0, "movers": [], "trending": [], "warn_at": 0.0, "status": "not_polled", "last_success": 0.0, "failures": 0}
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
_fresh_feed_emitted: set[str] = set()
_fresh_lock = threading.Lock()
_feed_started = False
_feed_connected = False
_feed_status = "not_started"
_feed_messages = 0
_feed_reconnects = 0
_migration_event_count = 0
_migration_pending: OrderedDict[str,float] = OrderedDict()
_migration_recent: OrderedDict[str,float] = OrderedDict()
_migration_stats = Counter()
_event_recheck_mints: set[str] = set()  # one-scan full-market observation lane
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
            _fresh_feed_emitted.discard(old_mint)
    update_curve_subscriptions([mint])
    return True


def is_migration_event(event: dict) -> bool:
    """Only explicit migration notices, not token creations or paid trade data."""
    if not isinstance(event, dict):
        return False
    labels = [str(event.get(k) or "").strip().lower() for k in ("txType", "type", "event", "method")]
    return any(k in ("migrate", "migration", "subscribemigration", "migrated", "complete") for k in labels)


def record_migration_event(mint: str, *, now: float | None = None) -> bool:
    """Pool-arrival hint only. A new primary DEX lookup is ALWAYS required."""
    global _migration_event_count
    if not isinstance(mint, str) or not _MINT_RE.fullmatch(mint) or mint == SOL_MINT:
        return False
    stamp = time.monotonic() if now is None else now
    with _fresh_lock:
        for key,t in list(_migration_recent.items()):
            if stamp-t > 3600:
                _migration_recent.pop(key, None)
        if stamp-_migration_recent.get(mint, -1e10) < 120:
            return False
        _migration_recent[mint]=stamp
        _migration_pending[mint]=stamp
        _migration_pending.move_to_end(mint)
        _migration_event_count += 1
        while len(_migration_pending)>2000:
            _migration_pending.popitem(last=False)
        while len(_migration_recent)>5000:
            _migration_recent.popitem(last=False)
    return True


def migration_hints(*, limit: int = 8) -> list[str]:
    with _fresh_lock:
        hints=[]
        for _ in range(max(0,min(12,int(limit)))):
            if not _migration_pending:
                break
            mint,_=_migration_pending.popitem(last=False)
            hints.append(mint)
    return hints


def fresh_launch_snapshot() -> list[str]:
    cutoff = time.monotonic() - FRESH_LAUNCH_TTL_SECONDS
    with _fresh_lock:
        for mint, timestamp in list(_fresh_launches.items()):
            if timestamp < cutoff:
                _fresh_launches.pop(mint, None)
                _fresh_checks.pop(mint, None)
                _fresh_feed_emitted.discard(mint)
        return list(reversed(_fresh_launches))



def matured_fresh_feed_hints(*, now: float | None = None, limit: int = 32,
                               min_age_seconds: float | None = None) -> list[str]:
    """Bounded once-only discovery leads from the live launch feed.

    A token announcement is NOT proof of a tradable SOL pool. This waits until
    the strategy minimum age, then requests the regular verified market scan.
    Never bypasses entry checks, liquidity checks, or the paper-only setting.
    """
    current = time.monotonic() if now is None else now
    maturity = ((SHADOW_MIN_AGE_MINUTES if PAPER_SHADOW_ACTIVE else MOMENTUM_MIN_PAIR_AGE_MINUTES) * 60
                if min_age_seconds is None else max(0.0, float(min_age_seconds)))
    with _fresh_lock:
        available = sorted(((stamp, mint) for mint, stamp in _fresh_launches.items()
                            if mint not in _fresh_feed_emitted
                            and 0 <= current - stamp <= FRESH_LAUNCH_TTL_SECONDS
                            and current - stamp >= maturity))
        # The websocket can deliver ~20 launches/min. Sweep mature backlog
        # without delaying the most recently matured launches indefinitely.
        quota = max(0, min(32, int(limit)))
        older = available[:quota // 2]
        fresh = list(reversed(available[-(quota - len(older)):])) if quota > len(older) else []
        chosen = list(dict.fromkeys([mint for _,mint in older + fresh]))[:quota]
        _fresh_feed_emitted.update(chosen)
    return chosen


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
            ws.send(json.dumps({"method": "subscribeMigration"}))
            _feed_connected = True
            _feed_status = "connected"
            log.info("FRESH FEED connected (PumpPortal new-token + migration streams; no paid trades)")
            backoff = 3
            while True:
                message = json.loads(ws.recv())
                if not isinstance(message, dict):
                    continue
                mint = message.get("mint")
                # The socket subscribes only to creates and migrations; event
                # producers can omit txType on migrations. Never interpret a
                # non-create payload as a new-token launch.
                tx_kind = str(message.get("txType") or message.get("type") or message.get("event") or "").lower()
                creation = tx_kind in ("create", "creation", "newtoken", "new_token") or (
                    not tx_kind and any(k in message for k in ("initialBuy", "bondingCurveKey", "vTokensInBondingCurve")))
                if is_migration_event(message) or (mint and not creation):
                    if record_migration_event(mint):
                        log.info("PAPER MIGRATION EVENT mint=%s source=pumpportal event_hint_only=True", mint)
                    continue
                # Trade/unknown notifications aren't launch announcements.
                if not creation:
                    continue
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
_shadow_gecko_hints: dict[str, float] = {}  # Gecko born-at hints, never trade evidence
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
    _shadow_gecko_hints.clear()
    _gecko_near_hints.clear()
    accepted = 0
    errors = 0
    pool_rejects = Counter()
    pool_discovery_signals = Counter()
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
    # Reserve page 1 of NEW Solana pools on every refresh for the 5-12m
    # shadow window. Old rotating momentum rankings are secondary.
    if PAPER_SHADOW_ACTIVE:
        endpoints = [('new_pools', {'page':p}) for p in (1,2,3)] + [e for e in endpoints if e != ('new_pools', {'page':1})][:1]
    source_report = []
    attempted = 0
    skipped_backoff = 0
    global_pause = max(0, int(_quality_global_backoff_until - now))
    budget_skips = 0
    for endpoint, params in endpoints:
        # Source-level 429 means no more calls to ANY GeckoTerminal endpoint.
        # A missing deep page (404) only disables that exact URL/page.
        backoff_key = (endpoint, tuple(sorted(params.items())))
        if now < _quality_global_backoff_until or now < _quality_source_backoff.get(backoff_key, 0.0):
            skipped_backoff += 1
            continue
        can_request, budget_reason, budget_wait = _gecko_shared_budget.acquire(now=now)
        if not can_request:
            budget_skips += 1
            # Preserve remaining source slots for the next scheduled poll.
            break
        page = params.get("page", 1)
        attempted += 1
        try:
            response = session.get(
                f"https://api.geckoterminal.com/api/v2/networks/solana/{endpoint}",
                params={"include": "base_token,quote_token", **params},
                headers={"Accept": "application/json;version=20230203"}, timeout=12)
            response.raise_for_status()
            payload = response.json()
            if PAPER_SHADOW_ACTIVE:
                shadow_hints=early_pool_hints(payload, sol_mint=SOL_MINT,
                    supported_dexes=active_entry_routes())
                _shadow_gecko_hints.update(shadow_hints)
                if SHADOW_AGE_STUDY_ENABLED:
                    # Independent wider hints: do NOT add them to primary queue.
                    extended=early_pool_hints(payload,sol_mint=SOL_MINT,
                        supported_dexes=active_entry_routes(),max_age_minutes=60)
                    with _age_study_lock:
                        for study_mint,study_born in extended.items():
                            _age_study.hint(study_mint,study_born)
                for early_mint in shadow_hints:
                    scores[early_mint] = max(scores.get(early_mint,0), 1_000_000)
            if MOMENTUM_SWING_MODE:
                _gecko_near_hints.update(early_gecko_pools(payload,
                    sol_mint=SOL_MINT, supported_dexes=active_entry_routes(),
                    min_age=discovery_earliest_pool_age_minutes(),
                    min_cap=MOMENTUM_MARKET_CAP_MIN_USD,
                    max_cap=MOMENTUM_MARKET_CAP_MAX_USD))
            candidates = extract_quality_pool_mints(payload,
                sol_mint=SOL_MINT,
                supported_dexes=frozenset(d.replace("_", "").replace("-", "") for d in active_entry_routes()),
                min_liquidity_usd=active_entry_liquidity_min(),
                min_volume_1h_usd=(0.0 if SNIPER_MODE or PAPER_NO_1H_REQUIREMENT else QUALITY_POOL_MIN_VOLUME_1H_USD),
                sniper_max_age_minutes=(SNIPER_MAX_AGE_MINUTES if SNIPER_MODE else None),
                sniper_min_volume_5m_usd=(SNIPER_MIN_VOLUME_5M_USD if SNIPER_MODE else None),
                sniper_min_change_5m_pct=(SNIPER_MIN_PRICE_CHANGE_5M_PCT if SNIPER_MODE else None),
                sniper_min_buy_sell_ratio=SNIPER_MIN_BUY_SELL_RATIO,
                sniper_min_txns_5m=SNIPER_MIN_TXNS_5M,
                sniper_max_change_5m_pct=SNIPER_MAX_PRICE_CHANGE_5M_PCT,
                sniper_min_age_minutes=MIN_PAIR_AGE_MINUTES,
                discovery_stage=("watch" if SNIPER_MODE else "continuation" if MOMENTUM_SWING_MODE else "strict"),
                discovery_min_market_cap_usd=(MOMENTUM_MARKET_CAP_MIN_USD if MOMENTUM_SWING_MODE else 0),
                rejection_counts=pool_rejects,
                discovery_counts=pool_discovery_signals)
            accepted += len(candidates)
            source_report.append(f"{endpoint}:{params.get('duration', 'ranked')}:p{page}:{len(candidates)}")
            for mint, score in candidates.items():
                scores[mint] = max(scores.get(mint, 0),
                    score + (1_000_000 if PAPER_SHADOW_ACTIVE and mint in _shadow_gecko_hints else 0))
        except requests.HTTPError as exc:
            errors += 1
            status = exc.response.status_code if exc.response is not None else 0
            if status == 429:
                try:
                    retry_seconds = min(600, max(180, int(exc.response.headers.get("Retry-After", "180"))))
                except (AttributeError, TypeError, ValueError):
                    retry_seconds = 180
                _quality_global_backoff_until = now + retry_seconds
                _gecko_shared_budget.note_429(retry_seconds, now=now)
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
    if budget_skips:
        log.info("DISCOVERY GECKO BUDGET skipped=%d reason=%s retry_s=%.0f status=%s crosschecks_share_budget=True",
                 budget_skips, budget_reason, budget_wait, _gecko_shared_budget.status(now=now))
    if pool_discovery_signals:
        log.info("DISCOVERY EARLY FLOW HINTS counts=%s final_dex_entry_checks_unchanged=True",
                 dict(pool_discovery_signals))
    # Preserve previous healthy sample during transient upstream failures.
    if not scores and (errors or budget_skips or skipped_backoff):
        scores = dict(_quality_cache["scores"])
    ordered = sorted(scores, key=scores.get, reverse=True)[:SNIPER_WATCHLIST_LIMIT if SNIPER_MODE else 200]
    _quality_cache.update(until=now + QUALITY_POOL_CACHE_SECONDS,
                          mints=ordered, scores={m: scores[m] for m in ordered})
    if PAPER_SHADOW_ACTIVE:
        log.info('PAPER SHADOW EARLY POOLS recent_0_to_12m=%d early_first_refresh=True primary_dex_refetch_required=True paper_only=True',len(_shadow_gecko_hints))
    log.info("SNIPER POOLS stage1_discovered=%d stage1_unique=%d pages=%d errors=%d rejected=%s cache_seconds=%d source=geckoterminal_poll fallback_when_ws_down=%s", accepted, len(ordered), len(endpoints), errors, dict(pool_rejects), QUALITY_POOL_CACHE_SECONDS, not _feed_connected) if SNIPER_MODE else log.info("QUALITY POOLS discovered=%d unique=%d errors=%d rejected=%s", accepted, len(ordered), errors, dict(pool_rejects))
    return ordered, dict(_quality_cache["scores"])


def pumpfun_board_candidates() -> dict[str, list[str]]:
    """Poll browser-observed Pump.fun Movers as UNTRUSTED discovery hints.

    Neither a JWT nor browser-cookie independence is guaranteed. Use the
    session/user parameters observed in the user's own working browser request.
    Unauthenticated server requests may fail. Never log a JWT, session ID,
    user ID, URL query, cookies, or response body.
    """
    empty = {"movers": [], "trending": []}
    if not (PUMPFUN_BOARDS_ENABLED and PUMPFUN_MOVERS_ENABLED):
        _board_cache["status"] = "disabled"
        return empty
    stamp = time.monotonic()
    if stamp < _board_cache["until"]:
        return {k: list(_board_cache[k]) for k in empty}
    result = dict(empty)
    if not (PUMPFUN_BOARD_SESSION_ID and PUMPFUN_BOARD_USER_ID):
        _board_cache.update(until=stamp + PUMPFUN_BOARDS_CACHE_SECONDS,
                            status="missing_parameters", **result)
        if stamp >= _board_cache["warn_at"]:
            log.info("PUMPFUN MOVERS status=missing_parameters hint_count=0 configure_session_id_and_userId=True ordinary_discovery_unaffected=True")
            _board_cache["warn_at"] = stamp + 600
        return result
    params = {"limit": PUMPFUN_BOARDS_LIMIT,
              "session_id": PUMPFUN_BOARD_SESSION_ID,
              "tier": PUMPFUN_BOARD_TIER,
              "userId": PUMPFUN_BOARD_USER_ID}
    if not valid_pumpfun_endpoint(MOVERS_URL):
        _board_cache.update(until=stamp + 600, status="invalid_endpoint", **result)
        return result
    status = "ok"
    delay = PUMPFUN_BOARDS_CACHE_SECONDS
    try:
        headers = {"Accept": "application/json", "Origin": "https://pump.fun"}
        if PUMPFUN_BOARD_JWT:
            headers["Authorization"] = f"Bearer {PUMPFUN_BOARD_JWT}"
        response = session.get(MOVERS_URL, params=params, headers=headers, timeout=8)
        response.raise_for_status()
        board_payload = response.json()
        result["movers"] = board_mints(board_payload, limit=PUMPFUN_BOARDS_LIMIT)
        if not result["movers"]:
            status = "empty_or_unrecognized_payload"
            # Safely report schema fingerprints without response values, IDs,
            # coins, session/user parameters, or browser credentials.
            shape = safe_board_shape(board_payload, limit=PUMPFUN_BOARDS_LIMIT)
            log.warning("PUMPFUN MOVERS SHAPE top_type=%s recognized_top_keys=%s state_type=%s objects=%s arrays=%s nodes=%s mint_fields=%s valid_mint_fields=%s entry_records=%s entry_key_count=%s known_entry_keys=%s entry_address_like_values=%s entry_address_like_keys=%s entry_pump_suffix_values=%s entry_pump_suffix_keys=%s entry_unique_address_values=%s entry_single_address_records=%s entry_multiple_address_records=%s entry_min_address_per_record=%s entry_max_address_per_record=%s entry_id_fields=%s valid_entry_id_fields=%s direct_mint_lists=%s truncated=%s all_values_redacted=True",
                        shape['top_type'], shape['recognized_top_keys'], shape['state_type'],
                        shape['objects'], shape['arrays'], shape['nodes'],
                        shape['mint_fields'], shape['valid_mint_fields'],
                        shape['entry_records'], shape['entry_key_count'],
                        shape['known_entry_keys'], shape['entry_address_like_values'],
                        shape['entry_address_like_keys'], shape['entry_pump_suffix_values'],
                        shape['entry_pump_suffix_keys'], shape['entry_unique_address_values'],
                        shape['entry_single_address_records'], shape['entry_multiple_address_records'],
                        shape['entry_min_address_per_record'], shape['entry_max_address_per_record'],
                        shape['entry_id_fields'], shape['valid_entry_id_fields'],
                        shape['direct_mint_lists'], shape['truncated'])
    except requests.HTTPError as exc:
        http_status = exc.response.status_code if exc.response is not None else None
        status = f"http_{http_status}" if http_status else "http_error"
        if http_status in (401, 403):
            delay = 900  # Credentials expired or access denied; no blind retry storm.
        elif http_status == 429:
            retry = exc.response.headers.get("Retry-After", "180") if exc.response is not None else "180"
            try:
                delay = max(60, min(1800, int(retry)))
            except (ValueError, TypeError, OverflowError):
                delay = 180
        elif http_status is not None and http_status >= 500:
            delay = 120
    except (requests.RequestException, ValueError, TypeError) as exc:
        status = type(exc).__name__
        delay = max(90, PUMPFUN_BOARDS_CACHE_SECONDS)
    _board_cache.update(until=stamp + delay, status=status, **result)
    if status == "ok":
        _board_cache["last_success"] = stamp
        _board_cache["failures"] = 0
    else:
        _board_cache["failures"] += 1
    if status != "ok" and stamp >= _board_cache["warn_at"]:
        log.warning("PUMPFUN MOVERS status=%s hint_count=%d retry_s=%d credentials_never_logged=True discovery_continues=True",
                    status, len(result["movers"]), delay)
        _board_cache["warn_at"] = stamp + 600
    log.info("PUMPFUN MOVERS status=%s hints=%d interval_s=%d uses_entry_filters=True",
             status, len(result["movers"]), delay)
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


def discovery_earliest_pool_age_minutes() -> float:
    """Source discovery must not wait beyond the active shadow's 5m window.

    This affects which mint addresses receive a fresh pool lookup, not any
    trade decision or the legacy strategy diagnostics.
    """
    return SHADOW_MIN_AGE_MINUTES if PAPER_SHADOW_ACTIVE else MOMENTUM_MIN_PAIR_AGE_MINUTES


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
            min_age=discovery_earliest_pool_age_minutes(), min_cap=MOMENTUM_MARKET_CAP_MIN_USD,
            max_cap=MOMENTUM_MARKET_CAP_MAX_USD))
        rejects = Counter()
        scores = search_pool_hints(
            pairs, sol_mint=SOL_MINT,
            supported_dexes=frozenset(d.replace("-", "").replace("_", "")
                                      for d in active_entry_routes()),
            min_liquidity_usd=active_entry_liquidity_min(),
            min_pair_age_minutes=discovery_earliest_pool_age_minutes(),
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
                        min_age=discovery_earliest_pool_age_minutes(),
                        min_cap=MOMENTUM_MARKET_CAP_MIN_USD,
                        max_cap=MOMENTUM_MARKET_CAP_MAX_USD))
                if ROLLING_DISCOVERY_ENABLED and MOMENTUM_SWING_MODE:
                    hints = search_pool_hints(
                        pairs, sol_mint=SOL_MINT,
                        supported_dexes=frozenset(d.replace("-", "").replace("_", "")
                                                  for d in active_entry_routes()),
                        min_liquidity_usd=active_entry_liquidity_min(),
                        min_pair_age_minutes=discovery_earliest_pool_age_minutes(),
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
    # Pool identity and order viability remain untrusted until fresh checks.
    # Board rank controls only scan order, never eligibility or position size.
    for rank, mint in enumerate(board_results["movers"]):
        priority[mint] = max(priority.get(mint, 0.0), 250.0 - rank * 2.0)
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
        min_age_minutes=discovery_earliest_pool_age_minutes(),
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
        min_liquidity=active_entry_liquidity_min(), min_age=discovery_earliest_pool_age_minutes(),
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
    discovered_first_time: set[str] = set()
    if ROLLING_DISCOVERY_ENABLED and MOMENTUM_SWING_MODE:
        if _rolling_watch.read_only:
            log.warning("DISCOVERY WATCHLIST persisted sidecar unreadable; leaving original file unchanged and using in-memory rotation")
        # Record only structurally plausible candidates: quality pools plus
        # supported/liquid SOL-quoted search hits, not paid-boost spam.
        # Source-yield telemetry is computed against the persisted historical
        # universe BEFORE update: reactivated old mints are never "truly new".
        historical = set(_rolling_watch.records)
        discovered_first_time = set(priority) - historical
        source_yields = {
            "geckoterminal": (len(set(quality_mints)), len(set(quality_mints) - historical)),
            "dexpaprika": (len(independent_scores), len(set(independent_scores) - historical)),
            "recent_profiles": (len(profile_scores), len(set(profile_scores) - historical)),
            "pumpfun_movers": (len(board_results["movers"]),
                               len(set(board_results["movers"]) - historical)),
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

    # Live WebSocket launches are untrusted mint-address hints. Wait for the
    # minimum-age window, then spend at most 32 lookup slots per refresh.
    feed_matured = (matured_fresh_feed_hints(limit=16)
                    if FRESH_LAUNCH_FEED_ENABLED and FRESH_BATCH_DISCOVERY_ENABLED else [])
    if FRESH_LAUNCH_FEED_ENABLED:
        log.info("FRESH FEED INTAKE connected=%s messages=%d mature_hints=%d "
                 "mature_intake_limit=16 entry_requires_new_verified_sol_pool=True paper_only=True",
                 _feed_connected, _feed_messages, len(feed_matured))

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
            "fresh_launch_feed":feed_matured,
            "shadow_recent_pools":list(_shadow_gecko_hints),
            "pumpfun_movers":board_results["movers"],
            **extra_sources,
        })
        # Normalize incomparable provider priority scales; record source
        # attribution for later first-pass conversion telemetry.
        ranked = _candidate_pipeline.rank(priority)
        for mint in _shadow_gecko_hints:
            ranked[mint] = max(ranked.get(mint,0), 1_000_000)
        for mint in board_results["movers"]:
            ranked.setdefault(mint, 175.0)  # source priority only, no buy approval
        for mint in feed_matured:
            ranked.setdefault(mint, 160.0)  # queue ordering only, NOT market approval
        # Provider source hints are untrusted, but more likely to have indexed
        # DEX liquidity than raw WebSocket creation announcements. Reserve
        # scan capacity for them; the buy path REFETCHES the actual pool.
        pool_hints = (set(quality_mints) | set(independent_scores) |
                      set(search_valid if DISCOVERY_SEARCH_TERMS else ()) |
                      set(profile_scores) | set(near_due) |
                      set().union(*(set(x) for x in extra_sources.values())))
        pool_hints |= set(_shadow_gecko_hints)
        offered = _fresh_batch.offer(
            ranked, novel_mints=discovered_first_time | set(feed_matured),
            feed_mints=set(feed_matured) | set(board_results["movers"]),
            pool_hints=pool_hints,
            mover_mints=set(board_results['movers']))
        shadow_refreshed = (_fresh_batch.offer_shadow_pool_hints(_shadow_gecko_hints)
                            if PAPER_SHADOW_ACTIVE else 0)
        if PAPER_SHADOW_ACTIVE:
            log.info('PAPER SHADOW EARLY SOURCE offered=%d currently_recent=%d watch_window=5_to_12m scan_priority=True',
                     shadow_refreshed, len(_shadow_gecko_hints))
        log.info("PAPER SHADOW SOURCE ALLOCATION early_verified_hints=%d other_pool_hints=%d launch_leads=%d recent_primary_lookup_required=True",
                 len(_shadow_gecko_hints),len(pool_hints-set(_shadow_gecko_hints)),len(feed_matured)) if PAPER_SHADOW_ACTIVE else log.info(
                 "PAPER V85 SOURCE ALLOCATION current_pool_hints=%d launch_leads=%d shared_mints=%d pool_discovery_first=True entry_filters_unchanged=True",
                 len(pool_hints),len(feed_matured),len(pool_hints & set(feed_matured)))
        active_stats = _fresh_batch.stats()
        log.info("FRESH BATCH REFILL discovered=%d active=%d unscanned=%d novel_unscanned=%d blocked_repeat=%d reactivated=%d quarantined=%d state=%s",
                 offered["new"], active_stats["active"], active_stats["unscanned"], active_stats["novel_unscanned"],
                 offered["blocked"], offered["reactivated"], active_stats["quarantined"], _fresh_batch.path)
        _fresh_batch.save()
        log.info("CANDIDATE SOURCE CONVERSION lifetime_24h=%s first_market_pass_not_paper_buy=True", _candidate_pipeline.source_metrics())
        _candidate_pipeline.save()

    # Feed mints are added even when DexScreener profiles/boosts are unchanged.
    # A newly launched token may have no indexed SOL pair yet; that is normal.
    stream_mints = fresh_launch_snapshot()
    sources["fresh_launch_feed"] = len(stream_mints)
    unique = ([m for m in _fresh_batch.due_v85(limit=FRESH_BATCH_ACTIVE_LIMIT, launch_quota=6) if m != SOL_MINT]
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


def momentum_trend_rejection(pair: dict, *, early_flow_qualified: bool = False, young_pool: bool = False) -> str | None:
    """Require short-term lift; hourly checks are optional in no-1h paper profile.

    Absent data is not interpreted as zero or green. This is a coarse API trend
    proxy, NOT a measured moving-average slope or candle confirmation.
    """
    volume = pair.get("volume") or {}
    change = pair.get("priceChange") or {}
    txns = (pair.get("txns") or {}).get("m5") or {}
    for window in ("m5", "h1", "h6", "h24"):
        # A 12-60 minute pool may lack older multi-hour windows. Only the
        # verified recent-flow microcap lane can use available 5m+1h data.
        if PAPER_NO_1H_REQUIREMENT and (window == "h1" or (young_pool and window in ("h6", "h24"))) and change.get(window) is None:
            continue
        if young_pool and early_flow_qualified and window in ("h6", "h24") and change.get(window) is None:
            continue
        if change.get(window) is None:
            return f"trend_{window}_missing"
        if not math.isfinite(as_float(change[window], float("nan"))):
            return f"trend_{window}_invalid"
    if as_float(volume.get("m5")) < MOMENTUM_MIN_VOLUME_5M_USD:
        return "momentum_volume"
    if not PAPER_NO_1H_REQUIREMENT and as_float(volume.get("h1")) < MOMENTUM_MIN_VOLUME_1H_USD and not early_flow_qualified:
        return "momentum_volume_1h"
    ch5, ch1, ch6, ch24 = [as_float(change.get(k)) for k in ("m5", "h1", "h6", "h24")]
    if not MOMENTUM_MIN_CHANGE_5M_PCT <= ch5 <= MOMENTUM_MAX_CHANGE_5M_PCT:
        return "momentum_price_change"
    if ((not PAPER_NO_1H_REQUIREMENT and ch1 < MOMENTUM_MIN_CHANGE_1H_PCT) or
        (change.get("h6") is not None and ch6 < MOMENTUM_MIN_CHANGE_6H_PCT) or
        (change.get("h24") is not None and ch24 < MOMENTUM_MIN_CHANGE_24H_PCT)):
        return "downtrend_multi_timeframe"
    if (not PAPER_NO_1H_REQUIREMENT and ch1 > 120) or ch6 > 300 or ch24 > 800:
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
        early_flow_ok, early_flow_reason = early_volume_exception(
            pair, age_minutes=age*60, cap_usd=cap, cap_source=cap_source)
        trend_reason = momentum_trend_rejection(
            pair, early_flow_qualified=early_flow_ok, young_pool=age*60<60)
        if early_flow_ok:
            log.info("PAPER EARLY VOLUME PASS mint=%s cap=$%.0f age_min=%.1f vol5=$%.0f vol1h=$%.0f buys=%d sells=%d same_pool=True no_hourly_volume_bypass_without_flow=True",
                     address, cap, age*60, m5_volume, h1_volume, m5_buys, m5_sells)
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
            if cap < 100000:
                # $35K-$100K: explicit PAPER-ONLY high-risk micro tier. The
                # liquidity belongs to THIS supported SOL pool, not the token's
                # other USDC/unsupported pools. No FDV-only cap is accepted.
                if (liquidity < 18000 or m5_volume < 1800
                        or (not PAPER_NO_1H_REQUIREMENT and h1_volume < 10000 and not early_flow_ok)
                        or m5_buys + m5_sells < 20 or m5_sells < 3
                        or m5_buys / m5_sells < 1.30 or (not PAPER_NO_1H_REQUIREMENT and ch1 < 4)
                        or not 0.8 <= ch5 <= 14):
                    reject("micro_35k_extra_safety")
                    return None
            elif cap < 250000:
                if (liquidity < 25000 or m5_volume < 1500
                        or (not PAPER_NO_1H_REQUIREMENT and h1_volume < 12000 and not early_flow_ok)
                        or m5_buys + m5_sells < 16 or m5_sells < 3
                        or m5_buys / m5_sells < 1.22 or (not PAPER_NO_1H_REQUIREMENT and ch1 < 5.5)
                        or not 0.8 <= ch5 <= 14):
                    reject("micro_cap_extra_safety")
                    return None
            elif (liquidity < 25000 or m5_volume < 1200 or (not PAPER_NO_1H_REQUIREMENT and h1_volume < 18000)
                    or m5_buys + m5_sells < 12 or m5_sells <= 0
                    or m5_buys / m5_sells < 1.20 or (not PAPER_NO_1H_REQUIREMENT and ch1 < 4)):
                reject("small_cap_extra_safety")
                return None
        # 12-30 minute pools require stronger same-pool liquidity and flow.
        # 30-60 minute pools retain the original guardrails.
        if 12 <= age * 60 < 30 and (liquidity < 22000 or m5_volume < 1800
                or (not PAPER_NO_1H_REQUIREMENT and h1_volume < 10000 and not early_flow_ok)
                or m5_buys + m5_sells < 18 or m5_sells < 3
                or m5_buys / m5_sells < 1.25
                or (not PAPER_NO_1H_REQUIREMENT and ch1 < 4) or not 0.8 <= ch5 <= 14):
            reject("early_15m_extra_safety")
            return None
        if 30 <= age * 60 < 60 and (liquidity < 20000 or m5_volume < 1450
                or (not PAPER_NO_1H_REQUIREMENT and h1_volume < 12000 and not early_flow_ok)
                or m5_buys + m5_sells < 16
                or m5_sells < 3 or m5_buys / m5_sells < 1.22 or (not PAPER_NO_1H_REQUIREMENT and ch1 < 5)):
            reject("young_pool_extra_safety")
            return None
        if PAPER_FAST_ONLY:
            quick = {"change_m5":ch5,"change_h1":ch1,"m5_volume":m5_volume,
                     "h1_volume":h1_volume,"liquidity":liquidity,
                     "m5_buys":m5_buys,"m5_sells":m5_sells}
            fast_ok, fast_reason, fast_rank = fast_mover_gate(quick, require_1h=not PAPER_NO_1H_REQUIREMENT)
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
        state["halted"] = bool(state.get("halted"))
        log.info("New day. Daily equity baseline reset to $%.2f", state["day_start_equity"])


def should_halt(state: dict) -> bool:
    baseline = as_float(state.get("day_start_equity"), STARTING_CAPITAL)
    current = equity(state)
    loss_fraction = (baseline - current) / max(1.0, baseline)
    if loss_fraction >= MAX_DAILY_LOSS_FRACTION:
        if not state.get("daily_loss_halt"):
            log.error("DAILY LOSS LIMIT HIT: %.2f%% baseline=$%.2f risk_equity=$%.2f limit=%.1f%%. New entries halted; requires explicit review.",
                      loss_fraction * 100, baseline, current, MAX_DAILY_LOSS_FRACTION * 100)
        state["daily_loss_halt"] = True
        state["halted"] = True
    if state.get('shadow_exit_safety_halt'):
        state['halted'] = True
    return bool(state.get("halted"))



def shadow_exit_risk_guard(state: dict, *, now: float | None = None) -> None:
    """Halt NEW paper entries if exits become unavailable or severe losses occur.

    Never closes a position or creates sale proceeds. Unresolved exits remain
    on the books for independent route rechecks and conservative valuation.
    Persistent halts require explicit review; day rollover does not clear them.
    """
    if not PAPER_MODE or not PAPER_SHADOW_ACTIVE:
        return
    stamp = time.time() if now is None else now
    for mint, raw in state.get('positions', {}).items():
        metrics = state.get('paper_trade_metrics', {}).get(mint, {})
        if metrics.get('strategy') != 'early_shadow_active':
            continue
        issue = state.get('exit_issues', {}).get(mint, {})
        unavailable_since = float(issue.get('first_seen_at') or stamp)
        unavailable_seconds = max(0., stamp - unavailable_since)
        pending = metrics.get('pending_exit_reason')
        adverse = metrics.get('max_adverse_net_pct')
        adverse = float(adverse) if adverse is not None else 0.
        severe = adverse <= -SHADOW_SEVERE_POSITION_LOSS_HALT_PCT
        impaired = issue.get('status') == 'IMPAIRED_EXIT_UNAVAILABLE'
        long_outage = bool(issue) and unavailable_seconds >= SHADOW_EXIT_UNAVAILABLE_HALT_SECONDS
        pending_since = float(metrics.get('pending_exit_first_at') or stamp)
        pending_overdue = bool(pending) and stamp - pending_since >= SHADOW_EXIT_UNAVAILABLE_HALT_SECONDS
        if not (severe or impaired or long_outage or pending_overdue):
            continue
        if not state.get('shadow_exit_safety_halt'):
            why = ('severe_open_position_loss' if severe else
                   'impaired_exit_unavailable' if impaired else
                   'exit_route_unavailable_overdue' if long_outage else 'pending_exit_overdue')
            log.error('PAPER SHADOW EXIT SAFETY HALT mint=%s reason=%s adverse_net_pct=%.2f '
                      'unavailable_s=%.0f pending_exit=%s new_entries_halted=True position_retained=True '
                      'no_assumed_sale=True manual_review_required=True',
                      mint, why, adverse, unavailable_seconds, pending)
            state['shadow_exit_safety_reason'] = why
            state['shadow_exit_safety_mint'] = mint
            state['shadow_exit_safety_at'] = stamp
        state['shadow_exit_safety_halt'] = True
        state['halted'] = True
        break


def shadow_risk_snapshot(state: dict, *, now: float | None = None, interval: float = 60.) -> bool:
    """Portfolio risk audit, including unresolved/overdue shadow exit requests."""
    stamp = time.time() if now is None else now
    shadow_exit_risk_guard(state, now=stamp)
    previous = as_float(state.get('last_shadow_risk_snapshot_at'), 0)
    if previous > 0 and 0 <= stamp - previous < interval:
        return False
    state['last_shadow_risk_snapshot_at'] = stamp
    trades = state.get('closed_trades', [])
    writeoffs = [t for t in trades if t.get('execution') == 'paper_writeoff_no_sale']
    impaired = [mint for mint in state.get('positions', {}) if
                state.get('exit_issues', {}).get(mint, {}).get('status') == 'IMPAIRED_EXIT_UNAVAILABLE']
    pending = [mint for mint in state.get('positions', {}) if
               state.get('paper_trade_metrics', {}).get(mint, {}).get('pending_exit_reason')]
    severe = [mint for mint in state.get('positions', {}) if
              state.get('paper_trade_metrics', {}).get(mint, {}).get('max_adverse_net_pct') is not None and
              as_float(state['paper_trade_metrics'][mint]['max_adverse_net_pct']) <= -SHADOW_SEVERE_POSITION_LOSS_HALT_PCT]
    pnl = sum(as_float(t.get('pnl_usd')) for t in trades)
    log.info('PAPER SHADOW RISK AUDIT historical_5_12_ledger=%s legacy_halt_does_not_block_promoted_0_60=%s '
             'equity_risk=$%.2f cash=$%.2f baseline=$%.2f realized_pnl=$%.2f '
             'closed=%d paper_wins=%d writeoffs=%d writeoff_pnl=$%.2f impaired_open=%d '
             'open_positions=%d halt=%s halt_daily_loss=%s halt_writeoff=%s '
             'pending_exits=%d severely_drawn_open=%d halt_exit_safety=%s '
             'no_real_fills=True historical_trade_records_included=True',
             bool(AGE_0_60_PRIMARY),bool(AGE_0_60_PRIMARY),
             equity(state),as_float(state.get('cash_usd')),as_float(state.get('day_start_equity')),
             pnl,len(trades),sum(as_float(t.get('pnl_usd')) > 0 for t in trades),
             len(writeoffs),sum(as_float(t.get('pnl_usd')) for t in writeoffs),
             len(impaired),len(state.get('positions', {})),bool(state.get('halted')),
             bool(state.get('daily_loss_halt')),bool(state.get('shadow_writeoff_halt')),
             len(pending),len(severe),bool(state.get('shadow_exit_safety_halt')))
    return True


def shadow_pending_exit(state: dict, mint: str, reason: str, *, now: float | None = None) -> None:
    """Persist the first unfulfilled exit signal so recovery can execute it.

    The first signal time never resets on repeated market observations or a
    Railway restart. This is an *intent*, not evidence of executed sellability.
    """
    metrics = state.setdefault('paper_trade_metrics', {}).setdefault(mint, {})
    stamp = time.time() if now is None else now
    if not metrics.get('pending_exit_reason'):
        metrics['pending_exit_reason'] = reason
        metrics['pending_exit_first_at'] = stamp
        log.warning('PAPER SHADOW EXIT PENDING mint=%s reason=%s no_verified_sale_yet=True '
                    'new_entries_may_halt=True', mint, reason)


def shadow_exit_route_check(state: dict, mint: str, pos: Position) -> tuple[bool, str, dict | None]:
    """Check an actual new read-only quote for a pending PAPER sell.

    Quotes are not transactions and do not imply tokens were sold. Keep the
    modeled exit proceeds separate from these SOL-denominated route estimates.
    """
    if not SHADOW_EXIT_ROUTE_QUOTE_REQUIRED:
        return True, 'not_required', None
    metrics = state.setdefault('paper_trade_metrics', {}).setdefault(mint, {})
    last = float(metrics.get('exit_route_last_attempt_at') or 0.)
    now = time.time()
    # Throttle failed calls, not a recent SUCCESSFUL health probe when an
    # actual stop/expiry now needs a fresher same-pool quote. Otherwise a
    # routine observation could postpone an emergency exit by 12 seconds.
    successful_last = (last > 0 and as_float(metrics.get('exit_route_last_success_at')) == last)
    if now - last < SHADOW_EXIT_ROUTE_RETRY_SECONDS and not successful_last:
        return False, 'route_retry_wait', None
    metrics['exit_route_last_attempt_at'] = now
    if not SOLANA_RPC_URL or not JUPITER_API_KEY or not pos.entry_pool_address:
        return False, 'rpc_or_jupiter_or_pinned_pool_unavailable', None
    if (pos.entry_pool_dex or '').lower() != 'pumpswap':
        return False, 'unsupported_exit_pool_program', None
    try:
        decimals = metrics.get('prebuy_safety', {}).get('readonly_route', {}).get('mint_decimals')
        if decimals is None:
            decimals = pump_pool_snapshot(SOLANA_RPC_URL, pos.entry_pool_address, mint).base_decimals
        decimals = int(decimals)
        if not 0 <= decimals <= 18:
            raise ValueError('invalid_exit_token_decimals')
        amount_raw = round(pos.token_units * (10 ** decimals))
        if amount_raw <= 0:
            raise ValueError('invalid_exit_amount')
        route = pinned_sell_quote(JUPITER_API_KEY, mint, pos.entry_pool_address, amount_raw)
        route['in_amount_raw'] = amount_raw
        metrics['exit_route_last_success_at'] = now
        metrics['exit_route_quote_min_lamports'] = route['min_lamports']
        return True, 'same_pinned_pool_readonly_quote', route
    except Exception as exc:
        # Avoid logging API keys/URLs, response bodies, or any secret-bearing data.
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return False, 'router_quote_' + reason, None


def shadow_held_route_probe(state: dict, mint: str, pos: Position,
                            metrics: dict, *, now: float | None = None) -> tuple[bool, dict | None]:
    """Detect a routed SOL-quote collapse before a lagging USD indexer update.

    The entry/exit quotes are SOL-denominated and not execution guarantees.
    Changes in SOL/USD may also contribute to measured divergence. No trade
    is created here, and network/rate-limit failures never imply a sale.
    """
    if not (PAPER_MODE and PAPER_SHADOW_ACTIVE and SHADOW_ROUTE_MONITOR_ENABLED and
            SHADOW_EXIT_ROUTE_QUOTE_REQUIRED and metrics.get('strategy') == 'early_shadow_active'):
        return False, None
    stamp = time.time() if now is None else now
    if stamp - as_float(metrics.get('shadow_route_probe_last_at')) < SHADOW_ROUTE_MONITOR_INTERVAL_SECONDS:
        return False, None
    # Keep the global quote rate bounded; entry/exit API operations need room.
    _shadow_route_probe_times[:] = [t for t in _shadow_route_probe_times if 0 <= stamp-t < 60]
    if len(_shadow_route_probe_times) >= SHADOW_ROUTE_MONITOR_MAX_PER_MINUTE:
        return False, None
    entry = metrics.get('prebuy_safety', {}).get('readonly_route', {})
    # Skip budgeted probes without a comparable same-quantity entry quote.
    # Otherwise they'd consume the exit retry slot without usable evidence.
    if not (as_float(entry.get('quote_min_sol_lamports')) > 0 and
            as_float(entry.get('token_amount_raw')) > 0):
        metrics['shadow_route_probe_last_status'] = 'missing_comparable_entry_anchor'
        return False, None
    metrics['shadow_route_probe_last_at'] = stamp
    _shadow_route_probe_times.append(stamp)
    ok, reason, proof = shadow_exit_route_check(state, mint, pos)
    if not ok or not proof:
        metrics['shadow_route_probe_last_status'] = reason
        metrics['shadow_route_probe_failures'] = int(metrics.get('shadow_route_probe_failures', 0)) + 1
        # A normal probe failure is NOT an exit order. The normal exit path
        # still fails closed if and when a protective signal arises.
        if reason != 'route_retry_wait':
            log.info('PAPER SHADOW HELD ROUTE PROBE mint=%s status=%s '
                     'no_price_confirmation=True no_paper_sale=True', mint, reason)
        return False, None
    entry = metrics.get('prebuy_safety', {}).get('readonly_route', {})
    initial_lamports = as_float(entry.get('quote_min_sol_lamports'))
    initial_amount = as_float(entry.get('token_amount_raw'))
    checked_amount = as_float(proof.get('in_amount_raw'))
    output_lamports = as_float(proof.get('min_lamports'))
    if min(initial_lamports, initial_amount, checked_amount, output_lamports) <= 0:
        metrics['shadow_route_probe_last_status'] = 'missing_comparable_entry_anchor'
        return False, None
    comparable_entry = initial_lamports * checked_amount / initial_amount
    route_net_pct = 100. * (output_lamports / comparable_entry - 1.)
    if not math.isfinite(route_net_pct):
        return False, None
    metrics['shadow_route_probe_last_status'] = 'ok'
    metrics['shadow_route_probe_last_net_pct'] = round(route_net_pct, 3)
    metrics['shadow_route_probe_last_success_at'] = stamp
    previous_min = metrics.get('shadow_route_probe_worst_net_pct')
    metrics['shadow_route_probe_worst_net_pct'] = min(float(previous_min), route_net_pct) if previous_min is not None else route_net_pct
    if route_net_pct <= SHADOW_ROUTE_MONITOR_NET_STOP_PCT:
        log.warning('PAPER SHADOW ROUTED DOWNSIDE OBSERVED mint=%s quote_change_pct=%.2f '
                    'threshold_pct=%.2f pool=%s no_guaranteed_exit=True '
                    'sol_usd_anchor_not_hedged=True', mint, route_net_pct,
                    SHADOW_ROUTE_MONITOR_NET_STOP_PCT, pos.entry_pool_address)
        return True, proof
    return False, None


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



def shadow_prebuy_safety(candidate: dict) -> tuple[bool, str]:
    """Fail-closed PAPER plausibility check, NOT an executable swap/sell proof.

    Uses a second DexScreener token response for the exact pinned SOL pool,
    then an independent GeckoTerminal *same pool, same base and SOL quote*
    observation. New/missing/unindexed secondary pools cause no entry.
    Providers can both be inaccurate; a REAL swap quote is not available.
    """
    mint, pool = candidate.get('address'), candidate.get('pool_address')
    if not mint or not pool:
        return False, 'missing_entry_pool_identity'
    try:
        fresh_pairs = token_pairs(mint)
    except Exception:
        return False, 'prebuy_primary_lookup_error'
    if not _token_pairs_healthy.get(mint, False):
        return False, 'prebuy_primary_unverified'
    fresh = next((p for p in fresh_pairs
                  if p.get('chainId') == 'solana' and p.get('pairAddress') == pool
                  and (p.get('baseToken') or {}).get('address') == mint
                  and (p.get('quoteToken') or {}).get('address') == SOL_MINT
                  and (p.get('dexId') or '').lower() in active_entry_routes()), None)
    if not fresh:
        return False, 'prebuy_original_pool_disappeared'
    price = as_float(fresh.get('priceUsd'))
    liq = as_float((fresh.get('liquidity') or {}).get('usd'))
    candidate_price = as_float(candidate.get('price'))
    candidate_liq = as_float(candidate.get('liquidity'))
    if min(price, liq, candidate_price, candidate_liq) <= 0 or liq < SHADOW_MIN_LIQUIDITY_USD:
        return False, 'prebuy_missing_price_or_liquidity'
    if abs(price / candidate_price - 1.) * 100 > SHADOW_PREBUY_MAX_PRICE_DRIFT_PCT:
        return False, 'prebuy_price_moved_since_signal'
    if liq < candidate_liq * SHADOW_PREBUY_MIN_LIQ_RATIO:
        return False, 'prebuy_primary_liquidity_collapsed'
    # The already configured paper cost model is applied to the $2 stake.
    size = MICRO_35K_PAPER_SIZE_USD
    entry_with_cost = candidate_price * (1. + SHADOW_ESTIMATED_ROUND_TRIP_COST_PCT / 200.)
    estimated_pos = Position(token=mint, symbol=str(candidate.get('symbol','?')),
        name=str(candidate.get('name','?')), entry_price=entry_with_cost,
        amount_usd=size, token_units=size / entry_with_cost,
        opened_at=time.time(), peak_price=entry_with_cost,
        last_price=entry_with_cost, score=0.)
    estimate = paper_exit_quote(estimated_pos, price, liq,
        round_trip_cost=SHADOW_ESTIMATED_ROUND_TRIP_COST_PCT / 100.)
    if estimate is None or estimate['impact_pct'] > SHADOW_PREBUY_MAX_IMPACT_PCT:
        return False, 'prebuy_insufficient_modeled_exit_depth'
    secondary = held_pool_fallback(pool, mint)
    if secondary is None:
        return False, 'prebuy_independent_same_pool_unavailable'
    other_price = as_float(secondary.get('price'))
    other_liq = as_float(secondary.get('liquidity'))
    if other_price <= 0 or other_liq < liq * SHADOW_PREBUY_MIN_LIQ_RATIO or other_liq > liq * SHADOW_PREBUY_MAX_LIQ_RATIO:
        return False, 'prebuy_independent_liquidity_disagreement'
    if abs(other_price / price - 1.) * 100 > PAPER_PREBUY_MAX_CROSSCHECK_PRICE_DEVIATION_PCT:
        return False, 'prebuy_independent_price_disagreement'
    candidate['prebuy_safety'] = {
        'status': 'two_provider_indicative_only',
        'primary_pool_liquidity_usd': round(liq, 2),
        'independent_pool_liquidity_usd': round(other_liq, 2),
        'modeled_exit_impact_pct': round(estimate['impact_pct'], 4),
        'time': now_iso(), 'executable_swap_verified': False,
    }
    log.info('PAPER SHADOW PREBUY CHECK mint=%s pool=%s primary_liq=$%.0f independent_liq=$%.0f modeled_impact=%.3f%% independent_pool_match=True actual_sell_not_proven=True',
             mint, pool, liq, other_liq, estimate['impact_pct'])
    return True, 'indicative_same_pool_two_provider_check'


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


def shadow_unhealthy_entry_reason(candidate: dict) -> str | None:
    """Extra optional paper-entry risk layer; NEVER loosens original eligibility.

    Motivated by the observed Claude AI, Mr Beast, and TikTok US cases:
    thin near-minimum five-minute volume combined with a nearly-maximal surge.
    This is a retrospective hypothesis, not a validated profit improvement.
    """
    if not SHADOW_UNHEALTHY_ENTRY_GUARD:
        return None
    change = as_float(candidate.get('change_m5'), float('nan'))
    vol = as_float(candidate.get('m5_volume'), float('nan'))
    if not (math.isfinite(change) and math.isfinite(vol)):
        return 'unhealthy_m5_missing'
    if change >= SHADOW_SURGE_CHANGE_5M_PCT and vol < SHADOW_SURGE_MIN_5M_VOLUME_USD:
        return 'unhealthy_high_surge_thin_volume'
    return None


def shadow_paper_candidate(pair: dict, *, now: float | None = None) -> dict | None:
    """Map the *original* shadow study signal to a paper-position candidate."""
    stamp = time.time() if now is None else now
    e = shadow_entry_evidence(pair, sol_mint=SOL_MINT, dexes=active_entry_routes(),
                              now_ms=stamp * 1000)
    if e is None:
        return None
    token = pair.get('baseToken') or {}
    return {
        'address': e['mint'], 'symbol': str(token.get('symbol') or '???')[:20],
        'name': str(token.get('name') or 'Unknown')[:50], 'price': e['price'],
        'market_cap_usd': e['cap'], 'market_cap_source': 'marketCap',
        'age_hours': e['age'] / 60, 'liquidity': e['liquidity'],
        'm5_volume': e['v5'], 'm5_buys': int(e['buys']),
        'm5_sells': int(e['sells']), 'change_m5': e['change5'],
        'change_h1': as_float((pair.get('priceChange') or {}).get('h1')),
        'change_h6': as_float((pair.get('priceChange') or {}).get('h6')),
        'change_h24': as_float((pair.get('priceChange') or {}).get('h24')),
        'score': 0.0, 'pool_address': e['pool'],
        'pool_dex': (pair.get('dexId') or '').lower(),
        'fast_mover_rank': 0.0, 'url': pair.get('url', ''),
    }


def shadow_paper_entry(state: dict, pair: dict, *, now: float | None = None,
                       signal_candidate: dict | None = None) -> tuple[bool, str]:
    """Original observational shadow entry as a paper-portfolio position.

    The ORIGINAL qualified DexScreener snapshot supplies both the signal AND
    indicative entry price, exactly as EarlyShadow.observe does. Do not demand
    a second price, independent pool confirmation, sellability model or new
    5-minute momentum test. We cannot promise real swap execution from this.
    """
    if not PAPER_MODE or not PAPER_SHADOW_ACTIVE:
        return False, 'not_shadow_profile'
    if signal_candidate is None:
        c = shadow_paper_candidate(pair, now=now)
        if c is None:
            return False, 'shadow_signal_not_eligible'
    else:
        c = dict(signal_candidate)
        # A signal must be associated with the *same* market observation the
        # scanner approved, never with a different pool or mint. This check is
        # pure identity validation, not an extra trading threshold or lookup.
        if (c.get('address') != (pair.get('baseToken') or {}).get('address') or
                c.get('pool_address') != pair.get('pairAddress') or
                as_float(c.get('price')) <= 0):
            return False, 'shadow_signal_snapshot_mismatch'
    mint = c['address']
    if mint in state.get('positions', {}):
        return False, 'already_held'
    if len(state.get('positions', {})) >= MAX_POSITIONS:
        return False, 'position_limit'
    if should_halt(state):
        return False, 'portfolio_halted'
    # EarlyShadow.observe() can observe the same non-held mint again after a
    # completed result while still in its 5-12m window. Legacy cooldowns do
    # not belong to this restored study. Written-off mints stay blacklisted.
    if (mint in state.get('rugged_mints', []) or any(
            t.get('token') == mint and t.get('execution') == 'paper_writeoff_no_sale'
            for t in state.get('closed_trades', []))):
        return False, 'rug_writeoff_blacklisted'
    shadow_held = sum(state.get('paper_trade_metrics', {}).get(m, {}).get('strategy') == 'early_shadow_active'
                      for m in state.get('positions', {}))
    if shadow_held >= MAX_SHADOW_POSITIONS:
        return False, 'shadow_position_limit'
    unhealthy_reason = shadow_unhealthy_entry_reason(c)
    if unhealthy_reason:
        log.warning('PAPER SHADOW UNHEALTHY ENTRY SKIP mint=%s pool=%s reason=%s '
                    'change5=%.2f vol5=$%.0f surge_threshold=%.2f volume_floor=$%.0f '
                    'paper_only=True', mint, c.get('pool_address'), unhealthy_reason,
                    as_float(c.get('change_m5')), as_float(c.get('m5_volume')),
                    SHADOW_SURGE_CHANGE_5M_PCT, SHADOW_SURGE_MIN_5M_VOLUME_USD)
        return False, unhealthy_reason
    if SHADOW_SAFETY_PREBUY_VERIFICATION:
        guard_ok, guard_reason = shadow_prebuy_safety(c)
        if not guard_ok:
            log.warning('PAPER SHADOW SAFETY SKIP mint=%s pool=%s reason=%s no_paper_buy=True',
                        mint, c.get('pool_address'), guard_reason)
            return False, guard_reason
    if SHADOW_REQUIRE_READONLY_EXIT_ROUTE:
        market_cap = as_float(c.get('market_cap_usd'))
        liquidity = as_float(c.get('liquidity'))
        if market_cap >= 5_000_000 and liquidity / market_cap < .01:
            log.warning('PAPER SHADOW ROUTE SKIP mint=%s reason=thin_liquidity_vs_cap ratio=%.4f min=0.01 no_paper_buy=True',
                        mint, liquidity / market_cap)
            return False, 'thin_liquidity_vs_cap'
        if not SOLANA_RPC_URL or not JUPITER_API_KEY:
            log.warning('PAPER SHADOW ROUTE SKIP mint=%s reason=rpc_or_jupiter_key_missing no_paper_buy=True', mint)
            return False, 'rpc_or_jupiter_key_missing'
        try:
            proof = verify_paper_exit_route(
                SOLANA_RPC_URL, JUPITER_API_KEY, mint=mint,
                pool=c['pool_address'], dex=c.get('pool_dex', ''),
                price_usd=as_float(c['price']), size_usd=MICRO_35K_PAPER_SIZE_USD)
        except Exception as exc:
            # No entry on missing/stale/unsupported RPC, no route, API errors,
            # mismatched liquidity, or any non-verifiable sell quote.
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            log.warning('PAPER SHADOW ROUTE SKIP mint=%s pool=%s dex=%s reason=%s no_paper_buy=True',
                        mint, c.get('pool_address'), c.get('pool_dex'), reason)
            return False, 'readonly_sell_route_' + reason
        c.setdefault('prebuy_safety', {})['readonly_route'] = proof
        log.info('PAPER SHADOW ONCHAIN PREBUY mint=%s pool=%s slot=%s quote_min_lamports=%s '
                 'same_pool=True read_only_quote=True actual_swap_unverified=True',
                 mint, c['pool_address'], proof['rpc_slot'], proof['quote_min_sol_lamports'])
    before = len(state['positions'])
    paper_buy(state, c, size_usd=MICRO_35K_PAPER_SIZE_USD, entry_profile='early_shadow')
    if len(state['positions']) == before:
        return False, 'buy_not_placed'
    metrics = state['paper_trade_metrics'][mint]
    metrics['entry_crosscheck'] = ('pinned_pool_rpc_and_jupiter_readonly' if SHADOW_REQUIRE_READONLY_EXIT_ROUTE
                                   else 'safety_two_provider_indicative' if SHADOW_SAFETY_PREBUY_VERIFICATION
                                   else 'original_shadow_single_observation')
    if c.get('prebuy_safety'):
        metrics['prebuy_safety'] = c['prebuy_safety']
    metrics['shadow_signal_snapshot_locked'] = True
    metrics['shadow_execution_age_minutes'] = c['age_hours'] * 60
    if FRESH_BATCH_DISCOVERY_ENABLED:
        _candidate_pipeline.record_paper_entry(mint)
        _candidate_pipeline.save(force=True)
    log.info('PAPER SHADOW ORIGINAL BUY mint=%s pool=%s symbol=%s age_m=%.2f cap=$%.0f liq=$%.0f vol5=$%.0f buys=%d sells=%d change5=%.2f%% paper_size=$%.2f source=original_snapshot indicative_only=True',
             mint, c['pool_address'], c['symbol'], c['age_hours'] * 60,
             c['market_cap_usd'], c['liquidity'], c['m5_volume'],
             c['m5_buys'], c['m5_sells'], c['change_m5'],state['positions'][mint]['amount_usd'])
    return True, 'paper_shadow_bought'


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
    # The new $35K-$100K / 12-30min early tier deliberately uses a $2
    # PAPER-ONLY stake. The default $5 floor still applies elsewhere.
    micro_or_early = (entry_profile == 'early_shadow' or
                      (PAPER_MODE and MOMENTUM_SWING_MODE and size_usd is not None and
                       (0 < as_float(c.get('market_cap_usd')) < 100000 or
                        12 <= as_float(c.get('age_hours'))*60 < 30)))
    if size < (2 if micro_or_early else 5):
        return
    # Conservative paper entry haircut for spread/impact. Live costs can be worse.
    paper_cost = (SHADOW_ESTIMATED_ROUND_TRIP_COST_PCT / 100
                  if entry_profile == 'early_shadow' else ESTIMATED_ROUND_TRIP_COST)
    entry = c["price"] * (1 + paper_cost / 2)
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
        "strategy_version": "v8.6.4_original_shadow" if entry_profile == 'early_shadow' else "v8.3",
        "entry_cap_tier": ("micro_35k_100k" if 0 < as_float(c.get("market_cap_usd")) < 100000
                            else "small_100k_500k" if 0 < as_float(c.get("market_cap_usd")) < 500000
                            else "established_500k_plus"),
        "v81_exit_shadows": {"quick_4pct":{"status":"open"},"trail_3p5_1p5":{"status":"open"}},
        "entry_5m_change_pct": c.get("change_m5"),
        "fast_mover_rank": c.get("fast_mover_rank", 0),
        "take_profit_net_pct": (8.0 if entry_profile == 'early_shadow' else PAPER_FAST_TP_NET_PCT if PAPER_FAST_ONLY and c.get("fast_mover_rank",0)>0 else TAKE_PROFIT*100),
        "primary_net_observations": [],
        "strategy": "early_shadow_active" if entry_profile == 'early_shadow' else "established_momentum" if MOMENTUM_SWING_MODE else "fresh_sniper" if SNIPER_MODE else "legacy",
        "entry_market_price": c["price"],  # Before the modeled buy haircut.
        "entry_market_cap_usd": c.get("market_cap_usd"),
        "entry_age_minutes": c.get("age_hours", 0)*60,
        "entry_market_cap_source": c.get("market_cap_source"),
        "entry_1h_change_pct": c.get("change_h1"),
        "entry_6h_change_pct": c.get("change_h6"),
        "entry_24h_change_pct": c.get("change_h24"),
        "entry_buy_pressure": c.get("m5_buys", 0) / max(1, c.get("m5_buys", 0) + c.get("m5_sells", 0)),
        "entry_5m_volume_usd": c.get("m5_volume"),
        "entry_pool_liquidity_usd": c.get("liquidity"),
        "liquidity_peak_usd": c.get("liquidity"),
        "liquidity_risk_next_at": time.time() + SHADOW_LIQUIDITY_MONITOR_SECONDS,
        "max_favorable_net_pct": None, "max_adverse_net_pct": None,
        "hit_take_profit_observed": False, "seconds_to_first_take_profit_observed": None, "observed_points": {},
        "last_net_pct": None, "fallback_only_observations": 0, "unverified_exit_observations": 0,
        "last_quote_source": "", "estimated_round_trip_cost_pct": paper_cost * 100,
        "shadow_next_sample_at": (time.time() + SHADOW_SAMPLE_INTERVAL_SECONDS if entry_profile == 'early_shadow' else None),
        "recent_primary_prices": [[time.time(), c["price"]]], "reversal_exit_attempts": 0,
        "shadow_c": {"status": "open", "tp_pct": SHADOW_STRATEGY_C_TP * 100,
                     "sl_pct": SHADOW_STRATEGY_C_SL * 100, "exit_reason": None,
                     "exit_net_pct": None, "exit_elapsed_seconds": None},
    }
    log.info("PAPER ENTRY METRICS %s discovery_to_entry_s=%s fee_haircut_est_pct=%.2f",
             c["symbol"], state["paper_trade_metrics"][c["address"]]["entry_delay_seconds"], paper_cost * 100)
    state["seen_tokens"] = (state.get("seen_tokens", []) + [c["address"]])[-2000:]
    log.info("PAPER BUY %s ($%.2f) score=%.1f price=%g", c["symbol"], size, c["score"], entry)


def paper_exit_quote(pos: Position, price: float, liquidity_usd: float, *,
                     round_trip_cost: float | None = None) -> dict | None:
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
    assumed_cost = ESTIMATED_ROUND_TRIP_COST if round_trip_cost is None else round_trip_cost
    cost = max(0.0, min(0.99, assumed_cost / 2))
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
    """Record a missing quote without inventing a fill or globally halting.

    Only repeated, independently observed absent/depleted pinned-pool liquidity
    can trigger the separate zero-recovery write-off accounting policy.
    """
    now = time.time()
    issues = state.setdefault('exit_issues', {})
    item = issues.get(mint)
    if not isinstance(item, dict):
        item = {'status': 'EXIT_UNAVAILABLE', 'first_seen_at': now,
                'last_logged_at': 0.0, 'checks': 0}
        issues[mint] = item
    age = now - float(item.get('first_seen_at', now))
    next_checks = int(item.get('checks', 0)) + 1
    impaired = age >= PAPER_IMPAIRED_AFTER_SECONDS and next_checks >= PAPER_IMPAIRED_AFTER_CHECKS
    item['status'] = 'IMPAIRED_EXIT_UNAVAILABLE' if impaired else 'EXIT_UNAVAILABLE'
    item['reason'] = reason
    item['last_checked_at'] = now
    item['checks'] = next_checks
    last_log = float(item.get('last_logged_at', 0))
    if last_log <= 0 or now - last_log >= PAPER_EXIT_WARNING_INTERVAL_SECONDS:
        log.warning('PAPER SHADOW EXIT UNAVAILABLE %s mint=%s reason=%s checks=%d elapsed=%.0fs position_retained=True no_sale=True entry_safety_guard_evaluates_halt=True',
                    symbol, mint, reason, next_checks, age)
        item['last_logged_at'] = now


def pinned_pool_liquidity_evidence(pos: Position, pairs: list[dict], *, primary_healthy: bool) -> dict:
    """Interpret a *successful fresh* pool response, not a cached price.

    Missing original pool / explicit zero liquidity is evidence of a missing
    exit, but a usable alternative SOL pair rules out automatic write-off.
    A provider timeout, unsupported schema, or an excessive-impact quote by
    itself is never sufficient to declare the tokens worthless.
    """
    if SHADOW_REQUIRE_READONLY_EXIT_ROUTE:
        # Never turn a disappeared indexer result into a CONFIRMED rug.
        # PumpSwap quote-side vault reserves are an independent on-chain check.
        # Other DEX account layouts are unsupported and never autowritten off.
        dex = str(getattr(pos, 'entry_pool_dex', '') or '').lower()
        if dex != 'pumpswap' or not SOLANA_RPC_URL or not pos.entry_pool_address:
            return {'verified': False, 'reason': 'onchain_writeoff_adapter_unavailable'}
        try:
            snapshot = pump_pool_snapshot(SOLANA_RPC_URL, pos.entry_pool_address, pos.token)
        except Exception as exc:
            return {'verified': False, 'reason': 'onchain_pool_read_unavailable_' + type(exc).__name__}
        if snapshot.effective_quote_raw > 10_000_000 and snapshot.base_raw > 0:
            return {'verified': False, 'reason': 'onchain_pool_reserves_still_present',
                    'rpc_slot': snapshot.slot}
        # A router may find liquidity outside the pinned pool. A fresh known
        # viable SOL route disqualifies even verified pinned-pool depletion.
        if not primary_healthy:
            return {'verified': False, 'reason': 'alternate_route_unverified'}
        supported = [p for p in pairs if p.get('chainId') == 'solana' and
                     (p.get('baseToken') or {}).get('address') == pos.token and
                     (p.get('quoteToken') or {}).get('address') == SOL_MINT]
        if any(p.get('pairAddress') != pos.entry_pool_address and
               paper_exit_quote(pos, as_float(p.get('priceUsd')),
                                as_float((p.get('liquidity') or {}).get('usd'))) is not None
               for p in supported):
            return {'verified': False, 'reason': 'alternative_liquid_sol_pool_found'}
        return {'verified': True, 'reason': 'onchain_pinned_pool_depleted',
                'rpc_slot': snapshot.slot,
                'quote_raw': snapshot.effective_quote_raw,
                'base_raw': snapshot.base_raw,
                'evidence_source': 'solana_rpc_confirmed'}
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
    """Migrates ONLY a clearly attributed old exit-issue halt; never a risk halt."""
    restore_legacy_exit_issue_halt(state)


def restore_legacy_exit_issue_halt(state: dict) -> None:
    # Legacy v8.6.3 halted globally on any missing exit quote, and wrote a
    # sentinel proving no halt existed beforehand. Don't override a preexisting
    # halt or an unknown/manual one.
    if state.get('halted_before_exit_issues') is not False:
        return
    baseline = as_float(state.get('day_start_equity'), STARTING_CAPITAL)
    loss_hit = ((baseline - equity(state)) / max(1., baseline) >= MAX_DAILY_LOSS_FRACTION)
    if state.get('daily_loss_halt') or state.get('manual_halt') or loss_hit:
        return
    state.pop('halted_before_exit_issues', None)
    state.pop('exit_recovery_review_required', None)
    if state.get('halted'):
        state['halted'] = False
        log.info('PAPER SHADOW LEGACY EXIT HALT MIGRATED previously_unhalted=True quote_issues_retained=True risk_halts_unchanged=True')


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
        "rug_evidence": evidence, "exit_failure_class": classify_unavailable_exit(str(issue.get("reason") or ""), evidence),
        "intentional_rug_proven": False, "entry_pool_address": pos.entry_pool_address,
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
    if metrics.get('strategy') == 'early_shadow_active':
        log.info('PAPER SHADOW LIQUIDITY PERFORMANCE version=v8.6.4_original_shadow tiers=%s paper_only=True',
                 liquidity_trade_breakdown(state['closed_trades']))
    release_halt_after_rug_writeoff(state)
    if SHADOW_WRITEOFF_CIRCUIT_ENABLED:
        utc_day = datetime.now(timezone.utc).date().isoformat()
        writeoffs_today = sum(t.get('execution') == 'paper_writeoff_no_sale' and
                              str(t.get('closed_at', '')).startswith(utc_day)
                              for t in state.get('closed_trades', []))
        if writeoffs_today >= SHADOW_MAX_DAILY_WRITEOFFS:
            state['shadow_writeoff_halt'] = True
            state['halted'] = True
            log.error('PAPER SHADOW WRITEOFF CIRCUIT HIT writeoffs_utc_today=%d limit=%d halted=True review_required=True',
                      writeoffs_today, SHADOW_MAX_DAILY_WRITEOFFS)
    return True


def maybe_write_off_rugged_position(state: dict, mint: str, evidence: dict | None) -> bool:
    """Require a continuous 300s evidence window, not merely an old quote outage.

    The entry/exit signals are intentionally untouched. Only independently
    timed healthy API observations of drained/missing SOL liquidity qualify.
    An unavailable response or a viable alternate pool breaks the evidence
    sequence, but it never turns a position into a simulated sale.
    """
    if not PAPER_RUG_WRITEOFF_ENABLED:
        return False
    issue = state.get("exit_issues", {}).get(mint)
    if not issue or mint not in state.get("positions", {}):
        return False
    if not evidence or not evidence.get("verified"):
        # Missing, stale and contradictory observations are NOT confirmations.
        for field in ("rug_confirmed_checks", "last_rug_confirmed_at",
                      "rug_evidence_first_at"):
            issue.pop(field, None)
        return False
    now = time.time()
    prev = float(issue.get("last_rug_confirmed_at", -1))
    if prev > now:
        # Clock moved backwards; discard any previously accumulated evidence.
        for field in ("rug_confirmed_checks", "last_rug_confirmed_at",
                      "rug_evidence_first_at"):
            issue.pop(field, None)
        prev = -1
    if now - prev >= 1.0:
        if "rug_evidence_first_at" not in issue:
            issue["rug_evidence_first_at"] = now
            issue["rug_confirmed_checks"] = 0
        issue["rug_confirmed_checks"] = int(issue.get("rug_confirmed_checks", 0)) + 1
        issue["last_rug_confirmed_at"] = now
    evidence_age = max(0.0, now - float(issue.get("rug_evidence_first_at", now)))
    outage_age = max(0.0, now - float(issue.get("first_seen_at", now)))
    checks = int(issue.get("rug_confirmed_checks", 0))
    if evidence_age >= PAPER_RUG_WRITEOFF_AFTER_SECONDS and checks >= PAPER_RUG_WRITEOFF_MIN_CHECKS:
        return write_off_rugged_paper_position(state, mint, {
            "reason": evidence.get("reason"), "exit_failure_class": classify_unavailable_exit(str(issue.get("reason") or ""), evidence),
            "unavailable_seconds": round(outage_age, 1),
            "confirmed_evidence_seconds": round(evidence_age, 1),
            "healthy_pool_checks": checks, "original_failure": issue.get("reason"),
            "note": "zero-recovery paper assumption after continuous evidence; permanence not provable",
            "evidence_source": evidence.get("evidence_source", "provider_indicative"),
            "rpc_slot": evidence.get("rpc_slot"),
        })
    if checks <= PAPER_RUG_WRITEOFF_MIN_CHECKS:
        log.info("PAPER RUG WATCH mint=%s primary_evidence=%s evidence_age=%.0fs outage_age=%.0fs confirmed_checks=%d/%d writeoff_after=%ds",
                 mint, evidence.get("reason"), evidence_age, outage_age, checks,
                 PAPER_RUG_WRITEOFF_MIN_CHECKS, PAPER_RUG_WRITEOFF_AFTER_SECONDS)
    return False


def clear_exit_issue(state: dict, mint: str, symbol: str) -> None:
    """Original shadow sampling resumes when the same-pool quote recovers."""
    item = state.get('exit_issues', {}).pop(mint, None)
    if item:
        log.info('PAPER SHADOW EXIT QUOTE RECOVERED %s mint=%s outage_checks=%d no_paper_sale_assumed=True',
                 symbol, mint, int(item.get('checks', 0)))


def close_position(state: dict, mint: str, price: float, reason: str, liquidity_usd: float | None = None,
                   quote_source: str = "", *, prechecked_route_proof: dict | None = None) -> bool:
    raw = state["positions"].get(mint)
    if not raw:
        return False
    pos = Position(**raw)
    failure = paper_exit_failure_reason(pos, price, liquidity_usd or 0, quote_source)
    active_metrics = state.get('paper_trade_metrics', {}).get(mint, {})
    cost_for_position = (SHADOW_ESTIMATED_ROUND_TRIP_COST_PCT / 100 if
                         active_metrics.get('strategy') == 'early_shadow_active' else ESTIMATED_ROUND_TRIP_COST)
    quote = None if failure else paper_exit_quote(pos, price, liquidity_usd or 0,
                                                    round_trip_cost=cost_for_position)
    if quote is None:
        if active_metrics.get('strategy') == 'early_shadow_active':
            shadow_pending_exit(state, mint, reason)
        note_unavailable_exit(state, mint, pos.symbol, failure or "exit_quote_unavailable")
        return False
    route_proof = None
    if active_metrics.get('strategy') == 'early_shadow_active':
        if prechecked_route_proof and SHADOW_EXIT_ROUTE_QUOTE_REQUIRED:
            # Only provided by the synchronous same-tick probe, which itself
            # fetched a direct, pool-pinned Jupiter quote; never persistent.
            route_ok, route_reason, route_proof = True, 'fresh_same_tick_probe', prechecked_route_proof
        else:
            route_ok, route_reason, route_proof = shadow_exit_route_check(state, mint, pos)
        if not route_ok:
            shadow_pending_exit(state, mint, reason)
            note_unavailable_exit(state, mint, pos.symbol, route_reason)
            log.warning('PAPER SHADOW EXIT ROUTE UNAVAILABLE mint=%s reason=%s no_sale_recorded=True',
                        mint, route_reason)
            return False
    clear_exit_issue(state, mint, pos.symbol)
    exit_price = quote["exit_price"]
    proceeds = quote["proceeds_usd"]
    # A read-only router quote is SOL-denominated. Use the ENTRY quote for
    # the same token amount as a conservative approximate USD anchor, rather
    # than treating an indexer's USD paper estimate as realizable proceeds.
    # SOL/USD itself may move; this remains paper modeling, NOT a real fill.
    if route_proof:
        entry_route = active_metrics.get('prebuy_safety', {}).get('readonly_route', {})
        anchor_lamports = as_float(entry_route.get('quote_min_sol_lamports'))
        anchor_raw = as_float(entry_route.get('token_amount_raw'))
        current_raw = as_float(route_proof.get('in_amount_raw'))
        if anchor_lamports > 0 and anchor_raw > 0 and current_raw > 0:
            anchor_for_holding = anchor_lamports * (current_raw / anchor_raw)
            anchored_value_usd = pos.amount_usd * route_proof['min_lamports'] / anchor_for_holding
            if math.isfinite(anchored_value_usd) and anchored_value_usd > 0:
                if abs(anchored_value_usd - proceeds) / max(proceeds, 1e-9) > .20:
                    log.warning('PAPER SHADOW EXIT VALUATION DIVERGENCE mint=%s dex_estimate=$%.4f '
                                'router_anchor_estimate=$%.4f conservative_min_used=True no_real_fill=True',
                                mint, proceeds, anchored_value_usd)
                proceeds = min(proceeds, anchored_value_usd)
                exit_price = proceeds / pos.token_units
                active_metrics['exit_router_anchor_estimate_usd'] = round(anchored_value_usd, 8)
    pnl = proceeds - pos.amount_usd
    state["cash_usd"] += proceeds
    metrics = state.setdefault("paper_trade_metrics", {}).pop(mint, {})
    if metrics.get('pending_exit_first_at'):
        metrics['exit_intent_latency_seconds'] = round(max(0., time.time() -
                                                float(metrics['pending_exit_first_at'])), 3)
    if metrics.get("max_favorable_net_pct") is not None:
        metrics["hit_take_profit_observed"] = bool(metrics.get("hit_take_profit_observed") or metrics["max_favorable_net_pct"] >= as_float(metrics.get("take_profit_net_pct"), TAKE_PROFIT*100))
    for shadow_name, shadow_info in metrics.get('v81_exit_shadows', {}).items():
        if shadow_info.get('status')=='open':
            shadow_info.update(status='not_observed_before_actual_exit')
    log.info("PAPER V81 EXIT COMPARISON %s version=%s actual_reason=%s actual_net_pct=%.3f shadows=%s indicative_only=True",
             pos.symbol, metrics.get('strategy_version','legacy'), reason,
             pnl / max(pos.amount_usd,1e-15)*100, metrics.get('v81_exit_shadows', {}))
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
             metrics.get("seconds_to_first_take_profit_observed"), metrics.get("observed_points", {}), cost_for_position * 100, quote["impact_pct"])
    state["closed_trades"].append({
        "closed_at": now_iso(), "token": mint, "symbol": pos.symbol,
        "entry_price": pos.entry_price, "exit_price": exit_price,
        "invested_usd": pos.amount_usd, "proceeds_usd": proceeds,
        "pnl_usd": pnl, "pnl_pct": pnl / max(1.0, pos.amount_usd) * 100,
        "reason": reason, "paper_metrics": metrics, "estimated_impact_pct": quote["impact_pct"],
        "pool_liquidity_usd": quote["liquidity_usd"], "quote_source": quote_source or "primary_pool_indicative",
        "execution": "paper_quote_backed_estimate" if route_proof else "paper_estimate_only",
        "exit_readonly_route": ({"pool": pos.entry_pool_address,
                                  "minimum_sol_lamports": route_proof['min_lamports'],
                                  "route_checked_at": time.time()} if route_proof else None)
    })
    state["closed_trades"] = state["closed_trades"][-1000:]
    if PAPER_MODE:
        performance_version = metrics.get('strategy_version') or 'v8.3'
        v81 = summarize_version_trades(state["closed_trades"], version=performance_version)
        log.info("PAPER PERFORMANCE VERSION version=%s trades=%d wins=%d losses=%d win_rate_pct=%s net_pnl_usd=%.3f avg_win_usd=%s avg_loss_usd=%s expectancy_usd=%s by_tier=%s exit_reasons=%s historical_versions_excluded=True indicative_only=True",
                 v81['version'], v81['trades'], v81['wins'], v81['losses'], v81['win_rate_pct'],
                 v81['net_pnl_usd'], v81['avg_win_usd'], v81['avg_loss_usd'], v81['expectancy_usd'],
                 v81['by_tier'], v81['reasons'])
        if metrics.get('strategy') == 'early_shadow_active':
            log.info('PAPER SHADOW LIQUIDITY PERFORMANCE version=v8.6.4_original_shadow tiers=%s indicative_only=True',
                     liquidity_trade_breakdown(state['closed_trades']))
        report = paper_trade_scorecard(state["closed_trades"])
        log.info("PAPER PERFORMANCE last_%d_trades=%d wins=%d win_rate_pct=%s net_pnl_usd=%.2f median_net_pct=%s indicative_only=True",
                 30, report["trades"], report["wins"], report["win_rate_pct"],
                 report["net_pnl_usd"], report["median_return_pct"])
    del state["positions"][mint]
    # Price observations from the prior holding must never confirm a new buy.
    _candidate_price_history.pop(mint, None)
    _pending_entry_mints.pop(mint, None)
    if metrics.get('strategy') == 'early_shadow_active' and pnl / max(pos.amount_usd, 1e-15) * 100 <= -SHADOW_CATASTROPHIC_CLOSED_LOSS_HALT_PCT:
        state['shadow_exit_safety_halt'] = True
        state['shadow_exit_safety_reason'] = 'catastrophic_closed_shadow_loss'
        state['shadow_exit_safety_mint'] = mint
        state['shadow_exit_safety_at'] = time.time()
        state['halted'] = True
        log.error('PAPER SHADOW CATASTROPHIC LOSS CIRCUIT mint=%s pnl_pct=%.2f%% threshold=-%.1f%% '
                  'new_entries_halted=True manual_review_required=True stop_price_not_guaranteed=True',
                  mint, pnl / max(pos.amount_usd, 1e-15) * 100, SHADOW_CATASTROPHIC_CLOSED_LOSS_HALT_PCT)
    log.info("PAPER SELL %s reason=%s PnL=$%.2f (%.1f%%) impact≈%.2f%% "
             "route_quote=%s [paper estimate; no actual fill]", pos.symbol, reason, pnl,
             pnl / max(1.0, pos.amount_usd) * 100, quote["impact_pct"],bool(route_proof))
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
        relationships = pool_data.get("relationships") or {}
        base_id = (((relationships.get("base_token") or {}).get("data") or {}).get("id", ""))
        quote_id = (((relationships.get("quote_token") or {}).get("data") or {}).get("id", ""))
        if mint and base_id != "solana_" + mint:
            raise ValueError("fallback pool base token does not match held mint")
        if quote_id != "solana_" + SOL_MINT:
            raise ValueError("fallback pool is not SOL-quoted")
        if (pool_data.get('id') or '') != 'solana_' + pool_address:
            raise ValueError("fallback pool identity does not match")
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
            # No executable evidence; do not claim a sell at a cached last_price.
            last_valid = pos.last_price_at or pos.opened_at
            stale_for = max(0.0, time.time() - last_valid)
            metrics = state.setdefault('paper_trade_metrics', {}).setdefault(mint, {})
            if metrics.get('strategy') == 'early_shadow_active' and time.time() - pos.opened_at >= 45 * 60:
                shadow_pending_exit(state, mint, 'shadow_45m_expiry')
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
        if (metrics.get('strategy') == 'early_shadow_active' and
                (source != 'dexscreener' or c.get('pool_address') != pos.entry_pool_address)):
            # Secondary/indexer-assisted valuations are not verified sells.
            failure = 'shadow_pinned_primary_quote_unavailable'
        cost_for_position = (SHADOW_ESTIMATED_ROUND_TRIP_COST_PCT / 100 if
                             metrics.get('strategy') == 'early_shadow_active' else ESTIMATED_ROUND_TRIP_COST)
        quote = None if failure else paper_exit_quote(pos, price, liquidity,
                                                        round_trip_cost=cost_for_position)
        if quote is None:
            metrics["unverified_exit_observations"] = int(metrics.get("unverified_exit_observations", 0)) + 1
            if metrics.get('strategy') == 'early_shadow_active' and time.time() - pos.opened_at >= 45 * 60:
                shadow_pending_exit(state, mint, 'shadow_45m_expiry')
            note_unavailable_exit(state, mint, pos.symbol, failure or "exit_quote_unavailable")
            maybe_write_off_rugged_position(state, mint, exit_evidence.get(mint))
            continue
        # A pool that has lost entry-grade depth is a deteriorating exit even
        # if a small modeled paper sale is still possible. Watch it on every
        # tick, not only on the 180-second shadow sample boundary.
        if (metrics.get('strategy') == 'early_shadow_active' and
                liquidity < SHADOW_MIN_LIQUIDITY_USD):
            note_unavailable_exit(state, mint, pos.symbol,
                                  'pinned_pool_below_entry_liquidity_exit_watch')
        else:
            clear_exit_issue(state, mint, pos.symbol)
        net_return = quote["proceeds_usd"] / max(pos.amount_usd, 1e-15) - 1
        metrics = state.setdefault("paper_trade_metrics", {}).setdefault(mint, {})
        net_pct = net_return * 100
        old_max = metrics.get("max_favorable_net_pct")
        old_min = metrics.get("max_adverse_net_pct")
        metrics["max_favorable_net_pct"] = max(old_max, net_pct) if old_max is not None else net_pct
        metrics["max_adverse_net_pct"] = min(old_min, net_pct) if old_min is not None else net_pct
        metrics['last_net_pct'] = net_pct
        if metrics.get('strategy') == 'early_shadow_active':
            observed_at = time.time()
            trace = metrics.setdefault('shadow_primary_quote_trace', [])
            previous_at = as_float(trace[-1][0]) if trace else as_float(pos.opened_at)
            metrics['max_primary_observation_gap_s'] = max(
                as_float(metrics.get('max_primary_observation_gap_s')),
                max(0., observed_at - previous_at))
            # Keep a bounded record; do not treat repeated indexer quotes as
            # independently fresh blockchain trades.
            trace.append([round(observed_at, 3), price, round(liquidity, 2),
                          round(net_pct, 3), source])
            del trace[:-80]
            route_proof = None
            # Exit signals found by fresh primary observations go FIRST; an
            # optional health probe must not consume the route retry budget.
            needs_direct_exit = (bool(metrics.get('pending_exit_reason')) or
                                 observed_at - pos.opened_at >= 45 * 60 or
                                 (SHADOW_PROTECTIVE_EXIT_ENABLED and
                                  net_pct <= SHADOW_PROTECTIVE_NET_STOP_PCT) or
                                 (as_float(metrics.get('entry_pool_liquidity_usd')) >= SHADOW_MIN_LIQUIDITY_USD and
                                  liquidity < as_float(metrics.get('entry_pool_liquidity_usd')) *
                                  (1. - SHADOW_LIQUIDITY_EXIT_DROP_PCT / 100.)))
            if (not needs_direct_exit and source == 'dexscreener' and
                    c.get('pool_address') == pos.entry_pool_address):
                routed_stop, route_proof = shadow_held_route_probe(state, mint, pos, metrics,
                                                                   now=observed_at)
                if routed_stop:
                    shadow_pending_exit(state, mint, 'shadow_routed_downside_stop')
                    close_position(state, mint, price, metrics['pending_exit_reason'],
                                   liquidity, source, prechecked_route_proof=route_proof)
                    continue

            # An existing sell request takes priority after quote recovery. A
            # timeout is an EXIT INTENT, not a guaranteed sale at expiry.
            if metrics.get('pending_exit_reason'):
                close_position(state, mint, price, metrics['pending_exit_reason'], liquidity, source)
                continue  # Retain first exit intent until a verified quote allows it.
            # Expiry must be checked on every refresh, not only when the 180s
            # sample timer fires or when the pool retains entry-grade depth.
            if time.time() - pos.opened_at >= 45 * 60:
                shadow_pending_exit(state, mint, 'shadow_45m_expiry')
                close_position(state, mint, price, metrics['pending_exit_reason'], liquidity, source)
                continue
            # Protective exits can use remaining POSITIVE pinned liquidity even
            # when it has fallen below the ENTRY liquidity threshold. The
            # actual quote validity/impact and router gate still apply.
            if SHADOW_PROTECTIVE_EXIT_ENABLED and source == 'dexscreener' and c.get('pool_address') == pos.entry_pool_address:
                if net_pct <= SHADOW_PROTECTIVE_NET_STOP_PCT:
                    shadow_pending_exit(state, mint, 'shadow_protective_stop')
                    log.warning('PAPER SHADOW PROTECTIVE STOP mint=%s observed_net=%.2f%% threshold=%.2f%% '
                                'liquidity=$%.0f detection_is_not_fill=True',
                                mint, net_pct, SHADOW_PROTECTIVE_NET_STOP_PCT, liquidity)
                    close_position(state, mint, price, metrics['pending_exit_reason'], liquidity, source)
                    continue
                entry_mkt = as_float(metrics.get('entry_market_price'))
                peak_return_pct = (pos.peak_price / entry_mkt - 1.) * 100 if entry_mkt > 0 else 0.
                decline_pct = (1. - price / pos.peak_price) * 100 if pos.peak_price > 0 else 0.
                if peak_return_pct >= SHADOW_PROTECTIVE_PEAK_ARM_PCT and decline_pct >= SHADOW_PROTECTIVE_PEAK_DROP_PCT:
                    shadow_pending_exit(state, mint, 'shadow_protective_reversal')
                    log.warning('PAPER SHADOW PROTECTIVE REVERSAL mint=%s peak_up=%.2f%% reversal=%.2f%% '
                                'observed_net=%.2f%% indicative_only=True',
                                mint, peak_return_pct, decline_pct, net_pct)
                    close_position(state, mint, price, metrics['pending_exit_reason'], liquidity, source)
                    continue
            # Rapid same-pool liquidity deterioration warrants an exit attempt,
            # but *only* if the current routed quote validates a sale estimate.
            entry_liq = as_float(metrics.get('entry_pool_liquidity_usd'))
            if (entry_liq >= SHADOW_MIN_LIQUIDITY_USD and
                    liquidity < entry_liq * (1. - SHADOW_LIQUIDITY_EXIT_DROP_PCT / 100.)):
                shadow_pending_exit(state, mint, 'shadow_liquidity_protective_exit')
                close_position(state, mint, price, metrics['pending_exit_reason'], liquidity, source)
                continue
            # Original shadow TP/SL tracker still uses 180-second price-path decisions.
            # The independent paper write-off monitor runs when a pool cannot
            # be valued/sold; no automatic liquidity-deterioration sale here.
            # Shadow observations were sampled every 180 seconds. Keep those
            # decision times and original 8/-12/45m price-path rules, not the
            # regular fast trail, legacy stop, momentum or reversal exits.
            # Quote prices may update more frequently for portfolio valuation.
            sample_now = time.time()
            due = as_float(metrics.get('shadow_next_sample_at'), pos.opened_at + SHADOW_SAMPLE_INTERVAL_SECONDS)
            if sample_now >= due:
                metrics['shadow_next_sample_at'] = sample_now + SHADOW_SAMPLE_INTERVAL_SECONDS
                # Match the shadow observer's pool_evidence() requirements
                # for subsequent samples: same primary SOL pool, 15k depth,
                # reported market cap and valid original pool creation time.
                shadow_pool_cap = as_float(c.get('shadow_market_cap', c.get('market_cap_usd')))
                shadow_pool_age = as_float(c.get('shadow_age_minutes',
                                                 as_float(c.get('age_hours')) * 60), -1)
                valid_shadow_pool = (source == 'dexscreener'
                                     and c.get('pool_address') == pos.entry_pool_address
                                     and liquidity >= SHADOW_MIN_LIQUIDITY_USD
                                     and shadow_pool_cap > 0
                                     and 0 <= shadow_pool_age < 525600)
                if valid_shadow_pool:
                    decision, sampled_pct = shadow_exit_signal(
                        as_float(metrics.get('entry_market_price')), price,
                        sample_now - pos.opened_at)
                    metrics['shadow_sample_count'] = int(metrics.get('shadow_sample_count', 0)) + 1
                    metrics['shadow_last_sample_net_pct'] = sampled_pct
                    log.info('PAPER SHADOW ACTIVE SAMPLE mint=%s sampled_net_pct=%.2f%% portfolio_est_net_pct=%.2f%% held_m=%.1f decision=%s primary_pool=True model_cost_pct=6 fee_and_impact_estimates_only=True',
                             mint, sampled_pct, net_pct, (sample_now-pos.opened_at)/60,
                             decision or 'hold')
                    if decision:
                        shadow_pending_exit(state, mint, decision)
                        close_position(state, mint, price, metrics['pending_exit_reason'], liquidity,
                                       source, prechecked_route_proof=route_proof)
                else:
                    note_unavailable_exit(state, mint, pos.symbol, 'shadow_unverified_pool_or_below_entry_liquidity')
                    log.info('PAPER SHADOW ACTIVE SAMPLE mint=%s status=unverified_primary_or_low_liquidity no_price_path_exit=True', mint)
            continue
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
        profit_lock, profit_lock_diag = False, {"status":"not_primary"}
        fast_loss, fast_loss_diag = False, {"status":"not_primary"}
        if primary_eligible:
            primary_net = metrics.setdefault("primary_net_observations", [])
            primary_net.append([time.time(), net_pct, price])
            primary_net[:] = [x for x in primary_net if 0 <= time.time()-x[0] <= 3600][-150:]
            if metrics.get('strategy_version') in ('v8.1', 'v8.3'):
                update_exit_shadows(metrics, primary_net, now=time.time(), opened_at=pos.opened_at)
            if PAPER_PROFIT_LOCK_V81 and metrics.get('strategy_version') in ('v8.1', 'v8.3'):
                profit_lock, profit_lock_diag = confirmed_profit_lock(
                    primary_net, now=time.time(), opened_at=pos.opened_at)
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
        elif profit_lock:
            log.info("PAPER POSITIVE PROFIT LOCK %s net_pct=%.2f%% peak=%s floor=%s primary_pool_confirmed=True indicative_only=True",
                     pos.symbol, net_pct, profit_lock_diag.get('peak_net_pct'), profit_lock_diag.get('target_net_pct'))
            close_position(state, mint, price, "confirmed_positive_profit_lock", liquidity, source)
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
    "too_new_for_established_strategy", "market_cap_below_range",
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
    if mint in _event_recheck_mints and FRESH_BATCH_DISCOVERY_ENABLED and mint in _fresh_batch.active:
        # Fresh migration notice grants exactly one new DATA lookup. This
        # doesn't approve an entry or manufacture evidence of SOL liquidity.
        return False
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
        if mint in held or mint in _event_recheck_mints:
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
        for mint in addresses:
            if len(selected) >= cap:
                break
            if mint in _fresh_batch.active and mint not in selected and entry_allowed(mint):
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



def promoted_held_pools() -> dict[str,str]:
    """Read-only snapshot of PRIMARY (promoted) v8.7.6 0_60 held pools."""
    with _age_study_lock:
        primary = _age_study.tracks[0]
        primary.load()
        return {mint: rec['pool'] for mint,rec in primary.positions.items()}


def promoted_ws_observation(mint: str, pinned_pool: str) -> None:
    """WS wake -> bounded HTTP snapshot -> UNMODIFIED AgeTrack.observe.

    No WebSocket notification itself can trigger a trade, as it lacks a USD
    price/volume snapshot. Never rely on a pool with a different ID.
    """
    global _age_ws_last_fetch_at, _age_ws_backoff_until
    global _live_ws_last_global_quote_at
    if not AGE_0_60_PRIMARY or not AGE_0_60_WS_ENABLED:return
    # For LIVE positions the authenticated on-chain pool notification triggers
    # an executable Jupiter quote *immediately*; no DexScreener HTTP fetch.
    # The Jupiter quote (not pool prices, chart MC, or WS payload) still
    # authorizes all net-return exits. Scheduled real pool observations retain
    # liquidity-collapse authority. Paper-only observers retain the old route.
    if LIVE_0_60_ENABLED:
        stamp=time.monotonic()
        if (stamp-_live_ws_last_quote_at.get(mint,0)<1.0 or
            stamp-_live_ws_last_global_quote_at<0.35):
            return
        if not _age_ws_fetch_guard.acquire(blocking=False):return
        try:
            with _age_study_lock:
                primary=_age_study.tracks[0]
                if primary.positions.get(mint,{}).get('pool')!=pinned_pool:return
                _live_ws_last_quote_at[mint]=stamp
                _live_ws_last_global_quote_at=stamp
                before=time.monotonic()
                event=primary.observe_live_wakeup(mint,pinned_pool)
                primary.save()
                if event.startswith('closed:') or event=='live_sell_pending':
                    log.warning('LIVE 0_60 FAST WS EXIT mint=%s event=%s elapsed_ms=%d '
                                'authority=fresh_jupiter_quote_or_pending_exit',
                                mint,event,round((time.monotonic()-before)*1000))
                elif event=='observed' and stamp-_live_ws_last_telemetry_at.get(mint,0)>7.:
                    _live_ws_last_telemetry_at[mint]=stamp
                    record=primary.positions.get(mint,{})
                    log.info('LIVE 0_60 FAST WS QUOTE mint=%s net_pct=%s peak_net_pct=%s '
                             'elapsed_ms=%d price_source=jupiter_executable_quote no_dex_http=True',
                             mint,record.get('last_executable_quote_net_pct'),
                             record.get('peak_net_pct'),round((time.monotonic()-before)*1000))
                elif event=='live_exit_quote_unavailable':
                    log.info('LIVE 0_60 FAST WS quote_unavailable mint=%s '
                             'no_synthetic_price_exit=True',mint)
        finally:
            _age_ws_fetch_guard.release()
        return
    now=time.monotonic()
    if now < _age_ws_backoff_until:return
    if not _age_ws_fetch_guard.acquire(blocking=False):return
    try:
        now=time.monotonic()
        if now-_age_ws_last_per_mint.get(mint,0)<2.0 or now-_age_ws_last_fetch_at<1.5:return
        if promoted_held_pools().get(mint)!=pinned_pool:return
        _age_ws_last_per_mint[mint]=now
        _age_ws_last_fetch_at=now
        # Dedicated one-shot HTTP request: do not race the primary scanner's
        # requests.Session or shared global API cooldown counters.
        try:
            response=requests.get(f'{BASE}/token-pairs/v1/solana/{mint}', timeout=6,
                                  headers={'User-Agent':'SolanaMemeRiskBot/0.1'})
            if response.status_code==429:
                _age_ws_backoff_until=time.monotonic()+60
                log.warning('PAPER PRIMARY WS QUOTE HTTP429 REST_polling_fallback=True')
                return
            response.raise_for_status()
            pairs=response.json()
            if not isinstance(pairs,list):return
        except (requests.RequestException,ValueError,TypeError):
            return
        pinned=next((p for p in pairs if isinstance(p,dict) and
            p.get('pairAddress')==pinned_pool and
            (p.get('baseToken') or {}).get('address')==mint),None)
        if pinned is None:return
        with _age_study_lock:
            if promoted_held_pools().get(mint)!=pinned_pool:return
            # WS cannot alter the separate 5_60 study: only the promoted
            # 0_60 track gets this extra observation.
            primary=_age_study.tracks[0]
            result=primary.observe(pinned,sol_mint=SOL_MINT,
                dexes=active_entry_routes(),surge_change=SHADOW_SURGE_CHANGE_5M_PCT,
                surge_volume=SHADOW_SURGE_MIN_5M_VOLUME_USD)
            if result not in ('unverified_pool','different_pool','read_only'):
                _age_study.last_lookup[mint]=time.time()
            primary.save()
        if result=='opened' or result.startswith('closed:'):
            log.info('PAPER PRIMARY 0_60 EVENT source=solana_ws_wakeup event=%s mint=%s '
                     'pool=%s primary_tp_net=25pct profit_reversal_enabled=True live_mode=%s',result,mint,pinned_pool,LIVE_0_60_ENABLED)
    finally:
        _age_ws_fetch_guard.release()


def start_promoted_ws() -> None:
    global _age_ws
    if not AGE_0_60_PRIMARY:return
    if not AGE_0_60_WS_ENABLED:
        log.warning('PAPER PRIMARY 0_60 websocket_disabled REST_observation_continues=True')
        return
    if not SOLANA_RPC_URL and not AGE_0_60_WS_RPC_URL:
        log.warning('PAPER PRIMARY WS missing_RPC_URL fallback_rest=True')
        return
    try:
        url=ws_url_for_rpc(SOLANA_RPC_URL,AGE_0_60_WS_RPC_URL)
    except ValueError:
        log.warning('PAPER PRIMARY WS invalid_url fallback_rest=True')
        return
    _age_ws=SolanaPoolWatcher(rpc_ws_url=url,held_pools=promoted_held_pools,
                              on_change=promoted_ws_observation,
                              async_callback=LIVE_0_60_ENABLED)
    _age_ws.start()
    log.info('PRIMARY 0_60 WS starting mode=accountSubscribe commitment=processed held_pools_only=True '
             'live_fast_executable_quote=%s paper_dex_snapshot=%s live_tp_net=40pct live_trail_arm=10pct live_trail_drop=5points '
             'live_mode=%s',LIVE_0_60_ENABLED,not LIVE_0_60_ENABLED,LIVE_0_60_ENABLED)


def run_background_age_study(*, scan_entries: bool) -> None:
    """Bounded independent read-only checks; cannot submit primary entries."""
    global _age_study_last_stats_at, _v893_last_audit_at
    if not SHADOW_AGE_STUDY_ENABLED:
        return
    stamp=time.time()
    # v9.0.2: Drain completed on-chain checks even when there are no fresh
    # DexScreener pairs or no entry scan. Does not authorize an entry or
    # fabricate a pool snapshot; every purchase still runs fresh gates.
    if _micro_v895_rug is not None and hasattr(_micro_v895_rug,'drain_completed'):
        with _age_study_lock:
            _micro_v895_rug.drain_completed(stamp)
    if _v893_risk is not None and hasattr(_v893_risk,'drain_completed'):
        with _age_study_lock:
            _v893_risk.drain_completed(stamp)
    # Standard 3-study lookups stay unchanged. Under a genuine discovery
    # backlog, add up to three MICRO-ONLY lookups; never expand other tracks.
    # A completed async RPC/Jupiter result can also prompt a fresh-pair
    # recheck between ordinary entry scans (bounded at 12/min globally).
    _micro_fast_followup_window[:] = [t for t in _micro_fast_followup_window
                                      if stamp-t<60]
    _micro_urgent_reserve_window[:] = [t for t in _micro_urgent_reserve_window
                                       if stamp-t<60]
    extra_mints=set()
    due_mints=[]
    if api_cooldown_remaining()<=0:
        with _age_study_lock:
            if scan_entries:
                normal_limit=min(3,SHADOW_AGE_STUDY_FETCH_PER_SCAN)
                # Held mandatory exits get first look at available slots,
                # before entry discovery and its less urgent callback queue.
                urgent=_age_study.micro_urgent_held(now=stamp,limit=3)
                due_mints=list(urgent)
                extra_mints.update(urgent)
                due_mints.extend(m for m in _age_study.due(now=stamp,limit=normal_limit)
                                 if m not in extra_mints)
                unscanned=_fresh_batch.stats().get('unscanned',0) if V893_RESEARCH_ENABLED else 0
                # A quiet discovery backlog must NOT disable micro-only
                # verification followups. Fill spare lookup slots with ready
                # candidates regardless of batch-discovery queue depth.
                if V893_RESEARCH_ENABLED and normal_limit<6:
                    # v908: guarantee one bounded lookup lane for already
                    # LP-verified pools with a mature on-chain reserve baseline.
                    # This is ONLY a lookup, never a synthetic safety pass.
                    history_due=_age_study.micro_history_due(
                        now=stamp,limit=min(1,max(0,6-len(due_mints))),exclude=due_mints)
                    due_mints.extend(history_due)
                    extra_mints.update(history_due)
                    micro_extra=_age_study.micro_extra_due(
                        now=stamp,limit=max(0,6-len(due_mints)),exclude=due_mints)
                    extra_mints.update(micro_extra)
                    due_mints.extend(micro_extra)
                    log.info('V908 MICRO RESERVE HISTORY SCHEDULER matured_reserved=%d '
                             'micro_extra=%d total_due=%d fresh_pool_required=True',
                             len(history_due),len(micro_extra),len(due_mints))
                log.info('V897 MICRO LOOKUP SCHEDULER backlog_unscanned=%d normal_budget=%d micro_extra=%d '
                         'total_selected=%d extra_even_without_discovery_backlog=True',
                         unscanned,normal_limit,len(extra_mints),len(due_mints))
            elif V893_RESEARCH_ENABLED:
                allowance=min(3,max(0,24-len(_micro_fast_followup_window)))
                if allowance:
                    urgent=_age_study.micro_urgent_held(now=stamp,limit=allowance)
                    follow=_age_study.micro_ready_followups(
                        now=stamp,limit=max(0,allowance-len(urgent)),exclude=urgent)
                    due_mints=(urgent+follow)[:allowance]
                    _micro_fast_followup_window.extend(stamp for _ in due_mints)
                else:
                    # Urgent exits cannot be starved by 24/min entry callbacks.
                    # This bounded, dedicated overflow never scans new mints.
                    urgent_budget=max(0,12-len(_micro_urgent_reserve_window))
                    due_mints=_age_study.micro_urgent_held(
                        now=stamp,limit=min(3,urgent_budget)) if urgent_budget else []
                    _micro_urgent_reserve_window.extend(stamp for _ in due_mints)
                    if due_mints:
                        log.warning('V908 MICRO URGENT EXIT RESERVED LOOKUPS count=%d '
                                    'shared_callback_budget_exhausted=True paper_only=True',
                                    len(due_mints))
                extra_mints.update(due_mints)
                if due_mints:
                    log.info('V895 MICRO FAST RECHECK held_age5_or_pending_and_ready=%d '
                             'fresh_pool_recheck=True no_stale_fill=True',len(due_mints))
        for mint in due_mints:
            try:
                current=token_pairs(mint)
            except DexRateLimited:
                break
            except Exception as exc:
                log.warning('PAPER AGE STUDY DATA UNAVAILABLE mint=%s error=%s no_virtual_exit=True',
                            mint,type(exc).__name__)
                with _age_study_lock:
                    _age_study.missing(mint,now=stamp,micro_only=mint in extra_mints)
                continue
            # Same-pool selected when held, never substitute another pool.
            has_valid=False
            for p in current:
                pid=p.get('pairAddress')
                if (p.get('baseToken') or {}).get('address')!=mint:
                    continue
                with _age_study_lock:
                    held={t.positions[mint]['pool'] for t in _age_study.tracks if mint in t.positions}
                    if held and pid not in held:
                        continue
                    e=_age_study.pair_observed(p,sol_mint=SOL_MINT,
                        dexes=active_entry_routes(),now=stamp,
                        surge_change=SHADOW_SURGE_CHANGE_5M_PCT,
                        surge_volume=SHADOW_SURGE_MIN_5M_VOLUME_USD,
                        micro_only=mint in extra_mints)
                    lookup=( _age_study.micro_last_lookup if mint in extra_mints
                             else _age_study.last_lookup )
                    if pid and lookup.get(mint)==stamp:
                        has_valid=True
                for study_track,study_event in e.items():
                    log.info('PAPER AGE STUDY EVENT track=%s event=%s mint=%s pool=%s primary_ledger_untouched=True indicative_only=True',
                             study_track,study_event,mint,pid)
            if not has_valid:
                with _age_study_lock:
                    _age_study.missing(mint,now=stamp,micro_only=mint in extra_mints)
                    still_held = any(mint in t.positions for t in _age_study.tracks)
                if still_held:
                    log.warning('PAPER AGE STUDY QUOTE MISSING mint=%s no_executable_exit_proven=True no_virtual_sale=True',mint)
    with _age_study_lock:
        _age_study.save()
    if stamp-_age_study_last_stats_at>=60:
        _age_study_last_stats_at=stamp
        with _age_study_lock:
            reports = _age_study.status()
        if V893_RESEARCH_ENABLED:
            log.info('V893 RESEARCH RISK AUDIT %s quote_sidecar=%s paper_only=True entry_authorization_not_shared=True',
                     _v893_risk.summary(),_v893_quotes.totals)
        # v909: transparent 3-arm paper experiment status. Each new variant
        # carries an independent persistent ledger, has no signing authority,
        # and does not change the canonical micro trader or its run ID.
        if V893_RESEARCH_ENABLED and hasattr(_age_study,'v909_comparison'):
            log.info('V909 PAPER MICRO COMPARISON %s '
                     'realized_only=True unresolved_positions_not_marked_as_sales=True '
                     'existing_micro_primary_unchanged=True',
                     _age_study.v909_comparison())
        for report in reports:
            if V893_RESEARCH_ENABLED and report['track'] in ('adaptive_0_60','micro_0_60'):
                log.info('V893 RESEARCH TRACK FUNNEL track=%s risk_or_route_blocks=%s market_cap_floor_usd=%s same_pool_liquidity_floor_usd=%s max_change5_pct=%s indicative_only=True',
                         report['track'],report.get('research_rejections',{}),
                         report.get('entry_min_market_cap_usd'),report.get('entry_min_liquidity_usd'),
                         report.get('max_change5_pct'))
                if report['track']=='micro_0_60':
                    log.info('V895 MICRO AGE+RUG AUDIT age_entry_allowed=0_to_under5_and_8_to_under60 '
                             'age5_exit_intent=True age5_unquoted_remains_pending=True '
                             'pending_exit_count=%d reentries_after8=%d pool_guard=%s '
                             'no_verified_lp_or_holder_data_means_no_new_micro_entry=True micro_run_id=%s',
                             report.get('pending_micro_exits',0),report.get('micro_reentries_after8',0),
                             (_micro_v895_rug.stats() if _micro_v895_rug else None),_age_study.micro_run_id)
                    log.info('V896 MICRO FRESH RUN ledger=%s starting_capital_usd=36.00 '
                             'run_id=%s open=%d closed=%d cash_usd=%.2f '
                             'old_micro_history_untouched=True',
                             next(t.path for t in _age_study.tracks if t.name=='micro_0_60'),
                             _age_study.micro_run_id,report['open'],report['closed'],report['cash_usd'])
                    log.info('V897 MICRO THROUGHPUT AUDIT mint_rpc_checks_max_per_min=%d '
                             'mint_rpc_pending=%d jupiter_entry=%s pool_guard_prefetch=%s '
                             'age_entry_rules_preserved=True micro_run_id=%s',
                             _v893_risk.max_rpc_per_minute,len(_v893_risk._pending),
                             _v893_quotes.entry_stats(),(_micro_v895_rug.stats() if _micro_v895_rug else None),
                             _age_study.micro_run_id)
                    # v906: surface the reason an unsellable micro position
                    # remains OPEN. Never claim a sale without a fresh,
                    # pinned same-pool minimum-out quote.
                    micro_track=next((t for t in _age_study.tracks
                                      if t.name=='micro_0_60'),None)
                    if micro_track is not None:
                        for held_mint,held_rec in micro_track.positions.items():
                            if (held_rec.get('pending_exit_reason') or
                                    float(held_rec.get('last_net_pct',0))<=-20):
                                log.warning('V906 MICRO HELD EXIT AUDIT mint=%s pool=%s '
                                    'pending_reason=%s quote_status=%s '
                                    'quote_attempts=%d last_spot_return_pct=%s '
                                    'entry_liquidity_usd=%s last_liquidity_usd=%s '
                                    'no_confirmed_paper_sale=True',
                                    held_mint,held_rec.get('pool'),
                                    held_rec.get('pending_exit_reason'),
                                    held_rec.get('exit_quote_status'),
                                    held_rec.get('unverified_exit_attempts',0),
                                    held_rec.get('last_net_pct'),
                                    held_rec.get('entry_liquidity'),
                                    held_rec.get('last_liquidity'))
                    log.info('V898 MICRO EXIT INTEGRITY pinned_buy_sell_quotes=True '
                             'conservative_buy_minimum_inventory=True held_quote_poll_12s=True '
                             'maximum_quote_age_s=7 age5_prearm_min=4.25 '
                             'quote_based_stop_usd=1.82 paper_accounting_revision=v898 '
                             'paper_only=True run_id=%s closed_using_new_accounting=%d '
                             'quoted_stop_closes=%d pending_exit_statuses=%s',
                             _age_study.micro_run_id,
                             report.get('v898_quote_accounted_closes',0),
                             report.get('v898_quote_stop_closes',0),
                             report.get('v898_pending_exit_statuses',{}))
                    log.info('V900 MICRO UNIQUE ENTRY FUNNEL %s micro_run_id=%s '
                             'counts_are_unique_pools_since_restart_not_attempts=True',
                             report.get('micro_unique_funnel',{}),_age_study.micro_run_id)
                    log.info('V905 MICRO OPPORTUNITY PROFILE min_change5_pct=%.2f '
                             'min_buys5=%d min_sells5=%d min_buy_sell_ratio=%.2f '
                             'min_cap_usd=%.0f min_pool_liq_usd=%.0f '
                             'eligible_age=0_to_under5_and_8_to_under60 '
                             'security_and_jupiter_gates_unchanged=True '
                             'funnel_unique_pool_stages=%s no_frequency_guarantee=True',
                             report.get('v905_min_change5_pct',0.8),
                             report.get('v905_min_buys5',10),
                             report.get('v905_min_sells5',1),
                             report.get('v905_min_buy_sell_ratio',1.25),
                             report.get('entry_min_market_cap_usd',3000),
                             report.get('entry_min_liquidity_usd',1000),
                             report.get('micro_unique_funnel',{}))
                    log.info('V895 MICRO FILTER FUNNEL rejects=%s legacy_volume_would_block=%s '
                             'minimum_5m_volume_usd=%s minimum_change5_pct=%.2f '
                             'minimum_buys5=%d minimum_sells5=%d '
                             'minimum_buy_sell_ratio=%.2f counts_are_attempts=True',
                             report.get('entry_filter_rejections',{}),
                             report.get('legacy_volume_observations',{}),
                             report.get('entry_min_volume5_usd'),
                             report.get('v905_min_change5_pct',0.8),
                             report.get('v905_min_buys5',10),
                             report.get('v905_min_sells5',1),
                             report.get('v905_min_buy_sell_ratio',1.25))
            if AGE_0_60_PRIMARY and report['track']=='0_60':
                log.info('PRIMARY 0_60 AUDIT live_mode=%s closed=%d wins=%d open=%d cash_estimate_usd=$%.2f strategy_accounting_pnl_usd=$%.4f halt=%s halt_reason=%s',
                         LIVE_0_60_ENABLED,report['closed'],report['wins'],report['open'],report['cash_usd'],
                         report['realized_pnl_usd'],report['halted'],report['halt_reason'])
                if LIVE_0_60_ENABLED:
                    if report.get('live_journal_safety_freeze'):
                        log.critical('LIVE 0_60 JOURNAL SAFETY MODE pending_orders=%d '
                                     'live_orders_frozen=True scanner_continues=True',
                                     report.get('live_journal_pending_count',0))
                    log.info('LIVE 0_60 ENTRY FUNNEL process_rejections=%s new_buys_enabled=%s note=only_candidates_with_fresh_verified_pool_observation_reach_live_track',
                             report.get('live_entry_rejections_process',{}),
                             report.get('live_new_buys_enabled'))
                    log.info('LIVE 0_60 INVENTORY AUDIT exceptions=%d unresolved_missing_inventory=%d new_buys_blocked_on_unresolved=%s operator_acknowledged_missing_inventory=%d',
                             report.get('inventory_exceptions',0),
                             report.get('unreconciled_inventory_exceptions',0),
                             bool(report.get('unreconciled_inventory_exceptions',0)),
                             report.get('operator_acknowledged_missing_inventory',0))
            log.info('PAPER AGE STUDY AUDIT track=%s closed=%d wins=%d losses=%d '
                     'open=%d cash=$%.2f realized_pnl=$%.4f marked_equity_indicative=$%.2f '
                     'missing_quotes=%d read_only=%s halt=%s halt_reason=%s primary_portfolio_separate=True',
                     report['track'],report['closed'],report['wins'],report['losses'],
                     report['open'],report['cash_usd'],report['realized_pnl_usd'],
                     report['indicative_stale_marked_equity_usd'],report['missing_quotes'],
                     report['read_only'],report['halted'],report['halt_reason'])


def transient_pool_index_retry_source(mint: str) -> bool:
    """Only these *discovery hints* qualify for bounded fresh indexing retries.

    A positive result is never a trade approval, a price, a SOL route, or a
    prebuy liquidity check. Every retry goes through the existing scanner.
    """
    return any(_candidate_pipeline.has_source(mint, source)
               for source in ('fresh_launch_feed', 'pumpportal_migration',
                              'pumpfun_movers'))


def scan_once(state: dict) -> None:
    global _entry_scan_next_at, _entry_scan_cursor
    # Always manage existing positions; discovery is a separate, slower job.
    now = time.monotonic()
    scan_entries = now >= _entry_scan_next_at and api_cooldown_remaining() <= 0
    if scan_entries:
        _event_recheck_mints.clear()
        _entry_scan_next_at = now + ENTRY_SCAN_INTERVAL_SECONDS
    addresses = latest_candidate_addresses() if scan_entries else []
    if scan_entries:
        if FRESH_BATCH_DISCOVERY_ENABLED:
            # Trust the *current* active queue, not a 60s discovery snapshot
            # that can still contain rejected or previously cooled candidates.
            revived = _fresh_batch.resume_hot_due(limit=4)
            recovered_due = _fresh_batch.resume_data_due(limit=4)
            threshold_due = _fresh_batch.resume_threshold_due(limit=4)
            launch_pool_due = _fresh_batch.resume_launch_pool_due(limit=6)
            if launch_pool_due:
                log.info("PAPER LAUNCH POOL DUE checked_again=%d fresh_dex_primary_pool_required=True no_auto_buy=True", len(launch_pool_due))
            if threshold_due:
                log.info("PAPER THRESHOLD WATCH DUE count=%d fresh_same_pool_market_check=required paper_only=True",len(threshold_due))
            if revived:
                log.info("PAPER HOT RECHECK due=%d bounded_attempts=2 fresh_primary_market_check=required paper_only=True", len(revived))
            if PAPER_SHADOW_ACTIVE:
                shadow_due = _fresh_batch.resume_shadow_due(limit=12)
                if shadow_due:
                    log.info('PAPER SHADOW RECHECK DUE count=%d verified_pool_age_again_required=True',len(shadow_due))
            if recovered_due:
                log.info("PAPER DATA RECOVERY DUE due=%d bounded_attempts=3 fresh_primary_market_check=required no_entry_override=True", len(recovered_due))
            migration_due = migration_hints(limit=8) if FRESH_LAUNCH_FEED_ENABLED else []
            if migration_due:
                _candidate_pipeline.register_sources({"pumpportal_migration": migration_due})
                _candidate_pipeline.save()
                accepted_events = sum(_fresh_batch.promote_pool_event(m) for m in migration_due)
                _event_recheck_mints.update(migration_due)
                _migration_stats["rechecks_offered"] += accepted_events
                log.info("PAPER MIGRATION PRIORITY offered=%d promoted=%d live_events=%d primary_dex_recheck_required=True no_auto_buy=True", len(migration_due), accepted_events, _migration_event_count)
            # Existing Railway v8.4 queue records have no lane metadata.
            # Backfill only their discovery-origin label, never liquidity or
            # a market-pass flag, from the persisted source attribution.
            legacy_labeled = 0
            for queued_mint, queue_rec in _fresh_batch.active.items():
                if 'feed_only' not in queue_rec:
                    from_feed = _candidate_pipeline.has_source(queued_mint, 'fresh_launch_feed')
                    from_pool = any(_candidate_pipeline.has_source(queued_mint, name)
                                    for name in ('geckoterminal','dexpaprika','jupiter',
                                                 'raydium_volume','dex_search','recent_profiles'))
                    queue_rec['feed_only'] = from_feed and not from_pool
                    queue_rec.setdefault('pool_hint_at', 0.0)
                    queue_rec.setdefault('event_pending', False)
                    legacy_labeled += 1
            if legacy_labeled:
                log.info("PAPER V85 QUEUE UPGRADE legacy_records_labeled=%d "
                         "no_cached_data_used_for_trading=True",legacy_labeled)
            if not PAPER_SHADOW_ACTIVE:
                defer_unavailable_fresh_batch_mints(held_mints=set(state.get("positions", {})))
            if PAPER_SHADOW_ACTIVE:
                retired_hints = _fresh_batch.retire_expired_shadow_hints()
                if V893_ADAPTIVE_SCAN:
                    old_launches=_fresh_batch.retire_stale_launches()
                    if old_launches:
                        log.info('V893 STALE LAUNCH RETIRE retired=%d age_floor=600s pool_unverified_only=True no_entry_override=True',old_launches)
                # Backfill around cooldowns/held mints rather than reserving
                # slots that are later silently dropped by select_entry_mints.
                held_now = set(state.get('positions', {}))
                queue_exclusions = {m for m in _fresh_batch.active
                                    if m in held_now or entry_lookup_blocked(m)}
                addresses = _fresh_batch.due_shadow(
                    limit=FRESH_BATCH_ACTIVE_LIMIT, launch_quota=4,
                    adaptive=V893_ADAPTIVE_SCAN,
                    excluded=queue_exclusions,
                    priority_hint_mints=set(_board_cache.get('movers') or ()),
                    priority_hint_quota=6)
                allocation = _fresh_batch.shadow_due_counts(addresses)
                schedule_health = _fresh_batch.shadow_scheduler_status()
                log.info('PAPER SHADOW SCHEDULER entry_window=%d pre_window=%d waiting_rechecks=%d due_rechecks=%d expired_retired=%d blocked_skipped=%d unknown_age=%d full_filters_unchanged=True',
                         schedule_health['entry_window'],schedule_health['pre_window'],
                         schedule_health['waiting_rechecks'],schedule_health['due_rechecks'],
                         retired_hints,len(queue_exclusions),schedule_health['unknown_age_active'])
                log.info('PAPER SHADOW QUEUE early_pool=%d migration=%d launch_probe=%d other_pool=%d waiting=%d expired_shadow_hints=%d no_buy_override=True',
                         allocation['early_pool'],allocation['migration'],allocation['launch_only'],
                         allocation['other_pool'],_fresh_batch.stats()['active']-len(addresses),
                         allocation['expired_shadow'])
                log.info('DISCOVERY SCAN ALLOCATION scan_cap=24 movers_due=%d movers_selected=%d backlog_unscanned=%d launch_scan_quota=4 adaptive_overflow=%s no_entry_override=True',
                         sum(m in _fresh_batch.active and not _fresh_batch.active[m].get('checked')
                             for m in set(_board_cache.get('movers') or ())),
                         sum(m in set(_board_cache.get('movers') or ()) for m in addresses),
                         _fresh_batch.stats()['unscanned'],V893_ADAPTIVE_SCAN)
                if V893_ADAPTIVE_SCAN:
                    lag=_fresh_batch.research_metrics()
                    log.info('V893 SCANNER PROFILE selection=%d unscanned=%d first_check_p50_s=%s first_check_p90_s=%s oldest_wait_s=%s unscanned_wait_p90_s=%s adaptive_observation_only=True',
                             len(addresses),lag['unscanned'],lag['first_check_lag_p50_s'],
                             lag['first_check_lag_p90_s'],lag['unscanned_oldest_s'],
                             lag['unscanned_age_p90_s'])
                _event_recheck_mints.update(m for m in addresses if _fresh_batch.active.get(m,{}).get('shadow_event_pending'))
            else:
                addresses = _fresh_batch.due_v85(limit=FRESH_BATCH_ACTIVE_LIMIT, launch_quota=6)
                allocation = _fresh_batch.due_v85_counts(addresses)
            if not PAPER_SHADOW_ACTIVE:
                log.info("PAPER V85 QUEUE scanned_pool_hints=%d launch_only=%d migration_priority=%d "
                         "active=%d launch_scan_quota=6 no_entry_bypass=True",
                         allocation['liquid_source_hints'], allocation['launch_only'],
                         allocation['migration'], len(_fresh_batch.active))
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
    data_recoveries: set[str] = set()
    threshold_watch_due: dict[str, tuple[str,int]] = {}
    followup_details: dict[str, str] = {}
    primary_pool_evidence: dict[str, dict] = {}
    shadow_candidates: dict[str, dict] = {}  # fresh verified raw pool snapshots
    shadow_observation_due: set[str] = set()  # age 3-to-12 fresh same-pool watch
    shadow_watch_pool_created: dict[str,float] = {}
    shadow_diagnostics: Counter = Counter()
    m5_data_quality: dict[str, str] = {}
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
            if SHADOW_AGE_STUDY_ENABLED:
                # Background study faults must not block the primary scanner.
                try:
                    for observed_pair in pairs:
                        with _age_study_lock:
                            study_events=_age_study.pair_observed(observed_pair,
                                sol_mint=SOL_MINT,dexes=active_entry_routes(),
                                surge_change=SHADOW_SURGE_CHANGE_5M_PCT,
                                surge_volume=SHADOW_SURGE_MIN_5M_VOLUME_USD)
                        for study_track,study_event in study_events.items():
                            log.info('PAPER AGE STUDY EVENT track=%s event=%s mint=%s primary_ledger_untouched=True indicative_only=True',
                                study_track,study_event,(observed_pair.get('baseToken') or {}).get('address'))
                except Exception as study_error:
                    log.exception('PAPER AGE STUDY ERROR stage=shared_market_batch error=%s primary_scanner_continues=True',
                                  type(study_error).__name__)
            for mint in batch:
                checked_mints.add(mint)
                if mint not in grouped:
                    missing_pairs += 1
                    market_data_rejections["pair_not_indexed"] += 1
                    batch_rejected[mint] = "pair_not_indexed"
                    if LIVE_0_60_ENABLED and transient_pool_index_retry_source(mint):
                        log.info('LIVE 0_60 MARKET REJECT mint=%s reason=pair_not_indexed '
                                 'stage=unverified_pool awaiting_bounded_index_recheck=True '
                                 'no_order_sent=True', mint)
                    if CANDIDATE_QUALITY_ENABLED and not transient_pool_index_retry_source(mint):
                        _quality_ledger.fail(mint, "pair_not_indexed")
                    # A missing index response is not evidence that yesterday's
                    # verified pool disappeared. Retain it until its TTL expires.
                    mark_fresh_checked(mint, "pair_not_indexed")
                    if mint not in _verified_dex_mints and not transient_pool_index_retry_source(mint):
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
                if PAPER_SHADOW_ACTIVE:
                    # Prefer a verified 5-12m pool even when an older SOL pool
                    # for the same mint happens to have greater depth. This
                    # changes which pool is OBSERVED, never its entry rules.
                    young = [p for p in mint_pairs if 5 <= age_hours(p)*60 < 12
                             and entry_pair_reason(p) is None
                             and as_float(p.get('marketCap')) >= 35000]
                    if young:
                        pair = max(young,key=lambda p:as_float((p.get('liquidity') or {}).get('usd')))
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
                    if LIVE_0_60_ENABLED and transient_pool_index_retry_source(mint):
                        log.info('LIVE 0_60 MARKET REJECT mint=%s reason=%s '
                                 'stage=unverified_or_unsupported_pool '
                                 'awaiting_bounded_index_recheck=%s no_order_sent=True',
                                 mint, diagnostic, diagnostic in POOL_WAIT_REASONS)
                    if CANDIDATE_QUALITY_ENABLED and not (
                        diagnostic in POOL_WAIT_REASONS and
                        transient_pool_index_retry_source(mint)):
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
                    if mint not in _verified_dex_mints and not (
                        diagnostic in POOL_WAIT_REASONS and
                        transient_pool_index_retry_source(mint)):
                        note_entry_rejection(mint, diagnostic)
                    if mint in stream_set and diagnostic in ("supported_pair_liquidity_unavailable", "supported_pair_missing_price", "no_sol_quoted_pair"):
                        curve_pending.append(mint)
                    continue
                # Missing fields and reported zero volume are distinct.
                m5_data_quality[mint] = primary_m5_activity(pair)
                # Record only markets re-verified from fresh pair data.
                was_verified = mint in _verified_dex_mints
                verified_market_this_scan += 1
                if (_candidate_pipeline.has_source(mint, 'fresh_launch_feed') or _candidate_pipeline.has_source(mint, 'pumpportal_migration')):
                    _migration_stats['supported_pools'] += 1
                    log.info("PAPER LAUNCH POOL FOUND mint=%s pool=%s dex=%s liquidity_usd=%.0f final_entry_filters_still_required=True paper_only=True",
                             mint, pair.get('pairAddress',''), pair.get('dexId',''),
                             as_float((pair.get('liquidity') or {}).get('usd')))
                can_reenter, _, was_traded = paper_reentry_decision(state, mint)
                reentry_age_allowed = can_reenter and was_traded
                retain_fresh_verified_pool(mint, pair, allow_reentry_age=reentry_age_allowed)
                # Observe the same verified primary pool even when it does not
                # pass final entry filters: acceleration must not be limited to
                # already-buyable tokens.
                accelerating = _candidate_pipeline.observe_market(mint, pair) if FRESH_BATCH_DISCOVERY_ENABLED else False
                # v8.6 makes the ORIGINAL 5-12m shadow signal the exclusive
                # active-paper strategy. No parallel sidecar entries or legacy
                # market-pass approvals can authorize a new paper buy.
                if PAPER_SHADOW_ACTIVE:
                    eligible_young_pools = [p for p in mint_pairs
                        if 5 <= age_hours(p)*60 < 12 and
                        entry_pair_reason(p) is None and
                        as_float(p.get('marketCap')) >= 35000]
                    if eligible_young_pools and mint not in state['positions']:
                        shadow_observation_due.add(mint)
                    shadow_pair = max((p for p in eligible_young_pools
                        if shadow_paper_candidate(p) is not None),
                        key=lambda p: as_float((p.get('liquidity') or {}).get('usd')), default=None)
                    if shadow_pair is not None and mint not in state['positions']:
                        # Lock the approved signal metrics at detection time.
                        # A later primary-pool lookup is pricing, not a second
                        # five-minute momentum entry decision.
                        signal_candidate = shadow_paper_candidate(shadow_pair)
                        shadow_candidates[mint] = {'pair':shadow_pair, 'candidate':signal_candidate}
                        log.info('PAPER SHADOW ACTIVE SIGNAL mint=%s pool=%s age_m=%.2f first_snapshot_only=True paper_entry_after_portfolio_exit_cycle=True',
                                 mint, shadow_pair.get('pairAddress'), age_hours(shadow_pair)*60)
                else:
                    shadow_event = _early_shadow_v80.observe(pair,sol_mint=SOL_MINT,dexes=active_entry_routes())
                    if shadow_event == 'shadow_opened' or (shadow_event.startswith('shadow_') and shadow_event != 'shadow_observed'):
                        log.info("PAPER EARLY SHADOW mint=%s event=%s indicative_only=True not_a_trade=True",mint,shadow_event)
                primary_pool_evidence[mint] = {
                    'cap': as_float(pair.get('marketCap')),
                    'liq': as_float((pair.get('liquidity') or {}).get('usd')),
                    'vol5': as_float((pair.get('volume') or {}).get('m5')),
                    'change5': (pair.get('priceChange') or {}).get('m5'),
                    'accelerating':accelerating,
                }
                if PAPER_SHADOW_ACTIVE:
                    reason, can_watch, evidence = shadow_entry_diagnostic(
                        pair, sol_mint=SOL_MINT, dexes=active_entry_routes())
                    shadow_diagnostics[reason] += 1
                    log.info('PAPER SHADOW TRACE mint=%s age_m=%.2f cap_usd=%.0f pool_liq_usd=%.0f vol5_usd=%.0f buys=%d sells=%d change5=%s reason=%s watchable=%s',
                             mint, evidence.get('age',-1),evidence.get('cap',0),
                             evidence.get('liquidity',0),evidence.get('v5',0),
                             int(evidence.get('buys',0)),int(evidence.get('sells',0)),
                             evidence.get('change5'), reason,can_watch)
                    if reason == 'eligible_shadow_signal':
                        market[mint] = shadow_paper_candidate(pair)  # full shadow entry metrics, no legacy scoring
                    else:
                        batch_rejected[mint]=reason
                        if can_watch:
                            shadow_observation_due.add(mint)
                            shadow_watch_pool_created[mint] = as_float(pair.get('pairCreatedAt'))/1000
                    mark_fresh_checked(mint,'eligible' if reason=='eligible_shadow_signal' else reason)
                    continue
                local_rejections: Counter = Counter()
                c = analyze_pair(pair, local_rejections, allow_reentry_age=reentry_age_allowed)
                if CANDIDATE_QUALITY_ENABLED:
                    _quality_ledger.note_verified(mint, score=c.get("score") if c else None)
                rejection_counts.update(local_rejections)
                if c is None and local_rejections:
                    reason = next(iter(local_rejections))
                    batch_rejected[mint] = reason
                    lane, why = followup_decision(
                        pair, reason, sol_mint=SOL_MINT, dexes=active_entry_routes(),
                        min_age_minutes=MOMENTUM_MIN_PAIR_AGE_MINUTES,
                        min_liquidity=active_entry_liquidity_min(),
                        min_cap=MOMENTUM_MARKET_CAP_MIN_USD,
                        max_cap=MOMENTUM_MARKET_CAP_MAX_USD,
                        require_hour=not PAPER_NO_1H_REQUIREMENT)
                    followup_details[mint] = f"{lane}:{why}"
                    if lane == 'hot':
                        hot_nearmisses.add(mint)
                    elif (PAPER_NO_1H_REQUIREMENT and short_flow_nearmiss(
                            pair, reason, sol_mint=SOL_MINT, dexes=active_entry_routes(),
                            min_age_minutes=MOMENTUM_MIN_PAIR_AGE_MINUTES)):
                        hot_nearmisses.add(mint)
                        followup_details[mint] = 'hot:verified_positive_5m_flow_no_hour_required'
                    elif lane == 'data':
                        data_recoveries.add(mint)
                    # For early-age/near-cap pools, schedule a fresh observation
                    # close to eligibility instead of the unconditional 900s wait.
                    watch_kind, watch_delay, watch_reason = threshold_watch(
                        pair, reason,sol_mint=SOL_MINT,dexes=active_entry_routes(),
                        min_cap=MOMENTUM_MARKET_CAP_MIN_USD,
                        min_age=MOMENTUM_MIN_PAIR_AGE_MINUTES,
                        max_cap=MOMENTUM_MARKET_CAP_MAX_USD)
                    if watch_kind != 'none':
                        threshold_watch_due[mint] = (watch_kind,watch_delay)
                        followup_details[mint] += f";threshold_{watch_kind}:{watch_reason}"
                    elif reason in ('too_new_for_established_strategy','market_cap_below_range'):
                        followup_details[mint] += f";threshold_none:{watch_reason}"
                    # Rate-limited same-EXACT-pool Gecko trend observation.
                    # This does not fill missing primary trend values or approve
                    # a paper buy; it only helps diagnose data-recovery stalls.
                    if reason in ('trend_m5_missing','trend_h1_missing','trend_h6_missing','trend_h24_missing','missing_pair_age','invalid_pair_age'):
                        clock=time.monotonic()
                        _secondary_trend_check_times[:] = [s for s in _secondary_trend_check_times if clock-s<60]
                        pool_id=pair.get('pairAddress') or ''
                        budget_ok, budget_reason, _ = _gecko_shared_budget.acquire(now=clock) if pool_id and len(_secondary_trend_check_times)<2 else (False,'local_quota',0)
                        if pool_id and budget_ok:
                            _secondary_trend_check_times.append(clock)
                            try:
                                reply=session.get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool_id}",
                                                  timeout=8,headers={'Accept':'application/json;version=20230203'})
                                if reply.status_code==200:
                                    sec,secondary_reason=gecko_same_pool_observation(pair,reply.json(),sol_mint=SOL_MINT)
                                    log.info("PAPER SAME POOL TREND mint=%s pool=%s status=%s m5=%s h1=%s independent_age_ms=%s independent_vol5_usd=%s observation_only=True not_entry_data=True",
                                             mint,pool_id,secondary_reason,sec.get('m5') if sec else None,sec.get('h1') if sec else None,
                                             sec.get('created_ms') if sec else None,sec.get('v5') if sec else None)
                                    if (sec and reason in ('missing_pair_age','invalid_pair_age') and
                                            sec.get('created_ms') and sec.get('m5') is not None and
                                            0.5<=sec['m5']<=16 and (sec.get('v5') or 0)>=900 and
                                            (sec.get('buys') or 0)>=8 and (sec.get('sells') or 0)>=1 and
                                            as_float((pair.get('liquidity') or {}).get('usd'))>=15000 and
                                            MOMENTUM_MARKET_CAP_MIN_USD<=as_float(pair.get('marketCap'))<=MOMENTUM_MARKET_CAP_MAX_USD and
                                            5<=(time.time()*1000-sec['created_ms'])/60000<525600):
                                        # Gecko age verifies whether a provider recheck is sensible,
                                        # but NEVER supplies the primary age used for paper entry.
                                        threshold_watch_due[mint]=('provider',120)
                                        followup_details[mint]+=';secondary_same_pool_provider_age_watch' 
                                else:
                                    if reply.status_code == 429:
                                        _gecko_shared_budget.note_429(180, now=clock)
                                    log.info("PAPER SAME POOL TREND mint=%s status=http_%d observation_only=True",mint,reply.status_code)
                            except (requests.RequestException,ValueError,TypeError,AttributeError):
                                log.info("PAPER SAME POOL TREND mint=%s status=provider_unavailable observation_only=True",mint)
                        elif pool_id and budget_reason != 'local_quota':
                            log.info("PAPER SAME POOL TREND mint=%s status=%s observation_only=True",mint,budget_reason)
                mark_fresh_checked(mint, "eligible" if c else "momentum_rejected")
                if MOMENTUM_SWING_MODE and not SNIPER_MODE:
                    if c is not None:
                        # Genuine momentum requalification clears the previous
                        # cooldown, never merely receiving another pool quote.
                        _entry_reject_until.pop(mint, None)
                        _entry_reject_reason.pop(mint, None)
                    elif local_rejections and not (PAPER_SHADOW_ACTIVE and mint in shadow_observation_due):
                        # Legacy 12m/multi-hour filters cannot cool down a
                        # 5-12m token the ACTIVE shadow strategy is watching.
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
        _migration_stats["market_passes"] += sum(m in market and _candidate_pipeline.has_source(m, "pumpportal_migration") for m in checked_mints)
        _migration_stats["scan_checks"] += sum(_candidate_pipeline.has_source(m, "pumpportal_migration") for m in checked_mints)
        log.info("PAPER MIGRATION CONVERSION events=%d rechecks=%d checked=%d supported_pool_checks=%d market_passes=%d paper_trades_are_separate=True",
                 _migration_event_count, _migration_stats['rechecks_offered'], _migration_stats['scan_checks'],
                 _migration_stats['supported_pools'], _migration_stats['market_passes'])
        feed_checked = sum(_candidate_pipeline.has_source(m,'fresh_launch_feed') for m in checked_mints)
        migration_checked = sum(_candidate_pipeline.has_source(m,'pumpportal_migration') for m in checked_mints)
        primary_supported = sum(m in primary_pool_evidence for m in checked_mints)
        log.info("PAPER SHADOW ACTIVE FUNNEL scanned=%d eligible_shadow_signals=%d shadow_pool_observations=%d market_passes=%d selected_entry_strategy=%s paper_only=True",
                 len(checked_mints),len(shadow_candidates),len(shadow_observation_due),
                 len(market),'early_shadow' if PAPER_SHADOW_ACTIVE else 'legacy')
        if PAPER_SHADOW_ACTIVE:
            log.info('PAPER SHADOW REJECTIONS reasons=%s available_pool=%d active_early_watches=%d legacy_momentum_filters_skipped=True',
                     dict(shadow_diagnostics),len(primary_pool_evidence),len(shadow_observation_due))
        log.info("PAPER V85 CONVERSION scanned=%d feed_origin=%d migration_origin=%d "
                 "primary_supported_pool=%d market_pass=%d paper_buy_separate=True",
                 len(checked_mints), feed_checked, migration_checked,
                 primary_supported, len(market))
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
        data_retries = 0
        pool_followup_count = 0
        final_followups: dict[str,str] = {}
        for mint, reason in batch_rejected.items():
            # A 5-12m liquid shadow pool that hasn't yet reached the exact
            # five-minute flow/price signal gets bounded fresh re-observations.
            # Reusing the persisted threshold-watch queue introduces NO new
            # entry filters and never treats older cached prices as a buy.
            if PAPER_SHADOW_ACTIVE and mint in shadow_observation_due:
                wait = _fresh_batch.schedule_shadow_recheck(
                    mint, reason, created_at=shadow_watch_pool_created.get(mint,0),
                    delay_seconds=45)
                if wait:
                    removed += 1
                    final_followups[mint] = 'shadow_early_pool_45s'
                    continue
            # Fresh launch announcements can precede SOL DEX pool indexing.
            # A failed lookup deserves a bounded future FULL lookup, not a
            # permanent or escalating market-quality quarantine.
            if pool_followup_count < 24 and transient_pool_index_retry_source(mint):
                delay = followup_delay(reason, _fresh_batch.launch_attempts(mint), feed_origin=True)
                if delay is not None and _fresh_batch.schedule_launch_pool_recheck(mint, reason, delay_seconds=delay):
                    pool_followup_count += 1
                    removed += 1
                    final_followups[mint] = f'launch_pool_watch_{delay}s'
                    continue
            if mint in threshold_watch_due:
                kind,delay=threshold_watch_due[mint]
                if _fresh_batch.schedule_threshold_watch(mint,kind,delay_seconds=delay):
                    removed += 1
                    final_followups[mint] = f'threshold_{kind}_{delay}s'
                    continue
            if mint in data_recoveries and _fresh_batch.schedule_data_recovery(mint, reason, delay_seconds=105):
                data_retries += 1
                removed += 1
                final_followups[mint] = 'data_recovery_105s'
                continue
            if mint in hot_nearmisses and _fresh_batch.schedule_hot_recheck(mint, reason, delay_seconds=75):
                hot_retries += 1
                removed += 1
                final_followups[mint] = 'hot_momentum_75s'
                continue
            if (mint in _candidate_pipeline.near and
                    reason in ('too_new_for_established_strategy', 'market_cap_below_range')):
                if _fresh_batch.defer_blocked(mint, 'near_eligible_retry', delay_seconds=900):
                    near_retries += 1
                    removed += 1
                    final_followups[mint] = 'ordinary_near_900s'
                    continue
            removed += int(_fresh_batch.discard(mint, reason))
            final_followups[mint] = 'normal_quarantine'
        if addresses:
            log.info("PAPER LAUNCH POOL FOLLOWUP screened=%d scheduled=%d attempts_max=%d wait_reasons=%s primary_market_recheck_required=True paper_only=True",
                     len(checked_mints), pool_followup_count, MAX_POOL_RECHECKS, sorted(POOL_WAIT_REASONS))
            log.info("PAPER M5 DATA QUALITY checked=%d statuses=%s missing_is_not_zero=True observation_only=True",
                     len(checked_mints), dict(Counter(m5_data_quality.values())))
            if PAPER_SHADOW_ACTIVE:
                log.info('PAPER SHADOW WINDOW min_age=5m max_age_exclusive=12m eligible_signals=%d early_observations=%d five_min_flow_required=True',
                         len(shadow_candidates),len(shadow_observation_due))
            else:
                log.info("PAPER THRESHOLD WATCH checked=%d eligible=%d age_wait=%d cap_wait=%d min_age_minutes=%.0f min_cap_usd=%.0f no_entry_override=True",
                         len(checked_mints),len(threshold_watch_due),
                         sum(k=='age' for k,_ in threshold_watch_due.values()),
                         sum(k=='cap' for k,_ in threshold_watch_due.values()),
                         MOMENTUM_MIN_PAIR_AGE_MINUTES,MOMENTUM_MARKET_CAP_MIN_USD)
            log.info("PAPER FOLLOWUP FUNNEL screened=%d market_passed=%d rejected=%d data_eligible=%d data_scheduled=%d hot_eligible=%d hot_scheduled=%d ordinary_near=%d full_entry_filters_unchanged=True paper_only=True",
                     len(checked_mints), len(market), len(batch_rejected), len(data_recoveries),
                     data_retries, len(hot_nearmisses), hot_retries, near_retries)
            for mint in sorted(checked_mints):
                rec = _candidate_pipeline.records.get(mint, {})
                evidence = primary_pool_evidence.get(mint, {})
                log.info("PAPER CANDIDATE TRACE mint=%s sources=%s status=%s reason=%s followup=%s detail=%s cap_usd=%.0f pool_liq_usd=%.0f vol5_usd=%.0f change5=%s accelerating=%s observations=%d",
                         mint, ','.join(rec.get('sources', [])) or 'unknown',
                         'market_pass' if mint in market else 'market_reject',
                         batch_rejected.get(mint,'none'), final_followups.get(mint,'none'),
                         followup_details.get(mint,'n/a'), evidence.get('cap',0),
                         evidence.get('liq',0), evidence.get('vol5',0),
                         evidence.get('change5','missing'), evidence.get('accelerating',False),
                         rec.get('observations',0))
        if hot_retries or (addresses and batch_rejected):
            log.info("PAPER FAST NEAR MISS checked=%d eligible_for_bounded_recheck=%d scheduled=%d cap_min=$100000 same_pool_liq_min=$25000 retry_s=75 max_attempts=2 no_entry_override=True",
                     len(checked_mints), len(hot_nearmisses), hot_retries)
        if data_recoveries or (addresses and batch_rejected):
            log.info("PAPER DATA RECOVERY checked=%d eligible=%d scheduled=%d interval_s=105 attempts_max=3 observation_only=True",
                     len(checked_mints), len(data_recoveries), data_retries)
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
            scanner_status = "shadow_filters_no_match" if PAPER_SHADOW_ACTIVE else "momentum_filters_no_match"
        # The funnel counts market-data eligibility only. No paper position is
        # authorized until independent same-pool sellability and trend checks.
        log.info("SCANNER HEALTH status=%s checked=%d accepted_market=%d api_batch_errors=%d missing_index=%d cooling=%d observed_candidates=%d; market_pass_does_not_equal_paper_buy",
                 scanner_status, evaluated, len(market), batch_lookup_failures,
                 missing_pairs, len(_entry_reject_until), len(addresses))

    # Periodic independent 5-12min early-entry paper SHADOW observations.
    # These never flow into the live/paper portfolio or candidate market dict.
    if scan_entries and PAPER_MODE and not PAPER_SHADOW_ACTIVE:
        for mint in _early_shadow_v80.due(limit=4):
            try:
                observations=token_pairs(mint)  # fresh DexScreener snapshots
                pinned=_early_shadow_v80.active.get(mint,{}).get('pool')
                exact=next((p for p in observations if p.get('pairAddress')==pinned and
                    (p.get('baseToken') or {}).get('address')==mint and
                    (p.get('quoteToken') or {}).get('address')==SOL_MINT),None)
                if exact:
                    event=_early_shadow_v80.observe(exact,sol_mint=SOL_MINT,
                            dexes=active_entry_routes(),allow_open=False)
                    if event.startswith('shadow_'):
                        log.info("PAPER EARLY SHADOW mint=%s event=%s indicative_only=True no_portfolio_trade=True",mint,event)
            except (requests.RequestException,ValueError,TypeError) as exc:
                log.info("PAPER EARLY SHADOW mint=%s status=quote_unavailable error_type=%s no_fabricated_result=True",mint,type(exc).__name__)
        _early_shadow_v80.save()
        stats=_early_shadow_v80.stats()
        if stats['open'] or stats['completed']:
            log.info("PAPER EARLY SHADOW SUMMARY open=%d completed=%d profitable_observed=%d cumulative_sampled_net_pct=%.2f no_real_or_portfolio_trades=True",
                     stats['open'],stats['completed'],stats['wins'],stats['net_pct_sum'])

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
            # Do not carry verified loss-of-liquidity time across a healthy or
            # inconclusive API observation. No sale/write-off is assumed here.
            issue = state.get("exit_issues", {}).get(mint, {})
            for field in ("rug_confirmed_checks", "last_rug_confirmed_at",
                          "rug_evidence_first_at"):
                issue.pop(field, None)
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
                                "quote_source": source, "pool_address": pair.get("pairAddress", ""),
                                "shadow_market_cap": as_float(pair.get('marketCap')),
                                "shadow_age_minutes": age_hours(pair) * 60}

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
            if as_float(fallback.get('liquidity')) >= SHADOW_MIN_LIQUIDITY_USD:
                exit_evidence[mint] = {'verified': False,
                    'reason': 'independent_pinned_pool_positive_valuation_no_verified_sale'}
            fallback_logged = state.setdefault("fallback_log_times", {})
            now_log = time.time()
            if now_log - float(fallback_logged.get(mint, 0)) >= PAPER_FALLBACK_LOG_INTERVAL_SECONDS:
                log.warning("PAPER POOL FALLBACK mint=%s pool=%s source=geckoterminal valuation_only=no_verified_exit",
                            mint, raw["entry_pool_address"])
                fallback_logged[mint] = now_log

    manage_positions(state, market, exit_evidence)
    restore_legacy_exit_issue_halt(state)
    roll_day_if_needed(state)
    if PAPER_SHADOW_ACTIVE:
        shadow_risk_snapshot(state)
    # Only after primary exits/risk updates, let the isolated study use its
    # bounded optional read-only queries. Keep it running during primary halt.
    try:
        run_background_age_study(scan_entries=scan_entries)
    except Exception as study_error:
        log.exception('PAPER AGE STUDY ERROR stage=background_tick error=%s primary_positions_continue=True',
                      type(study_error).__name__)
    # A pre-existing halt in the now HISTORICAL 5-12 primary ledger must not
    # block the separately owned 0-60 primary paper ledger. Legacy trading is
    # disabled while promoted, including when the legacy circuit is halted.
    if not AGE_0_60_PRIMARY and should_halt(state):
        save_state(state)
        return

    # Exclusive shadow strategy path. Use fresh pinned DexScreener source
    # snapshots for paper entry, bypassing *all* regular momentum/score/cost
    # entry thresholds. Old saved positions continue their original exit plan.
    if PAPER_SHADOW_ACTIVE:
        _event_recheck_mints.clear()
        if AGE_0_60_PRIMARY:
            # The v8.7.6 AgeTrack 0_60 has ALREADY received these fresh pairs
            # through the shared scanner and background checks. Do not execute
            # original 5-12 entries, and do not add ANY new 0-60 filters.
            with _age_study_lock:
                lead = _age_study.tracks[0]
                report = lead.stats()
            if scan_entries:
                log.info('PRIMARY 0_60 DECISIONS live_mode=%s mode=%s '
                         'closed=%d open=%d halt=%s reason=%s original_5_12_entries_disabled=True '
                         'indicative_price_signal=%s entry_cap_max_exclusive_usd=%.0f',
                         LIVE_0_60_ENABLED,
                         ('verified_quote_tp40_trail10_drop5_fast_ws' if LIVE_0_60_ENABLED else
                          'promoted_v876_entries_tp25_profit_reversal'),
                         report['closed'],report['open'],report['halted'],report['halt_reason'],
                         not LIVE_0_60_ENABLED,MOMENTUM_MARKET_CAP_MAX_USD)
            save_state(state)  # unchanged historical legacy ledger, if any
            return
        bought = 0
        reasons = Counter()
        for mint, observed_signal in sorted(shadow_candidates.items(),
            key=lambda item: as_float(item[1]['candidate'].get('m5_volume')), reverse=True):
            if len(state['positions']) >= MAX_POSITIONS:
                reasons['position_limit'] += 1
                break
            # The original shadow signal remains locked. The v8.7 PAPER
            # safety bridge then validates fresh same-pool exit plausibility
            # using two independent indicative data providers.
            first_signal = observed_signal['candidate']
            first_pair = observed_signal['pair']
            placed, result = shadow_paper_entry(state, first_pair,
                                                 signal_candidate=first_signal)
            reasons[result] += 1
            if not placed:
                log.info('PAPER SHADOW ORIGINAL ENTRY SKIP mint=%s pool=%s reason=%s',
                         mint, first_signal.get('pool_address'), result)
                # A brand-new pool may take time to appear on the independent
                # source. Retry boundedly within the ORIGINAL age window;
                # a delayed market listing is never permission to buy.
                pending_provider_reasons = {
                    'prebuy_independent_same_pool_unavailable',
                    'prebuy_primary_lookup_error',
                    'prebuy_primary_unverified',
                }
                if (result in pending_provider_reasons and FRESH_BATCH_DISCOVERY_ENABLED):
                    birth = as_float(first_pair.get('pairCreatedAt')) / 1000
                    if _fresh_batch.schedule_shadow_recheck(
                            mint, result, created_at=birth, delay_seconds=45):
                        _fresh_batch.save()
                        log.info('PAPER SHADOW SAFETY RECHECK mint=%s reason=%s delay_s=45 no_auto_buy=True',
                                 mint, result)
            bought += int(placed)
        if scan_entries:
            log.info('PAPER SHADOW ACTIVE DECISIONS signals=%d buys=%d reasons=%s positions=%d/%d old_strategy_entries_disabled=True paper_only=True',
                     len(shadow_candidates), bought, dict(reasons),len(state['positions']),MAX_POSITIONS)
        save_state(state)
        log.info('Equity≈$%.2f cash=$%.2f positions=%d shadow_signals=%d',
                 equity(state), state['cash_usd'],len(state['positions']),len(shadow_candidates))
        return
    # Migration hints must never exempt a candidate from downstream entry checks.
    _event_recheck_mints.clear()
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
            log.info("PAPER FAST CANDIDATES selected=%d top=%s fast_m5_min=0.8%% activity=volume_liquidity_hour_pace buy_pressure=verified paper_only=True",
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
                minimum_5m_change_pct=PAPER_COST_ENTRY_MIN_5M_PCT,
                require_1h=not PAPER_NO_1H_REQUIREMENT)
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
                        minimum_5m_change_pct=PAPER_COST_ENTRY_MIN_5M_PCT,
                        require_1h=not PAPER_NO_1H_REQUIREMENT)
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
            is_young = MOMENTUM_SWING_MODE and 12 <= as_float(c.get("age_hours"))*60 < 60
            if (is_small_cap or is_young) and any(
                    (0 < as_float(state.get("paper_trade_metrics", {}).get(m, {}).get("entry_market_cap_usd")) < SMALL_CAP_TIER_MAX_USD
                     or 12 <= as_float(state.get("paper_trade_metrics", {}).get(m, {}).get("entry_age_minutes"))*1 < 60)
                    for m in state["positions"]):
                skipped_entries["small_cap_position_limit"] += 1
                log.info("PAPER ENTRY SKIP %s reason=small_cap_position_limit maximum_open_small_cap=1", c["symbol"])
                continue
            proposed_size = (PAPER_EXPLORATION_POSITION_SIZE_USD if profile == "exploration" else None)
            if is_small_cap or is_young:
                tier_size = (MICRO_35K_PAPER_SIZE_USD if
                    (0 < as_float(c.get('market_cap_usd')) < 100000 or
                     12 <= as_float(c.get('age_hours'))*60 < 30)
                    else SMALL_CAP_PAPER_SIZE_USD)
                proposed_size = min(tier_size, proposed_size) if proposed_size is not None else tier_size
                log.info("PAPER SMALL CAP SIZING %s cap=$%.0f age_minutes=%.1f size=$%.2f tier_under=$%.0f max_concurrent_speculative=1 paper_only=True",
                         c["symbol"], c["market_cap_usd"], c["age_hours"]*60, proposed_size, SMALL_CAP_TIER_MAX_USD)
            before = len(state["positions"])
            paper_buy(state, c, size_usd=proposed_size,
                      entry_profile=("small_cap" if is_small_cap and profile == "standard" else profile))
            if len(state["positions"]) > before and FRESH_BATCH_DISCOVERY_ENABLED:
                sources = _candidate_pipeline.record_paper_entry(c["address"])
                _candidate_pipeline.save(force=True)
                log.info("PAPER SOURCE TO ENTRY mint=%s symbol=%s sources=%s paper_size_usd=%.2f source_attribution=shared_if_multiple_sources indicative_only=True",
                         c["address"], c["symbol"], ','.join(sources) or 'unknown',
                         state["positions"][c["address"]]["amount_usd"])
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
    if AGE_0_60_PRIMARY:
        if LIVE_0_60_ENABLED:
            log.info('PRIMARY STRATEGY=v8.8.9 live_mode=True age_track=0_60 '
                     'entry_filters=unchanged age=0_to_60m full_take_profit_executable_net_pct=40 '
                     'trailing_activation_net_pct=10 trailing_peak_drop_points=5 '
                     'protective_net_stop_pct=-9 liquidity_drop_pct=40 max_hold_minutes=45 '
                     'ws_wakeup=processed_pool_update ws_exit_authority=fresh_jupiter_quote '
                     'direct_pool_swap=False position_usd=2 automatic_loss_halts_enabled=False '
                     'entry_market_cap_max_exclusive_usd=%.0f verified_marketCap_only=True',MOMENTUM_MARKET_CAP_MAX_USD)
        else:
            log.info('PRIMARY STRATEGY=v8.7.8 live_mode=False age_track=0_60 '
                     'entry_filters=unchanged tp_net_pct=25 profit_reversal_on_first_decline=True '
                     'stop_net_pct=-9 liquidity_drop_pct=40 max_hold_minutes=45 '
                     'entry_market_cap_max_exclusive_usd=%.0f verified_marketCap_only=True',MOMENTUM_MARKET_CAP_MAX_USD)
    if PAPER_SHADOW_ACTIVE and not AGE_0_60_PRIMARY:
        log.info('PAPER PRIMARY STRATEGY=v8.6.4 ORIGINAL_SHADOW_ACTIVE exclusive_entries=True age=5-12m cap>=$35000 primary_sol_liq>=$15000 volume5m>=$1400 buys5m>=10 sells5m>=1 buy_sell_ratio>=1.25 price_change5m=0.8-16pct sample_seconds=180 tp_net=8pct sl_net=-12pct expiry_minutes=45 modeled_cost=6pct paper_only=True')
        log.info('PAPER EXISTING HOLDINGS legacy_exit_profiles_preserved=True max_positions=%d default_position_usd=%.2f early_shadow_position_usd=%.2f',MAX_POSITIONS,POSITION_SIZE_USD,MICRO_35K_PAPER_SIZE_USD)
        log.info('PAPER SHADOW SAFETY v8.7 prebuy_two_provider=%s protective_exit=%s protective_net_stop_pct=%.1f reversal_arm_pct=%.1f reversal_drop_pct=%.1f daily_loss_limit_pct=%.1f writeoff_circuit=%s max_writeoffs_day=%d stop_latency_non_guaranteed=True execution=paper_only',
                 SHADOW_SAFETY_PREBUY_VERIFICATION, SHADOW_PROTECTIVE_EXIT_ENABLED, SHADOW_PROTECTIVE_NET_STOP_PCT,
                 SHADOW_PROTECTIVE_PEAK_ARM_PCT, SHADOW_PROTECTIVE_PEAK_DROP_PCT,
                 MAX_DAILY_LOSS_FRACTION*100, SHADOW_WRITEOFF_CIRCUIT_ENABLED, SHADOW_MAX_DAILY_WRITEOFFS)
        log.info('PAPER SHADOW HEALTH PROTECTION v8.7.5 unhealthy_entry_guard=%s surge_change_min=%.2f surge_volume_floor=%.0f '
                 'routed_loss_monitor=%s interval_s=%.0f max_per_minute=%d route_stop_pct=%.1f '
                 'original_shadow_signal_unchanged=True paper_only=True',
                 SHADOW_UNHEALTHY_ENTRY_GUARD, SHADOW_SURGE_CHANGE_5M_PCT,
                 SHADOW_SURGE_MIN_5M_VOLUME_USD, SHADOW_ROUTE_MONITOR_ENABLED,
                 SHADOW_ROUTE_MONITOR_INTERVAL_SECONDS, SHADOW_ROUTE_MONITOR_MAX_PER_MINUTE,
                 SHADOW_ROUTE_MONITOR_NET_STOP_PCT)
        log.info('PAPER SHADOW EXIT PROTECTION v8.7.4 route_quote_required=%s route_retry_s=%.0f '
                 'exit_unavailable_halt_s=%.0f severe_open_loss_halt_pct=%.1f catastrophic_closed_loss_halt_pct=%.1f '
                 'expiry_pending_persists=True frozen_entry_rules=True',
                 SHADOW_EXIT_ROUTE_QUOTE_REQUIRED, SHADOW_EXIT_ROUTE_RETRY_SECONDS,
                 SHADOW_EXIT_UNAVAILABLE_HALT_SECONDS, SHADOW_SEVERE_POSITION_LOSS_HALT_PCT,
                 SHADOW_CATASTROPHIC_CLOSED_LOSS_HALT_PCT)
    if SHADOW_AGE_STUDY_ENABLED:
        log.info('PAPER AGE STUDY ACTIVE version=v8.7.6 tracks=0_to_60m,5_to_60m '
                 'position_usd=2 max_positions_per_track=5 separate_state_files=True '
                 'primary_5_to_12m_exclusive=True extended_gecko_hints_0_to_60m=True '
                 'further_pool_lookups_bounded=%d_per_scan no_real_trades=True '
                 'indicative_only_no_executable_fill_guarantee=True',
                 SHADOW_AGE_STUDY_FETCH_PER_SCAN)
    if MOMENTUM_SWING_MODE and not PAPER_SHADOW_ACTIVE:
        log.info("CANDIDATE PIPELINE v8.0 cap_floor=$%.0f small_cap_tier=under_$%.0f extra_liquidity_volume_flow_and_verified_cap=True near_watch=observation_only", MOMENTUM_MARKET_CAP_MIN_USD, SMALL_CAP_TIER_MAX_USD)
        log.info("ESTABLISHED MOMENTUM PAPER MODE cap=$%.0f-$%.0f min_age=%.0fm max_age=%s min_liq=$%.0f trend5m>=%.1f%% hourly_required=%s 6h>=%.1f%% 24h>=%.1f%% dexes=%s; all trades INDICATIVE paper simulations, not executable swap quotes",
                 MOMENTUM_MARKET_CAP_MIN_USD, MOMENTUM_MARKET_CAP_MAX_USD,
                 MOMENTUM_MIN_PAIR_AGE_MINUTES, MOMENTUM_MAX_PAIR_AGE_HOURS or "unlimited",
                 active_entry_liquidity_min(), MOMENTUM_MIN_CHANGE_5M_PCT, not PAPER_NO_1H_REQUIREMENT,
                 MOMENTUM_MIN_CHANGE_6H_PCT, MOMENTUM_MIN_CHANGE_24H_PCT, sorted(active_entry_routes()))
    if PAPER_FAST_ONLY and not PAPER_SHADOW_ACTIVE:
        log.info("PAPER FAST V8.5 active=True min_m5=0.8%% hourly_required=%s min_vol5m=$1400 min_volume_liquidity=2.0%% min_flow=1.14 net_take_profit=%.1f%% trail_arm=%.1f%% trail_giveback=%.1f%% loss_circuit=enabled only_verified_primary=True",
                 not PAPER_NO_1H_REQUIREMENT, PAPER_FAST_TP_NET_PCT, PAPER_PROFIT_TRAIL_ARM_NET_PCT, PAPER_PROFIT_TRAIL_GIVEBACK_PCT)
    if SNIPER_MODE:
        log.info("SNIPER MODE DEX paper only: max_age=%.0fm volume5m>=$%.0f buys/sells>=%.2f price5m>=%.1f%% estimated_entry_impact<=%.1f%%; bonding-curve entries blocked",
                 SNIPER_MAX_AGE_MINUTES, SNIPER_MIN_VOLUME_5M_USD, SNIPER_MIN_BUY_SELL_RATIO, SNIPER_MIN_PRICE_CHANGE_5M_PCT, SNIPER_MAX_ENTRY_IMPACT_PCT)
    # Explicit import verifies the strategy and execution bridge are deployed together.
    # Live transaction calls remain disabled until recovery and reconciliation are reviewed.
    from live_execution import execute_buy, execute_sell  # noqa: F401
    if LIVE_0_60_ENABLED:
        if os.getenv('LIVE_0_60_ARM')!='I_ACCEPT_AUTOMATIC_LIVE_TRADING':
            log.critical('LIVE ARM absent; refusing all real trading')
            raise SystemExit(2)
        if os.getenv('LIVE_TRADING_ENABLED')!='true':
            log.critical('LIVE_TRADING_ENABLED must also be true for live deployment')
            raise SystemExit(2)
        import fcntl
        _live_writer_lockfile = STATE_FILE.with_name('live_order_writer_v879.lock')
        _live_writer_lockfile.parent.mkdir(parents=True,exist_ok=True)
        global _live_writer_handle
        _live_writer_handle = _live_writer_lockfile.open('a+')
        try:fcntl.flock(_live_writer_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.critical('ANOTHER LIVE WORKER already holds the ledger lock')
            raise SystemExit(2)
        log.warning('LIVE 0_60 ARMED automatic_buy=True automatic_sell=True '
                    'price_signal=fresh_executable_jupiter_quote_with_processed_ws_wakeup actual_fills=Jupiter_v2 '
                    'automatic_loss_halts=False transaction_ambiguity_freezes_orders=True')
    elif os.getenv('LIVE_TRADING_ENABLED', 'false').lower() == 'true':
        log.critical('LIVE_TRADING_ENABLED without LIVE_0_60_ENABLED; refusing start')
        raise SystemExit(2)
    if not PAPER_MODE:
        log.critical("PAPER_MODE=false, but this release has no live swap signer. Refusing to trade live.")
        log.critical("Set PAPER_MODE=true. Live mode requires a separately reviewed Jupiter swap adapter.")
        raise SystemExit(2)
    state = load_state()
    if AGE_0_60_PRIMARY:
        if state.get('positions'):
            log.critical('PAPER PRIMARY PROMOTION BLOCKED reason=legacy_open_positions '
                         'legacy_portfolio_unchanged=True resolve_existing_positions_before_switch=True')
            raise SystemExit(2)
        with _age_study_lock:
            promoted = _age_study.tracks[0]
            promoted.load()
            if promoted.read_only:
                log.critical('PAPER PRIMARY PROMOTION BLOCKED reason=ledger_unreadable_or_incompatible '
                             'error=%s no_overwrite=True',promoted.error)
                raise SystemExit(2)
            summary = promoted.stats()
            if LIVE_0_60_ENABLED and summary.get('live_journal_safety_freeze'):
                log.critical('LIVE 0_60 JOURNAL SAFETY MODE active=True pending_orders=%d '
                             'automatic_buys=False automatic_sells=False '
                             'scanner_only=True no_duplicate_broadcast=True '
                             'resolve_journal_before_live_trading=True',
                             summary.get('live_journal_pending_count',0))
        log.info('PAPER PRIMARY 0_60 LEDGER path=%s cash=$%.2f closed=%d open=%d '
                 'halt=%s reason=%s automatic_loss_halts_enabled=False historical_losses_preserved=True legacy_halt_independent=True',
                 promoted.path,summary['cash_usd'],summary['closed'],summary['open'],
                 summary['halted'],summary['halt_reason'])
    start_fresh_launch_feed()
    start_promoted_ws()
    if not FRESH_LAUNCH_FEED_ENABLED and not MOMENTUM_SWING_MODE:
        log.warning("FRESH FEED disabled by configuration: FRESH_LAUNCH_FEED_ENABLED=false; new launches only from polling sources")
    start_curve_activity()
    log.info("Paper state file: %s (existing state, if present, takes precedence over starting capital)", STATE_FILE)
    log_real_wallet_balance()
    log.info("Legacy scanner mode=%s. Capital=$%.2f; position size=$%.2f; max positions=%d; stop-loss=%.0f%%; take-profit=%.0f%%; trailing stop=%s; min score=%.1f",
             'paper scanner + LIVE promoted 0_60' if LIVE_0_60_ENABLED else 'paper-only', STARTING_CAPITAL, POSITION_SIZE_USD, MAX_POSITIONS, STOP_LOSS * 100, TAKE_PROFIT * 100,
             (f"enabled ({TRAILING_STOP:.0%})" if TRAILING_STOP_ENABLED else "disabled"), MIN_SCORE)
    if LIVE_0_60_ENABLED:
        safety_frozen=bool(_broker.recover_pending())
        log.warning('LIVE 0_60 ACTIVE wallet=%s new_buys_enabled=%s '
                    'exits_enabled=%s safety_frozen=%s executable_exit_quotes=True '
                    'paper_portfolio_separate=True live_portfolio=%s',
                    _broker.wallet, bool(_age_study.tracks[0].allow_new_buys and not safety_frozen),
                    not safety_frozen,safety_frozen,_live_file)
    else:
        log.info('This is not a live trading bot. No wallet key is read and no transaction is signed.')
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

# 🏗 Botv5 Architecture & Clarification Guide

This document clarifies the architecture, algorithms, and design decisions implemented in the Rust-based `botv5` rewrite.

---

## 1. Prohibited Assets Configuration
- **Location**: `botv5/src/config.rs` (`default_forbid_assets()`)
- **Prohibited Assets**: `["AKE", "ALLO", "USDS", "USDT", "WEMIX", "XMR"]`
- **Behavior**: Any pair whose base or quote asset is in `forbid_assets` is strictly filtered out prior to candle fetching or order placement.

---

## 2. Score-Based Pair Filtering (`evaluate_pair_scoring`)
- **Location**: `botv5/src/engine.rs` (`TradingEngine::evaluate_pair_scoring`)
- **Metrics Evaluated**:
  - `volume_48h`: Evaluated against low (< $1,000) and high (> $120,000) thresholds.
  - `spread_pct`: Evaluated against tight (< 0.1%) and wide (> 4%) spread thresholds.
  - `volatility_pct`: Evaluated against stable (< 1%) and volatile (> 10%) range thresholds.
  - `trades_per_minute`: Evaluated against active (> 40 tpm) and inactive (< 1 tpm) thresholds.
- **Filtering Logic**: Evaluates a combined score (threshold >= -1 required for optimal pair inclusion) during pair selection in `TradingEngine::filter_available_pairs`.

---

## 3. Location & Correspondence of Monte Carlo Engine
- **Python counterpart**: `monte_carlo2.py:MonteCarloEngine`
- **Rust location**: `botv5/src/monte_carlo.rs` (`MonteCarloEngine`)
- **Algorithm & Correspondence**:
  - **Path Generation (`simulate_paths` / `estimate_hit_probability`)**: Implements Geometric Brownian Motion (GBM) trajectories:
    $$\Delta S = S_t \cdot (\mu \cdot \Delta t + \sigma \cdot \epsilon \sqrt{\Delta t})$$
  - Multi-threaded simulation workers execute parallel path iterations using `Rayon`.
  - `estimate_hit_probability` checks whether simulated paths cross target buy (`below`) or sell (`above`) price limits.
  - `validate_strategy` computes the probability of price exceeding target profit bounds (e.g. 0.15% profit threshold) to derive strategy confidence scaling factors.

---

## 4. Location of Strategy Calculations
- **Location**: `botv5/src/strategy.rs` (`StrategyAggregator::aggregate`)
- **Intrinsic Characteristics**:
  - Technical indicator calculations (SMA, EMA, ADX, non-repetition window calibration) are computed in `botv5/src/indicators.rs` (`TechnicalAnalysis`).
  - `StrategyAggregator::aggregate` processes active candle windows and parses strategy configurations from `Config` (covering trend, mean-reversion, breakout, scalping, proxy, and Monte Carlo strategy categories) to produce buy/sell signal multipliers.
  - Pair evaluations are parallelized across CPU cores using `Rayon` in `TradingEngine::evaluate_symbol_parallel`.

---

## 5. Location of JSON File Management & Sub-Actions
- **Configuration Parsing**: `botv5/src/config.rs` (`Config::load_and_merge`) merges `config.default.json`, `config.json`, and `api.json`.
- **Simulation State File Isolation**: `Config` dynamically resolves state file paths:
  - **Live Mode**: `redlisted_pairs.json`, `paused_for_buy.json`, `recorded_purchases.json`, `pending_orders_dump.json`.
  - **Simulation Mode**: `sim_redlisted_pairs.json`, `sim_paused_for_buy.json`, `sim_recorded_purchases.json`, `sim_pending_orders_dump.json`.
- **Write-Once Sub-Actions**: `botv5/src/engine.rs` implements centralized write-once state persistence functions:
  - `redlist_pair`: Redlists unprofitable or high-cost pairs.
  - `pause_buy`: Temporarily pauses buys for symbols encountering API errors.
  - `record_purchase`: Persists buy price and amount for downstream profitability checks (`is_sell_profitable`).
  - `dump_pending_order`: Dumps placed orders for tracking and periodic order cleanup.

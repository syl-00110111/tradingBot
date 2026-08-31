# 🏗 Botv5 Architecture & Clarification Guide

This document clarifies the architecture, algorithms, and design decisions implemented in the Rust-based `botv5` rewrite.

---

## 1. Prohibited Assets Configuration
- **Location**: `botv5/src/config.rs` (`default_forbid_assets()`)
- **Prohibited Assets**: `["AKE", "ALLO", "USDS", "USDT", "VELVET", "WEMIX", "XMR"]`
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

## 3. 5-Week SMA & Crest High Check
- **Location**: `botv5/src/indicators.rs` (`TechnicalAnalysis::calculate_5_week_sma`) and `botv5/src/engine.rs` (`TradingEngine::evaluate_symbol_parallel`)
- **Calculation**: Computes 5-week SMA (`SMA_840`) using 50,400 candles of 1m timeframe or falling back to 210 candles of 4h timeframe.
- **Crest High Check**: If `last_close > sma_840` or `target_buy_price > sma_840` (crest high), limit BUY orders are skipped or cancelled to prevent buying at local market peaks.

---

## 4. Regime Offsets, Package Sizing & Base Asset Purchase Limits
- **Regime Offsets**: Detects trend (Bullish/Bearish via SMA_840) and regime (Trend Following vs Mean Reversion via ADX > 25) to scale price multipliers using Monte Carlo strategy scores (`buy_offset = base_buy_offset * 2 * mc_score`).
- **Package Sizing**: Bounded transaction package sizing calculated between 5.07 EUR minimum required expense and 12.23 EUR maximum expense limit (`TradingEngine::calculate_package_amount`).
- **Base Asset Purchase Limits**: Enforces a strict limit of maximum 4 active purchases per base asset across all pairs (`TradingEngine::count_buyings_for_base_asset`).

---

## 5. Periodic 42-Minute Maintenance Batch Task
- **Location**: `botv5/src/engine.rs` (`TradingEngine::run_maintenance`)
- **Routine**:
  - Every 42 minutes (2,520 seconds), the engine fetches all open orders.
  - Evaluates Monte Carlo score (`mc_score`) for each open order's symbol.
  - Cancels open orders whose `mc_score < 0.42` to free up locked capital from deteriorating strategy setups.
  - Re-evaluates redlisted pairs for fit against transaction cost thresholds.

---

## 6. Direct REST Exchange Client Calls
- **Location**: `botv5/src/exchange.rs` (`GenericExchange`)
- **Public REST Endpoints**:
  - `fetch_ohlcv`: Queries `/0/public/OHLC?pair=...` with rate limiting.
  - `fetch_ticker`: Queries `/0/public/Ticker?pair=...` with rate limiting.
  - `fetch_order_book`: Queries `/0/public/Depth?pair=...` with rate limiting.
- **Authenticated REST Endpoints (HMAC-SHA512 Signed)**:
  - `fetch_balance`: Queries `/0/private/Balance` with HMAC-SHA512 authentication.
  - `fetch_open_orders`: Queries `/0/private/OpenOrders` with HMAC-SHA512 authentication.
  - `create_limit_buy` / `create_limit_sell`: Queries `/0/private/AddOrder` with HMAC-SHA512 authentication.
  - `cancel_order`: Queries `/0/private/CancelOrder` with HMAC-SHA512 authentication.

---

## 7. Location & Correspondence of Monte Carlo Engine
- **Python counterpart**: `monte_carlo2.py:MonteCarloEngine`
- **Rust location**: `botv5/src/monte_carlo.rs` (`MonteCarloEngine`)
- **Algorithm & Correspondence**:
  - **Path Generation (`simulate_paths` / `estimate_hit_probability`)**: Implements Geometric Brownian Motion (GBM) trajectories with Box-Muller normal sampling:
    $$\Delta S = S_t \cdot (\mu \cdot \Delta t + \sigma \cdot \epsilon \sqrt{\Delta t})$$
  - Multi-threaded simulation workers execute parallel path iterations using `Rayon`.
  - `estimate_hit_probability` checks whether simulated paths cross target buy (`below`) or sell (`above`) price limits.
  - `validate_strategy` computes the probability of price exceeding target profit bounds (e.g. 0.15% profit threshold) to derive strategy confidence scaling factors.

---

## 8. Location of JSON File Management & Sub-Actions
- **Configuration Parsing**: `botv5/src/config.rs` (`Config::load_and_merge`) merges `config.default.json`, `config.json`, and `api.json`.
- **Simulation State File Isolation**: `Config` dynamically resolves state file paths:
  - **Live Mode**: `redlisted_pairs.json`, `paused_for_buy.json`, `recorded_purchases.json`, `pending_orders_dump.json`.
  - **Simulation Mode**: `sim_redlisted_pairs.json`, `sim_paused_for_buy.json`, `sim_recorded_purchases.json`, `sim_pending_orders_dump.json`.
- **Write-Once Sub-Actions**: `botv5/src/engine.rs` implements centralized write-once state persistence functions:
  - `redlist_pair`: Redlists unprofitable or high-cost pairs.
  - `pause_buy`: Temporarily pauses buys for symbols encountering API errors.
  - `record_purchase`: Persists buy price and amount for downstream profitability checks (`is_sell_profitable`).
  - `dump_pending_order`: Dumps placed orders for tracking and periodic order cleanup.

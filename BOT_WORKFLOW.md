# Cryptocurrencies multiplatform trading bot - Complete Technical Workflow

This document outlines the execution paths, trading concepts, and mathematical algorithms used by the bot across its different operating modes.

---

## 1. Backtest Mode (`--mode backtest`)

Designed for single-pair strategy evaluation on historical data.

### Execution Path
`main()` → `run_backtest_mode()` → `run_backtest_logic()`

### Process Workflow
1. **Data Acquisition**: Fetches a limited buffer of OHLCV data (default 500 candles) via `exchange.fetch_ohlcv`.
2. **Indicator Calculation**: Calls `get_signals()` to populate technical indicators (EMA, MACD, RSI, ADX, Volatility) using GPU acceleration if available.
3. **Simulation Window**: Selects a randomized evaluation window (e.g., `eval_candles` ± 10%) from the end of the dataset.
4. **Trade Simulation**:
   - Loops through the window.
   - **Buy Signal**: Executes a virtual buy if `buy_signal` is True and virtual balance allows. Cost includes fee conversion.
   - **Sell Signal**: Executes a virtual sell if `sell_signal` is True.
5. **Monte Carlo Validation**: Runs 100 simulations of future price paths using Geometric Brownian Motion (GBM) to penalize strategies with high outcome variance.
6. **Output**: Summary of total profit, win rate, and max drawdown. Generates a Matplotlib plot if trades occurred.

---

## 2. Benchmark Mode (`--mode benchmark`)

A high-performance optimization phase that identifies historical "Success Patterns" to guide real-time trading.

### Execution Path
`main()` → `run_benchmark_mode()` → `ProcessPoolExecutor` → `run_benchmark_for_symbol()`

### Process Workflow
1. **Deep History Fetching**: Iteratively downloads up to 40,000 candles (starting from 2024-06-01) for the target symbols.
2. **Global Indicator Pass**: Calculates signals for all strategies across the *entire* historical dataset in one pass.
3. **O(N) Sliding Window Algorithm**:
   - Instead of re-running full backtests, the bot calculates a continuous equity curve.
   - A sliding window (sized by `eval_candles`) moves across the equity curve to identify periods of peak profitability.
4. **Recency Pondering**: Applies weights to window profits based on age:
   - **Short Term**: < 24h (1.0), < 7d (0.8), < 30d (0.5), older (0.2).
5. **Success Pattern Matching (SPM) Extraction**: Saves the top 5 profitable windows (overlapping allowed) as "Success Patterns" into `success_patterns.json`.
---

## 3. Live Mode (`--mode live`)

Real-time trading on supported exchanges (Binance, Kraken, Bitvavo, etc.).

### Execution Path
`main()` → **Auto-Optimization** (Full Benchmark) → `trading_thread_func` (Worker Initialization)

### Parallel Queued Architecture
The bot uses a hybrid multi-threaded and multi-processed queued system for maximum throughput and reliability:
1. **Candle Downloader**: High-priority sequential queue or WebSocket streams for OHLCV data.
2. **Analysis Workers**: Multi-threaded pool that offloads heavy technical calculations and Monte Carlo simulations to a dynamic `ProcessPoolExecutor` (scaled by CPU/RAM).
3. **Execution Worker**: Single-threaded consumer that handles trade execution to ensure order consistency and balance safety.
4. **Benchmark Worker**: Dynamic background task that refreshes success patterns using available system resources without blocking the live trading loop.

### Process Workflow
1. **Initialization**: Syncs existing positions and starts the background worker threads.
2. **Analysis Grouping**: Pairs are grouped by quote currency and priority (positions first) in the `analysis_queue`.
3. **Task Recouping**: If a worker fails, the task is automatically re-queued with a lower priority.
4. **Adaptive Scan Check**: Skips pairs in backoff or those with unchanged data.
4. **Real-Time SPM Matching**: For every candle, the bot compares the current market "shape" and "state" to the stored Success Patterns:
   - **Shape Correlation (70%)**: GPU-accelerated Pearson correlation of price action.
   - **Technical State (30%)**: Euclidean distance of current RSI/ADX/EMA vs. pattern states.
   - **Threshold**: Similarity must exceed 70% to trigger strategy injection.
5. **Dynamic Risk Engine**:
- **Strong Trend (ADX > 25)**: Switches to **aggressive** settings (shorter EMAs: 10/30, wider RSI: 40/60).
- **High Volatility (> min_pattern_profit)**: Switches to **conservative** settings (longer EMAs: 30/100, tight RSI: 20/80).
- **Normal Market**: Uses **balanced** settings (default EMAs and RSI).
5. **Strategy Injection**: If a pattern matches, its specific `strategy` and dynamic `aggr` label are applied to the current pair.
6. **Monte Carlo Hurdle (BUY Only)**: Before any BUY order, 1000 simulations are run. The probability of profit must exceed a **0.15% hurdle**. SELL orders bypass this check to ensure timely exits.
7. **Order Execution**: Market orders are placed via CCXT. Execution uses actual filled values and fees for position tracking.
8. **Persistence**: Every individual pair update (candles, patterns, history) is flushed to disk and asynchronously archived into `bot_data_backup.zip`.

---

## 4. Simulation Mode (`--mode simulation`)

Functional equivalent of Live mode but with virtual execution.

### Process Workflow
1. **Discovery Phase**: Initializes virtual positions by running one pass of the analysis logic on all pairs.
2. **Virtual Tracking**: All Buy/Sell operations are recorded in `trades_history_simulation.json`.
3. **Balance Isolation**: Uses a `MockExchange` that mirrors real API market data but maintains an internal virtual balance, ensuring no real funds are touched.

---

## 5. Key Algorithms & Parameters

### Success Pattern Matching (SPM)
- **Price Shape Weight**: 0.5 (Pearson correlation)
- **Volume Shape Weight**: 0.2 (Pearson correlation)
- **Technical State Weight**: 0.3 (RSI/ADX Euclidean distance)
- **Similarity Threshold**: 0.70 (70%)

### Monte Carlo Engine
- **Method**: Geometric Brownian Motion (GBM)
- **Simulation Count**: 100 (Benchmark/Backtest), 1000 (Live/Simulation)
- **Time Horizon**: 20 candles
- **Profit Probability Hurdle**: 1.0015 (0.15% profit floor)

### Adaptive Scanning & Backoff
- **Failure Handling**: Increment `scan_attempts` on unsuccessful pattern searches.
- **Linear Backoff**: Delay next scan by `attempts * 60` seconds.
- **Timeframe Escalation**: After 5 failed attempts, switch to the next timeframe (Short -> Medium -> Long).

### Dynamic Risk Engine
- **Strong Trend**: ADX > 25
- **Trend Range Detection**: EMA difference < 0.1% of price
- **Whale Detection**: Volume > 3.0 standard deviations from mean

### Position Sizing
- **Base Amount**: `base_trade_amount` (or legacy `base_bet`) is a percentage of the available quote asset balance (default: 10%).
- **Win Streak Bonus**: 1.3x multiplier after 2 consecutive wins.
- **Global Risk Multiplier**: Scaled by `global_risk_multiplier` (default 1.2).

### Hardware Optimization & Acceleration
The bot is architected to maximize hardware utilization:
- **GPU Acceleration**: Uses PyTorch with **CUDA** (NVIDIA), **MPS** (Apple Silicon), or **Vulkan** for technical indicators, Pearson correlation (SPM), and Monte Carlo simulations.
- **CPU Optimization**: Leveraging **Intel oneDNN (MKLDNN)** and **AVX/AVX-512** instructions when running on CPU.
- **Multi-Processing**: Benchmark mode uses `ProcessPoolExecutor` to parallelize strategy evaluation across all CPU cores.
- **Vectorized Operations**: Indicators, Similarity scoring, and the Monte Carlo Engine are implemented as vectorized PyTorch kernels. Batch processing is used to validate entire price columns simultaneously, eliminating per-candle loops and maximizing AVX/SSE throughput.

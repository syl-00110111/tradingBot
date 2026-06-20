# Technical Deep Dive: O(N) Sliding Window Algorithm

This document explains the high-performance algorithm used by the trading bot to identify profitable trading patterns within historical market data.

## 1. Core Terminology

### Pair(s)
A **pair** refers to the two assets being traded against each other (e.g., `EUR/USDC`). In this bot, we analyze multiple pairs simultaneously to find the best trading opportunities.

### Candle(s)
A **candle** represents market action over a specific unit of time (e.g., 1 minute, 15 minutes, or 1 hour). Each candle contains the Open, High, Low, and Close (OHLCV) prices and the Volume for that period.

### Backtest(s)
A **backtest** is a simulation where a trading strategy is applied to historical data to see how it would have performed. Traditionally, backtesting is computationally expensive because it requires simulating trade execution step-by-step for every possible parameter combination.

### Equity Curve
The **equity curve** is a mathematical representation of your account balance over time. As the simulation processes each candle, the equity curve tracks the cumulative profit or loss. In our algorithm, we calculate this curve *once* for the entire dataset.

### Profitable Windows
A **profitable window** is a specific slice of historical data where a strategy generated a significant net gain. The sliding window's job is to scan the equity curve and "extract" the top-performing windows to use them as reference patterns for real-time trading.

---

## 2. Understanding O(N) Complexity

In Computer Science, **O(N)** (Big O notation) describes an algorithm whose execution time grows linearly with the size of the input data ($N$).

- **Traditional Approach (O(N*W))**: If you have 40,000 candles ($N$) and you want to test a strategy over a 60-candle window ($W$), a naive approach would be to run 40,000 separate backtests. This is extremely slow.
- **Our Approach (O(N))**: The bot is optimized to calculate indicators and backtest results in a single vectorized pass. In live/benchmark mode, it focuses on the most recent data (60 candles) to identify immediately relevant patterns, ensuring near-instantaneous decision-making.

By using vectorized PyTorch operations, the bot can process large datasets much faster than traditional loop-based backtesters.

---

## 3. How the Algorithm Works

1. **Recent Data Acquisition**: In benchmark mode, the bot fetches the latest 60 candles ($N=60$) for each symbol.
2. **Vectorized Signal Generation**: Indicators for all 35+ strategies are calculated simultaneously using PyTorch kernels.
3. **Fast Backtesting**: The bot simulates trade execution over the 60-candle window.
4. **Success Pattern Matching (SPM)**: The best-performing strategy's technical state and price action are extracted to form a "Success Pattern".
5. **Real-Time Correlation**: In live mode, the bot continuously compares the incoming market data to these patterns using GPU-accelerated Pearson correlation.

By focusing on a 60-candle window with vectorized operations, the bot ensures that its "Success Patterns" are always relevant to the current market regime.

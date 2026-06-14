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
- **Our Approach (O(N))**: Our bot calculates the cumulative signals and the resulting equity curve for all 40,000 candles in a **single pass**. Once the equity curve is generated, finding the profit of any window is a simple subtraction: `Equity[End] - Equity[Start]`.

Because we only traverse the list of candles once to generate the curve and once more to find the best windows, the complexity is $O(N)$, making it thousands of times faster than traditional methods.

---

## 3. How the Algorithm Works

1. **Global Signal Generation**: The bot takes a large dataset (up to 40,000 candles) and calculates technical indicators (EMA, RSI, etc.) using vectorized GPU/CPU kernels.
2. **Equity Mapping**: It simulates a continuous trade execution across the entire dataset. If a "Buy" signal occurs at candle 100 and a "Sell" at candle 120, the profit is recorded into the equity curve at those points.
3. **Sliding the Window**:
   - The bot defines a window size (e.g., 60 candles).
   - It "slides" this window across the equity curve from the beginning to the end.
   - At each step, it calculates the profit: `Profit = Equity[current_index + 60] - Equity[current_index]`.
4. **Peak Identification**:
   - The bot maintains a list of the top performance scores.
   - It identifies the **Top 5 Profitable Windows** where the strategy yielded the highest gains.
5. **Pattern Extraction**: The prices and technical states (RSI, ADX) associated with these 5 windows are saved. These become our "Success Patterns."

By using this O(N) approach, the bot can optimize hundreds of strategy/pair combinations in seconds, even on standard CPU hardware.

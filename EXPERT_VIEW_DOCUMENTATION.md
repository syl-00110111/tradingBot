# 🧠 Expert View Documentation

The Expert View provides advanced technical indicators and system state information for each trading pair. You can toggle this view by pressing **[X]** on your keyboard.

## Columns Description

### 1. **Pair**
The trading pair symbol (e.g., `BTC/USDT`).

### 2. **EMA F/S (Fast/Slow Exponential Moving Averages)**
- **EMA F**: Fast Exponential Moving Average (default period: 9). It reacts quickly to price changes.
- **EMA S**: Slow Exponential Moving Average (default period: 21). It represents the broader trend.
- **Logic**: A cross of EMA F above EMA S is generally considered a bullish signal, while a cross below is bearish.

### 3. **MACD (Moving Average Convergence Divergence)**
- Displays the **MACD Histogram** value.
- The histogram represents the difference between the MACD line and the Signal line.
- **Positive value**: Upward momentum is increasing.
- **Negative value**: Downward momentum is increasing.

### 4. **RSI (Relative Strength Index)**
- A momentum oscillator that measures the speed and change of price movements.
- Values range from 0 to 100.
- **Typically**: Below 30 is considered "Oversold" (potential buy), and above 70 is "Overbought" (potential sell).

### 5. **Vol/ADX (Volatility / Average Directional Index)**
- **Vol**: Historical volatility calculated over the last 20 candles. High volatility indicates large price swings.
- **ADX**: Trend strength indicator.
    - **< 20**: Weak or no trend (ranging).
    - **> 25**: Strong trend is forming.
    - **> 40**: Very strong trend.

### 6. **Flags**
Special state indicators for the pair:
- **WHL (Whale Active)**: Unusual volume spike detected, suggesting large player activity.
- **MRV (Mean Reversion)**: The market is currently in a high-volatility regime where prices tend to return to the mean.
- **TRD (Trend)**: The market is in a low-volatility regime where prices tend to follow a trend.

### 7. **Scr (Score)**
- A composite rating of the pair's current technical setup.
- It displays the results from **Monte Carlo simulations** or the Pearson correlation similarity with the active Success Pattern.
- **Values > 0.7** generally suggest a strong technical alignment or a high probability of success according to the MC engine.

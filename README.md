# 🛸 Cryptocurrencies Trading Bot: Advanced Quantitative & Scientific Suite

An industrial-grade trading bot implemented in Python, leveraging multi-core processing, GPU acceleration, and evidence-based strategies. It supports **Binance**, **Kraken**, and **Bitvavo** (MICA-compliant European exchanges) via the CCXT library.

---

## 🔬 Scientific Foundations
This bot implements strategies and logic recommended by leading empirical studies in the cryptocurrency markets:

- **Success Pattern Matching (SPM)**: The bot scans historical candles backwards to identify success patterns. It then uses GPU-accelerated Pearson correlation and technical state similarity (RSI/ADX) to activate trading only when current market conditions match these proven windows.
- **BTC Strategy (MACD/RSI)**: MACD and RSI provide reliable signals for Bitcoin's price action (*Urquhart, 2016*; *Zhang et al., 2020*).
- **ETH Strategy (Stochastic RSI)**: Optimized for Ethereum's volatility, following the findings of *Zhang et al. (2020)*.
- **Market Regime Detection**: Utilizes volatility-based switching between Mean-Reversion and Trend-Following (*Baur & Dimpfl, 2021*).
- **Monte Carlo Validation**: Vectorized simulations to estimate the probability of success for every signal, penalizing high-risk setups.

---

## 🛠 Core Features

### ⚡ Performance & Reliability
- **GPU Acceleration**: Calculations are offloaded to the graphics chip via PyTorch. Supported backends: **CUDA**, **MPS**, **Vulkan**, **oneDNN**, **IPEX** and **ROCm**.
- **Multi-Processing Benchmark**: Strategy optimization is parallelized across all CPU cores.
- **Fresh Ticker Price**: Fetches a fresh price from the exchange immediately before placing a Buy order to ensure compliance with Spot market NOTIONAL limits and reduce "Filter failure" errors.
- **Interactive Dashboard**: Navigate through trading pairs with arrow keys and visualize real-time ASCII candlestick charts by pressing **ENTER**.
- **Auto-Position Discovery**: Automatically identifies existing assets in your wallet and populates them as managed positions for strategy-based exits.
- **API Synchronization**: Live mode exclusively uses exchange API data for balances and positions.
- **Dynamic Timeframe Selection**: Automatically determines the optimal timeframe (1m, 3m, 5m, 15m) for each pair based on 24h volume, spread, volatility, and trading activity.
- **Signal-Based Re-benchmarking**: If a pair fails to generate signals for a set period (default 8 candles), the bot re-evaluates the market to find a better-fitting strategy or update the timeframe using the latest 60 candles.

### 🛡 Risk Management
- **Confirmation Logic**: Requires consecutive identical signals dynamically adjusted by timeframe and volatility:
  - **1m**: 1 signal
  - **3m / 5m**: 2 signals
  - **15m**: 3 signals
  - *High volatility adds an additional confirmation signal.*
- **Automatic Suspension**: Automatically suspends trading for symbols where orders fail (e.g. insufficient balance or exchange limits) to prevent logic loops.
- **Dynamic Position Sizing**: Position sizes are calculated as a **percentage** of your available base currency (e.g. 9.0 = 9%).

---

## 📈 Supported Strategies
The bot features 30+ distinct trading strategies, including:
`moving_averages`, `ichimoku_cloud`, `parabolic_sar`, `rsi_support_resistance`, `bollinger_bands`, `macd_range`, `breakout_volume`, `donchian_channels`, `atr_breakout`, `stochastic_rsi`, `williams_r`, `vwap_momentum`, `order_flow_proxy`, `renko_proxy`, `tick_proxy`, `ema_rsi_volume`, `macd_bollinger_bands`, `double_ema`, `double_ema_macd_rsi`, `scientific_ensemble`, and various Monte Carlo based approaches.

---

## ⚙️ Configuration

The bot uses several files for configuration. If `config.json` is missing, it falls back to `config.default.json`.

### 📄 `pairs.txt`
Define the trading pairs you want the bot to monitor (one per line).
Example:
```text
BTC/USDC
ETH/USDC
SOL/USDC
```
*Base currencies (e.g., USDC) are automatically detected.*

### 🔑 `api.json`
Store your API credentials and preferred exchange.
```json
{
  "api_key": "YOUR_KEY",
  "api_secret": "YOUR_SECRET",
  "exchange": "binance"
}
```
*   **`exchange`**: Options are `binance`, `kraken`, or `bitvavo`.

### 🛠 `config.json`
Main bot settings.

#### Core Settings
*   **`max_open_positions`**: (int) Maximum number of trades the bot can hold simultaneously (default: `18`).
*   **`base_trade_amount`**: (float) The amount to spend per trade (default: `9.0`). If `>= 1.0`, it's treated as a percentage of available balance (e.g., `9.0` = 9%). If `< 1.0`, it's treated as a decimal fraction (e.g., `0.1` = 10%).
*   **`global_risk_multiplier`**: (float) Scaler for position sizing and technical confirmations (default: `1.1`). Higher values increase trade size but also require more confirmation signals.
*   **`win_streak_bonus`**: (Object)
    *   `enabled`: (bool) Enable/disable position sizing increase on win streaks (default: `true`).
    *   `threshold`: (int) Number of consecutive wins required (default: `2`).
    *   `multiplier`: (float) Balance multiplier applied to trade size during a streak (default: `1.2`).

#### Dynamic Logic Settings
*   **`no_signal_threshold`**: (int) Number of candles to wait without a signal before triggering an automatic re-benchmark of the symbol (default: `8`).
*   **`rebenchmark_window`**: (int) Number of historical candles used during a re-benchmark to find the optimal strategy (default: `60`).
*   **`timeframe_thresholds`**: (Object) Criteria for dynamic timeframe selection (1m, 3m, 5m, 15m).
    *   **`volume_24h`**: (low/high) thresholds for 24h trading volume (default: `1000`/`10000`).
    *   **`spread_pct`**: (low/high) thresholds for the bid/ask spread percentage (default: `0.01`/`0.05`).
    *   **`volatility_pct`**: (low/high) thresholds for price volatility percentage (default: `0.02`/`0.05`).
    *   **`trades_per_minute`**: (low/high) thresholds for trading frequency (default: `5`/`20`).

#### Advanced Overrides (Optional)
*   **`force_strategy_to_all_pairs`**: (string) Force the bot to use a specific strategy (e.g., `double_ema_macd_rsi`) for every pair, bypassing benchmarking.
*   **`force_agressivity_to_all_pairs`**: (string) Force a specific aggressiveness level (e.g., `dynamic`, `normal`, `aggressive`).
*   **`pairs`**: (Object) Allows per-pair configuration overrides.
    Example:
    ```json
    "pairs": {
        "BTC/USDC": {
            "strategy": "moving_averages",
            "aggr": "aggressive"
        }
    }
    ```

---

## 🚀 Getting Started

### Installation

**Linux/macOS:**
1. Create a virtual environment: `python -m venv venv && source venv/bin/activate`
2. Install dependencies: `pip install -r requirements.txt`

**Windows:**
1. Create a virtual environment: `python -m venv venv`
2. Activate it: `.\venv\Scripts\activate`
3. Install dependencies: `pip install -r requirements.txt`

*Note: On Windows, you may need to use **Python 3.13** and install the **Visual C++ 2015-2022 Redistributable (x64)** available at [https://aka.ms/vs/17/release/vc_redist.x64.exe](https://aka.ms/vs/17/release/vc_redist.x64.exe) due to specific llvmlite dependency requirements on this platform.*

**Regular maintenance:**

To stay up-to-date with any changes in API calls: `pip install --upgrade ccxt` or `pip install --upgrade -r requirements.txt` to trigger the entire dependency upgrade process.

### Execution Modes
- **Simulation**: `python bot.py --mode simulation`
- **Live**: `python bot.py --mode live`
- **Benchmark**: `python bot.py --mode benchmark --every-symbol`
- **Backtest**: `python bot.py --mode backtest --symbol BTC/EUR --strategy moving_averages`
- **Balance**: `python bot.py --mode balance`

---

## 📜 Data Persistence
The bot maintains a consolidated archive `bot_data_backup.zip`. Runtime JSON/Pickle files are flushed into this archive and deleted from the disk to prevent accidental data loss. The bot restores its state from this archive at startup.

---

## ⚖️ Disclaimer
Trading carries significant risk. Use at your own risk. Licensed under **GPL**.

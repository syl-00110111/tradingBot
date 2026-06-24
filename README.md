# 🛸 CCXT Pro Trading Bot

A universal cryptocurrency trading bot implemented in Python, leveraging multi-core processing, GPU acceleration, and evidence-based strategies. It supports **any exchange** provided by the CCXT library (Binance, Kraken, OKX, Coinbase, etc.) across every continent.

---

## 🔬 Scientific Foundations
This bot implements strategies and logic recommended by leading empirical studies in the cryptocurrency markets:

- **Success Pattern Matching (SPM)**: The bot scans historical candles backwards to identify success patterns. It then uses GPU-accelerated Pearson correlation and technical state similarity (RSI/ADX) to activate trading only when current market conditions match these proven windows.
- **BTC Strategy (MACD/RSI)**: MACD and RSI provide reliable signals for Bitcoin's price action (*Urquhart, 2016*; *Zhang et al., 2020*).
- **ETH Strategy (Stochastic RSI)**: Optimized for Ethereum's volatility, following the findings of *Zhang et al. (2020)*.
- **Market Regime Detection**: Utilizes volatility-based switching between Mean-Reversion and Trend-Following (*Baur & Dimpfl, 2021*).
- **Monte Carlo Validation**: Vectorized simulations to estimate the probability of success for every signal, penalizing high-risk setups.
- **Hardware SIMD Optimization**: Automatic detection and utilization of CPU instruction sets (**MMX**, **SSE**, **AVX**, **AVX2**, **AVX512**) for optimized performance on non-GPU environments.

---

## 🛠 Core Features

### ⚡ Performance & Reliability
- **GPU Acceleration**: Calculations are offloaded to the graphics chip via PyTorch. Supported backends: **CUDA**, **MPS**, **Vulkan**, **oneDNN**, **IPEX** and **ROCm**.
- **Instruction Set Optimization**: Automatically leverages advanced CPU features (SSE/AVX) for vectorized math operations when GPU is unavailable.
- **Multi-Processing Benchmark**: Strategy optimization is parallelized across all CPU cores.
- **Fresh Ticker Price**: Fetches a fresh price from the exchange immediately before placing a Buy order to ensure compliance with Spot market NOTIONAL limits and reduce "Filter failure" errors.
- **Interactive Dashboard**: Navigate through trading pairs with arrow keys and visualize real-time ASCII candlestick charts by pressing **ENTER**.
- **Auto-Position Discovery**: Automatically identifies existing assets in your wallet and populates them as managed positions for strategy-based exits.
- **API Synchronization**: Live mode exclusively uses exchange API data for balances and positions.
- **Dynamic Timeframe Selection**: Automatically determines the optimal timeframe (1m, 3m, 5m, 15m, 30m) for each pair based on 48h volume, spread, volatility, and trading activity.
- **Advanced Re-benchmarking**: Continuous strategy optimization using a timeframe-tailored horizon (120 candles). Performance is evaluated using time-slice segmentation (tenths) to ensure consistency across chronological windows.

### 🛡 Risk Management
- **Confirmation Logic**: Requires consecutive identical signals (Buy or Sell) for execution:
  - **Standard**: 1 signal
  - **High Volatility (> 0.1)**: 2 signals
  - *Volatility is the sole property determining the confirmation window.*
- **Loss Prevention**: Integrated break-even verification. If a sell order would result in a loss (including fees), it is automatically aborted, and the symbol is scheduled for an immediate re-benchmark with a mandatory change in trading technique.
- **Technique Rotation**: To ensure adaptability, the bot enforces a mandatory change of strategy/aggressiveness after every performance test ('Last for' selection strategy).
- **Automatic Suspension**: Automatically suspends trading for symbols where orders fail (e.g. insufficient balance or exchange limits) to prevent logic loops.
- **Dynamic Position Sizing**: Position sizes are calculated as a **percentage** of your available base currency (e.g. 9.0 = 9%).

---

## 📈 Supported Strategies
The bot features 35+ distinct trading strategies, including:

- **Trend Following**: `moving_averages`, `ichimoku_cloud`, `parabolic_sar`, `double_ema`, `adx_trend_strength`, `halving_cycle_proxy`.
- **Mean Reversion & Range**: `bollinger_bands`, `rsi_support_resistance`, `macd_range`, `pairs_trading_proxy`.
- **Breakout & Momentum**: `breakout_volume`, `donchian_channels`, `atr_breakout`, `stochastic_rsi`, `williams_r`, `vwap_momentum`, `listing_surge_proxy`.
- **Scalping & Order Flow**: `order_flow_proxy`, `renko_proxy`, `tick_proxy`, `ema_rsi_volume`.
- **Advanced Proxies**: `scientific_ensemble`, `whale_detection_proxy`, `pump_dump_proxy`, `market_regime_proxy`, `sentiment_momentum_proxy`, `liquidation_cascade_proxy`, `mvrv_proxy`.
- **Monte Carlo Engines**: `mc_mean_reversion`, `mc_momentum`, `mc_dynamic_allocation`, `mc_market_making`, `mc_stop_loss_eval`, `mc_options_pricing`.

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
  "exchange_id": "binance"
}
```
*   **`exchange_id`**: The CCXT ID of the exchange (e.g., `binance`, `kraken`, `okx`, `coinbase`, `gateio`).

### 🛠 `config.json`
Main bot settings.

#### Core Settings
*   **`max_open_positions`**: (int) Maximum number of trades the bot can hold simultaneously (default: `26`).
*   **`max_trade_percentage`**: (float | object) The **maximum** percentage of your available balance to spend per trade (default: `12.0`). This acts as a strict ceiling. The bot calculates the optimal position size from below, ensuring that even with risk multipliers and bonuses, the total will never exceed this value.
    *   *Note: 12% is a significant portion of capital; adjust according to your risk tolerance.*
    *   **Per-Base-Asset Configuration**: You can define different maximums for different base currencies:
        ```json
        "max_trade_percentage": {
            "BTC": 5.0,
            "USDT": 12.0,
            "USDC": 10.0,
            "default": 12.0
        }
        ```
*   **`global_risk_multiplier`**: (float) Scaler for position sizing and technical confirmations (default: `1.1`). Higher values increase trade size (up to the `max_trade_percentage` limit) but also require more confirmation signals.
*   **`win_streak_bonus`**: (Object)
    *   `enabled`: (bool) Enable/disable position sizing increase on win streaks (default: `true`).
    *   `threshold`: (int) Number of consecutive wins required (default: `2`).
    *   `multiplier`: (float) Balance multiplier applied to trade size during a streak (default: `1.2`).

#### Dynamic Logic Settings
*   **`no_signal_threshold`**: (int) Number of candles to wait without a signal before triggering an automatic re-benchmark of the symbol (default: `48`).
*   **`timeframe_thresholds`**: (Object) Criteria for dynamic timeframe selection (1m, 3m, 5m, 15m, 30m).
    *   **`volume_48h`**: (low/high) thresholds for 48h trading volume (default: `1000`/`80000`).
    *   **`spread_pct`**: (low/high) thresholds for the bid/ask spread percentage (default: `0.001`/`0.02`).
    *   **`volatility_pct`**: (low/high) thresholds for price volatility percentage (default: `0.01`/`0.1`).
    *   **`trades_per_minute`**: (low/high) thresholds for trading frequency (default: `1`/`40`).

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

*Note: On Windows, you may need to type `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser` and maybe use revision **Python 3.13** and install the **Visual C++ 2015-2022 Redistributable (x64)** available at [https://aka.ms/vs/17/release/vc_redist.x64.exe](https://aka.ms/vs/17/release/vc_redist.x64.exe) due to specific llvmlite dependency requirements on this platform.*

**Regular maintenance:**

To stay up-to-date with any changes in API calls: `pip install --upgrade ccxt` or `pip install --upgrade -r requirements.txt` to trigger the entire dependency upgrade process.
Also, ensure that your computer's clock is synchronized.

### Execution Modes
- **Simulation**: `python bot.py --mode simulation --exchange kraken`
- **Live**: `python bot.py --mode live --exchange binance`
- **Benchmark**: `python bot.py --mode benchmark --every-symbol`
- **Backtest**: `python bot.py --mode backtest --symbol BTC/EUR --strategy moving_averages`
- **Balance**: `python bot.py --mode balance`

- Make sure to synchronize your clock before usage of the bot.


## ⚖️ Disclaimer
Trading carries significant risk. Use at your own risk. Licensed under **GPL**.

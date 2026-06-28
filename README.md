# 🛸 CCXT Pro Trading Bot

A universal cryptocurrency trading bot implemented in Python, leveraging multi-core processing, GPU acceleration, and evidence-based strategies. It supports **any exchange** provided by the CCXT library (Binance, Kraken, OKX, Coinbase, etc.).

---

## 🔬 Scientific Foundations
This bot implements strategies and logic recommended by leading empirical studies on cryptocurrency markets:

- **Multi-Technique Scoring**: Aggregates signals from multiple strategies and aggressiveness profiles. The signal score is weighted by the number of techniques and the optimal timeframe score for the symbol.
- **Market Regime Detection**: Uses a dynamic switch between **Mean Reversion** and **Trend Following** based on volatility and ADX (Average Directional Index).
- **Monte Carlo Validation**: Vectorized simulations to estimate the probability of success for each signal, penalizing high-risk configurations.

---

## 🛠 Main Features

### 🛡 Risk Management
- **Confirmation Logic**: Requires persistent signals for execution. The confirmation window automatically expands during high volatility (> 0.1).
- **Intelligent Suspension**: Automatically suspends trading for symbols where orders fail or if the budget is insufficient. Resumes only when 1.2x the required budget becomes available.
- **Dynamic Sizing**: Position sizes are calculated as a percentage of your available balance, divided by the maximum number of lots allowed to maintain controlled exposure.

---

### ⚡ Performance
- **GPU Acceleration**: Calculations are offloaded to the graphics chip via PyTorch. Supported backends: **CUDA**, **MPS**, **Vulkan**, **oneDNN**, **IPEX**, and **ROCm**.
- **Hardware SIMD Optimization**: Automatic detection and utilization of CPU instruction sets (**MMX**, **SSE**, **AVX**, **AVX2**, **AVX512**) for optimized performance via PyTorch.

---

## 📈 Supported Strategies
The bot offers over 30 distinct strategies, categorized by market regime:

- **Trend Following**: `ichimoku_cloud`, `parabolic_sar`, `vwap_momentum`, `renko_proxy`, `ema_rsi_volume`, `mc_momentum`, `adx_trend_strength`, `halving_cycle_proxy`, `tema_crossover`, `heikin_ashi`, `donchian_channels`.
- **Mean Reversion**: `bollinger_bands`, `stochastic_rsi`, `williams_r`, `mc_mean_reversion`, `mc_market_making`, `pairs_trading_proxy`, `sinewave_cycle`.
- **Specialized Proxies & Others**: `mc_dynamic_allocation`, `mc_stop_loss_eval`, `mc_options_pricing`, `whale_detection_proxy`, `pump_dump_proxy`, `scientific_ensemble`, `sentiment_momentum_proxy`, `liquidation_cascade_proxy`, `listing_surge_proxy`, `candle_patterns`.

---

## ⚙️ Configuration

### 🛠 `config.json`
Main parameters of the bot.

*   **`max_lots_per_symbol`**: (int) Maximum number of buy lots allowed per symbol (default: `1`).
*   **`max_open_positions`**: (int) Maximum number of distinct trading pairs open simultaneously (default: `10`).
*   **`max_trade_percentage`**: (float | object) Maximum percentage of your total balance to expose per symbol (total of all lots) (default: `10.0`).
    ```json
    "max_trade_percentage": {
        "BTC": 5.0,
        "USDT": 12.0,
        "default": 10.0
    }
    ```
*   **`global_risk_multiplier`**: (float) Multiplier for position sizing and technical confirmations (default: `1.1`).
*   **`dynamic_pair_multiplier`**: (float) Multiplier applied specifically to 1-second timeframe pairs (default: `2.0`).
*   **`max_analysis_workers`**: (int) Number of parallel workers for technical analysis (default: `4`).
*   **`no_signal_threshold`**: (int) Number of candles without signals before triggering background optimization (default: `48`).

---

### 📄 `pairs.txt`
Define the trading pairs you want the bot to monitor (one per line).
Example:
```text
BTC/USDC
ETH/USDC
SOL/USDC
```

### 🔑 `api.json`
Store your API credentials and preferred exchange.
```json
{
  "api_key": "YOUR_KEY",
  "api_secret": "YOUR_SECRET",
  "exchange_id": "binance"
}
```

---

## 🚀 Quick Start

### Installation

**Linux/macOS:**
1. Create a virtual environment and activate it: `python -m venv venv && source venv/bin/activate`

**Windows:**
1. Create a virtual environment: `python -m venv venv`, then activate it: `.\venv\Scripts\Activate.ps1`

2. Install dependencies: `pip install --upgrade -r requirements.txt`

*Note: For Windows, you might need to run `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`, use Python **3.13**, and install **Visual C++ 2015-2022 Redistributable (x64)**.*

### Execution
- **Live**: `python bot2.py`
- **Options**:
    - `--no-gpu`: Force CPU execution.
    - `--fast-start`: Skip initial candle fetching for faster startup.

---

## ⚖️ Disclaimer
Trading involves significant risk. Use this bot at your own risk. Licensed under **GPL**.

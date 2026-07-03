# 🛸 CCXT Pro Trading Bot

A universal cryptocurrency trading bot implemented in Python, leveraging multi-core processing, GPU acceleration, and evidence-based strategies. It supports **any exchange** provided by the CCXT library (Binance, Kraken, OKX, Coinbase, etc.).

---

## 🔬 Scientific Foundations
This bot implements strategies and logic recommended by leading empirical studies on cryptocurrency markets:

- **Monte Carlo Validation**: Vectorized simulations to estimate the probability of success for each signal, penalizing high-risk configurations.

---

## 🛠 Main Features

### 🛡 Risk Management
- **Intelligent Suspension**: Automatically suspends trading for symbols where orders fail or if the budget is insufficient. Resumes only when 1.2x the required budget becomes available.
- **Dynamic Sizing**: Position sizes are calculated as a percentage of your available balance, divided by the maximum number of lots allowed to maintain controlled exposure.

---

### ⚡ Performance
- **GPU Acceleration**: Calculations are offloaded to the graphics chip via PyTorch. Supported backends: **CUDA**, **MPS**, **Vulkan**, **oneDNN**, **IPEX**, and **ROCm**.
- **Hardware SIMD Optimization**: Automatic detection and utilization of CPU instruction sets (**MMX**, **SSE**, **AVX**, **AVX2**, **AVX512**) for optimized performance via PyTorch.

---

## 📈 Supported Strategies
The bot offers over 30 distinct strategies, including:

- **Trend Following**: `ichimoku_cloud`, `parabolic_sar`, `adx_trend_strength`, `halving_cycle_proxy`.
- **Mean Reversion & Range**: `bollinger_bands`, `rsi_support_resistance`, `pairs_trading_proxy`.
- **Breakout & Momentum**: `breakout_volume`, `donchian_channels`, `atr_breakout`, `stochastic_rsi`, `williams_r`, `vwap_momentum`.
- **Scalping & Order Flow**: `order_flow_proxy`, `renko_proxy`, `tick_proxy`, `ema_rsi_volume`.
- **Advanced Proxies**: `scientific_ensemble`, `whale_detection_proxy`, `pump_dump_proxy`, `market_regime_proxy`, `sentiment_momentum_proxy`, `liquidation_cascade_proxy`.
- **Monte Carlo Engines**: `mc_mean_reversion`, `mc_momentum`, `mc_dynamic_allocation`, `mc_market_making`, `mc_stop_loss_eval`.

---

## ⚙️ Configuration

### 🛠 `config.json`
Main parameters of the bot. To personalize only certain parameters, you must copy the `config.default.json` file to `config.json` and then modify your settings in the latter.

*   **`quote_asset`**: (string) A quote asset you define (default: `USDC`). The bot automatically fetch the most dynamic symbols with a base asset associated with it.
*   **`max_lots_per_symbol`**: (int) Maximum number of buy lots allowed per symbol (default: `1`).
*   **`max_open_positions`**: (int) Maximum number of distinct trading pairs open simultaneously (default: `10`).
*   **`max_trade_percentage`**: (float) Maximum percentage of your total balance to expose per symbol (total of all lots) (default: `2`).
*   **Per-Base-Asset Configuration**: You can define different maximums for different base currencies:
    ```json
    "max_trade_percentage": {
        "BTC": 5.0,
        "USDT": 12.0,
        "USDC": 10.0,
        "default": 12.0
    }
    ```
*   **`global_risk_multiplier`**: (float) Multiplier for position sizing and technical confirmations (default: `1.1`).
*   **`dynamic_pair_multiplier`**: (float) Multiplier applied specifically to 1-second timeframe pairs (default: `1.4`).
*   **`no_signal_threshold`**: (int) Number of candles without signals.

#### Advanced Overrides (Optional)
*   **`pairs`**: (Object) Allows per-pair configuration.
    Example:
    ```json
    "pairs": {
        "BTC/USDC": {
            "strategy": "ichimoku_cloud",
            "aggr": "aggressive"
        }
    }
    ```

---

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

---

## 🚀 Quick Start

### Installation

**Linux/macOS:**
1. Create a virtual environment and activate it: `python -m venv venv && source venv/bin/activate`

**Windows:**
1. Create a virtual environment: `python -m venv venv`, then activate it: `.\venv\Scripts\Activate.ps1`

2. Install dependencies: `pip install --upgrade -r requirements.txt`

*Note: For Windows, you will need to run the command `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser` for security, use Python version **3.13**, and install the **Visual C++ 2015-2022 Redistributable (x64)** which you can find here [https://aka.ms/vs/17/release/vc_redist.x64.exe] due to platform-specific dependencies.*

**Regular Maintenance:**

To stay up to date with API call changes: `pip install --upgrade ccxt` or `pip install --upgrade -r requirements.txt` to run the full dependency update procedure. Also, ensure your computer's **clock** is synchronized.

### Execution
- **Live**: `python bot2.py`
- **Options**:
    - `--no-gpu`: Force CPU execution.
    - `--fast-start`: Skip initial candle fetching for faster startup.

---

## ⚖️ Disclaimer
Trading involves significant risk. Use this bot at your own risk. Licensed under **GPL**.

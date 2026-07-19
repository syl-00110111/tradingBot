# 🛸 CCXT Pro Trading Bot

A universal cryptocurrency trading bot implemented in Python, leveraging multi-core processing, GPU acceleration, and evidence-based strategies. It supports theoretically **any exchange** provided by the CCXT library (Binance, Kraken, OKX, Coinbase, etc.) but it's been tested only on Kraken.

---

## 🔬 Scientific Foundations
This bot implements strategies and logic recommended by leading empirical studies on cryptocurrency markets.

---

## 🛠 Main Features

### 🛡 Risk Management
- **Intelligent Suspension**: Automatically suspends trading for symbols where orders fail or if the budget is insufficient. Resumes only when the required budget becomes available.
- **Dynamic Sizing**: Position sizes are calculated as a percentage of your available balance.

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

### 🔑 `api.json`
Store your API credentials and preferred exchange.
```json
{
  "api_key": "YOUR_KEY",
  "api_secret": "YOUR_SECRET",
  "exchange_id": "kraken"
  "options": {
        "defaultType": "spot"
    }
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
- **Live**: `python botv4.py`

---

## ⚖️ Disclaimer
Trading involves significant risk. Use this bot at your own risk. Licensed under **GPL**.

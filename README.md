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
- **GPU Acceleration**: Calculations are offloaded to the graphics chip via PyTorch. Supported backends: **CUDA**, **MPS**, **Vulkan**, and **oneDNN** (Intel).
- **Multi-Processing Benchmark**: Strategy optimization is parallelized across all CPU cores.
- **Fresh Ticker Price**: Fetches a fresh price from the exchange immediately before placing a Buy order to ensure compliance with Spot market NOTIONAL limits and reduce "Filter failure" errors.
- **API Synchronization**: Live mode exclusively uses exchange API data for balances and positions.

### 🛡 Risk Management
- **Confirmation Logic**: Requires consecutive identical signals dynamically adjusted by term duration:
  - **Short Term (1h)**: 3 signals
  - **Medium Term (1d)**: 2 signals
  - **Long Term (1w)**: 1 signal
- **Secure Sell Toggle**: When enabled, ensures trades are only closed at a profit.
- **Dynamic Position Sizing**: Position sizes are calculated as a **percentage** of your available base currency (e.g. 10.0 = 10%).

---

## 📈 Supported Strategies
The bot features 30+ distinct trading strategies, including:
`moving_averages`, `ichimoku_cloud`, `parabolic_sar`, `rsi_support_resistance`, `bollinger_bands`, `macd_range`, `breakout_volume`, `donchian_channels`, `atr_breakout`, `stochastic_rsi`, `williams_r`, `vwap_momentum`, `order_flow_proxy`, `renko_proxy`, `tick_proxy`, `ema_rsi_volume`, `macd_bollinger_bands`, `double_ema`, `double_ema_macd_rsi`, `scientific_ensemble`, and various Monte Carlo based approaches.

---

## ⚙️ Configuration

### `pairs.txt`
Trading pairs are now defined in a simple `pairs.txt` file (one per line, e.g., `BTC/EUR`). Base currencies are automatically identified from this list.

### `api.json`
Store your credentials and preferred exchange:
```json
{
  "api_key": "YOUR_KEY",
  "api_secret": "YOUR_SECRET",
  "exchange": "binance"
}
```
*Options: `binance`, `kraken`, `bitvavo`.*

### `config.json`
```json
{
    "max_open_positions": 8,
    "base_trade_amount": 10.0,
    "secure_sell": false,
    "global_risk_multiplier": 1.0
}
```

## Performance & Reliability (GPU)
This bot is designed for high-performance trading. It leverages **GPU acceleration** via PyTorch for:
- Technical indicator calculations (EMA, MACD, RSI, ADX)
- Monte Carlo simulations
- Success Pattern Matching (Pearson Correlation)

Supported backends include **CUDA** (NVIDIA), **MPS** (Apple Silicon), and **oneDNN** (Intel Optimized CPU). Maybe some **Vulkan** support sonn.

**Crucial:** Ensure you keep your dependencies up to date regularly, especially CCXT, in order to maintain API compatibility:
```bash
pip install --upgrade ccxt
```

## Data Persistence
The bot maintains a consolidated archive `bot_data_backup.zip` containing all runtime state (trades, patterns, cache). This acts as a database and provides a safety net against accidental file deletion.

---

## 🚀 Setup

1. **Install PyTorch & CCXT**:
   ```bash
   pip install torch ccxt pandas pandas-ta rich readchar matplotlib
   pip install --upgrade ccxt
   ```
2. **Setup**: Create `api.json` and `pairs.txt`.
3. **Run**: `python bot.py --mode simulation --exchange binance`

---

## 📜 Data Persistence
The bot maintains a consolidated archive `bot_data_backup.zip`. Runtime JSON/Pickle files are flushed into this archive and deleted from the disk to prevent accidental data loss. The bot restores its state from this archive at startup.

---

## ⚖️ Disclaimer
Trading carries significant risk. Use at your own risk. Licensed under **GPL**.

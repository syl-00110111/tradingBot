SOON RELEASE BUT NOT TOTALLY FUNCTIONNAL STAY TUNED
# 🛸 Cryptocurrencies Trading Bot: Advanced Quantitative & Scientific Suite

An industrial-grade trading bot implemented in Python, leveraging multi-core processing, real-time market data, and evidence-based strategies derived from top cryptocurrency financial literature. While currently optimized for Binance via the CCXT library, the architecture is designed to support multiple exchanges in the future. Please note that the focus remains primarily on cryptocurrencies.

---

## 🔬 Scientific Foundations
This bot implements strategies and logic recommended by leading empirical studies in the cryptocurrency markets:

- **Success Pattern Matching (SPM)**: The bot scans up to 5000 historical candles backwards to identify the top 4 success patterns that would have yielded significant profit. It then uses normalized shape correlation and technical state similarity (RSI/ADX) to activate trading only when current market conditions match these proven windows.
- **BTC Strategy (MACD/RSI)**: As identified by *Urquhart (2016)* and *Zhang et al. (2020)*, MACD and RSI provide the most reliable signals for Bitcoin's price action.
- **ETH Strategy (Stochastic RSI)**: Optimized for Ethereum's volatility, following the findings of *Zhang et al. (2020)*.
- **Market Regime Detection**: Utilizes volatility-based switching between Mean-Reversion (Bollinger Bands) and Trend-Following (EMA), a methodology supported by *Baur & Dimpfl (2021)*.
- **Whale Activity & Pump Detection**: Implementation of volume-divergence proxies for on-chain metrics and market manipulation detection, based on *Bartoletti et al. (2017)* and *Kamps & Kleinberg (2018)*.
- **Monte Carlo Validation**: Vectorized simulations to estimate the probability of success for every signal, penalizing high-risk/low-probability setups.

---

## 🛠 Core Features

### ⚡ Performance & Reliability
- **Multi-Processing Benchmark**: Strategy optimization is parallelized across all available CPU cores using `ProcessPoolExecutor`.
- **Multi-Threaded Analysis**: Real-time market analysis of multiple pairs simultaneously via `ThreadPoolExecutor`.
- **API Synchronization**: Live mode exclusively uses exchange API data for balances and positions, ensuring zero reliance on potentially stale local caches.
- **Real-Time Fees**: Dynamic fee estimation using CCXT's `fetch_trading_fee` for high financial accuracy.

### 🛡 Risk Management
- **Confirmation Logic**: Requires consecutive identical signals (dynamically adjusted by term, e.g., 3 for short-term) within a confirmation window to filter out noise.
- **Secure Sell Toggle**: When enabled, ensures trades are only closed at a profit.
- **2nd Signal Stop-Loss**: If `secure_sell` is enabled, unprofitable positions are only liquidated after a 2nd distinct sell signal, providing a safety net while avoiding premature exits.
- **Dynamic Position Sizing**: Governed by a fixed `base_trade_amount` with optional `win_streak_bonus` multipliers and a global risk engine that adjusts parameters based on ADX and Volatility.

### 📊 Real-Time Dashboard
- **Interactive TUI**: Built with `Rich`, featuring marquee scrolling, "Expert Mode" for technical indicators, and real-time status bars.
- **Feedback**: Clear visual and audio feedback for key events.

---

## 📈 Supported Strategies
The bot features 19+ distinct trading strategies, including:
`moving_averages`, `ichimoku_cloud`, `parabolic_sar`, `rsi_support_resistance`, `bollinger_bands`, `macd_range`, `breakout_volume`, `donchian_channels`, `atr_breakout`, `stochastic_rsi`, `williams_r`, `vwap_momentum`, `order_flow_proxy`, `renko_proxy`, `tick_proxy`, `ema_rsi_volume`, `macd_bollinger_bands`, `double_ema`, `double_ema_macd_rsi`, `scientific_ensemble`, and various Monte Carlo based approaches.

---

## ⚙️ Configuration (`config.json`)

The bot prioritizes `config.json` and falls back to `config.default.json`. Users are encouraged to copy `config.default.json` to `config.json` as a starting point.

```json
{
    "base_currency": "EUR",
    "max_open_positions": 8,
    "base_trade_amount": 20.0,
    "secure_sell": false,
    "expected_profit_terms": {
        "short": { "duration_hours": 1, "timeframe": "1m", "eval_candles": 60 }
    },
    "win_streak_bonus": {
        "enabled": true,
        "threshold": 2,
        "multiplier": 1.2
    },
    "pairs": {
        "BTC/EUR": {},
        "ETH/EUR": {}
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
*Note: On Windows, you may need to use **Python 3.13** and install the **Visual C++ 2015-2022 Redistributable (x64)** available at [https://aka.ms/vs/17/release/vc_redist.x64.exe](https://aka.ms/vs/17/release/vc_redist.x64.exe) due to specific dependency requirements.*

### Setup Credentials
Copy `api.json.example` to `api.json` and enter your `api_key` and `api_secret`.

### Execution Modes
- **Simulation**: `python bot.py --mode simulation --term short`
- **Live**: `python bot.py --mode live --term medium`
- **Benchmark**: `python bot.py --mode benchmark --every-symbol`
- **Backtest**: `python bot.py --mode backtest --symbol BTC/EUR --strategy moving_averages`
- **Balance**: `python bot.py --mode balance`

---

## 📜 Disclaimer & License
This software is for educational and research purposes only. Trading cryptocurrencies carries significant risk. Use at your own risk.

Licensed under the **GNU General Public License (GPL)**.

---

## 🤝 Contributing
Contributors are welcome! Feel free to submit pull requests or report issues.

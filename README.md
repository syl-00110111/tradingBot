# 🛸 Cryptocurrencies Multiplatform Trading Bot (`botv6` & `botv5`)

A high-performance universal cryptocurrency trading bot written in Rust featuring multi-strategy aggregation (`botv6`), dual-core / multi-core parallel processing (`Rayon`), vector math acceleration (`ndarray`), async I/O (`Tokio`), custom DSL strategy rule parsing, and evidence-based trading strategy models with Monte Carlo probability engines.

---

## ⚙️ Rust Installation Instructions

Before building and running `botv6` or `botv5`, install the Rust compiler (`rustc`) and package manager (`cargo`).

### 1. Installing Rust

#### **Linux & macOS**
Open a terminal and run:
```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```
Follow the on-screen prompts (pressing `1` for standard installation). Once complete, reload your shell environment:
```bash
source "$HOME/.cargo/env"
```

#### **Windows**
1. Download `rustup-init.exe` from the official site: [https://rustup.rs/](https://rustup.rs/)
2. Run `rustup-init.exe` and follow the prompts.
3. Ensure you have the **Visual Studio C++ Build Tools** installed if prompted by the installer.

---

### 2. Verifying Installation
Verify that `rustc` and `cargo` are correctly installed and available in your PATH:
```bash
rustc --version
cargo --version
```

---

### 3. Updating Rust
To update your Rust installation to the latest stable version at any time:
```bash
rustup update
```

---

## 🔑 `api.json` Placement Instructions (Live Mode Credentials)

To trade in **Live Mode** with real exchange API keys, place your `api.json` credentials file in the working directory of the bot version you are running:

### **`botv6` Location**
```bash
# Path relative to repository root:
botv6/api.json
```

### **`botv5` Location**
```bash
# Path relative to repository root:
botv5/api.json
```

### **`api.json` Format**
```json
{
  "api_key": "YOUR_KRAKEN_API_KEY",
  "api_secret": "YOUR_KRAKEN_API_SECRET",
  "exchange_id": "kraken"
}
```

---

## 📁 Bot Data Files & Storage Locations

The bot automatically persists runtime state and trading history to JSON data files located in the working directory (`botv6` or `botv5`). Live mode and Simulation mode use strictly isolated filenames to prevent state pollution.

### **Live Mode Data Files**
- `redlisted_pairs.json`: Stores pairs suspended due to high transaction costs or API errors.
- `paused_for_buy.json`: Stores buy-paused pairs with expiration timestamps.
- `recorded_purchases.json`: Stores recorded buy prices and quantities for profitability checks (`is_sell_profitable`).
- `pending_orders_dump.json`: Dumps active/placed limit orders.
- `unscored_pairs.json`: Stores unscored / non-optimal pairs evaluated under reduced margin thresholds.
- `markets.json`: Cached market definitions and metadata (`id`, `symbol`, `precision`, `limits`).
- `balance.json`: Cached account balance payload.
- `volumes_trades_data.json`: Cached pair volume, spread, volatility, and trading density metrics.

### **Simulation Mode Data Files (Isolated)**
- `sim_redlisted_pairs.json`
- `sim_paused_for_buy.json`
- `sim_recorded_purchases.json`
- `sim_pending_orders_dump.json`
- `sim_unscored_pairs.json`

> **Note**: Legacy Python code (`botv4.py` and helper modules) is archived in the `botv4/` directory.

---

## 🛑 Clean Exit / Graceful Shutdown

To trigger a clean and graceful shutdown at any time:
- Press `Ctrl + C` in your terminal window.
- The engine catches the `SIGINT` signal, immediately persists all active position tracking (`recorded_purchases.json`) and paused buy states (`paused_for_buy.json`) to disk, and exits safely.

---

## 🛠 Main Features (`botv6`)

### ⚡ Modular Strategy Aggregation & Performance
- **Multi-Strategy Aggregation Engine**: Configurable signal voting system supporting `weighted_score`, `consensus`, and `majority` decision modes.
- **Custom DSL Rule Evaluator**: Dynamic custom rule parsing allowing user-defined indicator conditions without code recompilation.
- **Multi-Core & Dual-Core Parallel Processing**: Analyzes candidate pairs concurrently using `Rayon` thread pools across CPU cores.
- **Async I/O Engine**: Built on `Tokio` for low-latency REST API connectivity with CCXT/Kraken-compatible exchanges.
- **Monte Carlo Engine**: Multi-threaded strategy hit probability estimation (`botv6::monte_carlo`).
- **OHLC Non-Repetition Window Calibration**: Dynamically calibrates candle history window sizes based on non-repetitive active candles.

### 🛡 Risk Management & Sizing
- **Crest High & 5-Week SMA Protection**: Bypasses crest high buys when market prices exceed 5-week SMA (`SMA_840`) on low-peak windows (<= 3 peaks in 840 candles).
- **Position & Sizing Bounds**: Enforces maximum buyings (4 per base asset), trade package sizing (5.07 EUR minimum to 12.23 EUR maximum), and automatic redlisting for high minimum cost pairs without held inventory.
- **Simulation Isolation**: Paper trading and backtesting run with isolated state files (`sim_*`).

---

## 📊 Strategy Configuration Section (`botv6`)

`botv6` introduces a highly flexible, file-driven strategy system configured via `config.default.json` and optional user overrides in `config.json`.

### 1. Configuration Merging & Diff Logging
At startup, `botv6` loads `config.default.json` and merges custom settings from `config.json`. Any overridden values are logged automatically:
```text
[Config Diff] strategies.ichimoku.weight: default = 1, loaded = 1.5
```

---

### 2. Strategy Aggregation Engine (`strategy_aggregation`)

The strategy aggregator combines signals across all enabled strategies to determine final `Buy`, `Sell`, or `Hold` decisions.

#### **Configuration Schema**
```json
"strategy_aggregation": {
    "mode": "weighted_score",
    "min_buy_score": 1.0,
    "min_sell_score": 1.0,
    "consensus_threshold": 0.6
}
```

#### **Aggregation Modes**
- **`weighted_score`** (Default): Sums the weights of strategies signalling `Buy` and `Sell`.
  - Triggers `Buy` if `buy_score >= min_buy_score` and `buy_score > sell_score`.
  - Triggers `Sell` if `sell_score >= min_sell_score` and `sell_score > buy_score`.
- **`consensus`**: Evaluates the ratio of active signal weight relative to total enabled strategy weight.
  - Triggers `Buy` if `buy_score / total_enabled_weight >= consensus_threshold` and `buy_score > sell_score`.
  - Triggers `Sell` if `sell_score / total_enabled_weight >= consensus_threshold` and `sell_score > buy_score`.
- **`majority`**: Simple unweighted count comparison of strategies issuing buy vs. sell signals.

---

### 3. Built-In Strategy Catalog & Parameters (28 Models)

Each strategy entry in the `strategies` object supports `enabled` (boolean), `weight` (float multiplier), and strategy-specific `params`.

```json
"strategies": {
    "ichimoku": {
        "enabled": true,
        "weight": 1.0,
        "params": { "tenkan": 9, "kijun": 26, "senkou": 52 }
    }
}
```

#### **Standard Technical Strategies (22 Models)**

| Strategy Name | Description | Default Parameters |
| :--- | :--- | :--- |
| `ichimoku` | Ichimoku Kinko Hyo cloud trend crossover | `tenkan`: 9, `kijun`: 26, `senkou`: 52 |
| `psar` | Parabolic SAR trend reversal | `af`: 0.02, `max_af`: 0.2 |
| `bollinger` | Bollinger Bands mean reversion with RSI oversold check | `length`: 20, `std`: 2.0, `rsi_oversold`: 35.0 |
| `donchian` | Donchian Channel upper/lower breakout | `length`: 20 |
| `stoch_rsi` | Stochastic RSI overbought/oversold levels | `length`: 14, `rsi_length`: 14, `k`: 3, `d`: 3, `oversold`: 20, `overbought`: 80 |
| `williams_r` | Williams %R momentum indicator | `length`: 14, `oversold`: -80.0, `overbought`: -20.0 |
| `vwap_momentum` | Volume-Weighted Average Price momentum proxy | `{}` |
| `renko` | Renko brick synthetic trend proxy via ATR | `atr_length`: 14 |
| `ema_rsi_volume` | Multi-indicator alignment combining EMA crossover, RSI, and Volume MA | `ema_fast`: 9, `ema_slow`: 21, `rsi_length`: 14, `vol_ma`: 20 |
| `whale_detection` | Standard deviation volume spike whale order tracker | `length`: 20, `std_devs`: 3.0 |
| `pump_dump` | High-velocity price and volume surge detector | `vol_surge`: 1.5, `price_surge`: 0.001 |
| `scientific_ensemble` | Multi-indicator scoring ensemble (RSI, MACD, Bollinger Bands) | `rsi_oversold`: 35.0, `rsi_overbought`: 65.0 |
| `sentiment_momentum` | Rate of Change (ROC) and RSI sentiment momentum filter | `roc_length`: 10, `rsi_limit`: 60.0, `rsi_floor`: 40.0 |
| `liquidation_cascade` | Price cascade and volume multiplier spike detector | `pct_trigger`: 0.001, `vol_multiplier`: 1.5 |
| `adx_trend` | ADX trend strength filter with SMA trend confirmation | `threshold`: 25.0, `sma_length`: 20 |
| `pairs_trading` | Z-score price deviation mean reversion proxy | `ma_length`: 50, `z_threshold`: 0.02 |
| `halving_cycle` | Long-term macro EMA cycle trend proxy | `ema_long`: 200 |
| `listing_surge` | High volume anomaly / new listing surge tracker | `ma_length`: 20, `vol_multiplier`: 5.0 |
| `tema_crossover` | Triple Exponential Moving Average crossover proxy | `ema_length`: 9 |
| `heikin_ashi` | Heikin Ashi synthetic candle trend strategy | `{}` |
| `sinewave` | SMA-based cyclical sinewave trend approximation | `sma_length`: 7 |
| `candle_patterns` | Candlestick pattern recognition (Engulfing patterns) | `{}` |

---

#### **Monte Carlo Probability Strategies (6 Models)**

| Strategy Name | Description | Default Parameters |
| :--- | :--- | :--- |
| `mc_mean_reversion` | Simulated Geometric Brownian Motion path mean-reversion probability | `threshold`: 0.55, `sma_length`: 20 |
| `mc_momentum` | Forward path profit target hit probability engine | `threshold`: 0.55, `target_profit`: 0.01 |
| `mc_dynamic_allocation` | Historical simulation strategy confidence score evaluator | `high_threshold`: 0.8, `low_threshold`: 0.6 |
| `mc_market_making` | Bid/ask path hit probability estimation | `threshold`: 0.6 |
| `mc_stop_loss_eval` | Stop-loss breach risk estimation engine | `threshold`: 0.12, `sl_pct`: 0.05 |
| `mc_options_pricing` | Option payoff probability call/put ratio evaluator | `ratio`: 1.5 |

---

### 4. Custom DSL Rule Engine (`custom_strategies`)

`botv6` includes a Domain-Specific Language (DSL) evaluator (`CustomRuleEvaluator`) allowing users to define custom strategy conditions in `config.json` without recompiling Rust code.

#### **Configuration Schema**
```json
"custom_strategies": [
    {
        "name": "custom_rsi_sma_reversion",
        "enabled": true,
        "weight": 1.5,
        "buy_condition": "rsi(14) < 30.0 AND close > sma(50)",
        "sell_condition": "rsi(14) > 70.0 OR close < sma(50)"
    }
]
```

#### **Supported DSL Syntax & Components**
- **Candle Attributes**: `close`, `open`, `high`, `low`, `volume`.
- **Technical Indicators**:
  - `rsi(period)` (e.g. `rsi(14)`)
  - `sma(period)` (e.g. `sma(50)`)
  - `ema(period)` (e.g. `ema(20)`)
  - `adx(period)` (e.g. `adx(14)`)
- **Comparison Operators**: `<=`, `>=`, `==`, `<`, `>`.
- **Logical Operators**: `AND`, `OR`.

---

## 🚀 Quick Start & Building

### Build `botv6`
Navigate to the `botv6` directory and compile:
```bash
cd botv6
cargo build --release
```

### Execution Modes (`botv6`)

- **Live Trading**:
  ```bash
  cargo run -- --mode live
  ```

- **Simulation / Paper Trading Mode**:
  ```bash
  cargo run -- --mode simulation
  ```

- **Backtest Mode**:
  Runs historical backtesting simulations across calibrated candle windows:
  ```bash
  cargo run -- --mode backtest
  ```

> For `botv5`, navigate to `botv5/` and execute standard cargo commands (`cargo run -- --mode live`).

---

## ⚖️ Disclaimer
Trading involves significant risk. Licensed under **GPL**.

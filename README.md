# 🛸 Cryptocurrencies Multiplatform Trading Bot (Rust `botv5`)

A high-performance universal cryptocurrency trading bot written in Rust (`botv5`), featuring dual-core / multi-core parallel processing (`Rayon`), vector math compute acceleration (`ndarray`), async I/O (`Tokio`), and evidence-based trading strategy models with Monte Carlo probability engines.

---

## ⚙️ Rust Installation Instructions

Before building and running `botv5`, you need to install the Rust compiler (`rustc`) and package manager (`cargo`).

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

To trade in **Live Mode** with real exchange API keys, place your `api.json` credentials file directly in the `botv5` working directory:

### **Linux / macOS Location**
```bash
# Path relative to repository root:
/path/to/tradingBot/botv5/api.json
```

### **Windows Location**
```powershell
# Path relative to repository root:
C:\path\to\tradingBot\botv5\api.json
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

The bot automatically persists runtime state and trading history to JSON data files located in the `botv5` working directory. Live mode and Simulation mode use strictly isolated filenames to prevent state pollution.

### **Live Mode Data Files**
- `redlisted_pairs.json`: Stores pairs suspended due to high transaction costs or API errors.
- `paused_for_buy.json`: Stores buy-paused pairs with expiration timestamps.
- `recorded_purchases.json`: Stores recorded buy prices and quantities for profitability checks (`is_sell_profitable`).
- `pending_orders_dump.json`: Dumps active/placed limit orders.
- `markets.json`: Cached market definitions and metadata (`id`, `symbol`, `precision`, `limits`).
- `balance.json`: Cached account balance payload.
- `volumes_trades_data.json`: Cached pair volume, spread, volatility, and trading density metrics.

### **Simulation Mode Data Files (Isolated)**
- `sim_redlisted_pairs.json`
- `sim_paused_for_buy.json`
- `sim_recorded_purchases.json`
- `sim_pending_orders_dump.json`

> **Note**: Legacy Python code (`botv4.py` and helper modules) has been backed up into the `botv4/` directory.

---

## 🛑 Clean Exit / Graceful Shutdown

To trigger a clean and graceful shutdown at any time:
- Press `Ctrl + C` in your terminal window.
- The engine catches the `SIGINT` signal, immediately persists all active position tracking (`recorded_purchases.json`) and paused buy states (`paused_for_buy.json`) to disk, and exits safely.

---

## 🛠 Main Features

### ⚡ Performance & Acceleration
- **Multi-Core & Dual-Core Parallel Processing**: Uses `Rayon` thread pools to analyze multiple trading symbols concurrently across all CPU cores.
- **Async I/O Engine**: Built on `Tokio` for low-latency REST API connectivity with CCXT/Kraken-compatible exchanges.
- **Monte Carlo Probability Engine**: Multi-threaded strategy hit probability estimation (`botv5::monte_carlo`).
- **OHLC Non-Repetition Window Calibration**: Dynamically calibrates candle history window sizes based on non-repetitive active candles (checking Open, High, Low, Close relative differences against `epsilon = 1e-5`).

### 🛡 Risk Management & Advanced Execution Features
- **Deprecated Order Editing & Cancellation**: `cleanup_open_orders` checks crest high conditions (against 5-week SMA `SMA_840` when history contains <= 3 peaks in 840 candles), evaluates Monte Carlo hit probabilities, edits open orders when prices/amounts change, or cancels orders with insufficient hit probability (< 0.96).
- **Hit Probability & Multi-Quote Profitability Checks**: Enforces Monte Carlo hit probability thresholds (> 0.96) before order placement and checks cross-quote weighted average purchase prices (with a 0.3% profit margin) before executing sells.
- **Simultaneous Signal & Wind-Choice Prioritization**: Prioritizes simultaneous BUY/SELL signals using hit probabilities and applies "Wind-Choice" quote asset prioritization to pass on buys if a higher quote balance exists for the base asset in another pair.
- **Max Buyings & Sizing Limits**: Caps maximum positions to 4 per base asset, enforces package sizing bounds between 5.07 EUR minimum and 12.23 EUR maximum per trade, and redlists pairs exceeding 12.23 EUR cost unless base balance is held.
- **Write-Once Centralized Sub-Actions**: Redlisting pairs, pausing buys on error, recording purchases, and dumping pending orders are written once and shared across all pipelines.
- **Simulation Mode Isolation**: Run paper trading simulation or backtesting with strictly isolated state files (`sim_redlisted_pairs.json`, `sim_paused_for_buy.json`, `sim_recorded_purchases.json`) so live runs are never polluted!

---

## 📈 Supported Strategy Catalog (30+ Models)

The Rust trading engine supports over 30 distinct trading strategy models categorized into specialized groups:

- **Trend Following**: `ichimoku_cloud`, `parabolic_sar`, `adx_trend_strength`, `halving_cycle_proxy`, `tema_crossover`, `heikin_ashi`.
- **Mean Reversion & Range**: `bollinger_bands`, `pairs_trading_proxy`.
- **Breakout & Momentum**: `donchian_channels`, `stochastic_rsi`, `williams_r`, `vwap_momentum`, `sinewave_cycle`, `candle_patterns`.
- **Scalping & Order Flow Proxies**: `renko_proxy`, `ema_rsi_volume`.
- **Advanced Proxies**: `scientific_ensemble`, `whale_detection_proxy`, `pump_dump_proxy`, `sentiment_momentum_proxy`, `liquidation_cascade_proxy`, `listing_surge_proxy`.
- **Monte Carlo Engines**: `mc_mean_reversion`, `mc_momentum`, `mc_dynamic_allocation`, `mc_market_making`, `mc_stop_loss_eval`, `mc_options_pricing`.

---

## 🚀 Quick Start & Building

### Build
Navigate to the `botv5` crate directory and compile:
```bash
cd botv5
cargo build --release
```

### Execution Modes

- **Live Trading**:
  ```bash
  cargo run -- --mode live
  ```

- **Simulation / Paper Trading Mode**:
  ```bash
  cargo run -- --mode simulation
  ```

- **Backtest Mode**:
  Runs historical backtesting simulations across calibrated non-repetition candle windows, evaluating strategy aggregation signals, Monte Carlo probabilities, signal prioritization, and tracking balance, win rate, profit factor, and maximum drawdown:
  ```bash
  cargo run -- --mode backtest
  ```

---

## ⚖️ Disclaimer
Trading involves significant risk. Licensed under **GPL**.

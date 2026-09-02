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

### **Simulation Mode Data Files (Isolated)**
- `sim_redlisted_pairs.json`
- `sim_paused_for_buy.json`
- `sim_recorded_purchases.json`
- `sim_pending_orders_dump.json`

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

### 🛡 Risk Management & Sub-Actions Architecture
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
  ```bash
  cargo run -- --mode backtest
  ```

---

## ⚖️ Disclaimer
Trading involves significant risk. Licensed under **GPL**.

# 🛸 Cryptocurrencies Multiplatform Trading Bot (Rust `botv5`)

A high-performance universal cryptocurrency trading bot written in Rust (`botv5`), featuring dual-core / multi-core parallel processing (`Rayon`), vector math compute acceleration (`ndarray`), async I/O (`Tokio`), and evidence-based trading strategy models with Monte Carlo probability engines.

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

## 🚀 Quick Start & Installation

### Prerequisites
- Install **Rust** toolchain (1.75+ recommended):
  ```bash
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
  ```

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

## 🔑 Configuration (`api.json` & `config.json`)
The bot loads `config.default.json`, merges overrides from `config.json`, and injects exchange API keys from `api.json`:

```json
{
  "api_key": "YOUR_KEY",
  "api_secret": "YOUR_SECRET",
  "exchange_id": "kraken"
}
```

---

## ⚖️ Disclaimer
Trading involves significant risk. Licensed under **GPL**.

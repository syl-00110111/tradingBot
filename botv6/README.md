# 🛸 Botv6 Crate Documentation

`botv6` is an advanced Rust cryptocurrency trading engine featuring modular multi-strategy aggregation, dynamic strategy weighting, Monte Carlo probability simulation, custom DSL strategy rule evaluation, and async multi-core parallel processing.

---

## ⚙️ Requirements & Quick Start

### 1. Build
From this directory (`botv6/`):
```bash
cargo build --release
```

### 2. Live Credentials Setup
Place your exchange credentials in `botv6/api.json`:
```json
{
  "api_key": "YOUR_KRAKEN_API_KEY",
  "api_secret": "YOUR_KRAKEN_API_SECRET",
  "exchange_id": "kraken"
}
```

### 3. Execution Modes
- **Live Trading**:
  ```bash
  cargo run -- --mode live
  ```
- **Simulation / Paper Trading**:
  ```bash
  cargo run -- --mode simulation
  ```
- **Historical Backtesting**:
  ```bash
  cargo run -- --mode backtest
  ```

---

## 📁 Runtime Data Files & Isolated Storage

Runtime state files are maintained in the working directory. Simulation mode uses isolated state files prefixed with `sim_` to ensure live state is never overwritten:

| Live Mode File | Simulation Mode File | Description |
| :--- | :--- | :--- |
| `redlisted_pairs.json` | `sim_redlisted_pairs.json` | High cost / failed pairs suspended from trading |
| `paused_for_buy.json` | `sim_paused_for_buy.json` | Buy-paused pairs with temporary timeouts |
| `recorded_purchases.json` | `sim_recorded_purchases.json` | Active position buy prices and quantities |
| `pending_orders_dump.json` | `sim_pending_orders_dump.json` | Tracked open limit orders |
| `unscored_pairs.json` | `sim_unscored_pairs.json` | Pairs evaluated with reduced margin thresholds |

Shared metadata caches:
- `markets.json`: Cached market definitions and limits.
- `balance.json`: Account balance snapshots.
- `volumes_trades_data.json`: Pair characteristics (volume, spread, volatility, tpm).

---

## ⚙️ Strategy Configuration Reference

`botv6` loads configuration from `config.default.json` and applies optional overrides from `config.json`.

### 1. Strategy Aggregation Engine (`strategy_aggregation`)
```json
"strategy_aggregation": {
    "mode": "weighted_score",
    "min_buy_score": 1.0,
    "min_sell_score": 1.0,
    "consensus_threshold": 0.6
}
```
- **`weighted_score`**: Weighted score sum of active signals against `min_buy_score` and `min_sell_score`.
- **`consensus`**: Weighted score ratio against total enabled strategy weight relative to `consensus_threshold`.
- **`majority`**: Simple unweighted count comparison of buy vs. sell signals.

---

### 2. Built-In Strategies & Parameters (28 Models)

Configurations are placed in the `strategies` object with `enabled`, `weight`, and `params`:

```json
"strategies": {
    "ichimoku": { "enabled": true, "weight": 1.0, "params": { "tenkan": 9, "kijun": 26, "senkou": 52 } },
    "psar": { "enabled": true, "weight": 1.0, "params": { "af": 0.02, "max_af": 0.2 } },
    "bollinger": { "enabled": true, "weight": 1.0, "params": { "length": 20, "std": 2.0, "rsi_oversold": 35.0 } },
    "donchian": { "enabled": true, "weight": 1.0, "params": { "length": 20 } },
    "stoch_rsi": { "enabled": true, "weight": 1.0, "params": { "length": 14, "rsi_length": 14, "k": 3, "d": 3, "oversold": 20, "overbought": 80 } },
    "williams_r": { "enabled": true, "weight": 1.0, "params": { "length": 14, "oversold": -80.0, "overbought": -20.0 } },
    "vwap_momentum": { "enabled": true, "weight": 1.0, "params": {} },
    "renko": { "enabled": true, "weight": 1.0, "params": { "atr_length": 14 } },
    "ema_rsi_volume": { "enabled": true, "weight": 1.0, "params": { "ema_fast": 9, "ema_slow": 21, "rsi_length": 14, "vol_ma": 20 } },
    "whale_detection": { "enabled": true, "weight": 1.0, "params": { "length": 20, "std_devs": 3.0 } },
    "pump_dump": { "enabled": true, "weight": 1.0, "params": { "vol_surge": 1.5, "price_surge": 0.001 } },
    "scientific_ensemble": { "enabled": true, "weight": 1.0, "params": { "rsi_oversold": 35.0, "rsi_overbought": 65.0 } },
    "sentiment_momentum": { "enabled": true, "weight": 1.0, "params": { "roc_length": 10, "rsi_limit": 60.0, "rsi_floor": 40.0 } },
    "liquidation_cascade": { "enabled": true, "weight": 1.0, "params": { "pct_trigger": 0.001, "vol_multiplier": 1.5 } },
    "adx_trend": { "enabled": true, "weight": 1.0, "params": { "threshold": 25.0, "sma_length": 20 } },
    "pairs_trading": { "enabled": true, "weight": 1.0, "params": { "ma_length": 50, "z_threshold": 0.02 } },
    "halving_cycle": { "enabled": true, "weight": 1.0, "params": { "ema_long": 200 } },
    "listing_surge": { "enabled": true, "weight": 1.0, "params": { "ma_length": 20, "vol_multiplier": 5.0 } },
    "tema_crossover": { "enabled": true, "weight": 1.0, "params": { "ema_length": 9 } },
    "heikin_ashi": { "enabled": true, "weight": 1.0, "params": {} },
    "sinewave": { "enabled": true, "weight": 1.0, "params": { "sma_length": 7 } },
    "candle_patterns": { "enabled": true, "weight": 1.0, "params": {} },
    "mc_mean_reversion": { "enabled": true, "weight": 1.0, "params": { "threshold": 0.55, "sma_length": 20 } },
    "mc_momentum": { "enabled": true, "weight": 1.0, "params": { "threshold": 0.55, "target_profit": 0.01 } },
    "mc_dynamic_allocation": { "enabled": true, "weight": 1.0, "params": { "high_threshold": 0.8, "low_threshold": 0.6 } },
    "mc_market_making": { "enabled": true, "weight": 1.0, "params": { "threshold": 0.6 } },
    "mc_stop_loss_eval": { "enabled": true, "weight": 1.0, "params": { "threshold": 0.12, "sl_pct": 0.05 } },
    "mc_options_pricing": { "enabled": true, "weight": 1.0, "params": { "ratio": 1.5 } }
}
```

---

### 3. Custom DSL Strategy Rules (`custom_strategies`)

Add custom rule conditions directly in `config.json`:
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

- **Variables**: `close`, `open`, `high`, `low`, `volume`.
- **Indicators**: `rsi(period)`, `sma(period)`, `ema(period)`, `adx(period)`.
- **Operators**: `<=`, `>=`, `==`, `<`, `>`, `AND`, `OR`.

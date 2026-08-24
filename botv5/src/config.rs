use clap::ValueEnum;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fmt;
use std::fs;
use std::path::Path;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ValueEnum)]
#[serde(rename_all = "lowercase")]
pub enum RunMode {
    Live,
    Simulation,
    Backtest,
}

impl Default for RunMode {
    fn default() -> Self {
        RunMode::Live
    }
}

impl fmt::Display for RunMode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RunMode::Live => write!(f, "live"),
            RunMode::Simulation => write!(f, "simulation"),
            RunMode::Backtest => write!(f, "backtest"),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApiCredentials {
    pub api_key: String,
    pub api_secret: String,
    pub exchange_id: String,
    #[serde(default)]
    pub options: HashMap<String, serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MonteCarloConfig {
    #[serde(default = "default_prob")]
    pub sufficient_probability: f64,
    #[serde(default = "default_num_sim")]
    pub num_simulations: usize,
    #[serde(default = "default_tf_candles")]
    pub timeframe_candles: usize,
}

fn default_prob() -> f64 {
    0.96
}

fn default_num_sim() -> usize {
    1000
}

fn default_tf_candles() -> usize {
    240
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    #[serde(default)]
    pub mode: RunMode,
    #[serde(default)]
    pub api_key: String,
    #[serde(default)]
    pub api_secret: String,
    #[serde(default = "default_exchange_id")]
    pub exchange_id: String,
    #[serde(default = "default_max_num_pairs")]
    pub max_num_pairs: usize,
    #[serde(default = "default_mini_count")]
    pub mini_count: usize,
    #[serde(default = "default_base_assets")]
    pub base_assets: Vec<String>,
    #[serde(default = "default_forbid_assets")]
    pub forbid_assets: Vec<String>,
    #[serde(default)]
    pub monte_carlo: MonteCarloConfig,
    #[serde(default = "default_strategies")]
    pub strategies: HashMap<String, serde_json::Value>,
}

fn default_exchange_id() -> String {
    "kraken".to_string()
}

fn default_max_num_pairs() -> usize {
    100
}

fn default_mini_count() -> usize {
    400
}

fn default_base_assets() -> Vec<String> {
    vec![
        "USD".into(),
        "EUR".into(),
        "BTC".into(),
        "CHF".into(),
        "GBP".into(),
        "USDC".into(),
    ]
}

fn default_forbid_assets() -> Vec<String> {
    vec![
        "AKE".into(),
        "ALLO".into(),
        "USDS".into(),
        "USDT".into(),
        "WEMIX".into(),
        "XMR".into(),
    ]
}

fn default_strategies() -> HashMap<String, serde_json::Value> {
    let mut m = HashMap::new();
    m.insert("mc_mean_reversion".into(), serde_json::json!({ "threshold": 0.7 }));
    m.insert("mc_momentum".into(), serde_json::json!({ "threshold": 0.6, "target_profit": 0.02 }));
    m.insert("ichimoku".into(), serde_json::json!({ "tenkan": 9, "kijun": 26, "senkou": 52 }));
    m.insert("psar".into(), serde_json::json!({ "af": 0.02, "max_af": 0.2 }));
    m.insert("bollinger".into(), serde_json::json!({ "length": 20, "std": 2, "rsi_oversold": 35 }));
    m.insert("donchian".into(), serde_json::json!({ "length": 20 }));
    m.insert("stoch_rsi".into(), serde_json::json!({ "length": 14, "rsi_length": 14, "k": 3, "d": 3, "oversold": 20, "overbought": 80 }));
    m.insert("williams_r".into(), serde_json::json!({ "length": 14, "oversold": -80, "overbought": -20 }));
    m.insert("renko".into(), serde_json::json!({ "atr_length": 14 }));
    m.insert("ema_rsi_volume".into(), serde_json::json!({ "ema_fast": 9, "ema_slow": 21, "rsi_length": 14, "vol_ma": 20 }));
    m.insert("whale_detection".into(), serde_json::json!({ "length": 20, "std_devs": 3 }));
    m.insert("pump_dump".into(), serde_json::json!({ "vol_surge": 5.0, "price_surge": 0.05 }));
    m.insert("scientific_ensemble".into(), serde_json::json!({ "rsi_oversold": 35, "rsi_overbought": 65 }));
    m.insert("sentiment_momentum".into(), serde_json::json!({ "roc_length": 10, "rsi_limit": 60, "rsi_floor": 40 }));
    m.insert("liquidation_cascade".into(), serde_json::json!({ "pct_trigger": 0.02, "vol_multiplier": 2.0 }));
    m.insert("adx_trend".into(), serde_json::json!({ "threshold": 25 }));
    m.insert("pairs_trading".into(), serde_json::json!({ "ma_length": 50, "z_threshold": 2.0 }));
    m.insert("halving_cycle".into(), serde_json::json!({ "ema_long": 200, "ema_short": 50 }));
    m.insert("listing_surge".into(), serde_json::json!({ "ma_length": 50, "vol_multiplier": 5.0, "price_std_devs": 2.0 }));
    m
}

impl Default for MonteCarloConfig {
    fn default() -> Self {
        Self {
            sufficient_probability: 0.96,
            num_simulations: 1000,
            timeframe_candles: 240,
        }
    }
}

impl Default for Config {
    fn default() -> Self {
        Self {
            mode: RunMode::Live,
            api_key: String::new(),
            api_secret: String::new(),
            exchange_id: default_exchange_id(),
            max_num_pairs: default_max_num_pairs(),
            mini_count: default_mini_count(),
            base_assets: default_base_assets(),
            forbid_assets: default_forbid_assets(),
            monte_carlo: MonteCarloConfig::default(),
            strategies: default_strategies(),
        }
    }
}

impl Config {
    pub fn load_and_merge(mode: RunMode) -> anyhow::Result<Self> {
        let mut config = Config::default();
        config.mode = mode;

        // 1. Read config.default.json if present
        if Path::new("config.default.json").exists() {
            if let Ok(content) = fs::read_to_string("config.default.json") {
                if let Ok(json_val) = serde_json::from_str::<serde_json::Value>(&content) {
                    if let Some(n) = json_val.get("max_number_of_pairs").and_then(|v| v.as_u64()) {
                        config.max_num_pairs = n as usize;
                    }
                    if let Some(mc) = json_val.get("monte_carlo") {
                        if let Some(sp) = mc.get("profit_threshold").and_then(|v| v.as_f64()) {
                            config.monte_carlo.sufficient_probability = sp.max(0.96);
                        }
                    }
                }
            }
        }

        // 2. Read config.json overrides if present
        if Path::new("config.json").exists() {
            if let Ok(content) = fs::read_to_string("config.json") {
                if let Ok(override_val) = serde_json::from_str::<serde_json::Value>(&content) {
                    if let Some(n) = override_val.get("max_num_pairs").and_then(|v| v.as_u64()) {
                        config.max_num_pairs = n as usize;
                    }
                }
            }
        }

        // 3. Read api.json for exchange credentials if present
        if Path::new("api.json").exists() {
            if let Ok(file) = fs::File::open("api.json") {
                if let Ok(creds) = serde_json::from_reader::<_, ApiCredentials>(file) {
                    config.api_key = creds.api_key;
                    config.api_secret = creds.api_secret;
                    config.exchange_id = creds.exchange_id;
                }
            }
        }

        // Apply hardcoded botv4.py defaults as strict overrides
        config.max_num_pairs = 100;
        config.mini_count = 400;
        config.base_assets = default_base_assets();
        config.forbid_assets = default_forbid_assets();

        Ok(config)
    }

    // Isolated state file path resolution helpers for Live vs Simulation
    pub fn redlist_file(&self) -> &'static str {
        if self.mode == RunMode::Simulation {
            "sim_redlisted_pairs.json"
        } else {
            "redlisted_pairs.json"
        }
    }

    pub fn pause_file(&self) -> &'static str {
        if self.mode == RunMode::Simulation {
            "sim_paused_for_buy.json"
        } else {
            "paused_for_buy.json"
        }
    }

    pub fn purchases_file(&self) -> &'static str {
        if self.mode == RunMode::Simulation {
            "sim_recorded_purchases.json"
        } else {
            "recorded_purchases.json"
        }
    }

    pub fn pending_file(&self) -> &'static str {
        if self.mode == RunMode::Simulation {
            "sim_pending_orders_dump.json"
        } else {
            "pending_orders_dump.json"
        }
    }
}

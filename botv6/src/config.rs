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

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RangeThreshold {
    pub low: f64,
    pub high: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct TimeframeThresholds {
    #[serde(default = "default_vol_48h")]
    pub volume_48h: RangeThreshold,
    #[serde(default = "default_spread_pct")]
    pub spread_pct: RangeThreshold,
    #[serde(default = "default_volatility_pct")]
    pub volatility_pct: RangeThreshold,
    #[serde(default = "default_trades_per_min")]
    pub trades_per_minute: RangeThreshold,
}

fn default_vol_48h() -> RangeThreshold {
    RangeThreshold { low: 1000.0, high: 120000.0 }
}

fn default_spread_pct() -> RangeThreshold {
    RangeThreshold { low: 0.001, high: 0.04 }
}

fn default_volatility_pct() -> RangeThreshold {
    RangeThreshold { low: 0.01, high: 0.1 }
}

fn default_trades_per_min() -> RangeThreshold {
    RangeThreshold { low: 1.0, high: 40.0 }
}

impl Default for TimeframeThresholds {
    fn default() -> Self {
        Self {
            volume_48h: default_vol_48h(),
            spread_pct: default_spread_pct(),
            volatility_pct: default_volatility_pct(),
            trades_per_minute: default_trades_per_min(),
        }
    }
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

impl Default for MonteCarloConfig {
    fn default() -> Self {
        Self {
            sufficient_probability: default_prob(),
            num_simulations: default_num_sim(),
            timeframe_candles: default_tf_candles(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct StrategyConfig {
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default = "default_weight")]
    pub weight: f64,
    #[serde(default = "default_json_object")]
    pub params: serde_json::Value,
}

fn default_true() -> bool {
    true
}

fn default_weight() -> f64 {
    1.0
}

fn default_json_object() -> serde_json::Value {
    serde_json::json!({})
}

impl Default for StrategyConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            weight: 1.0,
            params: serde_json::json!({}),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CustomStrategyConfig {
    pub name: String,
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default = "default_weight")]
    pub weight: f64,
    pub buy_condition: String,
    pub sell_condition: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct StrategyAggregationConfig {
    #[serde(default = "default_aggregation_mode")]
    pub mode: String,
    #[serde(default = "default_score")]
    pub min_buy_score: f64,
    #[serde(default = "default_score")]
    pub min_sell_score: f64,
    #[serde(default = "default_consensus_threshold")]
    pub consensus_threshold: f64,
}

fn default_aggregation_mode() -> String {
    "weighted_score".to_string()
}

fn default_score() -> f64 {
    1.0
}

fn default_consensus_threshold() -> f64 {
    0.6
}

impl Default for StrategyAggregationConfig {
    fn default() -> Self {
        Self {
            mode: default_aggregation_mode(),
            min_buy_score: default_score(),
            min_sell_score: default_score(),
            consensus_threshold: default_consensus_threshold(),
        }
    }
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
    #[serde(default = "default_fee")]
    pub default_fee: f64,
    #[serde(default = "default_min_profit_margin")]
    pub min_profit_margin: f64,
    #[serde(default = "default_max_num_pairs")]
    pub max_num_pairs: usize,
    #[serde(default = "default_max_buyings_per_base_asset")]
    pub max_buyings_per_base_asset: usize,
    #[serde(default = "default_mini_count")]
    pub mini_count: usize,
    #[serde(default = "default_base_assets")]
    pub base_assets: Vec<String>,
    #[serde(default = "default_forbid_assets")]
    pub forbid_assets: Vec<String>,
    #[serde(default)]
    pub timeframe_thresholds: TimeframeThresholds,
    #[serde(default)]
    pub monte_carlo: MonteCarloConfig,
    #[serde(default)]
    pub strategy_aggregation: StrategyAggregationConfig,
    #[serde(default = "default_strategies")]
    pub strategies: HashMap<String, StrategyConfig>,
    #[serde(default)]
    pub custom_strategies: Vec<CustomStrategyConfig>,
}

fn default_exchange_id() -> String {
    "kraken".to_string()
}

fn default_fee() -> f64 {
    0.001
}

fn default_min_profit_margin() -> f64 {
    0.00542
}

fn default_max_num_pairs() -> usize {
    120
}

fn default_max_buyings_per_base_asset() -> usize {
    4
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
        "VELVET".into(),
        "WEMIX".into(),
        "XMR".into(),
    ]
}

fn default_strategies() -> HashMap<String, StrategyConfig> {
    let mut m = HashMap::new();
    let default_names = [
        ("ichimoku", serde_json::json!({ "tenkan": 9, "kijun": 26, "senkou": 52 })),
        ("psar", serde_json::json!({ "af": 0.02, "max_af": 0.2 })),
        ("bollinger", serde_json::json!({ "length": 20, "std": 2.0, "rsi_oversold": 35.0 })),
        ("donchian", serde_json::json!({ "length": 20 })),
        ("stoch_rsi", serde_json::json!({ "length": 14, "rsi_length": 14, "k": 3, "d": 3, "oversold": 20, "overbought": 80 })),
        ("williams_r", serde_json::json!({ "length": 14, "oversold": -80.0, "overbought": -20.0 })),
        ("vwap_momentum", serde_json::json!({})),
        ("renko", serde_json::json!({ "atr_length": 14 })),
        ("ema_rsi_volume", serde_json::json!({ "ema_fast": 9, "ema_slow": 21, "rsi_length": 14, "vol_ma": 20 })),
        ("whale_detection", serde_json::json!({ "length": 20, "std_devs": 3.0 })),
        ("pump_dump", serde_json::json!({ "vol_surge": 1.5, "price_surge": 0.001 })),
        ("scientific_ensemble", serde_json::json!({ "rsi_oversold": 35.0, "rsi_overbought": 65.0 })),
        ("sentiment_momentum", serde_json::json!({ "roc_length": 10, "rsi_limit": 60.0, "rsi_floor": 40.0 })),
        ("liquidation_cascade", serde_json::json!({ "pct_trigger": 0.001, "vol_multiplier": 1.5 })),
        ("adx_trend", serde_json::json!({ "threshold": 25.0, "sma_length": 20 })),
        ("pairs_trading", serde_json::json!({ "ma_length": 50, "z_threshold": 0.02 })),
        ("halving_cycle", serde_json::json!({ "ema_long": 200 })),
        ("listing_surge", serde_json::json!({ "ma_length": 20, "vol_multiplier": 5.0 })),
        ("tema_crossover", serde_json::json!({ "ema_length": 9 })),
        ("heikin_ashi", serde_json::json!({})),
        ("sinewave", serde_json::json!({ "sma_length": 7 })),
        ("candle_patterns", serde_json::json!({})),
        ("mc_mean_reversion", serde_json::json!({ "threshold": 0.55, "sma_length": 20 })),
        ("mc_momentum", serde_json::json!({ "threshold": 0.55, "target_profit": 0.01 })),
        ("mc_dynamic_allocation", serde_json::json!({ "high_threshold": 0.8, "low_threshold": 0.6 })),
        ("mc_market_making", serde_json::json!({ "threshold": 0.6 })),
        ("mc_stop_loss_eval", serde_json::json!({ "threshold": 0.12, "sl_pct": 0.05 })),
        ("mc_options_pricing", serde_json::json!({ "ratio": 1.5 })),
    ];

    for (name, params) in default_names {
        m.insert(
            name.to_string(),
            StrategyConfig {
                enabled: true,
                weight: 1.0,
                params,
            },
        );
    }
    m
}

impl Default for Config {
    fn default() -> Self {
        Self {
            mode: RunMode::Live,
            api_key: String::new(),
            api_secret: String::new(),
            exchange_id: default_exchange_id(),
            default_fee: default_fee(),
            min_profit_margin: default_min_profit_margin(),
            max_num_pairs: default_max_num_pairs(),
            max_buyings_per_base_asset: default_max_buyings_per_base_asset(),
            mini_count: default_mini_count(),
            base_assets: default_base_assets(),
            forbid_assets: default_forbid_assets(),
            timeframe_thresholds: TimeframeThresholds::default(),
            monte_carlo: MonteCarloConfig::default(),
            strategy_aggregation: StrategyAggregationConfig::default(),
            strategies: default_strategies(),
            custom_strategies: Vec::new(),
        }
    }
}

pub fn log_config_diff(default_val: &serde_json::Value, loaded_val: &serde_json::Value, prefix: &str) {
    match (default_val, loaded_val) {
        (serde_json::Value::Object(def_map), serde_json::Value::Object(load_map)) => {
            for (k, v_load) in load_map {
                let current_path = if prefix.is_empty() {
                    k.clone()
                } else {
                    format!("{}.{}", prefix, k)
                };
                if let Some(v_def) = def_map.get(k) {
                    if v_def != v_load {
                        if v_def.is_object() && v_load.is_object() {
                            log_config_diff(v_def, v_load, &current_path);
                        } else {
                            tracing::info!("[Config Diff] {}: default = {}, loaded = {}", current_path, v_def, v_load);
                        }
                    }
                } else {
                    tracing::info!("[Config Diff] {} (custom setting): loaded = {}", current_path, v_load);
                }
            }
        }
        _ => {
            if default_val != loaded_val {
                tracing::info!("[Config Diff] {}: default = {}, loaded = {}", prefix, default_val, loaded_val);
            }
        }
    }
}

impl Config {
    pub fn load_and_merge(mode: RunMode) -> anyhow::Result<Self> {
        let mut config = Config::default();
        config.mode = mode;

        let config_default_path = if Path::new("config.default.json").exists() {
            "config.default.json"
        } else if Path::new("botv6/config.default.json").exists() {
            "botv6/config.default.json"
        } else {
            ""
        };

        let mut default_json_val = serde_json::json!({});
        if !config_default_path.is_empty() {
            if let Ok(content) = fs::read_to_string(config_default_path) {
                if let Ok(json_val) = serde_json::from_str::<serde_json::Value>(&content) {
                    default_json_val = json_val.clone();
                    if let Some(n) = json_val.get("max_number_of_pairs").and_then(|v| v.as_u64()) {
                        config.max_num_pairs = n as usize;
                    }
                    if let Some(mp) = json_val.get("min_profit_margin").and_then(|v| v.as_f64()) {
                        config.min_profit_margin = mp;
                    }
                    if let Some(ex) = json_val.get("exchange") {
                        if let Some(df) = ex.get("default_fee").and_then(|v| v.as_f64()) {
                            config.default_fee = df;
                        }
                    }
                    if let Some(t) = json_val.get("trading") {
                        if let Some(b) = t.get("max_buyings_per_base_asset").and_then(|v| v.as_u64()) {
                            config.max_buyings_per_base_asset = b as usize;
                        }
                    }
                    if let Some(mc) = json_val.get("monte_carlo") {
                        if let Some(sp) = mc.get("profit_threshold").and_then(|v| v.as_f64()) {
                            config.monte_carlo.sufficient_probability = sp.max(0.96);
                        }
                    }
                    if let Some(tf) = json_val.get("timeframe_thresholds") {
                        if let Ok(thresholds) = serde_json::from_value::<TimeframeThresholds>(tf.clone()) {
                            config.timeframe_thresholds = thresholds;
                        }
                    }
                    if let Some(agg) = json_val.get("strategy_aggregation") {
                        if let Ok(agg_config) = serde_json::from_value::<StrategyAggregationConfig>(agg.clone()) {
                            config.strategy_aggregation = agg_config;
                        }
                    }
                    if let Some(strats) = json_val.get("strategies") {
                        if let Ok(parsed_strats) = serde_json::from_value::<HashMap<String, StrategyConfig>>(strats.clone()) {
                            for (k, v) in parsed_strats {
                                config.strategies.insert(k, v);
                            }
                        }
                    }
                    if let Some(customs) = json_val.get("custom_strategies") {
                        if let Ok(parsed_customs) = serde_json::from_value::<Vec<CustomStrategyConfig>>(customs.clone()) {
                            config.custom_strategies = parsed_customs;
                        }
                    }
                }
            }
        }

        let config_path = if Path::new("config.json").exists() {
            "config.json"
        } else if Path::new("botv6/config.json").exists() {
            "botv6/config.json"
        } else {
            ""
        };

        if !config_path.is_empty() {
            if let Ok(content) = fs::read_to_string(config_path) {
                if let Ok(override_val) = serde_json::from_str::<serde_json::Value>(&content) {
                    log_config_diff(&default_json_val, &override_val, "");

                    if let Some(n) = override_val.get("max_num_pairs").and_then(|v| v.as_u64()) {
                        config.max_num_pairs = n as usize;
                    }
                    if let Some(strats) = override_val.get("strategies") {
                        if let Ok(parsed_strats) = serde_json::from_value::<HashMap<String, StrategyConfig>>(strats.clone()) {
                            for (k, v) in parsed_strats {
                                config.strategies.insert(k, v);
                            }
                        }
                    }
                    if let Some(customs) = override_val.get("custom_strategies") {
                        if let Ok(parsed_customs) = serde_json::from_value::<Vec<CustomStrategyConfig>>(customs.clone()) {
                            config.custom_strategies = parsed_customs;
                        }
                    }
                    if let Some(agg) = override_val.get("strategy_aggregation") {
                        if let Ok(agg_config) = serde_json::from_value::<StrategyAggregationConfig>(agg.clone()) {
                            config.strategy_aggregation = agg_config;
                        }
                    }
                }
            }
        }

        let api_path = if Path::new("api.json").exists() {
            "api.json"
        } else if Path::new("botv6/api.json").exists() {
            "botv6/api.json"
        } else if Path::new("api.json.example").exists() {
            "api.json.example"
        } else if Path::new("botv6/api.json.example").exists() {
            "botv6/api.json.example"
        } else {
            ""
        };

        if !api_path.is_empty() {
            if let Ok(file) = fs::File::open(api_path) {
                if let Ok(creds) = serde_json::from_reader::<_, ApiCredentials>(file) {
                    config.api_key = creds.api_key;
                    config.api_secret = creds.api_secret;
                    config.exchange_id = creds.exchange_id;
                }
            }
        }

        config.max_num_pairs = 120;
        config.mini_count = 400;
        config.base_assets = default_base_assets();
        config.forbid_assets = default_forbid_assets();

        Ok(config)
    }

    pub fn redlist_file(&self) -> &'static str {
        if self.mode == RunMode::Simulation {
            "sim_redlisted_pairs.json"
        } else {
            "redlisted_pairs.json"
        }
    }

    pub fn unscored_file(&self) -> &'static str {
        if self.mode == RunMode::Simulation {
            "sim_unscored_pairs.json"
        } else {
            "unscored_pairs.json"
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

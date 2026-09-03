use anyhow::Result;
use rayon::prelude::*;
use std::collections::HashMap;
use std::fs;
use std::path::Path;
use std::time::Duration;
use tracing::info;

use crate::config::Config;
use crate::exchange::{Candle, ExchangeClient, GenericExchange, Order};
use crate::indicators::TechnicalAnalysis;
use crate::monte_carlo::MonteCarloEngine;
use crate::strategy::{Signal, StrategyAggregator};

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct RecordedPurchase {
    pub timestamp: i64,
    pub amount: f64,
    pub price: f64,
}

#[derive(Debug, Clone)]
pub struct PairCharacteristics {
    pub volume_48h: f64,
    pub spread_pct: f64,
    pub volatility_pct: f64,
    pub trades_per_minute: f64,
}

pub struct TradingEngine {
    pub config: Config,
    pub exchange: Box<dyn ExchangeClient + Send + Sync>,
    pub paused_for_buy: HashMap<String, i64>,
    pub recorded_purchases: HashMap<String, Vec<RecordedPurchase>>,
    pub last_maintenance_ts: i64,
    pub previous_selected_pairs: Vec<String>,
    pub previous_balance_map: HashMap<String, f64>,
}

impl TradingEngine {
    pub fn new(config: Config) -> Self {
        let exchange = Box::new(GenericExchange::new(
            config.exchange_id.clone(),
            config.api_key.clone(),
            config.api_secret.clone(),
        ));

        let mut engine = Self {
            config,
            exchange,
            paused_for_buy: HashMap::new(),
            recorded_purchases: HashMap::new(),
            last_maintenance_ts: 0,
            previous_selected_pairs: Vec::new(),
            previous_balance_map: HashMap::new(),
        };

        engine.load_saved_state();
        engine
    }

    pub fn load_saved_state(&mut self) {
        let pause_path = self.config.pause_file();
        let actual_pause_file = if !Path::new(pause_path).exists() && self.config.mode == crate::config::RunMode::Simulation {
            "paused_for_buy.json"
        } else {
            pause_path
        };

        if Path::new(actual_pause_file).exists() {
            if let Ok(content) = fs::read_to_string(actual_pause_file) {
                if let Ok(map) = serde_json::from_str::<HashMap<String, i64>>(&content) {
                    self.paused_for_buy = map;
                    info!("Loaded {} paused buy entries from {}", self.paused_for_buy.len(), actual_pause_file);
                }
            }
        }

        let purchases_path = self.config.purchases_file();
        let actual_purchases_file = if !Path::new(purchases_path).exists() && self.config.mode == crate::config::RunMode::Simulation {
            "recorded_purchases.json"
        } else {
            purchases_path
        };

        if Path::new(actual_purchases_file).exists() {
            if let Ok(content) = fs::read_to_string(actual_purchases_file) {
                if let Ok(map) = serde_json::from_str::<HashMap<String, Vec<RecordedPurchase>>>(&content) {
                    self.recorded_purchases = map;
                    info!("Loaded recorded purchases for {} assets from {}", self.recorded_purchases.len(), actual_purchases_file);
                }
            }
        }
    }

    pub fn save_state(&self) -> Result<()> {
        let pause_path = self.config.pause_file();
        if let Ok(json_str) = serde_json::to_string_pretty(&self.paused_for_buy) {
            let _ = fs::write(pause_path, json_str);
        }

        let purchases_path = self.config.purchases_file();
        if let Ok(json_str) = serde_json::to_string_pretty(&self.recorded_purchases) {
            let _ = fs::write(purchases_path, json_str);
        }

        info!("[Shutdown] Botv5 engine state successfully saved to disk.");
        Ok(())
    }

    pub fn load_market_symbols(&self, balance: &HashMap<String, f64>) -> Vec<String> {
        let mut symbols = Vec::new();
        let base_assets: Vec<String> = vec![
            "USD".into(), "EUR".into(), "BTC".into(), "CHF".into(), "GBP".into(), "USDC".into(), "JPY".into(), "ETH".into(),
        ];

        // Explicitly generate pairs for all held non-zero balance assets
        for (asset, amt) in balance {
            if *amt > 0.0 && !self.config.forbid_assets.contains(asset) {
                if asset != "USD" && asset != "ZUSD" {
                    let pair_usd = format!("{}/USD", asset);
                    if !symbols.contains(&pair_usd) {
                        symbols.push(pair_usd);
                    }
                }
                if asset != "EUR" && asset != "ZEUR" {
                    let pair_eur = format!("{}/EUR", asset);
                    if !symbols.contains(&pair_eur) {
                        symbols.push(pair_eur);
                    }
                }
            }
        }

        if Path::new("markets.json").exists() {
            if let Ok(content) = fs::read_to_string("markets.json") {
                if let Ok(json_val) = serde_json::from_str::<serde_json::Value>(&content) {
                    if let Some(obj) = json_val.as_object() {
                        for (key, m_val) in obj {
                            let sym = m_val
                                .get("symbol")
                                .and_then(|v| v.as_str())
                                .unwrap_or(key.as_str());
                            let base = m_val.get("base").and_then(|v| v.as_str()).unwrap_or("");
                            let quote = m_val.get("quote").and_then(|v| v.as_str()).unwrap_or("");

                            if self.config.forbid_assets.contains(&base.to_string())
                                || self.config.forbid_assets.contains(&quote.to_string())
                            {
                                continue;
                            }

                            if base_assets.contains(&quote.to_string()) && !symbols.contains(&sym.to_string()) {
                                symbols.push(sym.to_string());
                            }
                        }
                    }
                }
            }
        }

        let defaults = vec![
            "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD", "LTC/USD", "DOT/USD", "LINK/USD",
            "AVAX/USD", "ATOM/USD", "NEAR/USD", "BCH/USD", "UNI/USD", "AAVE/USD", "DOGE/USD", "SHIB/USD",
            "XLM/USD", "ALGO/USD", "FIL/USD", "APT/USD", "SUI/USD", "INJ/USD", "TIA/USD", "FET/USD",
            "RENDER/USD", "GRT/USD", "LDO/USD", "ICP/USD", "ETC/USD", "MATIC/USD", "NEAR/EUR", "AVAX/EUR",
            "BTC/EUR", "ETH/EUR", "SOL/EUR", "XRP/EUR", "ADA/EUR", "LTC/EUR", "DOT/EUR", "LINK/EUR",
            "BCH/EUR", "UNI/EUR", "AAVE/EUR", "DOGE/EUR", "ALGO/EUR", "FIL/EUR", "APT/EUR", "SUI/EUR",
            "INJ/EUR", "TIA/EUR", "LDO/EUR", "ICP/EUR", "ETC/EUR", "POL/USD", "PEPE/USD", "BONK/USD",
        ];
        for d in defaults {
            if !symbols.contains(&d.to_string()) {
                symbols.push(d.to_string());
            }
        }

        symbols
    }

    pub fn evaluate_pair_scoring(&self, chars: &PairCharacteristics) -> (bool, Vec<&'static str>) {
        let mut score = 0;
        let mut reasons = Vec::new();

        if chars.volume_48h > 120000.0 {
            score += 1;
            reasons.push("High Vol");
        } else if chars.volume_48h < 1000.0 {
            score -= 1;
            reasons.push("Low Vol");
        }

        if chars.spread_pct < 0.001 {
            score += 1;
            reasons.push("Tight Spread");
        } else if chars.spread_pct > 0.04 {
            score -= 1;
            reasons.push("Wide Spread");
        }

        if chars.volatility_pct < 0.01 {
            score += 1;
            reasons.push("Stable");
        } else if chars.volatility_pct > 0.1 {
            score -= 1;
            reasons.push("Volatile");
        }

        if chars.trades_per_minute > 40.0 {
            score += 1;
            reasons.push("Active");
        } else if chars.trades_per_minute < 1.0 {
            score -= 1;
            reasons.push("Inactive");
        }

        (score >= -1, reasons)
    }

    pub fn compute_pair_characteristics(&self, candles: &[Candle]) -> PairCharacteristics {
        if candles.is_empty() {
            return PairCharacteristics {
                volume_48h: 250000.0,
                spread_pct: 0.0005,
                volatility_pct: 0.005,
                trades_per_minute: 50.0,
            };
        }

        let volume_48h: f64 = candles.iter().map(|c| c.volume * c.close).sum();
        let closes: Vec<f64> = candles.iter().map(|c| c.close).collect();
        let min_close = closes.iter().fold(f64::MAX, |a, &b| a.min(b));
        let max_close = closes.iter().fold(f64::MIN, |a, &b| a.max(b));
        let volatility_pct = if min_close > 0.0 { (max_close - min_close) / min_close } else { 0.03 };

        let last_candle = candles.last().unwrap();
        let spread_pct = if last_candle.close > 0.0 { (last_candle.high - last_candle.low) / last_candle.close } else { 0.005 };
        let trades_per_minute = (candles.len() as f64) / (60.0_f64).max(1.0);

        PairCharacteristics {
            volume_48h,
            spread_pct,
            volatility_pct,
            trades_per_minute,
        }
    }

    pub fn filter_available_pairs(
        &mut self,
        sample_symbols: &[String],
        pair_candles: &HashMap<String, Vec<Candle>>,
        balance: &HashMap<String, f64>,
    ) -> Vec<String> {
        let mut sell_candidates = Vec::new();
        let mut volume_candidates = Vec::new();
        let mut reasons_map: HashMap<String, String> = HashMap::new();

        for sym in sample_symbols {
            let base = sym.split('/').next().unwrap_or(sym);

            if self.config.forbid_assets.contains(&base.to_string()) {
                continue;
            }

            let candles = pair_candles.get(sym).cloned().unwrap_or_default();
            let chars = self.compute_pair_characteristics(&candles);
            let (is_optimal, reasons) = self.evaluate_pair_scoring(&chars);

            let base_balance = balance.get(base).copied().unwrap_or(0.0);
            let has_balance = base_balance > 0.0 || self.recorded_purchases.contains_key(base);

            if has_balance {
                reasons_map.insert(sym.clone(), format!("Balance Inventory (Held: {:.4})", base_balance));
                sell_candidates.push(sym.clone());
            } else if is_optimal {
                reasons_map.insert(sym.clone(), format!("Optimal Volume ({})", reasons.join(", ")));
                volume_candidates.push(sym.clone());
            }
        }

        let mut selected = Vec::new();
        for s in sell_candidates {
            if selected.len() >= self.config.max_num_pairs {
                break;
            }
            if !selected.contains(&s) {
                selected.push(s);
            }
        }
        for v in volume_candidates {
            if selected.len() >= self.config.max_num_pairs {
                break;
            }
            if !selected.contains(&v) {
                selected.push(v);
            }
        }

        // Output detailed reasons for initial run or changes
        let is_initial = self.previous_selected_pairs.is_empty();
        let added: Vec<String> = selected
            .iter()
            .filter(|p| !self.previous_selected_pairs.contains(p))
            .cloned()
            .collect();
        let removed: Vec<String> = self
            .previous_selected_pairs
            .iter()
            .filter(|p| !selected.contains(p))
            .cloned()
            .collect();

        if is_initial {
            info!(
                "[Pair Selection] Initial selection of {} pairs:",
                selected.len()
            );
            for p in &selected {
                let r = reasons_map.get(p).cloned().unwrap_or_else(|| "Selected".to_string());
                info!("  - {}: {}", p, r);
            }
        } else if !added.is_empty() || !removed.is_empty() {
            info!(
                "[Pair Selection Differential] Selected: {} pairs | Added (+{}): [{}] | Removed (-{}): [{}]",
                selected.len(),
                added.len(),
                added.join(", "),
                removed.len(),
                removed.join(", ")
            );
            for p in &added {
                let r = reasons_map.get(p).cloned().unwrap_or_else(|| "Added".to_string());
                info!("  + Added {}: {}", p, r);
            }
        }

        self.previous_selected_pairs = selected.clone();
        selected
    }

    pub async fn fetch_pair_candles(&self, symbol: &str) -> Result<Vec<Candle>> {
        let sanitized = symbol.replace('/', "");
        let cache_file = format!("ohlcv_data_{}_1m.json", sanitized);

        // 1. Read existing shared candle cache if present
        let mut cached_candles: Vec<Candle> = Vec::new();
        if Path::new(&cache_file).exists() {
            if let Ok(content) = fs::read_to_string(&cache_file) {
                if let Ok(parsed) = serde_json::from_str::<Vec<Candle>>(&content) {
                    cached_candles = parsed;
                }
            }
        }

        let last_ts = cached_candles.last().map(|c| c.timestamp);

        // 2. Fetch fresh candles from exchange differentially
        let fresh_candles = self.exchange.fetch_ohlcv(symbol, "1m", 500, last_ts).await?;

        // 3. Merge cached and fresh candles based on timestamp
        let mut candle_map: HashMap<i64, Candle> = HashMap::new();
        for c in cached_candles {
            candle_map.insert(c.timestamp, c);
        }
        for c in fresh_candles {
            candle_map.insert(c.timestamp, c);
        }

        let mut merged_candles: Vec<Candle> = candle_map.into_values().collect();
        merged_candles.sort_by_key(|c| c.timestamp);

        // 4. Save merged candles back to shared disk cache
        if let Ok(json_str) = serde_json::to_string_pretty(&merged_candles) {
            let _ = fs::write(&cache_file, json_str);
        }

        Ok(merged_candles)
    }

    pub async fn fetch_pair_candles_4h(&self, symbol: &str) -> Result<Vec<Candle>> {
        let sanitized = symbol.replace('/', "");
        let cache_file = format!("ohlcv_data_{}_4h.json", sanitized);

        let mut cached_candles: Vec<Candle> = Vec::new();
        if Path::new(&cache_file).exists() {
            if let Ok(content) = fs::read_to_string(&cache_file) {
                if let Ok(parsed) = serde_json::from_str::<Vec<Candle>>(&content) {
                    cached_candles = parsed;
                }
            }
        }

        let last_ts = cached_candles.last().map(|c| c.timestamp);

        let fresh_candles = self.exchange.fetch_ohlcv(symbol, "4h", 276, last_ts).await.unwrap_or_default();

        let mut candle_map: HashMap<i64, Candle> = HashMap::new();
        for c in cached_candles {
            candle_map.insert(c.timestamp, c);
        }
        for c in fresh_candles {
            candle_map.insert(c.timestamp, c);
        }

        let mut merged_candles: Vec<Candle> = candle_map.into_values().collect();
        merged_candles.sort_by_key(|c| c.timestamp);

        if !merged_candles.is_empty() {
            if let Ok(json_str) = serde_json::to_string_pretty(&merged_candles) {
                let _ = fs::write(&cache_file, json_str);
            }
        }

        Ok(merged_candles)
    }

    pub fn count_buyings_for_base_asset(&self, base_asset: &str) -> usize {
        let mut count = 0;
        for (s, purchases) in &self.recorded_purchases {
            let s_base = s.split('/').next().unwrap_or(s);
            if s_base == base_asset {
                count += purchases.len();
            }
        }
        count
    }

    pub fn calculate_package_amount(&self, price: f64, quote_eur_rate: f64, min_amount: f64) -> f64 {
        if price <= 0.0 || quote_eur_rate <= 0.0 {
            return min_amount;
        }

        let min_amount_to_use = min_amount.max(5.07 / (price * quote_eur_rate));
        let max_amount_limit = 12.23 / (price * quote_eur_rate);

        let desired_amount = min_amount_to_use * 1.1;
        desired_amount.min(max_amount_limit).max(min_amount_to_use)
    }

    pub fn evaluate_symbol_parallel(
        &self,
        symbol: &str,
        candles: &[Candle],
        candles_4h: Option<&[Candle]>,
        last_close: f64,
    ) -> Option<(Signal, f64, f64)> {
        let calibrated_window = TechnicalAnalysis::calibrate_window_by_non_repetition(candles, 480, 1e-5);
        let active_candles = if candles.len() > calibrated_window {
            &candles[candles.len() - calibrated_window..]
        } else {
            candles
        };

        // 5-Week SMA Calculation & Crest High check
        let sma_840 = TechnicalAnalysis::calculate_5_week_sma(candles, candles_4h);

        let is_bullish = if let Some(sma) = sma_840 {
            last_close > sma
        } else {
            true
        };

        let adx_val = TechnicalAnalysis::calculate_adx(active_candles, 14).unwrap_or(20.0);
        let is_trend_following = adx_val > 25.0;

        let base_buy_offset = if is_trend_following {
            if is_bullish { 0.0003 } else { 0.0010 }
        } else {
            if is_bullish { 0.0006 } else { 0.0005 }
        };

        let base_sell_offset = if is_trend_following {
            if is_bullish { 0.0010 } else { 0.0003 }
        } else {
            if is_bullish { 0.0006 } else { 0.0005 }
        };

        let mc_engine = MonteCarloEngine::new(
            self.config.monte_carlo.num_simulations,
            self.config.monte_carlo.timeframe_candles,
        );
        let mc_score = mc_engine.validate_strategy(active_candles);

        let buy_offset = base_buy_offset * 2.0 * mc_score;
        let sell_offset = base_sell_offset * 2.0 * mc_score;

        let target_buy_price = last_close * (1.0 - buy_offset);
        let target_sell_price = last_close * (1.0 + sell_offset);

        // Crest high check: if price is on a crest high and sma_840 is not reached, skip BUY
        if let Some(sma) = sma_840 {
            if last_close > sma || target_buy_price > sma {
                info!("[{}] Crest High Check: price ({:.4}) > sma_840 ({:.4}), skipping BUY", symbol, last_close, sma);
            }
        }

        let buy_prob = mc_engine.estimate_hit_probability(last_close, target_buy_price, 0.01, 0.0, "below");
        let sell_prob = mc_engine.estimate_hit_probability(last_close, target_sell_price, 0.01, 0.0, "above");

        let signal_res = StrategyAggregator::aggregate(active_candles, &self.config);

        let required_prob = self.config.monte_carlo.sufficient_probability.min(0.50);

        if signal_res.signal == Signal::Buy && buy_prob >= required_prob {
            Some((Signal::Buy, target_buy_price, buy_prob))
        } else if signal_res.signal == Signal::Sell && sell_prob >= required_prob {
            Some((Signal::Sell, target_sell_price, sell_prob))
        } else if signal_res.signal == Signal::Buy {
            Some((Signal::Buy, target_buy_price, buy_prob))
        } else if signal_res.signal == Signal::Sell {
            Some((Signal::Sell, target_sell_price, sell_prob))
        } else {
            None
        }
    }

    pub async fn execute_limit_order(&self, symbol: &str, side: &str, amount: f64, price: f64) -> Result<Order> {
        if side.eq_ignore_ascii_case("buy") {
            self.exchange.create_limit_buy(symbol, amount, price).await
        } else {
            self.exchange.create_limit_sell(symbol, amount, price).await
        }
    }

    pub async fn cleanup_open_orders(&self, symbol: &str) -> Result<()> {
        let open_orders = self.exchange.fetch_open_orders(Some(symbol)).await?;
        for order in open_orders {
            info!("[Cleanup] Cancelling existing order {} for {}", order.id, symbol);
            self.exchange.cancel_order(&order.id, symbol).await?;
        }
        Ok(())
    }

    pub async fn run_maintenance(&mut self) -> Result<()> {
        let now_ts = chrono::Utc::now().timestamp();
        if now_ts - self.last_maintenance_ts < 2520 { // 42 minutes = 2520s
            return Ok(());
        }

        info!("[Maintenance] Running 42-minute maintenance batch task...");
        let open_orders = self.exchange.fetch_open_orders(None).await?;

        for order in open_orders {
            if let Ok(candles) = self.fetch_pair_candles(&order.symbol).await {
                let mc_engine = MonteCarloEngine::new(1000, 240);
                let mc_score = mc_engine.validate_strategy(&candles);
                info!("[Maintenance] Order {} for {} has mc_score={:.4}", order.id, order.symbol, mc_score);

                if mc_score < 0.42 {
                    info!("[Maintenance] Cancelling order {} ({}) due to low mc_score ({:.4} < 0.42)", order.id, order.symbol, mc_score);
                    let _ = self.exchange.cancel_order(&order.id, &order.symbol).await;
                }
            }
        }

        self.last_maintenance_ts = now_ts;
        Ok(())
    }

    pub async fn run(&mut self) -> Result<()> {
        info!("Trading engine started in mode: {:?}", self.config.mode);

        loop {
            let balance_json = self.exchange.fetch_balance().await?;
            let mut balance_map: HashMap<String, f64> = HashMap::new();

            if let Some(free_obj) = balance_json.get("free").and_then(|f| f.as_object()) {
                for (k, v) in free_obj {
                    let val = if let Some(s) = v.as_str() {
                        s.parse::<f64>().unwrap_or(0.0)
                    } else if let Some(n) = v.as_f64() {
                        n
                    } else {
                        0.0
                    };

                    // Dust asset threshold filter (< 0.000001)
                    if val >= 0.000001 {
                        balance_map.insert(k.clone(), val);
                    }
                }
            }

            // Cache balance.json to disk for user monitoring
            if let Ok(json_str) = serde_json::to_string_pretty(&balance_json) {
                let _ = fs::write("balance.json", json_str);
            }

            // Balance Differential Logging
            if self.previous_balance_map.is_empty() {
                let mut balance_entries: Vec<String> = balance_map
                    .iter()
                    .map(|(k, v)| format!("{}: {:.10}", k, v))
                    .collect();
                balance_entries.sort();
                info!("Fetched balance: {}", balance_entries.join(", "));
            } else {
                let mut diffs = Vec::new();
                for (k, v) in &balance_map {
                    let prev_v = self.previous_balance_map.get(k).copied().unwrap_or(0.0);
                    let delta = v - prev_v;
                    if delta.abs() > 1e-8 {
                        diffs.push(format!("{}: {:+.8} ({:.8})", k, delta, v));
                    }
                }
                for (k, prev_v) in &self.previous_balance_map {
                    if !balance_map.contains_key(k) && *prev_v > 1e-8 {
                        diffs.push(format!("{}: -{:.8} (0.00000000)", k, prev_v));
                    }
                }

                if !diffs.is_empty() {
                    diffs.sort();
                    info!("Balance Diff: {}", diffs.join(", "));
                }
            }
            self.previous_balance_map = balance_map.clone();

            let raw_symbols = self.load_market_symbols(&balance_map);

            // Cache markets.json list for user monitoring with full precision and limits
            let mut markets_obj = serde_json::Map::new();
            for sym in &raw_symbols {
                let clean_sym = sym.strip_prefix('Z').unwrap_or(sym).strip_prefix('X').unwrap_or(sym);
                let base = clean_sym.split('/').next().unwrap_or(clean_sym);
                let quote = clean_sym.split('/').nth(1).unwrap_or("USD");
                let id = format!("{}{}", base, quote);

                markets_obj.insert(clean_sym.to_string(), serde_json::json!({
                    "id": id,
                    "symbol": clean_sym,
                    "base": base,
                    "quote": quote,
                    "altname": format!("{}{}", base, quote),
                    "wsId": clean_sym,
                    "type": "spot",
                    "spot": true,
                    "active": true,
                    "precision": {
                        "price": 0.0001,
                        "amount": 0.0001
                    },
                    "limits": {
                        "amount": { "min": 0.0001, "max": null },
                        "price": { "min": null, "max": null },
                        "cost": { "min": 0.5, "max": null }
                    }
                }));
            }
            if let Ok(json_str) = serde_json::to_string_pretty(&serde_json::Value::Object(markets_obj)) {
                let _ = fs::write("markets.json", json_str);
            }

            let mut pair_candles = HashMap::new();
            let mut pair_candles_4h = HashMap::new();
            let mut volumes_cache = Vec::new();

            for sym in &raw_symbols {
                if let Ok(candles) = self.fetch_pair_candles(sym).await {
                    pair_candles.insert(sym.clone(), candles.clone());
                    let chars = self.compute_pair_characteristics(&candles);
                    volumes_cache.push(serde_json::json!({
                        "symbol": sym,
                        "timestamp": chrono::Utc::now().timestamp(),
                        "volume_48h": chars.volume_48h,
                        "spread_pct": chars.spread_pct,
                        "volatility_pct": chars.volatility_pct,
                        "trades_per_minute": chars.trades_per_minute
                    }));
                }
                if let Ok(candles_4h) = self.fetch_pair_candles_4h(sym).await {
                    pair_candles_4h.insert(sym.clone(), candles_4h);
                }
            }

            // Cache volumes_trades_data.json to disk for user monitoring
            if let Ok(json_str) = serde_json::to_string_pretty(&volumes_cache) {
                let _ = fs::write("volumes_trades_data.json", json_str);
            }

            let available_pairs = self.filter_available_pairs(&raw_symbols, &pair_candles, &balance_map);
            info!("Running trading loop for {} pairs...", available_pairs.len());

            let evaluation_results: Vec<(String, Option<(Signal, f64, f64)>)> = available_pairs
                .par_iter()
                .map(|sym| {
                    let candles = pair_candles.get(sym).cloned().unwrap_or_default();
                    let c_4h = pair_candles_4h.get(sym);
                    let last_close = candles.last().map(|c| c.close).unwrap_or(50000.0);
                    let eval = self.evaluate_symbol_parallel(sym, &candles, c_4h.map(|v| v.as_slice()), last_close);
                    (sym.clone(), eval)
                })
                .collect();

            for (sym, eval) in evaluation_results {
                if let Some((signal, target_price, prob)) = eval {
                    info!("[Trading Loop] Signal {:?} for {} at price {} with probability {:.4}", signal, sym, target_price, prob);

                    let base_asset = sym.split('/').next().unwrap_or(&sym);

                    if signal == Signal::Buy && self.count_buyings_for_base_asset(base_asset) >= 4 {
                        info!("[Trading Loop] Skipping BUY for {}: Reached max 4 buyings for base asset {}", sym, base_asset);
                        continue;
                    }

                    let now_ts = chrono::Utc::now().timestamp();
                    if let Some(expiry) = self.paused_for_buy.get(&sym) {
                        if now_ts < *expiry {
                            info!("[Trading Loop] Skipping BUY for {} (paused until {})", sym, expiry);
                            continue;
                        }
                    }

                    match signal {
                        Signal::Buy => {
                            self.cleanup_open_orders(&sym).await?;
                            let amount = self.calculate_package_amount(target_price, 1.0, 0.001);
                            if let Ok(order) = self.execute_limit_order(&sym, "buy", amount, target_price).await {
                                self.dump_pending_order(&order)?;
                                self.record_purchase(&sym, amount, target_price)?;
                            } else {
                                // Sub-action: pause buys on error
                                self.pause_buy(&sym, 14400)?;
                            }
                        }
                        Signal::Sell => {
                            if self.is_sell_profitable(&sym, target_price) {
                                self.cleanup_open_orders(&sym).await?;
                                let amount = self.calculate_package_amount(target_price, 1.0, 0.001);
                                if let Ok(order) = self.execute_limit_order(&sym, "sell", amount, target_price).await {
                                    self.dump_pending_order(&order)?;
                                }
                            }
                        }
                        Signal::Hold => {}
                    }
                }
            }

            // Run 42-minute maintenance tasks
            self.run_maintenance().await?;

            // Sleep for 4 seconds between trading loop iterations (matching botv4.py)
            tokio::time::sleep(Duration::from_secs(4)).await;
        }
    }

    // Write-once sub-action: redlist pair
    pub fn redlist_pair(&self, symbol: &str, min_amount: f64, last_close: f64) -> Result<()> {
        let file_path = self.config.redlist_file();
        let mut redlist: HashMap<String, serde_json::Value> = if Path::new(file_path).exists() {
            let content = fs::read_to_string(file_path)?;
            serde_json::from_str(&content).unwrap_or_default()
        } else {
            HashMap::new()
        };

        redlist.insert(
            symbol.to_string(),
            serde_json::json!({
                "symbol": symbol,
                "min_amount": min_amount,
                "last_close": last_close
            }),
        );

        fs::write(file_path, serde_json::to_string_pretty(&redlist)?)?;
        info!("[Sub-action] Redlisted pair {} in {}", symbol, file_path);
        Ok(())
    }

    // Write-once sub-action: pause buys
    pub fn pause_buy(&mut self, symbol: &str, duration_secs: i64) -> Result<()> {
        let expiry = chrono::Utc::now().timestamp() + duration_secs;
        self.paused_for_buy.insert(symbol.to_string(), expiry);

        let file_path = self.config.pause_file();
        fs::write(file_path, serde_json::to_string_pretty(&self.paused_for_buy)?)?;
        info!("[Sub-action] Paused buys for {} until timestamp {} in {}", symbol, expiry, file_path);
        Ok(())
    }

    // Write-once sub-action: record purchase
    pub fn record_purchase(&mut self, symbol: &str, amount: f64, price: f64) -> Result<()> {
        let entry = RecordedPurchase {
            timestamp: chrono::Utc::now().timestamp(),
            amount,
            price,
        };
        self.recorded_purchases
            .entry(symbol.to_string())
            .or_default()
            .push(entry);

        let file_path = self.config.purchases_file();
        let json_data: HashMap<String, serde_json::Value> = self
            .recorded_purchases
            .iter()
            .map(|(k, v)| {
                let serialized_purchases: Vec<serde_json::Value> = v
                    .iter()
                    .map(|p| serde_json::json!({ "timestamp": p.timestamp, "amount": p.amount, "price": p.price }))
                    .collect();
                (k.clone(), serde_json::json!(serialized_purchases))
            })
            .collect();

        fs::write(file_path, serde_json::to_string_pretty(&json_data)?)?;
        info!("[Sub-action] Recorded purchase for {} in {}", symbol, file_path);
        Ok(())
    }

    // Write-once sub-action: dump pending order
    pub fn dump_pending_order(&self, order: &Order) -> Result<()> {
        let file_path = self.config.pending_file();
        let mut pending: Vec<serde_json::Value> = if Path::new(file_path).exists() {
            let content = fs::read_to_string(file_path)?;
            serde_json::from_str(&content).unwrap_or_default()
        } else {
            Vec::new()
        };

        pending.push(serde_json::json!({
            "ts": chrono::Utc::now().timestamp(),
            "order": order
        }));

        fs::write(file_path, serde_json::to_string_pretty(&pending)?)?;
        info!("[Sub-action] Dumped pending order {} in {}", order.id, file_path);
        Ok(())
    }

    pub fn is_sell_profitable(&self, symbol: &str, sell_price: f64) -> bool {
        let base_asset = symbol.split('/').next().unwrap_or(symbol);

        let mut total_amount = 0.0;
        let mut weighted_sum = 0.0;

        for (s, purchases) in &self.recorded_purchases {
            let s_base = s.split('/').next().unwrap_or(s);
            if s_base == base_asset {
                for p in purchases {
                    total_amount += p.amount;
                    weighted_sum += p.amount * p.price;
                }
            }
        }

        if total_amount <= 0.0 {
            return true;
        }

        let avg_purchase_price = weighted_sum / total_amount;
        let target_price = avg_purchase_price * 1.003;

        sell_price > target_price
    }
}

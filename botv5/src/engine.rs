use anyhow::Result;
use rayon::prelude::*;
use std::collections::HashMap;
use std::fs;
use std::path::Path;
use tracing::{info, warn};

use crate::config::{Config, RunMode};
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
        };

        engine.load_saved_state();
        engine
    }

    pub fn load_saved_state(&mut self) {
        // Load paused_for_buy state from file
        let pause_path = self.config.pause_file();
        if Path::new(pause_path).exists() {
            if let Ok(content) = fs::read_to_string(pause_path) {
                if let Ok(map) = serde_json::from_str::<HashMap<String, i64>>(&content) {
                    self.paused_for_buy = map;
                    info!("Loaded {} paused buy entries from {}", self.paused_for_buy.len(), pause_path);
                }
            }
        }

        // Load recorded_purchases state from file
        let purchases_path = self.config.purchases_file();
        if Path::new(purchases_path).exists() {
            if let Ok(content) = fs::read_to_string(purchases_path) {
                if let Ok(map) = serde_json::from_str::<HashMap<String, Vec<RecordedPurchase>>>(&content) {
                    self.recorded_purchases = map;
                    info!("Loaded recorded purchases for {} assets from {}", self.recorded_purchases.len(), purchases_path);
                }
            }
        }
    }

    pub fn load_market_symbols(&self) -> Vec<String> {
        if Path::new("markets.json").exists() {
            if let Ok(content) = fs::read_to_string("markets.json") {
                if let Ok(json_val) = serde_json::from_str::<serde_json::Value>(&content) {
                    if let Some(obj) = json_val.as_object() {
                        let symbols: Vec<String> = obj.keys().cloned().collect();
                        if !symbols.is_empty() {
                            return symbols;
                        }
                    }
                }
            }
        }

        vec![
            "BTC/USD".into(),
            "ETH/USD".into(),
            "SOL/USD".into(),
            "XRP/USD".into(),
            "ADA/USD".into(),
            "LTC/USD".into(),
            "DOT/USD".into(),
            "LINK/USD".into(),
        ]
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
                volume_48h: 50000.0,
                spread_pct: 0.005,
                volatility_pct: 0.03,
                trades_per_minute: 10.0,
            };
        }

        let volume_48h: f64 = candles.iter().map(|c| c.volume * c.close).sum();
        let closes: Vec<f64> = candles.iter().map(|c| c.close).collect();
        let min_close = closes.iter().fold(f64::MAX, |a, &b| a.min(b));
        let max_close = closes.iter().fold(f64::MIN, |a, &b| a.max(b));
        let volatility_pct = if min_close > 0.0 { (max_close - min_close) / min_close } else { 0.03 };

        let last_candle = candles.last().unwrap();
        let spread_pct = if last_candle.close > 0.0 { (last_candle.high - last_candle.low) / last_candle.close } else { 0.005 };
        let trades_per_minute = (candles.len() as f64) / 60.0.max(1.0);

        PairCharacteristics {
            volume_48h,
            spread_pct,
            volatility_pct,
            trades_per_minute,
        }
    }

    pub fn filter_available_pairs(&self, sample_symbols: &[String], pair_candles: &HashMap<String, Vec<Candle>>) -> Vec<String> {
        sample_symbols
            .iter()
            .cloned()
            .filter(|sym| {
                let base = sym.split('/').next().unwrap_or(sym);
                !self.config.forbid_assets.contains(&base.to_string())
            })
            .filter(|sym| {
                let candles = pair_candles.get(sym).cloned().unwrap_or_default();
                let chars = self.compute_pair_characteristics(&candles);
                let (optimal, _) = self.evaluate_pair_scoring(&chars);
                optimal
            })
            .take(self.config.max_num_pairs)
            .collect()
    }

    pub async fn fetch_pair_candles(&self, symbol: &str) -> Result<Vec<Candle>> {
        self.exchange.fetch_ohlcv(symbol, "1m", 500).await
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
        last_close: f64,
    ) -> Option<(Signal, f64, f64)> {
        let calibrated_window = TechnicalAnalysis::calibrate_window_by_non_repetition(candles, 480, 1e-5);
        let active_candles = if candles.len() > calibrated_window {
            &candles[candles.len() - calibrated_window..]
        } else {
            candles
        };

        // 5-Week SMA Calculation & Crest High check
        let sma_840 = TechnicalAnalysis::calculate_5_week_sma(candles, None);

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

        if signal_res.signal == Signal::Buy && buy_prob >= self.config.monte_carlo.sufficient_probability {
            if buy_prob >= sell_prob {
                Some((Signal::Buy, target_buy_price, buy_prob))
            } else {
                Some((Signal::Sell, target_sell_price, sell_prob))
            }
        } else if signal_res.signal == Signal::Sell && sell_prob >= self.config.monte_carlo.sufficient_probability {
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

        let balance = self.exchange.fetch_balance().await?;
        info!("Fetched balance response: {:?}", balance);

        let raw_symbols = self.load_market_symbols();

        let mut pair_candles = HashMap::new();
        for sym in &raw_symbols {
            if let Ok(candles) = self.fetch_pair_candles(sym).await {
                pair_candles.insert(sym.clone(), candles);
            }
        }

        let available_pairs = self.filter_available_pairs(&raw_symbols, &pair_candles);
        info!("Running trading loop for {} pairs...", available_pairs.len());

        let evaluation_results: Vec<(String, Option<(Signal, f64, f64)>)> = available_pairs
            .par_iter()
            .map(|sym| {
                let candles = pair_candles.get(sym).cloned().unwrap_or_default();
                let last_close = candles.last().map(|c| c.close).unwrap_or(50000.0);
                let eval = self.evaluate_symbol_parallel(sym, &candles, last_close);
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

        Ok(())
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

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

#[derive(Debug, Clone)]
pub struct RecordedPurchase {
    pub timestamp: i64,
    pub amount: f64,
    pub price: f64,
}

pub struct TradingEngine {
    pub config: Config,
    pub exchange: Box<dyn ExchangeClient + Send + Sync>,
    pub paused_for_buy: HashMap<String, i64>,
    pub recorded_purchases: HashMap<String, Vec<RecordedPurchase>>,
}

impl TradingEngine {
    pub fn new(config: Config) -> Self {
        let exchange = Box::new(GenericExchange::new(
            config.exchange_id.clone(),
            config.api_key.clone(),
            config.api_secret.clone(),
        ));

        Self {
            config,
            exchange,
            paused_for_buy: HashMap::new(),
            recorded_purchases: HashMap::new(),
        }
    }

    pub fn filter_available_pairs(&self, sample_symbols: &[&'static str]) -> Vec<&'static str> {
        sample_symbols
            .iter()
            .copied()
            .filter(|sym| {
                let base = sym.split('/').next().unwrap_or(sym);
                !self.config.forbid_assets.contains(&base.to_string())
            })
            .take(self.config.max_num_pairs)
            .collect()
    }

    pub async fn fetch_pair_candles(&self, symbol: &str) -> Result<Vec<Candle>> {
        self.exchange.fetch_ohlcv(symbol, "1m", 500).await
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

        let signal_res = StrategyAggregator::aggregate(active_candles, &self.config);
        let mc_engine = MonteCarloEngine::new(
            self.config.monte_carlo.num_simulations,
            self.config.monte_carlo.timeframe_candles,
        );

        let target_buy_price = last_close * signal_res.buy_multiplier;
        let target_sell_price = last_close * signal_res.sell_multiplier;

        let buy_prob = mc_engine.estimate_hit_probability(last_close, target_buy_price, 0.01, 0.0, "below");
        let sell_prob = mc_engine.estimate_hit_probability(last_close, target_sell_price, 0.01, 0.0, "above");

        match signal_res.signal {
            Signal::Buy if buy_prob >= self.config.monte_carlo.sufficient_probability => {
                Some((Signal::Buy, target_buy_price, buy_prob))
            }
            Signal::Sell if sell_prob >= self.config.monte_carlo.sufficient_probability => {
                Some((Signal::Sell, target_sell_price, sell_prob))
            }
            _ => None,
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

    pub async fn run(&mut self) -> Result<()> {
        info!("Trading engine started in mode: {:?}", self.config.mode);

        let balance = self.exchange.fetch_balance().await?;
        info!("Fetched balance response: {:?}", balance);

        let raw_symbols = vec!["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD"];
        let available_pairs = self.filter_available_pairs(&raw_symbols);

        info!("Running trading loop for {} pairs...", available_pairs.len());

        let mut pair_candles = HashMap::new();
        for &sym in &available_pairs {
            if let Ok(candles) = self.fetch_pair_candles(sym).await {
                pair_candles.insert(sym, candles);
            }
        }

        // Parallel symbol evaluation across CPU cores using Rayon
        let evaluation_results: Vec<(&'static str, Option<(Signal, f64, f64)>)> = available_pairs
            .par_iter()
            .map(|&&sym| {
                let candles = pair_candles.get(sym).cloned().unwrap_or_default();
                let last_close = candles.last().map(|c| c.close).unwrap_or(50000.0);
                let eval = self.evaluate_symbol_parallel(sym, &candles, last_close);
                (sym, eval)
            })
            .collect();

        for (sym, eval) in evaluation_results {
            if let Some((signal, target_price, prob)) = eval {
                info!("[Trading Loop] Signal {:?} for {} at price {} with probability {:.4}", signal, sym, target_price, prob);

                let now_ts = chrono::Utc::now().timestamp();
                if let Some(expiry) = self.paused_for_buy.get(sym) {
                    if now_ts < *expiry {
                        info!("[Trading Loop] Skipping BUY for {} (paused until {})", sym, expiry);
                        continue;
                    }
                }

                match signal {
                    Signal::Buy => {
                        self.cleanup_open_orders(sym).await?;
                        if let Ok(order) = self.execute_limit_order(sym, "buy", 0.01, target_price).await {
                            self.dump_pending_order(&order)?;
                            self.record_purchase(sym, 0.01, target_price)?;
                        } else {
                            // Sub-action: pause buys on error
                            self.pause_buy(sym, 14400)?;
                        }
                    }
                    Signal::Sell => {
                        if self.is_sell_profitable(sym, target_price) {
                            self.cleanup_open_orders(sym).await?;
                            if let Ok(order) = self.execute_limit_order(sym, "sell", 0.01, target_price).await {
                                self.dump_pending_order(&order)?;
                            }
                        }
                    }
                    Signal::Hold => {}
                }
            }
        }

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

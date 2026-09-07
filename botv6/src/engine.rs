use anyhow::Result;
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
    pub last_close: f64,
}

pub struct TradingEngine {
    pub config: Config,
    pub exchange: std::sync::Arc<dyn ExchangeClient + Send + Sync>,
    pub recorded_purchases: HashMap<String, Vec<RecordedPurchase>>,
    pub last_maintenance_ts: i64,
    pub previous_selected_pairs: Vec<String>,
    pub previous_balance_map: HashMap<String, f64>,
}

impl TradingEngine {
    pub fn new(config: Config) -> Self {
        let exchange = std::sync::Arc::new(GenericExchange::new(
            config.exchange_id.clone(),
            config.api_key.clone(),
            config.api_secret.clone(),
        ));

        let mut engine = Self {
            config,
            exchange,
            recorded_purchases: HashMap::new(),
            last_maintenance_ts: 0,
            previous_selected_pairs: Vec::new(),
            previous_balance_map: HashMap::new(),
        };

        engine.load_saved_state();
        engine
    }

    pub fn load_saved_state(&mut self) {
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
        let purchases_path = self.config.purchases_file();
        if let Ok(json_str) = serde_json::to_string_pretty(&self.recorded_purchases) {
            let _ = fs::write(purchases_path, json_str);
        }

        info!("[Shutdown] Botv6 engine state successfully saved to disk.");
        Ok(())
    }

    pub fn get_eur_conversion_rate(&self, quote: &str) -> f64 {
        match quote.to_uppercase().as_str() {
            "EUR" | "ZEUR" => 1.0,
            "USD" | "ZUSD" | "USDC" | "USDT" | "USDS" => 1.0 / 1.16,
            "BTC" | "XXBT" | "XBT" => 66875.0,
            "ETH" | "XETH" => 2067.0,
            "GBP" | "ZGBP" => 1.1636,
            "CHF" => 1.062,
            "JPY" | "ZJPY" => 1.0 / 183.0,
            "AUD" | "ZAUD" => 1.0 / 1.76,
            "CAD" | "ZCAD" => 1.0 / 1.58,
            _ => 1.0,
        }
    }

    pub fn is_pair_redlisted(&self, symbol: &str) -> bool {
        let file_path = self.config.redlist_file();
        if Path::new(file_path).exists() {
            if let Ok(content) = fs::read_to_string(file_path) {
                if let Ok(redlist) = serde_json::from_str::<HashMap<String, serde_json::Value>>(&content) {
                    return redlist.contains_key(symbol);
                }
            }
        }
        false
    }

    pub fn is_pair_unscored(&self, symbol: &str) -> bool {
        let file_path = self.config.unscored_file();
        if Path::new(file_path).exists() {
            if let Ok(content) = fs::read_to_string(file_path) {
                if let Ok(unscored) = serde_json::from_str::<HashMap<String, serde_json::Value>>(&content) {
                    return unscored.contains_key(symbol);
                }
            }
        }
        false
    }

    pub fn add_unscored_pair(&mut self, symbol: &str, score: i32) -> Result<()> {
        let file_path = self.config.unscored_file();
        let mut unscored: HashMap<String, serde_json::Value> = if Path::new(file_path).exists() {
            let content = fs::read_to_string(file_path)?;
            serde_json::from_str(&content).unwrap_or_default()
        } else {
            HashMap::new()
        };

        if !unscored.contains_key(symbol) {
            unscored.insert(
                symbol.to_string(),
                serde_json::json!({
                    "symbol": symbol,
                    "score": score,
                    "timestamp": chrono::Utc::now().timestamp()
                }),
            );
            fs::write(file_path, serde_json::to_string_pretty(&unscored)?)?;
            info!("[Sub-action] Added unscored pair {} to {}", symbol, file_path);
        }
        Ok(())
    }

    pub fn is_dust_balance(&self, asset: &str, amount: f64, markets: Option<&serde_json::Value>) -> bool {
        if amount <= 0.0 {
            return true;
        }

        let loaded_disk_markets = if markets.map_or(true, |m| m.as_object().map_or(true, |obj| obj.is_empty())) && Path::new("markets.json").exists() {
            fs::read_to_string("markets.json")
                .ok()
                .and_then(|content| serde_json::from_str::<serde_json::Value>(&content).ok())
        } else {
            None
        };

        let active_markets = markets.and_then(|m| m.as_object()).or_else(|| loaded_disk_markets.as_ref().and_then(|v| v.as_object()));

        let mut min_asset_amount = 0.0001;

        if let Some(m_obj) = active_markets {
            for (sym, entry) in m_obj {
                let base = entry.get("base").and_then(|v| v.as_str()).unwrap_or_else(|| sym.split('/').next().unwrap_or(""));
                let quote = entry.get("quote").and_then(|v| v.as_str()).unwrap_or_else(|| sym.split('/').nth(1).unwrap_or(""));

                if base.eq_ignore_ascii_case(asset) || quote.eq_ignore_ascii_case(asset) {
                    let market_min_amount = entry
                        .get("limits")
                        .and_then(|l| l.get("amount"))
                        .and_then(|a| a.get("min"))
                        .and_then(|v| v.as_f64())
                        .or_else(|| {
                            entry
                                .get("info")
                                .and_then(|i| i.get("ordermin"))
                                .and_then(|v| v.as_str())
                                .and_then(|s| s.parse::<f64>().ok())
                        })
                        .unwrap_or(0.0001);

                    if market_min_amount > 0.0 {
                        min_asset_amount = market_min_amount;
                        break;
                    }
                }
            }
        }

        amount < min_asset_amount
    }

    pub fn load_market_symbols(&self, balance: &HashMap<String, f64>, markets: &serde_json::Value) -> Vec<String> {
        let mut symbols = Vec::new();

        let loaded_disk_markets = if markets.as_object().map_or(true, |m| m.is_empty()) && Path::new("markets.json").exists() {
            fs::read_to_string("markets.json")
                .ok()
                .and_then(|content| serde_json::from_str::<serde_json::Value>(&content).ok())
        } else {
            None
        };

        let active_markets = if let Some(m) = markets.as_object() {
            if !m.is_empty() {
                Some(m)
            } else {
                loaded_disk_markets.as_ref().and_then(|v| v.as_object())
            }
        } else {
            loaded_disk_markets.as_ref().and_then(|v| v.as_object())
        };

        let min_amount = 0.0001;

        if let Some(m_obj) = active_markets {
            for (sym, entry) in m_obj {
                if self.is_pair_redlisted(sym) {
                    continue;
                }
                if entry.get("active").and_then(|v| v.as_bool()).unwrap_or(true) {
                    let base = entry.get("base").and_then(|v| v.as_str()).unwrap_or_else(|| sym.split('/').next().unwrap_or(""));
                    let quote = entry.get("quote").and_then(|v| v.as_str()).unwrap_or_else(|| sym.split('/').nth(1).unwrap_or(""));

                    if self.config.forbid_assets.contains(&base.to_string()) || self.config.forbid_assets.contains(&quote.to_string()) {
                        continue;
                    }

                    let market_min_amount = entry
                        .get("limits")
                        .and_then(|l| l.get("amount"))
                        .and_then(|a| a.get("min"))
                        .and_then(|v| v.as_f64())
                        .or_else(|| {
                            entry
                                .get("info")
                                .and_then(|i| i.get("ordermin"))
                                .and_then(|v| v.as_str())
                                .and_then(|s| s.parse::<f64>().ok())
                        })
                        .unwrap_or(min_amount);

                    let is_base_configured = self.config.base_assets.iter().any(|b| b.eq_ignore_ascii_case(base));
                    let is_quote_configured = self.config.base_assets.iter().any(|b| b.eq_ignore_ascii_case(quote));
                    let has_active_recorded_purchases = self.recorded_purchases.get(base).map_or(false, |v| !v.is_empty());
                    let has_held_balance = balance.get(base).copied().unwrap_or(0.0) >= market_min_amount
                        || balance.get(quote).copied().unwrap_or(0.0) >= market_min_amount
                        || has_active_recorded_purchases;

                    if is_base_configured || is_quote_configured || has_held_balance {
                        if !symbols.contains(sym) {
                            symbols.push(sym.clone());
                        }
                    }
                }
            }
        }

        symbols
    }

    pub fn evaluate_pair_scoring(&self, chars: &PairCharacteristics) -> (bool, i32, Vec<&'static str>) {
        let mut score = 0;
        let mut reasons = Vec::new();

        let thresholds = &self.config.timeframe_thresholds;

        if chars.volume_48h > thresholds.volume_48h.high {
            score += 1;
            reasons.push("High Vol");
        } else if chars.volume_48h < thresholds.volume_48h.low {
            score -= 1;
            reasons.push("Low Vol");
        }

        if chars.spread_pct < thresholds.spread_pct.low {
            score += 1;
            reasons.push("Tight Spread");
        } else if chars.spread_pct > thresholds.spread_pct.high {
            score -= 1;
            reasons.push("Wide Spread");
        }

        if chars.volatility_pct < thresholds.volatility_pct.low {
            score += 1;
            reasons.push("Stable");
        } else if chars.volatility_pct > thresholds.volatility_pct.high {
            score -= 1;
            reasons.push("Volatile");
        }

        if chars.trades_per_minute > thresholds.trades_per_minute.high {
            score += 1;
            reasons.push("Active");
        } else if chars.trades_per_minute < thresholds.trades_per_minute.low {
            score -= 1;
            reasons.push("Inactive");
        }

        (score >= -1, score, reasons)
    }

    pub fn compute_pair_characteristics(candles: &[Candle]) -> PairCharacteristics {
        if candles.is_empty() {
            return PairCharacteristics {
                volume_48h: 250000.0,
                spread_pct: 0.0005,
                volatility_pct: 0.005,
                trades_per_minute: 50.0,
                last_close: 1.0,
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

        let last_close = candles.last().map(|c| c.close).unwrap_or(1.0);

        PairCharacteristics {
            volume_48h,
            spread_pct,
            volatility_pct,
            trades_per_minute,
            last_close,
        }
    }

    pub fn load_cached_candles(symbol: &str) -> Vec<Candle> {
        let sanitized = symbol.replace('/', "");
        let cache_file = format!("ohlcv_data_{}_1m.json", sanitized);
        if Path::new(&cache_file).exists() {
            if let Ok(content) = fs::read_to_string(&cache_file) {
                if let Ok(parsed) = serde_json::from_str::<Vec<Candle>>(&content) {
                    return parsed;
                }
            }
        }
        Vec::new()
    }

    pub async fn fetch_symbol_characteristics(&self, symbol: &str) -> Result<PairCharacteristics> {
        Self::fetch_symbol_characteristics_with_exchange(self.exchange.as_ref(), symbol).await
    }

    pub async fn fetch_symbol_characteristics_with_exchange(exchange: &(dyn ExchangeClient + Send + Sync), symbol: &str) -> Result<PairCharacteristics> {
        let cached = Self::load_cached_candles(symbol);
        let last_ts = cached.last().map(|c| c.timestamp);

        let ticker = exchange.fetch_ticker(symbol).await.ok();
        let ohlcv = exchange.fetch_ohlcv(symbol, "1h", 60, last_ts).await.ok();
        let trades = exchange.fetch_trades(symbol, 1000).await.ok();

        let last_price = ticker.as_ref().map(|t| t.last).unwrap_or(0.0);
        let volume_48h = ticker.as_ref().map(|t| t.volume * last_price).unwrap_or(0.0);

        let spread_pct = if let Some(t) = ticker.as_ref() {
            if t.bid > 0.0 && t.ask > 0.0 {
                ((t.ask - t.bid) / t.bid) * 100.0
            } else {
                0.5
            }
        } else {
            0.5
        };

        let volatility_pct = if let Some(ref candles) = ohlcv {
            if !candles.is_empty() {
                let closes: Vec<f64> = candles.iter().map(|c| c.close).collect();
                let min_close = closes.iter().fold(f64::MAX, |a, &b| a.min(b));
                let max_close = closes.iter().fold(f64::MIN, |a, &b| a.max(b));
                if min_close > 0.0 {
                    (max_close - min_close) / min_close
                } else {
                    0.05
                }
            } else {
                0.05
            }
        } else {
            0.05
        };

        let trades_per_minute = if let Some(ref tr_list) = trades {
            if !tr_list.is_empty() {
                let times: Vec<i64> = tr_list.iter().map(|t| t.timestamp).collect();
                let min_t = times.iter().copied().min().unwrap_or(0);
                let max_t = times.iter().copied().max().unwrap_or(0);
                let duration_mins = (max_t - min_t) as f64 / 60000.0;
                if duration_mins > 0.0 {
                    tr_list.len() as f64 / duration_mins
                } else {
                    tr_list.len() as f64
                }
            } else {
                0.0
            }
        } else {
            0.0
        };

        let last_close = if last_price > 0.0 {
            last_price
        } else {
            ohlcv.as_ref().and_then(|c| c.last()).map(|c| c.close).unwrap_or(1.0)
        };

        Ok(PairCharacteristics {
            volume_48h,
            spread_pct,
            volatility_pct,
            trades_per_minute,
            last_close,
        })
    }

    pub async fn get_only_optimal_with_cache(
        &self,
        symbol: &str,
        volumes: &mut Vec<serde_json::Value>,
    ) -> (bool, i32, Vec<&'static str>, PairCharacteristics, bool) {
        let now_sec = chrono::Utc::now().timestamp();

        let symbol_hash: u64 = symbol.bytes().fold(5381u64, |acc, b| (acc.wrapping_shl(5)).wrapping_add(acc).wrapping_add(b as u64));
        let cache_ttl_secs = 28800 + ((symbol_hash % 14400) as i64);

        for v in volumes.iter() {
            if v.get("symbol").and_then(|s| s.as_str()) == Some(symbol) {
                if let Some(ts) = v.get("timestamp").and_then(|t| t.as_i64()) {
                    if now_sec - ts < cache_ttl_secs {
                        let vol_48h = v.get("volume_48h").and_then(|x| x.as_f64()).unwrap_or(0.0);
                        let spread = v.get("spread_pct").and_then(|x| x.as_f64()).unwrap_or(0.5);
                        let vola = v.get("volatility_pct").and_then(|x| x.as_f64()).unwrap_or(0.05);
                        let tpm = v.get("trades_per_minute").and_then(|x| x.as_f64()).unwrap_or(0.0);
                        let last_close = v.get("last_close").and_then(|x| x.as_f64()).unwrap_or(1.0);

                        let chars = PairCharacteristics {
                            volume_48h: vol_48h,
                            spread_pct: spread,
                            volatility_pct: vola,
                            trades_per_minute: tpm,
                            last_close,
                        };
                        let (is_optimal, score, reasons) = self.evaluate_pair_scoring(&chars);
                        return (is_optimal, score, reasons, chars, false);
                    }
                }
            }
        }

        let chars = match self.fetch_symbol_characteristics(symbol).await {
            Ok(c) => c,
            Err(_) => {
                let candles = Self::load_cached_candles(symbol);
                Self::compute_pair_characteristics(&candles)
            }
        };

        let mut found = false;
        for v in volumes.iter_mut() {
            if v.get("symbol").and_then(|s| s.as_str()) == Some(symbol) {
                if let Some(obj) = v.as_object_mut() {
                    obj.insert("timestamp".into(), serde_json::json!(now_sec));
                    obj.insert("volume_48h".into(), serde_json::json!(chars.volume_48h));
                    obj.insert("spread_pct".into(), serde_json::json!(chars.spread_pct));
                    obj.insert("volatility_pct".into(), serde_json::json!(chars.volatility_pct));
                    obj.insert("trades_per_minute".into(), serde_json::json!(chars.trades_per_minute));
                    obj.insert("last_close".into(), serde_json::json!(chars.last_close));
                }
                found = true;
                break;
            }
        }

        if !found {
            volumes.push(serde_json::json!({
                "symbol": symbol,
                "id": symbol.replace('/', ""),
                "timestamp": now_sec,
                "volume_48h": chars.volume_48h,
                "spread_pct": chars.spread_pct,
                "volatility_pct": chars.volatility_pct,
                "trades_per_minute": chars.trades_per_minute,
                "last_close": chars.last_close
            }));
        }

        let (is_optimal, score, reasons) = self.evaluate_pair_scoring(&chars);
        (is_optimal, score, reasons, chars, true)
    }

    pub async fn get_only_optimal(&self, symbol: &str) -> (bool, i32, Vec<&'static str>, PairCharacteristics) {
        let mut volumes = Vec::new();
        if Path::new("volumes_trades_data.json").exists() {
            if let Ok(content) = fs::read_to_string("volumes_trades_data.json") {
                if let Ok(parsed) = serde_json::from_str::<Vec<serde_json::Value>>(&content) {
                    volumes = parsed;
                }
            }
        }
        let (is_optimal, score, reasons, chars, modified) = self.get_only_optimal_with_cache(symbol, &mut volumes).await;
        if modified {
            if let Ok(json_str) = serde_json::to_string_pretty(&volumes) {
                let _ = fs::write("volumes_trades_data.json", json_str);
            }
        }
        (is_optimal, score, reasons, chars)
    }

    pub async fn filter_available_pairs(
        &mut self,
        sample_symbols: &[String],
        balance: &HashMap<String, f64>,
    ) -> Vec<String> {
        let mut sell_candidates = Vec::new();
        let mut volume_candidates: Vec<(String, i32, f64)> = Vec::new();
        let mut reasons_map: HashMap<String, String> = HashMap::new();

        let mut volumes = Vec::new();
        if Path::new("volumes_trades_data.json").exists() {
            if let Ok(content) = fs::read_to_string("volumes_trades_data.json") {
                if let Ok(parsed) = serde_json::from_str::<Vec<serde_json::Value>>(&content) {
                    volumes = parsed;
                }
            }
        }

        let mut volumes_modified = false;
        let now_sec = chrono::Utc::now().timestamp();

        let mut uncached_symbols = Vec::new();

        for sym in sample_symbols {
            if self.is_pair_redlisted(sym) {
                continue;
            }

            let base = sym.split('/').next().unwrap_or(sym);
            let quote = sym.split('/').nth(1).unwrap_or("USD");

            if self.config.forbid_assets.contains(&base.to_string()) || self.config.forbid_assets.contains(&quote.to_string()) {
                continue;
            }

            let symbol_hash: u64 = sym.bytes().fold(5381u64, |acc, b| (acc.wrapping_shl(5)).wrapping_add(acc).wrapping_add(b as u64));
            let cache_ttl_secs = 28800 + ((symbol_hash % 14400) as i64);

            let mut cached_chars = None;
            for v in &volumes {
                if v.get("symbol").and_then(|s| s.as_str()) == Some(sym) {
                    if let Some(ts) = v.get("timestamp").and_then(|t| t.as_i64()) {
                        if now_sec - ts < cache_ttl_secs {
                            let vol_48h = v.get("volume_48h").and_then(|x| x.as_f64()).unwrap_or(0.0);
                            let spread = v.get("spread_pct").and_then(|x| x.as_f64()).unwrap_or(0.5);
                            let vola = v.get("volatility_pct").and_then(|x| x.as_f64()).unwrap_or(0.05);
                            let tpm = v.get("trades_per_minute").and_then(|x| x.as_f64()).unwrap_or(0.0);
                            let last_close = v.get("last_close").and_then(|x| x.as_f64()).unwrap_or(1.0);

                            cached_chars = Some(PairCharacteristics {
                                volume_48h: vol_48h,
                                spread_pct: spread,
                                volatility_pct: vola,
                                trades_per_minute: tpm,
                                last_close,
                            });
                            break;
                        }
                    }
                }
            }

            if let Some(chars) = cached_chars {
                let (is_optimal, score, reasons) = self.evaluate_pair_scoring(&chars);
                let (_, market_min_amount) = self.get_market_precision(sym);
                let min_amount = market_min_amount.max(0.0001);
                let base_balance = balance.get(base).copied().unwrap_or(0.0);
                let has_non_dust_balance = base_balance >= min_amount || self.recorded_purchases.contains_key(base);

                let last_close = if chars.last_close > 0.0 { chars.last_close } else { 1.0 };
                let quote_eur_rate = self.get_eur_conversion_rate(quote);
                let market_min_expense_eur = min_amount * last_close * quote_eur_rate;

                if market_min_expense_eur > 12.23 && base_balance <= min_amount {
                    let _ = self.redlist_pair(sym, min_amount, last_close);
                    continue;
                }

                if has_non_dust_balance {
                    reasons_map.insert(sym.clone(), format!("Balance Inventory (Held: {:.4})", base_balance));
                    sell_candidates.push(sym.clone());
                } else if is_optimal {
                    reasons_map.insert(sym.clone(), format!("Optimal Volume (Score: {}, Vol: {:.0}, {})", score, chars.volume_48h, reasons.join(", ")));
                    volume_candidates.push((sym.clone(), score, chars.volume_48h));
                }
            } else {
                uncached_symbols.push(sym.clone());
            }
        }

        if !uncached_symbols.is_empty() {
            let exchange_arc = self.exchange.clone();
            let tasks: Vec<_> = uncached_symbols.iter().map(|sym| {
                let sym_str = sym.clone();
                let ex = exchange_arc.clone();
                async move {
                    let chars = match Self::fetch_symbol_characteristics_with_exchange(ex.as_ref(), &sym_str).await {
                        Ok(c) => c,
                        Err(_) => {
                            let candles = Self::load_cached_candles(&sym_str);
                            Self::compute_pair_characteristics(&candles)
                        }
                    };
                    (sym_str, chars)
                }
            }).collect();

            let fetched_results = futures::future::join_all(tasks).await;

            for (sym, chars) in fetched_results {
                let mut found = false;
                for v in volumes.iter_mut() {
                    if v.get("symbol").and_then(|s| s.as_str()) == Some(&sym) {
                        if let Some(obj) = v.as_object_mut() {
                            obj.insert("timestamp".into(), serde_json::json!(now_sec));
                            obj.insert("volume_48h".into(), serde_json::json!(chars.volume_48h));
                            obj.insert("spread_pct".into(), serde_json::json!(chars.spread_pct));
                            obj.insert("volatility_pct".into(), serde_json::json!(chars.volatility_pct));
                            obj.insert("trades_per_minute".into(), serde_json::json!(chars.trades_per_minute));
                            obj.insert("last_close".into(), serde_json::json!(chars.last_close));
                        }
                        found = true;
                        break;
                    }
                }

                if !found {
                    volumes.push(serde_json::json!({
                        "symbol": sym,
                        "id": sym.replace('/', ""),
                        "timestamp": now_sec,
                        "volume_48h": chars.volume_48h,
                        "spread_pct": chars.spread_pct,
                        "volatility_pct": chars.volatility_pct,
                        "trades_per_minute": chars.trades_per_minute,
                        "last_close": chars.last_close
                    }));
                }

                volumes_modified = true;

                let (is_optimal, score, reasons) = self.evaluate_pair_scoring(&chars);
                let base = sym.split('/').next().unwrap_or(&sym);
                let quote = sym.split('/').nth(1).unwrap_or("USD");
                let (_, market_min_amount) = self.get_market_precision(&sym);
                let min_amount = market_min_amount.max(0.0001);
                let base_balance = balance.get(base).copied().unwrap_or(0.0);
                let has_non_dust_balance = base_balance >= min_amount || self.recorded_purchases.contains_key(base);

                let last_close = if chars.last_close > 0.0 { chars.last_close } else { 1.0 };
                let quote_eur_rate = self.get_eur_conversion_rate(quote);
                let market_min_expense_eur = min_amount * last_close * quote_eur_rate;

                if market_min_expense_eur > 12.23 && base_balance <= min_amount {
                    let _ = self.redlist_pair(&sym, min_amount, last_close);
                    continue;
                }

                if has_non_dust_balance {
                    reasons_map.insert(sym.clone(), format!("Balance Inventory (Held: {:.4})", base_balance));
                    sell_candidates.push(sym.clone());
                } else if is_optimal {
                    reasons_map.insert(sym.clone(), format!("Optimal Volume (Score: {}, Vol: {:.0}, {})", score, chars.volume_48h, reasons.join(", ")));
                    volume_candidates.push((sym.clone(), score, chars.volume_48h));
                }
            }
        }

        if volumes_modified {
            if let Ok(json_str) = serde_json::to_string_pretty(&volumes) {
                let _ = fs::write("volumes_trades_data.json", json_str);
            }
        }

        volume_candidates.sort_by(|a, b| {
            b.1.cmp(&a.1)
                .then_with(|| b.2.partial_cmp(&a.2).unwrap_or(std::cmp::Ordering::Equal))
        });

        let mut selected = Vec::new();
        for s in sell_candidates {
            if selected.len() >= self.config.max_num_pairs {
                break;
            }
            if !selected.contains(&s) {
                selected.push(s);
            }
        }
        for (v_sym, _score, _vol) in volume_candidates {
            if selected.len() >= self.config.max_num_pairs {
                break;
            }
            if !selected.contains(&v_sym) {
                selected.push(v_sym);
            }
        }

        self.previous_selected_pairs = selected.clone();
        selected
    }

    pub async fn fetch_pair_candles(&self, symbol: &str) -> Result<Vec<Candle>> {
        let sanitized = symbol.replace('/', "");
        let cache_file = format!("ohlcv_data_{}_1m.json", sanitized);

        let mut cached_candles: Vec<Candle> = Vec::new();
        if Path::new(&cache_file).exists() {
            if let Ok(content) = fs::read_to_string(&cache_file) {
                if let Ok(parsed) = serde_json::from_str::<Vec<Candle>>(&content) {
                    cached_candles = parsed;
                }
            }
        }

        let last_ts = cached_candles.last().map(|c| c.timestamp);
        let fresh_candles = self.exchange.fetch_ohlcv(symbol, "1m", 500, last_ts).await?;

        let mut candle_map: HashMap<i64, Candle> = HashMap::new();
        for c in cached_candles {
            candle_map.insert(c.timestamp, c);
        }
        for c in fresh_candles {
            candle_map.insert(c.timestamp, c);
        }

        let mut merged_candles: Vec<Candle> = candle_map.into_values().collect();
        merged_candles.sort_by_key(|c| c.timestamp);

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

    pub fn get_market_precision(&self, symbol: &str) -> (f64, f64) {
        if Path::new("markets.json").exists() {
            if let Ok(content) = fs::read_to_string("markets.json") {
                if let Ok(markets) = serde_json::from_str::<serde_json::Value>(&content) {
                    if let Some(entry) = markets.get(symbol) {
                        let price_prec = entry
                            .get("precision")
                            .and_then(|p| p.get("price"))
                            .and_then(|v| v.as_f64())
                            .unwrap_or(0.0001);
                        let amount_prec = entry
                            .get("precision")
                            .and_then(|p| p.get("amount"))
                            .and_then(|v| v.as_f64())
                            .unwrap_or(0.0001);
                        return (price_prec, amount_prec);
                    }
                }
            }
        }
        (0.0001, 0.0001)
    }

    pub fn get_market_min_amount(&self, symbol: &str) -> f64 {
        if Path::new("markets.json").exists() {
            if let Ok(content) = fs::read_to_string("markets.json") {
                if let Ok(markets) = serde_json::from_str::<serde_json::Value>(&content) {
                    if let Some(entry) = markets.get(symbol) {
                        if let Some(min_val) = entry
                            .get("limits")
                            .and_then(|l| l.get("amount"))
                            .and_then(|a| a.get("min"))
                            .and_then(|v| v.as_f64())
                        {
                            return min_val;
                        }
                    }
                }
            }
        }
        0.0001
    }

    pub fn round_to_precision(&self, val: f64, precision: f64) -> f64 {
        if precision <= 0.0 || !val.is_finite() {
            return val;
        }
        let decimals = if precision >= 1.0 {
            precision as i32
        } else {
            (-precision.log10()).round() as i32
        };

        if decimals <= 0 {
            val.round()
        } else {
            let factor = 10.0_f64.powi(decimals);
            (val * factor).round() / factor
        }
    }

    pub fn round_down_to_precision(&self, val: f64, precision: f64) -> f64 {
        if precision <= 0.0 || !val.is_finite() {
            return val;
        }
        let decimals = if precision >= 1.0 {
            precision as i32
        } else {
            (-precision.log10()).round() as i32
        };

        if decimals <= 0 {
            val.floor()
        } else {
            let factor = 10.0_f64.powi(decimals);
            (val * factor).floor() / factor
        }
    }

    pub fn calculate_package_amount(&self, price: f64, quote_eur_rate: f64, min_amount: f64, amount_precision: f64) -> f64 {
        if price <= 0.0 || quote_eur_rate <= 0.0 {
            return min_amount;
        }

        let min_amount_to_use = min_amount.max(5.07 / (price * quote_eur_rate));
        let max_amount_limit = 12.23 / (price * quote_eur_rate);

        let desired_amount = min_amount_to_use * 1.1;
        let final_amount = desired_amount.min(max_amount_limit).max(min_amount_to_use);

        self.round_down_to_precision(final_amount, amount_precision)
    }

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
        info!("[{}] Recorded purchase of {} at price {}", symbol, amount, price);
        Ok(())
    }

    pub fn remove_recorded_purchases(&mut self, symbol: &str) -> Result<()> {
        let base_asset = symbol.split('/').next().unwrap_or(symbol);
        let mut to_clear = Vec::new();

        for s in self.recorded_purchases.keys() {
            let s_base = s.split('/').next().unwrap_or(s);
            if s_base == base_asset {
                to_clear.push(s.clone());
            }
        }

        for s in to_clear {
            self.recorded_purchases.insert(s.clone(), Vec::new());
        }

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
        info!("[{}] Deleted all recorded purchases for base asset {}", symbol, base_asset);
        Ok(())
    }

    pub fn is_sell_profitable(&self, symbol: &str, sell_price: f64) -> (bool, String) {
        let base_asset = symbol.split('/').next().unwrap_or(symbol);
        let current_quote = symbol.split('/').nth(1).unwrap_or("USD");

        let mut total_amount = 0.0;
        let mut weighted_sum = 0.0;

        for (s, purchases) in &self.recorded_purchases {
            let s_base = s.split('/').next().unwrap_or(s);
            if s_base == base_asset {
                let p_quote = s.split('/').nth(1).unwrap_or("USD");
                let conversion_rate = if p_quote == current_quote {
                    1.0
                } else {
                    let rate_p = self.get_eur_conversion_rate(p_quote);
                    let rate_c = self.get_eur_conversion_rate(current_quote);
                    if rate_c > 0.0 { rate_p / rate_c } else { 1.0 }
                };

                for p in purchases {
                    let converted_price = p.price * conversion_rate;
                    total_amount += p.amount;
                    weighted_sum += p.amount * converted_price;
                }
            }
        }

        if total_amount <= 0.0 {
            return (true, "No remaining recorded purchase amount, allowing sell by default.".into());
        }

        let avg_purchase_price = weighted_sum / total_amount;
        let fee_rate = self.config.default_fee;
        let min_profit = if self.is_pair_unscored(symbol) { 0.00202 } else { self.config.min_profit_margin };
        let min_exit_price = avg_purchase_price * (1.0 + fee_rate) * (1.0 + min_profit) / (1.0 - fee_rate);
        let profitable = sell_price >= min_exit_price;
        let details = format!(
            "Sell Price: {:.8} vs Min Exit Price (converted to {}, fee: {:.4}, min profit: {:.5}): {:.8} (Avg Purchase Price: {:.8}, Remaining Amount: {:.6})",
            sell_price, current_quote, fee_rate, min_profit, min_exit_price, avg_purchase_price, total_amount
        );

        (profitable, details)
    }

    pub fn evaluate_symbol_parallel(
        &self,
        symbol: &str,
        candles: &[Candle],
        candles_4h: Option<&[Candle]>,
        last_close: f64,
    ) -> Option<(Signal, f64, f64)> {
        let calibrated_window = TechnicalAnalysis::calibrate_window_by_non_repetition(candles, 480, 1e-5);
        if candles.len() < calibrated_window {
            return None;
        }

        let active_candles = &candles[candles.len() - calibrated_window..];

        let sma_840 = TechnicalAnalysis::calculate_5_week_sma(candles, candles_4h);
        let is_bullish = if let Some(sma) = sma_840 {
            last_close > sma
        } else {
            true
        };

        let adx_val = TechnicalAnalysis::calculate_adx(active_candles, 14).unwrap_or(20.0);
        let is_trend_following = adx_val > 25.0;

        let base_buy_offset = if is_trend_following {
            if is_bullish { 0.0006 } else { 0.0020 }
        } else {
            if is_bullish { 0.0012 } else { 0.0010 }
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

        let num_peaks = TechnicalAnalysis::count_peaks(candles, 840);
        let is_crest_high = if num_peaks <= 3 {
            if let Some(sma) = sma_840 {
                last_close > sma || target_buy_price > sma
            } else {
                false
            }
        } else {
            false
        };

        let mut volatility = 0.0;
        let mut drift = 0.0;
        if active_candles.len() > 1 {
            let returns: Vec<f64> = active_candles
                .windows(2)
                .map(|w| (w[1].close / w[0].close).ln())
                .filter(|v| v.is_finite())
                .collect();
            if returns.len() > 1 {
                let mean = returns.iter().sum::<f64>() / returns.len() as f64;
                let var = returns.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / returns.len() as f64;
                volatility = var.sqrt();
                drift = mean;
            }
        }

        let buy_prob = mc_engine.estimate_hit_probability(last_close, target_buy_price, volatility, drift, "below");
        let sell_prob = mc_engine.estimate_hit_probability(last_close, target_sell_price, volatility, drift, "above");

        let signal_res = StrategyAggregator::aggregate(active_candles, &self.config);

        let (mut is_buy, mut is_sell) = (signal_res.signal == Signal::Buy, signal_res.signal == Signal::Sell);

        if is_buy && is_sell {
            if buy_prob >= sell_prob {
                is_sell = false;
            } else {
                is_buy = false;
            }
        }

        let threshold = self.config.monte_carlo.sufficient_probability;

        if is_buy {
            let buy_threshold = 0.73;
            if is_crest_high || buy_prob < buy_threshold {
                None
            } else {
                Some((Signal::Buy, target_buy_price, buy_prob))
            }
        } else if is_sell {
            if sell_prob < threshold {
                None
            } else {
                Some((Signal::Sell, target_sell_price, sell_prob))
            }
        } else {
            None
        }
    }

    pub fn redlist_pair(&mut self, symbol: &str, min_amount: f64, last_close: f64) -> Result<()> {
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

    pub async fn run(&mut self) -> Result<()> {
        info!("Trading engine started in mode: {:?}", self.config.mode);

        loop {
            let markets_json = self.exchange.fetch_markets().await.unwrap_or_default();
            if let Ok(json_str) = serde_json::to_string_pretty(&markets_json) {
                let _ = fs::write("markets.json", json_str);
            }

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

                    let norm_k = crate::exchange::normalize_kraken_symbol(k);
                    if val > 0.0 && !self.is_dust_balance(&norm_k, val, Some(&markets_json)) {
                        balance_map.insert(norm_k, val);
                    }
                }
            }

            if let Ok(json_str) = serde_json::to_string_pretty(&balance_json) {
                let _ = fs::write("balance.json", json_str);
            }

            let raw_symbols = self.load_market_symbols(&balance_map, &markets_json);
            let available_pairs = self.filter_available_pairs(&raw_symbols, &balance_map).await;
            info!("Running trading loop for {} pairs...", available_pairs.len());

            for sym in &available_pairs {
                if self.is_pair_redlisted(sym) {
                    continue;
                }

                let candles = match self.fetch_pair_candles(sym).await {
                    Ok(c) if !c.is_empty() => c,
                    _ => continue,
                };
                let candles_4h = self.fetch_pair_candles_4h(sym).await.ok();
                let last_close = candles.last().map(|c| c.close).unwrap_or(50000.0);

                let eval = self.evaluate_symbol_parallel(sym, &candles, candles_4h.as_deref(), last_close);

                if let Some((signal, target_price, prob)) = eval {
                    info!("[Trading Loop] Signal {:?} for {} at price {} with probability {:.4}", signal, sym, target_price, prob);
                }
            }

            tokio::time::sleep(Duration::from_secs(4)).await;
        }
    }

    pub async fn run_backtest(&mut self) -> Result<()> {
        info!("==================================================");
        info!("          BOTV6 BACKTEST SIMULATION ENGINE        ");
        info!("==================================================");

        let balance_json = self.exchange.fetch_balance().await.unwrap_or_else(|_| {
            serde_json::json!({
                "free": { "USD": 10000.0, "EUR": 10000.0 },
                "total": { "USD": 10000.0, "EUR": 10000.0 }
            })
        });

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

                if val >= 0.000001 {
                    balance_map.insert(k.clone(), val);
                }
            }
        }

        let markets_json = self.exchange.fetch_markets().await.unwrap_or_default();
        let sample_pairs = self.load_market_symbols(&balance_map, &markets_json);
        let selected_pairs = self.filter_available_pairs(&sample_pairs, &balance_map).await;

        info!("[Backtest] Running backtest across {} selected trading pairs...", selected_pairs.len());

        let mut total_simulated_trades = 0;
        let mut winning_trades = 0;
        let mut total_profit_usd = 0.0;
        let mut total_loss_usd = 0.0;
        let mut initial_balance = 10000.0;
        let mut current_balance = initial_balance;

        for symbol in &selected_pairs {
            let candles = match self.fetch_pair_candles(symbol).await {
                Ok(c) if !c.is_empty() => c,
                _ => continue,
            };

            let calibrated_window = TechnicalAnalysis::calibrate_window_by_non_repetition(&candles, 480, 1e-5);
            let active_candles = if candles.len() > calibrated_window {
                &candles[candles.len() - calibrated_window..]
            } else {
                &candles[..]
            };

            if active_candles.len() < 50 {
                continue;
            }

            let mut in_position = false;
            let mut entry_price = 0.0;
            let mut entry_amount = 0.0;

            for i in 50..active_candles.len() {
                let window = &active_candles[..=i];
                let last_close = active_candles[i].close;

                let signal_res = StrategyAggregator::aggregate(window, &self.config);

                if !in_position && signal_res.signal == Signal::Buy {
                    entry_price = last_close * signal_res.buy_multiplier;
                    entry_amount = 100.0 / entry_price;
                    if current_balance >= 100.0 {
                        current_balance -= 100.0;
                        in_position = true;
                        total_simulated_trades += 1;
                    }
                } else if in_position {
                    let exit_price = last_close * signal_res.sell_multiplier;
                    if signal_res.signal == Signal::Sell || i == active_candles.len() - 1 {
                        let revenue = exit_price * entry_amount;
                        let pnl = revenue - 100.0;
                        current_balance += revenue;
                        in_position = false;

                        if pnl > 0.0 {
                            winning_trades += 1;
                            total_profit_usd += pnl;
                        } else {
                            total_loss_usd += pnl.abs();
                        }
                    }
                }
            }
        }

        let total_pnl = current_balance - initial_balance;
        let win_rate = if total_simulated_trades > 0 {
            (winning_trades as f64 / total_simulated_trades as f64) * 100.0
        } else {
            0.0
        };

        info!("==================================================");
        info!("          BOTV6 BACKTEST RESULTS SUMMARY          ");
        info!("==================================================");
        info!("Initial Portfolio Balance: ${:.2}", initial_balance);
        info!("Final Portfolio Balance:   ${:.2}", current_balance);
        info!("Total Return (PnL):        ${:+.2}", total_pnl);
        info!("Total Executed Trades:     {}", total_simulated_trades);
        info!("Win Rate:                  {:.1}%", win_rate);
        info!("==================================================");

        Ok(())
    }
}

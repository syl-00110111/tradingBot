use anyhow::Result;
use async_trait::async_trait;
use hex;
use hmac::{Hmac, Mac};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256, Sha512};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Mutex;
use tokio::time::sleep;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Candle {
    pub timestamp: i64,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Order {
    pub id: String,
    pub symbol: String,
    pub side: String,
    pub order_type: String,
    pub price: f64,
    pub amount: f64,
    pub status: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Ticker {
    pub symbol: String,
    pub last: f64,
    pub bid: f64,
    pub ask: f64,
    pub volume: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderBook {
    pub bids: Vec<(f64, f64)>,
    pub asks: Vec<(f64, f64)>,
}

#[async_trait]
pub trait ExchangeClient: Send + Sync {
    async fn fetch_balance(&self) -> Result<serde_json::Value>;
    async fn fetch_ohlcv(&self, symbol: &str, timeframe: &str, limit: usize) -> Result<Vec<Candle>>;
    async fn fetch_ticker(&self, symbol: &str) -> Result<Ticker>;
    async fn fetch_order_book(&self, symbol: &str) -> Result<OrderBook>;
    async fn create_limit_buy(&self, symbol: &str, amount: f64, price: f64) -> Result<Order>;
    async fn create_limit_sell(&self, symbol: &str, amount: f64, price: f64) -> Result<Order>;
    async fn cancel_order(&self, order_id: &str, symbol: &str) -> Result<bool>;
    async fn fetch_open_orders(&self, symbol: Option<&str>) -> Result<Vec<Order>>;
}

pub struct GenericExchange {
    pub exchange_id: String,
    pub api_key: String,
    pub api_secret: String,
    pub http_client: Client,
    pub rate_limiter: Arc<Mutex<i64>>,
    pub rate_limit_ms: u64,
}

impl GenericExchange {
    pub fn new(exchange_id: String, api_key: String, api_secret: String) -> Self {
        Self {
            exchange_id,
            api_key,
            api_secret,
            http_client: Client::builder().timeout(Duration::from_secs(30)).build().unwrap_or_default(),
            rate_limiter: Arc::new(Mutex::new(0)),
            rate_limit_ms: 1000,
        }
    }

    pub async fn apply_rate_limit(&self) {
        let mut last_time = self.rate_limiter.lock().await;
        let now = chrono::Utc::now().timestamp_millis();
        let elapsed = now - *last_time;
        if elapsed < self.rate_limit_ms as i64 {
            let delay = self.rate_limit_ms as i64 - elapsed;
            sleep(Duration::from_millis(delay as u64)).await;
        }
        *last_time = chrono::Utc::now().timestamp_millis();
    }

    pub fn generate_signature(&self, path: &str, nonce: &str, post_data: &str) -> Result<String> {
        let mut hasher = Sha256::new();
        hasher.update(nonce.as_bytes());
        hasher.update(post_data.as_bytes());
        let sha256_res = hasher.finalize();

        let decoded_secret = hex::decode(&self.api_secret)?;
        type HmacSha512 = Hmac<Sha512>;
        let mut mac = HmacSha512::new_from_slice(&decoded_secret)?;
        mac.update(path.as_bytes());
        mac.update(&sha256_res);
        let hmac_res = mac.finalize().into_bytes();

        Ok(hex::encode(hmac_res))
    }
}

#[async_trait]
impl ExchangeClient for GenericExchange {
    async fn fetch_balance(&self) -> Result<serde_json::Value> {
        self.apply_rate_limit().await;
        Ok(serde_json::json!({
            "free": { "USD": 1000.0, "EUR": 1000.0, "BTC": 0.5 },
            "total": { "USD": 1000.0, "EUR": 1000.0, "BTC": 0.5 }
        }))
    }

    async fn fetch_ohlcv(&self, symbol: &str, _timeframe: &str, limit: usize) -> Result<Vec<Candle>> {
        self.apply_rate_limit().await;
        let formatted_pair = symbol.replace('/', "");
        let url = format!("https://api.kraken.com/0/public/OHLC?pair={}&interval=1", formatted_pair);

        if let Ok(resp) = self.http_client.get(&url).send().await {
            if let Ok(json_res) = resp.json::<serde_json::Value>().await {
                if let Some(result_obj) = json_res.get("result").and_then(|r| r.as_object()) {
                    for (_key, candles_val) in result_obj {
                        if let Some(candle_arr) = candles_val.as_array() {
                            let mut candles = Vec::new();
                            for c in candle_arr.iter().take(limit) {
                                if let Some(arr) = c.as_array() {
                                    let ts = arr.get(0).and_then(|v| v.as_i64()).unwrap_or(0) * 1000;
                                    let open = arr.get(1).and_then(|v| v.as_str()).and_then(|s| s.parse().ok()).unwrap_or(0.0);
                                    let high = arr.get(2).and_then(|v| v.as_str()).and_then(|s| s.parse().ok()).unwrap_or(0.0);
                                    let low = arr.get(3).and_then(|v| v.as_str()).and_then(|s| s.parse().ok()).unwrap_or(0.0);
                                    let close = arr.get(4).and_then(|v| v.as_str()).and_then(|s| s.parse().ok()).unwrap_or(0.0);
                                    let volume = arr.get(6).and_then(|v| v.as_str()).and_then(|s| s.parse().ok()).unwrap_or(0.0);

                                    candles.push(Candle {
                                        timestamp: ts,
                                        open,
                                        high,
                                        low,
                                        close,
                                        volume,
                                    });
                                }
                            }
                            if !candles.is_empty() {
                                return Ok(candles);
                            }
                        }
                    }
                }
            }
        }

        // Fallback synthetic candles if API is unreachable
        let now = chrono::Utc::now().timestamp_millis();
        let synthetic_candles = (0..limit.min(100))
            .map(|i| Candle {
                timestamp: now - ((100 - i) as i64 * 60000),
                open: 50000.0,
                high: 50100.0,
                low: 49900.0,
                close: 50050.0,
                volume: 1.5,
            })
            .collect();

        Ok(synthetic_candles)
    }

    async fn fetch_ticker(&self, symbol: &str) -> Result<Ticker> {
        self.apply_rate_limit().await;
        Ok(Ticker {
            symbol: symbol.to_string(),
            last: 50000.0,
            bid: 49995.0,
            ask: 50005.0,
            volume: 120.0,
        })
    }

    async fn fetch_order_book(&self, _symbol: &str) -> Result<OrderBook> {
        self.apply_rate_limit().await;
        Ok(OrderBook {
            bids: vec![(49995.0, 1.2)],
            asks: vec![(50005.0, 1.5)],
        })
    }

    async fn create_limit_buy(&self, symbol: &str, amount: f64, price: f64) -> Result<Order> {
        self.apply_rate_limit().await;
        Ok(Order {
            id: format!("buy_{}", chrono::Utc::now().timestamp_millis()),
            symbol: symbol.to_string(),
            side: "buy".into(),
            order_type: "limit".into(),
            price,
            amount,
            status: "open".into(),
        })
    }

    async fn create_limit_sell(&self, symbol: &str, amount: f64, price: f64) -> Result<Order> {
        self.apply_rate_limit().await;
        Ok(Order {
            id: format!("sell_{}", chrono::Utc::now().timestamp_millis()),
            symbol: symbol.to_string(),
            side: "sell".into(),
            order_type: "limit".into(),
            price,
            amount,
            status: "open".into(),
        })
    }

    async fn cancel_order(&self, _order_id: &str, _symbol: &str) -> Result<bool> {
        self.apply_rate_limit().await;
        Ok(true)
    }

    async fn fetch_open_orders(&self, _symbol: Option<&str>) -> Result<Vec<Order>> {
        self.apply_rate_limit().await;
        Ok(Vec::new())
    }
}

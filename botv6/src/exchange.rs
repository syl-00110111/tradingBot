use anyhow::{anyhow, Result};
use async_trait::async_trait;
use hmac::{Hmac, Mac};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256, Sha512};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::Mutex;
use tokio::time::{sleep, Duration};

type HmacSha512 = Hmac<Sha512>;

pub fn normalize_kraken_symbol(symbol: &str) -> String {
    let clean = symbol.trim().to_uppercase();
    match clean.as_str() {
        "ZEUR" | "EUR" => "EUR".to_string(),
        "ZUSD" | "USD" => "USD".to_string(),
        "ZGBP" | "GBP" => "GBP".to_string(),
        "ZAUD" | "AUD" => "AUD".to_string(),
        "ZCAD" | "CAD" => "CAD".to_string(),
        "ZJPY" | "JPY" => "JPY".to_string(),
        "XXBT" | "XBT" | "BTC" => "BTC".to_string(),
        "XETH" | "ETH" => "ETH".to_string(),
        _ => {
            if clean.starts_with('Z') && clean.len() == 4 {
                clean[1..].to_string()
            } else if clean.starts_with('X') && clean.len() == 4 && clean != "XMR" {
                clean[1..].to_string()
            } else {
                clean
            }
        }
    }
}

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
pub struct Ticker {
    pub symbol: String,
    pub bid: f64,
    pub ask: f64,
    pub last: f64,
    pub volume: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Trade {
    pub id: String,
    pub timestamp: i64,
    pub price: f64,
    pub amount: f64,
    pub side: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderBookEntry {
    pub price: f64,
    pub amount: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderBook {
    pub bids: Vec<OrderBookEntry>,
    pub asks: Vec<OrderBookEntry>,
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
    pub timestamp: i64,
}

#[async_trait]
pub trait ExchangeClient: Send + Sync {
    async fn fetch_markets(&self) -> Result<serde_json::Value>;
    async fn fetch_ohlcv(&self, symbol: &str, timeframe: &str, limit: usize, since: Option<i64>) -> Result<Vec<Candle>>;
    async fn fetch_ticker(&self, symbol: &str) -> Result<Ticker>;
    async fn fetch_trades(&self, symbol: &str, limit: usize) -> Result<Vec<Trade>>;
    async fn fetch_order_book(&self, symbol: &str, limit: usize) -> Result<OrderBook>;
    async fn fetch_balance(&self) -> Result<serde_json::Value>;
    async fn fetch_open_orders(&self, symbol: Option<&str>) -> Result<Vec<Order>>;
    async fn create_limit_buy(&self, symbol: &str, amount: f64, price: f64) -> Result<Order>;
    async fn create_limit_sell(&self, symbol: &str, amount: f64, price: f64) -> Result<Order>;
    async fn cancel_order(&self, order_id: &str, symbol: &str) -> Result<bool>;
}

pub struct GenericExchange {
    pub exchange_id: String,
    pub api_key: String,
    pub api_secret: String,
    pub client: Client,
    pub rate_limiter: Arc<Mutex<i64>>,
}

impl GenericExchange {
    pub fn new(exchange_id: String, api_key: String, api_secret: String) -> Self {
        Self {
            exchange_id,
            api_key,
            api_secret,
            client: Client::builder().timeout(Duration::from_secs(15)).build().unwrap(),
            rate_limiter: Arc::new(Mutex::new(0)),
        }
    }

    async fn apply_custom_rate_limit(&self, delay_ms: u64) {
        let mut last_call = self.rate_limiter.lock().await;
        let now = chrono::Utc::now().timestamp_millis();
        let elapsed = now - *last_call;
        if elapsed < delay_ms as i64 {
            sleep(Duration::from_millis(delay_ms - elapsed as u64)).await;
        }
        *last_call = chrono::Utc::now().timestamp_millis();
    }

    async fn apply_rate_limit(&self) {
        self.apply_custom_rate_limit(1000).await;
    }

    fn build_kraken_signature(&self, path: &str, nonce: &str, post_data: &str) -> Result<String> {
        let secret_bytes = base64::Engine::decode(&base64::engine::general_purpose::STANDARD, &self.api_secret)
            .map_err(|e| anyhow!("Base64 secret decode error: {}", e))?;

        let mut sha256_hasher = Sha256::new();
        sha256_hasher.update(nonce.as_bytes());
        sha256_hasher.update(post_data.as_bytes());
        let sha256_res = sha256_hasher.finalize();

        let mut mac = HmacSha512::new_from_slice(&secret_bytes)
            .map_err(|e| anyhow!("HMAC init error: {}", e))?;
        mac.update(path.as_bytes());
        mac.update(&sha256_res);

        Ok(base64::Engine::encode(&base64::engine::general_purpose::STANDARD, mac.finalize().into_bytes()))
    }

    async fn send_private_request(&self, endpoint: &str, mut params: HashMap<String, String>) -> Result<serde_json::Value> {
        self.apply_rate_limit().await;

        let path = format!("/0/private/{}", endpoint);
        let url = format!("https://api.kraken.com{}", path);

        let nonce = chrono::Utc::now().timestamp_millis().to_string();
        params.insert("nonce".to_string(), nonce.clone());

        let post_data: String = params.iter().map(|(k, v)| format!("{}={}", k, v)).collect::<Vec<_>>().join("&");

        let signature = self.build_kraken_signature(&path, &nonce, &post_data)?;

        let response = self
            .client
            .post(&url)
            .header("API-Key", &self.api_key)
            .header("API-Sign", signature)
            .header("Content-Type", "application/x-www-form-urlencoded")
            .body(post_data)
            .send()
            .await?;

        let json_val: serde_json::Value = response.json().await?;

        if let Some(errors) = json_val.get("error").and_then(|e| e.as_array()) {
            if !errors.is_empty() {
                let err_msgs: Vec<String> = errors.iter().filter_map(|v| v.as_str().map(|s| s.to_string())).collect();
                return Err(anyhow!("Kraken API Error: {}", err_msgs.join(", ")));
            }
        }

        Ok(json_val.get("result").cloned().unwrap_or(json_val))
    }

    fn to_kraken_pair(&self, symbol: &str) -> String {
        symbol.replace('/', "")
    }
}

#[async_trait]
impl ExchangeClient for GenericExchange {
    async fn fetch_markets(&self) -> Result<serde_json::Value> {
        self.apply_custom_rate_limit(250).await;
        let url = "https://api.kraken.com/0/public/AssetPairs";
        let resp = self.client.get(url).send().await?;
        let json_val: serde_json::Value = resp.json().await?;

        let mut normalized_markets = serde_json::Map::new();

        if let Some(result) = json_val.get("result").and_then(|r| r.as_object()) {
            for (pair_id, item) in result {
                if pair_id.ends_with(".d") {
                    continue;
                }

                let wsname = item.get("wsname").and_then(|v| v.as_str());
                let raw_base = item.get("base").and_then(|v| v.as_str()).unwrap_or("");
                let raw_quote = item.get("quote").and_then(|v| v.as_str()).unwrap_or("");

                let base_norm = normalize_kraken_symbol(raw_base);
                let quote_norm = normalize_kraken_symbol(raw_quote);

                let symbol = wsname
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| format!("{}/{}", base_norm, quote_norm));

                let price_precision = item.get("pair_decimals").and_then(|v| v.as_i64()).unwrap_or(4);
                let amount_precision = item.get("lot_decimals").and_then(|v| v.as_i64()).unwrap_or(8);

                let min_amount = item
                    .get("ordermin")
                    .and_then(|v| v.as_str())
                    .and_then(|s| s.parse::<f64>().ok())
                    .unwrap_or(0.0001);

                let fee_rate = item
                    .get("fees")
                    .and_then(|f| f.as_array())
                    .and_then(|arr| arr.first())
                    .and_then(|entry| entry.get(1))
                    .and_then(|v| v.as_f64())
                    .map(|pct| pct / 100.0)
                    .unwrap_or(0.001);

                let market_entry = serde_json::json!({
                    "id": pair_id,
                    "symbol": symbol,
                    "base": base_norm,
                    "quote": quote_norm,
                    "active": true,
                    "precision": {
                        "price": 10.0_f64.powi(-(price_precision as i32)),
                        "amount": 10.0_f64.powi(-(amount_precision as i32))
                    },
                    "limits": {
                        "amount": { "min": min_amount }
                    },
                    "fee": fee_rate,
                    "info": item
                });

                normalized_markets.insert(symbol, market_entry);
            }
        }

        Ok(serde_json::Value::Object(normalized_markets))
    }

    async fn fetch_ohlcv(&self, symbol: &str, timeframe: &str, limit: usize, since: Option<i64>) -> Result<Vec<Candle>> {
        self.apply_custom_rate_limit(250).await;
        let kraken_pair = self.to_kraken_pair(symbol);
        let interval = match timeframe {
            "1m" => 1,
            "5m" => 5,
            "15m" => 15,
            "1h" => 60,
            "4h" => 240,
            "1d" => 1440,
            _ => 1,
        };

        let mut url = format!(
            "https://api.kraken.com/0/public/OHLC?pair={}&interval={}",
            kraken_pair, interval
        );
        if let Some(ts) = since {
            url.push_str(&format!("&since={}", ts / 1000));
        }

        let resp = self.client.get(&url).send().await?;
        let json_val: serde_json::Value = resp.json().await?;

        let mut candles = Vec::new();
        if let Some(result) = json_val.get("result").and_then(|r| r.as_object()) {
            for (key, val) in result {
                if key != "last" {
                    if let Some(arr) = val.as_array() {
                        for c in arr.iter().take(limit) {
                            if let Some(c_arr) = c.as_array() {
                                if c_arr.len() >= 6 {
                                    let ts = c_arr[0].as_i64().unwrap_or(0) * 1000;
                                    let open = c_arr[1].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                                    let high = c_arr[2].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                                    let low = c_arr[3].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                                    let close = c_arr[4].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                                    let volume = c_arr[6].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0);

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
                        }
                    }
                }
            }
        }

        Ok(candles)
    }

    async fn fetch_ticker(&self, symbol: &str) -> Result<Ticker> {
        self.apply_custom_rate_limit(250).await;
        let kraken_pair = self.to_kraken_pair(symbol);
        let url = format!("https://api.kraken.com/0/public/Ticker?pair={}", kraken_pair);

        let resp = self.client.get(&url).send().await?;
        let json_val: serde_json::Value = resp.json().await?;

        if let Some(result) = json_val.get("result").and_then(|r| r.as_object()) {
            if let Some((_key, val)) = result.iter().next() {
                let ask = val.get("a").and_then(|a| a.get(0)).and_then(|v| v.as_str()).unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                let bid = val.get("b").and_then(|b| b.get(0)).and_then(|v| v.as_str()).unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                let last = val.get("c").and_then(|c| c.get(0)).and_then(|v| v.as_str()).unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                let volume = val.get("v").and_then(|v| v.get(1)).and_then(|v| v.as_str()).unwrap_or("0").parse::<f64>().unwrap_or(0.0);

                return Ok(Ticker {
                    symbol: symbol.to_string(),
                    bid,
                    ask,
                    last,
                    volume,
                });
            }
        }

        Err(anyhow!("Failed to parse ticker for {}", symbol))
    }

    async fn fetch_trades(&self, symbol: &str, limit: usize) -> Result<Vec<Trade>> {
        self.apply_custom_rate_limit(250).await;
        let kraken_pair = self.to_kraken_pair(symbol);
        let url = format!("https://api.kraken.com/0/public/Trades?pair={}", kraken_pair);

        let resp = self.client.get(&url).send().await?;
        let json_val: serde_json::Value = resp.json().await?;

        let mut trades = Vec::new();
        if let Some(result) = json_val.get("result").and_then(|r| r.as_object()) {
            for (key, val) in result {
                if key != "last" {
                    if let Some(arr) = val.as_array() {
                        for t in arr.iter().take(limit) {
                            if let Some(t_arr) = t.as_array() {
                                if t_arr.len() >= 4 {
                                    let price = t_arr[0].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                                    let amount = t_arr[1].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                                    let ts = (t_arr[2].as_f64().unwrap_or(0.0) * 1000.0) as i64;
                                    let side = t_arr[3].as_str().unwrap_or("b").to_string();

                                    trades.push(Trade {
                                        id: ts.to_string(),
                                        timestamp: ts,
                                        price,
                                        amount,
                                        side,
                                    });
                                }
                            }
                        }
                    }
                }
            }
        }

        Ok(trades)
    }

    async fn fetch_order_book(&self, symbol: &str, limit: usize) -> Result<OrderBook> {
        self.apply_custom_rate_limit(250).await;
        let kraken_pair = self.to_kraken_pair(symbol);
        let url = format!(
            "https://api.kraken.com/0/public/Depth?pair={}&count={}",
            kraken_pair, limit
        );

        let resp = self.client.get(&url).send().await?;
        let json_val: serde_json::Value = resp.json().await?;

        let mut bids = Vec::new();
        let mut asks = Vec::new();

        if let Some(result) = json_val.get("result").and_then(|r| r.as_object()) {
            if let Some((_key, val)) = result.iter().next() {
                if let Some(b_arr) = val.get("bids").and_then(|b| b.as_array()) {
                    for entry in b_arr {
                        if let Some(e) = entry.as_array() {
                            let price = e[0].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                            let amount = e[1].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                            bids.push(OrderBookEntry { price, amount });
                        }
                    }
                }
                if let Some(a_arr) = val.get("asks").and_then(|a| a.as_array()) {
                    for entry in a_arr {
                        if let Some(e) = entry.as_array() {
                            let price = e[0].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                            let amount = e[1].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                            asks.push(OrderBookEntry { price, amount });
                        }
                    }
                }
            }
        }

        Ok(OrderBook { bids, asks })
    }

    async fn fetch_balance(&self) -> Result<serde_json::Value> {
        if self.api_key.is_empty() || self.api_secret.is_empty() {
            return Ok(serde_json::json!({
                "free": { "USD": 10000.0, "EUR": 10000.0 },
                "total": { "USD": 10000.0, "EUR": 10000.0 }
            }));
        }

        let res = self.send_private_request("Balance", HashMap::new()).await?;

        let mut free_balances = serde_json::Map::new();
        if let Some(obj) = res.as_object() {
            for (asset, val) in obj {
                let norm_asset = normalize_kraken_symbol(asset);
                free_balances.insert(norm_asset, val.clone());
            }
        }

        Ok(serde_json::json!({
            "free": free_balances,
            "total": free_balances
        }))
    }

    async fn fetch_open_orders(&self, _symbol: Option<&str>) -> Result<Vec<Order>> {
        if self.api_key.is_empty() || self.api_secret.is_empty() {
            return Ok(Vec::new());
        }

        let res = self.send_private_request("OpenOrders", HashMap::new()).await?;

        let mut orders = Vec::new();
        if let Some(open_obj) = res.get("open").and_then(|o| o.as_object()) {
            for (order_id, val) in open_obj {
                if let Some(descr) = val.get("descr") {
                    let pair = descr.get("pair").and_then(|v| v.as_str()).unwrap_or("");
                    let side = descr.get("type").and_then(|v| v.as_str()).unwrap_or("buy");
                    let price = descr.get("price").and_then(|v| v.as_str()).unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                    let amount = val.get("vol").and_then(|v| v.as_str()).unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                    let status = val.get("status").and_then(|v| v.as_str()).unwrap_or("open");

                    orders.push(Order {
                        id: order_id.clone(),
                        symbol: pair.to_string(),
                        side: side.to_string(),
                        order_type: "limit".to_string(),
                        price,
                        amount,
                        status: status.to_string(),
                        timestamp: chrono::Utc::now().timestamp(),
                    });
                }
            }
        }

        Ok(orders)
    }

    async fn create_limit_buy(&self, symbol: &str, amount: f64, price: f64) -> Result<Order> {
        if self.api_key.is_empty() || self.api_secret.is_empty() {
            return Ok(Order {
                id: format!("sim_buy_{}", chrono::Utc::now().timestamp_millis()),
                symbol: symbol.to_string(),
                side: "buy".to_string(),
                order_type: "limit".to_string(),
                price,
                amount,
                status: "open".to_string(),
                timestamp: chrono::Utc::now().timestamp(),
            });
        }

        let mut params = HashMap::new();
        params.insert("pair".to_string(), self.to_kraken_pair(symbol));
        params.insert("type".to_string(), "buy".to_string());
        params.insert("ordertype".to_string(), "limit".to_string());
        params.insert("price".to_string(), format!("{}", price));
        params.insert("volume".to_string(), format!("{}", amount));

        let res = self.send_private_request("AddOrder", params).await?;

        let txid = res.get("txid")
            .and_then(|t| t.as_array())
            .and_then(|a| a.first())
            .and_then(|v| v.as_str())
            .unwrap_or("unknown_txid");

        Ok(Order {
            id: txid.to_string(),
            symbol: symbol.to_string(),
            side: "buy".to_string(),
            order_type: "limit".to_string(),
            price,
            amount,
            status: "open".to_string(),
            timestamp: chrono::Utc::now().timestamp(),
        })
    }

    async fn create_limit_sell(&self, symbol: &str, amount: f64, price: f64) -> Result<Order> {
        if self.api_key.is_empty() || self.api_secret.is_empty() {
            return Ok(Order {
                id: format!("sim_sell_{}", chrono::Utc::now().timestamp_millis()),
                symbol: symbol.to_string(),
                side: "sell".to_string(),
                order_type: "limit".to_string(),
                price,
                amount,
                status: "open".to_string(),
                timestamp: chrono::Utc::now().timestamp(),
            });
        }

        let mut params = HashMap::new();
        params.insert("pair".to_string(), self.to_kraken_pair(symbol));
        params.insert("type".to_string(), "sell".to_string());
        params.insert("ordertype".to_string(), "limit".to_string());
        params.insert("price".to_string(), format!("{}", price));
        params.insert("volume".to_string(), format!("{}", amount));

        let res = self.send_private_request("AddOrder", params).await?;

        let txid = res.get("txid")
            .and_then(|t| t.as_array())
            .and_then(|a| a.first())
            .and_then(|v| v.as_str())
            .unwrap_or("unknown_txid");

        Ok(Order {
            id: txid.to_string(),
            symbol: symbol.to_string(),
            side: "sell".to_string(),
            order_type: "limit".to_string(),
            price,
            amount,
            status: "open".to_string(),
            timestamp: chrono::Utc::now().timestamp(),
        })
    }

    async fn cancel_order(&self, order_id: &str, _symbol: &str) -> Result<bool> {
        if self.api_key.is_empty() || self.api_secret.is_empty() || order_id.starts_with("sim_") {
            return Ok(true);
        }

        let mut params = HashMap::new();
        params.insert("txid".to_string(), order_id.to_string());

        let _res = self.send_private_request("CancelOrder", params).await?;
        Ok(true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_normalize_kraken_symbol() {
        assert_eq!(normalize_kraken_symbol("ZEUR"), "EUR");
        assert_eq!(normalize_kraken_symbol("ZUSD"), "USD");
        assert_eq!(normalize_kraken_symbol("XXBT"), "BTC");
        assert_eq!(normalize_kraken_symbol("XETH"), "ETH");
        assert_eq!(normalize_kraken_symbol("BTC"), "BTC");
    }
}

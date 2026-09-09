use crate::config::{Config, StrategyConfig};
use crate::exchange::Candle;
use crate::indicators::TechnicalAnalysis;
use crate::monte_carlo::MonteCarloEngine;

#[derive(Debug, Clone, PartialEq)]
pub enum Signal {
    Buy,
    Sell,
    Hold,
}

#[derive(Debug, Clone)]
pub struct SignalResult {
    pub signal: Signal,
    pub buy_multiplier: f64,
    pub sell_multiplier: f64,
    pub tendency: String,
    pub buy_score: f64,
    pub sell_score: f64,
}

pub struct CustomRuleEvaluator;

impl CustomRuleEvaluator {
    pub fn evaluate_condition(condition: &str, candles: &[Candle]) -> bool {
        if condition.trim().is_empty() || candles.is_empty() {
            return false;
        }

        let upper_cond = condition.to_uppercase();

        if upper_cond.contains(" OR ") {
            let parts: Vec<&str> = condition.split(" OR ").collect();
            return parts.iter().any(|p| Self::evaluate_sub_condition(p, candles));
        }

        if upper_cond.contains(" AND ") {
            let parts: Vec<&str> = condition.split(" AND ").collect();
            return parts.iter().all(|p| Self::evaluate_sub_condition(p, candles));
        }

        Self::evaluate_sub_condition(condition, candles)
    }

    fn evaluate_sub_condition(sub_cond: &str, candles: &[Candle]) -> bool {
        let trimmed = sub_cond.trim();
        if trimmed.is_empty() {
            return false;
        }

        let operators = ["<=", ">=", "==", "<", ">"];
        let mut found_op = "";
        for op in operators {
            if trimmed.contains(op) {
                found_op = op;
                break;
            }
        }

        if found_op.is_empty() {
            return false;
        }

        let parts: Vec<&str> = trimmed.split(found_op).collect();
        if parts.len() != 2 {
            return false;
        }

        let left_val = Self::resolve_value(parts[0].trim(), candles);
        let right_val = Self::resolve_value(parts[1].trim(), candles);

        match (left_val, right_val) {
            (Some(l), Some(r)) => match found_op {
                "<=" => l <= r,
                ">=" => l >= r,
                "==" => (l - r).abs() < 1e-6,
                "<" => l < r,
                ">" => l > r,
                _ => false,
            },
            _ => false,
        }
    }

    fn resolve_value(token: &str, candles: &[Candle]) -> Option<f64> {
        if candles.is_empty() {
            return None;
        }

        if let Ok(num) = token.parse::<f64>() {
            return Some(num);
        }

        let lower = token.to_lowercase();
        let last_candle = candles.last().unwrap();

        if lower == "close" {
            return Some(last_candle.close);
        }
        if lower == "open" {
            return Some(last_candle.open);
        }
        if lower == "high" {
            return Some(last_candle.high);
        }
        if lower == "low" {
            return Some(last_candle.low);
        }
        if lower == "volume" {
            return Some(last_candle.volume);
        }

        if lower.starts_with("rsi(") && lower.ends_with(')') {
            let inner = &lower[4..lower.len() - 1];
            let period = inner.parse::<usize>().unwrap_or(14);
            return TechnicalAnalysis::calculate_rsi(candles, period);
        }

        if lower.starts_with("sma(") && lower.ends_with(')') {
            let inner = &lower[4..lower.len() - 1];
            let period = inner.parse::<usize>().unwrap_or(20);
            return TechnicalAnalysis::calculate_sma(candles, period);
        }

        if lower.starts_with("ema(") && lower.ends_with(')') {
            let inner = &lower[4..lower.len() - 1];
            let period = inner.parse::<usize>().unwrap_or(20);
            return TechnicalAnalysis::calculate_ema(candles, period);
        }

        if lower.starts_with("adx(") && lower.ends_with(')') {
            let inner = &lower[4..lower.len() - 1];
            let period = inner.parse::<usize>().unwrap_or(14);
            return TechnicalAnalysis::calculate_adx(candles, period);
        }

        None
    }
}

pub struct StrategyAggregator;

impl StrategyAggregator {
    fn get_param_f64(cfg: &StrategyConfig, key: &str, default_val: f64) -> f64 {
        cfg.params.get(key).and_then(|v| v.as_f64()).unwrap_or(default_val)
    }

    fn get_param_usize(cfg: &StrategyConfig, key: &str, default_val: usize) -> usize {
        cfg.params
            .get(key)
            .and_then(|v| v.as_u64())
            .map(|n| n as usize)
            .unwrap_or(default_val)
    }

    // 1. Ichimoku Cloud Strategy
    pub fn strategy_ichimoku(candles: &[Candle], cfg: &StrategyConfig) -> (bool, bool) {
        let tenkan_p = Self::get_param_usize(cfg, "tenkan", 9);
        let kijun_p = Self::get_param_usize(cfg, "kijun", 26);
        let senkou_p = Self::get_param_usize(cfg, "senkou", 52);

        if candles.len() < senkou_p {
            return (false, false);
        }
        let tenkan = TechnicalAnalysis::calculate_sma(candles, tenkan_p).unwrap_or(0.0);
        let kijun = TechnicalAnalysis::calculate_sma(candles, kijun_p).unwrap_or(0.0);
        let span_a = (tenkan + kijun) / 2.0;
        let span_b = TechnicalAnalysis::calculate_sma(candles, senkou_p).unwrap_or(0.0);

        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);
        let buy = tenkan > kijun && last_close > span_a && last_close > span_b;
        let sell = tenkan < kijun;
        (buy, sell)
    }

    // 2. Parabolic SAR Strategy
    pub fn strategy_psar(candles: &[Candle], _cfg: &StrategyConfig) -> (bool, bool) {
        if candles.len() < 2 {
            return (false, false);
        }
        let last = candles.last().unwrap();
        let prev = &candles[candles.len() - 2];
        let buy = last.close > last.low && prev.close <= prev.low;
        let sell = last.close < last.high && prev.close >= prev.high;
        (buy, sell)
    }

    // 3. Bollinger Bands Strategy
    pub fn strategy_bollinger(candles: &[Candle], cfg: &StrategyConfig) -> (bool, bool) {
        let length = Self::get_param_usize(cfg, "length", 20);
        let std_dev = Self::get_param_f64(cfg, "std", 2.0);
        let rsi_oversold = Self::get_param_f64(cfg, "rsi_oversold", 35.0);

        let (lower_band, mid_band, _upper_band) = match TechnicalAnalysis::calculate_bollinger_bands(candles, length, std_dev) {
            Some(res) => res,
            None => return (false, false),
        };
        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);
        let rsi = TechnicalAnalysis::calculate_rsi(candles, 14).unwrap_or(50.0);

        let buy = last_close <= lower_band && rsi < rsi_oversold;
        let sell = last_close >= mid_band;
        (buy, sell)
    }

    // 4. Donchian Channels Strategy
    pub fn strategy_donchian(candles: &[Candle], cfg: &StrategyConfig) -> (bool, bool) {
        let length = Self::get_param_usize(cfg, "length", 20);
        if candles.len() < length {
            return (false, false);
        }
        let slice = &candles[candles.len() - length..];
        let upper = slice.iter().map(|c| c.high).fold(f64::MIN, f64::max);
        let lower = slice.iter().map(|c| c.low).fold(f64::MAX, f64::min);
        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);

        let buy = last_close >= upper;
        let sell = last_close <= lower;
        (buy, sell)
    }

    // 5. Stochastic RSI Strategy
    pub fn strategy_stoch_rsi(candles: &[Candle], cfg: &StrategyConfig) -> (bool, bool) {
        let rsi_length = Self::get_param_usize(cfg, "rsi_length", 14);
        let oversold = Self::get_param_f64(cfg, "oversold", 20.0);
        let overbought = Self::get_param_f64(cfg, "overbought", 80.0);

        let rsi = match TechnicalAnalysis::calculate_rsi(candles, rsi_length) {
            Some(r) => r,
            None => return (false, false),
        };
        let buy = rsi < oversold;
        let sell = rsi > overbought;
        (buy, sell)
    }

    // 6. Williams %R Strategy
    pub fn strategy_williams_r(candles: &[Candle], cfg: &StrategyConfig) -> (bool, bool) {
        let length = Self::get_param_usize(cfg, "length", 14);
        let oversold = Self::get_param_f64(cfg, "oversold", -80.0);
        let overbought = Self::get_param_f64(cfg, "overbought", -20.0);

        if candles.len() < length {
            return (false, false);
        }
        let slice = &candles[candles.len() - length..];
        let highest_high = slice.iter().map(|c| c.high).fold(f64::MIN, f64::max);
        let lowest_low = slice.iter().map(|c| c.low).fold(f64::MAX, f64::min);
        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);

        if highest_high == lowest_low {
            return (false, false);
        }

        let willr = ((highest_high - last_close) / (highest_high - lowest_low)) * -100.0;
        let buy = willr < oversold;
        let sell = willr > overbought;
        (buy, sell)
    }

    // 7. VWAP Momentum Strategy
    pub fn strategy_vwap_momentum(candles: &[Candle], _cfg: &StrategyConfig) -> (bool, bool) {
        if candles.is_empty() {
            return (false, false);
        }
        let mut sum_pv = 0.0;
        let mut sum_v = 0.0;
        for c in candles {
            let tp = (c.high + c.low + c.close) / 3.0;
            sum_pv += tp * c.volume;
            sum_v += c.volume;
        }

        if sum_v == 0.0 {
            return (false, false);
        }

        let vwap = sum_pv / sum_v;
        let last = candles.last().unwrap();
        let prev = if candles.len() > 1 { &candles[candles.len() - 2] } else { last };

        let buy = last.close > vwap && last.volume > prev.volume;
        let sell = last.close < vwap;
        (buy, sell)
    }

    // 8. Renko Proxy Strategy
    pub fn strategy_renko_proxy(candles: &[Candle], cfg: &StrategyConfig) -> (bool, bool) {
        let atr_length = Self::get_param_usize(cfg, "atr_length", 14);
        if candles.len() < atr_length {
            return (false, false);
        }
        let last = candles.last().unwrap();
        let body = (last.close - last.open).abs();
        let atr = (last.high - last.low).max(0.0001);

        let buy = body > atr && last.close > last.open;
        let sell = body > atr && last.close < last.open;
        (buy, sell)
    }

    // 9. EMA RSI Volume Strategy
    pub fn strategy_ema_rsi_volume(candles: &[Candle], cfg: &StrategyConfig) -> (bool, bool) {
        let ema_fast_p = Self::get_param_usize(cfg, "ema_fast", 9);
        let ema_slow_p = Self::get_param_usize(cfg, "ema_slow", 21);
        let rsi_length_p = Self::get_param_usize(cfg, "rsi_length", 14);
        let vol_ma_p = Self::get_param_usize(cfg, "vol_ma", 20);

        let ema_9 = match TechnicalAnalysis::calculate_ema(candles, ema_fast_p) {
            Some(e) => e,
            None => return (false, false),
        };
        let ema_21 = match TechnicalAnalysis::calculate_ema(candles, ema_slow_p) {
            Some(e) => e,
            None => return (false, false),
        };
        let rsi = match TechnicalAnalysis::calculate_rsi(candles, rsi_length_p) {
            Some(r) => r,
            None => return (false, false),
        };

        let last_vol = candles.last().map(|c| c.volume).unwrap_or(0.0);
        let avg_vol = candles.iter().take(vol_ma_p).map(|c| c.volume).sum::<f64>() / (vol_ma_p as f64).max(1.0);

        let buy = ema_9 > ema_21 && rsi > 50.0 && last_vol > avg_vol;
        let sell = ema_9 < ema_21;
        (buy, sell)
    }

    // 10. Whale Detection Proxy Strategy
    pub fn strategy_whale_detection(candles: &[Candle], cfg: &StrategyConfig) -> (bool, bool) {
        let length = Self::get_param_usize(cfg, "length", 20);
        let std_devs = Self::get_param_f64(cfg, "std_devs", 3.0);

        if candles.len() < length {
            return (false, false);
        }
        let last = candles.last().unwrap();
        let prev = &candles[candles.len() - 2];
        let avg_vol = candles.iter().take(length).map(|c| c.volume).sum::<f64>() / length as f64;

        let whale_spike = last.volume > (avg_vol * std_devs);
        let buy = whale_spike && last.close > prev.close;
        let sell = whale_spike && last.close < prev.close;
        (buy, sell)
    }

    // 11. Pump and Dump Proxy Strategy
    pub fn strategy_pump_dump(candles: &[Candle], cfg: &StrategyConfig) -> (bool, bool) {
        let vol_surge = Self::get_param_f64(cfg, "vol_surge", 1.5);
        let price_surge = Self::get_param_f64(cfg, "price_surge", 0.001);

        if candles.len() < 2 {
            return (false, false);
        }
        let last = candles.last().unwrap();
        let prev = &candles[candles.len() - 2];

        let price_change = (last.close - prev.close) / prev.close.max(1e-9);
        let vol_change = (last.volume - prev.volume) / prev.volume.max(1e-9);

        let pump = vol_change > vol_surge && price_change > price_surge;
        let sell = pump && last.close < prev.close;
        (false, sell)
    }

    // 12. Scientific Ensemble Strategy
    pub fn strategy_scientific_ensemble(candles: &[Candle], cfg: &StrategyConfig) -> (bool, bool) {
        let rsi_oversold = Self::get_param_f64(cfg, "rsi_oversold", 35.0);
        let rsi_overbought = Self::get_param_f64(cfg, "rsi_overbought", 65.0);

        let mut score = 0;
        if let Some(rsi) = TechnicalAnalysis::calculate_rsi(candles, 14) {
            if rsi < rsi_oversold { score += 1; }
            if rsi > rsi_overbought { score -= 1; }
        }
        if let Some((macd, signal, _)) = TechnicalAnalysis::calculate_macd(candles, 12, 26, 9) {
            if macd > signal { score += 1; }
            if macd < signal { score -= 1; }
        }
        if let Some((lower, _, upper)) = TechnicalAnalysis::calculate_bollinger_bands(candles, 20, 2.0) {
            let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);
            if last_close < lower { score += 1; }
            if last_close > upper { score -= 1; }
        }

        (score >= 1, score <= -1)
    }

    // 13. Sentiment Momentum Proxy Strategy
    pub fn strategy_sentiment_momentum(candles: &[Candle], cfg: &StrategyConfig) -> (bool, bool) {
        let roc_length = Self::get_param_usize(cfg, "roc_length", 10);
        let rsi_limit = Self::get_param_f64(cfg, "rsi_limit", 60.0);
        let rsi_floor = Self::get_param_f64(cfg, "rsi_floor", 40.0);

        if candles.len() < roc_length {
            return (false, false);
        }
        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);
        let roc_val = (last_close - candles[candles.len() - roc_length].close) / candles[candles.len() - roc_length].close.max(1e-9);
        let rsi = TechnicalAnalysis::calculate_rsi(candles, 14).unwrap_or(50.0);

        let buy = roc_val > 0.0 && rsi < rsi_limit;
        let sell = roc_val < 0.0 && rsi > rsi_floor;
        (buy, sell)
    }

    // 14. Liquidation Cascade Proxy Strategy
    pub fn strategy_liquidation_cascade(candles: &[Candle], cfg: &StrategyConfig) -> (bool, bool) {
        let pct_trigger = Self::get_param_f64(cfg, "pct_trigger", 0.001);
        let vol_multiplier = Self::get_param_f64(cfg, "vol_multiplier", 1.5);

        if candles.len() < 2 {
            return (false, false);
        }
        let last = candles.last().unwrap();
        let prev = &candles[candles.len() - 2];
        let pct_change = (last.close - prev.close) / prev.close.max(1e-9);

        let buy = pct_change < -pct_trigger && last.volume > prev.volume * vol_multiplier;
        let sell = pct_change > pct_trigger && last.volume > prev.volume * vol_multiplier;
        (buy, sell)
    }

    // 15. ADX Trend Strength Strategy
    pub fn strategy_adx_trend(candles: &[Candle], cfg: &StrategyConfig) -> (bool, bool) {
        let threshold = Self::get_param_f64(cfg, "threshold", 25.0);
        let sma_length = Self::get_param_usize(cfg, "sma_length", 20);

        let adx = match TechnicalAnalysis::calculate_adx(candles, 14) {
            Some(a) => a,
            None => return (false, false),
        };
        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);
        let sma_val = TechnicalAnalysis::calculate_sma(candles, sma_length).unwrap_or(last_close);

        let buy = adx > threshold && last_close > sma_val;
        let sell = adx > threshold && last_close < sma_val;
        (buy, sell)
    }

    // 16. Pairs Trading Proxy Strategy
    pub fn strategy_pairs_trading(candles: &[Candle], cfg: &StrategyConfig) -> (bool, bool) {
        let ma_length = Self::get_param_usize(cfg, "ma_length", 50);
        let z_threshold = Self::get_param_f64(cfg, "z_threshold", 0.02);

        let ma_val = match TechnicalAnalysis::calculate_sma(candles, ma_length) {
            Some(m) => m,
            None => return (false, false),
        };
        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);
        let z_score = (last_close - ma_val) / ma_val.max(1e-9);

        let buy = z_score < -z_threshold;
        let sell = z_score > z_threshold;
        (buy, sell)
    }

    // 17. Halving Cycle Proxy Strategy
    pub fn strategy_halving_cycle(candles: &[Candle], cfg: &StrategyConfig) -> (bool, bool) {
        let ema_long = Self::get_param_usize(cfg, "ema_long", 200);

        let ema_val = match TechnicalAnalysis::calculate_ema(candles, ema_long) {
            Some(e) => e,
            None => return (false, false),
        };
        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);

        let buy = last_close > ema_val;
        let sell = last_close < ema_val;
        (buy, sell)
    }

    // 18. Listing Surge Proxy Strategy
    pub fn strategy_listing_surge(candles: &[Candle], cfg: &StrategyConfig) -> (bool, bool) {
        let ma_length = Self::get_param_usize(cfg, "ma_length", 20);
        let vol_multiplier = Self::get_param_f64(cfg, "vol_multiplier", 5.0);

        if candles.len() < ma_length {
            return (false, false);
        }
        let last = candles.last().unwrap();
        let avg_vol = candles.iter().take(ma_length).map(|c| c.volume).sum::<f64>() / ma_length as f64;

        let buy = last.volume > avg_vol * vol_multiplier;
        let sell = last.close < candles[candles.len() - 2].close;
        (buy, sell)
    }

    // 19. TEMA Crossover Strategy
    pub fn strategy_tema_crossover(candles: &[Candle], cfg: &StrategyConfig) -> (bool, bool) {
        let ema_length = Self::get_param_usize(cfg, "ema_length", 9);

        let ema1 = match TechnicalAnalysis::calculate_ema(candles, ema_length) {
            Some(e) => e,
            None => return (false, false),
        };
        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);

        let buy = last_close > ema1;
        let sell = last_close < ema1;
        (buy, sell)
    }

    // 20. Heikin Ashi Strategy
    pub fn strategy_heikin_ashi(candles: &[Candle], _cfg: &StrategyConfig) -> (bool, bool) {
        if candles.is_empty() {
            return (false, false);
        }
        let last = candles.last().unwrap();
        let ha_close = (last.open + last.high + last.low + last.close) / 4.0;
        let ha_open = (last.open + last.close) / 2.0;

        let buy = ha_close > ha_open;
        let sell = ha_close < ha_open;
        (buy, sell)
    }

    // 21. Sinewave Cycle Strategy
    pub fn strategy_sinewave(candles: &[Candle], cfg: &StrategyConfig) -> (bool, bool) {
        let sma_length = Self::get_param_usize(cfg, "sma_length", 7);
        if candles.len() < sma_length {
            return (false, false);
        }
        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);
        let sma = TechnicalAnalysis::calculate_sma(candles, sma_length).unwrap_or(last_close);

        let buy = last_close > sma;
        let sell = last_close < sma;
        (buy, sell)
    }

    // 22. Candle Patterns Strategy
    pub fn strategy_candle_patterns(candles: &[Candle], _cfg: &StrategyConfig) -> (bool, bool) {
        if candles.len() < 2 {
            return (false, false);
        }
        let last = candles.last().unwrap();
        let prev = &candles[candles.len() - 2];

        let bull_engulf = prev.close < prev.open && last.close > last.open && last.open < prev.close && last.close > prev.open;
        let bear_engulf = prev.close > prev.open && last.close < last.open && last.open > prev.close && last.close < prev.open;

        (bull_engulf, bear_engulf)
    }

    // 23-28. Monte Carlo Strategies
    pub fn handle_mc_strategy(candles: &[Candle], strategy_name: &str, cfg: &StrategyConfig, mc_engine: &MonteCarloEngine) -> (bool, bool) {
        if candles.len() < 20 {
            return (false, false);
        }

        let current_price = candles.last().map(|c| c.close).unwrap_or(1.0);
        let threshold = Self::get_param_f64(cfg, "threshold", 0.55);

        match strategy_name {
            "mc_mean_reversion" => {
                let sma_length = Self::get_param_usize(cfg, "sma_length", 20);
                let sma = TechnicalAnalysis::calculate_sma(candles, sma_length).unwrap_or(current_price);
                let prob_above = mc_engine.estimate_hit_probability(current_price, sma, 0.01, 0.0, "above");
                let prob_below = mc_engine.estimate_hit_probability(current_price, sma, 0.01, 0.0, "below");
                (current_price < sma && prob_below > threshold, current_price > sma && prob_above > threshold)
            }
            "mc_momentum" => {
                let target_profit = Self::get_param_f64(cfg, "target_profit", 0.01);
                let prob_up = mc_engine.estimate_hit_probability(current_price, current_price * (1.0 + target_profit), 0.01, 0.001, "above");
                let prob_down = mc_engine.estimate_hit_probability(current_price, current_price * (1.0 - target_profit), 0.01, -0.001, "below");
                (prob_up > threshold, prob_down > threshold)
            }
            "mc_dynamic_allocation" => {
                let high_thresh = Self::get_param_f64(cfg, "high_threshold", 0.8);
                let low_thresh = Self::get_param_f64(cfg, "low_threshold", 0.6);
                let score = mc_engine.validate_strategy(candles);
                (score > high_thresh, score < low_thresh)
            }
            "mc_market_making" => {
                let prob_up = mc_engine.estimate_hit_probability(current_price, current_price * 1.002, 0.005, 0.0, "above");
                let prob_down = mc_engine.estimate_hit_probability(current_price, current_price * 0.998, 0.005, 0.0, "below");
                (prob_up > threshold, prob_down > threshold)
            }
            "mc_stop_loss_eval" => {
                let sl_pct = Self::get_param_f64(cfg, "sl_pct", 0.05);
                let prob_sl = mc_engine.estimate_hit_probability(current_price, current_price * (1.0 - sl_pct), 0.02, -0.002, "below");
                (false, prob_sl > threshold)
            }
            "mc_options_pricing" => {
                let ratio = Self::get_param_f64(cfg, "ratio", 1.5);
                let call_prob = mc_engine.estimate_hit_probability(current_price, current_price * 1.05, 0.02, 0.0, "above");
                let put_prob = mc_engine.estimate_hit_probability(current_price, current_price * 0.95, 0.02, 0.0, "below");
                (call_prob > put_prob * ratio, put_prob > call_prob * ratio)
            }
            _ => (false, false),
        }
    }

    pub fn aggregate(candles: &[Candle], config: &Config) -> SignalResult {
        if candles.is_empty() {
            return SignalResult {
                signal: Signal::Hold,
                buy_multiplier: 0.9994,
                sell_multiplier: 1.0006,
                tendency: "Neutral".to_string(),
                buy_score: 0.0,
                sell_score: 0.0,
            };
        }

        let mut buy_score: f64 = 0.0;
        let mut sell_score: f64 = 0.0;
        let mut buy_count: usize = 0;
        let mut sell_count: usize = 0;
        let mut total_enabled_weight: f64 = 0.0;

        let mc_engine = MonteCarloEngine::new(
            config.monte_carlo.num_simulations,
            config.monte_carlo.timeframe_candles,
        );

        // Standard strategies
        let strategy_evaluators: [(&str, fn(&[Candle], &StrategyConfig) -> (bool, bool)); 22] = [
            ("ichimoku", Self::strategy_ichimoku),
            ("psar", Self::strategy_psar),
            ("bollinger", Self::strategy_bollinger),
            ("donchian", Self::strategy_donchian),
            ("stoch_rsi", Self::strategy_stoch_rsi),
            ("williams_r", Self::strategy_williams_r),
            ("vwap_momentum", Self::strategy_vwap_momentum),
            ("renko", Self::strategy_renko_proxy),
            ("ema_rsi_volume", Self::strategy_ema_rsi_volume),
            ("whale_detection", Self::strategy_whale_detection),
            ("pump_dump", Self::strategy_pump_dump),
            ("scientific_ensemble", Self::strategy_scientific_ensemble),
            ("sentiment_momentum", Self::strategy_sentiment_momentum),
            ("liquidation_cascade", Self::strategy_liquidation_cascade),
            ("adx_trend", Self::strategy_adx_trend),
            ("pairs_trading", Self::strategy_pairs_trading),
            ("halving_cycle", Self::strategy_halving_cycle),
            ("listing_surge", Self::strategy_listing_surge),
            ("tema_crossover", Self::strategy_tema_crossover),
            ("heikin_ashi", Self::strategy_heikin_ashi),
            ("sinewave", Self::strategy_sinewave),
            ("candle_patterns", Self::strategy_candle_patterns),
        ];

        for (name, func) in strategy_evaluators {
            if let Some(strat_cfg) = config.strategies.get(name) {
                if !strat_cfg.enabled {
                    continue;
                }
                total_enabled_weight += strat_cfg.weight;
                let (buy, sell) = func(candles, strat_cfg);
                if buy {
                    buy_score += strat_cfg.weight;
                    buy_count += 1;
                }
                if sell {
                    sell_score += strat_cfg.weight;
                    sell_count += 1;
                }
            }
        }

        // Monte Carlo strategies
        let mc_names = [
            "mc_mean_reversion",
            "mc_momentum",
            "mc_dynamic_allocation",
            "mc_market_making",
            "mc_stop_loss_eval",
            "mc_options_pricing",
        ];

        for name in mc_names {
            if let Some(strat_cfg) = config.strategies.get(name) {
                if !strat_cfg.enabled {
                    continue;
                }
                total_enabled_weight += strat_cfg.weight;
                let (buy, sell) = Self::handle_mc_strategy(candles, name, strat_cfg, &mc_engine);
                if buy {
                    buy_score += strat_cfg.weight;
                    buy_count += 1;
                }
                if sell {
                    sell_score += strat_cfg.weight;
                    sell_count += 1;
                }
            }
        }

        // Custom Rule-Based Strategies
        for custom_cfg in &config.custom_strategies {
            if !custom_cfg.enabled {
                continue;
            }
            total_enabled_weight += custom_cfg.weight;
            let buy = CustomRuleEvaluator::evaluate_condition(&custom_cfg.buy_condition, candles);
            let sell = CustomRuleEvaluator::evaluate_condition(&custom_cfg.sell_condition, candles);

            if buy {
                buy_score += custom_cfg.weight;
                buy_count += 1;
            }
            if sell {
                sell_score += custom_cfg.weight;
                sell_count += 1;
            }
        }

        let mode = config.strategy_aggregation.mode.as_str();
        let min_buy = config.strategy_aggregation.min_buy_score;
        let min_sell = config.strategy_aggregation.min_sell_score;
        let consensus_thresh = config.strategy_aggregation.consensus_threshold;

        let signal = match mode {
            "consensus" => {
                if total_enabled_weight > 0.0 {
                    let buy_ratio = buy_score / total_enabled_weight;
                    let sell_ratio = sell_score / total_enabled_weight;
                    if buy_ratio >= consensus_thresh && buy_score > sell_score {
                        Signal::Buy
                    } else if sell_ratio >= consensus_thresh && sell_score > buy_score {
                        Signal::Sell
                    } else {
                        Signal::Hold
                    }
                } else {
                    Signal::Hold
                }
            }
            "majority" => {
                if buy_count > sell_count && buy_count >= 1 {
                    Signal::Buy
                } else if sell_count > buy_count && sell_count >= 1 {
                    Signal::Sell
                } else {
                    Signal::Hold
                }
            }
            _ => {
                // "weighted_score"
                if buy_score >= min_buy && buy_score > sell_score {
                    Signal::Buy
                } else if sell_score >= min_sell && sell_score > buy_score {
                    Signal::Sell
                } else {
                    Signal::Hold
                }
            }
        };

        let buy_multiplier = 1.0 - (0.0006 * buy_score.min(2.0_f64));
        let sell_multiplier = 1.0 + (0.0006 * sell_score.min(2.0_f64));

        let tendency = if buy_score > sell_score {
            "Bullish".to_string()
        } else if sell_score > buy_score {
            "Bearish".to_string()
        } else {
            "Neutral".to_string()
        };

        SignalResult {
            signal,
            buy_multiplier,
            sell_multiplier,
            tendency,
            buy_score,
            sell_score,
        }
    }
}

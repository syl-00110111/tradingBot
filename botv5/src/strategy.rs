use crate::config::Config;
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
}

pub struct StrategyAggregator;

impl StrategyAggregator {
    // 1. Ichimoku Cloud Strategy
    pub fn strategy_ichimoku(candles: &[Candle]) -> (bool, bool) {
        if candles.len() < 52 {
            return (false, false);
        }
        let tenkan = TechnicalAnalysis::calculate_sma(candles, 9).unwrap_or(0.0);
        let kijun = TechnicalAnalysis::calculate_sma(candles, 26).unwrap_or(0.0);
        let span_a = (tenkan + kijun) / 2.0;
        let span_b = TechnicalAnalysis::calculate_sma(candles, 52).unwrap_or(0.0);

        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);
        let buy = tenkan > kijun && last_close > span_a && last_close > span_b;
        let sell = tenkan < kijun;
        (buy, sell)
    }

    // 2. Parabolic SAR Strategy
    pub fn strategy_psar(candles: &[Candle]) -> (bool, bool) {
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
    pub fn strategy_bollinger(candles: &[Candle]) -> (bool, bool) {
        let (lower_band, mid_band, _upper_band) = match TechnicalAnalysis::calculate_bollinger_bands(candles, 20, 2.0) {
            Some(res) => res,
            None => return (false, false),
        };
        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);
        let rsi = TechnicalAnalysis::calculate_rsi(candles, 14).unwrap_or(50.0);

        let buy = last_close <= lower_band && rsi < 35.0;
        let sell = last_close >= mid_band;
        (buy, sell)
    }

    // 4. Donchian Channels Strategy
    pub fn strategy_donchian(candles: &[Candle]) -> (bool, bool) {
        if candles.len() < 20 {
            return (false, false);
        }
        let slice = &candles[candles.len() - 20..];
        let upper = slice.iter().map(|c| c.high).fold(f64::MIN, f64::max);
        let lower = slice.iter().map(|c| c.low).fold(f64::MAX, f64::min);
        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);

        let buy = last_close >= upper;
        let sell = last_close <= lower;
        (buy, sell)
    }

    // 5. Stochastic RSI Strategy
    pub fn strategy_stoch_rsi(candles: &[Candle]) -> (bool, bool) {
        let rsi = match TechnicalAnalysis::calculate_rsi(candles, 14) {
            Some(r) => r,
            None => return (false, false),
        };
        let buy = rsi < 20.0;
        let sell = rsi > 80.0;
        (buy, sell)
    }

    // 6. Williams %R Strategy
    pub fn strategy_williams_r(candles: &[Candle]) -> (bool, bool) {
        if candles.len() < 14 {
            return (false, false);
        }
        let slice = &candles[candles.len() - 14..];
        let highest_high = slice.iter().map(|c| c.high).fold(f64::MIN, f64::max);
        let lowest_low = slice.iter().map(|c| c.low).fold(f64::MAX, f64::min);
        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);

        if highest_high == lowest_low {
            return (false, false);
        }

        let willr = ((highest_high - last_close) / (highest_high - lowest_low)) * -100.0;
        let buy = willr < -80.0;
        let sell = willr > -20.0;
        (buy, sell)
    }

    // 7. VWAP Momentum Strategy
    pub fn strategy_vwap_momentum(candles: &[Candle]) -> (bool, bool) {
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
    pub fn strategy_renko_proxy(candles: &[Candle]) -> (bool, bool) {
        if candles.len() < 14 {
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
    pub fn strategy_ema_rsi_volume(candles: &[Candle]) -> (bool, bool) {
        let ema_9 = match TechnicalAnalysis::calculate_ema(candles, 9) {
            Some(e) => e,
            None => return (false, false),
        };
        let ema_21 = match TechnicalAnalysis::calculate_ema(candles, 21) {
            Some(e) => e,
            None => return (false, false),
        };
        let rsi = match TechnicalAnalysis::calculate_rsi(candles, 14) {
            Some(r) => r,
            None => return (false, false),
        };

        let last_vol = candles.last().map(|c| c.volume).unwrap_or(0.0);
        let avg_vol = candles.iter().take(20).map(|c| c.volume).sum::<f64>() / (20.0_f64).max(1.0);

        let buy = ema_9 > ema_21 && rsi > 50.0 && last_vol > avg_vol;
        let sell = ema_9 < ema_21;
        (buy, sell)
    }

    // 10. Whale Detection Proxy Strategy
    pub fn strategy_whale_detection(candles: &[Candle]) -> (bool, bool) {
        if candles.len() < 20 {
            return (false, false);
        }
        let last = candles.last().unwrap();
        let prev = &candles[candles.len() - 2];
        let avg_vol = candles.iter().take(20).map(|c| c.volume).sum::<f64>() / 20.0;

        let whale_spike = last.volume > (avg_vol * 3.0);
        let buy = whale_spike && last.close > prev.close;
        let sell = whale_spike && last.close < prev.close;
        (buy, sell)
    }

    // 11. Pump and Dump Proxy Strategy
    pub fn strategy_pump_dump(candles: &[Candle]) -> (bool, bool) {
        if candles.len() < 2 {
            return (false, false);
        }
        let last = candles.last().unwrap();
        let prev = &candles[candles.len() - 2];

        let price_change = (last.close - prev.close) / prev.close.max(1e-9);
        let vol_change = (last.volume - prev.volume) / prev.volume.max(1e-9);

        let pump = vol_change > 1.5 && price_change > 0.001;
        let sell = pump && last.close < prev.close;
        (false, sell)
    }

    // 12. Scientific Ensemble Strategy
    pub fn strategy_scientific_ensemble(candles: &[Candle]) -> (bool, bool) {
        let mut score = 0;
        if let Some(rsi) = TechnicalAnalysis::calculate_rsi(candles, 14) {
            if rsi < 35.0 { score += 1; }
            if rsi > 65.0 { score -= 1; }
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
    pub fn strategy_sentiment_momentum(candles: &[Candle]) -> (bool, bool) {
        if candles.len() < 10 {
            return (false, false);
        }
        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);
        let roc_10 = (last_close - candles[candles.len() - 10].close) / candles[candles.len() - 10].close.max(1e-9);
        let rsi = TechnicalAnalysis::calculate_rsi(candles, 14).unwrap_or(50.0);

        let buy = roc_10 > 0.0 && rsi < 60.0;
        let sell = roc_10 < 0.0 && rsi > 40.0;
        (buy, sell)
    }

    // 14. Liquidation Cascade Proxy Strategy
    pub fn strategy_liquidation_cascade(candles: &[Candle]) -> (bool, bool) {
        if candles.len() < 2 {
            return (false, false);
        }
        let last = candles.last().unwrap();
        let prev = &candles[candles.len() - 2];
        let pct_change = (last.close - prev.close) / prev.close.max(1e-9);

        let buy = pct_change < -0.001 && last.volume > prev.volume * 1.5;
        let sell = pct_change > 0.001 && last.volume > prev.volume * 1.5;
        (buy, sell)
    }

    // 15. ADX Trend Strength Strategy
    pub fn strategy_adx_trend(candles: &[Candle]) -> (bool, bool) {
        let adx = match TechnicalAnalysis::calculate_adx(candles, 14) {
            Some(a) => a,
            None => return (false, false),
        };
        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);
        let sma_20 = TechnicalAnalysis::calculate_sma(candles, 20).unwrap_or(last_close);

        let buy = adx > 25.0 && last_close > sma_20;
        let sell = adx > 25.0 && last_close < sma_20;
        (buy, sell)
    }

    // 16. Pairs Trading Proxy Strategy
    pub fn strategy_pairs_trading(candles: &[Candle]) -> (bool, bool) {
        let ma_50 = match TechnicalAnalysis::calculate_sma(candles, 50) {
            Some(m) => m,
            None => return (false, false),
        };
        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);
        let z_score = (last_close - ma_50) / ma_50.max(1e-9);

        let buy = z_score < -0.02;
        let sell = z_score > 0.02;
        (buy, sell)
    }

    // 17. Halving Cycle Proxy Strategy
    pub fn strategy_halving_cycle(candles: &[Candle]) -> (bool, bool) {
        let ema_200 = match TechnicalAnalysis::calculate_ema(candles, 200) {
            Some(e) => e,
            None => return (false, false),
        };
        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);

        let buy = last_close > ema_200;
        let sell = last_close < ema_200;
        (buy, sell)
    }

    // 18. Listing Surge Proxy Strategy
    pub fn strategy_listing_surge(candles: &[Candle]) -> (bool, bool) {
        if candles.len() < 20 {
            return (false, false);
        }
        let last = candles.last().unwrap();
        let avg_vol = candles.iter().take(20).map(|c| c.volume).sum::<f64>() / 20.0;

        let buy = last.volume > avg_vol * 5.0;
        let sell = last.close < candles[candles.len() - 2].close;
        (buy, sell)
    }

    // 19. TEMA Crossover Strategy
    pub fn strategy_tema_crossover(candles: &[Candle]) -> (bool, bool) {
        let ema1 = match TechnicalAnalysis::calculate_ema(candles, 9) {
            Some(e) => e,
            None => return (false, false),
        };
        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);

        let buy = last_close > ema1;
        let sell = last_close < ema1;
        (buy, sell)
    }

    // 20. Heikin Ashi Strategy
    pub fn strategy_heikin_ashi(candles: &[Candle]) -> (bool, bool) {
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
    pub fn strategy_sinewave(candles: &[Candle]) -> (bool, bool) {
        if candles.len() < 7 {
            return (false, false);
        }
        let last_close = candles.last().map(|c| c.close).unwrap_or(0.0);
        let sma = TechnicalAnalysis::calculate_sma(candles, 7).unwrap_or(last_close);

        let buy = last_close > sma;
        let sell = last_close < sma;
        (buy, sell)
    }

    // 22. Candle Patterns Strategy
    pub fn strategy_candle_patterns(candles: &[Candle]) -> (bool, bool) {
        if candles.len() < 2 {
            return (false, false);
        }
        let last = candles.last().unwrap();
        let prev = &candles[candles.len() - 2];

        // Bullish Engulfing
        let bull_engulf = prev.close < prev.open && last.close > last.open && last.open < prev.close && last.close > prev.open;
        // Bearish Engulfing
        let bear_engulf = prev.close > prev.open && last.close < last.open && last.open > prev.close && last.close < prev.open;

        (bull_engulf, bear_engulf)
    }

    // 23-28. Monte Carlo Strategies
    pub fn handle_mc_strategy(candles: &[Candle], strategy_name: &str, mc_engine: &MonteCarloEngine) -> (bool, bool) {
        if candles.len() < 20 {
            return (false, false);
        }

        let current_price = candles.last().map(|c| c.close).unwrap_or(1.0);
        let sma_20 = TechnicalAnalysis::calculate_sma(candles, 20).unwrap_or(current_price);

        match strategy_name {
            "mc_mean_reversion" => {
                let prob_above = mc_engine.estimate_hit_probability(current_price, sma_20, 0.01, 0.0, "above");
                let prob_below = mc_engine.estimate_hit_probability(current_price, sma_20, 0.01, 0.0, "below");
                (current_price < sma_20 && prob_below > 0.55, current_price > sma_20 && prob_above > 0.55)
            }
            "mc_momentum" => {
                let prob_up = mc_engine.estimate_hit_probability(current_price, current_price * 1.01, 0.01, 0.001, "above");
                let prob_down = mc_engine.estimate_hit_probability(current_price, current_price * 0.99, 0.01, -0.001, "below");
                (prob_up > 0.55, prob_down > 0.55)
            }
            "mc_dynamic_allocation" => {
                let score = mc_engine.validate_strategy(candles);
                (score > 0.8, score < 0.6)
            }
            "mc_market_making" => {
                let prob_up = mc_engine.estimate_hit_probability(current_price, current_price * 1.002, 0.005, 0.0, "above");
                let prob_down = mc_engine.estimate_hit_probability(current_price, current_price * 0.998, 0.005, 0.0, "below");
                (prob_up > 0.6, prob_down > 0.6)
            }
            "mc_stop_loss_eval" => {
                let prob_sl = mc_engine.estimate_hit_probability(current_price, current_price * 0.95, 0.02, -0.002, "below");
                (false, prob_sl > 0.12)
            }
            "mc_options_pricing" => {
                let call_prob = mc_engine.estimate_hit_probability(current_price, current_price * 1.05, 0.02, 0.0, "above");
                let put_prob = mc_engine.estimate_hit_probability(current_price, current_price * 0.95, 0.02, 0.0, "below");
                (call_prob > put_prob * 1.5, put_prob > call_prob * 1.5)
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
            };
        }

        let mut buy_score: f64 = 0.0;
        let mut sell_score: f64 = 0.0;

        let mc_engine = MonteCarloEngine::new(
            config.monte_carlo.num_simulations,
            config.monte_carlo.timeframe_candles,
        );

        // Dispatch evaluations across all strategy models
        let strats_eval = [
            Self::strategy_ichimoku(candles),
            Self::strategy_psar(candles),
            Self::strategy_bollinger(candles),
            Self::strategy_donchian(candles),
            Self::strategy_stoch_rsi(candles),
            Self::strategy_williams_r(candles),
            Self::strategy_vwap_momentum(candles),
            Self::strategy_renko_proxy(candles),
            Self::strategy_ema_rsi_volume(candles),
            Self::strategy_whale_detection(candles),
            Self::strategy_pump_dump(candles),
            Self::strategy_scientific_ensemble(candles),
            Self::strategy_sentiment_momentum(candles),
            Self::strategy_liquidation_cascade(candles),
            Self::strategy_adx_trend(candles),
            Self::strategy_pairs_trading(candles),
            Self::strategy_halving_cycle(candles),
            Self::strategy_listing_surge(candles),
            Self::strategy_tema_crossover(candles),
            Self::strategy_heikin_ashi(candles),
            Self::strategy_sinewave(candles),
            Self::strategy_candle_patterns(candles),
            Self::handle_mc_strategy(candles, "mc_mean_reversion", &mc_engine),
            Self::handle_mc_strategy(candles, "mc_momentum", &mc_engine),
            Self::handle_mc_strategy(candles, "mc_dynamic_allocation", &mc_engine),
            Self::handle_mc_strategy(candles, "mc_market_making", &mc_engine),
            Self::handle_mc_strategy(candles, "mc_stop_loss_eval", &mc_engine),
            Self::handle_mc_strategy(candles, "mc_options_pricing", &mc_engine),
        ];

        for (b, s) in strats_eval {
            if b { buy_score += 1.0; }
            if s { sell_score += 1.0; }
        }

        let signal = if buy_score > sell_score && buy_score >= 1.0 {
            Signal::Buy
        } else if sell_score > buy_score && sell_score >= 1.0 {
            Signal::Sell
        } else {
            Signal::Hold
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
        }
    }
}

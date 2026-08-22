use crate::config::Config;
use crate::exchange::Candle;

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
}

pub struct StrategyAggregator;

impl StrategyAggregator {
    pub fn aggregate(candles: &[Candle], config: &Config) -> SignalResult {
        if candles.is_empty() {
            return SignalResult {
                signal: Signal::Hold,
                buy_multiplier: 0.9994,
                sell_multiplier: 1.0006,
            };
        }

        let mut buy_score = 0.0;
        let mut sell_score = 0.0;

        // Supported strategy catalog categories
        // Trend: ichimoku_cloud, parabolic_sar, adx_trend_strength, halving_cycle_proxy, tema_crossover, heikin_ashi
        // Mean-reversion: bollinger_bands, pairs_trading_proxy
        // Breakout/Momentum: donchian_channels, stochastic_rsi, williams_r, vwap_momentum, sinewave_cycle, candle_patterns
        // Scalping & Proxies: renko_proxy, ema_rsi_volume, scientific_ensemble, whale_detection_proxy, pump_dump_proxy, sentiment_momentum_proxy, liquidation_cascade_proxy, listing_surge_proxy
        // Monte Carlo: mc_mean_reversion, mc_momentum, mc_dynamic_allocation, mc_market_making, mc_stop_loss_eval, mc_options_pricing

        for (strat_name, strat_config) in &config.strategies {
            let threshold = strat_config.get("threshold").and_then(|v| v.as_f64()).unwrap_or(0.5);
            if strat_name.contains("mean_reversion") || strat_name.contains("bollinger") || strat_name.contains("rsi") || strat_name.contains("ichimoku") {
                buy_score += threshold;
            } else if strat_name.contains("momentum") || strat_name.contains("trend") || strat_name.contains("cascade") || strat_name.contains("pump") {
                sell_score += threshold;
            }
        }

        let signal = if buy_score > sell_score && buy_score >= 1.0 {
            Signal::Buy
        } else if sell_score > buy_score && sell_score >= 1.0 {
            Signal::Sell
        } else {
            Signal::Hold
        };

        let buy_multiplier = 1.0 - (0.0006 * buy_score.min(2.0));
        let sell_multiplier = 1.0 + (0.0006 * sell_score.min(2.0));

        SignalResult {
            signal,
            buy_multiplier,
            sell_multiplier,
        }
    }
}

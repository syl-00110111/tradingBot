use crate::exchange::Candle;
use rayon::prelude::*;

pub struct TechnicalAnalysis;

impl TechnicalAnalysis {
    pub fn calculate_sma(candles: &[Candle], period: usize) -> Option<f64> {
        if candles.len() < period || period == 0 {
            return None;
        }
        let slice = &candles[candles.len() - period..];
        let sum: f64 = slice.par_iter().map(|c| c.close).sum();
        Some(sum / (period as f64))
    }

    pub fn count_peaks(candles: &[Candle], max_candles: usize) -> usize {
        if candles.len() < 3 {
            return 0;
        }
        let window_size = candles.len().min(max_candles);
        let slice = &candles[candles.len() - window_size..];

        let mut count = 0;
        for i in 1..slice.len() - 1 {
            let prev = slice[i - 1].high;
            let curr = slice[i].high;
            let next = slice[i + 1].high;

            if curr > prev && curr > next {
                count += 1;
            }
        }
        count
    }

    pub fn calculate_5_week_sma(candles_1m: &[Candle], candles_4h: Option<&[Candle]>) -> Option<f64> {
        // 5 weeks in 1m candles = 5 * 7 * 24 * 60 = 50,400 candles
        if candles_1m.len() >= 50400 {
            return Self::calculate_sma(candles_1m, 50400);
        }

        // Fallback to 4-hour candles: 5 weeks = 210 candles of 4h
        if let Some(c_4h) = candles_4h {
            if c_4h.len() >= 210 {
                return Self::calculate_sma(c_4h, 210);
            } else if !c_4h.is_empty() {
                let sum: f64 = c_4h.par_iter().map(|c| c.close).sum();
                return Some(sum / (c_4h.len() as f64));
            }
        }

        // Fallback to available 1m history
        if !candles_1m.is_empty() {
            let sum: f64 = candles_1m.par_iter().map(|c| c.close).sum();
            return Some(sum / (candles_1m.len() as f64));
        }

        None
    }

    pub fn calculate_ema(candles: &[Candle], period: usize) -> Option<f64> {
        if candles.len() < period || period == 0 {
            return None;
        }
        let alpha = 2.0 / (period as f64 + 1.0);
        let mut ema = candles[0].close;
        for c in &candles[1..] {
            ema = c.close * alpha + ema * (1.0 - alpha);
        }
        Some(ema)
    }

    pub fn calculate_rsi(candles: &[Candle], period: usize) -> Option<f64> {
        if candles.len() <= period || period == 0 {
            return None;
        }

        let mut gains = 0.0;
        let mut losses = 0.0;

        for i in 1..=period {
            let diff = candles[i].close - candles[i - 1].close;
            if diff > 0.0 {
                gains += diff;
            } else {
                losses += diff.abs();
            }
        }

        let mut avg_gain = gains / (period as f64);
        let mut avg_loss = losses / (period as f64);

        for i in (period + 1)..candles.len() {
            let diff = candles[i].close - candles[i - 1].close;
            if diff > 0.0 {
                avg_gain = (avg_gain * (period as f64 - 1.0) + diff) / (period as f64);
                avg_loss = (avg_loss * (period as f64 - 1.0)) / (period as f64);
            } else {
                avg_gain = (avg_gain * (period as f64 - 1.0)) / (period as f64);
                avg_loss = (avg_loss * (period as f64 - 1.0) + diff.abs()) / (period as f64);
            }
        }

        if avg_loss == 0.0 {
            return Some(100.0);
        }

        let rs = avg_gain / avg_loss;
        Some(100.0 - (100.0 / (1.0 + rs)))
    }

    pub fn calculate_macd(
        candles: &[Candle],
        fast_period: usize,
        slow_period: usize,
        signal_period: usize,
    ) -> Option<(f64, f64, f64)> {
        if candles.len() < slow_period + signal_period {
            return None;
        }

        let fast_ema = Self::calculate_ema(candles, fast_period)?;
        let slow_ema = Self::calculate_ema(candles, slow_period)?;
        let macd_line = fast_ema - slow_ema;

        let signal_line = macd_line * (2.0 / (signal_period as f64 + 1.0));
        let histogram = macd_line - signal_line;

        Some((macd_line, signal_line, histogram))
    }

    pub fn calculate_bollinger_bands(
        candles: &[Candle],
        period: usize,
        std_dev_multiplier: f64,
    ) -> Option<(f64, f64, f64)> {
        if candles.len() < period || period == 0 {
            return None;
        }

        let sma = Self::calculate_sma(candles, period)?;
        let slice = &candles[candles.len() - period..];

        let variance: f64 = slice.par_iter().map(|c| (c.close - sma).powi(2)).sum::<f64>() / (period as f64);
        let std_dev = variance.sqrt();

        let upper_band = sma + (std_dev_multiplier * std_dev);
        let lower_band = sma - (std_dev_multiplier * std_dev);

        Some((lower_band, sma, upper_band))
    }

    pub fn calculate_adx(candles: &[Candle], period: usize) -> Option<f64> {
        if candles.len() <= period || period == 0 {
            return None;
        }

        let mut tr_sum = 0.0;
        for i in (candles.len() - period)..candles.len() {
            let high_low = candles[i].high - candles[i].low;
            let high_prev_close = (candles[i].high - candles[i - 1].close).abs();
            let low_prev_close = (candles[i].low - candles[i - 1].close).abs();
            let tr = high_low.max(high_prev_close).max(low_prev_close);
            tr_sum += tr;
        }

        let atr = tr_sum / (period as f64);
        let last_candle = candles.last()?;
        let first_candle = &candles[candles.len() - period];
        let price_range = (last_candle.high - first_candle.low).abs();

        if atr > 0.0 {
            Some((price_range / atr * 10.0).min(100.0).max(10.0))
        } else {
            Some(20.0)
        }
    }

    pub fn calibrate_window_by_non_repetition(
        candles: &[Candle],
        target_active: usize,
        epsilon: f64,
    ) -> usize {
        let n = candles.len();
        if n <= 1 {
            return n;
        }

        let mut active_count = 0;
        let mut scanned_count = 0;

        for i in (1..n).rev() {
            scanned_count += 1;
            let c1 = &candles[i];
            let c2 = &candles[i - 1];

            let max_c = c1.close.max(c2.close).max(1e-9);
            let max_o = c1.open.max(c2.open).max(1e-9);
            let max_h = c1.high.max(c2.high).max(1e-9);
            let max_l = c1.low.max(c2.low).max(1e-9);

            let diff_c = (c1.close - c2.close).abs() / max_c;
            let diff_o = (c1.open - c2.open).abs() / max_o;
            let diff_h = (c1.high - c2.high).abs() / max_h;
            let diff_l = (c1.low - c2.low).abs() / max_l;

            let is_rep = diff_c <= epsilon && diff_o <= epsilon && diff_h <= epsilon && diff_l <= epsilon;

            if !is_rep {
                active_count += 1;
            }

            if active_count >= target_active {
                break;
            }
        }

        let window_size = scanned_count + 1;
        window_size.min(n).max(target_active)
    }
}

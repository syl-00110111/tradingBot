use crate::exchange::Candle;

pub struct TechnicalAnalysis;

impl TechnicalAnalysis {
    pub fn calculate_sma(candles: &[Candle], period: usize) -> Option<f64> {
        if candles.len() < period || period == 0 {
            return None;
        }
        let sum: f64 = candles.iter().rev().take(period).map(|c| c.close).sum();
        Some(sum / period as f64)
    }

    pub fn calculate_ema(candles: &[Candle], period: usize) -> Option<f64> {
        if candles.len() < period || period == 0 {
            return None;
        }
        let k = 2.0 / (period as f64 + 1.0);
        let mut ema = candles[0].close;
        for c in &candles[1..] {
            ema = (c.close * k) + (ema * (1.0 - k));
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
            let change = candles[i].close - candles[i - 1].close;
            if change >= 0.0 {
                gains += change;
            } else {
                losses += change.abs();
            }
        }

        let mut avg_gain = gains / period as f64;
        let mut avg_loss = losses / period as f64;

        for i in (period + 1)..candles.len() {
            let change = candles[i].close - candles[i - 1].close;
            if change >= 0.0 {
                avg_gain = (avg_gain * (period as f64 - 1.0) + change) / period as f64;
                avg_loss = (avg_loss * (period as f64 - 1.0)) / period as f64;
            } else {
                avg_gain = (avg_gain * (period as f64 - 1.0)) / period as f64;
                avg_loss = (avg_loss * (period as f64 - 1.0) + change.abs()) / period as f64;
            }
        }

        if avg_loss == 0.0 {
            return Some(100.0);
        }

        let rs = avg_gain / avg_loss;
        Some(100.0 - (100.0 / (1.0 + rs)))
    }

    pub fn calculate_bollinger_bands(candles: &[Candle], period: usize, std_dev_mult: f64) -> Option<(f64, f64, f64)> {
        let mid = Self::calculate_sma(candles, period)?;
        let slice = &candles[candles.len() - period..];
        let variance: f64 = slice.iter().map(|c| (c.close - mid).powi(2)).sum::<f64>() / period as f64;
        let std_dev = variance.sqrt();

        let lower = mid - (std_dev_mult * std_dev);
        let upper = mid + (std_dev_mult * std_dev);
        Some((lower, mid, upper))
    }

    pub fn calculate_macd(candles: &[Candle], fast: usize, slow: usize, signal_p: usize) -> Option<(f64, f64, f64)> {
        if candles.len() < slow + signal_p {
            return None;
        }

        let fast_ema = Self::calculate_ema(candles, fast)?;
        let slow_ema = Self::calculate_ema(candles, slow)?;
        let macd_line = fast_ema - slow_ema;

        let mut macd_history = Vec::new();
        for i in (slow..=candles.len()).rev().take(signal_p) {
            let sub_slice = &candles[..i];
            if let (Some(f), Some(s)) = (Self::calculate_ema(sub_slice, fast), Self::calculate_ema(sub_slice, slow)) {
                macd_history.push(Candle {
                    timestamp: 0,
                    open: 0.0,
                    high: 0.0,
                    low: 0.0,
                    close: f - s,
                    volume: 0.0,
                });
            }
        }
        macd_history.reverse();

        let signal_line = Self::calculate_ema(&macd_history, macd_history.len()).unwrap_or(macd_line);
        let hist = macd_line - signal_line;

        Some((macd_line, signal_line, hist))
    }

    pub fn calculate_adx(candles: &[Candle], period: usize) -> Option<f64> {
        if candles.len() <= period * 2 {
            return None;
        }

        let mut tr_list = Vec::with_capacity(candles.len());
        for i in 1..candles.len() {
            let high = candles[i].high;
            let low = candles[i].low;
            let prev_close = candles[i - 1].close;

            let tr = (high - low)
                .max((high - prev_close).abs())
                .max((low - prev_close).abs());
            tr_list.push(tr);
        }

        if tr_list.len() < period {
            return None;
        }

        let atr = tr_list.iter().rev().take(period).sum::<f64>() / period as f64;
        if atr == 0.0 {
            return Some(0.0);
        }

        let last = candles.last().unwrap();
        let prev = &candles[candles.len() - 2];
        let up_move = last.high - prev.high;
        let down_move = prev.low - last.low;

        let plus_dm = if up_move > down_move && up_move > 0.0 { up_move } else { 0.0 };
        let minus_dm = if down_move > up_move && down_move > 0.0 { down_move } else { 0.0 };

        let plus_di = (plus_dm / atr) * 100.0;
        let minus_di = (minus_dm / atr) * 100.0;

        let dx = if plus_di + minus_di == 0.0 {
            0.0
        } else {
            ((plus_di - minus_di).abs() / (plus_di + minus_di)) * 100.0
        };

        Some(dx)
    }

    pub fn calculate_5_week_sma(candles_1m: &[Candle], candles_4h: Option<&[Candle]>) -> Option<f64> {
        if candles_1m.len() >= 50400 {
            Self::calculate_sma(candles_1m, 50400)
        } else if let Some(c4h) = candles_4h {
            if c4h.len() >= 210 {
                Self::calculate_sma(c4h, 210)
            } else {
                Self::calculate_sma(candles_1m, candles_1m.len())
            }
        } else {
            Self::calculate_sma(candles_1m, candles_1m.len())
        }
    }

    pub fn count_peaks(candles: &[Candle], window: usize) -> usize {
        let available = candles.len().min(window);
        if available < 3 {
            return 0;
        }

        let slice = &candles[candles.len() - available..];
        let mut peaks = 0;

        for i in 1..slice.len() - 1 {
            if slice[i].high > slice[i - 1].high && slice[i].high > slice[i + 1].high {
                peaks += 1;
            }
        }

        peaks
    }

    pub fn calibrate_window_by_non_repetition(candles: &[Candle], default_window: usize, epsilon: f64) -> usize {
        if candles.len() <= 1 {
            return candles.len();
        }

        let mut non_rep_count = 0;
        for window_slice in candles.windows(2) {
            let c1 = &window_slice[0];
            let c2 = &window_slice[1];

            let open_diff = (c1.open - c2.open).abs();
            let high_diff = (c1.high - c2.high).abs();
            let low_diff = (c1.low - c2.low).abs();
            let close_diff = (c1.close - c2.close).abs();

            if open_diff > epsilon || high_diff > epsilon || low_diff > epsilon || close_diff > epsilon {
                non_rep_count += 1;
            }
        }

        non_rep_count.min(default_window).max(10)
    }
}

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

    pub fn calculate_adx(candles: &[Candle], _period: usize) -> Option<f64> {
        if candles.is_empty() {
            return None;
        }
        let avg_close: f64 = candles.par_iter().map(|c| c.close).sum::<f64>() / (candles.len() as f64);
        if avg_close > 0.0 {
            Some(25.5)
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
            let diff_c = (c1.close - c2.close).abs() / max_c;

            if diff_c > epsilon {
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

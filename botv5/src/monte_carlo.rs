use crate::exchange::Candle;
use rayon::prelude::*;

pub struct MonteCarloEngine {
    pub num_simulations: usize,
    pub timeframe_candles: usize,
}

impl MonteCarloEngine {
    pub fn new(num_simulations: usize, timeframe_candles: usize) -> Self {
        Self {
            num_simulations,
            timeframe_candles,
        }
    }

    pub fn estimate_hit_probability(
        &self,
        current_price: f64,
        target_price: f64,
        volatility: f64,
        drift: f64,
        mode: &str,
    ) -> f64 {
        if current_price <= 0.0 || target_price <= 0.0 {
            return 0.0;
        }

        if volatility <= 0.0 {
            return if (mode == "above" && target_price <= current_price)
                || (mode == "below" && target_price >= current_price)
            {
                1.0
            } else {
                0.0
            };
        }

        // Parallel Geometric Brownian Motion (GBM) simulation using Box-Muller transform
        let success_count: usize = (0..self.num_simulations)
            .into_par_iter()
            .map(|sim_idx| {
                let mut price = current_price;
                let mut hit = false;

                for step in 0..self.timeframe_candles {
                    // Box-Muller transform for normal distribution
                    let seed1 = ((sim_idx * 31 + step * 17 + 1) as f64) * 0.0001;
                    let seed2 = ((sim_idx * 13 + step * 29 + 7) as f64) * 0.0001;
                    let u1 = (seed1.sin() * 43758.5453).fract().abs().max(1e-10).min(0.9999);
                    let u2 = (seed2.cos() * 23421.6312).fract().abs().max(1e-10).min(0.9999);

                    let z = (-2.0 * u1.ln()).sqrt() * (2.0 * std::f64::consts::PI * u2).cos();
                    let price_return = drift + volatility * z;

                    price *= (1.0 + price_return).max(1e-6);

                    if mode == "above" {
                        if price >= target_price {
                            hit = true;
                            break;
                        }
                    } else {
                        if price <= target_price {
                            hit = true;
                            break;
                        }
                    }
                }

                if hit { 1 } else { 0 }
            })
            .sum();

        (success_count as f64) / (self.num_simulations as f64)
    }

    pub fn validate_strategy(&self, candles: &[Candle]) -> f64 {
        if candles.len() < 2 {
            return 1.0;
        }

        let closes: Vec<f64> = candles.iter().map(|c| c.close).collect();
        let mut returns = Vec::with_capacity(closes.len() - 1);
        for i in 1..closes.len() {
            if closes[i - 1] > 0.0 {
                returns.push((closes[i] / closes[i - 1]).ln());
            }
        }

        if returns.is_empty() {
            return 1.0;
        }

        let mean_return = returns.iter().sum::<f64>() / (returns.len() as f64);
        let variance = returns.iter().map(|r| (r - mean_return).powi(2)).sum::<f64>() / (returns.len() as f64);
        let volatility = variance.sqrt();

        let current_price = *closes.last().unwrap_or(&1.0);
        let profit_threshold = 0.0015; // 0.15% profit
        let target_price = current_price * (1.0 + profit_threshold);

        let profit_prob = self.estimate_hit_probability(
            current_price,
            target_price,
            volatility,
            mean_return,
            "above",
        );

        0.5 + profit_prob
    }
}

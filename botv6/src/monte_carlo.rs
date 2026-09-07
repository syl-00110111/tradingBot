use crate::exchange::Candle;
use ndarray::Array1;
use rayon::prelude::*;

pub struct MonteCarloEngine {
    pub num_simulations: usize,
    pub timeframe_candles: usize,
}

impl MonteCarloEngine {
    pub fn new(num_simulations: usize, timeframe_candles: usize) -> Self {
        Self {
            num_simulations: num_simulations.max(100),
            timeframe_candles: timeframe_candles.max(10),
        }
    }

    fn generate_normal_samples(&self, size: usize) -> Vec<f64> {
        let mut samples = Vec::with_capacity(size);
        let mut i = 0;
        while i < size {
            let u1: f64 = (i as f64 + 1.0) / (size as f64 + 2.0);
            let u2: f64 = ((i + 1) as f64 * 0.314159) % 1.0;
            let u2 = if u2 == 0.0 { 0.5 } else { u2 };

            let z0 = (-2.0 * u1.ln()).sqrt() * (2.0 * std::f64::consts::PI * u2).cos();
            let z1 = (-2.0 * u1.ln()).sqrt() * (2.0 * std::f64::consts::PI * u2).sin();

            samples.push(if z0.is_finite() { z0 } else { 0.0 });
            if samples.len() < size {
                samples.push(if z1.is_finite() { z1 } else { 0.0 });
            }
            i += 2;
        }
        samples
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

        let is_above = mode.eq_ignore_ascii_case("above");
        if is_above && target_price <= current_price {
            return 1.0;
        }
        if !is_above && target_price >= current_price {
            return 1.0;
        }

        let dt = 1.0 / (self.timeframe_candles as f64);
        let vol = volatility.max(0.0001);
        let num_sims = self.num_simulations;
        let num_steps = self.timeframe_candles;

        let total_samples = num_sims * num_steps;
        let normal_samples = self.generate_normal_samples(total_samples);

        let hits: usize = (0..num_sims)
            .into_par_iter()
            .map(|sim_idx| {
                let mut price = current_price;
                let mut hit = false;
                let sample_offset = sim_idx * num_steps;

                for step in 0..num_steps {
                    let z = normal_samples[(sample_offset + step) % total_samples];
                    let drift_term = (drift - 0.5 * vol * vol) * dt;
                    let shock = vol * dt.sqrt() * z;
                    price *= (drift_term + shock).exp();

                    if is_above {
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

        hits as f64 / num_sims as f64
    }

    pub fn validate_strategy(&self, candles: &[Candle]) -> f64 {
        if candles.len() < 10 {
            return 0.5;
        }

        let closes: Vec<f64> = candles.iter().map(|c| c.close).collect();
        let arr = Array1::from(closes);
        let current_price = *arr.last().unwrap_or(&1.0);

        let returns: Vec<f64> = arr
            .windows(2)
            .into_iter()
            .map(|w| (w[1] / w[0]).ln())
            .filter(|v| v.is_finite())
            .collect();

        if returns.is_empty() {
            return 0.5;
        }

        let mean = returns.iter().sum::<f64>() / returns.len() as f64;
        let var = returns.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / returns.len() as f64;
        let volatility = var.sqrt();

        let target_profit_price = current_price * 1.0015;
        let prob = self.estimate_hit_probability(current_price, target_profit_price, volatility, mean, "above");

        (0.5 + prob * 0.5).min(1.0).max(0.0)
    }
}

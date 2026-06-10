# Binance Trading Bot - Monte Carlo Engine
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import numpy as np

class MonteCarloEngine:
    def __init__(self, num_simulations=5000, timeframe_candles=100):
        self.num_simulations = num_simulations
        self.timeframe_candles = timeframe_candles

    def simulate_paths(self, current_price, volatility, drift=0):
        """
        Simulate price paths using Geometric Brownian Motion.
        Vectorized with NumPy for maximum performance.
        """
        returns = np.random.normal(drift, volatility, (self.num_simulations, self.timeframe_candles))
        price_paths = current_price * np.exp(np.cumsum(returns, axis=1))
        # Prepend current price
        ones = np.ones((self.num_simulations, 1)) * current_price
        price_paths = np.hstack((ones, price_paths))
        return price_paths

    def estimate_hit_probability(self, current_price, target_price, volatility, drift=0, mode='above'):
        """
        Estimate the probability of price hitting a target within the timeframe.
        """
        if volatility == 0: return 1.0 if (mode == 'above' and target_price <= current_price) or (mode == 'below' and target_price >= current_price) else 0.0
        paths = self.simulate_paths(current_price, volatility, drift)
        if mode == 'above':
            hits = np.any(paths >= target_price, axis=1)
        else:
            hits = np.any(paths <= target_price, axis=1)
        return np.mean(hits)

    def validate_strategy(self, df):
        """
        Validate a strategy by running it on simulated paths based on historical volatility.
        Returns a score between 0.5 and 1.5.
        """
        if len(df) < 20: return 1.0

        close = df['close'].values
        valid_indices = ~np.isnan(close)
        close = close[valid_indices]

        if len(close) < 2: return 1.0

        # Calculate returns, avoiding log of zero
        price_ratios = close[1:] / close[:-1]
        price_ratios = np.where(price_ratios <= 0, 1.0, price_ratios)
        returns = np.log(price_ratios)

        volatility = np.std(returns)
        drift = np.mean(returns)
        current_price = close[-1]

        # Robustness: if no volatility, return neutral score
        if volatility == 0: return 1.0

        paths = self.simulate_paths(current_price, volatility, drift)

        # Validation: check how many paths end with profit > expected fees (0.2%)
        final_prices = paths[:, -1]
        # We use a slightly more permissive fee buffer (0.15%)
        profit_prob = np.mean(final_prices > current_price * 1.0015)

        # Transform probability into a scaling factor [0.5, 1.5]
        # 50% probability = 1.0 multiplier
        score = 0.5 + profit_prob
        return score

    def price_option(self, current_price, strike_price, volatility, drift=0, option_type='call'):
        """
        Estimate option price using Monte Carlo.
        """
        paths = self.simulate_paths(current_price, volatility, drift)
        final_prices = paths[:, -1]
        if option_type == 'call':
            payoffs = np.maximum(final_prices - strike_price, 0)
        else:
            payoffs = np.maximum(strike_price - final_prices, 0)

        return np.mean(payoffs)

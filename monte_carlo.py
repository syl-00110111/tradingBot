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

"""
Monte Carlo simulation engine for probability estimation and strategy validation.

This module uses Geometric Brownian Motion and PyTorch-accelerated simulations
 to assess the potential success of trading strategies.
"""

import numpy as np
import torch

class MonteCarloEngine:
    """
    Monte Carlo engine for simulating future price paths and estimating probabilities.

    Uses Geometric Brownian Motion (GBM) for simulations, accelerated via PyTorch.

    Parameters
    ----------
    num_simulations : int, optional
        Number of paths to simulate (default is 5000).
    timeframe_candles : int, optional
        Number of steps (candles) to simulate in the future (default is 100).
    """
    def __init__(self, num_simulations=5000, timeframe_candles=100):
        self.num_simulations = num_simulations
        self.timeframe_candles = timeframe_candles
        # Device will be updated by the bot at runtime, but we default to CPU
        self.device = torch.device("cpu")

    def set_device(self, device):
        """
        Updates the computation device (CPU or GPU).

        Parameters
        ----------
        device : torch.device
            The device to use for tensor operations.
        """
        self.device = device

    def simulate_paths(self, current_price, volatility, drift=0):
        """
        Simulate price paths using Geometric Brownian Motion.

        Vectorized with PyTorch for hardware acceleration.

        Parameters
        ----------
        current_price : float
            The starting price for simulation.
        volatility : float
            The standard deviation of log returns.
        drift : float, optional
            The mean of log returns (default is 0).

        Returns
        -------
        torch.Tensor
            A 2D tensor of shape (num_simulations, timeframe_candles + 1)
            containing the simulated paths.
        """
        # Ensure inputs are tensors and moved to device
        curr_p = torch.tensor(current_price, device=self.device, dtype=torch.float64)
        vol = torch.tensor(volatility, device=self.device, dtype=torch.float64)
        drft = torch.tensor(drift, device=self.device, dtype=torch.float64)

        # random.normal_ equivalent in torch
        returns = torch.randn((self.num_simulations, self.timeframe_candles), device=self.device) * vol + drft

        # Cumulative sum for path simulation
        price_paths = curr_p * torch.exp(torch.cumsum(returns, dim=1))

        # Prepend current price
        ones = torch.ones((self.num_simulations, 1), device=self.device) * curr_p
        price_paths = torch.cat((ones, price_paths), dim=1)
        return price_paths

    def estimate_hit_probability(self, current_price, target_price, volatility, drift=0, mode="above"):
        """
        Estimate the probability of price hitting a target within the timeframe.

        Parameters
        ----------
        current_price : float
            Starting price.
        target_price : float
            The price target to hit.
        volatility : float
            Log returns volatility.
        drift : float, optional
            Log returns drift.
        mode : str, optional
            Whether to check if price hits "above" or "below" the target.

        Returns
        -------
        float
            The estimated probability (0.0 to 1.0).
        """
        if volatility == 0:
            return 1.0 if (mode == "above" and target_price <= current_price) or (mode == "below" and target_price >= current_price) else 0.0

        paths = self.simulate_paths(current_price, volatility, drift)

        if mode == "above":
            hits = torch.any(paths >= target_price, dim=1)
        else:
            hits = torch.any(paths <= target_price, dim=1)

        return torch.mean(hits.double()).item()

    def validate_strategy(self, df):
        """
        Validates a strategy's potential by simulating future paths.

        Calculates historical volatility and drift from the provided data,
        then determines the probability of price exceeding a 0.15% profit threshold.

        Parameters
        ----------
        df : pandas.DataFrame
            Historical OHLCV data.

        Returns
        -------
        float
            A scaling factor score between 0.5 and 1.5 based on profit probability.
        """
        if len(df) < 20: return 1.0

        close = df["close"].values
        valid_indices = ~np.isnan(close)
        close = close[valid_indices]

        if len(close) < 2: return 1.0

        # Calculate returns
        price_ratios = close[1:] / close[:-1]
        price_ratios = np.where(price_ratios <= 0, 1.0, price_ratios)
        returns = np.log(price_ratios)

        volatility = np.std(returns)
        drift = np.mean(returns)
        current_price = close[-1]

        if volatility == 0: return 1.0

        paths = self.simulate_paths(current_price, volatility, drift)

        # Validation: check how many paths end with profit > expected fees (0.15%)
        final_prices = paths[:, -1]
        profit_prob = torch.mean((final_prices > current_price * 1.0015).double()).item()

        # Transform probability into a scaling factor [0.5, 1.5]
        score = 0.5 + profit_prob
        return score

    def price_option(self, current_price, strike_price, volatility, drift=0, option_type="call"):
        """
        Estimates an option price using Monte Carlo simulation.

        Parameters
        ----------
        current_price : float
            Current asset price.
        strike_price : float
            Option strike price.
        volatility : float
            Log returns volatility.
        drift : float, optional
            Log returns drift.
        option_type : str, optional
            Type of option ("call" or "put").

        Returns
        -------
        float
            The estimated fair price of the option.
        """
        paths = self.simulate_paths(current_price, volatility, drift)
        final_prices = paths[:, -1]
        if option_type == "call":
            payoffs = torch.maximum(final_prices - strike_price, torch.tensor(0.0, device=self.device))
        else:
            payoffs = torch.maximum(torch.tensor(strike_price, device=self.device) - final_prices, torch.tensor(0.0, device=self.device))

        return torch.mean(payoffs).item()

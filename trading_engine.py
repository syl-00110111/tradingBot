# Binance Trading Bot - Trading Engine
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

"""
Core trading engine for position sizing and risk management.

This module handles the calculation of trade amounts and the dynamic adjustment
of technical settings based on market conditions.
"""

import logging

class TradingEngine:
    """
    Main engine for trading logic, including position sizing and dynamic risk adjustment.

    Parameters
    ----------
    config : dict
        The bot configuration.
    """
    def __init__(self, config):
        self.config = config
        self.risk_multiplier = float(config.get('global_risk_multiplier', 1.1))

    def get_dynamic_settings(self, adx, volatility):
        """
        Adjusts technical indicator parameters based on current market regimes.

        The engine identifies three regimes:
        1. Trending (ADX > 25): Uses faster EMAs and more aggressive RSI thresholds.
        2. High Volatility (Volatility > 0.015): Uses slower EMAs and wider RSI
           thresholds to avoid noise.
        3. Extreme Volatility (Volatility > 0.1): Increases the confirmation window
           to 2 signals.
        4. Default: Balanced settings for range-bound or low-volatility markets.

        Parameters
        ----------
        adx : float
            Current Average Directional Index (trend strength).
        volatility : float
            Current market volatility (standard deviation of log returns).

        Returns
        -------
        dict
            A dictionary of technical indicator settings.
        """
        settings = {
            "ema_fast": 20, "ema_slow": 50,
            "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
            "rsi_period": 14, "rsi_buy": 30, "rsi_sell": 70,
            "confirmation_window": 1
        }
        if adx > 25:
            settings.update({
                "ema_fast": 10, "ema_slow": 30,
                "rsi_buy": 40, "rsi_sell": 60
            })
        elif volatility > 0.015:
            settings.update({
                "ema_fast": 30, "ema_slow": 100,
                "rsi_buy": 20, "rsi_sell": 80
            })

        # High volatility adds an additional confirmation signal
        if volatility > 0.1:
            settings["confirmation_window"] = 2

        return settings

    def get_min_exit_price(self, entry_price, fee_rate=0.001):
        """
        Calculates the minimum exit price required to break even, including fees.

        Uses the precise formula:
        Price_exit * (1 - f) = Price_entry * (1 + f)
        Price_exit = Price_entry * (1 + f) / (1 - f)

        Parameters
        ----------
        entry_price : float
            The price at which the asset was bought.
        fee_rate : float, optional
            The exchange fee rate (default is 0.001 for 0.1%).

        Returns
        -------
        float
            The break-even exit price.
        """
        # Precise break-even: Price_exit * (1 - f) = Price_entry * (1 + f)
        # Price_exit = Price_entry * (1 + f) / (1 - f)
        return entry_price * (1 + fee_rate) / (1 - fee_rate)

    def is_profitable(self, current_price, entry_price, fee_rate=0.001):
        """
        Checks if closing a position at the current price would be profitable.

        Parameters
        ----------
        current_price : float
            The current market price.
        entry_price : float
            The position's entry price.
        fee_rate : float, optional
            The exchange fee rate.

        Returns
        -------
        bool
            True if the net profit is positive after fees, False otherwise.
        """
        return current_price > self.get_min_exit_price(entry_price, fee_rate)

    def check_profitability(self, current_price, entry_price, symbol, fee_rate=0.001):
        """
        Alias for is_profitable.
        """
        return self.is_profitable(current_price, entry_price, fee_rate)

    def calculate_position_size(self, balance, current_price, base_currency, win_streak=0):
        """
        Calculates the amount of an asset to buy based on wallet balance and risk.

        Takes into account the `base_trade_amount` (percentage or absolute),
        the `global_risk_multiplier`, and applies an optional bonus for win streaks.

        Parameters
        ----------
        balance : dict or float
            Current wallet balance.
        current_price : float
            The asset's current market price.
        base_currency : str
            The base currency (e.g., 'EUR') used to calculate the cost.
        win_streak : int, optional
            Number of consecutive winning trades for the pair.

        Returns
        -------
        float
            The calculated amount of asset to purchase.
        """
        base_balance = 0
        if isinstance(balance, dict):
            if 'free' in balance: base_balance = balance['free'].get(base_currency, 0)
            else: base_balance = balance.get(base_currency, 0)
        else: base_balance = balance.get(base_currency, 0)

        raw_val = float(self.config.get('base_trade_amount', 9.0))
        base_percentage = raw_val / 100.0 if raw_val >= 1.0 else raw_val
        trade_amount_base = base_balance * base_percentage
        trade_amount_base *= self.risk_multiplier

        ws_config = self.config.get('win_streak_bonus', {})
        if ws_config.get('enabled') and win_streak >= ws_config.get('threshold', 2):
             multiplier = ws_config.get('multiplier', 1.2)
             trade_amount_base *= multiplier
             logging.info(f"Win streak detected ({win_streak}), applying {multiplier}x multiplier. New target: {trade_amount_base:.2f} {base_currency}")

        if trade_amount_base > base_balance: trade_amount_base = base_balance
        if current_price > 0: return trade_amount_base / current_price
        return 0

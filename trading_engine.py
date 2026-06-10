# Binance Trading Bot - Trading Engine
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

import logging

class TradingEngine:
    def __init__(self, config):
        self.config = config
        self.base_currency = config.get('base_currency', 'EUR')
        self.risk_multiplier = float(config.get('global_risk_multiplier', 1.0))

    def get_dynamic_settings(self, adx, volatility):
        """
        Dynamically determines strategy parameters based on market conditions (Regime Detection).
        Based on Baur & Dimpfl (2021) and Zhang (2020).
        """
        # Default 'Balanced' settings
        settings = {
            "ema_fast": 20, "ema_slow": 50,
            "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
            "rsi_period": 14, "rsi_buy": 30, "rsi_sell": 70,
            "confirmation_window": 3
        }

        # Strong Trend (Trend Following - Urquhart 2016)
        if adx > 25:
            settings.update({
                "ema_fast": 10, "ema_slow": 30,
                "rsi_buy": 40, "rsi_sell": 60,
                "confirmation_window": 2
            })
        # High Volatility (Mean Reversion - Baur & Dimpfl 2021)
        elif volatility > 0.015:
            settings.update({
                "ema_fast": 30, "ema_slow": 100,
                "rsi_buy": 20, "rsi_sell": 80,
                "confirmation_window": 4
            })

        # Apply risk multiplier to confirmation window
        # (higher risk = smaller window = faster entry)
        settings["confirmation_window"] = max(1, int(settings["confirmation_window"] / self.risk_multiplier))

        return settings

    def is_profitable(self, current_price, entry_price, fee_rate=0.001):
        """
        Returns True if current_price covers entry_price plus fees.
        Profitability formula: current_price > entry_price * (1 + fee_rate * 2)
        """
        min_exit_price = entry_price * (1 + fee_rate * 2)
        return current_price > min_exit_price

    def check_profitability(self, current_price, entry_price, symbol, fee_rate=0.001):
        """
        Check if selling now would be profitable after fees.
        Only enforces profitability if secure_sell is True in config.
        """
        if not self.config.get('secure_sell', False):
            return True

        profitable = self.is_profitable(current_price, entry_price, fee_rate)
        if not profitable:
            min_exit_price = entry_price * (1 + fee_rate * 2)
            def fmt(p):
                return f"{p:.3e}" if p < 0.01 else f"{p:.2f}"
            logging.info(f"[{symbol}] Profitability check failed: {fmt(current_price)} <= {fmt(min_exit_price)}")
        return profitable

    def calculate_position_size(self, balance, current_price, win_streak=0):
        """
        Calculate position size based on available balance and optional win streak bonus.
        """
        base_balance = 0
        if isinstance(balance, dict) and 'free' in balance:
            base_balance = balance['free'].get(self.base_currency, 0)
        else:
            base_balance = balance.get(self.base_currency, 0)

        # Determine base amount: use config 'base_trade_amount'
        base_amount = self.config.get('base_trade_amount')

        if base_amount is not None:
             trade_amount_base = float(base_amount)
        else:
             # Fallback if base_trade_amount is missing
             trade_amount_base = 20.0

        # Apply Global Risk Multiplier
        trade_amount_base *= self.risk_multiplier

        # Apply Win Streak Bonus
        ws_config = self.config.get('win_streak_bonus', {})
        if ws_config.get('enabled') and win_streak >= ws_config.get('threshold', 2):
             multiplier = ws_config.get('multiplier', 1.2)
             trade_amount_base *= multiplier
             logging.info(f"Win streak detected ({win_streak}), applying {multiplier}x multiplier. New target: {trade_amount_base:.2f} {self.base_currency}")

        # Ensure we don't exceed available balance
        if trade_amount_base > base_balance:
             trade_amount_base = base_balance

        if current_price > 0:
            return trade_amount_base / current_price
        return 0

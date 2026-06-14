# Crypto-Currencies MultiPlatform Trading Bot - Trading Engine
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import logging

class TradingEngine:
    def __init__(self, config):
        self.config = config
        self.risk_multiplier = float(config.get('global_risk_multiplier', 1.2))

    def parse_base_bet(self):
        """
        Parses base_bet as a percentage of the available balance.
        Example: "10%" or 10.0 -> returns 0.10.
        """
        raw_val = self.config.get('base_trade_amount', self.config.get('base_bet', '10%'))
        if isinstance(raw_val, str):
            try:
                val = float(raw_val.replace('%', '').strip())
                return val / 100.0 if val >= 1.0 else val
            except ValueError:
                return 0.10
        return float(raw_val) / 100.0 if float(raw_val) >= 1.0 else float(raw_val)

    def get_dynamic_settings(self, adx, volatility):
        settings = {
            "ema_fast": 9, "ema_slow": 21,
            "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
            "rsi_period": 14, "rsi_buy": 30, "rsi_sell": 70,
        }
        if adx > 25:
            settings.update({
                "ema_fast": 10, "ema_slow": 30,
                "rsi_buy": 40, "rsi_sell": 60,
            })
        elif volatility > self.config.get('profit_thresholds', {}).get('min_pattern_profit', 0.015):
            settings.update({
                "ema_fast": 30, "ema_slow": 100,
                "rsi_buy": 20, "rsi_sell": 80,
            })
        return settings

    def is_profitable(self, current_price, entry_price, fee_rate=0.001):
        min_exit_price = entry_price * (1 + fee_rate * 2)
        return current_price > min_exit_price

    def check_profitability(self, current_price, entry_price, symbol, fee_rate=0.001):
        return self.is_profitable(current_price, entry_price, fee_rate)

    def calculate_position_size(self, balance, current_price, base_currency, win_streak=0, exchange=None):
        """
        Calculates position size based on a percentage of the available balance of the quote asset.
        """
        base_balance = 0
        if isinstance(balance, dict):
            if 'free' in balance: base_balance = balance['free'].get(base_currency, 0)
            else: base_balance = balance.get(base_currency, 0)
        else: base_balance = balance.get(base_currency, 0)

        base_percentage = self.parse_base_bet()
        trade_amount_base = base_balance * base_percentage
        trade_amount_base *= self.risk_multiplier

        ws_config = self.config.get('win_streak_bonus', {})
        if ws_config.get('enabled') and win_streak >= ws_config.get('threshold', 2):
             multiplier = ws_config.get('multiplier', 1.3)
             trade_amount_base *= multiplier
             logging.info(f"Win streak detected ({win_streak}), applying {multiplier}x multiplier. New target: {trade_amount_base:.2f} {base_currency}")

        if trade_amount_base > base_balance: trade_amount_base = base_balance
        if current_price > 0: return trade_amount_base / current_price
        return 0

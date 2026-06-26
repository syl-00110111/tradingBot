# Binance Trading Bot - Persistence & State Management
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

"""
Persistence and state management for the trading bot.

This module manages in-memory and potentially persistent storage for trade
history, open positions, discovered patterns, and short-term caches.
"""

import json
import os
import time
import logging

class PatternManager:
    """
    Manager for storing and retrieving technical patterns for similarity matching.
    """
    def __init__(self):
        self.data = {}

    def set_patterns(self, symbol, patterns):
        """
        Stores the top 4 successful patterns for a symbol.

        Parameters
        ----------
        symbol : str
            The trading pair symbol.
        patterns : list of dict
            A list of discovered patterns.
        """
        self.data[symbol] = patterns[:4]

    def get_patterns(self, symbol):
        """
        Retrieves stored patterns for a symbol.

        Parameters
        ----------
        symbol : str
            The trading pair symbol.

        Returns
        -------
        list of dict
            The list of stored patterns.
        """
        return self.data.get(symbol, [])

class DataManager:
    """
    Manager for the bot's trading state, including open positions and trade history.

    Parameters
    ----------
    mode : str, optional
        The operation mode (default is 'simulation').
    """
    def __init__(self, mode='simulation'):
        self.mode = mode
        self.filename = f"trades_{mode}.json"
        self.data = {"open_positions": {}, "trade_history": []}
        self.load()

    def _save(self):
        # Local persistence disabled (trades synced via API)
        pass

    def load(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    self.data = json.load(f)
            except Exception as e:
                logging.error(f"Failed to load trade data: {e}")

    def clear_history(self):
        """
        Resets the trade history and open positions.
        """
        self.data = {"open_positions": {}, "trade_history": []}

    def add_position(self, symbol, entry_price, amount, fee, trigger_data, timestamp, total_base=0):
        """
        Records a new open position.

        Parameters
        ----------
        symbol : str
            The trading pair symbol.
        entry_price : float
            The price at which the asset was purchased.
        amount : float
            The amount of asset purchased.
        fee : float
            The fee paid for the entry order.
        trigger_data : dict
            Technical indicator data at the time of entry.
        timestamp : float
            The entry timestamp.
        total_base : float, optional
            The total cost in base currency (default is 0).
        """
        self.data["open_positions"][symbol] = {
            "entry_price": entry_price, "amount": amount, "entry_fee": fee,
            "entry_total_base": total_base, "trigger_data": trigger_data,
            "timestamp": timestamp, "sell_signals_received": 0, "last_sell_signal_candle_ts": None
        }
        self._save()

    def increment_sell_signals(self, symbol, candle_ts):
        """
        Increments the count of consecutive sell signals for a position.

        Parameters
        ----------
        symbol : str
            The trading pair symbol.
        candle_ts : int or float
            The timestamp of the current candle.

        Returns
        -------
        bool
            True if the signal count was incremented, False otherwise.
        """
        if symbol in self.data["open_positions"]:
            pos = self.data["open_positions"][symbol]
            if pos.get("last_sell_signal_candle_ts") != candle_ts:
                pos["sell_signals_received"] = pos.get("sell_signals_received", 0) + 1
                pos["last_sell_signal_candle_ts"] = candle_ts
                return True
        return False

    def flag_ignore_sell(self, symbol, value=True):
        """
        Flags a position to ignore future sell signals.

        Parameters
        ----------
        symbol : str
            The trading pair symbol.
        value : bool, optional
            The flag value (default is True).
        """
        if symbol in self.data["open_positions"]:
            self.data["open_positions"][symbol]["ignore_sell"] = value
            self._save()

    def close_position(self, symbol, exit_price, exit_fee, profit, trigger_data, timestamp, total_base=0):
        """
        Closes an open position and moves it to trade history.

        Parameters
        ----------
        symbol : str
            The trading pair symbol.
        exit_price : float
            The price at which the asset was sold.
        exit_fee : float
            The fee paid for the exit order.
        profit : float
            The net profit/loss for the trade.
        trigger_data : dict
            Technical indicator data at the time of exit.
        timestamp : float
            The exit timestamp.
        total_base : float, optional
            The total received in base currency (default is 0).

        Returns
        -------
        dict or None
            The recorded trade data, or None if no open position was found.
        """
        if symbol in self.data["open_positions"]:
            position = self.data["open_positions"].pop(symbol)
            trade = {
                "symbol": symbol, "entry_price": position["entry_price"], "exit_price": exit_price,
                "amount": position["amount"], "entry_fee": position.get("entry_fee", 0),
                "entry_total_base": position.get("entry_total_base", 0), "exit_fee": exit_fee,
                "exit_total_base": total_base, "profit": profit, "entry_trigger": position.get("trigger_data", {}),
                "exit_trigger": trigger_data, "entry_timestamp": position["timestamp"], "exit_timestamp": timestamp,
                "sell_signals_received": position.get("sell_signals_received", 0)
            }
            self.data["trade_history"].append(trade)
            self._save()
            return trade
        return None

    def get_open_positions(self):
        """
        Retrieves all currently open positions.

        Returns
        -------
        dict
            A dictionary of open positions.
        """
        return self.data["open_positions"]

    def get_position(self, symbol):
        """
        Retrieves a specific open position.

        Parameters
        ----------
        symbol : str
            The trading pair symbol.

        Returns
        -------
        dict or None
            The position data, or None if not found.
        """
        return self.data["open_positions"].get(symbol)

    def get_win_streak(self, symbol):
        """
        Calculates the current win streak for a symbol.

        Parameters
        ----------
        symbol : str
            The trading pair symbol.

        Returns
        -------
        int
            The number of consecutive profitable trades.
        """
        streak = 0
        history = [t for t in self.data.get("trade_history", []) if t.get("symbol") == symbol]
        for trade in reversed(history):
            if trade.get("profit", 0) > 0: streak += 1
            else: break
        return streak

class CacheManager:
    """
    Manager for short-term in-memory caching of discovery results.
    """
    def __init__(self):
        self.cache = {}

    def get(self, symbol, timeframe, max_age_seconds):
        """
        Retrieves cached data if it's not older than max_age_seconds.

        Parameters
        ----------
        symbol : str
            The trading pair symbol.
        timeframe : str
            The timeframe.
        max_age_seconds : int
            Maximum age of the cache in seconds.

        Returns
        -------
        any or None
            The cached data, or None if not found or expired.
        """
        key = f"{symbol}_{timeframe}"
        if key in self.cache:
            entry = self.cache[key]
            if time.time() - entry['timestamp'] < max_age_seconds: return entry['data']
        return None

    def set(self, symbol, timeframe, data):
        """
        Stores data in the cache.

        Parameters
        ----------
        symbol : str
            The trading pair symbol.
        timeframe : str
            The timeframe.
        data : any
            The data to cache.
        """
        key = f"{symbol}_{timeframe}"
        self.cache[key] = {'timestamp': time.time(), 'data': data}

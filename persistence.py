# Binance Trading Bot - Persistence & State Management
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

import json
import os
import time

class DataManager:
    def __init__(self, mode='simulation'):
        self.filepath = f'trades_history_{mode}.json'
        self.data = self._load_data()

    def _load_data(self):
        default_data = {"open_positions": {}, "trade_history": [], "success_patterns": {}}
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r') as f:
                    data = json.load(f)
                    # Ensure legacy files have the required keys
                    if "open_positions" not in data: data["open_positions"] = {}
                    if "trade_history" not in data: data["trade_history"] = []
                    if "success_patterns" not in data: data["success_patterns"] = {}

                    # Initialize missing keys for loaded positions
                    for sym, pos in data["open_positions"].items():
                        if "sell_signals_received" not in pos:
                            pos["sell_signals_received"] = 0
                        if "last_sell_signal_candle_ts" not in pos:
                            pos["last_sell_signal_candle_ts"] = None
                    return data
            except json.JSONDecodeError:
                return default_data
        else:
            return default_data

    def _save_data(self):
        with open(self.filepath, 'w') as f:
            json.dump(self.data, f, indent=4)

    def add_position(self, symbol, entry_price, amount, fee, trigger_data, timestamp, total_base=0):
        self.data["open_positions"][symbol] = {
            "entry_price": entry_price,
            "amount": amount,
            "entry_fee": fee,
            "entry_total_base": total_base,
            "trigger_data": trigger_data,
            "timestamp": timestamp,
            "sell_signals_received": 0,
            "last_sell_signal_candle_ts": None
        }
        self._save_data()

    def increment_sell_signals(self, symbol, candle_ts):
        if symbol in self.data["open_positions"]:
            pos = self.data["open_positions"][symbol]
            if pos.get("last_sell_signal_candle_ts") != candle_ts:
                pos["sell_signals_received"] = pos.get("sell_signals_received", 0) + 1
                pos["last_sell_signal_candle_ts"] = candle_ts
                self._save_data()
                return True
        return False

    def close_position(self, symbol, exit_price, exit_fee, profit, trigger_data, timestamp, total_base=0):
        if symbol in self.data["open_positions"]:
            position = self.data["open_positions"].pop(symbol)
            trade = {
                "symbol": symbol,
                "entry_price": position["entry_price"],
                "exit_price": exit_price,
                "amount": position["amount"],
                "entry_fee": position.get("entry_fee", 0),
                "entry_total_base": position.get("entry_total_base", 0),
                "exit_fee": exit_fee,
                "exit_total_base": total_base,
                "profit": profit,
                "entry_trigger": position.get("trigger_data", {}),
                "exit_trigger": trigger_data,
                "entry_timestamp": position["timestamp"],
                "exit_timestamp": timestamp,
                "sell_signals_received": position.get("sell_signals_received", 0)
            }
            self.data["trade_history"].append(trade)
            self._save_data()
            return trade
        return None

    def get_open_positions(self):
        return self.data["open_positions"]

    def get_position(self, symbol):
        return self.data["open_positions"].get(symbol)

    def set_patterns(self, symbol, patterns):
        """Stores up to 4 patterns for a symbol."""
        if "success_patterns" not in self.data:
            self.data["success_patterns"] = {}
        self.data["success_patterns"][symbol] = patterns[:4]
        self._save_data()

    def get_patterns(self, symbol):
        return self.data.get("success_patterns", {}).get(symbol, [])

    def get_win_streak(self, symbol):
        """Counts consecutive profitable trades for a symbol, starting from the most recent."""
        streak = 0
        history = [t for t in self.data.get("trade_history", []) if t.get("symbol") == symbol]
        for trade in reversed(history):
            if trade.get("profit", 0) > 0:
                streak += 1
            else:
                break
        return streak

class CacheManager:
    def __init__(self, filename='benchmark_cache.json'):
        self.filename = filename
        self.cache = self._load()

    def _load(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    return json.load(f)
            except Exception: return {}
        return {}

    def save(self):
        with open(self.filename, 'w') as f:
            json.dump(self.cache, f, indent=4)

    def get(self, symbol, term, max_age_seconds):
        key = f"{symbol}_{term}"
        if key in self.cache:
            entry = self.cache[key]
            if time.time() - entry['timestamp'] < max_age_seconds:
                return entry['data']
        return None

    def set(self, symbol, term, data):
        key = f"{symbol}_{term}"
        self.cache[key] = {
            'timestamp': time.time(),
            'data': data
        }
        self.save()

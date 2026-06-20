# Cryptocurrencies multiplatform trading bot - In-Memory State Management
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import logging

class DataManager:
    def __init__(self, mode='simulation'):
        self.mode = mode
        self.positions = {}
        self.history = []

    def add_position(self, symbol, entry_price, amount, entry_fee, trigger_data, timestamp, total_base=0):
        if symbol not in self.positions:
            self.positions[symbol] = []

        pos = {
            'entry_price': entry_price,
            'amount': amount,
            'entry_fee': entry_fee,
            'entry_total_base': total_base if total_base > 0 else (entry_price * amount),
            'trigger_data': trigger_data,
            'timestamp': timestamp,
            'ignore_sell': False
        }
        self.positions[symbol].append(pos)


    def close_position(self, symbol, exit_price, exit_fee, profit, trigger_data, timestamp, total_base=0, position_idx=0):
        if symbol in self.positions and position_idx < len(self.positions[symbol]):
            pos = self.positions[symbol].pop(position_idx)

            trade = {
                'symbol': symbol,
                'entry_price': pos['entry_price'],
                'exit_price': exit_price,
                'amount': pos['amount'],
                'entry_fee': pos['entry_fee'],
                'exit_fee': exit_fee,
                'entry_total_base': pos['entry_total_base'],
                'exit_total_base': total_base if total_base > 0 else (exit_price * pos['amount']),
                'profit': profit,
                'trigger_data': trigger_data,
                'entry_timestamp': pos['timestamp'],
                'exit_timestamp': timestamp
            }
            self.history.append(trade)
            if not self.positions[symbol]:
                del self.positions[symbol]
            return True
        return False

    def get_positions(self, symbol):
        return list(self.positions.get(symbol, []))

    def get_position(self, symbol):
        pos_list = self.get_positions(symbol)
        return pos_list[0] if pos_list else None

    def get_open_positions(self):
        return dict(self.positions)

    def flag_ignore_sell(self, symbol, position_idx=0):
        if symbol in self.positions and position_idx < len(self.positions[symbol]):
            self.positions[symbol][position_idx]['ignore_sell'] = True

    def get_win_streak(self, symbol):
        streak = 0
        for trade in reversed(self.history):
            if trade['symbol'] == symbol:
                if trade['profit'] > 0:
                    streak += 1
                else:
                    break
        return streak

    def clear_history(self):
        self.history = []

class PatternManager:
    def __init__(self, filename=None):
        self.patterns = {}

    def get_patterns(self, symbol):
        return list(self.patterns.get(symbol, []))

    def set_patterns(self, symbol, patterns, save=True):
        self.patterns[symbol] = patterns

    def save_all(self):
        pass

class CacheManager:
    def __init__(self, filename=None):
        self.cache = {}

    def get(self, symbol, term):
        key = f"{symbol}_{term}"
        return self.cache.get(key)

    def set(self, symbol, term, data, save=True):
        key = f"{symbol}_{term}"
        self.cache[key] = data

    def save_all(self):
        pass

class OHLCVCacheManager:
    def __init__(self, directory=None, mode='simulation'):
        self.mode = mode
        self.memory_cache = {}

    def get(self, symbol, timeframe):
        key = (symbol, timeframe)
        return self.memory_cache.get(key)

    def set(self, symbol, timeframe, data):
        key = (symbol, timeframe)
        self.memory_cache[key] = data

    def flush_to_disk(self, symbol, timeframe, data):
        pass

    def flush_all(self):
        pass

class MonteCarloCacheManager:
    def __init__(self, directory=None):
        self.cache = {}

    def get(self, strategy_id):
        return self.cache.get(strategy_id)

    def set(self, strategy_id, data):
        self.cache[strategy_id] = data

# Dummy objects for compatibility
def migrate_fresh_files_to_archive():
    pass

def load_from_archive():
    pass

class DummyArchiver:
    def trigger(self): pass
    def stop(self): pass

archiver = DummyArchiver()

# Binance Trading Bot - Persistence & State Management
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import json
import os
import time
import zipfile
import logging
import shutil

ARCHIVE_NAME = 'bot_data_backup.zip'
CACHE_DIR = 'cache'

def create_consolidated_archive():
    """
    Creates/updates a compressed archive of all runtime data files and deletes the source files.
    Includes the cache/ directory and its contents.
    """
    files_to_archive = [
        'success_patterns.json',
        'benchmark_cache.json',
        'ohlcv_cache.pkl',
        'trades_history_live.json',
        'trades_history_simulation.json',
        'trades_history_sell.json'
    ]
    try:
        # We perform a fresh write to ensure consistency as per "Consolidated"
        with zipfile.ZipFile(ARCHIVE_NAME, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # Archive individual files
            for file in files_to_archive:
                if os.path.exists(file):
                    zipf.write(file)

            # Archive cache directory
            if os.path.exists(CACHE_DIR):
                for root, dirs, files in os.walk(CACHE_DIR):
                    for file in files:
                        filepath = os.path.join(root, file)
                        zipf.write(filepath)

        # Delete source files and cache as requested (bot design pattern)
        for file in files_to_archive:
            if os.path.exists(file):
                try: os.remove(file)
                except: pass

        if os.path.exists(CACHE_DIR):
            try: shutil.rmtree(CACHE_DIR)
            except: pass

    except Exception as e:
        logging.error(f"Failed to create consolidated archive: {e}")

def load_from_archive(filename=None):
    """
    Extracts files from the archive. If filename provided, only extracts that one.
    If filename is None, extracts everything including the cache/ directory.
    """
    if not os.path.exists(ARCHIVE_NAME):
        return False
    try:
        with zipfile.ZipFile(ARCHIVE_NAME, 'r') as zipf:
            if filename:
                # Handle filename being a path inside the zip
                if filename in zipf.namelist():
                    zipf.extract(filename)
                    return True
                return False

            # Extract everything
            zipf.extractall()
            return True
    except:
        return False

def migrate_fresh_files_to_archive():
    """
    Compares flat files on disk with the consolidated archive.
    If disk files are fresher or new, they are moved into the archive.
    The archive is consolidated before any computation phase.
    """
    files_to_check = [
        'success_patterns.json',
        'benchmark_cache.json',
        'ohlcv_cache.pkl',
        'trades_history_live.json',
        'trades_history_simulation.json',
        'trades_history_sell.json'
    ]

    # If no archive exists, create one from current disk state
    if not os.path.exists(ARCHIVE_NAME):
        any_file = any(os.path.exists(f) for f in files_to_check) or os.path.exists(CACHE_DIR)
        if any_file:
            logging.info("Initializing bot archive from disk files...")
            create_consolidated_archive()
        return

    try:
        updated = False
        all_disk_files = []
        for f in files_to_check:
            if os.path.exists(f): all_disk_files.append(f)

        if os.path.exists(CACHE_DIR):
            for root, dirs, files in os.walk(CACHE_DIR):
                for file in files:
                    all_disk_files.append(os.path.join(root, file))

        if not all_disk_files: return

        # Open archive to compare timestamps
        with zipfile.ZipFile(ARCHIVE_NAME, 'r') as zipf:
            archive_members = {info.filename: info for info in zipf.infolist()}

            for disk_file in all_disk_files:
                norm_path = disk_file.replace('\\', '/')
                disk_mtime = os.path.getmtime(disk_file)

                if norm_path not in archive_members:
                    updated = True; break

                # Compare disk vs archive timestamp
                z_time = archive_members[norm_path].date_time
                archive_mtime = time.mktime((*z_time, 0, 0, -1))

                if disk_mtime > (archive_mtime + 2): # 2s buffer for FAT/ZIP precision
                    updated = True; break

        if updated:
            logging.info("Found fresher or new data files on disk. Consolidating into archive...")
            create_consolidated_archive()
        else:
            # Files are consistent, clean up disk to force use of archive (Standard Bot Design)
            for f in all_disk_files:
                 try: os.remove(f)
                 except: pass
            if os.path.exists(CACHE_DIR):
                 try: shutil.rmtree(CACHE_DIR)
                 except: pass
    except Exception as e:
        logging.error(f"Error during archive consolidation: {e}")

class PatternManager:
    def __init__(self, filename='success_patterns.json'):
        self.filename = filename
        self.data = self._load()

    def _load(self):
        if not os.path.exists(self.filename):
            load_from_archive(self.filename)

        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    return json.load(f)
            except Exception: return {}
        return {}

    def save(self):
        with open(self.filename, 'w') as f:
            json.dump(self.data, f, indent=4)

    def set_patterns(self, symbol, patterns):
        self.data[symbol] = patterns[:5]
        self.save()

    def get_patterns(self, symbol):
        return self.data.get(symbol, [])

class DataManager:
    def __init__(self, mode='simulation'):
        self.filepath = f'trades_history_{mode}.json'
        self.data = self._load_data()

    def _load_data(self):
        default_data = {"open_positions": {}, "trade_history": []}
        if not os.path.exists(self.filepath):
            load_from_archive(self.filepath)

        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r') as f:
                    data = json.load(f)
                    if "open_positions" not in data: data["open_positions"] = {}
                    if "trade_history" not in data: data["trade_history"] = []
                    # Migration: convert single position (dict) to list of positions
                    for sym, pos in data["open_positions"].items():
                        if isinstance(pos, dict):
                            data["open_positions"][sym] = [pos]

                        # Add missing keys to all positions in the list
                        for p in data["open_positions"][sym]:
                            if "sell_signals_received" not in p: p["sell_signals_received"] = 0
                            if "last_sell_signal_candle_ts" not in p: p["last_sell_signal_candle_ts"] = None
                    return data
            except json.JSONDecodeError:
                return default_data
        else:
            return default_data

    def _save_data(self):
        if not self.data["open_positions"] and not self.data["trade_history"]:
            if os.path.exists(self.filepath):
                os.remove(self.filepath)
            return

        with open(self.filepath, 'w') as f:
            json.dump(self.data, f, indent=4)

    def clear_history(self):
        self.data = {"open_positions": {}, "trade_history": []}
        if os.path.exists(self.filepath):
            os.remove(self.filepath)

    def add_position(self, symbol, entry_price, amount, fee, trigger_data, timestamp, total_base=0):
        if symbol not in self.data["open_positions"]:
            self.data["open_positions"][symbol] = []

        self.data["open_positions"][symbol].append({
            "entry_price": entry_price, "amount": amount, "entry_fee": fee,
            "entry_total_base": total_base, "trigger_data": trigger_data,
            "timestamp": timestamp, "sell_signals_received": 0, "last_sell_signal_candle_ts": None
        })
        self._save_data()

    def increment_sell_signals(self, symbol, candle_ts):
        if symbol in self.data["open_positions"]:
            updated = False
            for pos in self.data["open_positions"][symbol]:
                if pos.get("last_sell_signal_candle_ts") != candle_ts:
                    pos["sell_signals_received"] = pos.get("sell_signals_received", 0) + 1
                    pos["last_sell_signal_candle_ts"] = candle_ts
                    updated = True
            if updated:
                self._save_data()
                return True
        return False

    def flag_ignore_sell(self, symbol, position_idx=0):
        if symbol in self.data["open_positions"] and len(self.data["open_positions"][symbol]) > position_idx:
            self.data["open_positions"][symbol][position_idx]["ignore_sell"] = True
            self._save_data()

    def close_position(self, symbol, exit_price, exit_fee, profit, trigger_data, timestamp, total_base=0, position_idx=0):
        if symbol in self.data["open_positions"] and len(self.data["open_positions"][symbol]) > position_idx:
            position = self.data["open_positions"][symbol].pop(position_idx)
            if not self.data["open_positions"][symbol]:
                self.data["open_positions"].pop(symbol)

            trade = {
                "symbol": symbol, "entry_price": position["entry_price"], "exit_price": exit_price,
                "amount": position["amount"], "entry_fee": position.get("entry_fee", 0),
                "entry_total_base": position.get("entry_total_base", 0), "exit_fee": exit_fee,
                "exit_total_base": total_base, "profit": profit, "entry_trigger": position.get("trigger_data", {}),
                "exit_trigger": trigger_data, "entry_timestamp": position["timestamp"], "exit_timestamp": timestamp,
                "sell_signals_received": position.get("sell_signals_received", 0)
            }
            self.data["trade_history"].append(trade)
            self._save_data()
            return trade
        return None

    def get_open_positions(self): return self.data["open_positions"]
    def get_positions(self, symbol): return self.data["open_positions"].get(symbol, [])
    def get_position(self, symbol):
        # For legacy compatibility, return the first position or None
        pos_list = self.get_positions(symbol)
        return pos_list[0] if pos_list else None

    def get_win_streak(self, symbol):
        streak = 0
        history = [t for t in self.data.get("trade_history", []) if t.get("symbol") == symbol]
        for trade in reversed(history):
            if trade.get("profit", 0) > 0: streak += 1
            else: break
        return streak

class CacheManager:
    """
    Manages benchmark results using individual files for each symbol/term context.
    """
    def __init__(self):
        if not os.path.exists(CACHE_DIR):
            load_from_archive()
            os.makedirs(CACHE_DIR, exist_ok=True)

    def _get_path(self, symbol, term):
        safe_symbol = symbol.replace('/', '_')
        return os.path.join(CACHE_DIR, f"bench_{safe_symbol}_{term}.json")

    def get(self, symbol, term, max_age_seconds):
        path = self._get_path(symbol, term)
        if not os.path.exists(path):
            load_from_archive(path)
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    entry = json.load(f)
                    if time.time() - entry['timestamp'] < max_age_seconds:
                        return entry['data']
            except Exception: pass
        return None

    def set(self, symbol, term, data):
        path = self._get_path(symbol, term)
        try:
            with open(path, 'w') as f:
                json.dump({'timestamp': time.time(), 'data': data}, f, indent=4)
            # Just write to disk. Archiving will happen on bot exit or PatternManager save.
        except Exception as e:
            logging.error(f"Failed to set cache for {symbol}/{term}: {e}")

    def delete(self, symbol, term):
        path = self._get_path(symbol, term)
        if os.path.exists(path):
            try:
                os.remove(path)
                # No immediate archive here to save I/O; exit will handle it.
            except: pass

class MonteCarloCacheManager:
    """
    Manages Monte Carlo validation results using individual files.
    """
    def __init__(self):
        if not os.path.exists(CACHE_DIR):
            load_from_archive()
            os.makedirs(CACHE_DIR, exist_ok=True)

    def _get_path(self, symbol, timeframe, timestamp):
        safe_symbol = symbol.replace('/', '_')
        return os.path.join(CACHE_DIR, f"mc_{safe_symbol}_{timeframe}_{timestamp}.json")

    def get(self, symbol, timeframe, timestamp):
        path = self._get_path(symbol, timeframe, timestamp)
        if not os.path.exists(path):
            load_from_archive(path)
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    return json.load(f).get('score')
            except Exception: pass
        return None

    def set(self, symbol, timeframe, timestamp, score):
        path = self._get_path(symbol, timeframe, timestamp)
        try:
            with open(path, 'w') as f:
                json.dump({'score': score, 'timestamp': time.time()}, f)
            self.cleanup_old_cache(symbol, timeframe)
        except Exception: pass

    def cleanup_old_cache(self, symbol, timeframe, keep=5):
        """
        Keeps only the most recent 'keep' MC cache files for a given symbol and timeframe.
        """
        try:
            safe_symbol = symbol.replace('/', '_')
            prefix = f"mc_{safe_symbol}_{timeframe}_"
            files = [f for f in os.listdir(CACHE_DIR) if f.startswith(prefix) and f.endswith(".json")]
            if len(files) <= keep:
                return

            # Sort by modification time which is safer
            full_paths = [os.path.join(CACHE_DIR, f) for f in files]
            full_paths.sort(key=os.path.getmtime, reverse=True)

            for old_file in full_paths[keep:]:
                try: os.remove(old_file)
                except: pass
        except Exception: pass

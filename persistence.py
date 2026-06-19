# Cryptocurrencies multiplatform trading bot - Persistence & State Management
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import json
import os
import time
import zipfile
import logging
import shutil
import pickle
import threading
import queue

ARCHIVE_NAME = 'bot_data_backup.zip'
CACHE_DIR = 'cache'
OHLCV_DIR = os.path.join(CACHE_DIR, 'ohlcv')

# Global re-entrant lock for all file and archive operations to prevent race conditions
persistence_lock = threading.RLock()

# Global Async Archiver
class AsyncArchiver(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.queue = queue.Queue()
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.is_set():
            try:
                # Wait for a request, but timeout to check stop event
                self.queue.get(timeout=1)
                # Clear queue to group multiple requests
                while not self.queue.empty():
                    self.queue.get_nowait()

                logging.debug("Async Archiver: Updating archive...")
                # Async updates don't delete from disk to avoid the overwrite-empty-zip bug
                create_consolidated_archive(delete_after=False)

                self.queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logging.error(f"Async Archiver error: {e}")

    def trigger(self):
        self.queue.put(True)

    def stop(self):
        self._stop_event.set()
        if self.is_alive():
            # Final consolidation on stop - here we CAN delete after archiving
            logging.info("Async Archiver: Finalizing archive before exit...")
            create_consolidated_archive(delete_after=True)
            try:
                self.join(timeout=5)
            except RuntimeError:
                pass

# Global archiver instance
archiver = AsyncArchiver()
archiver.start()

def create_consolidated_archive(delete_after=True):
    """
    Creates/updates a compressed archive of all runtime data files.
    Ensures that existing data in the archive is preserved if not present on disk.
    """
    files_to_archive = [
        'success_patterns.json',
        'benchmark_cache.json',
        'trades_history_live.json',
        'trades_history_simulation.json'
    ]

    with persistence_lock:
        try:
            # 1. Collect what we currently have on disk
            present_on_disk = []
            for f in files_to_archive:
                if os.path.exists(f):
                    present_on_disk.append(f)

            # Also include everything in cache/ohlcv/
            if os.path.exists(OHLCV_DIR):
                for f in os.listdir(OHLCV_DIR):
                    if f.endswith('.pkl'):
                        present_on_disk.append(os.path.join(OHLCV_DIR, f))

            if not present_on_disk and not os.path.exists(ARCHIVE_NAME):
                return

            # Normalize present_on_disk to forward slashes for membership check
            present_normalized = [f.replace('\\', '/') for f in present_on_disk]

            # 2. Create temporary archive
            tmp_archive = ARCHIVE_NAME + '.tmp'

            with zipfile.ZipFile(tmp_archive, 'w', zipfile.ZIP_DEFLATED) as z_new:
                # First, copy EVERYTHING from the old archive that is NOT currently on disk
                if os.path.exists(ARCHIVE_NAME):
                    with zipfile.ZipFile(ARCHIVE_NAME, 'r') as z_old:
                        for item in z_old.infolist():
                            # Zip files ALWAYS use forward slashes
                            if item.filename not in present_normalized:
                                z_new.writestr(item, z_old.read(item.filename))

                # Then, add everything currently on disk (this overwrites if it was in the old zip)
                for f in present_on_disk:
                    # Explicitly use forward slashes for the archive name
                    z_new.write(f, arcname=f.replace('\\', '/'))

            # 3. Atomic replacement
            if os.path.exists(ARCHIVE_NAME):
                os.remove(ARCHIVE_NAME)
            os.rename(tmp_archive, ARCHIVE_NAME)

            # 4. Cleanup disk if requested
            if delete_after:
                for f in present_on_disk:
                    try:
                        os.remove(f)
                    except: pass
                # Try to remove empty directories
                try:
                    if os.path.exists(OHLCV_DIR) and not os.listdir(OHLCV_DIR):
                        os.rmdir(OHLCV_DIR)
                    if os.path.exists(CACHE_DIR) and not os.listdir(CACHE_DIR):
                        os.rmdir(CACHE_DIR)
                except: pass

        except Exception as e:
            logging.error(f"Failed to consolidate archive: {e}")
            if os.path.exists(tmp_archive):
                try: os.remove(tmp_archive)
                except: pass

def load_from_archive():
    """Extracts files from the consolidated archive back to disk."""
    if not os.path.exists(ARCHIVE_NAME) or os.path.getsize(ARCHIVE_NAME) == 0:
        return

    with persistence_lock:
        try:
            with zipfile.ZipFile(ARCHIVE_NAME, 'r') as z:
                if not z.namelist():
                    return
                z.extractall()
            logging.info(f"Successfully restored data from {ARCHIVE_NAME}")
        except Exception as e:
            logging.error(f"Failed to load from archive: {e}")

def migrate_fresh_files_to_archive():
    """One-time migration at startup to ensure disk files are in the zip and cleaned up."""
    files_to_archive = [
        'success_patterns.json',
        'benchmark_cache.json',
        'trades_history_live.json',
        'trades_history_simulation.json'
    ]
    present = any(os.path.exists(f) for f in files_to_archive)
    if not present and os.path.exists(OHLCV_DIR):
        try:
            if any(f.endswith('.pkl') for f in os.listdir(OHLCV_DIR)):
                present = True
        except: pass

    if present:
        logging.info("Syncing disk files to archive...")
        create_consolidated_archive(delete_after=True)

class DataManager:
    def __init__(self, mode='simulation'):
        self.mode = mode
        self.history_file = f'trades_history_{mode}.json'
        self.positions = {}
        self.history = []
        self._load()

    def _load(self):
        with persistence_lock:
            if os.path.exists(self.history_file):
                try:
                    with open(self.history_file, 'r') as f:
                        data = json.load(f)
                        self.positions = data.get('positions', {})
                        self.history = data.get('history', [])
                except Exception as e:
                    logging.error(f"Failed to load history: {e}")

    def _save(self):
        with persistence_lock:
            try:
                with open(self.history_file, 'w') as f:
                    json.dump({'positions': self.positions, 'history': self.history}, f, indent=4)
                # Request async archival
                archiver.trigger()
            except Exception as e:
                logging.error(f"Failed to save history: {e}")

    def add_position(self, symbol, entry_price, amount, entry_fee, trigger_data, timestamp, total_base=0, term="short"):
        if symbol not in self.positions:
            self.positions[symbol] = []

        pos = {
            'entry_price': entry_price,
            'amount': amount,
            'entry_fee': entry_fee,
            'entry_total_base': total_base if total_base > 0 else (entry_price * amount),
            'trigger_data': trigger_data,
            'timestamp': timestamp,
            'ignore_sell': False,
            'term': term
        }
        self.positions[symbol].append(pos)
        self._save()

    def update_position_term(self, symbol, position_idx, new_term):
        if symbol in self.positions and position_idx < len(self.positions[symbol]):
            self.positions[symbol][position_idx]['term'] = new_term
            self._save()
            return True
        return False

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
            self._save()
            return True
        return False

    def get_positions(self, symbol):
        return self.positions.get(symbol, [])

    def get_position(self, symbol):
        pos_list = self.get_positions(symbol)
        return pos_list[0] if pos_list else None

    def get_open_positions(self):
        return self.positions

    def flag_ignore_sell(self, symbol, position_idx=0):
        if symbol in self.positions and position_idx < len(self.positions[symbol]):
            self.positions[symbol][position_idx]['ignore_sell'] = True
            self._save()

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
        self._save()

class PatternManager:
    def __init__(self, filename='success_patterns.json'):
        self.filename = filename
        self.patterns = {}
        self._load()

    def _load(self):
        with persistence_lock:
            if os.path.exists(self.filename):
                try:
                    with open(self.filename, 'r') as f:
                        self.patterns = json.load(f)
                except Exception as e:
                    logging.error(f"Failed to load patterns: {e}")

    def _save(self):
        with persistence_lock:
            try:
                with open(self.filename, 'w') as f:
                    json.dump(self.patterns, f, indent=4)
                archiver.trigger()
            except Exception as e:
                logging.error(f"Failed to save patterns: {e}")

    def get_patterns(self, symbol):
        return self.patterns.get(symbol, [])

    def set_patterns(self, symbol, patterns, save=True):
        self.patterns[symbol] = patterns
        if save: self._save()

    def save_all(self):
        self._save()

class CacheManager:
    def __init__(self, filename='benchmark_cache.json'):
        self.filename = filename
        self.cache = {}
        self._load()

    def _load(self):
        with persistence_lock:
            if os.path.exists(self.filename):
                try:
                    with open(self.filename, 'r') as f:
                        self.cache = json.load(f)
                except Exception as e:
                    logging.error(f"Failed to load benchmark cache: {e}")

    def _save(self):
        with persistence_lock:
            try:
                with open(self.filename, 'w') as f:
                    json.dump(self.cache, f, indent=4)
                archiver.trigger()
            except Exception as e:
                logging.error(f"Failed to save benchmark cache: {e}")

    def get(self, symbol, term):
        key = f"{symbol}_{term}"
        return self.cache.get(key)

    def set(self, symbol, term, data, save=True):
        key = f"{symbol}_{term}"
        self.cache[key] = data
        if save: self._save()

    def save_all(self):
        self._save()

class OHLCVCacheManager:
    def __init__(self, directory=OHLCV_DIR, mode='simulation'):
        self.directory = directory
        self.mode = mode
        self.memory_cache = {}
        # Perfect Trader: memory-optimized live mode
        if self.mode != 'live' and not os.path.exists(self.directory):
            os.makedirs(self.directory, exist_ok=True)

    def _get_path(self, symbol, timeframe):
        safe_sym = symbol.replace('/', '_')
        return os.path.join(self.directory, f"{safe_sym}_{timeframe}.pkl")

    def get(self, symbol, timeframe):
        key = (symbol, timeframe)
        if key in self.memory_cache:
            return self.memory_cache[key]

        path = self._get_path(symbol, timeframe)
        with persistence_lock:
            if os.path.exists(path):
                try:
                    with open(path, 'rb') as f:
                        data = pickle.load(f)
                        self.memory_cache[key] = data
                        return data
                except Exception as e:
                    logging.error(f"Failed to load OHLCV cache for {symbol}: {e}")
        return None

    def set(self, symbol, timeframe, data):
        key = (symbol, timeframe)
        self.memory_cache[key] = data

        if self.mode == 'live':
            return

        self.flush_to_disk(symbol, timeframe, data)

    def flush_to_disk(self, symbol, timeframe, data):
        path = self._get_path(symbol, timeframe)
        with persistence_lock:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'wb') as f:
                    pickle.dump(data, f)
                archiver.trigger()
            except Exception as e:
                logging.error(f"Failed to save OHLCV cache for {symbol}: {e}")

    def flush_all(self):
        logging.info("Forcing flush of all memory-cached candles to disk...")
        for (symbol, timeframe), data in self.memory_cache.items():
            self.flush_to_disk(symbol, timeframe, data)

class MonteCarloCacheManager:
    def __init__(self, directory=os.path.join(CACHE_DIR, 'mc')):
        self.directory = directory
        if not os.path.exists(self.directory):
            os.makedirs(self.directory, exist_ok=True)

    def _get_path(self, strategy_id):
        return os.path.join(self.directory, f"{strategy_id}.pkl")

    def get(self, strategy_id):
        path = self._get_path(strategy_id)
        with persistence_lock:
            if os.path.exists(path):
                try:
                    with open(path, 'rb') as f:
                        return pickle.load(f)
                except Exception as e:
                    logging.error(f"Failed to load MC cache for {strategy_id}: {e}")
        return None

    def set(self, strategy_id, data):
        path = self._get_path(strategy_id)
        with persistence_lock:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'wb') as f:
                    pickle.dump(data, f)
                archiver.trigger()
            except Exception as e:
                logging.error(f"Failed to save MC cache for {strategy_id}: {e}")

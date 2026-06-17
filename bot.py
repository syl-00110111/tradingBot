# Cryptocurrencies multiplatform trading bot
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
import time
import logging
import argparse
import os
import copy
import pandas as pd
import sys
import threading
import signal
import random
import concurrent.futures
import queue
import torch
import gc
import psutil
import importlib.util
from datetime import datetime

from rich.live import Live
from rich.logging import RichHandler
from rich.console import Console
from rich.table import Table

import dashboard
from exchange_handler import EXCHANGE_MAPPING, MockExchange, fetch_ohlcv_incremental
from indicators import get_signals, get_common_indicators, calculate_similarity, STRATEGIES
from persistence import DataManager, CacheManager, PatternManager, OHLCVCacheManager, archiver, migrate_fresh_files_to_archive, load_from_archive
import trading_engine
from trading_engine import TradingEngine
from monte_carlo import MonteCarloEngine
import utils
from utils import format_price, format_amount, get_base_currency, play_sound, silent_worker_init, load_config, load_config_from_path

# Global objects
ohlcv_cache_manager = None
global_pattern_pool = []
candle_queue = queue.PriorityQueue()
analysis_queue = queue.PriorityQueue()
execution_queue = queue.Queue()
pending_analysis = set()
pending_downloads = set()
last_download_time = {}
bot_state = {}
available_assets = []
benchmarking_pairs = set()
suspended_pairs = set()
signal_arrival_times = {}
bot_lock = threading.RLock()
shutdown_event = threading.Event()
pending_asset_update = False

console = Console()
ui = dashboard.DashboardUI(console)

class CandleDownloader(threading.Thread):
    def __init__(self, exchange, ohlcv_cache_manager):
        super().__init__(daemon=True)
        self.exchange = exchange
        self.ohlcv_cache_manager = ohlcv_cache_manager

    def run(self):
        while not shutdown_event.is_set():
            try:
                # Priority, Symbol, Timeframe, Limit, Since
                item = candle_queue.get(timeout=1)
                priority, symbol, timeframe, limit, since = item

                try:
                    fetch_ohlcv_incremental(self.exchange, symbol, timeframe, self.ohlcv_cache_manager, limit=limit, since=since)
                finally:
                    with bot_lock:
                        pending_downloads.discard((symbol, timeframe))
                        last_download_time[(symbol, timeframe)] = time.time()
                    candle_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logging.error(f"CandleDownloader error: {e}")

def update_available_assets_live(exchange, config):
    global pending_asset_update
    time.sleep(random.uniform(3.0, 10.0))
    try:
        new_assets = trading_engine.get_sellable_assets(exchange, config)
        with bot_lock:
            available_assets[:] = new_assets
            pending_asset_update = False
    except Exception as e:
        logging.error(f"Failed to update assets from API: {e}")
        with bot_lock: pending_asset_update = False

def analyze_pair(exchange, data_manager, pattern_manager, symbol, pair_config, global_config, engine=None):
    try:
        patterns = pattern_manager.get_patterns(symbol)

        # Determine analysis term based on open positions
        open_positions = data_manager.get_positions(symbol) if data_manager else []
        term_order = ['short', 'medium', 'long']
        active_term = global_config.get('_active_term', 'short')

        if open_positions:
            # Find the "longest" term among open positions for this symbol
            max_term_idx = max([term_order.index(p.get('term', 'short')) for p in open_positions])
            term = term_order[max_term_idx]
        else:
            term = pair_config.get('term_override', active_term)

        term_cfg = global_config.get('expected_profit_terms', {}).get(term, {})
        timeframe = term_cfg.get('timeframe', '5m')

        # Request candle update from background downloader
        with bot_lock:
            download_key = (symbol, timeframe)
            now_ts = time.time()
            if download_key not in pending_downloads and (now_ts - last_download_time.get(download_key, 0) > 30):
                priority = 0 if open_positions else 1
                pending_downloads.add(download_key)
                candle_queue.put((priority, symbol, timeframe, 500, None))

        cached = ohlcv_cache_manager.get(symbol, timeframe)
        if not cached:
            return None

        ohlcv = cached[-500:]
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        if df.empty:
            return None

        device = global_config.get('device', torch.device('cpu'))
        df = get_common_indicators(df, device)

        with bot_lock:
            current_data = bot_state.get(symbol, {})
            current_pattern_id = current_data.get('active_pattern_id')
            pattern_match_ts = current_data.get('pattern_match_ts', 0)
            last_mc_ts = current_data.get('last_mc_ts', 0)
            last_mc_score = current_data.get('mc_score', 1.1)
            cached_sim_candidates = current_data.get('cached_sim_candidates', [])
            last_processed_ts = current_data.get('last_processed_ts', 0)

        latest_row_base = df.iloc[-1]
        candle_ts = latest_row_base['timestamp']

        # Efficiency optimization: only re-process if new data or pattern list changed
        if candle_ts == last_processed_ts and not benchmarking_pairs:
             return current_data
        with bot_lock:
            current_global_pool = list(global_pattern_pool)
        search_pool = patterns + current_global_pool; active_patterns = []
        device = global_config.get('device', torch.device('cpu'))
        if search_pool:
            for p in search_pool:
                p_len = len(p['prices'])
                if len(df) < p_len: continue
                buffer_window = df.iloc[-p_len:]; sim = calculate_similarity(buffer_window, p, device=device)
                if sim > 0.70:
                    p_copy = p.copy()
                    active_patterns.append({'sim': sim, 'pattern': p_copy})

        active_pattern = None
        tf_map = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}
        tf_secs = tf_map.get(timeframe, 300)

        candidates = []
        for item in active_patterns:
            p = item['pattern']
            p_id = f"{p.get('symbol')}_{p.get('start_time')}_{p.get('strategy')}"
            p_match_ts = pattern_match_ts if p_id == current_pattern_id else candle_ts
            p_len = len(p['prices'])
            p_expired = (abs(candle_ts - p_match_ts) // tf_secs) >= p_len

            p_tech = p.get('tech_state', {})
            curr_adx = latest_row_base.get('adx', 0)
            curr_vol = latest_row_base.get('volatility', 0)
            p_adx = p_tech.get('adx', curr_adx)
            p_vol = p_tech.get('volatility', curr_vol)

            p_regime_shift = False
            if p_adx > 0 and abs(curr_adx - p_adx) / p_adx > 0.50: p_regime_shift = True
            if p_vol > 0 and abs(curr_vol - p_vol) / p_vol > 0.50: p_regime_shift = True

            item['expired'] = p_expired
            item['regime_shift'] = p_regime_shift
            item['id'] = p_id
            candidates.append(item)

        valid_candidates = []
        if candidates:
            # Check if we should reuse MC scores based on the 5% threshold
            # Total real-time duration of pattern: length * timeframe_seconds
            p_len_max = max([len(c['pattern']['prices']) for c in candidates])
            p_duration_secs = p_len_max * tf_secs
            spm_threshold_secs = p_duration_secs * 0.05

            can_reuse_mc = (current_pattern_id is not None) and (time.time() - last_mc_ts < spm_threshold_secs)

            if can_reuse_mc and last_mc_score > 0:
                 # Reuse last score for candidates matching current pattern ID
                 for c in candidates:
                     p = c['pattern']
                     p_id = f"{p.get('symbol')}_{p.get('start_time')}_{p.get('strategy')}"
                     if p_id == current_pattern_id:
                          p['mc_score'] = last_mc_score
                          valid_candidates.append(c)
                 if not valid_candidates: can_reuse_mc = False # Force recalculate if current ID not found

            if not can_reuse_mc:
                def validate_candidate_mc(c):
                    p = c['pattern']
                    mc_engine = MonteCarloEngine(num_simulations=1000, timeframe_candles=20)
                    mc_engine.set_device(device)
                    temp_cfg = {'strategy': p['strategy'], 'device': device}
                    df_mc = get_signals(df.tail(100).copy(), temp_cfg, is_backtest=False)
                    score = mc_engine.validate_strategy(df_mc)
                    hurdle = global_config.get('profit_thresholds', {}).get('mc_validation_hurdle', 0.0015)
                    p['mc_score'] = score
                    return c, (score > 1.0 + hurdle)

                with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(candidates), 10)) as mc_executor:
                    mc_futures = [mc_executor.submit(validate_candidate_mc, c) for c in candidates]
                    for f in concurrent.futures.as_completed(mc_futures):
                        try:
                            c_res, is_valid = f.result()
                            if is_valid:
                                valid_candidates.append(c_res)
                        except Exception as e:
                            logging.error(f"Error in MC validation for {symbol}: {e}")
                last_mc_ts = time.time()

        active_pattern_id = None
        if valid_candidates:
            valid_candidates.sort(key=lambda x: x['sim'], reverse=True)
            active_pattern = valid_candidates[0]['pattern']
            active_pattern_id = valid_candidates[0]['id']
        elif candidates:
            candidates.sort(key=lambda x: x['sim'], reverse=True)
            active_pattern = candidates[0]['pattern']
            active_pattern_id = candidates[0]['id']

        if active_pattern:
            with bot_lock:
                if symbol in bot_state:
                    bot_state[symbol]['scan_attempts'] = 0
                    bot_state[symbol]['next_scan_allowed'] = 0

            if active_pattern_id != current_pattern_id:
                pattern_match_ts = candle_ts
                current_pattern_id = active_pattern_id

            p_len = len(active_pattern['prices'])
            expired = (abs(candle_ts - pattern_match_ts) // tf_secs) >= p_len
            p_tech = active_pattern.get('tech_state', {})
            curr_adx = latest_row_base.get('adx', 0)
            curr_vol = latest_row_base.get('volatility', 0)
            p_adx = p_tech.get('adx', curr_adx)
            p_vol = p_tech.get('volatility', curr_vol)
            regime_shift = False
            if p_adx > 0 and abs(curr_adx - p_adx) / p_adx > 0.50: regime_shift = True
            if p_vol > 0 and abs(curr_vol - p_vol) / p_vol > 0.50: regime_shift = True

            if expired or regime_shift:
                p_len = len(active_pattern['prices'])
                p_duration_secs = p_len * tf_secs
                spm_threshold_secs = p_duration_secs * 0.05

                # Instruction: only re-benchmark if at least 5% of pattern duration has elapsed
                if (time.time() - pattern_match_ts) > spm_threshold_secs:
                    with bot_lock: benchmarking_pairs.add(symbol)

            strategy = active_pattern['strategy']
            aggr = active_pattern['aggr']

            settings = engine.get_dynamic_settings(curr_adx, curr_vol)
            settings.update({'strategy': strategy, 'device': device})
            df = get_signals(df, settings, is_backtest=False)
            latest = df.iloc[-1]

            new_data = {
                'price': latest['close'],
                'mc_score': active_pattern.get('mc_score', 1.1),
                'ema_f': latest.get('ema_f', 0),
                'ema_s': latest.get('ema_s', 0),
                'macd_hist': latest.get('macd_hist', 0),
                'rsi': latest.get('rsi', 0),
                'adx': latest.get('adx', 0),
                'volatility': latest.get('volatility', 0),
                'whale_active': latest.get('whale_active', 0),
                'is_mean_rev': latest.get('is_mean_rev', 0),
                'tendency': latest.get('tendency', 'Neutral'),
                'buy_signal': latest['buy_signal'],
                'sell_signal': latest['sell_signal'],
                'strategy': strategy,
                'aggr': aggr,
                'bench_profit': active_pattern.get('avg_bench_profit', active_pattern['profit']),
                'active_pattern_id': active_pattern_id,
                'pattern_match_ts': pattern_match_ts,
                'last_mc_ts': last_mc_ts,
                'mc_score': active_pattern.get('mc_score', 1.1),
                'last_processed_ts': candle_ts,
                'last_20_candles': {'prices': df['close'].tail(20).tolist(), 'volumes': df['volume'].tail(20).tolist()}
            }
            return new_data
        else:
            with bot_lock:
                if symbol in bot_state:
                    bot_state[symbol]['scan_attempts'] = bot_state[symbol].get('scan_attempts', 0) + 1
                    attempts = bot_state[symbol]['scan_attempts']
                    # Linear backoff: 1 min, 2 min, 3 min...
                    bot_state[symbol]['next_scan_allowed'] = time.time() + (attempts * 60)
                    logging.info(f"[{symbol}] No profitable patterns found (Attempt {attempts}). Next scan allowed in {attempts} minutes.")

                    if attempts >= 5:
                        # Switch timeframe
                        current_term = pair_config.get('term_override', global_config.get('_active_term', 'short'))
                        term_order = ['short', 'medium', 'long']
                        if current_term in term_order:
                            idx = term_order.index(current_term)
                            if idx < len(term_order) - 1:
                                new_term = term_order[idx + 1]
                                # Note: This affects global term for this pair if we can isolate it,
                                # but currently _active_term is global.
                                # Let's see if we can override it in pair_config.
                                pair_config['term_override'] = new_term
                                bot_state[symbol]['scan_attempts'] = 0
                                logging.info(f"[{symbol}] Max scan attempts reached. Switching to {new_term} term.")
                            else:
                                logging.info(f"[{symbol}] Max scan attempts reached on longest term (long).")

                benchmarking_pairs.add(symbol)
            return None
    except Exception as e:
        logging.error(f"Error in analyze_pair for {symbol}: {e}")
        return None

class AnalysisWorker(threading.Thread):
    def __init__(self, exchange, data_manager, pattern_manager, engine, config):
        super().__init__(daemon=True)
        self.exchange = exchange
        self.data_manager = data_manager
        self.pattern_manager = pattern_manager
        self.engine = engine
        self.config = config

    def run(self):
        while not shutdown_event.is_set():
            try:
                priority, group_id, symbol = analysis_queue.get(timeout=1)
                try:
                    pairs_dict = self.config.get('pairs', {})
                    res = analyze_pair(self.exchange, self.data_manager, self.pattern_manager, symbol, pairs_dict.get(symbol, {}), self.config, self.engine)
                    if res:
                        execution_queue.put((symbol, res))
                except Exception as e:
                    logging.error(f"AnalysisWorker error for {symbol}: {e}")
                    # Recoup: Put back in queue with lower priority if it wasn't a major failure
                    if not shutdown_event.is_set():
                         analysis_queue.put((priority + 5, group_id, symbol))
                         continue # Skip discard to keep it "pending"
                finally:
                    with bot_lock:
                        pending_analysis.discard(symbol)
                    analysis_queue.task_done()
            except queue.Empty:
                continue

class ExecutionWorker(threading.Thread):
    def __init__(self, exchange, data_manager, engine, config):
        super().__init__(daemon=True)
        self.exchange = exchange
        self.data_manager = data_manager
        self.engine = engine
        self.config = config

    def run(self):
        while not shutdown_event.is_set():
            try:
                symbol, res = execution_queue.get(timeout=1)
                try:
                    with bot_lock:
                        if symbol not in bot_state: continue
                        bot_state[symbol].update(res)
                        data = bot_state[symbol]

                        if res['buy_signal']:
                            data['consecutive_buys'] += 1
                            data['consecutive_sells'] = 0
                            if symbol not in signal_arrival_times: signal_arrival_times[symbol] = time.time()
                        elif res['sell_signal']:
                            data['consecutive_sells'] += 1
                            data['consecutive_buys'] = 0
                            if symbol not in signal_arrival_times: signal_arrival_times[symbol] = time.time()
                        else:
                            data['consecutive_buys'] = 0
                            data['consecutive_sells'] = 0
                            signal_arrival_times.pop(symbol, None)

                        if data['consecutive_buys'] >= 1 and not data['position']:
                            active_term = self.config.get('_active_term', 'short')
                            if self.engine.validate_trade_mc(symbol, data, self.config) and trading_engine.execute_buy(self.exchange, self.data_manager, self.engine, symbol, data, self.config, bot_lock, available_assets, suspended_pairs, term=active_term):
                                data['last_action'] = 'BUY'
                                data['position'] = self.data_manager.get_position(symbol)
                                data['positions'] = self.data_manager.get_positions(symbol)
                                play_sound("buy", self.config)

                        if data['consecutive_sells'] >= 1 and data['positions']:
                             for idx, pos in enumerate(data['positions']):
                                 if self.engine.is_profitable(data['price'], pos['entry_price']):
                                     if self.engine.validate_trade_mc(symbol, data, self.config) and trading_engine.execute_sell(self.exchange, self.data_manager, self.engine, symbol, data, self.config, position_idx=idx):
                                         data['last_action'] = 'SELL'
                                         data['positions'] = self.data_manager.get_positions(symbol)
                                         data['position'] = self.data_manager.get_position(symbol)
                                         play_sound("sell", self.config)
                                         break
                                 elif data['consecutive_sells'] >= 3:
                                     current_term = pos.get('term', 'short')
                                     term_order = ['short', 'medium', 'long']
                                     if current_term in term_order and term_order.index(current_term) < len(term_order) - 1:
                                         new_term = term_order[term_order.index(current_term) + 1]
                                         if self.data_manager.update_position_term(symbol, idx, new_term):
                                             logging.info(f"[{symbol}] Shifting to {new_term} term.")
                                             data['consecutive_sells'] = 0
                                     else:
                                         if self.engine.validate_trade_mc(symbol, data, self.config) and trading_engine.execute_sell(self.exchange, self.data_manager, self.engine, symbol, data, self.config, position_idx=idx):
                                             data['last_action'] = 'SELL'
                                             data['positions'] = self.data_manager.get_positions(symbol)
                                             data['position'] = self.data_manager.get_position(symbol)
                                             play_sound("sell", self.config)
                                             logging.warning(f"[{symbol}] Auto-executing sell on longest term.")
                                             break
                except Exception as e:
                    logging.error(f"ExecutionWorker error for {symbol}: {e}")
                finally:
                    execution_queue.task_done()
            except queue.Empty:
                continue

def trading_thread_func(exchange, data_manager, pattern_manager, engine, config, args):
    global available_assets, pending_asset_update
    priority_order = config.get('_priority_pairs')
    pairs_dict = config.get('pairs', {})
    all_symbols = list(pairs_dict.keys())

    if priority_order:
        all_symbols = [s for s in priority_order if s in pairs_dict] + [s for s in all_symbols if s not in priority_order]

    for sym in all_symbols:
        with bot_lock:
            bot_state[sym] = {
                'price': 0, 'positions': data_manager.get_positions(sym),
                'position': data_manager.get_position(sym),
                'strategy': 'Benchmarking...', 'aggr': 'N/A', 'bench_profit': 0,
                'consecutive_buys': 0, 'consecutive_sells': 0, 'last_action': 'WAITING',
                'scan_attempts': 0, 'next_scan_allowed': 0,
                'last_mc_ts': 0, 'mc_score': 1.1, 'last_processed_ts': 0
            }

    # Start workers
    cpu_count = os.cpu_count() or 1
    for _ in range(cpu_count):
        AnalysisWorker(exchange, data_manager, pattern_manager, engine, config).start()

    ExecutionWorker(exchange, data_manager, engine, config).start()

    import optimization
    while not shutdown_event.is_set():
        start_time = time.time()

        with bot_lock:
            rebench_syms = list(benchmarking_pairs)

        if rebench_syms:
            optimization.run_benchmark_mode(exchange, config, args, shutdown_event, bot_lock, global_pattern_pool, benchmarking_pairs, term_override=config.get('_active_term', 'short'), data_manager=data_manager, pattern_manager=pattern_manager, engine=engine, device=config.get('device'), symbols_to_process=rebench_syms, ohlcv_cache_manager=ohlcv_cache_manager)

        for symbol in all_symbols:
            if symbol in suspended_pairs: continue
            with bot_lock:
                if symbol not in pending_analysis and time.time() >= bot_state[symbol].get('next_scan_allowed', 0):
                    # Organise by quote currency group
                    quote = symbol.split('/')[1] if '/' in symbol else 'default'
                    priority = 0 if bot_state[symbol]['positions'] else 1
                    pending_analysis.add(symbol)
                    analysis_queue.put((priority, quote, symbol))

        if not pending_asset_update and time.time() % 30 < 1:
            pending_asset_update = True
            threading.Thread(target=update_available_assets_live, args=(exchange, config), daemon=True).start()

        # Routine memory management
        gc.collect()
        if device.type == 'cuda' and torch.cuda.is_available():
            torch.cuda.empty_cache()
        elif device.type == 'xpu':
            try:
                import intel_extension_for_pytorch as ipex
                torch.xpu.empty_cache()
            except: pass
        elif device.type == 'mps':
            try: torch.mps.empty_cache()
            except: pass

        # Wait for next cycle
        elapsed = time.time() - start_time
        if elapsed < 5.0: time.sleep(5.0 - elapsed)

def main():
    # Initialize logging early to capture all events in Dashboard
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    db_handler = dashboard.DashboardHandler(ui, bot_lock)
    logging.basicConfig(
        level=logging.INFO,
        format='%(message)s',
        handlers=[db_handler, RichHandler(console=console, show_time=False, show_level=False, show_path=False)]
    )
    logging.root.setLevel(logging.INFO)

    global ohlcv_cache_manager
    migrate_fresh_files_to_archive()
    load_from_archive()

    parser = argparse.ArgumentParser(description='Cryptocurrencies Multiplatform Trading Bot')
    parser.add_argument('--mode', choices=['live', 'simulation', 'backtest', 'benchmark', 'sell', 'balance', 'virtual'], default='simulation')
    parser.add_argument('--symbol', help='Symbol for backtest/benchmark')
    parser.add_argument('--strategy', choices=STRATEGIES, help='Strategy for backtest')
    parser.add_argument('--term', choices=['short', 'medium', 'long'], default='short', help='Term for optimization/backtest/benchmark')
    parser.add_argument('--exchange', choices=list(EXCHANGE_MAPPING.keys()), default='binance')
    parser.add_argument('--config', help='Path to custom config.json')
    parser.add_argument('--no-gpu', action='store_true', help='Disable GPU acceleration')
    parser.add_argument('--since', help='Start date for backtest (YYYY-MM-DD)')
    parser.add_argument('--until', help='End date for backtest (YYYY-MM-DD)')
    parser.add_argument('--every-symbol', action='store_true', help='Benchmark all symbols in pairs.txt')
    parser.add_argument('--backtest-positions', action='store_true', help='Show positions during backtest')
    parser.add_argument('--wallet', help='Initial wallet for virtual mode (e.g. "100 USDC")')
    args = parser.parse_args()

    ohlcv_cache_manager = OHLCVCacheManager(mode=args.mode)

    num_cores = os.cpu_count() or 1
    torch.set_num_threads(num_cores)
    os.environ['OMP_NUM_THREADS'] = str(num_cores)
    os.environ['MKL_NUM_THREADS'] = str(num_cores)

    global device, gpu_enabled, gpu_accel
    gpu_enabled = False
    gpu_accel = "CPU"
    if args.no_gpu:
        device = torch.device('cpu')
    else:
        # 1. CUDA (NVIDIA)
        if torch.cuda.is_available():
            device = torch.device('cuda'); gpu_enabled = True; gpu_accel = "CUDA"
        # 2. ROCm (AMD via CUDA shim or native)
        elif hasattr(torch, 'version') and torch.version.hip:
            device = torch.device('cuda'); gpu_enabled = True; gpu_accel = "ROCm"
        # 3. IPEX (Intel XPU)
        elif 'ipex' in sys.modules or (lambda: (importlib.util.find_spec('intel_extension_for_pytorch') is not None) if 'importlib' in sys.modules else False)():
            try:
                import intel_extension_for_pytorch as ipex
                if hasattr(ipex, 'xpu') and ipex.xpu.is_available():
                    device = torch.device('xpu'); gpu_enabled = True; gpu_accel = "IPEX"
            except: pass
        # 4. MPS (Apple Silicon)
        if not gpu_enabled and hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            device = torch.device('mps'); gpu_enabled = True; gpu_accel = "MPS"
        # 5. Vulkan (via Torch-Vulkan if available)
        if not gpu_enabled:
            try:
                if 'vulkan' in torch.backends.__dict__ and torch.backends.vulkan.is_available():
                     device = torch.device('vulkan'); gpu_enabled = True; gpu_accel = "Vulkan"
            except: pass
        # 6. oneDNN (CPU Acceleration)
        if not gpu_enabled and torch.backends.mkldnn.is_available():
            device = torch.device('cpu'); gpu_enabled = True; torch.backends.mkldnn.enabled = True; gpu_accel = "oneDNN"

        if not gpu_enabled:
            device = torch.device('cpu')

    if args.config: config = load_config_from_path(args.config)
    else: config = load_config()
    config['device'] = device
    config['gpu_accel'] = gpu_accel

    if os.path.exists('pairs.txt'):
        with open('pairs.txt', 'r') as f:
            pairs = [line.strip() for line in f if line.strip()]
    else: pairs = []
    config['pairs'] = {p: {} for p in pairs}
    config['base_currencies'] = sorted(list(set([p.split('/')[1] for p in pairs if '/' in p])))

    api_creds = {}
    if os.path.exists('api.json'):
        try:
            with open('api.json', 'r') as f: api_creds = json.load(f)
        except Exception as e:
            err_msg = f"Error parsing api.json: {e}"
            console.print(f"[bold red]{err_msg}[/]")
            logging.error(err_msg)

    with console.status("[bold green]Initializing Cryptocurrencies multiplatform bot...", spinner="dots") as status:
        # Pre-load patterns or other metadata if needed before dashboard
        try:
            import cpuinfo
            info = cpuinfo.get_cpu_info()
            flags = info.get('flags', [])
            opt_level = "Unknown"
            if 'avx512f' in flags: opt_level = "AVX-512"
            elif 'avx2' in flags: opt_level = "AVX2"
            elif 'avx' in flags: opt_level = "AVX"
            elif 'sse4_2' in flags: opt_level = "SSE4.2"
            elif 'sse4_1' in flags: opt_level = "SSE4.1"
            elif 'ssse3' in flags: opt_level = "SSSE3"
            elif 'sse3' in flags: opt_level = "SSE3"
            elif 'sse2' in flags: opt_level = "SSE2"
            elif 'sse' in flags: opt_level = "SSE"
            elif 'mmx' in flags: opt_level = "MMX"

            hw_msg = f"Hardware optimization level: [bold blue]{opt_level}[/] ({info.get('brand_raw', 'Unknown CPU')})"
            logging.info(hw_msg)
        except: pass

        data_manager = DataManager(args.mode) if args.mode in ['live', 'simulation', 'sell', 'virtual'] else None
        pattern_manager = PatternManager()
        engine = TradingEngine(config)

        api_key = api_creds.get('api_key') or config.get('api_key')
        api_secret = api_creds.get('api_secret') or config.get('api_secret')
        ex_class = EXCHANGE_MAPPING.get(args.exchange, MockExchange)

        if args.mode == 'live': exchange = ex_class(api_key, api_secret)
        elif args.mode == 'simulation': exchange = MockExchange(api_key, api_secret, exchange_type=args.exchange)
        elif args.mode == 'virtual':
            exchange = MockExchange(exchange_type=args.exchange)
            if args.wallet:
                try:
                    amount, asset = args.wallet.split()
                    exchange.balance = {asset: float(amount)}
                    logging.info(f"Virtual mode initialized with wallet: {amount} {asset}")
                except:
                    logging.warning("Failed to parse wallet argument. Using default virtual balance.")
        elif args.mode == 'sell':
            exchange = ex_class(api_key, api_secret) if api_key not in [None, "YOUR_API_KEY"] else MockExchange(exchange_type=args.exchange)
            status.stop(); trading_engine.interactive_sell(exchange, data_manager, engine, config, console); return
        elif args.mode == 'balance':
            exchange = ex_class(api_key, api_secret) if api_key not in [None, "YOUR_API_KEY"] else MockExchange(exchange_type=args.exchange)
            trading_engine.show_balance(exchange, config, console, Table); return
        elif args.mode == 'backtest':
            exchange = ex_class(api_key, api_secret) if api_key not in [None, "YOUR_API_KEY"] else MockExchange(exchange_type=args.exchange)
            import optimization
            optimization.run_backtest_mode(exchange, config, args, engine=engine, device=device, ohlcv_cache_manager=ohlcv_cache_manager); return
        elif args.mode == 'benchmark':
            exchange = ex_class(api_key, api_secret) if api_key not in [None, "YOUR_API_KEY"] else MockExchange(exchange_type=args.exchange)
            import optimization
            optimization.run_benchmark_mode(exchange, config, args, shutdown_event, bot_lock, global_pattern_pool, benchmarking_pairs, status=status, data_manager=None, pattern_manager=pattern_manager, engine=engine, device=device, ohlcv_cache_manager=ohlcv_cache_manager); return


        # Initial synchronous asset update to ensure immediate availability
        try:
            available_assets[:] = trading_engine.get_sellable_assets(exchange, config)
        except: pass

    # Start candle downloader
    downloader = CandleDownloader(exchange, ohlcv_cache_manager)
    downloader.start()

    # Dynamic benchmarking if capacity exists
    def dynamic_benchmark_worker():
        while not shutdown_event.is_set():
            try:
                cpu_usage = psutil.cpu_percent(interval=1.0)
                mem_available = psutil.virtual_memory().available / (1024 * 1024 * 1024)
                if cpu_usage < 30 and mem_available > 2.0:
                    with bot_lock:
                        if not benchmarking_pairs and config.get('pairs'):
                            all_syms = list(config['pairs'].keys())
                            if all_syms:
                                 benchmarking_pairs.add(random.choice(all_syms))
                time.sleep(60)
            except: time.sleep(60)

    threading.Thread(target=dynamic_benchmark_worker, daemon=True).start()

    # Silence ALL other handlers during Dashboard execution, keeping ONLY DashboardHandler
    all_other_handlers = [h for h in logging.root.handlers if not isinstance(h, dashboard.DashboardHandler)]

    with Live(ui.make_dashboard(args.mode, config, bot_state, signal_arrival_times, bot_lock), refresh_per_second=2, screen=True) as live:
        # Silence console output once dashboard is live
        for h in all_other_handlers:
            logging.root.removeHandler(h)

        if args.mode in ['live', 'simulation', 'virtual']:
            # Re-initialize bot_state to ensure keys exist before benchmarking
            for symbol in config['pairs']:
                pos_list = data_manager.get_positions(symbol) if data_manager else []
                bot_state[symbol] = {'aggr': 'N/A', 'strategy': 'Benchmarking...', 'last_action': 'BUY' if pos_list else 'Waiting', 'positions': pos_list, 'position': pos_list[0] if pos_list else None, 'bench_profit': 0}
            live.update(ui.make_dashboard(args.mode, config, bot_state, signal_arrival_times, bot_lock))

            config['_active_term'] = args.term
            import optimization
            sellable = []
            try:
                sellable = trading_engine.get_sellable_assets(exchange, config)
                with bot_lock: available_assets[:] = sellable
            except: pass

            # Initial re-benchmarking for live/simulation inside the dashboard context
            opt_map = optimization.run_benchmark_mode(exchange, config, args, shutdown_event, bot_lock, global_pattern_pool, benchmarking_pairs, term_override=args.term, status=None, data_manager=data_manager, pattern_manager=pattern_manager, engine=engine, device=device, ohlcv_cache_manager=ohlcv_cache_manager, priority_symbols=sellable)
            pair_priorities = []
            for sym, best in opt_map.items():
                if sym in config['pairs']:
                    config['pairs'][sym].update({'aggr': best['aggr'], 'strategy': best['strategy'], 'expected_profit': best.get('avg_bench_profit', best['profit'])})
                    pair_priorities.append((sym, best['profit']))
            config['_priority_pairs'] = [p[0] for p in sorted(pair_priorities, key=lambda x: x[1], reverse=True)]

            if args.mode in ['simulation', 'virtual']:
                trading_engine.initialize_simulation(exchange, data_manager, pattern_manager, engine, config, bot_state)

            for symbol in config['pairs']:
                pos_list = data_manager.get_positions(symbol)
                bot_state[symbol] = {'aggr': config['pairs'][symbol].get('aggr', 'normal'), 'strategy': config['pairs'][symbol].get('strategy', 'simple_ema'), 'last_action': 'BUY' if pos_list else 'Waiting', 'positions': pos_list, 'position': pos_list[0] if pos_list else None, 'bench_profit': config['pairs'][symbol].get('expected_profit', 0)}

        try:
            threading.Thread(target=trading_thread_func, args=(exchange, data_manager, pattern_manager, engine, config, args), daemon=True).start()
            threading.Thread(target=ui.input_thread_func, args=(exchange, data_manager, engine, config, bot_state, bot_lock, shutdown_event, trading_engine.execute_buy, trading_engine.execute_sell, play_sound), daemon=True).start()
            while not shutdown_event.is_set():
                now_ts = time.time()

                live.update(ui.make_dashboard(args.mode, config, bot_state, signal_arrival_times, bot_lock))
                time.sleep(0.5)
        finally:
            # Restore console logging on exit
            for h in all_other_handlers:
                logging.root.addHandler(h)

    if ohlcv_cache_manager:
        ohlcv_cache_manager.flush_all()
    archiver.stop()
    shutdown_msg = "Bot shutdown gracefully."
    console.print(f"[bold green]{shutdown_msg}[/]")

if __name__ == "__main__":
    main()

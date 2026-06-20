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
import asyncio

from rich.live import Live
from rich.logging import RichHandler
from rich.console import Console
from rich.table import Table

import dashboard
from exchange_handler import EXCHANGE_MAPPING, MockExchange, fetch_ohlcv_incremental
from indicators import get_signals, get_common_indicators, calculate_similarity, calculate_similarity_batch, STRATEGIES
from persistence import DataManager, CacheManager, PatternManager, OHLCVCacheManager, archiver, migrate_fresh_files_to_archive, load_from_archive
import trading_engine
from trading_engine import TradingEngine
from monte_carlo import MonteCarloEngine
import utils
from utils import format_price, format_amount, get_base_currency, play_sound, silent_worker_init, load_config, load_config_from_path

from core import TradingCore

# Global objects
ohlcv_cache_manager = None
global_core = None
global_pattern_pool = []
candle_queue = queue.PriorityQueue()
analysis_queue = queue.PriorityQueue()
execution_queue = queue.Queue()
pending_analysis = set()
pending_downloads = set()
last_download_time = {}
instrumented_mem_footprint = {'analysis': 1.0 * 1024 * 1024 * 1024}
bot_state = {}
available_assets = []
benchmarking_pairs = set()
suspended_pairs = set()
signal_arrival_times = {}
bot_lock = threading.RLock()
shutdown_event = threading.Event()
async_shutdown_event = None # Initialized in main
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
    time.sleep(random.uniform(1.0, 2.0))
    try:
        new_assets_with_amounts = trading_engine.get_sellable_assets_with_amounts(exchange, config)
        new_assets = sorted(list(new_assets_with_amounts.keys()))
        with bot_lock:
            available_assets[:] = new_assets

            # Update Amt in bot_state
            for symbol, state in bot_state.items():
                asset = symbol.split('/')[0]
                if asset in new_assets_with_amounts:
                    state['amt'] = new_assets_with_amounts[asset]
                else:
                    state['amt'] = 0

            pending_asset_update = False
    except Exception as e:
        logging.error(f"Failed to update assets from API: {e}")
        with bot_lock: pending_asset_update = False

def perform_analysis_calculation(symbol, timeframe, tf_secs, df, search_pool, global_config_lite, pattern_info):
    """
    CPU-intensive analysis task designed to run in a subprocess.
    """
    try:
        device = global_config_lite.get('device', torch.device('cpu'))
        # 1. Indicators
        df = get_common_indicators(df, device)

        current_pattern_id = pattern_info.get('active_pattern_id')
        pattern_match_ts = pattern_info.get('pattern_match_ts', 0)
        last_mc_ts = pattern_info.get('last_mc_ts', 0)
        last_mc_score = pattern_info.get('mc_score', 1.1)
        candle_ts = df.iloc[-1]['timestamp']

        # 2. Similarity Matching (Vectorized Batch)
        active_patterns = []
        if search_pool:
            active_patterns = calculate_similarity_batch(df, search_pool, device=device)

        candidates = []
        for item in active_patterns:
            p = item['pattern']
            p_id = f"{p.get('symbol')}_{p.get('start_time')}_{p.get('strategy')}"
            p_match_ts = pattern_match_ts if p_id == current_pattern_id else candle_ts
            p_len = len(p['prices'])
            p_expired = (abs(candle_ts - p_match_ts) // tf_secs) >= p_len

            p_tech = p.get('tech_state', {})
            latest_row = df.iloc[-1]
            curr_adx = latest_row.get('adx', 0)
            curr_vol = latest_row.get('volatility', 0)
            p_adx = p_tech.get('adx', curr_adx)
            p_vol = p_tech.get('volatility', curr_vol)

            p_regime_shift = False
            if p_adx > 0 and abs(curr_adx - p_adx) / p_adx > 0.50: p_regime_shift = True
            if p_vol > 0 and abs(curr_vol - p_vol) / p_vol > 0.50: p_regime_shift = True

            item['expired'] = p_expired
            item['regime_shift'] = p_regime_shift
            item['id'] = p_id
            candidates.append(item)

        # 3. Selection
        active_pattern = None
        active_pattern_id = None
        if candidates:
            candidates.sort(key=lambda x: x['sim'], reverse=True)
            active_pattern = candidates[0]['pattern']
            active_pattern_id = candidates[0]['id']

        latest = df.iloc[-1]
        if active_pattern:
            if active_pattern_id != current_pattern_id:
                pattern_match_ts = candle_ts
                current_pattern_id = active_pattern_id

            curr_adx = latest.get('adx', 0)
            curr_vol = latest.get('volatility', 0)

            # Replicate get_dynamic_settings logic with dynamic labeling
            settings = {
                "ema_fast": 9, "ema_slow": 21, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
                "rsi_period": 14, "rsi_buy": 30, "rsi_sell": 70,
                "label": "balanced"
            }
            if curr_adx > 25:
                settings.update({
                    "ema_fast": 10, "ema_slow": 30,
                    "rsi_buy": 40, "rsi_sell": 60,
                    "label": "aggressive"
                })
            elif curr_vol > global_config_lite.get('min_profit', 0.01):
                settings.update({
                    "ema_fast": 30, "ema_slow": 100,
                    "rsi_buy": 20, "rsi_sell": 80,
                    "label": "conservative"
                })

            settings.update({'strategy': active_pattern['strategy'], 'device': device})
            df = get_signals(df, settings, is_backtest=False)
            latest = df.iloc[-1]

            p_len = len(active_pattern['prices'])
            expired = (abs(candle_ts - pattern_match_ts) // tf_secs) >= p_len
            regime_shift = False # Already checked above
            if curr_adx > 0 and abs(curr_adx - active_pattern.get('tech_state', {}).get('adx', curr_adx)) / curr_adx > 0.50: regime_shift = True

            trigger_rebench = False
            if expired or regime_shift:
                p_duration_secs = p_len * tf_secs
                if (time.time() - pattern_match_ts) > (p_duration_secs * 0.05):
                    trigger_rebench = True

            res = {
                'symbol': symbol,
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
                'buy_signal': latest.get('buy_signal', False),
                'sell_signal': latest.get('sell_signal', False),
                'strategy': active_pattern['strategy'],
                'aggr': settings.get('label', active_pattern['aggr']),
                'bench_profit': active_pattern.get('avg_bench_profit', active_pattern['profit']),
                'score': latest.get('score', active_pattern.get('mc_score', pattern_info.get('mc_score', 1.1))),
                'active_pattern_id': active_pattern_id,
                'pattern_match_ts': pattern_match_ts,
                'last_mc_ts': last_mc_ts,
                'last_processed_ts': candle_ts,
                'last_20_candles': {'prices': df['close'].tail(20).tolist(), 'volumes': df['volume'].tail(20).tolist()},
                'last_100_candles': {'prices': df['close'].tail(100).tolist(), 'volumes': df['volume'].tail(100).tolist()},
                'trigger_rebenchmark': trigger_rebench
            }
            return res
        else:
            return {'symbol': symbol, 'price': latest['close'], 'trigger_rebenchmark': True, 'no_patterns': True, 'buy_signal': False, 'sell_signal': False}
    except Exception as e:
        return {'symbol': symbol, 'error': str(e)}

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
        timeframe = term_cfg.get('timeframe', '1m')

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
        if df.empty: return None

        with bot_lock:
            current_data = bot_state.get(symbol, {})
            last_processed_ts = current_data.get('last_processed_ts', 0)

        candle_ts = df.iloc[-1]['timestamp']
        # Efficiency optimization: only re-process if new data or pattern list changed
        if candle_ts == last_processed_ts and not benchmarking_pairs:
             return current_data

        with bot_lock:
            current_global_pool = list(global_pattern_pool)

        search_pool = patterns + current_global_pool
        tf_map = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}
        tf_secs = tf_map.get(timeframe, 300)

        global_config_lite = {
            'device': global_config.get('device'),
            'mc_hurdle': global_config.get('profit_thresholds', {}).get('mc_validation_hurdle', 0.0015),
            'min_profit': global_config.get('profit_thresholds', {}).get('min_pattern_profit', 0.01)
        }

        pattern_info = {
            'active_pattern_id': current_data.get('active_pattern_id'),
            'pattern_match_ts': current_data.get('pattern_match_ts', 0),
            'last_mc_ts': current_data.get('last_mc_ts', 0),
            'mc_score': current_data.get('mc_score', 1.1)
        }

        # This call will be parallelized in trading_thread_func
        res = perform_analysis_calculation(symbol, timeframe, tf_secs, df, search_pool, global_config_lite, pattern_info)

        if res and 'error' not in res:
            if res.get('trigger_rebenchmark'):
                with bot_lock: benchmarking_pairs.add(symbol)

            if res.get('no_patterns'):
                with bot_lock:
                    if symbol in bot_state:
                        bot_state[symbol]['scan_attempts'] = bot_state[symbol].get('scan_attempts', 0) + 1
                        attempts = bot_state[symbol]['scan_attempts']
                        bot_state[symbol]['next_scan_allowed'] = time.time() + (attempts * 60)
                        if attempts >= 5:
                            current_term = pair_config.get('term_override', global_config.get('_active_term', 'short'))
                            term_order = ['short', 'medium', 'long']
                            if current_term in term_order:
                                idx = term_order.index(current_term)
                                if idx < len(term_order) - 1:
                                    pair_config['term_override'] = term_order[idx + 1]
                                    bot_state[symbol]['scan_attempts'] = 0
                with bot_lock: benchmarking_pairs.add(symbol)
                return None

            with bot_lock:
                if symbol in bot_state:
                    bot_state[symbol]['scan_attempts'] = 0
                    bot_state[symbol]['next_scan_allowed'] = 0
            return res
        return None
    except Exception as e:
        logging.error(f"Error in analyze_pair for {symbol}: {e}")
        return None

def wrapped_analysis_task(symbol, timeframe, tf_secs, df, search_pool, global_config_lite, pattern_info):
    """Picklable wrapper for the multiprocess analysis task."""
    return perform_analysis_calculation(symbol, timeframe, tf_secs, df, search_pool, global_config_lite, pattern_info)

# Async Core handles all workers now.
# Legacy synchronous workers removed.

def trading_thread_func(exchange, data_manager, pattern_manager, engine, config, args):
    global available_assets, pending_asset_update
    priority_order = config.get('_priority_pairs')
    pairs_dict = config.get('pairs', {})
    all_symbols = list(pairs_dict.keys())

    if priority_order:
        all_symbols = [s for s in priority_order if s in pairs_dict] + [s for s in all_symbols if s not in priority_order]

    for sym in all_symbols:
        with bot_lock:
            if sym not in bot_state:
                bot_state[sym] = {
                    'price': 0, 'positions': data_manager.get_positions(sym),
                    'position': data_manager.get_position(sym),
                    'strategy': 'Discovering...', 'aggr': 'N/A', 'bench_profit': 0,
                    'consecutive_buys': 0, 'consecutive_sells': 0, 'last_action': 'WAITING',
                    'scan_attempts': 0, 'next_scan_allowed': 0,
                    'last_mc_ts': 0, 'mc_score': 1.1, 'last_processed_ts': 0
                }
            else:
                # Ensure latest positions are synced
                bot_state[sym].update({
                    'positions': data_manager.get_positions(sym),
                    'position': data_manager.get_position(sym),
                })

    # Start workers
    AnalysisWorker(exchange, data_manager, pattern_manager, engine, config).start()
    ExecutionWorker(exchange, data_manager, engine, config).start()

    # Pre-warm OHLCV cache for all symbols in parallel
    logging.info("Pre-warming OHLCV cache for all symbols...")
    with bot_lock:
        term = config.get('_active_term', 'short')
        term_cfg = config.get('expected_profit_terms', {}).get(term, {})
        timeframe = term_cfg.get('timeframe', '1m')
        for sym in all_symbols:
            if (sym, timeframe) not in pending_downloads:
                candle_queue.put((2, sym, timeframe, 500, None))
                pending_downloads.add((sym, timeframe))

    import optimization
    while not shutdown_event.is_set():
        start_time = time.time()

        with bot_lock:
            rebench_syms = list(benchmarking_pairs)

        if rebench_syms:
            optimization.run_benchmark_mode(exchange, config, args, shutdown_event, bot_lock, global_pattern_pool, benchmarking_pairs, term_override=config.get('_active_term', 'short'), data_manager=data_manager, pattern_manager=pattern_manager, engine=engine, device=config.get('device'), symbols_to_process=rebench_syms, ohlcv_cache_manager=ohlcv_cache_manager, bot_state=bot_state)

        for symbol in all_symbols:
            # Allow analysis if we have a position, even if suspended (so we can sell)
            has_pos = False
            with bot_lock:
                if symbol in bot_state and bot_state[symbol].get('positions'):
                    has_pos = True

            if symbol in suspended_pairs and not has_pos: continue

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
        if elapsed < 2.0: time.sleep(2.0 - elapsed)

def setup_bot_state(config, data_manager, bot_state):
    """Initializes the bot state for all configured pairs, using cache if available."""
    from persistence import CacheManager
    cache_mgr = CacheManager()
    active_term = config.get('_active_term', 'short')

    for symbol in config['pairs']:
        pos_list = data_manager.get_positions(symbol) if data_manager else []

        # Load from cache to avoid "Discovering..." if we already know this pair
        cached = cache_mgr.get(symbol, active_term)
        if cached and isinstance(cached, list) and len(cached) > 0:
            best = cached[0]
            aggr = best.get('aggr', 'balanced')
            strategy = best.get('strategy', 'simple_ema')
            bench_profit = best.get('avg_bench_profit', best.get('profit', 0))
        else:
            aggr = 'N/A'
            strategy = 'Discovering...'
            bench_profit = 0

        bot_state[symbol] = {
            'aggr': aggr,
            'strategy': strategy,
            'last_action': 'BUY' if pos_list else 'Waiting',
            'positions': pos_list,
            'position': pos_list[0] if pos_list else None,
            'bench_profit': bench_profit,
            'consecutive_buys': 0,
            'consecutive_sells': 0,
            'last_mc_ts': 0,
            'mc_score': 1.1,
            'last_processed_ts': 0,
            'amt': 0
        }

def run_initial_benchmarking(exchange, config, args, shutdown_event, bot_lock, global_pattern_pool, benchmarking_pairs, data_manager, pattern_manager, engine, device, ohlcv_cache_manager, available_assets, trading_engine, bot_state, ui=None):
    """Runs the initial benchmarking and sets pair priorities."""
    config['_active_term'] = args.term
    import optimization
    sellable = []
    try:
        sellable_with_amounts = trading_engine.get_sellable_assets_with_amounts(exchange, config)
        sellable = sorted(list(sellable_with_amounts.keys()))
        with bot_lock:
            available_assets[:] = sellable
            for symbol, state in bot_state.items():
                asset = symbol.split('/')[0]
                state['amt'] = sellable_with_amounts.get(asset, 0)
    except: pass

    # Set default priority
    config['_priority_pairs'] = sorted(list(config['pairs'].keys()))

    def bg_benchmark():
        # Initial re-benchmarking
        opt_map = optimization.run_benchmark_mode(
            exchange, config, args, shutdown_event, bot_lock, global_pattern_pool,
            benchmarking_pairs, term_override=args.term, status=None,
            data_manager=data_manager, pattern_manager=pattern_manager,
            engine=engine, device=device, ohlcv_cache_manager=ohlcv_cache_manager,
            priority_symbols=sellable, bot_state=bot_state
        )

        pair_priorities = []
        for sym, best in opt_map.items():
            if sym in config['pairs']:
                with bot_lock:
                    config['pairs'][sym].update({
                        'aggr': best['aggr'],
                        'strategy': best['strategy'],
                        'expected_profit': best.get('avg_bench_profit', best['profit'])
                    })
                pair_priorities.append((sym, best['profit']))

        if pair_priorities:
            with bot_lock:
                config['_priority_pairs'] = [p[0] for p in sorted(pair_priorities, key=lambda x: x[1], reverse=True)]

    threading.Thread(target=bg_benchmark, daemon=True).start()

    if args.mode in ['simulation', 'virtual']:
        trading_engine.initialize_simulation(exchange, data_manager, pattern_manager, engine, config, bot_state)

    for symbol in config['pairs']:
        pos_list = data_manager.get_positions(symbol)
        with bot_lock:
            # Ensure keys exist for the trading loop
            if 'aggr' not in config['pairs'][symbol]: config['pairs'][symbol]['aggr'] = 'normal'
            if 'strategy' not in config['pairs'][symbol]: config['pairs'][symbol]['strategy'] = 'simple_ema'
            if 'expected_profit' not in config['pairs'][symbol]: config['pairs'][symbol]['expected_profit'] = 0

            bot_state[symbol].update({
                'aggr': config['pairs'][symbol]['aggr'],
                'strategy': config['pairs'][symbol]['strategy'],
                'last_action': 'BUY' if pos_list else 'Waiting',
                'positions': pos_list,
                'position': pos_list[0] if pos_list else None,
                'bench_profit': config['pairs'][symbol]['expected_profit']
            })

def main():
    parser = argparse.ArgumentParser(description='Cryptocurrencies Multiplatform Trading Bot')
    parser.add_argument('--headless', action='store_true', help='Disable TUI and use structured JSON logging (can be used for DEBUG)')
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
    args, unknown = parser.parse_known_args()

    # Handle positional arguments and misplaced flags (e.g., bot.py --mode benchmark PEPE/USDC -- term short)
    i = 0
    while i < len(unknown):
        arg = unknown[i]
        if arg in ['--', '-']:
            i += 1; continue

        if not args.symbol and ('/' in arg or arg.isupper()):
            args.symbol = arg
            unknown.pop(i)
            continue

        if arg == 'term' and i + 1 < len(unknown):
            args.term = unknown[i+1]
            unknown.pop(i); unknown.pop(i)
            continue

        i += 1

    # Initialize logging early
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    if args.headless:
        logging.basicConfig(level=logging.INFO, handlers=[utils.JSONLoggingHandler()])
    else:
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

    # Ensure requested symbol is in pairs
    if args.symbol and args.symbol not in config['pairs']:
        config['pairs'][args.symbol] = {}

    config['base_currencies'] = sorted(list(set([p.split('/')[1] for p in config['pairs'] if '/' in p])))

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

            config['opt_level'] = opt_level
        except: pass

        data_manager = DataManager(args.mode) if args.mode in ['live', 'simulation', 'sell', 'virtual'] else None
        pattern_manager = PatternManager()
        engine = TradingEngine(config)

        # Priority: Environment Variables -> api.json -> config.json
        api_key = os.environ.get('api_key') or api_creds.get('api_key') or config.get('api_key')
        api_secret = os.environ.get('api_secret') or api_creds.get('api_secret') or config.get('api_secret')
        market_type = api_creds.get('market', config.get('market', 'spot'))
        ex_class = EXCHANGE_MAPPING.get(args.exchange, MockExchange)

        if args.mode == 'live': exchange = ex_class(api_key, api_secret, market_type=market_type)
        elif args.mode == 'simulation': exchange = MockExchange(api_key, api_secret, exchange_type=args.exchange, market_type=market_type)
        elif args.mode == 'virtual':
            exchange = MockExchange(exchange_type=args.exchange, market_type=market_type)
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
            status.stop()
            exchange = ex_class(api_key, api_secret) if api_key not in [None, "YOUR_API_KEY"] else MockExchange(exchange_type=args.exchange)
            trading_engine.show_balance(exchange, config, console, Table); return
        elif args.mode == 'backtest':
            status.stop()
            exchange = ex_class(api_key, api_secret) if api_key not in [None, "YOUR_API_KEY"] else MockExchange(exchange_type=args.exchange)
            import optimization
            optimization.run_backtest_mode(exchange, config, args, engine=engine, device=device, ohlcv_cache_manager=ohlcv_cache_manager); return
        elif args.mode == 'benchmark':
            status.stop()
            exchange = ex_class(api_key, api_secret) if api_key not in [None, "YOUR_API_KEY"] else MockExchange(exchange_type=args.exchange)
            import optimization
            optimization.run_benchmark_mode(exchange, config, args, shutdown_event, bot_lock, global_pattern_pool, benchmarking_pairs, status=None, data_manager=None, pattern_manager=pattern_manager, engine=engine, device=device, ohlcv_cache_manager=ohlcv_cache_manager); return

        logging.info(f"REST API enabled for {args.exchange} ({market_type}).")

        # Initial synchronous asset update to ensure immediate availability
        try:
            available_assets[:] = trading_engine.get_sellable_assets(exchange, config)
        except: pass

    # Prior to websockets, perform initial download with REST API
    if args.mode in ['live', 'simulation', 'virtual']:
        logging.info("Performing initial REST API data download for all pairs (wallet assets prioritized)...")
        term = config.get('_active_term', 'short')
        timeframe = config.get('expected_profit_terms', {}).get(term, {}).get('timeframe', '1m')

        # Prioritize wallet assets for absolute priority
        all_configured_symbols = list(config.get('pairs', {}).keys())
        wallet_assets = set(available_assets)
        prioritized_symbols = [s for s in all_configured_symbols if s.split('/')[0] in wallet_assets]
        other_symbols = [s for s in all_configured_symbols if s.split('/')[0] not in wallet_assets]
        symbols = prioritized_symbols + other_symbols

        if symbols:
            # Throttled download: 5 workers and 0.2s delay to avoid 429 errors
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(symbols), 5)) as executor:
                futures = []
                for sym in symbols:
                    if shutdown_event.is_set(): break
                    futures.append(executor.submit(fetch_ohlcv_incremental, exchange, sym, timeframe, ohlcv_cache_manager, limit=500))
                    time.sleep(0.2)
                concurrent.futures.wait(futures)
        logging.info("Initial REST API download complete.")

    def signal_handler(sig, frame):
        logging.info("Interrupt received, shutting down gracefully...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start Websocket manager if not in backtest/sell/balance mode
    ws_started = False
    if args.mode in ['live', 'simulation', 'virtual'] and args.exchange != 'mock':
        from exchange_handler import AsyncExchangeManager
        timeframes = [config.get('expected_profit_terms', {}).get(t, {}).get('timeframe', '1m') for t in ['short', 'medium', 'long']]
        market_type = api_creds.get('market', config.get('market', 'spot'))
        # Transition to Async Core
        core = TradingCore(config, exchange, data_manager, pattern_manager, ohlcv_cache_manager, shutdown_event)
        global global_core
        global_core = core

        async_manager = AsyncExchangeManager(
            args.exchange, api_key, api_secret,
            list(config['pairs'].keys()),
            list(set(timeframes)),
            bot_state, bot_lock, ohlcv_cache_manager, shutdown_event,
            market_type=market_type
        )
        async_manager.core_market_data = core.market_data
        if async_manager.is_supported:
            async_manager.start()
            ws_started = True
            logging.info(f"Websockets enabled for {args.exchange} ({market_type}).")
        else:
            logging.warning(f"Websockets not supported by ccxt.pro for {args.exchange}. Falling back to polling.")

    if not ws_started:
        # Start candle downloader fallback
        downloader = CandleDownloader(exchange, ohlcv_cache_manager)
        downloader.start()

    # Dynamic benchmarking if capacity exists - increased frequency for "10 hands"
    def dynamic_benchmark_worker():
        while not shutdown_event.is_set():
            try:
                cpu_usage = psutil.cpu_percent(interval=1.0)
                mem_available = psutil.virtual_memory().available

                with bot_lock:
                    footprint = instrumented_mem_footprint.get('analysis', 1.0 * 1024 * 1024 * 1024)

                # Aggressive threshold for poor hardware - 10 hands
                # Only check if we have some RAM left, ignore CPU as O(N) is fast
                if mem_available > (footprint * 0.5):
                    with bot_lock:
                        if not benchmarking_pairs and config.get('pairs'):
                            all_syms = list(config['pairs'].keys())
                            if all_syms:
                                 # Re-benchmark 2 random pairs every cycle
                                 benchmarking_pairs.add(random.choice(all_syms))
                                 benchmarking_pairs.add(random.choice(all_syms))
                time.sleep(2) # Run every 10 seconds instead of 60
            except: time.sleep(2)

    threading.Thread(target=dynamic_benchmark_worker, daemon=True).start()

    if args.headless:
        if args.mode in ['live', 'simulation', 'virtual']:
            setup_bot_state(config, data_manager, bot_state)
            run_initial_benchmarking(exchange, config, args, shutdown_event, bot_lock, global_pattern_pool, benchmarking_pairs, data_manager, pattern_manager, engine, device, ohlcv_cache_manager, available_assets, trading_engine, bot_state)

        # Ensure core exists if WS didn't start it
        if 'core' not in locals():
            core = TradingCore(config, exchange, data_manager, pattern_manager, ohlcv_cache_manager, shutdown_event)
            global_core = core
        # Sync state
        with bot_lock:
            core.bot_state = bot_state
            core.global_pattern_pool = global_pattern_pool
            core.available_assets = available_assets
            core.suspended_pairs = suspended_pairs
            core.benchmarking_pairs = benchmarking_pairs
            core.signal_arrival_times = signal_arrival_times

        async def run_core():
            await core.main_loop()

        core_thread = threading.Thread(target=lambda: asyncio.run(run_core()), daemon=True)
        core_thread.start()

        try:
            while not shutdown_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            shutdown_event.set()

        core_thread.join(timeout=5)
    else:
        # Silence ALL other handlers during Dashboard execution, keeping ONLY DashboardHandler
        all_other_handlers = [h for h in logging.root.handlers if not isinstance(h, dashboard.DashboardHandler)]

        with Live(ui.make_dashboard(args.mode, config, bot_state, signal_arrival_times, bot_lock), refresh_per_second=2, screen=True) as live:
            # Silence console output once dashboard is live
            for h in all_other_handlers:
                logging.root.removeHandler(h)

            def startup_sequence():
                if args.mode in ['live', 'simulation', 'virtual']:
                    setup_bot_state(config, data_manager, bot_state)
                    run_initial_benchmarking(exchange, config, args, shutdown_event, bot_lock, global_pattern_pool, benchmarking_pairs, data_manager, pattern_manager, engine, device, ohlcv_cache_manager, available_assets, trading_engine, bot_state, ui=ui)

                # Ensure core exists
                if 'core' not in globals() and 'core' not in locals():
                    core = TradingCore(config, exchange, data_manager, pattern_manager, ohlcv_cache_manager, shutdown_event)
                    global global_core
                    global_core = core
                # Sync state
                with bot_lock:
                    core.bot_state = bot_state
                    core.global_pattern_pool = global_pattern_pool
                    core.available_assets = available_assets
                    core.suspended_pairs = suspended_pairs
                    core.benchmarking_pairs = benchmarking_pairs
                    core.signal_arrival_times = signal_arrival_times

                async def run_core():
                    await core.main_loop()

                threading.Thread(target=lambda: asyncio.run(run_core()), daemon=True).start()

            threading.Thread(target=startup_sequence, daemon=True).start()
            # Start input thread IMMEDIATELY so TUI is responsive to keys like 'Q' or 'H'
            threading.Thread(target=ui.input_thread_func, args=(exchange, data_manager, engine, config, bot_state, bot_lock, shutdown_event, trading_engine.execute_buy, trading_engine.execute_sell, play_sound), daemon=True).start()

            try:
                while not shutdown_event.is_set():
                    live.update(ui.make_dashboard(args.mode, config, bot_state, signal_arrival_times, bot_lock))
                    time.sleep(1.0)
            finally:
                # Restore console logging on exit
                for h in all_other_handlers:
                    logging.root.addHandler(h)

    if global_core:
        try:
            asyncio.run(global_core.shutdown())
        except: pass

    if ohlcv_cache_manager:
        ohlcv_cache_manager.flush_all()
    archiver.stop()
    shutdown_msg = "Bot shutdown gracefully."
    console.print(f"[bold green]{shutdown_msg}[/]")

if __name__ == "__main__":
    main()

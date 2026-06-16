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
import torch
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
        term = global_config.get('_active_term', 'short'); term_cfg = global_config.get('expected_profit_terms', {}).get(term, {})
        timeframe = term_cfg.get('timeframe', '5m')

        ohlcv_data = fetch_ohlcv_incremental(exchange, symbol, timeframe, ohlcv_cache_manager, limit=500)
        if not ohlcv_data or not isinstance(ohlcv_data, tuple) or not ohlcv_data[0]:
            return None

        ohlcv = ohlcv_data[0]
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        if df.empty:
            return None

        df['average'] = (df['open'] + df['high'] + df['low'] + df['close']) / 4
        device = global_config.get('device', torch.device('cpu'))
        df = get_common_indicators(df, device)

        with bot_lock:
            current_data = bot_state.get(symbol, {})
            current_pattern_id = current_data.get('active_pattern_id')
            pattern_match_ts = current_data.get('pattern_match_ts', 0)

        latest_row_base = df.iloc[-1]
        candle_ts = latest_row_base['timestamp']

        # Cross-pair pattern matching
        with bot_lock:
            current_global_pool = list(global_pattern_pool)
        search_pool = patterns + current_global_pool; active_patterns = []
        device = global_config.get('device', torch.device('cpu'))
        if search_pool:
            for p in search_pool:
                p_len = len(p['prices'])
                if len(df) < p_len: continue
                buffer_window = df.iloc[-p_len:]; sim = calculate_similarity(buffer_window, p, device=device)
                if sim > 0.70: active_patterns.append({'sim': sim, 'pattern': p})

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
            def validate_candidate_mc(c):
                if not c['expired'] and not c['regime_shift']:
                    return c, True
                p = c['pattern']
                mc_engine = MonteCarloEngine(num_simulations=1000, timeframe_candles=20)
                mc_engine.set_device(device)
                temp_cfg = {'strategy': p['strategy'], 'device': device}
                df_mc = get_signals(df.tail(100).copy(), temp_cfg, is_backtest=False)
                score = mc_engine.validate_strategy(df_mc)
                hurdle = global_config.get('profit_thresholds', {}).get('mc_validation_hurdle', 0.0015)
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
                with bot_lock: benchmarking_pairs.add(symbol)

            strategy = active_pattern['strategy']
            aggr = active_pattern['aggr']

            settings = engine.get_dynamic_settings(curr_adx, curr_vol)
            settings.update({'strategy': strategy, 'device': device})
            df = get_signals(df, settings, is_backtest=False)
            latest = df.iloc[-1]

            new_data = {
                'price': latest['close'],
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
                'last_20_candles': {'prices': df['average'].tail(20).tolist(), 'volumes': df['volume'].tail(20).tolist()}
            }
            return new_data
        else:
            with bot_lock: benchmarking_pairs.add(symbol)
            return None
    except Exception as e:
        logging.error(f"Error in analyze_pair for {symbol}: {e}")
        return None

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
                'consecutive_buys': 0, 'consecutive_sells': 0, 'last_action': 'WAITING'
            }

    import optimization
    while not shutdown_event.is_set():
        start_time = time.time()

        with bot_lock:
            rebench_syms = list(benchmarking_pairs)

        if rebench_syms:
            optimization.run_benchmark_mode(exchange, config, args, shutdown_event, bot_lock, global_pattern_pool, benchmarking_pairs, term_override=config.get('_active_term', 'short'), data_manager=data_manager, pattern_manager=pattern_manager, engine=engine, device=config.get('device'), symbols_to_process=rebench_syms, ohlcv_cache_manager=ohlcv_cache_manager)

        for symbol in all_symbols:
            if shutdown_event.is_set(): break
            if symbol in suspended_pairs: continue

            res = analyze_pair(exchange, data_manager, pattern_manager, symbol, pairs_dict[symbol], config, engine)
            if res:
                with bot_lock:
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
                        if trading_engine.execute_buy(exchange, data_manager, engine, symbol, data, config, bot_lock, available_assets, suspended_pairs):
                            data['last_action'] = 'BUY'
                            data['position'] = data_manager.get_position(symbol)
                            data['positions'] = data_manager.get_positions(symbol)
                            play_sound("buy", config)

                    if data['consecutive_sells'] >= 1 and data['positions']:
                         for idx, pos in enumerate(data['positions']):
                             if engine.is_profitable(data['price'], pos['entry_price']):
                                 if trading_engine.execute_sell(exchange, data_manager, engine, symbol, data, config, position_idx=idx):
                                     data['last_action'] = 'SELL'
                                     data['positions'] = data_manager.get_positions(symbol)
                                     data['position'] = data_manager.get_position(symbol)
                                     play_sound("sell", config)
                                     break
                             else:
                                 with bot_lock:
                                     if ui.sell_proposal_pair is None:
                                         ui.sell_proposal_pair = symbol
                                         ui.sell_proposal_profit = (data['price'] - pos['entry_price']) / pos['entry_price'] * 100
                                         ui.sell_proposal_time = time.time()
                                         logging.warning(f"[{symbol}] SELL signal received at non-profitable price ({format_price(data['price'])} < {format_price(pos['entry_price'])}). Manual confirmation required.")

            if not pending_asset_update and time.time() % 30 < 1:
                pending_asset_update = True
                threading.Thread(target=update_available_assets_live, args=(exchange, config), daemon=True).start()

        elapsed = time.time() - start_time
        if elapsed < 1.0: time.sleep(1.0 - elapsed)

def main():
    global ohlcv_cache_manager
    migrate_fresh_files_to_archive()
    load_from_archive()
    ohlcv_cache_manager = OHLCVCacheManager()

    parser = argparse.ArgumentParser(description='Cryptocurrencies Multiplatform Trading Bot')
    parser.add_argument('--mode', choices=['live', 'simulation', 'backtest', 'benchmark', 'sell', 'balance'], default='simulation')
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
    args = parser.parse_args()

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
        if torch.cuda.is_available():
            device = torch.device('cuda'); gpu_enabled = True; gpu_accel = "CUDA"
        elif torch.backends.mkldnn.is_available():
            device = torch.device('cpu'); gpu_enabled = True; torch.backends.mkldnn.enabled = True; gpu_accel = "oneDNN"
        elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            device = torch.device('mps'); gpu_enabled = True; gpu_accel = "MPS"
        else:
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

            hw_msg = f"Hardware optimization level: {opt_level} ({info.get('brand_raw', 'Unknown CPU')})"
            console.print(f"[bold green]{hw_msg}[/]")
            logging.info(hw_msg)
        except: pass

        data_manager = DataManager(args.mode) if args.mode in ['live', 'simulation', 'sell'] else None
        pattern_manager = PatternManager()
        engine = TradingEngine(config)

        api_key = api_creds.get('api_key') or config.get('api_key')
        api_secret = api_creds.get('api_secret') or config.get('api_secret')
        ex_class = EXCHANGE_MAPPING.get(args.exchange, MockExchange)

        if args.mode == 'live': exchange = ex_class(api_key, api_secret)
        elif args.mode == 'simulation': exchange = MockExchange(api_key, api_secret, exchange_type=args.exchange)
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

        if args.mode in ['live', 'simulation']:
            config['_active_term'] = args.term
            import optimization
            opt_map = optimization.run_benchmark_mode(exchange, config, args, shutdown_event, bot_lock, global_pattern_pool, benchmarking_pairs, term_override=args.term, status=status, data_manager=data_manager, pattern_manager=pattern_manager, engine=engine, device=device, ohlcv_cache_manager=ohlcv_cache_manager)
            pair_priorities = []
            for sym, best in opt_map.items():
                if sym in config['pairs']:
                    config['pairs'][sym].update({'aggr': best['aggr'], 'strategy': best['strategy'], 'expected_profit': best.get('avg_bench_profit', best['profit'])})
                    pair_priorities.append((sym, best['profit']))
            config['_priority_pairs'] = [p[0] for p in sorted(pair_priorities, key=lambda x: x[1], reverse=True)]

        for symbol in config['pairs']:
            pos_list = data_manager.get_positions(symbol)
            bot_state[symbol] = {'aggr': config['pairs'][symbol].get('aggr', 'normal'), 'strategy': config['pairs'][symbol].get('strategy', 'simple_ema'), 'last_action': 'BUY' if pos_list else 'Waiting', 'positions': pos_list, 'position': pos_list[0] if pos_list else None, 'bench_profit': config['pairs'][symbol].get('expected_profit', 0)}

    db_handler = dashboard.DashboardHandler(ui, bot_lock)
    logging.basicConfig(level=logging.INFO, handlers=[db_handler, RichHandler(console=console, show_time=False)])

    with Live(ui.make_dashboard(args.mode, config, bot_state, signal_arrival_times, bot_lock), refresh_per_second=2, screen=True) as live:
        threading.Thread(target=trading_thread_func, args=(exchange, data_manager, pattern_manager, engine, config, args), daemon=True).start()
        threading.Thread(target=ui.input_thread_func, args=(exchange, data_manager, engine, config, bot_state, bot_lock, shutdown_event, trading_engine.execute_buy, trading_engine.execute_sell, play_sound), daemon=True).start()
        while not shutdown_event.is_set():
            live.update(ui.make_dashboard(args.mode, config, bot_state, signal_arrival_times, bot_lock))
            time.sleep(0.5)

    archiver.stop()
    shutdown_msg = "Bot shutdown gracefully."
    console.print(f"[bold green]{shutdown_msg}[/]")
    logging.info(shutdown_msg)

if __name__ == "__main__":
    main()

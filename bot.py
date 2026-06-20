# Cryptocurrencies multiplatform trading bot
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import json
import time
import logging
import argparse
import os
import copy
import pandas as pd
import sys
import signal
import random
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
bot_state = {}
available_assets = []
benchmarking_pairs = set()
suspended_pairs = set()
signal_arrival_times = {}
shutdown_event = asyncio.Event()

console = Console()
ui = dashboard.DashboardUI(console)

async def perform_analysis_calculation(symbol, timeframe, tf_secs, df, search_pool, device, pattern_info, pattern_manager=None):
    """
    CPU-intensive analysis task.
    """
    try:
        # 1. Indicators
        df = get_common_indicators(df, device)

        # SPM: Try to find new patterns in the current in-memory history (O(N))
        from optimization import run_benchmark_for_symbol
        from indicators import STRATEGIES

        # Reduced strategies for speed during live analysis
        _, new_patterns = await run_benchmark_for_symbol(symbol, {}, 'short', ['balanced'], STRATEGIES, df, device=device)

        # Merge new patterns with existing ones for this session
        combined_pool = search_pool + new_patterns

        current_pattern_id = pattern_info.get('active_pattern_id')
        pattern_match_ts = pattern_info.get('pattern_match_ts', 0)
        last_mc_ts = pattern_info.get('last_mc_ts', 0)
        last_mc_score = pattern_info.get('mc_score', 1.1)
        candle_ts = df.iloc[-1]['timestamp']

        # 2. Similarity Matching (Vectorized Batch)
        active_patterns = []
        if combined_pool:
            active_patterns = calculate_similarity_batch(df, combined_pool, device=device)

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
            elif curr_vol > 0.01:
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

def wrapped_analysis_task(symbol, timeframe, tf_secs, df, search_pool, device, pattern_info):
    """Picklable wrapper for the analysis task."""
    return perform_analysis_calculation(symbol, timeframe, tf_secs, df, search_pool, device, pattern_info)

def setup_bot_state(config, data_manager, bot_state):
    """Initializes the bot state for all configured pairs, using cache if available."""
    from persistence import CacheManager
    cache_mgr = CacheManager()

    for symbol in config['pairs']:
        pos_list = data_manager.get_positions(symbol) if data_manager else []

        # Load from cache to avoid "Discovering..." if we already know this pair
        cached = cache_mgr.get(symbol, 'short')
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

async def run_initial_benchmarking(exchange, config, args, shutdown_event, global_pattern_pool, benchmarking_pairs, data_manager, pattern_manager, engine, device, ohlcv_cache_manager, available_assets, trading_engine, bot_state, ui=None):
    """Initializes the bot state and discover wallet assets/positions."""
    sellable_with_amounts = {}
    try:
        sellable_with_amounts = await trading_engine.get_sellable_assets_with_amounts(exchange, config)
        sellable = sorted(list(sellable_with_amounts.keys()))
        available_assets[:] = sellable
        for symbol, state in bot_state.items():
            asset = symbol.split('/')[0]
            state['amt'] = sellable_with_amounts.get(asset, 0)
    except Exception as e:
        logging.error(f"Failed to fetch initial wallet assets: {e}")

    # Set default priority
    config['_priority_pairs'] = sorted(list(config['pairs'].keys()))

    if args.mode in ['simulation', 'virtual', 'live']:
        await trading_engine.initialize_wallet_positions(exchange, data_manager, pattern_manager, engine, config, bot_state)

    for symbol in config['pairs']:
        pos_list = data_manager.get_positions(symbol)
        # Default values as we don't have benchmarks anymore
        if 'aggr' not in config['pairs'][symbol]: config['pairs'][symbol]['aggr'] = 'balanced'
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

async def main():
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
        db_handler = dashboard.DashboardHandler(ui)
        logging.basicConfig(
            level=logging.INFO,
            format='%(message)s',
            handlers=[db_handler, RichHandler(console=console, show_time=False, show_level=False, show_path=False)]
        )
    logging.root.setLevel(logging.INFO)

    global ohlcv_cache_manager
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

    # Hardcode max_symbol_bet default if not in config
    if 'max_symbol_bet' not in config:
        config['max_symbol_bet'] = '10%'

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
            status.stop(); await trading_engine.interactive_sell(exchange, data_manager, engine, config, console); return
        elif args.mode == 'balance':
            status.stop()
            exchange = ex_class(api_key, api_secret) if api_key not in [None, "YOUR_API_KEY"] else MockExchange(exchange_type=args.exchange)
            await trading_engine.show_balance(exchange, config, console, Table); return
        elif args.mode == 'backtest':
            status.stop()
            exchange = ex_class(api_key, api_secret) if api_key not in [None, "YOUR_API_KEY"] else MockExchange(exchange_type=args.exchange)
            import optimization
            await optimization.run_backtest_mode(exchange, config, args, engine=engine, device=device, ohlcv_cache_manager=ohlcv_cache_manager); return
        elif args.mode == 'benchmark':
            status.stop()
            exchange = ex_class(api_key, api_secret) if api_key not in [None, "YOUR_API_KEY"] else MockExchange(exchange_type=args.exchange)
            import optimization
            await optimization.run_benchmark_mode(exchange, config, args, shutdown_event, global_pattern_pool, benchmarking_pairs, status=None, data_manager=None, pattern_manager=pattern_manager, engine=engine, device=device, ohlcv_cache_manager=ohlcv_cache_manager); return

        logging.info(f"REST API enabled for {args.exchange} ({market_type}).")

        # Initial synchronous asset update to ensure immediate availability
        try:
            available_assets[:] = await trading_engine.get_sellable_assets(exchange, config)
        except: pass


    # Handle signals compatibly across platforms
    def signal_handler(sig, frame):
        logging.info("Interrupt received, shutting down gracefully...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Sequential Core handles everything now.
    core = TradingCore(config, exchange, data_manager, pattern_manager, ohlcv_cache_manager, headless=args.headless, ui=ui)
    global global_core
    global_core = core

    # Sync state
    core.bot_state = bot_state
    core.global_pattern_pool = global_pattern_pool
    core.available_assets = available_assets
    core.suspended_pairs = suspended_pairs
    core.benchmarking_pairs = benchmarking_pairs
    core.signal_arrival_times = signal_arrival_times

    if args.mode in ['live', 'simulation', 'virtual']:
        setup_bot_state(config, data_manager, bot_state)
        await run_initial_benchmarking(exchange, config, args, shutdown_event, global_pattern_pool, benchmarking_pairs, data_manager, pattern_manager, engine, device, ohlcv_cache_manager, available_assets, trading_engine, bot_state)

    if args.headless:
        await core.main_loop()
    else:
        # Silence ALL other handlers during Dashboard execution, keeping ONLY DashboardHandler
        all_other_handlers = [h for h in logging.root.handlers if not isinstance(h, dashboard.DashboardHandler)]
        for h in all_other_handlers:
            logging.root.removeHandler(h)

        with Live(ui.make_dashboard(args.mode, config, bot_state, signal_arrival_times, None), refresh_per_second=2, screen=True) as live:
            core.live = live
            await core.main_loop()

        # Restore console logging on exit
        for h in all_other_handlers:
            logging.root.addHandler(h)

    if global_core:
        try:
            await global_core.shutdown()
        except: pass

    shutdown_msg = "Bot shutdown gracefully."
    console.print(f"[bold green]{shutdown_msg}[/]")

if __name__ == "__main__":
    asyncio.run(main())

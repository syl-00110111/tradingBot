# CCXT Pro Trading Bot v2 (Asynchronous)
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import asyncio
import json
import time
import logging
import signal
import argparse
import os
import sys
import platform
import random
import math
import numpy as np
import threading
import queue
from collections import deque
import pandas as pd
import torch
import concurrent.futures
import plotext as plt_ascii
from datetime import datetime, timedelta, timezone

from rich.live import Live
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel
from rich.console import Console
from rich.logging import RichHandler
from rich.text import Text

import readchar

# Import renamed modules
from exchange_handler2 import CCXTExchange2
from indicators2 import get_signals, STRATEGIES, STRATEGY_GROUPS
from persistence2 import DataManager, CacheManager, PatternManager
from trading_engine2 import TradingEngine
from monte_carlo2 import MonteCarloEngine

# Global Monte Carlo Engine
mc_engine = None # Initialized in main after config load

# Global analysis tracking to avoid overlapping
analysis_in_progress = set()
analysis_lock = asyncio.Lock()

# Global Watcher Task
ohlcv_task = None

# Track orders placed by the bot to process them via WebSocket confirmation
pending_orders = {} # order_id -> metadata_dict
pending_orders_lock = asyncio.Lock()
processed_orders = None # Initialized in main after config load
processed_orders_lock = asyncio.Lock()

# Global controls for dashboard
pairs_scroll_offset = 0
selected_pair_index = 0
show_chart = False
chart_symbol = None
chart_cache = {"symbol": None, "last_update": 0, "content": None}
local_timezone = datetime.now().astimezone().tzinfo
plotext_lock = threading.Lock()
logs_scroll_offset = 0
focused_panel = "pairs"
ohlcv_cache = {}
all_logs = []
status_scroll_index = 0
expert_mode = False
show_help = False
startup_complete = False
marquee_enabled = False
shutdown_event = asyncio.Event()
ui_task = None
background_tasks = []
active_scans = {}
bench_executor = None

# Global UI Constants
MAX_STRAT_LEN = 20 # Updated in main

# Marquee Timing Control
last_marquee_update = 0
pairs_pause_until = 0
logs_pause_until = 0
status_scroll_index = 0
ctrl_c_count = 0

def handle_stop_signal(sig=None, frame=None):
    global ctrl_c_count
    ctrl_c_count += 1
    if ctrl_c_count > 1:
        os._exit(1)
    shutdown_event.set()

def global_exception_handler(loop, context):
    msg = context.get("message")
    exception = context.get("exception")

    # Suppress known harmless aiohttp/WebSocket connection reset errors that occur in background tasks
    suppress = False
    if msg and "Cannot write to closing transport" in msg:
        suppress = True
    elif exception and "ClientConnectionResetError" in str(type(exception)):
        suppress = True

    if suppress:
        logging.debug(f"Suppressed background task exception: {msg}")
        return

    # Call the default handler for everything else
    loop.default_exception_handler(context)

# Sound Queue and Worker for non-blocking audio
sound_queue = queue.Queue()

def sound_worker():
    while True:
        try:
            item = sound_queue.get()
            if item is None: break
            action, bot_config = item

            system = platform.system().lower()
            audio_cfg = bot_config.get('audio', {}) if bot_config else {}
            if system == "windows":
                import winsound
                if action == "startup":
                    cfg = audio_cfg.get('startup', {})
                    for _ in range(cfg.get('beeps', 5)):
                        winsound.Beep(random.randint(cfg.get('min_freq', 440), cfg.get('max_freq', 880)), cfg.get('duration', 100))
                elif action == "buy":
                    cfg = audio_cfg.get('buy', {})
                    winsound.MessageBeep(winsound.MB_OK)
                    winsound.Beep(cfg.get('freq', 1000), cfg.get('duration', 250))
                elif action == "sell":
                    cfg = audio_cfg.get('sell', {})
                    winsound.MessageBeep(winsound.MB_ICONHAND)
                    winsound.Beep(cfg.get('freq', 600), cfg.get('duration', 250))
            else:
                if action == "startup":
                    sys.stdout.write("\a")
                elif action == "buy":
                    sys.stdout.write("\a")
                elif action == "sell":
                    sys.stdout.write("\a\a")
                sys.stdout.flush()
            sound_queue.task_done()
        except Exception:
            pass

# Start the dedicated sound thread
threading.Thread(target=sound_worker, daemon=True).start()

def play_sound(action, config=None):
    sound_queue.put((action, config))

# State shared between tasks
bot_state = {}
pair_suspensions = {}
current_balances = {}
bot_lock = asyncio.Lock()
ohlcv_lock = asyncio.Lock()

console = Console()

class AsyncDashboardHandler(logging.Handler):
    def __init__(self, duration=5):
        super().__init__()
        self.duration = duration

    def emit(self, record):
        msg = self.format(record)
        timestamp = datetime.now().strftime("%H:%M:%S")
        expiry = datetime.now() + timedelta(seconds=self.duration)

        # Status update merging
        if "Syncing positions from" in msg and "done." in msg:
            base_msg = msg.replace(" done.", "")
            for log in all_logs:
                if base_msg in log['msg'] and "done." not in log['msg']:
                    log['msg'] = msg
                    log['timestamp'] = timestamp
                    log['expiry'] = expiry
                    return

        if "Bot v2 fully operational." in msg or "[bold green]Bot v2 fully operational." in msg:
            for log in all_logs:
                if "Waiting for system initialization..." in log['msg']:
                    log['msg'] = msg
                    log['timestamp'] = timestamp
                    log['expiry'] = expiry
                    return

        all_logs.append({'msg': msg, 'timestamp': timestamp, 'expiry': expiry})
        if len(all_logs) > 500:
            all_logs.pop(0)

        # Print to console during startup to keep track of progress
        if not startup_complete:
            console.print(f"[{timestamp}] {msg}")

db_handler = AsyncDashboardHandler()
db_handler.setFormatter(logging.Formatter("%(message)s"))

# root_logger will only use db_handler to avoid clearing console
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(db_handler)

def deep_merge(base, override):
    """
    Recursively merges override into base.
    """
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base

def load_config_from_path(path):
    if not os.path.exists(path):
        console.print(f"[bold red]Error: Configuration file '{path}' not found.[/]")
        sys.exit(1)
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception as e:
        console.print(f"[bold red]Error parsing configuration file '{path}': {e}[/]")
        sys.exit(1)

def validate_config(config):
    required_sections = ['timeouts', 'exchange', 'monte_carlo', 'ui', 'trading', 'strategies', 'audio']
    missing_sections = [s for s in required_sections if s not in config]
    if missing_sections:
        logging.warning(f"Configuration is missing sections: {', '.join(missing_sections)}. Using internal fallbacks.")

    # Check for critical parameters
    critical_keys = [
        ('exchange', 'default_fee'),
        ('trading', 'base_target_pct'),
        ('monte_carlo', 'num_simulations')
    ]
    for section, key in critical_keys:
        if section in config and key not in config[section]:
            logging.warning(f"Critical configuration parameter missing: {section}.{key}. Using internal fallback.")

def load_config():
    config = {}
    if os.path.exists('config.default.json'):
        config = load_config_from_path('config.default.json')

    if os.path.exists('config.json'):
        override = load_config_from_path('config.json')
        if not config:
            config = override
        else:
            config = deep_merge(config, override)

    if not config:
        console.print(f"[bold red]Error: No configuration file found (config.json or config.default.json).[/]")
        sys.exit(1)

    validate_config(config)
    return config

def precision_to_int(p):
    if p is None: return 8
    if isinstance(p, int): return p
    if isinstance(p, float):
        if p > 0:
            return max(0, int(-math.log10(p)))
    return 8

def format_price(price, precision=None, config=None):
    if price is None: return "-"
    if not isinstance(price, (int, float)): return str(price)
    if price == 0: return "0"

    ui_cfg = config.get('ui', {}) if config else {}
    sci_threshold = ui_cfg.get('scientific_threshold', 0.00001)

    # Scientific notation for very small numbers
    if abs(price) < sci_threshold:
        return f"{price:.4e}"

    # Default to configured decimal places, but keep precision if provided
    default_p = ui_cfg.get('display_precision', 6)
    p_int = precision_to_int(precision) if precision is not None else default_p
    formatted = f"{price:.{p_int}f}".rstrip('0').rstrip('.')
    if (formatted == "" or formatted == "0") and price != 0:
        max_p = (ui_cfg.get('default_precision', 8) or 8) + 2
        return f"{price:.{max_p}f}".rstrip('0').rstrip('.')
    return formatted if formatted != "" else "0"

def format_amt(amt, precision=None, config=None):
    if amt is None: return "-"
    if not isinstance(amt, (int, float)): return str(amt)
    if amt == 0: return "0"

    ui_cfg = config.get('ui', {}) if config else {}
    sci_threshold = ui_cfg.get('scientific_threshold', 0.00001)

    if abs(amt) < sci_threshold:
        return f"{amt:.4e}"

    default_p = ui_cfg.get('display_precision', 6)
    p_int = precision_to_int(precision) if precision is not None else default_p
    formatted = f"{amt:.{p_int}f}".rstrip('0').rstrip('.')
    if (formatted == "" or formatted == "0") and amt != 0:
        max_p = (ui_cfg.get('default_precision', 8) or 8) + 2
        return f"{amt:.{max_p}f}".rstrip('0').rstrip('.')
    return formatted if formatted != "" else "0"

def render_ascii_chart_sync(symbol, df, config, width, height):
    global local_timezone
    with plotext_lock:
        try:
            plt_ascii.clear_figure()
            plt_ascii.clf()
            plt_ascii.theme('dark')
            plt_ascii.subplots(2, 1)

            plt_ascii.subplot(1, 1)
            plt_ascii.clf()
            plt_ascii.theme('dark')
            plt_ascii.title(f"K-Lines: {symbol} (1s)")

            if df.index.tz is None:
                utc_times = df.index.tz_localize(timezone.utc)
            else:
                utc_times = df.index.tz_convert(timezone.utc)

            labels = [t.astimezone(local_timezone).strftime("%H:%M:%S") for t in utc_times]

            indices = list(range(len(df)))
            df_plot = df[['open', 'high', 'low', 'close']].copy()
            df_plot.columns = ['Open', 'High', 'Low', 'Close']
            df_plot.reset_index(drop=True, inplace=True)

            plt_ascii.candlestick(indices, df_plot)
            tick_indices = np.linspace(0, len(df) - 1, min(10, len(df)), dtype=int).tolist()
            plt_ascii.xticks(tick_indices, [labels[i] for i in tick_indices])

            plt_ascii.subplot(2, 1)
            plt_ascii.clf()
            plt_ascii.theme('dark')
            volumes = df['volume'].tolist()
            plt_ascii.bar(indices, volumes, color='blue', label='Volume')
            plt_ascii.title("Volume")
            plt_ascii.xticks(tick_indices, [labels[i] for i in tick_indices])

            h_volume = max(5, height // 3)
            h_klines = height - h_volume

            plt_ascii.subplot(1, 1).plotsize(width, h_klines)
            plt_ascii.subplot(2, 1).plotsize(width, h_volume)
            return Text.from_ansi(plt_ascii.build())
        except Exception as e:
            return Text(f"Rendering error: {e}", style="bold red")

def render_ascii_chart(symbol, config):
    global chart_cache
    if chart_cache["symbol"] == symbol and chart_cache["content"]:
        return chart_cache["content"]
    return Text(f"Preparing chart for {symbol}...", style="yellow")

async def chart_renderer_task(config):
    global chart_symbol, show_chart, chart_cache
    last_rendered_ts = 0

    while not shutdown_event.is_set():
        try:
            if show_chart and chart_symbol:
                symbol = chart_symbol
                df = ohlcv_cache.get(symbol)
                if df is not None and not df.empty:
                    ui_cfg = config.get('ui', {})
                    df_tail = df.tail(ui_cfg.get('chart_candles', 100))
                    current_last_ts = int(df_tail.index[-1].timestamp())

                    if chart_cache["symbol"] != symbol or chart_cache["last_update"] != current_last_ts:
                        # Throttle rendering to 0.2s for near real-time updates
                        if time.time() - last_rendered_ts > 0.2:
                            width = console.width - 4
                            h_offset = ui_cfg.get('panel_height_offset', 20)
                            height = console.height - h_offset
                            min_w, min_h = ui_cfg.get('min_width', 20), ui_cfg.get('min_height', 15)
                            width, height = max(width, min_w), max(height, min_h)

                            loop = asyncio.get_running_loop()
                            content = await loop.run_in_executor(None, render_ascii_chart_sync, symbol, df_tail.copy(), config, width, height)

                            chart_cache = {
                                "symbol": symbol,
                                "last_update": current_last_ts,
                                "content": content
                            }
                            last_rendered_ts = time.time()
            await asyncio.sleep(0.1)
        except Exception:
            await asyncio.sleep(1)

async def input_task(exchange, config, data_manager, engine):
    global focused_panel, selected_pair_index, pairs_scroll_offset, logs_scroll_offset
    global expert_mode, marquee_enabled, show_help, show_chart, chart_symbol
    global pairs_pause_until, logs_pause_until

    while not shutdown_event.is_set():
        try:
            loop = asyncio.get_event_loop()

            # Use non-blocking check for stdin if possible to remain responsive to shutdown_event
            system = platform.system().lower()
            if system == "windows":
                import msvcrt
                if not msvcrt.kbhit():
                    await asyncio.sleep(0.1)
                    continue
            else:
                import select
                if not select.select([sys.stdin], [], [], 0.5)[0]:
                    continue

            key = await loop.run_in_executor(None, readchar.readkey)

            if key == readchar.key.CTRL_C or key == '\x03':
                handle_stop_signal()
                break

            if not startup_complete: continue

            all_pairs = get_sorted_symbols(config)
            ui_cfg = config.get('ui', {})
            h_offset = ui_cfg.get('panel_height_offset', 20)
            pairs_height = console.height - h_offset
            if pairs_height < 3: pairs_height = 3

            if show_chart or show_help:
                if key in [readchar.key.ENTER, readchar.key.ESC, 'q', 'Q', 'h', 'H']:
                    show_chart = False
                    show_help = False
                elif show_chart and chart_symbol and key.lower() == 'b':
                    price = bot_state.get(chart_symbol, {}).get('price', 0)
                    if price > 0:
                        logging.info(f"[Manual] Triggering BUY for {chart_symbol}")
                        candle_count = len(ohlcv_cache.get(chart_symbol, []))
                        asyncio.create_task(execute_buy(exchange, chart_symbol, {'close': price}, data_manager, engine, config, manual=True, strategy="Manual", candle_count=candle_count))
                elif show_chart and chart_symbol and key.lower() == 's':
                    price = bot_state.get(chart_symbol, {}).get('price', 0)
                    if price > 0:
                        logging.info(f"[Manual] Triggering SELL for {chart_symbol}")
                        candle_count = len(ohlcv_cache.get(chart_symbol, []))
                        asyncio.create_task(execute_sell(exchange, chart_symbol, {'close': price}, data_manager, engine, config, force=True, strategy="Manual", candle_count=candle_count))
                continue

            if key == readchar.key.TAB:
                focused_panel = "logs" if focused_panel == "pairs" else "pairs"
            elif key == readchar.key.UP:
                if focused_panel == "pairs":
                    selected_pair_index = max(0, selected_pair_index - 1)
                    if selected_pair_index < pairs_scroll_offset:
                        pairs_scroll_offset = selected_pair_index
                    pairs_pause_until = time.time() + config.get('timeouts', {}).get('ui_pause_short', 5)
                else:
                    logs_scroll_offset += 1
                    logs_pause_until = time.time() + config.get('timeouts', {}).get('ui_pause_long', 30)
            elif key == readchar.key.DOWN:
                if focused_panel == "pairs":
                    selected_pair_index = min(len(all_pairs) - 1, selected_pair_index + 1)
                    if selected_pair_index >= pairs_scroll_offset + pairs_height:
                        pairs_scroll_offset = selected_pair_index - pairs_height + 1
                    pairs_pause_until = time.time() + config.get('timeouts', {}).get('ui_pause_short', 5)
                else:
                    logs_scroll_offset = max(0, logs_scroll_offset - 1)
                    logs_pause_until = time.time() + config.get('timeouts', {}).get('ui_pause_long', 30)
            elif key == readchar.key.PAGE_UP:
                scroll_step = ui_cfg.get('scroll_step', 10)
                if focused_panel == "pairs":
                    selected_pair_index = max(0, selected_pair_index - pairs_height)
                    pairs_scroll_offset = max(0, pairs_scroll_offset - pairs_height)
                    pairs_pause_until = time.time() + config.get('timeouts', {}).get('ui_pause_short', 5)
                elif focused_panel == "logs":
                    logs_scroll_offset += scroll_step
                    logs_pause_until = time.time() + config.get('timeouts', {}).get('ui_pause_long', 30)
            elif key == readchar.key.PAGE_DOWN:
                scroll_step = ui_cfg.get('scroll_step', 10)
                if focused_panel == "pairs":
                    max_pairs_offset = max(0, len(all_pairs) - pairs_height)
                    selected_pair_index = min(len(all_pairs) - 1, selected_pair_index + pairs_height)
                    pairs_scroll_offset = min(max_pairs_offset, pairs_scroll_offset + pairs_height)
                    pairs_pause_until = time.time() + config.get('timeouts', {}).get('ui_pause_short', 5)
                elif focused_panel == "logs":
                    logs_scroll_offset = max(0, logs_scroll_offset - scroll_step)
                    logs_pause_until = time.time() + config.get('timeouts', {}).get('ui_pause_long', 30)
            elif key == readchar.key.HOME:
                if focused_panel == "pairs":
                    selected_pair_index = 0
                    pairs_scroll_offset = 0
                    pairs_pause_until = time.time() + config.get('timeouts', {}).get('ui_pause_short', 5)
                elif focused_panel == "logs":
                    logs_scroll_offset = max(0, len(all_logs) - 8) # 8 is log_height
                    logs_pause_until = time.time() + config.get('timeouts', {}).get('ui_pause_long', 30)
            elif key == readchar.key.END:
                if focused_panel == "pairs":
                    max_pairs_offset = max(0, len(all_pairs) - pairs_height)
                    selected_pair_index = len(all_pairs) - 1
                    pairs_scroll_offset = max_pairs_offset
                    pairs_pause_until = time.time() + config.get('timeouts', {}).get('ui_pause_short', 5)
                elif focused_panel == "logs":
                    logs_scroll_offset = 0
                    logs_pause_until = time.time() + config.get('timeouts', {}).get('ui_pause_long', 30)
            elif key.lower() == 'x':
                expert_mode = not expert_mode
            elif key.lower() == 'm':
                marquee_enabled = not marquee_enabled
            elif key.lower() == 'h':
                show_help = not show_help
            elif key == readchar.key.ENTER:
                if focused_panel == "pairs" and all_pairs:
                    chart_symbol = all_pairs[selected_pair_index]
                    show_chart = True

        except (asyncio.CancelledError, KeyboardInterrupt):
            break
        except Exception as e:
            logging.error(f"Input error: {e}")
        await asyncio.sleep(0.1)

async def watch_ohlcv_global_task(exchange, watch_pairs, config, data_manager, pattern_manager, engine, device, executor):
    """
    Single watcher task for all symbols.
    'watch_pairs' is a list of [symbol, timeframe] where timeframe is always 1s.
    """
    logging.info(f"[bold cyan]Starting global OHLCV watcher for {len(watch_pairs)} symbols.")

    while not shutdown_event.is_set():
        try:
            async for updates in exchange.watch_ohlcv_for_symbols(watch_pairs):
                if shutdown_event.is_set(): break

                # Update all prices in the batch first for maximum perceived responsiveness in the dashboard
                async with bot_lock:
                    for data in updates:
                        if isinstance(data, tuple) and len(data) == 3:
                            symbol, _, candles = data
                            if symbol in bot_state:
                                bot_state[symbol]['price'] = candles[-1][4]

                for data in updates:
                    if isinstance(data, tuple) and len(data) == 3:
                        symbol, timeframe, candles = data
                    else: continue

                    async with ohlcv_lock:
                        if symbol not in ohlcv_cache:
                            ohlcv_cache[symbol] = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')

                        df = ohlcv_cache[symbol]

                        # Optimization: if only updating the current/last candle, do it in-place
                        last_candle = candles[-1]
                        last_ts = pd.to_datetime(last_candle[0], unit='ms')

                        if not df.empty and last_ts == df.index[-1]:
                            # In-place update of the most recent candle
                            df.iloc[-1] = last_candle[1:]
                        else:
                            # Full update for gaps or new candles
                            new_candles_df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                            new_candles_df['timestamp'] = pd.to_datetime(new_candles_df['timestamp'], unit='ms')
                            new_candles_df.set_index('timestamp', inplace=True)

                            df = pd.concat([df, new_candles_df])
                            df = df[~df.index.duplicated(keep='last')]
                            df.sort_index(inplace=True)
                            ohlcv_cache[symbol] = df.tail(config.get('exchange', {}).get('fetch_ohlcv_limit', 10000))

                    # Trigger analysis
                    async with analysis_lock:
                        if symbol not in analysis_in_progress:
                            analysis_in_progress.add(symbol)
                            asyncio.create_task(analyze_and_trade_wrapper(exchange, symbol, config, data_manager, pattern_manager, engine, device, executor))

        except Exception as e:
            if not shutdown_event.is_set():
                err_msg = str(e).lower()
                logging.error(f"WebSocket OHLCV error: {e}")
                if "ping-pong" in err_msg or "timeout" in err_msg:
                    try: await exchange.close()
                    except: pass
                await asyncio.sleep(5)
            else: break

async def sync_live_positions(exchange, data_manager, config):
    exchange_id = exchange.exchange_id
    logging.info(f"Syncing positions from {exchange_id} API...")
    balance = await exchange.fetch_balance()
    if balance is None:
        logging.error("Failed to sync live positions: balances are unavailable.")
        return

    if isinstance(balance, dict) and 'free' in balance and isinstance(balance['free'], dict):
        free_balances = balance['free']
    else:
        free_balances = balance

    pairs_dict = config.get('pairs', {})
    base_currencies = sorted(list(set([p.split('/')[1] for p in pairs_dict.keys() if '/' in p])))
    if not base_currencies: base_currencies = ['USDT', 'USDC', 'EUR']

    sellable_found = False
    all_tickers = {}
    try:
        all_tickers = await exchange.fetch_tickers()
    except: pass

    async def process_asset(asset, amount):
        nonlocal sellable_found
        if asset in base_currencies or amount <= 0: return

        symbol = None
        for bc in base_currencies:
            candidate = f"{asset}/{bc}"
            if candidate in pairs_dict:
                symbol = candidate
                break
        if not symbol: return

        existing_pos_list = data_manager.get_position(symbol)
        if existing_pos_list:
            total_existing_amount = sum(p['amount'] for p in existing_pos_list)
            sync_tolerance = config.get('exchange', {}).get('sync_tolerance', 0.001)
            if abs(total_existing_amount - amount) / amount < sync_tolerance:
                sellable_found = True
                return

        is_dust = False
        try:
            ticker = all_tickers.get(symbol) or await exchange.fetch_ticker(symbol)
            if symbol in exchange.markets:
                m = exchange.markets[symbol]
                min_amt = m['limits']['amount']['min']
                min_cost = m['limits']['cost']['min'] or config.get('exchange', {}).get('min_notional_fallback', 10.0)
                if ticker and ticker.get('last') is not None and (amount < min_amt or (amount * ticker['last']) < min_cost):
                    is_dust = True
            elif amount <= config.get('exchange', {}).get('dust_threshold_amount', 1e-6): is_dust = True
        except: pass

        if is_dust: return
        sellable_found = True

        avg_price = 0
        total_cost = 0
        total_fee = 0
        accumulated_amount = 0
        try:
            # Add timeout to prevent hanging on slow responses
            trades = await asyncio.wait_for(exchange.fetch_my_trades(symbol, limit=50), timeout=config.get('timeouts', {}).get('order_fetch', 10))
            trades.sort(key=lambda t: t['timestamp'], reverse=True)

            for t in trades:
                if t['side'] == 'buy':
                    remaining_to_fill = amount - accumulated_amount
                    if remaining_to_fill <= 0: break

                    trade_amt = min(t['amount'], remaining_to_fill)
                    total_cost += trade_amt * t['price']

                    if 'fee' in t and t['fee']:
                        fee_cost = t['fee'].get('cost', 0)
                        fee_currency = t['fee'].get('currency')
                        if fee_cost > 0:
                            actual_fee = await exchange.get_fee_in_quote(symbol, fee_cost, fee_currency)
                            total_fee += actual_fee * (trade_amt / t['amount'])

                    accumulated_amount += trade_amt

            if accumulated_amount > 0:
                avg_price = total_cost / accumulated_amount
                sync_threshold = config.get('exchange', {}).get('sync_threshold', 0.99)
                if accumulated_amount < amount * sync_threshold:
                    ticker = all_tickers.get(symbol) or await exchange.fetch_ticker(symbol)
                    curr_p = (ticker.get('last') or 0) if ticker else 0
                    if curr_p > 0:
                        rest_amount = amount - accumulated_amount
                        rest_cost = rest_amount * curr_p
                        total_cost += rest_cost

                        # Estimate remaining fee if some trades are missing
                        if accumulated_amount > 0:
                            total_fee += (total_fee / accumulated_amount) * rest_amount

                        avg_price = total_cost / amount
        except Exception as e:
            logging.warning(f"[{symbol}] Error fetching trade history for sync: {e}")

        if avg_price <= 0:
            ticker = all_tickers.get(symbol) or await exchange.fetch_ticker(symbol)
            avg_price = (ticker.get('last') or 0) if ticker else 0

        if avg_price > 0:
            data_manager.add_position(
                symbol, avg_price, amount, total_fee,
                {"info": "launch_sync", "auto_sell_disabled": True}, time.time(),
                total_base=total_cost + total_fee
            )
            logging.info(f"[{symbol}] Synced balance: {amount} at calculated avg price {format_price(avg_price)}")
        else:
            logging.warning(f"[{symbol}] Asset found in wallet but price unavailable.")

    # Parallelize processing of all assets with a semaphore to avoid rate limits
    sync_semaphore = asyncio.Semaphore(config.get('exchange', {}).get('max_concurrent_syncs', 3))
    async def process_with_semaphore(asset, amount):
        async with sync_semaphore:
            await process_asset(asset, amount)

    await asyncio.gather(*[process_with_semaphore(a, am) for a, am in free_balances.items()])

    # Update global bot_state for dashboard
    async with bot_lock:
        open_positions = data_manager.get_open_positions()
        for symbol, pos_list in open_positions.items():
            if symbol not in bot_state:
                bot_state[symbol] = {}
            bot_state[symbol]['position'] = pos_list

    logging.info(f"Syncing positions from {exchange_id} API done.")

def worker_process_init():
    import signal
    try:
        # Ignore SIGINT in worker processes to avoid messy KeyboardInterrupt tracebacks
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except:
        pass

async def analyze_and_trade_wrapper(exchange, symbol, config, data_manager, pattern_manager, engine, device, executor):
    # configured cooldown per pair
    async with bot_lock:
        last_analysis = bot_state.get(symbol, {}).get('last_analysis_ts', 0)

    cooldown = config.get('timeouts', {}).get('analysis_cooldown', 12)
    if time.time() - last_analysis < cooldown:
        async with analysis_lock:
            if symbol in analysis_in_progress:
                analysis_in_progress.remove(symbol)
        return

    try:
        await analyze_and_trade(exchange, symbol, config, data_manager, pattern_manager, engine, device, executor)
    except Exception as e:
        logging.error(f"Error in analysis for {symbol}: {e}", exc_info=True)
    finally:
        async with bot_lock:
            if symbol in bot_state:
                bot_state[symbol]['last_analysis_ts'] = time.time()
                # Schedule strategy change on cooldown expiry
                bot_state[symbol]['strategy'] = random.choice(STRATEGIES)
        async with analysis_lock:
            if symbol in analysis_in_progress:
                analysis_in_progress.remove(symbol)

async def analyze_and_trade(exchange, symbol, config, data_manager, pattern_manager, engine, device, executor=None):
    try:
        # Check suspensions
        now_ts = time.time()
        is_suspended = False
        if symbol in pair_suspensions:
            susp = pair_suspensions[symbol]
            if now_ts < susp.get('until', 0):
                is_suspended = True
            elif susp.get('reason') == 'budget':
                balance = current_balances
                base_curr = symbol.split('/')[1]
                free_bal = balance.get(base_curr, {}).get('free', 0) if isinstance(balance.get(base_curr), dict) else balance.get(base_curr, 0)
                if free_bal >= susp.get('amount_required', 0) * 1.2:
                    logging.info(f"[{symbol}] Budget recovered. Resuming pair.")
                    del pair_suspensions[symbol]
                else:
                    is_suspended = True
            else:
                del pair_suspensions[symbol]

        async with ohlcv_lock:
            if symbol not in ohlcv_cache: return
            df = ohlcv_cache[symbol].copy()

        if df.empty or len(df) < 250: return

        loop = asyncio.get_event_loop()

        # Populate common indicators
        common_settings = {
            'device': device,
            'ema_fast': config.get('ema_fast'),
            'ema_slow': config.get('ema_slow'),
            'macd_fast': config.get('macd_fast'),
            'macd_slow': config.get('macd_slow'),
            'macd_signal': config.get('macd_signal'),
            'rsi_period': config.get('rsi_period'),
            'tema_length': config.get('tema_length')
        }
        if executor:
            df = await loop.run_in_executor(executor, get_signals, df, common_settings, False, config)
        else:
            df = get_signals(df, common_settings, global_config=config)

        latest_base = df.iloc[-1]

        # Single Strategy Evaluation
        pair_config = config['pairs'].get(symbol, {})
        strat = pair_config.get('strategy') or data_manager.data.get('open_positions', {}).get(symbol, [{}])[0].get('strategy') or bot_state.get(symbol, {}).get('strategy')
        aggr = pair_config.get('aggr') or data_manager.data.get('open_positions', {}).get(symbol, [{}])[0].get('aggr') or bot_state.get(symbol, {}).get('aggr')

        if not strat:
            strat = random.choice(STRATEGIES)
        if not aggr:
            aggr = random.choice(['normal', 'aggressive', 'dynamic'])

        mode_settings = engine.get_dynamic_settings(latest_base.get('adx'), latest_base.get('volatility'), aggr=aggr)
        mode_settings['strategy'] = strat
        mode_settings['device'] = device

        if executor:
            df = await loop.run_in_executor(executor, get_signals, df, mode_settings, False, config)
        else:
            df = get_signals(df, mode_settings, global_config=config)

        if df.empty: return

        latest = df.iloc[-1]
        buy_candidate = latest.get('buy_signal', False)
        sell_candidate = latest.get('sell_signal', False)
        total_score = 1 if buy_candidate else (-1 if sell_candidate else 0)

        # Simple backtest profit metric for display
        profit = 0
        test_df = df.tail(config.get('trading', {}).get('backtest_profit_candles', 400))
        pos = None
        for _, row in test_df.iterrows():
            if row['buy_signal'] and pos is None:
                pos = row['close']
            elif row['sell_signal'] and pos is not None:
                profit += (row['close'] - pos)
                pos = None

        async with bot_lock:
            if symbol not in bot_state: bot_state[symbol] = {}
            bot_state[symbol].update({
                'strategy': strat,
                'aggr': mode_settings.get('effective_aggr', aggr),
                'expected_profit': profit
            })

        # Update State
        async with bot_lock:
            bot_state[symbol].update({
                'price': latest['close'],
                'ema_f': latest.get('ema_f', 0),
                'ema_s': latest.get('ema_s', 0),
                'macd_hist': latest.get('macd_hist', 0),
                'rsi': latest.get('rsi', 0),
                'adx': latest.get('adx', 0),
                'volatility': latest.get('volatility', 0),
                'score': total_score,
                'consecutive_buys': 1 if buy_candidate else 0,
                'consecutive_sells': 1 if sell_candidate else 0,
                'tendency': "Bullish" if total_score > 0 else ("Bearish" if total_score < 0 else "Neutral"),
                'last_signal': 'Buy' if buy_candidate else ('Sell' if sell_candidate else 'Waiting')
            })

        if buy_candidate and not is_suspended:
            # Monte Carlo validation for buy signals
            mc_score = await loop.run_in_executor(None, mc_engine.validate_strategy, df)
            mc_threshold = config.get('mc_threshold', 0.86)
            if mc_score >= mc_threshold:
                await execute_buy(exchange, symbol, latest, data_manager, engine, config, strategy=strat, candle_count=len(df))
            else:
                async with bot_lock:
                    if symbol in bot_state:
                        bot_state[symbol].update({
                            'last_signal': 'Waiting',
                            'score': 0,
                            'consecutive_buys': 0,
                            'consecutive_sells': 0,
                            'strategy': random.choice(STRATEGIES)
                        })
        elif sell_candidate:
            await execute_sell(exchange, symbol, latest, data_manager, engine, config, strategy=strat, candle_count=len(df))

    except Exception as e:
        logging.error(f"Analysis error for {symbol}: {e}")

async def execute_buy(exchange, symbol, data, data_manager, engine, config, manual=False, strategy=None, candle_count=0):
    global current_balances

    # Check for pending orders to avoid duplicates
    async with pending_orders_lock:
        for oid, po in pending_orders.items():
            if po['symbol'] == symbol and po['side'] == 'buy':
                logging.debug(f"[{symbol}] Skipping BUY: order {oid} is already pending.")
                return

    async with bot_lock:
        pos = data_manager.get_position(symbol)
        max_lots = config['pairs'].get(symbol, {}).get('max_lots_per_symbol') or config.get('max_lots_per_symbol')
        if pos is not None and len(pos) >= max_lots:
            if manual: logging.warning(f"[{symbol}] Manual BUY ignored: max_lots_per_symbol ({max_lots}) reached.")
            return

        open_positions = data_manager.get_open_positions()
        max_open = config.get('max_open_positions')
        if symbol not in open_positions and len(open_positions) >= max_open:
            if manual: logging.warning(f"[{symbol}] Manual BUY ignored: max_open_positions ({max_open}) reached.")
            return

    try:
        price = data['close']
        order = None

        # Check Notional Limit
        market = exchange.markets.get(symbol)

        quote_curr = symbol.split('/')[1]
        async with bot_lock:
            balance = current_balances

        # Verify feasibility by checking the available balance for the quote asset
        # Fetch if no data has been received for the quote asset
        quote_bal_data = balance.get(quote_curr) if balance else None
        if quote_bal_data is None:
            logging.info(f"[{symbol}] Balance for {quote_curr} missing. Fetching from exchange...")
            balance = await exchange.fetch_balance()
            async with bot_lock:
                current_balances = balance

        amount = engine.calculate_position_size(balance, price, quote_curr)
        cost = amount * price

        if amount > 0:
            min_notional = config.get('exchange', {}).get('min_notional_fallback', 10.0)
            if market and 'limits' in market and 'cost' in market['limits'] and market['limits']['cost']['min']:
                min_notional = float(market['limits']['cost']['min'])

            if cost < min_notional:
                buffer = config.get('exchange', {}).get('notional_buffer', 1.05)
                amount = (min_notional / price) * buffer
                cost = amount * price

            quote_curr = symbol.split('/')[1]
            free_balance = balance.get(quote_curr, {}).get('free', 0) if isinstance(balance.get(quote_curr), dict) else balance.get(quote_curr, 0)

            if free_balance < cost:
                pair_suspensions[symbol] = {'reason': 'budget', 'amount_required': cost}
                return

            # Create the order first (outside the lock) to avoid blocking other symbols/tasks
            order = await exchange.create_order(symbol, 'buy', amount)

            if order and 'id' in order:
                async with pending_orders_lock:
                    pending_orders[str(order['id'])] = {
                        'symbol': symbol,
                        'side': 'buy',
                        'timestamp': time.time(),
                        'trigger_data': {},
                        'strategy': strategy or ("Manual" if manual else "Unknown"),
                        'candle_count': candle_count
                    }
            else:
                pair_suspensions[symbol] = {'reason': 'budget', 'amount_required': cost}
                return

        # If order is already closed (some exchanges return filled orders immediately), process it.
        # We call this outside the lock to avoid re-entrancy issues with process_order_fill
        if order and order.get('status') == 'closed':
            await process_order_fill(order, exchange, data_manager, config, engine)
    except Exception as e:
        logging.error(f"Buy failed for {symbol}: {e}")

async def execute_sell(exchange, symbol, data, data_manager, engine, config, force=False, strategy=None, candle_count=0):
    # Check for pending orders to avoid duplicates
    async with pending_orders_lock:
        for oid, po in pending_orders.items():
            if po['symbol'] == symbol and po['side'] == 'sell':
                logging.debug(f"[{symbol}] Skipping SELL: order {oid} is already pending.")
                return

    async with bot_lock:
        positions = data_manager.get_position(symbol)
        if not positions: return

    # Use ticker price if possible for more accurate profitability check
    try:
        ticker = await exchange.fetch_ticker(symbol)
        price = (ticker.get('last') or data['close']) if ticker else data['close']
    except:
        price = data['close']

    fee_rate = await exchange.fetch_trading_fee(symbol)

    # Fetch actual balance to avoid "insufficient balance" errors due to external trades or fees
    asset = symbol.split('/')[0]
    balance = await exchange.fetch_balance()
    free_balance = 0
    if balance and 'free' in balance:
        free_balance = balance['free'].get(asset, 0)
    elif balance:
        free_balance = balance.get(asset, 0)

    # Stage 1: Collect profitable lots (or all if forced)
    sell_lot_indices = []
    total_sell_amount = 0
    total_entry_cost = 0
    for i, pos in enumerate(positions):
        is_profitable = engine.is_profitable(price, pos['entry_price'], fee_rate=fee_rate, entry_total_base=pos.get('entry_total_base', 0), amount=pos['amount'])

        # Respect auto_sell_disabled flag for automated sells
        auto_disabled = pos.get('trigger_data', {}).get('auto_sell_disabled', False)
        if (force or is_profitable) and (force or not auto_disabled):
            sell_lot_indices.append(i)
            total_sell_amount += pos['amount']
            total_entry_cost += pos.get('entry_total_base', 0)

    if not sell_lot_indices:
        return

    # Stage 2: If under limit, try adding non-profitable lots to reach limit IF entire bundle remains profitable
    market = exchange.markets.get(symbol)
    min_amt = 0
    min_cost = 0
    if market and 'limits' in market:
        min_amt = market.get('limits', {}).get('amount', {}).get('min') or 0
        min_cost = market.get('limits', {}).get('cost', {}).get('min') or 0

    if not force and (total_sell_amount < min_amt or (total_sell_amount * price) < min_cost):
        other_indices = [i for i in range(len(positions)) if i not in sell_lot_indices]
        # Sort by performance (closest to break-even first)
        other_indices.sort(key=lambda idx: price / positions[idx]['entry_price'], reverse=True)

        for idx in other_indices:
            pos = positions[idx]
            # Skip if auto-sell is disabled for this lot
            if pos.get('trigger_data', {}).get('auto_sell_disabled', False):
                continue

            new_amount = total_sell_amount + pos['amount']
            new_entry_cost = total_entry_cost + pos.get('entry_total_base', 0)
            # estimated net proceeds for the whole bundle
            new_net_proceeds = new_amount * price * (1 - fee_rate)

            if new_net_proceeds > new_entry_cost:
                sell_lot_indices.append(idx)
                total_sell_amount = new_amount
                total_entry_cost = new_entry_cost
                if total_sell_amount >= min_amt and (total_sell_amount * price) >= min_cost:
                    break

    # Cap total sell amount to actual free balance
    if total_sell_amount > free_balance:
        total_sell_amount = free_balance

    # Final check against exchange limits
    if total_sell_amount < min_amt:
        logging.warning(f"[{symbol}] Bundle sell amount {total_sell_amount:.4f} still below minimum {min_amt}. Skipping.")
        return
    if (total_sell_amount * price) < min_cost:
        logging.warning(f"[{symbol}] Bundle sell cost {total_sell_amount * price:.4f} still below minimum notional {min_cost}. Skipping.")
        return

    try:
        # Create the order first (outside the lock) to avoid blocking other symbols/tasks
        order = await exchange.create_order(symbol, 'sell', total_sell_amount)

        if order and 'id' in order:
            async with pending_orders_lock:
                pending_orders[str(order['id'])] = {
                    'symbol': symbol,
                    'side': 'sell',
                    'sell_lot_indices': sell_lot_indices,
                    'timestamp': time.time(),
                    'trigger_data': {},
                    'strategy': strategy or ("Manual" if force else "Unknown"),
                    'candle_count': candle_count
                }

        # If order is already closed, process it immediately
        # We call this outside the lock to avoid re-entrancy issues with process_order_fill
        if order and order.get('status') == 'closed':
            await process_order_fill(order, exchange, data_manager, config, engine)
    except Exception as e:
        logging.error(f"Aggregated sell failed for {symbol}: {e}")
        async with bot_lock:
            bot_state[symbol]['position'] = data_manager.get_position(symbol)

async def process_order_fill(order, exchange, data_manager, config, engine):
    """
    Centralized handler for completed orders to avoid race conditions and lost confirmations.
    """
    terminal_statuses = ['closed', 'canceled', 'expired', 'rejected']
    status = order.get('status')
    if status not in terminal_statuses:
        return False

    order_id = str(order['id'])
    async with processed_orders_lock:
        if order_id in processed_orders:
            # Ensure it's removed from pending even if already processed
            async with pending_orders_lock:
                if order_id in pending_orders:
                    pending_orders.pop(order_id)
            return False
        processed_orders.append(order_id)

    meta = None
    # Short retry loop to wait for metadata if it was placed by the bot but not yet registered
    # This addresses the race condition where WebSocket update arrives before create_order returns its ID
    for _ in range(5):
        async with pending_orders_lock:
            if order_id in pending_orders:
                meta = pending_orders.pop(order_id)
                break
        await asyncio.sleep(0.1)

    # Use data from order object, or meta if order is missing info
    symbol = order.get('symbol') or (meta['symbol'] if meta else None)

    if status != 'closed' and order.get('filled', 0) == 0:
        if meta:
            logging.info(f"[{symbol}] Order {order_id} was {status} with no fill. Removing from pending.")
        return False

    if status != 'closed':
        logging.info(f"[{symbol}] Order {order_id} was {status} but had partial fill ({order.get('filled')}). Processing fill.")
    side = order.get('side') or (meta['side'] if meta else None)
    if not symbol or not side:
        return False

    filled_amount = order.get('filled', 0.0)
    actual_price = order.get('price') or order.get('average', 0.0)
    cost = order.get('cost') or (filled_amount * actual_price)

    fee_cost = order.get('fee', {}).get('cost', 0.0)
    fee_currency = order.get('fee', {}).get('currency')
    total_fee = await exchange.get_fee_in_quote(symbol, fee_cost, fee_currency)

    trigger_data = meta['trigger_data'] if meta else {}
    timestamp = meta['timestamp'] if meta else time.time()
    strategy = meta.get('strategy', 'Unknown') if meta else 'Unknown'
    candle_count = meta.get('candle_count', 0) if meta else 0

    if side == 'buy':
        # Verify amount on exchange to account for fees deducted from the acquired asset
        verified_order = await exchange.fetch_order(order_id, symbol)
        if verified_order and verified_order.get('status') == 'closed':
            filled_amount = verified_order.get('filled', filled_amount)
            actual_price = verified_order.get('price') or verified_order.get('average', actual_price)
            cost = verified_order.get('cost') or (filled_amount * actual_price)
            fee_data = verified_order.get('fee')
            if fee_data:
                fee_cost = fee_data.get('cost', 0.0)
                fee_currency = fee_data.get('currency')

                # If fee was deducted from the acquired asset (base currency), update filled_amount
                base_asset = symbol.split('/')[0]
                if fee_currency == base_asset:
                    filled_amount -= fee_cost

                total_fee = await exchange.get_fee_in_quote(symbol, fee_cost, fee_currency)

        total_val = cost + total_fee
        # Ensure trigger_data includes strategy and candle_count for persistence
        final_strategy = meta.get('strategy', 'Unknown') if meta else 'Unknown'
        final_candle_count = meta.get('candle_count', 0) if meta else 0

        if 'strategy' not in trigger_data:
            trigger_data['strategy'] = final_strategy
        if 'candle_count' not in trigger_data:
            trigger_data['candle_count'] = final_candle_count

        data_manager.add_position(symbol, actual_price, filled_amount, total_fee, trigger_data, timestamp, total_base=total_val)

        _, quote = symbol.split('/')
        logging.info(f"[{symbol}] BUY executed at {format_price(actual_price, config=config)} (Filled: {format_amt(filled_amount, config=config)}, Spent: {format_price(total_val, config=config)} {quote}, Technique: {final_strategy}, Candles: {final_candle_count})")
        play_sound("buy", config)

    elif side == 'sell':
        total_net_received = cost - total_fee
        sell_lot_indices = meta.get('sell_lot_indices') if meta else []
        
        async with bot_lock:
            positions = data_manager.get_position(symbol)
            if positions:
                if not sell_lot_indices:
                    # If external sell or missing indices, close from the oldest lots
                    sell_lot_indices = list(range(len(positions)))
                
                sell_lot_indices.sort(reverse=True)

                remaining_filled = filled_amount
                remaining_net_received = total_net_received
                remaining_fee = total_fee
                total_entry_cost_of_filled = 0.0

                for idx, i in enumerate(sell_lot_indices):
                    if remaining_filled <= config.get('exchange', {}).get('dust_threshold_cost', 1e-10): break
                    if i >= len(positions): continue

                    pos = positions[i]
                    lot_close_amt = min(pos['amount'], remaining_filled)
                    if lot_close_amt <= 0: continue

                    if idx == len(sell_lot_indices) - 1 or lot_close_amt >= remaining_filled - 1e-10:
                        current_lot_received = remaining_net_received
                        current_lot_fee = remaining_fee
                        lot_close_amt = remaining_filled
                    else:
                        proportion = lot_close_amt / filled_amount
                        current_lot_received = total_net_received * proportion
                        current_lot_fee = total_fee * proportion

                    entry_cost_proportion = (lot_close_amt / pos['amount']) if pos['amount'] > 0 else 1.0
                    entry_cost_part = pos.get('entry_total_base', 0.0) * entry_cost_proportion
                    total_entry_cost_of_filled += entry_cost_part
                    lot_profit = current_lot_received - entry_cost_part

                    data_manager.close_position(
                        symbol, actual_price, current_lot_fee, lot_profit, trigger_data, time.time(),
                        total_base=current_lot_received, lot_index=i, amount=lot_close_amt
                    )

                    remaining_filled -= lot_close_amt
                    remaining_net_received -= current_lot_received
                    remaining_fee -= current_lot_fee

                actual_total_profit = total_net_received - total_entry_cost_of_filled
                _, quote = symbol.split('/')
                lot_prefix = f"Lot {len(sell_lot_indices)} SOLD" if sell_lot_indices else "SELL executed"
                final_strategy = meta.get('strategy', 'Unknown') if meta else 'Unknown'
                final_candle_count = meta.get('candle_count', 0) if meta else 0
                logging.info(f"[{symbol}] {lot_prefix} at {format_price(actual_price, config=config)} (Filled: {format_amt(filled_amount, config=config)}, Profit: {format_price(actual_total_profit, config=config)}, Received: {format_price(total_net_received, config=config)} {quote}, Technique: {final_strategy}, Candles: {final_candle_count})")
                play_sound("sell", config)

        # Fetch fresh balance to ensure dust check and future buys are accurate
        try:
            fresh_balance = await exchange.fetch_balance()
            if fresh_balance:
                async with bot_lock:
                    global current_balances
                    current_balances = fresh_balance
        except Exception as e:
            logging.warning(f"Failed to update balance after sell for {symbol}: {e}")

        # Post-sale dust cleanup
        if await is_pair_dust(symbol, exchange, config):
            data_manager.clear_positions(symbol)
            logging.info(f"[{symbol}] Remaining balance is dust. Clearing open positions.")

        # Trigger re-analysis for budget-suspended pairs to resume them quickly
        asyncio.create_task(resume_suspended_pairs(exchange, config, data_manager, engine))

    async with bot_lock:
        if symbol not in bot_state: bot_state[symbol] = {}
        bot_state[symbol]['position'] = data_manager.get_position(symbol)
    
    return True

async def resume_suspended_pairs(exchange, config, data_manager, engine):
    """
    Attempts to resume trading for pairs that were suspended due to budget constraints.
    """
    suspended_symbols = [s for s, susp in pair_suspensions.items() if susp.get('reason') == 'budget']
    if not suspended_symbols:
        return

    # Give the exchange a moment to settle
    await asyncio.sleep(0.5)

    for symbol in suspended_symbols:
        # Triggering a re-analysis will automatically check the budget again in analyze_and_trade
        async with analysis_lock:
            if symbol not in analysis_in_progress:
                analysis_in_progress.add(symbol)
                # Note: This might use None for pattern_manager, device, executor if not globally available,
                # but analyze_and_trade handles it. We'll use the ones from main() if possible or let the next update handle it.
                # Actually, it's safer to just let the next candle update do it, or we need to pass these refs.
                # Let's try to find a way to trigger analyze_and_trade with current globals.
                pass

async def watch_balance_task(exchange, data_manager):
    global current_balances
    logging.info("WebSocket: watch_balance task started.")
    while not shutdown_event.is_set():
        try:
            async for balance in exchange.watch_balance():
                async with bot_lock:
                    current_balances = balance
                logging.debug("Balance updated via WebSocket")
        except Exception as e:
            if not shutdown_event.is_set():
                err_msg = str(e).lower()
                logging.error(f"WebSocket balance error: {e}")
                if "ping-pong" in err_msg or "timeout" in err_msg:
                    try: await exchange.close()
                    except: pass
                await asyncio.sleep(5)
            else: break

async def watch_orders_task(exchange, data_manager, config, engine):
    logging.info("WebSocket: watch_orders task started.")
    while not shutdown_event.is_set():
        try:
            async for orders in exchange.watch_orders():
                for order in orders:
                    await process_order_fill(order, exchange, data_manager, config, engine)
        except Exception as e:
            if not shutdown_event.is_set():
                err_msg = str(e).lower()
                logging.error(f"WebSocket orders error: {e}")
                if "ping-pong" in err_msg or "timeout" in err_msg:
                    try: await exchange.close()
                    except: pass
                await asyncio.sleep(5)
            else: break

async def is_pair_dust(symbol, exchange, config):
    """
    Checks if the remaining balance for a symbol is considered 'dust'
    based on exchange minimums.
    """
    asset = symbol.split('/')[0]

    async with bot_lock:
        bal_data = current_balances
        price = bot_state.get(symbol, {}).get('price', 0)

    amount = 0
    if bal_data and isinstance(bal_data, dict):
        if 'free' in bal_data:
            free_data = bal_data['free']
            amount = free_data.get(asset, 0) if isinstance(free_data, dict) else 0
        else:
            amount = bal_data.get(asset, 0)
            if isinstance(amount, dict):
                amount = amount.get('free', 0)

    market = exchange.markets.get(symbol)
    if not market: 
        return amount < 1e-6 # Fallback for unknown markets

    limits = market.get('limits', {})
    min_amt = limits.get('amount', {}).get('min') or config.get('exchange', {}).get('min_amount_fallback', 1e-8)
    min_cost = limits.get('cost', {}).get('min') or config.get('exchange', {}).get('min_notional_fallback', 10.0)

    if amount < min_amt: return True
    if price > 0 and (amount * price) < min_cost: return True
    
    # Very small absolute amount check
    if amount < config.get('exchange', {}).get('dust_threshold_cost', 1e-10): return True

    return False

def get_sorted_symbols(config):
    all_pairs = sorted(
        [s for s in bot_state.keys() if not s.startswith("_")]
    )
    return all_pairs

def make_dashboard(config):
    now = datetime.now()
    now_ts = time.time()
    global status_scroll_index, pairs_scroll_offset, logs_scroll_offset
    global pairs_pause_until, logs_pause_until, last_marquee_update
    global selected_pair_index, show_chart, chart_symbol, marquee_enabled

    should_step = False
    if marquee_enabled and (now_ts - last_marquee_update >= 0.4):
        should_step = True
        last_marquee_update = now_ts

    all_pairs = get_sorted_symbols(config)

    # 1. Logs Panel
    log_height = 8
    log_content = Text()
    max_logs_offset = max(0, len(all_logs) - log_height)

    if max_logs_offset > 0 and should_step:
        if now_ts > logs_pause_until:
            if logs_scroll_offset > 0:
                logs_scroll_offset -= 1
                if logs_scroll_offset == 0:
                    logs_pause_until = now_ts + 1
            else:
                logs_scroll_offset = max_logs_offset
                logs_pause_until = now_ts + 1

    logs_scroll_offset = max(0, min(logs_scroll_offset, max_logs_offset))
    start = max(0, len(all_logs) - log_height - logs_scroll_offset)
    end = max(0, len(all_logs) - logs_scroll_offset)
    for log_entry in all_logs[start:end]:
        style = "bold italic bright_green" if log_entry['expiry'] > now else "dim green"
        try:
            msg = Text.from_markup(log_entry['msg'])
        except:
            msg = Text(log_entry['msg'])
        log_content.append(f"[{log_entry['timestamp']}] ")
        log_content.append(msg)
        log_content.append("\n", style=style)

    log_panel = Panel(
        log_content,
        title="[bold]Infos[/]",
        border_style="bold green" if focused_panel == "logs" else "blue"
    )

    # 2. Pairs Panel
    ui_cfg = config.get('ui', {})
    table = Table(expand=True, box=None, padding=(0, 1))
    if expert_mode:
        table.add_column("Pair", style="cyan", no_wrap=True)
        table.add_column("EMA F/S", style="green", no_wrap=True)
        table.add_column("MACD", style="blue", no_wrap=True)
        table.add_column("RSI", style="yellow", no_wrap=True)
        table.add_column("Vol/ADX", style="dim white", no_wrap=True)
        table.add_column("Scr", style="bold white", no_wrap=True)
        table.add_column("B.Prof", style="bold green", no_wrap=True)
        table.add_column("Aggr", style="white", no_wrap=True)
        table.add_column("Strategy", style="bold cyan", no_wrap=True, width=MAX_STRAT_LEN)
    else:
        table.add_column("Pair", style="cyan", no_wrap=True)
        table.add_column("Price", style="magenta", no_wrap=True)
        table.add_column("Amt", style="cyan", no_wrap=True)
        table.add_column("Entry", style="magenta", no_wrap=True)
        table.add_column("Fee", style="red", no_wrap=True)
        table.add_column("B.Prof", style="bold green", no_wrap=True)
        table.add_column("Tendency", style="bold white", no_wrap=True)
        table.add_column("Signal", style="bold", no_wrap=True)
        table.add_column("Aggr", style="white", no_wrap=True)
        table.add_column("Strategy", style="bold cyan", no_wrap=True, width=MAX_STRAT_LEN)

    h_offset = ui_cfg.get('panel_height_offset', 20)
    pairs_height = console.height - h_offset
    if pairs_height < 3:
        pairs_height = 3
    max_pairs_offset = max(0, len(all_pairs) - pairs_height)

    if max_pairs_offset > 0 and should_step:
        if now_ts > pairs_pause_until:
            if pairs_scroll_offset < max_pairs_offset:
                pairs_scroll_offset += 1
                if pairs_scroll_offset == max_pairs_offset:
                    pairs_pause_until = now_ts + 1
            else:
                pairs_scroll_offset = 0
                pairs_pause_until = now_ts + 1

    pairs_scroll_offset = max(0, min(pairs_scroll_offset, max_pairs_offset))
    visible_symbols = all_pairs[pairs_scroll_offset : pairs_scroll_offset + pairs_height]

    for i, symbol in enumerate(visible_symbols):
        abs_idx = pairs_scroll_offset + i
        is_selected = (abs_idx == selected_pair_index and focused_panel == "pairs")
        row_style = "bold reverse" if is_selected else ""

        data = bot_state[symbol]
        pos = data.get('position')
        buy_count = data.get('consecutive_buys', 0)
        sell_count = data.get('consecutive_sells', 0)

        current_signal = "Waiting"
        if buy_count > 0: current_signal = f"{buy_count} Buy"
        elif sell_count > 0: current_signal = f"{sell_count} Sell"

        sig_style = "bold green" if "Buy" in current_signal else "bold red" if "Sell" in current_signal else "white"

        amt_str = "-"
        entry_str = "-"
        fee_str = "-"
        if pos:
            _, quote = symbol.split('/')
            if isinstance(pos, list):
                total_amount = sum(p['amount'] for p in pos)
                total_cost = sum(p['entry_price'] * p['amount'] for p in pos)
                avg_entry_price = total_cost / total_amount if total_amount > 0 else 0
                total_fee = sum(p.get('entry_fee', 0) for p in pos)
                amt_str = f"{format_amt(total_amount, config=config)} ({len(pos)})"
                entry_str = format_price(avg_entry_price, config=config)
                fee_str = f"{format_price(total_fee, config=config)} {quote}"
            else:
                amt_str = format_amt(pos['amount'], config=config)
                entry_str = format_price(pos['entry_price'], config=config)
                fee_str = f"{format_price(pos.get('entry_fee', 0), config=config)} {quote}"

        macd_hist = data.get('macd_hist', 0)
        macd_threshold = ui_cfg.get('macd_display_threshold', 0.001)
        macd_str = f"{macd_hist:.4e}" if abs(macd_hist) < macd_threshold else f"{macd_hist:.4f}"

        display_strat = data.get('strategy') or config.get('pairs', {}).get(symbol, {}).get('strategy', 'N/A')

        if expert_mode:
            row_vals = [
                symbol,
                f"{format_price(data.get('ema_f', 0), config=config)}/{format_price(data.get('ema_s', 0), config=config)}",
                macd_str,
                f"{data.get('rsi', 0):.2f}",
                f"{data.get('volatility', 0):.6f}/{data.get('adx', 0):.2f}",
                str(data.get('score', 0)),
                f"{data.get('expected_profit', 0):.4f}",
                data.get('aggr', 'N/A'),
                str(display_strat)
            ]
        else:
            tendency = data.get('tendency', 'Neutral')
            t_style = "green" if tendency == "Bullish" else "bold red" if tendency == "Bearish" else "white"
            row_vals = [
                symbol,
                format_price(data.get('price'), config=config),
                amt_str, entry_str, fee_str,
                f"{data.get('expected_profit', 0):.4f}",
                f"[{t_style}]{tendency}[/]",
                f"[{sig_style}]{current_signal}[/]",
                data.get('aggr', 'N/A'),
                f"[bold cyan]{display_strat}[/]"
            ]

        table.add_row(*row_vals, style=row_style)

    pairs_panel = Panel(
        table,
        title="[bold]Trading Pairs[/]",
        border_style="bold green" if focused_panel == "pairs" else "cyan"
    )

    # 3. Status Bar
    status_text = Text()
    status_text.append(f"Update: {now.strftime('%H:%M:%S')} | ", style="bold brown")
    status_text.append("TAB: Switch | Arrows: Scroll | H: Help | X: Expert | M: Marquee | B/S: Buy/Sell (Candles) | Exit: Ctrl+C", style="bold red")

    display_width = console.width - 4
    max_status_offset = max(0, len(status_text) - display_width)

    if max_status_offset > 0 and should_step:
        status_scroll_index = (status_scroll_index + 1) % (max_status_offset + 10)
        if status_scroll_index > max_status_offset:
            status_display = status_text[0 : display_width]
        else:
            status_display = status_text[status_scroll_index : status_scroll_index + display_width]
    else:
        status_display = status_text
        status_display.justify = "center"

    if show_help:
        help_text = Text()
        help_text.append("\n[bold cyan]Keyboard Shortcuts:[/]\n", style="white")
        help_text.append("  TAB    : Switch focus between Logs and Pairs\n")
        help_text.append("  UP/DN  : Move selection / Scroll the focused panel\n")
        help_text.append("  ENTER  : Show/Hide K-Lines for selected symbol\n")
        help_text.append("  B      : Manual BUY (only in Candle View)\n")
        help_text.append("  S      : Manual SELL (only in Candle View)\n")
        help_text.append("  X      : Toggle Expert Mode\n")
        help_text.append("  M      : Toggle Marquee Effect\n")
        help_text.append("  H      : Close this help menu\n")
        help_text.append("  Ctrl+C : Stop the bot gracefully\n")
        pairs_panel = Panel(help_text, title="[bold]Help / Info[/]", border_style="bold yellow")

    if show_chart and chart_symbol:
        chart_content = render_ascii_chart(chart_symbol, config)
        pairs_panel = Panel(chart_content, title=f"[bold]K-Lines: {chart_symbol}[/]", border_style="bold magenta")

    if not startup_complete:
        waiting_text = Text.from_markup("\n\n\n\n\n[bold blink yellow]Waiting for system initialization...[/]\n", justify="center")
        waiting_text.append_text(Text.from_markup("[dim]Fetching market data and calculating first signals...[/]\n", style="white"))
        pairs_panel = Panel(waiting_text, title="[bold]System Startup[/]", border_style="bold yellow")

    layout = Layout()
    layout.split(
        Layout(Panel(Text("🛸 CCXT Pro Trading Bot v2 (Async)", style="bold magenta", justify="center"), border_style="blue"), size=3),
        Layout(log_panel, size=log_height+2),
        Layout(pairs_panel, name="main"),
        Layout(Panel(status_display, title="Status", border_style="cyan"), size=3)
    )
    return layout

async def run_dashboard(config):
    try:
        refresh_rate = config.get('ui', {}).get('refresh_rate', 4)
        # Start Live immediately but without screen=True to allow startup logs to be visible
        # or use a simplified layout during startup.
        with Live(make_dashboard(config), refresh_per_second=refresh_rate, screen=False) as live:
            while not startup_complete and not shutdown_event.is_set():
                live.update(make_dashboard(config))
                await asyncio.sleep(0.5)

            # Switch to screen mode once startup is complete
            # We have to close the old live and start a new one to change screen=True
            pass

        if shutdown_event.is_set(): return

        with Live(make_dashboard(config), refresh_per_second=refresh_rate, screen=True) as live:
            while not shutdown_event.is_set():
                live.update(make_dashboard(config))
                await asyncio.sleep(1.0 / refresh_rate)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.info(f"[red]Dashboard error: {e}")

async def heartbeat_task(exchange, data_manager, engine, config):
    while not shutdown_event.is_set():
        # Cleanup stuck pending orders
        stuck_timeout = config.get('timeouts', {}).get('stuck_order_cleanup', 300)

        async with pending_orders_lock:
            now = time.time()
            stuck_candidates = [(oid, meta) for oid, meta in pending_orders.items() if now - meta.get('timestamp', 0) > stuck_timeout]

        for oid, meta in stuck_candidates:
            symbol = meta.get('symbol')
            try:
                # Double check status with the exchange before giving up
                logging.info(f"[{symbol}] Checking status of potentially stuck order {oid}...")
                verified_order = await exchange.fetch_order(oid, symbol)
                if verified_order:
                    is_processed = await process_order_fill(verified_order, exchange, data_manager, config, engine)
                    if is_processed:
                        logging.info(f"[{symbol}] Stuck order {oid} was found to be {verified_order.get('status')} and has been processed.")
                        continue
                    elif verified_order.get('status') in ['canceled', 'expired', 'rejected']:
                        # process_order_fill already removed it if it saw the terminal status
                        continue

                # If still open or not found, remove it manually to unblock the pair
                async with pending_orders_lock:
                    if oid in pending_orders:
                        pending_orders.pop(oid)
                logging.warning(f"[{symbol}] Removing stuck pending {meta.get('side')} order {oid} after timeout (Order status on exchange: {verified_order.get('status') if verified_order else 'unknown'}).")
            except Exception as e:
                logging.error(f"[{symbol}] Error during stuck order cleanup for {oid}: {e}")
                async with pending_orders_lock:
                    if oid in pending_orders:
                        pending_orders.pop(oid)

        await asyncio.sleep(config.get('timeouts', {}).get('heartbeat_interval', 30))

async def main():
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(global_exception_handler)

    parser = argparse.ArgumentParser(description='CCXT Pro Trading Bot v2 (Asynchronous)')
    parser.add_argument('--no-gpu', action='store_true', help='Disable GPU acceleration (force CPU)')
    parser.add_argument('--fast-start', action='store_true', help='Skip fetching initial candles')

    args = parser.parse_args()

    # Hardware Acceleration Detection
    global device
    if args.no_gpu:
        device = torch.device('cpu')
    else:
        if torch.cuda.is_available():
            device = torch.device('cuda')
        elif torch.backends.mkldnn.is_available():
            device = torch.device('cpu')
            torch.backends.mkldnn.enabled = True
        elif hasattr(torch, 'vulkan') and torch.vulkan.is_available():
            device = torch.device('vulkan')
        elif torch.cuda.is_available() and hasattr(torch.version, 'hip') and torch.version.hip:
            device = torch.device('cuda')
        elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            device = torch.device('mps')
        else:
            try:
                import intel_extension_for_pytorch as ipex
                if torch.xpu.is_available():
                    device = torch.device('xpu')
                else: raise Exception()
            except:
                device = torch.device('cpu')

    config = load_config()

    global mc_engine, processed_orders, MAX_STRAT_LEN
    mc_engine = MonteCarloEngine(config=config)
    mc_engine.set_device(device)
    processed_orders = deque(maxlen=config.get('ui', {}).get('log_limit', 1000))
    MAX_STRAT_LEN = max(len(s) for s in STRATEGIES) if STRATEGIES else 20

    api_creds = {}
    if os.path.exists('api.json'):
        with open('api.json', 'r') as f: api_creds = json.load(f)

    exchange_id = api_creds.get('exchange_id')
    if not exchange_id:
        logging.error("No exchange found. Check your api.json file.")
        return
    
    exchange = CCXTExchange2(exchange_id,
                             api_creds.get('api_key') or config.get('api_key'),
                             api_creds.get('api_secret') or config.get('api_secret'),
                             config=config)
        
    logging.info(f"Connecting to {exchange_id}...")
    try:
        await exchange.load_markets()
    except Exception as e:
        logging.error(f"Failed to load markets: {e}")
        await exchange.close()
        return

    data_manager = DataManager()
    pattern_manager = PatternManager()
    engine = TradingEngine(config)

    if 'pairs' not in config:
        config['pairs'] = {}

    # Discover and select pairs based on volume, configuration, and balances
    logging.info("Retrieving best pairs based on 24h volume and balance analysis...")
    try:
        # Fetch initial balances and tickers early to reuse
        logging.info("Retrieving initial balances and tickers...")
        initial_balance = await asyncio.wait_for(exchange.fetch_balance(), timeout=30)
        async with bot_lock:
            global current_balances
            current_balances = initial_balance

        # 1. Fetch 24h tickers to find high volume pairs
        all_tickers = await exchange.fetch_tickers()

        quote_asset = config.get('quote_asset')
        num_pairs = config.get('number_of_pairs')

        # Candidate symbols: Must end with quote_asset and have volume/price data
        candidates = []
        for symbol, ticker in all_tickers.items():
            if not symbol.endswith(f"/{quote_asset}"):
                continue

            volume = ticker.get('quoteVolume') or (ticker.get('baseVolume') or 0) * (ticker.get('last') or 0)
            if volume > 0:
                candidates.append({
                    'symbol': symbol,
                    'volume': volume,
                    'base': symbol.split('/')[0]
                })

        # Sort by volume descending
        candidates.sort(key=lambda x: x['volume'], reverse=True)
        top_volume_pairs = [c['symbol'] for c in candidates[:num_pairs]]

        # 2. Add pairs from inventory if enabled
        inventory_pairs = []
        if config.get('include_inventory_pairs') or config.get('include_all_quote_pairs'):
            balance = initial_balance
            free_balances = balance.get('free', {}) if isinstance(balance, dict) and 'free' in balance else {}

            for asset, amount in free_balances.items():
                if amount <= 0: continue

                # Check for Base Asset matches
                if config.get('include_inventory_pairs'):
                    symbol = f"{asset}/{quote_asset}"
                    if symbol in all_tickers and symbol not in top_volume_pairs:
                        inventory_pairs.append(symbol)

                # Check for any pair where user has the Quote Asset
                if config.get('include_all_quote_pairs'):
                    # This logic adds all symbols for which the user has the quote currency in balance.
                    # We limit this to top volume pairs with that quote to avoid adding thousands.
                    for s in all_tickers.keys():
                        if s.endswith(f"/{asset}") and s not in top_volume_pairs and s not in inventory_pairs:
                            # Only add top 5 volume for this specific quote if it's not the main one
                            inventory_pairs.append(s)

        final_pairs = list(set(top_volume_pairs + inventory_pairs + list(config['pairs'].keys())))

        # Filter pairs that exist in markets
        final_pairs = [p for p in final_pairs if p in exchange.markets]

        for p in final_pairs:
            if p not in config['pairs']:
                config['pairs'][p] = {}

        pairs = list(config['pairs'].keys())
        logging.info(f"Initialized with {len(pairs)} pairs (Top Volume: {len(top_volume_pairs)}, Inventory: {len(inventory_pairs)}).")

    except Exception as e:
        logging.error(f"Failed to dynamically retrieve pairs: {e}")
        pairs = list(config['pairs'].keys())

    if not pairs:
        logging.error("No pairs could be initialized.")
        await exchange.close()
        return

    # Start UI task
    global ui_task, background_tasks, startup_complete
    ui_task = asyncio.create_task(run_dashboard(config))

    logging.info("[bold cyan]System initialization started...")

    # Initial Batch
    for symbol in pairs:
        pair_cfg = config['pairs'][symbol]
        strat = pair_cfg.get('strategy') or random.choice(STRATEGIES)
        aggr = pair_cfg.get('aggr') or random.choice(['normal', 'aggressive', 'dynamic'])

        bot_state[symbol] = {
            'price': 0, 'rsi': 0, 'tendency': 'Neutral',
            'last_signal': 'Init',
            'position': data_manager.get_position(symbol),
            'aggr': aggr,
            'strategy': strat,
            'consecutive_buys': 0,
            'consecutive_sells': 0,
            'score': 0,
            'ema_f': 0,
            'ema_s': 0,
            'adx': 0,
            'volatility': 0,
            'expected_profit': 0,
            'last_analysis_ts': 0
        }

    if args.fast_start:
        logging.info("[bold yellow]Fast start enabled: Skipping initial candles fetch.")
        for symbol in pairs:
            ohlcv_cache[symbol] = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')
    else:
        logging.info(f"[bold cyan]Fetching initial candles for {len(pairs)} pairs...")
        semaphore = asyncio.Semaphore(5)

        async def init_symbol(symbol):
            async with semaphore:
                try:
                    logging.info(f"Fetching initial 1s candles for {symbol} (Target: 10000)...")
                    ohlcv_1s = await exchange.fetch_ohlcv_10k(symbol, '1s', limit=10000)
                    df_1s = pd.DataFrame(ohlcv_1s, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df_1s['timestamp'] = pd.to_datetime(df_1s['timestamp'], unit='ms')
                    for col in ['open', 'high', 'low', 'close', 'volume']:
                        df_1s[col] = pd.to_numeric(df_1s[col], errors='coerce')
                    df_1s.set_index('timestamp', inplace=True)
                    ohlcv_cache[symbol] = df_1s
                    logging.info(f"[{symbol}] Loaded {len(df_1s)} candles (1s).")

                except Exception as e:
                    logging.error(f"Failed to load candles for {symbol}: {e}")
                    # Fallback empty dataframes to avoid crashes
                    empty_df = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')
                    ohlcv_cache[symbol] = empty_df

        await asyncio.gather(*[init_symbol(s) for s in pairs])

    # Exclude pairs that have no candles after initial download
    async with ohlcv_lock:
        to_remove = [s for s in pairs if s not in ohlcv_cache or ohlcv_cache[s].empty]
        for s in to_remove:
            logging.warning(f"[{s}] No initial candle data available. Excluding from active monitoring.")
            if s in bot_state:
                del bot_state[s]
            pairs.remove(s)

    # Analysis Executor
    max_workers = max(1, (os.cpu_count() or 4) - 2)
    executor = concurrent.futures.ProcessPoolExecutor(
        max_workers=max_workers,
        initializer=worker_process_init
    )

    # Start WebSocket Tasks
    logging.info("[bold green]Starting WebSocket tasks...")
    background_tasks = [
        asyncio.create_task(watch_balance_task(exchange, data_manager)),
        asyncio.create_task(watch_orders_task(exchange, data_manager, config, engine)),
        asyncio.create_task(input_task(exchange, config, data_manager, engine)),
        asyncio.create_task(heartbeat_task(exchange, data_manager, engine, config)),
        asyncio.create_task(chart_renderer_task(config))
    ]

    # Start Global OHLCV Watcher
    watch_pairs = [[s, '1s'] for s in pairs]
    ohlcv_task = asyncio.create_task(watch_ohlcv_global_task(exchange, watch_pairs, config, data_manager, pattern_manager, engine, device, executor))

    # Ensure all watchers are setup (Wait a bit for connections to stabilize)
    await asyncio.sleep(2)


    # Synchronizing positions from the exchange API
    logging.info(f"Synchronizing positions from the {exchange_id.capitalize()} API...")
    try:
        await asyncio.wait_for(sync_live_positions(exchange, data_manager, config), timeout=120)
    except asyncio.TimeoutError:
        logging.error("Balance synchronization timed out. Proceeding with partial data.")
    except Exception as e:
        logging.error(f"Error during balance synchronization: {e}")

    # Initial analysis for all pairs
    for symbol in pairs:
        async with analysis_lock:
            if symbol not in analysis_in_progress:
                analysis_in_progress.add(symbol)
                asyncio.create_task(analyze_and_trade_wrapper(exchange, symbol, config, data_manager, pattern_manager, engine, device, executor))

    # Wait a tad bit before dropping the message startup complete since the previous task can be taking the lead sometime
    await asyncio.sleep(config.get('timeouts', {}).get('startup_wait', 4))
    startup_complete = True
    play_sound("startup", config)
    logging.info("[bold green]Bot v2 fully operational.")

    # Trigger initial chart and display update
    # In rich Live, the dashboard is already refreshing at 4Hz.
    # We just need to make sure the data is there.

    # Signal handling for graceful shutdown
    loop = asyncio.get_running_loop()
    if platform.system().lower() != 'windows':
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, handle_stop_signal)
            except NotImplementedError:
                pass
    else:
        signal.signal(signal.SIGINT, handle_stop_signal)

    try:
        await shutdown_event.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        handle_stop_signal()
    except Exception as e:
        logging.error(f"Main loop error: {e}")
    finally:
        shutdown_event.set()
        logging.info("Shutting down... cancelling tasks.")

        # Consolidate all tasks for cleanup
        all_tasks = background_tasks.copy()
        if ui_task:
            all_tasks.append(ui_task)
        if ohlcv_task:
            all_tasks.append(ohlcv_task)

        # Cancel all pending tasks
        for t in all_tasks:
            if not t.done():
                t.cancel()

        # Wait for all tasks to acknowledge cancellation with a timeout
        if all_tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*all_tasks, return_exceptions=True), timeout=5)
            except asyncio.TimeoutError:
                logging.warning("Shutdown cleanup timed out.")
            except Exception as e:
                logging.error(f"Error during shutdown cleanup: {e}")

        if executor: executor.shutdown(wait=False)
        global bench_executor
        if bench_executor: bench_executor.shutdown(wait=False)

        try:
            await exchange.close()
        except: pass

        # Clear screen and show final logs
        console.clear()
        console.print("[bold red]Bot v2 shutdown sequence complete.[/]")
        console.print("[bold white]Final Log Summary:[/]")
        for log in all_logs[-20:]:
            console.print(f"[{log['timestamp']}] {log['msg']}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as e:
        # Final emergency log
        with open("fatal_error.log", "a") as f:
            f.write(f"{datetime.now()} - FATAL ERROR: {str(e)}\n")
            import traceback
            f.write(traceback.format_exc())
        console.print_exception()

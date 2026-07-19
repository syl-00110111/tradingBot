# CCXT Pro Manual Trading Interface (Asynchronous)
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

# Import functions from symbols_utils as requested
from symbols_utils import computeSymbols, updateTradingCount

# Global Monte Carlo Engine
mc_engine = None # Initialized in main after config load

# Global analysis tracking to avoid overlapping
analysis_in_progress = set()
analysis_lock = asyncio.Lock()

# Global Watcher Tasks
ohlcv_tasks = []

# Global Active Pairs Management
active_pairs = []
discovery_pool = []
pair_last_trade_time = {}
pair_hour_trades = {} # symbol -> deque of timestamps

# Global Buy Queue for manual processing tracking if any
buy_queue = []
buy_queue_lock = asyncio.Lock()

# Track orders placed manually
pending_orders = {} # order_id -> metadata_dict
pending_orders_lock = asyncio.Lock()
processed_orders = None # Initialized in main after config load
processed_orders_lock = asyncio.Lock()
# Prevent consecutive sells on the same symbol until confirmation
pending_sells = set()
pending_sells_lock = asyncio.Lock()

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
symbol_timeframes = {}
default_candle_timeframe = '1s'
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
                    for _ in range(max(random.randint(int(cfg.get('beeps', 5)), int(cfg.get('beeps', 5))*2))):
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

        if "Manual trader fully operational." in msg or "[bold green]Manual trader fully operational." in msg:
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


def safe_float(v, default=0.0):
    """Convert value to float safely, tolerate strings with commas or None."""
    if v is None:
        return float(default)
    if isinstance(v, (int, float)):
        try:
            return float(v)
        except Exception:
            return float(default)


def sanitize_order_dict(order):
    """Attempt to coerce common order fields to expected types in-place."""
    if not isinstance(order, dict):
        return order
    # numeric fields
    for k in ('filled', 'price', 'average', 'cost'):
        if k in order:
            try:
                order[k] = safe_float(order[k], 0.0)
            except Exception:
                order[k] = 0.0
    # timestamp
    if 'timestamp' in order:
        try:
            order['timestamp'] = safe_float(order['timestamp'], time.time())
        except Exception:
            order['timestamp'] = time.time()
    # fee
    try:
        fee = order.get('fee')
        if fee and isinstance(fee, dict):
            fee['cost'] = safe_float(fee.get('cost', 0.0), 0.0)
            order['fee'] = fee
    except Exception:
        pass
    return order

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
    with plotext_lock:
        try:
            # Check if dataframe has data
            if df.empty or len(df) == 0:
                return Text("No chart data available", style="yellow")
            
            plt_ascii.clear_figure()
            plt_ascii.clf()
            plt_ascii.theme('dark')
            plt_ascii.subplots(2, 1)

            plt_ascii.subplot(1, 1)
            plt_ascii.clf()
            plt_ascii.theme('dark')

            # Use simple OHLC data without any date/time indices
            # Reset index and extract numeric values
            try:
                # Reset index to avoid any string index issues
                df_reset = df.reset_index(drop=True)
                
                # Create clean numeric series
                opens = pd.to_numeric(df_reset['open'], errors='coerce').fillna(0).astype(float)
                highs = pd.to_numeric(df_reset['high'], errors='coerce').fillna(0).astype(float)
                lows = pd.to_numeric(df_reset['low'], errors='coerce').fillna(0).astype(float)
                closes = pd.to_numeric(df_reset['close'], errors='coerce').fillna(0).astype(float)
                
                # Create a clean DataFrame for candlestick
                df_ohlc = pd.DataFrame({
                    'Open': opens,
                    'High': highs,
                    'Low': lows,
                    'Close': closes
                })
                
                # Use numeric indices to avoid date parsing issues
                indices = list(range(len(df_ohlc)))
                plt_ascii.candlestick(indices, df_ohlc)
            except Exception as e:
                logging.error(f"Failed to prepare OHLC data for {symbol}: {e}")
                raise

            plt_ascii.subplot(2, 1)
            plt_ascii.clf()
            plt_ascii.theme('dark')
            volumes = pd.to_numeric(df_reset['volume'], errors='coerce').fillna(0).astype(float).tolist()
            plt_ascii.bar(indices, volumes, color='blue', label='Volume')
            plt_ascii.title("Volume")

            h_volume = max(5, height // 3)
            h_klines = height - h_volume

            plt_ascii.subplot(1, 1).plotsize(width, h_klines)
            plt_ascii.subplot(2, 1).plotsize(width, h_volume)
            result = plt_ascii.build()
            return Text.from_ansi(result)
        except Exception as e:
            import traceback
            logging.error(f"Chart rendering error: {e}\n{traceback.format_exc()}")
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
            # Ensure numeric UI config values are converted safely to integers
            try:
                h_offset = int(ui_cfg.get('panel_height_offset', 20) or 20)
            except (ValueError, TypeError):
                h_offset = 20
            try:
                console_height = int(console.height)
            except (ValueError, TypeError):
                console_height = 24
            pairs_height = console_height - h_offset
            if pairs_height < 3:
                pairs_height = 3

            if show_chart or show_help:
                if key in [readchar.key.ENTER, readchar.key.ESC, 'q', 'Q', 'h', 'H']:
                    show_chart = False
                    show_help = False
                elif show_chart and chart_symbol and key.lower() == 'b':
                    price = bot_state.get(chart_symbol, {}).get('price', 0)
                    try:
                        price_f = float(price)
                    except (ValueError, TypeError):
                        price_f = 0
                    if price_f > 0:
                        logging.info(f"[Manual] Triggering BUY for {chart_symbol}")
                        candle_count = len(ohlcv_cache.get(chart_symbol, []))
                        asyncio.create_task(execute_buy(exchange, chart_symbol, {'close': price_f}, data_manager, engine, config, manual=True, strategy="Manual", candle_count=candle_count))
                elif show_chart and chart_symbol and key.lower() == 's':
                    price = bot_state.get(chart_symbol, {}).get('price', 0)
                    try:
                        price_f = float(price)
                    except (ValueError, TypeError):
                        price_f = 0
                    if price_f > 0:
                        logging.info(f"[Manual] Triggering SELL for {chart_symbol}")
                        candle_count = len(ohlcv_cache.get(chart_symbol, []))
                        asyncio.create_task(execute_sell(exchange, chart_symbol, {'close': price_f}, data_manager, engine, config, force=True, strategy="Manual", candle_count=candle_count))
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
                try:
                    scroll_step = int(ui_cfg.get('scroll_step', 10) or 10)
                except (ValueError, TypeError):
                    scroll_step = 10
                if focused_panel == "pairs":
                    selected_pair_index = max(0, selected_pair_index - pairs_height)
                    pairs_scroll_offset = max(0, pairs_scroll_offset - pairs_height)
                    pairs_pause_until = time.time() + config.get('timeouts', {}).get('ui_pause_short', 5)
                elif focused_panel == "logs":
                    logs_scroll_offset += scroll_step
                    logs_pause_until = time.time() + config.get('timeouts', {}).get('ui_pause_long', 30)
            elif key == readchar.key.PAGE_DOWN:
                try:
                    scroll_step = int(ui_cfg.get('scroll_step', 10) or 10)
                except (ValueError, TypeError):
                    scroll_step = 10
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

async def watch_ohlcv_single_market(exchange, symbol, timeframe, config, device, executor):
    """
    Watcher for a single market (pair) that streams updates and calculates signals only for display purposes.
    """
    logging.info(f"Starting single market OHLCV watcher for {symbol}")
    while not shutdown_event.is_set():
        try:
            async for symbol_upd, tf_upd, candles in exchange.watch_ohlcv(symbol, timeframe):
                if shutdown_event.is_set():
                    break
                if not candles:
                    continue

                # 1. Update the price immediately in bot_state
                async with bot_lock:
                    if symbol in bot_state:
                        bot_state[symbol]['price'] = candles[-1][4]

                # 2. Update the in-memory ohlcv cache
                async with ohlcv_lock:
                    if symbol not in ohlcv_cache:
                        ohlcv_cache[symbol] = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')

                    df = ohlcv_cache[symbol]
                    last_candle = candles[-1]
                    last_ts = pd.to_datetime(last_candle[0], unit='ms')

                    if not df.empty and last_ts == df.index[-1]:
                        df.iloc[-1] = last_candle[1:]
                    elif not df.empty and last_ts > df.index[-1] and len(candles) == 1:
                        new_row = pd.DataFrame([last_candle[1:]], columns=['open', 'high', 'low', 'close', 'volume'], index=[last_ts])
                        ohlcv_cache[symbol] = pd.concat([df, new_row]).tail(1000)
                    else:
                        new_candles_df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        new_candles_df['timestamp'] = pd.to_datetime(new_candles_df['timestamp'], unit='ms')
                        new_candles_df.set_index('timestamp', inplace=True)
                        df = pd.concat([df, new_candles_df])
                        df = df[~df.index.duplicated(keep='last')]
                        df.sort_index(inplace=True)
                        ohlcv_cache[symbol] = df.tail(1000)

                    # Trigger calculations in process pool only for UI display metrics
                    df_copy = ohlcv_cache[symbol].copy()

                if len(df_copy) >= 20:
                    strat = bot_state.get(symbol, {}).get('strategy', random.choice(STRATEGIES))
                    aggr = bot_state.get(symbol, {}).get('aggr', 'normal')
                    settings = {
                        'device': device,
                        'strategy': strat,
                        'aggr': aggr,
                        'ema_fast': config.get('ema_fast'),
                        'ema_slow': config.get('ema_slow'),
                        'macd_fast': config.get('macd_fast'),
                        'macd_slow': config.get('macd_slow'),
                        'macd_signal': config.get('macd_signal'),
                        'rsi_period': config.get('rsi_period'),
                        'tema_length': config.get('tema_length')
                    }

                    loop = asyncio.get_running_loop()
                    if executor:
                        df_signals = await loop.run_in_executor(executor, get_signals, df_copy, settings, False, config)
                    else:
                        df_signals = get_signals(df_copy, settings, global_config=config)

                    if not df_signals.empty:
                        latest = df_signals.iloc[-1]
                        async with bot_lock:
                            bot_state[symbol].update({
                                'ema_f': latest.get('ema_f', 0),
                                'ema_s': latest.get('ema_s', 0),
                                'macd_hist': latest.get('macd_hist', 0),
                                'rsi': latest.get('rsi', 0),
                                'adx': latest.get('adx', 0),
                                'volatility': latest.get('volatility', 0),
                                'score': 1 if latest.get('buy_signal') else (-1 if latest.get('sell_signal') else 0),
                                'tendency': "Bullish" if latest.get('buy_signal') else ("Bearish" if latest.get('sell_signal') else "Neutral")
                            })

        except Exception as e:
            if not shutdown_event.is_set():
                logging.error(f"WebSocket OHLCV watcher error for {symbol}: {e}")
                await asyncio.sleep(5)
            else:
                break

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
    
    base_currencies = set()
    for p in pairs_dict.keys():
        if '/' in p:
            parts = p.split('/')
            if len(parts) == 2:
                base_currencies.add(parts[1])
    
    fallbacks = ['EUR']
    for currency in fallbacks:
        base_currencies.add(currency)
    
    base_currencies = sorted(list(base_currencies))
    
    if not base_currencies:
        base_currencies = ['EUR']

    sellable_found = False
    all_tickers = {}
    try:
        all_tickers = await exchange.fetch_tickers()
    except: pass

    async def process_asset(asset, amount):
        nonlocal sellable_found
        try:
            amount = float(amount) if amount is not None else 0
        except (ValueError, TypeError):
            amount = 0
        if asset in base_currencies or amount <= 0: return

        symbol = None
        for bc in base_currencies:
            candidate = f"{asset}/{bc}"
            if candidate in pairs_dict:
                symbol = candidate
                break
        
        if not symbol and hasattr(exchange, 'markets') and exchange.markets:
            for bc in base_currencies:
                candidate = f"{asset}/{bc}"
                if candidate in exchange.markets:
                    symbol = candidate
                    break
            
            if not symbol:
                for bc in base_currencies:
                    candidate = f"{bc}/{asset}"
                    if candidate in exchange.markets:
                        symbol = candidate
                        break
        
        if not symbol:
            if asset in active_pairs:
                symbol = asset
            elif asset in pairs_dict:
                symbol = asset
        
        if not symbol:
            return

        existing_pos_list = data_manager.get_position(symbol)
        if existing_pos_list:
            total_existing_amount = sum(p['amount'] for p in existing_pos_list)
            sync_tolerance = config.get('exchange', {}).get('sync_tolerance', 0.001)
            try:
                amount_float = float(amount)
                total_existing_amount_float = float(total_existing_amount)
                sync_tolerance_float = float(sync_tolerance)
                if amount_float > 0 and abs(total_existing_amount_float - amount_float) / amount_float < sync_tolerance_float:
                    sellable_found = True
                    return
            except (ValueError, TypeError, ZeroDivisionError):
                pass

        is_dust = False
        try:
            ticker = all_tickers.get(symbol) or await exchange.fetch_ticker(symbol)
            if symbol in exchange.markets:
                m = exchange.markets[symbol]
                min_amt = float(m['limits']['amount']['min']) if m['limits']['amount']['min'] is not None else 0
                min_cost = float(m['limits']['cost']['min'] or config.get('exchange', {}).get('min_notional_fallback', 10.0))
                if ticker and ticker.get('last') is not None:
                    amount_float = float(amount) if amount is not None else 0
                    ticker_last = float(ticker['last']) if ticker['last'] is not None else 0
                    if amount_float < min_amt or (amount_float * ticker_last) < min_cost:
                        is_dust = True
            else:
                amount_float = float(amount) if amount is not None else 0
                dust_threshold = float(config.get('exchange', {}).get('dust_threshold_amount', 1e-6))
                if amount_float <= dust_threshold:
                    is_dust = True
        except: pass

        avg_price = 0
        total_cost = 0
        total_fee = 0
        accumulated_amount = 0
        try:
            trades = []
            try:
                trades = await asyncio.wait_for(exchange.fetch_my_trades(symbol, limit=50), timeout=config.get('timeouts', {}).get('order_fetch', 10))
            except Exception:
                try:
                    trades = await asyncio.wait_for(exchange.fetch_my_trades(symbol, limit=200), timeout=5)
                except Exception:
                    trades = []

            try:
                trades = sorted(trades, key=lambda t: t.get('timestamp', 0), reverse=True)
            except Exception:
                pass

            async def compute_estimated_fee(sym, trade_amt, trade_price):
                try:
                    fee_rate = await exchange.fetch_trading_fee(sym)
                except Exception:
                    fee_rate = config.get('exchange', {}).get('default_fee', 0.001)
                estimated_cost = trade_amt * trade_price * (fee_rate or 0)
                try:
                    est = await exchange.get_fee_in_quote(sym, estimated_cost, None)
                    return float(est or 0)
                except Exception:
                    return float(estimated_cost or 0)

            for t in trades:
                try:
                    if t.get('side') != 'buy':
                        continue
                    remaining_to_fill = amount - accumulated_amount
                    if remaining_to_fill <= 0:
                        break

                    trade_amt = min(float(t.get('amount') or 0), remaining_to_fill)
                    trade_price = float(t.get('price') or t.get('cost') / (t.get('amount') or 1) if t.get('amount') else 0)
                    total_cost += trade_amt * trade_price

                    fee_info = t.get('fee') or {}
                    fee_cost = fee_info.get('cost', 0) if isinstance(fee_info, dict) else 0
                    fee_currency = fee_info.get('currency') if isinstance(fee_info, dict) else None
                    if not fee_cost or float(fee_cost) == 0:
                        est_fee = await compute_estimated_fee(symbol, trade_amt, trade_price)
                        total_fee += est_fee
                    else:
                        try:
                            actual_fee = await exchange.get_fee_in_quote(symbol, fee_cost, fee_currency)
                            total_fee += actual_fee * (trade_amt / float(t.get('amount') or trade_amt))
                        except Exception:
                            est_fee = await compute_estimated_fee(symbol, trade_amt, trade_price)
                            total_fee += est_fee

                    accumulated_amount += trade_amt
                except Exception:
                    continue

            if accumulated_amount > 0:
                avg_price = total_cost / accumulated_amount
            elif len(trades) == 0:
                ticker = all_tickers.get(symbol) or await exchange.fetch_ticker(symbol)
                curr_p = (ticker.get('last') or 0) if ticker else 0
                if curr_p > 0:
                    avg_price = curr_p
                    total_cost = amount * avg_price
        except Exception as e:
            logging.warning(f"[{symbol}] Error fetching trade history for sync: {e}")
            ticker = all_tickers.get(symbol) or await exchange.fetch_ticker(symbol)
            curr_p = (ticker.get('last') or 0) if ticker else 0
            if curr_p > 0:
                avg_price = curr_p
                total_cost = amount * avg_price

        if avg_price <= 0:
            ticker = all_tickers.get(symbol) or await exchange.fetch_ticker(symbol)
            avg_price = (ticker.get('last') or 0) if ticker else 0
            if avg_price > 0:
                total_cost = amount * avg_price

        if avg_price > 0:
            data_manager.add_position(
                symbol, avg_price, amount, total_fee,
                {"info": "launch_sync", "auto_sell_disabled": False, "strategy": "Synced", "candle_count": 0}, time.time(),
                total_base=total_cost + total_fee
            )
            logging.info(f"[{symbol}] Synced balance: {amount} at calculated avg price {format_price(avg_price)}")

    sync_semaphore = asyncio.Semaphore(config.get('exchange', {}).get('max_concurrent_syncs', 3))
    async def process_with_semaphore(asset, amount):
        async with sync_semaphore:
            await process_asset(asset, amount)

    await asyncio.gather(*[process_with_semaphore(a, am) for a, am in free_balances.items()])

    open_positions = data_manager.get_open_positions()
    new_symbols = []
    for symbol in open_positions.keys():
        if symbol not in active_pairs:
            active_pairs.append(symbol)
            new_symbols.append(symbol)
            logging.info(f"[{symbol}] Added to active_pairs due to existing position")
    
    for symbol in new_symbols:
        async with ohlcv_lock:
            if symbol not in ohlcv_cache:
                ohlcv_cache[symbol] = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')
        if symbol not in bot_state:
            bot_state[symbol] = {
                'price': 0, 'rsi': 0, 'tendency': 'Neutral',
                'last_signal': 'Init', 'position': open_positions[symbol],
                'consecutive_buys': 0, 'consecutive_sells': 0,
                'score': 0, 'ema_f': 0, 'ema_s': 0, 'adx': 0,
                'volatility': 0, 'expected_profit': 0, 'last_analysis_ts': 0,
                'strategy': random.choice(STRATEGIES), 'aggr': random.choice(['normal', 'aggressive', 'dynamic'])
            }

    logging.info(f"[{exchange_id}] Position sync completed. Created positions: {list(open_positions.keys())}")
    
    async with bot_lock:
        for symbol, pos_list in open_positions.items():
            if symbol not in bot_state:
                bot_state[symbol] = {}
            bot_state[symbol]['position'] = pos_list

    logging.info(f"Syncing positions from {exchange_id} API done.")

def worker_process_init():
    import signal
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except:
        pass

async def execute_buy(exchange, symbol, data, data_manager, engine, config, manual=False, strategy=None, candle_count=0):
    global current_balances

    async with pending_orders_lock:
        for oid, po in pending_orders.items():
            if po['symbol'] == symbol and po['side'] == 'buy':
                logging.info(f"[{symbol}] Skipping BUY: order {oid} is already pending.")
                return

    async with bot_lock:
        pos = data_manager.get_position(symbol)
        max_lots = config['pairs'].get(symbol, {}).get('max_lots_per_symbol') or config.get('max_lots_per_symbol') or float('inf')
        if pos is not None and len(pos) >= max_lots:
            logging.info(f"[{symbol}] Skipping BUY: max_lots_per_symbol ({max_lots}) reached.")
            return

        open_positions = data_manager.get_open_positions()
        max_open = config.get('max_open_positions') or float('inf')
        if symbol not in open_positions and len(open_positions) >= max_open:
            logging.info(f"[{symbol}] Skipping BUY: max_open_positions ({max_open}) reached.")
            return

    try:
        price = None
        try:
            order_book = await exchange.fetch_order_book(symbol, limit=200)
            if order_book and order_book['bids'] and order_book['asks']:
                best_bid = order_book['bids'][0][0]
                best_ask = order_book['asks'][0][0]
                price = (best_bid + best_ask) / 2
                logging.info(f"[{symbol}] Mid-price calculated: {price} (Bid: {best_bid}, Ask: {best_ask})")
        except Exception as e:
            logging.debug(f"[{symbol}] Failed to fetch order book: {e}")
        
        if price is None:
            price = data.get('close')
            if price is None:
                logging.error(f"[{symbol}] No price available from order book or data")
                raise ValueError("Unable to determine price")

        order = None
        market = exchange.markets.get(symbol)
        quote_curr = symbol.split('/')[1]
        async with bot_lock:
            balance = current_balances

        if not balance:
            logging.info(f"[{symbol}] No balance data available. Fetching from exchange...")
            balance = await exchange.fetch_balance()
            async with bot_lock:
                current_balances = balance
        
        if not balance:
            raise ValueError(f"[{symbol}] Unable to fetch balance data")

        amount = engine.calculate_position_size(balance, price, quote_curr)
        if amount is None:
            logging.error(f"[{symbol}] calculate_position_size returned None (balance: {balance}, price: {price}, quote_curr: {quote_curr})")
            raise ValueError("Position size calculation returned None")
        try:
            amount = float(amount)
            price = float(price)
            cost = amount * price
        except (ValueError, TypeError):
            raise ValueError(f"Cannot convert amount or price to float: amount={amount}, price={price}")

        if amount <= 0:
            raise Exception(f"Non-positive amount: {amount}")

        if amount > 0:
            exchange_config = config.get('exchange', {})
            min_notional = exchange_config.get('min_notional_fallback', 10.0) if exchange_config else 10.0
            if market and 'limits' in market and 'cost' in market['limits'] and market['limits']['cost']:
                min_limit = market['limits']['cost'].get('min')
                if min_limit is not None:
                    try:
                        min_notional = float(min_limit)
                    except (ValueError, TypeError):
                        min_notional = 10.0

            try:
                if cost < min_notional:
                    buffer = exchange_config.get('notional_buffer', 1.05) if exchange_config else 1.05
                    if min_notional > 0 and price > 0:
                        amount = (min_notional / price) * buffer
                        cost = amount * price
                    else:
                        raise ValueError(f"Cannot calculate adjusted amount: min_notional={min_notional}, price={price}")
            except (ValueError, TypeError) as e:
                raise

            quote_curr = symbol.split('/')[1]
            quote_bal = balance.get(quote_curr)
            if isinstance(quote_bal, dict):
                free_balance = quote_bal.get('total', 0) or 0
                try:
                    free_balance = float(free_balance) if free_balance is not None else 0
                except (ValueError, TypeError):
                    free_balance = 0
            else:
                free_balance = float(quote_bal) if quote_bal is not None else 0

            try:
                if free_balance < cost:
                    pair_suspensions[symbol] = {'reason': 'budget', 'amount_required': cost}
                    raise Exception(f"Insufficient balance: need {cost}, have {free_balance}")
            except Exception:
                raise

            # Use create_order with price for limit buy
            order = await exchange.create_order(symbol, 'buy', amount, price=price)

            if order and 'id' in order:
                async with pending_orders_lock:
                    pending_orders[str(order['id'])] = {
                        'symbol': symbol,
                        'side': 'buy',
                        'timestamp': time.time(),
                        'trigger_data': {},
                        'strategy': strategy or ("Manual" if manual else "Unknown"),
                        'candle_count': candle_count,
                        'is_limit': True
                    }
                logging.info(f"[{symbol}] BUY order created: {order['id']}")
            else:
                pair_suspensions[symbol] = {'reason': 'budget', 'amount_required': cost}
                raise Exception(f"Order creation failed: no order ID returned")

        # Since we do not use WS watch_orders in manual trader to avoid conflicting state, we process the order fill manually after a short delay or synchronously
        if order:
            await asyncio.sleep(1.0)
            try:
                verified = await exchange.fetch_order(order['id'], symbol)
                if verified:
                    order = verified
            except Exception as e:
                logging.debug(f"Failed to fetch order status for manual buy: {e}")
            await process_order_fill(order, exchange, data_manager, config, engine)
    except Exception as e:
        error_str = str(e)
        logging.error(f"Buy failed for {symbol}: {e}")

async def execute_sell(exchange, symbol, data, data_manager, engine, config, force=False, strategy=None, candle_count=0):
    async with pending_sells_lock:
        if symbol in pending_sells:
            logging.debug(f"[{symbol}] Skipping SELL: another sell is already in-flight for this symbol.")
            return

    async with pending_orders_lock:
        for oid, po in pending_orders.items():
            if po['symbol'] == symbol and po['side'] == 'sell':
                logging.debug(f"[{symbol}] Skipping SELL: order {oid} is already pending.")
                return

    async with bot_lock:
        positions = data_manager.get_position(symbol)
        if not positions: return

    try:
        ticker = await exchange.fetch_ticker(symbol)
        price = float(ticker.get('last') or data['close']) if ticker else float(data['close'])
    except (ValueError, TypeError):
        price = float(data['close'])

    fee_rate = await exchange.fetch_trading_fee(symbol)

    balance = await exchange.fetch_balance()
    free_balance = 0
    asset = symbol.split('/')[0]
    if balance and 'free' in balance:
        free_balance = balance['free'].get(asset, 0)
        try:
            free_balance = float(free_balance) if free_balance is not None else 0
        except (ValueError, TypeError):
            free_balance = 0
    elif balance:
        free_balance = balance.get(asset, 0)
        try:
            free_balance = float(free_balance) if free_balance is not None else 0
        except (ValueError, TypeError):
            free_balance = 0

    sell_lot_indices = []
    total_sell_amount = 0
    total_entry_cost = 0
    for i, pos in enumerate(positions):
        is_profitable = engine.is_profitable(price, pos['entry_price'], fee_rate=fee_rate, entry_total_base=pos.get('entry_total_base', 0), amount=pos['amount'])
        auto_disabled = pos.get('trigger_data', {}).get('auto_sell_disabled', False)
        if (force or is_profitable) and (force or not auto_disabled):
            sell_lot_indices.append(i)
            total_sell_amount += pos['amount']
            total_entry_cost += pos.get('entry_total_base', 0)

    if not sell_lot_indices:
        return

    market = exchange.markets.get(symbol)
    min_amt = 0
    min_cost = 0
    if market and 'limits' in market:
        try:
            min_amt = float(market.get('limits', {}).get('amount', {}).get('min') or 0)
            min_cost = float(market.get('limits', {}).get('cost', {}).get('min') or 0)
        except (ValueError, TypeError):
            min_amt = 0
            min_cost = 0

    try:
        total_sell_amount = float(total_sell_amount) if total_sell_amount is not None else 0
        min_amt = float(min_amt) if min_amt is not None else 0
        min_cost = float(min_cost) if min_cost is not None else 0
        price = float(price) if price is not None else 0
    except (ValueError, TypeError):
        total_sell_amount = 0
        min_amt = 0
        min_cost = 0
        price = 0

    free_balance = float(free_balance) if free_balance is not None else 0
    if total_sell_amount > free_balance:
        total_sell_amount = free_balance

    if total_sell_amount < min_amt:
        logging.warning(f"[{symbol}] Bundle sell amount {total_sell_amount:.4f} still below minimum {min_amt}. Skipping.")
        return
    if (total_sell_amount * price) < min_cost:
        logging.warning(f"[{symbol}] Bundle sell cost {total_sell_amount * price:.4f} still below minimum notional {min_cost}. Skipping.")
        return

    try:
        async with pending_sells_lock:
            pending_sells.add(symbol)

        order = None
        try:
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
        except Exception as exc:
            async with pending_sells_lock:
                if symbol in pending_sells:
                    pending_sells.discard(symbol)
            raise

        if order:
            await asyncio.sleep(1.0)
            try:
                verified = await exchange.fetch_order(order['id'], symbol)
                if verified:
                    order = verified
            except Exception as e:
                logging.debug(f"Failed to fetch order status for manual sell: {e}")
            await process_order_fill(order, exchange, data_manager, config, engine)
    except Exception as e:
        logging.error(f"Aggregated sell failed for {symbol}: {e}")
        async with bot_lock:
            bot_state[symbol]['position'] = data_manager.get_position(symbol)
        async with pending_sells_lock:
            if symbol in pending_sells:
                pending_sells.discard(symbol)

async def process_order_fill(order, exchange, data_manager, config, engine):
    """
    Centralized handler for completed orders to avoid race conditions and lost confirmations.
    """
    terminal_statuses = ['closed', 'canceled', 'expired', 'rejected']
    status = order.get('status')
    if status not in terminal_statuses:
        return False

    order_id = str(order.get('id'))
    async with processed_orders_lock:
        if order_id in processed_orders:
            async with pending_orders_lock:
                if order_id in pending_orders:
                    meta = pending_orders.pop(order_id)
                    try:
                        if meta and meta.get('side') == 'sell':
                            async with pending_sells_lock:
                                pending_sells.discard(meta.get('symbol'))
                    except Exception:
                        pass
            return False
        processed_orders.append(order_id)

    meta = None
    for _ in range(5):
        async with pending_orders_lock:
            if order_id in pending_orders:
                meta = pending_orders.pop(order_id)
                break
        await asyncio.sleep(0.1)

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

    filled_amount = safe_float(order.get('filled', 0.0) or 0.0)
    actual_price = safe_float(order.get('price') or order.get('average', 0.0) or 0.0)
    cost = safe_float(order.get('cost') or (filled_amount * actual_price) or 0.0)

    fee_obj = order.get('fee') or {}
    try:
        fee_cost_raw = fee_obj.get('cost', 0.0) if isinstance(fee_obj, dict) else 0.0
    except Exception:
        fee_cost_raw = 0.0
    fee_cost = safe_float(fee_cost_raw, 0.0)
    fee_currency = fee_obj.get('currency') if isinstance(fee_obj, dict) else None
    total_fee = 0.0
    try:
        total_fee = await exchange.get_fee_in_quote(symbol, fee_cost, fee_currency)
        total_fee = safe_float(total_fee, 0.0)
    except Exception:
        total_fee = safe_float(fee_cost, 0.0)

    try:
        if (not total_fee or float(total_fee) == 0.0) and filled_amount > 0 and actual_price > 0:
            try:
                fee_rate = await exchange.fetch_trading_fee(symbol)
            except Exception:
                fee_rate = config.get('exchange', {}).get('default_fee', 0.001)
            try:
                est_fee = filled_amount * actual_price * float(fee_rate or 0)
                total_fee = float(est_fee)
            except Exception:
                pass
    except Exception:
        pass

    trigger_data = meta['trigger_data'] if meta else {}
    timestamp = time.time()
    if meta and 'timestamp' in meta:
        try:
            timestamp = safe_float(meta.get('timestamp'), time.time())
        except Exception:
            timestamp = time.time()
    strategy = meta.get('strategy', 'Unknown') if meta else 'Unknown'
    candle_count = meta.get('candle_count', 0) if meta else 0

    if side == 'buy':
        total_val = cost + total_fee
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
                    lot_close_amt = min(float(pos['amount'] or 0), remaining_filled)
                    if lot_close_amt <= 0: continue

                    if idx == len(sell_lot_indices) - 1 or lot_close_amt >= remaining_filled - 1e-10:
                        current_lot_received = remaining_net_received
                        current_lot_fee = remaining_fee
                        lot_close_amt = remaining_filled
                    else:
                        proportion = lot_close_amt / filled_amount
                        current_lot_received = total_net_received * proportion
                        current_lot_fee = total_fee * proportion

                    pos_amount = float(pos['amount'] or 0)
                    entry_cost_proportion = (lot_close_amt / pos_amount) if pos_amount > 0 else 1.0
                    entry_cost_part = float(pos.get('entry_total_base', 0.0) or 0) * entry_cost_proportion
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

        try:
            fresh_balance = await exchange.fetch_balance()
            if fresh_balance:
                async with bot_lock:
                    global current_balances
                    current_balances = fresh_balance
        except Exception as e:
            logging.warning(f"Failed to update balance after sell for {symbol}: {e}")

        if await is_pair_dust(symbol, exchange, config):
            data_manager.clear_positions(symbol)
            logging.info(f"[{symbol}] Remaining balance is dust. Clearing open positions.")

    async with bot_lock:
        if symbol not in bot_state: bot_state[symbol] = {}
        bot_state[symbol]['position'] = data_manager.get_position(symbol)
    
    return True

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
        return amount < 1e-6

    limits = market.get('limits', {})
    min_amt = limits.get('amount', {}).get('min') or config.get('exchange', {}).get('min_amount_fallback', 1e-8)
    min_cost = limits.get('cost', {}).get('min') or config.get('exchange', {}).get('min_notional_fallback', 10.0)

    if amount < min_amt: return True
    if price > 0 and (amount * price) < min_cost: return True
    
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
                fee_str = f"{format_price(total_fee, config=config)} {quote}" if total_fee > 0 else "-"
            else:
                amt_str = format_amt(pos['amount'], config=config)
                entry_str = format_price(pos['entry_price'], config=config)
                fee_str = f"{format_price(pos.get('entry_fee', 0), config=config)} {quote}" if pos.get('entry_fee', 0) > 0 else "-"

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
        help_text.append("  Ctrl+C : Stop manual interface gracefully\n")
        pairs_panel = Panel(help_text, title="[bold]Help / Info[/]", border_style="bold yellow")

    if show_chart and chart_symbol:
        chart_content = render_ascii_chart(chart_symbol, config)
        chart_tf = symbol_timeframes.get(chart_symbol, default_candle_timeframe)
        pairs_panel = Panel(chart_content, title=f"[bold]Candles for {chart_symbol} at {chart_tf}[/]", border_style="bold magenta")

    if not startup_complete:
        waiting_text = Text.from_markup("\n\n\n\n\n[bold blink yellow]Waiting for system initialization...[/]\n", justify="center")
        waiting_text.append_text(Text.from_markup("[dim]Fetching market data and calculating first signals...[/]\n", style="white"))
        pairs_panel = Panel(waiting_text, title="[bold]System Startup[/]", border_style="bold yellow")

    layout = Layout()
    layout.split(
        Layout(Panel(Text("🛸 CCXT Pro Manual Trader (Async)", style="bold magenta", justify="center"), border_style="blue"), size=3),
        Layout(log_panel, size=log_height+2),
        Layout(pairs_panel, name="main"),
        Layout(Panel(status_display, title="Status", border_style="cyan"), size=3)
    )
    return layout

async def run_dashboard(config):
    try:
        refresh_rate = config.get('ui', {}).get('refresh_rate', 4)
        with Live(make_dashboard(config), refresh_per_second=refresh_rate, screen=False) as live:
            while not startup_complete and not shutdown_event.is_set():
                live.update(make_dashboard(config))
                await asyncio.sleep(0.5)

        if shutdown_event.is_set(): return

        with Live(make_dashboard(config), refresh_per_second=refresh_rate, screen=True) as live:
            while not shutdown_event.is_set():
                live.update(make_dashboard(config))
                await asyncio.sleep(1.0 / refresh_rate)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.info(f"[red]Dashboard error: {e}")

async def main():
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(global_exception_handler)

    parser = argparse.ArgumentParser(description='CCXT Pro Manual Trader (Asynchronous)')
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

    logging.info("Retrieving best pairs based on balances and 24h volume using computeSymbols...")
    global discovery_pool, active_pairs, default_candle_timeframe
    try:
        # Retrieve balance and compute symbols using botv4-style computeSymbols
        balance = await exchange.fetch_balance()
        async with bot_lock:
            global current_balances
            current_balances = balance

        # availablePairs = computeSymbols(balance, None)
        available_pairs_data = computeSymbols(balance, None)
        discovery_pool = [item[0] for item in available_pairs_data]
        
        num_pairs = int(config.get('max_number_of_pairs', 40))
        active_pairs = discovery_pool[:num_pairs]

        logging.info(f"Initialized discovery pool with {len(discovery_pool)} pairs using computeSymbols.")
        logging.info(f"Initial active set: {len(active_pairs)} pairs.")

    except Exception as e:
        logging.error(f"Failed to initialize pairs using computeSymbols: {e}")
        active_pairs = list(config.get('pairs', {}).keys())
        discovery_pool = active_pairs[:40]

    if not active_pairs:
        logging.error("No pairs could be initialized.")
        await exchange.close()
        return

    # Start UI task
    global ui_task, background_tasks, startup_complete, ohlcv_tasks
    ui_task = asyncio.create_task(run_dashboard(config))

    logging.info("[bold cyan]System initialization started...")

    # Initial Batch
    for symbol in active_pairs:
        pair_cfg = config.get('pairs', {}).get(symbol, {})
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
        pair_last_trade_time[symbol] = time.time()

    if args.fast_start:
        logging.info("[bold yellow]Fast start enabled: Skipping initial candles fetch.")
        for symbol in active_pairs:
            ohlcv_cache[symbol] = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')
    else:
        logging.info(f"[bold cyan]Fetching initial candles for {len(active_pairs)} pairs...")
        semaphore = asyncio.Semaphore(5)

        async def init_symbol(symbol):
            async with semaphore:
                try:
                    logging.info(f"Fetching initial 1s candles for {symbol} (Target: 10000)...")
                    ohlcv_res, actual_tf = await exchange.fetch_ohlcv_10k(symbol, '1s', limit=10000)
                    df_1s = pd.DataFrame(ohlcv_res, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df_1s['timestamp'] = pd.to_datetime(df_1s['timestamp'], unit='ms')
                    for col in ['open', 'high', 'low', 'close', 'volume']:
                        df_1s[col] = pd.to_numeric(df_1s[col], errors='coerce')
                    df_1s.set_index('timestamp', inplace=True)
                    ohlcv_cache[symbol] = df_1s
                    symbol_timeframes[symbol] = actual_tf
                    logging.info(f"[{symbol}] Loaded {len(df_1s)} candles ({actual_tf}).")

                except Exception as e:
                    logging.warning(f"Failed to load candles for {symbol}: {e}")
                    empty_df = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')
                    ohlcv_cache[symbol] = empty_df

        await asyncio.gather(*[init_symbol(s) for s in active_pairs])

    default_candle_timeframe, _ = await exchange._get_supported_timeframe('1s')
    for symbol in active_pairs:
        symbol_timeframes.setdefault(symbol, default_candle_timeframe)

    async with ohlcv_lock:
        for s in active_pairs:
            if s not in ohlcv_cache:
                empty_df = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')
                ohlcv_cache[s] = empty_df

    # Analysis Executor
    max_workers = max(1, (os.cpu_count() or 4) - 2)
    executor = concurrent.futures.ProcessPoolExecutor(
        max_workers=max_workers,
        initializer=worker_process_init
    )

    # Start minimal essential manual background tasks
    logging.info("[bold green]Starting background tasks...")
    background_tasks = [
        asyncio.create_task(input_task(exchange, config, data_manager, engine)),
        asyncio.create_task(chart_renderer_task(config))
    ]

    # Start single market watchers - ONE watcher task per active pair to update candles as requested
    logging.info("[bold green]Spawning one OHLCV watcher per market...")
    for symbol in active_pairs:
        tf = symbol_timeframes.get(symbol, default_candle_timeframe)
        watcher = asyncio.create_task(watch_ohlcv_single_market(exchange, symbol, tf, config, device, executor))
        ohlcv_tasks.append(watcher)

    await asyncio.sleep(2)

    # Synchronizing positions
    logging.info(f"Synchronizing positions from the {exchange_id.capitalize()} API...")
    try:
        await asyncio.wait_for(sync_live_positions(exchange, data_manager, config), timeout=120)
    except asyncio.TimeoutError:
        logging.error("Balance synchronization timed out. Proceeding with partial data.")
    except Exception as e:
        logging.error(f"Error during balance synchronization: {e}")

    await asyncio.sleep(config.get('timeouts', {}).get('startup_wait', 4))
    startup_complete = True
    play_sound("startup", config)
    logging.info("[bold green]Manual trader fully operational.")

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

        all_tasks = background_tasks.copy() + ohlcv_tasks.copy()
        if ui_task:
            all_tasks.append(ui_task)

        for t in all_tasks:
            if not t.done():
                t.cancel()

        if all_tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*all_tasks, return_exceptions=True), timeout=5)
            except asyncio.TimeoutError:
                logging.warning("Shutdown cleanup timed out.")
            except Exception as e:
                logging.error(f"Error during shutdown cleanup: {e}")

        if executor: executor.shutdown(wait=False)

        try:
            await exchange.close()
        except: pass

        console.clear()
        console.print("[bold red]Manual trader shutdown sequence complete.[/]")
        console.print("[bold white]Final Log Summary:[/]")
        for log in all_logs[-20:]:
            console.print(f"[{log['timestamp']}] {log['msg']}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as e:
        console.print_exception()

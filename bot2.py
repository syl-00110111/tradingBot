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
trades_task = None

# Global Active Pairs Management
active_pairs = []
discovery_pool = []
pair_last_trade_time = {}
pair_hour_trades = {} # symbol -> deque of timestamps

# Global Buy Queue for turn-based processing
buy_queue = []
buy_queue_lock = asyncio.Lock()

# Track orders placed by the bot to process them via WebSocket confirmation
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
    if isinstance(v, str):
        try:
            return float(v.replace(',', ''))
        except Exception:
            try:
                # Strip non-numeric characters
                filtered = ''.join(ch for ch in v if (ch.isdigit() or ch in '.-eE'))
                return float(filtered) if filtered not in ('', '.', '-', '-.') else float(default)
            except Exception:
                return float(default)
    try:
        return float(v)
    except Exception:
        return float(default)

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

async def build_discovery_pool_from_balances(exchange, config):
    """
    Build discovery pool based on available balances and 24h volume.
    For each currency we have balance for, find all symbols trading with that currency as quote or base,
    then pick the top volume symbols.
    """
    global discovery_pool
    try:
        logging.info("Building discovery pool from available balances...")

        # 1. Fetch current balances and store globally
        balance = await exchange.fetch_balance()
        async with bot_lock:
            global current_balances
            current_balances = balance

        # 2. Determine currencies with non-zero free balance
        free_balances = balance.get('free', {}) if isinstance(balance, dict) and 'free' in balance else {}
        currencies_with_balance = []
        # Treat amounts below dust threshold as zero
        dust_threshold_amount = float(config.get('exchange', {}).get('dust_threshold_amount', 1e-6))
        for curr, amount in free_balances.items():
            try:
                # handle nested balance dicts or raw numeric values
                if isinstance(amount, dict):
                    amt = amount.get('free') or amount.get('total') or amount.get('used') or 0
                else:
                    amt = amount
                amount_float = float(amt) if amt is not None else 0
                if amount_float > dust_threshold_amount:
                    currencies_with_balance.append(curr)
            except (ValueError, TypeError):
                continue

        # 3. Fallback if none found
        if not currencies_with_balance:
            logging.warning("No currencies with non-dust balance found. Building fallback currency list from markets but filtering dust.")
            # Use most common currencies from markets as fallback but only include those present in balances and not dust
            markets_keys = list(exchange.markets.keys()) if hasattr(exchange, 'markets') else []
            bases = [s.split('/')[0] for s in markets_keys if '/' in s]
            quotes = [s.split('/')[1] for s in markets_keys if '/' in s]
            candidate_currencies = list(dict.fromkeys(bases + quotes))

            # Filter candidate currencies against any balance entries (free/total) and dust threshold
            filtered = []
            for c in candidate_currencies:
                found_amt = None
                # check free, total top-level, or any nested dict
                try:
                    if isinstance(balance, dict):
                        # direct key
                        if c in balance and not isinstance(balance[c], dict):
                            found_amt = float(balance[c])
                        # check total/free dicts
                        if found_amt is None and 'free' in balance and isinstance(balance['free'], dict) and c in balance['free']:
                            found_amt = float(balance['free'].get(c) or 0)
                        if found_amt is None and 'total' in balance and isinstance(balance['total'], dict) and c in balance['total']:
                            found_amt = float(balance['total'].get(c) or 0)
                except Exception:
                    found_amt = None
                if found_amt is not None and found_amt > dust_threshold_amount:
                    filtered.append(c)

            if not filtered:
                # final fallback to common stable currencies
                currencies_with_balance = ['EUR']
            else:
                currencies_with_balance = filtered

        # 4. Fetch tickers and prepare top 24h volume pairs
        all_tickers = await exchange.fetch_tickers()
        tickers_list = []
        for symbol, tk in (all_tickers.items() if isinstance(all_tickers, dict) else []):
            if symbol not in exchange.markets:
                continue
            vol = tk.get('quoteVolume') or ((tk.get('baseVolume') or 0) * (tk.get('last') or 0)) or 0
            tickers_list.append({'symbol': symbol, 'volume24h': float(vol)})

        # sort by 24h volume descending and keep top candidates to analyze recent trades
        tickers_list.sort(key=lambda x: x['volume24h'], reverse=True)
        top_scan_limit = int(config.get('discovery', {}).get('top_24h_scan_limit', 200))
        top_candidates = tickers_list[:top_scan_limit]

        # 5. From top 24h pairs, fetch recent trades and count trades within last hour
        now = time.time()
        one_hour_ago = now - 3600
        traded_counts = []
        for item in top_candidates:
            s = item['symbol']
            try:
                trades = await exchange.fetch_trades(s, since=None, limit=1000)
            except Exception:
                trades = []
            count = 0
            try:
                for t in trades:
                    ts = (t.get('timestamp') or t.get('time') or 0) / 1000.0
                    if ts >= one_hour_ago:
                        count += 1
                    else:
                        # trades are usually returned newest-first or oldest-first depending on exchange; don't rely on order
                        continue
            except Exception:
                count = 0
            traded_counts.append({'symbol': s, 'trades_last_hour': count, 'volume24h': item['volume24h']})

        # sort by trades in last hour (desc), then by 24h volume
        traded_counts.sort(key=lambda x: (x['trades_last_hour'], x['volume24h']), reverse=True)

        # 6. Compose discovery pool: first include best pairs for currencies with balance
        max_pairs = int(config.get('max_number_of_pairs', 40))
        pool = []
        added = set()

        # helper: find best pair for a currency from top 24h list
        symbol_by_currency = {}
        for rec in tickers_list:
            try:
                base, quote = rec['symbol'].split('/')
            except Exception:
                continue
            # prefer pair where currency is base first, else quote
            symbol_by_currency.setdefault(base, []).append((rec['symbol'], rec['volume24h']))
            symbol_by_currency.setdefault(quote, []).append((rec['symbol'], rec['volume24h']))
        # Only pairs buyable with available quote balances should be considered.
        buyable_quotes = set(currencies_with_balance)

        for curr in currencies_with_balance:
            # Prefer pairs where the quote currency equals the currency with balance
            found = False
            for rec in tickers_list:
                sym = rec['symbol']
                if '/' not in sym: continue
                base, quote = sym.split('/')
                if quote == curr and sym in exchange.markets and sym not in added:
                    pool.append(sym)
                    added.add(sym)
                    found = True
                    break
            if found:
                if len(pool) >= max_pairs:
                    break
                continue
            # If none found in top tickers, try symbol_by_currency fallback but require quote to be buyable
            candidates_for_curr = symbol_by_currency.get(curr, [])
            if not candidates_for_curr:
                continue
            candidates_for_curr.sort(key=lambda x: x[1], reverse=True)
            for sym, _ in candidates_for_curr:
                if '/' not in sym: continue
                b, q = sym.split('/')
                if q in buyable_quotes and sym in exchange.markets and sym not in added:
                    pool.append(sym)
                    added.add(sym)
                    break
            if len(pool) >= max_pairs:
                break

        # 7. Fill the rest with the top traded pairs from traded_counts
        for rec in traded_counts:
            if len(pool) >= max_pairs:
                break
            sym = rec['symbol']
            if '/' not in sym: continue
            if sym not in added and sym in exchange.markets:
                b, q = sym.split('/')
                if buyable_quotes and q not in buyable_quotes:
                    continue
                pool.append(sym)
                added.add(sym)

        # 8. Final fallback: if still not enough, fill with highest 24h volume pairs
        if len(pool) < max_pairs:
            for rec in tickers_list:
                if len(pool) >= max_pairs:
                    break
                sym = rec['symbol']
                if '/' not in sym: continue
                if sym not in added and sym in exchange.markets:
                    b, q = sym.split('/')
                    if buyable_quotes and q not in buyable_quotes:
                        continue
                    pool.append(sym)
                    added.add(sym)

        discovery_pool = pool[:max_pairs]
        logging.info(f"Discovery pool built with {len(discovery_pool)} symbols (currencies with balance: {len(currencies_with_balance)}).")
        return discovery_pool
    except Exception as e:
        logging.error(f"Error building discovery pool: {e}")
        return []


async def refresh_discovery_pool_task(exchange, config):
    """
    Refreshes the discovery pool every hour based on balances and 24h volume.
    """
    global discovery_pool
    while not shutdown_event.is_set():
        try:
            await asyncio.sleep(3600)
            await build_discovery_pool_from_balances(exchange, config)
        except Exception as e:
            logging.error(f"Error refreshing discovery pool: {e}")
            await asyncio.sleep(60)

async def fetch_trades_count_task(exchange, config):
    """
    Periodically fetches the number of trades for pairs during the latest hour.
    Updates pair_hour_trades for active monitoring.
    """
    logging.info(f"[bold cyan]Starting trades count fetcher.")
    
    while not shutdown_event.is_set():
        try:
            # Fetch trade counts for all symbols we care about (discovery_pool + active_pairs)
            symbols_to_fetch = list(set(discovery_pool + active_pairs))
            
            for symbol in symbols_to_fetch:
                try:
                    # Fetch recent trades for this symbol
                    # Use limit to get trades from approximately the last hour
                    trades = await exchange.fetch_trades(symbol, since=None, limit=1000)
                    
                    now = time.time()
                    if symbol not in pair_hour_trades:
                        pair_hour_trades[symbol] = deque()
                    
                    # Clear old data
                    pair_hour_trades[symbol].clear()
                    
                    # Add timestamps of recent trades (within the last hour)
                    one_hour_ago = now - 3600
                    for trade_data in trades:
                        trade_time = trade_data['timestamp'] / 1000  # Convert ms to seconds
                        if trade_time >= one_hour_ago:
                            pair_hour_trades[symbol].append(trade_time)
                        else:
                            # Trades are in chronological order, so we can break early
                            break
                    
                    # Update last trade time
                    if trades:
                        pair_last_trade_time[symbol] = trades[-1]['timestamp'] / 1000
                    
                except Exception as e:
                    logging.debug(f"Failed to fetch trades for {symbol}: {e}")
                    continue
            
            # Wait before next fetch
            await asyncio.sleep(60)  # Fetch every minute
            
        except Exception as e:
            if not shutdown_event.is_set():
                logging.error(f"Trades count fetch error: {e}")
                await asyncio.sleep(10)
            else: break

async def add_active_pair(symbol, exchange, config, data_manager):
    if symbol in active_pairs: return
    
    # Initialize bot state
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
    
    if symbol not in ohlcv_cache:
        ohlcv_cache[symbol] = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')
        
    active_pairs.append(symbol)
    symbol_timeframes.setdefault(symbol, default_candle_timeframe)
    logging.info(f"[{symbol}] Added to active monitoring set.")

async def remove_active_pair(symbol):
    if symbol in active_pairs:
        active_pairs.remove(symbol)
        # We keep bot_state and ohlcv_cache for a bit or just leave them, 
        # but they won't be updated by the watcher anymore.
        logging.info(f"[{symbol}] Removed from active monitoring set.")

async def watch_ohlcv_global_task(exchange, watch_pairs_list, config, data_manager, pattern_manager, engine, device, executor):
    """
    Single watcher task for all symbols in active_pairs.
    'watch_pairs_list' is the live list active_pairs.
    """
    logging.info(f"[bold cyan]Starting global OHLCV watcher.")

    while not shutdown_event.is_set():
        try:
            # We pass a wrapper that yields [symbol, '1s'] for each symbol in active_pairs
            # But I modified watch_ohlcv_for_symbols to handle dynamic lists if I pass the list itself.
            async for updates in exchange.watch_ohlcv_for_symbols(watch_pairs_list):
                if shutdown_event.is_set(): break

                # Update all prices in the batch first for maximum perceived responsiveness in the dashboard
                async with bot_lock:
                    for update in updates:
                        symbol, _, candles = update
                        if symbol in bot_state:
                            bot_state[symbol]['price'] = candles[-1][4]

                for symbol, timeframe, candles in updates:
                    symbol_timeframes[symbol] = timeframe
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
                        elif not df.empty and last_ts > df.index[-1] and len(candles) == 1:
                            # Efficiently append a single new candle
                            new_row = pd.DataFrame([last_candle[1:]], columns=['open', 'high', 'low', 'close', 'volume'], index=[last_ts])
                            ohlcv_cache[symbol] = pd.concat([df, new_row]).tail(config.get('exchange', {}).get('fetch_ohlcv_limit', 10000))
                        else:
                            # Full update for gaps or multiple new candles
                            new_candles_df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                            new_candles_df['timestamp'] = pd.to_datetime(new_candles_df['timestamp'], unit='ms')
                            new_candles_df.set_index('timestamp', inplace=True)

                            df = pd.concat([df, new_candles_df])
                            df = df[~df.index.duplicated(keep='last')]
                            df.sort_index(inplace=True)
                            ohlcv_cache[symbol] = df.tail(config.get('exchange', {}).get('fetch_ohlcv_limit', 10000))

                    # Trigger analysis only if cooldown passed to avoid task spam
                    async with bot_lock:
                        last_analysis = bot_state.get(symbol, {}).get('last_analysis_ts', 0)

                    cooldown = config.get('timeouts', {}).get('analysis_cooldown', 12)
                    if time.time() - last_analysis >= cooldown:
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
    
    # Collect quote currencies from configured pairs and common fallback currencies
    # These are used as the quote part when matching assets to symbols
    base_currencies = set()
    
    # Add quote currencies from configured pairs
    for p in pairs_dict.keys():
        if '/' in p:
            parts = p.split('/')
            if len(parts) == 2:
                base_currencies.add(parts[1])  # quote currency
    
    # Add common fallback currencies (these are typically quote currencies)
    fallbacks = ['EUR']
    for currency in fallbacks:
        base_currencies.add(currency)
    
    base_currencies = sorted(list(base_currencies))
    
    # Ensure we have at least some base currencies
    if not base_currencies:
        base_currencies = ['EUR']

    sellable_found = False
    all_tickers = {}
    try:
        all_tickers = await exchange.fetch_tickers()
    except: pass

    logging.info(f"[{exchange_id}] Starting position sync. Free balances: {list(free_balances.keys())}")
    logging.info(f"[{exchange_id}] Active pairs: {active_pairs}")
    logging.info(f"[{exchange_id}] Base currencies: {base_currencies}")

    async def process_asset(asset, amount):
        nonlocal sellable_found
        try:
            amount = float(amount) if amount is not None else 0
        except (ValueError, TypeError):
            amount = 0
        if asset in base_currencies or amount <= 0: return

        symbol = None
        # Try to find a matching symbol for this asset
        # First priority: symbols from pairs_dict (configured pairs)
        for bc in base_currencies:
            candidate = f"{asset}/{bc}"
            if candidate in pairs_dict:
                symbol = candidate
                break
        
        # Second priority: symbols from exchange markets
        if not symbol and hasattr(exchange, 'markets') and exchange.markets:
            for bc in base_currencies:
                candidate = f"{asset}/{bc}"
                if candidate in exchange.markets:
                    symbol = candidate
                    break
            
            # Also try reverse pair (some exchanges use quote/base format)
            if not symbol:
                for bc in base_currencies:
                    candidate = f"{bc}/{asset}"
                    if candidate in exchange.markets:
                        symbol = candidate
                        break
        
        # Third priority: check if the asset itself is in active_pairs or pairs_dict
        if not symbol:
            if asset in active_pairs:
                symbol = asset
            elif asset in pairs_dict:
                symbol = asset
        
        if not symbol:
            logging.warning(f"[{asset}] Could not find matching symbol in active_pairs: {active_pairs}, config pairs: {list(pairs_dict.keys())}, exchange markets: {list(exchange.markets.keys()) if hasattr(exchange, 'markets') and exchange.markets else 'N/A'}")
            return

        logging.info(f"[{symbol}] Processing asset {asset} with amount {amount}")
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

        if is_dust:
            logging.warning(f"[{symbol}] Asset amount {amount} is below minimum thresholds (dust), but will create position for tracking")
            # Don't return - continue to create position for dust amounts too
        sellable_found = True

        avg_price = 0
        total_cost = 0
        total_fee = 0
        accumulated_amount = 0
        try:
            # Add timeout to prevent hanging on slow responses
            # Primary attempt: fetch recent user trades
            trades = []
            try:
                trades = await asyncio.wait_for(exchange.fetch_my_trades(symbol, limit=50), timeout=config.get('timeouts', {}).get('order_fetch', 10))
            except Exception:
                # Best-effort: try again with a larger limit without blocking too long
                try:
                    trades = await asyncio.wait_for(exchange.fetch_my_trades(symbol, limit=200), timeout=5)
                except Exception:
                    trades = []

            # Normalize and sort by timestamp descending
            try:
                trades = sorted(trades, key=lambda t: t.get('timestamp', 0), reverse=True)
            except Exception:
                pass

            # Helper: compute fallback fee when missing
            async def compute_estimated_fee(sym, trade_amt, trade_price):
                try:
                    fee_rate = await exchange.fetch_trading_fee(sym)
                except Exception:
                    fee_rate = config.get('exchange', {}).get('default_fee', 0.001)
                estimated_cost = trade_amt * trade_price * (fee_rate or 0)
                # Assume fee currency is quote; convert to quote value via get_fee_in_quote
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
                        # estimate fee when missing or zero
                        est_fee = await compute_estimated_fee(symbol, trade_amt, trade_price)
                        total_fee += est_fee
                    else:
                        try:
                            actual_fee = await exchange.get_fee_in_quote(symbol, fee_cost, fee_currency)
                            total_fee += actual_fee * (trade_amt / float(t.get('amount') or trade_amt))
                        except Exception:
                            # fallback to estimated fee
                            est_fee = await compute_estimated_fee(symbol, trade_amt, trade_price)
                            total_fee += est_fee

                    accumulated_amount += trade_amt
                except Exception:
                    continue

            # If trades didn't cover the expected amount, try a larger historical fetch as fallback
            if accumulated_amount > 0:
                avg_price = total_cost / accumulated_amount
                sync_threshold = float(config.get('exchange', {}).get('sync_threshold', 0.99))
                if accumulated_amount < amount * sync_threshold:
                    # Try fetching a larger set of trades (could be paginated) to recover missing fills
                    try:
                        more_trades = await asyncio.wait_for(exchange.fetch_my_trades(symbol, limit=500), timeout=10)
                        more_trades = sorted(more_trades, key=lambda t: t.get('timestamp', 0), reverse=True)
                        for t in more_trades:
                            if accumulated_amount >= amount: break
                            if t.get('side') != 'buy': continue
                            trade_amt = min(float(t.get('amount') or 0), amount - accumulated_amount)
                            trade_price = float(t.get('price') or 0)
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
                        pass

                    # Fill missing remainder using current ticker price as best-effort
                    try:
                        ticker = all_tickers.get(symbol) or await exchange.fetch_ticker(symbol)
                        curr_p = float((ticker.get('last') or 0) if ticker else 0)
                    except Exception:
                        curr_p = 0
                    if curr_p > 0 and accumulated_amount < amount:
                        rest_amount = amount - accumulated_amount
                        rest_cost = rest_amount * curr_p
                        total_cost += rest_cost
                        # approximate remaining fee
                        try:
                            est_fee = await compute_estimated_fee(symbol, rest_amount, curr_p)
                            total_fee += est_fee
                        except Exception:
                            pass
                        avg_price = total_cost / amount
            elif len(trades) == 0:
                # No trades found - use current price for the full amount
                ticker = all_tickers.get(symbol) or await exchange.fetch_ticker(symbol)
                curr_p = (ticker.get('last') or 0) if ticker else 0
                if curr_p > 0:
                    avg_price = curr_p
                    total_cost = amount * avg_price
        except Exception as e:
            logging.warning(f"[{symbol}] Error fetching trade history for sync: {e}")
            # If trade history fetch fails completely, try to use current price
            ticker = all_tickers.get(symbol) or await exchange.fetch_ticker(symbol)
            curr_p = (ticker.get('last') or 0) if ticker else 0
            if curr_p > 0:
                avg_price = curr_p
                total_cost = amount * avg_price

        if avg_price <= 0:
            ticker = all_tickers.get(symbol) or await exchange.fetch_ticker(symbol)
            avg_price = (ticker.get('last') or 0) if ticker else 0
            if avg_price > 0:
                total_cost = amount * avg_price  # Ensure total_cost is consistent

        if avg_price > 0:
            data_manager.add_position(
                symbol, avg_price, amount, total_fee,
                {"info": "launch_sync", "auto_sell_disabled": False, "strategy": "Synced", "candle_count": 0}, time.time(),
                total_base=total_cost + total_fee
            )
            logging.info(f"[{symbol}] Synced balance: {amount} at calculated avg price {format_price(avg_price)}")
        else:
            logging.warning(f"[{symbol}] Asset found in wallet but price unavailable. avg_price={avg_price}, total_cost={total_cost}")

    # Parallelize processing of all assets with a semaphore to avoid rate limits
    sync_semaphore = asyncio.Semaphore(config.get('exchange', {}).get('max_concurrent_syncs', 3))
    async def process_with_semaphore(asset, amount):
        async with sync_semaphore:
            await process_asset(asset, amount)

    await asyncio.gather(*[process_with_semaphore(a, am) for a, am in free_balances.items()])

    # Ensure symbols with positions are in active_pairs for monitoring
    open_positions = data_manager.get_open_positions()
    new_symbols = []
    for symbol in open_positions.keys():
        if symbol not in active_pairs:
            active_pairs.append(symbol)
            new_symbols.append(symbol)
            logging.info(f"[{symbol}] Added to active_pairs due to existing position")
    
    # Initialize OHLCV cache and bot_state for newly added symbols
    for symbol in new_symbols:
        async with ohlcv_lock:
            if symbol not in ohlcv_cache:
                ohlcv_cache[symbol] = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')
                logging.info(f"[{symbol}] Initialized OHLCV cache due to existing position")
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
    
    # Update global bot_state for dashboard
    async with bot_lock:
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
    # Cooldown check is now also performed before calling the wrapper for efficiency,
    # but kept here for safety.
    async with bot_lock:
        last_analysis = bot_state.get(symbol, {}).get('last_analysis_ts', 0)

    cooldown = config.get('timeouts', {}).get('analysis_cooldown', 12)
    if time.time() - last_analysis < cooldown:
        async with analysis_lock:
            if symbol in analysis_in_progress:
                analysis_in_progress.remove(symbol)
        return

    try:
        # Added timeout to prevent individual analysis tasks from locking up indefinitely
        analysis_timeout = config.get('timeouts', {}).get('analysis_timeout', 60)
        await asyncio.wait_for(
            analyze_and_trade(exchange, symbol, config, data_manager, pattern_manager, engine, device, executor),
            timeout=analysis_timeout
        )
    except asyncio.TimeoutError:
        logging.warning(f"Analysis for {symbol} timed out after {analysis_timeout}s.")
    except Exception as e:
        logging.error(f"Error in analysis for {symbol}: {e}", exc_info=True)
    finally:
        async with bot_lock:
            if symbol in bot_state:
                bot_state[symbol]['last_analysis_ts'] = time.time()
                # Schedule strategy change
                bot_state[symbol]['strategy'] = random.choice(STRATEGIES)
        async with analysis_lock:
            if symbol in analysis_in_progress:
                analysis_in_progress.remove(symbol)

async def analyze_and_trade(exchange, symbol, config, data_manager, pattern_manager, engine, device, executor=None):
    try:
        # Check suspensions
        now_ts = time.time()
        is_suspended = False
        positions = data_manager.get_position(symbol)
        if symbol in pair_suspensions:
            susp = pair_suspensions[symbol]
            if now_ts < susp.get('until', 0):
                is_suspended = True
                logging.debug(f"[{symbol}] Suspended until {susp.get('until', 0)}: {susp.get('reason')}")
            elif susp.get('reason') == 'budget':
                balance = current_balances or {}
                base_curr = symbol.split('/')[1] if '/' in symbol else symbol
                free_bal = 0
                try:
                    free_bal = balance.get(base_curr, {}).get('free', 0) if isinstance(balance.get(base_curr), dict) else balance.get(base_curr, 0) or 0
                    free_bal = float(free_bal) if free_bal is not None else 0
                except:
                    free_bal = 0
                required_amount = float(susp.get('amount_required', 0) or 0) * 1.2
                if free_bal >= required_amount:
                    logging.info(f"[{symbol}] Budget recovered. Resuming pair.")
                    del pair_suspensions[symbol]
                else:
                    is_suspended = True
            elif susp.get('reason') == 'volume_minimum':
                balance = current_balances or {}
                base_curr = symbol.split('/')[1] if '/' in symbol else symbol
                free_bal = 0
                try:
                    free_bal = balance.get(base_curr, {}).get('free', 0) if isinstance(balance.get(base_curr), dict) else balance.get(base_curr, 0) or 0
                    free_bal = float(free_bal) if free_bal is not None else 0
                except:
                    free_bal = 0
                
                min_amount = float(susp.get('min_amount', 0) or 0)
                min_cost = float(susp.get('min_cost', 0) or 0)
                
                # Get current price from OHLCV cache or bot_state
                current_price = 0
                try:
                    async with ohlcv_lock:
                        if symbol in ohlcv_cache and not ohlcv_cache[symbol].empty:
                            current_price = float(ohlcv_cache[symbol]['close'].iloc[-1] or 0)
                    if current_price == 0:
                        # Fallback to bot_state price
                        current_price = float(bot_state.get(symbol, {}).get('price', 0) or 0)
                except:
                    current_price = 0
                
                # Check if we have enough balance to meet minimum volume requirements
                if free_bal > 0 and current_price > 0:
                    # We can meet the minimum if our balance >= min_amount OR (balance * price) >= min_cost
                    can_meet_min_amount = free_bal >= min_amount
                    can_meet_min_cost = (free_bal * current_price) >= min_cost
                    
                    if can_meet_min_amount or can_meet_min_cost:
                        logging.info(f"[{symbol}] Volume minimum requirements met (balance: {free_bal}, min_amount: {min_amount}, min_cost: {min_cost}). Resuming pair.")
                        del pair_suspensions[symbol]
                    else:
                        is_suspended = True
                        logging.debug(f"[{symbol}] Still suspended: balance {free_bal} < min_amount {min_amount}, and balance*price {free_bal * current_price:.8f} < min_cost {min_cost}")
                else:
                    # If we can't determine requirements, keep suspended
                    is_suspended = True
                    logging.debug(f"[{symbol}] Volume minimum suspension: insufficient data for recovery check (free_bal={free_bal}, price={current_price})")
            else:
                del pair_suspensions[symbol]

        async with ohlcv_lock:
            if symbol not in ohlcv_cache: return
            df = ohlcv_cache[symbol].copy()

        # For symbols with existing positions, use lower candle requirement
        min_candles = config.get('trading', {}).get('min_candles_for_analysis', 250) or 250
        try:
            min_candles = int(min_candles)
        except (ValueError, TypeError):
            min_candles = 250
        if positions:  # If we have positions for this symbol, be more lenient
            min_candles = max(1, min_candles // 10)  # Use 10% of normal requirement, minimum 1
            logging.debug(f"[{symbol}] Using reduced candle requirement: {min_candles}")
        
        # Safety check for df
        df_len = len(df) if isinstance(df, pd.DataFrame) else 0
        if not isinstance(df, pd.DataFrame) or df.empty or df_len < min_candles: 
            logging.debug(f"[{symbol}] Insufficient candles ({df_len} < {min_candles}) for analysis")
            return

        loop = asyncio.get_event_loop()

        # Single Strategy Evaluation - Determined BEFORE executor call to consolidate
        pair_config = config['pairs'].get(symbol, {})
        strat = pair_config.get('strategy') or data_manager.data.get('open_positions', {}).get(symbol, [{}])[0].get('strategy') or bot_state.get(symbol, {}).get('strategy')
        aggr = pair_config.get('aggr') or data_manager.data.get('open_positions', {}).get(symbol, [{}])[0].get('aggr') or bot_state.get(symbol, {}).get('aggr')

        if not strat: strat = random.choice(STRATEGIES)
        if not aggr: aggr = random.choice(['normal', 'aggressive', 'dynamic'])

        # Consolidate indicator settings
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

        # Run analysis in one go
        if executor:
            df = await loop.run_in_executor(executor, get_signals, df, settings, False, config)
        else:
            df = get_signals(df, settings, global_config=config)

        if df.empty: return

        latest = df.iloc[-1]
        buy_candidate = latest.get('buy_signal', False)
        sell_candidate = latest.get('sell_signal', False)
        total_score = 1 if buy_candidate else (-1 if sell_candidate else 0)

        # Simple backtest profit metric for display (Vectorized-ish)
        test_df = df.tail(config.get('trading', {}).get('backtest_profit_candles', 400))
        buys = test_df[test_df['buy_signal']]['close']
        sells = test_df[test_df['sell_signal']]['close']

        profit = 0
        if not buys.empty and not sells.empty:
            # Simplified matched-trade profit
            # Check if indices are datetime64 and convert to numeric timestamps for comparison
            buys_numeric = buys.copy()
            sells_numeric = sells.copy()
            
            # Convert datetime64 index to numeric timestamp if needed
            if pd.api.types.is_datetime64_any_dtype(buys.index):
                buys_numeric.index = buys.index.astype('int64') // 10**6  # Convert nanoseconds to milliseconds
            if pd.api.types.is_datetime64_any_dtype(sells.index):
                sells_numeric.index = sells.index.astype('int64') // 10**6
            
            for b_idx, b_p in buys_numeric.items():
                future_sells = sells_numeric[sells_numeric.index > b_idx]
                if not future_sells.empty:
                    s_idx = future_sells.index[0]
                    profit += (future_sells.iloc[0] - b_p)
                    sells_numeric = sells_numeric[sells_numeric.index > s_idx]
                else: break

        # Consolidated Update State
        async with bot_lock:
            if symbol not in bot_state: bot_state[symbol] = {}
            bot_state[symbol].update({
                'strategy': strat,
                'aggr': latest.get('effective_aggr', aggr),
                'expected_profit': profit,
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

            # Reassign mc_threshold as requested (defaulting to 1.0)
            mc_threshold = config.get('mc_threshold', 1.0)

            # Ensure numeric types for comparison
            try:
                mc_score = float(mc_score) if mc_score is not None else 0
            except (ValueError, TypeError):
                mc_score = 0
            try:
                mc_threshold = float(mc_threshold) if mc_threshold is not None else 1.0
            except (ValueError, TypeError):
                mc_threshold = 1.0

            if mc_score >= mc_threshold:
                # Do not add to queue if expected profit is negative
                try:
                    profit = float(profit) if profit is not None else 0
                except (ValueError, TypeError):
                    profit = 0
                if profit <= 0:
                    logging.debug(f"[{symbol}] Buy signal validated but profit is non-positive ({profit:.4f}). Not adding to queue.")
                else:
                    # Add to buy queue instead of immediate execution
                    async with buy_queue_lock:
                        # Check if already in queue
                        if not any(item['symbol'] == symbol for item in buy_queue):
                            buy_queue.append({
                                'symbol': symbol,
                                'data': latest,
                                'expected_profit': profit,
                                'strategy': strat,
                                'candle_count': len(df),
                                'timestamp': time.time()
                            })
                            logging.info(f"[{symbol}] Buy signal validated (MC Score: {mc_score:.2f}). Added to queue (Profit: {profit:.4f}).")
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
        # Improve purchasing behavior: fetch order book and compute 50% spread
        price = None
        try:
            order_book = await exchange.fetch_order_book(symbol, limit=200)
            if order_book and order_book['bids'] and order_book['asks']:
                best_bid = order_book['bids'][0][0]
                best_ask = order_book['asks'][0][0]
                # 50% of the spread with bids and asks
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

        # Check Notional Limit
        market = exchange.markets.get(symbol)

        quote_curr = symbol.split('/')[1]
        async with bot_lock:
            balance = current_balances

        # Ensure we have a valid balance
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
            # logging.warning(f"[{symbol}] Cannot execute buy: calculated amount is {amount} (balance: {balance}, price: {price})")
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
                if "Cannot calculate adjusted amount" not in str(e):
                    raise ValueError(f"Cost comparison failed: {e}")
                else:
                    raise

            quote_curr = symbol.split('/')[1]
            quote_bal = balance.get(quote_curr)
            if isinstance(quote_bal, dict):
                free_balance = quote_bal.get('total', 0) or 0 # quick fix
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

        if order and order.get('status') == 'closed':
            await process_order_fill(order, exchange, data_manager, config, engine)
    except Exception as e:
        error_str = str(e)
        logging.error(f"Buy failed for {symbol}: {e}")
        
        # Handle volume minimum not met error - don't retry until acceptable volume can be met
        if "volume minimum not met" in error_str.lower():
            # Calculate the minimum required amount based on exchange limits
            min_required_amount = 0
            min_required_cost = 0
            if market and 'limits' in market:
                try:
                    min_amount = float(market.get('limits', {}).get('amount', {}).get('min') or 0)
                    min_cost = float(market.get('limits', {}).get('cost', {}).get('min') or 0)
                    current_price = float(price or data.get('close') or 0)
                    if current_price > 0:
                        # We need to meet either min_amount OR min_cost
                        min_required_amount = max(min_amount, min_cost / current_price) if min_cost > 0 else min_amount
                        min_required_cost = min_required_amount * current_price
                except (ValueError, TypeError):
                    min_required_amount = 0
                    min_required_cost = 0
            
            # Suspend the symbol until we have enough balance for minimum volume
            pair_suspensions[symbol] = {
                'reason': 'volume_minimum',
                'min_amount': min_required_amount,
                'min_cost': min_required_cost
            }
            logging.warning(f"[{symbol}] Suspended due to volume minimum not met. Need at least {min_required_amount} amount or {min_required_cost} {quote_curr} cost.")

async def execute_sell(exchange, symbol, data, data_manager, engine, config, force=False, strategy=None, candle_count=0):
    # Prevent concurrent sells for the same symbol: check pending_sells first
    async with pending_sells_lock:
        if symbol in pending_sells:
            logging.debug(f"[{symbol}] Skipping SELL: another sell is already in-flight for this symbol.")
            return

    # Also check pending_orders for already pending sell orders (older path)
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
        price = float(ticker.get('last') or data['close']) if ticker else float(data['close'])
    except (ValueError, TypeError):
        price = float(data['close'])

    fee_rate = await exchange.fetch_trading_fee(symbol)

    # Fetch actual balance to avoid "insufficient balance" errors due to external trades or fees
    asset = symbol.split('/')[0]
    balance = await exchange.fetch_balance()
    free_balance = 0
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

    if not force and (total_sell_amount < min_amt or (total_sell_amount * price) < min_cost):
        other_indices = [i for i in range(len(positions)) if i not in sell_lot_indices]
        # Sort by performance (closest to break-even first)
        other_indices.sort(key=lambda idx: float(price) / float(positions[idx]['entry_price']), reverse=True)

        for idx in other_indices:
            pos = positions[idx]
            # Skip if auto-sell is disabled for this lot
            if pos.get('trigger_data', {}).get('auto_sell_disabled', False):
                continue

            new_amount = total_sell_amount + float(pos['amount'])
            new_entry_cost = total_entry_cost + float(pos.get('entry_total_base', 0))
            # estimated net proceeds for the whole bundle
            new_net_proceeds = new_amount * price * (1 - float(fee_rate))

            if new_net_proceeds > new_entry_cost:
                sell_lot_indices.append(idx)
                total_sell_amount = new_amount
                total_entry_cost = new_entry_cost
                if total_sell_amount >= min_amt and (total_sell_amount * price) >= min_cost:
                    break

    # Cap total sell amount to actual free balance
    free_balance = float(free_balance) if free_balance is not None else 0
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
        # Mark sell in-flight to avoid consecutive triggers while we create the order
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
            # Ensure we clear the in-flight marker on failure to create order
            async with pending_sells_lock:
                if symbol in pending_sells:
                    pending_sells.discard(symbol)
            raise

        # If order is already closed, process it immediately
        # We call this outside the lock to avoid re-entrancy issues with process_order_fill
        if order and order.get('status') == 'closed':
            await process_order_fill(order, exchange, data_manager, config, engine)
    except Exception as e:
        logging.error(f"Aggregated sell failed for {symbol}: {e}")
        async with bot_lock:
            bot_state[symbol]['position'] = data_manager.get_position(symbol)
        # Clear any in-flight marker on unexpected failure
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
            # Ensure it's removed from pending even if already processed
            async with pending_orders_lock:
                if order_id in pending_orders:
                    meta = pending_orders.pop(order_id)
                    # Also clear pending_sells if this was a sell
                    try:
                        if meta and meta.get('side') == 'sell':
                            async with pending_sells_lock:
                                pending_sells.discard(meta.get('symbol'))
                    except Exception:
                        pass
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

    # Normalize numeric fields safely
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

    # Fallback: if fee missing or zero, estimate using trading fee rate and filled amount*price
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
        # Wait at least 2 minutes before trying to fetch order to ensure it's propagated on the exchange
        order_age = time.time() - timestamp
        if order_age < 120:  # 120 seconds = 2 minutes
            remaining_wait = 120 - order_age
            logging.info(f"[{symbol}] Order {order_id} is {order_age:.0f}s old, waiting {remaining_wait:.0f}s more before fetching...")
            await asyncio.sleep(remaining_wait)
        
        # Verify amount on exchange to account for fees deducted from the acquired asset
        try:
            verified_order = await exchange.fetch_order(order_id, symbol)
        except Exception as e:
            logging.warning(f"[{symbol}] Failed to fetch order {order_id} after 2 minute wait: {e}. Proceeding without verification.")
            verified_order = None
        
        if verified_order and verified_order.get('status') == 'closed':
            filled_amount = float(verified_order.get('filled', filled_amount) or 0)
            actual_price = float(verified_order.get('price') or verified_order.get('average', actual_price) or 0)
            cost = float(verified_order.get('cost') or (filled_amount * actual_price) or 0)
            fee_data = verified_order.get('fee')
            if fee_data:
                fee_cost = float(fee_data.get('cost', 0.0) or 0)
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
                    try:
                        await process_order_fill(order, exchange, data_manager, config, engine)
                    except TypeError as te:
                        logging.error(f"TypeError processing WS order: {te}. Attempting to sanitize order and retry. Order: {order}")
                        try:
                            sanitize_order_dict(order)
                            await process_order_fill(order, exchange, data_manager, config, engine)
                        except Exception as e2:
                            logging.exception(f"Failed to process order after sanitization: {e2}. Order: {order}")
                    except Exception as e:
                        logging.exception(f"Unexpected error processing WS order: {e}. Order: {order}")
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
        help_text.append("  Ctrl+C : Stop the bot gracefully\n")
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

async def buy_queue_processor_task(exchange, data_manager, engine, config):
    """
    Processes the buy queue every 'turn' (linked to analysis_cooldown).
    Picks the pair with the highest expected profit.
    """
    while not shutdown_event.is_set():
        cooldown = config.get('timeouts', {}).get('analysis_cooldown', 12)
        await asyncio.sleep(cooldown)

        async with buy_queue_lock:
            if not buy_queue:
                continue

            # Sort by expected profit descending
            buy_queue.sort(key=lambda x: x['expected_profit'], reverse=True)

            # Process all items in queue, starting with highest profit
            items_to_keep = []
            for item in buy_queue:
                best_buy = item
                logging.info(f"[Queue] Processing {best_buy['symbol']} (Profit: {best_buy['expected_profit']:.4f}).")

                try:
                    # Execute buy and wait for it to complete
                    await execute_buy(
                        exchange, best_buy['symbol'], best_buy['data'],
                        data_manager, engine, config,
                        strategy=best_buy['strategy'],
                        candle_count=best_buy['candle_count']
                    )
                    # Only keep in queue if execution failed (it will raise exception)
                    # If successful, don't re-queue
                except Exception as e:
                    logging.error(f"[Queue] Buy failed for {best_buy['symbol']}: {e}. Keeping in queue for retry.")
                    items_to_keep.append(best_buy)

            # Update queue with only items that failed (for retry)
            buy_queue[:] = items_to_keep

async def heartbeat_task(exchange, data_manager, engine, config):
    while not shutdown_event.is_set():
        # Cleanup stuck pending orders
        stuck_timeout = config.get('timeouts', {}).get('stuck_order_cleanup', 300)
        limit_order_timeout = 300 # 5 minutes as requested

        async with pending_orders_lock:
            now = time.time()
            stuck_candidates = []
            for oid, meta in pending_orders.items():
                timeout = limit_order_timeout if meta.get('is_limit') else stuck_timeout
                if now - meta.get('timestamp', 0) > timeout:
                    stuck_candidates.append((oid, meta))

        for oid, meta in stuck_candidates:
            symbol = meta.get('symbol')
            try:
                # Double check status with the exchange before giving up
                logging.info(f"[{symbol}] Checking status of potentially stuck order {oid}...")
                verified_order = await exchange.fetch_order(oid, symbol)
                if verified_order:
                    if verified_order.get('status') == 'open' and meta.get('is_limit'):
                        logging.info(f"[{symbol}] Cancelling unfilled limit order {oid} after timeout.")
                        await exchange.cancel_order(oid, symbol)
                        async with pending_orders_lock:
                            if oid in pending_orders: pending_orders.pop(oid)
                        continue

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

    # Discover and select pairs based on balances and 24h volume
    logging.info("Retrieving best pairs based on balances and 24h volume...")
    global discovery_pool, active_pairs, default_candle_timeframe
    try:
        # 1. Build discovery pool from available balances
        discovery_pool = await build_discovery_pool_from_balances(exchange, config)
        
        # 2. If discovery pool is empty, fallback to config pairs
        if not discovery_pool:
            discovery_pool = list(config.get('pairs', {}).keys())
        
        # 3. Fetch trade counts for the last hour for discovery pool symbols
        # This populates pair_hour_trades before trading starts
        logging.info("Fetching initial trade counts for the last hour... PLEASE WAIT... It can be taking up to three minutes...")
        num_pairs = int(config.get('max_number_of_pairs', 40))
        
        # Fetch trades for discovery pool symbols
        for symbol in discovery_pool:
            try:
                trades = await exchange.fetch_trades(symbol, limit=10)
                now = time.time()
                if symbol not in pair_hour_trades:
                    pair_hour_trades[symbol] = deque()
                pair_hour_trades[symbol].clear()
                one_hour_ago = now - 3600
                for trade_data in trades:
                    trade_time = trade_data['timestamp'] / 1000
                    if trade_time >= one_hour_ago:
                        pair_hour_trades[symbol].append(trade_time)
                    else:
                        break
                if trades:
                    pair_last_trade_time[symbol] = trades[-1]['timestamp'] / 1000
            except Exception as e:
                logging.debug(f"Failed to fetch initial trades for {symbol}: {e}")
                continue
        
        # 4. Initialize active pairs from discovery pool (only buyable pairs)
        num_pairs = int(config.get('max_number_of_pairs', 40))

        # Determine buyable quotes (non-dust free balances)
        try:
            bal = await exchange.fetch_balance()
            free_bal = bal.get('free', {}) if isinstance(bal, dict) and 'free' in bal else (bal if isinstance(bal, dict) else {})
            currencies_with_balance = []
            for c, v in free_bal.items():
                try:
                    amt = 0
                    if isinstance(v, dict):
                        amt = float(v.get('free') or v.get('total') or 0)
                    else:
                        amt = float(v or 0)
                    if amt > float(config.get('exchange', {}).get('dust_threshold_amount', 1e-6)):
                        currencies_with_balance.append(c)
                except Exception:
                    continue
        except Exception:
            currencies_with_balance = []

        buyable_quotes = set(currencies_with_balance)

        active_pairs = []

        # First, for each currency with balance, prefer pairs from discovery_pool where quote == currency
        for curr in currencies_with_balance:
            if len(active_pairs) >= num_pairs:
                break
            for sym in discovery_pool:
                if '/' not in sym: continue
                if sym not in exchange.markets: continue
                base, quote = sym.split('/')
                if quote == curr and sym not in active_pairs:
                    active_pairs.append(sym)
                    break

        # Then fill remaining slots with discovery_pool symbols whose quote is buyable
        for sym in discovery_pool:
            if len(active_pairs) >= num_pairs:
                break
            if '/' not in sym: continue
            if sym not in exchange.markets: continue
            base, quote = sym.split('/')
            if buyable_quotes and quote not in buyable_quotes:
                continue
            if sym not in active_pairs:
                active_pairs.append(sym)

        logging.info(f"Initialized discovery pool with {len(discovery_pool)} pairs.")
        logging.info(f"Initial active set: {len(active_pairs)} pairs.")

    except Exception as e:
        logging.error(f"Failed to initialize pairs: {e}")
        # Fallback to config pairs
        active_pairs = list(config.get('pairs', {}).keys())
        discovery_pool = active_pairs[:40]

    if not active_pairs:
        logging.error("No pairs could be initialized.")
        await exchange.close()
        return

    # Start UI task
    global ui_task, background_tasks, startup_complete, ohlcv_task, trades_task
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
                    # Try to find an alternative symbol format that works
                    alt_symbol = None
                    if hasattr(exchange, 'markets') and exchange.markets:
                        # Try to find the symbol in available markets
                        if symbol in exchange.markets:
                            alt_symbol = symbol
                        else:
                            # Try common variations
                            parts = symbol.split('/') if '/' in symbol else []
                            if len(parts) == 2:
                                base, quote = parts
                                variations = [
                                    f"{base}/{quote}",  # original format
                                    f"{quote}/{base}",  # reversed
                                ]
                                # Try Kraken-specific variations
                                if base == "BTC": variations.append(f"XBT/{quote}")
                                if base == "XBT": variations.append(f"BTC/{quote}")
                                # Try to find any market that contains the same assets
                                for market_symbol, market_info in exchange.markets.items():
                                    market_parts = market_symbol.split('/') if '/' in market_symbol else []
                                    if len(market_parts) == 2:
                                        market_base, market_quote = market_parts
                                        if (base == market_base and quote == market_quote) or \
                                           (base == market_quote and quote == market_base):
                                            variations.append(market_symbol)
                                
                                for var in variations:
                                    if var in exchange.markets and var != symbol:
                                        alt_symbol = var
                                        break
                    
                    if alt_symbol and alt_symbol != symbol:
                        try:
                            logging.info(f"Trying alternative symbol {alt_symbol} for {symbol}...")
                            ohlcv_res, actual_tf = await exchange.fetch_ohlcv_10k(alt_symbol, '1s', limit=10000)
                            df_1s = pd.DataFrame(ohlcv_res, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                            df_1s['timestamp'] = pd.to_datetime(df_1s['timestamp'], unit='ms')
                            for col in ['open', 'high', 'low', 'close', 'volume']:
                                df_1s[col] = pd.to_numeric(df_1s[col], errors='coerce')
                            df_1s.set_index('timestamp', inplace=True)
                            ohlcv_cache[symbol] = df_1s
                            symbol_timeframes[symbol] = actual_tf
                            logging.info(f"[{symbol}] Loaded {len(df_1s)} candles ({actual_tf}) using {alt_symbol}.")
                        except Exception as e2:
                            logging.debug(f"Alternative symbol {alt_symbol} also failed for {symbol}: {e2}")
                            # Fallback empty dataframes to avoid crashes
                            empty_df = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')
                            ohlcv_cache[symbol] = empty_df
                    else:
                        # Fallback empty dataframes to avoid crashes
                        empty_df = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')
                        ohlcv_cache[symbol] = empty_df

        await asyncio.gather(*[init_symbol(s) for s in active_pairs])

    default_candle_timeframe, _ = await exchange._get_supported_timeframe('1s')
    for symbol in active_pairs:
        symbol_timeframes.setdefault(symbol, default_candle_timeframe)

    # Keep pairs even with empty candle data for price watching and trading
    async with ohlcv_lock:
        for s in active_pairs:
            if s not in ohlcv_cache:
                # Ensure all active pairs have at least an empty dataframe
                empty_df = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')
                ohlcv_cache[s] = empty_df
                logging.warning(f"[{s}] No candle data available. Will use empty cache for price watching.")

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
        asyncio.create_task(buy_queue_processor_task(exchange, data_manager, engine, config)),
        asyncio.create_task(chart_renderer_task(config))
    ]

    # Start Global OHLCV Watcher
    ohlcv_task = asyncio.create_task(watch_ohlcv_global_task(exchange, active_pairs, config, data_manager, pattern_manager, engine, device, executor))
    
    # Start Trades Count Fetcher (fetch instead of watch)
    trades_task = asyncio.create_task(fetch_trades_count_task(exchange, config))
    
    # Start Discovery Pool Refresher
    background_tasks.append(asyncio.create_task(refresh_discovery_pool_task(exchange, config)))

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

    # Initial analysis for all pairs (Skipped as requested)
    # for symbol in active_pairs:
    #     async with analysis_lock:
    #         if symbol not in analysis_in_progress:
    #             analysis_in_progress.add(symbol)
    #             asyncio.create_task(analyze_and_trade_wrapper(exchange, symbol, config, data_manager, pattern_manager, engine, device, executor))

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

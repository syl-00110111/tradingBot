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
import threading
import queue
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
from exchange_handler2 import CCXTExchange2, MockExchange2
from indicators2 import get_signals, STRATEGIES, STRATEGY_GROUPS
from persistence2 import DataManager, CacheManager, PatternManager
from trading_engine2 import TradingEngine
from monte_carlo2 import MonteCarloEngine

# Analysis Queue and Tracking
analysis_queue = asyncio.PriorityQueue()
analysis_set = set()
analysis_tracking_lock = asyncio.Lock()

class WatcherManager:
    def __init__(self, exchange, config):
        self.exchange = exchange
        self.config = config
        self.global_watcher = None
        self.pending_reschedules = {}  # symbol -> (old_tf, new_tf)
        self.aggregation_lock = asyncio.Lock()
        self.aggregation_task = None
        self._startup_message_shown = False

    async def schedule_reschedule(self, symbol, old_tf, new_tf):
        async with self.aggregation_lock:
            self.pending_reschedules[symbol] = (old_tf, new_tf)
            if not self.aggregation_task or self.aggregation_task.done():
                self.aggregation_task = asyncio.create_task(self._process_reschedules())

    async def _process_reschedules(self):
        await asyncio.sleep(30)  # 30-second aggregation window
        async with self.aggregation_lock:
            changes = self.pending_reschedules.copy()
            self.pending_reschedules.clear()

        if not changes: return

        await self.start_global_watcher()

    async def start_global_watcher(self):
        if self.global_watcher and not self.global_watcher.done():
            self.global_watcher.cancel()

        # Build list of [symbol, timeframe] for all pairs
        watch_pairs = []
        for s in self.config['pairs']:
            tf = self.config['pairs'][s].get('timeframe', '1m')
            watch_pairs.append([s, tf])

        self.global_watcher = asyncio.create_task(watch_ohlcv_global_task(self.exchange, watch_pairs, self.config))

# Global Watcher Manager
watcher_manager = None

# Global controls for dashboard
pairs_scroll_offset = 0
selected_pair_index = 0
show_chart = False
chart_symbol = None
chart_cache = {"symbol": None, "last_update": 0, "content": None}
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
MAX_STRAT_LEN = max(len(s) for s in STRATEGIES) if STRATEGIES else 20
TIMEFRAME_SECONDS = {'1s': 1, '1m': 60, '3m': 180, '5m': 300, '15m': 900}

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

# Sound Queue and Worker for non-blocking audio
sound_queue = queue.Queue()

def sound_worker():
    while True:
        try:
            item = sound_queue.get()
            if item is None: break
            action, config = item

            system = platform.system().lower()
            if system == "windows":
                import winsound
                if action == "startup":
                    for _ in range(5):
                        winsound.Beep(random.randint(440, 880), 100)
                elif action == "buy":
                    # Use MessageBeep for better compatibility with sound cards
                    winsound.MessageBeep(winsound.MB_OK)
                    # Also try Beep as fallback/extra
                    winsound.Beep(1000, 250)
                elif action == "sell":
                    winsound.MessageBeep(winsound.MB_ICONHAND)
                    winsound.Beep(600, 250)
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

def load_config():
    path = None
    for p in ['config.json', 'config.default.json']:
        if os.path.exists(p):
            path = p
            break
    if not path:
        console.print(f"[bold red]Error: Configuration file not found.[/]")
        sys.exit(1)
    return load_config_from_path(path)

def precision_to_int(p):
    if p is None: return 8
    if isinstance(p, int): return p
    if isinstance(p, float):
        if p > 0:
            return max(0, int(-math.log10(p)))
    return 8

def format_price(price, precision=None):
    if price is None: return "-"
    if not isinstance(price, (int, float)): return str(price)
    if price == 0: return "0"
    if precision is None:
        if abs(price) < 0.000001:
            formatted = f"{price:.10f}".rstrip('0').rstrip('.')
            if len(formatted.split('.')[-1]) > 8: return f"{price:.4e}"
            return formatted
        return f"{price:.10f}".rstrip('0').rstrip('.')
    else:
        p_int = precision_to_int(precision)
        formatted = f"{price:.{p_int}f}".rstrip('0').rstrip('.')
        if (formatted == "" or formatted == "0") and price != 0:
            if abs(price) < 0.000001: return f"{price:.4e}"
            return f"{price:.10f}".rstrip('0').rstrip('.')
        return formatted if formatted != "" else "0"

def format_amt(amt, precision=None):
    if amt is None: return "-"
    if not isinstance(amt, (int, float)): return str(amt)
    if amt == 0: return "0"
    if precision is None:
        return f"{amt:.10f}".rstrip('0').rstrip('.')
    else:
        p_int = precision_to_int(precision)
        formatted = f"{amt:.{p_int}f}".rstrip('0').rstrip('.')
        if (formatted == "" or formatted == "0") and amt != 0:
            if abs(amt) < 0.000001: return f"{amt:.4e}"
            return f"{amt:.10f}".rstrip('0').rstrip('.')
        return formatted if formatted != "" else "0"

def render_ascii_chart(symbol, config):
    global chart_cache

    async def get_df():
        async with ohlcv_lock:
            if symbol in ohlcv_cache:
                return ohlcv_cache[symbol].copy()
        return None

    # This is a bit tricky because render_ascii_chart is called from make_dashboard which is sync
    # We'll use the cache or return a message if not ready.
    # In bot2.py, we'll try to keep it simple.

    df = None
    # Corrected lookup for adaptive cache keys:
    timeframe = config['pairs'].get(symbol, {}).get('timeframe', '1s')
    cache_key = f"{symbol}_{timeframe}"
    if cache_key in ohlcv_cache:
        df = ohlcv_cache[cache_key]

    if df is None or df.empty:
        return Text(f"No data available for {symbol}", style="bold red")

    df = df.tail(100)
    last_ts = int(df.index[-1].timestamp())

    if chart_cache["symbol"] == symbol and chart_cache["last_update"] == last_ts:
        return chart_cache["content"]

    plt_ascii.clear_figure()
    plt_ascii.clf()
    plt_ascii.theme('dark')
    plt_ascii.subplots(2, 1)

    plt_ascii.subplot(1, 1)
    plt_ascii.clf()
    plt_ascii.theme('dark')
    plt_ascii.title(f"K-Lines: {symbol} ({timeframe})")
    indices = list(range(len(df)))
    df_plot = df[['open', 'high', 'low', 'close']].copy()
    df_plot.columns = ['Open', 'High', 'Low', 'Close']
    df_plot.reset_index(drop=True, inplace=True)
    plt_ascii.candlestick(indices, df_plot)

    plt_ascii.subplot(2, 1)
    plt_ascii.clf()
    plt_ascii.theme('dark')
    volumes = df['volume'].tolist()
    plt_ascii.bar(indices, volumes, color='blue', label='Volume')
    plt_ascii.title("Volume")

    width = console.width - 4
    height = console.height - 20
    if width < 20: width = 20
    if height < 15: height = 15

    h_volume = max(5, height // 3)
    h_klines = height - h_volume

    plt_ascii.subplot(1, 1).plotsize(width, h_klines)
    plt_ascii.subplot(2, 1).plotsize(width, h_volume)
    content = Text.from_ansi(plt_ascii.build())

    chart_cache = {"symbol": symbol, "last_update": last_ts, "content": content}
    return content

async def input_task(config):
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
            pairs_height = console.height - 20
            if pairs_height < 3: pairs_height = 3

            if show_chart or show_help:
                if key in [readchar.key.ENTER, readchar.key.ESC, 'q', 'Q', 'h', 'H']:
                    show_chart = False
                    show_help = False
                continue

            if key == readchar.key.TAB:
                focused_panel = "logs" if focused_panel == "pairs" else "pairs"
            elif key == readchar.key.UP:
                if focused_panel == "pairs":
                    selected_pair_index = max(0, selected_pair_index - 1)
                    if selected_pair_index < pairs_scroll_offset:
                        pairs_scroll_offset = selected_pair_index
                    pairs_pause_until = time.time() + 5
                else:
                    logs_scroll_offset += 1
                    logs_pause_until = time.time() + 30
            elif key == readchar.key.DOWN:
                if focused_panel == "pairs":
                    selected_pair_index = min(len(all_pairs) - 1, selected_pair_index + 1)
                    if selected_pair_index >= pairs_scroll_offset + pairs_height:
                        pairs_scroll_offset = selected_pair_index - pairs_height + 1
                    pairs_pause_until = time.time() + 5
                else:
                    logs_scroll_offset = max(0, logs_scroll_offset - 1)
                    logs_pause_until = time.time() + 30
            elif key == readchar.key.PAGE_UP:
                if focused_panel == "pairs":
                    selected_pair_index = max(0, selected_pair_index - pairs_height)
                    pairs_scroll_offset = max(0, pairs_scroll_offset - pairs_height)
                    pairs_pause_until = time.time() + 5
                elif focused_panel == "logs":
                    logs_scroll_offset += 10
                    logs_pause_until = time.time() + 30
            elif key == readchar.key.PAGE_DOWN:
                if focused_panel == "pairs":
                    max_pairs_offset = max(0, len(all_pairs) - pairs_height)
                    selected_pair_index = min(len(all_pairs) - 1, selected_pair_index + pairs_height)
                    pairs_scroll_offset = min(max_pairs_offset, pairs_scroll_offset + pairs_height)
                    pairs_pause_until = time.time() + 5
                elif focused_panel == "logs":
                    logs_scroll_offset = max(0, logs_scroll_offset - 10)
                    logs_pause_until = time.time() + 30
            elif key == readchar.key.HOME:
                if focused_panel == "pairs":
                    selected_pair_index = 0
                    pairs_scroll_offset = 0
                    pairs_pause_until = time.time() + 5
                elif focused_panel == "logs":
                    logs_scroll_offset = max(0, len(all_logs) - 8) # 8 is log_height
                    logs_pause_until = time.time() + 30
            elif key == readchar.key.END:
                if focused_panel == "pairs":
                    max_pairs_offset = max(0, len(all_pairs) - pairs_height)
                    selected_pair_index = len(all_pairs) - 1
                    pairs_scroll_offset = max_pairs_offset
                    pairs_pause_until = time.time() + 5
                elif focused_panel == "logs":
                    logs_scroll_offset = 0
                    logs_pause_until = time.time() + 30
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

async def get_optimal_timeframe(exchange, symbol, config):
    """
    Dynamically determines the optimal timeframe for a pair.

    The decision is based on 48h volume, spread, volatility, and trades per minute,
    comparing them against thresholds defined in the configuration.

    Parameters
    ----------
    exchange : ExchangeInterface
        The exchange instance to fetch market data from.
    symbol : str
        The trading pair symbol.
    config : dict
        The bot configuration containing timeframe thresholds.

    Returns
    -------
    tf : str
        The suggested timeframe (e.g., '1m', '5m', '30m').
    score : int
        The calculated score based on market conditions.
    reasons : list of str
        List of reasons contributing to the chosen timeframe.
    """
    thresholds = config.get('timeframe_thresholds', {})

    try:
        ticker = await exchange.fetch_ticker(symbol)
        ohlcv = await exchange.fetch_ohlcv(symbol, '1h', limit=60)
        trades = await exchange.fetch_trades(symbol, limit=1000)

        # 1. Volume 48h
        volume_48h = ticker.get('quoteVolume', 0) or ticker.get('baseVolume', 0) * ticker.get('last', 1)
        vol_low = thresholds.get('volume_48h', {}).get('low', 1000)
        vol_high = thresholds.get('volume_48h', {}).get('high', 80000)

        # 2. Spread
        spread_pct = 0.5
        if ticker.get('ask') and ticker.get('bid') and ticker['bid'] > 0:
            spread = ticker['ask'] - ticker['bid']
            spread_pct = (spread / ticker['bid']) * 100
        spr_low = thresholds.get('spread_pct', {}).get('low', 0.001)
        spr_high = thresholds.get('spread_pct', {}).get('high', 0.02)

        # 3. Volatility
        volatility = 0.05
        if ohlcv and len(ohlcv) > 0:
            closes = [candle[4] for candle in ohlcv]
            volatility = (max(closes) - min(closes)) / min(closes)
        vlt_low = thresholds.get('volatility_pct', {}).get('low', 0.01)
        vlt_high = thresholds.get('volatility_pct', {}).get('high', 0.1)

        # 4. Trades per minute
        if trades:
            times = [t['timestamp'] for t in trades]
            duration_mins = (max(times) - min(times)) / 60000
            trades_per_min = len(trades) / duration_mins if duration_mins > 0 else 0
        else:
            trades_per_min = 0
        tpm_low = thresholds.get('trades_per_minute', {}).get('low', 1)
        tpm_high = thresholds.get('trades_per_minute', {}).get('high', 40)

        # Scoring logic: higher score = faster timeframe
        score = 0
        reasons = []
        if volume_48h > vol_high: score += 1; reasons.append("High Vol")
        elif volume_48h < vol_low: score -= 1; reasons.append("Low Vol")

        if spread_pct < spr_low: score += 1; reasons.append("Tight Spread")
        elif spread_pct > spr_high: score -= 1; reasons.append("Wide Spread")

        if volatility < vlt_low: score += 1; reasons.append("Stable")
        elif volatility > vlt_high: score -= 1; reasons.append("Volatile")

        if trades_per_min > tpm_high: score += 1; reasons.append("Active")
        elif trades_per_min < tpm_low: score -= 1; reasons.append("Inactive")

        if score >= 3: tf = '1s'
        elif score == 2: tf = '1m'
        elif score == 1: tf = '3m'
        elif score == 0: tf = '5m'
        else: tf = '15m'

        # logging.info(f"[{symbol}] Optimal timeframe: {tf} (Score: {score}, Reasons: {', '.join(reasons)})")
        return tf, score

    except Exception as e:
        err_msg = str(e)
        logging.warning(f"Error determining timeframe for {symbol}: {err_msg}. Defaulting to 1m.")
        return '1m', 0, [f"Error: {err_msg}"]

async def watch_ohlcv_global_task(exchange, watch_pairs, config):
    """
    Single watcher task for all symbols across potentially different timeframes.
    'watch_pairs' is a list of [symbol, timeframe].
    """
    if not watcher_manager._startup_message_shown:
        logging.info(f"[bold cyan]Starting global OHLCV watcher for {len(watch_pairs)} symbols.")
        watcher_manager._startup_message_shown = True

    while not shutdown_event.is_set():
        try:
            async for data in exchange.watch_ohlcv_for_symbols(watch_pairs):
                if shutdown_event.is_set(): break

                if isinstance(data, tuple) and len(data) == 3:
                    symbol, timeframe, candles = data
                else: continue

                async with ohlcv_lock:
                    cache_key = f"{symbol}_{timeframe}"
                    if cache_key not in ohlcv_cache:
                        ohlcv_cache[cache_key] = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')

                    df = ohlcv_cache[cache_key]
                    new_data = []
                    for candle in candles:
                        ts = pd.to_datetime(candle[0], unit='ms')
                        if ts in df.index:
                            df.loc[ts] = [float(x) for x in candle[1:]]
                        else:
                            new_data.append(candle)

                    if new_data:
                        new_df = pd.DataFrame(new_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        new_df['timestamp'] = pd.to_datetime(new_df['timestamp'], unit='ms')
                        for col in ['open', 'high', 'low', 'close', 'volume']:
                            new_df[col] = pd.to_numeric(new_df[col], errors='coerce')
                        new_df.set_index('timestamp', inplace=True)
                        df = pd.concat([df, new_df]).tail(1000)

                        ohlcv_cache[cache_key] = df
                        async with bot_lock:
                            if symbol in bot_state:
                                bot_state[symbol]['price'] = candles[-1][4]

                async with analysis_tracking_lock:
                    if symbol not in analysis_set:
                        last_anal = 0
                        async with bot_lock:
                            if symbol in bot_state:
                                last_anal = bot_state[symbol].get('last_analysis_ts', 0)

                        # Schedule analysis only if the timeframe interval has passed
                        interval = TIMEFRAME_SECONDS.get(timeframe, 60)
                        if time.time() - last_anal >= interval:
                            await analysis_queue.put((last_anal, (symbol, timeframe)))
                            analysis_set.add(symbol)
        except Exception as e:
            if not shutdown_event.is_set():
                logging.error(f"WebSocket OHLCV reconnection error: {e}")
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

    for asset, amount in free_balances.items():
        if asset in base_currencies or amount <= 0: continue

        symbol = None
        for bc in base_currencies:
            candidate = f"{asset}/{bc}"
            if candidate in pairs_dict:
                symbol = candidate
                break
        if not symbol: continue

        existing_pos_list = data_manager.get_position(symbol)
        if existing_pos_list:
            total_existing_amount = sum(p['amount'] for p in existing_pos_list)
            if abs(total_existing_amount - amount) / amount < 0.001:
                sellable_found = True
                continue

        is_dust = False
        try:
            ticker = all_tickers.get(symbol) or await exchange.fetch_ticker(symbol)
            if symbol in exchange.markets:
                m = exchange.markets[symbol]
                min_amt = m['limits']['amount']['min']
                min_cost = m['limits']['cost']['min'] or 10
                if ticker and (amount < min_amt or (amount * ticker['last']) < min_cost):
                    is_dust = True
            elif amount <= 0.000001: is_dust = True
        except: pass

        if is_dust: continue
        sellable_found = True

        curr_price = ticker['last'] if ticker else 0
        if curr_price > 0:
            try:
                my_trades = await exchange.fetch_my_trades(symbol, limit=50)
                if my_trades:
                    buys = [t for t in my_trades if t['side'] == 'buy']
                    if buys:
                        # Sort by timestamp descending to get most recent first
                        buys.sort(key=lambda x: x['timestamp'], reverse=True)

                        remaining_amount = amount
                        batches_added = 0

                        for b in buys:
                            if remaining_amount <= amount * 0.001:  # Done
                                break

                            trade_amt = b['amount']
                            # Clip trade amount to remaining wallet amount
                            take_amt = min(trade_amt, remaining_amount)

                            # Calculate fee for this portion
                            trade_fee = 0
                            if 'fee' in b and b['fee']:
                                fee_cost = b['fee'].get('cost', 0)
                                fee_currency = b['fee'].get('currency')
                                _, quote = symbol.split('/')

                                if fee_currency and fee_currency != quote:
                                    try:
                                        fticker = await exchange.fetch_ticker(f"{fee_currency}/{quote}")
                                        if fticker:
                                            fee_cost *= fticker['last']
                                    except:
                                        pass
                                # Pro-rate fee if we only take a portion of the trade
                                trade_fee = (
                                    fee_cost * take_amt / trade_amt) if trade_amt > 0 else 0

                            entry_price = b['price']
                            total_base = (take_amt * entry_price) + trade_fee

                            data_manager.add_position(symbol, entry_price, take_amt, trade_fee, {
                                                      "info": f"recovered_batch_{batches_added}"}, b['timestamp']/1000, total_base=total_base)

                            remaining_amount -= take_amt
                            batches_added += 1

                        if remaining_amount > amount * 0.01:
                            logging.warning(
                                f"[{symbol}] Could only recover {amount - remaining_amount} out of {amount} from history. Adding residue as placeholder.")
                            data_manager.add_position(symbol, curr_price, remaining_amount, 0, {
                                                      "info": "residue_placeholder"}, time.time(), total_base=remaining_amount * curr_price)
                else:
                    # No history, fallback to current price
                    data_manager.add_position(symbol, curr_price, amount, 0, {
                                              "info": "no_history_fallback"}, time.time(), total_base=amount * curr_price)
            except Exception as e:
                logging.warning(
                    f"[{symbol}] Failed to recover trade history: {e}. Using current price fallback.")
                data_manager.add_position(symbol, curr_price, amount, 0, {
                                          "info": "error_fallback"}, time.time(), total_base=amount * curr_price)
        else:
            logging.warning(
                f"[{symbol}] Asset found in wallet but price unavailable.")

    # Update global bot_state for dashboard
    async with bot_lock:
        open_positions = data_manager.get_open_positions()
        for symbol, pos_list in open_positions.items():
            if symbol not in bot_state:
                bot_state[symbol] = {}
            bot_state[symbol]['position'] = pos_list

    logging.info(f"Syncing positions from {exchange_id} API done.")

async def dedicated_analysis_task(exchange, config, data_manager, pattern_manager, engine, device):
    max_workers = config.get('max_analysis_workers', 4)
    logging.info(f"Dedicated analysis and trade task started with {max_workers} workers.")
    executor = concurrent.futures.ProcessPoolExecutor(max_workers=os.cpu_count() or 4)

    async def worker():
        while not shutdown_event.is_set():
            symbol, timeframe = None, None
            try:
                priority, (symbol, timeframe) = await asyncio.wait_for(analysis_queue.get(), timeout=1.0)
                await analyze_and_trade(exchange, symbol, timeframe, config, data_manager, pattern_manager, engine, device, executor)

                # Update last analysis timestamp
                async with bot_lock:
                    if symbol in bot_state:
                        bot_state[symbol]['last_analysis_ts'] = time.time()

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error in analysis worker: {e}")
            finally:
                if symbol:
                    async with analysis_tracking_lock:
                        if symbol in analysis_set:
                            analysis_set.remove(symbol)
                    analysis_queue.task_done()

    try:
        workers = [asyncio.create_task(worker()) for _ in range(max_workers)]
        await asyncio.gather(*workers)
    finally:
        executor.shutdown(wait=False)

def run_optimization_for_symbol_sync(symbol, config, timeframe, aggrs, strategies, df, engine, device):
    from indicators2 import get_signals
    import torch
    import pandas as pd

    best_profit = -999
    best_strat = None

    for strat in strategies:
        mode_settings = engine.get_dynamic_settings(20, 0.005)
        mode_settings['strategy'] = strat
        mode_settings['device'] = torch.device('cpu')

        try:
            res_df = get_signals(df.copy(), mode_settings, is_scan=True)
            profit = 0
            pos = None
            for i in range(len(res_df)):
                row = res_df.iloc[i]
                if row['buy_signal'] and not pos:
                    pos = row['close']
                elif row['sell_signal'] and pos:
                    profit += (row['close'] - pos)
                    pos = None
            if profit > best_profit:
                best_profit = profit
                best_strat = strat
        except: continue

    return symbol, best_strat, best_profit

async def analyze_and_trade(exchange, symbol, timeframe, config, data_manager, pattern_manager, engine, device, executor=None):
    try:
        # Check suspensions
        now_ts = time.time()
        if symbol in pair_suspensions:
            susp = pair_suspensions[symbol]
            if now_ts < susp.get('until', 0): return
            if susp.get('reason') == 'budget':
                balance = current_balances
                base_curr = symbol.split('/')[1]
                free_bal = balance.get(base_curr, {}).get('free', 0) if isinstance(balance.get(base_curr), dict) else balance.get(base_curr, 0)
                if free_bal >= susp.get('amount_required', 0) * 1.2:
                    logging.info(f"[{symbol}] Budget recovered. Resuming pair.")
                    del pair_suspensions[symbol]
                else: return
            else:
                del pair_suspensions[symbol]

        cache_key = f"{symbol}_{timeframe}"
        async with ohlcv_lock:
            if cache_key not in ohlcv_cache: return
            df = ohlcv_cache[cache_key].copy()

        if df.empty or len(df) < 20: return

        loop = asyncio.get_event_loop()

        # 1. Expert Mode indicators & Regime detection
        if executor:
            df = await loop.run_in_executor(executor, get_signals, df, {'device': device})
        else:
            df = get_signals(df, {'device': device})

        latest_base = df.iloc[-1]
        market_regime = latest_base.get('regime', 'trend_following')
        logging.debug(f"[{symbol}] Market Regime detected: {market_regime}")

        # 2. Adaptive Timeframe Discovery
        last_tf_check = config['pairs'].get(symbol, {}).get('_last_tf_check', 0)
        if time.time() - last_tf_check > 900:
            new_tf, score = await get_optimal_timeframe(exchange, symbol, config)
            config['pairs'][symbol]['_last_tf_check'] = time.time()
            if new_tf != timeframe:
                config['pairs'][symbol]['timeframe'] = new_tf
                if watcher_manager:
                    await watcher_manager.schedule_reschedule(symbol, timeframe, new_tf)

        # 3. Multi-technique Evaluation
        pair_config = config['pairs'].get(symbol, {})
        techniques = pair_config.get('techniques', [])
        if not techniques:
            # Prioritize current regime, then others
            regime_strats = STRATEGY_GROUPS.get(market_regime, [])
            other_regime = 'trend_following' if market_regime == 'mean_reversion' else 'mean_reversion'
            other_regime_strats = STRATEGY_GROUPS.get(other_regime, [])
            misc_strats = STRATEGY_GROUPS.get('other', [])

            ordered_strats = regime_strats + misc_strats + other_regime_strats
            techniques = [{"strategy": s, "aggr": ["normal", "aggressive", "dynamic"]} for s in ordered_strats]
        else:
            # Reorder existing techniques to prioritize the detected regime
            def technique_priority(t):
                strat = t.get('strategy')
                if strat in STRATEGY_GROUPS.get(market_regime, []):
                    return 0
                if strat in STRATEGY_GROUPS.get('other', []):
                    return 1
                return 2
            techniques = sorted(techniques, key=technique_priority)

        # Filtering mechanism: Prioritize current regime or generic, but keep ALL techniques
        # We don't want to drop user-configured strategies even if they aren't in a group.
        def is_relevant(t):
            strat = t.get('strategy')
            return strat in STRATEGY_GROUPS.get(market_regime, []) or strat in STRATEGY_GROUPS.get('other', [])

        filtered_techniques = [t for t in techniques if is_relevant(t)]
        other_techniques = [t for t in techniques if not is_relevant(t)]

        # Combine them, ensuring priority techniques are first
        final_techniques = filtered_techniques + other_techniques

        # Group techniques by their STRATEGY_GROUPS category
        grouped_techniques = {}
        for t in final_techniques:
            strat = t.get('strategy')
            found_group = 'other'
            for gname, strats in STRATEGY_GROUPS.items():
                if strat in strats:
                    found_group = gname
                    break
            if found_group not in grouped_techniques:
                grouped_techniques[found_group] = []
            grouped_techniques[found_group].append(t)

        ordered_groups = [market_regime]
        other_regime = 'trend_following' if market_regime == 'mean_reversion' else 'mean_reversion'
        if 'other' in grouped_techniques: ordered_groups.append('other')
        if other_regime in grouped_techniques: ordered_groups.append(other_regime)
        for gname in grouped_techniques:
            if gname not in ordered_groups: ordered_groups.append(gname)

        buy_count = 0
        sell_count = 0
        total_score = 0

        for gname in ordered_groups:
            if gname not in grouped_techniques: continue

            group_signal_found = False
            for t in grouped_techniques[gname]:
                strat = t.get('strategy')

                async with bot_lock:
                    bot_state[symbol]['strategy'] = strat

                aggr_list = t.get('aggr', ['normal'])
                if isinstance(aggr_list, str): aggr_list = [aggr_list]

                strat_tasks = []
                for a in aggr_list:
                    mode_settings = engine.get_dynamic_settings(latest_base.get('adx', 20), latest_base.get('volatility', 0.001), aggr=a)
                    mode_settings['strategy'] = strat
                    mode_settings['device'] = device
                    strat_tasks.append(loop.run_in_executor(executor, get_signals, df.copy(), mode_settings))

                if strat_tasks:
                    done_results = await asyncio.gather(*strat_tasks)
                    strat_buy = False
                    strat_sell = False
                    for res_df in done_results:
                        if res_df.empty: continue
                        latest = res_df.iloc[-1]
                        total_score += latest.get('score', 0)
                        if latest.get('buy_signal'):
                            buy_count += 1
                            strat_buy = True
                        if latest.get('sell_signal'):
                            sell_count += 1
                            strat_sell = True

                    if strat_buy or strat_sell:
                        group_signal_found = True
                        async with bot_lock:
                            bot_state[symbol]['last_strat_with_signal'] = strat
                        break # Skip remaining strategies in this group

        buy_candidate = buy_count > sell_count and buy_count > 0
        sell_candidate = sell_count > buy_count and sell_count > 0

        # Consecutive signals logic
        candle_ts = int(latest_base.name.timestamp())
        async with bot_lock:
            if symbol not in bot_state: bot_state[symbol] = {}
            prev_ts = bot_state[symbol].get('_last_candle_ts')
            consecutive_buys = bot_state[symbol].get('consecutive_buys', 0)
            consecutive_sells = bot_state[symbol].get('consecutive_sells', 0)

            if prev_ts != candle_ts:
                if buy_candidate:
                    consecutive_buys += 1
                    consecutive_sells = 0
                elif sell_candidate:
                    consecutive_sells += 1
                    consecutive_buys = 0
                else:
                    consecutive_buys = 0
                    consecutive_sells = 0
                bot_state[symbol]['_last_candle_ts'] = candle_ts

            bot_state[symbol]['consecutive_buys'] = consecutive_buys
            bot_state[symbol]['consecutive_sells'] = consecutive_sells

        # Thresholds (Stability)
        buy_signal = consecutive_buys >= 1
        sell_signal = consecutive_sells >= 1

        # Monte Carlo Validation
        if buy_signal or (sell_signal and data_manager.get_position(symbol)):
            mc = MonteCarloEngine(num_simulations=500, timeframe_candles=20)
            mc.set_device(device)
            mc_score = mc.validate_strategy(df)
            if buy_signal and mc_score < 1.1: buy_signal = False
            if sell_signal and mc_score > 0.9: sell_signal = False

        # 4. Background Optimization if no signals
        async with bot_lock:
            candles_since = bot_state[symbol].get('candles_since_last_signal', 0)
            if not buy_signal and not sell_signal:
                candles_since += 1
            else:
                candles_since = 0
            bot_state[symbol]['candles_since_last_signal'] = candles_since

            if candles_since >= config.get('no_signal_threshold', 20) and symbol not in active_scans:
                global bench_executor
                if not bench_executor: bench_executor = concurrent.futures.ProcessPoolExecutor(max_workers=os.cpu_count() or 4)
                task = loop.run_in_executor(bench_executor, run_optimization_for_symbol_sync,
                                          symbol, config, timeframe, ['normal'], STRATEGIES[:10], df, engine, device)
                active_scans[symbol] = task
                def optimization_done(fut):
                    try:
                        sym, best_s, best_p = fut.result()
                        if best_s:
                            config['pairs'][sym]['strategy'] = best_s
                            asyncio.run_coroutine_threadsafe(update_expected_profit(sym, best_p), loop)
                        if sym in active_scans: del active_scans[sym]
                    except: pass

                async def update_expected_profit(sym, profit):
                    async with bot_lock:
                        bot_state[sym]['expected_profit'] = profit

                task.add_done_callback(optimization_done)

        # Update State
        async with bot_lock:
            bot_state[symbol].update({
                'price': latest_base['close'],
                'ema_f': latest_base.get('ema_f', 0),
                'ema_s': latest_base.get('ema_s', 0),
                'rsi': latest_base.get('rsi', 0),
                'adx': latest_base.get('adx', 0),
                'volatility': latest_base.get('volatility', 0),
                'score': total_score,
                'tendency': "Bullish" if total_score > 0 else ("Bearish" if total_score < 0 else "Neutral"),
                'last_signal': 'Buy' if buy_signal else ('Sell' if sell_signal else 'Waiting')
            })

        if buy_signal:
            await execute_buy(exchange, symbol, latest_base, data_manager, engine, config)
        elif sell_signal:
            await execute_sell(exchange, symbol, latest_base, data_manager, engine, config)

    except Exception as e:
        logging.error(f"Analysis error for {symbol}: {e}")

async def execute_buy(exchange, symbol, data, data_manager, engine, config):
    global current_balances
    async with bot_lock:
        pos = data_manager.get_position(symbol)
        max_lots = config['pairs'].get(symbol, {}).get('max_lots_per_symbol') or config.get('max_lots_per_symbol', 1)
        if pos and len(pos) >= max_lots: return

    try:
        price = data['close']
        timeframe = config['pairs'].get(symbol, {}).get('timeframe', '1s')

        # Check Notional Limit
        market = exchange.markets.get(symbol)

        async with bot_lock:
            balance = current_balances
        if not balance:
            balance = await exchange.fetch_balance()
            async with bot_lock: current_balances = balance

        amount = engine.calculate_position_size(balance, price, symbol.split('/')[1], timeframe=timeframe)
        cost = amount * price

        if amount > 0:
            if market and 'limits' in market and 'cost' in market['limits'] and market['limits']['cost']['min']:
                min_notional = float(market['limits']['cost']['min'])
                if cost < min_notional:
                    amount = (min_notional / price) * 1.05 # 5% buffer
                    cost = amount * price

            base_curr = symbol.split('/')[1]
            free_balance = balance.get(base_curr, {}).get('free', 0) if isinstance(balance.get(base_curr), dict) else balance.get(base_curr, 0)

            if free_balance < cost:
                pair_suspensions[symbol] = {'reason': 'budget', 'amount_required': cost}
                return

            order = await exchange.create_order(symbol, 'buy', amount)
            if order:
                fee = order.get('fee', {}).get('cost', 0)
                final_price = order.get('price') or price
                total_base = (amount * final_price) + fee
                data_manager.add_position(symbol, final_price, amount, fee, {}, time.time(), total_base=total_base)
                logging.info(f"[{symbol}] BUY executed at {final_price}")
                play_sound("buy")
                async with bot_lock:
                    bot_state[symbol]['position'] = data_manager.get_position(symbol)
            else:
                pair_suspensions[symbol] = {'reason': 'budget', 'amount_required': cost}
    except Exception as e:
        logging.error(f"Buy failed for {symbol}: {e}")

async def execute_sell(exchange, symbol, data, data_manager, engine, config):
    async with bot_lock:
        positions = data_manager.get_position(symbol)
        if not positions: return

    price = data['close']
    fee_rate = await exchange.fetch_trading_fee(symbol)

    any_sold = False
    for i in range(len(positions) - 1, -1, -1):
        pos = positions[i]
        if engine.is_profitable(price, pos['entry_price'], fee_rate=fee_rate, entry_total_base=pos.get('entry_total_base', 0), amount=pos['amount']):
            try:
                order = await exchange.create_order(symbol, 'sell', pos['amount'])
                if order:
                    fee = order.get('fee', {}).get('cost', 0)
                    actual_price = order.get('price') or price
                    total_received = (pos['amount'] * actual_price) - fee
                    profit = total_received - pos.get('entry_total_base', 0)
                    data_manager.close_position(symbol, actual_price, fee, profit, {}, time.time(), total_base=total_received, lot_index=i)
                    logging.info(f"[{symbol}] SELL executed at {actual_price} (Profit: {profit:.2f})")
                    play_sound("sell")
                    any_sold = True
            except Exception as e:
                logging.error(f"Sell failed for {symbol} lot {i}: {e}")

    if any_sold:
        async with bot_lock:
            bot_state[symbol]['position'] = data_manager.get_position(symbol)

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
                logging.error(f"WebSocket balance reconnection error: {e}")
                await asyncio.sleep(5)
            else: break

async def watch_orders_task(exchange, data_manager):
    logging.info("WebSocket: watch_orders task started.")
    while not shutdown_event.is_set():
        try:
            async for orders in exchange.watch_orders():
                for order in orders:
                    if order['status'] == 'closed':
                        logging.info(f"Order Completed: {order['symbol']} {order['side']} @ {order['price']}")
        except Exception as e:
            if not shutdown_event.is_set():
                logging.error(f"WebSocket orders reconnection error: {e}")
                await asyncio.sleep(5)
            else: break
                # We could sync positions with data_manager here if we wanted to be
                # fully WebSocket-driven for fills.

def get_sorted_symbols(config):
    tf_priority = {'1s': 0, '1m': 1, '3m': 2, '5m': 3, '15m': 4, '30m': 5}
    all_pairs = sorted(
        [s for s in bot_state.keys() if not s.startswith("_")],
        key=lambda x: (tf_priority.get(config['pairs'].get(x, {}).get('timeframe', '5m'), 99), x)
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
    table = Table(expand=True, box=None, padding=(0, 1))
    if expert_mode:
        table.add_column("Pair", style="cyan", no_wrap=True)
        table.add_column("TF", style="yellow", no_wrap=True)
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
        table.add_column("TF", style="yellow", no_wrap=True)
        table.add_column("Price", style="magenta", no_wrap=True)
        table.add_column("Amt", style="cyan", no_wrap=True)
        table.add_column("Entry", style="magenta", no_wrap=True)
        table.add_column("Fee", style="red", no_wrap=True)
        table.add_column("B.Prof", style="bold green", no_wrap=True)
        table.add_column("Tendency", style="bold white", no_wrap=True)
        table.add_column("Signal", style="bold", no_wrap=True)
        table.add_column("Aggr", style="white", no_wrap=True)
        table.add_column("Strategy", style="bold cyan", no_wrap=True, width=MAX_STRAT_LEN)

    pairs_height = console.height - 20
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
        tf = config['pairs'].get(symbol, {}).get('timeframe', '1m')

        amt_str = "-"
        entry_str = "-"
        fee_str = "-"
        if pos:
            if isinstance(pos, list):
                total_amount = sum(p['amount'] for p in pos)
                total_cost = sum(p['entry_price'] * p['amount'] for p in pos)
                avg_entry_price = total_cost / total_amount if total_amount > 0 else 0
                total_fee = sum(p.get('entry_fee', 0) for p in pos)
                amt_str = f"{format_amt(total_amount)} ({len(pos)})"
                entry_str = format_price(avg_entry_price)
                fee_str = format_price(total_fee)
            else:
                amt_str = format_amt(pos['amount'])
                entry_str = format_price(pos['entry_price'])
                fee_str = format_price(pos.get('entry_fee', 0))

        macd_hist = data.get('macd_hist', 0)
        macd_str = f"{macd_hist:.4e}" if abs(macd_hist) < 0.001 else f"{macd_hist:.4f}"

        display_strat = data.get('last_strat_with_signal') or data.get('strategy') or config.get('pairs', {}).get(symbol, {}).get('strategy', 'N/A')

        if expert_mode:
            row_vals = [
                symbol, tf,
                f"{format_price(data.get('ema_f', 0))}/{format_price(data.get('ema_s', 0))}",
                macd_str,
                f"{data.get('rsi', 0):.2f}",
                f"{data.get('volatility', 0):.6f}/{data.get('adx', 0):.2f}",
                str(data.get('score', 0)),
                f"{data.get('expected_profit', 0):.4f}",
                data.get('aggr', 'N/A'),
                str(display_strat)
            ]
        else:
            row_vals = [
                symbol, tf,
                format_price(data.get('price')),
                amt_str, entry_str, fee_str,
                f"{data.get('expected_profit', 0):.4f}",
                data.get('tendency', 'Neutral'),
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
    status_text.append("TAB: Switch | Arrows: Scroll | H: Help | X: Expert | M: Marquee | Exit: Ctrl+C", style="bold red")

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
        # Start Live immediately but without screen=True to allow startup logs to be visible
        # or use a simplified layout during startup.
        with Live(make_dashboard(config), refresh_per_second=4, screen=False) as live:
            while not startup_complete and not shutdown_event.is_set():
                live.update(make_dashboard(config))
                await asyncio.sleep(0.5)

            # Switch to screen mode once startup is complete
            # We have to close the old live and start a new one to change screen=True
            pass

        if shutdown_event.is_set(): return

        with Live(make_dashboard(config), refresh_per_second=4, screen=True) as live:
            while not shutdown_event.is_set():
                live.update(make_dashboard(config))
                await asyncio.sleep(0.25)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.info(f"[red]Dashboard error: {e}")

async def heartbeat_task():
    while not shutdown_event.is_set():
        await asyncio.sleep(30)

async def main():
    parser = argparse.ArgumentParser(description='CCXT Pro Trading Bot v2 (Asynchronous)')
    parser.add_argument('--no-gpu', action='store_true', help='Disable GPU acceleration (force CPU)')
    parser.add_argument('--fast-start', action='store_true', help='Skip fetching initial candles')

    args = parser.parse_args()

    # Hardware Acceleration Detection
    global device, gpu_enabled, use_mkldnn
    use_mkldnn = False
    if args.no_gpu:
        device = torch.device('cpu')
        gpu_enabled = False
    else:
        if torch.cuda.is_available():
            device = torch.device('cuda')
            gpu_enabled = True
        elif torch.backends.mkldnn.is_available():
            device = torch.device('cpu')
            use_mkldnn = True
            torch.backends.mkldnn.enabled = True
            os.environ['OMP_NUM_THREADS'] = '1'
            os.environ['MKL_NUM_THREADS'] = '1'
            torch.set_num_threads(1)
            gpu_enabled = True
        elif hasattr(torch, 'vulkan') and torch.vulkan.is_available():
            device = torch.device('vulkan')
            gpu_enabled = True
        elif torch.cuda.is_available() and hasattr(torch.version, 'hip') and torch.version.hip:
            device = torch.device('cuda')
            gpu_enabled = True
        elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            device = torch.device('mps')
            gpu_enabled = True
        else:
            try:
                import intel_extension_for_pytorch as ipex
                if torch.xpu.is_available():
                    device = torch.device('xpu')
                    gpu_enabled = True
                else: raise Exception()
            except:
                device = torch.device('cpu')
                gpu_enabled = False

    config = load_config()

    api_creds = {}
    if os.path.exists('api.json'):
        with open('api.json', 'r') as f: api_creds = json.load(f)

    exchange_id = api_creds.get('exchange_id')
    if not exchange_id:
        logging.error("No exchange found. Check your api.json file.")
    
    exchange = CCXTExchange2(exchange_id,
                             api_creds.get('api_key') or config.get('api_key'),
                             api_creds.get('api_secret') or config.get('api_secret'))
        
    logging.info(f"Connecting to {exchange_id}...")
    await exchange.load_markets()

    data_manager = DataManager()
    pattern_manager = PatternManager()
    engine = TradingEngine(config)

    if 'pairs' not in config:
        config['pairs'] = {}

    pairs_from_file = []
    if os.path.exists('pairs.txt'):
        with open('pairs.txt', 'r') as f:
            pairs_from_file = [line.strip() for line in f if line.strip()]

    for p in pairs_from_file:
        if p not in config['pairs']:
            config['pairs'][p] = {}

    pairs = list(config['pairs'].keys())
    if not pairs:
        logging.error("No pairs found in config or pairs.txt")
        return

    # Start UI task
    global ui_task, background_tasks, startup_complete
    ui_task = asyncio.create_task(run_dashboard(config))

    # Initial Batch
    for symbol in pairs:
        pair_cfg = config['pairs'][symbol]
        techniques_cfg = pair_cfg.get('techniques', [])
        if not techniques_cfg:
            techniques_cfg = [{"strategy": s, "aggr": ["normal", "aggressive", "dynamic"]} for s in STRATEGIES]

        bot_state[symbol] = {
            'price': 0, 'rsi': 0, 'tendency': 'Neutral',
            'last_signal': 'Init',
            'position': data_manager.get_position(symbol),
            'aggr': techniques_cfg[0].get('aggr', ['normal'])[0] if isinstance(techniques_cfg[0].get('aggr'), list) else techniques_cfg[0].get('aggr', 'normal'),
            'strategy': techniques_cfg[0].get('strategy', 'N/A'),
            'strategies': [t.get('strategy') for t in techniques_cfg],
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
            tf = config['pairs'].get(symbol, {}).get('timeframe', '1m')
            ohlcv_cache[f"{symbol}_{tf}"] = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')
    else:
        logging.info(f"[bold cyan]Fetching initial candles for {len(pairs)} pairs...")
        semaphore = asyncio.Semaphore(5)

        async def init_symbol(symbol):
            async with semaphore:
                try:
                    tf = config['pairs'].get(symbol, {}).get('timeframe', '1m')
                    logging.info(f"Fetching candles for {symbol} ({tf})...")
                    ohlcv = await exchange.fetch_ohlcv(symbol, tf, limit=500)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                    for col in ['open', 'high', 'low', 'close', 'volume']:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                    df.set_index('timestamp', inplace=True)
                    ohlcv_cache[f"{symbol}_{tf}"] = df
                    logging.info(f"[{symbol}] Loaded {len(df)} candles.")
                except Exception as e:
                    logging.error(f"Failed to load candles for {symbol}: {e}")
                    ohlcv_cache[f"{symbol}_{tf}"] = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')

        await asyncio.gather(*[init_symbol(s) for s in pairs])

    # Seed analysis queue with initial pairs (priority 0)
    async with analysis_tracking_lock:
        for symbol in pairs:
            tf = config['pairs'].get(symbol, {}).get('timeframe', '1m')
            await analysis_queue.put((0, (symbol, tf)))
            analysis_set.add(symbol)

    # Start WebSocket Tasks
    logging.info("[bold green]Starting WebSocket tasks...")
    background_tasks = [
        asyncio.create_task(watch_balance_task(exchange, data_manager)),
        asyncio.create_task(watch_orders_task(exchange, data_manager)),
        asyncio.create_task(input_task(config)),
        asyncio.create_task(heartbeat_task())
    ]

    # Initialize Watcher Manager
    global watcher_manager
    watcher_manager = WatcherManager(exchange, config)

    # Start Global OHLCV Watcher
    await watcher_manager.start_global_watcher()

    # Ensure all watchers are setup (Wait a bit for connections to stabilize)
    await asyncio.sleep(2)

    # Now that watchers are set up, perform initial sync and balance retrieval
    try:
        logging.info("Retrieving initial balances...")
        initial_balance = await exchange.fetch_balance()
        async with bot_lock:
            global current_balances
            current_balances = initial_balance
    except Exception as e:
        logging.info(f"[yellow]Warning: Could not fetch initial balances: {e}")

    # Synchronizing positions from the exchange API
    logging.info(f"Synchronizing positions from the {exchange_id.capitalize()} API...")
    await sync_live_positions(exchange, data_manager, config)

    # Dedicated analysis/trade worker
    background_tasks.append(asyncio.create_task(dedicated_analysis_task(exchange, config, data_manager, pattern_manager, engine, device)))

    # Wait a tad bit before dropping the message startup complete since the previous task can be taking the lead sometime
    await asyncio.sleep(4)
    startup_complete = True
    logging.info("[bold green]Bot v2 fully operational.")

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

        # Cancel all background tasks
        all_tasks = background_tasks.copy()

        # Cancel UI task first to restore terminal sooner
        if ui_task:
            ui_task.cancel()
            # No await here, we want to proceed with other cancellations
            # and the final console.clear() as fast as possible.

        if watcher_manager and watcher_manager.global_watcher:
            all_tasks.append(watcher_manager.global_watcher)
        if watcher_manager and watcher_manager.aggregation_task:
            all_tasks.append(watcher_manager.aggregation_task)

        for t in all_tasks:
            if not t.done():
                t.cancel()

        # Wait for tasks to finish (with timeout)
        if all_tasks:
            try:
                await asyncio.wait(all_tasks, timeout=3)
            except: pass

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

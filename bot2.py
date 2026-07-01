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

# Global analysis tracking to avoid overlapping
analysis_in_progress = set()
analysis_lock = asyncio.Lock()

# Global Watcher Task
global_watcher_task = None

# Track orders placed by the bot to process them via WebSocket confirmation
pending_orders = {} # order_id -> metadata_dict
pending_orders_lock = asyncio.Lock()

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

    df = None
    if symbol in ohlcv_cache:
        df = ohlcv_cache[symbol]

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
    plt_ascii.title(f"K-Lines: {symbol} (1s)")
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
            pairs_height = console.height - 20
            if pairs_height < 3: pairs_height = 3

            if show_chart or show_help:
                if key in [readchar.key.ENTER, readchar.key.ESC, 'q', 'Q', 'h', 'H']:
                    show_chart = False
                    show_help = False
                elif show_chart and chart_symbol and key.lower() == 'b':
                    price = bot_state.get(chart_symbol, {}).get('price', 0)
                    if price > 0:
                        logging.info(f"[Manual] Triggering BUY for {chart_symbol}")
                        asyncio.create_task(execute_buy(exchange, chart_symbol, {'close': price}, data_manager, engine, config, manual=True))
                elif show_chart and chart_symbol and key.lower() == 's':
                    price = bot_state.get(chart_symbol, {}).get('price', 0)
                    if price > 0:
                        logging.info(f"[Manual] Triggering SELL for {chart_symbol}")
                        asyncio.create_task(execute_sell(exchange, chart_symbol, {'close': price}, data_manager, engine, config, force=True))
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

async def watch_ohlcv_global_task(exchange, watch_pairs, config, data_manager, pattern_manager, engine, device, executor):
    """
    Single watcher task for all symbols.
    'watch_pairs' is a list of [symbol, timeframe] where timeframe is always 1s.
    """
    logging.info(f"[bold cyan]Starting global OHLCV watcher for {len(watch_pairs)} symbols.")

    while not shutdown_event.is_set():
        try:
            async for data in exchange.watch_ohlcv_for_symbols(watch_pairs):
                if shutdown_event.is_set(): break

                if isinstance(data, tuple) and len(data) == 3:
                    symbol, timeframe, candles = data
                else: continue

                async with ohlcv_lock:
                    if symbol not in ohlcv_cache:
                        ohlcv_cache[symbol] = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')

                    df = ohlcv_cache[symbol]

                    new_candles_df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    new_candles_df['timestamp'] = pd.to_datetime(new_candles_df['timestamp'], unit='ms')
                    new_candles_df.set_index('timestamp', inplace=True)

                    df = pd.concat([df, new_candles_df])
                    df = df[~df.index.duplicated(keep='last')]
                    df.sort_index(inplace=True)
                    ohlcv_cache[symbol] = df.tail(10000)

                    async with bot_lock:
                        if symbol in bot_state:
                            bot_state[symbol]['price'] = candles[-1][4]

                # Trigger analysis
                async with analysis_lock:
                    if symbol not in analysis_in_progress:
                        analysis_in_progress.add(symbol)
                        asyncio.create_task(analyze_and_trade_wrapper(exchange, symbol, config, data_manager, pattern_manager, engine, device, executor))

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
            if abs(total_existing_amount - amount) / amount < 0.001:
                sellable_found = True
                return

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

        if is_dust: return
        sellable_found = True

        avg_price = 0
        total_cost = 0
        accumulated_amount = 0
        try:
            # Add timeout to prevent hanging on slow responses
            trades = await asyncio.wait_for(exchange.fetch_my_trades(symbol, limit=50), timeout=10)
            trades.sort(key=lambda t: t['timestamp'], reverse=True)

            for t in trades:
                if t['side'] == 'buy':
                    remaining_to_fill = amount - accumulated_amount
                    if remaining_to_fill <= 0: break

                    trade_amt = min(t['amount'], remaining_to_fill)
                    total_cost += trade_amt * t['price']
                    accumulated_amount += trade_amt

            if accumulated_amount > 0:
                avg_price = total_cost / accumulated_amount
                if accumulated_amount < amount * 0.99:
                    ticker = all_tickers.get(symbol) or await exchange.fetch_ticker(symbol)
                    curr_p = ticker['last'] if ticker else 0
                    if curr_p > 0:
                        rest_amount = amount - accumulated_amount
                        total_cost += rest_amount * curr_p
                        avg_price = total_cost / amount
        except Exception as e:
            logging.warning(f"[{symbol}] Error fetching trade history for sync: {e}")

        if avg_price <= 0:
            ticker = all_tickers.get(symbol) or await exchange.fetch_ticker(symbol)
            avg_price = ticker['last'] if ticker else 0

        if avg_price > 0:
            data_manager.add_position(
                symbol, avg_price, amount, 0,
                {"info": "launch_sync", "auto_sell_disabled": True}, time.time(),
                total_base=amount * avg_price
            )
            logging.info(f"[{symbol}] Synced balance: {amount} at calculated avg price {format_price(avg_price)}")
        else:
            logging.warning(f"[{symbol}] Asset found in wallet but price unavailable.")

    # Parallelize processing of all assets with a semaphore to avoid rate limits
    sync_semaphore = asyncio.Semaphore(3)
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
    try:
        await analyze_and_trade(exchange, symbol, config, data_manager, pattern_manager, engine, device, executor)
        async with bot_lock:
            if symbol in bot_state:
                bot_state[symbol]['last_analysis_ts'] = time.time()
    finally:
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
        if executor:
            # We force CPU for subprocesses to avoid CUDA fork issues
            df = await loop.run_in_executor(executor, get_signals, df, {'device': torch.device('cpu')})
        else:
            df = get_signals(df, {'device': device})

        latest_base = df.iloc[-1]

        # Single Strategy Evaluation + Random Scan
        pair_config = config['pairs'].get(symbol, {})
        current_strat = pair_config.get('strategy') or STRATEGIES[0]
        current_aggr = pair_config.get('aggr', 'dynamic')

        # Randomly select a new technique to explore
        random_strat = random.choice(STRATEGIES)
        random_aggr = random.choice(['normal', 'aggressive', 'dynamic'])

        techniques = [
            {'strategy': current_strat, 'aggr': current_aggr},
            {'strategy': random_strat, 'aggr': random_aggr}
        ]

        async def evaluate_technique(t):
            strat = t.get('strategy')
            aggr = t.get('aggr')
            mode_settings = engine.get_dynamic_settings(latest_base.get('adx', 20), latest_base.get('volatility', 0.001), aggr=aggr)
            mode_settings['strategy'] = strat
            mode_settings['device'] = torch.device('cpu')

            res_df = await loop.run_in_executor(executor, get_signals, df, mode_settings)
            if not res_df.empty:
                latest = res_df.iloc[-1]
                # Simple backtest profit metric (last 100 candles)
                profit = 0
                test_df = res_df.tail(100)
                pos = None
                for _, row in test_df.iterrows():
                    if row['buy_signal'] and pos is None:
                        pos = row['close']
                    elif row['sell_signal'] and pos is not None:
                        profit += (row['close'] - pos)
                        pos = None
                return {
                    'latest': latest,
                    'profit': profit,
                    'strategy': strat,
                    'aggr': mode_settings.get('effective_aggr', aggr)
                }
            return None

        eval_results = await asyncio.gather(*[evaluate_technique(t) for t in techniques])
        valid_results = [r for r in eval_results if r is not None]

        if not valid_results: return

        # Result for current strategy (index 0)
        current_res = valid_results[0]
        best_res = current_res

        # If random technique (index 1) performed better, switch to it
        if len(valid_results) > 1:
            random_res = valid_results[1]
            if random_res['profit'] > current_res['profit']:
                best_res = random_res
                # Update config with the better technique
                config['pairs'][symbol]['strategy'] = best_res['strategy']
                config['pairs'][symbol]['aggr'] = best_res['aggr']

        latest = best_res['latest']
        buy_candidate = latest.get('buy_signal', False)
        sell_candidate = latest.get('sell_signal', False)
        total_score = 1 if buy_candidate else (-1 if sell_candidate else 0)

        async with bot_lock:
            if symbol not in bot_state: bot_state[symbol] = {}
            bot_state[symbol].update({
                'strategy': best_res['strategy'],
                'aggr': best_res['aggr'],
                'expected_profit': best_res['profit']
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
            await execute_buy(exchange, symbol, latest, data_manager, engine, config)
        elif sell_candidate:
            await execute_sell(exchange, symbol, latest, data_manager, engine, config)

    except Exception as e:
        logging.error(f"Analysis error for {symbol}: {e}")

async def execute_buy(exchange, symbol, data, data_manager, engine, config, manual=False):
    global current_balances

    # Check for pending orders to avoid duplicates
    async with pending_orders_lock:
        for po in pending_orders.values():
            if po['symbol'] == symbol and po['side'] == 'buy':
                return

    async with bot_lock:
        pos = data_manager.get_position(symbol)
        max_lots = config['pairs'].get(symbol, {}).get('max_lots_per_symbol') or config.get('max_lots_per_symbol', 1)
        if pos is not None and len(pos) >= max_lots:
            if manual: logging.warning(f"[{symbol}] Manual BUY ignored: max_lots_per_symbol ({max_lots}) reached.")
            return

        open_positions = data_manager.get_open_positions()
        max_open = config.get('max_open_positions', 10)
        if symbol not in open_positions and len(open_positions) >= max_open:
            if manual: logging.warning(f"[{symbol}] Manual BUY ignored: max_open_positions ({max_open}) reached.")
            return

    try:
        price = data['close']

        # Check Notional Limit
        market = exchange.markets.get(symbol)

        async with bot_lock:
            balance = current_balances
        if not balance:
            balance = await exchange.fetch_balance()
            async with bot_lock: current_balances = balance

        amount = engine.calculate_position_size(balance, price, symbol.split('/')[1])
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
            if order and 'id' in order:
                async with pending_orders_lock:
                    pending_orders[str(order['id'])] = {
                        'symbol': symbol,
                        'side': 'buy',
                        'timestamp': time.time(),
                        'trigger_data': {}
                    }
            else:
                pair_suspensions[symbol] = {'reason': 'budget', 'amount_required': cost}
    except Exception as e:
        logging.error(f"Buy failed for {symbol}: {e}")

async def execute_sell(exchange, symbol, data, data_manager, engine, config, force=False):
    # Check for pending orders to avoid duplicates
    async with pending_orders_lock:
        for po in pending_orders.values():
            if po['symbol'] == symbol and po['side'] == 'sell':
                return

    async with bot_lock:
        positions = data_manager.get_position(symbol)
        if not positions: return

    # Use ticker price if possible for more accurate profitability check
    try:
        ticker = await exchange.fetch_ticker(symbol)
        price = ticker['last'] if ticker else data['close']
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
        order = await exchange.create_order(symbol, 'sell', total_sell_amount)
        if order and 'id' in order:
            async with pending_orders_lock:
                pending_orders[str(order['id'])] = {
                    'symbol': symbol,
                    'side': 'sell',
                    'sell_lot_indices': sell_lot_indices,
                    'timestamp': time.time(),
                    'trigger_data': {}
                }
    except Exception as e:
        logging.error(f"Aggregated sell failed for {symbol}: {e}")
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

async def watch_orders_task(exchange, data_manager, config, engine):
    logging.info("WebSocket: watch_orders task started.")
    while not shutdown_event.is_set():
        try:
            async for orders in exchange.watch_orders():
                for order in orders:
                    if order['status'] == 'closed':
                        order_id = str(order['id'])
                        meta = None
                        async with pending_orders_lock:
                            if order_id in pending_orders:
                                meta = pending_orders.pop(order_id)

                        if meta:
                            symbol = meta['symbol']
                            side = meta['side']
                            filled_amount = order.get('filled', 0.0)
                            actual_price = order.get('price') or order.get('average', 0.0)
                            cost = order.get('cost') or (filled_amount * actual_price)

                            fee_cost = order.get('fee', {}).get('cost', 0.0)
                            fee_currency = order.get('fee', {}).get('currency')
                            total_fee = await exchange.get_fee_in_quote(symbol, fee_cost, fee_currency)

                            if side == 'buy':
                                total_val = cost + total_fee
                                data_manager.add_position(symbol, actual_price, filled_amount, total_fee, meta['trigger_data'], meta['timestamp'], total_base=total_val)

                                _, quote = symbol.split('/')
                                logging.info(f"[{symbol}] BUY executed at {format_price(actual_price)} (Filled: {format_amt(filled_amount)}, Spent: {format_price(total_val)} {quote})")
                                play_sound("buy")

                            elif side == 'sell':
                                total_net_received = cost - total_fee
                                sell_lot_indices = meta['sell_lot_indices']
                                sell_lot_indices.sort(reverse=True)

                                async with bot_lock:
                                    positions = data_manager.get_position(symbol)
                                    if not positions: continue

                                    remaining_filled = filled_amount
                                    remaining_net_received = total_net_received
                                    remaining_fee = total_fee
                                    total_entry_cost_of_filled = 0.0

                                    for idx, i in enumerate(sell_lot_indices):
                                        if remaining_filled <= 1e-10: break
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
                                            symbol, actual_price, current_lot_fee, lot_profit, meta['trigger_data'], time.time(),
                                            total_base=current_lot_received, lot_index=i, amount=lot_close_amt
                                        )

                                        remaining_filled -= lot_close_amt
                                        remaining_net_received -= current_lot_received
                                        remaining_fee -= current_lot_fee

                                    actual_total_profit = total_net_received - total_entry_cost_of_filled
                                    _, quote = symbol.split('/')
                                    logging.info(f"[{symbol}] Aggregated SELL executed at {format_price(actual_price)} (Filled: {format_amt(filled_amount)}, Profit: {format_price(actual_total_profit)}, Received: {format_price(total_net_received)} {quote})")
                                    play_sound("sell")

                                    # Post-sale dust cleanup
                                    if await is_pair_dust(symbol, exchange, config):
                                        data_manager.clear_positions(symbol)
                                        logging.info(f"[{symbol}] Remaining balance is dust. Clearing open positions.")

                            async with bot_lock:
                                if symbol not in bot_state: bot_state[symbol] = {}
                                bot_state[symbol]['position'] = data_manager.get_position(symbol)
                        else:
                            # logging.info(f"External Order Completed: {order['symbol']} {order['side']} @ {order['price']}")
                            pass
        except Exception as e:
            if not shutdown_event.is_set():
                logging.error(f"WebSocket orders reconnection error: {e}")
                await asyncio.sleep(5)
            else: break
                # We could sync positions with data_manager here if we wanted to be
                # fully WebSocket-driven for fills.

async def is_pair_dust(symbol, exchange, config):
    """
    Checks if the remaining balance for a symbol is considered 'dust'
    based on exchange minimums.
    """
    asset = symbol.split('/')[0]

    # We do NOT use bot_lock here because this is called within blocks
    # that ALREADY hold bot_lock in watch_orders_task, avoiding deadlocks.
    bal_data = current_balances
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
    if not market: return False

    limits = market.get('limits', {})
    min_amt = limits.get('amount', {}).get('min') or 0
    min_cost = limits.get('cost', {}).get('min') or 0

    # Use price from bot_state if available (updated by WebSocket)
    price = bot_state.get(symbol, {}).get('price', 0)

    if amount < min_amt: return True
    if price > 0 and (amount * price) < min_cost: return True

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
                amt_str = f"{format_amt(total_amount)} ({len(pos)})"
                entry_str = format_price(avg_entry_price)
                fee_str = f"{format_price(total_fee)} {quote}"
            else:
                amt_str = format_amt(pos['amount'])
                entry_str = format_price(pos['entry_price'])
                fee_str = f"{format_price(pos.get('entry_fee', 0))} {quote}"

        macd_hist = data.get('macd_hist', 0)
        macd_str = f"{macd_hist:.4e}" if abs(macd_hist) < 0.001 else f"{macd_hist:.4f}"

        display_strat = data.get('strategy') or config.get('pairs', {}).get(symbol, {}).get('strategy', 'N/A')

        if expert_mode:
            row_vals = [
                symbol,
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
                symbol,
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

    logging.info("[bold cyan]System initialization started...")

    # Initial Batch
    for symbol in pairs:
        pair_cfg = config['pairs'][symbol]
        strat = pair_cfg.get('strategy') or STRATEGIES[0]
        aggr = pair_cfg.get('aggr', 'dynamic')

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
        asyncio.create_task(heartbeat_task())
    ]

    # Start Global OHLCV Watcher
    watch_pairs = [[s, '1s'] for s in pairs]
    global_watcher_task = asyncio.create_task(watch_ohlcv_global_task(exchange, watch_pairs, config, data_manager, pattern_manager, engine, device, executor))

    # Ensure all watchers are setup (Wait a bit for connections to stabilize)
    await asyncio.sleep(2)

    # Now that watchers are set up, perform initial sync and balance retrieval
    try:
        logging.info("Retrieving initial balances...")
        # Add timeout to balance retrieval
        initial_balance = await asyncio.wait_for(exchange.fetch_balance(), timeout=30)
        async with bot_lock:
            global current_balances
            current_balances = initial_balance
    except Exception as e:
        logging.info(f"[yellow]Warning: Could not fetch initial balances: {e}")

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
    await asyncio.sleep(4)
    startup_complete = True
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

        # Cancel all background tasks
        all_tasks = background_tasks.copy()

        # Cancel UI task first to restore terminal sooner
        if ui_task:
            ui_task.cancel()

        if global_watcher_task:
            all_tasks.append(global_watcher_task)

        for t in all_tasks:
            if not t.done():
                t.cancel()

        # Wait for tasks to finish (with timeout)
        if all_tasks:
            try:
                await asyncio.wait(all_tasks, timeout=3)
            except: pass

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

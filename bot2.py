# CCXT Pro Trading Bot v2 (Asynchronous)
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import asyncio
import json
import time
import logging
import argparse
import os
import sys
import platform
import random
import math
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
from indicators2 import get_signals, STRATEGIES
from persistence2 import DataManager, CacheManager, PatternManager
from trading_engine2 import TradingEngine
from monte_carlo2 import MonteCarloEngine

# Analysis Queue
analysis_queue = asyncio.Queue()

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

# Marquee Timing Control
last_marquee_update = 0
pairs_pause_until = 0
logs_pause_until = 0
status_scroll_index = 0

def play_sound(action, config=None):
    system = platform.system().lower()
    try:
        if system == "windows":
            import winsound
            if action == "startup":
                num_blips = 5
                for _ in range(num_blips):
                    freq = random.randint(200, 1400)
                    dur = random.randint(50, 400)
                    winsound.Beep(freq, dur)
                return
            frequency = 800 if action == "buy" else 1800
            winsound.Beep(frequency, 240)
        else:
            if action == "startup":
                sys.stdout.write("\a"); sys.stdout.flush()
                return
            bell_char = "\a" if action == "buy" else "\a\a"
            sys.stdout.write(bell_char)
            sys.stdout.flush()
    except Exception: pass

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

        if "Bot v2 fully operational." in msg:
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
            key = await loop.run_in_executor(None, readchar.readkey)

            if key == readchar.key.CTRL_C:
                shutdown_event.set()
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
                    logs_pause_until = time.time() + 5
            elif key == readchar.key.DOWN:
                if focused_panel == "pairs":
                    selected_pair_index = min(len(all_pairs) - 1, selected_pair_index + 1)
                    if selected_pair_index >= pairs_scroll_offset + pairs_height:
                        pairs_scroll_offset = selected_pair_index - pairs_height + 1
                    pairs_pause_until = time.time() + 5
                else:
                    logs_scroll_offset = max(0, logs_scroll_offset - 1)
                    logs_pause_until = time.time() + 5
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

        except Exception as e:
            logging.error(f"Input error: {e}")
        await asyncio.sleep(0.1)

async def get_optimal_timeframe(exchange, symbol, config):
    try:
        ticker = await exchange.fetch_ticker(symbol)
        if not ticker: return '1m', 0
        ohlcv_1h = await exchange.fetch_ohlcv(symbol, '1h', limit=24)

        volume_48h = ticker.get('quoteVolume', 0) or ticker.get('baseVolume', 0) * ticker.get('last', 1)

        spread_pct = 0.5
        if ticker.get('ask') and ticker.get('bid') and ticker['bid'] > 0:
            spread = ticker['ask'] - ticker['bid']
            spread_pct = (spread / ticker['bid']) * 100

        volatility = 0.05
        if ohlcv_1h and len(ohlcv_1h) > 0:
            closes = [candle[4] for candle in ohlcv_1h]
            volatility = (max(closes) - min(closes)) / min(closes)

        score = 0
        if volume_48h > 1200000: score += 2
        elif volume_48h > 120000: score += 1
        if spread_pct < 0.01: score += 1
        if volatility > 0.05: score += 1

        if score >= 3: tf = '1s'
        elif score == 2: tf = '1m'
        elif score == 1: tf = '3m'
        elif score == 0: tf = '5m'
        else: tf = '15m'
        return tf, score
    except Exception as e:
        logging.warning(f"Error determining timeframe for {symbol}: {e}")
        return '1m', 0

async def watch_ohlcv_timeframe_task(exchange, symbols, timeframe, config):
    active_symbols = set(symbols)
    logging.info(f"Starting OHLCV watcher for {timeframe} group: {list(active_symbols)}")

    async for data in exchange.watch_ohlcv_for_symbols(list(active_symbols), timeframe):
        if shutdown_event.is_set(): break

        if isinstance(data, tuple) and len(data) == 2:
            symbol, candles = data
        else: continue

        # Adaptive migration check
        target_tf = config['pairs'].get(symbol, {}).get('timeframe', timeframe)
        if target_tf != timeframe:
            if symbol in active_symbols:
                active_symbols.remove(symbol)
                logging.info(f"[{symbol}] Migrating from {timeframe} watcher.")
            if not active_symbols: break
            continue

        async with ohlcv_lock:
            cache_key = f"{symbol}_{timeframe}"
            if cache_key not in ohlcv_cache:
                ohlcv_cache[cache_key] = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')

            df = ohlcv_cache[cache_key]
            new_data = []
            for candle in candles:
                ts = pd.to_datetime(candle[0], unit='ms')
                if ts in df.index:
                    df.loc[ts] = candle[1:]
                else:
                    new_data.append(candle)

            if new_data:
                new_df = pd.DataFrame(new_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                new_df['timestamp'] = pd.to_datetime(new_df['timestamp'], unit='ms')
                new_df.set_index('timestamp', inplace=True)
                df = pd.concat([df, new_df]).tail(1000)

            ohlcv_cache[cache_key] = df
            async with bot_lock:
                if symbol in bot_state:
                    bot_state[symbol]['price'] = candles[-1][4]

        await analysis_queue.put((symbol, timeframe))

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
            entry_price = curr_price
            entry_fee = 0
            entry_total_base = amount * curr_price

            try:
                my_trades = await exchange.fetch_my_trades(symbol, limit=10)
                if my_trades:
                    buys = [t for t in my_trades if t['side'] == 'buy']
                    if buys:
                        buys.sort(key=lambda x: x['timestamp'], reverse=True)
                        last_buy = buys[0]
                        entry_price = last_buy['price']

                        total_fee = 0
                        accumulated_amount = 0
                        for b in buys:
                            if accumulated_amount >= amount * 0.99:
                                break
                            trade_amt = b['amount']
                            if 'fee' in b and b['fee']:
                                fee_cost = b['fee'].get('cost', 0)
                                fee_currency = b['fee'].get('currency')
                                _, quote = symbol.split('/')
                                if fee_currency and fee_currency != quote:
                                    try:
                                        fticker = await exchange.fetch_ticker(f"{fee_currency}/{quote}")
                                        if fticker: fee_cost *= fticker['last']
                                    except: pass
                                total_fee += fee_cost
                            accumulated_amount += trade_amt

                        entry_fee = total_fee
                        entry_total_base = (amount * entry_price) + entry_fee
            except Exception as e:
                logging.warning(f"[{symbol}] Failed to recover trade history: {e}")

            data_manager.add_position(symbol, entry_price, amount, entry_fee, {"info": "auto_populated"}, time.time(), total_base=entry_total_base)
        else:
            logging.warning(f"[{symbol}] Asset found in wallet but price unavailable.")

    logging.info(f"Syncing positions from {exchange_id} API done.")

async def dedicated_analysis_task(exchange, config, data_manager, pattern_manager, engine, device):
    logging.info("Dedicated analysis and trade task started.")
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4)

    while not shutdown_event.is_set():
        try:
            symbol, timeframe = await asyncio.wait_for(analysis_queue.get(), timeout=1.0)
            await analyze_and_trade(exchange, symbol, timeframe, config, data_manager, pattern_manager, engine, device, executor)
            analysis_queue.task_done()
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logging.error(f"Error in dedicated analysis task: {e}")

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

        # 2. Adaptive Timeframe Discovery
        last_tf_check = config['pairs'].get(symbol, {}).get('_last_tf_check', 0)
        if time.time() - last_tf_check > 900:
            new_tf, score = await get_optimal_timeframe(exchange, symbol, config)
            config['pairs'][symbol]['_last_tf_check'] = time.time()
            if new_tf != timeframe:
                logging.info(f"[{symbol}] Timeframe change: {timeframe} -> {new_tf}")
                config['pairs'][symbol]['timeframe'] = new_tf
                asyncio.create_task(watch_ohlcv_timeframe_task(exchange, [symbol], new_tf, config))

        # 3. Multi-technique Evaluation
        pair_config = config['pairs'].get(symbol, {})
        techniques = pair_config.get('techniques', [])
        if not techniques:
            techniques = [{"strategy": s, "aggr": ["normal", "aggressive", "dynamic"]} for s in STRATEGIES]

        buy_count = 0
        sell_count = 0
        total_score = 0

        tasks = []
        for t in techniques:
            strat = t.get('strategy')
            aggr_list = t.get('aggr', ['normal'])
            if isinstance(aggr_list, str): aggr_list = [aggr_list]
            for a in aggr_list:
                mode_settings = engine.get_dynamic_settings(latest_base.get('adx', 20), latest_base.get('volatility', 0.001), aggr=a)
                mode_settings['strategy'] = strat
                mode_settings['device'] = device
                tasks.append(loop.run_in_executor(executor, get_signals, df.copy(), mode_settings))

        if tasks:
            done_results = await asyncio.gather(*tasks)
            for res_df in done_results:
                if res_df.empty: continue
                latest = res_df.iloc[-1]
                total_score += latest.get('score', 0)
                if latest.get('buy_signal'): buy_count += 1
                if latest.get('sell_signal'): sell_count += 1

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
                if not bench_executor: bench_executor = concurrent.futures.ProcessPoolExecutor(max_workers=2)
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
    async for balance in exchange.watch_balance():
        async with bot_lock:
            current_balances = balance
        logging.debug("Balance updated via WebSocket")

async def watch_orders_task(exchange, data_manager):
    logging.info("WebSocket: watch_orders task started.")
    async for orders in exchange.watch_orders():
        for order in orders:
            if order['status'] == 'closed':
                logging.info(f"Order Completed: {order['symbol']} {order['side']} @ {order['price']}")
                # We could sync positions with data_manager here if we wanted to be
                # fully WebSocket-driven for fills.

async def show_balances(exchange):
    console.print("\n[bold magenta]=== Real Wallet Balance (All Assets) ===[/]")
    balance = await exchange.fetch_balance()
    if balance is None:
        console.print("[bold red]Error: Failed to fetch balances.[/]")
        return

    table = Table(title="Asset Inventory", expand=True)
    table.add_column("Asset", style="cyan")
    table.add_column("Free", justify="right")
    table.add_column("Used", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Est. EUR", justify="right", style="green")

    total_balances = balance.get('total', balance)
    free_balances = balance.get('free', {})
    used_balances = balance.get('used', {})

    total_eur_value = 0
    for asset in sorted(total_balances.keys()):
        total = total_balances[asset]
        if not isinstance(total, (int, float)) or total == 0: continue
        free = free_balances.get(asset, 0)
        used = used_balances.get(asset, 0)

        eur_val = 0
        if asset in ['EUR', 'USDT', 'USDC']: eur_val = total
        else:
            ticker = await exchange.fetch_ticker(f"{asset}/EUR")
            if ticker: eur_val = total * ticker['last']

        total_eur_value += eur_val
        table.add_row(asset, format_amt(free), format_amt(used), format_amt(total), format_price(eur_val))

    console.print(table)
    console.print(f"\n[bold yellow]Estimated Total Wallet Value: {total_eur_value:.2f} EUR[/]\n")

def get_sorted_symbols(config):
    tf_priority = {'1s': 0, '1m': 1, '3m': 2, '5m': 3, '15m': 4, '30m': 5}
    all_pairs = sorted(
        [s for s in bot_state.keys() if not s.startswith("_")],
        key=lambda x: (tf_priority.get(config['pairs'].get(x, {}).get('timeframe', '5m'), 99), x)
    )
    return all_pairs

def make_dashboard(mode, config):
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
        table.add_column("Strategy", style="bold cyan", no_wrap=True)
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
        table.add_column("Strategy", style="bold cyan", no_wrap=True)

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
                data.get('strategy', 'N/A')
            ]
        else:
            strat = config.get('pairs', {}).get(symbol, {}).get('strategy', 'tema')
            row_vals = [
                symbol, tf,
                format_price(data.get('price')),
                amt_str, entry_str, fee_str,
                f"{data.get('expected_profit', 0):.4f}",
                data.get('tendency', 'Neutral'),
                f"[{sig_style}]{current_signal}[/]",
                data.get('aggr', 'N/A'),
                f"[bold cyan]{strat}[/]"
            ]

        # Rolling effect for strategy display (if multiple)
        strats = data.get('strategies')
        if strats and len(strats) > 1:
            strat_idx = int(time.time() / 2) % len(strats)
            row_vals[-1] = f"[bold cyan]{strats[strat_idx]}[/]"

        table.add_row(*row_vals, style=row_style)

    pairs_panel = Panel(
        table,
        title="[bold]Trading Pairs[/]",
        border_style="bold green" if focused_panel == "pairs" else "cyan"
    )

    # 3. Status Bar
    status_text = Text()
    status_text.append(f"Update: {now.strftime('%H:%M:%S')} | Mode: {mode.upper()} | ", style="bold brown")
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

async def run_dashboard(mode, config):
    try:
        # Start Live immediately but without screen=True to allow startup logs to be visible
        # or use a simplified layout during startup.
        with Live(make_dashboard(mode, config), refresh_per_second=4, screen=False) as live:
            while not startup_complete and not shutdown_event.is_set():
                live.update(make_dashboard(mode, config))
                await asyncio.sleep(0.5)

            # Switch to screen mode once startup is complete
            # We have to close the old live and start a new one to change screen=True
            pass

        if shutdown_event.is_set(): return

        with Live(make_dashboard(mode, config), refresh_per_second=4, screen=True) as live:
            while not shutdown_event.is_set():
                live.update(make_dashboard(mode, config))
                await asyncio.sleep(0.25)
    except Exception as e:
        logging.info(f"[red]Dashboard error: {e}")

async def heartbeat_task():
    while not shutdown_event.is_set():
        logging.info("Bot heartbeat: alive and watching...")
        await asyncio.sleep(30)

def plot_scan(df, symbol, strategy_name, aggr_name, results):
    import matplotlib.pyplot as plt
    plt.figure(figsize=(12, 7))
    plt.plot(df.index, df['close'], label='Price', color='blue', alpha=0.6)
    buys = df[df['buy_signal']]
    plt.scatter(buys.index, buys['close'], marker='^', color='green', label='BUY', s=100)
    sells = df[df['sell_signal']]
    plt.scatter(sells.index, sells['close'], marker='v', color='red', label='SELL', s=100)
    plt.title(f"Scan: {symbol} | Strategy: {strategy_name} | Aggr: {aggr_name}")
    plt.legend()
    plt.grid(True, alpha=0.3)
    filename = f"scan_{symbol.replace('/', '_')}_{strategy_name}.png"
    plt.savefig(filename)
    console.print(f"[bold green]Scan plot saved as {filename}[/]")
    plt.close()

async def run_scan_logic(exchange, symbol, strategy, aggr_name, config, timeframe='1m', df_in=None, limit=500, engine=None, device=None):
    if df_in is None:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        if not ohlcv: return None
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
    else:
        df = df_in.copy()

    # Base settings for scan
    mode_settings = engine.get_dynamic_settings(20, 0.005, aggr=aggr_name)
    mode_settings['strategy'] = strategy
    mode_settings['device'] = device

    df = get_signals(df, mode_settings, is_scan=True)

    balance = 100.0
    position = None
    trades = []

    for i in range(len(df)):
        row = df.iloc[i]
        price = row['close']

        if row['sell_signal'] and position:
            profit = (position['amount'] * price * 0.999) - position['cost']
            balance += (position['amount'] * price * 0.999)
            trades.append(profit)
            position = None
        elif row['buy_signal'] and not position and balance > 10:
            cost = balance * 0.1
            amount = (cost * 0.999) / price
            position = {'amount': amount, 'cost': cost}
            balance -= cost

    total_profit = sum(trades)
    return {
        'df': df,
        'profit': total_profit,
        'win_rate': len([t for t in trades if t > 0]) / len(trades) if trades else 0,
        'trades_count': len(trades)
    }

async def run_scan_mode(exchange, config, args, engine, device):
    strategy = args.strategy
    aggr = args.aggr or 'normal'
    symbol = args.symbol
    timeframe = args.timeframe or '1m'

    console.print(f"[bold blue]Running Scan for {symbol} | Strategy: {strategy}...[/]")
    results = await run_scan_logic(exchange, symbol, strategy, aggr, config, timeframe=timeframe, engine=engine, device=device)

    if results:
        if results['trades_count'] > 0:
            plot_scan(results['df'], symbol, strategy, aggr, results)
        console.print(f"\n[bold yellow]Scan Summary for {symbol}:[/]")
        console.print(f"Total Profit: {results['profit']:.2f} EUR")
        console.print(f"Win Rate: {results['win_rate']:.1%}")
        console.print(f"Total Trades: {results['trades_count']}")

async def run_optimization_test(exchange, config, args, data_manager, pattern_manager, engine, device):
    symbols = [args.symbol] if args.symbol else list(config['pairs'].keys())
    console.print(f"[bold blue]Scanning strategies for {len(symbols)} symbol(s) using multi-processing...[/]")

    loop = asyncio.get_event_loop()
    global bench_executor
    if not bench_executor: bench_executor = concurrent.futures.ProcessPoolExecutor(max_workers=os.cpu_count() or 4)

    tasks = []
    for symbol in symbols:
        timeframe = config['pairs'].get(symbol, {}).get('timeframe', '1m')
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=1000)
        if not ohlcv: continue

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)

        task = loop.run_in_executor(bench_executor, run_optimization_for_symbol_sync,
                                  symbol, config, timeframe, ['normal'], STRATEGIES, df, engine, device)
        tasks.append(task)

    results = await asyncio.gather(*tasks)

    console.print("\n[bold magenta]=== DISCOVERY RECOMMENDATIONS ===[/]")
    for sym, best_strat, profit in results:
        if best_strat:
            console.print(f"[bold green]🏆 DISCOVERY FOR {sym}:[/] {best_strat} | Profit: {profit:.2f} EUR")
            config['pairs'][sym]['strategy'] = best_strat

async def initialize_simulation(exchange, data_manager, pattern_manager, engine, config, device):
    logging.info("Initializing Simulation positions (Discovery phase)...")
    await sync_live_positions(exchange, data_manager, config)

    pairs = list(config['pairs'].keys())

    # Discovery phase: find best strategies for simulation start if not cached
    for symbol in pairs:
        timeframe = config['pairs'].get(symbol, {}).get('timeframe', '1m')
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=500)
        if ohlcv:
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)

            best_p = -999
            best_s = 'tema_crossover'
            for s in STRATEGIES[:5]:
                res = await run_scan_logic(exchange, symbol, s, 'normal', config, df_in=df, engine=engine, device=device)
                if res and res['profit'] > best_p:
                    best_p = res['profit']
                    best_s = s
            config['pairs'][symbol]['strategy'] = best_s
            logging.info(f"[{symbol}] Initialized with strategy {best_s}")

    logging.info("Initialization of simulation positions completed.")

async def main():
    parser = argparse.ArgumentParser(description='CCXT Pro Trading Bot v2 (Asynchronous)')
    parser.add_argument('--no-gpu', action='store_true', help='Disable GPU acceleration (force CPU)')
    parser.add_argument('--exchange', help='CCXT Exchange ID to use (e.g., binance, kraken, bitvavo)')
    parser.add_argument('--mode', choices=['live', 'simulation', 'balance', 'scan', 'optimization'], default='simulation', help='Bot mode')
    parser.add_argument('--config', help='Path to config file (optional)')
    parser.add_argument('--symbol', help='Target symbol (e.g. BTC/EUR)')
    parser.add_argument('--strategy', help='Strategy name for scan mode')
    parser.add_argument('--aggr', help='Agressivity for scan mode')
    parser.add_argument('--timeframe', help='Manual timeframe override')
    parser.add_argument('--since', help='Start date (YYYY-MM-DD HH:MM)')
    parser.add_argument('--until', help='End date (YYYY-MM-DD HH:MM)')
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

    if args.config:
        config = load_config_from_path(args.config)
    else:
        config = load_config()

    api_creds = {}
    if os.path.exists('api.json'):
        with open('api.json', 'r') as f: api_creds = json.load(f)

    exchange_id = api_creds.get('exchange_id') or args.exchange or config.get('exchange') or 'binance'
    if args.mode == 'simulation':
        exchange = MockExchange2(api_creds.get('api_key') or config.get('api_key'),
                                api_creds.get('api_secret') or config.get('api_secret'),
                                exchange_id)
    else:
        exchange = CCXTExchange2(exchange_id,
                                api_creds.get('api_key') or config.get('api_key'),
                                api_creds.get('api_secret') or config.get('api_secret'))

    logging.info(f"Connecting to {exchange_id}...")
    await exchange.load_markets()

    # Pre-initialize balances from REST
    try:
        logging.info("Fetching initial balances...")
        initial_balance = await exchange.fetch_balance()
        async with bot_lock:
            global current_balances
            current_balances = initial_balance
    except Exception as e:
        logging.info(f"[yellow]Warning: Could not fetch initial balances: {e}")

    data_manager = DataManager()
    pattern_manager = PatternManager()
    engine = TradingEngine(config)

    # Initial sync happens at start
    if args.mode == 'simulation':
        await initialize_simulation(exchange, data_manager, pattern_manager, engine, config, device)
    elif args.mode == 'live':
        await sync_live_positions(exchange, data_manager, config)

    if args.mode == 'balance':
        await show_balances(exchange)
        await exchange.close()
        return

    if args.mode == 'scan':
        if not args.symbol or not args.strategy:
            console.print("[bold red]Error: Scan mode requires --symbol and --strategy.[/]")
            await exchange.close()
            return
        await run_scan_mode(exchange, config, args, engine, device)
        await exchange.close()
        return

    if args.mode == 'optimization':
        await run_optimization_test(exchange, config, args, data_manager, pattern_manager, engine, device)
        await exchange.close()
        return

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
    mode = args.mode if args.mode else config.get('mode', 'simulation')
    ui_task = asyncio.create_task(run_dashboard(mode, config))

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
            'expected_profit': 0
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
                    df.set_index('timestamp', inplace=True)
                    ohlcv_cache[f"{symbol}_{tf}"] = df
                    logging.info(f"[{symbol}] Loaded {len(df)} candles.")
                except Exception as e:
                    logging.error(f"Failed to load candles for {symbol}: {e}")
                    ohlcv_cache[f"{symbol}_{tf}"] = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')

        await asyncio.gather(*[init_symbol(s) for s in pairs])

    # Start WebSocket Tasks
    logging.info("[bold green]Starting WebSocket tasks...")
    background_tasks = [
        asyncio.create_task(watch_balance_task(exchange, data_manager)),
        asyncio.create_task(watch_orders_task(exchange, data_manager)),
        asyncio.create_task(input_task(config)),
        asyncio.create_task(heartbeat_task())
    ]

    # Adaptive OHLCV Watchers (Grouped by timeframe)
    tf_groups = {}
    for symbol in pairs:
        tf = config['pairs'].get(symbol, {}).get('timeframe', '1m')
        if tf not in tf_groups: tf_groups[tf] = []
        tf_groups[tf].append(symbol)

    for tf, syms in tf_groups.items():
        background_tasks.append(asyncio.create_task(watch_ohlcv_timeframe_task(exchange, syms, tf, config)))

    # Dedicated analysis/trade worker
    background_tasks.append(asyncio.create_task(dedicated_analysis_task(exchange, config, data_manager, pattern_manager, engine, device)))

    startup_complete = True
    logging.info("Bot v2 fully operational.")

    try:
        await shutdown_event.wait()
    except Exception as e:
        logging.error(f"Main loop error: {e}")
    finally:
        shutdown_event.set()
        for t in background_tasks: t.cancel()
        if ui_task: ui_task.cancel()
        global bench_executor
        if bench_executor: bench_executor.shutdown(wait=False)
        await exchange.close()

        # Clear screen and show final logs
        console.clear()
        console.print("[bold red]Bot v2 shutdown sequence complete.[/]")
        console.print("[bold white]Final Log Summary:[/]")
        for log in all_logs[-20:]:
            console.print(f"[{log['timestamp']}] {log['msg']}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        # Final emergency log
        with open("fatal_error.log", "a") as f:
            f.write(f"{datetime.now()} - FATAL ERROR: {str(e)}\n")
            import traceback
            f.write(traceback.format_exc())
        console.print_exception()

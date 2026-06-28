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

        # Capture the raw message including markup
        all_logs.append({'msg': msg, 'timestamp': timestamp, 'expiry': expiry})
        if len(all_logs) > 500:
            all_logs.pop(0)

db_handler = AsyncDashboardHandler()
db_handler.setFormatter(logging.Formatter("%(message)s"))

# root_logger will only use db_handler to avoid clearing console
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(db_handler)

def load_config():
    path = 'config.json' if os.path.exists('config.json') else 'config.default.json'
    with open(path, 'r') as f:
        return json.load(f)

def format_price(price, precision=None):
    if price is None: return "-"
    try:
        p = float(price)
        if precision is None: return f"{p:.8f}".rstrip('0').rstrip('.')
        return f"{p:.{precision}f}".rstrip('0').rstrip('.')
    except: return str(price)

def format_amt(amt, precision=None):
    if amt is None: return "-"
    try:
        a = float(amt)
        if precision is None: return f"{a:.8f}".rstrip('0').rstrip('.')
        return f"{a:.{precision}f}".rstrip('0').rstrip('.')
    except: return str(amt)

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

async def input_task():
    global focused_panel, selected_pair_index, pairs_scroll_offset, logs_scroll_offset
    global expert_mode, marquee_enabled, show_help, show_chart, chart_symbol

    while not shutdown_event.is_set():
        try:
            loop = asyncio.get_event_loop()
            key = await loop.run_in_executor(None, readchar.readkey)

            if key == readchar.key.CTRL_C:
                shutdown_event.set()
                break

            if not startup_complete: continue

            sorted_symbols = sorted(bot_state.keys())
            pairs_height = console.height - 15

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
                else:
                    logs_scroll_offset += 1
            elif key == readchar.key.DOWN:
                if focused_panel == "pairs":
                    selected_pair_index = min(len(sorted_symbols) - 1, selected_pair_index + 1)
                    if selected_pair_index >= pairs_scroll_offset + pairs_height:
                        pairs_scroll_offset = selected_pair_index - pairs_height + 1
                else:
                    logs_scroll_offset = max(0, logs_scroll_offset - 1)
            elif key.lower() == 'x':
                expert_mode = not expert_mode
            elif key.lower() == 'h':
                show_help = True
            elif key == readchar.key.ENTER:
                if focused_panel == "pairs" and sorted_symbols:
                    chart_symbol = sorted_symbols[selected_pair_index]
                    show_chart = True

        except Exception as e:
            logging.error(f"Input error: {e}")
        await asyncio.sleep(0.1)

async def watch_ohlcv_all_symbols_task(exchange, symbols, timeframe):
    logging.info(f"Starting single OHLCV watcher for all {len(symbols)} symbols")
    async for data in exchange.watch_ohlcv_for_symbols(symbols, timeframe):
        if shutdown_event.is_set(): break

        # Handler now consistently returns (symbol, candles)
        if isinstance(data, tuple) and len(data) == 2:
            symbol, candles = data
        else:
            logging.warning(f"Unexpected OHLCV data format: {type(data)}")
            continue

        async with ohlcv_lock:
            if symbol not in ohlcv_cache: continue
            df = ohlcv_cache[symbol]
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
                df = pd.concat([df, new_df]).tail(11000)

            ohlcv_cache[symbol] = df

        # Put symbol in queue for dedicated analysis task
        await analysis_queue.put(symbol)

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
                                 if accumulated_amount >= amount * 0.99: break
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
    # Use ThreadPoolExecutor for CPU-bound technical analysis
    # This fulfills the requirement of having a dedicated "thread" for analysis
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4)

    while not shutdown_event.is_set():
        try:
            # Get next symbol that received an update
            symbol = await asyncio.wait_for(analysis_queue.get(), timeout=1.0)

            # Run analysis and trading logic
            await analyze_and_trade(exchange, symbol, config, data_manager, pattern_manager, engine, device, executor)

            analysis_queue.task_done()
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logging.error(f"Error in dedicated analysis task: {e}")

async def analyze_and_trade(exchange, symbol, config, data_manager, pattern_manager, engine, device, executor=None):
    try:
        async with ohlcv_lock:
            df = ohlcv_cache[symbol].copy()

        if df.empty or len(df) < 20: return

        loop = asyncio.get_event_loop()

        # Base technical analysis for regime detection
        # Offload to executor if provided to keep event loop responsive
        if executor:
            df = await loop.run_in_executor(executor, get_signals, df, {'device': device})
        else:
            df = get_signals(df, {'device': device})

        latest = df.iloc[-1]

        pair_config = config.get('pairs', {}).get(symbol, {})
        strategy = pair_config.get('strategy', 'tema_crossover')

        # Dynamic risk settings based on current market state
        mode_settings = engine.get_dynamic_settings(
            latest.get('adx', 20),
            latest.get('volatility', 0.001)
        )
        mode_settings['strategy'] = strategy
        mode_settings['device'] = device

        # Strategy-specific analysis
        if executor:
            df = await loop.run_in_executor(executor, get_signals, df, mode_settings)
        else:
            df = get_signals(df, mode_settings)

        latest = df.iloc[-1]

        async with bot_lock:
            bot_state[symbol]['price'] = latest['close']
            bot_state[symbol]['rsi'] = latest.get('rsi', 0)
            bot_state[symbol]['tendency'] = latest.get('tendency', 'Neutral')
            bot_state[symbol]['last_signal'] = 'Buy' if latest.get('buy_signal') else ('Sell' if latest.get('sell_signal') else 'Waiting')

        if latest.get('buy_signal'):
            await execute_buy(exchange, symbol, latest, data_manager, engine, config)
        elif latest.get('sell_signal'):
            await execute_sell(exchange, symbol, latest, data_manager, engine, config)

    except Exception as e:
        logging.error(f"Analysis error for {symbol}: {e}")

async def execute_buy(exchange, symbol, data, data_manager, engine, config):
    global current_balances
    async with bot_lock:
        pos = data_manager.get_position(symbol)
        max_lots = config.get('pairs', {}).get(symbol, {}).get('max_lots_per_symbol') or config.get('max_lots_per_symbol', 1)
        if pos and len(pos) >= max_lots:
            return

    try:
        price = data['close']

        async with bot_lock:
            balance = current_balances

        if not balance:
            balance = await exchange.fetch_balance()
            async with bot_lock:
                current_balances = balance

        amount = engine.calculate_position_size(balance, price, symbol.split('/')[1], timeframe='1s')

        if amount > 0:
            order = await exchange.create_order(symbol, 'buy', amount)
            if order:
                fee = order.get('fee', {}).get('cost', 0)
                data_manager.add_position(symbol, price, amount, fee, {}, time.time())
                logging.info(f"BUY {symbol} executed at {price}")
                async with bot_lock:
                    bot_state[symbol]['position'] = data_manager.get_position(symbol)
    except Exception as e:
        logging.error(f"Buy failed for {symbol}: {e}")

async def execute_sell(exchange, symbol, data, data_manager, engine, config):
    async with bot_lock:
        positions = data_manager.get_position(symbol)
        if not positions: return

    price = data['close']
    fee_rate = 0.001

    any_sold = False
    for i in range(len(positions) - 1, -1, -1):
        pos = positions[i]
        if engine.is_profitable(price, pos['entry_price'], fee_rate=fee_rate):
            try:
                order = await exchange.create_order(symbol, 'sell', pos['amount'])
                if order:
                    fee = order.get('fee', {}).get('cost', 0)
                    profit = (pos['amount'] * price) - (pos['amount'] * pos['entry_price']) - fee
                    data_manager.close_position(symbol, price, fee, profit, {}, time.time(), lot_index=i)
                    logging.info(f"SELL {symbol} executed at {price} (Profit: {profit:.2f})")
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

def make_dashboard(mode, config):
    now = datetime.now()
    layout = Layout()

    # Calculate height for pairs
    pairs_height = console.height - 15
    if pairs_height < 5: pairs_height = 5

    # Status bar
    status_text = Text(f"Mode: {mode.upper()} | Update: {now.strftime('%H:%M:%S')} | Symbols: {len(bot_state)}", justify="center")

    layout.split(
        Layout(Panel(Text("🛸 CCXT Pro Trading Bot v2 (Async/1s)", style="bold magenta", justify="center"), border_style="blue"), size=3),
        Layout(name="main"),
        Layout(Panel(status_text, title="Status", border_style="cyan"), size=3)
    )

    # Logs Panel (Utilize more height)
    log_content = Text()
    log_limit = console.height // 4
    start_log = max(0, len(all_logs) - log_limit - logs_scroll_offset)
    end_log = max(0, len(all_logs) - logs_scroll_offset)
    for log in all_logs[start_log:end_log]:
        try:
            msg_text = Text.from_markup(f"[{log['timestamp']}] {log['msg']}")
        except:
            msg_text = Text(f"[{log['timestamp']}] {log['msg']}")
        if log['expiry'] < now: msg_text.stylize("dim green")
        else:
            if not any(span.style for span in msg_text.spans): msg_text.stylize("bold green")
        log_content.append_text(msg_text); log_content.append("\n")

    # Pairs Panel with Expert Mode support
    table = Table(expand=True, box=None, padding=(0, 1))
    if expert_mode:
        table.add_column("Pair", style="cyan")
        table.add_column("Price", style="magenta")
        table.add_column("EMA F/S", style="green")
        table.add_column("RSI", style="yellow")
        table.add_column("ADX/Vol", style="dim white")
        table.add_column("Score", style="bold white")
        table.add_column("Signal", style="bold")
    else:
        table.add_column("Pair", style="cyan")
        table.add_column("Price", style="magenta")
        table.add_column("RSI", style="yellow")
        table.add_column("Tendency", style="bold")
        table.add_column("Signal", style="bold")
        table.add_column("Lots", style="yellow", justify="center")
        table.add_column("Strategy", style="dim cyan")

    sorted_symbols = sorted(bot_state.keys())
    visible_symbols = sorted_symbols[pairs_scroll_offset : pairs_scroll_offset + pairs_height]

    for i, symbol in enumerate(visible_symbols):
        abs_idx = pairs_scroll_offset + i
        data = bot_state[symbol]
        row_style = "bold reverse" if abs_idx == selected_pair_index and focused_panel == "pairs" else ""

        price = format_price(data.get('price'))
        sig = data.get('last_signal', 'Waiting')
        sig_style = "bold green" if "Buy" in sig else "bold red" if "Sell" in sig else "white"

        if expert_mode:
            row_vals = [
                symbol, price,
                f"{data.get('ema_f', 0):.2f}/{data.get('ema_s', 0):.2f}",
                f"{data.get('rsi', 0):.2f}",
                f"{data.get('adx', 0):.1f}/{data.get('volatility', 0):.4f}",
                str(data.get('score', 0)),
                f"[{sig_style}]{sig}[/]"
            ]
        else:
            pos = data.get('position')
            lots = str(len(pos)) if pos else "0"
            strat = config.get('pairs', {}).get(symbol, {}).get('strategy', 'tema')
            row_vals = [
                symbol, price, f"{data.get('rsi', 0):.2f}",
                data.get('tendency', 'Neutral'),
                f"[{sig_style}]{sig}[/]",
                lots, strat
            ]
        table.add_row(*row_vals, style=row_style)

    layout["main"].split_row(
        Layout(Panel(log_content, title="Live Logs", border_style="green" if focused_panel=="logs" else "blue"), ratio=1),
        Layout(Panel(table, title="Trading Pairs", border_style="green" if focused_panel=="pairs" else "blue"), ratio=2)
    )

    if show_help:
        help_text = Text()
        help_text.append("\nKeyboard Shortcuts:\n", style="bold cyan")
        help_text.append("  TAB    : Switch Panels\n")
        help_text.append("  UP/DN  : Navigate / Scroll\n")
        help_text.append("  ENTER  : Show K-Lines\n")
        help_text.append("  X      : Expert Mode\n")
        help_text.append("  H      : Close Help\n")
        help_text.append("  Ctrl+C : Quit\n")
        layout["main"].update(Panel(help_text, title="Help", border_style="bold yellow"))

    if show_chart and chart_symbol:
        chart_content = render_ascii_chart(chart_symbol, config)
        layout["main"].update(Panel(chart_content, title=f"Chart: {chart_symbol}", border_style="bold magenta"))

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

async def main():
    parser = argparse.ArgumentParser(description='CCXT Pro Trading Bot v2')
    parser.add_argument('--mode', choices=['live', 'simulation'], default='simulation')
    parser.add_argument('--fast-start', action='store_true', help='Skip fetching 10,000 candles')
    args = parser.parse_args()

    config = load_config()
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    api_creds = {}
    if os.path.exists('api.json'):
        with open('api.json', 'r') as f: api_creds = json.load(f)

    exchange_id = api_creds.get('exchange_id', 'binance')
    if args.mode == 'simulation':
        exchange = MockExchange2(api_creds.get('api_key'), api_creds.get('api_secret'), exchange_id)
    else:
        exchange = CCXTExchange2(exchange_id, api_creds.get('api_key'), api_creds.get('api_secret'))

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

    # Sync live positions from real wallet (works for both Live and Simulation with API keys)
    await sync_live_positions(exchange, data_manager, config)

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

    # Start UI task (it will wait for startup_complete to start Live)
    global ui_task, background_tasks, startup_complete
    mode = args.mode if args.mode else config.get('mode', 'simulation')
    ui_task = asyncio.create_task(run_dashboard(mode, config))

    # Initial Batch
    for symbol in pairs:
        bot_state[symbol] = {
            'price': 0, 'rsi': 0, 'tendency': 'Neutral',
            'last_signal': 'Init', 'position': data_manager.get_position(symbol)
        }

    if args.fast_start:
        logging.info("[bold yellow]Fast start enabled: Skipping 10,000 candles fetch.")
        for symbol in pairs:
            ohlcv_cache[symbol] = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')
    else:
        logging.info(f"[bold cyan]Fetching 10,000 initial candles (1s) for {len(pairs)} pairs...")

        semaphore = asyncio.Semaphore(5) # Limit concurrency

        async def init_symbol(symbol):
            async with semaphore:
                try:
                    logging.info(f"Fetching candles for {symbol}...")
                    ohlcv = await exchange.fetch_ohlcv_10k(symbol, '1s', 10000)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                    df.set_index('timestamp', inplace=True)
                    ohlcv_cache[symbol] = df
                    logging.info(f"[{symbol}] Loaded {len(df)} candles.")
                except Exception as e:
                    logging.error(f"Failed to load candles for {symbol}: {e}")
                    ohlcv_cache[symbol] = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']).set_index('timestamp')

        await asyncio.gather(*[init_symbol(s) for s in pairs])

    # Start WebSocket Tasks
    logging.info("[bold green]Starting WebSocket tasks...")
    background_tasks = [
        asyncio.create_task(watch_balance_task(exchange, data_manager)),
        asyncio.create_task(watch_orders_task(exchange, data_manager)),
        asyncio.create_task(input_task()),
        asyncio.create_task(heartbeat_task())
    ]

    # Single watcher for all symbols
    background_tasks.append(asyncio.create_task(watch_ohlcv_all_symbols_task(exchange, pairs, '1s')))

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

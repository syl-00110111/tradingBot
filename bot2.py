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

# Global controls for dashboard
pairs_scroll_offset = 0
selected_pair_index = 0
show_chart = False
chart_symbol = None
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

            if key == readchar.key.TAB:
                focused_panel = "logs" if focused_panel == "pairs" else "pairs"
            elif key == readchar.key.UP:
                if focused_panel == "pairs":
                    selected_pair_index = max(0, selected_pair_index - 1)
                else:
                    logs_scroll_offset += 1
            elif key == readchar.key.DOWN:
                if focused_panel == "pairs":
                    selected_pair_index += 1
                else:
                    logs_scroll_offset = max(0, logs_scroll_offset - 1)
            elif key.lower() == 'x':
                expert_mode = not expert_mode
            elif key.lower() == 'h':
                show_help = not show_help
            elif key == readchar.key.ENTER:
                if focused_panel == "pairs":
                    symbols = sorted(bot_state.keys())
                    if symbols and selected_pair_index < len(symbols):
                        chart_symbol = symbols[selected_pair_index]
                        show_chart = not show_chart

        except Exception as e:
            logging.error(f"Input error: {e}")
        await asyncio.sleep(0.1)

async def watch_ohlcv_task(exchange, symbol, timeframe, config, data_manager, pattern_manager, engine, device):
    logging.info(f"Starting OHLCV watcher for {symbol}")
    async for candles in exchange.watch_ohlcv(symbol, timeframe):
        if shutdown_event.is_set(): break

        if not isinstance(candles[0], list):
            candles = [candles]

        async with ohlcv_lock:
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

        asyncio.create_task(analyze_and_trade(exchange, symbol, config, data_manager, pattern_manager, engine, device))

async def analyze_and_trade(exchange, symbol, config, data_manager, pattern_manager, engine, device):
    try:
        async with ohlcv_lock:
            df = ohlcv_cache[symbol].copy()

        if df.empty or len(df) < 20: return

        # Base technical analysis for regime detection
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
    layout.split(
        Layout(Panel(Text("🛸 CCXT Pro Trading Bot v2 (Async/1s)", style="bold magenta", justify="center"), border_style="blue"), size=3),
        Layout(name="main"),
        Layout(Panel(Text(f"Mode: {mode.upper()} | Update: {now.strftime('%H:%M:%S')} | Symbols: {len(bot_state)}", justify="center"), title="Status", border_style="cyan"), size=3)
    )
    log_content = Text()
    start_log = max(0, len(all_logs) - 15 - logs_scroll_offset)
    end_log = max(0, len(all_logs) - logs_scroll_offset)
    for log in all_logs[start_log:end_log]:
        try:
            msg_text = Text.from_markup(f"[{log['timestamp']}] {log['msg']}")
        except:
            msg_text = Text(f"[{log['timestamp']}] {log['msg']}")

        if log['expiry'] < now:
            msg_text.stylize("dim green")
        else:
            if not any(span.style for span in msg_text.spans):
                msg_text.stylize("bold green")

        log_content.append_text(msg_text)
        log_content.append("\n")
    table = Table(expand=True, box=None)
    table.add_column("Pair", style="cyan")
    table.add_column("Price", style="magenta")
    table.add_column("RSI", style="yellow")
    table.add_column("Tendency", style="bold")
    table.add_column("Signal", style="bold")
    table.add_column("Lots", style="yellow", justify="center")
    table.add_column("Strategy", style="dim cyan")
    symbols = sorted(bot_state.keys())
    for i, symbol in enumerate(symbols):
        data = bot_state[symbol]
        style = "bold reverse" if i == selected_pair_index else ""
        price = format_price(data.get('price'))
        rsi = f"{data.get('rsi', 0):.2f}"
        tend = data.get('tendency', 'Neutral')
        sig = data.get('last_signal', 'Waiting')
        pos = data.get('position')
        lots = str(len(pos)) if pos else "0"
        strat = config.get('pairs', {}).get(symbol, {}).get('strategy', 'tema')
        table.add_row(symbol, price, rsi, tend, sig, lots, strat, style=style)
    layout["main"].split_row(
        Layout(Panel(log_content, title="Live Logs (H for Help)", border_style="green" if focused_panel=="logs" else "blue"), ratio=1),
        Layout(Panel(table, title="Trading Pairs (1s Interval)", border_style="green" if focused_panel=="pairs" else "blue"), ratio=2)
    )
    if show_help:
        help_text = Text("\nTAB: Switch Panels\nArrows: Navigate\nENTER: Chart\nX: Expert Mode\nCtrl+C: Quit", justify="center")
        layout["main"].update(Panel(help_text, title="Help", border_style="bold yellow"))

    if show_chart and chart_symbol:
        # Placeholder for chart implementation
        layout["main"].update(Panel(Text(f"Chart for {chart_symbol} goes here...\n(Press ENTER to return)", justify="center"), title=f"Chart: {chart_symbol}", border_style="bold magenta"))

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
        bot_state[symbol] = {'price': 0, 'rsi': 0, 'tendency': 'Neutral', 'last_signal': 'Init', 'position': None}

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

    for symbol in pairs:
        background_tasks.append(asyncio.create_task(watch_ohlcv_task(exchange, symbol, '1s', config, data_manager, pattern_manager, engine, device)))

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
        logging.info("Graceful shutdown complete.")

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

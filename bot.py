# Binance Trading Bot
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
import gzip
import copy
import pickle
import pandas as pd
import sys
import threading
import platform
import signal
import random
import concurrent.futures
import matplotlib.pyplot as plt
import torch
from datetime import datetime, timedelta

from rich.live import Live
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel
from rich.console import Console
from rich.logging import RichHandler
from rich.columns import Columns
from rich.text import Text

import readchar

from exchange_handler import BinanceExchange, MockExchange, KrakenExchange, BitvavoExchange
from indicators import get_signals, calculate_similarity, STRATEGIES
from persistence import DataManager, CacheManager, PatternManager, MonteCarloCacheManager
from trading_engine import TradingEngine
from monte_carlo import MonteCarloEngine

# Global controls for dashboard
pairs_scroll_offset = 0
logs_scroll_offset = 0
focused_panel = "pairs"
ohlcv_cache = {}
all_logs = []
status_scroll_index = 0
expert_mode = False
show_help = False
marquee_enabled = True
shutdown_event = threading.Event()
suspended_pairs = set()
signal_arrival_times = {}
global_pattern_pool = []
selected_pair_index = 0
show_candles_for_pair = None
sell_proposal_pair = None
sell_proposal_profit = 0
sell_proposal_time = 0
last_sell_proposal_check = 0

# Marquee Timing Control
last_marquee_update = 0
bot_start_time = time.time()
pairs_marquee_dir = 1
logs_marquee_dir = 1
status_marquee_dir = 1
pairs_pause_until = 0
logs_pause_until = 0
status_pause_until = 0

# State shared between threads
bot_state = {}
available_assets = []
bot_lock = threading.Lock()
pending_asset_update = False

def format_price(price):
    if price is None: return "-"
    if not isinstance(price, (int, float)): return str(price)
    if price == 0: return "0.00"
    if abs(price) < 0.01:
        return f"{price:.3e}"
    return f"{price:.2f}"

def parse_base_bet(config):
    if not config: return 10.0, 'USDT'
    raw_val = config.get('base_bet', '20.0 USDT')
    if isinstance(raw_val, str):
        try:
            parts = raw_val.split(' ')
            val = float(parts[0])
            curr = parts[1] if len(parts) > 1 else 'USDT'
            return val, curr
        except (ValueError, IndexError):
            return 10.0, 'USDT'
    return float(raw_val), 'USDT'

class DashboardHandler(logging.Handler):
    def __init__(self, duration=5):
        super().__init__()
        self.duration = duration

    def emit(self, record):
        msg = self.format(record)
        timestamp = datetime.now().strftime("%H:%M:%S")
        expiry = datetime.now() + timedelta(seconds=self.duration)

        with bot_lock:
            # Connection pool log filtering
            pool_msg = "Connection pool is full, discarding connection: api.binance.com"
            if pool_msg in msg:
                 for log in all_logs:
                      if pool_msg in log['msg']:
                           log['msg'] = f"[{timestamp}] {msg}"
                           log['expiry'] = expiry
                           return

            # Simulation init replacement
            if "Simulation initialization complete" in msg or "Initialization of the simulation positions completed" in msg:
                 replacement = "Initialization of the simulation positions completed."
                 for log in all_logs:
                      if "Initializing Simulation positions" in log['msg']:
                           log['msg'] = f"[{timestamp}] {replacement}"
                           log['expiry'] = expiry
                           return

            # Deduplication for specific log types (Profitability check or Stop-loss)
            dedup_triggers = ["Profitability check failed", "Stop-loss triggered", "SELL signal received at non-profitable price"]
            matching_trigger = next((t for t in dedup_triggers if t in msg), None)

            if matching_trigger:
                 symbol_tag = msg.split(']')[0] + ']' if ']' in msg else ""
                 # Find existing and update
                 for log in all_logs:
                      if matching_trigger in log['msg'] and symbol_tag in log['msg']:
                           log['msg'] = f"[{timestamp}] {msg}"
                           log['expiry'] = expiry
                           return

            all_logs.append({'msg': f"[{timestamp}] {msg}", 'expiry': expiry})
            if len(all_logs) > 500:
                all_logs.pop(0)

console = Console()
db_handler = DashboardHandler()
db_handler.setFormatter(logging.Formatter("%(message)s"))

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
for handler in root_logger.handlers[:]:
    root_logger.removeHandler(handler)
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
        console.print(f"Please create 'config.json' from 'config.default.json' before running the bot.")
        sys.exit(1)
    return load_config_from_path(path)

def make_dashboard(global_mode, config):
    now = datetime.now()
    now_ts = time.time()
    global status_scroll_index, pairs_scroll_offset, logs_scroll_offset
    global pairs_marquee_dir, logs_marquee_dir, status_marquee_dir, pairs_pause_until, logs_pause_until, status_pause_until, last_marquee_update
    global selected_pair_index, show_candles_for_pair, sell_proposal_pair, sell_proposal_profit, sell_proposal_time

    # Slow down marquee (e.g., 2 steps per second)
    should_step = False
    if marquee_enabled and (now_ts - last_marquee_update >= 0.4):
         should_step = True
         last_marquee_update = now_ts

    with bot_lock:
        # 1. Logs Panel
        log_height = 8
        log_content = Text()
        max_logs_offset = max(0, len(all_logs) - log_height)

        if max_logs_offset > 0 and should_step:
             if now_ts > logs_pause_until:
                  if logs_marquee_dir == 1:
                       if logs_scroll_offset < max_logs_offset:
                            logs_scroll_offset += 1
                       if logs_scroll_offset >= max_logs_offset:
                            logs_marquee_dir = -1
                            logs_pause_until = now_ts + 2
                  else:
                       if logs_scroll_offset > 0:
                            logs_scroll_offset -= 1
                       if logs_scroll_offset <= 0:
                            logs_marquee_dir = 1
                            logs_pause_until = now_ts + 2

        logs_scroll_offset = max(0, min(logs_scroll_offset, max_logs_offset))
        start = max(0, len(all_logs) - log_height - logs_scroll_offset)
        end = max(0, len(all_logs) - logs_scroll_offset)
        for log_entry in all_logs[start:end]:
            style = "bold italic bright_green" if log_entry['expiry'] > now else "dim green"
            log_content.append(log_entry['msg'] + "\n", style=style)

        log_panel = Panel(
            log_content,
            title="[bold]Logs (INFO)[/]",
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
            table.add_column("Flags", style="bold white", no_wrap=True)
            table.add_column("Scr", style="bold white", no_wrap=True)
            # table.add_column("Bench", style="bold green", no_wrap=True) # Hide in Expert Mode
            table.add_column("Agressivity", style="white", no_wrap=True)
            table.add_column("Strategy", style="bold cyan", no_wrap=True)
        else:
            table.add_column("Pair", style="cyan", no_wrap=True)
            table.add_column("Price", style="magenta", no_wrap=True)
            table.add_column("Amt", style="cyan", no_wrap=True)
            table.add_column("Entry", style="magenta", no_wrap=True)
            table.add_column("Fee", style="red", no_wrap=True)
            table.add_column("Bench", style="bold green", no_wrap=True)
            table.add_column("Tendency", style="bold white", no_wrap=True)
            table.add_column("Last Order", style="bold", no_wrap=True)
            table.add_column("Signal", style="bold", no_wrap=True)
            table.add_column("Agressivity", style="white", no_wrap=True)
            table.add_column("Strategy", style="bold cyan", no_wrap=True)

        sorted_symbols = sorted([s for s in bot_state.keys() if not s.startswith("_")])

        if sell_proposal_pair and (now_ts - sell_proposal_time < 60):
            symbol = sell_proposal_pair
            data = bot_state.get(symbol, {})
            candles_text = Text()
            candles_text.append(f"PROPOSAL: SELL {symbol} for {format_price(sell_proposal_profit)} profit?\n", style="bold yellow")
            candles_text.append("Type 'y' to confirm, 'n' to dismiss. (Auto-close in 1 min)\n\n", style="dim")
            if 'last_20_candles' in data:
                prices = data['last_20_candles']
                min_p, max_p = min(prices), max(prices)
                diff = max_p - min_p if max_p > min_p else 1.0
                chart_height = 5
                for h in reversed(range(chart_height)):
                    for p in prices:
                        threshold = min_p + (h / chart_height) * diff
                        if p >= threshold: candles_text.append("█ ", style="red")
                        else: candles_text.append("  ")
                    candles_text.append("\n")
            pairs_panel = Panel(candles_text, title="[bold red]SELL PROPOSAL[/]", border_style="bold red")
        elif show_candles_for_pair:
            symbol = show_candles_for_pair
            data = bot_state.get(symbol, {})
            candles_text = Text()
            candles_text.append(f"Last 20 candles for {symbol}:\n\n", style="bold cyan")
            if 'last_20_candles' in data:
                prices = data['last_20_candles']
                min_p, max_p = min(prices), max(prices)
                diff = max_p - min_p if max_p > min_p else 1.0
                chart_height = 8
                for h in reversed(range(chart_height)):
                    for p in prices:
                        threshold = min_p + (h / chart_height) * diff
                        if p >= threshold: candles_text.append("█ ", style="red")
                        else: candles_text.append("  ")
                    candles_text.append("\n")
            else:
                candles_text.append("Candle data not available yet.\n", style="dim")
            candles_text.append("\nPress any key to return...", style="dim")
            pairs_panel = Panel(candles_text, title=f"[bold cyan]{symbol} Candles[/]", border_style="bold cyan")
        else:
            pairs_height = console.height - 20
            if pairs_height < 3: pairs_height = 3
            max_pairs_offset = max(0, len(sorted_symbols) - pairs_height)

            if max_pairs_offset > 0 and should_step:
                 if now_ts > pairs_pause_until:
                      if pairs_marquee_dir == 1:
                           if pairs_scroll_offset < max_pairs_offset:
                               pairs_scroll_offset += 1
                           if pairs_scroll_offset >= max_pairs_offset:
                               pairs_marquee_dir = -1
                               pairs_pause_until = now_ts + 2
                      else:
                           if pairs_scroll_offset > 0:
                               pairs_scroll_offset -= 1
                           if pairs_scroll_offset <= 0:
                               pairs_marquee_dir = 1
                               pairs_pause_until = now_ts + 2

            pairs_scroll_offset = max(0, min(pairs_scroll_offset, max_pairs_offset))
            visible_symbols = sorted_symbols[pairs_scroll_offset : pairs_scroll_offset + pairs_height]

            for i, symbol in enumerate(visible_symbols):
                data = bot_state[symbol]
                is_selected = (pairs_scroll_offset + i) == selected_pair_index
                has_position = data.get('position') is not None

                current_signal = "Waiting"
                buy_count = data.get('consecutive_buys', 0)
                sell_count = data.get('consecutive_sells', 0)

                if buy_count > 0: current_signal = f"{buy_count} Buy"
                elif sell_count > 0: current_signal = f"{sell_count} Sell"

                last_order = data.get('last_action', 'Waiting')
                if last_order == "WAITING": last_order = "Waiting"

                # Instruction 3: Bold and bright for new signals (20s delay)
                is_new_signal = (symbol in signal_arrival_times) and (now_ts - signal_arrival_times[symbol] < 20)
                if is_new_signal:
                    signal_style = "bold bright_green" if "Buy" in current_signal else "bold bright_red" if "Sell" in current_signal else "white"
                else:
                    signal_style = "bold green" if "Buy" in current_signal else "bold red" if "Sell" in current_signal else "white"
                last_order_style = "bold green" if last_order == "BUY" else "bold red" if last_order == "SELL" else "white"

                amt_str, entry_str, fee_str = "-", "-", "-"
                if has_position:
                    p = data['position']
                    amt_str = f"{p['amount']:.6f}"
                    entry_str = format_price(p['entry_price'])
                    fee_str = f"{p.get('entry_fee', 0):.4f}"

                tendency = data.get('tendency', 'N/A')
                tend_style = "bold green" if tendency == "Bullish" else "bold red" if tendency == "Bearish" else "bold yellow" if tendency == "Range" else "white"

                row_style = "bold black on yellow" if is_selected else ""

                if expert_mode:
                    flags = []
                    if data.get('whale_active'): flags.append("WHL")
                    if data.get('is_mean_rev'): flags.append("MRV")
                    else: flags.append("TRD")
                    flags_str = ",".join(flags)

                    row_vals = [
                        symbol,
                        f"{format_price(data.get('ema_f', 0))}/{format_price(data.get('ema_s', 0))}",
                        f"{data.get('macd_hist', 0):.4e}" if abs(data.get('macd_hist', 0)) < 0.001 else f"{data.get('macd_hist', 0):.4f}",
                        f"{data.get('rsi', 0):.2f}",
                        f"{data.get('volatility', 0):.4f}/{data.get('adx', 0):.1f}",
                        f"[{'bold cyan' if 'WHL' in flags_str else 'dim white'}]{flags_str}[/]",
                        f"{data.get('score', 0)}",
                        data.get('aggr', 'N/A'),
                        (lambda d: d['all_matching_strategies'][int(now_ts % len(d['all_matching_strategies']))] if 'all_matching_strategies' in d and d['all_matching_strategies'] else d.get('strategy', 'N/A'))(data)
                    ]
                else:
                    row_vals = [
                        symbol,
                        format_price(data.get('price', 0)),
                        amt_str, entry_str, fee_str,
                        format_price(data.get('expected_profit', 0)) if has_position else '0.00',
                        f"[{tend_style}]{tendency}[/]",
                        f"[{last_order_style}]{last_order}[/]",
                        f"[{signal_style}]{current_signal}[/]",
                        data.get('aggr', 'N/A'),
                        (lambda d: d['all_matching_strategies'][int(now_ts % len(d['all_matching_strategies']))] if 'all_matching_strategies' in d and d['all_matching_strategies'] else d.get('strategy', 'N/A'))(data)
                    ]

                table.add_row(*row_vals, style=row_style)

            # Add a spacer row if we are at the end of the list to ensure the last line isn't cut off
            if len(visible_symbols) > 0 and visible_symbols[-1] == sorted_symbols[-1]:
                 num_cols = 10 if expert_mode else 11
                 table.add_row(*([""] * num_cols))

            pairs_panel = Panel(
                table,
                title="[bold]Trading Pairs[/]",
                border_style="bold green" if focused_panel == "pairs" else "cyan"
            )

        # 3. Status Bar Marquee
        status_text = Text()
        status_text.append(f"Update: {datetime.now().strftime('%H:%M:%S')} | Mode: {global_mode.capitalize()} | ", style="bold brown")
        status_text.append(f"Sellable: {', '.join(available_assets) if available_assets else 'None'} | ", style="bold yellow")
        status_text.append("TAB: Switch | Arrows: Scroll | H: Help | Exit: Ctrl+C", style="bold red")

        display_width = console.width - 4
        max_status_offset = max(0, len(status_text) - display_width)

        if max_status_offset > 0:
             if should_step and now_ts > status_pause_until:
                  if status_marquee_dir == 1:
                       if status_scroll_index < max_status_offset:
                            status_scroll_index += 1
                       if status_scroll_index >= max_status_offset:
                            status_marquee_dir = -1
                            status_pause_until = now_ts + 2
                  else:
                       if status_scroll_index > 0:
                            status_scroll_index -= 1
                       if status_scroll_index <= 0:
                            status_marquee_dir = 1
                            status_pause_until = now_ts + 2

             status_scroll_index = max(0, min(status_scroll_index, max_status_offset))
             status_display = status_text[status_scroll_index : status_scroll_index + display_width]
        else:
             status_display = status_text
             status_display.justify = "center"

    if show_help:
        help_text = Text()
        help_text.append("\n[bold cyan]Keyboard Shortcuts:[/]\n", style="white")
        help_text.append("  TAB    : Switch focus between Logs and Pairs\n")
        help_text.append("  UP/DN  : Scroll the focused panel\n")
        help_text.append("  X      : Toggle Expert Mode (Show/Hide Indicators)\n")
        help_text.append("  M      : Toggle Marquee Effect (Pause/Resume scrolling)\n")
        help_text.append("  H      : Close this help menu\n")
        help_text.append("  Ctrl+C : Stop the bot gracefully\n")

        pairs_panel = Panel(help_text, title="[bold]Help / Info[/]", border_style="bold yellow")

    layout = Layout()
    layout.split(
        Layout(Panel(Text("Binance Trading Bot Dashboard", style="bold magenta", justify="center"), border_style="blue"), size=3),
        Layout(log_panel, size=log_height+2),
        Layout(pairs_panel, name="main"),
        Layout(Panel(status_display, title="Status", border_style="cyan"), size=3)
    )
    return layout

def input_thread_func(exchange, data_manager, engine, config):
    global pairs_scroll_offset, logs_scroll_offset, focused_panel
    global pairs_pause_until, logs_pause_until, expert_mode, show_help, marquee_enabled
    global selected_pair_index, show_candles_for_pair, sell_proposal_pair, sell_proposal_time

    while not shutdown_event.is_set():
        try:
            key = readchar.readkey()

            # Handle sell proposal (Instruction 6)
            if sell_proposal_pair and (time.time() - sell_proposal_time < 60):
                if key.lower() == 'y':
                    symbol = sell_proposal_pair
                    data = bot_state.get(symbol, {})
                    if execute_sell(exchange, data_manager, engine, symbol, data, config):
                         with bot_lock:
                             data['last_action'] = 'SELL'
                             data['position'] = None
                         play_sound("sell", config)
                    sell_proposal_pair = None
                    continue
                elif key.lower() == 'n':
                    sell_proposal_pair = None
                    continue
            else:
                sell_proposal_pair = None # Clear if expired

            if show_candles_for_pair:
                show_candles_for_pair = None
                continue

            sorted_symbols = sorted([s for s in bot_state.keys() if not s.startswith("_")])

            if key == readchar.key.TAB:
                focused_panel = "logs" if focused_panel == "pairs" else "pairs"
            elif key == readchar.key.UP:
                if focused_panel == "pairs":
                    selected_pair_index = max(0, selected_pair_index - 1)
                    # Adjust scroll if needed
                    if selected_pair_index < pairs_scroll_offset:
                        pairs_scroll_offset = selected_pair_index
                    pairs_pause_until = time.time() + 5 # Longer pause on manual interaction
                else:
                    logs_scroll_offset = min(500, logs_scroll_offset + 1)
                    logs_pause_until = time.time() + 5
            elif key == readchar.key.DOWN:
                if focused_panel == "pairs":
                    selected_pair_index = min(len(sorted_symbols) - 1, selected_pair_index + 1)
                    # Adjust scroll if needed
                    pairs_height = console.height - 20
                    if selected_pair_index >= pairs_scroll_offset + pairs_height:
                        pairs_scroll_offset = selected_pair_index - pairs_height + 1
                    pairs_pause_until = time.time() + 5
                else:
                    logs_scroll_offset = max(0, logs_scroll_offset - 1)
                    logs_pause_until = time.time() + 5
            elif key == readchar.key.ENTER:
                if focused_panel == "pairs" and sorted_symbols:
                    show_candles_for_pair = sorted_symbols[selected_pair_index]
            elif key.lower() == 'b':
                # Manual Buy (Instruction 3)
                if focused_panel == "pairs" and sorted_symbols:
                    symbol = sorted_symbols[selected_pair_index]
                    data = bot_state[symbol]
                    if not data.get('position'):
                        def manual_buy_task():
                            if execute_buy(exchange, data_manager, engine, symbol, data, config):
                                with bot_lock:
                                    data['last_action'] = 'BUY'
                                    data['position'] = data_manager.get_position(symbol)
                                play_sound("buy", config)
                        threading.Thread(target=manual_buy_task, daemon=True).start()
            elif key.lower() == 's':
                # Manual Sell (Instruction 3)
                if focused_panel == "pairs" and sorted_symbols:
                    symbol = sorted_symbols[selected_pair_index]
                    data = bot_state[symbol]
                    if data.get('position'):
                        def manual_sell_task():
                            if execute_sell(exchange, data_manager, engine, symbol, data, config):
                                with bot_lock:
                                    data['last_action'] = 'SELL'
                                    data['position'] = None
                                play_sound("sell", config)
                        threading.Thread(target=manual_sell_task, daemon=True).start()
            elif key.lower() == 'x':
                expert_mode = not expert_mode
            elif key.lower() == 'm':
                marquee_enabled = not marquee_enabled
            elif key.lower() == 'h':
                show_help = not show_help
            elif key == readchar.key.CTRL_C:
                shutdown_event.set()
                break
        except (KeyboardInterrupt, EOFError):
            shutdown_event.set()
            break
        except Exception: pass

def update_available_assets_live(exchange, config):
    global available_assets, pending_asset_update
    # Randomized delay between 3 and 10 seconds
    delay = random.uniform(3.0, 10.0)
    time.sleep(delay)

    # We use a buffered approach: even if multiple threads were started,
    # only one will actually perform the update if they are synchronized.
    # The 'pending_asset_update' flag in the main loop ensures we don't spawn too many.

    try:
        new_assets = get_sellable_assets(exchange, config)
        with bot_lock:
            available_assets[:] = new_assets
            pending_asset_update = False
    except Exception as e:
        logging.error(f"Failed to update assets from API: {e}")
        with bot_lock: pending_asset_update = False

def trading_thread_func(exchange, data_manager, pattern_manager, engine, config, mode):
    global available_assets, pending_asset_update
    priority_order = config.get('_priority_pairs')
    pairs_dict = config.get('pairs', {})
    pair_keys = priority_order if priority_order else list(pairs_dict.keys())

    last_assets_update = 0
    sim_init_done = False

    # Track inconsistent pairs (Instruction 5)
    inconsistent_pairs = {}
    warning_issued_pairs = set()

    time.sleep(5)
    exchange.load_markets()

    global last_sell_proposal_check, sell_proposal_pair, sell_proposal_profit, sell_proposal_time

    while not shutdown_event.is_set():
        if mode == 'simulation' and not sim_init_done:
            initialize_simulation(exchange, data_manager, pattern_manager, engine, config, bot_state)
            sim_init_done = True

        if mode == 'live' and not sim_init_done:
            # First time load for live
            sync_live_positions(exchange, data_manager, config)
            new_assets = get_sellable_assets(exchange, config)
            with bot_lock:
                available_assets[:] = new_assets
            sim_init_done = True

        try:
            if mode == 'simulation' and time.time() - last_assets_update > 60:
                new_available_assets = get_sellable_assets_sim(data_manager)
                with bot_lock:
                     available_assets[:] = new_available_assets
                last_assets_update = time.time()

            potential_buys = []

            # Parallelize pair analysis
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(pair_keys)) as executor:
                future_to_sym = {executor.submit(analyze_pair, exchange, data_manager, pattern_manager, sym, pairs_dict[sym], config, engine=engine): sym for sym in pair_keys}
                for future in concurrent.futures.as_completed(future_to_sym):
                    if shutdown_event.is_set(): break
                    symbol = future_to_sym[future]
                    if symbol in suspended_pairs: continue
                    try:
                        data = future.result()
                        if data:
                            # API Health Check (Instruction 5)
                            candles = data.get('last_20_candles', [])
                            if len(candles) >= 2:
                                is_variable = any(candles[i] != candles[i-1] for i in range(1, len(candles)))
                                if not is_variable:
                                    if symbol not in inconsistent_pairs:
                                        inconsistent_pairs[symbol] = time.time()
                                    elif time.time() - inconsistent_pairs[symbol] > 300: # 5 minutes
                                        if symbol not in warning_issued_pairs:
                                            logging.warning(f"Pair {symbol} has been inconsistent for 5 minutes. Disabling updates.")
                                            warning_issued_pairs.add(symbol)
                                            suspended_pairs.add(symbol)
                                else:
                                    inconsistent_pairs.pop(symbol, None)

                            with bot_lock:
                                data['last_action'] = bot_state[symbol].get('last_action', 'WAITING')
                                # Instruction 3: Bold and bright new signals
                            old_buy = bot_state.get(symbol, {}).get('buy', False)
                            old_sell = bot_state.get(symbol, {}).get('sell', False)
                            if (data.get('buy') and not old_buy) or (data.get('sell') and not old_sell):
                                signal_arrival_times[symbol] = time.time()
                            bot_state[symbol] = data

                            if data.get('sell_triggered'):
                                 if execute_sell(exchange, data_manager, engine, symbol, data, config):
                                      with bot_lock:
                                          data['last_action'] = 'SELL'
                                          data['position'] = None
                                          if mode == 'live' and not pending_asset_update:
                                              pending_asset_update = True
                                              threading.Thread(target=update_available_assets_live, args=(exchange, config), daemon=True).start()
                                      play_sound("sell", config)

                            if data.get('buy') and not data.get('position'):
                                 potential_buys.append((symbol, data))
                    except Exception as e:
                        logging.error(f"Error analyzing {symbol}: {e}")

            if potential_buys and not shutdown_event.is_set():
                max_open = int(config.get('max_open_positions', 5))
                current_open = len(data_manager.get_open_positions())
                slots_available = max_open - current_open
                if slots_available > 0:
                     # Prioritize by benchmark profit (casted to float for robust sorting)
                     potential_buys.sort(key=lambda x: float(x[1].get('expected_profit', 0)), reverse=True)
                     balance = exchange.fetch_balance()
                     for i in range(min(len(potential_buys), slots_available)):
                          if shutdown_event.is_set(): break
                          symbol, data = potential_buys[i]
                          if execute_buy(exchange, data_manager, engine, symbol, data, config, balance=balance):
                               with bot_lock:
                                   data['last_action'] = 'BUY'
                                   data['position'] = data_manager.get_position(symbol)
                               play_sound("buy", config)
                               # Update balance for next iteration
                               balance = exchange.fetch_balance()

            # Proactive Sell Proposal (Instruction 6)
            now = time.time()
            if (now - bot_start_time > 300) and (now - last_sell_proposal_check > 300):
                last_sell_proposal_check = now
                open_positions = data_manager.get_open_positions()
                for symbol, pos in open_positions.items():
                    if symbol in bot_state:
                        current_price = bot_state[symbol].get('price', 0)
                        entry_price = pos.get('entry_price', 0)
                        if current_price > 0 and entry_price > 0:
                            fee_rate = 0.001
                            try: fee_rate = exchange.fetch_trading_fee(symbol)
                            except: pass

                            is_prof = engine.is_profitable(current_price, entry_price, fee_rate)
                            if is_prof:
                                amount = pos.get('amount', 0)
                                profit = (amount * current_price * (1 - fee_rate)) - pos.get('entry_total_base', 0)
                                if profit > 0:
                                    sell_proposal_pair = symbol
                                    sell_proposal_profit = profit
                                    sell_proposal_time = now
                                    break # Only one proposal at a time

            for _ in range(100):
                 if shutdown_event.is_set(): break
                 time.sleep(0.1)
        except Exception as e:
            logging.error(f"Error in trading thread: {e}")
            time.sleep(5)


def load_ohlcv_cache():
    if os.path.exists('ohlcv_cache.pkl'):
        try:
            with open('ohlcv_cache.pkl', 'rb') as f:
                return pickle.load(f)
        except Exception: return {}
    return {}

def save_ohlcv_cache(cache):
    with open('ohlcv_cache.pkl', 'wb') as f:
        pickle.dump(cache, f)

def main():
    from persistence import load_from_archive
    load_from_archive()
    parser = argparse.ArgumentParser(description='Binance Trading Bot')
    parser.add_argument('--no-gpu', action='store_true', help='Disable GPU acceleration (force CPU)')
    parser.add_argument('--exchange', choices=['binance', 'kraken', 'bitvavo'], default='binance', help='Exchange to use')
    parser.add_argument('--mode', choices=['live', 'simulation', 'sell', 'balance', 'backtest', 'benchmark'], default='simulation', help='Bot mode')
    parser.add_argument('--config', help='Path to config file (optional, defaults to config.json or config.default.json)')
    parser.add_argument('--symbol', help='Target symbol for backtest/benchmark (e.g. BTC/USDT)')
    parser.add_argument('--every-symbol', action='store_true', help='Run benchmark for all configured pairs')

    strat_help = f"Strategy for backtest. Available: {', '.join(STRATEGIES)}"
    parser.add_argument('--strategy', help=strat_help)
    parser.add_argument('--aggr', help='Agressivity for backtest')
    parser.add_argument('--backtest-positions', type=int, default=1, help='Max simultaneous positions in backtest (1-4)')
    parser.add_argument('--term', choices=['short', 'medium', 'long'], default='short', help='Time term for strategy optimization (default: short)')
    parser.add_argument('--since', help='Start date for backtest/benchmark (YYYY-MM-DD HH:MM)')
    parser.add_argument('--until', help='End date for backtest/benchmark (YYYY-MM-DD HH:MM)')

    args = parser.parse_args()

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

    # Load pairs from pairs.txt if available
    if os.path.exists('pairs.txt'):
        with open('pairs.txt', 'r') as f:
            pairs = [line.strip() for line in f if line.strip()]
    else:
        # Final fallbacks or empty list if no pairs.txt
        pairs = []

    config['pairs'] = {p: {} for p in pairs}
    base_currencies = sorted(list(set([p.split('/')[1] for p in pairs if '/' in p])))
    config['base_currencies'] = base_currencies

    # Load API credentials from api.json if available
    api_creds = {}
    if os.path.exists('api.json'):
        try:
            with open('api.json', 'r') as f:
                api_creds = json.load(f)
        except Exception as e:
            console.print(f"[bold red]Error parsing api.json: {e}[/]")

    with console.status("[bold green]Initializing Binance Trading Bot...", spinner="dots") as status:

        # MMX, SSE, AVX Gradation Check (Instruction 6)
        try:
            import cpuinfo
            info = cpuinfo.get_cpu_info()
            flags = info.get('flags', [])
            best_simd = "None"
            if 'mmx' in flags: best_simd = "MMX"
            if 'sse' in flags: best_simd = "SSE"
            if 'avx' in flags or 'avx2' in flags: best_simd = "AVX"
            if 'avx512' in flags: best_simd = "AVX512"
            console.print(f"[bold green]Hardware optimization level detected: {best_simd}[/]")
        except Exception:
            console.print("[yellow]SIMD detection skipped. Ensure CPU instructions are optimized in your environment.[/]")

        if not gpu_enabled:
            console.print("[bold yellow]Warning: GPU acceleration is disabled or no compatible GPU found.[/]")
            console.print("[yellow]Computations will run on CPU, which can be significantly slower (minutes to hours) for the first benchmarks.[/]")
            console.print("[yellow]Please ensure benchmark_cache.json remains intact once finished to avoid re-running slow benchmarks.[/]")
        else:
            console.print(f"[bold green]GPU Acceleration enabled using device: {device}[/]")

        db_handler.duration = 5
        data_manager = DataManager(args.mode) if args.mode in ['live', 'simulation', 'sell'] else None
        pattern_manager = PatternManager()
        engine = TradingEngine(config)

        # Use credentials from api.json if available, otherwise config.default.json
        api_key = api_creds.get('api_key') or config.get('api_key')
        api_secret = api_creds.get('api_secret') or config.get('api_secret')

        if args.mode == 'live':
            if args.exchange == 'binance':
                exchange = BinanceExchange(api_key, api_secret)
            elif args.exchange == 'kraken':

                exchange = KrakenExchange(api_key, api_secret)
            elif args.exchange == 'bitvavo':

                exchange = BitvavoExchange(api_key, api_secret)
            logging.info(f"Starting bot in LIVE mode on {args.exchange}")
        elif args.mode == 'simulation':
            exchange = MockExchange(api_key, api_secret, exchange_type=args.exchange)
            logging.info(f"Starting bot in SIMULATION mode ({args.exchange} discovery)")
        elif args.mode == 'sell':
            exchange = MockExchange(api_key, api_secret) if api_key in [None, "YOUR_API_KEY"] else BinanceExchange(api_key, api_secret)
            exchange.load_markets()
            if hasattr(exchange, 'balance'): exchange.balance['TEST'] = True
            status.stop()  # Stop status before interactive input
            interactive_sell(exchange, data_manager, engine, config)
            return
        elif args.mode == 'balance':
            exchange = MockExchange(api_key, api_secret) if api_key in [None, "YOUR_API_KEY"] else BinanceExchange(api_key, api_secret)
            exchange.load_markets()
            show_balance(exchange, config)
            return
        elif args.mode == 'backtest':
            if not args.symbol:
                console.print("[red]Error: --symbol required for backtest[/]")
                return
            exchange = MockExchange(api_key, api_secret) if api_key in [None, "YOUR_API_KEY"] else BinanceExchange(api_key, api_secret)
            run_backtest_mode(exchange, config, args, engine=engine, device=device)
            return
        elif args.mode == 'benchmark':
            if not args.symbol and not args.every_symbol:
                console.print("[red]Error: --symbol or --every-symbol required for benchmark[/]")
                return
            exchange = MockExchange(api_key, api_secret) if api_key in [None, "YOUR_API_KEY"] else BinanceExchange(api_key, api_secret)
            # Pass data_manager=None in pure benchmark mode to avoid creating trade history files
            run_benchmark_mode(exchange, config, args, status=status, data_manager=None, pattern_manager=pattern_manager, engine=engine, device=device)
            return

        pairs = config.get('pairs', {})
        # Global override for agressivity
        global_agressivity = config.get('force_agressivity_to_all_pairs')

        # Auto-optimization via benchmarking
        if args.mode in ['live', 'simulation']:
            config['_active_term'] = args.term
            status.update(f"[bold blue]Optimizing strategies for {args.term} term...")
            opt_map = run_benchmark_mode(exchange, config, args, term_override=args.term, status=status, data_manager=data_manager, pattern_manager=pattern_manager, engine=engine, device=device)
            # Store profits for prioritization
            _, base_bet_curr = parse_base_bet(config)
            pair_priorities = []
            for sym, data in opt_map.items():
                # data can be a list (patterns) or a single pattern (legacy cache)
                best = data[0] if isinstance(data, list) else data
                if sym in config['pairs']:
                        config['pairs'][sym]['aggr'] = best['aggr']
                        config['pairs'][sym]['strategy'] = best['strategy']

                        # Store patterns in DataManager if not already there (e.g. from cache)
                        if isinstance(data, list):
                             pattern_manager.set_patterns(sym, data)

                        # Score for prioritization (the profit predicted for the term)
                        priority_score = best['profit']
                        config['pairs'][sym]['expected_profit'] = best.get('avg_bench_profit', priority_score)
                        pair_priorities.append((sym, priority_score))
                        if best.get('is_cached'):
                         console.print(f"[bold green][{sym}][/] Optimized from [cyan]cached results[/] to [cyan]{best['strategy']}[/] ([dim]{best['aggr']}[/]) | {args.term.upper()} Term Profit: {format_price(priority_score)} {base_bet_curr}")

                time.sleep(1) # Brief pause after bench

                # Global sort pairs by expected profit for priority execution
                sorted_pairs = [p[0] for p in sorted(pair_priorities, key=lambda x: x[1], reverse=True)]
                config['_priority_pairs'] = sorted_pairs

            if args.mode == 'simulation' and data_manager:
                data_manager.clear_history()

        for symbol in pairs:
            # Check if we already have an open position for this symbol
            pos = data_manager.get_position(symbol)

            # Retrieve optimized settings from config if available (after benchmark)
            pair_cfg = pairs[symbol]
            aggr_val = pair_cfg.get('aggr', 'normal')
            strat_val = pair_cfg.get('strategy', 'simple_ema')
            exp_profit = float(pair_cfg.get('expected_profit', 0))

            bot_state[symbol] = {
                'aggr': aggr_val,
                'strategy': strat_val,
                'last_action': 'BUY' if pos else 'Waiting',
                'position': pos,
                'expected_profit': exp_profit
            }

    threading.Thread(target=input_thread_func, args=(exchange, data_manager, engine, config), daemon=True).start()
    threading.Thread(target=trading_thread_func, args=(exchange, data_manager, pattern_manager, engine, config, args.mode), daemon=True).start()

    play_sound("startup")
    try:
        with Live(make_dashboard(args.mode, config), refresh_per_second=10, console=console, auto_refresh=True) as live:
            while not shutdown_event.is_set():
                live.update(make_dashboard(args.mode, config))
                time.sleep(0.1)
    except (KeyboardInterrupt, SystemExit):
        shutdown_event.set()
    finally:
        shutdown_event.set()
        from persistence import create_consolidated_archive
        save_ohlcv_cache(ohlcv_cache)
        create_consolidated_archive()

    logging.info("Bot stopped gracefully.")

def play_sound(action, config=None):
    system = platform.system().lower()
    try:
        if system == "windows":
            import winsound
            if action == "startup":
                 # Randomized sequence equal to max_open_positions
                 num_blips = int(config.get('max_open_positions', 5)) if config else 5
                 for _ in range(num_blips):
                      freq = random.randint(400, 1200)
                      dur = random.randint(100, 300)
                      winsound.Beep(freq, dur)
                 return
            frequency = 1000 if action == "buy" else 1500
            winsound.Beep(frequency, 200)
        else:
            if action == "startup":
                 sys.stdout.write("\a"); sys.stdout.flush()
                 return
            bell_char = "\a" if action == "buy" else "\a\a"
            sys.stdout.write(bell_char)
            sys.stdout.flush()
    except Exception: pass

def analyze_pair(exchange, data_manager, pattern_manager, symbol, pair_config, global_config, engine=None):
    patterns = pattern_manager.get_patterns(symbol)
    term = global_config.get('_active_term', 'short'); term_cfg = global_config.get('expected_profit_terms', {}).get(term, {})
    timeframe = term_cfg.get('timeframe', '5m')
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=500)
    if not ohlcv: return None
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    with bot_lock:
        if symbol in bot_state: bot_state[symbol]['last_20_candles'] = df['close'].tail(20).tolist()
    df = get_signals(df, {"device": device}, is_backtest=False); latest_row_base = df.iloc[-1]

    # Cross-pair pattern matching (Instruction 1 & 2)
    search_pool = patterns + global_pattern_pool; active_patterns = []
    if search_pool:
        for p in search_pool:
            p_len = len(p['prices'])
            if len(df) < p_len: continue
            buffer_window = df.iloc[-p_len:]; sim = calculate_similarity(buffer_window, p, device=device)
            if sim > 0.70: active_patterns.append((sim, p))
    active_patterns.sort(key=lambda x: x[0], reverse=True); active_pattern = active_patterns[0][1] if active_patterns else None

    if active_pattern:
        strategy_name = active_pattern['strategy']; mode_name = active_pattern['aggr']
        if engine: mode_settings = engine.get_dynamic_settings(latest_row_base.get('adx', 0), latest_row_base.get('volatility', 0))
        else: mode_settings = {"ema_fast": 9, "ema_slow": 21, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "rsi_period": 14, "rsi_buy": 30, "rsi_sell": 70}
        mode_settings['strategy'] = strategy_name; mode_settings['device'] = device
        df = get_signals(df, mode_settings, is_backtest=False); latest_row = df.iloc[-1]

        # Instruction 2b: New Monte Carlo tests pondering with pattern score
        mc_cache = MonteCarloCacheManager()
        candle_ts = int(df.iloc[-1]['timestamp']) if not isinstance(df.iloc[-1]['timestamp'], (pd.Timestamp, datetime)) else int(df.iloc[-1]['timestamp'].timestamp())
        mc_score = mc_cache.get(symbol, timeframe, candle_ts)

        if mc_score is None:
            mc = MonteCarloEngine(num_simulations=1000, timeframe_candles=20); mc.set_device(device); mc_score = mc.validate_strategy(df)
            mc_cache.set(symbol, timeframe, candle_ts, mc_score)

        pattern_score = active_pattern.get('score', 1.0); combined_mc_score = mc_score * (1 + pattern_score)
        if combined_mc_score < 0.5: latest_row['buy_signal'] = False; latest_row['sell_signal'] = False
    else:
        strategy_name = "N/A"; mode_name = "N/A"; latest_row = latest_row_base.copy()
        latest_row['buy_signal'] = False; latest_row['sell_signal'] = False

    exclude = ['open', 'high', 'low', 'close', 'volume', 'buy_candidate', 'sell_candidate', 'ema_up_win', 'macd_up_win', 'rsi_up_win', 'ema_down_win', 'macd_down_win', 'rsi_down_win', 'ema_up', 'ema_down', 'macd_up', 'macd_down', 'rsi_up', 'rsi_down']
    trigger_data = {k: v for k, v in latest_row.to_dict().items() if k not in exclude and not isinstance(v, (pd.Timestamp, datetime))}
    candle_ts = int(latest_row['timestamp']) if not isinstance(latest_row['timestamp'], (pd.Timestamp, datetime)) else int(latest_row['timestamp'].timestamp())
    trigger_data['candle_ts'] = candle_ts; prev_data = bot_state.get(symbol, {}); last_candle_ts = prev_data.get('_last_candle_ts')
    consecutive_buys = prev_data.get('consecutive_buys', 0); consecutive_sells = prev_data.get('consecutive_sells', 0)
    if last_candle_ts is None:
        buy_hist = df['buy_signal'].tolist(); sell_hist = df['sell_signal'].tolist()
        c_buys = 0;
        for s in reversed(buy_hist[-10:]):
            if s: c_buys += 1
            else: break
        c_sells = 0;
        for s in reversed(sell_hist[-10:]):
            if s: c_sells += 1
            else: break
        consecutive_buys = c_buys; consecutive_sells = c_sells
    elif last_candle_ts != candle_ts:
        if latest_row['buy_signal']: consecutive_buys += 1; consecutive_sells = 0
        elif latest_row['sell_signal']: consecutive_sells += 1; consecutive_buys = 0
        else: consecutive_buys = 0; consecutive_sells = 0
    else:
        if not latest_row['buy_signal'] and not latest_row['sell_signal']: consecutive_buys = 0; consecutive_sells = 0

    buy_threshold = 1
    if term == 'medium': buy_threshold = 2
    elif term == 'long': buy_threshold = 3

    return {
        'price': latest_row['close'], 'ema_f': latest_row.get('ema_f', 0), 'ema_s': latest_row.get('ema_s', 0),
        'macd_hist': latest_row.get('macd_hist', 0), 'rsi': latest_row.get('rsi', 0), 'adx': latest_row.get('adx', 0),
        'volatility': latest_row.get('volatility', 0), 'score': latest_row.get('score', 0),
        'whale_active': bool(latest_row.get('whale_active', 0)), 'is_mean_rev': bool(latest_row.get('is_mean_rev', 0)),
        'aggr': mode_name, 'strategy': strategy_name,
        'all_matching_strategies': [ap[1]['strategy'] for ap in active_patterns] if active_patterns else [strategy_name],
        'tendency': latest_row.get('tendency', 'Neutral'), 'buy': consecutive_buys >= buy_threshold, 'sell': consecutive_sells >= 3,
        'consecutive_buys': consecutive_buys, 'consecutive_sells': consecutive_sells, '_last_candle_ts': candle_ts,
        'sell_triggered': consecutive_sells >= 3 and data_manager.get_position(symbol) and not data_manager.get_position(symbol).get('ignore_sell'),
        'position': data_manager.get_position(symbol), 'expected_profit': float(pair_config.get('expected_profit', 0)),
        'trigger_data': trigger_data
    }

def execute_buy(exchange, data_manager, engine, symbol, data, config, balance=None):
    if balance is None:
        balance = exchange.fetch_balance()
    win_streak = data_manager.get_win_streak(symbol)

    # Refresh price for Spot market accuracy (prevent NOTIONAL filters)
    fresh_ticker = exchange.fetch_ticker(symbol)
    current_price = fresh_ticker['last'] if fresh_ticker else data['price']

    base_curr = symbol.split('/')[1]
    amount = engine.calculate_position_size(balance, current_price, base_curr, win_streak=win_streak, exchange=exchange)
    base_currency = symbol.split('/')[1]
    if amount > 0:
        # Check if balance is sufficient before attempting order
        cost = amount * current_price
        base_asset = base_currency
        free_balance = balance.get(base_asset, {}).get('free', 0) if isinstance(balance, dict) and 'free' in balance else balance.get(base_asset, 0)

        if free_balance < cost:
            logging.warning(f"[{symbol}] Buy aborted: Insufficient {base_asset} balance ({format_price(free_balance)} < {format_price(cost)})")
            return False

        order = exchange.create_order(symbol, 'buy', amount)
        if isinstance(order, dict) and 'insufficient balance' in str(order.get('message', '')).lower():
            logging.error(f"[{symbol}] Buy failed: Insufficient balance. Suspending pair.")
            suspended_pairs.add(symbol)
            return False
        if isinstance(order, dict) and 'code' in str(order) and 'Filter failure: NOTIONAL' in str(order):
            logging.error(f"[{symbol}] Buy failed: Filter failure NOTIONAL. Suspending pair.")
            suspended_pairs.add(symbol)
            return False
        if order:
            # Use executed values if available
            exec_price = order.get('average', order.get('price', current_price))
            exec_amount = order.get('filled', order.get('amount', amount))
            fee = order.get('calculated_fee', 0)

            total_paid = (exec_amount * exec_price) + fee
            logging.info(f"[{symbol}] Executing buy of amount {exec_amount:.6f} at {exec_price}, final price paid: {total_paid:.2f} {symbol.split('/')[1] if '/' in symbol else parse_base_bet(config)[1]}")
            data_manager.add_position(symbol, exec_price, exec_amount, fee, data.get('trigger_data', {}), time.time(), total_base=total_paid)

            # Immediately update Sellable list (Instruction 2)
            asset = symbol.split('/')[0]
            with bot_lock:
                if asset not in available_assets:
                    available_assets.append(asset)
                    available_assets.sort()

            return True
        else:
            logging.warning(f"[{symbol}] Buy execution failed: Exchange rejected order for amount {amount:.6f}")
    else:
        logging.warning(f"[{symbol}] Buy aborted: Calculated amount is zero or negative.")
    return False

def execute_sell(exchange, data_manager, engine, symbol, data, config):
    position = data['position']
    should_execute = True

    # Instruction 7: check for "guaranteed" sale price
    ticker = exchange.fetch_ticker(symbol)
    guaranteed_price = ticker['bid'] if ticker and 'bid' in ticker else data['price']

    if should_execute:
        base_asset = symbol.split('/')[0]

        # Bypass balance check for simulation mode
        # In simulation, we trust the internal DataManager state
        is_simulation = isinstance(exchange, MockExchange)

        balance = exchange.fetch_balance()
        free_balance = balance.get(base_asset, {}).get('free', 0) if 'free' in balance else balance.get(base_asset, 0)
        base_currency = symbol.split('/')[1]

        if is_simulation or free_balance >= position['amount']:
            order = exchange.create_order(symbol, 'sell', position['amount'])
            if isinstance(order, dict) and order.get('error') == 'dust_limit':
                logging.warning(f"[{symbol}] Sell aborted: Balance is dust/below precision. Ignoring future sell signals for this position.")
                data_manager.flag_ignore_sell(symbol)
                return False
            if order:
                # Use executed values if available
                exec_price = order.get('average', order.get('price', guaranteed_price))
                exec_amount = order.get('filled', order.get('amount', position['amount']))
                fee = order.get('calculated_fee', 0)

                total_received = (exec_amount * exec_price) - fee
                logging.info(f"[{symbol}] Executing sell of amount {exec_amount:.6f} at {exec_price}, final price received: {total_received:.2f} {symbol.split('/')[1] if '/' in symbol else parse_base_bet(config)[1]}")
                profit = total_received - position.get('entry_total_base', 0)
                data_manager.close_position(symbol, exec_price, fee, profit, data.get('trigger_data', {}), time.time(), total_base=total_received)
                return True
    return False

def initialize_simulation(exchange, data_manager, pattern_manager, engine, config, bot_state):
    logging.info("Initializing Simulation positions (Discovery phase)...")
    sync_live_positions(exchange, data_manager, config)
    # Then proceed with virtual buy signals...
    priority_order = config.get('_priority_pairs')
    pairs_dict = config.get('pairs', {})
    pair_keys = priority_order if priority_order else list(pairs_dict.keys())

    potential_buys = []
    for symbol in pair_keys:
        pair_config = pairs_dict[symbol]
        if not data_manager.get_position(symbol):
            # Pass pair_config to analyze_pair
            data = analyze_pair(exchange, data_manager, pattern_manager, symbol, pair_config, config, engine=engine)
            if data and data.get('buy'):
                potential_buys.append((symbol, data))

    if potential_buys:
        max_open = int(config.get('max_open_positions', 5))
        current_open = len(data_manager.get_open_positions())
        slots_available = max_open - current_open
        if slots_available > 0:
            # Prioritize by benchmark profit
            potential_buys.sort(key=lambda x: float(x[1].get('expected_profit', 0)), reverse=True)
            balance = exchange.fetch_balance()
            for i in range(min(len(potential_buys), slots_available)):
                symbol, data = potential_buys[i]
                if execute_buy(exchange, data_manager, engine, symbol, data, config, balance=balance):
                    with bot_lock:
                        bot_state[symbol]['position'] = data_manager.get_position(symbol)
                        bot_state[symbol]['price'] = data['price']
                        bot_state[symbol]['last_action'] = 'BUY'
                    # Refresh balance for next buy
                    balance = exchange.fetch_balance()

    logging.info(f"Initialization of the simulation positions completed.")

def sync_live_positions(exchange, data_manager, config):
    logging.info(f"Syncing positions from {exchange.__class__.__name__} API...")
    balance = exchange.fetch_balance()
    free_balances = balance.get('free', balance)
    base_currencies = config.get('base_currencies', [parse_base_bet(config)[1]])

    # We clear local cache for Live mode as requested
    data_manager.data['open_positions'] = {}
    sellable_found = False

    for asset, amount in free_balances.items():
        if asset in base_currencies or amount <= 0: continue

        # Find which base currency this asset belongs to
        symbol = None
        for bc in base_currencies:
            candidate = f"{asset}/{bc}"
            if candidate in config.get('pairs', {}):
                symbol = candidate
                break
        if not symbol: continue

        # Check if it's dust
        is_dust = False
        try:
            markets = exchange.markets if hasattr(exchange, 'markets') and exchange.markets else exchange.load_markets()
            if symbol in markets:
                m = markets[symbol]
                min_amt = m['limits']['amount']['min']
                min_cost = m['limits']['cost']['min'] or 10
                ticker = exchange.fetch_ticker(symbol)
                if ticker and (amount < min_amt or (amount * ticker['last']) < min_cost):
                    is_dust = True
            elif amount <= 0.000001: is_dust = True
        except: pass

        if is_dust: continue
        sellable_found = True

        # Try to find purchase price
        entry_price = 0
        trades = exchange.fetch_my_trades(symbol, limit=20)
        if trades:
            buy_trades = [t for t in trades if t['side'] == 'buy']
            if buy_trades:
                entry_price = buy_trades[-1]['price']

        if entry_price > 0:
            logging.info(f"[{symbol}] Found purchase price: {entry_price}. Adding to tracking.")
            data_manager.add_position(symbol, entry_price, amount, 0, {}, time.time())
        else:
            logging.warning(f"[{symbol}] Asset found in wallet but no purchase record found via API. Please connect to exchange and sell manually or manage this asset.")

    if not sellable_found and any(v > 0 for k, v in free_balances.items() if k not in base_currencies):
        has_base_balance = any(free_balances.get(bc, 0) > 10 for bc in base_currencies)
        if not has_base_balance:
            logging.warning("No sellable assets found. Your wallet contains only 'dust' (amounts below exchange limits). Please add funds or use the exchange website to convert dust to a base currency.")
        else:
            logging.info("No non-base sellable assets found, but base currency balance is available.")

def get_sellable_assets_sim(data_manager):
    positions = data_manager.get_open_positions()
    return sorted([s.split('/')[0] for s in positions.keys()])

def get_sellable_assets(exchange, config=None):
    balance = exchange.fetch_balance()
    assets = []
    default_base = parse_base_bet(config)[1] if config else 'USDT'
    base_currencies = config.get('base_currencies', [default_base]) if config else [default_base]
    free_balances = balance.get('free', balance)

    for asset, amount in free_balances.items():
        if not isinstance(amount, (int, float)) or amount <= 0: continue
        if asset in base_currencies or asset == 'USDT': continue

        # Find pair
        symbol = None
        for bc in base_currencies:
            candidate = f"{asset}/{bc}"
            if config and candidate in config.get('pairs', {}):
                symbol = candidate
                break
        if not symbol: continue

        try:
            markets = exchange.markets if hasattr(exchange, 'markets') and exchange.markets else exchange.load_markets()
            if symbol in markets:
                market = markets[symbol]
                min_amount = market['limits']['amount']['min']
                min_cost = market['limits']['cost']['min'] or 10
                ticker = exchange.fetch_ticker(symbol)
                if ticker and (amount < min_amount or (amount * ticker['last']) < min_cost): continue
            elif amount <= 0.000001: continue
            assets.append(asset)
        except Exception:
            if amount > 0.000001: assets.append(asset)
    return sorted(assets)

def interactive_sell(exchange, data_manager, engine, config):
    console.print("\n[bold magenta]=== Interactive Sell Mode (Real Wallet) ===[/]")
    balance = exchange.fetch_balance()
    free_balances = balance.get('free', balance)
    base_currencies = config.get('base_currencies', [parse_base_bet(config)[1]])

    sellable_found = False
    for asset, amount in free_balances.items():
        if asset in base_currencies or asset == 'USDT' or not isinstance(amount, (float, int)) or amount <= 0:
            continue

        # Find pair
        symbol = None
        for bc in base_currencies:
            candidate = f"{asset}/{bc}"
            if candidate in config.get('pairs', {}):
                symbol = candidate
                break
        if not symbol: continue

        # Handle markets access for both BinanceExchange and MockExchange
        markets = {}
        if hasattr(exchange, 'exchange') and exchange.exchange.markets:
            markets = exchange.exchange.markets
        elif hasattr(exchange, 'markets'):
            markets = exchange.markets

        # Skip if no base currency market exists for this asset
        if not markets or symbol not in markets:
            continue

        market = markets[symbol]
        min_amount = market['limits']['amount']['min']
        min_cost = market['limits']['cost']['min'] or 10

        ticker = exchange.fetch_ticker(symbol)
        if not ticker:
            continue

        price = ticker['last']
        cost = amount * price

        if amount < min_amount or cost < min_cost:
            continue

        sellable_found = True
        quote = symbol.split('/')[1] if '/' in symbol else parse_base_bet(config)[1]
        console.print(f"\n[bold cyan]Asset:[/] {asset} | [bold cyan]Balance:[/] {amount:.6f} | [bold cyan]Value:[/] {format_price(cost)} {quote}")

        confirm = input(f"Confirm sell of entire {asset} balance? (y/n): ").lower()
        if confirm == 'y':
            quote = symbol.split('/')[1] if '/' in symbol else parse_base_bet(config)[1]
            console.print(f"[yellow]Selling {amount} {asset} at ~{format_price(price)} {quote}...[/]")
            order = exchange.create_order(symbol, 'sell', amount)
            if order:
                fee = order.get('calculated_fee', 0)
                total_received = (amount * price) - fee
                quote = symbol.split('/')[1] if '/' in symbol else parse_base_bet(config)[1]
                logging.info(f"[{symbol}] Executing sell of amount {amount:.6f} at {price}, final price received: {total_received:.2f} {quote}")
                console.print(f"[bold green]Successfully sold {asset}! Final received: {total_received:.2f} {quote}[/]")
                play_sound("sell", None)
                # Also close position in data manager if it exists
                if data_manager.get_position(symbol):
                    pos = data_manager.get_position(symbol)
                    profit = total_received - pos.get('entry_total_base', 0)
                    data_manager.close_position(symbol, price, fee, profit, {}, time.time(), total_base=total_received)
            else:
                console.print(f"[bold red]Failed to sell {asset}.[/]")
        else:
            console.print(f"[dim]Skipping {asset}.[/]")

    if not sellable_found:
        console.print("[yellow]No sellable assets (above dust threshold) found in your real wallet.[/]")

def show_balance(exchange, config):
    console.print("\n[bold magenta]=== Real Wallet Balance (All Assets) ===[/]")
    balance = exchange.fetch_balance()

    table = Table(title="Asset Inventory", expand=True)
    _, base_bet_curr = parse_base_bet(config)
    table.add_column("Asset", style="cyan")
    table.add_column("Free", justify="right")
    table.add_column("Used", justify="right")
    table.add_column("Total", justify="right")
    table.add_column(f"Estimated Value ({base_bet_curr})", justify="right", style="green")

    # Access balances correctly
    total_balances = balance.get('total', balance)
    free_balances = balance.get('free', {})
    used_balances = balance.get('used', {})

    total_value_base = 0

    # Sort assets alphabetically
    for asset in sorted(total_balances.keys()):
        total = total_balances[asset]
        if not isinstance(total, (int, float)) or total == 0:
            continue

        free = free_balances.get(asset, 0)
        used = used_balances.get(asset, 0)

        val_in_base = 0
        if asset in [base_bet_curr, 'USDT', 'USDC']:
            val_in_base = total # Simplified valuation for base currencies
        else:
            # Try finding any valid pair with this asset as base
            ticker = None
            for bc in [base_bet_curr, 'USDT', 'USDC']:
                candidate = f"{asset}/{bc}"
                ticker = exchange.fetch_ticker(candidate)
                if ticker and ticker.get('last', 0) > 0:
                     val_in_base = total * ticker['last']
                     break
            if not ticker or ticker.get('last', 0) <= 0:
                # Try USDT bridge if base pair not found
                ticker_usdt = exchange.fetch_ticker(f"{asset}/USDT")
                ticker_base_usdt = exchange.fetch_ticker(f"{base_bet_curr}/USDT")
                if ticker_usdt and ticker_base_usdt and ticker_base_usdt['last'] > 0:
                    val_in_base = (total * ticker_usdt['last']) / ticker_base_usdt['last']

        total_value_base += val_in_base
        val_str = format_price(val_in_base) if val_in_base > 0 else "N/A"

        table.add_row(
            asset,
            f"{free:.8f}",
            f"{used:.8f}",
            f"{total:.8f}",
            val_str
        )

    console.print(table)
    console.print(f"\n[bold yellow]Estimated Total Wallet Value: {total_value_base:.2f} {base_bet_curr}[/]\n")

def plot_backtest(df, symbol, strategy_name, aggr_name, results, engine, config):
    """Generates a matplotlib plot for backtesting results."""
    plt.figure(figsize=(12, 7))
    plt.plot(df['timestamp'], df['close'], label='Price', color='blue', alpha=0.6)

    # Plot buy signals
    buys = df[df['buy_signal']]
    plt.scatter(buys['timestamp'], buys['close'], marker='^', color='green', label='BUY Signal', s=100)

    # Plot sell signals
    sells = df[df['sell_signal']]
    plt.scatter(sells['timestamp'], sells['close'], marker='v', color='red', label='SELL Signal', s=100)

    plt.title(f"Backtest: {symbol} | Strategy: {strategy_name} | Aggr: {aggr_name}")
    plt.xlabel("Time")
    plt.ylabel("Price")

    p_str = format_price(results['profit'])
    _, base_bet_curr = parse_base_bet(config)
    stats_text = f"Profit: {p_str} {base_bet_curr}\nWin Rate: {results['win_rate']:.1%}\nMax DD: {results['max_dd']:.1%}"
    plt.annotate(stats_text, xy=(0.02, 0.95), xycoords='axes fraction',
                 bbox=dict(boxstyle="round", fc="w", alpha=0.8), fontsize=10, verticalalignment='top')

    plt.legend()
    plt.grid(True, alpha=0.3)

    # Save plot
    filename = f"backtest_{symbol.replace('/', '_')}_{strategy_name}.png"
    plt.savefig(filename)
    console.print(f"[bold green]Backtest plot saved as {filename}[/]")
    plt.close()

def run_backtest_logic(exchange, symbol, strategy, aggr_name, config, term='short', df_in=None, limit=500, engine=None, device=None, skip_mc=False, return_full_df=False):
    """Core backtesting simulation logic."""
    from indicators import get_signals

    fee_rate = 0.001 # Default 0.1%
    if exchange:
        try:
            fee_rate = exchange.fetch_trading_fee(symbol)
        except Exception:
            pass

    # Use Dynamic Risk Engine if available, otherwise balanced defaults
    if engine and df_in is not None and not df_in.empty:
         # Use the technical state from the end of the data to get dynamic settings
         base_df = get_signals(df_in.copy(), {"device": device if device is not None else torch.device("cpu")}, is_backtest=True)
         latest = base_df.iloc[-1]
         aggr_settings = engine.get_dynamic_settings(latest.get('adx', 0), latest.get('volatility', 0))
    else:
         aggr_settings = {
             "ema_fast": 9, "ema_slow": 21, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
         }

    mc = MonteCarloEngine(num_simulations=100, timeframe_candles=20)
    mc.set_device(device if device is not None else torch.device("cpu"))

    term_settings = config.get('expected_profit_terms', {}).get(term, {})
    if not term_settings:
        return None

    # Copy settings and inject strategy and timeframe
    test_config = aggr_settings.copy()
    test_config['strategy'] = strategy
    timeframe = term_settings.get('timeframe', '5m')

    if df_in is None:
        # Use a large buffer for indicator stability
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        except Exception as e:
            console.print(f"[red]Error fetching OHLCV for {symbol} ({timeframe}): {e}[/]")
            return None

        if not ohlcv:
            console.print(f"[red]No OHLCV returned for {symbol} ({timeframe}).[/]")
            return None

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    else:
        df = df_in.copy()

    if 'buy_signal' not in df.columns:
        try:
            test_config['device'] = device if device is not None else torch.device('cpu')
            df = get_signals(df, test_config, is_backtest=True)
        except Exception as e:
            if exchange is not None:
                 console.print(f"[red]Error calculating signals for {symbol}: {e}[/]")
            return None

    if df is None or df.empty:
        if exchange is not None:
             console.print(f"[red]Signal calculation returned empty for {symbol}.[/]")
        return None

    # Evaluation window (how many candles we actually trade on)
    eval_window_base = term_settings.get('eval_candles', 60)
    max_rand = max(1, int(eval_window_base * 0.1))
    eval_window = eval_window_base + random.randint(-max_rand, max_rand)
    # We always trade on the LAST eval_window candles of df
    start_idx = max(0, len(df) - eval_window)

    if len(df) < eval_window:
        if exchange is not None:
             console.print(f"[yellow]Warning: Only {len(df)} candles available for {symbol}, but term requested {eval_window}.[/]")

    # Simulation
    _, base_bet_curr = parse_base_bet(config)
    balance = 100.0 # Starting virtual balance
    position = None
    trades = []
    equity_curve = []

    # We loop through the whole DF for indicators, but only execute trades in the eval window
    for i in range(len(df)):
        if i < start_idx:
            equity_curve.append(balance)
            continue

        row = df.iloc[i]
        price = row['close']

        # Sell logic
        if position and row['sell_signal']:
            revenue = price * position['amount']
            fee = revenue * fee_rate
            revenue_net = revenue - fee

            profit = revenue_net - position['entry_cost']
            balance += revenue_net
            trades.append({'profit': profit})
            position = None

        # Buy logic
        base_bet_cfg = config.get('base_bet', '10.0 USDT')
        if isinstance(base_bet_cfg, str):
            try:
                raw_val = float(base_bet_cfg.split(' ')[0])
            except ValueError:
                raw_val = 10.0
        else:
            raw_val = float(base_bet_cfg)
        base_percentage = raw_val / 100.0 if raw_val >= 1.0 else raw_val
        trade_amount = balance * base_percentage
        if not position and row['buy_signal'] and balance >= trade_amount:
            fee = trade_amount * fee_rate
            cost_total = trade_amount + fee

            if balance >= cost_total:
                buy_amount = trade_amount / price
                balance -= cost_total
                position = {'entry_price': price, 'amount': buy_amount, 'entry_cost': cost_total}

        equity_curve.append(balance + (position['amount'] * price if position else 0))

    # Stats
    total_profit = equity_curve[-1] - 100.0
    if len(equity_curve) > start_idx:
        total_profit = equity_curve[-1] - equity_curve[start_idx]

    # Monte Carlo Validation
    if not skip_mc:
        mc_score = mc.validate_strategy(df)
        total_profit *= mc_score # Penalize if MC validation is low
    else:
        mc_score = 1.0

    wins = [t for t in trades if t['profit'] > 0]
    win_rate = len(wins) / len(trades) if trades else 0

    # Drawdown
    equity_series = pd.Series(equity_curve)
    max_dd = (equity_series.cummax() - equity_series).max() / equity_series.cummax().max() if not equity_series.empty else 0

    # Determine evaluation date range
    eval_df = df.iloc[start_idx:] if start_idx < len(df) else df.iloc[-1:]
    start_time_dt = eval_df['timestamp'].iloc[0]
    end_time_dt = eval_df['timestamp'].iloc[-1]

    # Store technical state at the end of the window for pattern matching
    latest = df.iloc[-1]
    tech_state = {
        'rsi': float(latest.get('rsi', 50)),
        'adx': float(latest.get('adx', 0)),
        'ema_f': float(latest.get('ema_f', 0)),
        'ema_s': float(latest.get('ema_s', 0))
    }

    return {
        'df': df,
        'profit': total_profit,
        'profit_raw': equity_curve[-1] - equity_curve[start_idx] if len(equity_curve) > start_idx else 0,
        'win_rate': win_rate,
        'max_dd': max_dd,
        'trades_count': len(trades),
        'start_time': start_time_dt.strftime("%Y-%m-%d %H:%M"),
        'end_time': end_time_dt.strftime("%Y-%m-%d %H:%M"),
        'start_ts': start_time_dt.timestamp(),
        'prices': eval_df['close'].tolist(),
        'tech_state': tech_state,
        'equity_curve': equity_curve if return_full_df else []
    }

def run_backtest_mode(exchange, config, args, engine=None, device=None):
    # Default strategy for backtest
    default_strategy = "simple_ema"

    strategy = args.strategy or default_strategy
    aggr = args.aggr or config.get('force_agressivity_to_all_pairs', 'normal')
    term = getattr(args, 'term', 'short')

    if strategy not in STRATEGIES:
        console.print(f"[bold red]Error: Strategy '{strategy}' not found.[/]")
        console.print(f"Available strategies: {', '.join(STRATEGIES)}")
        console.print("[dim]Please check for typos.[/]")
        return

    console.print(f"[bold blue]Running Backtest for {args.symbol} | Strategy: {strategy} | Aggr: {aggr} | Term: {term}...[/]")
    results = run_backtest_logic(exchange, args.symbol, strategy, aggr, config, term=term, engine=engine, device=device)

    if results:
        if results['trades_count'] > 0:
            plot_backtest(results['df'], args.symbol, strategy, aggr, results, engine, config)
        else:
            console.print("[yellow]No trades executed during backtest. Plot not generated.[/]")

        console.print(f"\n[bold yellow]Backtest Summary for {args.symbol}:[/]")
        _, base_bet_curr = parse_base_bet(config)
        console.print(f"Total Profit: {format_price(results['profit'])} {base_bet_curr}")
        console.print(f"Win Rate: {results['win_rate']:.1%}")
        console.print(f"Max Drawdown: {results['max_dd']:.1%}")
        console.print(f"Total Trades: {results['trades_count']}")
    else:
        console.print(f"[red]Backtest failed for {args.symbol} using {strategy} ({aggr}). Check symbol and aggr settings.[/]")

def run_benchmark_for_symbol(symbol, config, term_to_test, aggrs, strategies, df_in, engine=None, device=None):
    """
    Scans historical data for the top 4 success patterns using a high-performance single-pass approach.
    """
    if df_in is None or len(df_in) < 100: return symbol, []

    term_cfg = config.get('expected_profit_terms', {}).get(term_to_test, {})
    eval_window_base = term_cfg.get('eval_candles', 60)
    max_rand = max(1, int(eval_window_base * 0.1))
    eval_window = eval_window_base + random.randint(-max_rand, max_rand)
    patterns = []
    now_ts = time.time()

    from indicators import get_signals

    # We use 'dynamic' as the default aggr for benchmarking
    aggr = aggrs[0] if aggrs else 'dynamic'

    # Instruction 8: Convert thresholds to base currency
    quote = symbol.split('/')[1]
    threshold_conv = 1.0
    _, base_bet_curr = parse_base_bet(config)
    if quote != base_bet_curr:
        try:
            ticker = exchange.fetch_ticker(f'{base_bet_curr}/{quote}')
            if ticker and ticker.get('last'):
                threshold_conv = ticker['last']
        except: pass

    profit_threshold = config.get('profit_thresholds', {}).get('min_pattern_profit', 0.015) * threshold_conv

    for strategy in strategies:
        # Prepare settings
        if engine:
            mode_settings = engine.get_dynamic_settings(25.0, 0.001)
        else:
            mode_settings = {
                "ema_fast": 9, "ema_slow": 21, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
            }
        mode_settings['strategy'] = strategy
        mode_settings['device'] = device if device is not None else torch.device("cpu")

        # 1. Calculate signals once for the entire dataset
        try:
            full_df = get_signals(df_in.copy(), mode_settings, is_backtest=True)
        except Exception:
            continue

        # 2. Run a single backtest for the entire dataset
        # We set eval_candles to the whole length to get a continuous equity curve
        full_term_config = copy.deepcopy(config)
        # Create a temporary term that covers the whole DF
        full_term_name = f"full_{strategy}"
        full_term_config['expected_profit_terms'][full_term_name] = {
            'eval_candles': len(full_df),
            'timeframe': term_cfg.get('timeframe', '5m')
        }

        res_full = run_backtest_logic(None, symbol, strategy, aggr, full_term_config,
                                     term=full_term_name, df_in=full_df, engine=engine,
                                     device=device, skip_mc=True, return_full_df=True)

        if not res_full or not res_full.get('equity_curve'):
            continue

        equity = res_full['equity_curve']

        # 3. Slide window over the equity curve to find profitable periods
        # Complexity is now O(N) instead of O(N*W) backtest runs
        max_offset = len(full_df) - eval_window
        step = 5

        for offset in range(0, max_offset, step):
            start_idx = offset
            end_idx = offset + eval_window

            # Profit in this window
            win_profit = equity[end_idx-1] - equity[start_idx]

            if win_profit < profit_threshold:
                continue

            # Score with Recency Pondering
            window_ts = full_df['timestamp'].iloc[start_idx].timestamp()
            age_hours = (now_ts - window_ts) / 3600
            recency_score = 1.0
            if term_to_test == 'short':
                 if age_hours > 24: recency_score = 0.8
                 if age_hours > 168: recency_score = 0.5
                 if age_hours > 720: recency_score = 0.2
            elif term_to_test == 'medium':
                 if age_hours > 168: recency_score = 0.8
                 if age_hours > 720: recency_score = 0.5

            final_score = win_profit * recency_score

            # For technical state, we take the values at the end of the window
            latest_row = full_df.iloc[end_idx-1]
            tech_state = {
                'rsi': float(latest_row.get('rsi', 50)),
                'adx': float(latest_row.get('adx', 0)),
                'ema_f': float(latest_row.get('ema_f', 0)),
                'ema_s': float(latest_row.get('ema_s', 0))
            }

            patterns.append({
                'profit': win_profit,
                'score': final_score,
                'strategy': strategy,
                'aggr': aggr,
                'symbol': symbol,
                'start_time': full_df['timestamp'].iloc[start_idx].strftime("%Y-%m-%d %H:%M"),
                'end_time': full_df['timestamp'].iloc[end_idx-1].strftime("%Y-%m-%d %H:%M"),
                'start_ts': window_ts,
                'prices': full_df['close'].iloc[start_idx:end_idx].tolist(),
                'tech_state': tech_state
            })

    # Keep top 4 non-overlapping (by time) patterns
    patterns.sort(key=lambda x: x['score'], reverse=True)
    unique_patterns = []
    seen_times = []

    for p in patterns:
        if len(unique_patterns) >= 10: break
        is_overlap = False
        for st in seen_times:
            if abs(p['start_ts'] - st) < (eval_window * 60):
                is_overlap = True
                break
        if not is_overlap:
            # NOW apply Monte Carlo validation to the top patterns only (for speed)
            # Find the window in df_in
            # Use searchsorted for O(log N) instead of O(N)
            p_start_ts_dt = pd.to_datetime(p['start_ts'], unit='s')
            p_start_idx = df_in['timestamp'].searchsorted(p_start_ts_dt)

            if p_start_idx != -1:
                window_df = df_in.iloc[max(0, p_start_idx-250):p_start_idx+eval_window]
                mc = MonteCarloEngine(num_simulations=100, timeframe_candles=20)
                mc.set_device(device if device is not None else torch.device("cpu"))
                mc_score = mc.validate_strategy(window_df)
                p['profit'] *= mc_score
                p['score'] *= mc_score

            unique_patterns.append(p)
            seen_times.append(p.get('start_ts', 0))

    return symbol, unique_patterns

def run_benchmark_mode(exchange, config, args, term_override=None, status=None, data_manager=None, pattern_manager=None, engine=None, device=None):

    # Respect global overrides if they exist
    global_aggr = config.get('force_agressivity_to_all_pairs')
    global_strat = config.get('force_strategy_to_all_pairs')

    # Agressivity is now dynamic
    aggrs = [global_aggr] if global_aggr else ['dynamic']
    strategies = [global_strat] if global_strat else STRATEGIES

    cache_mgr = CacheManager()

    # Cache validity mapping (1hr per evaluation term)
    terms_cfg = config.get('expected_profit_terms', {})
    validity_map = {
        'short': terms_cfg.get('short', {}).get('duration_hours', 1) * 3600,
        'medium': terms_cfg.get('medium', {}).get('duration_hours', 24) * 3600,
        'long': terms_cfg.get('long', {}).get('duration_hours', 168) * 3600
    }

    symbols = [args.symbol] if (hasattr(args, 'symbol') and args.symbol) else list(config.get('pairs', {}).keys())

    # Best per symbol
    best_per_symbol = {}

    # Best performers across all symbols
    best_overall = {
        'short': {'profit': -999999, 'params': None},
        'medium': {'profit': -999999, 'params': None},
        'long': {'profit': -999999, 'params': None},
        'total': {'profit': -999999, 'params': None}
    }

    optimization_map = {}
    # Use the currency from base_bet for display
    _, base_bet_curr = parse_base_bet(config)

    # If explicit benchmark mode (no term_override and not backtest), we scan all terms
    # Otherwise we scan just the requested term.
    # Actually, to fulfill Instruction 3, we should ensure we scan what is requested.
    term_to_test = term_override if term_override else getattr(args, 'term', 'short')
    term_cfg = config.get('expected_profit_terms', {}).get(term_to_test, {})
    timeframe = term_cfg.get('timeframe', '5m')

    symbols_to_bench = []
    for symbol in symbols:
        if term_override:
            cached_patterns = cache_mgr.get(symbol, term_override, validity_map.get(term_override, 3600))
            if cached_patterns:
                # Context-based invalidation: check if market context evolved too much
                best = cached_patterns[0]
                try:
                    # Small limit to minimize bandwidth and rate-limit risk
                    ohlcv_now = exchange.fetch_ohlcv(symbol, timeframe, limit=20)
                    if ohlcv_now:
                        df_now = pd.DataFrame(ohlcv_now, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        df_now = get_signals(df_now, {"device": device}, is_backtest=False)
                        # Reuse similarity logic for invalidation
                        sim = calculate_similarity(df_now.iloc[-len(best['prices']):] if len(df_now) >= len(best['prices']) else df_now, best, device=device)
                        if sim < 0.70:
                             logging.info(f"[{symbol}] Cache invalidated: Market context evolved too much (sim: {sim:.2f})")
                             cache_mgr.delete(symbol, term_override)
                             symbols_to_bench.append(symbol)
                             continue
                except Exception as e:
                    logging.warning(f"[{symbol}] Failed to verify cache context: {e}")

                best['is_cached'] = True
                optimization_map[symbol] = best
                if data_manager:
                    pattern_manager.set_patterns(symbol, cached_patterns)
                continue
        symbols_to_bench.append(symbol)

    if symbols_to_bench:
        msg = f"Benchmarking all strategies for {len(symbols_to_bench)} symbol(s) using multi-processing..."
        if status: status.update(f"[bold blue]{msg}")
        else: console.print(f"[bold blue]{msg}")

        # Pre-fetch historical data for all symbols in the process
        ohlcv_cache = load_ohlcv_cache()
        symbol_data_map = {}
        term_cfg = config.get('expected_profit_terms', {}).get(term_to_test, {})
        timeframe = term_cfg.get('timeframe', '5m')

        # Date filtering logic
        since_ts = None
        if args.since:
             try: since_ts = int(datetime.strptime(args.since, "%Y-%m-%d %H:%M").timestamp() * 1000)
             except Exception: console.print(f"[red]Invalid --since format. Use YYYY-MM-DD HH:MM[/]")

        for i, symbol in enumerate(symbols_to_bench):
            all_ohlcv = []
            target_date = datetime(2021, 1, 1); target_ts = int(target_date.timestamp() * 1000)
            current_since = since_ts if since_ts else target_ts
            if status: status.update(f"[bold cyan][{i+1}/{len(symbols_to_bench)}] Fetching history for {symbol}...")
            try:
                cache_key = f"{symbol}_{timeframe}_deep"
                if cache_key in ohlcv_cache:
                    symbol_data_map[symbol] = ohlcv_cache[cache_key]; continue
                while True:
                    try:
                        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=current_since, limit=1000)
                    except Exception as e:
                        logging.warning(f"[{symbol}] Failed to fetch OHLCV at {current_since}: {e}")
                        break
                    if not ohlcv or len(ohlcv) == 0: break
                    all_ohlcv.extend(ohlcv); current_since = ohlcv[-1][0] + 1
                    if len(all_ohlcv) > 100000: break
                    if status: status.update(f"[bold cyan][{i+1}/{len(symbols_to_bench)}] Fetching {symbol}: {len(all_ohlcv)} candles...")
                if all_ohlcv:
                    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                    symbol_data_map[symbol] = df; ohlcv_cache[cache_key] = df
            except Exception as e:
                if not status: console.print(f"[red]Failed to fetch {symbol}: {e}")

        def handle_bench_shutdown(sig, frame):
             shutdown_event.set()
             executor.shutdown(wait=False, cancel_futures=True)
             sys.exit(0)

        if status: status.update('[bold yellow]Analyzing patterns and optimizing strategies...')
        # On CPU with oneDNN, ThreadPoolExecutor might be more efficient for many small torch tasks
        # than ProcessPoolExecutor which has pickling overhead.
        executor_class = concurrent.futures.ProcessPoolExecutor
        with executor_class() as executor:
            # Register signal handler during optimization
            original_handler = signal.signal(signal.SIGINT, handle_bench_shutdown)
            try:
                futures = [executor.submit(run_benchmark_for_symbol, sym, config, term_to_test, aggrs, strategies, symbol_data_map[sym], engine, device)
                           for sym in symbol_data_map]
                for future in concurrent.futures.as_completed(futures):
                    if shutdown_event.is_set(): break
                    sym, patterns = future.result()
                    if patterns:
                        if len(patterns) < 10: msg_target.print(f'[bold yellow]Warning: {sym} has only {len(patterns)} successful patterns (history too short).[/]')
                        # Instruction 4: Bench is average of techniques >= 0.22 base_bet_curr
                        winning_patterns = [p for p in patterns if p['profit'] >= 0.22]
                        if winning_patterns:
                            avg_profit = sum(p['profit'] for p in winning_patterns) / len(winning_patterns)
                        else:
                            avg_profit = patterns[0]['profit']

                        best_for_symbol = patterns[0].copy()
                        best_for_symbol['avg_bench_profit'] = avg_profit
                        best_per_symbol[sym] = best_for_symbol

                        # Store patterns in DataManager for real-time matching
                        if data_manager:
                             pattern_manager.set_patterns(sym, patterns)

                        period_str = f" [dim](From {best_for_symbol.get('start_time')} to {best_for_symbol.get('end_time')})[/]"
                        # Always save patterns to benchmark_cache.json
                        cache_mgr.set(sym, term_to_test, patterns)

                        msg_target = status.console if status else console
                        if term_override:
                            optimization_map[sym] = best_for_symbol
                            msg_target.print(f"\n[bold green]🏆 BEST FOR {sym} ({term_override}):[/] [bold]{best_for_symbol['strategy']} ({best_for_symbol['aggr']})[/] | Bench: {format_price(best_for_symbol['avg_bench_profit'])} {base_bet_curr}{period_str}")
                        else:
                            msg_target.print(f"\n[bold green]🏆 BEST FOR {sym}:[/] [bold]{best_for_symbol['strategy']} ({best_for_symbol['aggr']})[/] | Bench: {format_price(best_for_symbol['avg_bench_profit'])} {base_bet_curr}{period_str}")

                        if best_overall.get(term_to_test) and best_for_symbol['profit'] > best_overall[term_to_test]['profit']:
                            best_overall[term_to_test] = {'profit': best_for_symbol['profit'], 'params': (best_for_symbol['strategy'], best_for_symbol['aggr'], sym)}

                        # Use a generic 'total' score if no term specified
                        if best_for_symbol['profit'] > best_overall['total']['profit']:
                             best_overall['total'] = {'profit': best_for_symbol['profit'], 'params': (best_for_symbol['strategy'], best_for_symbol['aggr'], sym)}
            finally:
                save_ohlcv_cache(ohlcv_cache)
                from persistence import create_consolidated_archive
                create_consolidated_archive()
                signal.signal(signal.SIGINT, original_handler)

    # If we are in optimization mode for live/sim, return the map
    if term_override:
        if status: status.update('[bold green]Optimization complete.')
        if best_per_symbol:
            time.sleep(3)
        # Instruction 1d & 2: populate global pool for cross-pair matching
        with bot_lock:
            global_pattern_pool.clear()
            for sym in optimization_map:
                patterns = pattern_manager.get_patterns(sym)
                global_pattern_pool.extend(patterns)
        return optimization_map

    console.print("\n[bold magenta]=== BENCHMARK RECOMMENDATIONS ===[/]")
    found_any = False
    for term in ['short', 'medium', 'long', 'total']:
        label = terms_cfg.get(term, {}).get('label', term.upper())
        data = best_overall.get(term)
        if not data: continue
        if data['params']:
            found_any = True
            strat, aggr, sym = data['params']
            console.print(f"[{label}] Best Performance on {sym}:")
            console.print(f"  > [bold cyan]Strategy:[/] {strat}")
            console.print(f"  > [bold cyan]Agressivity:[/] {aggr}")
            console.print(f"  > [bold green]Estimated Gain:[/] {format_price(data['profit'])} {base_bet_curr}\n")

    if not found_any:
        # Instruction 8: Convert message threshold to base currency
        # We take the first pair's quote currency as a representative
        _, base_bet_curr = parse_base_bet(config)
        msg_threshold = f"0.022 {base_bet_curr}"
        if symbols:
            quote = symbols[0].split('/')[1]
            if quote != base_bet_curr:
                try:
                    ticker = exchange.fetch_ticker(f'{base_bet_curr}/{quote}')
                    if ticker and ticker.get('last'):
                        msg_threshold = f"{config.get('profit_thresholds', {}).get('no_patterns_msg_threshold', 0.022) * ticker['last']:.3f} {quote}"
                except: pass

        console.print(f"[yellow]No successful patterns (> {msg_threshold}) were found in the scanned historical data.[/]")
    else:
        # Final check: if some symbols returned nothing, let the user know
        for sym in symbols_to_bench:
            if sym not in best_per_symbol:
                 console.print(f"[dim][{sym}] No profitable patterns found in current scan.[/]")

if __name__ == "__main__":
    main()

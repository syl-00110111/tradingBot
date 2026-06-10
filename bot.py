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

from exchange_handler import BinanceExchange, MockExchange
from indicators import get_signals, calculate_similarity, STRATEGIES
from persistence import DataManager, CacheManager
from trading_engine import TradingEngine
from monte_carlo import MonteCarloEngine

# Global controls for dashboard
pairs_scroll_offset = 0
logs_scroll_offset = 0
focused_panel = "pairs"
all_logs = []
status_scroll_index = 0
expert_mode = False
show_help = False
marquee_enabled = True
shutdown_event = threading.Event()

# Marquee Timing Control
last_marquee_update = 0
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
            table.add_column("B.Prof", style="bold green", no_wrap=True)
            table.add_column("Agressivity", style="white", no_wrap=True)
            table.add_column("Strategy", style="bold cyan", no_wrap=True)
        else:
            table.add_column("Pair", style="cyan", no_wrap=True)
            table.add_column("Price", style="magenta", no_wrap=True)
            table.add_column("Amt", style="cyan", no_wrap=True)
            table.add_column("Entry", style="magenta", no_wrap=True)
            table.add_column(f"Fee ({config.get('base_currency', 'EUR')})", style="red", no_wrap=True)
            table.add_column("B.Prof", style="bold green", no_wrap=True)
            table.add_column("Tendency", style="bold white", no_wrap=True)
            table.add_column("Last Order", style="bold", no_wrap=True)
            table.add_column("Signal", style="bold", no_wrap=True)
            table.add_column("Agressivity", style="white", no_wrap=True)
            table.add_column("Strategy", style="bold cyan", no_wrap=True)

        sorted_symbols = sorted([s for s in bot_state.keys() if not s.startswith("_")])
        # Calculate exactly available height: Header(3) + Logs(10) + Status(3) + Panel Border(2) = 18
        # Increased to 20 to provide more margin and avoid cutting off rows.
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

        for symbol in visible_symbols:
            data = bot_state[symbol]
            has_position = data.get('position') is not None

            # Show actual strategy signal with count
            current_signal = "Waiting"
            buy_count = data.get('consecutive_buys', 0)
            sell_count = data.get('consecutive_sells', 0)

            if buy_count > 0: current_signal = f"{buy_count} Buy"
            elif sell_count > 0: current_signal = f"{sell_count} Sell"

            last_order = data.get('last_action', 'Waiting')
            if last_order == "WAITING": last_order = "Waiting"

            signal_style = "bold green" if "Buy" in current_signal else "bold red" if "Sell" in current_signal else "white"
            last_order_style = "bold green" if last_order == "BUY" else "bold red" if last_order == "SELL" else "white"

            amt_str = "-"
            entry_str = "-"
            fee_str = "-"
            if has_position:
                 p = data['position']
                 amt_str = f"{p['amount']:.6f}"
                 entry_str = format_price(p['entry_price'])
                 fee_str = f"{p.get('entry_fee', 0):.4f}"

            tendency = data.get('tendency', 'N/A')
            tend_style = "bold green" if tendency == "Bullish" else "bold red" if tendency == "Bearish" else "bold yellow" if tendency == "Range" else "white"

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
                      format_price(data.get('expected_profit', 0)),
                      data.get('aggr', 'N/A'),
                      data.get('strategy', 'N/A')
                 ]
            else:
                 row_vals = [
                      symbol,
                      format_price(data.get('price', 0)),
                      amt_str, entry_str, fee_str,
                      format_price(data.get('expected_profit', 0)),
                      f"[{tend_style}]{tendency}[/]",
                      f"[{last_order_style}]{last_order}[/]",
                      f"[{signal_style}]{current_signal}[/]",
                      data.get('aggr', 'N/A'),
                      data.get('strategy', 'N/A')
                 ]

            table.add_row(*row_vals)

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

def input_thread_func():
    global pairs_scroll_offset, logs_scroll_offset, focused_panel
    global pairs_pause_until, logs_pause_until, expert_mode, show_help, marquee_enabled
    while not shutdown_event.is_set():
        try:
            key = readchar.readkey()
            if key == readchar.key.TAB:
                focused_panel = "logs" if focused_panel == "pairs" else "pairs"
            elif key == readchar.key.UP:
                if focused_panel == "pairs":
                    pairs_scroll_offset = max(0, pairs_scroll_offset - 1)
                    pairs_pause_until = time.time() + 5 # Longer pause on manual interaction
                else:
                    logs_scroll_offset = min(500, logs_scroll_offset + 1)
                    logs_pause_until = time.time() + 5
            elif key == readchar.key.DOWN:
                if focused_panel == "pairs":
                    pairs_scroll_offset += 1
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
            elif key == readchar.key.CTRL_C:
                shutdown_event.set()
                break
        except (KeyboardInterrupt, EOFError):
            shutdown_event.set()
            break
        except Exception: pass

def update_available_assets_live(exchange):
    global available_assets, pending_asset_update
    # Randomized delay between 3 and 10 seconds
    delay = random.uniform(3.0, 10.0)
    time.sleep(delay)

    # We use a buffered approach: even if multiple threads were started,
    # only one will actually perform the update if they are synchronized.
    # The 'pending_asset_update' flag in the main loop ensures we don't spawn too many.

    try:
        new_assets = get_sellable_assets(exchange)
        with bot_lock:
            available_assets[:] = new_assets
            pending_asset_update = False
    except Exception as e:
        logging.error(f"Failed to update assets from API: {e}")
        with bot_lock: pending_asset_update = False

def trading_thread_func(exchange, data_manager, engine, config, mode):
    global available_assets, pending_asset_update
    priority_order = config.get('_priority_pairs')
    pairs_dict = config.get('pairs', {})
    pair_keys = priority_order if priority_order else list(pairs_dict.keys())

    last_assets_update = 0
    sim_init_done = False

    time.sleep(5)
    exchange.load_markets()

    while not shutdown_event.is_set():
        if mode == 'simulation' and not sim_init_done:
            initialize_simulation(exchange, data_manager, engine, config, bot_state)
            sim_init_done = True

        if mode == 'live' and not sim_init_done:
            # First time load for live
            sync_live_positions(exchange, data_manager, config)
            new_assets = get_sellable_assets(exchange)
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
                future_to_sym = {executor.submit(analyze_pair, exchange, data_manager, sym, pairs_dict[sym], config, engine=engine): sym for sym in pair_keys}
                for future in concurrent.futures.as_completed(future_to_sym):
                    if shutdown_event.is_set(): break
                    symbol = future_to_sym[future]
                    try:
                        data = future.result()
                        if data:
                            with bot_lock:
                                data['last_action'] = bot_state[symbol].get('last_action', 'WAITING')
                                bot_state[symbol] = data

                            if data.get('sell_triggered'):
                                 if execute_sell(exchange, data_manager, engine, symbol, data):
                                      with bot_lock:
                                          data['last_action'] = 'SELL'
                                          data['position'] = None
                                          if mode == 'live' and not pending_asset_update:
                                              pending_asset_update = True
                                              threading.Thread(target=update_available_assets_live, args=(exchange,), daemon=True).start()
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

            for _ in range(100):
                 if shutdown_event.is_set(): break
                 time.sleep(0.1)
        except Exception as e:
            logging.error(f"Error in trading thread: {e}")
            time.sleep(5)

def main():
    parser = argparse.ArgumentParser(description='Binance Trading Bot')
    parser.add_argument('--no-gpu', action='store_true', help='Disable GPU acceleration (force CPU)')
    parser.add_argument('--mode', choices=['live', 'simulation', 'sell', 'balance', 'backtest', 'benchmark'], default='simulation', help='Bot mode')
    parser.add_argument('--config', help='Path to config file (optional, defaults to config.json or config.default.json)')
    parser.add_argument('--symbol', help='Target symbol for backtest/benchmark (e.g. BTC/EUR)')
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
            torch.set_num_threads(1) # Optimized for parallel workers
            gpu_enabled = True
        elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            device = torch.device('mps')
            gpu_enabled = True
        else:
            device = torch.device('cpu')
            gpu_enabled = False

    if args.config:
        config = load_config_from_path(args.config)
    else:
        config = load_config()

    # Load API credentials from api.json if available
    api_creds = {}
    if os.path.exists('api.json'):
        try:
            with open('api.json', 'r') as f:
                api_creds = json.load(f)
        except Exception as e:
            console.print(f"[bold red]Error parsing api.json: {e}[/]")

    with console.status("[bold green]Initializing Binance Trading Bot...", spinner="dots") as status:
        if not gpu_enabled:
            console.print("[bold yellow]Warning: GPU acceleration is disabled or no compatible GPU found.[/]")
            console.print("[yellow]Computations will run on CPU, which can be significantly slower (minutes to hours) for the first benchmarks.[/]")
            console.print("[yellow]Please ensure benchmark_cache.json remains intact once finished to avoid re-running slow benchmarks.[/]")
        else:
            console.print(f"[bold green]GPU Acceleration enabled using device: {device}[/]")

        db_handler.duration = 5
        data_manager = None
        if args.mode in ['live', 'simulation', 'sell']:
            data_manager = DataManager(args.mode)
        engine = TradingEngine(config)

        # Use credentials from api.json if available, otherwise config.default.json
        api_key = api_creds.get('api_key') or config.get('api_key')
        api_secret = api_creds.get('api_secret') or config.get('api_secret')

        if args.mode == 'live':
            exchange = BinanceExchange(api_key, api_secret)
            logging.info("Starting bot in LIVE mode")
        elif args.mode == 'simulation':
            exchange = MockExchange(api_key, api_secret)
            logging.info("Starting bot in SIMULATION mode")
        elif args.mode == 'sell':
            exchange = MockExchange(api_key, api_secret) if api_key in [None, "YOUR_API_KEY"] else BinanceExchange(api_key, api_secret)
            exchange.load_markets()
            if hasattr(exchange, 'balance'): exchange.balance['TEST'] = True
            interactive_sell(exchange, data_manager, engine)
            return
        elif args.mode == 'balance':
            exchange = MockExchange(api_key, api_secret) if api_key in [None, "YOUR_API_KEY"] else BinanceExchange(api_key, api_secret)
            exchange.load_markets()
            show_balance(exchange)
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
            run_benchmark_mode(exchange, config, args, status=status, data_manager=None, engine=engine, device=device)
            return

        pairs = config.get('pairs', {})
        # Global override for agressivity
        global_agressivity = config.get('force_agressivity_to_all_pairs')

        # Auto-optimization via benchmarking
        if args.mode in ['live', 'simulation']:
            config['_active_term'] = args.term
            status.update(f"[bold blue]Optimizing strategies for {args.term} term...")
            opt_map = run_benchmark_mode(exchange, config, args, term_override=args.term, status=status, data_manager=data_manager, engine=engine, device=device)
            # Store profits for prioritization
            pair_priorities = []
            for sym, data in opt_map.items():
                # data can be a list (patterns) or a single pattern (legacy cache)
                best = data[0] if isinstance(data, list) else data
                if sym in config['pairs']:
                        # Force scientific defaults for specific pairs (Urquhart, 2016; Zhang et al., 2020)
                        if sym == 'BTC/EUR':
                            best['strategy'] = 'double_ema_macd_rsi' # Recommends MACD/RSI
                        elif sym == 'ETH/EUR':
                            best['strategy'] = 'stochastic_rsi' # Recommends Stochastic

                        config['pairs'][sym]['aggr'] = best['aggr']
                        config['pairs'][sym]['strategy'] = best['strategy']

                        # Store patterns in DataManager if not already there (e.g. from cache)
                        if isinstance(data, list):
                             data_manager.set_patterns(sym, data)

                        # Score for prioritization (the profit predicted for the term)
                        priority_score = best['profit']
                        config['pairs'][sym]['expected_profit'] = priority_score
                        pair_priorities.append((sym, priority_score))
                        if best.get('is_cached'):
                             console.print(f"[bold green][{sym}][/] Optimized from [cyan]cached results[/] to [cyan]{best['strategy']}[/] ([dim]{best['aggr']}[/]) | {args.term.upper()} Term Profit: {format_price(priority_score)} EUR")

                time.sleep(1) # Brief pause after bench

                # Global sort pairs by expected profit for priority execution
                sorted_pairs = [p[0] for p in sorted(pair_priorities, key=lambda x: x[1], reverse=True)]
                config['_priority_pairs'] = sorted_pairs

        for symbol in pairs:
            # Check if we already have an open position for this symbol
            pos = data_manager.get_position(symbol)

            # Retrieve optimized settings from config if available (after benchmark)
            pair_cfg = pairs[symbol]
            aggr_val = pair_cfg.get('aggr', 'normal')
            strat_val = pair_cfg.get('strategy', 'double_ema_macd_rsi')
            exp_profit = float(pair_cfg.get('expected_profit', 0))

            bot_state[symbol] = {
                'aggr': aggr_val,
                'strategy': strat_val,
                'last_action': 'BUY' if pos else 'Waiting',
                'position': pos,
                'expected_profit': exp_profit
            }

    threading.Thread(target=input_thread_func, daemon=True).start()
    threading.Thread(target=trading_thread_func, args=(exchange, data_manager, engine, config, args.mode), daemon=True).start()

    play_sound("startup")
    try:
        with Live(make_dashboard(args.mode, config), refresh_per_second=10, console=console, auto_refresh=True) as live:
            while not shutdown_event.is_set():
                live.update(make_dashboard(args.mode, config))
                time.sleep(0.1)
    except KeyboardInterrupt:
        shutdown_event.set()

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

def analyze_pair(exchange, data_manager, symbol, pair_config, global_config, engine=None):
    # Retrieve patterns for matching
    patterns = data_manager.get_patterns(symbol)

    # Timeframe now comes from the expected_profit_terms based on the bot's term (default short)
    term = global_config.get('_active_term', 'short')
    term_cfg = global_config.get('expected_profit_terms', {}).get(term, {})
    timeframe = term_cfg.get('timeframe', '5m')

    # Large buffer for indicator stability + pattern matching (500 is enough)
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=500)
    if not ohlcv: return None
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    # Pre-calculate common indicators for regime detection
    df = get_signals(df, {"device": device}, is_backtest=False)
    latest_row_base = df.iloc[-1]

    # 1. Pattern Matching logic
    active_pattern = None
    if patterns:
        best_sim = 0
        for p in patterns:
            p_len = len(p['prices'])
            if len(df) < p_len: continue

            buffer_window = df.iloc[-p_len:]
            sim = calculate_similarity(buffer_window, p)
            if sim > 0.70 and sim > best_sim: # Lowered threshold to 70% for better responsiveness
                best_sim = sim
                active_pattern = p

    # 2. Dynamic Activation
    if active_pattern:
        strategy_name = active_pattern['strategy']
        mode_name = active_pattern['aggr']

        # Use Dynamic Risk Engine if engine is available
        if engine:
            mode_settings = engine.get_dynamic_settings(latest_row_base.get('adx', 0), latest_row_base.get('volatility', 0))
        else:
            # Fallback to balanced defaults if no engine
            mode_settings = {
                "ema_fast": 20, "ema_slow": 50, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
                "rsi_period": 14, "rsi_buy": 30, "rsi_sell": 70, "confirmation_window": 3
            }

        mode_settings['strategy'] = strategy_name
        mode_settings['device'] = device
        df = get_signals(df, mode_settings, is_backtest=False)
        latest_row = df.iloc[-1]
    else:
        # No pattern active -> N/A and No Signals
        strategy_name = "N/A"
        mode_name = "N/A"
        latest_row = latest_row_base.copy()
        latest_row['buy_signal'] = False
        latest_row['sell_signal'] = False

    # Clean up trigger data
    exclude = ['open', 'high', 'low', 'close', 'volume', 'buy_candidate', 'sell_candidate', 'ema_up_win', 'macd_up_win', 'rsi_up_win', 'ema_down_win', 'macd_down_win', 'rsi_down_win', 'ema_up', 'ema_down', 'macd_up', 'macd_down', 'rsi_up', 'rsi_down']
    trigger_data = {k: v for k, v in latest_row.to_dict().items() if k not in exclude and not isinstance(v, (pd.Timestamp, datetime))}

    # Store candle timestamp for signal tracking
    candle_ts = int(latest_row['timestamp']) if not isinstance(latest_row['timestamp'], (pd.Timestamp, datetime)) else int(latest_row['timestamp'].timestamp())
    trigger_data['candle_ts'] = candle_ts

    # Consecutive signal logic
    prev_data = bot_state.get(symbol, {})
    last_candle_ts = prev_data.get('_last_candle_ts')

    consecutive_buys = prev_data.get('consecutive_buys', 0)
    consecutive_sells = prev_data.get('consecutive_sells', 0)

    # RESTART FIX: If first run, look back at historical signals to pick up current trend
    if last_candle_ts is None:
        buy_hist = df['buy_signal'].tolist()
        sell_hist = df['sell_signal'].tolist()

        c_buys = 0
        for s in reversed(buy_hist):
            if s: c_buys += 1
            else: break

        c_sells = 0
        for s in reversed(sell_hist):
            if s: c_sells += 1
            else: break

        consecutive_buys = c_buys
        consecutive_sells = c_sells
    elif last_candle_ts != candle_ts:
        if latest_row['buy_signal']:
            consecutive_buys += 1
            consecutive_sells = 0
        elif latest_row['sell_signal']:
            consecutive_sells += 1
            consecutive_buys = 0
        else:
            consecutive_buys = 0
            consecutive_sells = 0
    else:
        # If it's the same candle, keep existing counts unless signal lost
        if not latest_row['buy_signal'] and not latest_row['sell_signal']:
            consecutive_buys = 0
            consecutive_sells = 0

    # Dynamic confirmation window based on term (Instruction 2)
    # Short: 3, Medium: 2, Long: 1
    buy_threshold = 3
    if term == 'medium': buy_threshold = 2
    elif term == 'long': buy_threshold = 1

    return {
        'price': latest_row['close'],
        'ema_f': latest_row.get('ema_f', 0),
        'ema_s': latest_row.get('ema_s', 0),
        'macd_hist': latest_row.get('macd_hist', 0),
        'rsi': latest_row.get('rsi', 0),
        'adx': latest_row.get('adx', 0),
        'volatility': latest_row.get('volatility', 0),
        'score': latest_row.get('score', 0),
        'whale_active': bool(latest_row.get('whale_active', 0)),
        'is_mean_rev': bool(latest_row.get('is_mean_rev', 0)),
        'aggr': mode_name,
        'strategy': strategy_name,
        'tendency': latest_row.get('tendency', 'Neutral'),
        'buy': consecutive_buys >= buy_threshold,
        'sell': consecutive_sells >= 3, # Keep 3 for sells as it's for risk reduction
        'consecutive_buys': consecutive_buys,
        'consecutive_sells': consecutive_sells,
        '_last_candle_ts': candle_ts,
        'sell_triggered': consecutive_sells >= 3 and data_manager.get_position(symbol),
        'position': data_manager.get_position(symbol),
        'expected_profit': float(pair_config.get('expected_profit', 0)),
        'trigger_data': trigger_data
    }

def execute_buy(exchange, data_manager, engine, symbol, data, global_config, balance=None):
    if balance is None:
        balance = exchange.fetch_balance()
    win_streak = data_manager.get_win_streak(symbol)

    amount = engine.calculate_position_size(
        balance, data['price'],
        win_streak=win_streak
    )
    base_currency = global_config.get('base_currency', 'EUR')
    if amount > 0:
        # Check if balance is sufficient before attempting order
        cost = amount * data['price']
        base_asset = base_currency
        free_balance = balance.get(base_asset, {}).get('free', 0) if isinstance(balance, dict) and 'free' in balance else balance.get(base_asset, 0)

        if free_balance < cost:
            logging.warning(f"[{symbol}] Buy aborted: Insufficient {base_asset} balance ({format_price(free_balance)} < {format_price(cost)})")
            return False

        order = exchange.create_order(symbol, 'buy', amount)
        if order:
            fee = order.get('calculated_fee', 0)
            total_paid = (amount * data['price']) + fee
            logging.info(f"[{symbol}] Executing buy of amount {amount:.6f} at {data['price']}, final price paid: {total_paid:.2f} {base_currency}")
            data_manager.add_position(symbol, data['price'], amount, fee, data.get('trigger_data', {}), time.time(), total_base=total_paid)
            return True
        else:
            logging.warning(f"[{symbol}] Buy execution failed: Exchange rejected order for amount {amount:.6f}")
    else:
        logging.warning(f"[{symbol}] Buy aborted: Calculated amount is zero or negative.")
    return False

def execute_sell(exchange, data_manager, engine, symbol, data):
    position = data['position']
    fee_rate = exchange.fetch_trading_fee(symbol)

    is_profitable = engine.is_profitable(data['price'], position['entry_price'], fee_rate=fee_rate)

    should_execute = False
    if is_profitable:
        should_execute = True
    else:
        # Not profitable
        if engine.config.get('secure_sell', False):
            candle_ts = data.get('trigger_data', {}).get('candle_ts')
            if data_manager.increment_sell_signals(symbol, candle_ts):
                pos_updated = data_manager.get_position(symbol)
                count = pos_updated.get('sell_signals_received', 0)
                if count >= 2:
                    logging.info(f"[{symbol}] Stop-loss triggered: {count}nd SELL signal received at non-profitable price.")
                    should_execute = True
                else:
                    logging.info(f"[{symbol}] Sell signal ignored (secure_sell=True and not profitable), count: {count}")
                    return False
            else:
                # Already counted this candle
                return False
        else:
            # secure_sell is False: sell even if not profitable on 1st signal
            should_execute = True

    if should_execute:
        base_asset = symbol.split('/')[0]

        # Bypass balance check for simulation mode
        # In simulation, we trust the internal DataManager state
        is_simulation = isinstance(exchange, MockExchange)

        balance = exchange.fetch_balance()
        free_balance = balance.get(base_asset, {}).get('free', 0) if 'free' in balance else balance.get(base_asset, 0)
        base_currency = engine.base_currency

        if is_simulation or free_balance >= position['amount']:
            order = exchange.create_order(symbol, 'sell', position['amount'])
            if order:
                fee = order.get('calculated_fee', 0)
                amount = position['amount']
                total_received = (amount * data['price']) - fee
                logging.info(f"[{symbol}] Executing sell of amount {amount:.6f} at {data['price']}, final price received: {total_received:.2f} {base_currency}")
                profit = total_received - position.get('entry_total_base', 0)
                data_manager.close_position(symbol, data['price'], fee, profit, data.get('trigger_data', {}), time.time(), total_base=total_received)
                return True
    return False

def initialize_simulation(exchange, data_manager, engine, config, bot_state):
    logging.info("Initializing Simulation positions...")
    priority_order = config.get('_priority_pairs')
    pairs_dict = config.get('pairs', {})
    pair_keys = priority_order if priority_order else list(pairs_dict.keys())

    potential_buys = []
    for symbol in pair_keys:
        pair_config = pairs_dict[symbol]
        if not data_manager.get_position(symbol):
            # Pass pair_config to analyze_pair
            data = analyze_pair(exchange, data_manager, symbol, pair_config, config, engine=engine)
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
    logging.info("Syncing positions from Binance API...")
    balance = exchange.fetch_balance()
    free_balances = balance.get('free', balance)
    base_currency = config.get('base_currency', 'EUR')

    # We clear local cache for Live mode as requested
    data_manager.data['open_positions'] = {}

    for asset, amount in free_balances.items():
        if asset == base_currency or amount <= 0: continue
        symbol = f"{asset}/{base_currency}"

        # Try to find entry price from last trades
        trades = exchange.fetch_my_trades(symbol, limit=5)
        entry_price = 0
        if trades:
            # Find last buy
            buy_trades = [t for t in trades if t['side'] == 'buy']
            if buy_trades:
                entry_price = buy_trades[-1]['price']

        if entry_price > 0:
            data_manager.add_position(symbol, entry_price, amount, 0, {}, time.time())

def get_sellable_assets_sim(data_manager):
    positions = data_manager.get_open_positions()
    return sorted([s.split('/')[0] for s in positions.keys()])

def get_sellable_assets(exchange):
    balance = exchange.fetch_balance()
    assets = []
    base_currency = 'EUR'

    # Access free balance
    free_balances = balance.get('free', balance)

    for asset, amount in free_balances.items():
        if not isinstance(amount, (int, float)) or amount <= 0:
            continue
        if asset in [base_currency, 'USDT']:
            continue

        symbol = f"{asset}/{base_currency}"
        try:
            # Handle markets access for both BinanceExchange and MockExchange
            markets = {}
            if hasattr(exchange, 'exchange') and exchange.exchange.markets:
                markets = exchange.exchange.markets
            elif hasattr(exchange, 'markets'):
                markets = exchange.markets

            # Check limits if markets are loaded
            if symbol in markets:
                market = markets[symbol]
                min_amount = market['limits']['amount']['min']
                min_cost = market['limits']['cost']['min'] or 10

                # Check minimum amount
                if min_amount and amount < min_amount:
                    continue

                # Check minimum cost (10 EUR typically)
                ticker = exchange.fetch_ticker(symbol)
                if ticker and (amount * ticker['last']) < min_cost:
                    continue
            elif amount <= 0.000001: # Fallback for unknown markets
                continue

            assets.append(asset)
        except Exception:
            if amount > 0.000001:
                assets.append(asset)

    return sorted(assets)

def interactive_sell(exchange, data_manager, engine):
    console.print("\n[bold magenta]=== Interactive Sell Mode (Real Wallet) ===[/]")
    balance = exchange.fetch_balance()
    free_balances = balance.get('free', balance)
    base_currency = 'EUR'

    sellable_found = False
    for asset, amount in free_balances.items():
        if asset in [base_currency, 'USDT'] or not isinstance(amount, (int, float)) or amount <= 0:
            continue

        symbol = f"{asset}/{base_currency}"

        # Handle markets access for both BinanceExchange and MockExchange
        markets = {}
        if hasattr(exchange, 'exchange') and exchange.exchange.markets:
            markets = exchange.exchange.markets
        elif hasattr(exchange, 'markets'):
            markets = exchange.markets

        # Skip if no EUR market exists for this asset
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
        console.print(f"\n[bold cyan]Asset:[/] {asset} | [bold cyan]Balance:[/] {amount:.6f} | [bold cyan]Value:[/] {format_price(cost)} {base_currency}")

        confirm = input(f"Confirm sell of entire {asset} balance? (y/n): ").lower()
        if confirm == 'y':
            console.print(f"[yellow]Selling {amount} {asset} at ~{format_price(price)} {base_currency}...[/]")
            order = exchange.create_order(symbol, 'sell', amount)
            if order:
                fee = order.get('calculated_fee', 0)
                total_received = (amount * price) - fee
                logging.info(f"[{symbol}] Executing sell of amount {amount:.6f} at {price}, final price received: {total_received:.2f} {base_currency}")
                console.print(f"[bold green]Successfully sold {asset}! Final received: {total_received:.2f} {base_currency}[/]")
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

def show_balance(exchange):
    console.print("\n[bold magenta]=== Real Wallet Balance (All Assets) ===[/]")
    balance = exchange.fetch_balance()

    table = Table(title="Asset Inventory", expand=True)
    table.add_column("Asset", style="cyan")
    table.add_column("Free", justify="right")
    table.add_column("Used", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Estimated Value (EUR)", justify="right", style="green")

    # Access balances correctly
    total_balances = balance.get('total', balance)
    free_balances = balance.get('free', {})
    used_balances = balance.get('used', {})

    total_eur_value = 0

    # Sort assets alphabetically
    for asset in sorted(total_balances.keys()):
        total = total_balances[asset]
        if not isinstance(total, (int, float)) or total == 0:
            continue

        free = free_balances.get(asset, 0)
        used = used_balances.get(asset, 0)

        eur_val = 0
        if asset == 'EUR':
            eur_val = total
        else:
            ticker = exchange.fetch_ticker(f"{asset}/EUR")
            if ticker:
                eur_val = total * ticker['last']
            else:
                # Try USDT bridge if EUR pair not found
                ticker_usdt = exchange.fetch_ticker(f"{asset}/USDT")
                ticker_eur_usdt = exchange.fetch_ticker("EUR/USDT")
                if ticker_usdt and ticker_eur_usdt:
                    eur_val = (total * ticker_usdt['last']) / ticker_eur_usdt['last']

        total_eur_value += eur_val
        val_str = format_price(eur_val) if eur_val > 0 else "N/A"

        table.add_row(
            asset,
            f"{free:.8f}",
            f"{used:.8f}",
            f"{total:.8f}",
            val_str
        )

    console.print(table)
    console.print(f"\n[bold yellow]Estimated Total Wallet Value: {total_eur_value:.2f} EUR[/]\n")

def plot_backtest(df, symbol, strategy_name, aggr_name, results):
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
    stats_text = f"Profit: {p_str} EUR\nWin Rate: {results['win_rate']:.1%}\nMax DD: {results['max_dd']:.1%}"
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
             "ema_fast": 20, "ema_slow": 50, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
             "rsi_period": 14, "rsi_buy": 30, "rsi_sell": 70, "confirmation_window": 3
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
    if "buy_signal" not in df.columns:
        try:
            test_config["device"] = device if device is not None else torch.device("cpu")
            df = get_signals(df, test_config, is_backtest=True)
        except Exception as e:
            if exchange is not None:
                 console.print(f"[red]Error calculating signals for {symbol}: {e}[/]")
            return None
    else:
        # Signals already present in df_in
        pass
        if exchange is not None:
             console.print(f"[red]Error calculating signals for {symbol}: {e}[/]")
        return None

    if df is None or df.empty:
        if exchange is not None:
             console.print(f"[red]Signal calculation returned empty for {symbol}.[/]")
        return None

    # Evaluation window (how many candles we actually trade on)
    eval_window = term_settings.get('eval_candles', 60)
    # We always trade on the LAST eval_window candles of df
    start_idx = max(0, len(df) - eval_window)

    if len(df) < eval_window:
        if exchange is not None:
             console.print(f"[yellow]Warning: Only {len(df)} candles available for {symbol}, but term requested {eval_window}.[/]")

    # Simulation
    balance = 100.0 # Starting virtual EUR
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
        trade_amount = float(config.get('base_trade_amount', 20.0))
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
    # Scientific defaults for specific pairs (Urquhart, 2016; Zhang et al., 2020)
    default_strategy = "double_ema_macd_rsi"
    if args.symbol == 'BTC/EUR': default_strategy = "double_ema_macd_rsi" # MACD/RSI
    elif args.symbol == 'ETH/EUR': default_strategy = "stochastic_rsi" # Stochastic

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
            plot_backtest(results['df'], args.symbol, strategy, aggr, results)
        else:
            console.print("[yellow]No trades executed during backtest. Plot not generated.[/]")

        console.print(f"\n[bold yellow]Backtest Summary for {args.symbol}:[/]")
        console.print(f"Total Profit: {format_price(results['profit'])} EUR")
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
    eval_window = term_cfg.get('eval_candles', 60)
    patterns = []
    now_ts = time.time()

    from indicators import get_signals

    # We use 'dynamic' as the default aggr for benchmarking
    aggr = aggrs[0] if aggrs else 'dynamic'

    for strategy in strategies:
        # Prepare settings
        if engine:
            mode_settings = engine.get_dynamic_settings(25.0, 0.001)
        else:
            mode_settings = {
                "ema_fast": 20, "ema_slow": 50, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
                "rsi_period": 14, "rsi_buy": 30, "rsi_sell": 70, "confirmation_window": 3
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
        full_term_config = config.copy()
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

            if win_profit < 0.015:
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
        if len(unique_patterns) >= 4: break
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

def run_benchmark_mode(exchange, config, args, term_override=None, status=None, data_manager=None, engine=None, device=None):

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
    # If explicit benchmark mode (no term_override and not backtest), we scan all terms
    # Otherwise we scan just the requested term.
    # Actually, to fulfill Instruction 3, we should ensure we scan what is requested.
    term_to_test = term_override if term_override else getattr(args, 'term', 'short')

    symbols_to_bench = []
    for symbol in symbols:
        if term_override:
            cached_patterns = cache_mgr.get(symbol, term_override, validity_map.get(term_override, 3600))
            if cached_patterns:
                # cached_patterns is a list of pattern dicts
                best = cached_patterns[0]
                best['is_cached'] = True
                optimization_map[symbol] = best
                if data_manager:
                    data_manager.set_patterns(symbol, cached_patterns)
                continue
        symbols_to_bench.append(symbol)

    if symbols_to_bench:
        msg = f"Benchmarking all strategies for {len(symbols_to_bench)} symbol(s) using multi-processing..."
        if status: status.update(f"[bold blue]{msg}")
        else: console.print(f"[bold blue]{msg}")

        # Pre-fetch historical data for all symbols in the process
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
            target_limit = 10000
            current_since = since_ts

            if status: status.update(f"[bold cyan][{i+1}/{len(symbols_to_bench)}] Fetching up to {target_limit} candles for {symbol}...")

            try:
                # Paginate fetch to bypass API limits
                while len(all_ohlcv) < target_limit:
                    fetch_limit = min(1000, target_limit - len(all_ohlcv))
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=current_since, limit=fetch_limit)
                    if not ohlcv or len(ohlcv) == 0: break

                    all_ohlcv.extend(ohlcv)
                    # Move since pointer to last candle + 1ms
                    current_since = ohlcv[-1][0] + 1
                    if len(ohlcv) < fetch_limit: break

                if all_ohlcv:
                    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

                    # Filter by --until if provided
                    if args.until:
                         try:
                              until_dt = datetime.strptime(args.until, "%Y-%m-%d %H:%M")
                              df = df[df['timestamp'] <= until_dt]
                         except Exception: pass

                    symbol_data_map[symbol] = df
                    if not status: console.print(f"[dim][{symbol}] Successfully fetched {len(df)} candles.[/]")
                else:
                    if not status: console.print(f"[yellow]No OHLCV returned for {symbol} ({timeframe}) during pre-fetch.[/]")
            except Exception as e:
                if not status: console.print(f"[red]Failed to fetch {symbol} for benchmark: {e}[/]")

        def handle_bench_shutdown(sig, frame):
             shutdown_event.set()
             executor.shutdown(wait=False, cancel_futures=True)
             sys.exit(0)

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
                        # For now, the 'best' is the first pattern in the list (highest score)
                        best_for_symbol = patterns[0]
                        best_per_symbol[sym] = best_for_symbol

                        # Store patterns in DataManager for real-time matching
                        if data_manager:
                             data_manager.set_patterns(sym, patterns)

                        period_str = f" [dim](From {best_for_symbol.get('start_time')} to {best_for_symbol.get('end_time')})[/]"
                        if term_override:
                            optimization_map[sym] = best_for_symbol
                            cache_mgr.set(sym, term_override, patterns) # Cache all patterns
                            console.print(f"\n[bold green]🏆 BEST FOR {sym} ({term_override}):[/] [bold]{best_for_symbol['strategy']} ({best_for_symbol['aggr']})[/] | Profit: {format_price(best_for_symbol['profit'])} EUR{period_str}")
                        else:
                            console.print(f"\n[bold green]🏆 BEST FOR {sym}:[/] [bold]{best_for_symbol['strategy']} ({best_for_symbol['aggr']})[/] | Profit: {format_price(best_for_symbol['profit'])} EUR{period_str}")

                        if best_overall.get(term_to_test) and best_for_symbol['profit'] > best_overall[term_to_test]['profit']:
                            best_overall[term_to_test] = {'profit': best_for_symbol['profit'], 'params': (best_for_symbol['strategy'], best_for_symbol['aggr'], sym)}

                        # Use a generic 'total' score if no term specified
                        if best_for_symbol['profit'] > best_overall['total']['profit']:
                             best_overall['total'] = {'profit': best_for_symbol['profit'], 'params': (best_for_symbol['strategy'], best_for_symbol['aggr'], sym)}
            finally:
                signal.signal(signal.SIGINT, original_handler)

    # If we are in optimization mode for live/sim, return the map
    if term_override:
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
            console.print(f"  > [bold green]Estimated Gain:[/] {format_price(data['profit'])} EUR\n")

    if not found_any:
        console.print("[yellow]No successful patterns (> 0.022 profit) were found in the scanned historical data.[/]")
    else:
        # Final check: if some symbols returned nothing, let the user know
        for sym in symbols_to_bench:
            if sym not in best_per_symbol:
                 console.print(f"[dim][{sym}] No profitable patterns found in current scan.[/]")

if __name__ == "__main__":
    main()

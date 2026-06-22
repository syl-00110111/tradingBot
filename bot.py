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
import plotext as plt_ascii
import torch
from datetime import datetime, timedelta, timezone

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
from persistence import DataManager, CacheManager, PatternManager
from trading_engine import TradingEngine
from monte_carlo import MonteCarloEngine

# Global controls for dashboard
pairs_scroll_offset = 0
selected_pair_index = 0
show_chart = False
chart_symbol = None
chart_cache = {"symbol": None, "last_update": 0, "content": None}
logs_scroll_offset = 0
focused_panel = "pairs"
ohlcv_cache = {}
ohlcv_cache_lock = threading.Lock()
all_logs = []
status_scroll_index = 0
expert_mode = False
show_help = False
marquee_enabled = False
shutdown_event = threading.Event()
suspended_pairs = set()

# Marquee Timing Control
last_marquee_update = 0
pairs_pause_until = 0
logs_pause_until = 0
status_pause_until = 0

# State shared between threads
bot_state = {}
bot_lock = threading.Lock()

def format_price(price):
    if price is None: return "-"
    if not isinstance(price, (int, float)): return str(price)
    if price == 0: return "0.0000000"
    return f"{price:.7f}"

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

def get_optimal_timeframe(exchange, symbol, config):
    """
    Dynamically determines the optimal timeframe for a pair based on volume, spread, volatility, and trades/min.
    """
    thresholds = config.get('timeframe_thresholds', {})

    try:
        ticker = exchange.fetch_ticker(symbol)
        ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=60)
        trades = exchange.fetch_trades(symbol, limit=1000)

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

        if score >= 2: tf = '1m'
        elif score == 1: tf = '3m'
        elif score <= 0 and score >= -1: tf = '5m'
        elif score == -2: tf = '15m'
        else: tf = '30m'

        # logging.info(f"[{symbol}] Optimal timeframe: {tf} (Score: {score}, Reasons: {', '.join(reasons)})")
        return tf, score, reasons

    except Exception as e:
        logging.warning(f"Error determining timeframe for {symbol}: {e}. Defaulting to 1m.")
        return '1m', 0, ["Error"]

def render_ascii_chart(symbol, config):
    global chart_cache
    timeframe = config['pairs'].get(symbol, {}).get('timeframe', '1m')
    cache_key = f"{symbol}_{timeframe}"

    with ohlcv_cache_lock:
        if cache_key not in ohlcv_cache or ohlcv_cache[cache_key].empty:
            return Text(f"No data available for {symbol}", style="bold red")
        df = ohlcv_cache[cache_key].copy()

    # Limit to last 100 candles for the chart
    df = df.tail(100)
    last_ts = int(df.iloc[-1]['timestamp'].timestamp())

    # Performance: Check cache
    if chart_cache["symbol"] == symbol and chart_cache["last_update"] == last_ts:
         return chart_cache["content"]

    plt_ascii.clf()
    plt_ascii.theme('dark')
    plt_ascii.title(f"K-Lines: {symbol} ({timeframe})")

    # Use numeric indices for x-axis to avoid parsing crashes (ValueError: Date Form should be...)
    indices = list(range(len(df)))
    df_plot = df[['open', 'high', 'low', 'close']].copy()
    df_plot.columns = ['Open', 'High', 'Low', 'Close']
    df_plot.reset_index(drop=True, inplace=True)

    plt_ascii.candlestick(indices, df_plot)

    # Set xticks manually for labels
    if len(df) > 5:
         step = len(df) // 5
         tick_indices = list(range(0, len(df), step))
         tick_labels = [df.iloc[i]['timestamp'].strftime("%H:%M") for i in tick_indices]
         plt_ascii.xticks(tick_indices, tick_labels)

    # Get plot size from console
    width = console.width - 10
    height = console.height - 15
    if width < 20: width = 20
    if height < 10: height = 10

    plt_ascii.plotsize(width, height)
    content = Text.from_ansi(plt_ascii.build())

    # Update cache
    chart_cache = {
         "symbol": symbol,
         "last_update": last_ts,
         "content": content
    }
    return content

def make_dashboard(global_mode, config):
    now = datetime.now()
    now_ts = time.time()
    global status_scroll_index, pairs_scroll_offset, logs_scroll_offset
    global pairs_pause_until, logs_pause_until, status_pause_until, last_marquee_update
    global selected_pair_index, show_chart, chart_symbol

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
            log_content.append(log_entry['msg'] + "\n", style=style)

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
            table.add_column("Flags", style="bold white", no_wrap=True)
            table.add_column("Scr", style="bold white", no_wrap=True)
            table.add_column("B.Prof", style="bold green", no_wrap=True)
            table.add_column("Aggress", style="white", no_wrap=True)
            table.add_column("Strategy", style="bold cyan", no_wrap=True)
        else:
            table.add_column("Pair", style="cyan", no_wrap=True)
            table.add_column("Price", style="magenta", no_wrap=True)
            table.add_column("Amt", style="cyan", no_wrap=True)
            table.add_column("Entry", style="magenta", no_wrap=True)
            table.add_column("Fee", style="red", no_wrap=True)
            table.add_column("B.Prof", style="bold green", no_wrap=True)
            table.add_column("Tendency", style="bold white", no_wrap=True)
            table.add_column("Last Order", style="bold", no_wrap=True)
            table.add_column("Signal", style="bold", no_wrap=True)
            table.add_column("Aggress", style="white", no_wrap=True)
            table.add_column("Strategy", style="bold cyan", no_wrap=True)

        sorted_symbols = sorted([s for s in bot_state.keys() if not s.startswith("_")])
        # Calculate exactly available height: Header(3) + Logs(10) + Status(3) + Panel Border(2) = 18
        # Increased to 20 to provide more margin and avoid cutting off rows.
        pairs_height = console.height - 20
        if pairs_height < 3: pairs_height = 3
        max_pairs_offset = max(0, len(sorted_symbols) - pairs_height)

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
        visible_symbols = sorted_symbols[pairs_scroll_offset : pairs_scroll_offset + pairs_height]

        for i, symbol in enumerate(visible_symbols):
            abs_idx = pairs_scroll_offset + i
            is_selected = (abs_idx == selected_pair_index and focused_panel == "pairs")
            row_style = "bold reverse" if is_selected else ""

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
                 amt_str = f"{p['amount']:.8f}"
                 entry_str = format_price(p['entry_price'])
                 fee_str = f"{p.get('entry_fee', 0):.7f}"

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
                      f"{data.get('expected_profit', 0):.5f}" if has_position else '0.00000',
                      data.get('aggr', 'N/A'),
                      data.get('strategy', 'N/A')
                 ]
            else:
                 row_vals = [
                      symbol,
                      format_price(data.get('price', 0)),
                      amt_str, entry_str, fee_str,
                      f"{data.get('expected_profit', 0):.5f}" if has_position else '0.00000',
                      f"[{tend_style}]{tendency}[/]",
                      f"[{last_order_style}]{last_order}[/]",
                      f"[{signal_style}]{current_signal}[/]",
                      data.get('aggr', 'N/A'),
                      data.get('strategy', 'N/A')
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
        status_text.append("TAB: Switch | Arrows: Scroll | H: Help | Exit: Ctrl+C", style="bold red")

        display_width = console.width - 4
        max_status_offset = max(0, len(status_text) - display_width)

        if max_status_offset > 0:
             if should_step and now_ts > status_pause_until:
                  if status_scroll_index < max_status_offset:
                       status_scroll_index += 1
                       if status_scroll_index == max_status_offset:
                            status_pause_until = now_ts + 1
                  else:
                       status_scroll_index = 0
                       status_pause_until = now_ts + 1

             status_scroll_index = max(0, min(status_scroll_index, max_status_offset))
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
        help_text.append("  X      : Toggle Expert Mode (Show/Hide Indicators)\n")
        help_text.append("  M      : Toggle Marquee Effect (Pause/Resume scrolling)\n")
        help_text.append("  H      : Close this help menu\n")
        help_text.append("  Ctrl+C : Stop the bot gracefully\n")

        pairs_panel = Panel(help_text, title="[bold]Help / Info[/]", border_style="bold yellow")

    if show_chart:
        chart_content = render_ascii_chart(chart_symbol, config)
        pairs_panel = Panel(chart_content, title=f"[bold]K-Lines: {chart_symbol}[/]", border_style="bold magenta")

    layout = Layout()
    layout.split(
        Layout(Panel(Text("Binance Trading Bot Dashboard", style="bold magenta", justify="center"), border_style="blue"), size=3),
        Layout(log_panel, size=log_height+2),
        Layout(pairs_panel, name="main"),
        Layout(Panel(status_display, title="Status", border_style="cyan"), size=3)
    )
    return layout

def input_thread_func():
    global pairs_scroll_offset, selected_pair_index, show_chart, chart_symbol, logs_scroll_offset, focused_panel
    global pairs_pause_until, logs_pause_until, expert_mode, show_help, marquee_enabled
    while not shutdown_event.is_set():
        try:
            key = readchar.readkey()
            # Calculate heights for clamping
            with bot_lock:
                 log_height = 8
                 max_logs_offset = max(0, len(all_logs) - log_height)
                 sorted_symbols = sorted([s for s in bot_state.keys() if not s.startswith("_")])
                 pairs_height = console.height - 20
                 if pairs_height < 3: pairs_height = 3
                 max_pairs_offset = max(0, len(sorted_symbols) - pairs_height)

            if show_chart:
                if key in [readchar.key.ENTER, readchar.key.ESC, 'q', 'Q']:
                    show_chart = False
                continue

            if key == readchar.key.TAB:
                focused_panel = "logs" if focused_panel == "pairs" else "pairs"
            elif key == readchar.key.UP:
                if focused_panel == "pairs":
                    selected_pair_index = max(0, selected_pair_index - 1)
                    if selected_pair_index < pairs_scroll_offset:
                        pairs_scroll_offset = selected_pair_index
                    pairs_pause_until = time.time() + 5 # Longer pause on manual interaction
                else:
                    logs_scroll_offset = min(max_logs_offset, logs_scroll_offset + 1)
                    logs_pause_until = time.time() + 5
            elif key == readchar.key.DOWN:
                if focused_panel == "pairs":
                    selected_pair_index = min(len(sorted_symbols) - 1, selected_pair_index + 1)
                    if selected_pair_index >= pairs_scroll_offset + pairs_height:
                        pairs_scroll_offset = selected_pair_index - pairs_height + 1
                    pairs_pause_until = time.time() + 5
                else:
                    logs_scroll_offset = max(0, logs_scroll_offset - 1)
                    logs_pause_until = time.time() + 5
            elif key == readchar.key.ENTER:
                if focused_panel == "pairs" and sorted_symbols:
                    chart_symbol = sorted_symbols[selected_pair_index]
                    show_chart = True
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

def ohlcv_watcher_thread(exchange, symbol, timeframe, config):
    """Background thread to watch OHLCV and update cache."""
    # logging.info(f"Starting OHLCV watcher for {symbol} ({timeframe})")
    try:
        # Pre-fill cache with historical data for indicator stability
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=500)
        if ohlcv:
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df = df.drop_duplicates(subset=['timestamp']).set_index('timestamp', drop=False)
            with ohlcv_cache_lock:
                ohlcv_cache[f"{symbol}_{timeframe}"] = df

        for candle in exchange.watch_ohlcv(symbol, timeframe):
            if shutdown_event.is_set(): break
            # Update cache
            with ohlcv_cache_lock:
                cache_key = f"{symbol}_{timeframe}"
                if cache_key in ohlcv_cache:
                    df = ohlcv_cache[cache_key]
                    # candle is [timestamp, open, high, low, close, volume]
                    new_row = pd.DataFrame([candle], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    new_ts = pd.to_datetime(new_row['timestamp'], unit='ms').iloc[0]
                    new_row['timestamp'] = new_ts
                    new_row.set_index('timestamp', drop=False, inplace=True)

                    # Update or append
                    if not df.empty and new_ts in df.index:
                        df.loc[new_ts] = new_row.iloc[0]
                    else:
                        df = pd.concat([df, new_row]).tail(1000)
                        # Ensure chronological order and remove potential duplicates
                        df = df[~df.index.duplicated(keep='last')].sort_index()
                        ohlcv_cache[cache_key] = df
                else:
                    df = pd.DataFrame([candle], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                    df.set_index('timestamp', drop=False, inplace=True)
                    ohlcv_cache[cache_key] = df
    except Exception as e:
        logging.error(f"Error in OHLCV watcher for {symbol}: {e}")

def trading_thread_func(exchange, data_manager, pattern_manager, engine, config, mode):
    # Start watchers for all pairs
    for symbol in config.get('pairs', {}):
        timeframe = config['pairs'][symbol].get('timeframe', '1m')
        threading.Thread(target=ohlcv_watcher_thread, args=(exchange, symbol, timeframe, config), daemon=True).start()

    priority_order = config.get('_priority_pairs')
    pairs_dict = config.get('pairs', {})
    pair_keys = priority_order if priority_order else list(pairs_dict.keys())

    last_assets_update = 0
    sim_init_done = False
    active_benchmarks = {} # symbol -> future

    time.sleep(5)
    exchange.load_markets()

    # Use a persistent ProcessPoolExecutor for re-benchmarking
    with concurrent.futures.ProcessPoolExecutor() as bench_executor:
        try:
            while not shutdown_event.is_set():
                if mode == 'simulation' and not sim_init_done:
                    initialize_simulation(exchange, data_manager, pattern_manager, engine, config, bot_state)
                    sim_init_done = True

                if mode == 'live' and not sim_init_done:
                    # First time load for live
                    sync_live_positions(exchange, data_manager, config)
                    sim_init_done = True

            try:
                # 1. Check completed re-benchmarks
                completed_symbols = []
                for sym, future in active_benchmarks.items():
                    if future.done():
                        try:
                            sym_result, patterns = future.result()
                            if not patterns:
                                msg_target = status.console if status else console
                                msg_target.print(f"[dim][{sym}] No profitable strategy found after search.")
                            if patterns:
                                best = patterns[0]
                                config["pairs"][sym]["aggr"] = best["aggr"]
                                config["pairs"][sym]["strategy"] = best["strategy"]
                                config["pairs"][sym]["expected_profit"] = best["profit"]
                                pattern_manager.set_patterns(sym, patterns)
                                with bot_lock:
                                    bot_state[sym]["aggr"] = best["aggr"]
                                    bot_state[sym]["strategy"] = best["strategy"]
                                    bot_state[sym]["expected_profit"] = best["profit"]
                                # logging.info(f"[{sym}] Re-benchmarked to {best["strategy"]} ({best["aggr"]})")

                                # Re-evaluate timeframe after re-benchmarking (background)
                                new_tf, _, _ = get_optimal_timeframe(exchange, sym, config)
                                if new_tf != config["pairs"][sym].get("timeframe"):
                                    # logging.info(f"[{sym}] Updating timeframe to {new_tf}")
                                    config["pairs"][sym]["timeframe"] = new_tf

                        except Exception as e:
                            logging.error(f"Error in background re-benchmark for {sym}: {e}")
                        completed_symbols.append(sym)

                for sym in completed_symbols:
                    del active_benchmarks[sym]

            except Exception as e:
                logging.error(f"Error in background benchmark processing: {e}")

            potential_buys = []

            # 2. Parallelize pair analysis
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(pair_keys)) as executor:
                future_to_sym = {executor.submit(analyze_pair, exchange, data_manager, pattern_manager, sym, pairs_dict[sym], config, engine=engine): sym for sym in pair_keys}
                for future in concurrent.futures.as_completed(future_to_sym):
                    if shutdown_event.is_set(): break
                    symbol = future_to_sym[future]
                    if symbol in suspended_pairs: continue
                    try:
                        data = future.result()
                        if data:
                            no_signal_thresh = config.get('no_signal_threshold', 8)

                            with bot_lock:
                                candles_since = bot_state[symbol].get('candles_since_last_signal', 0)

                            if not data.get('buy') and not data.get('sell'):
                                candles_since += 1
                            else:
                                candles_since = 0

                            # 3. Handle re-benchmarking triggers (Asynchronous)
                            if candles_since >= no_signal_thresh and symbol not in active_benchmarks:
                                # logging.info(f"[{symbol}] No signal for {no_signal_thresh} candles. Scheduling background re-benchmark...")
                                candles_since = 0

                                # Use cached OHLCV data instead of blocking network call
                                timeframe = config['pairs'][symbol].get('timeframe', '1m')
                                cache_key = f"{symbol}_{timeframe}"
                                df_bench = None
                                with ohlcv_cache_lock:
                                    if cache_key in ohlcv_cache:
                                        df_bench = ohlcv_cache[cache_key].copy()

                                if df_bench is not None and not df_bench.empty:
                                    global_aggr = config.get('force_agressivity_to_all_pairs')
                                    global_strat = config.get('force_strategy_to_all_pairs')
                                    aggrs = [global_aggr] if global_aggr else ['dynamic']
                                    strategies = [global_strat] if global_strat else STRATEGIES

                                    # Submit re-benchmark task to ProcessPoolExecutor
                                    # Determine current technique to exclude (rotation rule)
                                    exclude = None
                                    with bot_lock:
                                         if symbol in bot_state:
                                              exclude = (bot_state[symbol].get('strategy'), bot_state[symbol].get('aggr'))

                                    active_benchmarks[symbol] = bench_executor.submit(
                                        run_benchmark_for_symbol, symbol, config, timeframe, aggrs, strategies, df_bench, engine, device, exclude_technique=exclude
                                    )

                            with bot_lock:
                                data['last_action'] = bot_state[symbol].get('last_action', 'WAITING')
                                bot_state[symbol].update(data)
                                bot_state[symbol]['candles_since_last_signal'] = candles_since

                            if data.get('sell_triggered'):
                                 pos = data.get('position')
                                 # Use real fee rate for profit check if available
                                 fee_rate = 0.001
                                 try:
                                      fee_rate = exchange.fetch_trading_fee(symbol)
                                 except: pass

                                 if pos and not engine.is_profitable(data['price'], pos['entry_price'], fee_rate=fee_rate):
                                      # Profit check failed, report details, flag to avoid repeating, and trigger immediate re-benchmark
                                      min_exit = engine.get_min_exit_price(pos['entry_price'], fee_rate=fee_rate)
                                      logging.warning(f"[{symbol}] Sell aborted: Potential loss. Price: {data['price']:.7f} < Min Exit: {min_exit:.7f} (Entry: {pos['entry_price']:.7f}, Fee: {fee_rate*100:.2f}%). Flagging pair and re-benchmarking...")

                                      # Flag to avoid repeating this failed operation for the current position
                                      data_manager.flag_ignore_sell(symbol)

                                      # Clear signal to "reverse" it for current cycle
                                      data['sell'] = False
                                      data['sell_triggered'] = False

                                      # Trigger re-benchmark
                                      if symbol not in active_benchmarks:
                                           timeframe = config['pairs'][symbol].get('timeframe', '1m')
                                           cache_key = f"{symbol}_{timeframe}"
                                           df_bench = None
                                           with ohlcv_cache_lock:
                                                if cache_key in ohlcv_cache:
                                                     df_bench = ohlcv_cache[cache_key].copy()
                                           if df_bench is not None and not df_bench.empty:
                                                active_benchmarks[symbol] = bench_executor.submit(
                                                     run_benchmark_for_symbol, symbol, config, timeframe, ['dynamic'], STRATEGIES, df_bench, engine, device,
                                                     exclude_technique=(bot_state[symbol].get('strategy'), bot_state[symbol].get('aggr'))
                                                )
                                 elif execute_sell(exchange, data_manager, engine, symbol, data):
                                      with bot_lock:
                                          bot_state[symbol]['last_action'] = 'SELL'
                                          bot_state[symbol]['position'] = None
                                          data['last_action'] = 'SELL'
                                          data['position'] = None
                                      play_sound("sell", config)

                            if data.get('buy') and not data.get('position'):
                                 potential_buys.append((symbol, data))
                    except Exception as e:
                        logging.error(f"Error analyzing {symbol}: {e}")

            if potential_buys and not shutdown_event.is_set():
                max_open = int(config.get('max_open_positions', 18))
                current_open = len(data_manager.get_open_positions())
                slots_available = max_open - current_open
                if slots_available > 0:
                     # Prioritize by benchmark profit (casted to float for robust sorting)
                     potential_buys.sort(key=lambda x: float(x[1].get('expected_profit', 0)), reverse=True)
                     balance = exchange.fetch_balances()
                     for i in range(min(len(potential_buys), slots_available)):
                          if shutdown_event.is_set(): break
                          symbol, data = potential_buys[i]
                          if execute_buy(exchange, data_manager, engine, symbol, data, config, balance=balance):
                               with bot_lock:
                                   bot_state[symbol]['last_action'] = 'BUY'
                                   bot_state[symbol]['position'] = data_manager.get_position(symbol)
                                   data['last_action'] = 'BUY'
                                   data['position'] = data_manager.get_position(symbol)
                               play_sound("buy", config)
                               # Update balance for next iteration
                               balance = exchange.fetch_balances()

                for _ in range(5):
                     if shutdown_event.is_set(): break
                     time.sleep(0.1)
        except Exception as e:
            logging.error(f"Error in trading thread: {e}")
            time.sleep(5)
        finally:
            bench_executor.shutdown(wait=False, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(description='Binance Trading Bot')
    parser.add_argument('--no-gpu', action='store_true', help='Disable GPU acceleration (force CPU)')
    parser.add_argument('--exchange', choices=['binance', 'kraken', 'bitvavo'], default='binance', help='Exchange to use')
    parser.add_argument('--mode', choices=['live', 'simulation', 'balance', 'backtest', 'benchmark'], default='simulation', help='Bot mode')
    parser.add_argument('--config', help='Path to config file (optional, defaults to config.json or config.default.json)')
    parser.add_argument('--symbol', help='Target symbol for backtest/benchmark (e.g. BTC/EUR)')
    parser.add_argument('--every-symbol', action='store_true', help='Run benchmark for all configured pairs')

    strat_help = f"Strategy for backtest. Available: {', '.join(STRATEGIES)}"
    parser.add_argument('--strategy', help=strat_help)
    parser.add_argument('--aggr', help='Agressivity for backtest')
    parser.add_argument('--backtest-positions', type=int, default=1, help='Max simultaneous positions in backtest (1-4)')
    parser.add_argument('--timeframe', choices=['1m', '3m', '5m', '15m', '30m'], help='Manual timeframe override')
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
        data_manager = DataManager(args.mode) if args.mode in ['live', 'simulation'] else None
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
        elif args.mode == 'balance':
            exchange = MockExchange(api_key, api_secret) if api_key in [None, "YOUR_API_KEY"] else BinanceExchange(api_key, api_secret)
            exchange.load_markets()
            show_balances(exchange)
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
            # Determine optimal timeframe for each pair first
            status.update("[bold blue]Determining optimal timeframes for all pairs...")
            for symbol in config['pairs']:
                if args.timeframe:
                    tf = args.timeframe
                    score = "N/A"
                    reasons = ["Manual Override"]
                else:
                    tf, score, reasons = get_optimal_timeframe(exchange, symbol, config)
                config['pairs'][symbol]['timeframe'] = tf
                console.print(f"[dim][{symbol}] Optimal timeframe: {tf} (Score: {score}, Reasons: {', '.join(reasons)})")

            status.update(f"[bold blue]Optimizing strategies for all pairs...")
            opt_map = run_benchmark_mode(exchange, config, args, status=status, data_manager=data_manager, pattern_manager=pattern_manager, engine=engine, device=device)
            # Store profits for prioritization
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

                        # Score for prioritization (the predicted profit)
                        priority_score = best['profit']
                        config['pairs'][sym]['expected_profit'] = priority_score
                        pair_priorities.append((sym, priority_score))
                        if best.get('is_cached'):
                             console.print(f"[bold green][{sym}][/] Optimized from [cyan]cached results[/] to [cyan]{best['strategy']}[/] ([dim]{best['aggr']}[/]) | Predicted Profit: {format_price(priority_score)} EUR")

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
    threading.Thread(target=trading_thread_func, args=(exchange, data_manager, pattern_manager, engine, config, args.mode), daemon=True).start()

    play_sound("startup")
    try:
        with Live(make_dashboard(args.mode, config), refresh_per_second=10, console=console, auto_refresh=True) as live:
            while not shutdown_event.is_set():
                live.update(make_dashboard(args.mode, config))
                time.sleep(0.1)
    except (KeyboardInterrupt, SystemExit):
        shutdown_event.set()

    logging.info("Bot stopped gracefully.")

def play_sound(action, config=None):
    system = platform.system().lower()
    try:
        if system == "windows":
            import winsound
            if action == "startup":
                 # Randomized sequence equal to max_open_positions
                 num_blips = int(config.get('max_open_positions', 18)) if config else 18
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
    # Retrieve patterns for matching
    patterns = pattern_manager.get_patterns(symbol)

    timeframe = pair_config.get('timeframe', '1m')

    # Try to get from cache first (updated by watch_ohlcv)
    cache_key = f"{symbol}_{timeframe}"
    df = None
    with ohlcv_cache_lock:
        if cache_key in ohlcv_cache:
            df = ohlcv_cache[cache_key].copy()

    if df is None:
        # Fallback to fetch if not yet watched (Done OUTSIDE the lock to prevent freezing)
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=500)
        if not ohlcv: return None
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.drop_duplicates(subset=['timestamp']).set_index('timestamp', drop=False)
        with ohlcv_cache_lock:
            ohlcv_cache[cache_key] = df

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
            sim = calculate_similarity(buffer_window, p, device=device)
            if sim > 0.70 and sim > best_sim: # Lowered threshold to 70% for better responsiveness
                best_sim = sim
                active_pattern = p

    # 2. Dynamic Activation
    if active_pattern:
        strategy_name = active_pattern['strategy']
        mode_name = active_pattern['aggr']
    else:
        # Fallback to benchmarked strategy if no pattern matches
        strategy_name = pair_config.get('strategy', 'double_ema_macd_rsi')
        mode_name = pair_config.get('aggr', 'normal')

    # Use Dynamic Risk Engine if engine is available
    if engine:
        mode_settings = engine.get_dynamic_settings(latest_row_base.get('adx', 0), latest_row_base.get('volatility', 0))
    else:
        # Fallback to balanced defaults if no engine
        mode_settings = {
            "ema_fast": 20, "ema_slow": 50, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
            "rsi_period": 14, "rsi_buy": 30, "rsi_sell": 70, "confirmation_window": 1
        }

    mode_settings['strategy'] = strategy_name
    mode_settings['device'] = device
    df = get_signals(df, mode_settings, is_backtest=False)
    latest_row = df.iloc[-1]

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
        for s in reversed(buy_hist[-10:]):
            if s: c_buys += 1
            else: break

        c_sells = 0
        for s in reversed(sell_hist[-10:]):
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

    # Dynamic confirmation window based on volatility
    # Default is 1 signal for both buy and sell.
    buy_threshold = 1
    sell_threshold = 1

    # High volatility adds an additional confirmation signal
    volatility = latest_row.get('volatility', 0)
    if volatility > 0.1: # High volatility
        buy_threshold += 1
        sell_threshold += 1

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
        'sell': consecutive_sells >= sell_threshold,
        'consecutive_buys': consecutive_buys,
        'consecutive_sells': consecutive_sells,
        '_last_candle_ts': candle_ts,
        'sell_triggered': consecutive_sells >= sell_threshold and data_manager.get_position(symbol) and not data_manager.get_position(symbol).get('ignore_sell'),
        'position': data_manager.get_position(symbol),
        'expected_profit': float(pair_config.get('expected_profit', 0)),
        'trigger_data': trigger_data
    }

def execute_buy(exchange, data_manager, engine, symbol, data, global_config, balance=None):
    if balance is None:
        balance = exchange.fetch_balances()
    win_streak = data_manager.get_win_streak(symbol)

    # Use the freshest price from our watched OHLCV cache
    timeframe = global_config['pairs'].get(symbol, {}).get('timeframe', '1m')
    cache_key = f"{symbol}_{timeframe}"

    current_price = data['price']
    with ohlcv_cache_lock:
        if cache_key in ohlcv_cache and not ohlcv_cache[cache_key].empty:
            current_price = ohlcv_cache[cache_key].iloc[-1]['close']

    base_curr = symbol.split('/')[1]
    amount = engine.calculate_position_size(
        balance, current_price, base_curr, win_streak=win_streak
    )
    base_currency = symbol.split('/')[1]
    if amount > 0:
        # Check if balance is sufficient before attempting order
        cost = amount * current_price
        base_asset = base_currency
        free_balance = balance.get(base_asset, {}).get('free', 0) if isinstance(balance, dict) and 'free' in balance else balance.get(base_asset, 0)

        if free_balance < cost:
            logging.warning(f"[{symbol}] Buy aborted: Insufficient {base_asset} balance ({format_price(free_balance)} < {format_price(cost)})")
            return False

        try:
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
                fee = order.get('calculated_fee', 0)
                total_paid = (amount * current_price) + fee
                logging.info(f"[{symbol}] Executing buy of amount {amount:.8f} at {current_price:.7f}, final price paid: {total_paid:.7f} {symbol.split('/')[1] if '/' in symbol else 'EUR'}")
                data_manager.add_position(symbol, current_price, amount, fee, data.get('trigger_data', {}), time.time(), total_base=total_paid)
                return True
            else:
                logging.warning(f"[{symbol}] Buy execution failed: Exchange rejected order for amount {amount:.6f}. Suspending pair.")
                suspended_pairs.add(symbol)
        except Exception as e:
            logging.error(f"[{symbol}] Buy failed with exception: {e}. Suspending pair.")
            suspended_pairs.add(symbol)
            return False
    else:
        logging.warning(f"[{symbol}] Buy aborted: Calculated amount is zero or negative.")
    return False

def execute_sell(exchange, data_manager, engine, symbol, data):
    position = data['position']
    should_execute = True

    if should_execute:
        base_asset = symbol.split('/')[0]

        # Bypass balance check for simulation mode
        # In simulation, we trust the internal DataManager state
        is_simulation = isinstance(exchange, MockExchange)

        balance = exchange.fetch_balances()
        free_balance = balance.get(base_asset, {}).get('free', 0) if 'free' in balance else balance.get(base_asset, 0)
        base_currency = symbol.split('/')[1]

        if is_simulation or free_balance >= position['amount']:
            order = exchange.create_order(symbol, 'sell', position['amount'])
            if isinstance(order, dict) and order.get('error') == 'dust_limit':
                logging.warning(f"[{symbol}] Sell aborted: Balance is dust/below precision. Flagging pair to avoid repeat sell attempts.")
                data_manager.flag_ignore_sell(symbol)
                return False
            if order:
                fee = order.get('calculated_fee', 0)
                amount = position['amount']
                total_received = (amount * data['price']) - fee
                logging.info(f"[{symbol}] Executing sell of amount {amount:.8f} at {data['price']:.7f}, final price received: {total_received:.7f} {symbol.split('/')[1] if '/' in symbol else 'EUR'}")
                profit = total_received - position.get('entry_total_base', 0)
                data_manager.close_position(symbol, data['price'], fee, profit, data.get('trigger_data', {}), time.time(), total_base=total_received)
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
        max_open = int(config.get('max_open_positions', 18))
        current_open = len(data_manager.get_open_positions())
        slots_available = max_open - current_open
        if slots_available > 0:
            # Prioritize by benchmark profit
            potential_buys.sort(key=lambda x: float(x[1].get('expected_profit', 0)), reverse=True)
            balance = exchange.fetch_balances()
            for i in range(min(len(potential_buys), slots_available)):
                symbol, data = potential_buys[i]
                if execute_buy(exchange, data_manager, engine, symbol, data, config, balance=balance):
                    with bot_lock:
                        bot_state[symbol]['position'] = data_manager.get_position(symbol)
                        bot_state[symbol]['price'] = data['price']
                        bot_state[symbol]['last_action'] = 'BUY'
                    # Refresh balance for next buy
                    balance = exchange.fetch_balances()

    logging.info(f"Initialization of the simulation positions completed.")

def sync_live_positions(exchange, data_manager, config):
    logging.info("Syncing positions from Binance API")
    balance = exchange.fetch_balances()
    # Robustly handle different balance structures
    if isinstance(balance, dict) and 'free' in balance and isinstance(balance['free'], dict):
        free_balances = balance['free']
    else:
        free_balances = balance
    base_currencies = config.get('base_currencies', ['EUR'])

    # We clear local cache for Live mode as requested
    data_manager.data['open_positions'] = {}
    sellable_found = False

    assets_to_sync = []
    asset_to_symbol = {}

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

        assets_to_sync.append(symbol)
        asset_to_symbol[symbol] = (asset, amount)

    if assets_to_sync:
        tickers = exchange.fetch_tickers(assets_to_sync)
        markets = exchange.markets if hasattr(exchange, 'markets') and exchange.markets else exchange.load_markets()

        for symbol in assets_to_sync:
            asset, amount = asset_to_symbol[symbol]
            ticker = tickers.get(symbol)
            if not ticker: continue

            curr_price = ticker['last']
            is_dust = False

            try:
                if symbol in markets:
                    m = markets[symbol]
                    min_amt = m['limits']['amount']['min']
                    min_cost = m['limits']['cost']['min'] or 10
                    if amount < min_amt or (amount * curr_price) < min_cost:
                        is_dust = True
                elif amount <= 0.000001: is_dust = True
            except: pass

            if is_dust: continue
            sellable_found = True

            if curr_price > 0:
                 data_manager.add_position(symbol, curr_price, amount, 0, {"info": "auto_populated"}, time.time(), total_base=amount*curr_price)
            else:
                 logging.warning(f"[{symbol}] Asset found in wallet but price unavailable. Please manage manually.")

    if not sellable_found and any(v > 0 for k, v in free_balances.items() if k not in base_currencies):
        logging.warning("No sellable assets found. Your wallet contains only 'dust' (amounts below exchange limits) or maybe adjust you pairs.txt file.")



def show_balances(exchange):
    console.print("\n[bold magenta]=== Real Wallet Balance (All Assets) ===[/]")
    balance = exchange.fetch_balances()

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
        if asset in ['EUR', 'USDT', 'USDC']:
            eur_val = total # Simplified valuation for base currencies
        else:
            # Try finding any valid pair with this asset as base
            symbol = None
            for bc in ['EUR', 'USDT', 'USDC']:
                candidate = f"{asset}/{bc}"
                # We don't have config here but we can try common ones
                # Try to use OHLCV instead of fetch_ticker to limit methods
                ohlcv = exchange.fetch_ohlcv(candidate, '1m', limit=1)
                if ohlcv and len(ohlcv) > 0:
                     eur_val = total * ohlcv[0][4]
                     break

            if eur_val == 0:
                # Try USDT bridge if EUR pair not found
                ohlcv_usdt = exchange.fetch_ohlcv(f"{asset}/USDT", '1m', limit=1)
                ohlcv_eur_usdt = exchange.fetch_ohlcv("EUR/USDT", '1m', limit=1)
                if ohlcv_usdt and ohlcv_eur_usdt:
                    eur_val = (total * ohlcv_usdt[0][4]) / ohlcv_eur_usdt[0][4]

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

def run_backtest_logic(exchange, symbol, strategy, aggr_name, config, timeframe='1m', df_in=None, limit=500, engine=None, device=None, skip_mc=False, return_full_df=False, eval_candles=None):
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
             "ema_fast": 20, "ema_slow": 50, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
             "rsi_period": 14, "rsi_buy": 30, "rsi_sell": 70, "confirmation_window": 1
         }

    mc = MonteCarloEngine(num_simulations=100, timeframe_candles=20)
    mc.set_device(device if device is not None else torch.device("cpu"))

    # Copy settings and inject strategy and timeframe
    test_config = aggr_settings.copy()
    test_config['strategy'] = strategy

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
        df = df.drop_duplicates(subset=['timestamp']).set_index('timestamp', drop=False)
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
    if eval_candles:
        eval_window_base = eval_candles
    else:
        # Default based on timeframe
        eval_window_base = 60

    max_rand = max(1, int(eval_window_base * 0.1))
    eval_window = eval_window_base + random.randint(-max_rand, max_rand)
    # We always trade on the LAST eval_window candles of df
    start_idx = max(0, len(df) - eval_window)

    if len(df) < eval_window:
        if exchange is not None:
             console.print(f"[yellow]Warning: Only {len(df)} candles available for {symbol}, but requested {eval_window}.[/]")

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
        raw_val = float(config.get('base_trade_amount', 9.0))
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

    # Determine evaluation range
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
    default_strategy = "double_ema_macd_rsi"

    strategy = args.strategy or default_strategy
    aggr = args.aggr or config.get('force_agressivity_to_all_pairs', 'normal')
    timeframe = args.timeframe or config['pairs'].get(args.symbol, {}).get('timeframe', '1m')

    if strategy not in STRATEGIES:
        console.print(f"[bold red]Error: Strategy '{strategy}' not found.[/]")
        console.print(f"Available strategies: {', '.join(STRATEGIES)}")
        console.print("[dim]Please check for typos.[/]")
        return

    console.print(f"[bold blue]Running Backtest for {args.symbol} | Strategy: {strategy} | Aggr: {aggr} | Timeframe: {timeframe}...[/]")
    results = run_backtest_logic(exchange, args.symbol, strategy, aggr, config, timeframe=timeframe, engine=engine, device=device)

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

def run_benchmark_for_symbol(symbol, config, timeframe, aggrs, strategies, df_in, engine=None, device=None, exclude_technique=None, current_lookback=120):
    """
    Scans historical data for the top 4 success patterns using expanding time slices (tenths).
    Enforces a mandatory change in technique and recursively doubles lookback if no strategy is found.
    """
    if df_in is None or len(df_in) < current_lookback: return symbol, []

    # Analyze only the requested lookback window
    df_slice = df_in.tail(current_lookback)
    patterns = []
    now_ts = time.time()
    from indicators import get_signals

    # Filter out current technique if rotating
    if exclude_technique:
        ex_strat, ex_aggr = exclude_technique
    else:
        ex_strat, ex_aggr = None, None

    for aggr in aggrs:
        for strategy in strategies:
            if strategy == ex_strat and aggr == ex_aggr:
                continue

            # Prepare settings
            if engine:
                mode_settings = engine.get_dynamic_settings(25.0, 0.001)
            else:
                mode_settings = {
                    "ema_fast": 20, "ema_slow": 50, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
                    "rsi_period": 14, "rsi_buy": 30, "rsi_sell": 70, "confirmation_window": 1
                }
            mode_settings['strategy'] = strategy
            mode_settings['device'] = device if device is not None else torch.device("cpu")

            # 1. Calculate signals once for the current lookback
            try:
                full_df = get_signals(df_slice.copy(), mode_settings, is_backtest=True)
            except Exception:
                continue

            # 2. Run backtest for the entire lookback block
            res_full = run_backtest_logic(None, symbol, strategy, aggr, config,
                                         timeframe=timeframe, df_in=full_df, engine=engine,
                                         device=device, skip_mc=True, return_full_df=True, eval_candles=len(full_df))

            if not res_full or not res_full.get('equity_curve'):
                continue

            equity = res_full['equity_curve']

            # 3. Expanding Time Slices (Tenths)
            segment_profits = []
            segment_scores = []

            tenth = current_lookback // 10
            for i in range(1, 11):
                segment_len = i * tenth
                start_idx = len(full_df) - segment_len
                end_idx = len(full_df)

                win_profit = equity[end_idx-1] - equity[start_idx]

                # Recency Pondering for this segment
                window_ts = full_df['timestamp'].iloc[start_idx].timestamp()
                age_hours = (now_ts - window_ts) / 3600
                recency_score = 1.0
                if timeframe in ['1m', '3m', '5m']:
                     if age_hours > 24: recency_score = 0.8
                     if age_hours > 168: recency_score = 0.5
                elif timeframe in ['15m', '30m']:
                     if age_hours > 168: recency_score = 0.8

                segment_profits.append(win_profit)
                segment_scores.append(win_profit * recency_score)

            # Combined score: mean of all segments (penalizes inconsistent performance)
            avg_score = sum(segment_scores) / len(segment_scores)
            avg_profit = sum(segment_profits) / len(segment_profits)

            if avg_profit < 0.01:
                continue

            latest_row = full_df.iloc[-1]
            tech_state = {
                'rsi': float(latest_row.get('rsi', 50)),
                'adx': float(latest_row.get('adx', 0)),
                'ema_f': float(latest_row.get('ema_f', 0)),
                'ema_s': float(latest_row.get('ema_s', 0))
            }

            patterns.append({
                'profit': avg_profit,
                'score': avg_score,
                'strategy': strategy,
                'aggr': aggr,
                'symbol': symbol,
                'start_time': full_df['timestamp'].iloc[0].strftime("%Y-%m-%d %H:%M"),
                'end_time': full_df['timestamp'].iloc[-1].strftime("%Y-%m-%d %H:%M"),
                'start_ts': full_df['timestamp'].iloc[0].timestamp(),
                'prices': full_df['close'].tolist(),
                'tech_state': tech_state
            })

    # If no profitable strategy found, recursively double lookback if data available
    if not patterns and current_lookback < 480 and len(df_in) >= current_lookback * 2:
        return run_benchmark_for_symbol(
            symbol, config, timeframe, aggrs, strategies, df_in,
            engine, device, exclude_technique, current_lookback * 2
        )
    # Rule: "Last for" - we no longer look for the best, but the very last profitable one
    # Note: patterns are added sequentially by strategy, we sort by start_ts (recency)
    patterns.sort(key=lambda x: x['start_ts'], reverse=True)
    unique_patterns = []
    seen_times = []

    for p in patterns:
        if len(unique_patterns) >= 4: break

        # Since all patterns now use the same 120-candle window, overlap check is redundant
        # but we still want to apply Monte Carlo validation to the chosen ones.

        # Find the window in df_in
        p_start_ts_dt = pd.to_datetime(p['start_ts'], unit='s')
        p_start_idx = df_in['timestamp'].searchsorted(p_start_ts_dt)

        if p_start_idx != -1:
            # Validate the full block (standard 120 or doubled 240)
            window_df = df_in.iloc[max(0, p_start_idx-250):]
            mc = MonteCarloEngine(num_simulations=100, timeframe_candles=20)
            mc.set_device(device if device is not None else torch.device("cpu"))
            mc_score = mc.validate_strategy(window_df)
            p['profit'] *= mc_score
            p['score'] *= mc_score

        unique_patterns.append(p)

    return symbol, unique_patterns

def run_benchmark_mode(exchange, config, args, status=None, data_manager=None, pattern_manager=None, engine=None, device=None):

    # Respect global overrides if they exist
    global_aggr = config.get('force_agressivity_to_all_pairs')
    global_strat = config.get('force_strategy_to_all_pairs')

    # Agressivity is now dynamic
    aggrs = [global_aggr] if global_aggr else ['dynamic']
    strategies = [global_strat] if global_strat else STRATEGIES

    cache_mgr = CacheManager()

    # Cache validity mapping (default 1hr)
    validity_duration = 3600

    symbols = [args.symbol] if (hasattr(args, 'symbol') and args.symbol) else list(config.get('pairs', {}).keys())

    # Best per symbol
    best_per_symbol = {}

    # Best performers across all symbols
    best_overall = {
        'total': {'profit': -999999, 'params': None}
    }

    optimization_map = {}

    symbols_to_bench = []
    for symbol in symbols:
        timeframe = config['pairs'].get(symbol, {}).get('timeframe', '1m')
        cached_patterns = cache_mgr.get(symbol, timeframe, validity_duration)
        if cached_patterns:
            # cached_patterns is a list of pattern dicts
            best = cached_patterns[0]
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
        symbol_data_map = {}

        # Date filtering logic
        # Durations (up to 240 candles for doubling): 1m(4h), 3m(12h), 5m(20h), 15m(60h), 30m(120h)
        now_ts = time.time()
        since_map = {
            '1m': int((now_ts - 4 * 3600) * 1000),
            '3m': int((now_ts - 12 * 3600) * 1000),
            '5m': int((now_ts - 20 * 3600) * 1000),
            '15m': int((now_ts - 60 * 3600) * 1000),
            '30m': int((now_ts - 120 * 3600) * 1000)
        }
        if args.since:
             try: since_ts = int(datetime.strptime(args.since, "%Y-%m-%d %H:%M").timestamp() * 1000)
             except Exception: console.print(f"[red]Invalid --since format. Use YYYY-MM-DD HH:MM[/]")

        for i, symbol in enumerate(symbols_to_bench):
            all_ohlcv = []
            target_limit = 2000 # Increased to ensure enough data for double lookback
            timeframe = config['pairs'].get(symbol, {}).get('timeframe', '1m')

            current_since = since_map.get(timeframe, since_map['1m'])

            if status: status.update(f"[bold cyan][{i+1}/{len(symbols_to_bench)}] Fetching up to {target_limit} candles for {symbol} ({timeframe})...")

            try:
                # Check cache first
                cache_key = f"{symbol}_{timeframe}_{target_limit}_12-24h"
                with ohlcv_cache_lock:
                    if cache_key in ohlcv_cache:
                        df = ohlcv_cache[cache_key]
                        symbol_data_map[symbol] = df
                        if not status: console.print(f"[dim][{symbol}] Loaded {len(df)} candles from cache.")
                        continue

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
                    df = df.drop_duplicates(subset=['timestamp']).set_index('timestamp', drop=False)

                    # Filter by --until if provided
                    if args.until:
                         try:
                              until_dt = datetime.strptime(args.until, "%Y-%m-%d %H:%M")
                              df = df[df['timestamp'] <= until_dt]
                         except Exception: pass

                    # Enforce timeframe-specific limits (Using UTC for consistency with exchange data)
                    # We fetch enough for 240 candles but the default benchmark only uses 120
                    duration_hours = {
                        '1m': 4, '3m': 12, '5m': 20, '15m': 60, '30m': 120
                    }.get(timeframe, 4)
                    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=duration_hours)
                    df = df[df['timestamp'] >= cutoff]

                    symbol_data_map[symbol] = df
                    with ohlcv_cache_lock:
                        ohlcv_cache[cache_key] = df
                    if not status: console.print(f"[dim][{symbol}] Successfully fetched {len(df)} candles.[/]")
                else:
                    if not status: console.print(f"[yellow]No OHLCV returned for {symbol} ({timeframe}) during pre-fetch.[/]")
            except Exception as e:
                if not status: console.print(f"[red]Failed to fetch {symbol} for benchmark: {e}[/]")

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
                futures = []
                for sym in symbol_data_map:
                    # Determine if we should exclude current technique (for re-benchmarking rotation)
                    exclude = None
                    if sym in bot_state:
                         exclude = (bot_state[sym].get('strategy'), bot_state[sym].get('aggr'))

                    futures.append(executor.submit(
                        run_benchmark_for_symbol, sym, config, config['pairs'][sym].get('timeframe', '1m'),
                        aggrs, strategies, symbol_data_map[sym], engine, device, exclude_technique=exclude
                    ))
                for future in concurrent.futures.as_completed(futures):
                    if shutdown_event.is_set(): break
                    sym, patterns = future.result()

                    if patterns:
                        # "Last for": pick the most recent profitable pattern
                        last_for_symbol = patterns[0]
                        best_per_symbol[sym] = last_for_symbol

                        # Store patterns in DataManager for real-time matching
                        if data_manager:
                             pattern_manager.set_patterns(sym, patterns)

                        # Update current technique in bot_state immediately
                        if sym in bot_state:
                             with bot_lock:
                                  bot_state[sym]['strategy'] = last_for_symbol['strategy']
                                  bot_state[sym]['aggr'] = last_for_symbol['aggr']

                        period_str = f" [dim](From {last_for_symbol.get('start_time')} to {last_for_symbol.get('end_time')})[/]"
                        # Always save patterns to cache
                        timeframe = config['pairs'][sym].get('timeframe', '1m')
                        cache_mgr.set(sym, timeframe, patterns)

                        msg_target = status.console if status else console
                        optimization_map[sym] = last_for_symbol
                        msg_target.print(f"\n[bold green]🏆 LAST FOR {sym}:[/] [bold]{last_for_symbol['strategy']} ({last_for_symbol['aggr']})[/] | Profit: {format_price(last_for_symbol['profit'])} EUR{period_str}")

                        # Use a generic 'total' score for recommendations
                        if last_for_symbol['profit'] > best_overall['total']['profit']:
                             best_overall['total'] = {'profit': last_for_symbol['profit'], 'params': (last_for_symbol['strategy'], last_for_symbol['aggr'], sym)}
            finally:
                signal.signal(signal.SIGINT, original_handler)

    # If we are in optimization mode for live/sim, return the map
    if status: status.update('[bold green]Optimization complete.')
    if best_per_symbol:
        time.sleep(3)

    if args.mode in ['live', 'simulation']:
        return optimization_map

    console.print("\n[bold magenta]=== BENCHMARK RECOMMENDATIONS ===[/]")
    found_any = False
    for key in ['total']:
        label = "Recommended"
        data = best_overall.get(key)
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

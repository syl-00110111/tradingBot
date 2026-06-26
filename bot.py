# CCXT Pro Trading Bot
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

"""
Main entry point and dashboard logic for the CCXT Pro Trading Bot.

This module coordinates the dashboard UI, user input, background OHLCV fetching,
and the core trading loop across multiple pairs and strategies.
"""

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
import re
import math
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

from exchange_handler import CCXTExchange, MockExchange
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
startup_complete = False
marquee_enabled = False
shutdown_event = threading.Event()

# Marquee Timing Control
last_marquee_update = 0
pairs_pause_until = 0
logs_pause_until = 0
status_pause_until = 0

# State shared between threads
bot_state = {}
bot_lock = threading.Lock()
# symbol -> {until: float, reason: str, amount_required: float}
pair_suspensions = {}

def precision_to_int(p):
    """Converts various precision formats (int or float step) to decimal places."""
    if p is None: return 8
    if isinstance(p, int): return p
    if isinstance(p, float):
        if p > 0:
            import math
            return max(0, int(-math.log10(p)))
    return 8

def format_price(price, precision=None):
    """
    Formats a numeric price into a string with adaptive precision.

    Parameters
    ----------
    price : float or int or None
        The price value to format.
    precision : int or float, optional
        The number of decimal places to use or the step size (e.g., 0.001).
        If None, it uses up to 10.

    Returns
    -------
    str
        The formatted price string, or "-" if price is None.
    """
    if price is None: return "-"
    if not isinstance(price, (int, float)): return str(price)
    if price == 0: return "0"

    if precision is None:
        # For very small prices, use scientific notation if it's smaller than 0.000001
        if abs(price) < 0.000001:
            formatted = f"{price:.10f}".rstrip('0').rstrip('.')
            if len(formatted.split('.')[-1]) > 8: # If still very small and long
                 return f"{price:.4e}"
            return formatted

        # Default: up to 10 decimal places, then strip zeros
        return f"{price:.10f}".rstrip('0').rstrip('.')
    else:
        p_int = precision_to_int(precision)
        formatted = f"{price:.{p_int}f}".rstrip('0').rstrip('.')
        if (formatted == "" or formatted == "0") and price != 0:
             # Fallback if precision is too low for the value
             if abs(price) < 0.000001: return f"{price:.4e}"
             return f"{price:.10f}".rstrip('0').rstrip('.')
        return formatted if formatted != "" else "0"

def format_amt(amt, precision=None):
    """
    Formats a numeric amount into a string with adaptive precision.

    Parameters
    ----------
    amt : float or int or None
        The amount value to format.
    precision : int or float, optional
        The number of decimal places to use or the step size (e.g., 0.01).

    Returns
    -------
    str
        The formatted amount string, or "-" if amt is None.
    """
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

class DashboardHandler(logging.Handler):
    """
    Custom logging handler for the bot dashboard.

    Manages log entry expiration and deduplication of specific trading-related messages.

    Parameters
    ----------
    duration : int, optional
        Duration in seconds for which a log entry is considered "fresh" (highlighted).
    """
    def __init__(self, duration=5):
        super().__init__()
        self.duration = duration

    def emit(self, record):
        msg = self.format(record)
        # Avoid displaying HTML error pages for HTTP 500
        if "HTTP 500" in msg and "<html" in msg.upper():
            msg = msg.split("<html")[0].strip()

        timestamp = datetime.now().strftime("%H:%M:%S")
        expiry = datetime.now() + timedelta(seconds=self.duration)

        with bot_lock:
            # Group HTTP Errors (Point 1)
            http_err_match = re.search(r'(HTTP \d{3}) Error Code', msg)
            if http_err_match:
                error_type = http_err_match.group(1)
                for log in all_logs:
                    if error_type in log['msg'] and "Error fetching OHLCV" in log['msg']:
                        try:
                            parts = log['msg'].split(']')
                            times_str = parts[0][1:]
                            rest = parts[1]
                            if timestamp not in times_str: times_str += f",{timestamp}"
                            current_symbols = re.findall(r'([A-Z0-9]+/[A-Z0-9]+)', rest)
                            new_symbol = re.search(r'([A-Z0-9]+/[A-Z0-9]+)', msg)
                            if new_symbol:
                                sym = new_symbol.group(1)
                                if sym not in current_symbols: current_symbols.append(sym)
                            exchange_name = "exchange"
                            ex_match = re.search(r'on (\w+)', rest)
                            if ex_match: exchange_name = ex_match.group(1)
                            symbols_str = ",".join(current_symbols)
                            log['msg'] = f"[{times_str}] Error fetching OHLCV for {symbols_str} on {exchange_name} {error_type}"
                            log['expiry'] = expiry
                            return
                        except: pass

            # Transform raw CCXT errors into the requested grouped format (Point 2)
            # Raw example: "Error during buy order on MEGA/USDC via binance {"code":-1013,"msg":"Filter failure: NOTIONAL"}"
            if "Error during buy order" in msg:
                 err_match = re.search(r'Error during buy order on ([A-Z0-9/]+) via (\w+) (\{.*\})', msg)
                 if err_match:
                      symbol, exchange, err_json = err_match.groups()
                      try:
                           err_data = json.loads(err_json)
                           code = err_data.get('code', 'N/A')
                           m_msg = err_data.get('msg', 'N/A')
                           # Use temporary format that will be caught by the grouper below
                           msg = f"[{symbol}] Buy execution failed: Exchange rejected order ({code}, {m_msg}). Suspending pair."
                      except: pass

            # Group Buy execution failed errors by error code
            if "Buy execution failed" in msg:
                 code_match = re.search(r'\((\-?\d+)', msg)
                 if code_match:
                      code = code_match.group(1)
                      for log in all_logs:
                           if "Buy execution failed" in log['msg'] and f"({code}" in log['msg']:
                                try:
                                     parts = log['msg'].split(']')
                                     times_str = parts[0][1:]
                                     rest = parts[1]
                                     if timestamp not in times_str: times_str += f",{timestamp}"
                                     current_symbols = re.findall(r'([A-Z0-9]+/[A-Z0-9]+)', log['msg'])
                                     new_symbol = re.search(r'\[([A-Z0-9/]+)\]', msg)
                                     if new_symbol:
                                          sym = new_symbol.group(1)
                                          if sym not in current_symbols: current_symbols.append(sym)

                                     symbols_str = ",".join(current_symbols)
                                     # Extract original error details
                                     err_details = re.search(r'\(.*\)', log['msg']).group(0)
                                     log['msg'] = f"[{times_str}] [{symbols_str}] Buy execution failed: Exchange rejected order {err_details}. Suspending pair."
                                     log['expiry'] = expiry
                                     return
                                except: pass

            # Connection pool log filtering (generic)
            pool_msg = "Connection pool is full, discarding connection"
            if pool_msg in msg:
                 for log in all_logs:
                      if pool_msg in log['msg']:
                           log['msg'] = f"[{timestamp}] {msg}"
                           log['expiry'] = expiry
                           return

            # Status update merging (Point 2)
            # 1. Syncing positions
            if "Syncing positions from" in msg and "done." in msg:
                 base_msg = msg.replace(" done.", "")
                 for log in all_logs:
                      if base_msg in log['msg'] and "done." not in log['msg']:
                           log['msg'] = f"[{timestamp}] {msg}"
                           log['expiry'] = expiry
                           return

            # 2. First data streams
            if "First data streams acquired and analysis done." in msg:
                 for log in all_logs:
                      if "Waiting for first data streams and analysis..." in log['msg']:
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
    """
    Loads configuration from a specific JSON file path.

    Parameters
    ----------
    path : str
        The file path to the JSON configuration.

    Returns
    -------
    dict
        The parsed configuration dictionary.

    Raises
    ------
    SystemExit
        If the file is not found or parsing fails.
    """
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
    """
    Locates and loads the bot configuration file.

    Checks for 'config.json' and 'config.default.json' in the current directory.

    Returns
    -------
    dict
        The parsed configuration dictionary.

    Raises
    ------
    SystemExit
        If no configuration file is found.
    """
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

def get_optimal_timeframe(exchange, symbol, config, current_score=None):
    """
    Dynamically determines the optimal timeframe for a pair.

    Decision is based on 48h volume, spread, volatility, and trades per minute.
    Supports '1s' for highly active pairs and absolute scoring.

    Parameters
    ----------
    exchange : ExchangeInterface
        The exchange instance.
    symbol : str
        The trading pair symbol.
    config : dict
        Bot configuration.
    current_score : int, optional
        Deprecated: scoring is now absolute to avoid drift.

    Returns
    -------
    tf : str
        The suggested timeframe ('1s', '1m', '3m', '5m', '15m', '30m').
    score : int
        The calculated absolute score.
    reasons : list of str
        Contributing reasons.
    """
    thresholds = config.get('timeframe_thresholds', {})

    try:
        ticker = exchange.fetch_ticker(symbol)
        if not ticker: return '1m', 0, ["Ticker unavailable"]

        ohlcv_1h = exchange.fetch_ohlcv(symbol, '1h', limit=24)
        trades = exchange.fetch_trades(symbol, limit=200) # Reduced limit to lower API pressure

        # 1. Volume 48h (approx)
        volume_48h = ticker.get('quoteVolume', 0) or ticker.get('baseVolume', 0) * ticker.get('last', 1)
        vol_low = thresholds.get('volume_48h', {}).get('low', 1000)
        vol_high = thresholds.get('volume_48h', {}).get('high', 120000)

        # 2. Spread
        spread_pct = 0.5
        if ticker.get('ask') and ticker.get('bid') and ticker['bid'] > 0:
            spread = ticker['ask'] - ticker['bid']
            spread_pct = (spread / ticker['bid']) * 100
        spr_low = thresholds.get('spread_pct', {}).get('low', 0.001)
        spr_high = thresholds.get('spread_pct', {}).get('high', 0.04)

        # 3. Volatility (Short-term)
        volatility = 0.05
        if ohlcv_1h and len(ohlcv_1h) > 0:
            closes = [candle[4] for candle in ohlcv_1h]
            volatility = (max(closes) - min(closes)) / min(closes)
        vlt_low = thresholds.get('volatility_pct', {}).get('low', 0.01)
        vlt_high = thresholds.get('volatility_pct', {}).get('high', 0.1)

        # 4. Trades activity
        if trades:
            times = [t['timestamp'] for t in trades]
            duration_mins = (max(times) - min(times)) / 60000
            trades_per_min = len(trades) / duration_mins if duration_mins > 0 else 0
        else:
            trades_per_min = 0
        tpm_low = thresholds.get('trades_per_minute', {}).get('low', 1)
        tpm_high = thresholds.get('trades_per_minute', {}).get('high', 100)

        # Base Score (Absolute)
        score = 0
        reasons = []

        # Absolute increments
        if volume_48h > vol_high * 10: score += 2; reasons.append("Ultra High Vol")
        elif volume_48h > vol_high: score += 1; reasons.append("High Vol")

        if spread_pct < spr_low: score += 1; reasons.append("Tight Spread")

        if volatility > vlt_high: score += 1; reasons.append("High Volatility")

        if trades_per_min > tpm_high: score += 2; reasons.append("Very Active")
        elif trades_per_min > tpm_high / 2: score += 1; reasons.append("Active")

        # Absolute decrements
        if volume_48h < vol_low: score -= 1; reasons.append("Low Vol")
        if spread_pct > spr_high: score -= 1; reasons.append("Wide Spread")
        if volatility < vlt_low: score -= 1; reasons.append("Stable")
        if trades_per_min < tpm_low: score -= 1; reasons.append("Inactive")

        # Score Capping
        score = max(-2, min(4, score))

        # Mapping score to timeframe
        if score >= 3: tf = '1s'
        elif score == 2: tf = '1m'
        elif score == 1: tf = '3m'
        elif score == 0: tf = '5m'
        elif score == -1: tf = '15m'
        else: tf = '30m'

        return tf, score, reasons

    except Exception as e:
        logging.warning(f"Error determining timeframe for {symbol}: {e}. Defaulting to 1m.")
        return '1m', 0, [str(e)]

def render_ascii_chart(symbol, config):
    """
    Renders an ASCII candlestick chart for a specific symbol using plotext.

    Parameters
    ----------
    symbol : str
        The trading pair symbol to chart.
    config : dict
        The bot configuration to retrieve timeframe information.

    Returns
    -------
    rich.text.Text
        A Rich Text object containing the rendered ASCII chart.
    """
    global chart_cache
    # Use current adaptive timeframe for chart
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

    plt_ascii.clear_figure()
    plt_ascii.clf()
    plt_ascii.theme('dark')

    # Use subplots to show volume under k-lines (Point 4)
    plt_ascii.subplots(2, 1)

    # Subplot 1: Candlestick
    plt_ascii.subplot(1, 1)
    plt_ascii.clf()
    plt_ascii.theme('dark')
    plt_ascii.title(f"K-Lines et Volume: {symbol} ({timeframe})")
    indices = list(range(len(df)))
    df_plot = df[['open', 'high', 'low', 'close']].copy()
    df_plot.columns = ['Open', 'High', 'Low', 'Close']
    df_plot.reset_index(drop=True, inplace=True)
    plt_ascii.candlestick(indices, df_plot)

    # Set xticks manually for labels with hours (Point 4)
    if len(df) > 5:
         step = len(df) // 5
         tick_indices = list(range(0, len(df), step))
         tick_labels = [df.iloc[i]['timestamp'].strftime("%H:%M") for i in tick_indices]
         plt_ascii.xticks(tick_indices, tick_labels)

    # Subplot 2: Volume
    plt_ascii.subplot(2, 1)
    # Clear subplot to avoid merging labels/scales
    plt_ascii.clf()
    plt_ascii.theme('dark')
    volumes = df['volume'].tolist()
    plt_ascii.bar(indices, volumes, color='blue', label='Volume')
    plt_ascii.title("Volume")
    if len(df) > 5:
         plt_ascii.xticks(tick_indices, tick_labels)

    # Get plot size from console
    # On utilise 100% de la largeur disponible (moins une petite marge pour le panel)
    width = console.width - 4
    # On réduit la hauteur pour s'assurer que ça rentre dans le panel sans dépasser
    height = console.height - 24

    if width < 20: width = 20
    if height < 15: height = 15 # Minimum augmenté pour accueillir les deux subplots confortablement

    # Hauteur 1/3 pour le volume, 2/3 pour les k-lines
    h_volume = max(5, height // 3)
    h_klines = height - h_volume

    plt_ascii.subplot(1, 1).plotsize(width, h_klines)
    plt_ascii.subplot(2, 1).plotsize(width, h_volume)
    content = Text.from_ansi(plt_ascii.build())

    # Update cache
    chart_cache = {
         "symbol": symbol,
         "last_update": last_ts,
         "content": content
    }
    return content

def get_sorted_symbols(config):
    """Returns the list of trading symbols sorted by timeframe priority."""
    tf_priority = {'1s': 0, '1m': 1, '3m': 2, '5m': 3, '15m': 4, '30m': 5}
    with bot_lock:
        all_pairs = sorted(
            [s for s in bot_state.keys() if not s.startswith("_")],
            key=lambda x: (tf_priority.get(config['pairs'].get(x, {}).get('timeframe', '5m'), 99), x)
        )
    return all_pairs

def make_dashboard(global_mode, config):
    """
    Constructs the bot's Rich dashboard layout.

    The dashboard includes a header, log panel, pairs status panel, and a status bar.
    It handles marquee effects for scrolling text and toggles between standard
    and expert modes.

    Parameters
    ----------
    global_mode : str
        The current bot mode ('live', 'simulation', etc.).
    config : dict
        The bot configuration.

    Returns
    -------
    rich.layout.Layout
        The complete Rich layout for the dashboard.
    """
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

    all_pairs = get_sorted_symbols(config)
    with bot_lock:
        max_pair_w = max([len(s) for s in all_pairs] + [len("Pair")])
        max_tendency_w = max([len(bot_state[s].get('tendency', 'N/A')) for s in all_pairs] + [len("Tendency")])

        last_order_lens = []
        for s in all_pairs:
            lo = bot_state[s].get('last_action', 'Waiting')
            if lo == "WAITING": lo = "Waiting"
            last_order_lens.append(len(lo))
        max_last_order_w = max(last_order_lens + [len("Last Order")])

        signal_lens = []
        for s in all_pairs:
            data = bot_state[s]
            buy_count = data.get('consecutive_buys', 0)
            sell_count = data.get('consecutive_sells', 0)
            if buy_count > 0: sig = f"{buy_count} Buy"
            elif sell_count > 0: sig = f"{sell_count} Sell"
            else: sig = "Waiting"
            signal_lens.append(len(sig))
        max_signal_w = max(signal_lens + [len("Signal")])

        max_aggr_w = max([len(str(bot_state[s].get('aggr', 'N/A'))) for s in all_pairs] + [len("Aggress")])

        max_strat_w = 0
        for s in all_pairs:
            strats = bot_state[s].get('strategies', [bot_state[s].get('strategy', 'N/A')])
            for st in strats:
                max_strat_w = max(max_strat_w, len(str(st)) if st else 0)
        max_strat_w = max(max_strat_w, len("Strategy"))

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
            table.add_column("Pair", style="cyan", no_wrap=True, width=max_pair_w)
            table.add_column("TF", style="yellow", no_wrap=True, width=4)
            table.add_column("EMA F/S", style="green", no_wrap=True, width=18)
            table.add_column("MACD", style="blue", no_wrap=True, width=10)
            table.add_column("RSI", style="yellow", no_wrap=True, width=7)
            table.add_column("Vol/ADX", style="dim white", no_wrap=True, width=15)
            table.add_column("Flags", style="bold white", no_wrap=True, width=8)
            table.add_column("Scr", style="bold white", no_wrap=True, width=5)
            table.add_column("B.Prof", style="bold green", no_wrap=True, width=8)
            table.add_column("Aggress", style="white", no_wrap=True, width=max_aggr_w)
            table.add_column("Strategy", style="bold cyan", no_wrap=True, width=max_strat_w)
        else:
            table.add_column("Pair", style="cyan", no_wrap=True, width=max_pair_w)
            table.add_column("TF", style="yellow", no_wrap=True, width=4)
            table.add_column("Price", style="magenta", no_wrap=True, width=10)
            table.add_column("Amt", style="cyan", no_wrap=True, width=12)
            table.add_column("Entry", style="magenta", no_wrap=True, width=10)
            table.add_column("Fee", style="red", no_wrap=True, width=10)
            table.add_column("B.Prof", style="bold green", no_wrap=True, width=8)
            table.add_column("Tendency", style="bold white", no_wrap=True, width=max_tendency_w)
            table.add_column("Last Order", style="bold", no_wrap=True, width=max_last_order_w)
            table.add_column("Signal", style="bold", no_wrap=True, width=max_signal_w)
            table.add_column("Aggress", style="white", no_wrap=True, width=max_aggr_w)
            table.add_column("Strategy", style="bold cyan", no_wrap=True, width=max_strat_w)

        sorted_symbols = all_pairs
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
                 positions = data['position']
                 if isinstance(positions, list):
                      total_amount = sum(p['amount'] for p in positions)
                      # Prix d'entrée moyen pondéré
                      total_cost = sum(p['entry_price'] * p['amount'] for p in positions)
                      avg_entry_price = total_cost / total_amount if total_amount > 0 else 0
                      total_fee = sum(p.get('entry_fee', 0) for p in positions)

                      amt_str = f"{format_amt(total_amount, precision=data.get('amount_precision'))} ({len(positions)})"
                      entry_str = format_price(avg_entry_price, precision=data.get('price_precision'))
                      fee_str = format_price(total_fee, precision=data.get('price_precision'))
                 else:
                      # Compatibilité pour le cas où ce n'est pas encore une liste
                      amt_str = format_amt(positions['amount'], precision=data.get('amount_precision'))
                      entry_str = format_price(positions['entry_price'], precision=data.get('price_precision'))
                      fee_str = format_price(positions.get('entry_fee', 0), precision=data.get('price_precision'))

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
                      f"[yellow]{config['pairs'].get(symbol, {}).get('timeframe', '1m')}[/]",
                      f"{format_price(data.get('ema_f', 0), precision=data.get('price_precision'))}/{format_price(data.get('ema_s', 0), precision=data.get('price_precision'))}",
                      f"{data.get('macd_hist', 0):.4e}" if abs(data.get('macd_hist', 0)) < 0.001 else f"{data.get('macd_hist', 0):.4f}",
                      f"{data.get('rsi', 0):.2f}",
                      f"{data.get('volatility', 0):.6f}/{data.get('adx', 0):.2f}",
                      f"[{'bold cyan' if 'WHL' in flags_str else 'dim white'}]{flags_str}[/]",
                      f"{data.get('score', 0)}",
                      f"{data.get('expected_profit', 0):.5f}" if has_position else '0.00000',
                      data.get('aggr', 'N/A'),
                      data.get('strategy', 'N/A')
                 ]
            else:
                 row_vals = [
                      symbol,
                      f"[yellow]{config['pairs'].get(symbol, {}).get('timeframe', '1m')}[/]",
                      format_price(data.get('price', 0), precision=data.get('price_precision')),
                      amt_str, entry_str, fee_str,
                      f"{data.get('expected_profit', 0):.5f}" if has_position else '0.00000',
                      f"[{tend_style}]{tendency}[/]",
                      f"[{last_order_style}]{last_order}[/]",
                      f"[{signal_style}]{current_signal}[/]",
                      data.get('aggr', 'N/A'),
                      data.get('strategy', 'N/A')
                 ]

            # Rolling effect for strategy display
            if 'strategies' in data and len(data['strategies']) > 1:
                # Alternate every 2 seconds
                strat_idx = int(time.time() / 2) % len(data['strategies'])
                strat_display = data['strategies'][strat_idx]
                # Replace the strategy column (last one)
                row_vals[-1] = f"[bold cyan]{strat_display}[/]"

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
        help_text.append("  ENTER  : Show/Hide K-Lines et Volume for selected symbol\n")
        help_text.append("  X      : Toggle Expert Mode (Show/Hide Indicators)\n")
        help_text.append("  M      : Toggle Marquee Effect (Pause/Resume scrolling)\n")
        help_text.append("  H      : Close this help menu\n")
        help_text.append("  Ctrl+C : Stop the bot gracefully\n")

        pairs_panel = Panel(help_text, title="[bold]Help / Info[/]", border_style="bold yellow")

    if show_chart:
        chart_content = render_ascii_chart(chart_symbol, config)
        pairs_panel = Panel(chart_content, title=f"[bold]K-Lines et Volume: {chart_symbol}[/]", border_style="bold magenta")

    if not startup_complete:
         waiting_text = Text.from_markup("\n\n\n\n\n[bold blink yellow]Waiting for system initialization...[/]\n", justify="center")
         waiting_text.append_text(Text.from_markup("[dim]Fetching market data and calculating first signals...[/]\n", style="white"))
         pairs_panel = Panel(waiting_text, title="[bold]System Startup[/]", border_style="bold yellow")

    layout = Layout()
    layout.split(
        Layout(Panel(Text("CCXT Pro Trading Bot Dashboard", style="bold magenta", justify="center"), border_style="blue"), size=3),
        Layout(log_panel, size=log_height+2),
        Layout(pairs_panel, name="main"),
        Layout(Panel(status_display, title="Status", border_style="cyan"), size=3)
    )
    return layout

def input_thread_func(config):
    """
    Background thread to handle user keyboard input.

    Listens for keys to switch panels, scroll through logs or pairs,
    toggle chart view, and toggle expert/marquee modes.
    """
    global pairs_scroll_offset, selected_pair_index, show_chart, chart_symbol, logs_scroll_offset, focused_panel
    global pairs_pause_until, logs_pause_until, expert_mode, show_help, marquee_enabled, startup_complete
    while not shutdown_event.is_set():
        try:
            key = readchar.readkey()
            if not startup_complete:
                 continue
            # Calculate heights for clamping
            sorted_symbols = get_sorted_symbols(config)
            with bot_lock:
                 log_height = 8
                 max_logs_offset = max(0, len(all_logs) - log_height)
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

def ohlcv_watcher_thread(exchange, symbol, config):
    """
    Background thread to watch OHLCV updates for a symbol.
    Supports adaptive timeframe changes.
    """
    current_tf = config['pairs'].get(symbol, {}).get('timeframe', '1m')

    while not shutdown_event.is_set():
        try:
            # 1. Fetch initial historical data for the current timeframe
            ohlcv = exchange.fetch_ohlcv(symbol, current_tf, limit=500)
            if ohlcv:
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                df = df.drop_duplicates(subset=['timestamp']).set_index('timestamp', drop=False)
                with ohlcv_cache_lock:
                    ohlcv_cache[f"{symbol}_{current_tf}"] = df

            # 2. Watch for real-time updates
            for candle in exchange.watch_ohlcv(symbol, current_tf):
                if shutdown_event.is_set(): break

                # Check if timeframe needs to change
                with bot_lock:
                    new_tf = config['pairs'].get(symbol, {}).get('timeframe', '1m')

                if new_tf != current_tf:
                    logging.info(f"[{symbol}] Timeframe changing from {current_tf} to {new_tf}. Restarting watcher.")
                    current_tf = new_tf
                    break # Restart loop with new timeframe

                with ohlcv_cache_lock:
                    cache_key = f"{symbol}_{current_tf}"
                    if cache_key in ohlcv_cache:
                        df = ohlcv_cache[cache_key]
                        new_row = pd.DataFrame([candle], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        new_ts = pd.to_datetime(new_row['timestamp'], unit='ms').iloc[0]

                        # Check for gaps
                        if not df.empty:
                            last_ts = df.index[-1]
                            # Adaptive delta based on timeframe
                            delta_map = {'1s': timedelta(seconds=1), '1m': timedelta(minutes=1), '3m': timedelta(minutes=3), '5m': timedelta(minutes=5), '15m': timedelta(minutes=15), '30m': timedelta(minutes=30)}
                            period_delta = delta_map.get(current_tf, timedelta(minutes=1))

                            if new_ts > last_ts + period_delta:
                                since_ms = int(last_ts.timestamp() * 1000) + 1
                                missing_ohlcv = exchange.fetch_ohlcv(symbol, current_tf, since=since_ms)
                                if missing_ohlcv:
                                    m_df = pd.DataFrame(missing_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                                    m_df['timestamp'] = pd.to_datetime(m_df['timestamp'], unit='ms')
                                    m_df.set_index('timestamp', drop=False, inplace=True)
                                    df = pd.concat([df, m_df])

                        new_row['timestamp'] = new_ts
                        new_row.set_index('timestamp', drop=False, inplace=True)

                        if not df.empty and new_ts in df.index:
                            df.loc[new_ts] = new_row.iloc[0]
                        else:
                            df = pd.concat([df, new_row]).tail(1000)

                        df = df[~df.index.duplicated(keep='last')].sort_index()
                        ohlcv_cache[cache_key] = df
                    else:
                        df = pd.DataFrame([candle], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                        df.set_index('timestamp', drop=False, inplace=True)
                        ohlcv_cache[cache_key] = df
        except Exception as e:
            if not shutdown_event.is_set():
                logging.error(f"Error in OHLCV watcher for {symbol}: {e}")
                time.sleep(5)

def trading_thread_func(exchange, data_manager, pattern_manager, engine, config, mode):
    """
    Main background thread for the trading loop.

    Starts OHLCV watchers, periodically analyzes all pairs, handles
    re-scaning triggers, and executes buy/sell orders based on signals.

    Parameters
    ----------
    exchange : ExchangeInterface
        The exchange instance for data and orders.
    data_manager : DataManager
        Manager for trade persistence.
    pattern_manager : PatternManager
        Manager for historical success patterns.
    engine : TradingEngine
        Engine for risk and position sizing.
    config : dict
        The bot configuration.
    mode : str
        Bot operation mode ('live', 'simulation', etc.).
    """
    first_analysis_done = False

    # Initial Optimal Timeframe Discovery
    for symbol in config.get('pairs', {}):
        try:
            tf, score, _ = get_optimal_timeframe(exchange, symbol, config)
            config['pairs'][symbol]['optimal_tf'] = tf
            config['pairs'][symbol]['optimal_tf_score'] = score
            # Initialize adaptive timeframe
            config['pairs'][symbol]['timeframe'] = tf
        except:
            config['pairs'][symbol]['optimal_tf'] = '1m'
            config['pairs'][symbol]['optimal_tf_score'] = 0
            config['pairs'][symbol]['timeframe'] = '1m'

    # Start watchers for all pairs
    for symbol in config.get('pairs', {}):
        threading.Thread(target=ohlcv_watcher_thread, args=(exchange, symbol, config), daemon=True).start()

    priority_order = config.get('_priority_pairs')
    pairs_dict = config.get('pairs', {})
    pair_keys = priority_order if priority_order else list(pairs_dict.keys())

    active_scans = {} # symbol -> future
    last_scan_time = {sym: 0 for sym in pair_keys}

    time.sleep(5)
    markets = exchange.load_markets()

    # Initial sync happens exactly once at the start (Point 1)
    if mode == 'simulation':
        initialize_simulation(exchange, data_manager, pattern_manager, engine, config, bot_state)
    else:
        sync_live_positions(exchange, data_manager, config)

    # Use a persistent ProcessPoolExecutor for re-scaning
    with concurrent.futures.ProcessPoolExecutor() as bench_executor:
      while not shutdown_event.is_set():
        now_ts = time.time()
        try:
            # 1. Check completed re-scans
            completed_symbols = []
            for sym, future in active_scans.items():
                if future.done():
                    try:
                        sym_result, patterns = future.result()
                        if patterns:
                            best = patterns[0]
                            config['pairs'][sym]['aggr'] = best['aggr']
                            config['pairs'][sym]['strategy'] = best['strategy']
                            config['pairs'][sym]['expected_profit'] = best['profit']
                            pattern_manager.set_patterns(sym, patterns)
                            with bot_lock:
                                bot_state[sym]['aggr'] = best['aggr']
                                bot_state[sym]['strategy'] = best['strategy']
                                bot_state[sym]['expected_profit'] = best['profit']
                            # logging.info(f"[{sym}] Re-tested to {best['strategy']} ({best['aggr']})")

                            # Re-evaluate optimal timeframe after re-scanning (background)
                            # Every pair stays at 1m timeframe, we only update the score weighting
                            new_tf, new_score, _ = get_optimal_timeframe(exchange, sym, config)
                            config['pairs'][sym]['optimal_tf'] = new_tf
                            config['pairs'][sym]['optimal_tf_score'] = new_score

                    except Exception as e:
                        logging.error(f"Error in background re-scan for {sym}: {e}")
                    completed_symbols.append(sym)

                    # Periodically re-evaluate optimal timeframe adaptively
                    # Call only if it's been more than 15 minutes since last timeframe check
                    last_tf_check = config['pairs'][sym].get('_last_tf_check', 0)
                    if time.time() - last_tf_check > 900: # 15 minutes
                        try:
                            old_tf = config['pairs'][sym].get('timeframe')
                            old_score = config['pairs'][sym].get('optimal_tf_score', 0)
                            new_tf, new_score, _ = get_optimal_timeframe(exchange, sym, config, current_score=old_score)

                            config['pairs'][sym]['optimal_tf'] = new_tf
                            config['pairs'][sym]['optimal_tf_score'] = new_score
                            config['pairs'][sym]['_last_tf_check'] = time.time()

                            # Apply adaptive change
                            if new_tf != old_tf:
                                with bot_lock:
                                    config['pairs'][sym]['timeframe'] = new_tf
                                config['pairs'][sym]['_last_processed_ts'] = None
                                # ohlcv_watcher_thread will detect this and restart
                        except: pass

            for sym in completed_symbols:
                del active_scans[sym]

            potential_buys = []

            # 2. Parallelize pair analysis
            # Prioritize pairs waiting longest (Point 4)
            sorted_pair_keys = sorted(pair_keys, key=lambda x: last_scan_time.get(x, 0))

            if not first_analysis_done:
                logging.info("Waiting for first data streams and analysis...")

            # Determine which pairs need analysis based on their timeframe
            pairs_to_analyze = []
            for sym in sorted_pair_keys:
                tf = config['pairs'][sym].get('timeframe', '1m')
                cache_key = f"{sym}_{tf}"

                new_candle = False
                with ohlcv_cache_lock:
                    if cache_key in ohlcv_cache and not ohlcv_cache[cache_key].empty:
                        latest_ts = ohlcv_cache[cache_key].index[-1]
                        last_processed_ts = config['pairs'][sym].get('_last_processed_ts')

                        if last_processed_ts is None or latest_ts > last_processed_ts:
                            new_candle = True
                            config['pairs'][sym]['_last_processed_ts'] = latest_ts

                if new_candle or not first_analysis_done:
                    pairs_to_analyze.append(sym)

            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(pairs_to_analyze))) as executor:
                future_to_sym = {executor.submit(analyze_pair, exchange, data_manager, pattern_manager, sym, pairs_dict[sym], config, engine=engine): sym for sym in pairs_to_analyze}
                for future in concurrent.futures.as_completed(future_to_sym):
                    if shutdown_event.is_set(): break
                    symbol = future_to_sym[future]

                    # Check suspensions
                    now_ts = time.time()
                    if symbol in pair_suspensions:
                        susp = pair_suspensions[symbol]
                        if now_ts < susp.get('until', 0):
                            continue

                        # Special check for budget suspension
                        if susp.get('reason') == 'budget':
                            # Check USDC availability (1.5x)
                            try:
                                balance = exchange.fetch_balances()
                                if balance is None: continue
                                base_curr = symbol.split('/')[1]
                                free_bal = balance.get(base_curr, {}).get('free', 0) if isinstance(balance.get(base_curr), dict) else balance.get(base_curr, 0)
                                if free_bal >= susp.get('amount_required', 0) * 1.5:
                                    logging.info(f"[{symbol}] Budget recovered (1.5x available). Resuming pair.")
                                    del pair_suspensions[symbol]
                                else:
                                    continue
                            except: continue
                        else:
                            # Time-based suspension (e.g. HTTP 500)
                            logging.info(f"[{symbol}] Suspension expired. Resuming pair.")
                            del pair_suspensions[symbol]

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

                            # 3. Handle re-scaning triggers (Asynchronous)
                            # Prioritize pairs waiting longest and skip if has signal (Point 4)
                            if candles_since >= no_signal_thresh and symbol not in active_scans:
                                if data.get('buy') or data.get('sell'):
                                     # Pause/skip scanning if there is a signal
                                     candles_since = 0
                                else:
                                    last_scan_time[symbol] = now_ts
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

                                    # Submit re-scan task to ProcessPoolExecutor
                                    # Determine current technique to exclude (rotation rule)
                                    exclude = None
                                    with bot_lock:
                                         if symbol in bot_state:
                                              exclude = (bot_state[symbol].get('strategy'), bot_state[symbol].get('aggr'))

                                    active_scans[symbol] = bench_executor.submit(
                                        run_optimization_test_for_symbol, symbol, config, timeframe, aggrs, strategies, df_bench, engine, device, exclude_technique=exclude
                                    )

                            with bot_lock:
                                data['last_action'] = bot_state[symbol].get('last_action', 'WAITING')
                                # Inject precision from markets
                                if symbol in markets:
                                     m = markets[symbol]
                                     if 'precision' in m:
                                          data['price_precision'] = m['precision'].get('price')
                                          data['amount_precision'] = m['precision'].get('amount')

                                bot_state[symbol].update(data)
                                bot_state[symbol]['candles_since_last_signal'] = candles_since

                            if data.get('sell_triggered'):
                                 # Dans le cadre multi-lots, sell_triggered est vrai si SELL signal reçu
                                 # execute_sell s'occupera de ne vendre que les lots profitables

                                 # Vérifier si au moins un lot est profitable
                                 positions = data.get('position', [])
                                 fee_rate = 0.001
                                 try:
                                      fee_rate = exchange.fetch_trading_fee(symbol)
                                 except: pass

                                 profitable_positions = [p for p in positions if engine.is_profitable(data['price'], p['entry_price'], fee_rate=fee_rate, entry_total_base=p.get('entry_total_base', 0), amount=p['amount'])]

                                 if not profitable_positions:
                                      # Aucun lot n'est profitable, on ignore le signal pour l'instant
                                      data_manager.flag_ignore_sell(symbol, value=True)
                                      data['sell'] = False
                                      data['sell_triggered'] = False

                                      # Trigger re-scan
                                      if symbol not in active_scans:
                                           timeframe = config['pairs'][symbol].get('timeframe', '1m')
                                           cache_key = f"{symbol}_{timeframe}"
                                           df_bench = None
                                           with ohlcv_cache_lock:
                                                if cache_key in ohlcv_cache:
                                                     df_bench = ohlcv_cache[cache_key].copy()
                                           if df_bench is not None and not df_bench.empty:
                                                active_scans[symbol] = bench_executor.submit(
                                                     run_optimization_test_for_symbol, symbol, config, timeframe, ['dynamic'], STRATEGIES, df_bench, engine, device,
                                                     exclude_technique=(bot_state[symbol].get('strategy'), bot_state[symbol].get('aggr'))
                                                )
                                 elif execute_sell(exchange, data_manager, engine, symbol, data, config):
                                      with bot_lock:
                                          bot_state[symbol]['last_action'] = 'SELL'
                                          bot_state[symbol]['position'] = None
                                          data['last_action'] = 'SELL'
                                          data['position'] = None
                                      play_sound("sell", config)

                            # Achat possible si signal BUY et limite de lots non atteinte
                            max_lots = config['pairs'].get(symbol, {}).get('max_lots_per_symbol') or config.get('max_lots_per_symbol', 1)
                            current_lots = len(data.get('position', []) or [])
                            if data.get('buy') and current_lots < max_lots:
                                 potential_buys.append((symbol, data))
                    except Exception as e:
                        err_msg = str(e)
                        http_err = re.search(r'(HTTP \d{3}) Error Code', err_msg)
                        if http_err:
                            status_code = http_err.group(1)
                            logging.error(f"Error analyzing {symbol}: {status_code} Error Code")
                            # Suspend for 21 minutes
                            pair_suspensions[symbol] = {'until': time.time() + 21 * 60, 'reason': 'http_error'}
                        else:
                            logging.error(f"Error analyzing {symbol}: {e}")

            if potential_buys and not shutdown_event.is_set():
                max_open = int(config.get('max_open_positions', 18))
                current_open = len(data_manager.get_open_positions())
                slots_available = max_open - current_open
                if slots_available > 0:
                     # Prioritize by signal score
                     potential_buys.sort(key=lambda x: x[1].get('score', 0), reverse=True)
                     balance = exchange.fetch_balances()
                     if balance is None:
                          logging.error("Failed to fetch balances for execution loop.")
                          continue

                     for i in range(min(len(potential_buys), slots_available)):
                          if shutdown_event.is_set(): break
                          symbol, data = potential_buys[i]
                          tf = config['pairs'].get(symbol, {}).get('timeframe', '1m')
                          if execute_buy(exchange, data_manager, engine, symbol, data, config, balance=balance, timeframe=tf):
                               with bot_lock:
                                   bot_state[symbol]['last_action'] = 'BUY'
                                   bot_state[symbol]['position'] = data_manager.get_position(symbol)
                                   data['last_action'] = 'BUY'
                                   data['position'] = data_manager.get_position(symbol)
                               play_sound("buy", config)
                               # Update balance for next iteration
                               balance = exchange.fetch_balances()

            if not first_analysis_done:
                logging.info("First data streams acquired and analysis done.")
                first_analysis_done = True
                global startup_complete
                startup_complete = True

            # Check if any pair is on 1s timeframe
            has_1s = any(config['pairs'][s].get('timeframe') == '1s' for s in pair_keys)

            # Dynamic sleep based on fastest timeframe
            if has_1s:
                # 0.5s loop for 1s response
                for _ in range(5):
                    if shutdown_event.is_set(): break
                    time.sleep(0.1)
            else:
                # 2s loop for other timeframes
                for _ in range(20):
                    if shutdown_event.is_set(): break
                    time.sleep(0.1)
        except Exception as e:
            logging.error(f"Error in trading thread: {e}")
            time.sleep(5)


def main():
    """
    Entry point for the CCXT Pro Trading Bot.

    Parses command-line arguments, detects hardware acceleration (GPU/SIMD),
    initializes the exchange, and starts the dashboard and trading threads.
    """
    parser = argparse.ArgumentParser(description='CCXT Pro Trading Bot')
    parser.add_argument('--no-gpu', action='store_true', help='Disable GPU acceleration (force CPU)')
    parser.add_argument('--exchange', help='CCXT Exchange ID to use (e.g., binance, kraken, bitvavo)')
    parser.add_argument('--mode', choices=['live', 'simulation', 'balance'], default='simulation', help='Bot mode')
    parser.add_argument('--config', help='Path to config file (optional, defaults to config.json or config.default.json)')
    parser.add_argument('--symbol', help='Target symbol (e.g. BTC/EUR)')
    parser.add_argument('--timeframe', choices=['1m', '3m', '5m', '15m', '30m'], help='Manual timeframe override')
    parser.add_argument('--since', help='Start date (YYYY-MM-DD HH:MM)')
    parser.add_argument('--until', help='End date (YYYY-MM-DD HH:MM)')

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
    pairs_from_file = []
    if os.path.exists('pairs.txt'):
        with open('pairs.txt', 'r') as f:
            pairs_from_file = [line.strip() for line in f if line.strip()]

    if 'pairs' not in config:
        config['pairs'] = {}

    for p in pairs_from_file:
        if p not in config['pairs']:
            config['pairs'][p] = {}

    # Ensure all configured pairs are in pairs_from_file (optional but good for consistency)
    pairs = list(config['pairs'].keys())
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

    with console.status("[bold green]Initializing CCXT Pro Trading Bot...", spinner="dots") as status:

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
            console.print("[yellow]Computations will run on CPU, which can be significantly slower (minutes to hours) for the first tests.[/]")
            console.print("[yellow]Please ensure test_cache.json remains intact once finished to avoid re-running slow tests.[/]")
        else:
            console.print(f"[bold green]GPU Acceleration enabled using device: {device}[/]")

        db_handler.duration = 5
        data_manager = DataManager() if args.mode in ['live', 'simulation'] else None
        pattern_manager = PatternManager()
        engine = TradingEngine(config)

        # Prioritize exchange_id: api.json > CLI argument > config > default 'binance'
        exchange_id = api_creds.get('exchange_id') or args.exchange or config.get('exchange') or 'binance'
        exchange_options = api_creds.get('options')

        # Use credentials from api.json if available, otherwise config.default.json
        api_key = api_creds.get('api_key') or config.get('api_key')
        api_secret = api_creds.get('api_secret') or config.get('api_secret')

        if args.mode == 'live':
            exchange = CCXTExchange(exchange_id, api_key, api_secret, options=exchange_options)
            logging.info(f"Starting bot in LIVE mode on {exchange_id}")
        elif args.mode == 'simulation':
            exchange = MockExchange(api_key, api_secret, exchange_id=exchange_id, options=exchange_options)
            logging.info(f"Starting bot in SIMULATION mode ({exchange_id})")
        elif args.mode == 'balance':
            exchange = MockExchange(api_key, api_secret, exchange_id=exchange_id, options=exchange_options) if api_key in [None, "YOUR_API_KEY"] else CCXTExchange(exchange_id, api_key, api_secret, options=exchange_options)
            exchange.load_markets()
            show_balances(exchange)
            return

        pairs = config.get('pairs', {})
        # Global override for agressivity
        global_agressivity = config.get('force_agressivity_to_all_pairs')

        if args.mode in ['live', 'simulation']:
            # Do not clear history if we want to see previous trades
            # if args.mode == 'simulation' and data_manager:
            #     data_manager.clear_history()
            pass

        for symbol in pairs:
            # Check if we already have an open position for this symbol
            pos = data_manager.get_position(symbol)

            pair_cfg = config['pairs'][symbol]
            techniques_cfg = pair_cfg.get('techniques', [])
            if not techniques_cfg:
                # Default: all strategies with all common aggr levels
                techniques_cfg = [{"strategy": s, "aggr": ["normal", "aggressive", "dynamic"]} for s in STRATEGIES]

            bot_state[symbol] = {
                'aggr': techniques_cfg[0].get('aggr', ['normal'])[0] if isinstance(techniques_cfg[0].get('aggr'), list) else techniques_cfg[0].get('aggr', 'normal'),
                'strategy': techniques_cfg[0].get('strategy', 'N/A'),
                'strategies': [t.get('strategy') for t in techniques_cfg],
                'last_action': 'BUY' if pos else 'Waiting',
                'position': pos,
                'expected_profit': 0
            }

    threading.Thread(target=input_thread_func, args=(config,), daemon=True).start()
    threading.Thread(target=trading_thread_func, args=(exchange, data_manager, pattern_manager, engine, config, args.mode), daemon=True).start()

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
    """
    Plays a system sound based on the bot's action.

    Parameters
    ----------
    action : str
        The action that triggered the sound ('startup', 'buy', 'sell').
    config : dict, optional
        Bot configuration, used for randomized startup sounds.
    """
    system = platform.system().lower()
    try:
        if system == "windows":
            import winsound
            if action == "startup":
                 # Randomized sequence equal to max_open_positions - 4
                 num_blips = max(4, (int(config.get('max_open_positions', 26)) if config else 26) - 18)
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
    """
    Analyzes a trading pair to determine buy/sell signals based on technical indicators and patterns.

    This function fetches OHLCV data, calculates technical indicators, performs pattern matching
    against historical success patterns, and evaluates signals using a dynamic risk engine.

    Parameters
    ----------
    exchange : ExchangeInterface
        The exchange instance to fetch data from if not in cache.
    data_manager : DataManager
        Manager for persistent trade data and position tracking.
    pattern_manager : PatternManager
        Manager for historical success patterns used in similarity matching.
    symbol : str
        The trading pair symbol (e.g., 'BTC/USDT').
    pair_config : dict
        Configuration specific to the trading pair (e.g., timeframe, strategy).
    global_config : dict
        Global bot configuration settings.
    engine : TradingEngine, optional
        The trading engine for dynamic risk and setting adjustments.

    Returns
    -------
    dict or None
        A dictionary containing signal data, price, indicators, and state transitions
        (e.g., 'buy', 'sell', 'sell_triggered'). Returns None if data is unavailable.

    Notes
    -----
    The function maintains state transitions for consecutive signals to avoid false positives
    and applies volatility-based confirmation windows. It also handles re-initialization
    of signal counts on first run by looking at historical data.
    """
    # Force adaptive timeframe for analysis
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
    df = get_signals(df, {"device": device}, is_scan=False)
    latest_row_base = df.iloc[-1]

    # Dynamic Activation & Multi-strategy Evaluation
    techniques_cfg = pair_config.get('techniques', [])
    if not techniques_cfg:
        # If no configuration, try all aggressiveness profiles for all strategies
        techniques_cfg = []
        for strat in STRATEGIES:
            techniques_cfg.append({"strategy": strat, "aggr": ["normal", "aggressive", "dynamic"]})

    # Use Dynamic Risk Engine if engine is available
    if engine:
        mode_settings = engine.get_dynamic_settings(latest_row_base.get('adx', 0), latest_row_base.get('volatility', 0))
    else:
        # Fallback to balanced defaults if no engine
        mode_settings = {
            "ema_fast": 20, "ema_slow": 50, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
            "rsi_period": 14, "rsi_buy": 30, "rsi_sell": 70, "confirmation_window": 1
        }
    mode_settings['device'] = device

    # Evaluate signals for all techniques and aggressiveness profiles
    buy_count = 0
    sell_count = 0
    total_tendency_score = 0
    total_score = 0

    tf_score = pair_config.get('optimal_tf_score', 0)
    all_results = []

    # Optimization: Pre-calculate indicator variants for different profiles
    # (Since profiles only affect parameters like EMA/RSI periods)
    profile_results = {}
    unique_profiles = set()
    for t in techniques_cfg:
        aggr_list = t.get('aggr', ['normal'])
        if isinstance(aggr_list, str): aggr_list = [aggr_list]
        unique_profiles.update(aggr_list)

    for p in unique_profiles:
        p_settings = engine.get_dynamic_settings(latest_row_base.get('adx', 0), latest_row_base.get('volatility', 0), aggr=p)
        p_settings['device'] = device
        # Pre-calculate base indicators for this profile
        # We need a way to pass this to get_signals to avoid full recalculation
        profile_results[p] = p_settings

    # To avoid blocking, we limit the number of techniques scanned if it's too many
    # or we use a more efficient way. For now, let's limit to top strategies if
    # none were specified.
    active_techniques = techniques_cfg
    if not pair_config.get('techniques') and len(active_techniques) > 5:
        # If no specific config, and we have many strategies, maybe just pick a subset
        # or just run them all but be aware of the performance hit.
        pass

    # Multi-technique scanning (Point 3)
    # The user wants ALL techniques scanned if no specific config is provided.
    # Map aggr -> list of strategies to minimize recalculations of common indicators
    aggr_to_strats = {}
    for t in active_techniques:
        strat = t.get('strategy')
        aggr_list = t.get('aggr', ['normal'])
        if isinstance(aggr_list, str): aggr_list = [aggr_list]
        for a in aggr_list:
            if a not in aggr_to_strats: aggr_to_strats[a] = []
            aggr_to_strats[a].append(strat)

    # Multi-processing for faster multi-technique scanning (Optimization Point 3)
    # Given the high number of strategy/aggr combinations, we use parallelism.
    all_tasks = []
    for aggr, strats in aggr_to_strats.items():
        ts_base = profile_results[aggr].copy()
        ts_base['strategy'] = None
        df_aggr = get_signals(df.copy(), ts_base, is_scan=False)
        for strategy in strats:
            ts = ts_base.copy()
            ts['strategy'] = strategy
            all_tasks.append((df_aggr, ts))

    # Using ThreadPoolExecutor for lightweight tasks that are partially GPU accelerated
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(all_tasks), 16)) as t_executor:
        futures = [t_executor.submit(get_signals, d, t, False) for d, t in all_tasks]
        for future in concurrent.futures.as_completed(futures):
            try:
                res_df = future.result()
                if res_df.empty: continue
                latest = res_df.iloc[-1]
                all_results.append(latest)
                t_map = {"Bullish": 1, "Bearish": -1, "Neutral": 0, "Range": 0}
                total_tendency_score += t_map.get(latest.get('tendency', 'Neutral'), 0)
                total_score += latest.get('score', 0)
                if latest.get('buy_signal'): buy_count += 1
                if latest.get('sell_signal'): sell_count += 1
            except: pass

    # Score calculation
    final_buy_score = buy_count + tf_score if buy_count > 0 else 0
    final_sell_score = sell_count + tf_score if sell_count > 0 else 0

    final_buy_confirmed = False
    final_sell_confirmed = False

    if final_buy_score > final_sell_score and final_buy_score > 0:
        final_buy_confirmed = True
    elif final_sell_score > final_buy_score and final_sell_score > 0:
        final_sell_confirmed = True

    latest_row = all_results[0] if all_results else latest_row_base

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

    if last_candle_ts != candle_ts:
        if final_buy_confirmed:
            consecutive_buys += 1
            consecutive_sells = 0
        elif final_sell_confirmed:
            consecutive_sells += 1
            consecutive_buys = 0
        else:
            consecutive_buys = 0
            consecutive_sells = 0
            if data_manager.get_position(symbol):
                data_manager.flag_ignore_sell(symbol, value=False)
    else:
        if not final_buy_confirmed and not final_sell_confirmed:
            consecutive_buys = 0
            consecutive_sells = 0
            if data_manager.get_position(symbol):
                data_manager.flag_ignore_sell(symbol, value=False)

    # Dynamic confirmation window based on volatility
    buy_threshold = 1
    sell_threshold = 1
    volatility = latest_row.get('volatility', 0)
    if volatility > 0.1: # High volatility
        buy_threshold += 1
        sell_threshold += 1

    # Validation Monte Carlo pour les transactions réelles
    mc_verified_buy = False
    mc_verified_sell = False
    positions = data_manager.get_position(symbol)
    if consecutive_buys >= buy_threshold or (consecutive_sells >= sell_threshold and positions):
        mc = MonteCarloEngine(num_simulations=500, timeframe_candles=20)
        mc.set_device(device if device is not None else torch.device("cpu"))
        mc_score = mc.validate_strategy(df)

        if consecutive_buys >= buy_threshold:
            if mc_score > 0.45: # Acceptance threshold
                mc_verified_buy = True

        if consecutive_sells >= sell_threshold:
            if mc_score > 0.35: # Slightly lower threshold for exit to be safe
                mc_verified_sell = True

    # Determine final tendency and total score for UI
    final_tendency = "Neutral"
    if total_tendency_score > 0: final_tendency = "Bullish"
    elif total_tendency_score < 0: final_tendency = "Bearish"

    return {
        'price': latest_row['close'],
        'ema_f': latest_row.get('ema_f', 0),
        'ema_s': latest_row.get('ema_s', 0),
        'macd_hist': latest_row.get('macd_hist', 0),
        'rsi': latest_row.get('rsi', 0),
        'adx': latest_row.get('adx', 0),
        'volatility': latest_row.get('volatility', 0),
        'score': total_score,
        'whale_active': bool(latest_row.get('whale_active', 0)),
        'is_mean_rev': bool(latest_row.get('is_mean_rev', 0)),
        'aggr': techniques_cfg[0].get('aggr', ['normal'])[0] if isinstance(techniques_cfg[0].get('aggr'), list) else techniques_cfg[0].get('aggr', 'normal'),
        'strategy': techniques_cfg[0].get('strategy', 'N/A'),
        'strategies': [t.get('strategy') for t in techniques_cfg],
        'tendency': final_tendency,
        'buy': mc_verified_buy,
        'sell': mc_verified_sell,
        'consecutive_buys': consecutive_buys,
        'consecutive_sells': consecutive_sells,
        '_last_candle_ts': candle_ts,
        'sell_triggered': mc_verified_sell and positions and any(not p.get('ignore_sell') for p in positions),
        'position': data_manager.get_position(symbol),
        'expected_profit': float(pair_config.get('expected_profit', 0)),
        'trigger_data': trigger_data
    }

def execute_buy(exchange, data_manager, engine, symbol, data, global_config, balance=None, timeframe='1m'):
    """
    Exécute un ordre d'achat pour une paire de trading spécifique.

    Calcule la taille de la position en fonction du solde et de la série de victoires,
    vérifie les fonds disponibles et place un ordre d'achat au marché.
    """
    if balance is None:
        balance = exchange.fetch_balances()

    if balance is None:
        logging.error(f"[{symbol}] Buy aborted: balances are unavailable.")
        return False

    win_streak = data_manager.get_win_streak(symbol)

    # Limite de lots
    max_lots = global_config['pairs'].get(symbol, {}).get('max_lots_per_symbol') or global_config.get('max_lots_per_symbol', 1)
    current_positions = data_manager.get_position(symbol) or []
    if len(current_positions) >= max_lots:
        logging.warning(f"[{symbol}] Achat annulé : Limite de lots atteinte ({len(current_positions)}/{max_lots}).")
        return False

    # Utilise le prix le plus frais du cache OHLCV
    timeframe = global_config['pairs'].get(symbol, {}).get('timeframe', '1m')
    cache_key = f"{symbol}_{timeframe}"

    current_price = data['price']
    with ohlcv_cache_lock:
        if cache_key in ohlcv_cache and not ohlcv_cache[cache_key].empty:
            current_price = ohlcv_cache[cache_key].iloc[-1]['close']

    base_curr = symbol.split('/')[1]
    amount = engine.calculate_position_size(
        balance, current_price, base_curr, win_streak=win_streak, max_lots=max_lots, timeframe=timeframe
    )
    base_currency = symbol.split('/')[1]
    if amount > 0:
        # Check if balance is sufficient before attempting order
        # Use API before any buy to verify availability (Point 2)
        try:
             balance = exchange.fetch_balances()
        except: pass

        cost = amount * current_price

        # Check Notional Limit (Point 3)
        try:
            market = exchange.markets.get(symbol) if hasattr(exchange, 'markets') else None
            if not market:
                # Fallback if markets not loaded
                markets = exchange.load_markets()
                market = markets.get(symbol)

            if market and 'limits' in market and 'cost' in market['limits'] and market['limits']['cost']['min']:
                min_notional = float(market['limits']['cost']['min'])
                if cost < min_notional:
                    # Attempt to increase trade size to minimum allowed
                    new_amount = min_notional / current_price

                    # Round up to the nearest valid amount to avoid being still below min notional
                    if 'precision' in market and 'amount' in market['precision'] and market['precision']['amount']:
                        prec = market['precision']['amount']
                        # Check if precision is decimal places or step size
                        if isinstance(prec, float) or (isinstance(prec, str) and '.' in prec):
                             step = float(prec) if isinstance(prec, float) else float(prec)
                             new_amount = math.ceil(new_amount / step) * step
                        else:
                             # Assume it's decimal places
                             new_amount = math.ceil(new_amount * (10**int(prec))) / (10**int(prec))

                    # Use exchange precision if available
                    if hasattr(exchange, 'amount_to_precision'):
                        new_amount = float(exchange.amount_to_precision(symbol, new_amount))

                    new_cost = new_amount * current_price
                    if new_cost < min_notional:
                         # Still below min notional? Add one more precision step
                         if 'precision' in market and 'amount' in market['precision'] and market['precision']['amount']:
                              prec = market['precision']['amount']
                              step = float(prec) if isinstance(prec, float) else (10**-int(prec))
                              new_amount += step
                              if hasattr(exchange, 'amount_to_precision'):
                                   new_amount = float(exchange.amount_to_precision(symbol, new_amount))
                              new_cost = new_amount * current_price

                    logging.info(f"[{symbol}] Cost {cost:.2f} is below min notional {min_notional:.2f}. Adjusting amount from {amount:.6f} to {new_amount:.6f} (New cost: {new_cost:.2f})")
                    amount = new_amount
                    cost = new_cost
        except Exception as ne:
            logging.warning(f"[{symbol}] Could not verify notional limit: {ne}")

        base_asset = base_currency
        free_balance = balance.get(base_asset, {}).get('free', 0) if isinstance(balance.get(base_asset), dict) else balance.get(base_asset, 0)

        if free_balance < cost:
            logging.warning(f"[{symbol}] Buy aborted: Insufficient {base_asset} balance ({format_price(free_balance)} < {format_price(cost)}). Suspending pair until 1.5x budget available.")
            pair_suspensions[symbol] = {'reason': 'budget', 'amount_required': cost}
            return False

        try:
            order = exchange.create_order(symbol, 'buy', amount)
            if isinstance(order, dict) and 'insufficient balance' in str(order.get('message', '')).lower():
                logging.error(f"Error during buy order on {symbol} via {getattr(exchange, 'exchange_id', 'exchange')} " + '{"code":-1013,"msg":"Insufficient balance"}')
                pair_suspensions[symbol] = {'reason': 'budget', 'amount_required': cost}
                return False
            if isinstance(order, dict) and 'code' in str(order) and 'Filter failure: NOTIONAL' in str(order):
                logging.error(f"Error during buy order on {symbol} via {getattr(exchange, 'exchange_id', 'exchange')} " + '{"code":-1013,"msg":"Filter failure: NOTIONAL"}')
                pair_suspensions[symbol] = {'reason': 'budget', 'amount_required': cost}
                return False
            if order:
                fee = order.get('calculated_fee', 0)
                # Use actual price from order if available
                final_entry_price = order.get('price') or current_price
                total_paid = (amount * final_entry_price) + fee
                logging.info(f"[{symbol}] Executing buy of amount {format_amt(amount)} at {format_price(final_entry_price)}, final price paid: {format_price(total_paid)} {symbol.split('/')[1] if '/' in symbol else 'EUR'}")
                data_manager.add_position(symbol, final_entry_price, amount, fee, data.get('trigger_data', {}), time.time(), total_base=total_paid)
                return True
            else:
                logging.warning(f"[{symbol}] Buy execution failed: Exchange rejected order for amount {amount:.6f}. Suspending pair.")
        except Exception as e:
            logging.error(f"Error during buy order on {symbol} via {getattr(exchange, 'exchange_id', 'exchange')} {e}")
            pair_suspensions[symbol] = {'reason': 'budget', 'amount_required': cost}
            return False
    else:
        logging.warning(f"[{symbol}] Buy aborted: Calculated amount is zero or negative.")
    return False

def execute_sell(exchange, data_manager, engine, symbol, data, global_config):
    """
    Exécute un ordre de vente pour clôturer les lots rentables.
    """
    positions = data['position']
    if not positions:
        return False

    base_asset = symbol.split('/')[0]
    is_simulation = isinstance(exchange, MockExchange)

    # Refresh price from cache for maximum responsiveness
    timeframe = global_config['pairs'].get(symbol, {}).get('timeframe', '1m')
    cache_key = f"{symbol}_{timeframe}"
    current_price = data['price']
    with ohlcv_cache_lock:
        if cache_key in ohlcv_cache and not ohlcv_cache[cache_key].empty:
            current_price = ohlcv_cache[cache_key].iloc[-1]['close']

    # Récupérer le taux de commission
    fee_rate = 0.001
    try:
        fee_rate = exchange.fetch_trading_fee(symbol)
    except:
        pass

    any_sold = False

    # On parcourt les lots en sens inverse pour ne pas perturber les index lors de la suppression
    for i in range(len(positions) - 1, -1, -1):
        pos = positions[i]

        # Vérifier si le lot est profitable (Point 1 du sujet)
        # Un lot peut être revendu dès que son prix d'acquisition est dépassé par son potentiel prix de vente
        if engine.is_profitable(current_price, pos['entry_price'], fee_rate=fee_rate, entry_total_base=pos.get('entry_total_base', 0), amount=pos['amount']):

            balance = exchange.fetch_balances()
            if balance is None: continue
            free_balance = balance.get(base_asset, {}).get('free', 0) if 'free' in balance else balance.get(base_asset, 0)

            if is_simulation or free_balance >= pos['amount']:
                order = exchange.create_order(symbol, 'sell', pos['amount'])
                if isinstance(order, dict) and order.get('error') == 'dust_limit':
                    logging.warning(f"[{symbol}] Vente lot {i} annulée : Poussière. Ignoré pour ce lot.")
                    # Marquer ce lot spécifique pour ignorer les ventes ?
                    # Pour simplifier on passe au suivant.
                    continue

                if order:
                    fee = order.get('calculated_fee', 0)
                    amount = pos['amount']
                    # Use actual order price if returned, otherwise our current_price
                    actual_price = order.get('price') or current_price
                    total_received = (amount * actual_price) - fee
                    profit = total_received - pos.get('entry_total_base', 0)
                    quote = symbol.split('/')[1]
                    logging.info(f"[{symbol}] Vente profitable du lot {i + 1} d'un montant {format_amt(amount)} à {format_price(actual_price)} (profit: {profit:.2f} {quote})")

                    data_manager.close_position(symbol, actual_price, fee, profit, data.get('trigger_data', {}), time.time(), total_base=total_received, lot_index=i)
                    any_sold = True
        else:
            logging.info(f"[{symbol}] Le lot {i + 1} (achat: {format_price(pos['entry_price'])}) n'est pas encore profitable.")

    return any_sold

def initialize_simulation(exchange, data_manager, pattern_manager, engine, config, bot_state):
    """
    Initializes simulation mode by discovering potential entry positions.

    Syncs live positions first, then scans all configured pairs for buy signals
    to populate the initial simulation state.

    Parameters
    ----------
    exchange : ExchangeInterface
        The exchange instance.
    data_manager : DataManager
        Manager for trade history.
    pattern_manager : PatternManager
        Manager for historical success patterns.
    engine : TradingEngine
        Engine for risk and position sizing.
    config : dict
        Bot configuration.
    bot_state : dict
        Shared state for the UI dashboard.
    """
    logging.info("Initializing Simulation positions (Discovery phase)...")
    sync_live_positions(exchange, data_manager, config)
    # Then proceed with virtual buy signals...
    priority_order = config.get('_priority_pairs')
    pairs_dict = config.get('pairs', {})
    pair_keys = priority_order if priority_order else list(pairs_dict.keys())

    potential_buys = []
    for symbol in pair_keys:
        pair_config = pairs_dict[symbol]
        # En simulation, on peut aussi acheter plusieurs lots au démarrage si des signaux BUY sont présents
        max_lots = pair_config.get('max_lots_per_symbol') or config.get('max_lots_per_symbol', 1)
        current_lots = len(data_manager.get_position(symbol) or [])

        if current_lots < max_lots:
            data = analyze_pair(exchange, data_manager, pattern_manager, symbol, pair_config, config, engine=engine)
            if data and data.get('buy'):
                potential_buys.append((symbol, data))

    if potential_buys:
        max_open = int(config.get('max_open_positions', 18))
        current_open = len(data_manager.get_open_positions())
        slots_available = max_open - current_open
        if slots_available > 0:
            # Prioritize by signal score
            potential_buys.sort(key=lambda x: x[1].get('score', 0), reverse=True)
            balance = exchange.fetch_balances()
            if balance is None:
                 logging.error("Failed to fetch balances for simulation initialization.")
                 return

            for i in range(min(len(potential_buys), slots_available)):
                symbol, data = potential_buys[i]
                tf = config['pairs'].get(symbol, {}).get('timeframe', '1m')
                if execute_buy(exchange, data_manager, engine, symbol, data, config, balance=balance, timeframe=tf):
                    with bot_lock:
                        bot_state[symbol]['position'] = data_manager.get_position(symbol)
                        bot_state[symbol]['price'] = data['price']
                        bot_state[symbol]['last_action'] = 'BUY'
                    # Refresh balance for next buy
                    balance = exchange.fetch_balances()

    logging.info(f"Initialization of the simulation positions completed.")

def sync_live_positions(exchange, data_manager, config):
    """
    Synchronizes local position tracking with real wallet balances from the exchange.

    Identifies sellable assets (non-dust) and adds them to the `DataManager`
    as open positions.

    Parameters
    ----------
    exchange : ExchangeInterface
        The exchange instance to fetch balances from.
    data_manager : DataManager
        Manager to update with discovered positions.
    config : dict
        Bot configuration for identifying base currencies and pairs.
    """
    exchange_id = getattr(exchange, 'exchange_id', 'Exchange')
    logging.info(f"Syncing positions from {exchange_id} API...")
    balance = exchange.fetch_balances()
    if balance is None:
        logging.error("Failed to sync live positions: balances are unavailable. Check API credentials or if your computer's clock is synchronized.")
        return

    # Robustly handle different balance structures
    if isinstance(balance, dict) and 'free' in balance and isinstance(balance['free'], dict):
        free_balances = balance['free']
    else:
        free_balances = balance
    base_currencies = config.get('base_currencies', ['EUR'])

    # We keep local cache but will update it to avoid redundant history fetch
    # data_manager.data['open_positions'] = {}
    sellable_found = False

    # Pre-fetch all tickers to avoid multiple API calls
    all_tickers = {}
    try:
        if hasattr(exchange.exchange, 'fetch_tickers'):
            all_tickers = exchange.exchange.fetch_tickers()
    except: pass

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

        # Vérifie si nous connaissons déjà cette position pour éviter une récupération redondante de l'historique
        existing_pos_list = data_manager.get_position(symbol)
        if existing_pos_list:
            total_existing_amount = sum(p['amount'] for p in existing_pos_list)
            if abs(total_existing_amount - amount) / amount < 0.001:
                sellable_found = True
                continue

        # Check if it's dust
        is_dust = False
        try:
            markets = exchange.markets if hasattr(exchange, 'markets') and exchange.markets else exchange.load_markets()
            ticker = all_tickers.get(symbol) or exchange.fetch_ticker(symbol)
            if symbol in markets:
                m = markets[symbol]
                min_amt = m['limits']['amount']['min']
                min_cost = m['limits']['cost']['min'] or 10
                if ticker and (amount < min_amt or (amount * ticker['last']) < min_cost):
                    is_dust = True
            elif amount <= 0.000001: is_dust = True
        except: pass

        if is_dust: continue
        sellable_found = True

        # Fetch current price for placeholder entry
        curr_price = ticker['last'] if ticker else 0
        if curr_price == 0:
            try:
                 ticker = exchange.fetch_ticker(symbol)
                 if ticker: curr_price = ticker['last']
            except: pass

        if curr_price > 0:
             # Attempt to fetch real entry price and fee from trade history
             entry_price = curr_price
             entry_fee = 0
             entry_total_base = amount * curr_price

             try:
                  # limit to 10 for performance
                  my_trades = exchange.fetch_my_trades(symbol, limit=10)
                  if my_trades:
                       # Filter buy trades
                       buys = [t for t in my_trades if t['side'] == 'buy']
                       if buys:
                            # Sort by timestamp descending
                            buys.sort(key=lambda x: x['timestamp'], reverse=True)

                            # We take the most recent buy(s) that could have formed this position
                            # For simplicity, we take the last one's price and fee rate
                            last_buy = buys[0]
                            entry_price = last_buy['price']

                            # Calculate total fee if possible
                            total_fee = 0
                            accumulated_amount = 0
                            for b in buys:
                                 if accumulated_amount >= amount * 0.99: # 1% tolerance
                                      break

                                 trade_amt = b['amount']
                                 trade_price = b['price']

                                 if 'fee' in b and b['fee']:
                                      fee_cost = b['fee'].get('cost', 0)
                                      fee_currency = b['fee'].get('currency')
                                      _, quote = symbol.split('/')

                                      if fee_currency and fee_currency != quote:
                                           try:
                                                fticker = exchange.fetch_ticker(f"{fee_currency}/{quote}")
                                                if fticker: fee_cost *= fticker['last']
                                           except: pass
                                      total_fee += fee_cost

                                 accumulated_amount += trade_amt

                            entry_fee = total_fee
                            entry_total_base = (amount * entry_price) + entry_fee
                            # logging.info(f"[{symbol}] Recovered real entry price {entry_price} and fees {entry_fee} from trade history.")
             except Exception as e:
                  logging.warning(f"[{symbol}] Failed to recover trade history: {e}")

             data_manager.add_position(symbol, entry_price, amount, entry_fee, {"info": "auto_populated"}, time.time(), total_base=entry_total_base)
        else:
             logging.warning(f"[{symbol}] Asset found in wallet but price unavailable. Please manage manually.")

    if not sellable_found and any(v > 0 for k, v in free_balances.items() if k not in base_currencies):
        logging.warning("No sellable assets found. Your wallet contains only 'dust' (amounts below exchange limits) or maybe adjust you pairs.txt file.")

    logging.info(f"Syncing positions from {exchange_id} API done.")



def show_balances(exchange):
    """
    Fetches and displays the real wallet balance in a formatted table.

    Includes asset amounts and their estimated value in EUR.

    Parameters
    ----------
    exchange : ExchangeInterface
        The exchange instance to fetch balances from.
    """
    console.print("\n[bold magenta]=== Real Wallet Balance (All Assets) ===[/]")
    balance = exchange.fetch_balances()
    if balance is None:
        console.print("[bold red]Error: Failed to fetch balances from exchange. Please check your connection and API keys.[/]")
        return

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
            format_amt(free),
            format_amt(used),
            format_amt(total),
            val_str
        )

    console.print(table)
    console.print(f"\n[bold yellow]Estimated Total Wallet Value: {total_eur_value:.2f} EUR[/]\n")

def plot_scan(df, symbol, strategy_name, aggr_name, results):
    """
    Generates and saves a matplotlib plot for scaning results.

    The plot shows price action, buy/sell signals, and key performance statistics.

    Parameters
    ----------
    df : pandas.DataFrame
        Dataframe containing OHLCV and signal data.
    symbol : str
        The trading pair symbol.
    strategy_name : str
        Name of the strategy tested.
    aggr_name : str
        Name of the aggressivity mode tested.
    results : dict
        Scan results including profit, win rate, and drawdown.
    """
    plt.figure(figsize=(12, 7))
    plt.plot(df['timestamp'], df['close'], label='Price', color='blue', alpha=0.6)

    # Plot buy signals
    buys = df[df['buy_signal']]
    plt.scatter(buys['timestamp'], buys['close'], marker='^', color='green', label='BUY Signal', s=100)

    # Plot sell signals
    sells = df[df['sell_signal']]
    plt.scatter(sells['timestamp'], sells['close'], marker='v', color='red', label='SELL Signal', s=100)

    plt.title(f"Scan: {symbol} | Strategy: {strategy_name} | Aggr: {aggr_name}")
    plt.xlabel("Time")
    plt.ylabel("Price")

    p_str = format_price(results['profit'])
    stats_text = f"Profit: {p_str} EUR\nWin Rate: {results['win_rate']:.1%}\nMax DD: {results['max_dd']:.1%}"
    plt.annotate(stats_text, xy=(0.02, 0.95), xycoords='axes fraction',
                 bbox=dict(boxstyle="round", fc="w", alpha=0.8), fontsize=10, verticalalignment='top')

    plt.legend()
    plt.grid(True, alpha=0.3)

    # Save plot
    filename = f"scan_{symbol.replace('/', '_')}_{strategy_name}.png"
    plt.savefig(filename)
    console.print(f"[bold green]Scan plot saved as {filename}[/]")
    plt.close()

def run_scan_logic(exchange, symbol, strategy, aggr_name, config, timeframe='1m', df_in=None, limit=500, engine=None, device=None, skip_mc=False, return_full_df=False, eval_candles=None):
    """
    Core logic for simulating a trading strategy over historical data.

    Calculates signals, simulates trades including fees, and computes performance
    metrics like total profit, win rate, and drawdown. Optionally applies
    Monte Carlo validation.

    Parameters
    ----------
    exchange : ExchangeInterface
        The exchange to fetch data from if `df_in` is None.
    symbol : str
        The trading pair symbol.
    strategy : str
        Strategy name to test.
    aggr_name : str
        Agressivity mode name.
    config : dict
        Bot configuration.
    timeframe : str, optional
        Timeframe for OHLCV data.
    df_in : pandas.DataFrame, optional
        Pre-loaded data. If None, data is fetched.
    limit : int, optional
        Number of candles to fetch if `df_in` is None.
    engine : TradingEngine, optional
        Engine for dynamic settings.
    device : torch.device, optional
        Computation device (CPU/GPU).
    skip_mc : bool, optional
        Whether to skip Monte Carlo validation.
    return_full_df : bool, optional
        Whether to return the equity curve in the results.
    eval_candles : int, optional
        Number of candles to use for the evaluation window.

    Returns
    -------
    dict or None
        Summary of scan results, or None if calculation fails.
    """
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
         base_df = get_signals(df_in.copy(), {"device": device if device is not None else torch.device("cpu")}, is_scan=True)
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
            df = get_signals(df, test_config, is_scan=True)
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
    sell_occurred = False
    sell_buy_sequence = False

    # We loop through the whole DF for indicators, but only execute trades in the eval window
    for i in range(len(df)):
        if i < start_idx:
            equity_curve.append(balance)
            continue

        row = df.iloc[i]
        price = row['close']

        # Sell logic
        if row['sell_signal']:
            sell_occurred = True
            if position:
                revenue = price * position['amount']
                fee = revenue * fee_rate
                revenue_net = revenue - fee

                profit = revenue_net - position['entry_cost']
                balance += revenue_net
                trades.append({'profit': profit})
                position = None

        # Buy logic
        raw_val = float(config.get('max_trade_percentage', 12.0))
        base_percentage = raw_val / 100.0 if raw_val >= 1.0 else raw_val
        trade_amount = balance * base_percentage
        if not position and row['buy_signal'] and balance >= trade_amount:
            if sell_occurred:
                sell_buy_sequence = True

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

    # Monte Carlo Validation removed from Scan as requested
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
        'sell_buy_sequence': sell_buy_sequence,
        'start_time': start_time_dt.strftime("%Y-%m-%d %H:%M"),
        'end_time': end_time_dt.strftime("%Y-%m-%d %H:%M"),
        'start_ts': start_time_dt.timestamp(),
        'prices': eval_df['close'].tolist(),
        'tech_state': tech_state,
        'equity_curve': equity_curve if return_full_df else []
    }

def run_scan_mode(exchange, config, args, engine=None, device=None):
    """
    Executes the bot in scan mode based on command-line arguments.

    Parameters
    ----------
    exchange : ExchangeInterface
        The exchange instance.
    config : dict
        Bot configuration.
    args : argparse.Namespace
        Parsed command-line arguments.
    engine : TradingEngine, optional
        The trading engine.
    device : torch.device, optional
        Computation device.
    """
    strategy = args.strategy
    aggr = args.aggr or config.get('force_agressivity_to_all_pairs', 'normal')
    timeframe = args.timeframe or config['pairs'].get(args.symbol, {}).get('timeframe', '1m')

    if strategy not in STRATEGIES:
        console.print(f"[bold red]Error: Strategy '{strategy}' not found.[/]")
        console.print(f"Available strategies: {', '.join(STRATEGIES)}")
        console.print("[dim]Please check for typos.[/]")
        return

    console.print(f"[bold blue]Running Scan for {args.symbol} | Strategy: {strategy} | Aggr: {aggr} | Timeframe: {timeframe}...[/]")
    results = run_scan_logic(exchange, args.symbol, strategy, aggr, config, timeframe=timeframe, engine=engine, device=device)

    if results:
        if results['trades_count'] > 0:
            plot_scan(results['df'], args.symbol, strategy, aggr, results)
        else:
            console.print("[yellow]No trades executed during scan. Plot not generated.[/]")

        console.print(f"\n[bold yellow]Scan Summary for {args.symbol}:[/]")
        console.print(f"Total Profit: {format_price(results['profit'])} EUR")
        console.print(f"Win Rate: {results['win_rate']:.1%}")
        console.print(f"Max Drawdown: {results['max_dd']:.1%}")
        console.print(f"Total Trades: {results['trades_count']}")
        if results.get('sell_buy_sequence'):
             console.print(f"[bold green]Signal Sequence Detected: SELL followed by BUY[/]")
    else:
        console.print(f"[red]Scan failed for {args.symbol} using {strategy} ({aggr}). Check symbol and aggr settings.[/]")

def run_optimization_test_for_symbol(symbol, config, timeframe, aggrs, strategies, df_in, engine=None, device=None, exclude_technique=None):
    """
    Scans historical data for success patterns using expanding time slices.

    Iterates through multiple strategies and aggressivity levels over expanding
    lookback windows to find profitable patterns or specific signal sequences
    (e.g., SELL followed by BUY).

    Parameters
    ----------
    symbol : str
        The trading pair symbol.
    config : dict
        Bot configuration.
    timeframe : str
        Timeframe for analysis.
    aggrs : list of str
        Agressivity levels to test.
    strategies : list of str
        Strategies to test.
    df_in : pandas.DataFrame
        Historical OHLCV data.
    engine : TradingEngine, optional
        The trading engine.
    device : torch.device, optional
        Computation device.
    exclude_technique : tuple, optional
        A (strategy, aggr) pair to exclude from discovery (e.g., for rotation).

    Returns
    -------
    symbol : str
        The symbol analyzed.
    unique_patterns : list of dict
        List of profitable patterns found, validated via Monte Carlo.
    """
    if df_in is None or len(df_in) < 120: return symbol, []

    from indicators import get_signals
    now_ts = time.time()

    # Filter out current technique if rotating
    if exclude_technique:
        ex_strat, ex_aggr = exclude_technique
    else:
        ex_strat, ex_aggr = None, None

    lookback = 120
    max_available = len(df_in)

    while lookback <= max_available:
        current_df = df_in.tail(lookback)
        patterns = []

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

                # 1. Calculate signals
                try:
                    full_df = get_signals(current_df.copy(), mode_settings, is_scan=True)
                except Exception:
                    continue

                # 2. Run scan
                res_full = run_scan_logic(None, symbol, strategy, aggr, config,
                                             timeframe=timeframe, df_in=full_df, engine=engine,
                                             device=device, skip_mc=True, return_full_df=True, eval_candles=len(full_df))

                if not res_full or not res_full.get('equity_curve'):
                    continue

                equity = res_full['equity_curve']

                # 3. Expanding Time Slices (Tenths)
                segment_profits = []
                segment_scores = []

                tenth = max(1, lookback // 10)
                for i in range(1, 11):
                    segment_len = i * tenth
                    start_idx = len(full_df) - segment_len
                    end_idx = len(full_df)

                    win_profit = equity[end_idx-1] - equity[start_idx]

                    # Recency Pondering
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

                avg_score = sum(segment_scores) / len(segment_scores)
                avg_profit = sum(segment_profits) / len(segment_profits)

                # Acceptance criteria: either profitable OR a clear SELL -> BUY sequence
                if avg_profit < 0.01 and not res_full.get('sell_buy_sequence'):
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

        if patterns:
            # Found profitable patterns, perform final steps and return
            # Sort by profit to get the "best performing" ones
            patterns.sort(key=lambda x: x['profit'], reverse=True)
            unique_patterns = []
            for p in patterns:
                if len(unique_patterns) >= 4: break
                # Monte Carlo validation removed from Discovery as requested
                unique_patterns.append(p)
            return symbol, unique_patterns

        # No patterns found, double lookback
        if lookback == max_available:
            break
        lookback *= 2
        if lookback > max_available:
            lookback = max_available

    return symbol, []

def run_optimization_test(exchange, config, args, status=None, data_manager=None, pattern_manager=None, engine=None, device=None):
    """
    Orchestrates the strategy discovery/optimization process for multiple symbols.

    Checks for cached results first, then parallelizes the analysis of historical
    data across all configured pairs to find optimal strategies.

    Parameters
    ----------
    exchange : ExchangeInterface
        The exchange instance.
    config : dict
        Bot configuration.
    args : argparse.Namespace
        Parsed command-line arguments.
    status : rich.status.Status, optional
        Rich status object for UI updates.
    data_manager : DataManager, optional
        Manager for persistent trade data.
    pattern_manager : PatternManager, optional
        Manager for historical success patterns.
    engine : TradingEngine, optional
        The trading engine.
    device : torch.device, optional
        Computation device.

    Returns
    -------
    dict
        A map of symbol to the best discovered pattern/strategy.
    """

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
            # Select top 2 distinct strategies from cache
            distinct_techniques = []
            seen_strats = set()
            sorted_patterns = sorted(cached_patterns, key=lambda x: x['profit'], reverse=True)
            for p in sorted_patterns:
                if p['strategy'] not in seen_strats:
                    p['is_cached'] = True
                    distinct_techniques.append(p)
                    seen_strats.add(p['strategy'])
                    if len(distinct_techniques) == 2:
                        break

            if not distinct_techniques:
                 distinct_techniques = [cached_patterns[0]]
                 distinct_techniques[0]['is_cached'] = True

            optimization_map[symbol] = distinct_techniques
            if data_manager:
                pattern_manager.set_patterns(symbol, cached_patterns)
            continue
        symbols_to_bench.append(symbol)

    if symbols_to_bench:
        msg = f"Scanning strategies for {len(symbols_to_bench)} symbol(s) using multi-processing..."
        if status: status.update(f"[bold blue]{msg}")
        else: console.print(f"[bold blue]{msg}")

        # Pre-fetch historical data for all symbols in the process
        symbol_data_map = {}

        # Date filtering logic
        # Durations (120 candles): 1m(2h), 3m(6h), 5m(10h), 15m(30h), 30m(60h)
        now_ts = time.time()
        since_map = {
            '1m': int((now_ts - 48 * 3600) * 1000),
            '3m': int((now_ts - 144 * 3600) * 1000),
            '5m': int((now_ts - 240 * 3600) * 1000),
            '15m': int((now_ts - 720 * 3600) * 1000),
            '30m': int((now_ts - 1440 * 3600) * 1000)
        }
        if args.since:
             try: since_ts = int(datetime.strptime(args.since, "%Y-%m-%d %H:%M").timestamp() * 1000)
             except Exception: console.print(f"[red]Invalid --since format. Use YYYY-MM-DD HH:MM[/]")

        for i, symbol in enumerate(symbols_to_bench):
            all_ohlcv = []
            target_limit = 2000
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

                    # Enforce timeframe-specific limits (allowing up to ~2880 candles)
                    duration_hours = {
                        '1m': 48, '3m': 144, '5m': 240, '15m': 720, '30m': 1440
                    }.get(timeframe, 48)
                    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=duration_hours)
                    df = df[df['timestamp'] >= cutoff]

                    symbol_data_map[symbol] = df
                    with ohlcv_cache_lock:
                        ohlcv_cache[cache_key] = df
                    if not status: console.print(f"[dim][{symbol}] Successfully fetched {len(df)} candles.[/]")
                else:
                    if not status: console.print(f"[yellow]No OHLCV returned for {symbol} ({timeframe}) during pre-fetch.[/]")
            except Exception as e:
                if not status: console.print(f"[red]Failed to fetch {symbol} for test: {e}[/]")

        def handle_bench_shutdown(sig, frame):
             shutdown_event.set()
             executor.shutdown(wait=False, cancel_futures=True)
             sys.exit(0)

        if status: status.update('[bold yellow]Analyzing patterns and discovering strategies...')
        # On CPU with oneDNN, ThreadPoolExecutor might be more efficient for many small torch tasks
        # than ProcessPoolExecutor which has pickling overhead.
        executor_class = concurrent.futures.ProcessPoolExecutor
        with executor_class() as executor:
            # Register signal handler during optimization
            original_handler = signal.signal(signal.SIGINT, handle_bench_shutdown)
            try:
                futures = []
                for sym in symbol_data_map:
                    # Determine if we should exclude current technique (for re-scaning rotation)
                    exclude = None
                    if sym in bot_state:
                         exclude = (bot_state[sym].get('strategy'), bot_state[sym].get('aggr'))

                    futures.append(executor.submit(
                        run_optimization_test_for_symbol, sym, config, config['pairs'][sym].get('timeframe', '1m'),
                        aggrs, strategies, symbol_data_map[sym], engine, device, exclude_technique=exclude
                    ))
                for future in concurrent.futures.as_completed(futures):
                    if shutdown_event.is_set(): break
                    sym, patterns = future.result()
                    if patterns:
                        # Select top 2 distinct strategies based on profit
                        distinct_techniques = []
                        seen_strats = set()
                        # Sort patterns by profit to get the "best performing"
                        sorted_patterns = sorted(patterns, key=lambda x: x['profit'], reverse=True)
                        for p in sorted_patterns:
                            if p['strategy'] not in seen_strats:
                                distinct_techniques.append(p)
                                seen_strats.add(p['strategy'])
                                if len(distinct_techniques) == 2:
                                    break

                        if not distinct_techniques:
                             distinct_techniques = [patterns[0]]

                        best_tech = distinct_techniques[0]
                        best_per_symbol[sym] = best_tech

                        # Store patterns in DataManager for real-time matching
                        if data_manager:
                             pattern_manager.set_patterns(sym, patterns)

                        # Update current techniques in bot_state immediately
                        if sym in bot_state:
                             with bot_lock:
                                  bot_state[sym]['strategy'] = best_tech['strategy']
                                  bot_state[sym]['aggr'] = best_tech['aggr']
                                  bot_state[sym]['strategies'] = [t['strategy'] for t in distinct_techniques]

                        period_str = f" [dim](From {best_tech.get('start_time')} to {best_tech.get('end_time')})[/]"
                        # Always save patterns to cache
                        timeframe = config['pairs'][sym].get('timeframe', '1m')
                        cache_mgr.set(sym, timeframe, patterns)

                        msg_target = status.console if status else console
                        optimization_map[sym] = distinct_techniques # Store the list of top techniques

                        tech_desc = " + ".join([f"{t['strategy']} ({t['aggr']})" for t in distinct_techniques])
                        msg_target.print(f"\n[bold green]🏆 DISCOVERY FOR {sym}:[/] [bold]{tech_desc}[/] | Best Profit: {format_price(best_tech['profit'])} EUR{period_str}")

                        # Use a generic 'total' score for recommendations
                        if best_tech['profit'] > best_overall['total']['profit']:
                             best_overall['total'] = {'profit': best_tech['profit'], 'params': (best_tech['strategy'], best_tech['aggr'], sym)}
            finally:
                signal.signal(signal.SIGINT, original_handler)

    # If we are in optimization mode for live/sim, return the map
    if status: status.update('[bold green]Discovery complete.')
    if best_per_symbol:
        time.sleep(3)

    if args.mode in ['live', 'simulation']:
        return optimization_map

    console.print("\n[bold magenta]=== DISCOVERY RECOMMENDATIONS ===[/]")
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
        console.print("[yellow]No suitable strategies found in the scanned historical data.[/]")
    else:
        # Final check: if some symbols returned nothing, let the user know
        for sym in symbols_to_bench:
            if sym not in best_per_symbol:
                 console.print(f"[dim][{sym}] No suitable patterns found in current scan.[/]")

if __name__ == "__main__":
    main()

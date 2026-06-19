# Cryptocurrencies multiplatform trading bot - Dashboard and UI
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import time
import logging
import threading
import platform
import sys
import random
from datetime import datetime, timedelta

from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel
from rich.console import Console
from rich.text import Text
from rich.columns import Columns

import readchar

from utils import format_price, format_amount

class DashboardUI:
    def __init__(self, console):
        self.console = console
        self.pairs_scroll_offset = 0
        self.logs_scroll_offset = 0
        self.focused_panel = "pairs"
        self.all_logs = []
        self.status_scroll_index = 0
        self.expert_mode = False
        self.show_help = False
        self.marquee_enabled = True
        self.selected_pair_index = 0
        self.selected_log_index = 0
        self.show_candles_for_pair = None

        # Marquee Timing Control
        self.last_marquee_update = 0
        self.bot_start_time = time.time()
        self.pairs_marquee_dir = 1
        self.logs_marquee_dir = 1
        self.status_marquee_dir = 1
        self.pairs_pause_until = 0
        self.logs_pause_until = 0
        self.status_pause_until = 0

    def make_dashboard(self, global_mode, config, bot_state, signal_arrival_times, bot_lock):
        now = datetime.now()
        now_ts = time.time()

        with bot_lock:
            # Moderate speed marquee (e.g., 10 steps per second)
            should_step = False
            if self.marquee_enabled and (now_ts - self.last_marquee_update >= 0.1):
                should_step = True
                self.last_marquee_update = now_ts

            layout = Layout()
            layout.split_column(
                Layout(name="header", size=3),
                Layout(name="body", ratio=1),
                Layout(name="footer", size=3)
            )
            layout["body"].split_row(
                Layout(name="pairs", ratio=4),
                Layout(name="logs", ratio=1)
            )

            # Header: Clock, Mode and Marquee Status
            uptime = timedelta(seconds=int(now_ts - self.bot_start_time))

            header_left = Text()
            header_left.append("Cryptocurrencies multiplatform trading bot", style="bold bright_white")
            header_left.append(f" | {global_mode.upper()} MODE", style="bold green" if global_mode == "live" else "bold yellow")

            header_right = Text()
            header_right.append(now.strftime('%Y-%m-%d %H:%M:%S'), style="cyan")
            header_right.append(f" | Uptime: {uptime}", style="dim white")
            header_right.append(" | ")
            header_right.append("[H] Hide Help" if self.show_help else "[H] Help", style="bold yellow" if self.show_help else "dim")

            header_table = Table.grid(expand=True, padding=0)
            header_table.add_column(justify="left")
            header_table.add_column(justify="right")
            header_table.add_row(header_left, header_right)
            layout["header"].update(Panel(header_table, border_style="bright_blue", padding=(0, 1)))

            # Footer: Quick actions and Status Bar
            footer_left = Text("[B] Buy  [S] Sell  [X] Expert  [TAB] Switch Panel  [M] Marquee  [Q] Quit")

            instr = config.get('opt_level', 'Unknown')
            footer_right = Text()
            footer_right.append("Instr: ")
            footer_right.append(instr, style="bold blue")
            footer_right.append(", Accel: ")
            footer_right.append(f"{config.get('gpu_accel', 'CPU')}", style="bold green")
            footer_right.append(f" | Risk: {config.get('global_risk_multiplier', 1.2)}x")
            footer_right.append(f" | Hurdle: {config.get('profit_thresholds', {}).get('mc_validation_hurdle', 0.0015)}")

            footer_table = Table.grid(expand=True, padding=0)
            footer_table.add_column(justify="left")
            footer_table.add_column(justify="right")
            footer_table.add_row(footer_left, footer_right)

            layout["footer"].update(Panel(footer_table, border_style="bright_blue", padding=(0, 1)))

            # Pairs Panel
            table = Table(expand=True, box=None, padding=(0, 1))
            if self.expert_mode:
                table.add_column("Pair", style="cyan", no_wrap=True)
                table.add_column("EMA F/S", style="green", no_wrap=True)
                table.add_column("MACD", style="blue", no_wrap=True)
                table.add_column("RSI", style="yellow", no_wrap=True)
                table.add_column("Vol/ADX", style="dim white", no_wrap=True)
                table.add_column("Flags", style="bold white", no_wrap=True)
                table.add_column("Scr", style="bold white", no_wrap=True)
            else:
                table.add_column("Pair", style="cyan", no_wrap=True)
                table.add_column("Price", style="magenta", no_wrap=True)
                table.add_column("Amt", style="cyan", no_wrap=True)
                table.add_column("Entry", style="magenta", no_wrap=True)
                table.add_column("Fee", style="red", no_wrap=True, width=6)
                table.add_column("Bench", style="bold green", no_wrap=True)
                table.add_column("Tendency", style="bold white", no_wrap=True)
                table.add_column("Last Order", style="bold", no_wrap=True)
                table.add_column("Signal", style="bold", no_wrap=True)
                table.add_column("Aggr", style="white", no_wrap=True)
                table.add_column("Strategy", style="bold cyan", no_wrap=True, width=10)

            sorted_symbols = sorted([s for s in bot_state.keys() if not s.startswith("_")])

            if self.show_candles_for_pair:
                symbol = self.show_candles_for_pair
                data = bot_state.get(symbol, {})
                candles_text = Text()
                candles_text.append(f"Last 20 candles for {symbol}:\n\n", style="bold cyan")
                if 'last_20_candles' in data:
                    prices = data['last_20_candles']['prices']
                    volumes = data['last_20_candles']['volumes']
                    min_p, max_p = min(prices), max(prices)
                    diff = max_p - min_p if max_p > min_p else 1.0
                    chart_height = 8
                    for h in reversed(range(chart_height)):
                        for p in prices:
                            norm_p = (p - min_p) / diff
                            scaled_p = norm_p * chart_height
                            if scaled_p >= h: candles_text.append("█ ", style="red")
                            else: candles_text.append("  ")
                        candles_text.append("\n")

                    if volumes:
                        min_v, max_v = min(volumes), max(volumes)
                        diff_v = max_v - min_v if max_v > min_v else 1.0
                        vol_height = 4
                        candles_text.append("\n")
                        for vh in reversed(range(vol_height)):
                            for v in volumes:
                                norm_v = (v - min_v) / diff_v
                                scaled_v = norm_v * vol_height
                                if scaled_v >= vh: candles_text.append("█ ", style="blue")
                                else: candles_text.append("  ")
                            candles_text.append("\n")
                    candles_text.append("\nPress any key to return...", style="dim")
                else:
                    candles_text.append("Candle data not available yet.\n", style="dim")
                pairs_panel = Panel(candles_text, title=f"[bold cyan]{symbol} Candles[/]", border_style="bold cyan")
            else:
                pairs_height = self.console.height - 9
                if pairs_height < 3: pairs_height = 3

                if len(sorted_symbols) > pairs_height and should_step and self.marquee_enabled:
                     if now_ts > self.pairs_pause_until:
                          self.pairs_scroll_offset = (self.pairs_scroll_offset + 1) % len(sorted_symbols)

                if len(sorted_symbols) <= pairs_height:
                    visible_symbols_with_idx = [(i, s) for i, s in enumerate(sorted_symbols)]
                else:
                    visible_symbols_with_idx = []
                    for i in range(pairs_height):
                        idx = (self.pairs_scroll_offset + i) % len(sorted_symbols)
                        visible_symbols_with_idx.append((idx, sorted_symbols[idx]))

                for idx, symbol in visible_symbols_with_idx:
                    data = bot_state[symbol]
                    is_selected = idx == self.selected_pair_index
                    positions = data.get('positions', [])
                    has_position = len(positions) > 0
                    current_signal = "Waiting"
                    buy_count = data.get('consecutive_buys', 0)
                    sell_count = data.get('consecutive_sells', 0)

                    if buy_count > 0: current_signal = f"{buy_count} Buy"
                    elif sell_count > 0: current_signal = f"{sell_count} Sell"

                    last_order = data.get('last_action', 'Waiting')
                    if last_order == "WAITING": last_order = "Waiting"

                    is_new_signal = (symbol in signal_arrival_times) and (now_ts - signal_arrival_times[symbol] < 20)
                    if is_new_signal:
                        signal_style = "bold bright_green" if "Buy" in current_signal else "bold bright_red" if "Sell" in current_signal else "white"
                    else:
                        signal_style = "bold green" if "Buy" in current_signal else "bold red" if "Sell" in current_signal else "white"
                    last_order_style = "bold green" if last_order == "BUY" else "bold red" if last_order == "SELL" else "white"

                    amt_str, entry_str, fee_str = "-", "-", "-"
                    if has_position:
                        p = positions[-1]
                        amt_str = f"{format_amount(p['amount'])}"
                        if len(positions) > 1: amt_str = f"({len(positions)}) {amt_str}"
                        entry_str = format_price(p['entry_price'])
                        fee_str = f"{format_amount(p.get('entry_fee', 0))}"

                    tendency = data.get('tendency', 'N/A')
                    tend_style = "bold green" if tendency == "Bullish" else "bold red" if tendency == "Bearish" else "bold yellow" if tendency == "Range" else "white"

                    row_style = "bold black on yellow" if is_selected else ""

                    if self.expert_mode:
                        flags = []
                        if data.get('whale_active'): flags.append("WHL")
                        if data.get('is_mean_rev'): flags.append("MRV")
                        else: flags.append("TRD")
                        flags_str = ",".join(flags)

                        def fmt_sig(v, sig):
                            try:
                                return f"{float(v):.{sig}g}"
                            except:
                                return str(v)

                        row_vals = [
                            symbol,
                            f"{fmt_sig(data.get('ema_f', 0), 7)}/{format_price(data.get('ema_s', 0))}",
                            f"{fmt_sig(data.get('macd_hist', 0), 8)}",
                            f"{fmt_sig(data.get('rsi', 0), 4)}",
                            f"{fmt_sig(data.get('volatility', 0), 11)}/{float(data.get('adx', 0)):.1f}",
                            f"[{'bold cyan' if 'WHL' in flags_str else 'dim white'}]{flags_str}[/]",
                            f"{data.get('score', 0)}"
                        ]
                    else:
                        row_vals = [
                            symbol, format_price(data.get('price', 0)), amt_str, entry_str, fee_str,
                            f"{data.get('bench_profit', 0):.2f}%",
                            Text(tendency, style=tend_style),
                            Text(last_order, style=last_order_style),
                            Text(current_signal, style=signal_style),
                            str(data.get('aggr', 'N/A'))[:6],
                            data.get('strategy', 'N/A')
                        ]
                    table.add_row(*row_vals, style=row_style)

                pairs_panel = Panel(table, title="[bold cyan]Trading Pairs[/]", border_style="bright_blue" if self.focused_panel == "pairs" else "dim white")

            layout["pairs"].update(pairs_panel)

            # Logs Panel
            log_height = self.console.height - 9
            if log_height < 3: log_height = 3

            # We estimate that each log takes about 2 visual lines on average due to wrapping
            estimated_visual_lines_per_log = 2
            visible_count = log_height // estimated_visual_lines_per_log
            if visible_count < 1: visible_count = 1

            # Scrolling and following logic
            if len(self.all_logs) <= visible_count:
                self.logs_scroll_offset = 0
                visible_logs_with_idx = [(i, log) for i, log in enumerate(self.all_logs)]
            else:
                if now_ts > self.logs_pause_until:
                     # Auto-follow mode: keep the cursor on the latest and ensure it's visible
                     self.selected_log_index = len(self.all_logs) - 1
                     self.logs_scroll_offset = len(self.all_logs) - visible_count
                else:
                     # Manual mode: ensure the selected index is visible
                     if self.selected_log_index < self.logs_scroll_offset:
                         self.logs_scroll_offset = self.selected_log_index
                     elif self.selected_log_index >= self.logs_scroll_offset + visible_count:
                         self.logs_scroll_offset = self.selected_log_index - visible_count + 1

                visible_logs_with_idx = []
                for i in range(visible_count):
                    idx = (self.logs_scroll_offset + i) % len(self.all_logs)
                    visible_logs_with_idx.append((idx, self.all_logs[idx]))

            log_table = Table(expand=True, box=None, padding=0, show_header=False)
            log_table.add_column("Message")

            for idx, log_entry in visible_logs_with_idx:
                is_selected = idx == self.selected_log_index
                row_style = "bold black on yellow" if is_selected else ""
                expiry_style = "dim" if log_entry.get('expiry') and now > log_entry['expiry'] else ""

                try:
                    msg_text = Text.from_markup(log_entry['msg'], style=expiry_style)
                except Exception:
                    msg_text = Text(log_entry['msg'], style=expiry_style)

                log_table.add_row(msg_text, style=row_style)
                # Reduced spacing: instead of \n\n (which is 2 newlines), we use 1 newline to make it 2 lines total
                log_table.add_row("", style=row_style)

            layout["logs"].update(Panel(log_table, title="[bold cyan]System Logs[/]", border_style="bright_blue" if self.focused_panel == "logs" else "dim white"))

            # Overlay Help
            if self.show_help:
                 help_text = Text("""
                 [UP/DOWN]   Scroll through trading pairs
                 [TAB]       Switch focus between Pairs and Logs
                 [ENTER]     Show 20-candle chart for selected pair
                 [B]         Manual Market BUY for selected pair
                 [S]         Manual Market SELL for selected pair
                 [X]         Toggle Expert Mode (Indicators vs Positions)
                 [M]         Toggle Marquee (Auto-scrolling)
                 [H]         Toggle this Help screen
                 [Q]         Graceful Shutdown
                 """, style="bold white")
                 return Panel(help_text, title="[bold yellow]Keyboard Shortcuts[/]", border_style="bold yellow")

            return layout

    def input_thread_func(self, exchange, data_manager, engine, config, bot_state, bot_lock, shutdown_event, execute_buy_func, execute_sell_func, play_sound_func):
        while not shutdown_event.is_set():
            try:
                key = readchar.readkey()

                if self.show_candles_for_pair:
                    self.show_candles_for_pair = None
                    continue

                sorted_symbols = sorted([s for s in bot_state.keys() if not s.startswith("_")])

                if key == readchar.key.TAB:
                    self.focused_panel = "logs" if self.focused_panel == "pairs" else "pairs"
                elif key == readchar.key.UP:
                    if self.focused_panel == "pairs":
                        self.selected_pair_index = (self.selected_pair_index - 1) % len(sorted_symbols) if sorted_symbols else 0
                        self.pairs_scroll_offset = self.selected_pair_index
                        self.pairs_pause_until = time.time() + 5
                    else:
                        if self.all_logs:
                            self.selected_log_index = max(0, self.selected_log_index - 1)
                        self.logs_pause_until = time.time() + 10 # Increase pause for manual inspection
                elif key == readchar.key.DOWN:
                    if self.focused_panel == "pairs":
                        self.selected_pair_index = (self.selected_pair_index + 1) % len(sorted_symbols) if sorted_symbols else 0
                        self.pairs_scroll_offset = self.selected_pair_index
                        self.pairs_pause_until = time.time() + 5
                    else:
                        if self.all_logs:
                            self.selected_log_index = min(len(self.all_logs) - 1, self.selected_log_index + 1)
                        self.logs_pause_until = time.time() + 10
                elif key == readchar.key.ENTER:
                    if self.focused_panel == "pairs" and sorted_symbols:
                        self.show_candles_for_pair = sorted_symbols[self.selected_pair_index]
                elif key.lower() == 'b':
                    # Manual Buy
                    if self.focused_panel == "pairs" and sorted_symbols:
                        symbol = sorted_symbols[self.selected_pair_index]
                        data = bot_state[symbol]
                        if not data.get('position'):
                            def manual_buy_task():
                                if execute_buy_func(exchange, data_manager, engine, symbol, data, config):
                                    with bot_lock:
                                        data['last_action'] = 'BUY'
                                        data['position'] = data_manager.get_position(symbol)
                                    play_sound_func("buy", config)
                            threading.Thread(target=manual_buy_task, daemon=True).start()
                elif key.lower() == 's':
                    # Manual Sell
                    if self.focused_panel == "pairs" and sorted_symbols:
                        symbol = sorted_symbols[self.selected_pair_index]
                        data = bot_state[symbol]
                        if data.get('positions'):
                            def manual_sell_task():
                                if execute_sell_func(exchange, data_manager, engine, symbol, data, config, position_idx=0):
                                    with bot_lock:
                                        data['last_action'] = 'SELL'
                                        data['positions'] = data_manager.get_positions(symbol)
                                        data['position'] = data_manager.get_position(symbol)
                                    play_sound_func("sell", config)
                            threading.Thread(target=manual_sell_task, daemon=True).start()
                elif key.lower() == 'x':
                    self.expert_mode = not self.expert_mode
                elif key.lower() == 'm':
                    self.marquee_enabled = not self.marquee_enabled
                elif key.lower() == 'h':
                    self.show_help = not self.show_help
                elif key.lower() == 'q' or key == readchar.key.CTRL_C:
                    shutdown_event.set()
                    break
            except (KeyboardInterrupt, EOFError):
                shutdown_event.set()
                break
            except Exception: pass

class DashboardHandler(logging.Handler):
    def __init__(self, ui, bot_lock, duration=5):
        super().__init__()
        self.ui = ui
        self.bot_lock = bot_lock
        self.duration = duration
        self.trigger_cache = {} # (trigger, symbol_tag) -> log_entry_ref

    def emit(self, record):
        msg = self.format(record)
        expiry = datetime.now() + timedelta(seconds=self.duration)

        with self.bot_lock:
            # Connection pool log filtering
            pool_msg = "Connection pool is full, discarding connection: api.binance.com"
            if pool_msg in msg:
                 if pool_msg in self.trigger_cache:
                      log = self.trigger_cache[pool_msg]
                      log['msg'] = msg
                      log['expiry'] = expiry
                      return
                 for log in self.ui.all_logs:
                      if pool_msg in log['msg']:
                           self.trigger_cache[pool_msg] = log
                           log['msg'] = msg
                           log['expiry'] = expiry
                           return

            # Simulation init replacement
            if "Simulation initialization complete" in msg or "Initialization of the simulation positions completed" in msg:
                 replacement = "Initialization of the simulation positions completed."
                 search_key = "Initializing Simulation positions"
                 if search_key in self.trigger_cache:
                      log = self.trigger_cache[search_key]
                      log['msg'] = replacement
                      log['expiry'] = expiry
                      return
                 for log in self.ui.all_logs:
                      if search_key in log['msg']:
                           self.trigger_cache[search_key] = log
                           log['msg'] = replacement
                           log['expiry'] = expiry
                           return

            # Deduplication for specific log types
            dedup_triggers = ["Profitability check failed", "Stop-loss triggered", "SELL signal received at non-profitable price", "Benchmarking all strategies"]
            matching_trigger = next((t for t in dedup_triggers if t in msg), None)

            if matching_trigger:
                 symbol_tag = msg.split(']')[0] + ']' if ']' in msg else ""
                 cache_key = (matching_trigger, symbol_tag)
                 if cache_key in self.trigger_cache:
                      log = self.trigger_cache[cache_key]
                      log['msg'] = msg
                      log['expiry'] = expiry
                      return
                 for log in self.ui.all_logs:
                      if matching_trigger in log['msg'] and symbol_tag in log['msg']:
                           self.trigger_cache[cache_key] = log
                           log['msg'] = msg
                           log['expiry'] = expiry
                           return

            new_log = {'msg': msg, 'expiry': expiry}
            self.ui.all_logs.append(new_log)
            if matching_trigger:
                 self.trigger_cache[(matching_trigger, symbol_tag)] = new_log

            if len(self.ui.all_logs) > 500:
                popped = self.ui.all_logs.pop(0)
                self.trigger_cache = {k: v for k, v in self.trigger_cache.items() if v is not popped}
                if self.ui.selected_log_index > 0:
                    self.ui.selected_log_index -= 1

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
        self.show_candles_for_pair = None
        self.sell_proposal_pair = None
        self.sell_proposal_profit = 0
        self.sell_proposal_time = 0
        self.last_sell_proposal_check = 0

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
                Layout(name="pairs", ratio=2),
                Layout(name="logs", ratio=1)
            )

            # Header: Clock, Mode and Marquee Status
            header_text = Text()
            header_text.append(f" Cryptocurrencies multiplatform", style="bold bright_white")
            header_text.append(f" | {global_mode.upper()} MODE", style="bold green" if global_mode == "live" else "bold yellow")
            header_text.append(f" | {now.strftime('%Y-%m-%d %H:%M:%S')}", style="cyan")

            uptime = timedelta(seconds=int(now_ts - self.bot_start_time))
            header_text.append(f" | Uptime: {uptime}", style="dim white")

            if self.show_help:
                header_text.append(" | [H] Hide Help", style="bold yellow")
            else:
                header_text.append(" | [H] Help", style="dim")

            layout["header"].update(Panel(header_text, border_style="bright_blue"))

            # Footer: Quick actions and Status Bar
            footer_cols = []
            footer_cols.append("[B] Buy  [S] Sell  [X] Expert  [TAB] Switch Panel  [M] Marquee  [Q] Quit")

            status_items = [
                 f"Accel: {config.get('gpu_accel', 'CPU')}",
                 f"Risk: {config.get('global_risk_multiplier', 1.2)}x",
                 f"Hurdle: {config.get('profit_thresholds', {}).get('mc_validation_hurdle', 0.0015)}"
            ]

            # Marquee for status items
            if should_step and len(status_items) > 3:
                 self.status_scroll_index = (self.status_scroll_index + 1) % len(status_items)

            visible_status = status_items[self.status_scroll_index:self.status_scroll_index+3]
            footer_cols.append(" | ".join(visible_status))

            layout["footer"].update(Panel(Columns(footer_cols, expand=True), border_style="bright_blue"))

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

            if self.sell_proposal_pair and (now_ts - self.sell_proposal_time < 61):
                symbol = self.sell_proposal_pair
                data = bot_state.get(symbol, {})
                candles_text = Text()
                time_left = max(0, int(61 - (now_ts - self.sell_proposal_time)))
                candles_text.append(f"PROPOSAL: SELL {symbol} for {format_price(self.sell_proposal_profit)} profit?\n", style="bold yellow")
                candles_text.append(f"Confirm sell? [Y/n] (Auto-exec in {time_left}s)\n\n", style="dim")
                if 'last_20_candles' in data:
                    prices = data['last_20_candles']['prices']
                    volumes = data['last_20_candles']['volumes']
                    min_p, max_p = min(prices), max(prices)
                    diff = max_p - min_p if max_p > min_p else 1.0
                    chart_height = 5
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
                        vol_height = 2
                        candles_text.append("\n")
                        for vh in reversed(range(vol_height)):
                            for v in volumes:
                                norm_v = (v - min_v) / diff_v
                                scaled_v = norm_v * vol_height
                                if scaled_v >= vh: candles_text.append("█ ", style="blue")
                                else: candles_text.append("  ")
                            candles_text.append("\n")
                pairs_panel = Panel(candles_text, title="[bold red]SELL PROPOSAL[/]", border_style="bold red")
            elif self.show_candles_for_pair:
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
                pairs_height = self.console.height - 8
                if pairs_height < 3: pairs_height = 3
                max_pairs_offset = max(0, len(sorted_symbols) - pairs_height)

                if max_pairs_offset > 0 and should_step:
                     if now_ts > self.pairs_pause_until:
                          if self.pairs_marquee_dir == 1:
                               if self.pairs_scroll_offset < max_pairs_offset:
                                   self.pairs_scroll_offset += 1
                               if self.pairs_scroll_offset >= max_pairs_offset:
                                   self.pairs_marquee_dir = -1
                                   self.pairs_pause_until = now_ts + 2
                          else:
                               if self.pairs_scroll_offset > 0:
                                   self.pairs_scroll_offset -= 1
                               else:
                                   self.pairs_marquee_dir = 1
                                   self.pairs_pause_until = now_ts + 2

                self.pairs_scroll_offset = max(0, min(self.pairs_scroll_offset, max_pairs_offset))
                visible_symbols = sorted_symbols[self.pairs_scroll_offset : self.pairs_scroll_offset + pairs_height]
                for i, symbol in enumerate(visible_symbols):
                    data = bot_state[symbol]
                    is_selected = (self.pairs_scroll_offset + i) == self.selected_pair_index
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

                        row_vals = [
                            symbol,
                            f"{format_price(data.get('ema_f', 0))}/{format_price(data.get('ema_s', 0))}",
                            f"{data.get('macd_hist', 0):.4e}" if abs(data.get('macd_hist', 0)) < 0.001 else f"{data.get('macd_hist', 0)}",
                            f"{data.get('rsi', 0)}",
                            f"{data.get('volatility', 0)}/{float(data.get('adx', 0)):.1f}",
                            f"[{'bold cyan' if 'WHL' in flags_str else 'dim white'}]{flags_str}[/]",
                            f"{data.get('score', 0)}",
                            data.get('aggr', 'N/A'),
                            (lambda d: d['all_matching_strategies'][int(now_ts % len(d['all_matching_strategies']))] if 'all_matching_strategies' in d and d['all_matching_strategies'] else d.get('strategy', 'N/A'))(data)
                        ]
                    else:
                        row_vals = [
                            symbol, format_price(data.get('price', 0)), amt_str, entry_str, fee_str,
                            f"{data.get('bench_profit', 0):.2f}%",
                            Text(tendency, style=tend_style),
                            Text(last_order, style=last_order_style),
                            Text(current_signal, style=signal_style),
                            data.get('aggr', 'N/A'),
                            data.get('strategy', 'N/A')
                        ]
                    table.add_row(*row_vals, style=row_style)

                pairs_panel = Panel(table, title="[bold cyan]Trading Pairs[/]", border_style="bright_blue" if self.focused_panel == "pairs" else "dim white")

            layout["pairs"].update(pairs_panel)

            # Logs Panel
            log_height = self.console.height - 4
            if log_height < 3: log_height = 3
            max_logs_offset = max(0, len(self.all_logs) - log_height)

            if max_logs_offset > 0 and should_step:
                 if now_ts > self.logs_pause_until:
                      if self.logs_marquee_dir == 1:
                           if self.logs_scroll_offset < max_logs_offset:
                                self.logs_scroll_offset += 1
                           if self.logs_scroll_offset >= max_logs_offset:
                                self.logs_marquee_dir = -1
                                self.logs_pause_until = now_ts + 5
                      else:
                           if self.logs_scroll_offset > 0:
                                self.logs_scroll_offset -= 1
                           else:
                                self.logs_marquee_dir = 1
                                self.logs_pause_until = now_ts + 5

            self.logs_scroll_offset = max(0, min(self.logs_scroll_offset, max_logs_offset))

            log_text = Text()
            start = max(0, len(self.all_logs) - log_height - self.logs_scroll_offset)
            end = max(0, len(self.all_logs) - self.logs_scroll_offset)
            for log_entry in self.all_logs[start:end]:
                style = "dim" if log_entry.get('expiry') and now > log_entry['expiry'] else ""
                log_text.append(log_entry['msg'] + "\n", style=style)

            layout["logs"].update(Panel(log_text, title="[bold cyan]System Logs[/]", border_style="bright_blue" if self.focused_panel == "logs" else "dim white"))

            # Overlay Help
            if self.show_help:
                 help_text = Text("""
                 [UP/DOWN]   Scroll through pairs or logs
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

                # Handle sell proposal
                if self.sell_proposal_pair and (time.time() - self.sell_proposal_time < 61):
                    if key.lower() == 'y' or key == readchar.key.ENTER:
                        symbol = self.sell_proposal_pair
                        data = bot_state.get(symbol, {})
                        if execute_sell_func(exchange, data_manager, engine, symbol, data, config, position_idx=0):
                             with bot_lock:
                                 data['last_action'] = 'SELL'
                                 data['positions'] = data_manager.get_positions(symbol)
                                 data['position'] = data_manager.get_position(symbol)
                             play_sound_func("sell", config)
                        with bot_lock:
                             self.sell_proposal_pair = None
                             self.sell_proposal_time = 0
                             self.sell_proposal_profit = 0
                        continue
                    elif key.lower() == 'n':
                        with bot_lock:
                             self.sell_proposal_pair = None
                             self.sell_proposal_time = 0
                             self.sell_proposal_profit = 0
                        continue
                else:
                    if self.sell_proposal_pair:
                        with bot_lock:
                             self.sell_proposal_pair = None
                             self.sell_proposal_time = 0
                             self.sell_proposal_profit = 0

                if self.show_candles_for_pair:
                    self.show_candles_for_pair = None
                    continue

                sorted_symbols = sorted([s for s in bot_state.keys() if not s.startswith("_")])

                if key == readchar.key.TAB:
                    self.focused_panel = "logs" if self.focused_panel == "pairs" else "pairs"
                elif key == readchar.key.UP:
                    if self.focused_panel == "pairs":
                        self.selected_pair_index = max(0, self.selected_pair_index - 1)
                        if self.selected_pair_index < self.pairs_scroll_offset:
                            self.pairs_scroll_offset = self.selected_pair_index
                        self.pairs_pause_until = time.time() + 5
                    else:
                        self.logs_scroll_offset = min(500, self.logs_scroll_offset + 1)
                        self.logs_pause_until = time.time() + 5
                elif key == readchar.key.DOWN:
                    if self.focused_panel == "pairs":
                        self.selected_pair_index = min(len(sorted_symbols) - 1, self.selected_pair_index + 1)
                        pairs_height = self.console.height - 8
                        if self.selected_pair_index >= self.pairs_scroll_offset + pairs_height:
                            self.pairs_scroll_offset = self.selected_pair_index - pairs_height + 1
                        self.pairs_pause_until = time.time() + 5
                    else:
                        self.logs_scroll_offset = max(0, self.logs_scroll_offset - 1)
                        self.logs_pause_until = time.time() + 5
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

    def emit(self, record):
        msg = self.format(record)
        timestamp = datetime.now().strftime("%H:%M:%S")
        expiry = datetime.now() + timedelta(seconds=self.duration)

        with self.bot_lock:
            # Connection pool log filtering
            pool_msg = "Connection pool is full, discarding connection: api.binance.com"
            if pool_msg in msg:
                 for log in self.ui.all_logs:
                      if pool_msg in log['msg']:
                           log['msg'] = f"[{timestamp}] {msg}"
                           log['expiry'] = expiry
                           return

            # Simulation init replacement
            if "Simulation initialization complete" in msg or "Initialization of the simulation positions completed" in msg:
                 replacement = "Initialization of the simulation positions completed."
                 for log in self.ui.all_logs:
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
                 for log in self.ui.all_logs:
                      if matching_trigger in log['msg'] and symbol_tag in log['msg']:
                           log['msg'] = f"[{timestamp}] {msg}"
                           log['expiry'] = expiry
                           return

            self.ui.all_logs.append({'msg': f"[{timestamp}] {msg}", 'expiry': expiry})
            if len(self.ui.all_logs) > 500:
                self.ui.all_logs.pop(0)

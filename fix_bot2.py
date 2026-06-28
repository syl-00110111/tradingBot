def fix_dashboard():
    with open('bot2.py', 'r') as f:
        content = f.read()

    start_marker = "def make_dashboard(mode, config):"
    end_marker = "async def run_dashboard"

    start_idx = content.find(start_marker)
    end_idx = content.find(end_marker)

    new_block = """def make_dashboard(mode, config):
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
        log_content.append("\\n", style=style)

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
    visible_symbols = all_pairs[pairs_scroll_offset :
                                pairs_scroll_offset + pairs_height]

    for i, symbol in enumerate(visible_symbols):
        abs_idx = pairs_scroll_offset + i
        is_selected = (abs_idx == selected_pair_index and
                       focused_panel == "pairs")
        row_style = "bold reverse" if is_selected else ""

        data = bot_state[symbol]
        pos = data.get('position')
        buy_count = data.get('consecutive_buys', 0)
        sell_count = data.get('consecutive_sells', 0)

        current_signal = "Waiting"
        if buy_count > 0:
            current_signal = f"{buy_count} Buy"
        elif sell_count > 0:
            current_signal = f"{sell_count} Sell"

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
        macd_str = f"{macd_hist:.4e}" if abs(
            macd_hist) < 0.001 else f"{macd_hist:.4f}"

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
    status_text.append(
        f"Update: {now.strftime('%H:%M:%S')} | Mode: {mode.upper()} | ", style="bold brown")
    status_text.append(
        "TAB: Switch | Arrows: Scroll | H: Help | X: Expert | M: Marquee | Exit: Ctrl+C", style="bold red")

    display_width = console.width - 4
    max_status_offset = max(0, len(status_text) - display_width)

    if max_status_offset > 0 and should_step:
        status_scroll_index = (status_scroll_index + 1) % (max_status_offset + 10)
        if status_scroll_index > max_status_offset:
            status_display = status_text[0: display_width]
        else:
            status_display = status_text[status_scroll_index: status_scroll_index + display_width]
    else:
        status_display = status_text
        status_display.justify = "center"

    if show_help:
        help_text = Text()
        help_text.append("\\n[bold cyan]Keyboard Shortcuts:[/]\\n", style="white")
        help_text.append("  TAB    : Switch focus between Logs and Pairs\\n")
        help_text.append("  UP/DN  : Move selection / Scroll the focused panel\\n")
        help_text.append("  ENTER  : Show/Hide K-Lines for selected symbol\\n")
        help_text.append("  X      : Toggle Expert Mode\\n")
        help_text.append("  M      : Toggle Marquee Effect\\n")
        help_text.append("  H      : Close this help menu\\n")
        help_text.append("  Ctrl+C : Stop the bot gracefully\\n")
        pairs_panel = Panel(
            help_text, title="[bold]Help / Info[/]", border_style="bold yellow")

    if show_chart and chart_symbol:
        chart_content = render_ascii_chart(chart_symbol, config)
        pairs_panel = Panel(
            chart_content, title=f"[bold]K-Lines: {chart_symbol}[/]", border_style="bold magenta")

    if not startup_complete:
        waiting_text = Text.from_markup(
            "\\n\\n\\n\\n\\n[bold blink yellow]Waiting for system initialization...[/]\\n", justify="center")
        waiting_text.append_text(Text.from_markup(
            "[dim]Fetching market data and calculating first signals...[/]\\n", style="white"))
        pairs_panel = Panel(
            waiting_text, title="[bold]System Startup[/]", border_style="bold yellow")

    layout = Layout()
    layout.split(
        Layout(Panel(Text("🛸 CCXT Pro Trading Bot v2 (Async)",
               style="bold magenta", justify="center"), border_style="blue"), size=3),
        Layout(log_panel, size=log_height+2),
        Layout(pairs_panel, name="main"),
        Layout(Panel(status_display, title="Status", border_style="cyan"), size=3)
    )
    return layout

"""
    new_content = content[:start_idx] + new_block + "\n" + content[end_idx:]
    with open('bot2.py', 'w') as f:
        f.write(new_content)

fix_dashboard()

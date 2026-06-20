# Cryptocurrencies multiplatform trading bot - Utilities
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import json
import os
import sys
import platform
import datetime
import random
import signal
import logging
import queue
from rich.console import Console

console = Console()

# Background sound thread initialization
sound_queue = queue.Queue()

def sound_worker():
    while True:
        try:
            action, config = sound_queue.get()
            if action == "shutdown":
                break
            _execute_play_sound(action, config)
            sound_queue.task_done()
        except Exception:
            pass


def _execute_play_sound(action, config):
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
            # Use os.write to ensure it's written even if stdout is buffered or redirected
            try:
                os.write(sys.stdout.fileno(), bell_char.encode())
            except:
                sys.stdout.write(bell_char)
                sys.stdout.flush()
    except Exception: pass

def format_price(price):
    """
    Formats price with at least 6 significant figures.
    Placing the dot smartly based on magnitude.
    """
    if price is None: return "-"
    if not isinstance(price, (int, float)): return str(price)
    if price == 0: return "0.000000"

    abs_price = abs(price)
    # Standard: Use g format with 10 significant digits
    if abs_price < 0.0001:
        # For very small prices, use more precision
        formatted = f"{price:.12f}".rstrip('0').rstrip('.')
    else:
        formatted = f"{price:.10g}"

    if 'e' in formatted:
        formatted = f"{price:.12f}".rstrip('0').rstrip('.')
    return formatted

def format_amount(amount):
    """
    Formats amount avoiding excessive zeros and maintaining precision for small assets.
    """
    if amount is None: return "-"
    if not isinstance(amount, (int, float)): return str(amount)
    if amount == 0: return "0"
    # Format with high precision then strip trailing zeros. Round to avoid float representation noise.
    return f"{round(amount, 12):.12f}".rstrip('0').rstrip('.')

def parse_base_bet(config):
    """
    Parses base_bet as a percentage of available balance.
    Returns (percentage as float, "%").
    """
    if not config: return 0.10, "%"
    raw_val = config.get('base_trade_amount', config.get('base_bet', '10%'))
    if isinstance(raw_val, str):
        try:
            val = float(raw_val.replace('%', '').strip())
            pct = val / 100.0 if val >= 1.0 else val
            return pct, "%"
        except ValueError:
            return 0.10, "%"
    val = float(raw_val)
    pct = val / 100.0 if val >= 1.0 else val
    return pct, "%"

def get_base_currency(symbol, config):
    """Returns the quote currency for a symbol, or the first configured base currency."""
    if symbol and '/' in symbol:
        return symbol.split('/')[1]
    base_currencies = config.get('base_currencies', ['USDT'])
    return base_currencies[0] if base_currencies else 'USDT'

def silent_worker_init():
    """Initializer to ignore SIGINT in worker processes."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)

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

def play_sound(action, config=None):
    """Adds a sound notification to the background queue."""
    sound_queue.put((action, config))

class JSONLoggingHandler(logging.Handler):
    def emit(self, record):
        log_entry = {
            "timestamp": datetime.datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
            "funcName": record.funcName,
            "lineNo": record.lineno,
        }
        if record.exc_info:
            log_entry["exc_info"] = logging.Formatter().formatException(record.exc_info)
        print(json.dumps(log_entry))

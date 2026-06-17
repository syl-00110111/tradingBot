# Cryptocurrencies multiplatform trading bot - Optimization & Backtesting
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import time
import logging
import random
import os
import signal
import threading
import concurrent.futures
import gc
import pandas as pd
import numpy as np
import torch
import psutil
import matplotlib.pyplot as plt
from rich.console import Console

from utils import format_price, format_amount, parse_base_bet, get_base_currency, silent_worker_init
from indicators import get_signals, get_common_indicators, STRATEGIES
from exchange_handler import fetch_ohlcv_incremental
from monte_carlo import MonteCarloEngine
from persistence import CacheManager

console = Console()

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
    base_bet_curr = get_base_currency(None, config)
    stats_text = f"Profit: {p_str} {base_bet_curr}\nWin Rate: {results['win_rate']:.1%}\nMax DD: {results['max_dd']:.1%}"
    plt.annotate(stats_text, xy=(0.02, 0.95), xycoords='axes fraction',
                 bbox=dict(boxstyle="round", fc="w", alpha=0.8), fontsize=10, verticalalignment='top')

    plt.legend()
    plt.grid(True, alpha=0.3)

    filename = f"backtest_{symbol.replace('/', '_')}_{strategy_name}.png"
    plt.savefig(filename)
    console.print(f"[bold green]Backtest plot saved as {filename}[/]")
    plt.close()

def run_backtest_logic(exchange, symbol, strategy, aggr_name, config, term='short', df_in=None, limit=500, engine=None, device=None, skip_mc=True, return_full_df=False, copy_df=True, ohlcv_cache_manager=None):
    """Core backtesting simulation logic."""
    fee_rate = 0.001
    if exchange:
        try:
            fee_rate = exchange.fetch_trading_fee(symbol)
        except Exception:
            pass

    if engine and df_in is not None and not df_in.empty:
         base_df = get_common_indicators(df_in.copy(), device if device is not None else torch.device("cpu"))
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

    test_config = aggr_settings.copy()
    test_config['strategy'] = strategy
    timeframe = term_settings.get('timeframe', '5m')

    if df_in is None:
        try:
            ohlcv, _ = fetch_ohlcv_incremental(exchange, symbol, timeframe, ohlcv_cache_manager, limit=limit)
        except Exception as e:
            console.print(f"[red]Error fetching OHLCV for {symbol} ({timeframe}): {e}[/]")
            return None

        if not ohlcv:
            console.print(f"[red]No OHLCV returned for {symbol} ({timeframe}).[/]")
            return None

        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    else:
        df = df_in.copy() if copy_df else df_in

    if 'buy_signal' not in df.columns:
        try:
            test_config['device'] = device if device is not None else torch.device('cpu')
            df = get_signals(df, test_config, is_backtest=True)
        except Exception as e:
            if exchange is not None:
                 console.print(f"[red]Error calculating signals for {symbol}: {e}[/]")
            return None

    if df is None or df.empty:
        return None

    eval_window_base = term_settings.get('eval_candles', 60)
    max_rand = max(1, int(eval_window_base * 0.1))
    eval_window = eval_window_base + random.randint(-max_rand, max_rand)
    start_idx = max(0, len(df) - eval_window)

    base_percentage, _ = parse_base_bet(config)
    balance = 100.0
    position = None
    trades = []
    equity_curve = []

    for i in range(len(df)):
        if i < start_idx:
            equity_curve.append(balance)
            continue

        row = df.iloc[i]
        price = row['close']

        if position and row['sell_signal']:
            revenue = price * position['amount']
            fee = revenue * fee_rate
            revenue_net = revenue - fee
            profit = revenue_net - position['entry_cost']
            balance += revenue_net
            trades.append({'profit': profit})
            position = None

        trade_amount = balance * base_percentage
        if not position and row['buy_signal'] and balance >= trade_amount:
            fee = trade_amount * fee_rate
            cost_total = trade_amount + fee
            if balance >= cost_total:
                buy_amount = trade_amount / price
                balance -= cost_total
                position = {'entry_price': price, 'amount': buy_amount, 'entry_cost': cost_total}

        equity_curve.append(balance + (position['amount'] * price if position else 0))

    total_profit = equity_curve[-1] - equity_curve[start_idx] if len(equity_curve) > start_idx else 0

    if not skip_mc:
        mc_score = mc.validate_strategy(df)
        total_profit *= mc_score
    else:
        mc_score = 1.0

    wins = [t for t in trades if t['profit'] > 0]
    win_rate = len(wins) / len(trades) if trades else 0

    equity_series = pd.Series(equity_curve)
    max_dd = (equity_series.cummax() - equity_series).max() / equity_series.cummax().max() if not equity_series.empty else 0

    eval_df = df.iloc[start_idx:] if start_idx < len(df) else df.iloc[-1:]
    start_time_dt = eval_df['timestamp'].iloc[0]
    end_time_dt = eval_df['timestamp'].iloc[-1]

    latest = df.iloc[-1]
    tech_state = {
        'rsi': float(latest.get('rsi', 50)),
        'adx': float(latest.get('adx', 0)),
        'volatility': float(latest.get('volatility', 0)),
        'ema_f': float(latest.get('ema_f', 0)),
        'ema_s': float(latest.get('ema_s', 0))
    }

    return {
        'df': df,
        'profit': total_profit,
        'profit_raw': total_profit,
        'win_rate': win_rate,
        'max_dd': max_dd,
        'trades_count': len(trades),
        'start_time': start_time_dt.strftime("%Y-%m-%d %H:%M"),
        'end_time': end_time_dt.strftime("%Y-%m-%d %H:%M"),
        'start_ts': start_time_dt.timestamp(),
        'prices': eval_df['close'].tolist(), 'volumes': eval_df['volume'].tolist(),
        'tech_state': tech_state,
        'equity_curve': equity_curve if return_full_df else []
    }

def run_backtest_mode(exchange, config, args, engine=None, device=None, ohlcv_cache_manager=None):
    symbol = args.symbol
    strategy = args.strategy
    term = args.term or 'short'

    results = run_backtest_logic(exchange, symbol, strategy, 'balanced', config, term=term, engine=engine, device=device, skip_mc=False, ohlcv_cache_manager=ohlcv_cache_manager)
    if not results:
        console.print("[bold red]Backtest failed.[/]")
        return

    console.print(f"\n[bold cyan]Backtest Results for {symbol} ({strategy}):[/]")
    console.print(f"  Profit: [bold green]{results['profit']:.4f}[/]")
    console.print(f"  Win Rate: {results['win_rate']:.1%}")
    console.print(f"  Max Drawdown: {results['max_dd']:.1%}")
    console.print(f"  Trades: {results['trades_count']}")
    console.print(f"  Period: {results['start_time']} to {results['end_time']}")

    if results['df'] is not None:
        plot_backtest(results['df'], symbol, strategy, 'balanced', results, engine, config)

def run_benchmark_for_strategy(symbol, strategy, config, term_to_test, aggr, df_with_common, engine, device, eval_window, profit_threshold, now_ts):
    res = run_backtest_logic(None, symbol, strategy, aggr, config, term=term_to_test, df_in=df_with_common, engine=engine, device=device, skip_mc=True, copy_df=False)
    if res and res['profit'] >= profit_threshold:
        return {
            'strategy': strategy, 'aggr': aggr, 'symbol': symbol,
            'profit': res['profit'], 'win_rate': res['win_rate'], 'max_dd': res['max_dd'],
            'start_time': res['start_time'], 'end_time': res['end_time'], 'start_ts': res['start_ts'],
            'prices': res['prices'], 'volumes': res['volumes'], 'tech_state': res['tech_state'],
            'last_bench_ts': now_ts,
            'sim': 0
        }
    return None

def run_benchmark_for_symbol(symbol, config, term_to_test, aggrs, strategies, df_in, engine=None, device=None, threshold_conv=1.0):
    term_cfg = config.get('expected_profit_terms', {}).get(term_to_test, {})
    eval_window = term_cfg.get('eval_candles', 60)
    profit_threshold = config.get('profit_thresholds', {}).get('min_pattern_profit', 0.015)
    profit_threshold *= threshold_conv
    now_ts = time.time()
    patterns = []

    df_with_common = get_common_indicators(df_in.copy(), device)

    for strategy in strategies:
        for aggr in aggrs:
            p = run_benchmark_for_strategy(symbol, strategy, config, term_to_test, aggr, df_with_common, engine, device, eval_window, profit_threshold, now_ts)
            if p: patterns.append(p)

    patterns.sort(key=lambda x: x['profit'], reverse=True)
    return symbol, patterns[:5]

def run_benchmark_mode(exchange, config, args, shutdown_event, bot_lock, global_pattern_pool, benchmarking_pairs, term_override=None, status=None, data_manager=None, pattern_manager=None, engine=None, device=None, symbols_to_process=None, ohlcv_cache_manager=None, priority_symbols=None):
    term_to_test = term_override if term_override else (args.term if args else 'short')
    terms_cfg = config.get('expected_profit_terms', {})
    term_cfg = terms_cfg.get(term_to_test, {})
    timeframe = term_cfg.get('timeframe', '5m')

    strategies = STRATEGIES
    aggrs = ['balanced']

    all_pairs = list(config.get('pairs', {}).keys())
    if symbols_to_process:
        symbols = symbols_to_process
    else:
        symbols = all_pairs
    # Prioritize symbols with open positions
    open_pos_symbols = []
    if data_manager:
        open_pos_symbols = [s for s, pos in data_manager.get_open_positions().items() if pos]

    if open_pos_symbols:
        priority_set = set(open_pos_symbols)
        symbols = [s for s in symbols if s in priority_set] + [s for s in symbols if s not in priority_set]
    elif priority_symbols:
        # Move priority symbols to the front
        priority_set = set(priority_symbols)
        symbols = [s for s in symbols if s.split('/')[0] in priority_set] + [s for s in symbols if s.split('/')[0] not in priority_set]
    base_bet_curr = get_base_currency(None, config)

    cache_mgr = CacheManager()
    best_overall = {t: {'profit': -999, 'params': None} for t in ['short', 'medium', 'long', 'total']}
    best_per_symbol = {}
    optimization_map = {}
    symbol_data_map = {}
    symbols_to_bench = []

    def fetch_and_validate(sym):
        if shutdown_event.is_set(): return None
        try:
            cached_patterns = cache_mgr.get(sym, term_to_test)
            best_cached = None
            # If we have cached patterns, we MUST retry them on new candles if possible
            # BUT we still prioritize symbols without ANY patterns or with old ones.

            force_refresh = False
            if cached_patterns:
                best_cached = cached_patterns[0]
                now_ts = time.time()

                # Instruction: Only test the benchmarks if at least 5% of the SPM has elapsed relative to real time.
                p_len = len(best_cached.get('prices', []))
                tf_map = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}
                tf_secs = tf_map.get(timeframe, 300)
                p_duration_secs = p_len * tf_secs
                spm_threshold_secs = p_duration_secs * 0.05 if p_len > 0 else (3600 * 24)

                # If patterns are older than threshold, let's try to refresh them
                if now_ts - best_cached.get('last_bench_ts', 0) > spm_threshold_secs:
                    force_refresh = True
                else:
                    return sym, None, cached_patterns, best_cached

            limit = 20000 if term_to_test == 'short' else 40000
            ohlcv, _ = fetch_ohlcv_incremental(exchange, sym, timeframe, ohlcv_cache_manager, limit=limit)
            if not ohlcv: return None
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return sym, df, None, None
        except Exception as e:
            logging.error(f"Error preparing {sym}: {e}")
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_and_validate, sym): sym for sym in symbols}
        for i, future in enumerate(concurrent.futures.as_completed(futures)):
            res = future.result()
            if not res: continue
            sym, df, cached_patterns, best = res

            if best:
                best_per_symbol[sym] = best.copy()
                optimization_map[sym] = best
                if data_manager:
                    pattern_manager.set_patterns(sym, cached_patterns)
            else:
                symbols_to_bench.append(sym)
                symbol_data_map[sym] = df

            if status: status.update(f"[bold cyan][{i+1}/{len(symbols)}] Processed history for {sym}...")

    if symbols_to_bench:
        def handle_bench_shutdown(sig, frame):
             shutdown_event.set()
             if hasattr(executor, '_processes'):
                 for p in executor._processes.values():
                     try: p.terminate()
                     except: pass
             executor.shutdown(wait=False, cancel_futures=True)
             raise KeyboardInterrupt

        if status: status.update('[bold yellow]Analyzing patterns and optimizing strategies...')

        cpu_count = os.cpu_count() or 1
        max_workers = cpu_count

        try:
            cpu_usage = psutil.cpu_percent(interval=0.5)
            mem_available = psutil.virtual_memory().available / (1024 * 1024 * 1024) # GB

            # Instruction: if below 40% cpu usage and still 1G is free, allocate one process more
            if cpu_usage < 40 and mem_available > 1.0:
                max_workers += 1
                logging.info(f"Dynamic worker allocation: Boosting to {max_workers} processes (CPU: {cpu_usage}%, RAM Free: {mem_available:.2f}GB)")
        except Exception as e:
            logging.debug(f"Failed to calculate dynamic workers: {e}")

        executor_class = concurrent.futures.ProcessPoolExecutor
        with executor_class(max_workers=max_workers, initializer=silent_worker_init) as executor:
            original_handler = None
            if threading.current_thread() == threading.main_thread():
                original_handler = signal.signal(signal.SIGINT, handle_bench_shutdown)
            try:
                futures = []
                conv_cache = {}
                for sym in symbols_to_bench:
                    if sym not in symbol_data_map: continue
                    quote = sym.split('/')[1]
                    if quote not in conv_cache:
                        t_conv = 1.0
                        if quote != base_bet_curr:
                            try:
                                ticker = exchange.fetch_ticker(f'{base_bet_curr}/{quote}')
                                if ticker and ticker.get('last'):
                                    t_conv = ticker['last']
                            except: pass
                        conv_cache[quote] = t_conv
                    futures.append(executor.submit(run_benchmark_for_symbol, sym, config, term_to_test, aggrs, strategies, symbol_data_map[sym], engine, device, threshold_conv=conv_cache[quote]))
                for future in concurrent.futures.as_completed(futures):
                    if shutdown_event.is_set(): break
                    sym, patterns = future.result()
                    if patterns:
                        msg_target = status.console if status else console
                        bench_threshold = config.get('profit_thresholds', {}).get('bench_avg_threshold', 0.22)
                        winning_patterns = [p for p in patterns if p['profit'] >= bench_threshold]
                        if winning_patterns:
                            avg_profit = sum(p['profit'] for p in winning_patterns) / len(winning_patterns)
                        else:
                            avg_profit = patterns[0]['profit']

                        best_for_symbol = patterns[0].copy()
                        best_for_symbol['avg_bench_profit'] = avg_profit
                        best_per_symbol[sym] = best_for_symbol
                        if data_manager:
                             pattern_manager.set_patterns(sym, patterns)

                        period_str = f" [dim](From {best_for_symbol.get('start_time')} to {best_for_symbol.get('end_time')})[/]"
                        cache_mgr.set(sym, term_to_test, patterns)

                        if term_override:
                            optimization_map[sym] = best_for_symbol

                        if best_overall.get(term_to_test) and best_for_symbol['profit'] > best_overall[term_to_test]['profit']:
                            best_overall[term_to_test] = {'profit': best_for_symbol['profit'], 'params': (best_for_symbol['strategy'], best_for_symbol['aggr'], sym)}

                        if best_for_symbol['profit'] > best_overall['total']['profit']:
                             best_overall['total'] = {'profit': best_for_symbol['profit'], 'params': (best_for_symbol['strategy'], best_for_symbol['aggr'], sym)}
            finally:
                if original_handler:
                    signal.signal(signal.SIGINT, original_handler)
                symbol_data_map.clear()

    if term_override:
        if status: status.update('[bold green]Optimization complete.')
        if best_per_symbol:
            time.sleep(3)
        with bot_lock:
            global_pattern_pool.clear()
            all_pairs = list(config.get('pairs', {}).keys())
            for sym in all_pairs:
                patterns = pattern_manager.get_patterns(sym)
                global_pattern_pool.extend(patterns)
            benchmarking_pairs.difference_update(set(symbols))
        gc.collect()
        if device.type == 'cuda' and torch.cuda.is_available():
            torch.cuda.empty_cache()
        elif device.type == 'xpu':
            try:
                import intel_extension_for_pytorch as ipex
                torch.xpu.empty_cache()
            except: pass
        elif device.type == 'mps':
            try: torch.mps.empty_cache()
            except: pass
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
        base_bet_curr = get_base_currency(None, config)
        try:
            balance_data = exchange.fetch_balance()
            total_balance = balance_data.get(base_bet_curr, {}).get('total', 0) if isinstance(balance_data.get(base_bet_curr), dict) else balance_data.get(base_bet_curr, 0)
        except:
            total_balance = 0

        threshold_pct = config.get('profit_thresholds', {}).get('no_patterns_msg_threshold_pct', 0.01)
        if total_balance > 0:
            msg_threshold_val = total_balance * threshold_pct
        else:
            msg_threshold_val = config.get('profit_thresholds', {}).get('no_patterns_msg_threshold', 0.022)

        msg_threshold = f"{msg_threshold_val:.4g} {base_bet_curr}"
        if symbols:
            quote = symbols[0].split('/')[1]
            if quote != base_bet_curr:
                try:
                    ticker = exchange.fetch_ticker(f'{base_bet_curr}/{quote}')
                    if ticker and ticker.get('last'):
                        msg_threshold = f"{msg_threshold_val * ticker['last']:.4g} {quote}"
                except: pass

        console.print(f"[yellow]No successful patterns (> {msg_threshold}) were found in the scanned historical data.[/]")
    else:
        for sym in symbols:
            if sym not in best_per_symbol:
                 console.print(f"[dim][{sym}] No profitable patterns found in current scan.[/]")

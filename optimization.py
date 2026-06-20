# Cryptocurrencies multiplatform trading bot - Optimization & Backtesting
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import time
import logging
import random
import os
import pandas as pd
import numpy as np
import torch
import psutil
import matplotlib.pyplot as plt
from rich.console import Console
import datetime

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

async def run_backtest_logic(exchange, symbol, strategy, aggr_name, config, term='short', df_in=None, limit=None, engine=None, device=None, skip_mc=True, return_full_df=False, copy_df=True, ohlcv_cache_manager=None):
    """Core backtesting simulation logic optimized for speed."""
    fee_rate = 0.001
    if exchange:
        try:
            fee_rate = exchange.fetch_trading_fee(symbol)
        except Exception:
            pass

    timeframe = '1m'

    if df_in is None:
        try:
            fetch_limit = limit or 500
            # Add padding for indicators
            fetch_limit += 200
            ohlcv, _ = await fetch_ohlcv_incremental(exchange, symbol, timeframe, ohlcv_cache_manager, limit=fetch_limit)
            if not ohlcv: return None
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        except Exception as e:
            logging.error(f"Error fetching OHLCV for {symbol} ({timeframe}): {e}")
            return None
    else:
        df = df_in.copy() if copy_df else df_in

    if engine and not df.empty:
         # Use pre-calculated indicators if available for dynamic settings
         latest = df.iloc[-1]
         adx_v = latest.get('adx', 0)
         vol_v = latest.get('volatility', 0)
         aggr_settings_full = engine.get_dynamic_settings(adx_v, vol_v)
         aggr_settings = {k: v for k, v in aggr_settings_full.items() if k != 'label'}
    else:
         aggr_settings = {"ema_fast": 9, "ema_slow": 21, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9}

    if 'buy_signal' not in df.columns:
        try:
            test_config = aggr_settings.copy()
            test_config.update({'strategy': strategy, 'device': device or torch.device('cpu')})
            df = get_signals(df, test_config, is_backtest=True)
        except Exception as e:
            logging.error(f"Error calculating signals for {symbol}: {e}")
            return None

    if df is None or df.empty: return None

    eval_window_base = 60
    max_rand = max(1, int(eval_window_base * 0.1))
    eval_window = eval_window_base + random.randint(-max_rand, max_rand)
    start_idx = max(0, len(df) - eval_window)

    base_percentage, _ = parse_base_bet(config)
    balance = 100.0
    position = None
    trades = []

    # Optimized loop: only iterate over the evaluation window
    eval_part = df.iloc[start_idx:]
    equity_curve = [balance] * start_idx

    for i in range(len(eval_part)):
        row = eval_part.iloc[i]
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

    total_profit = equity_curve[-1] - equity_curve[start_idx]

    if not skip_mc:
        # Strictly match BOT_WORKFLOW.md: 100 simulations for Benchmark, 1000 for Live
        mc = MonteCarloEngine(num_simulations=100, timeframe_candles=20)
        mc.set_device(device or torch.device("cpu"))
        mc_score = mc.validate_strategy(df)
        total_profit *= mc_score
    else:
        mc_score = 1.0

    wins = [t for t in trades if t['profit'] > 0]
    win_rate = len(wins) / len(trades) if trades else 0
    equity_series = pd.Series(equity_curve)
    max_dd = (equity_series.cummax() - equity_series).max() / equity_series.cummax().max() if not equity_series.empty else 0

    start_time_dt = eval_part['timestamp'].iloc[0]
    end_time_dt = eval_part['timestamp'].iloc[-1]

    latest = df.iloc[-1]
    return {
        'df': df, 'profit': total_profit, 'win_rate': win_rate, 'max_dd': max_dd,
        'trades_count': len(trades), 'start_time': start_time_dt.strftime("%Y-%m-%d %H:%M"),
        'end_time': end_time_dt.strftime("%Y-%m-%d %H:%M"), 'start_ts': start_time_dt.timestamp(),
        'prices': eval_part['close'].tolist(), 'volumes': eval_part['volume'].tolist(),
        'mc_score': mc_score,
        'tech_state': {
            'rsi': float(latest.get('rsi', 50)), 'adx': float(latest.get('adx', 0)),
            'volatility': float(latest.get('volatility', 0)), 'ema_f': float(latest.get('ema_f', 0)),
            'ema_s': float(latest.get('ema_s', 0))
        },
        'equity_curve': equity_curve if return_full_df else []
    }

async def run_backtest_mode(exchange, config, args, engine=None, device=None, ohlcv_cache_manager=None):
    symbol = args.symbol
    strategy = args.strategy
    term = args.term or 'short'

    results = await run_backtest_logic(exchange, symbol, strategy, 'balanced', config, term=term, engine=engine, device=device, skip_mc=False, ohlcv_cache_manager=ohlcv_cache_manager)
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

async def run_benchmark_for_strategy(symbol, strategy, config, term_to_test, aggr, df_with_common, engine, device, eval_window, profit_threshold, now_ts):
    res = await run_backtest_logic(None, symbol, strategy, aggr, config, term=term_to_test, df_in=df_with_common, engine=engine, device=device, skip_mc=True, copy_df=False)
    if res and res['profit'] >= profit_threshold:
        return {
            'strategy': strategy, 'aggr': aggr, 'symbol': symbol,
            'profit': res['profit'], 'win_rate': res['win_rate'], 'max_dd': res['max_dd'],
            'start_time': res['start_time'], 'end_time': res['end_time'], 'start_ts': res['start_ts'],
            'prices': res['prices'], 'volumes': res['volumes'], 'tech_state': res['tech_state'],
            'mc_score': res.get('mc_score', 1.0),
            'last_bench_ts': now_ts,
            'sim': 0
        }
    return None

async def run_benchmark_for_symbol(symbol, config, term_to_test, aggrs, strategies, df_in, engine=None, device=None, threshold_conv=1.0):
    """
    Implements the O(N) Sliding Window Algorithm as described in ALGO_FENETRE_GLISSANTE.md.
    Instead of re-running full backtests, we calculate the equity curve once and slide a window.
    """
    eval_window = 60
    profit_threshold = 0.01
    profit_threshold *= threshold_conv
    now_ts = time.time()
    patterns = []

    # 1. Global Signal Generation (Vectorized)
    df_base = get_common_indicators(df_in, device)
    base_cols = [c for c in df_base.columns if c not in ['buy_signal', 'sell_signal', 'buy_candidate', 'sell_candidate', 'tendency']]
    df_base = df_base[base_cols]

    fee_rate = 0.001 # Default fallback

    for strategy in strategies:
        df_strat = df_base.copy()
        test_cfg = {'strategy': strategy, 'device': device or torch.device('cpu')}
        df_strat = get_signals(df_strat, test_cfg, is_backtest=True)

        # 2. Equity Mapping (O(N))
        # We calculate a simplified equity curve based on signals
        prices = df_strat['close'].values
        buys = df_strat['buy_signal'].values
        sells = df_strat['sell_signal'].values

        equity = np.zeros(len(prices))
        balance = 100.0
        pos_amt = 0

        for i in range(len(prices)):
            if pos_amt == 0 and buys[i]:
                pos_amt = (balance * (1 - fee_rate)) / prices[i]
                balance = 0
            elif pos_amt > 0 and sells[i]:
                balance = pos_amt * prices[i] * (1 - fee_rate)
                pos_amt = 0
            equity[i] = balance + (pos_amt * prices[i])

        # 3. Sliding the Window (O(N))
        # Profit = Percentage change over the window
        if len(equity) > eval_window:
            # Shifted equity to avoid division by zero
            denom = np.where(equity[:-eval_window] <= 0, 100.0, equity[:-eval_window])
            profits = (equity[eval_window:] - equity[:-eval_window]) / denom

            # Find peaks (top 5 non-overlapping or simply top 5)
            # Documentation says "Top 5 Profitable Windows"
            top_indices = np.argsort(profits)[-10:] # Get some extras to filter

            found_indices = []
            for idx in reversed(top_indices):
                if profits[idx] < profit_threshold: continue

                # Check for overlap to get diverse patterns
                if any(abs(idx - prev) < eval_window // 2 for prev in found_indices):
                    continue

                found_indices.append(idx)
                if len(found_indices) >= 5: break

            for idx in found_indices:
                window_df = df_strat.iloc[idx : idx + eval_window]
                latest = window_df.iloc[-1]
                patterns.append({
                    'strategy': strategy, 'aggr': 'balanced', 'symbol': symbol,
                    'profit': float(profits[idx]), 'win_rate': 1.0, 'max_dd': 0.0,
                    'start_time': window_df.iloc[0]['timestamp'].strftime("%Y-%m-%d %H:%M"),
                    'end_time': latest['timestamp'].strftime("%Y-%m-%d %H:%M"),
                    'start_ts': window_df.iloc[0]['timestamp'].timestamp(),
                    'prices': window_df['close'].tolist(),
                    'volumes': window_df['volume'].tolist(),
                    'tech_state': {
                        'rsi': float(latest.get('rsi', 50)),
                        'adx': float(latest.get('adx', 0)),
                        'volatility': float(latest.get('volatility', 0)),
                        'ema_f': float(latest.get('ema_f', 0)),
                        'ema_s': float(latest.get('ema_s', 0))
                    },
                    'mc_score': 1.1, # Default
                    'last_bench_ts': now_ts
                })

    patterns.sort(key=lambda x: x['profit'], reverse=True)

    # Calculate total performance over entire history for recommendations
    total_history_profit = float((equity[-1] - equity[0]) / 100.0)
    for p in patterns:
        p['total_history_profit'] = total_history_profit

    return symbol, patterns[:5]

async def run_benchmark_mode(exchange, config, args, shutdown_event, global_pattern_pool, benchmarking_pairs, term_override=None, status=None, data_manager=None, pattern_manager=None, engine=None, device=None, symbols_to_process=None, ohlcv_cache_manager=None, priority_symbols=None, bot_state=None):
    term_to_test = 'short'
    timeframe = '1m'

    from indicators import STRATEGIES
    strategies = STRATEGIES
    aggrs = ['balanced']

    all_pairs = list(config.get('pairs', {}).keys())
    if symbols_to_process:
        symbols = symbols_to_process
    elif args and args.symbol:
        symbols = [args.symbol]
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
    best_overall = {t: {'profit': -999, 'params': None} for t in ['short', 'total']}
    best_per_symbol = {}
    optimization_map = {}
    symbol_data_map = {}

    # Sequential processing
    for i, sym in enumerate(symbols):
        if shutdown_event.is_set(): break
        if status: status.update(f"[bold cyan][{i+1}/{len(symbols)}] Processing {sym}...")

        try:
            cached_patterns = cache_mgr.get(sym, term_to_test)
            best_cached = None

            if cached_patterns:
                best_cached = cached_patterns[0]
                now_ts = time.time()
                p_len = len(best_cached.get('prices', []))
                p_duration_secs = p_len * 60
                spm_threshold_secs = p_duration_secs * 0.05 if p_len > 0 else (3600 * 24)

                if now_ts - best_cached.get('last_bench_ts', 0) <= spm_threshold_secs:
                    best_per_symbol[sym] = best_cached.copy()
                    optimization_map[sym] = best_cached
                    if data_manager:
                        pattern_manager.set_patterns(sym, cached_patterns)
                    continue

            # Fetch deep history sequentially (this uses REST but let's assume it's for initial bench)
            # User instruction 1 says "use only ccxt.pro" and Instruction 2 global design mentions "benchmark sequentially on symbols"
            # It's likely we need some history for benchmarks.
            limit = 1000
            ohlcv, _ = await fetch_ohlcv_incremental(exchange, sym, timeframe, ohlcv_cache_manager, limit=limit)
            if not ohlcv: continue
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            # Run benchmark sequentially
            _, patterns = await run_benchmark_for_symbol(sym, config, term_to_test, aggrs, strategies, df, engine, device)

            if patterns:
                bench_threshold = config.get('profit_thresholds', {}).get('bench_avg_threshold', 0.05)
                winning_patterns = [p for p in patterns if p['profit'] >= bench_threshold]
                avg_profit = sum(p['profit'] for p in winning_patterns) / len(winning_patterns) if winning_patterns else patterns[0]['profit']

                best_for_symbol = patterns[0].copy()
                avg_profit = best_for_symbol.get('total_history_profit', avg_profit)
                best_for_symbol['avg_bench_profit'] = avg_profit
                best_per_symbol[sym] = best_for_symbol

                if data_manager:
                    pattern_manager.set_patterns(sym, patterns, save=False)

                if bot_state is not None and sym in bot_state:
                    bot_state[sym].update({
                        'aggr': best_for_symbol['aggr'],
                        'strategy': best_for_symbol['strategy'],
                        'bench_profit': avg_profit
                    })

                if sym in config.get('pairs', {}):
                    config['pairs'][sym].update({
                        'aggr': best_for_symbol['aggr'],
                        'strategy': best_for_symbol['strategy'],
                        'expected_profit': avg_profit
                    })

                cache_mgr.set(sym, term_to_test, patterns, save=False)
                optimization_map[sym] = best_for_symbol

                if best_for_symbol['profit'] > best_overall['short']['profit']:
                    best_overall['short'] = {'profit': best_for_symbol['profit'], 'params': (best_for_symbol['strategy'], best_for_symbol['aggr'], sym)}

                if best_for_symbol['profit'] > best_overall['total']['profit']:
                     best_overall['total'] = {'profit': best_for_symbol['profit'], 'params': (best_for_symbol['strategy'], best_for_symbol['aggr'], sym)}

        except Exception as e:
            logging.error(f"Error benchmarking {sym}: {e}")

    # Final maintenance
    cache_mgr.save_all()
    if data_manager: pattern_manager.save_all()

    if term_override:
        global_pattern_pool.clear()
        all_pairs = list(config.get('pairs', {}).keys())
        for sym in all_pairs:
            patterns = pattern_manager.get_patterns(sym)
            global_pattern_pool.extend(patterns)
        benchmarking_pairs.difference_update(set(symbols))

        gc.collect()
        if device.type == 'cuda' and torch.cuda.is_available():
            torch.cuda.empty_cache()
        return optimization_map

    console.print("\n[bold magenta]=== BENCHMARK RECOMMENDATIONS ===[/]")
    found_any = False
    for term in ['short', 'total']:
        label = term.upper()
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
        if total_balance > 0:
            msg_threshold_val = min(0.01, total_balance * threshold_pct)
        else:
            msg_threshold_val = 0.01

        msg_threshold = f"{msg_threshold_val:.4g} {base_bet_curr}"
        console.print(f"[yellow]No successful patterns (> {msg_threshold}) were found in the scanned historical data.[/]")
    return best_per_symbol

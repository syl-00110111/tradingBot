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
            fetch_limit = limit or 60
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

    eval_window = 60
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
    Bare-bone benchmarking: backtest strategies on the provided history (max 60 candles)
    and return patterns for Success Pattern Matching (SPM).
    """
    now_ts = time.time()
    patterns = []

    # Limit to latest 60 candles as per requirement
    df_work = df_in.tail(60).copy()

    # Pre-calculate common indicators once
    df_work = get_common_indicators(df_work, device)

    for strategy in strategies:
        res = await run_backtest_logic(
            None, symbol, strategy, 'balanced', config,
            df_in=df_work, engine=engine, device=device,
            skip_mc=True, copy_df=True
        )

        if res:
            patterns.append({
                'strategy': strategy,
                'aggr': 'balanced',
                'symbol': symbol,
                'profit': res['profit'],
                'win_rate': res['win_rate'],
                'max_dd': res['max_dd'],
                'start_time': res['start_time'],
                'end_time': res['end_time'],
                'start_ts': res['start_ts'],
                'prices': res['prices'],
                'volumes': res['volumes'],
                'tech_state': res['tech_state'],
                'mc_score': res.get('mc_score', 1.0),
                'last_bench_ts': now_ts
            })

    # Sort by profit and return patterns
    patterns.sort(key=lambda x: x['profit'], reverse=True)
    return symbol, patterns

async def run_benchmark_mode(exchange, config, args, shutdown_event, global_pattern_pool, benchmarking_pairs, term_override=None, status=None, data_manager=None, pattern_manager=None, engine=None, device=None, symbols_to_process=None, ohlcv_cache_manager=None, priority_symbols=None, bot_state=None):
    from indicators import STRATEGIES

    symbols = symbols_to_process or ( [args.symbol] if args and args.symbol else list(config.get('pairs', {}).keys()) )

    best_per_symbol = {}

    console.print("\n[bold magenta]=== BARE-BONE BENCHMARK ===[/]")

    for i, sym in enumerate(symbols):
        if shutdown_event.is_set(): break
        if status: status.update(f"[bold cyan][{i+1}/{len(symbols)}] Benchmarking {sym}...")
        else: console.print(f"[{i+1}/{len(symbols)}] Benchmarking {sym}...")

        try:
            # Fetch latest 60 candles
            limit = 60
            ohlcv, _ = await fetch_ohlcv_incremental(exchange, sym, '1m', ohlcv_cache_manager, limit=limit)
            if not ohlcv: continue
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            _, patterns = await run_benchmark_for_symbol(sym, config, 'short', ['balanced'], STRATEGIES, df, engine, device)

            if patterns:
                best = patterns[0]
                best_per_symbol[sym] = best
                console.print(f"  > {sym}: [bold green]{best['strategy']}[/] (Profit: {best['profit']:.4f})")

                if pattern_manager:
                    pattern_manager.set_patterns(sym, [best], save=True)

                if global_pattern_pool is not None:
                    # Keep pool fresh with the best patterns
                    global_pattern_pool[:] = [p for p in global_pattern_pool if p.get('symbol') != sym]
                    global_pattern_pool.append(best)

                if bot_state and sym in bot_state:
                    bot_state[sym].update({
                        'strategy': best['strategy'],
                        'bench_profit': best['profit'],
                        'active_pattern': best
                    })
        except Exception as e:
            logging.error(f"Error benchmarking {sym}: {e}")

    if not best_per_symbol:
        console.print(f"[yellow]No successful patterns were found in the scanned historical data.[/]")
    return best_per_symbol

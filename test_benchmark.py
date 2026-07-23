import sys
import os
import json
import math
import time

# 1. Setup Mock Environment if pandas/numpy/ccxt/rich are not installed
try:
    import pandas as pd
    import numpy as np
    import ccxt
    import rich
    import plotext
    PANDAS_INSTALLED = True
except ImportError:
    PANDAS_INSTALLED = False

if not PANDAS_INSTALLED:
    from unittest.mock import MagicMock

    class MockSeries:
        def __init__(self, data):
            self.data = list(data)
        def fillna(self, val):
            return MockSeries([val if x is None or (isinstance(x, float) and math.isnan(x)) else x for x in self.data])
        def tolist(self):
            return list(self.data)
        def __len__(self):
            return len(self.data)
        def __getitem__(self, idx):
            return self.data[idx]

    class MockDataFrame:
        def __init__(self, data=None, columns=None, index=None):
            if data is None:
                self.data = []
            elif isinstance(data, dict):
                keys = list(data.keys())
                length = len(data[keys[0]]) if keys else 0
                self.data = []
                for i in range(length):
                    row = {k: data[k][i] for k in keys}
                    self.data.append(row)
            else:
                raw_data = list(data)
                self.data = []
                for item in raw_data:
                    if isinstance(item, dict):
                        self.data.append(dict(item))
                    elif isinstance(item, (list, tuple)) and columns:
                        row_dict = {columns[i]: item[i] for i in range(min(len(columns), len(item)))}
                        self.data.append(row_dict)
                    else:
                        self.data.append(item)
            self.columns = columns
            self.index = index

        @property
        def empty(self):
            return len(self.data) == 0

        def copy(self):
            return MockDataFrame([dict(r) if isinstance(r, dict) else list(r) for r in self.data], self.columns, self.index)

        def get(self, key, default=None):
            if not self.data:
                return default
            if isinstance(self.data[0], dict):
                if key in self.data[0]:
                    return MockSeries([r[key] for r in self.data])
                return default
            return default

        def __len__(self):
            return len(self.data)

        def __getitem__(self, key):
            return self.get(key)

        @property
        def iloc(self):
            class ILocIndexer:
                def __init__(self, df):
                    self.df = df
                def __getitem__(self, idx):
                    if isinstance(idx, slice):
                        return MockDataFrame(self.df.data[idx], self.df.columns, self.df.index)
                    row = self.df.data[idx]
                    if isinstance(row, dict):
                        class RowObject:
                            def __init__(self, r):
                                self.r = r
                            def __getitem__(self, key):
                                return self.r[key]
                            def get(self, key, default=None):
                                return self.r.get(key, default)
                            def to_dict(self):
                                return dict(self.r)
                        return RowObject(row)
                    return row
            return ILocIndexer(self)

        def tail(self, n):
            return MockDataFrame(self.data[-n:], self.columns, self.index)

    # Inject mock standard libraries into sys.modules
    mock_pandas = MagicMock()
    mock_pandas.DataFrame = MockDataFrame
    mock_pandas.Series = MockSeries
    sys.modules['pandas'] = mock_pandas

    mock_pandas_ta = MagicMock()
    sys.modules['pandas_ta'] = mock_pandas_ta

    mock_numpy = MagicMock()
    mock_numpy.nan = float('nan')
    mock_numpy.log = math.log
    mock_numpy.sqrt = math.sqrt
    sys.modules['numpy'] = mock_numpy

    mock_torch = MagicMock()
    sys.modules['torch'] = mock_torch

    mock_ccxt = MagicMock()
    sys.modules['ccxt'] = mock_ccxt

    mock_plotext = MagicMock()
    sys.modules['plotext'] = mock_plotext

    mock_readchar = MagicMock()
    sys.modules['readchar'] = mock_readchar

    mock_psutil = MagicMock()
    sys.modules['psutil'] = mock_psutil

    mock_rich = MagicMock()
    sys.modules['rich'] = mock_rich
    sys.modules['rich.console'] = mock_rich
    sys.modules['rich.live'] = mock_rich
    sys.modules['rich.table'] = mock_rich
    sys.modules['rich.progress'] = mock_rich
    sys.modules['rich.layout'] = mock_rich
    sys.modules['rich.panel'] = mock_rich
    sys.modules['rich.logging'] = mock_rich
    sys.modules['rich.text'] = mock_rich

# Mock indicators2.get_signals to return price-derived buy/sell signals
def mock_get_signals(df_candles, settings, is_scan=True, global_config=None):
    strategy = settings.get('strategy')
    N = len(df_candles)

    closes = []
    for i in range(N):
        closes.append(float(df_candles.iloc[i].get('close')))

    buy_signals = [False] * N
    sell_signals = [False] * N

    if strategy == 'ichimoku_cloud':
        # Trend following: buy if close is above average of last 5 candles, sell if below
        for i in range(5, N):
            avg = sum(closes[i-5:i]) / 5
            if closes[i] > avg:
                buy_signals[i] = True
            else:
                sell_signals[i] = True

    elif strategy == 'williams_r':
        # Mean reversion: buy if close is below the lowest close of last 10 candles, sell if above
        for i in range(10, N):
            sub = closes[i-10:i]
            low = min(sub)
            high = max(sub)
            if closes[i] <= low:
                buy_signals[i] = True
            elif closes[i] >= high:
                sell_signals[i] = True

    elif strategy == 'vwap_momentum':
        # Momentum: buy if price rising over last 3 candles, sell if falling
        for i in range(3, N):
            if closes[i] > closes[i-3]:
                buy_signals[i] = True
            elif closes[i] < closes[i-3]:
                sell_signals[i] = True

    elif strategy == 'pairs_trading_proxy':
        # Periodic oscillation
        for i in range(N):
            if i % 12 < 4:
                buy_signals[i] = True
            elif i % 12 >= 8:
                sell_signals[i] = True

    # Return using the appropriate DataFrame class
    if PANDAS_INSTALLED:
        return pd.DataFrame({'buy_signal': buy_signals, 'sell_signal': sell_signals, 'close': closes})
    else:
        return MockDataFrame({'buy_signal': buy_signals, 'sell_signal': sell_signals, 'close': closes},
                             columns=['buy_signal', 'sell_signal', 'close'])

# Inject indicators2 get_signals mock
if not PANDAS_INSTALLED:
    mock_indicators2 = MagicMock()
    mock_indicators2.get_signals = mock_get_signals
    sys.modules['indicators2'] = mock_indicators2
else:
    # If pandas is installed, we can patch indicators2.get_signals
    import indicators2
    indicators2.get_signals = mock_get_signals

# Import real functions from strategy_aggregator & backtest (promoting code reuse)
from strategy_aggregator import aggregate_signals
from backtest import fetch_ohlcv_data

# 2. Mock startup file generation functions
def create_mock_startup_files():
    balance_data = {
        "free": {
            "USD": 10000.0,
            "EUR": 10000.0,
            "BTC": 0.05,
            "ETH": 0.5
        },
        "timestamp": int(time.time())
    }
    with open("balance.json", "w") as f:
        json.dump(balance_data, f, indent=4)

    volumes_data = [
        {"symbol": "BTC/USD", "id": "BTCUSD", "trades_count": 1000, "timestamp": int(time.time())},
        {"symbol": "ETH/USD", "id": "ETHUSD", "trades_count": 800, "timestamp": int(time.time())},
        {"symbol": "LTC/USD", "id": "LTCUSD", "trades_count": 750, "timestamp": int(time.time())},
        {"symbol": "XRP/USD", "id": "XRPUSD", "trades_count": 900, "timestamp": int(time.time())},
        {"symbol": "ADA/USD", "id": "ADAUSD", "trades_count": 650, "timestamp": int(time.time())},
        {"symbol": "SOL/USD", "id": "SOLUSD", "trades_count": 850, "timestamp": int(time.time())},
        {"symbol": "DOT/USD", "id": "DOTUSD", "trades_count": 700, "timestamp": int(time.time())},
        {"symbol": "DOGE/USD", "id": "DOGEUSD", "trades_count": 950, "timestamp": int(time.time())},
        {"symbol": "AVAX/USD", "id": "AVAXUSD", "trades_count": 620, "timestamp": int(time.time())},
        {"symbol": "LINK/USD", "id": "LINKUSD", "trades_count": 680, "timestamp": int(time.time())}
    ]
    with open("volumes_trades_data.json", "w") as f:
        json.dump(volumes_data, f, indent=4)

    markets_data = {
        "BTC/USD": {
            "symbol": "BTC/USD", "id": "BTCUSD", "base": "BTC", "quote": "USD",
            "limits": {"amount": {"min": 0.0001}}, "precision": {"price": 0.01, "amount": 0.00001}
        },
        "ETH/USD": {
            "symbol": "ETH/USD", "id": "ETHUSD", "base": "ETH", "quote": "USD",
            "limits": {"amount": {"min": 0.001}}, "precision": {"price": 0.01, "amount": 0.0001}
        },
        "LTC/USD": {
            "symbol": "LTC/USD", "id": "LTCUSD", "base": "LTC", "quote": "USD",
            "limits": {"amount": {"min": 0.01}}, "precision": {"price": 0.01, "amount": 0.001}
        },
        "XRP/USD": {
            "symbol": "XRP/USD", "id": "XRPUSD", "base": "XRP", "quote": "USD",
            "limits": {"amount": {"min": 1.0}}, "precision": {"price": 0.0001, "amount": 1.0}
        },
        "ADA/USD": {
            "symbol": "ADA/USD", "id": "ADAUSD", "base": "ADA", "quote": "USD",
            "limits": {"amount": {"min": 1.0}}, "precision": {"price": 0.0001, "amount": 1.0}
        },
        "SOL/USD": {
            "symbol": "SOL/USD", "id": "SOLUSD", "base": "SOL", "quote": "USD",
            "limits": {"amount": {"min": 0.1}}, "precision": {"price": 0.01, "amount": 0.1}
        },
        "DOT/USD": {
            "symbol": "DOT/USD", "id": "DOTUSD", "base": "DOT", "quote": "USD",
            "limits": {"amount": {"min": 0.1}}, "precision": {"price": 0.01, "amount": 0.1}
        },
        "DOGE/USD": {
            "symbol": "DOGE/USD", "id": "DOGEUSD", "base": "DOGE", "quote": "USD",
            "limits": {"amount": {"min": 1.0}}, "precision": {"price": 0.00001, "amount": 1.0}
        },
        "AVAX/USD": {
            "symbol": "AVAX/USD", "id": "AVAXUSD", "base": "AVAX", "quote": "USD",
            "limits": {"amount": {"min": 0.1}}, "precision": {"price": 0.01, "amount": 0.1}
        },
        "LINK/USD": {
            "symbol": "LINK/USD", "id": "LINKUSD", "base": "LINK", "quote": "USD",
            "limits": {"amount": {"min": 0.1}}, "precision": {"price": 0.01, "amount": 0.1}
        }
    }
    with open("markets.json", "w") as f:
        json.dump(markets_data, f, indent=4)

def generate_synthetic_candles():
    base_prices = {
        "BTCUSD": 60000.0,
        "ETHUSD": 3000.0,
        "LTCUSD": 80.0,
        "XRPUSD": 0.5,
        "ADAUSD": 0.4,
        "SOLUSD": 140.0,
        "DOTUSD": 6.0,
        "DOGEUSD": 0.12,
        "AVAXUSD": 25.0,
        "LINKUSD": 15.0
    }

    start_ts = int(time.time() - 200 * 60) * 1000
    for pair_id, base_price in base_prices.items():
        candles = []
        prev_close = base_price
        for i in range(200):
            ts = start_ts + i * 60000
            factor = 1.0 + 0.05 * math.sin(i / 15.0) + 0.02 * math.cos(i / 5.0) + 0.0001 * i
            close_price = base_price * factor
            open_price = prev_close
            high_price = max(open_price, close_price) * 1.002
            low_price = min(open_price, close_price) * 0.998
            volume = 10000 + (i % 10) * 500
            candles.append([ts, open_price, high_price, low_price, close_price, volume])
            prev_close = close_price

        file_name = f"ohlcv_data_{pair_id}_1m.json"
        with open(file_name, "w") as f:
            json.dump(candles, f, indent=4)

# 4. Implement Profit Backtester
def run_backtest_profit(df_candles, global_buy, global_sell, tailed_candles):
    N = len(df_candles)
    tail_len = min(tailed_candles, N)
    start_idx = N - tail_len

    position = "flat"  # "flat" or "long"
    buy_price = 0.0
    total_profit = 0.0

    for idx in range(start_idx, N):
        close_price = float(df_candles.iloc[idx].get('close'))
        is_buy = global_buy[idx]
        is_sell = global_sell[idx]

        if is_buy and is_sell:
            continue

        if position == "flat" and is_buy:
            position = "long"
            buy_price = close_price
        elif position == "long" and is_sell:
            profit_pct = (close_price - buy_price) / buy_price
            total_profit += profit_pct * 100.0  # express as %
            position = "flat"

    if position == "long":
        close_price = float(df_candles.iloc[N-1].get('close'))
        profit_pct = (close_price - buy_price) / buy_price
        total_profit += profit_pct * 100.0

    return total_profit

# 5. Implement Parameter Sweep (Benchmark)
def run_benchmark():
    # Setup mock files
    create_mock_startup_files()
    generate_synthetic_candles()

    import symbols_utils
    balance = json.load(open("balance.json", "r"))
    available_pairs = symbols_utils.computeSymbols(
        balance=balance,
        previousPairs=None,
        source_assets=[],
        forbid_assets=['USDT'],
        base_assets=["USD", "EUR"],
        max_num_pairs=50,
        mini_count=600
    )

    print(f"\nStartup Phase selected {len(available_pairs)} trading pairs:")
    for pair in available_pairs:
        print(f"  - {pair[0]} (id: {pair[1]})")

    # Pre-load candle data
    candles_by_pair = {}
    for pair in available_pairs:
        symbol = pair[0]
        _id = pair[1]
        df_candles = fetch_ohlcv_data(_id, symbol)
        if df_candles is not None and len(df_candles) > 0:
            candles_by_pair[symbol] = df_candles

    # Pre-compute signal frames and consecutive counts to make the sweep ultra-fast (500x speedup)
    print("\nPre-computing signal frames and consecutive counts for all pairs...")
    import indicators2
    get_signals = indicators2.get_signals
    from strategy_aggregator import load_config, consecutive_count
    cfg = load_config()
    STRATS = ['ichimoku_cloud', 'williams_r', 'vwap_momentum', 'pairs_trading_proxy']

    # Grid parameters definition
    windows = [20, 30]
    score_buys = [2, 3]
    score_sells = [2, 3]
    tailed_values = [120]

    # Sub-strategy buy/sell thresholds grids
    ichimoku_buys = [3, 4]
    ichimoku_sells = [3, 4]
    williams_buys = [2, 3]
    williams_sells = [2, 3]
    vwap_buys = [5, 6]
    vwap_sells = [5, 6]
    pairs_buys = [2, 3]
    pairs_sells = [2, 3]

    # Pre-computed lookup dictionary: pre_computed[symbol][strat][window]['buy'/'sell'] -> list
    pre_computed = {}
    for symbol, df_candles in candles_by_pair.items():
        pre_computed[symbol] = {}
        for strat in STRATS:
            pre_computed[symbol][strat] = {}
            settings = {'strategy': strat, 'device': None}
            df_sign = get_signals(df_candles.copy(), settings, is_scan=True, global_config=cfg)
            if df_sign is None:
                if PANDAS_INSTALLED:
                    df_sign = pd.DataFrame(index=df_candles.index)
                else:
                    df_sign = MockDataFrame(index=df_candles.index)

            buys_raw = df_sign.get('buy_signal').tolist()
            sells_raw = df_sign.get('sell_signal').tolist()

            # Pre-compute for each window in grid + 60
            for w in windows + [60]:
                pre_computed[symbol][strat][w] = {
                    'buy': consecutive_count(buys_raw, window=w),
                    'sell': consecutive_count(sells_raw, window=w)
                }

    results = []

    print("\nRunning offline performance parameter sweep...")
    total_combinations = (
        len(windows) * len(score_buys) * len(score_sells) * len(tailed_values) *
        len(ichimoku_buys) * len(ichimoku_sells) *
        len(williams_buys) * len(williams_sells) *
        len(vwap_buys) * len(vwap_sells) *
        len(pairs_buys) * len(pairs_sells)
    )
    print(f"Total parameter combinations to test: {total_combinations}")

    start_time = time.time()
    for w in windows:
        for sb in score_buys:
            for ss in score_sells:
                for t in tailed_values:
                    for ib in ichimoku_buys:
                        for is_ in ichimoku_sells:
                            for wb in williams_buys:
                                for ws in williams_sells:
                                    for vb in vwap_buys:
                                        for vs in vwap_sells:
                                            for pb in pairs_buys:
                                                for ps in pairs_sells:
                                                    pair_profits = []
                                                    for symbol, df_candles in candles_by_pair.items():
                                                        N = len(df_candles)

                                                        # Lookups from pre-computed consecutive count arrays
                                                        ichimoku_buys_cc = pre_computed[symbol]['ichimoku_cloud'][w]['buy']
                                                        ichimoku_sells_cc = pre_computed[symbol]['ichimoku_cloud'][w]['sell']

                                                        williams_buys_cc = pre_computed[symbol]['williams_r'][w]['buy']
                                                        williams_sells_cc = pre_computed[symbol]['williams_r'][w]['sell']

                                                        vwap_buys_cc = pre_computed[symbol]['vwap_momentum'][w]['buy']
                                                        vwap_sells_cc = pre_computed[symbol]['vwap_momentum'][w]['sell']

                                                        pairs_buys_cc = pre_computed[symbol]['pairs_trading_proxy'][60]['buy']
                                                        pairs_sells_cc = pre_computed[symbol]['pairs_trading_proxy'][60]['sell']

                                                        # Fast scoring loop
                                                        global_buy = [False] * N
                                                        global_sell = [False] * N
                                                        for i in range(N):
                                                            score_buy = 0.0
                                                            score_sell = 0.0

                                                            # 1) ichimoku_cloud (à l'envers)
                                                            if ichimoku_buys_cc[i] >= is_:
                                                                score_sell += 1
                                                            if ichimoku_sells_cc[i] >= ib:
                                                                score_buy += 1

                                                            # 2) williams_r
                                                            if williams_buys_cc[i] >= wb:
                                                                score_buy += 1
                                                            if williams_sells_cc[i] >= ws:
                                                                score_sell += 1

                                                            # 3) vwap_momentum (à l'envers)
                                                            if vwap_buys_cc[i] >= vs:
                                                                score_sell += 1
                                                            if vwap_sells_cc[i] >= vb:
                                                                score_buy += 1

                                                            # 4) pairs_trading_proxy
                                                            if pairs_buys_cc[i] >= pb:
                                                                score_buy += 1
                                                            if pairs_sells_cc[i] >= ps:
                                                                score_sell += 1

                                                            global_buy[i] = (score_buy >= sb)
                                                            global_sell[i] = (score_sell >= ss)

                                                        # Calculate profit on tailed subset
                                                        profit = run_backtest_profit(df_candles, global_buy, global_sell, t)
                                                        pair_profits.append(profit)

                                                    # Sort profits descending and take top 10 (or all if < 10)
                                                    pair_profits.sort(reverse=True)
                                                    top_10 = pair_profits[:10]
                                                    avg_profit = sum(top_10) / len(top_10) if top_10 else 0.0

                                                    results.append({
                                                        'window': w,
                                                        'score_buy': sb,
                                                        'score_sell': ss,
                                                        'tailed': t,
                                                        'ichimoku_buy': ib,
                                                        'ichimoku_sell': is_,
                                                        'williams_buy': wb,
                                                        'williams_sell': ws,
                                                        'vwap_buy': vb,
                                                        'vwap_sell': vs,
                                                        'pairs_buy': pb,
                                                        'pairs_sell': ps,
                                                        'avg_profit': avg_profit
                                                    })

    duration = time.time() - start_time
    print(f"Sweep completed in {duration:.2f} seconds.")

    # Sort results to identify the most profitable combinations
    results.sort(key=lambda x: x['avg_profit'], reverse=True)

    # Print top 10 most profitable combinations
    print("\n=================================== TOP 10 MOST PROFITABLE PARAMETER COMBINATIONS ===================================")
    header = (
        f"{'Rank':<5} | {'Win':<4} | {'Sc Buy':<6} | {'Sc Sell':<7} | {'Tail':<5} | "
        f"{'Ichi B/S':<8} | {'Will B/S':<8} | {'Vwap B/S':<8} | {'Pairs B/S':<9} | {'Avg Top 10 Profit (%)':<22}"
    )
    print(header)
    print("-" * len(header))
    for idx, res in enumerate(results[:10]):
        row = (
            f"{idx+1:<5} | {res['window']:<4} | {res['score_buy']:<6} | {res['score_sell']:<7} | {res['tailed']:<5} | "
            f"{res['ichimoku_buy']}/{res['ichimoku_sell']:<6} | {res['williams_buy']}/{res['williams_sell']:<6} | "
            f"{res['vwap_buy']}/{res['vwap_sell']:<6} | {res['pairs_buy']}/{res['pairs_sell']:<7} | {res['avg_profit']:<22.2f}%"
        )
        print(row)

    # Analyze and identify the tightest profitable window values
    max_profit = results[0]['avg_profit']
    profitable_threshold = max_profit * 0.95

    good_combinations = [r for r in results if r['avg_profit'] >= profitable_threshold]
    # find the smallest window among good combinations
    good_combinations.sort(key=lambda x: x['window'])
    tightest_window_combination = good_combinations[0]

    print("\n================ TIGHTEST HIGHLY PROFITABLE COMBINATION ================")
    print(f"The tightest (smallest) window value achieving high profitability (>= 95% of max profit) is:")
    print(f"  - Window: {tightest_window_combination['window']}")
    print(f"  - Score Buy Threshold: {tightest_window_combination['score_buy']}")
    print(f"  - Score Sell Threshold: {tightest_window_combination['score_sell']}")
    print(f"  - Tailed (Buffered candles): {tightest_window_combination['tailed']} minutes")
    print(f"  - Ichimoku Buy/Sell Thresholds: {tightest_window_combination['ichimoku_buy']}/{tightest_window_combination['ichimoku_sell']}")
    print(f"  - Williams %R Buy/Sell Thresholds: {tightest_window_combination['williams_buy']}/{tightest_window_combination['williams_sell']}")
    print(f"  - VWAP Momentum Buy/Sell Thresholds: {tightest_window_combination['vwap_buy']}/{tightest_window_combination['vwap_sell']}")
    print(f"  - Pairs Trading Buy/Sell Thresholds: {tightest_window_combination['pairs_buy']}/{tightest_window_combination['pairs_sell']}")
    print(f"  - Average Top 10 Profit: {tightest_window_combination['avg_profit']:.2f}%")

if __name__ == '__main__':
    run_benchmark()

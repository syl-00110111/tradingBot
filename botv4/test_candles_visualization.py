#!/usr/bin/env python3
"""
Independently executable test script for visualizing full candle lengths of any trading pair.
Usage:
  python test_candles_visualization.py --symbol BTC/USD
  python test_candles_visualization.py --mock
"""

import os
import sys
import json
import argparse
from datetime import datetime

# Initialize and check plotext and pandas
try:
    import pandas as pd
except ImportError:
    print("Error: pandas is required to run this script. Please install it using 'pip install pandas'.")
    sys.exit(1)

try:
    import plotext as plt
except ImportError:
    print("Error: plotext is required to run this script. Please install it using 'pip install plotext'.")
    sys.exit(1)


def generate_mock_candles(length=50):
    """Generate high-quality mock candlestick data for self-contained execution."""
    import random
    candles = []
    start_ts = int(time_current_ms() - length * 60 * 1000)
    price = 50000.0
    for i in range(length):
        ts = start_ts + i * 60 * 1000
        change = random.uniform(-500.0, 500.0)
        open_price = price
        close_price = price + change
        high_price = max(open_price, close_price) + random.uniform(0, 300.0)
        low_price = min(open_price, close_price) - random.uniform(0, 300.0)
        volume = random.uniform(10.0, 150.0)
        candles.append([ts, open_price, high_price, low_price, close_price, volume])
        price = close_price
    return candles


def time_current_ms():
    import time
    return int(time.time() * 1000)


def load_candles(symbol, pair_id, mock_mode=False, file_path=None):
    """Loads candle data from file, live exchange fallback, or mock data."""
    if mock_mode:
        print("Generating mock candle data (mock mode enabled)...")
        data = generate_mock_candles()
        return pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']), "Mock Data"

    # Determine filename
    if file_path:
        dataFile = file_path
    else:
        # standard filename templates
        sanitized_id = pair_id if pair_id else symbol.replace('/', '_')
        dataFile = f"ohlcv_data_{sanitized_id}_1m.json"

    if os.path.exists(dataFile):
        print(f"Loading full candles from cached file: {dataFile}")
        try:
            with open(dataFile, 'r') as f:
                data = json.load(f)
            if not isinstance(data, list) or len(data) == 0:
                raise ValueError("Cached file is empty or invalid.")
            df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            return df, f"Cached Cache ({dataFile})"
        except Exception as e:
            print(f"Failed to read from cache '{dataFile}': {e}")

    # Fallback to fetching live candles if exchange and api.json are available
    if os.path.exists('api.json'):
        print(f"No cache file found. Attempting live fetch for {symbol}...")
        try:
            import ccxt
            with open('api.json', 'r') as f:
                api_creds = json.load(f)
            exchange_id = api_creds.get('exchange_id')
            options = api_creds.get('options', {})
            defaultType = options.get('defaultType', 'spot')
            exchange_class = getattr(ccxt, exchange_id)
            exchange = exchange_class({
                'apiKey': api_creds.get('api_key'),
                'secret': api_creds.get('api_secret'),
                'enableRateLimit': True,
                'options': {'defaultType': defaultType}
            })
            print(f"Fetching candles from {exchange_id}...")
            raw_candles = exchange.fetch_ohlcv(symbol, '1m')
            if raw_candles:
                df = pd.DataFrame(raw_candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                # Cache them for future runs
                with open(dataFile, 'w') as f:
                    json.dump(raw_candles, f, indent=4)
                print(f"Cached fetched candles to {dataFile}")
                return df, f"Live Exchange ({exchange_id})"
        except Exception as e:
            print(f"Failed to fetch live candles: {e}")

    # Final fallback to Mock Data
    print("No cache or live exchange available. Falling back to mock data...")
    data = generate_mock_candles()
    return pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']), "Fallback Mock Data"


def visualize_candles(df, title):
    """Draws candlestick chart + volume bars using plotext."""
    if df is None or df.empty:
        print("No candles available to visualize.")
        return

    length = len(df)
    print(f"Visualizing full length of candle data. Total candles: {length}")

    timestamps = df['timestamp'].astype(int).tolist()
    opens = df['open'].astype(float).tolist()
    highs = df['high'].astype(float).tolist()
    lows = df['low'].astype(float).tolist()
    closes = df['close'].astype(float).tolist()
    volumes = df['volume'].astype(float).tolist()
    dates = [datetime.fromtimestamp(int(ts) / 1000).strftime('%d/%m %H:%M') for ts in timestamps]

    try:
        plt.clf()
    except AttributeError:
        plt.clear_figure()

    plt.theme('dark')
    plt.title(f"{title} - Full Length ({length} candles)")
    plt.xlabel('Date')
    plt.ylabel('Price')

    data = {"Open": opens, "High": highs, "Low": lows, "Close": closes}
    x = list(range(len(dates)))
    plt.candlestick(x, data)

    # Draw volume bars as vertical lines anchored below the candles
    max_volume = max(volumes) if volumes else 1
    min_price = min(lows) if lows else 0
    max_price = max(highs) if highs else 1
    price_range = max_price - min_price if max_price != min_price else max_price

    base = min_price - price_range * 0.02
    height_factor = price_range * 0.35  # occupying ~35% of the height

    for i, v in enumerate(volumes):
        h = (v / max_volume) * height_factor if max_volume else 0
        plt.plot([i, i], [base, base + h], color='yellow')

    # Sample x ticks for readability
    step = max(1, len(dates) // 8)
    x_ticks = x[::step]
    x_labels = [dates[idx] for idx in x_ticks]
    plt.xticks(x_ticks, x_labels)

    plt.show()


def main():
    parser = argparse.ArgumentParser(description="Full candles length visualization tool.")
    parser.add_argument('--symbol', type=str, default='BTC/USD', help="Trading pair symbol (e.g. BTC/USD)")
    parser.add_argument('--id', type=str, default='', help="Trading pair ID used in file name (e.g. BTC_USD or XLTCZEUR)")
    parser.add_argument('--mock', action='store_true', help="Force mock data generation")
    parser.add_argument('--file', type=str, default='', help="Direct path to an ohlcv JSON file")
    args = parser.parse_args()

    df, source_info = load_candles(args.symbol, args.id, args.mock, args.file)
    visualize_candles(df, f"Candles: {args.symbol} from {source_info}")


if __name__ == '__main__':
    main()

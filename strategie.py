import argparse
from datetime import datetime
import pandas as pd
import plotext as plt
from rich.console import Console
console = Console()

from backtest import fetch_ohlcv_data
from strategy_aggregator import aggregate_signals, load_config


def main(symbol: str, _id: str):
    cfg = load_config()
    # fetch OHLCV via backtest helper (it will cache to ohlcv_data_{symbol}_1m.json)
    # use sanitized id for filename
    if _id is None:
        _id = symbol.replace('/', '')
    df_candles = fetch_ohlcv_data(_id, symbol).tail(100)  # (TODO TEST variance temporelle 100 minutes)
    if df_candles is None or len(df_candles) == 0:
        console.print(f"[red]No candle data for {symbol}[/red]")
        return

    res = aggregate_signals(df_candles, global_config=cfg)
    N = res['N']
    global_buy = res['global_buy']
    global_sell = res['global_sell']

    timestamps = df_candles['timestamp'].astype(int).tolist()
    opens = df_candles['open'].astype(float).tolist()
    highs = df_candles['high'].astype(float).tolist()
    lows = df_candles['low'].astype(float).tolist()
    closes = df_candles['close'].astype(float).tolist()
    volumes = df_candles['volume'].astype(float).tolist()
    dates = [datetime.fromtimestamp(int(ts) / 1000).strftime('%d/%m %H:%M') for ts in timestamps]

    plt.clf()
    # single subplot: draw candlesticks and volumes on the same axes
    plt.subplots(1, 1)
    plt.theme('dark')
    plt.title(f'Strategie agrégée pour {symbol}')
    plt.subplot(1, 1)
    plt.xlabel('Date')
    plt.ylabel('Prix (USD)')

    data = {"Open": opens, "High": highs, "Low": lows, "Close": closes}
    x = list(range(len(dates)))
    plt.candlestick(x, data)
    # draw volumes on the same subplot as short vertical lines anchored below the candles
    max_volume = max(volumes) if volumes else 1
    min_price = min(lows) if lows else 0
    max_price = max(highs) if highs else 1
    price_range = max_price - min_price if max_price != min_price else max_price
    # base position below the lowest low, and a height factor for volumes
    base = min_price - price_range * 0.02
    height_factor = price_range * 0.64
    # draw vertical dots for each volume data
    for i, v in enumerate(volumes):
        h = (v / max_volume) * height_factor if max_volume else 0
        plt.plot([i, i], [base, base + h], color='yellow')

    buy_positions = [i for i, v in enumerate(global_buy) if v]
    sell_positions = [i for i, v in enumerate(global_sell) if v]
    buy_prices = [closes[i] for i in buy_positions]
    sell_prices = [closes[i] for i in sell_positions]
    if buy_positions:
        plt.scatter(buy_positions, buy_prices, marker='x', color='green')
    if sell_positions:
        plt.scatter(sell_positions, sell_prices, marker='o', color='red')

    # set xticks so both series share the same abscisse
    step = max(1, len(dates) // 8)
    x_ticks = x[::step]
    x_labels = [dates[i] for i in x_ticks]
    plt.xticks(x_ticks, x_labels)

    plt.show()

    # draw second time pour bien voir
    if sell_positions:
        plt.scatter(sell_positions, sell_prices, marker='o', color='red')
    if buy_positions:
        plt.scatter(buy_positions, buy_prices, marker='x', color='green')

    # set xticks so both series share the same abscisse
    step = max(1, len(dates) // 8)
    x_ticks = x[::step]
    x_labels = [dates[i] for i in x_ticks]
    plt.xticks(x_ticks, x_labels)

    plt.show()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('symbol', help='Trading pair symbol, e.g. LTC/EUR.')
    parser.add_argument('id', help='Trading pair id, e.g. XLTC/ZEUR.')
    args = parser.parse_args()
    main(args.symbol, args.id)

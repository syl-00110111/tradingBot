import json
import pandas as pd
import logging
from bot import run_backtest_logic, load_config
from exchange_handler import MockExchange
from trading_engine import TradingEngine
from indicators import STRATEGIES

# Disable logging to keep output clean
logging.getLogger().setLevel(logging.ERROR)

def debug_bench():
    config = load_config()
    exchange = MockExchange()
    engine = TradingEngine(config)

    symbol = "PEPE/EUR"
    term = "short"

    # Fetch data - using limit 1000 for more history
    print(f"Fetching data for {symbol}...")
    ohlcv = exchange.fetch_ohlcv(symbol, "1m", limit=1000)
    if not ohlcv:
        print("Failed to fetch data.")
        return
    df_all = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df_all['timestamp'] = pd.to_datetime(df_all['timestamp'], unit='ms')

    print(f"Got {len(df_all)} candles.")

    for strategy in STRATEGIES[:5]: # Test first 5 strategies
        for aggr in ["dynamic"]:
            res = run_backtest_logic(exchange, symbol, strategy, aggr, config, term=term, df_in=df_all, engine=engine)
            if res:
                print(f"Strategy: {strategy:25} | Profit: {res['profit']:8.4f} | Trades: {res['trades_count']:3}")
            else:
                print(f"Strategy: {strategy:25} | Failed")

if __name__ == "__main__":
    debug_bench()

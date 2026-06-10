import ccxt
import pandas as pd

def test_fetch():
    ex = ccxt.binance()
    try:
        ohlcv = ex.fetch_ohlcv("PEPE/EUR", "1m", limit=100)
        print(f"Fetched {len(ohlcv)} candles for PEPE/EUR")
    except Exception as e:
        print(f"Error: {e}")

    try:
        ohlcv = ex.fetch_ohlcv("BTC/EUR", "1m", limit=100)
        print(f"Fetched {len(ohlcv)} candles for BTC/EUR")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_fetch()

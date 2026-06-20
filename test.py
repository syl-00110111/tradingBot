import asyncio
import json
import os
import ccxt.pro as ccxtpro

async def main():
    # 1. Load API credentials
    if not os.path.exists('api.json'):
        print("Error: api.json not found.")
        return

    with open('api.json', 'r') as f:
        api_creds = json.load(f)

    api_key = api_creds.get('api_key')
    api_secret = api_creds.get('api_secret')
    exchange_id = api_creds.get('exchange', 'binance')
    market_type = api_creds.get('market', 'spot')

    # 2. Initialize exchange
    exchange_class = getattr(ccxtpro, exchange_id)
    exchange = exchange_class({
        'apiKey': api_key,
        'secret': api_secret,
        'enableRateLimit': True,
        'options': {
            'defaultType': market_type,
        },
    })

    try:
        # 3. fetchBalance
        print("\n--- Fetching Balance ---")
        balance = await exchange.fetch_balance()
        print(balance)

        # 4. Load pairs
        if not os.path.exists('pairs.txt'):
            print("Error: pairs.txt not found.")
            return

        with open('pairs.txt', 'r') as f:
            pairs = [line.strip() for line in f if line.strip()]

        # 5. fetchOHLCV (60 candles per pair)
        print("\n--- Fetching 60 OHLCV candles for each pair ---")
        for symbol in pairs:
            try:
                ohlcv = await exchange.fetch_ohlcv(symbol, timeframe='1m', limit=60)
                print(f"\nOHLCV for {symbol}:")
                print(ohlcv)
            except Exception as e:
                print(f"Error fetching OHLCV for {symbol}: {e}")

        # 6. watchOHLCV (Real-time updates)
        print("\n--- Watching OHLCV in real-time (Ctrl+C to stop) ---")

        # We'll watch all pairs concurrently
        async def watch_pair(symbol):
            while True:
                try:
                    ohlcv = await exchange.watch_ohlcv(symbol, timeframe='1m')
                    print(f"\nNEW Candle update for {symbol}:")
                    print(ohlcv)
                except Exception as e:
                    print(f"Error watching OHLCV for {symbol}: {e}")
                    await asyncio.sleep(5)

        tasks = [watch_pair(symbol) for symbol in pairs]
        await asyncio.gather(*tasks)

    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        await exchange.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")

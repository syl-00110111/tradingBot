import ccxt.pro as ccxtpro
import asyncio
import logging
import time
import pandas as pd
import re

class ExchangeInterface2:
    def __init__(self):
        self.markets = {}

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100): raise NotImplementedError
    async def fetch_ohlcv_10k(self, symbol, timeframe, limit=10000): raise NotImplementedError
    async def watch_ohlcv(self, symbol, timeframe): raise NotImplementedError
    async def watch_ohlcv_for_symbols(self, symbols, timeframe): raise NotImplementedError
    async def watch_balance(self): raise NotImplementedError
    async def watch_orders(self, symbol=None): raise NotImplementedError
    async def create_order(self, symbol, side, amount, price=None): raise NotImplementedError
    async def fetch_ticker(self, symbol): raise NotImplementedError
    async def fetch_balance(self): raise NotImplementedError
    async def load_markets(self): raise NotImplementedError
    async def fetch_trading_fee(self, symbol): raise NotImplementedError
    async def fetch_my_trades(self, symbol, limit=10): raise NotImplementedError
    def amount_to_precision(self, symbol, amount): raise NotImplementedError
    def price_to_precision(self, symbol, price): raise NotImplementedError
    async def close(self): raise NotImplementedError

class CCXTExchange2(ExchangeInterface2):
    def __init__(self, exchange_id, api_key, api_secret, options=None):
        super().__init__()
        config = {
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'options': options or {}
        }
        if 'binance' in exchange_id:
            if 'options' not in config: config['options'] = {}
            config['options']['recvWindow'] = 60000
            config['options']['adjustForTimeDifference'] = True

        self.exchange = getattr(ccxtpro, exchange_id)(config)
        self.exchange_id = exchange_id

    async def load_markets(self):
        self.markets = await self.exchange.load_markets()
        return self.markets

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100):
        try:
            return await asyncio.wait_for(
                self.exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit),
                timeout=30
            )
        except Exception as e:
            logging.error(f"Error fetching OHLCV for {symbol}: {e}")
            return []

    async def fetch_ohlcv_10k(self, symbol, timeframe, limit=10000):
        all_ohlcv = []
        try:
            tf_seconds = self.exchange.parse_timeframe(timeframe)
        except:
            tf_seconds = 1

        duration_ms = limit * tf_seconds * 1000
        since = self.exchange.milliseconds() - duration_ms

        retries = 0
        max_retries = 3

        while len(all_ohlcv) < limit and retries < max_retries:
            fetch_limit = min(1000, limit - len(all_ohlcv))
            try:
                chunk = await asyncio.wait_for(
                    self.exchange.fetch_ohlcv(symbol, timeframe, since, limit=fetch_limit),
                    timeout=20
                )
                if not chunk:
                    if len(all_ohlcv) > 0: break
                    else:
                        retries += 1
                        await asyncio.sleep(1)
                        continue

                all_ohlcv.extend(chunk)
                since = chunk[-1][0] + 1
                retries = 0 # reset on success

                if len(chunk) < fetch_limit:
                    break
            except Exception as e:
                logging.warning(f"Error in fetch_ohlcv_10k for {symbol} (chunk {len(all_ohlcv)}): {e}")
                retries += 1
                await asyncio.sleep(2)

        if len(all_ohlcv) < 100:
            logging.warning(f"[{symbol}] Only {len(all_ohlcv)} candles retrieved. Bot might need more history for accuracy.")

        return all_ohlcv[-limit:]

    async def watch_ohlcv(self, symbol, timeframe):
        while True:
            try:
                candles = await self.exchange.watch_ohlcv(symbol, timeframe)
                if candles:
                    # Some exchanges return a list of candles, some just the latest.
                    # We yield the latest candle or the list for the bot to process.
                    yield candles
            except Exception as e:
                logging.error(f"Error in watch_ohlcv for {symbol}: {e}")
                await asyncio.sleep(1)

    async def watch_ohlcv_for_symbols(self, symbols, timeframe):
        if not isinstance(symbols, list):
            symbols = list(symbols)

        # Multiplexed individual watchers is often more reliable than watchOHLCVForSymbols
        # for high-frequency 1s updates across various exchanges.
        queue = asyncio.Queue()

        async def _worker(symbol):
            try:
                async for candles in self.watch_ohlcv(symbol, timeframe):
                    await queue.put((symbol, candles))
            except Exception as e:
                logging.error(f"Worker error for {symbol}: {e}")

        tasks = [asyncio.create_task(_worker(s)) for s in symbols]
        try:
            while True:
                yield await queue.get()
        finally:
            for t in tasks:
                t.cancel()

    async def watch_balance(self):
        while True:
            try:
                balance = await self.exchange.watch_balance()
                yield balance
            except Exception as e:
                logging.error(f"Error in watch_balance: {e}")
                await asyncio.sleep(1)

    async def watch_orders(self, symbol=None):
        while True:
            try:
                orders = await self.exchange.watch_orders(symbol)
                yield orders
            except Exception as e:
                logging.error(f"Error in watch_orders: {e}")
                await asyncio.sleep(1)

    async def create_order(self, symbol, side, amount, price=None):
        try:
            amount_str = self.exchange.amount_to_precision(symbol, amount)
            amount = float(amount_str)
            if side == 'buy':
                return await self.exchange.create_market_buy_order(symbol, amount)
            else:
                return await self.exchange.create_market_sell_order(symbol, amount)
        except Exception as e:
            logging.error(f"Error creating order for {symbol}: {e}")
            raise e

    async def fetch_ticker(self, symbol):
        try:
            return await self.exchange.fetch_ticker(symbol)
        except Exception as e:
            logging.error(f"Error fetching ticker for {symbol}: {e}")
            return None

    async def fetch_tickers(self, symbols=None):
        try:
            return await self.exchange.fetch_tickers(symbols)
        except Exception as e:
            logging.error(f"Error fetching tickers: {e}")
            return {}

    async def fetch_balance(self):
        try:
            return await self.exchange.fetch_balance()
        except Exception as e:
            logging.error(f"Error fetching balance: {e}")
            return None

    async def fetch_trading_fee(self, symbol):
        try:
            fees = await self.exchange.fetch_trading_fee(symbol)
            return fees.get('taker', 0.001)
        except Exception as e:
            logging.warning(f"Error fetching trading fee for {symbol}: {e}. Defaulting to 0.1%")
            return 0.001

    async def fetch_my_trades(self, symbol, limit=10):
        try:
            return await self.exchange.fetch_my_trades(symbol, limit=limit)
        except Exception as e:
            logging.error(f"Error fetching my trades for {symbol}: {e}")
            return []

    def amount_to_precision(self, symbol, amount):
        return self.exchange.amount_to_precision(symbol, amount)

    def price_to_precision(self, symbol, price):
        return self.exchange.price_to_precision(symbol, price)

    async def close(self):
        await self.exchange.close()

class MockExchange2(ExchangeInterface2):
    def __init__(self, api_key=None, api_secret=None, exchange_id='binance', options=None):
        super().__init__()
        self.balance = {'USDT': 10000.0, 'USDC': 10000.0, 'EUR': 10000.0}
        self.exchange_id = exchange_id
        self.real_exchange = CCXTExchange2(exchange_id, api_key, api_secret, options) if api_key and api_key != "YOUR_API_KEY" else None
        self._balance_initialized = False

    async def _init_balance(self):
        if self._balance_initialized: return
        if self.real_exchange:
            try:
                real_bal = await self.real_exchange.fetch_balance()
                if real_bal and 'total' in real_bal:
                    for asset, amt in real_bal['total'].items():
                        if amt > 0:
                            self.balance[asset] = amt
                logging.info(f"Mock2 virtual balance initialized from real {self.exchange_id} wallet.")
            except Exception as e:
                logging.error(f"Failed to sync virtual balance for Mock2: {e}")
        self._balance_initialized = True

    async def load_markets(self):
        if self.real_exchange:
            self.markets = await self.real_exchange.load_markets()
        return self.markets

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100):
        if self.real_exchange:
            return await self.real_exchange.fetch_ohlcv(symbol, timeframe, since, limit)
        return []

    async def fetch_ohlcv_10k(self, symbol, timeframe, limit=10000):
        if self.real_exchange:
            return await self.real_exchange.fetch_ohlcv_10k(symbol, timeframe, limit)
        return []

    async def watch_ohlcv(self, symbol, timeframe):
        if self.real_exchange:
            async for candles in self.real_exchange.watch_ohlcv(symbol, timeframe):
                yield candles
        else:
            while True:
                await asyncio.sleep(1)
                yield [[time.time()*1000, 100, 105, 95, 102, 1000]]

    async def watch_ohlcv_for_symbols(self, symbols, timeframe):
        if self.real_exchange:
            async for data in self.real_exchange.watch_ohlcv_for_symbols(symbols, timeframe):
                yield data
        else:
            while True:
                for symbol in symbols:
                    yield (symbol, [[time.time()*1000, 100, 105, 95, 102, 1000]])
                await asyncio.sleep(1)

    async def watch_balance(self):
        await self._init_balance()
        while True:
            yield {'free': self.balance, 'total': self.balance, 'timestamp': time.time()*1000}
            await asyncio.sleep(10)

    async def watch_orders(self, symbol=None):
        while True:
            await asyncio.sleep(60)
            yield []

    async def create_order(self, symbol, side, amount, price=None):
        await self._init_balance()
        if not price:
            ticker = await self.fetch_ticker(symbol)
            price = ticker['last']

        base, quote = symbol.split('/')
        cost = amount * price
        fee = cost * 0.001

        if side == 'buy':
            if self.balance.get(quote, 0) >= (cost + fee):
                self.balance[quote] -= (cost + fee)
                self.balance[base] = self.balance.get(base, 0) + amount
                return {'id': 'mock_' + str(time.time()), 'symbol': symbol, 'side': side, 'amount': amount, 'price': price, 'status': 'closed', 'fee': {'cost': fee, 'currency': quote}}
        else:
            if self.balance.get(base, 0) >= amount:
                self.balance[base] -= amount
                self.balance[quote] = self.balance.get(quote, 0) + cost - fee
                return {'id': 'mock_' + str(time.time()), 'symbol': symbol, 'side': side, 'amount': amount, 'price': price, 'status': 'closed', 'fee': {'cost': fee, 'currency': quote}}
        raise Exception("Insufficient balance")

    async def fetch_ticker(self, symbol):
        if self.real_exchange:
            return await self.real_exchange.fetch_ticker(symbol)
        return {'last': 100.0}

    async def fetch_tickers(self, symbols=None):
        if self.real_exchange:
            return await self.real_exchange.fetch_tickers(symbols)
        return {}

    async def fetch_balance(self):
        await self._init_balance()
        return {'free': self.balance, 'total': self.balance}

    async def fetch_trading_fee(self, symbol):
        return 0.001

    async def fetch_my_trades(self, symbol, limit=10):
        if self.real_exchange:
            return await self.real_exchange.fetch_my_trades(symbol, limit)
        return []

    def amount_to_precision(self, symbol, amount):
        return str(amount)

    def price_to_precision(self, symbol, price):
        return str(price)

    async def close(self):
        if self.real_exchange:
            await self.real_exchange.close()

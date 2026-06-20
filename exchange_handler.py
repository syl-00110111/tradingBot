# Cryptocurrencies multiplatform trading bot - Simplified Exchange Interface
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import ccxt.pro as ccxtpro
import asyncio
import os
import logging

class ExchangeInterface:
    async def load_markets(self): raise NotImplementedError
    async def fetch_balance(self): raise NotImplementedError
    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100): raise NotImplementedError
    async def watch_ohlcv(self, symbol, timeframe, since=None, limit=100): raise NotImplementedError
    async def fetch_order_book(self, symbol, limit=20): raise NotImplementedError
    async def create_order(self, symbol, side, amount, price=None): raise NotImplementedError
    async def close(self): raise NotImplementedError

class CCXTExchange(ExchangeInterface):
    def __init__(self, exchange_id, api_key, api_secret, options=None, market_type='spot'):
        ex_class = getattr(ccxtpro, exchange_id)
        default_options = {
            'apiKey': api_key, 'secret': api_secret, 'enableRateLimit': True,
            'options': {'defaultType': market_type},
        }

        proxy_url = os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY') or os.environ.get('proxy')
        if proxy_url:
            if '://' not in proxy_url:
                proxy_url = f'socks5://{proxy_url}'
                if ':' not in proxy_url.split('://')[1]:
                    proxy_url += ':1080'
            default_options['proxies'] = {'http': proxy_url, 'https': proxy_url}

        if options: default_options.update(options)
        self.exchange = ex_class(default_options)

    async def load_markets(self):
        try: return await self.exchange.load_markets()
        except Exception as e: logging.error(f"Failed to load markets: {e}"); return {}

    async def fetch_balance(self):
        try: return await self.exchange.fetch_balance()
        except Exception as e: logging.error(f"Error fetching balance: {e}"); return None

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100):
        try: return await self.exchange.fetch_ohlcv(symbol, timeframe, since, limit)
        except Exception as e: logging.error(f"Error fetching OHLCV for {symbol}: {e}"); return []

    async def watch_ohlcv(self, symbol, timeframe, since=None, limit=100):
        try: return await self.exchange.watch_ohlcv(symbol, timeframe, since, limit)
        except Exception as e: logging.error(f"Error watching OHLCV for {symbol}: {e}"); return None

    async def fetch_order_book(self, symbol, limit=20):
        try: return await self.exchange.fetch_order_book(symbol, limit)
        except Exception as e: logging.error(f"Error fetching order book for {symbol}: {e}"); return None

    async def create_order(self, symbol, side, amount, price=None):
        try:
            if not self.exchange.markets: await self.exchange.load_markets()
            amount_str = self.exchange.amount_to_precision(symbol, amount)
            amount = float(amount_str)
            if side == 'buy': order = await self.exchange.create_market_buy_order(symbol, amount)
            else: order = await self.exchange.create_market_sell_order(symbol, amount)
            return order
        except Exception as e:
            logging.error(f"Error during {side} order on {symbol}: {e}"); return None

    async def close(self):
        await self.exchange.close()

class BinanceExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('binance', api_key, api_secret, market_type=market_type)

class KrakenExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('kraken', api_key, api_secret, market_type=market_type)

class BitvavoExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('bitvavo', api_key, api_secret, market_type=market_type)

class CoinbaseExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('coinbaseexchange', api_key, api_secret, market_type=market_type)

class GeminiExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('gemini', api_key, api_secret, market_type=market_type)

class BitsoExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('bitso', api_key, api_secret, market_type=market_type)

class BitstampExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('bitstamp', api_key, api_secret, market_type=market_type)

class WhiteBITExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('whitebit', api_key, api_secret, market_type=market_type)

class UpbitExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('upbit', api_key, api_secret, market_type=market_type)

class LunoExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('luno', api_key, api_secret, market_type=market_type)

class IndependentReserveExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('independentreserve', api_key, api_secret, market_type=market_type)

EXCHANGE_MAPPING = {
    'binance': BinanceExchange, 'kraken': KrakenExchange, 'bitvavo': BitvavoExchange,
    'coinbase': CoinbaseExchange, 'gemini': GeminiExchange,
    'bitso': BitsoExchange, 'bitstamp': BitstampExchange, 'whitebit': WhiteBITExchange,
    'upbit': UpbitExchange, 'luno': LunoExchange,
    'independentreserve': IndependentReserveExchange
}

class MockExchange(ExchangeInterface):
    def __init__(self, api_key=None, api_secret=None, exchange_type='binance', market_type='spot'):
        self.balance = {'USDT': 1000.0, 'USDC': 1000.0}
        self.markets = {}
        self.exchange_type = exchange_type
        self.market_type = market_type

    async def load_markets(self): return {}
    async def fetch_balance(self): return {'total': self.balance, 'free': self.balance}
    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100): return []
    async def watch_ohlcv(self, symbol, timeframe, since=None, limit=100): return []
    async def fetch_order_book(self, symbol, limit=20): return {'bids': [[99, 1]], 'asks': [[101, 1]]}
    async def create_order(self, symbol, side, amount, price=None):
        return {'id': 'mock', 'status': 'closed', 'price': 100.0, 'amount': amount}
    async def close(self): pass

async def fetch_ohlcv_incremental(exchange, symbol, timeframe, ohlcv_cache_manager, limit=500, since=None):
    # Simplified fallback to standard fetch_ohlcv
    ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
    return ohlcv, 0

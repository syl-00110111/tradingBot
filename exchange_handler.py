# Binance Trading Bot - Exchange Interface
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import ccxt
import time
import logging
import threading
import requests
from requests.adapters import HTTPAdapter

class ThrottledExchange:
    def __init__(self, exchange, delay_ms=42):
        self.exchange = exchange
        self.delay_s = delay_ms / 1000.0
        self.lock = threading.Lock()
        self.last_request_time = 0

    def _wait(self):
        with self.lock:
            now = time.time()
            elapsed = now - self.last_request_time
            if elapsed < self.delay_s:
                time.sleep(self.delay_s - elapsed)
            self.last_request_time = time.time()

    def __getattr__(self, name):
        attr = getattr(self.exchange, name)
        if callable(attr):
            def throttled_wrapper(*args, **kwargs):
                self._wait()
                return attr(*args, **kwargs)
            return throttled_wrapper
        return attr

def create_ccxt_session():
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session

class ExchangeInterface:
    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100): raise NotImplementedError
    def create_order(self, symbol, side, amount, price=None): raise NotImplementedError
    def fetch_balance(self): raise NotImplementedError
    def fetch_ticker(self, symbol): raise NotImplementedError
    def fetch_trading_fee(self, symbol): raise NotImplementedError

class CCXTExchange(ExchangeInterface):
    def __init__(self, exchange_id, api_key, api_secret, options=None):
        ex_class = getattr(ccxt, exchange_id)
        default_options = {
            'apiKey': api_key, 'secret': api_secret, 'enableRateLimit': True,
            'options': {'poolSize': 50},
            'session': create_ccxt_session()
        }
        if options: default_options.update(options)
        self.exchange = ThrottledExchange(ex_class(default_options))

    def load_markets(self):
        try: return self.exchange.load_markets()
        except Exception as e: logging.error(f"Failed to load markets: {e}"); return {}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100):
        try: return self.exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        except Exception as e: logging.error(f"Error fetching OHLCV for {symbol}: {e}"); return None

    def fetch_ticker(self, symbol):
        try: return self.exchange.fetch_ticker(symbol)
        except Exception as e: logging.error(f"Error fetching ticker for {symbol}: {e}"); return None

    def fetch_balance(self):
        try: return self.exchange.fetch_balance()
        except Exception as e: logging.error(f"Error fetching balance: {e}"); return None

    def fetch_my_trades(self, symbol, limit=10):
        try: return self.exchange.fetch_my_trades(symbol, limit=limit)
        except Exception as e: logging.error(f"Error fetching trades for {symbol}: {e}"); return []

    def fetch_trading_fee(self, symbol):
        try:
            fees = self.exchange.fetch_trading_fee(symbol)
            return fees.get('taker', 0.001)
        except Exception as e:
            logging.warning(f"Error fetching trading fee for {symbol}: {e}. Falling back to 0.1%")
            return 0.001

    def create_order(self, symbol, side, amount, price=None):
        try:
            if not self.exchange.markets: self.exchange.load_markets()
            amount_str = self.exchange.amount_to_precision(symbol, amount)
            amount = float(amount_str)
            if side == 'sell':
                base, _ = symbol.split('/')
                balance = self.fetch_balance()
                free_balance = balance.get(base, {}).get('free', 0) if isinstance(balance.get(base), dict) else balance.get(base, 0)
                if free_balance < amount:
                    if free_balance > 0 and (amount - free_balance) / amount < 0.01:
                        amount = float(self.exchange.amount_to_precision(symbol, free_balance))
                    else:
                        logging.warning(f"Aborting sell of {symbol}: Insufficient {base} balance ({free_balance} < {amount})")
                        return None
            if side == 'buy': order = self.exchange.create_market_buy_order(symbol, amount)
            else: order = self.exchange.create_market_sell_order(symbol, amount)
            if order and 'fee' in order and order['fee']:
                fee_cost = order['fee'].get('cost', 0)
                fee_currency = order['fee'].get('currency', '')
                _, quote = symbol.split('/')

                # If fee is in base currency, convert it to quote currency
                if fee_currency != quote and fee_cost > 0:
                    ticker = self.fetch_ticker(symbol)
                    if ticker and ticker.get('last'):
                        fee_cost = fee_cost * ticker['last']

                order['calculated_fee'] = fee_cost
            else:
                 ticker = self.fetch_ticker(symbol)
                 fee_rate = self.fetch_trading_fee(symbol)
                 order['calculated_fee'] = amount * (ticker['last'] if ticker else 0) * fee_rate
            return order
        except Exception as e:
            err_msg = str(e)
            if 'minimum amount precision' in err_msg or 'dust' in err_msg.lower():
                return {'error': 'dust_limit', 'message': err_msg}
            logging.error(f"Error during {side} order on {symbol}: {e}"); return None

class BinanceExchange(CCXTExchange):
    def __init__(self, api_key, api_secret):
        super().__init__('binance', api_key, api_secret, options={'options': {'defaultType': 'spot', 'poolSize': 50}})

class KrakenExchange(CCXTExchange):
    def __init__(self, api_key, api_secret):
        super().__init__('kraken', api_key, api_secret)

class BitvavoExchange(CCXTExchange):
    def __init__(self, api_key, api_secret):
        super().__init__('bitvavo', api_key, api_secret)

class CoinbaseExchange(CCXTExchange):
    def __init__(self, api_key, api_secret):
        super().__init__('coinbaseexchange', api_key, api_secret)

class GeminiExchange(CCXTExchange):
    def __init__(self, api_key, api_secret):
        super().__init__('gemini', api_key, api_secret)

class MercadoBitcoinExchange(CCXTExchange):
    def __init__(self, api_key, api_secret):
        super().__init__('mercado', api_key, api_secret)

class BitsoExchange(CCXTExchange):
    def __init__(self, api_key, api_secret):
        super().__init__('bitso', api_key, api_secret)

class BitstampExchange(CCXTExchange):
    def __init__(self, api_key, api_secret):
        super().__init__('bitstamp', api_key, api_secret)

class WhiteBITExchange(CCXTExchange):
    def __init__(self, api_key, api_secret):
        super().__init__('whitebit', api_key, api_secret)

class IndodaxExchange(CCXTExchange):
    def __init__(self, api_key, api_secret):
        super().__init__('indodax', api_key, api_secret)

class UpbitExchange(CCXTExchange):
    def __init__(self, api_key, api_secret):
        super().__init__('upbit', api_key, api_secret)

class LunoExchange(CCXTExchange):
    def __init__(self, api_key, api_secret):
        super().__init__('luno', api_key, api_secret)

class IndependentReserveExchange(CCXTExchange):
    def __init__(self, api_key, api_secret):
        super().__init__('independentreserve', api_key, api_secret)

class BTCMarketsExchange(CCXTExchange):
    def __init__(self, api_key, api_secret):
        super().__init__('btcmarkets', api_key, api_secret)

class MockExchange(ExchangeInterface):
    def __init__(self, api_key=None, api_secret=None, exchange_type='binance'):
        self.balance = {'USDT': 1000.0, 'USDC': 1000.0}
        self.ohlcv_data = {}
        self.real_exchange = None
        self.fee_rate = 0.001
        self.markets = {}
        self.exchange_type = exchange_type
        self._balance_initialized = False
        if api_key and api_secret and api_key != "YOUR_API_KEY":
            try:
                mapping = {
                    'binance': BinanceExchange, 'kraken': KrakenExchange, 'bitvavo': BitvavoExchange,
                    'coinbase': CoinbaseExchange, 'gemini': GeminiExchange, 'mercado': MercadoBitcoinExchange,
                    'bitso': BitsoExchange, 'bitstamp': BitstampExchange, 'whitebit': WhiteBITExchange,
                    'indodax': IndodaxExchange, 'upbit': UpbitExchange, 'luno': LunoExchange,
                    'independentreserve': IndependentReserveExchange, 'btcmarkets': BTCMarketsExchange
                }
                ex_class = mapping.get(exchange_type, BinanceExchange)
                self.real_exchange = ex_class(api_key, api_secret)
                logging.info(f"Mock initialized with real {exchange_type} balance discovery (deferred)")
            except Exception as e: logging.error(f"Failed to initialize real exchange for Mock: {e}")

    def _init_balance(self):
        if self._balance_initialized: return
        if self.real_exchange:
            try:
                real_bal = self.real_exchange.fetch_balance()
                total = real_bal.get('total', real_bal)
                for asset, amt in total.items():
                    if not isinstance(amt, (int, float)) or amt <= 0: continue
                    # Ignore dust logic here if needed, simplified for now
                    self.balance[asset] = amt
                logging.info(f"Mock virtual balance initialized from real {self.exchange_type} wallet.")
            except Exception as e:
                logging.error(f"Failed to sync virtual balance from real API: {e}")
        self._balance_initialized = True

    def load_markets(self):
        if self.real_exchange:
            try:
                self.markets = self.real_exchange.load_markets()
                return self.markets
            except Exception as e: logging.error(f"Mock failed to load markets: {e}")
        return {}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100):
        if self.real_exchange:
             try: return self.real_exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
             except Exception: pass
        if symbol not in self.ohlcv_data:
            try:
                public_ex = ccxt.binance({'session': create_ccxt_session()})
                return public_ex.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
            except Exception: return []
        return self.ohlcv_data.get(symbol, [])[:limit]

    def fetch_ticker(self, symbol):
        if self.real_exchange:
             try: return self.real_exchange.fetch_ticker(symbol)
             except Exception: pass
        data = self.ohlcv_data.get(symbol, [])
        if data: return {'last': data[-1][4]}
        try:
            public_ex = ccxt.binance({'session': create_ccxt_session()})
            return public_ex.fetch_ticker(symbol)
        except Exception: return {'last': 0.0}

    def fetch_balance(self):
        self._init_balance()
        return {'total': self.balance, 'free': self.balance}

    def fetch_my_trades(self, symbol, limit=10):
        if self.real_exchange:
            try: return self.real_exchange.fetch_my_trades(symbol, limit=limit)
            except Exception: pass
        return []

    def fetch_trading_fee(self, symbol):
        if self.real_exchange:
            try: return self.real_exchange.fetch_trading_fee(symbol)
            except Exception: pass
        return self.fee_rate

    def create_order(self, symbol, side, amount, price=None):
        self._init_balance()
        ticker = self.fetch_ticker(symbol)
        price = ticker['last']
        if price <= 0: return None

        cost = amount * price
        fee_rate = self.fetch_trading_fee(symbol)
        fee = cost * fee_rate
        base, quote = symbol.split('/')

        free_quote = self.balance.get(quote, 0.0)
        free_base = self.balance.get(base, 0.0)

        if side == 'buy':
            if free_quote >= (cost + fee):
                self.balance[quote] = free_quote - (cost + fee)
                self.balance[base] = free_base + amount
                return {'id': 'mock_buy_' + str(time.time()), 'status': 'closed', 'price': price, 'amount': amount, 'calculated_fee': fee}
        else:
            if free_base >= amount:
                self.balance[base] = free_base - amount
                self.balance[quote] = free_quote + cost - fee
                return {'id': 'mock_sell_' + str(time.time()), 'status': 'closed', 'price': price, 'amount': amount, 'calculated_fee': fee}
        return None

# Binance Trading Bot - Exchange Interface
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import ccxt
import time
import logging
import threading
import requests
from requests.adapters import HTTPAdapter

class ThrottledExchange:
    def __init__(self, exchange, delay_ms=2):
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
    adapter = HTTPAdapter(pool_connections=150, pool_maxsize=200)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session

class ExchangeInterface:
    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100): raise NotImplementedError
    def watch_ohlcv(self, symbol, timeframe): raise NotImplementedError
    def create_order(self, symbol, side, amount, price=None): raise NotImplementedError
    def fetch_balances(self): raise NotImplementedError
    def fetch_ticker(self, symbol): raise NotImplementedError
    def fetch_trades(self, symbol, limit=100): raise NotImplementedError
    def fetch_trading_fee(self, symbol): raise NotImplementedError

class BinanceExchange(ExchangeInterface):
    def __init__(self, api_key, api_secret):
        config = {
            'apiKey': api_key, 'secret': api_secret, 'enableRateLimit': True,
            'options': {'defaultType': 'spot', 'poolSize': 50},
            'session': create_ccxt_session()
        }
        self.exchange = ThrottledExchange(ccxt.binance(config))

    def load_markets(self):
        try: return self.exchange.load_markets()
        except Exception as e: logging.error(f"Failed to load markets: {e}"); return {}

    def watch_ohlcv(self, symbol, timeframe):
        # Implementation of watch_ohlcv as a generator
        # Fallback to polling for sync CCXT
        # logging.info(f"Starting watch_ohlcv for {symbol} ({timeframe}) via polling fallback")
        last_candle = None
        while True:
            try:
                ohlcv = self.fetch_ohlcv(symbol, timeframe, limit=5)
                if ohlcv:
                    for candle in ohlcv:
                        # Yield if it's a new candle OR if the current candle has changed data
                        if last_candle is None or candle[0] > last_candle[0] or (candle[0] == last_candle[0] and candle != last_candle):
                            yield candle
                            last_candle = candle
            except Exception as e:
                logging.error(f"Error in watch_ohlcv loop for {symbol}: {e}")
            time.sleep(2)

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100):
        try: return self.exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        except Exception as e: logging.error(f"Error fetching OHLCV for {symbol}: {e}"); return None

    def fetch_ticker(self, symbol):
        # We prefer using fetch_ohlcv for latest price to stay within allowed methods where possible
        ohlcv = self.fetch_ohlcv(symbol, '1m', limit=1)
        if ohlcv: return {'last': ohlcv[0][4]}
        try: return self.exchange.fetch_ticker(symbol)
        except Exception as e: logging.error(f"Error fetching ticker for {symbol}: {e}"); return None

    def fetch_balances(self):
        try:
            return self.exchange.fetch_balance()
        except Exception as e: logging.error(f"Error fetching balances: {e}"); return None

    def fetch_trades(self, symbol, limit=100):
        try: return self.exchange.fetch_trades(symbol, limit=limit)
        except Exception as e: logging.error(f"Error fetching trades for {symbol}: {e}"); return []

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
                balance = self.fetch_balances()
                free_balance = balance.get(base, {}).get('free', 0)
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
                fee_currency = order['fee'].get('currency')
                _, quote = symbol.split('/')

                if fee_currency and fee_currency != quote:
                    # Convert fee to quote currency
                    try:
                        ticker = self.fetch_ticker(f"{fee_currency}/{quote}")
                        if ticker:
                            fee_cost *= ticker['last']
                    except:
                        # Fallback to estimation if conversion pair not found
                        price = price or self.fetch_ticker(symbol).get('last', 0)
                        fee_rate = self.fetch_trading_fee(symbol)
                        fee_cost = amount * price * fee_rate
                order['calculated_fee'] = fee_cost
            elif isinstance(order, dict):
                 price = price or self.fetch_ticker(symbol).get('last', 0)
                 fee_rate = self.fetch_trading_fee(symbol)
                 order['calculated_fee'] = amount * price * fee_rate
            return order
        except Exception as e:
            err_msg = str(e)
            if 'minimum amount precision' in err_msg or 'dust' in err_msg.lower():
                return {'error': 'dust_limit', 'message': err_msg}
            logging.error(f"Error during {side} order on {symbol}: {e}"); return None

class MockExchange(ExchangeInterface):
    def __init__(self, api_key=None, api_secret=None, exchange_type='binance'):
        self.balance = {'EUR': 1000.0, 'USDC': 1000.0, 'USDT': 1000.0}
        self.ohlcv_data = {}
        self.real_exchange = None
        self.fee_rate = 0.001
        self.markets = {}
        self._balance_initialized = False
        if api_key and api_secret and api_key != "YOUR_API_KEY":
            try:
                if exchange_type == 'binance':
                    self.real_exchange = BinanceExchange(api_key, api_secret)
                elif exchange_type == 'kraken':
                    self.real_exchange = KrakenExchange(api_key, api_secret)
                elif exchange_type == 'bitvavo':
                    self.real_exchange = BitvavoExchange(api_key, api_secret)
                logging.info("Mock initialized with real API balance discovery (deferred)")
            except Exception as e: logging.error(f"Failed to initialize real exchange for Mock: {e}")

    def _init_balance(self):
        if self._balance_initialized: return
        if self.real_exchange:
            try:
                real_bal = self.real_exchange.fetch_balances()
                total = real_bal.get('total', {})
                for asset, amt in total.items():
                    if amt <= 0: continue
                    # Ignore dust: value must be > 1.0 in base currency
                    is_dust = False
                    if asset not in ['EUR', 'USDT', 'USDC']:
                        try:
                            # Use fetch_ohlcv for latest price to limit methods
                            ohlcv = self.fetch_ohlcv(f"{asset}/EUR", '1m', limit=1)
                            if ohlcv and (amt * ohlcv[0][4]) < 1.0:
                                is_dust = True
                        except: pass
                    if not is_dust:
                        self.balance[asset] = amt
                logging.info("Mock virtual balance initialized from real wallet (dust ignored).")
            except Exception as e:
                logging.error(f"Failed to sync virtual balance from real API: {e}")
        self._balance_initialized = True

    def load_markets(self):
        if self.real_exchange:
            return self.real_exchange.load_markets()
        return {}

    def watch_ohlcv(self, symbol, timeframe):
        if self.real_exchange:
             yield from self.real_exchange.watch_ohlcv(symbol, timeframe)
        else:
             while True:
                 ohlcv = self.fetch_ohlcv(symbol, timeframe, limit=1)
                 if ohlcv: yield ohlcv[0]
                 time.sleep(10)

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
             return self.real_exchange.fetch_ticker(symbol)
        data = self.ohlcv_data.get(symbol, [])
        if data: return {'last': data[-1][4]}
        try:
            public_ex = ccxt.binance({'session': create_ccxt_session()})
            ohlcv = public_ex.fetch_ohlcv(symbol, '1m', limit=1)
            if ohlcv: return {'last': ohlcv[0][4]}
            return public_ex.fetch_ticker(symbol)
        except Exception: return {'last': 0.0}

    def fetch_balances(self):
        self._init_balance()
        return {'total': self.balance, 'free': self.balance}

    def fetch_trades(self, symbol, limit=100):
        if self.real_exchange:
            return self.real_exchange.fetch_trades(symbol, limit=limit)
        try:
            public_ex = ccxt.binance({'session': create_ccxt_session()})
            return public_ex.fetch_trades(symbol, limit=limit)
        except Exception: return []

    def fetch_my_trades(self, symbol, limit=10):
        if self.real_exchange:
            return self.real_exchange.fetch_my_trades(symbol, limit=limit)
        return []

    def fetch_trading_fee(self, symbol):
        if self.real_exchange:
            return self.real_exchange.fetch_trading_fee(symbol)
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

class KrakenExchange(BinanceExchange):
    def __init__(self, api_key, api_secret):
        config = {
            'apiKey': api_key, 'secret': api_secret, 'enableRateLimit': True,
            'session': create_ccxt_session()
        }
        self.exchange = ThrottledExchange(ccxt.kraken(config))

class BitvavoExchange(BinanceExchange):
    def __init__(self, api_key, api_secret):
        config = {
            'apiKey': api_key, 'secret': api_secret, 'enableRateLimit': True,
            'session': create_ccxt_session()
        }
        self.exchange = ThrottledExchange(ccxt.bitvavo(config))

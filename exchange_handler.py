# CCXT Pro Trading Bot - Exchange Interface
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

"""
Interfaces and handlers for communicating with cryptocurrency exchanges.

This module provides a unified interface for any exchange supported by CCXT
and includes a Mock handler for simulations and testing.
"""

import ccxt
import time
import logging
import threading
import requests
from requests.adapters import HTTPAdapter

class ThrottledExchange:
    """
    Wrapper for CCXT exchange instances to provide rate-limiting/throttling.

    Ensures that requests to the exchange are spaced out by at least a specified
    delay to avoid triggering rate limits.

    Parameters
    ----------
    exchange : ccxt.Exchange
        The underlying CCXT exchange instance.
    delay_ms : int, optional
        Minimum delay between requests in milliseconds (default is 2ms).
    """
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
    """
    Creates a requests Session with a connection pool optimized for frequent API calls.

    Returns
    -------
    requests.Session
        The configured session object.
    """
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=500, pool_maxsize=500)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session

class ExchangeInterface:
    """
    Abstract base class defining the required interface for exchange handlers.

    Developers adding new exchanges should inherit from this class and
    implement all abstract methods.
    """
    def __init__(self):
        self.markets = {}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100): raise NotImplementedError
    def watch_ohlcv(self, symbol, timeframe): raise NotImplementedError
    def create_order(self, symbol, side, amount, price=None): raise NotImplementedError
    def fetch_balances(self): raise NotImplementedError
    def fetch_ticker(self, symbol): raise NotImplementedError
    def fetch_trades(self, symbol, limit=100): raise NotImplementedError
    def fetch_trading_fee(self, symbol): raise NotImplementedError
    def amount_to_precision(self, symbol, amount): raise NotImplementedError

class CCXTExchange(ExchangeInterface):
    """
    Generic exchange handler using CCXT.

    Parameters
    ----------
    exchange_id : str
        The CCXT exchange ID (e.g., 'binance', 'kraken').
    api_key : str
        Exchange API key.
    api_secret : str
        Exchange API secret.
    """
    def __init__(self, exchange_id, api_key, api_secret, options=None):
        super().__init__()
        if not hasattr(ccxt, exchange_id):
            raise ValueError(f"Exchange '{exchange_id}' is not supported by CCXT.")

        exchange_class = getattr(ccxt, exchange_id)
        config = {
            'apiKey': api_key, 'secret': api_secret, 'enableRateLimit': True,
            'options': {'poolSize': 500, 'adjustForTimeDifference': True},
            'session': create_ccxt_session()
        }

        # Increase recvWindow for Binance to handle clock drift/latency
        if 'binance' in exchange_id:
            config['options']['recvWindow'] = 60000

        if options and isinstance(options, dict):
            config['options'].update(options)

        self.exchange = ThrottledExchange(exchange_class(config))
        self.exchange_id = exchange_id

    def load_markets(self):
        try:
            self.markets = self.exchange.load_markets()
            return self.markets
        except Exception as e: logging.error(f"Failed to load markets for {self.exchange_id}: {e}"); return {}

    def watch_ohlcv(self, symbol, timeframe):
        """
        Watches for OHLCV updates using a polling fallback mechanism.
        Preference for real WebSockets (Watch) over Fetch where possible.

        Since the standard CCXT library is synchronous, this method simulates
        a real-time stream by periodically fetching the latest candles and
        yielding new or updated ones.

        Parameters
        ----------
        symbol : str
            The trading pair symbol.
        timeframe : str
            The timeframe to watch.

        Yields
        ------
        list
            A single OHLCV candle [timestamp, open, high, low, close, volume].
        """
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
                err_msg = str(e)
                if "500" in err_msg:
                    # Let it bubble up for suspension handling in bot.py
                    raise e
                logging.error(f"Error in watch_ohlcv loop for {symbol} on {self.exchange_id}: {e}")
            time.sleep(2)

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100):
        try:
            return self.exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        except Exception as e:
            err_msg = str(e)
            # Grouping and clean error reporting
            status_code = "Error"
            import re
            # Catch HTTP status codes (4xx, 5xx)
            code_match = re.search(r'\b([45]\d{2})\b', err_msg)
            if code_match:
                status_code = f"HTTP {code_match.group(1)}"
            elif "timeout" in err_msg.lower():
                status_code = "Timeout"
            elif "not found" in err_msg.lower() or "invalid symbol" in err_msg.lower():
                status_code = "Invalid Symbol"

            if any(code in err_msg for code in ["500", "502", "503", "504"]):
                 # Let it bubble up for grouping in bot.py
                 raise Exception(f"{status_code} Error Code for {symbol} on {self.exchange_id}")

            # For other errors, we log with more context if possible
            clean_err = err_msg.split('{"code"')[0].strip() if '{"code"' in err_msg else err_msg
            if len(clean_err) > 100: clean_err = clean_err[:97] + "..."
            logging.error(f"Error fetching OHLCV for {symbol} on {self.exchange_id}: {status_code} ({clean_err})")
            return None

    def fetch_ticker(self, symbol):
        try: return self.exchange.fetch_ticker(symbol)
        except Exception as e:
            logging.error(f"Error fetching ticker for {symbol} on {self.exchange_id}: {e}")
            # Fallback to fetch_ohlcv for latest price if real fetch_ticker fails
            ohlcv = self.fetch_ohlcv(symbol, '1m', limit=1)
            if ohlcv: return {'last': ohlcv[0][4]}
            return None

    def fetch_balances(self):
        try:
            return self.exchange.fetch_balance()
        except Exception as e:
            err_msg = str(e)
            clean_err = err_msg.split('{"code"')[0].strip() if '{"code"' in err_msg else err_msg
            if len(clean_err) > 100: clean_err = clean_err[:97] + "..."
            logging.error(f"Error fetching balances on {self.exchange_id}: {clean_err}")
            return None

    def fetch_trades(self, symbol, limit=100):
        try: return self.exchange.fetch_trades(symbol, limit=limit)
        except Exception as e: logging.error(f"Error fetching trades for {symbol} on {self.exchange_id}: {e}"); return []

    def fetch_my_trades(self, symbol, limit=10):
        try: return self.exchange.fetch_my_trades(symbol, limit=limit)
        except Exception as e: logging.error(f"Error fetching my trades for {symbol} on {self.exchange_id}: {e}"); return []

    def fetch_trading_fee(self, symbol):
        try:
            fees = self.exchange.fetch_trading_fee(symbol)
            return fees.get('taker', 0.001)
        except Exception as e:
            logging.warning(f"Error fetching trading fee for {symbol} on {self.exchange_id}: {e}. Falling back to 0.1%")
            return 0.001

    def amount_to_precision(self, symbol, amount):
        return self.exchange.amount_to_precision(symbol, amount)

    def create_order(self, symbol, side, amount, price=None):
        try:
            if not self.exchange.markets: self.exchange.load_markets()
            amount_str = self.exchange.amount_to_precision(symbol, amount)
            amount = float(amount_str)
            if side == 'sell':
                base, _ = symbol.split('/')
                balance = self.fetch_balances()
                if balance is None:
                     logging.warning(f"Aborting sell of {symbol} on {self.exchange_id}: Balance unavailable.")
                     return None
                free_balance = balance.get(base, {}).get('free', 0)
                if free_balance < amount:
                    if free_balance > 0 and (amount - free_balance) / amount < 0.01:
                        amount = float(self.exchange.amount_to_precision(symbol, free_balance))
                    else:
                        logging.warning(f"Aborting sell of {symbol} on {self.exchange_id}: Insufficient {base} balance ({free_balance} < {amount})")
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
            raise e

class MockExchange(ExchangeInterface):
    """
    A simulated exchange for testing and simulation modes.

    Can be initialized with real API credentials to sync initial balances,
    but executes trades against a virtual local balance.

    Parameters
    ----------
    api_key : str, optional
        API key for real balance discovery.
    api_secret : str, optional
        API secret for real balance discovery.
    exchange_id : str, optional
        The type of exchange to simulate ('binance', 'kraken', etc.).
    """
    def __init__(self, api_key=None, api_secret=None, exchange_id='binance', options=None):
        super().__init__()
        self.balance = {'EUR': 1000.0, 'USDC': 1000.0, 'USDT': 1000.0}
        self.ohlcv_data = {}
        self.real_exchange = None
        self.fee_rate = 0.001
        self.exchange_id = exchange_id
        self._balance_initialized = False
        if api_key and api_secret and api_key != "YOUR_API_KEY":
            try:
                self.real_exchange = CCXTExchange(exchange_id, api_key, api_secret, options=options)
                logging.info(f"Mock initialized with real {exchange_id} API balance discovery (deferred)")
            except Exception as e: logging.error(f"Failed to initialize real exchange {exchange_id} for Mock: {e}")

    def _init_balance(self):
        if self._balance_initialized: return
        if self.real_exchange:
            try:
                real_bal = self.real_exchange.fetch_balances()
                if real_bal is None: return
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
                logging.info(f"Mock virtual balance initialized from real {self.exchange_id} wallet (dust ignored).")
            except Exception as e:
                logging.error(f"Failed to sync virtual balance from real {self.exchange_id} API: {e}")
        self._balance_initialized = True

    def load_markets(self):
        if self.real_exchange:
            self.markets = self.real_exchange.load_markets()
            return self.markets
        return self.markets

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
                exchange_class = getattr(ccxt, self.exchange_id)
                public_ex = exchange_class({'session': create_ccxt_session()})
                return public_ex.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
            except Exception: return []
        return self.ohlcv_data.get(symbol, [])[:limit]

    def fetch_ticker(self, symbol):
        if self.real_exchange:
             return self.real_exchange.fetch_ticker(symbol)
        try:
            exchange_class = getattr(ccxt, self.exchange_id)
            public_ex = exchange_class({'session': create_ccxt_session()})
            return public_ex.fetch_ticker(symbol)
        except Exception:
            data = self.ohlcv_data.get(symbol, [])
            if data: return {'last': data[-1][4]}
            return {'last': 0.0, 'quoteVolume': 0.0, 'baseVolume': 0.0}

    def fetch_balances(self):
        self._init_balance()
        return {'total': self.balance, 'free': self.balance}

    def fetch_trades(self, symbol, limit=100):
        if self.real_exchange:
            return self.real_exchange.fetch_trades(symbol, limit=limit)
        try:
            exchange_class = getattr(ccxt, self.exchange_id)
            public_ex = exchange_class({'session': create_ccxt_session()})
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

    def amount_to_precision(self, symbol, amount):
        if self.real_exchange:
            return self.real_exchange.amount_to_precision(symbol, amount)
        return str(amount) # No precision logic for pure mock yet

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

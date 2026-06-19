# Cryptocurrencies multiplatform trading bot - Exchange Interface
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import ccxt
import ccxt.pro as ccxtpro
import asyncio
import os
import time
import logging
import threading
import requests
import pandas as pd
from requests.adapters import HTTPAdapter

class ThrottledExchange:
    def __init__(self, exchange, delay_ms=2):
        self.exchange = exchange
        self.base_delay_s = delay_ms / 1000.0
        self.delay_s = self.base_delay_s
        self.lock = threading.Lock()
        self.last_request_time = 0
        self.burst_count = 0
        self.base_max_burst = 10
        self.max_burst = self.base_max_burst
        self.last_burst_reset = time.time()

    def _wait(self):
        with self.lock:
            now = time.time()

            # Reset burst every second
            if now - self.last_burst_reset > 1.0:
                self.burst_count = 0
                self.last_burst_reset = now

                # Recovery mechanism: gradually return to base settings
                if self.delay_s > self.base_delay_s:
                    self.delay_s = max(self.base_delay_s, self.delay_s * 0.95)
                if self.max_burst < self.base_max_burst:
                    self.max_burst = min(self.base_max_burst, self.max_burst + 1)

            if self.burst_count < self.max_burst:
                self.burst_count += 1
                self.last_request_time = now
                return

            elapsed = now - self.last_request_time
            wait_time = 0
            if elapsed < self.delay_s:
                wait_time = self.delay_s - elapsed

        if wait_time > 0:
            time.sleep(wait_time)

        with self.lock:
            self.last_request_time = time.time()

    def __getattr__(self, name):
        attr = getattr(self.exchange, name)
        if callable(attr):
            def throttled_wrapper(*args, **kwargs):
                retries = 3
                last_error = None
                while retries > 0:
                    try:
                        self._wait()
                        return attr(*args, **kwargs)
                    except (ccxt.RateLimitExceeded, ccxt.DDoSProtection) as e:
                        retries -= 1
                        last_error = e
                        wait_time = float(getattr(e, 'retry_after', 5)) or 5
                        logging.warning(f"Rate limit exceeded. Waiting {wait_time}s... ({retries} retries left)")
                        time.sleep(wait_time)
                        with self.lock:
                            self.delay_s *= 1.2 # Dynamically increase delay
                            self.max_burst = max(1, self.max_burst - 1)
                    except Exception as e:
                        raise e
                if last_error:
                    raise last_error
                return None
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
    def fetch_order_book(self, symbol, limit=20): raise NotImplementedError
    def get_effective_price(self, symbol, side, amount): raise NotImplementedError

class CCXTExchange(ExchangeInterface):
    def __init__(self, exchange_id, api_key, api_secret, options=None, market_type='spot'):
        ex_class = getattr(ccxt, exchange_id)
        default_options = {
            'apiKey': api_key, 'secret': api_secret, 'enableRateLimit': True,
            'options': {'poolSize': 50, 'adjustForTimeDifference': True, 'defaultType': market_type},
            'session': create_ccxt_session()
        }

        # Proxy support: prioritizes standard env vars, then 'proxy' var
        # Support full URL in 'proxy' or fallback to socks5://{proxy}:1080
        proxy_url = os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY') or os.environ.get('proxy')
        if proxy_url:
            if '://' not in proxy_url:
                proxy_url = f'socks5://{proxy_url}'
                if ':' not in proxy_url.split('://')[1]:
                    proxy_url += ':1080'

            default_options['proxies'] = {
                'http': proxy_url,
                'https': proxy_url,
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

    def fetch_order_book(self, symbol, limit=20):
        try: return self.exchange.fetch_order_book(symbol, limit=limit)
        except Exception as e: logging.error(f"Error fetching order book for {symbol}: {e}"); return None

    def get_effective_price(self, symbol, side, amount):
        """
        Calculates the effective execution price considering order book depth (slippage).
        Perfect trader logic: "ask everything possible to the API prior to consider anything".
        """
        book = self.fetch_order_book(symbol, limit=50)
        if not book:
            ticker = self.fetch_ticker(symbol)
            return ticker['last'] if ticker else 0

        orders = book['asks'] if side == 'buy' else book['bids']
        remaining = amount
        total_cost = 0

        for price, vol in orders:
            exec_vol = min(remaining, vol)
            total_cost += exec_vol * price
            remaining -= exec_vol
            if remaining <= 0:
                break

        if remaining > 0:
            # Not enough liquidity in the fetched depth, fallback to last price with penalty
            logging.warning(f"[{symbol}] Insufficient liquidity in order book for {amount}. Using fallback.")
            return orders[-1][0] if orders else 0

        return total_cost / amount

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
                base, quote = symbol.split('/')

                # If fee is in a different currency than the quote asset, convert it to quote asset
                if fee_currency != quote and fee_cost > 0:
                    if fee_currency == base:
                        ticker = self.fetch_ticker(symbol)
                        if ticker and ticker.get('last'):
                            fee_cost = fee_cost * ticker['last']
                    else:
                        # Try conversion via fee_currency/quote
                        try:
                            conv_ticker = self.fetch_ticker(f"{fee_currency}/{quote}")
                            if conv_ticker and conv_ticker.get('last'):
                                fee_cost = fee_cost * conv_ticker['last']
                        except:
                            # Fallback to pair price if it was somehow related or just keep as is
                            pass

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
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('binance', api_key, api_secret, options={'options': {'defaultType': market_type, 'poolSize': 50}}, market_type=market_type)

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

class MercadoBitcoinExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('mercado', api_key, api_secret, market_type=market_type)

class BitsoExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('bitso', api_key, api_secret, market_type=market_type)

class BitstampExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('bitstamp', api_key, api_secret, market_type=market_type)

class WhiteBITExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('whitebit', api_key, api_secret, market_type=market_type)

class IndodaxExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('indodax', api_key, api_secret, market_type=market_type)

class UpbitExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('upbit', api_key, api_secret, market_type=market_type)

class LunoExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('luno', api_key, api_secret, market_type=market_type)

class IndependentReserveExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('independentreserve', api_key, api_secret, market_type=market_type)

class BTCMarketsExchange(CCXTExchange):
    def __init__(self, api_key, api_secret, market_type='spot'):
        super().__init__('btcmarkets', api_key, api_secret, market_type=market_type)


def fetch_ohlcv_incremental(exchange, symbol, timeframe, ohlcv_cache_manager, limit=500, since=None):
    """
    Retrieves candles incrementally using ohlcv_cache_manager.
    If candles exist in cache, it bridges gaps both forwards and backwards.
    Returns (data, new_candles_count)
    """
    cached_data = ohlcv_cache_manager.get(symbol, timeframe)
    if isinstance(cached_data, pd.DataFrame):
         cached_data = cached_data.values.tolist()
    if not cached_data: cached_data = []

    new_count = 0
    updated = False

    # 1. Forward Update (New candles since last cache)
    if cached_data:
        last_ts = cached_data[-1][0]
        try:
            new_candles = exchange.fetch_ohlcv(symbol, timeframe, since=int(last_ts + 1))
            if new_candles:
                 new_count += len(new_candles)
                 cached_data.extend([c for c in new_candles if c[0] > last_ts])
                 updated = True
        except Exception as e:
            logging.warning(f"[{symbol}] Forward incremental fetch failed: {e}")

    # 2. Backward Update (If 'since' is earlier than cache start)
    if since and (not cached_data or cached_data[0][0] > since):
        target_since = since
        backward_candles = []
        while True:
            try:
                limit_back = 1000
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=int(target_since), limit=limit_back)
                if not ohlcv: break

                reach_cache = False
                if cached_data:
                    first_cached_ts = cached_data[0][0]
                    filtered = [c for c in ohlcv if c[0] < first_cached_ts]
                    if len(filtered) < len(ohlcv): reach_cache = True
                    ohlcv = filtered

                if not ohlcv: break
                backward_candles.extend(ohlcv)
                new_count += len(ohlcv)
                target_since = ohlcv[-1][0] + 1

                if reach_cache or len(backward_candles) > 100000: break
            except Exception as e:
                logging.warning(f"[{symbol}] Backward incremental fetch failed: {e}")
                break

        if backward_candles:
            cached_data = backward_candles + cached_data
            updated = True

    # 3. Fallback: if cache still empty and since was not provided
    if not cached_data:
        try:
            target_limit = limit if limit else 500
            fetch_since = int(since) if since is not None else None

            # If we need deep history and don't have 'since', calculate it to avoid small default fetches
            if fetch_since is None and target_limit > 1000:
                tf_map = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}
                duration_ms = target_limit * tf_map.get(timeframe, 300) * 1000
                fetch_since = int(time.time() * 1000 - duration_ms)

            # Optimization: Try to fetch as many as possible in fewer calls
            while len(cached_data) < target_limit:
                current_limit = min(1000, target_limit - len(cached_data))
                new_candles = exchange.fetch_ohlcv(symbol, timeframe, since=fetch_since, limit=current_limit)
                if not new_candles: break

                cached_data.extend(new_candles)
                new_count += len(new_candles)
                fetch_since = new_candles[-1][0] + 1
                updated = True

                if len(new_candles) < 100: break # Probably reached the end or limit
        except Exception as e:
             logging.warning(f"[{symbol}] Initial fetch failed: {e}")

    # Final maintenance
    if updated:
         df_tmp = pd.DataFrame(cached_data, columns=['ts', 'o', 'h', 'l', 'c', 'v'])

         # Fix Volume/Candle Integrity: handle 0.0 volumes
         # In illiquid markets, 0.0 volume candles might have same price as previous.
         # We keep them but ensure they don't break indicators by forward-filling prices if needed.
         # Actually, OHLCV from API should be fine, but we drop duplicates to be safe.
         df_tmp.drop_duplicates(subset='ts', keep='first', inplace=True)
         df_tmp.sort_values('ts', inplace=True)

         # Sanity check: if volume is 0 but price moved, it's suspicious but we trust API.
         # If volume is 0 and price is 0, that's corruption.
         df_tmp = df_tmp[df_tmp['c'] > 0]

         cached_data = df_tmp.values.tolist()

         if len(cached_data) > 150000:
              cached_data = cached_data[-150000:]

         ohlcv_cache_manager.set(symbol, timeframe, cached_data)

    if limit is None or limit <= 0:
        return cached_data, new_count

    data = cached_data[-limit:] if len(cached_data) > limit else cached_data
    return data, new_count

EXCHANGE_MAPPING = {
    'binance': BinanceExchange, 'kraken': KrakenExchange, 'bitvavo': BitvavoExchange,
    'coinbase': CoinbaseExchange, 'gemini': GeminiExchange, 'mercado': MercadoBitcoinExchange,
    'bitso': BitsoExchange, 'bitstamp': BitstampExchange, 'whitebit': WhiteBITExchange,
    'indodax': IndodaxExchange, 'upbit': UpbitExchange, 'luno': LunoExchange,
    'independentreserve': IndependentReserveExchange, 'btcmarkets': BTCMarketsExchange
}

class AsyncExchangeManager:
    def __init__(self, exchange_id, api_key, api_secret, symbols, timeframes, bot_state, bot_lock, ohlcv_cache_manager, shutdown_event, market_type='spot'):
        self.is_supported = exchange_id in ccxtpro.exchanges
        self.exchange_id = exchange_id
        self.api_key = api_key
        self.api_secret = api_secret
        self.symbols = symbols
        self.timeframes = timeframes
        self.bot_state = bot_state
        self.bot_lock = bot_lock
        self.ohlcv_cache_manager = ohlcv_cache_manager
        self.external_shutdown_event = shutdown_event
        self.market_type = market_type
        self.loop = None
        self.exchange = None

    def start(self):
        threading.Thread(target=self._run_loop, daemon=True).start()

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.main())
        except Exception as e:
            logging.error(f"AsyncExchangeManager loop error: {e}")
        finally:
            self.loop.close()

    async def main(self):
        ex_class = getattr(ccxtpro, self.exchange_id)
        options = {
            'apiKey': self.api_key,
            'secret': self.api_secret,
            'enableRateLimit': True,
        }
        if self.exchange_id == 'binance':
            options['options'] = {'defaultType': self.market_type}

        self.exchange = ex_class(options)

        tasks = []
        for symbol in self.symbols:
            tasks.append(self.watch_ticker_loop(symbol))
            for tf in self.timeframes:
                tasks.append(self.watch_ohlcv_loop(symbol, tf))

        # Monitor shutdown
        tasks.append(self.shutdown_monitor())

        await asyncio.gather(*tasks)
        await self.exchange.close()

    async def shutdown_monitor(self):
        while not self.external_shutdown_event.is_set():
            await asyncio.sleep(1)
        logging.info("AsyncExchangeManager shutting down...")

    async def watch_ticker_loop(self, symbol):
        while not self.external_shutdown_event.is_set():
            try:
                ticker = await self.exchange.watch_ticker(symbol)
                with self.bot_lock:
                    if symbol in self.bot_state:
                        self.bot_state[symbol]['price'] = ticker['last']
            except Exception as e:
                if not self.external_shutdown_event.is_set():
                    logging.debug(f"WS Ticker Error for {symbol}: {e}")
                    await asyncio.sleep(5)
                else: break

    async def watch_ohlcv_loop(self, symbol, timeframe):
        while not self.external_shutdown_event.is_set():
            try:
                ohlcvs = await self.exchange.watch_ohlcv(symbol, timeframe)
                if ohlcvs:
                    # Update cache
                    with self.bot_lock:
                        cached = self.ohlcv_cache_manager.get(symbol, timeframe)
                        if not cached: cached = []

                        # Convert to list if it's a DataFrame (shouldn't happen with OHLCVCacheManager usually but safety first)
                        if isinstance(cached, pd.DataFrame):
                            cached = cached.values.tolist()

                        # Merge new candles
                        last_ts = cached[-1][0] if cached else -1
                        new_candles = [c for c in ohlcvs if c[0] > last_ts]

                        if new_candles:
                            new_cached = cached + new_candles
                            # Perfect Trader: Keep it sane but large for deep analysis
                            if len(new_cached) > 150000:
                                new_cached = new_cached[-150000:]
                            self.ohlcv_cache_manager.set(symbol, timeframe, new_cached)

                            # Inform core of WS update
                            if hasattr(self, 'core_market_data') and self.core_market_data:
                                self.core_market_data.ws_updates[(symbol, timeframe)] = time.time()
                        # logging.debug(f"WS OHLCV Update for {symbol} {timeframe}: {len(new_candles)} new candles")
            except Exception as e:
                if not self.external_shutdown_event.is_set():
                    logging.debug(f"WS OHLCV Error for {symbol} {timeframe}: {e}")
                    await asyncio.sleep(5)
                else: break

class MockExchange(ExchangeInterface):
    def __init__(self, api_key=None, api_secret=None, exchange_type='binance', market_type='spot'):
        self.balance = {'USDT': 1000.0, 'USDC': 1000.0}
        self.ohlcv_data = {}
        self.order_book_data = {}
        self.real_exchange = None
        self.fee_rate = 0.001
        self.markets = {}
        self.exchange_type = exchange_type
        self.market_type = market_type
        self._balance_initialized = False
        if api_key and api_secret and api_key != "YOUR_API_KEY":
            try:
                ex_class = EXCHANGE_MAPPING.get(exchange_type, BinanceExchange)
                self.real_exchange = ex_class(api_key, api_secret, market_type=market_type)
                logging.info(f"Mock initialized with real {exchange_type} ({market_type}) balance discovery (deferred)")
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

    def fetch_order_book(self, symbol, limit=20):
        if self.real_exchange:
            try: return self.real_exchange.fetch_order_book(symbol, limit=limit)
            except Exception: pass
        if symbol in self.order_book_data:
            return self.order_book_data[symbol]

        # Default mock order book around last price
        ticker = self.fetch_ticker(symbol)
        price = ticker['last']
        if price == 0: price = 100.0
        return {
            'bids': [[price * 0.999, 1000.0], [price * 0.998, 2000.0]],
            'asks': [[price * 1.001, 1000.0], [price * 1.002, 2000.0]],
            'timestamp': time.time() * 1000,
            'datetime': datetime.utcnow().isoformat()
        }

    def get_effective_price(self, symbol, side, amount):
        book = self.fetch_order_book(symbol)
        orders = book['asks'] if side == 'buy' else book['bids']
        remaining = amount
        total_cost = 0
        for price, vol in orders:
            exec_vol = min(remaining, vol)
            total_cost += exec_vol * price
            remaining -= exec_vol
            if remaining <= 0: break
        return total_cost / amount if amount > 0 else 0

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

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
    async def watch_ohlcv_for_symbols(self, symbols, timeframe): raise NotImplementedError
    async def watch_balance(self): raise NotImplementedError
    async def watch_orders(self, symbol=None): raise NotImplementedError
    async def create_order(self, symbol, side, amount, price=None): raise NotImplementedError
    async def fetch_order(self, order_id, symbol=None): raise NotImplementedError
    async def fetch_ticker(self, symbol): raise NotImplementedError
    async def fetch_balance(self): raise NotImplementedError
    async def load_markets(self): raise NotImplementedError
    async def fetch_trading_fee(self, symbol): raise NotImplementedError
    async def get_fee_in_quote(self, symbol, fee_cost, fee_currency): raise NotImplementedError
    async def fetch_my_trades(self, symbol, limit=10): raise NotImplementedError
    async def fetch_trades(self, symbol, limit=100): raise NotImplementedError
    def amount_to_precision(self, symbol, amount): raise NotImplementedError
    def price_to_precision(self, symbol, price): raise NotImplementedError
    async def close(self): raise NotImplementedError

class CCXTExchange2(ExchangeInterface2):
    def __init__(self, exchange_id, api_key, api_secret, config, options=None):
        super().__init__()
        self.config = config
        exchange_config = {
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'options': options or {}
        }
        if 'binance' in exchange_id:
            if 'options' not in exchange_config: exchange_config['options'] = {}
            exchange_config['options']['recvWindow'] = 60000
            exchange_config['options']['adjustForTimeDifference'] = True

        self.exchange = getattr(ccxtpro, exchange_id)(exchange_config)
        self.exchange_id = exchange_id

    async def load_markets(self):
        self.markets = await self.exchange.load_markets()
        return self.markets

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100):
        try:
            return await asyncio.wait_for(
                self.exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit),
                timeout=self.config.get('timeouts', {}).get('ohlcv_fetch', 30)
            )
        except Exception as e:
            logging.error(f"Error fetching OHLCV for {symbol}: {e}")
            return []

    async def fetch_ohlcv_10k(self, symbol, timeframe, limit=None):
        if limit is None:
            limit = self.config.get('exchange', {}).get('fetch_ohlcv_limit', 10000)
        all_ohlcv = []
        try:
            tf_seconds = self.exchange.parse_timeframe(timeframe)
        except:
            tf_seconds = 1

        duration_ms = limit * tf_seconds * 1000
        since = self.exchange.milliseconds() - duration_ms

        retries = 0
        max_retries = self.config.get('exchange', {}).get('max_retries', 3)

        while len(all_ohlcv) < limit and retries < max_retries:
            fetch_limit = min(self.config.get('exchange', {}).get('fetch_chunk_size', 1000), limit - len(all_ohlcv))
            try:
                chunk = await asyncio.wait_for(
                    self.exchange.fetch_ohlcv(symbol, timeframe, since, limit=fetch_limit),
                    timeout=self.config.get('timeouts', {}).get('ohlcv_chunk', 20)
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

    async def watch_ohlcv_for_symbols(self, symbols, timeframe='1s'):
        """
        Watches OHLCV for multiple symbols using watchOHLCVForSymbols.
        Only yields when new candles are available to prevent redundant updates.
        """
        if not symbols:
            return

        # Normalize to list of [symbol, timeframe] pairs as required by CCXT Pro's unified API.
        # This prevents character-iteration bugs when passing strings where lists are expected.
        ohlcv_input = []
        if isinstance(symbols, str):
            if symbols in self.markets:
                ohlcv_input.append([symbols, timeframe])
        elif hasattr(symbols, '__iter__') and not isinstance(symbols, dict):
            seen = set()
            for item in symbols:
                if isinstance(item, (list, tuple)) and len(item) >= 1:
                    s = str(item[0])
                    t = str(item[1]) if len(item) >= 2 else timeframe
                else:
                    s = str(item)
                    t = timeframe

                if s in self.markets and s not in seen:
                    ohlcv_input.append([s, t])
                    seen.add(s)

        if not ohlcv_input:
            return

        # Track last yielded state (timestamp, close_price) to avoid redundant updates
        # while still allowing intra-candle price updates.
        last_yielded_state = {}
        # Pre-map symbols to timeframes for faster lookup
        symbol_to_tf = {p[0]: p[1] for p in ohlcv_input}

        while True:
            try:
                # Call CCXT Pro unified API
                result = await self.exchange.watchOHLCVForSymbols(ohlcv_input)
                updates = []

                if isinstance(result, dict):
                    for symbol, data in result.items():
                        # CCXT Pro may return { symbol: [candles] } or { symbol: { timeframe: [candles] } }
                        if isinstance(data, dict):
                            for tf, candles in data.items():
                                if not candles: continue
                                last_candle = candles[-1]
                                current_state = (last_candle[0], last_candle[4]) # (timestamp, close)
                                if current_state != last_yielded_state.get((symbol, tf)):
                                    last_yielded_state[(symbol, tf)] = current_state
                                    updates.append((symbol, tf, candles))
                        elif isinstance(data, list):
                            if not data: continue
                            last_candle = data[-1]
                            tf_found = symbol_to_tf.get(symbol, timeframe)
                            current_state = (last_candle[0], last_candle[4])
                            if current_state != last_yielded_state.get((symbol, tf_found)):
                                last_yielded_state[(symbol, tf_found)] = current_state
                                updates.append((symbol, tf_found, data))

                if updates:
                    yield updates

            except Exception as e:
                err_str = str(e).lower()
                if "restricted location" in err_str or "451" in err_str:
                    logging.error(f"WebSocket restricted: {self.exchange_id} is not available in your region.")
                    await asyncio.sleep(60)
                else:
                    logging.error(f"Error in watchOHLCVForSymbols: {e}")
                    await asyncio.sleep(5)

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

    async def fetch_order(self, order_id, symbol=None):
        try:
            return await self.exchange.fetch_order(order_id, symbol)
        except Exception as e:
            logging.error(f"Error fetching order {order_id}: {e}")
            return None

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
        default_fee = self.config.get('exchange', {}).get('default_fee', 0.001)
        try:
            fees = await self.exchange.fetch_trading_fee(symbol)
            return fees.get('taker', default_fee)
        except Exception as e:
            logging.warning(f"Error fetching trading fee for {symbol}: {e}. Defaulting to {default_fee*100}%")
            return default_fee

    async def fetch_my_trades(self, symbol, limit=10):
        try:
            return await self.exchange.fetch_my_trades(symbol, limit=limit)
        except Exception as e:
            logging.error(f"Error fetching my trades for {symbol}: {e}")
            return []

    async def fetch_trades(self, symbol, limit=100):
        try:
            return await self.exchange.fetch_trades(symbol, limit=limit)
        except Exception as e:
            logging.error(f"Error fetching trades for {symbol}: {e}");
            return []

    async def get_fee_in_quote(self, symbol, fee_cost, fee_currency):
        if not fee_currency or not fee_cost: return fee_cost
        _, quote = symbol.split('/')
        if fee_currency == quote: return fee_cost

        try:
            # Try to find a ticker for fee_currency/quote (direct pair)
            pair = f"{fee_currency}/{quote}"
            ticker = await self.fetch_ticker(pair)
            if ticker:
                return fee_cost * ticker['last']
        except:
            pass

        # Try indirect paths via bridge currencies
        bridges = ['USDT', 'USDC', 'BTC', 'ETH']
        for bridge in bridges:
            if bridge in [quote, fee_currency]: continue
            try:
                ticker_fee = await self.fetch_ticker(f"{fee_currency}/{bridge}")
                ticker_quote = await self.fetch_ticker(f"{quote}/{bridge}")
                if ticker_fee and ticker_quote:
                    return fee_cost * (ticker_fee['last'] / ticker_quote['last'])
            except:
                continue

        return fee_cost # Final fallback

    def amount_to_precision(self, symbol, amount):
        return self.exchange.amount_to_precision(symbol, amount)

    def price_to_precision(self, symbol, price):
        return self.exchange.price_to_precision(symbol, price)

    async def close(self):
        await self.exchange.close()

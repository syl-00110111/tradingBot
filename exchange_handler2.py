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
    async def watch_trades_for_symbols(self, symbols): raise NotImplementedError
    async def watch_trades(self, symbol): raise NotImplementedError
    async def watch_balance(self): raise NotImplementedError
    async def watch_orders(self, symbol=None): raise NotImplementedError
    async def fetch_order_book(self, symbol, limit=20): raise NotImplementedError
    async def create_order(self, symbol, side, amount, price=None): raise NotImplementedError
    async def cancel_order(self, order_id, symbol=None): raise NotImplementedError
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

    async def _get_supported_timeframe(self, timeframe):
        original_timeframe = timeframe
        tf_seconds = None

        # 1. Try to parse the requested timeframe
        try:
            tf_seconds = self.exchange.parse_timeframe(timeframe)
        except:
            pass

        # 2. Check if the timeframe is explicitly in the exchange's supported list
        supported_tfs = getattr(self.exchange, 'timeframes', {})
        if tf_seconds is not None and timeframe not in supported_tfs:
            # Even if parseable, it might not be supported by this specific exchange
            tf_seconds = None

        # 3. Fallback if not supported
        if tf_seconds is None:
            if supported_tfs:
                # Sort supported timeframes by their duration in seconds
                tfs_with_seconds = []
                for tf in supported_tfs:
                    try:
                        s = self.exchange.parse_timeframe(tf)
                        if s is not None:
                            tfs_with_seconds.append((tf, s))
                    except:
                        continue

                if tfs_with_seconds:
                    tfs_with_seconds.sort(key=lambda x: x[1])
                    timeframe, tf_seconds = tfs_with_seconds[0]
                    logging.warning(f"Timeframe '{original_timeframe}' unsupported by {self.exchange_id}. Falling back to '{timeframe}'.")

            # 4. Final safety fallback
            if tf_seconds is None:
                logging.error(f"Could not find any supported timeframe for {self.exchange_id}. Defaulting to 1m (60s).")
                timeframe = '1m'
                tf_seconds = 60

        return timeframe, tf_seconds

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100):
        timeframe, _ = await self._get_supported_timeframe(timeframe)
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

        # Robustness: Ensure limit is a valid integer and not None
        if limit is None:
            limit = 10000
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            limit = 10000

        actual_timeframe, tf_seconds = await self._get_supported_timeframe(timeframe)

        all_ohlcv = []
        duration_ms = int(limit * tf_seconds * 1000)
        since = self.exchange.milliseconds() - duration_ms

        retries = 0
        max_retries = self.config.get('exchange', {}).get('max_retries', 3)

        while len(all_ohlcv) < limit and retries < max_retries:
            fetch_limit = min(self.config.get('exchange', {}).get('fetch_chunk_size', 1000), limit - len(all_ohlcv))
            try:
                chunk = await asyncio.wait_for(
                    self.exchange.fetch_ohlcv(symbol, actual_timeframe, since, limit=fetch_limit),
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

        return all_ohlcv[-limit:], actual_timeframe

    async def watch_trades(self, symbol):
        while True:
            try:
                trades = await self.exchange.watch_trades(symbol)
                yield trades
            except Exception as e:
                logging.error(f"Error in watch_trades for {symbol}: {e}")
                await asyncio.sleep(5)

    async def watch_trades_for_symbols(self, symbols):
        """
        Watches trades for multiple symbols.
        Falls back to individual watchers if watchTradesForSymbols is not supported.
        """
        if not symbols:
            return

        use_fallback = False
        last_symbols_snapshot = None
        individual_tasks = {}
        queue = asyncio.Queue()

        async def individual_watcher(symbol):
            try:
                async for trades in self.watch_trades(symbol):
                    await queue.put(trades)
            except Exception as e:
                logging.error(f"Individual trades watcher error for {symbol}: {e}")

        while True:
            try:
                if not use_fallback:
                    try:
                        trades = await self.exchange.watchTradesForSymbols(symbols)
                        yield trades
                    except Exception as e:
                        if "not supported" in str(e).lower() or "not implemented" in str(e).lower():
                            logging.warning(f"watchTradesForSymbols not supported by {self.exchange_id}, falling back to individual watchers.")
                            use_fallback = True
                        else:
                            raise e

                if use_fallback:
                    current_symbols_snapshot = set(symbols)
                    if last_symbols_snapshot != current_symbols_snapshot:
                        # Remove tasks for symbols no longer in the list
                        if last_symbols_snapshot:
                            for s in last_symbols_snapshot - current_symbols_snapshot:
                                if s in individual_tasks:
                                    individual_tasks[s].cancel()
                                    del individual_tasks[s]

                        # Add tasks for new symbols
                        for s in current_symbols_snapshot:
                            if s not in individual_tasks:
                                individual_tasks[s] = asyncio.create_task(individual_watcher(s))

                        last_symbols_snapshot = current_symbols_snapshot

                    # Wait for data from any individual watcher
                    try:
                        trades = await asyncio.wait_for(queue.get(), timeout=1.0)
                        yield trades
                    except asyncio.TimeoutError:
                        continue

            except Exception as e:
                logging.error(f"Error in trades watcher: {e}")
                await asyncio.sleep(5)

    async def watch_ohlcv(self, symbol, timeframe):
        while True:
            try:
                candles = await self.exchange.watch_ohlcv(symbol, timeframe)
                yield symbol, timeframe, candles
            except Exception as e:
                logging.error(f"Error in watch_ohlcv for {symbol}: {e}")
                await asyncio.sleep(5)

    async def watch_ohlcv_for_symbols(self, symbols, timeframe='1s'):
        """
        Watches OHLCV for multiple symbols using watchOHLCVForSymbols.
        Only yields when new candles are available to prevent redundant updates.
        Falls back to individual watchers if not supported.
        """
        if not symbols:
            return

        use_fallback = False
        last_symbols_snapshot = None
        ohlcv_input = []
        symbol_to_tf = {}
        individual_tasks = {}
        queue = asyncio.Queue()

        async def individual_watcher(symbol, tf):
            try:
                async for update in self.watch_ohlcv(symbol, tf):
                    await queue.put([update]) # Wrap in list to match updates format
            except Exception as e:
                logging.error(f"Individual OHLCV watcher error for {symbol}: {e}")

        # Track last yielded state (timestamp, close_price) to avoid redundant updates
        last_yielded_state = {}

        while True:
            try:
                # Dynamic update of ohlcv_input if symbols list changed
                current_symbols_snapshot_str = str(symbols)
                if current_symbols_snapshot_str != last_symbols_snapshot:
                    ohlcv_input = []
                    seen = set()
                    input_list = [symbols] if isinstance(symbols, str) else symbols
                    for item in input_list:
                        if isinstance(item, (list, tuple)) and len(item) >= 1:
                            s = str(item[0])
                            t = str(item[1]) if len(item) >= 2 else timeframe
                        else:
                            s = str(item)
                            t = timeframe

                        if s in self.markets and s not in seen:
                            ohlcv_input.append([s, t])
                            seen.add(s)

                    last_symbols_snapshot = current_symbols_snapshot_str
                    symbol_to_tf = {p[0]: p[1] for p in ohlcv_input}

                    if use_fallback:
                        current_set = set([(p[0], p[1]) for p in ohlcv_input])
                        # Cancel removed
                        for key in list(individual_tasks.keys()):
                            if key not in current_set:
                                individual_tasks[key].cancel()
                                del individual_tasks[key]
                        # Add new
                        for key in current_set:
                            if key not in individual_tasks:
                                individual_tasks[key] = asyncio.create_task(individual_watcher(key[0], key[1]))

                if not ohlcv_input:
                    await asyncio.sleep(1)
                    continue

                if not use_fallback:
                    try:
                        result = await self.exchange.watchOHLCVForSymbols(ohlcv_input)
                        updates_to_process = []
                        if isinstance(result, dict):
                            for s, data in result.items():
                                if isinstance(data, dict):
                                    for tf, candles in data.items():
                                        updates_to_process.append((s, tf, candles))
                                elif isinstance(data, list):
                                    updates_to_process.append((s, symbol_to_tf.get(s, timeframe), data))
                        elif isinstance(result, list):
                            # Some exchanges return a list of [symbol, timeframe, candles] or similar
                            # This depends on CCXT implementation for specific exchange
                            pass

                        updates = updates_to_process
                    except Exception as e:
                        if "not supported" in str(e).lower() or "not implemented" in str(e).lower():
                            logging.warning(f"watchOHLCVForSymbols not supported by {self.exchange_id}, falling back to individual watchers.")
                            use_fallback = True
                            # Force re-evaluation of individual tasks
                            last_symbols_snapshot = None
                            continue
                        else:
                            raise e
                else:
                    try:
                        updates = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue

                # Process updates
                batch_updates = []
                for symbol_upd, tf_upd, candles in updates:
                    if not candles: continue
                    last_candle = candles[-1]
                    current_state = (last_candle[0], last_candle[4])
                    if current_state != last_yielded_state.get((symbol_upd, tf_upd)):
                        last_yielded_state[(symbol_upd, tf_upd)] = current_state
                        batch_updates.append((symbol_upd, tf_upd, candles))

                if batch_updates:
                    yield batch_updates

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

    async def fetch_order_book(self, symbol, limit=20):
        try:
            return await self.exchange.fetch_order_book(symbol, limit)
        except Exception as e:
            logging.error(f"Error fetching order book for {symbol}: {e}")
            return None

    async def create_order(self, symbol, side, amount, price=None):
        try:
            amount_str = self.exchange.amount_to_precision(symbol, amount)
            amount = float(amount_str)
            if price is not None:
                price_str = self.exchange.price_to_precision(symbol, price)
                price = float(price_str)
                if side == 'buy':
                    return await self.exchange.create_limit_buy_order(symbol, amount, price)
                else:
                    return await self.exchange.create_limit_sell_order(symbol, amount, price)
            else:
                if side == 'buy':
                    return await self.exchange.create_market_buy_order(symbol, amount)
                else:
                    return await self.exchange.create_market_sell_order(symbol, amount)
        except Exception as e:
            logging.error(f"Error creating order for {symbol}: {e}")
            raise e

    async def cancel_order(self, order_id, symbol=None):
        try:
            return await self.exchange.cancel_order(order_id, symbol)
        except Exception as e:
            logging.error(f"Error cancelling order {order_id}: {e}")
            return None

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

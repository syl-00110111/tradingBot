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
        # Cache for timeframe warnings to avoid duplicate log messages
        self._warned_timeframes = set()

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
                    # Only log warning once per unsupported timeframe
                    warning_key = f"{original_timeframe}->{timeframe}"
                    if warning_key not in self._warned_timeframes:
                        self._warned_timeframes.add(warning_key)
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
        actual_timeframe, _ = await self._get_supported_timeframe(timeframe)
        while True:
            try:
                candles = await self.exchange.watch_ohlcv(symbol, actual_timeframe)
                yield symbol, actual_timeframe, candles
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

        resolved_timeframe, _ = await self._get_supported_timeframe(timeframe)

        use_fallback = False
        last_symbols_snapshot = None
        ohlcv_input = []
        symbol_to_tf = {}
        individual_tasks = {}
        queue = asyncio.Queue()

        async def individual_watcher(symbol, tf):
            try:
                async for symbol_upd, tf_upd, candles in self.watch_ohlcv(symbol, tf):
                    await queue.put((symbol_upd, tf_upd, candles))
            except Exception as e:
                logging.error(f"Individual OHLCV watcher error for {symbol}: {e}")

        def ensure_individual_watchers():
            current_set = set((p[0], p[1]) for p in ohlcv_input)
            for key in list(individual_tasks.keys()):
                if key not in current_set:
                    individual_tasks[key].cancel()
                    del individual_tasks[key]
            for key in current_set:
                task = individual_tasks.get(key)
                if task is None or task.done():
                    individual_tasks[key] = asyncio.create_task(individual_watcher(key[0], key[1]))

        def normalize_updates(raw):
            if isinstance(raw, tuple) and len(raw) == 3:
                return [raw]
            if isinstance(raw, list):
                if not raw:
                    return []
                if isinstance(raw[0], tuple) and len(raw[0]) == 3:
                    return raw
                if len(raw) >= 3 and isinstance(raw[0], str):
                    return [tuple(raw[:3])]
            return []

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
                            t = str(item[1]) if len(item) >= 2 else resolved_timeframe
                        else:
                            s = str(item)
                            t = resolved_timeframe

                        if s in self.markets and s not in seen:
                            ohlcv_input.append([s, t])
                            seen.add(s)
                    
                    last_symbols_snapshot = current_symbols_snapshot_str
                    symbol_to_tf = {p[0]: p[1] for p in ohlcv_input}

                if not ohlcv_input:
                    await asyncio.sleep(1)
                    continue

                updates = []

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
                                    updates_to_process.append((s, symbol_to_tf.get(s, resolved_timeframe), data))
                        elif isinstance(result, list):
                            for item in result:
                                if isinstance(item, (list, tuple)) and len(item) >= 3:
                                    updates_to_process.append((item[0], item[1], item[2]))
                        
                        updates = updates_to_process
                    except Exception as e:
                        if "not supported" in str(e).lower() or "not implemented" in str(e).lower():
                            logging.warning(f"watchOHLCVForSymbols not supported by {self.exchange_id}, falling back to individual watchers.")
                            use_fallback = True
                        else:
                            raise e

                if use_fallback:
                    ensure_individual_watchers()
                    try:
                        raw_update = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    updates = normalize_updates(raw_update)
                    if not updates:
                        continue

                if not updates:
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
            # Preserve original amount to detect excessive rounding to zero
            orig_amount = amount
            amount_str = self.exchange.amount_to_precision(symbol, amount)
            try:
                amount = float(amount_str)
            except (ValueError, TypeError):
                amount = 0.0

            # If precision rounding produced 0 for a small but non-zero amount,
            # try to determine the exchange minimum and either use it or raise.
            if amount == 0.0 and orig_amount and float(orig_amount) > 0:
                market = self.markets.get(symbol) or getattr(self.exchange, 'markets', {}).get(symbol) if hasattr(self, 'markets') or hasattr(self.exchange, 'markets') else None
                min_amount = None
                try:
                    if market and isinstance(market, dict):
                        min_amount = (market.get('limits', {}) or {}).get('amount', {}).get('min')
                        if min_amount is None:
                            # Try precision fallback
                            precision_amt = (market.get('precision') or {}).get('amount')
                            if precision_amt is not None:
                                try:
                                    precision_amt = int(precision_amt)
                                    min_amount = 10 ** (-precision_amt)
                                except Exception:
                                    min_amount = None
                except Exception:
                    min_amount = None

                try:
                    orig_f = float(orig_amount)
                except Exception:
                    orig_f = 0.0

                if min_amount is None:
                    # Unknown min amount: raise explicit error to avoid submitting zero-sized orders
                    raise ValueError(f"Calculated order amount {orig_f} was rounded to 0 by exchange precision for {symbol}. Unable to determine minimum lot size.")

                min_amount = float(min_amount)
                if orig_f < min_amount:
                    # Before replacing by min_amount, verify the wallet balance can cover that minimum
                    def find_key(d, key):
                        if not isinstance(d, dict):
                            return None
                        if key in d:
                            return key
                        up = key.upper()
                        low = key.lower()
                        if up in d:
                            return up
                        if low in d:
                            return low
                        return None

                    # Fetch balance to verify we can use the minimum
                    try:
                        bal = await self.fetch_balance()
                    except Exception:
                        bal = None

                    if side == 'buy':
                        try:
                            _, quote = symbol.split('/')
                        except Exception:
                            quote = None

                        # Determine price to compute required quote amount
                        price_for_calc = None
                        try:
                            price_for_calc = float(price) if price is not None else None
                        except Exception:
                            price_for_calc = None

                        if price_for_calc is None:
                            try:
                                tk = await self.fetch_ticker(symbol)
                                if tk:
                                    price_for_calc = float(tk.get('last') or tk.get('close') or 0)
                            except Exception:
                                price_for_calc = None

                        if price_for_calc is None or price_for_calc <= 0:
                            raise ValueError(f"Cannot determine price to evaluate minimum buy amount for {symbol}.")

                        required_quote = min_amount * price_for_calc

                        free_quote = 0.0
                        if isinstance(bal, dict):
                            if 'free' in bal and isinstance(bal['free'], dict):
                                qk = find_key(bal['free'], quote)
                                if qk:
                                    free_quote = float(bal['free'].get(qk) or 0)
                            if free_quote == 0:
                                qk = find_key(bal, quote)
                                if qk:
                                    free_quote = float(bal.get(qk) or 0)
                                elif 'total' in bal and isinstance(bal['total'], dict):
                                    qk = find_key(bal['total'], quote)
                                    if qk:
                                        free_quote = float(bal['total'].get(qk) or 0)

                        if free_quote < required_quote:
                            raise ValueError(f"Insufficient quote balance to buy minimum amount: need {required_quote}, have {free_quote} {quote} for {symbol}.")

                    else:
                        try:
                            base, _ = symbol.split('/')
                        except Exception:
                            base = None

                        free_base = 0.0
                        if isinstance(bal, dict):
                            if 'free' in bal and isinstance(bal['free'], dict):
                                bk = find_key(bal['free'], base)
                                if bk:
                                    free_base = float(bal['free'].get(bk) or 0)
                            if free_base == 0:
                                bk = find_key(bal, base)
                                if bk:
                                    free_base = float(bal.get(bk) or 0)
                                elif 'total' in bal and isinstance(bal['total'], dict):
                                    bk = find_key(bal['total'], base)
                                    if bk:
                                        free_base = float(bal['total'].get(bk) or 0)

                        if free_base < min_amount:
                            raise ValueError(f"Insufficient base balance to sell minimum amount: need {min_amount}, have {free_base} {base} for {symbol}.")

                    # If balance suffices, use the minimum amount (rounded to exchange precision)
                    amount_str = self.exchange.amount_to_precision(symbol, min_amount)
                    try:
                        amount = float(amount_str)
                    except Exception:
                        amount = float(min_amount)
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
            logging.debug(f"Error fetching ticker for {symbol}: {e}")
            return None

    async def fetch_tickers(self, symbols=None):
        try:
            return await self.exchange.fetch_tickers(symbols)
        except Exception as e:
            logging.debug(f"Error fetching tickers: {e}")
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
            logging.debug(f"Error fetching my trades for {symbol}: {e}")
            return []

    async def fetch_trades(self, symbol, limit=100):
        try:
            return await self.exchange.fetch_trades(symbol, limit=limit)
        except Exception as e:
            logging.debug(f"Error fetching trades for {symbol}: {e}")
            return []

    async def get_fee_in_quote(self, symbol, fee_cost, fee_currency):
        if not fee_currency or not fee_cost: return float(fee_cost or 0)
        _, quote = symbol.split('/')
        if fee_currency == quote: return fee_cost

        try:
            # Try to find a ticker for fee_currency/quote (direct pair)
            pair = f"{fee_currency}/{quote}"
            ticker = await self.fetch_ticker(pair)
            if ticker:
                return fee_cost * float(ticker['last'] or 0)
        except:
            pass

        # Try indirect paths via bridge currencies
        bridges = ['EUR']
        for bridge in bridges:
            if bridge in [quote, fee_currency]: continue
            try:
                ticker_fee = await self.fetch_ticker(f"{fee_currency}/{bridge}")
                ticker_quote = await self.fetch_ticker(f"{quote}/{bridge}")
                if ticker_fee and ticker_quote:
                    return fee_cost * (float(ticker_fee['last'] or 0) / float(ticker_quote['last'] or 1))
            except:
                continue

        return float(fee_cost or 0) # Final fallback

    def amount_to_precision(self, symbol, amount):
        return self.exchange.amount_to_precision(symbol, amount)

    def price_to_precision(self, symbol, price):
        return self.exchange.price_to_precision(symbol, price)

    async def close(self):
        await self.exchange.close()

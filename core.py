# Cryptocurrencies multiplatform trading bot - Simplified Core Engine
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import asyncio
import logging
import time
import pandas as pd
import torch
import threading
from typing import Dict, List, Set, Optional
from rich.live import Live

from persistence import DataManager, PatternManager, OHLCVCacheManager
from trading_engine import TradingEngine, execute_buy, execute_sell
from indicators import get_common_indicators, get_signals, calculate_similarity_batch

class TradingCore:
    def __init__(self, config, exchange, data_manager, pattern_manager, ohlcv_cache_manager, headless=False, ui=None, shutdown_event=None):
        self.config = config
        self.exchange = exchange
        self.data_manager = data_manager
        self.pattern_manager = pattern_manager
        self.ohlcv_cache_manager = ohlcv_cache_manager
        self.engine = TradingEngine(config)
        self.headless = headless
        self.ui = ui
        self.live = None

        self.bot_state = {}
        self.global_pattern_pool = []
        self.available_assets = []
        self.suspended_pairs = set()
        self.benchmarking_pairs = set()
        self.signal_arrival_times = {}
        self.shutdown_event = shutdown_event or asyncio.Event()
        self._stop_event = threading.Event()

        self.ohlcv_data = {} # symbol -> DataFrame
        self.ohlcv_lock = threading.Lock()
        self.strategy_indices = {s: 0 for s in config.get('pairs', {})}
        self.balance_data = {}
        self.balance_lock = threading.Lock()
        self.threads = []
        self.thread_exchanges = []
        self.mode = config.get('mode', 'simulation')

    def log(self, message):
        logging.info(message)

    def watch_ohlcv_thread(self, symbol, timeframe):
        """Thread dedicated to watching OHLCV for a specific symbol."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # We need a fresh exchange instance for each thread to be thread-safe
        from exchange_handler import EXCHANGE_MAPPING, MockExchange
        import os
        import json

        api_creds = {}
        if os.path.exists('api.json'):
            try:
                with open('api.json', 'r') as f: api_creds = json.load(f)
            except: pass

        api_key = os.environ.get('api_key') or api_creds.get('api_key') or self.config.get('api_key')
        api_secret = os.environ.get('api_secret') or api_creds.get('api_secret') or self.config.get('api_secret')
        market_type = api_creds.get('market', self.config.get('market', 'spot'))

        ex_class = EXCHANGE_MAPPING.get(self.config.get('exchange', 'binance'), MockExchange)

        if self.mode == 'live':
            thread_exchange = ex_class(api_key, api_secret, market_type=market_type)
        else:
            thread_exchange = MockExchange(api_key, api_secret, exchange_type=self.config.get('exchange', 'binance'), market_type=market_type)

        self.thread_exchanges.append(thread_exchange)

        async def _run():
            # First acquisition: Fetch
            self.log(f"[{symbol}] Initial OHLCV fetch...")
            ohlcv = await thread_exchange.fetch_ohlcv(symbol, timeframe, limit=400)
            if ohlcv:
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                with self.ohlcv_lock:
                    self.ohlcv_data[symbol] = df

            self.log(f"[{symbol}] Starting watch loop...")
            while not self._stop_event.is_set():
                try:
                    # Use timeout to allow periodic check of _stop_event
                    new_candles = await asyncio.wait_for(thread_exchange.watch_ohlcv(symbol, timeframe), timeout=2.0)
                    if new_candles:
                        with self.ohlcv_lock:
                            # Accumulation logic
                            if symbol in self.ohlcv_data:
                                df_new = pd.DataFrame(new_candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                                combined = pd.concat([self.ohlcv_data[symbol], df_new]).drop_duplicates('timestamp').sort_values('timestamp')
                                self.ohlcv_data[symbol] = combined.tail(500) # Keep a reasonable history
                            else:
                                self.ohlcv_data[symbol] = pd.DataFrame(new_candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    self.log(f"[{symbol}] Watch error: {e}")
                    await asyncio.sleep(5)

            try:
                await thread_exchange.close()
            except: pass

        loop.run_until_complete(_run())

    def watch_balance_thread(self):
        """Thread dedicated to watching balance."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        from exchange_handler import EXCHANGE_MAPPING, MockExchange
        import os
        import json

        api_creds = {}
        if os.path.exists('api.json'):
            try:
                with open('api.json', 'r') as f: api_creds = json.load(f)
            except: pass

        api_key = os.environ.get('api_key') or api_creds.get('api_key') or self.config.get('api_key')
        api_secret = os.environ.get('api_secret') or api_creds.get('api_secret') or self.config.get('api_secret')
        market_type = api_creds.get('market', self.config.get('market', 'spot'))

        ex_class = EXCHANGE_MAPPING.get(self.config.get('exchange', 'binance'), MockExchange)

        if self.mode == 'live':
            thread_exchange = ex_class(api_key, api_secret, market_type=market_type)
        else:
            thread_exchange = MockExchange(api_key, api_secret, exchange_type=self.config.get('exchange', 'binance'), market_type=market_type)

        self.thread_exchanges.append(thread_exchange)

        async def _run():
            # First acquisition: Fetch
            self.log("Initial balance fetch...")
            balance = await thread_exchange.fetch_balance()
            if balance:
                with self.balance_lock:
                    self.balance_data = balance

            self.log("Starting balance watch loop...")
            while not self._stop_event.is_set():
                try:
                    # Not all exchanges support watch_balance, but we try
                    ccxt_ex = getattr(thread_exchange, 'exchange', None)
                    if ccxt_ex and hasattr(ccxt_ex, 'watchBalance'):
                        balance = await asyncio.wait_for(ccxt_ex.watchBalance(), timeout=2.0)
                        if balance:
                            with self.balance_lock:
                                self.balance_data = balance
                    else:
                        # Fallback for exchanges without watchBalance (not recommended as per requirement but good for safety)
                        for _ in range(60):
                            if self._stop_event.is_set(): break
                            await asyncio.sleep(1)
                        if self._stop_event.is_set(): break
                        balance = await thread_exchange.fetch_balance()
                        if balance:
                            with self.balance_lock:
                                self.balance_data = balance
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    self.log(f"Balance watch error: {e}")
                    await asyncio.sleep(10)

            try:
                await thread_exchange.close()
            except: pass

        loop.run_until_complete(_run())

    def dashboard_thread_func(self):
        """Thread dedicated to the Dashboard UI."""
        if self.headless or not self.ui:
            return

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _run():
            with Live(self.ui.make_dashboard(self.mode, self.config, self.bot_state, self.signal_arrival_times), refresh_per_second=2, screen=True) as live:
                self.live = live
                while not self._stop_event.is_set():
                    try:
                        await self.ui.input_handler(self)
                        live.update(self.ui.make_dashboard(self.mode, self.config, self.bot_state, self.signal_arrival_times))
                        await asyncio.sleep(0.1)
                    except Exception as e:
                        logging.error(f"Dashboard error: {e}")
                        await asyncio.sleep(1)

        loop.run_until_complete(_run())

    async def main_loop(self):
        """Sequential Analysis Loop, consuming data from threads."""
        self.log("Starting trading bot core...")

        symbols = list(self.config.get('pairs', {}).keys())

        # Start Watcher Threads
        t_bal = threading.Thread(target=self.watch_balance_thread, daemon=True)
        t_bal.start()
        self.threads.append(t_bal)

        for symbol in symbols:
            t = threading.Thread(target=self.watch_ohlcv_thread, args=(symbol, '1m'), daemon=True)
            t.start()
            self.threads.append(t)

        # Start Dashboard Thread
        if not self.headless:
            t_dash = threading.Thread(target=self.dashboard_thread_func, daemon=True)
            t_dash.start()
            self.threads.append(t_dash)

        self.log("All systems launched. Entering analysis loop.")

        while not self.shutdown_event.is_set():
            try:
                # 0. Update state from balance thread
                with self.balance_lock:
                    if self.balance_data:
                        # Update available_assets (assets with > 0 balance)
                        new_assets = []
                        # CCXT balance format can vary, usually has 'total' or just asset keys
                        total_bal = self.balance_data.get('total', self.balance_data)
                        for asset, total in total_bal.items():
                            if isinstance(total, (int, float)) and total > 0:
                                new_assets.append(asset)
                            elif isinstance(total, dict) and total.get('total', 0) > 0:
                                new_assets.append(asset)

                        if new_assets:
                            self.available_assets[:] = sorted(list(set(new_assets)))

                        # Update bot_state amounts for symbols
                        for symbol in symbols:
                            asset = symbol.split('/')[0]
                            if asset in total_bal:
                                val = total_bal[asset]
                                if isinstance(val, (int, float)):
                                    self.bot_state[symbol]['amt'] = val
                                elif isinstance(val, dict):
                                    self.bot_state[symbol]['amt'] = val.get('total', 0)

                # Sequential Analysis on Symbols
                for symbol in symbols:
                    if self.shutdown_event.is_set(): break

                    df = None
                    with self.ohlcv_lock:
                        if symbol in self.ohlcv_data:
                            df = self.ohlcv_data[symbol].copy()

                    if df is None or df.empty:
                        continue

                    try:
                        # Sequential Analysis
                        from bot import perform_analysis_calculation
                        device = self.config.get('device', torch.device('cpu'))

                        current_data = self.bot_state.get(symbol, {})
                        pinfo = {
                            'active_pattern_id': current_data.get('active_pattern_id'),
                            'pattern_match_ts': current_data.get('pattern_match_ts', 0),
                            'last_mc_ts': current_data.get('last_mc_ts', 0),
                            'mc_score': current_data.get('mc_score', 1.1)
                        }

                        # Rolling strategy logic
                        from indicators import STRATEGIES
                        idx = self.strategy_indices.get(symbol, 0)
                        next_strat = STRATEGIES[idx % len(STRATEGIES)]
                        self.strategy_indices[symbol] = idx + 1

                        current_pattern = current_data.get('active_pattern')

                        res = await perform_analysis_calculation(
                            symbol, '1m', 60, df, current_pattern, device, pinfo,
                            next_strategy=next_strat, config=self.config,
                            global_pattern_pool=self.global_pattern_pool
                        )

                        if res and 'error' not in res:
                            self.bot_state[symbol].update(res)

                            # Signal detection
                            if res.get('buy_signal') or res.get('sell_signal'):
                                self.signal_arrival_times[symbol] = time.time()

                                if res.get('buy_signal'):
                                    self.log(f"Signal: BUY detected for {symbol}")
                                    positions = self.bot_state[symbol].get('positions', [])
                                    if len(positions) >= 3:
                                        self.log(f"Aborted: Max concurrent positions reached for {symbol}")
                                    elif symbol in self.suspended_pairs:
                                        self.log(f"Aborted: {symbol} is suspended")
                                    else:
                                        if self.engine.validate_trade_mc(symbol, self.bot_state[symbol], self.config):
                                            self.log(f"Executing BUY for {symbol}")
                                            await execute_buy(
                                                self.exchange, self.data_manager, self.engine,
                                                symbol, self.bot_state[symbol], self.config,
                                                self.available_assets, self.suspended_pairs
                                            )
                                elif res.get('sell_signal'):
                                    self.log(f"Signal: SELL detected for {symbol}")
                                    positions = self.bot_state[symbol].get('positions', [])
                                    for idx, pos in enumerate(positions):
                                        self.log(f"Executing SELL for {symbol}")
                                        await execute_sell(
                                            self.exchange, self.data_manager, self.engine,
                                            symbol, self.bot_state[symbol], self.config, idx
                                        )
                                        break
                    except Exception as e:
                        logging.error(f"Error processing {symbol}: {e}")

                await asyncio.sleep(1)
            except Exception as e:
                logging.error(f"Main loop error: {e}")
                await asyncio.sleep(5)

    async def shutdown(self):
        self.log("Shutting down core...")
        self.shutdown_event.set()
        self._stop_event.set()

        # Give threads a moment to finish
        await asyncio.sleep(1)

        # Try to close all thread-specific exchanges
        for ex in self.thread_exchanges:
            try:
                # We can't easily await from here if they are in different loops,
                # but closing the underlying connection might help.
                pass
            except: pass

        try:
            await self.exchange.close()
        except:
            pass
        self.log("Core shutdown complete.")

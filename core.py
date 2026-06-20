# Cryptocurrencies multiplatform trading bot - Sequential Core Engine
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import asyncio
import logging
import time
import pandas as pd
import torch
from typing import Dict, List, Set, Optional

from persistence import DataManager, PatternManager, OHLCVCacheManager
from trading_engine import TradingEngine, execute_buy, execute_sell
from indicators import get_common_indicators, get_signals, calculate_similarity_batch

class TradingCore:
    def __init__(self, config, exchange, data_manager, pattern_manager, ohlcv_cache_manager, headless=False, ui=None):
        self.config = config
        self.exchange = exchange
        self.data_manager = data_manager
        self.pattern_manager = pattern_manager
        self.ohlcv_cache_manager = ohlcv_cache_manager
        self.engine = TradingEngine(config)
        self.headless = headless
        self.ui = ui
        self.live = None # Will be set by bot.py if not headless

        self.bot_state = {}
        self.global_pattern_pool = []
        self.available_assets = []
        self.suspended_pairs = set()
        self.benchmarking_pairs = set()
        self.signal_arrival_times = {}
        self.shutdown_event = asyncio.Event()

    def log(self, message):
        if self.headless:
            logging.info(message)
        # If not headless, the DashboardHandler will catch logging.info and show it in the UI logs panel

    async def main_loop(self):
        """Pure Sequential Async Main Loop as per Instruction 2."""
        self.log("DashBoard initialization of our tradingBot:")

        # Step: websocket open (implicit via ccxt pro)
        self.log("Step: websocket open")

        symbols = list(self.config.get('pairs', {}).keys())

        while not self.shutdown_event.is_set():
            try:
                if self.live: self.live.refresh()
                if self.ui: await self.ui.input_handler(self)

                # 1. Watch Balance
                self.log("Step: watchBalance")
                try:
                    # We use a timeout to keep the loop moving even if no balance update occurs
                    balance = await asyncio.wait_for(self.exchange.watch_balance(), timeout=2.0)
                    self.log("Balance updated.")
                except asyncio.TimeoutError:
                    self.log("watchBalance timeout, moving on")

                if self.live: self.live.refresh()

                # 2. Watch Orders For Symbols
                self.log("Step: watchOrdersForSymbols")
                for symbol in symbols:
                    try:
                        orders = await asyncio.wait_for(self.exchange.watch_orders(symbol), timeout=1.0)
                        self.log(f"Fetched {len(orders)} orders for {symbol}")
                    except asyncio.TimeoutError:
                        pass
                    except Exception as e:
                        self.log(f"watchOrders failed for {symbol}: {e}")
                    if self.live: self.live.refresh()

                # 3. Benchmark Sequentially on Symbols
                self.log("Step: benchmark sequentially on symbols")
                for symbol in symbols:
                    if self.shutdown_event.is_set(): break
                    self.log(f"Processing symbol: {symbol}")

                    try:
                        # fetch candles for 1m
                        self.log(f"Step: watchOHLCV for {symbol}")
                        ohlcv = None
                        try:
                            ohlcv = await asyncio.wait_for(self.exchange.watch_ohlcv(symbol, '1m', limit=100), timeout=2.0)
                        except asyncio.TimeoutError:
                            self.log(f"watchOHLCV timeout for {symbol}")

                        if not ohlcv:
                            self.log(f"No OHLCV for {symbol}, skipping.")
                            continue

                        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

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

                        self.log(f"Step: Analyzing {symbol}")
                        res = await perform_analysis_calculation(symbol, '1m', 60, df, self.global_pattern_pool, device, pinfo)

                        if res and 'error' not in res:
                            self.bot_state[symbol].update(res)

                            # Signal detection
                            if res.get('buy_signal') or res.get('sell_signal'):
                                self.signal_arrival_times[symbol] = time.time()

                                # Trade if balance ok, signal detected and monte carlo validates strategy
                                if res.get('buy_signal'):
                                    self.log(f"Signal: BUY detected for {symbol}")
                                    positions = self.bot_state[symbol].get('positions', [])
                                    if len(positions) >= 3:
                                        self.log(f"Aborted: Max concurrent positions reached for {symbol}")
                                    elif symbol in self.suspended_pairs:
                                        self.log(f"Aborted: {symbol} is suspended")
                                    else:
                                        self.log(f"Step: Monte Carlo validation for {symbol}")
                                        if self.engine.validate_trade_mc(symbol, self.bot_state[symbol], self.config):
                                            self.log(f"Step: Executing BUY for {symbol}")
                                            await execute_buy(
                                                self.exchange, self.data_manager, self.engine,
                                                symbol, self.bot_state[symbol], self.config,
                                                self.available_assets, self.suspended_pairs
                                            )
                                        else:
                                            self.log(f"Aborted: Monte Carlo validation failed for {symbol}")
                                elif res.get('sell_signal'):
                                    self.log(f"Signal: SELL detected for {symbol}")
                                    positions = self.bot_state[symbol].get('positions', [])
                                    for idx, pos in enumerate(positions):
                                        self.log(f"Step: Executing SELL for {symbol} (position {idx})")
                                        await execute_sell(
                                            self.exchange, self.data_manager, self.engine,
                                            symbol, self.bot_state[symbol], self.config, idx
                                        )
                                        break
                    except Exception as e:
                        logging.error(f"Error processing {symbol}: {e}")

                    if self.live: self.live.refresh()

                await asyncio.sleep(0.1)
            except Exception as e:
                logging.error(f"Main loop error: {e}")
                await asyncio.sleep(5)

    async def shutdown(self):
        self.shutdown_event.set()
        try:
            await self.exchange.close()
        except:
            pass

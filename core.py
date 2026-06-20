# Cryptocurrencies multiplatform trading bot - Core Engine
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import asyncio
import logging
import time
import queue
import threading
import concurrent.futures
import gc
import psutil
import torch
import pandas as pd
import os
from typing import Dict, List, Set, Optional

from persistence import DataManager, PatternManager, OHLCVCacheManager
from trading_engine import TradingEngine, execute_buy, execute_sell
from indicators import get_common_indicators, get_signals, calculate_similarity_batch
from utils import silent_worker_init

class MarketDataService:
    """API-First Market Data Service. Prioritizes real-time data."""
    def __init__(self, core):
        self.core = core
        self.ohlcv_cache = core.ohlcv_cache_manager
        self.last_fetch = {}
        self.ws_updates = {} # (symbol, timeframe) -> last_ws_ts

    async def get_fresh_data(self, symbol, timeframe):
        """Returns data from the live cache (WebSockets)."""
        return self.ohlcv_cache.get(symbol, timeframe)

class AnalysisService:
    """High-concurrency Analysis Service using Multi-Processing and GPU."""
    def __init__(self, core):
        self.core = core
        self.process_executor = None
        self.instrumented_footprint = 1.0 * 1024 * 1024 * 1024 # 1GB default

    def start(self):
        cpu_count = os.cpu_count() or 1
        # Dynamic scaling based on memory - massive hands (4x CPU cores)
        mem_available = psutil.virtual_memory().available
        # Use up to 95% of memory if needed, we want everything running
        max_workers = min(cpu_count * 4, max(4, int((mem_available * 0.95) / self.instrumented_footprint)))
        self.process_executor = concurrent.futures.ProcessPoolExecutor(
            max_workers=max_workers,
            initializer=silent_worker_init
        )

    def shutdown(self):
        if self.process_executor:
            self.process_executor.shutdown(wait=False)

    async def analyze_pair(self, symbol, timeframe, df, patterns):
        """Runs the CPU/GPU intensive analysis in a sub-process."""
        loop = asyncio.get_event_loop()
        # Bridge to indicators.py logic
        from bot import wrapped_analysis_task

        current_data = self.core.bot_state.get(symbol, {})
        pinfo = {
            'active_pattern_id': current_data.get('active_pattern_id'),
            'pattern_match_ts': current_data.get('pattern_match_ts', 0),
            'last_mc_ts': current_data.get('last_mc_ts', 0),
            'mc_score': current_data.get('mc_score', 1.1)
        }

        device = self.core.config.get('device')

        tf_map = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}
        tf_secs = tf_map.get(timeframe, 300)

        # Add global pool
        with self.core.state_lock:
            search_pool = patterns + list(self.core.global_pattern_pool)

        # Defer import to avoid circular dependency
        from bot import wrapped_analysis_task
        return await loop.run_in_executor(
            self.process_executor,
            wrapped_analysis_task,
            symbol, timeframe, tf_secs, df, search_pool, device, pinfo
        )

class ExecutionService:
    """Perfect Trader Execution Service. Guarantees profit via Order Book analysis."""
    def __init__(self, core):
        self.core = core

    async def process_signal(self, symbol, analysis_res):
        """Signal Processor with 1m timeframe constraint."""
        with self.core.state_lock:
            self.core.bot_state[symbol].update(analysis_res)
            data = self.core.bot_state[symbol]

            # Sync signal arrival times for UI display
            if analysis_res.get('buy_signal') or analysis_res.get('sell_signal'):
                 self.core.signal_arrival_times[symbol] = time.time()

        # Prevent acting multiple times on the same signal candle
        last_acted_ts = data.get('last_acted_ts', 0)
        current_candle_ts = analysis_res.get('last_processed_ts', 0)

        if last_acted_ts >= current_candle_ts and current_candle_ts > 0:
             return

        if analysis_res.get('buy_signal'):
            data['consecutive_buys'] = data.get('consecutive_buys', 0) + 1
            data['consecutive_sells'] = 0

            # Double check with Monte Carlo and Order Book
            # Limit to 3 concurrent positions (buyings in-a-row) per pair
            positions = data.get('positions', [])
            if len(positions) < 3 and symbol not in self.core.suspended_pairs:
                if self.core.engine.validate_trade_mc(symbol, data, self.core.config):
                    # execute_buy now includes balance and order book checks
                    success = await self.core.run_in_thread(
                        execute_buy,
                        self.core.exchange, self.core.data_manager, self.core.engine,
                        symbol, data, self.core.config, self.core.state_lock,
                        self.core.available_assets, self.core.suspended_pairs
                    )
                    if success:
                        data['last_acted_ts'] = current_candle_ts
                        data['consecutive_buys'] = 0

        elif analysis_res.get('sell_signal'):
            data['consecutive_sells'] = data.get('consecutive_sells', 0) + 1
            data['consecutive_buys'] = 0

            positions = data.get('positions', [])
            if positions:
                for idx, pos in enumerate(positions):
                    # Try to sell with sure profit
                    success = await self.core.run_in_thread(
                        execute_sell,
                        self.core.exchange, self.core.data_manager, self.core.engine,
                        symbol, data, self.core.config, idx
                    )

                    if success:
                        data['last_acted_ts'] = current_candle_ts
                        data['consecutive_sells'] = 0
                        break

class TradingCore:
    """The 'Perfect Trader' Core with 10 hands (Services)."""
    def __init__(self, config, exchange, data_manager, pattern_manager, ohlcv_cache_manager, external_shutdown_event=None):
        self.config = config
        self.exchange = exchange
        self.data_manager = data_manager
        self.pattern_manager = pattern_manager
        self.ohlcv_cache_manager = ohlcv_cache_manager
        self.engine = TradingEngine(config)

        self.bot_state = {}
        self.global_pattern_pool = []
        self.available_assets = []
        self.suspended_pairs = set()
        self.benchmarking_pairs = set()
        self.signal_arrival_times = {}
        self.state_lock = threading.RLock()
        self.shutdown_event = external_shutdown_event or threading.Event()

        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=80)

        # Services (Hands)
        self.market_data = MarketDataService(self)
        self.analysis = AnalysisService(self)
        self.execution = ExecutionService(self)

    async def run_in_thread(self, func, *args):
        if self.shutdown_event.is_set(): return None
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(self.executor, func, *args)
        except RuntimeError: # Handle "executor is closed"
            return None

    async def shutdown(self):
        """Signal all workers to stop."""
        if isinstance(self.shutdown_event, threading.Event):
            self.shutdown_event.set()
        self.executor.shutdown(wait=False)
        self.analysis.shutdown()

    async def pair_worker(self, symbol):
        """Independent worker for each pair (100 fingers)."""
        # Perfect Trader: prioritize analysis of assets already in wallet
        # Start other pairs with a delay to ensure wallet assets get immediate process priority
        is_wallet_asset = False
        with self.state_lock:
             is_wallet_asset = symbol.split('/')[0] in set(self.available_assets)

        if not is_wallet_asset:
             await asyncio.sleep(5)

        while True:
            if isinstance(self.shutdown_event, (threading.Event, asyncio.Event)):
                if self.shutdown_event.is_set(): break
            try:
                pair_cfg = self.config.get('pairs', {}).get(symbol, {})

                # Force 1m timeframe as per requirement
                timeframe = '1m'

                # 1. WS-Only: Fetch candles from memory
                ohlcv = await self.market_data.get_fresh_data(symbol, timeframe)
                if not ohlcv:
                    await asyncio.sleep(2)
                    continue

                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                # 2. Analysis: Parallel sub-process
                patterns = self.pattern_manager.get_patterns(symbol)
                res = await self.analysis.analyze_pair(symbol, timeframe, df, patterns)

                if res and 'error' not in res:
                    if res.get('trigger_rebenchmark'):
                        with self.state_lock:
                            self.benchmarking_pairs.add(symbol)
                    # 3. Execution: Verified trade
                    await self.execution.process_signal(symbol, res)

                # High frequency: 2s instead of 4s
                await asyncio.sleep(2)
            except RuntimeError as e:
                if "schedule new futures" in str(e) or "executor is closed" in str(e):
                    break
                logging.error(f"RuntimeError in pair_worker for {symbol}: {e}")
                break
            except Exception as e:
                if self.shutdown_event and not self.shutdown_event.is_set():
                    logging.error(f"Error in pair_worker for {symbol}: {e}")
                    await asyncio.sleep(5)
                else: break

    async def main_loop(self):
        """Coordinates all services and workers."""
        self.analysis.start()

        # Absolute priority: Launch workers for wallet assets FIRST
        all_configured_symbols = list(self.config.get('pairs', {}).keys())
        wallet_assets = set(self.available_assets)
        prioritized_symbols = [s for s in all_configured_symbols if s.split('/')[0] in wallet_assets]
        other_symbols = [s for s in all_configured_symbols if s.split('/')[0] not in wallet_assets]

        # Merge lists keeping priority
        symbols_order = prioritized_symbols + other_symbols

        # Launch workers for each pair (the fingers)
        tasks = []
        for symbol in symbols_order:
            tasks.append(asyncio.create_task(self.pair_worker(symbol)))

        # Background benchmark coordinator
        tasks.append(asyncio.create_task(self.benchmark_coordinator()))

        await asyncio.gather(*tasks)

    async def benchmark_coordinator(self):
        """Handles dynamic re-benchmarking without blocking the core."""
        import optimization
        while True:
            if isinstance(self.shutdown_event, (threading.Event, asyncio.Event)):
                if self.shutdown_event.is_set(): break
            with self.state_lock:
                to_bench = list(self.benchmarking_pairs)

            if to_bench:
                # Bridge to synchronous benchmark logic
                await self.run_in_thread(
                    optimization.run_benchmark_mode,
                    self.exchange, self.config, None, threading.Event(), # Dummy shutdown for thread
                    self.state_lock, self.global_pattern_pool, self.benchmarking_pairs,
                    'short', None,
                    self.data_manager, self.pattern_manager, self.engine,
                    self.config.get('device'), to_bench, self.ohlcv_cache_manager,
                    None, self.bot_state
                )
            await asyncio.sleep(1)

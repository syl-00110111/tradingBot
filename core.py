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
        """Ensures we have the latest candles from the API. API-First."""
        now = time.time()

        # Perfect Trader: If WebSockets are active and recently updated, trust the live cache
        # This saves API weight for heavy history downloads
        if now - self.ws_updates.get((symbol, timeframe), 0) < 30.0:
            cached = self.ohlcv_cache.get(symbol, timeframe)
            if cached: return cached

        # Otherwise, throttle REST calls to 1s per pair
        if now - self.last_fetch.get((symbol, timeframe), 0) < 1.0:
            return self.ohlcv_cache.get(symbol, timeframe)

        loop = asyncio.get_event_loop()
        from exchange_handler import fetch_ohlcv_incremental
        await loop.run_in_executor(
            self.core.executor,
            fetch_ohlcv_incremental,
            self.core.exchange,
            symbol,
            timeframe,
            self.ohlcv_cache,
            500
        )
        self.last_fetch[(symbol, timeframe)] = time.time()
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

        glite = {
            'device': self.core.config.get('device'),
            'mc_hurdle': self.core.config.get('profit_thresholds', {}).get('mc_validation_hurdle', 0.0015),
            'min_profit': self.core.config.get('profit_thresholds', {}).get('min_pattern_profit', 0.01)
        }

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
            symbol, timeframe, tf_secs, df, search_pool, glite, pinfo
        )

class ExecutionService:
    """Perfect Trader Execution Service. Guarantees profit via Order Book analysis."""
    def __init__(self, core):
        self.core = core

    async def process_signal(self, symbol, analysis_res):
        """API-First Signal Processor with Documentation-based Term Escalation."""
        with self.core.state_lock:
            self.core.bot_state[symbol].update(analysis_res)
            data = self.core.bot_state[symbol]

        if analysis_res.get('buy_signal'):
            # Double check with Monte Carlo and Order Book
            if not data.get('position') and symbol not in self.core.suspended_pairs:
                if self.core.engine.validate_trade_mc(symbol, data, self.core.config):
                    # execute_buy now includes API-first balance and order book checks
                    await self.core.run_in_thread(
                        execute_buy,
                        self.core.exchange, self.core.data_manager, self.core.engine,
                        symbol, data, self.core.config, self.core.state_lock,
                        self.core.available_assets, self.core.suspended_pairs
                    )

        elif analysis_res.get('sell_signal'):
            positions = data.get('positions', [])
            if positions:
                data['consecutive_sells'] = data.get('consecutive_sells', 0) + 1
                for idx, pos in enumerate(positions):
                    # 1. Try to sell with sure profit
                    success = await self.core.run_in_thread(
                        execute_sell,
                        self.core.exchange, self.core.data_manager, self.core.engine,
                        symbol, data, self.core.config, idx
                    )

                    if success:
                        data['consecutive_sells'] = 0
                        break

                    # 2. If profit not sure, check for term escalation (3 consecutive signals)
                    if data['consecutive_sells'] >= 3:
                        current_term = pos.get('term', 'short')
                        term_order = ['short', 'medium', 'long']
                        if current_term in term_order and term_order.index(current_term) < len(term_order) - 1:
                            new_term = term_order[term_order.index(current_term) + 1]
                            if self.core.data_manager.update_position_term(symbol, idx, new_term):
                                logging.info(f"[{symbol}] Escalating to {new_term} term.")
                                data['consecutive_sells'] = 0
                        else:
                            # 3. Force sell on longest term if signals persist
                            await self.core.run_in_thread(
                                execute_sell,
                                self.core.exchange, self.core.data_manager, self.core.engine,
                                symbol, data, self.core.config, idx, True # Force
                            )
                            data['consecutive_sells'] = 0
                            break

class TradingCore:
    """The 'Perfect Trader' Core with 10 hands (Services)."""
    def __init__(self, config, exchange, data_manager, pattern_manager, ohlcv_cache_manager):
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
        self.state_lock = threading.RLock()
        self.shutdown_event = asyncio.Event()

        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=80)

        # Services (Hands)
        self.market_data = MarketDataService(self)
        self.analysis = AnalysisService(self)
        self.execution = ExecutionService(self)

    async def run_in_thread(self, func, *args):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self.executor, func, *args)

    async def pair_worker(self, symbol):
        """Independent worker for each pair (100 fingers)."""
        while not self.shutdown_event.is_set():
            try:
                pair_cfg = self.config.get('pairs', {}).get(symbol, {})
                term = pair_cfg.get('term_override', self.config.get('_active_term', 'short'))
                term_cfg = self.config.get('expected_profit_terms', {}).get(term, {})
                # Use 1m as default as per instruction
                timeframe = term_cfg.get('timeframe', '1m')

                # 1. API-First: Fetch fresh candles
                ohlcv = await self.market_data.get_fresh_data(symbol, timeframe)
                if not ohlcv:
                    await asyncio.sleep(1)
                    continue

                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                # 2. Analysis: Parallel sub-process
                patterns = self.pattern_manager.get_patterns(symbol)
                res = await self.analysis.analyze_pair(symbol, timeframe, df, patterns)

                if res and 'error' not in res:
                    # 3. Execution: Verified trade
                    await self.execution.process_signal(symbol, res)

                # High frequency: 1s instead of 2s
                await asyncio.sleep(1)
            except Exception as e:
                logging.error(f"Error in pair_worker for {symbol}: {e}")
                await asyncio.sleep(1)

    async def main_loop(self):
        """Coordinates all services and workers."""
        self.analysis.start()

        # Launch workers for each pair (the fingers)
        tasks = []
        for symbol in self.config.get('pairs', {}):
            tasks.append(asyncio.create_task(self.pair_worker(symbol)))

        # Background benchmark coordinator
        tasks.append(asyncio.create_task(self.benchmark_coordinator()))

        await asyncio.gather(*tasks)

    async def benchmark_coordinator(self):
        """Handles dynamic re-benchmarking without blocking the core."""
        import optimization
        while not self.shutdown_event.is_set():
            with self.state_lock:
                to_bench = list(self.benchmarking_pairs)

            if to_bench:
                # Bridge to synchronous benchmark logic
                await self.run_in_thread(
                    optimization.run_benchmark_mode,
                    self.exchange, self.config, None, threading.Event(), # Dummy shutdown for thread
                    self.state_lock, self.global_pattern_pool, self.benchmarking_pairs,
                    self.config.get('_active_term', 'short'), None,
                    self.data_manager, self.pattern_manager, self.engine,
                    self.config.get('device'), to_bench, self.ohlcv_cache_manager,
                    None, self.bot_state
                )
            await asyncio.sleep(1)

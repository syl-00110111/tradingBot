"""
Symbol and trading-count utilities extracted from botv4.py
Provides:
- updateTradingCount(symbol, exchange, volumes_file='volumes_trades_data.json') -> int
- computeSymbols(balance, previousPairs=None, markets_file='markets.json', volumes_file='volumes_trades_data.json', forbidAssets=None, baseAssets=None, maxNumPairs=50, miniCount=600)

These functions use market_utils for logging, rate limit and JSON helpers.
"""
from typing import Any, List, Dict, Optional
import json
import os
import random

import market_utils


def updateTradingCount(symbol: Optional[str], exchange: Any, volumes_file: str = 'volumes_trades_data.json') -> int:
    """Update trades count for symbol by fetching recent trades and persisting volumes file.
    Returns new trades_count (int) or 0 on failure.
    """
    try:
        with open(volumes_file, 'r') as f:
            _volumes = json.load(f)
    except Exception as e:
        raise ValueError(f"volume trades data file problem: {e}")
    trades_count = 0
    for _vol in _volumes:
        if symbol == _vol.get('symbol') and symbol is not None:
            _since = _vol.get('timestamp')
            now_minus_4h = int(__import__('time').time()) * 1000 - (4*3600*1000)
            if _since is None:
                _since_int = now_minus_4h
            else:
                try:
                    _since_int = int(_since)
                except Exception:
                    _since_int = now_minus_4h
            if (_since_int >= now_minus_4h) or _vol.get('trades_count') == 1000:
                try:
                    market_utils.respect_rate_limit(exchange)
                    trades = exchange.fetch_trades(symbol, now_minus_4h)
                    trades_count = len(trades) if trades is not None else 0
                    # update
                    _vol['trades_count'] = trades_count
                    _vol['timestamp'] = int(__import__('time').time())
                except Exception as e:
                    market_utils.log_exception(e, "updateTradingCount:fetch_trades")
                break
    try:
        try:
            market_utils.write_json_atomic(volumes_file, _volumes)
        except Exception as e:
            market_utils.log_exception(e, "updateTradingCount:backup")
            with open(volumes_file, 'w') as f:
                json.dump(_volumes, f, indent=4)
    except Exception as e:
        market_utils.log_exception(e, "updateTradingCount:write")
    return trades_count


def computeSymbols(balance: Dict, previousPairs: Optional[List] = None, markets_file: str = 'markets.json', volumes_file: str = 'volumes_trades_data.json', forbidAssets: Optional[List] = None, baseAssets: Optional[List] = None, maxNumPairs: int = 50, miniCount: int = 600) -> List:
    """Compute list of symbols to track based on balance, markets and volumes.
    Returns list of symbol entries similar to original botv4 computeSymbols.
    """
    __symbols = []
    if forbidAssets is None:
        forbidAssets = ['AKE', 'DCR', 'USDT', 'XMR']
    if baseAssets is None:
        baseAssets = ["USD", "EUR"]

    # handle balance cleanup
    sourceAssets: List[str] = []
    if isinstance(balance, dict):
        free_balances = balance.get('free', {}) or {}
        if isinstance(free_balances, dict):
            for asset, amount in free_balances.items():
                try:
                    if float(amount) > 0:
                        sourceAssets.append(asset)
                except Exception:
                    market_utils.log_exception(Exception(f"bad balance amount for {asset}"), "computeSymbols:balance_asset")
    # read markets and volumes
    try:
        with open(markets_file, 'r') as f:
            _markets = json.load(f)
        with open(volumes_file, 'r') as f:
            _volumes = json.load(f)
    except Exception as e:
        market_utils.log_exception(e, "computeSymbols:read_files")
        return __symbols

    # existing_symbols
    if previousPairs is None:
        previousPairs = []
    try:
        existing_symbols = {str(p[0]).upper() for p in previousPairs if isinstance(p, (list, tuple)) and len(p) > 0 and p[0] is not None}
    except Exception as e:
        market_utils.log_exception(e, "computeSymbols:existing_symbols")
        existing_symbols = set()

    _g = {'id': []}
    for _v in _volumes:
        try:
            if _v.get('trades_count', 0) > miniCount:
                _g['id'].append(_v.get('id'))
        except Exception:
            continue

    sell_candidates = []
    volume_candidates = []

    # _markets can be dict or list
    try:
        items = _markets.items() if isinstance(_markets, dict) else list(_markets)
    except Exception:
        items = []

    for m in items:
        try:
            market = m[1] if isinstance(m, tuple) else m
            mb = market.get('base') if isinstance(market, dict) else None
            mq = market.get('quote') if isinstance(market, dict) else None
            mid = market.get('id') if isinstance(market, dict) else None
            symbol = market.get('symbol') if isinstance(market, dict) else None
            if not mb or not mq or not mid or not symbol:
                continue
            if mb in forbidAssets or mq in forbidAssets:
                continue
            if (mid in _g.get('id')) and (mq in baseAssets):
                a = [symbol, mid, mb, mq, market.get('limits', {}).get('amount', {}).get('min'), market.get('precision', {}).get('price'), market.get('precision', {}).get('amount')]
                if (mb in sourceAssets) and (str(a[0]).upper() not in existing_symbols):
                    sell_candidates.append(a)
                    existing_symbols.add(str(a[0]).upper())
                elif str(a[0]).upper() not in existing_symbols:
                    volume_candidates.append(a)
                    existing_symbols.add(str(a[0]).upper())
        except Exception as e:
            market_utils.log_exception(e, "computeSymbols:market_loop")
            continue

    combined = []
    for item in sell_candidates:
        if len(combined) >= maxNumPairs:
            break
        combined.append(item)
    for item in volume_candidates:
        if len(combined) >= maxNumPairs:
            break
        combined.append(item)
    __symbols.extend(combined)
    return __symbols

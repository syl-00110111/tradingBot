"""
Utilities for market data and balance computation extracted from botv4.py
This module provides:
- respect_rate_limit
- log_exception
- write_json_atomic / read_json_file
- fetch_ohlcv_data
- check_candles_consistency
- fetch_balance

The functions accept needed parameters to avoid tight coupling with botv4 globals.
"""
import time
import os
import json
import traceback
from typing import Any, Dict, List, Optional
import pandas as pd


def log_exception(e: Exception, ctx: Optional[str] = None) -> None:
    try:
        prefix = f"[{ctx}] " if ctx else ""
        print(f"{prefix}Exception: {e}")
        tb = traceback.format_exc()
        print(tb)
    except Exception:
        pass


def respect_rate_limit(exchange: Any) -> None:
    try:
        ms = getattr(exchange, 'rateLimit', 200) or 200
        time.sleep(ms / 1000)
    except Exception:
        try:
            time.sleep(0.2)
        except Exception:
            pass


def write_json_atomic(path: str, data: Any) -> bool:
    try:
        try:
            # try to use safe write if available
            import safe_json
            safe_json.atomic_write_json(path, data, backup=True, indent=2)
        except Exception:
            with open(path, 'w') as f:
                json.dump(data, f, indent=2)
        return True
    except Exception as e:
        log_exception(e, f"write_json_atomic:{path}")
        return False


def read_json_file(path: str, default=None):
    try:
        if os.path.exists(path):
            with open(path, 'r') as f:
                return json.load(f)
    except Exception as e:
        log_exception(e, f"read_json_file:{path}")
    return default


def check_candles_consistency(symbol: str, expected_interval_ms: int = 60000) -> List:
    try:
        fpath = f"ohlcv_data_{symbol}_1m.json"
        if not os.path.exists(fpath):
            return []
        with open(fpath, 'r') as f:
            data = json.load(f)
        if not isinstance(data, list) or len(data) < 2:
            return []
        prev = int(data[0][0])
        issue = None
        bad_index = None
        for i, row in enumerate(data[1:], start=1):
            try:
                ts = int(row[0])
            except Exception as e:
                log_exception(e, "check_candles_consistency:parse_ts")
                issue = 'invalid timestamp'
                bad_index = i
                break
            if ts <= prev:
                issue = 'non-increasing timestamp'
                bad_index = i
                break
            diff = ts - prev
            if diff > expected_interval_ms * 1.5:
                issue = f'gap {diff}ms'
                bad_index = i
                break
            prev = ts
        if issue and bad_index is not None:
            try:
                new_data = data[bad_index:]
                try:
                    write_json_atomic(fpath, new_data)
                except Exception:
                    with open(fpath, 'w') as f:
                        json.dump(new_data, f, indent=2)
            except Exception as e:
                log_exception(e, "check_candles_consistency:trim")
            return [(fpath, bad_index, issue)]
        return []
    except Exception as e:
        log_exception(e, f"check_candles_consistency:{symbol}")
        return []


def fetch_ohlcv_data(_id: str, symbol: str, exchange: Any) -> 'pd.DataFrame':
    """Return OHLCV data as a pandas.DataFrame with columns ['timestamp','open','high','low','close','volume'].
    Falls back to an empty DataFrame when no data is available.
    """
    # This function returns a DataFrame of ohlcv rows or an empty DataFrame
    dataFile = f'ohlcv_data_{_id}_1m.json'
    data2 = []
    existing_data = []
    if os.path.exists(dataFile):
        with open(dataFile, 'r') as f:
            try:
                data2 = json.load(f)
            except Exception as e:
                log_exception(e, f"fetch_ohlcv_data:parse:{dataFile}")
                data2 = []
            if not isinstance(data2, list):
                data2 = []
            try:
                if len(data2) > 0:
                    lastTimestamp = int(data2[-1][0])
                else:
                    lastTimestamp = None
            except Exception as e:
                log_exception(e, "fetch_ohlcv_data:invalid_last_ts")
                lastTimestamp = None
        existing_data = data2
        currentTimestamp = int(time.time()*1000)
        data = []
        if lastTimestamp is None or lastTimestamp < currentTimestamp:
            since = lastTimestamp
            try:
                while True:
                    respect_rate_limit(exchange)
                    batch = exchange.fetch_ohlcv(symbol, '1m', since)
                    if not batch:
                        break
                    data.extend(batch)
                    last_ts = int(batch[-1][0])
                    if last_ts <= (since or 0):
                        break
                    since = last_ts + 60 * 1000
                    if last_ts >= currentTimestamp - 60*1000:
                        break
            except Exception as e:
                log_exception(e, f"fetch_ohlcv_data:batch_fetch:{symbol}")
                try:
                    respect_rate_limit(exchange)
                    data = exchange.fetch_ohlcv(symbol, '1m')
                except Exception as e2:
                    log_exception(e2, f"fetch_ohlcv_data:fallback:{symbol}")
                    data = []
        # merge existing_data + data and write back
        merged = existing_data[:]
        try:
            if data:
                merged.extend(data)
            try:
                write_json_atomic(dataFile, merged)
            except Exception as e:
                log_exception(e, f"fetch_ohlcv_data:write:{dataFile}")
        except Exception as e:
            log_exception(e, f"fetch_ohlcv_data:merge:{symbol}")
        # return as DataFrame
        try:
            if merged:
                return pd.DataFrame(merged, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        except Exception:
            pass
        return pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    else:
        try:
            respect_rate_limit(exchange)
            data = exchange.fetch_ohlcv(symbol, '1m')
            try:
                write_json_atomic(dataFile, data)
            except Exception as e:
                log_exception(e, f"fetch_ohlcv_data:write_new:{dataFile}")
            try:
                if data:
                    return pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            except Exception:
                pass
            return pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        except Exception as e:
            log_exception(e, f"fetch_ohlcv_data:initial_fetch:{symbol}")
            return pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])


def fetch_balance(exchange: Any, startup: bool, config: Dict, markets_src: Any, pending_dump_file: str, base_assets: List[str]) -> Dict:
    """
    A focused version of fetch_balance that computes asset_avg_prices and persists balance.
    Returns balance dict as fetched from exchange with added 'asset_avg_prices'.
    """
    try:
        balance = exchange.fetch_balance()
        balance['timestamp'] = int(time.time())
        try:
            write_json_atomic("balance.json", balance)
        except Exception as e:
            log_exception(e, "fetch_balance:backup_write")
        asset_avg_prices = {}
        quote_preferred = config.get('quote_asset') if isinstance(config, dict) else None
        # ensure markets_src provided
        try:
            items = markets_src.items() if isinstance(markets_src, dict) else list(markets_src)
        except Exception:
            items = []
        free_balances = balance.get('free', {}) or {}
        for asset, amt in free_balances.items():
            try:
                if amt is None or float(amt) <= 0:
                    continue
            except Exception:
                continue
            if asset in base_assets:
                continue
            prices_by_quote = {}
            market_symbols_by_quote = {}
            for m in items:
                try:
                    market = m[1] if isinstance(m, tuple) else m
                    m_base = market.get('base') if isinstance(market, dict) else None
                    m_quote = market.get('quote') if isinstance(market, dict) else None
                    m_id = market.get('id') if isinstance(market, dict) else None
                    if not m_base or not m_quote or not m_id:
                        continue
                    if str(m_base).upper() != str(asset).upper():
                        continue
                    if str(m_quote).upper() not in [b.upper() for b in base_assets]:
                        continue
                    fpath = f"ohlcv_data_{m_id}_1m.json"
                    if not os.path.exists(fpath):
                        continue
                    try:
                        with open(fpath, 'r') as fh:
                            data = json.load(fh)
                    except Exception:
                        continue
                    closes = []
                    for row in data:
                        try:
                            closes.append(float(row[4]))
                        except Exception:
                            continue
                    if closes:
                        q = str(m_quote).upper()
                        prices_by_quote.setdefault(q, []).append(sum(closes) / len(closes))
                        try:
                            msym = market.get('symbol') if isinstance(market, dict) else None
                        except Exception:
                            msym = None
                        if not msym:
                            msym = m_id
                        market_symbols_by_quote.setdefault(q, []).append((msym, m_id))
                except Exception:
                    continue
            try:
                avg_by_quote = {q: (sum(vals) / len(vals)) for q, vals in prices_by_quote.items() if vals}
            except Exception:
                avg_by_quote = {}
            additional_prices_by_quote = {}
            try:
                if os.path.exists(pending_dump_file):
                    with open(pending_dump_file, 'r') as pf:
                        pending_snapshots = json.load(pf)
                else:
                    pending_snapshots = []
            except Exception:
                pending_snapshots = []
            for snap in pending_snapshots:
                try:
                    order = snap.get('order') if isinstance(snap, dict) else None
                    if not isinstance(order, dict):
                        continue
                    sym = order.get('symbol') or (order.get('info') or {}).get('symbol') or (order.get('info') or {}).get('pair')
                    if not isinstance(sym, str):
                        continue
                    parts = __import__('re').split(r"[/:_-]", sym)
                    if not parts:
                        continue
                    base_from_order = parts[0].upper()
                    quote_from_order = parts[1].upper() if len(parts) > 1 else None
                    if base_from_order != str(asset).upper():
                        continue
                    price = None
                    for k in ('price', 'rate'):
                        try:
                            price = float(order.get(k)) if order.get(k) is not None else price
                        except Exception:
                            pass
                    if price is None:
                        info = order.get('info') or {}
                        try:
                            price = float(info.get('price') or info.get('rate') or info.get('price_1'))
                        except Exception:
                            price = None
                    if price is None:
                        continue
                    q = quote_from_order or 'UNKNOWN'
                    additional_prices_by_quote.setdefault(q, []).append(price)
                except Exception:
                    continue
            # combine
            combined_avg_by_quote = {}
            for q, vals in avg_by_quote.items():
                combined = [vals]
                extra = additional_prices_by_quote.get(q.upper(), []) or additional_prices_by_quote.get(q, [])
                if extra:
                    combined.extend(extra)
                try:
                    combined_avg_by_quote[q] = sum(combined) / len(combined)
                except Exception:
                    combined_avg_by_quote[q] = vals
            for q, vals in additional_prices_by_quote.items():
                if q not in combined_avg_by_quote and vals:
                    try:
                        combined_avg_by_quote[q] = sum(vals) / len(vals)
                    except Exception:
                        continue
            asset_avg_prices[asset] = combined_avg_by_quote
        balance['asset_avg_prices'] = asset_avg_prices
        try:
            write_json_atomic('asset_avg_prices.json', asset_avg_prices)
        except Exception:
            pass
        return balance
    except Exception as e:
        log_exception(e, "fetch_balance:main")
        return {}

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
from datetime import datetime
import pandas as pd
import safe_json


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


def check_candles_consistency(symbol: str, expected_interval_ms: int = 60000, console: Optional[Any] = None) -> List:
    # Verify temporal coherence for the symbol's local OHLCV cache.
    # If an inconsistency is detected, discard all data chronologically preceding the first inconsistency
    # and persist the trimmed file.
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
        # Parcourir les lignes à partir de la deuxième
        for i, row in enumerate(data[1:], start=1):
            try:
                ts = int(row[0])
            except Exception:
                issue = 'invalid timestamp'
                bad_index = i
                break
            if ts <= prev:
                issue = 'non-increasing timestamp'
                bad_index = i
                break
            diff = ts - prev
            # allow a small tolerance (50%) for missing/faster samples
            if diff > expected_interval_ms * 1.5:
                issue = f'gap {diff}ms'
                bad_index = i
                break
            prev = ts
        if issue and bad_index is not None:
            msg = f"Candle inconsistency detected: file={fpath} index={bad_index} issue={issue}"
            if console:
                console.print(msg)
            else:
                print(msg)
            try:
                # keep only data from the first good candle (bad_index) onwards
                new_data = data[bad_index:]
                try:
                    safe_json.atomic_write_json(fpath, new_data, backup=True, indent=2)
                except Exception:
                    with open(fpath, 'w') as f:
                        json.dump(new_data, f, indent=2)
                msg_trim = f"Trimmed {fpath}: removed {bad_index} entries before inconsistency"
                if console:
                    console.print(msg_trim)
                else:
                    print(msg_trim)
            except Exception as e:
                msg_fail = f"Failed to trim candles file {fpath}: {e}"
                if console:
                    console.print(msg_fail)
                else:
                    print(msg_fail)
            return [(fpath, bad_index, issue)]
        return []
    except Exception as e:
        msg_exc = f"check_candles_consistency failed for {symbol}: {e}"
        if console:
            console.print(msg_exc)
        else:
            print(msg_exc)
        return []


def fetch_ohlcv_data(exchange: Any, _id: str, symbol: str, pausedForBuy: Optional[Dict] = None, PAUSE_FILE: str = 'paused_for_buy.json', console: Optional[Any] = None, timeframe: str = '1m', limit: Optional[int] = None) -> pd.DataFrame:
    if pausedForBuy is None:
        pausedForBuy = {}

    rate_limit_ms = getattr(exchange, 'rateLimit', 1000) or 1000
    time.sleep(rate_limit_ms / 1000) # time.sleep wants seconds

    dataFile = f"ohlcv_data_{_id}_{timeframe}.json"
    data2 = []
    existing_data = []
    # Charger les données existantes si le fichier existe
    if os.path.exists(dataFile):
        with open(dataFile, 'r') as f:
            try:
                data2 = json.load(f)
            except Exception as e:
                msg_warn = f"Warning: failed to parse {dataFile} for {symbol}: {e}"
                if console:
                    console.print(msg_warn)
                else:
                    print(msg_warn)
                data2 = []
            # ensure data2 is a list of lists
            if not isinstance(data2, list):
                data2 = []
            try:
                if len(data2) > 0:
                    lastTimestamp = int(data2[-1][0])  # Utilisation de [-1] pour le dernier élément
                else:
                    lastTimestamp = None
            except (IndexError, TypeError, ValueError) as e:
                msg_warn2 = f"Warning: invalid last timestamp in cache {dataFile} for {symbol}: {e}"
                if console:
                    console.print(msg_warn2)
                else:
                    print(msg_warn2)
                lastTimestamp = None
        # conserver les données précédentes
        existing_data = data2
        currentTimestamp = int(time.time()*1000)  # Current timestamp in milliseconds

        data = []
        if lastTimestamp is not None and lastTimestamp < currentTimestamp:
            # fetch en boucles avec 'since' = lastTimestamp (ccxt attend un timestamp absolu en ms)
            since = lastTimestamp
            try:
                while True:
                    time.sleep(rate_limit_ms / 1000)
                    batch = exchange.fetch_ohlcv(symbol, timeframe, since)
                    if not batch:
                        break
                    data.extend(batch)
                    last_ts = int(batch[-1][0])
                    if last_ts <= since:
                        break
                    tf_ms = 60 * 1000
                    if timeframe == '4h':
                        tf_ms = 4 * 60 * 60 * 1000
                    since = last_ts + tf_ms
                    if last_ts >= currentTimestamp - tf_ms:
                        break
            except Exception as e:
                msg_warn3 = f"Warning: fetch_ohlcv batch failed for {symbol}: {e}"
                if console:
                    console.print(msg_warn3)
                else:
                    print(msg_warn3)
                try:
                    time.sleep(rate_limit_ms / 1000)
                    if limit is not None:
                        data = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
                    else:
                        data = exchange.fetch_ohlcv(symbol, timeframe)
                except Exception as e2:
                    msg_fail = f"Fallback fetch failed for {symbol}: {e2}"
                    if console:
                        console.print(msg_fail)
                    else:
                        print(msg_fail)
                    data = []
    # sinon si le fichier n'existe pas
    else:
        try:
            if limit is not None:
                data = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            else:
                data = exchange.fetch_ohlcv(symbol, timeframe)
        except Exception as e:
            msg_warn4 = f"Warning: initial fetch_ohlcv failed for {symbol}: {e}"
            if console:
                console.print(msg_warn4)
            else:
                print(msg_warn4)
            data = []
        if data is None:
            data = []

    # ensure data is iterable/list before using
    if data is None:
        data = []
    _len = len(data)

    # pause this pair for 8 hours if the OHLCV fetch is empty
    if _len == 0:
        msg_empty = f"No new OHLCV for {symbol}: fetched 0 candles. existing cache size={len(existing_data) if existing_data is not None else 0}"
        if console:
            console.print(msg_empty)
        else:
            print(msg_empty)
        # persist cache (to keep existing data untouched)
        try:
            try:
                safe_json.atomic_write_json(dataFile, existing_data if existing_data is not None else [], backup=True, indent=4)
            except Exception:
                with open(dataFile, 'w') as f:
                    json.dump(existing_data if existing_data is not None else [], f, indent=4)
        except Exception as e:
            msg_warn5 = f"Warning: failed to write ohlcv cache for {symbol}: {e}"
            if console:
                console.print(msg_warn5)
            else:
                print(msg_warn5)
        # pause buys for this symbol for 8 hours
        try:
            expiry_ts = int(time.time()) + 8 * 3600
            pausedForBuy[symbol] = expiry_ts
            try:
                safe_json.atomic_write_json(PAUSE_FILE, pausedForBuy, backup=True)
            except Exception:
                with open(PAUSE_FILE, 'w') as f:
                    json.dump(pausedForBuy, f)
            msg_pause = f"Paused buys for {symbol} until {datetime.fromtimestamp(expiry_ts)} due to empty OHLCV fetch"
            if console:
                console.print(msg_pause)
            else:
                print(msg_pause)
        except Exception as e:
            msg_fail2 = f"Failed to persist pausedForBuy for {symbol}: {e}"
            if console:
                console.print(msg_fail2)
            else:
                print(msg_fail2)
        return pd.DataFrame(existing_data if existing_data is not None else [], columns=['timestamp','open','high','low','close','volume'])

    # Retirer les doublons par timestamp
    for data_point in data:
        timestamp_to_remove = data_point[0]
        # Parcourir les bougies pour trouver celle à supprimer
        for j, candle in enumerate(existing_data):
            try:
                if candle[0] == timestamp_to_remove:
                    del existing_data[j]
                    break
            except Exception:
                continue
    # Ajouter les nouvelles données
    existing_data.extend(data)
    # Sauvegarder atomiquement
    try:
        try:
            safe_json.atomic_write_json(dataFile, existing_data, backup=True, indent=4)
        except Exception:
            with open(dataFile, 'w') as f:
                json.dump(existing_data, f, indent=4)
    except Exception as e:
        msg_warn6 = f"Warning: failed to write ohlcv cache for {symbol}: {e}"
        if console:
            console.print(msg_warn6)
        else:
            print(msg_warn6)
    return pd.DataFrame(existing_data, columns=['timestamp','open','high','low','close','volume'])


def fetch_balance(exchange: Any, console: Optional[Any] = None) -> Dict:
    balance = exchange.fetch_balance()
    balance['timestamp'] = int(time.time())
    try:
        try:
            safe_json.atomic_write_json("balance.json", balance, backup=True, indent=4)
        except Exception:
            with open("balance.json", 'w') as f:
                json.dump(balance, f, indent=4)
    except Exception as e:
        msg_exc = f"Balance backup file exception: {e}"
        if console:
            console.print(msg_exc)
        else:
            print(msg_exc)
    return balance

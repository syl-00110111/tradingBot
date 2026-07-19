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


def check_candles_consistency(symbol, expected_interval_ms=60000):
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
            console.print(f"Candle inconsistency detected: file={fpath} index={bad_index} issue={issue}")
            try:
                # keep only data from the first good candle (bad_index) onwards
                new_data = data[bad_index:]
                try:
                    safe_json.atomic_write_json(fpath, new_data, backup=True, indent=2)
                except Exception:
                    with open(fpath, 'w') as f:
                        json.dump(new_data, f, indent=2)
                console.print(f"Trimmed {fpath}: removed {bad_index} entries before inconsistency")
            except Exception as e:
                console.print(f"Failed to trim candles file {fpath}: {e}")
            return [(fpath, bad_index, issue)]
        return []
    except Exception as e:
        console.print(f"check_candles_consistency failed for {symbol}: {e}")
        return []

def fetch_ohlcv_data(_id, symbol):
    time.sleep (exchange.rateLimit / 1000) # time.sleep wants seconds
    # console.print(f"Fetching OHLCV data for {symbol}...")
    dataFile = 'ohlcv_data_'+ _id + '_1m' + '.json'
    data2 = []
    existing_data = []
    # Charger les données existantes si le fichier existe
    if os.path.exists(dataFile):
        with open(dataFile, 'r') as f:
            try:
                data2 = json.load(f)
            except Exception as e:
                console.print(f"Warning: failed to parse {dataFile} for {symbol}: {e}")
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
                console.print(f"Warning: invalid last timestamp in cache {dataFile} for {symbol}: {e}")
                lastTimestamp = None
        # conserver les données précédentes
        existing_data = data2
        currentTimestamp = int(time.time()*1000)  # Current timestamp in milliseconds
        #console.print(f"Last Timestamp: {lastTimestamp}")
        #console.print(f"Current Timestamp: {currentTimestamp}")
        data = []
        if lastTimestamp < currentTimestamp:
            # fetch en boucles avec 'since' = lastTimestamp (ccxt attend un timestamp absolu en ms)
            since = lastTimestamp
            try:
                while True:
                    time.sleep(exchange.rateLimit / 1000)
                    batch = exchange.fetch_ohlcv(symbol, '1m', since)
                    if not batch:
                        break
                    data.extend(batch)
                    last_ts = int(batch[-1][0])
                    if last_ts <= since:
                        break
                    since = last_ts + 60 * 1000
                    if last_ts >= currentTimestamp - 60*1000:
                        break
            except Exception as e:
                console.print(f"Warning: fetch_ohlcv batch failed for {symbol}: {e}")
                try:
                    time.sleep(exchange.rateLimit / 1000)
                    data = exchange.fetch_ohlcv(symbol, '1m')
                except Exception as e2:
                    console.print(f"Fallback fetch failed for {symbol}: {e2}")
                    data = []
            # [ [1783382400000, 55953.0, 56217.1, 54798.5, 55529.0, 573.16980314], ... ]
            # UTC timestamp in milliseconds, integer
            #data[0].get('timestamp', 1783382400000)  # Example timestamp (milliseconds since epoch)
            #data[0].get('open', 55953.0)  # Example open price
            #data[0].get('highest', 56217.1)  # Example highest price
            #data[0].get('lowest', 54798.5)  # Example lowest price
            #data[0].get('closing', 55529.0)  # Example closing price
            #data[0].get('volume', 573.16980314)  # Example volume
    # sinon si le fichier n'existe pas
    else:
        try:
            data = exchange.fetch_ohlcv(symbol, '1m')  # ce fetch trouve son max naturellement
        except Exception as e:
            console.print(f"Warning: initial fetch_ohlcv failed for {symbol}: {e}")
            data = []
        if data is None:
            data = []
    # ensure data is iterable/list before using
    if data is None:
        data = []
    _len = len(data)
    # console.print(f"Fetched {_len} OHLCV data points for {symbol}.")
    # pause this pair for 8 hours if the OHLCV fetch is empty
    if _len == 0:
        console.print(f"No new OHLCV for {symbol}: fetched 0 candles. existing cache size={len(existing_data) if existing_data is not None else 0}")
        # persist cache (to keep existing data untouched)
        try:
            try:
                safe_json.atomic_write_json(dataFile, existing_data if existing_data is not None else [], backup=True, indent=4)
            except Exception:
                with open(dataFile, 'w') as f:
                    json.dump(existing_data if existing_data is not None else [], f, indent=4)
        except Exception as e:
            console.print(f"Warning: failed to write ohlcv cache for {symbol}: {e}")
        # pause buys for this symbol for 8 hours
        try:
            expiry_ts = int(time.time()) + 8 * 3600
            pausedForBuy[symbol] = expiry_ts
            try:
                safe_json.atomic_write_json(PAUSE_FILE, pausedForBuy, backup=True)
            except Exception:
                with open(PAUSE_FILE, 'w') as f:
                    json.dump(pausedForBuy, f)
            console.print(f"Paused buys for {symbol} until {datetime.fromtimestamp(expiry_ts)} due to empty OHLCV fetch")
        except Exception as e:
            console.print(f"Failed to persist pausedForBuy for {symbol}: {e}")
        return pd.DataFrame(existing_data if existing_data is not None else [], columns=['timestamp','open','high','low','close','volume'])
    #else:
        #try:
            #first_ts = int(data[0][0])
            #last_ts = int(data[-1][0])
            #console.print(f"Fetched {len(data)} new OHLCV candles for {symbol}: first={datetime.fromtimestamp(first_ts/1000)} last={datetime.fromtimestamp(last_ts/1000)}")
        #except Exception:
            #console.print(f"Fetched {len(data)} new OHLCV candles for {symbol}")
    # Retirer les doublons par timestamp
    for data_point in data:
        # console.print(f"Timestamp: {data_point[0]}, Open: {data_point[1]}, High: {data_point[2]}, Low: {data_point[3]}, Close: {data_point[4]}, Volume: {data_point[5]}")
        timestamp_to_remove = data_point[0]
        # Parcourir les bougies pour trouver celle à supprimer
        for j, candle in enumerate(existing_data):
            # cadence defensive: vérifier que candle est indexable
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
        console.print(f"Warning: failed to write ohlcv cache for {symbol}: {e}")
    # ohlcv: [ [ts, open, high, low, close, volume], ... ]
    return pd.DataFrame(existing_data, columns=['timestamp','open','high','low','close','volume'])

def fetch_balance(exchange):
    balance = exchange.fetch_balance()
    balance['timestamp'] = int(time.time())
    try:
        try:
            safe_json.atomic_write_json("balance.json", balance, backup=True, indent=4)
        except Exception:
            with open("balance.json", 'w') as f:
                json.dump(balance, f, indent=4)
    except Exception as e:
        console.print(f"Balance backup file exception: {e}")
    return balance

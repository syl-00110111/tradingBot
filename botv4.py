# init start
from rich.console import Console
console = Console()

with console.status("Bot init. Please wait some time, or expect a random error if you break.", spinner="dots"):
    # core and third-party imports (duplicates removed for clarity)
    import ccxt
    import asyncio
    import logging
    import time
    import re
    import json
    import signal
    import argparse
    import os
    import sys
    import platform
    import random
    import math
    import threading
    import queue
    from collections import deque

    # numeric / data libraries
    import numpy as np
    import pandas as pd

    # ML / plotting / concurrency
    import torch
    import concurrent.futures
    import plotext as plt_ascii

    # datetime and rich UI
    from datetime import datetime, timedelta, timezone
    from rich.live import Live
    from rich.table import Table
    from rich.progress import Progress
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.logging import RichHandler
    from rich.text import Text

    # utilities
    import readchar
    import psutil
    import safe_json

    run = False

    def respect_rate_limit(exchange) -> None:
        """Sleep according to exchange.rateLimit (ms) with a safe default.
        Use this helper instead of repeating the expression.
        """
        try:
            ms = getattr(exchange, 'rateLimit', 200) or 200
            time.sleep(ms / 1000)
        except Exception as e:
            # best-effort fallback
            try:
                time.sleep(0.2)
            except Exception as e:
                pass

    # logging helper
    import traceback

    def log_exception(e: Exception, ctx: str = None) -> None:
        """Centralise l'affichage des exceptions pour faciliter le debug.
        Affiche la trace complète via rich.console.
        """
        try:
            prefix = f"[{ctx}] " if ctx else ""
            console.print(f"{prefix}Exception: {e}")
            tb = traceback.format_exc()
            console.print(tb)
        except Exception as e:
            # never raise from a logging helper
            pass

    # configure logging to use RichHandler for consistent output
    try:
        logging.basicConfig(level=logging.INFO, handlers=[RichHandler()])
    except Exception as e:
        pass

    # simple JSON helpers
    def read_json_file(path: str, default=None):
        try:
            if os.path.exists(path):
                with open(path, 'r') as f:
                    return json.load(f)
        except Exception as e:
            log_exception(e, f"read_json_file:{path}")
        return default

    def write_json_atomic(path: str, data) -> bool:
        try:
            try:
                safe_json.atomic_write_json(path, data, backup=True, indent=2)
            except Exception as e:
                with open(path, 'w') as f:
                    json.dump(data, f, indent=2)
            return True
        except Exception as e:
            log_exception(e, f"write_json_atomic:{path}")
            return False

    #currentSwap = psutil.swap_memory().used
    #lowerSwap = currentSwap

    exchangeLoaded = False
    balanceFetched = False
    marketsFetched = False
    sourceAssets = []
    forbidAssets = ['AKE', 'USDT', 'XMR']
    previousPairs = []
    availablePairs = []
    maxNumPairs = 50

    # ccxt markets keyword is used
    _markets = []
    _positions = {}  # key: symbol, value: {'amount': float, 'avg_price': float}

    # periodic pending orders dump / candle consistency check
    last_pending_fetch = 0
    PENDING_DUMP_FILE = 'pending_orders_dump.json'
        
    miniCount = 600
    # monnaies d'usage pour considérer les paires à leur quote asset
    baseAssets = ["USD", "EUR"]
    # mapping asset -> average price in quote currency (computed from available ohlcv caches)
    asset_avg_prices = {}
    
    # trading state (position tracking for profit calc)
    def add_pending_order(order):
        """Append a single order dict to the pending dump file.
        This function relies exclusively on the local file.
        """
        try:
            snapshot = {'ts': int(time.time()), 'order': order}
            cur = []
            if os.path.exists(PENDING_DUMP_FILE):
                try:
                    with open(PENDING_DUMP_FILE, 'r') as f:
                        cur = json.load(f)
                except Exception as e:
                    log_exception(e, "add_pending_order:read")
                    cur = []
            cur.append(snapshot)
            # write atomically to avoid partial files on interruption
            try:
                write_json_atomic(PENDING_DUMP_FILE, cur)
            except Exception as e:
                log_exception(e, "add_pending_order:write")
            return snapshot
        except Exception as e:
            log_exception(e, "add_pending_order")
            return None

    def pending_orders():
        """Read the pending orders dump file and return its contents.
        This function relies exclusively on the local file.
        """
        try:
            cur = []
            if os.path.exists(PENDING_DUMP_FILE):
                with open(PENDING_DUMP_FILE, 'r') as f:
                    try:
                        cur = json.load(f)
                    except Exception as e:
                        log_exception(e, "pending_orders:read")
                        cur = []
            return {'ts': int(time.time()), 'snapshots': cur}
        except Exception as e:
            log_exception(e, "pending_orders")
            return None

    def cleanup_existing_orders(exchange, symbol, last_close, side, threshold=0.02, cancel_other_sides=False):
        """Récupère les ordres ouverts pour `symbol` et annule ceux dont le prix est
        hors de portée (> threshold) et non favorable au side donné.
        Retourne (existing_orders_after_cleanup, cancelled_count).
        """
        try:
            respect_rate_limit(exchange)
            existing = exchange.fetch_open_orders(symbol)
        except Exception as e:
            # don't treat a fetch error as "no orders" -- mark as None so callers
            # can skip any destructive sync operations that would clear the dump file
            log_exception(e, "cleanup_existing_orders:fetch")
            existing = None
        cancelled = 0
        cancelled_oids = []
        try:
            to_cancel = []
            last_close_f = None
            try:
                last_close_f = float(last_close) if last_close is not None else None
            except Exception as e:
                log_exception(e, "cleanup_existing_orders:parse_last_close")
                last_close_f = None
            for ex in existing:
                try:
                    ex_side = (ex.get('side') or '').lower()
                except Exception as e:
                    log_exception(e, "cleanup_existing_orders:ex_side")
                    ex_side = ''
                # si cancel_other_sides est False, n'examiner que le même side
                if (not cancel_other_sides) and ex_side and ex_side != side:
                    continue
                # extract price
                ex_price = None
                try:
                    ex_price = ex.get('price')
                    if ex_price is None and isinstance(ex.get('info'), dict):
                        info = ex.get('info')
                        ex_price = info.get('price') or info.get('rate') or info.get('price_1')
                except Exception as e:
                    log_exception(e, "cleanup_existing_orders:ex_price")
                    ex_price = None
                if ex_price is None:
                    continue
                try:
                    ex_price_f = float(ex_price)
                except Exception as e:
                    log_exception(e, "cleanup_existing_orders:ex_price_float")
                    continue
                if last_close_f is None or last_close_f == 0:
                    continue
                pct = (ex_price_f - last_close_f) / last_close_f
                # favorable condition: for buy => ex_price < last_close, for sell => ex_price > last_close
                favorable = (ex_price_f < last_close_f) if side == 'buy' else (ex_price_f > last_close_f)
                if abs(pct) > threshold and not favorable:
                    oid = ex.get('id') or ex.get('orderId') or (ex.get('info') or {}).get('id')
                    to_cancel.append((oid, ex_price_f, pct))
            if to_cancel:
                for (oid, op, pct) in to_cancel:
                    if not oid:
                        continue
                    try:
                        console.print(f"Cancelling stale {side.upper()} order {oid} price={op} (diff={pct*100:.2f}%)")
                        respect_rate_limit(exchange)
                        try:
                            exchange.cancel_order(oid, symbol)
                        except TypeError as e:
                            try:
                                exchange.cancel_order(oid)
                            except Exception as e:
                                log_exception(e, "cleanup_existing_orders:cancel")
                                continue
                        cancelled += 1
                        try:
                            cancelled_oids.append(str(oid))
                        except Exception as e:
                            log_exception(e, "cleanup_existing_orders:append_oid")
                            pass
                    except Exception as e:
                        log_exception(e, "cleanup_existing_orders:cancel_loop")
                        continue
        except Exception as e:
            log_exception(e, "cleanup_existing_orders:main")
        # après annulations, synchroniser immédiatement le dump local des ordres en attente
        try:
            try:
                # only proceed to sync the pending dump if we successfully fetched existing orders
                if existing is None:
                    # fetch failed earlier; skip sync to avoid accidental clearing of the dump
                    pass
                else:
                    recent_ids = set()
                    for ex in existing:
                        oid = ex.get('id') or ex.get('orderId') or ex.get('clientOrderId')
                        if not oid and isinstance(ex.get('info'), dict):
                            info = ex.get('info')
                            oid = info.get('id') or info.get('orderId') or info.get('txid')
                        if oid:
                            recent_ids.add(str(oid))
                    # perform a symbol-scoped sync: only remove snapshots that belong to this symbol
                    if os.path.exists(PENDING_DUMP_FILE):
                        try:
                            with open(PENDING_DUMP_FILE, 'r') as f:
                                pending_list = json.load(f)
                        except Exception as e:
                            log_exception(e, "cleanup_existing_orders:pending_read")
                            pending_list = []
                        new_pending = []
                        removed = 0
                        for snap in pending_list:
                            try:
                                order = snap.get('order', {})
                                snap_sym = order.get('symbol') or (order.get('info') or {}).get('symbol') or (order.get('info') or {}).get('pair')
                                oid = order.get('id') or order.get('orderId') or order.get('clientOrderId')
                                if not oid and isinstance(order.get('info'), dict):
                                    info = order.get('info')
                                    oid = info.get('id') or info.get('orderId') or info.get('txid')
                            except Exception as e:
                                log_exception(e, "cleanup_existing_orders:snap_order")
                                snap_sym = None
                                oid = None
                            # If snapshot is for the symbol we processed, only keep it if its oid is still present
                            if snap_sym and str(snap_sym) == str(symbol):
                                if oid is None or str(oid) in recent_ids:
                                    new_pending.append(snap)
                                else:
                                    removed += 1
                            else:
                                # keep snapshots for other symbols untouched
                                new_pending.append(snap)
                        if removed > 0:
                            try:
                                safe_json.atomic_write_json(PENDING_DUMP_FILE, new_pending, backup=True, indent=2)
                            except Exception as e:
                                log_exception(e, "cleanup_existing_orders:pending_write")
                                with open(PENDING_DUMP_FILE, 'w') as f:
                                    json.dump(new_pending, f, indent=2)
            except Exception as e:
                log_exception(e, "cleanup_existing_orders:pending_sync")
        except Exception as e:
            log_exception(e, "cleanup_existing_orders:final")
        return existing, cancelled, cancelled_oids


    def try_place_limit_order(exchange, side, symbol, amount, price):
        """Place un ordre limit buy/sell et retourne l'objet order.
        Lance l'exception si échec."""
        if side == 'buy':
            console.print(f"Placing LIMIT BUY {symbol} amount={amount} price={price}")
            order = exchange.create_limit_buy_order(symbol, amount, price)
        else:
            console.print(f"Placing LIMIT SELL {symbol} amount={amount} price={price}")
            order = exchange.create_limit_sell_order(symbol, amount, price)
        # persist pending order
        try:
            add_pending_order(order)
        except Exception as e:
            log_exception(e, "try_place_limit_order:add_pending_order")
        console.print(f"{side.upper()} order passed: {order}")
        return order


    def sync_pending_dump(recent_ids):
        """Garder dans PENDING_DUMP_FILE uniquement les snapshots dont l'order id
        figure dans recent_ids."""
        try:
            if not os.path.exists(PENDING_DUMP_FILE):
                return 0
            try:
                with open(PENDING_DUMP_FILE, 'r') as f:
                    pending_list = json.load(f)
            except Exception as e:
                log_exception(e, "sync_pending_dump:read")
                pending_list = []
            new_pending = []
            removed = 0
            for snap in pending_list:
                try:
                    order = snap.get('order', {})
                    oid = order.get('id') or order.get('orderId') or order.get('clientOrderId')
                    if not oid and isinstance(order.get('info'), dict):
                        info = order.get('info')
                        oid = info.get('id') or info.get('orderId') or info.get('txid')
                except Exception as e:
                    oid = None
                # Keep snapshots that lack an identifiable id (can't be safely removed).
                if oid is None:
                    new_pending.append(snap)
                elif str(oid) in recent_ids:
                    new_pending.append(snap)
                else:
                    removed += 1
            if removed > 0:
                try:
                    safe_json.atomic_write_json(PENDING_DUMP_FILE, new_pending, backup=True, indent=2)
                except Exception as e:
                    log_exception(e, "sync_pending_dump:write")
                    with open(PENDING_DUMP_FILE, 'w') as f:
                        json.dump(new_pending, f, indent=2)
                # Also purge related entries from my_trades_cache.json for orders that disappeared
                def extract_symbol_from_snap(snap):
                    try:
                        order = snap.get('order', {}) if isinstance(snap, dict) else {}
                        sym = order.get('symbol') or (order.get('info') or {}).get('symbol') or (order.get('info') or {}).get('pair')
                        if isinstance(sym, str):
                            return sym
                    except Exception as e:
                        log_exception(e, "sync_pending_dump:extract_symbol")
                    return None
                # build sets of symbols present before and after
                before_symbols = set()
                after_symbols = set()
                for snap in pending_list:
                    try:
                        s = extract_symbol_from_snap(snap)
                        if s:
                            before_symbols.add(str(s))
                    except Exception as e:
                        log_exception(e, "sync_pending_dump:before_symbols_loop")
                        continue
                for snap in new_pending:
                    try:
                        s = extract_symbol_from_snap(snap)
                        if s:
                            after_symbols.add(str(s))
                    except Exception as e:
                        log_exception(e, "sync_pending_dump:after_symbols_loop")
                        continue

                removed_symbols = before_symbols - after_symbols
                if removed_symbols:
                    mt_cache = {}
                    try:
                        if os.path.exists('my_trades_cache.json'):
                            with open('my_trades_cache.json', 'r') as mtf:
                                try:
                                    mt_cache = json.load(mtf)
                                except Exception as e:
                                    log_exception(e, "sync_pending_dump:my_trades_cache_read")
                                    mt_cache = {}
                    except Exception as e:
                        log_exception(e, "sync_pending_dump:my_trades_cache_access")
                        mt_cache = {}
                    # purge keys matching removed symbols (both symbol and upper/lower variants)
                    keys_to_remove = []
                    for k in list(mt_cache.keys()):
                        try:
                            for rs in removed_symbols:
                                if not k:
                                    continue
                                if str(k) == str(rs) or str(k).upper() == str(rs).upper():
                                    keys_to_remove.append(k)
                                    break
                        except Exception as e:
                            log_exception(e, "sync_pending_dump:keys_to_remove_loop")
                            continue
                    for kr in keys_to_remove:
                        try:
                            mt_cache.pop(kr, None)
                        except Exception as e:
                            log_exception(e, "sync_pending_dump:keys_to_remove_pop")
                            pass
                    # persist cache if modified
                    if keys_to_remove:
                        try:
                            try:
                                safe_json.atomic_write_json('my_trades_cache.json', mt_cache, backup=True, indent=2)
                            except Exception as e:
                                log_exception(e, "sync_pending_dump:my_trades_cache_write")
                                with open('my_trades_cache.json', 'w') as mtf:
                                    json.dump(mt_cache, mtf, indent=2)
                        except Exception as e:
                            log_exception(e, "sync_pending_dump:my_trades_cache_persist")
                            pass
                console.print(f"Pending dump synced: removed {removed} stale snapshot(s)")
            return removed
        except Exception as e:
            log_exception(e, "sync_pending_dump")
            return 0


    def plot_order_ascii(symbol, df_candles, latest_idx, marker, color, title_suffix=''):
        try:
            plt_ascii.clf()
            plt_ascii.theme('dark')
            plt_ascii.subplots(1, 1)
            timestamps = df_candles['timestamp'].astype(int).tolist()
            dates = [datetime.fromtimestamp(int(ts)/1000).strftime('%d/%m %H:%M') for ts in timestamps]
            opens = df_candles['open'].astype(float).tolist()
            highs = df_candles['high'].astype(float).tolist()
            lows = df_candles['low'].astype(float).tolist()
            closes = df_candles['close'].astype(float).tolist()
            volumes = df_candles['volume'].astype(float).tolist()
            data = {"Open": opens, "High": highs, "Low": lows, "Close": closes}
            x = list(range(len(dates)))
            plt_ascii.title(f"{symbol} - {title_suffix}")
            plt_ascii.subplot(1, 1)
            plt_ascii.candlestick(x, data)
            max_volume = max(volumes) if volumes else 1
            min_price = min(lows) if lows else 0
            max_price = max(highs) if highs else 1
            price_range = max_price - min_price if max_price != min_price else max_price
            base = min_price - price_range * 0.02
            height_factor = price_range * 0.64
            for i, v in enumerate(volumes):
                h = (v / max_volume) * height_factor if max_volume else 0
                plt_ascii.plot([i, i], [base, base + h], color='yellow')
            plt_ascii.scatter([latest_idx], [closes[latest_idx]], marker=marker, color=color)
            step = max(1, len(dates) // 8)
            x_ticks = x[::step]
            x_labels = [dates[i] for i in x_ticks]
            plt_ascii.xticks(x_ticks, x_labels)
            plt_ascii.show()
        except Exception as e:
            log_exception(e, f"plot_order_ascii:{symbol}")

    def updateTradingCount(symbol):
        try:
            with open('volumes_trades_data.json','r') as f: _volumes = json.load(f)
        except Exception as e:
            raise ValueError(f"volume trades data file problem: {e}")
        trades_count = 0
        for _vol in _volumes:
            if symbol == _vol.get('symbol') and symbol is not None:
                _since = _vol.get('timestamp')
                # normalize _since to an integer timestamp (ms);
                # if missing/invalid, consider it as current time (so we won't fetch historical trades)
                now_minus_4h = int(time.time()) * 1000 - (4*3600*1000)
                if _since is None:
                    _since_int = now_minus_4h
                else:
                    _since_int = int(_since)
                # if now < since by 4 hours
                if (_since_int >= now_minus_4h) or _vol.get('trades_count') == 1000:
                    #console.print(f"_since: {_since_int}, since_4h: {now_minus_4h}, int(time.time()): {int(time.time())}")
                    respect_rate_limit(exchange)
                    trades = exchange.fetch_trades(symbol, now_minus_4h)
                    trades_count = len(trades) if trades is not None else 0
                    # console.print(f"Old trades count for {symbol}: {_vol['trades_count']}")
                    console.print(f"New fetched trades count (last 4h) for {symbol}: {trades_count}")
                    # mettre à jour avec le nouveau volume
                    _vol['trades_count'] = trades_count
                    _vol['timestamp'] = int(time.time())
                    break
        try:
            try:
                safe_json.atomic_write_json('volumes_trades_data.json', _volumes, backup=True, indent=4)
            except Exception as e:
                log_exception(e, "updateTradingCount:backup")
                with open('volumes_trades_data.json', 'w') as f:
                    json.dump(_volumes, f, indent=4)
            # console.print(f"Fichier volumes_trades_data.json mis à jour pour le symbole {symbol}.")
        except Exception as e:
            log_exception(e, "updateTradingCount:write")
        return trades_count

    def computeSymbols(balance, previousPairs):
        __symbols = []
        # balance existante
        # # Vérifier que 'balance' est un dictionnaire valide
        if not isinstance(balance, dict):
            console.print("[ERROR] La structure 'balance' est invalide.")
        elif 'free' not in balance:
            console.print("[ERROR] La clé 'free' est manquante dans 'balance'.")
        else:
            free_balances = balance.get('free')
            if not isinstance(free_balances, dict):
                console.print("[ERROR] La clé 'free' ne contient pas un dictionnaire valide.")
            else:
                for asset, amount in free_balances.items():
                    try:
                        # Convertir le montant en float
                        amount_float = float(amount)
                        # Ajouter l'actif à la liste si le montant est supérieur à 0
                        if amount_float > 0:
                            sourceAssets.append(asset)
                            # console.print(f"source asset: {asset} {amount_float}")
                    except (ValueError, TypeError) as e:
                        log_exception(e, f"computeSymbols:balance_asset:{asset}")
        try:
            with open('markets.json','r') as f: _markets = json.load(f)
            with open('volumes_trades_data.json','r') as f: _volumes = json.load(f)
            # build a set of existing symbols (normalized) to compare reliably
            try:
                existing_symbols = {str(p[0]).upper() for p in previousPairs if isinstance(p, (list, tuple)) and len(p) > 0 and p[0] is not None}
            except Exception as e:
                log_exception(e, "computeSymbols:existing_symbols")
                existing_symbols = set()
            _g = {'id':[]}
            for _v in _volumes:
                if _v.get('trades_count') > miniCount:
                    # tri du volume à part
                    _g['id'].append(_v.get('id'))
            # construire deux listes distinctes : prioriser les paires à vendre (base dans sourceAssets), puis les paires volume
            sell_candidates = []
            volume_candidates = []
            _a = []
            for _m in _markets.items():
                _a = [_m[1].get('symbol'), _m[1].get('id'), _m[1].get('base'), _m[1].get('quote'), _m[1].get('limits').get('amount').get('min'), _m[1].get('precision').get('price'), _m[1].get('precision').get('amount')]
                # si pas interdit dans notre zone
                if (_m[1].get('base') not in forbidAssets) and (_m[1].get('quote') not in forbidAssets):
                    # paire présente dans les volumes importants et quote dans monnaies d'usage
                    if (_m[1].get('id') in _g.get('id')) and (_m[1].get('quote') in baseAssets):
                        # si la base est dans la balance -> priorité vente
                        if (_m[1].get('base') in sourceAssets) and (str(_a[0]).upper() not in existing_symbols):
                            sell_candidates.append(_a)
                            existing_symbols.add(str(_a[0]).upper())
                            console.print(f"balance add: {_m[1].get('symbol')}")
                        # sinon, c'est une paire volume
                        elif (str(_a[0]).upper() not in existing_symbols):
                            volume_candidates.append(_a)
                            existing_symbols.add(str(_a[0]).upper())
                            console.print(f"volume add: {_a[0]}")
            # combiner en respectant maxNumPairs : priorité aux ventes
            combined = []
            # ajouter d'abord les ventes
            for item in sell_candidates:
                if len(combined) >= maxNumPairs:
                    break
                combined.append(item)
            # compléter avec les paires volume si besoin
            for item in volume_candidates:
                if len(combined) >= maxNumPairs:
                    break
                combined.append(item)
            __symbols.extend(combined)
        except Exception as e:
            log_exception(e, "computeSymbols")
        return __symbols

# boucle principale du bot
while True:
    try:
        # init end
        if not run:
            console.print("Bot running.")
            run = True
        # init step for allocation of computationnal task
        #currentSwap = psutil.swap_memory().used
        #if currentSwap < lowerSwap:
        #    lowerSwap = currentSwap

        #exchange
        if not exchangeLoaded:
            exchange = loadExchange()
            exchangeLoaded = True

        # interroger les assets disponibles sur la plateforme pour l'utilisateur et les stocker dans une variable globale
        if not balanceFetched:
            balance = fetch_balance(exchange, True)
            balanceFetched = True
            # console.print(f"original balance: {balance}")
        
        # markets fetch
        if not marketsFetched:
            _markets = loadMarkets(exchange, "markets.json")
            availablePairs = computeSymbols(balance, None)
            marketsFetched = True

        # taux
        # comparer taux xxbt / xeth / zeur pour aave par exemple

        # paires

        # watch orders

        # watch balance

        # watch new candles

        if run and exchangeLoaded and balanceFetched and marketsFetched:
            # For each pair, compute aggregated strategy signals (based on strategie.py) and act on latest signal
            for availablePair in availablePairs:
                # console.print(f"availablePair: {availablePair}")
                # For each available pair, fetch recent OHLCV candles (1m) to feed strategies
                candles_per_pair = {}
                # availablePair format: [symbol, id, base, quote, amount, price_precision, amount_precision]
                symbol = availablePair[0]
                _id = availablePair[1]
                base = availablePair[2]
                quote = availablePair[3]
                min_amount = availablePair[4]
                price_precision = availablePair[5]
                amount_precision = availablePair[6]
                if exchange.has.get('fetchOHLCV'):
                    try:
                        candles_per_pair[symbol] = fetch_ohlcv_data(_id, symbol)
                        try:
                            # vérifier la cohérence des chandelles immédiatement après le fetch
                            check_candles_consistency(symbol)
                        except Exception as e:
                            log_exception(e, f"check_candles_consistency:{symbol}")
                    except Exception as e:
                        log_exception(e, f"Failed to fetch OHLCV for {symbol}: {e}")
                df_candles = candles_per_pair.get(symbol)
                # check
                if df_candles is None or df_candles.empty:
                    continue
                N = len(df_candles)
                # compute per-strategy signals
                signal_frames = {}
                # compute aggregated signals using shared aggregator
                try:
                    res = aggregate_signals(df_candles, global_config=config)
                except Exception as e:
                    log_exception(e, f"aggregate_signals failed for {symbol}")
                    continue
                N = res.get('N', 0)
                signal_frames = res.get('signal_frames', {})
                global_buy = res.get('global_buy', [])
                global_sell = res.get('global_sell', [])
                if N == 0:
                    continue
                # act on the latest candle
                latest_idx = N - 1
                last_close = float(df_candles.iloc[latest_idx]['close'])
                # read average price for base asset in the pair's quote (now stored as per-symbol keys like "BASE/QUOTE")
                avg_price_for_base = None
                try:
                    # try direct key first
                    key = f"{base}/{quote}"
                    entry = asset_avg_prices.get(key)
                    if isinstance(entry, dict):
                        avg_price_for_base = entry.get('price')
                    elif isinstance(entry, (int, float)):
                        # defensive: accept numeric legacy-style values if present
                        avg_price_for_base = entry
                    else:
                        # fallback: search for any entry starting with 'BASE/' and pick preferred quote if configured
                        candidates = []
                        pref = str(config.get('quote_preferred', '')).upper() if isinstance(config, dict) else ''
                        for k, v in asset_avg_prices.items():
                            try:
                                if not isinstance(k, str):
                                    continue
                                parts = k.split('/')
                                if len(parts) != 2:
                                    continue
                                b = parts[0].upper()
                                q = parts[1].upper()
                                if b != str(base).upper():
                                    continue
                                price_val = None
                                if isinstance(v, dict):
                                    price_val = v.get('price')
                                elif isinstance(v, (int, float)):
                                    price_val = v
                                if price_val is None:
                                    continue
                                candidates.append((q, float(price_val)))
                            except Exception:
                                log_exception(e, f"price_candidate_loop:{base}")
                                continue
                        selected = None
                        if pref:
                            for qk, pv in candidates:
                                if qk == pref:
                                    selected = pv
                                    break
                        if selected is None:
                            for bq in baseAssets:
                                for qk, pv in candidates:
                                    if qk == str(bq).upper():
                                        selected = pv
                                        break
                                if selected is not None:
                                    break
                        if selected is None and candidates:
                            selected = candidates[0][1]
                        avg_price_for_base = selected
                except Exception as e:
                    log_exception(e, f"avg_price_lookup:{symbol}")
                    avg_price_for_base = None
                # threshold from config or default 5%
                try:
                    price_dev_threshold = float(config.get('price_deviation_threshold', 0.05))
                except Exception:
                    price_dev_threshold = 0.05

                # decide buy
                if global_buy[latest_idx]:
                    # skip if buys are paused for this symbol
                    now_ts = int(time.time())
                    expiry = pausedForBuy.get(symbol)
                    if expiry and now_ts < int(expiry):
                        console.print(f"Buy for {symbol} is paused until {datetime.fromtimestamp(int(expiry))}")
                    else:
                        # cleanup expired pause entry
                        if expiry and now_ts >= int(expiry):
                            try:
                                pausedForBuy.pop(symbol, None)
                                try:
                                    safe_json.atomic_write_json(PAUSE_FILE, pausedForBuy, backup=True)
                                except Exception as e:
                                    log_exception(e, "pausedForBuy:write")
                                    with open(PAUSE_FILE, 'w') as f:
                                        json.dump(pausedForBuy, f)
                            except Exception as e:
                                log_exception(e, "pausedForBuy:cleanup")
                                pass
                        # compute amount to buy: use a fixed package value
                        try:
                            order_book = exchange.fetch_order_book(symbol)
                        except Exception as e:
                            log_exception(e, f"Failed to fetch order book for {symbol}")
                            order_book = {'asks':[], 'bids':[]}
                        price = order_book.get('asks')[0][0] if order_book.get('asks') else last_close
                        price = price * 0.999
                        price = round(price, int(-math.log10(price_precision)))
                        # if price > avg, buy not authorized
                        try:
                            if avg_price_for_base is not None:
                                if price > avg_price_for_base:
                                    console.print(f"Skip BUY {symbol}: ordered price {price} would be greater than observed average of {avg_price_for_base}")
                                    continue
                        except Exception as e:
                            log_exception(e, "buy_price_check")
                            pass
                        # read quote balance robustly
                        _b = balance.get('free').get(quote)
                        if _b is not None:
                            quote_free = float(_b)
                        else:
                            quote_free = 0
                        if quote_free <= 0:
                            console.print(f"No quote balance available for {symbol} to BUY")
                        else:
                            # calculate desired amount that equals package at the current price
                            if price > 0:
                                amount = round(min_amount * 2.001, int(-math.log10(amount_precision)))
                            else:
                                amount = 0
                            # final amount check
                            if amount <= min_amount:
                                console.print(f"Calculated buy amount ({amount}) is below minimum amount of {min_amount} for {symbol}")
                            else:
                                try:
                                    existing, cancelled, cancelled_oids = cleanup_existing_orders(exchange, symbol, last_close, 'buy', cancel_other_sides=True)
                                    if existing:
                                        console.print(f"Skipped BUY: existing open order(s) for {symbol} found: {len(existing)}")
                                    else:
                                        try:
                                            order = try_place_limit_order(exchange, 'buy', symbol, amount, price)
                                            # update balance
                                            balance = fetch_balance(exchange, False)
                                            # plot BUY
                                            plot_order_ascii(symbol, df_candles, latest_idx, marker='x', color='green', title_suffix='BUY')
                                        except Exception as e:
                                            log_exception(e, f"Buy order placement failed for {symbol}")
                                            # detect specific errors and pause buys for some time
                                            err = str(e).lower()
                                            expiry_ts = None
                                            if ('invalid permissions' in err):
                                                expiry_ts = int(time.time()) + (366 * 24 * 3600)
                                            elif ('insufficient funds' in err) or ('minimum' in err and 'not met' in err) or ('invalid arguments' in err and 'volume' in err) or ('must be greater than minimum' in err):
                                                expiry_ts = int(time.time()) + (4 * 3600)
                                            if expiry_ts:
                                                pausedForBuy[symbol] = expiry_ts
                                                try:
                                                    try:
                                                        safe_json.atomic_write_json(PAUSE_FILE, pausedForBuy, backup=True)
                                                    except Exception as e:
                                                        log_exception(e, "pausedForBuy:write_failed")
                                                        with open(PAUSE_FILE, 'w') as f:
                                                            json.dump(pausedForBuy, f)
                                                except Exception as ex:
                                                    log_exception(ex, "pausedForBuy:persist_failed")
                                                console.print(f"Paused buys for {symbol} until {datetime.fromtimestamp(expiry_ts)} due to error: {e}")
                                except Exception as e:
                                    log_exception(e, f"Unexpected error during buy flow for {symbol}: {e}")

                # decide sell
                if global_sell[latest_idx]:
                    _b = balance.get('free').get(base)
                    if _b is not None:
                        base_free = float(_b)
                    else:
                        base_free = 0
                    if base_free <= 0:
                        console.print(f"No base balance available for {symbol} to SELL")
                    else:
                        try:
                            order_book = exchange.fetch_order_book(symbol)
                        except Exception as e:
                            log_exception(e, f"Failed to fetch order book for {symbol}")
                            order_book = {'asks':[], 'bids':[]}
                        price = order_book.get('bids')[0][0] if order_book.get('bids') else last_close
                        price = price * 1.001
                        price = round(price, int(-math.log10(price_precision)))
                        # if price < avg, sell not authorized
                        try:
                            if avg_price_for_base is not None:
                                if price < avg_price_for_base:
                                    console.print(f"Skip SELL {symbol}: ordered price {price} would be lesser than observed average {avg_price_for_base}")
                                    continue
                        except Exception as e:
                            log_exception(e, "sell_price_check")
                            pass
                        # sell everything if symbol paused
                        now_ts = int(time.time())
                        expiry = pausedForBuy.get(symbol)
                        if expiry and now_ts < int(expiry):
                            # sell everything if paused
                            amount = round(base_free, int(-math.log10(amount_precision)))
                        else: # tier hardcoded
                            amount = round(base_free * (5 / 6), int(-math.log10(amount_precision)))
                        if amount <= min_amount:
                            console.print(f"Calculated sell amount of {amount} below minimum required of {min_amount} for {symbol}")
                        else:
                            try:
                                existing, cancelled, cancelled_oids = cleanup_existing_orders(exchange, symbol, last_close, 'sell', cancel_other_sides=True)
                                if existing:
                                    console.print(f"Skipped SELL: existing open order(s) for {symbol} found: {len(existing)}")
                                else:
                                    try:
                                        order = try_place_limit_order(exchange, 'sell', symbol, amount, price)
                                        # update balance
                                        balance = fetch_balance(exchange, False)
                                        # plot SELL
                                        plot_order_ascii(symbol, df_candles, latest_idx, marker='o', color='red', title_suffix='SELL')
                                    except Exception as e:
                                        log_exception(e, f"Sell order placement failed for {symbol}")
                            except Exception as e:
                                log_exception(e, f"Unexpected error during sell flow for {symbol}: {e}")

                time.sleep(1.0)

            # Periodic background tasks: dump pending orders every 30 minutes,
            # check candle coherence, and compute profit for recent SELL orders.
            try:
                now_ts = time.time()
                if now_ts - last_pending_fetch >= 30 * 60:
                    # batch symbole au hasard - choisir correctement quand _markets est un dict
                    try:
                        if isinstance(_markets, dict):
                            market_sample = random.choice(list(_markets.values())) if _markets else None
                        else:
                            market_sample = random.choice(_markets) if _markets else None
                        if isinstance(market_sample, dict):
                            symbolChoose = market_sample.get('symbol') or market_sample.get('id')
                        else:
                            symbolChoose = str(market_sample) if market_sample is not None else None
                        console.print(f"Chose to update symbol: {symbolChoose}")
                    except Exception as e:
                        log_exception(e, "updateTradingCount:random_choice")
                        symbolChoose = None
                    if updateTradingCount(symbolChoose) > miniCount:
                        availablePairs.append(symbolChoose)
                        console.print(f"Appended {symbolChoose} to tracked pairs.")
                    last_pending_fetch = now_ts
                    console.print(f"[yellow]Periodic task: fetching open orders at {datetime.fromtimestamp(now_ts)}[/yellow]")
                    recent = []
                    try:
                        # https://docs.ccxt.com/docs/exchanges/kraken#fetchordersbyids
                        recent = exchange.fetchOpenOrders()
                        # console.print(f"DEBUG recent={recent}")
                    except Exception as e:
                        log_exception(e, "periodic_task:fetchOpenOrders")
                        recent = []
                    # draw open orders
                    for o in recent:
                        try:
                            side = (o.get('side')).lower()
                            status = (o.get('status')).lower()
                            symbol_o = o.get('symbol')
                            sell_ts = o.get('timestamp')
                            sell_price = o.get('price')
                            filled = o.get('filled')
                            amount_o = o.get('amount')
                            if side == 'sell' and status == 'open':
                                console.print(f"Sell order: {symbol_o} {sell_price} {amount_o} {sell_ts}")
                            elif side == 'buy' and status == 'open':
                                console.print(f"Buy order: {symbol_o} {sell_price} {amount_o} {sell_ts}")
                        except Exception as e:
                            log_exception(e, "periodic_task:order_sort")
                    # TODO match order ids with the dump file
                    # Mettre à jour pending_orders_dump.json en retirant les snapshots
                    # dont l'ordre n'apparaît plus dans recent
                    try:
                        # construire set d'ids récents
                        if recent is None:
                            # fetch failed earlier; skip pending dump update to avoid accidental clearing
                            recent_ids = None
                        else:
                            recent_ids = set()
                            for o in recent:
                                try:
                                    oid = o.get('id') or o.get('orderId') or o.get('clientOrderId')
                                except Exception as e:
                                    log_exception(e, "periodic_task:order_id_extract")
                                    oid = None
                                if not oid and isinstance(o.get('info'), dict):
                                    info = o.get('info')
                                    oid = info.get('id') or info.get('orderId') or info.get('txid')
                                if oid:
                                    recent_ids.add(str(oid))

                        if recent_ids is not None and os.path.exists(PENDING_DUMP_FILE):
                            try:
                                with open(PENDING_DUMP_FILE, 'r') as f:
                                    pending_list = json.load(f)
                            except Exception as e:
                                log_exception(e, "periodic_task:pending_read")
                                pending_list = []

                            # pending_list is a list of snapshots: {'ts':..., 'order': {...}}
                            new_pending = []
                            removed = 0
                            for snap in pending_list:
                                try:
                                    order = snap.get('order', {})
                                    oid = order.get('id') or order.get('orderId') or order.get('clientOrderId')
                                    if not oid and isinstance(order.get('info'), dict):
                                        info = order.get('info')
                                        oid = info.get('id') or info.get('orderId') or info.get('txid')
                                except Exception as e:
                                    oid = None
                                if oid and str(oid) in recent_ids:
                                    new_pending.append(snap)
                                else:
                                    removed += 1

                            if removed > 0:
                                try:
                                    safe_json.atomic_write_json(PENDING_DUMP_FILE, new_pending, backup=True, indent=2)
                                except Exception as e:
                                    log_exception(e, "periodic_task:pending_write")
                                    with open(PENDING_DUMP_FILE, 'w') as f:
                                        json.dump(new_pending, f, indent=2)
                                console.print(f"Pending dump updated: removed {removed} stale snapshot(s)")
                    except Exception as e:
                        log_exception(e, "failed to update pending dump")
            except Exception as e:
                log_exception(e, "periodic_task:main")

                # fetch ticker, order book, trades for each available pair
                #for availablePair in availablePairs:
                #    symbol = availablePair[0]
                #    base = availablePair[1]
                #    quote = availablePair[2]
                #    console.print(f"Available pair: {symbol} ({base}/{quote})")
                #    # fetch ticker
                #    ticker = exchange.fetch_ticker(symbol)
                #    console.print(ticker['symbol'])
                #    console.print(ticker['low'])
                #    # console.print(ticker)
                #    # fetch order book
                #    order_book = exchange.fetch_order_book(symbol)
                #    # console.print(order_book)
                #    # fetch trades
                #    trades = exchange.fetch_trades(symbol)
                #    # console.print(trades)

                # compute strategy
        
                # end step for allocating computationnal task
                # if psutil.cpu_percent(interval=0.4) < 80.1 and psutil.virtual_memory().percent < 96.1 and lowerSwap <= psutil.swap_memory().used and balanceFetched:
                    # console.print("CPU usage is below 80.1%, memory usage is below 96.1%, and swap is not growing. Allocating 1 computationnal task.")
                    # console.print(ticker['low'])

        # TODO exclure un symbole dont le trading count aurait baissé si sa balance est vide
        # TODO mettre en place un système d'horaires
        # TODO cherche d'autres symboles si un décompte de 50 paires ne peut être établi

        time.sleep(4.0)

    except KeyboardInterrupt:
        console.print("Bot exiting.")
        break

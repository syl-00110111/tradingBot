# init start
from rich.console import Console
console = Console()

with console.status("Bot init. Please wait or expect an random error.", spinner="dots"):
    import ccxt
    import asyncio
    import logging
    import time
    import pandas as pd
    import re
    import json
    import time
    import logging
    import signal
    import argparse
    import os
    import sys
    import platform
    import random
    import math
    import numpy as np
    import threading
    import queue
    from collections import deque
    import pandas as pd
    import torch
    import concurrent.futures
    import plotext as plt_ascii
    from datetime import datetime, timedelta, timezone
    from rich.live import Live
    from rich.table import Table
    from rich.progress import Progress
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.logging import RichHandler
    from rich.text import Text
    import readchar
    import psutil
    import time
    run = False
    currentSwap = psutil.swap_memory().used
    lowerSwap = currentSwap
    exchangeLoaded = False
    balanceFetched = False
    marketsFetched = False
    availablePairs = []
    # mapping symbol -> expiry_timestamp for paused buys (persisted to disk)
    pausedForBuy = {}
    PAUSE_FILE = 'paused_for_buy.json'
    # load persisted pauses if present
    if os.path.exists(PAUSE_FILE):
        try:
            with open(PAUSE_FILE, 'r') as f:
                pausedForBuy = json.load(f)
        except Exception:
            pausedForBuy = {}
    # liste d'assets sources, de leur valeur minimale de transaction, et de leur nom complexe
    sourceAssets = [['ZEC', 0.01, 'XZEC'], ['TAO', 0.025, "TAO"], ['BTC', 0.00005, 'XXBT'], ['ETH', 0.001, 'XETH'], ['USDC', 5, 'USDC'], ['USD', 5, 'ZUSD'], ['EUR', 5, 'ZEUR']]
    def loadExchange():
        if os.path.exists('api.json'):
            try:
                with open('api.json', 'r') as f: api_creds = json.load(f)
                exchange_id = api_creds.get('exchange_id')
                options = api_creds.get('options', {})
                defaultType = options.get('defaultType')
                exchange_config = {
                    'apiKey': api_creds.get('api_key'),
                    'secret': api_creds.get('api_secret'),
                    'enableRateLimit': True,
                    'options': {'defaultType': defaultType}
                }
                # console.print(exchange_config)
            except Exception as e:
                console.print(f"Error loading API credentials: {e}")
                sys.exit(1)
            try:
                exchange = getattr(ccxt, exchange_id)(exchange_config)
            except Exception as e:
                console.print(f"Error initializing exchange: {e}")
                sys.exit(1)
            return exchange
        else:
            console.print("api.json file not found. Please create it with your API credentials. There is an 'api.json.example' example file.")
            sys.exit(1)
    def loadMarkets(exchange):
        return exchange.load_markets()
    balance = None
    def fetch_balance(exchange):
        balance = exchange.fetch_balance()
        return balance
    # load config (merge default and optional override)
    config = {}
    try:
        if os.path.exists('config.default.json'):
            with open('config.default.json','r') as f: config = json.load(f)
        if os.path.exists('config.json'):
            with open('config.json','r') as f:
                override = json.load(f)
                if config: config.update(override)
                else: config = override
    except Exception:
        config = {}

    def fetch_ohlcv_data(symbol):
        if exchange.has.get('fetchOHLCV'):
            time.sleep (exchange.rateLimit / 1000) # time.sleep wants seconds
            # console.print(f"Fetching OHLCV data for {symbol} on Kraken...")
            # Charger les données existantes (si le fichier existe)
            existing_data = []
            dataFile = 'ohlcv_data_'+ symbol + '_1m' + '.json'
            data2 = []
            # lire fichier si existe
            if os.path.exists(dataFile):
                with open(dataFile, 'r') as f: data2 = json.load(f)
                currentTimestamp = int(time.time()*1000)  # Current timestamp in milliseconds
                lastTimestamp = int(data2[len(data2)-1][0])
                #console.print(f"Last Timestamp: {lastTimestamp}")
                #console.print(f"Current Timestamp: {currentTimestamp}")
                if lastTimestamp < currentTimestamp:
                    data = exchange.fetch_ohlcv (symbol, '1m', currentTimestamp - lastTimestamp)  # Fetch since lastTimestamp if file exists
                    # [ [1783382400000, 55953.0, 56217.1, 54798.5, 55529.0, 573.16980314], ... ]
                    # UTC timestamp in milliseconds, integer
                    #data[0].get('timestamp', 1783382400000)  # Example timestamp (milliseconds since epoch)
                    #data[0].get('open', 55953.0)  # Example open price
                    #data[0].get('highest', 56217.1)  # Example highest price
                    #data[0].get('lowest', 54798.5)  # Example lowest price
                    #data[0].get('closing', 55529.0)  # Example closing price
                    #data[0].get('volume', 573.16980314)  # Example volume
            else:
                data = exchange.fetch_ohlcv (symbol, '1m')  # Fetch max minutes of data if file doesn't exist
            _len = len(data)
            # console.print(f"Fetched {_len} OHLCV data points for {symbol} on Kraken.")
            # Afficher les données récupérées
            for i in range(max(1, _len)):
                data_point = data[i]
                # console.print(f"Timestamp: {data_point[0]}, Open: {data_point[1]}, High: {data_point[2]}, Low: {data_point[3]}, Close: {data_point[4]}, Volume: {data_point[5]}")
                # Retirer une bougie par son timestamp car souvent la dernière bougie était incomplète et doit être mise à jour
                timestamp_to_remove = data_point[0]
                # Parcourir les bougies pour trouver celle à supprimer
                for i, candle in enumerate(existing_data):
                    if candle[0] == timestamp_to_remove:  # s'il s'agit de celle-ci
                        del existing_data[i]
                        break  # Sortir de la boucle après suppression
            # Ajouter les nouvelles données
            existing_data.extend(data)
            # Sauvegarder
            with open(dataFile, 'w') as f:
                json.dump(existing_data, f, indent=4)
            # ohlcv: [ [ts, open, high, low, close, volume], ... ]
            return pd.DataFrame(existing_data, columns=['timestamp','open','high','low','close','volume'])

# boucle principale du bot
while True:
    try:
        # init end
        if run == False:
            console.print("Bot running.")
        run = True
        # init step for allocation of computationnal task
        currentSwap = psutil.swap_memory().used
        if currentSwap < lowerSwap:
            lowerSwap = currentSwap

        #exchange
        if exchangeLoaded == False:
            exchange = loadExchange()
            exchangeLoaded = True

        # interroger les assets disponibles sur la plateforme pour l'utilisateur et les stocker dans une variable globale
        if balanceFetched == False:
            balance = fetch_balance(exchange)
            balanceFetched = True
            console.print(f"original balance: {balance}")
        
        # markets fetch
        if marketsFetched == False:
            # déterminer les paires de trading disponibles pour l'utilisateur en fonction des assets sources et des paires disponibles sur la plateforme
            # assets depuis lesquelles l'utilisateur peut trader en fonction de son solde disponible et de la valeur minimale de transaction
            sourceAssets = [[asset, minAmount, wsname] for asset, minAmount, wsname in sourceAssets if asset in balance['free']]
            validAssets = [asset[2] for asset in sourceAssets]
            markets = loadMarkets(exchange)
            # build candidate list first (do not modify availablePairs yet)
            candidate_pairs = []
            for _market in markets.items():
                # si spot, actif et quote dans les validAssets
                if _market[1].get('active') == True and _market[1].get('spot') == True and _market[1].get('info').get('quote') in validAssets:
                    # keep consistent structure: [symbol, id, base, quote]
                    candidate_pairs.append([_market[1].get('symbol'), _market[1].get('id'), _market[1].get('info').get('base'), _market[1].get('info').get('quote')])
            console.print(f"Found {candidate_pairs} after validAssets+active+spot")
            # Ne garder que les 40 paires les plus dynamiques en se basant sur le volume et le nombre de trades
            try:
                # Attempt to load cached metrics if recent (less than 2 hours old)
                cache_file = 'volumes_trades_data.json'
                cache_loaded = False
                dynamic_metrics = []
                try:
                    if os.path.exists(cache_file):
                        with open(cache_file, 'r') as f:
                            cache = json.load(f)
                        cache_ts = int(cache.get('timestamp', 0))
                        age_ms = int(time.time() * 1000) - cache_ts
                        if age_ms < (2 * 60 * 60 * 1000):
                            console.print(f"Using cached volumes/trades data (age {age_ms/1000:.0f}s)")
                            for m in cache.get('metrics', []):
                                dynamic_metrics.append((m.get('symbol'), m.get('id'), m.get('base'), m.get('quote'), float(m.get('vol', 0.0)), int(m.get('trades_count', 0))))
                            cache_loaded = True
                except Exception as e:
                    console.print(f"Warning: failed to load cache file: {e}")
                if not cache_loaded:
                    # collect volume (one call for all pairs) and recent trades count for each pair
                    try:
                        time.sleep(exchange.rateLimit / 1000)
                        all_tickers = exchange.fetch_tickers()
                    except Exception as e:
                        console.print(f"Warning: failed to fetch tickers: {e}")
                        all_tickers = {}
                    # compute 'since' timestamp for last 120 minutes
                    since_120m = int((time.time() - (120 * 60)) * 1000)
                    metrics_to_cache = []
                    for availablePair in candidate_pairs:
                        symbol = availablePair[0]
                        mid = availablePair[1]
                        base_asset = availablePair[2]
                        quote_asset = availablePair[3]
                        try:
                            # get volume from the pre-fetched tickers
                            vol = 0.0
                            ticker = all_tickers.get(symbol) if isinstance(all_tickers, dict) else None
                            if ticker is not None:
                                vol = float(ticker.get('baseVolume') or ticker.get('quoteVolume') or 0.0)
                                console.print(f"Fetched volume for {symbol}: {vol}")
                            else:
                                # fallback to single ticker fetch if needed
                                try:
                                    time.sleep(exchange.rateLimit / 2000)
                                    single_t = exchange.fetch_ticker(symbol)
                                    vol = float(single_t.get('baseVolume') or single_t.get('quoteVolume') or 0.0)
                                    console.print(f"Fetched single-ticker volume for {symbol}: {vol}")
                                except Exception:
                                    vol = 0.0
                            # fetch trades only from the last 120 minutes using 'since'
                            time.sleep(exchange.rateLimit / 1000)
                            trades = exchange.fetch_trades(symbol, since_120m)
                            trades_count = len(trades) if trades is not None else 0
                            console.print(f"Fetched trades count (last 120m) for {symbol}: {trades_count}")
                            # store tuple: symbol, id, base, quote, volume, trades_count
                            dynamic_metrics.append((symbol, mid, base_asset, quote_asset, vol, trades_count))
                            metrics_to_cache.append({'symbol': symbol, 'id': mid, 'base': base_asset, 'quote': quote_asset, 'vol': vol, 'trades_count': trades_count})
                        except Exception as e:
                            console.print(f"Warning: failed to fetch metrics for {symbol}: {e}")
                    # save cache
                    try:
                        cache_obj = {'timestamp': int(time.time() * 1000), 'metrics': metrics_to_cache}
                        with open(cache_file, 'w') as f:
                            json.dump(cache_obj, f, indent=2)
                        console.print(f"Saved volumes/trades cache to {cache_file}")
                    except Exception as e:
                        console.print(f"Warning: failed to write cache file: {e}")
                # sort by trades_count
                # trades_count index is 5 in the tuple (s,id,base,quote,vol,trades_count)
                dynamic_metrics.sort(key=lambda x: (x[5]), reverse=True)
                top_n = 40
                selected = dynamic_metrics[:top_n]
                # rebuild availablePairs to contain only top selected (consistent [symbol, id, base, quote])
                availablePairs = [[s, i, b, q] for (s, i, b, q, v, t) in selected]
                console.print(f"Filtered to top {len(availablePairs)} dynamic pairs based on weighted trades_count+volume")
                console.print(f"availablePairs après tri: {availablePairs}")
                # Ensure pairs for which the user holds a balance are kept available for potential sells (or buys)
                try:
                    for cp in candidate_pairs:
                        sym, mid, base_asset, quote_asset = cp
                        # read balances robustly (ccxt balance structure varies)
                        try:
                            base_free = float(balance.get('free', {}).get(base_asset, 0) or 0)
                        except Exception:
                            base_free = 0.0
                        try:
                            quote_free = float(balance.get('free', {}).get(quote_asset, 0) or 0)
                        except Exception:
                            quote_free = 0.0
                        if (base_free > 0 or quote_free > 0) and not any(p[0] == sym for p in availablePairs):
                            availablePairs.append([sym, mid, base_asset, quote_asset])
                            console.print(f"Added {sym} to availablePairs because balance base:{base_free} quote:{quote_free}")
                except Exception as e:
                    console.print(f"Warning: failed to ensure balance-based pairs included: {e}")
            except Exception as e:
                console.print(f"Warning: failed to filter dynamic pairs: {e}")
            marketsFetched = True

        # taux
        # comparer taux xxbt / xeth / zeur pour aave par exemple

        # paires

        # watch orders

        # watch balance

        # watch new candles

        # For each available pair, fetch recent OHLCV candles (1m) to feed strategies
        candles_per_pair = {}
        if exchange.has.get('fetchOHLCV'):
            for availablePair in availablePairs:
                symbol = availablePair[0]
                try:
                    candles_per_pair[symbol] = fetch_ohlcv_data(availablePair[1])
                except Exception as e:
                    console.print(f"Failed to fetch OHLCV for {symbol}: {e}")
        # trading state (position tracking for profit calc)
        positions = {}  # key: symbol, value: {'amount': float, 'avg_price': float}
        executed_orders = []
        # import strategy helper
        try:
            from indicators2 import get_signals
        except Exception as e:
            console.print(f"Warning: unable to import indicators2.get_signals: {e}")
        # For each pair, compute aggregated strategy signals (based on strategie.py) and act on latest signal
        STRATS = [
            'pairs_trading_proxy',
            'liquidation_cascade_proxy',
            'williams_r',
            'mc_dynamic_allocation',
            'vwap_momentum'
        ]
        def consecutive_count(series):
            out = [0]*len(series)
            count = 0
            for i, v in enumerate(series):
                if bool(v):
                    count += 1
                else:
                    count = 0
                out[i] = count
            return out

        for availablePair in availablePairs:
            symbol = availablePair[0]
            # availablePair format: [symbol, id, base, quote]
            base = availablePair[2]
            quote = availablePair[3]
            df_candles = candles_per_pair.get(symbol)
            if df_candles is None or df_candles.empty:
                continue
            N = len(df_candles)
            # compute per-strategy signals
            signal_frames = {}
            for strat in STRATS:
                try:
                    settings = {'strategy': strat, 'device': None}
                    df_sign = get_signals(df_candles.copy(), settings, is_scan=True, global_config=config)
                    if df_sign is None:
                        df_sign = pd.DataFrame(index=df_candles.index)
                    signal_frames[strat] = df_sign
                except Exception as e:
                    console.print(f"Signal computation failed for {symbol} / {strat}: {e}")
                    signal_frames[strat] = pd.DataFrame(index=df_candles.index)

            # aggregate scores (appliqué depuis strategie.py)
            # utilises deux vecteurs distincts pour confirmations buy / sell
            score_buy = [0.0] * N
            score_sell = [0.0] * N

            # 1) pairs_trading_proxy: double-confirm only after 4 buy or 6 sell signals in a row
            pt = signal_frames.get('pairs_trading_proxy')
            if pt is not None and not pt.empty:
                buys = consecutive_count(pt.get('buy_signal', pd.Series([False]*N)).fillna(False).tolist())
                sells = consecutive_count(pt.get('sell_signal', pd.Series([False]*N)).fillna(False).tolist())
                for i in range(N):
                    if buys[i] >= 4:
                        score_buy[i] += 2
                    if sells[i] >= 6:
                        score_sell[i] += 2

            # 2) liquidation_cascade_proxy: demi-confirmation si difference de 3 sur last 4 typed signals
            lc = signal_frames.get('liquidation_cascade_proxy')
            if lc is not None and not lc.empty:
                # utilise la colonne numerique 'score' produite par la strategie (1=buy, -1=sell, 0=none)
                score_series = lc.get('score', pd.Series([0]*N)).fillna(0).astype(int)
                for i in range(N):
                    start = max(0, i-3)
                    window_sum = int(score_series.iloc[start:i+1].sum())
                    if window_sum >= 3:
                        score_buy[i] += 1
                    elif window_sum <= -3:
                        score_sell[i] += 1

            # 3) williams_r: demi-confirmation by subtraction
            wr = signal_frames.get('williams_r')
            if wr is not None and not wr.empty:
                buy_s = wr.get('buy_signal', pd.Series([False]*N)).fillna(False).astype(bool)
                sell_s = wr.get('sell_signal', pd.Series([False]*N)).fillna(False).astype(bool)
                for i in range(N):
                    net = int(buy_s.iloc[i]) - int(sell_s.iloc[i])
                    if net > 0:
                        score_buy[i] += 1
                    elif net < 0:
                        score_sell[i] += 1

            # 4) mc_dynamic_allocation: confirm buy if >18 sell_signals in a row
            mc = signal_frames.get('mc_dynamic_allocation')
            if mc is not None and not mc.empty:
                sells_run = consecutive_count(mc.get('sell_signal', pd.Series([False]*N)).fillna(False).tolist())
                for i in range(N):
                    if sells_run[i] > 18:
                        score_buy[i] += 1

            # 5) vwap_momentum: confirm sells if >10 buy_signals in a row
            vw = signal_frames.get('vwap_momentum')
            if vw is not None and not vw.empty:
                buys_run = consecutive_count(vw.get('buy_signal', pd.Series([False]*N)).fillna(False).tolist())
                for i in range(N):
                    if buys_run[i] > 10:
                        score_sell[i] += 1

            # construire signaux globaux
            global_buy = [s >= 3.0 for s in score_buy]
            global_sell = [s >= 3.0 for s in score_sell]

            # act on the latest candle
            if N == 0:
                continue
            latest_idx = N - 1
            last_close = float(df_candles.iloc[latest_idx]['close'])

            # fetch current order book to price orders
            try:
                order_book = exchange.fetch_order_book(symbol)
            except Exception as e:
                console.print(f"Failed to fetch order book for {symbol}: {e}")
                order_book = {'asks':[], 'bids':[]}

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
                            with open(PAUSE_FILE, 'w') as f:
                                json.dump(pausedForBuy, f)
                        except Exception:
                            pass
                    # compute amount to buy (use config.max_trade_percentage of quote balance)
                    pct = float(config.get('max_trade_percentage', 2.0)) / 10.0
                    # read quote balance robustly
                    try:
                        quote_free = float(balance.get('info').get('result').get(quote).get('balance'))
                    except Exception:
                        quote_free = 0.0
                    if quote_free <= 0:
                        console.print(f"No quote balance available for {symbol} to BUY")
                    else:
                        price = order_book.get('asks')[0][0] if order_book.get('asks') else last_close
                        amount = (quote_free * pct) / price if price > 0 else 0
                        if amount <= 0:
                            console.print(f"Calculated buy amount is zero for {symbol}")
                        else:
                            # enforce minimal buy in EUR
                            min_buy_eur = float(config.get('min_buy_eur', 5.0))
                            if price * amount < min_buy_eur:
                                # try to bump amount to reach minimal EUR if quote balance allows
                                needed = min_buy_eur / price if price > 0 else 0
                                if quote_free >= min_buy_eur:
                                    amount = needed
                                    console.print(f"Adjusted buy amount for {symbol} to meet min {min_buy_eur} EUR -> amount={amount:.8f}")
                                else:
                                    console.print(f"Skipping BUY for {symbol}: not enough quote balance to meet min {min_buy_eur} EUR")
                                    amount = 0
                            # final amount check
                            if amount and amount > 0:
                                try:
                                    console.print(f"Placing LIMIT BUY {symbol} amount={amount:.8f} price={price}")
                                    order = exchange.create_limit_buy_order(symbol, amount, price)
                                    executed_orders.append(order)
                                    # update positions
                                    pos = positions.get(symbol, {'amount':0.0,'avg_price':0.0})
                                    total_cost = pos['avg_price']*pos['amount'] + price*amount
                                    new_amount = pos['amount'] + amount
                                    new_avg = (total_cost / new_amount) if new_amount>0 else 0.0
                                    positions[symbol] = {'amount': new_amount, 'avg_price': new_avg}
                                    console.print(f"BUY executed: {order}")
                                    # plot a small chart with the BUY marker
                                    try:
                                        plt_ascii.clear_figure()
                                        plt_ascii.theme('dark')
                                        plt_ascii.title(f"{symbol} - BUY")
                                        timestamps = df_candles['timestamp'].astype(int).tolist()
                                        opens = df_candles['open'].astype(float).tolist()
                                        highs = df_candles['high'].astype(float).tolist()
                                        lows = df_candles['low'].astype(float).tolist()
                                        closes = df_candles['close'].astype(float).tolist()
                                        data = {"Open": opens, "High": highs, "Low": lows, "Close": closes}
                                        x = list(range(len(closes)))
                                        plt_ascii.candlestick(x, data)
                                        plt_ascii.scatter([latest_idx], [closes[latest_idx]], marker='x', color='green')
                                        plt_ascii.show()
                                    except Exception as e:
                                        console.print(f"Plot failed for BUY {symbol}: {e}")
                                except Exception as e:
                                    console.print(f"Buy order failed for {symbol}: {e}")
                                    # detect Kraken-specific errors and pause buys for 2 hours for this symbol
                                    err = str(e).lower()
                                    if ('minimum' in err and 'not met' in err) or ('invalid arguments' in err and 'volume' in err) or ('invalid permissions' in err) or ('must be greater than minimum' in err):
                                        expiry_ts = int(time.time()) + (2 * 3600)
                                        pausedForBuy[symbol] = expiry_ts
                                        try:
                                            with open(PAUSE_FILE, 'w') as f:
                                                json.dump(pausedForBuy, f)
                                        except Exception as ex:
                                            console.print(f"Failed to persist pausedForBuy: {ex}")
                                        console.print(f"Paused buys for {symbol} until {datetime.fromtimestamp(expiry_ts)} due to error: {e}")

            # decide sell
            if global_sell[latest_idx]:
                # pct = float(config.get('max_trade_percentage', 2.0)) / 200.0
                #base_free = float(balance.get('free', {}).get(base, 0) or 0)
                console.print(f"testSell ?")
                if base in balance['free']:
                    base_free = float(balance.get('info').get('result').get(base).get('balance'))
                else:
                    base_free = 0
                if base_free <= 0:
                    console.print(f"No base balance available for {symbol} to SELL")
                else:
                    price = order_book.get('bids')[0][0] if order_book.get('bids') else last_close
                    amount = base_free * 1
                    if amount <= 0:
                        console.print(f"Calculated sell amount is zero for {symbol}")
                    else:
                        try:
                            console.print(f"Placing LIMIT SELL {symbol} amount={amount:.8f} price={price}")
                            order = exchange.create_limit_sell_order(symbol, amount, price)
                            executed_orders.append(order)
                            # compute profit against tracked positions
                            pos = positions.get(symbol, {'amount':0.0,'avg_price':0.0})
                            sold_amount = min(amount, pos['amount']) if pos['amount']>0 else amount
                            profit = 0.0
                            if pos['amount']>0 and sold_amount>0:
                                profit = (price - pos['avg_price']) * sold_amount
                                # update position
                                remaining = pos['amount'] - sold_amount
                                if remaining <= 0:
                                    positions.pop(symbol, None)
                                else:
                                    positions[symbol] = {'amount': remaining, 'avg_price': pos['avg_price']}
                            console.print(f"SELL executed: {order}")
                            console.print(f"Profit realized for {symbol}: {profit}")
                            # plot SELL
                            try:
                                plt_ascii.clear_figure()
                                plt_ascii.theme('dark')
                                plt_ascii.title(f"{symbol} - SELL")
                                timestamps = df_candles['timestamp'].astype(int).tolist()
                                opens = df_candles['open'].astype(float).tolist()
                                highs = df_candles['high'].astype(float).tolist()
                                lows = df_candles['low'].astype(float).tolist()
                                closes = df_candles['close'].astype(float).tolist()
                                data = {"Open": opens, "High": highs, "Low": lows, "Close": closes}
                                x = list(range(len(closes)))
                                plt_ascii.candlestick(x, data)
                                plt_ascii.scatter([latest_idx], [closes[latest_idx]], marker='o', color='red')
                                plt_ascii.show()
                            except Exception as e:
                                console.print(f"Plot failed for SELL {symbol}: {e}")
                        except Exception as e:
                            console.print(f"Sell order failed for {symbol}: {e}")

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
        time.sleep(0.1)

    except KeyboardInterrupt:
        console.print("Bot exiting.")
        break

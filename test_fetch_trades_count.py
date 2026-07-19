# init start
from rich.console import Console
console = Console()

with console.status("Fetch trades count. Please wait or expect an random error if you break.", spinner="dots"):
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

    exchange = loadExchange()

    def loadMarkets(exchange, file):
        try:
            time.sleep(exchange.rateLimit / 1000)
            markets = exchange.load_markets()
        except Exception as e:
            console.print(f"Markets fetch exception: {e}")
        try:
            with open(file, 'w') as f: markets = json.dump(markets, f, indent=4)
        except Exception as e:
            console.print(f"Markets backup file exception: {e}")
        return markets

    def readMarkets(file):
        try:
            with open(file, 'r') as f: markets = json.load(f)
            return markets
        except Exception as e:
            console.print(f"Error loading markets.json file: {e}")
    
    loadMarkets(exchange, "markets.json")
    
    data = readMarkets("markets.json")

    availablePairs = []

    # id, baseId, quoteId, spot (bool), active (bool)
    for _m in data.items():
        _a = [_m[1].get('symbol'), _m[1].get('id'), _m[1].get('base'), _m[1].get('quote')]
        availablePairs.append(_a)

    # Ne garder que les 40 paires les plus dynamiques en se basant sur le volume et le nombre de trades
    try:
        # Attempt to load cached metrics if recent (less than 24 hours old)
        cache_file = 'volumes_trades_data.json'
        dynamic_metrics = []
        # collect volume (one call for all pairs) and recent trades count for each pair
        try:
            time.sleep(exchange.rateLimit / 1000)
            all_tickers = exchange.fetch_tickers()
        except Exception as e:
            console.print(f"Warning: failed to fetch tickers: {e}")
            all_tickers = {}
        # compute 'since' timestamp for last 4 heures
        since_4h = int(time.time()) * 1000 - (4*3600*1000)
        metrics_to_cache = []
        for availablePair in availablePairs:
            #quirck
            if availablePair[0]:
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
                            time.sleep(exchange.rateLimit / 1000)
                            single_t = exchange.fetch_ticker(symbol)
                            vol = float(single_t.get('baseVolume') or single_t.get('quoteVolume') or 0.0)
                            console.print(f"Fetched single-ticker volume for {symbol}: {vol}")
                        except Exception:
                            vol = 0.0
                    # fetch trades only from the last 4 heures using 'since'
                    time.sleep(exchange.rateLimit / 1000)
                    trades = exchange.fetch_trades(symbol, since_4h)
                    trades_count = len(trades) if trades is not None else 0
                    console.print(f"Fetched trades count (last 4h) for {symbol}: {trades_count}")
                    # store tuple: symbol, id, base, quote, volume, trades_count
                    dynamic_metrics.append((symbol, mid, base_asset, quote_asset, vol, trades_count))
                    metrics_to_cache.append({'symbol': symbol, 'id': mid, 'base': base_asset, 'quote': quote_asset, 'vol': vol, 'trades_count': trades_count})
                    try:
                        with open(cache_file, 'w') as f: markets = json.dump(metrics_to_cache, f, indent=4)
                    except Exception as e:
                        console.print(f"Markets backup file exception: {e}")
                except Exception as e:
                    console.print(f"Warning: failed to fetch metrics for {symbol}: {e}")
    except Exception as e:
        console.print(f"Warning: failed to filter dynamic pairs: {e}")

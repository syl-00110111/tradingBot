# init start
from rich.console import Console
console = Console()

with console.status("Bot init. Please wait some time, or expect a random error if you break.", spinner="dots"):
    import ccxt
    import asyncio
    import logging
    import time
    import pandas as pd
    import pandas_ta as ta
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
    import safe_json
    import market_utils
    import symbols_utils
    run = False
    #currentSwap = psutil.swap_memory().used
    #lowerSwap = currentSwap

    exchangeLoaded = False
    balanceFetched = False
    marketsFetched = False
    sourceAssets = []
    forbidAssets = ['USDT', 'XMR']
    previousPairs = []
    availablePairs = []
    maxNumPairs = 50

    # ccxt markets keyword is used
    _markets = []
    _balance = None
    _positions = {}  # key: symbol, value: {'amount': float, 'avg_price': float}

    # periodic pending orders dump / candle consistency check
    last_pending_fetch = 0
    PENDING_DUMP_FILE = 'pending_orders_dump.json'

    miniCount = 600
    # monnaies d'usage pour considérer les paires à leur quote asset
    baseAssets = ["USD", "EUR"]

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
                except Exception:
                    cur = []
            cur.append(snapshot)
            # write atomically to avoid partial files on interruption
            try:
                safe_json.atomic_write_json(PENDING_DUMP_FILE, cur, backup=True, indent=2)
            except Exception:
                # fallback to plain write
                with open(PENDING_DUMP_FILE, 'w') as f:
                    json.dump(cur, f, indent=2)
            return snapshot
        except Exception as e:
            console.print(f"dump_pending_order failed: {e}")
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
                    except Exception:
                        cur = []
            return {'ts': int(time.time()), 'snapshots': cur}
        except Exception as e:
            console.print(f"dump_pending_orders failed (file read): {e}")
            return None

    def cleanup_open_orders(exchange, symbol, new_price, side, df_candles, last_close):
        """Fetches open orders for the symbol and cancels a previous order set to be replaced
        by the new order, ONLY if:
        1. The side has changed (old order side != new order side)
        2. The execution probability of the old order based on price convergence (via Monte Carlo) is no longer sufficient.
        """
        try:
            console.print(f"[{symbol}] Running open orders check before placing new order (side {side}) at price {new_price}...")
            time.sleep(exchange.rateLimit / 1000)

            open_orders = []
            try:
                open_orders = exchange.fetch_open_orders(symbol)
            except Exception as e:
                console.print(f"[{symbol}] exchange.fetch_open_orders(symbol) failed, trying fallback: {e}")
                try:
                    time.sleep(exchange.rateLimit / 1000)
                    all_open = exchange.fetch_open_orders()
                    if all_open:
                        open_orders = [o for o in all_open if o.get('symbol') == symbol]
                except Exception as ex:
                    console.print(f"[{symbol}] Fallback fetch_open_orders failed: {ex}")
                    return

            if not open_orders:
                console.print(f"[{symbol}] No open orders found.")
                return

            # Compute volatility & drift for Monte Carlo from df_candles
            volatility = 0.0
            drift = 0.0
            if df_candles is not None and len(df_candles) > 1:
                try:
                    closes = pd.to_numeric(df_candles['close'], errors='coerce')
                    returns = np.log(closes / closes.shift(1)).replace([np.inf, -np.inf], np.nan).dropna()
                    if len(returns) > 1:
                        volatility = float(returns.std())
                        drift = float(returns.mean())
                except Exception as ve:
                    console.print(f"[{symbol}] Error computing volatility for Monte Carlo: {ve}")

            from monte_carlo2 import MonteCarloEngine
            mc_engine = MonteCarloEngine(num_simulations=1000, timeframe_candles=100)

            # Get the probability threshold from config
            threshold = 0.15
            if config and isinstance(config, dict):
                threshold = config.get('monte_carlo', {}).get('sufficient_probability', 0.15)

            for o in open_orders:
                oid = o.get('id') or o.get('orderId')
                if not oid:
                    continue

                o_side = o.get('side')
                o_price = o.get('price')
                if o_side is None or o_price is None:
                    continue

                o_side_lower = o_side.lower()
                side_lower = side.lower()
                o_price_float = float(o_price)

                # Requirement 3: "only proceed with the cancellation if the side has changed and that probability is no longer sufficient."
                side_changed = (o_side_lower != side_lower)

                if side_changed:
                    # Check execution probability based on price convergence
                    mode = "below" if o_side_lower == "buy" else "above"
                    prob = mc_engine.estimate_hit_probability(
                        current_price=last_close,
                        target_price=o_price_float,
                        volatility=volatility,
                        drift=drift,
                        mode=mode
                    )
                    insufficient_prob = (prob < threshold)

                    console.print(f"[{symbol}] Found previous open order {oid} ({o_side} at {o_price}). New order side is {side}. Side changed: {side_changed}. Execution probability: {prob:.4f} (threshold: {threshold})")

                    if insufficient_prob:
                        console.print(f"[{symbol}] Cancelling order {oid}: Side changed and execution probability ({prob:.4f}) is no longer sufficient (< {threshold})")
                        try:
                            time.sleep(exchange.rateLimit / 1000)
                            try:
                                exchange.cancel_order(oid, symbol)
                            except TypeError:
                                exchange.cancel_order(oid)
                            console.print(f"[{symbol}] Order {oid} successfully cancelled.")
                        except Exception as e:
                            console.print(f"[{symbol}] Failed to cancel order {oid}: {e}")
                    else:
                        console.print(f"[{symbol}] Keeping order {oid} as execution probability ({prob:.4f}) is still sufficient (>= {threshold})")
                else:
                    console.print(f"[{symbol}] Keeping order {oid} since side has not changed ({o_side_lower} == {side_lower})")

        except Exception as e:
            console.print(f"[{symbol}] Exception in cleanup_open_orders: {e}")


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

    # File to persist recorded purchases to check profitability of SELL events
    PURCHASES_FILE = 'recorded_purchases.json'
    recorded_purchases = {}
    if os.path.exists(PURCHASES_FILE):
        try:
            with open(PURCHASES_FILE, 'r') as f:
                recorded_purchases = json.load(f)
        except Exception:
            recorded_purchases = {}

    def record_purchase(symbol, amount, price):
        """Record a BUY event (purchase) to recorded_purchases."""
        try:
            if symbol not in recorded_purchases:
                recorded_purchases[symbol] = []
            recorded_purchases[symbol].append({
                'timestamp': int(time.time()),
                'amount': float(amount),
                'price': float(price)
            })
            try:
                safe_json.atomic_write_json(PURCHASES_FILE, recorded_purchases, backup=True, indent=2)
            except Exception:
                with open(PURCHASES_FILE, 'w') as f:
                    json.dump(recorded_purchases, f, indent=2)
            console.print(f"[{symbol}] Recorded purchase of {amount} at price {price}")
        except Exception as e:
            console.print(f"[{symbol}] Failed to record purchase: {e}")

    def is_sell_profitable(symbol, sell_price, sell_amount):
        """Check if selling sell_amount at sell_price is profitable against recorded purchases.
        We match the sell_amount against the oldest recorded purchases (FIFO) or average price.
        If there are no recorded purchases, we default to True to allow the sell.
        Returns (is_profitable, details_str).
        """
        try:
            purchases = recorded_purchases.get(symbol, [])
            if not purchases:
                return True, "No recorded purchases found for symbol, allowing sell by default."

            # Calculate the weighted average price of our remaining recorded purchases
            total_amount = sum(float(p['amount']) for p in purchases)
            if total_amount <= 0:
                return True, "No remaining recorded purchase amount, allowing sell by default."

            weighted_sum = sum(float(p['amount']) * float(p['price']) for p in purchases)
            avg_purchase_price = weighted_sum / total_amount

            # A sell is profitable if sell_price > avg_purchase_price
            profitable = float(sell_price) > avg_purchase_price
            details = f"Sell Price: {sell_price:.8f} vs Avg Purchase Price: {avg_purchase_price:.8f} (Remaining Amount: {total_amount:.6f})"
            return profitable, details
        except Exception as e:
            console.print(f"[{symbol}] Exception in is_sell_profitable check: {e}")
            return True, f"Error in profitability check: {e}. Allowing sell by default."

    def remove_recorded_purchases(symbol, sell_amount):
        """Deduct sell_amount from our recorded purchases (FIFO manner) after a successful SELL."""
        try:
            purchases = recorded_purchases.get(symbol, [])
            if not purchases:
                return

            remaining_to_deduct = float(sell_amount)
            new_purchases = []
            for p in purchases:
                if remaining_to_deduct <= 0:
                    new_purchases.append(p)
                    continue

                p_amount = float(p['amount'])
                if p_amount <= remaining_to_deduct:
                    remaining_to_deduct -= p_amount
                    # This purchase is fully matched/exhausted, so we don't append it to new_purchases
                else:
                    p['amount'] = p_amount - remaining_to_deduct
                    remaining_to_deduct = 0.0
                    new_purchases.append(p)

            recorded_purchases[symbol] = new_purchases
            try:
                safe_json.atomic_write_json(PURCHASES_FILE, recorded_purchases, backup=True, indent=2)
            except Exception:
                with open(PURCHASES_FILE, 'w') as f:
                    json.dump(recorded_purchases, f, indent=2)
            console.print(f"[{symbol}] Deducted {sell_amount} from recorded purchases. Remaining recorded lots: {len(new_purchases)}")
        except Exception as e:
            console.print(f"[{symbol}] Failed to update recorded purchases after sell: {e}")

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

    # import strategy helper
    try:
        from indicators2 import get_signals
    except Exception as e:
        console.print(f"Warning: unable to import indicators2.get_signals: {e}")
    try:
        from strategy_aggregator import aggregate_signals
    except Exception as e:
        console.print(f"Warning: unable to import strategy_aggregator.aggregate_signals: {e}")

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

    def loadMarkets(exchange, file):
        try:
            time.sleep(exchange.rateLimit / 1000)
            _markets = exchange.load_markets()
        except Exception as e:
            console.print(f"Markets fetch exception: {e}")
        try:
            # write markets backup atomically
            try:
                safe_json.atomic_write_json(file, _markets, backup=True, indent=4)
            except Exception:
                with open(file, 'w') as f:
                    json.dump(_markets, f, indent=4)
        except Exception as e:
            console.print(f"Markets backup file exception: {e}")
        return _markets

    def readMarkets(file):
        try:
            with open(file, 'r') as f: _markets = json.load(f)
        except Exception as e:
            console.print(f"Error loading markets.json file: {e}")
        return _markets

# boucle principale du bot
if __name__ == '__main__':
    while True:
        try:
            # init end
            if run == False:
                console.print("Bot running.")
                run = True
            # init step for allocation of computationnal task
            #currentSwap = psutil.swap_memory().used
            #if currentSwap < lowerSwap:
            #    lowerSwap = currentSwap

            #exchange
            if exchangeLoaded == False:
                exchange = loadExchange()
                exchangeLoaded = True

            # interroger les assets disponibles sur la plateforme pour l'utilisateur et les stocker dans une variable globale
            if balanceFetched == False:
                _balance = market_utils.fetch_balance(exchange, console=console)
                balanceFetched = True
                # console.print(f"original balance: {_balance}")

            # markets fetch
            if marketsFetched == False:
                _markets = loadMarkets(exchange, "markets.json")
                availablePairs = symbols_utils.computeSymbols(
                    balance=_balance,
                    previousPairs=None,
                    source_assets=sourceAssets,
                    forbid_assets=forbidAssets,
                    base_assets=baseAssets,
                    max_num_pairs=maxNumPairs,
                    mini_count=miniCount,
                    console=console
                )
                marketsFetched = True

            # taux
            # comparer taux xxbt / xeth / zeur pour aave par exemple

            # paires

            # watch orders

            # watch balance

            # watch new candles

            if run == True and exchangeLoaded == True and balanceFetched == True and marketsFetched == True:
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
                            candles_per_pair[symbol] = market_utils.fetch_ohlcv_data(
                                exchange=exchange,
                                _id=_id,
                                symbol=symbol,
                                pausedForBuy=pausedForBuy,
                                PAUSE_FILE=PAUSE_FILE,
                                console=console
                            ).tail(180)  # (TODO TEST variance temporelle 180 minutes)
                            try:
                                # vérifier la cohérence des chandelles immédiatement après le fetch
                                market_utils.check_candles_consistency(symbol, console=console)
                            except Exception as e:
                                console.print(f"check_candles_consistency failed for {symbol}: {e}")
                        except Exception as e:
                            console.print(f"Failed to fetch OHLCV for {symbol}: {e}")
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
                        console.print(f"Warning: aggregate_signals failed for {symbol}: {e}")
                        continue
                    N = res.get('N', 0)
                    signal_frames = res.get('signal_frames', {})
                    global_buy = res.get('global_buy', [])
                    global_sell = res.get('global_sell', [])
                    if N == 0:
                        continue
                    # act on the latest candle
                    latest_idx = N - 1

                    # 1/ Only calculate the reference price, trend, and regime when a signal is present.
                    if not (global_buy[latest_idx] or global_sell[latest_idx]):
                        continue

                    last_close = float(df_candles.iloc[latest_idx]['close'])

                    # 1/ récupération fetch_trades ou moyenne des chandelles stockées pour calcul seuils
                    ref_price = None
                    try:
                        time.sleep(exchange.rateLimit / 1000)
                        public_trades = exchange.fetch_trades(symbol, limit=20)
                        if public_trades and len(public_trades) > 0:
                            ref_price = sum(float(t['price']) for t in public_trades) / len(public_trades)
                            console.print(f"[{symbol}] Prix de référence calculé sur {len(public_trades)} trades publics: {ref_price:.8f}")
                    except Exception as e:
                        console.print(f"[{symbol}] Échec de fetch_trades, fallback sur les chandelles: {e}")

                    if ref_price is None:
                        if len(df_candles) >= 20:
                            ref_price = float(df_candles['close'].tail(20).mean())
                        else:
                            ref_price = float(df_candles['close'].mean())
                        console.print(f"[{symbol}] Prix de référence calculé sur les chandelles stockées: {ref_price:.8f}")

                    # 2/ prise en compte tendance Bullish / Bearish
                    if len(df_candles) >= 50:
                        sma_50 = float(df_candles['close'].tail(50).mean())
                    else:
                        sma_50 = float(df_candles['close'].mean())
                    is_bullish = last_close > sma_50
                    trend_str = 'Bullish' if is_bullish else 'Bearish'
                    console.print(f"[{symbol}] Tendance détectée: {trend_str} (Last Close: {last_close:.8f} vs SMA 50: {sma_50:.8f})")

                    # 3/ détection trend following / mean reversion
                    regime_str = 'Mean Reversion'
                    try:
                        adx_df = ta.adx(df_candles['high'], df_candles['low'], df_candles['close'], length=14)
                        if adx_df is not None and not adx_df.empty:
                            adx_val = float(adx_df.iloc[-1, 0])
                            if adx_val > 25:
                                regime_str = 'Trend Following'
                            console.print(f"[{symbol}] ADX: {adx_val:.2f} -> Régime détecté: {regime_str}")
                        else:
                            console.print(f"[{symbol}] ADX indisponible, régime par défaut: {regime_str}")
                    except Exception as e:
                        console.print(f"[{symbol}] Échec du calcul de l'ADX, régime par défaut: {regime_str}: {e}")

                    # Ajustement des seuils de déclenchement
                    # c'était 6-6
                    buy_multiplier = 0.9994
                    sell_multiplier = 1.0006

                    if regime_str == 'Trend Following':
                        if is_bullish:
                            # c'était 3-10
                            buy_multiplier = 0.9997
                            sell_multiplier = 1.0010
                        else:
                            # c'était 10-3
                            buy_multiplier = 0.9990
                            sell_multiplier = 1.0003
                    else:  # Mean Reversion
                        if is_bullish:
                            # c'était 6-6
                            buy_multiplier = 0.9994
                            sell_multiplier = 1.0006
                        else:
                            # c'était 5-5
                            buy_multiplier = 0.9995
                            sell_multiplier = 1.0005

                    # If both buy and sell signals are present at the same time, prioritize the one most likely to occur.
                    if global_buy[latest_idx] and global_sell[latest_idx]:
                        console.print(f"[{symbol}] Both BUY and SELL signals triggered simultaneously. Prioritizing based on probability of occurrence...")
                        # Compute volatility & drift for Monte Carlo from df_candles
                        volatility = 0.0
                        drift = 0.0
                        if df_candles is not None and len(df_candles) > 1:
                            try:
                                closes = pd.to_numeric(df_candles['close'], errors='coerce')
                                returns = np.log(closes / closes.shift(1)).replace([np.inf, -np.inf], np.nan).dropna()
                                if len(returns) > 1:
                                    volatility = float(returns.std())
                                    drift = float(returns.mean())
                            except Exception as ve:
                                console.print(f"[{symbol}] Error computing volatility for Monte Carlo: {ve}")

                        from monte_carlo2 import MonteCarloEngine
                        mc_engine = MonteCarloEngine(num_simulations=1000, timeframe_candles=100)

                        target_buy_price = round(ref_price * buy_multiplier, int(-math.log10(price_precision)))
                        target_sell_price = round(ref_price * sell_multiplier, int(-math.log10(price_precision)))

                        # Estimate hit probability for BUY (mode "below" because limit buy price is below current price)
                        prob_buy = mc_engine.estimate_hit_probability(
                            current_price=last_close,
                            target_price=target_buy_price,
                            volatility=volatility,
                            drift=drift,
                            mode="below"
                        )

                        # Estimate hit probability for SELL (mode "above" because limit sell price is above current price)
                        prob_sell = mc_engine.estimate_hit_probability(
                            current_price=last_close,
                            target_price=target_sell_price,
                            volatility=volatility,
                            drift=drift,
                            mode="above"
                        )

                        console.print(f"[{symbol}] Simultaneous signals: BUY prob = {prob_buy:.4f} (target: {target_buy_price:.8f}), SELL prob = {prob_sell:.4f} (target: {target_sell_price:.8f})")

                        if prob_buy >= prob_sell:
                            global_sell[latest_idx] = False
                            console.print(f"[{symbol}] Prioritizing BUY signal (probability {prob_buy:.4f} >= {prob_sell:.4f}).")
                        else:
                            global_buy[latest_idx] = False
                            console.print(f"[{symbol}] Prioritizing SELL signal (probability {prob_sell:.4f} > {prob_buy:.4f}).")

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
                                    except Exception:
                                        with open(PAUSE_FILE, 'w') as f:
                                            json.dump(pausedForBuy, f)
                                except Exception as e:
                                    console.print(f"Failed to write PAUSE_FILE: {e}")
                                    pass
                            # compute amount to buy: use a fixed package value
                            try:
                                order_book = exchange.fetch_order_book(symbol)
                            except Exception as e:
                                console.print(f"Failed to fetch order book for {symbol}: {e}")
                                order_book = {'asks':[], 'bids':[]}
                            base_price = ref_price if ref_price is not None else (order_book.get('asks')[0][0] if order_book.get('asks') else last_close)
                            price = round ( base_price * buy_multiplier, int(-math.log10( price_precision ) ) )
                            package = round ( price * min_amount, int(-math.log10( price_precision ) ) )
                            # read quote balance robustly
                            _b = _balance.get('free').get(quote)
                            if _b is not None:
                                quote_free = float(_b)
                            else:
                                quote_free = 0
                            if quote_free <= 0:
                                console.print(f"No quote balance available for {symbol} to BUY")
                            else:
                                # calculate desired amount that equals package at the current price
                                if price > 0:
                                    desired_amount = min_amount * 2.0002
                                    max_affordable = quote_free / package / 4.0004 # tier-hardcoded
                                    amount = round ( min(desired_amount, max_affordable) * (10/9), int(-math.log10( amount_precision ) ) )
                                else:
                                    amount = 0
                                # final amount check
                                if amount <= min_amount:
                                    console.print(f"Calculated buy amount ({amount}) is below minimum amount of {min_amount} for {symbol}")
                                else:
                                    try:
                                        cleanup_open_orders(exchange, symbol, price, 'buy', df_candles, last_close)
                                        console.print(f"Placing LIMIT BUY {symbol} amount={amount} price={price}")
                                        order = exchange.create_limit_buy_order(symbol, amount, price)
                                        # persist pending order to file
                                        add_pending_order(order)
                                        console.print(f"BUY order passed: {order}")
                                        # Record purchase to ensure that SELL events can check profitability later
                                        record_purchase(symbol, amount, price)
                                        # update balance
                                        _balance = market_utils.fetch_balance(exchange, console=console)
                                        # plot a small chart with the BUY marker
                                        try:
                                            plt_ascii.clf()
                                            plt_ascii.theme('dark')
                                            plt_ascii.subplots(1, 1)
                                            # prepare data
                                            timestamps = df_candles['timestamp'].astype(int).tolist()
                                            dates = [datetime.fromtimestamp(int(ts)/1000).strftime('%d/%m %H:%M') for ts in timestamps]
                                            opens = df_candles['open'].astype(float).tolist()
                                            highs = df_candles['high'].astype(float).tolist()
                                            lows = df_candles['low'].astype(float).tolist()
                                            closes = df_candles['close'].astype(float).tolist()
                                            volumes = df_candles['volume'].astype(float).tolist()
                                            data = {"Open": opens, "High": highs, "Low": lows, "Close": closes}
                                            x = list(range(len(dates)))
                                            # 1 plot containing both: the candlesticks on top, volume bars below
                                            plt_ascii.title(f"{symbol} - BUY")
                                            plt_ascii.subplot(1, 1)
                                            plt_ascii.candlestick(x, data)
                                            # draw volumes on the same subplot as short vertical lines anchored below the candles
                                            max_volume = max(volumes) if volumes else 1
                                            min_price = min(lows) if lows else 0
                                            max_price = max(highs) if highs else 1
                                            price_range = max_price - min_price if max_price != min_price else max_price
                                            # base position below the lowest low, and a height factor for volumes
                                            base = min_price - price_range * 0.02
                                            height_factor = price_range * 0.64
                                            # draw vertical dots for each volume data
                                            for i, v in enumerate(volumes):
                                                h = (v / max_volume) * height_factor if max_volume else 0
                                                plt_ascii.plot([i, i], [base, base + h], color='yellow')
                                            plt_ascii.scatter([latest_idx], [closes[latest_idx]], marker='x', color='green')
                                            # set x ticks as human-readable dates on bottom plot
                                            step = max(1, len(dates) // 8)
                                            x_ticks = x[::step]
                                            x_labels = [dates[i] for i in x_ticks]
                                            plt_ascii.xticks(x_ticks, x_labels)
                                            plt_ascii.show()
                                        except Exception as e:
                                            console.print(f"Plot failed for BUY {symbol}: {e}")
                                    except Exception as e:
                                        console.print(f"Buy order failed for {symbol}: {e}")
                                        # detect specific errors and pause buys for 2 hours for this symbol
                                        err = str(e).lower()
                                        if ('invalid permissions' in err):
                                            expiry_ts = int(time.time()) + (366 * 24 * 3600)
                                        elif ('insufficient funds' in err) or ('minimum' in err and 'not met' in err) or ('invalid arguments' in err and 'volume' in err) or ('must be greater than minimum' in err):
                                            expiry_ts = int(time.time()) + (4 * 3600)
                                            pausedForBuy[symbol] = expiry_ts
                                            try:
                                                try:
                                                    safe_json.atomic_write_json(PAUSE_FILE, pausedForBuy, backup=True)
                                                except Exception:
                                                    with open(PAUSE_FILE, 'w') as f:
                                                        json.dump(pausedForBuy, f)
                                            except Exception as ex:
                                                console.print(f"Failed to persist pausedForBuy: {ex}")
                                            console.print(f"Paused buys for {symbol} until {datetime.fromtimestamp(expiry_ts)} due to error: {e}")

                    # decide sell
                    if global_sell[latest_idx]:
                        _b = _balance.get('free').get(base)
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
                                console.print(f"Failed to fetch order book for {symbol}: {e}")
                                order_book = {'asks':[], 'bids':[]}
                            base_price = ref_price if ref_price is not None else (order_book.get('bids')[0][0] if order_book.get('bids') else last_close)
                            price = round ( base_price * sell_multiplier, int(-math.log10( price_precision ) ) )
                            # sell everything if symbol paused
                            now_ts = int(time.time())
                            expiry = pausedForBuy.get(symbol)
                            if expiry and now_ts < int(expiry):
                                # sell everything if paused
                                amount = round ( base_free, int(-math.log10( amount_precision ) ) )
                            else: # tier hardcoded
                                amount = round ( base_free * (8 / 9), int(-math.log10( amount_precision ) ) )
                            if amount <= min_amount:
                                console.print(f"Calculated sell amount of {amount} below minimum required of {min_amount} for {symbol}")
                            else:
                                try:
                                    # 2/ Record purchases to ensure that SELL events are ignored if they are unprofitable.
                                    profitable, details_str = is_sell_profitable(symbol, price, amount)
                                    if not profitable:
                                        console.print(f"[{symbol}] Ignoring SELL event because it is unprofitable: {details_str}")
                                    else:
                                        cleanup_open_orders(exchange, symbol, price, 'sell', df_candles, last_close)
                                        console.print(f"Placing LIMIT SELL {symbol} amount={amount} price={price}")
                                        order = exchange.create_limit_sell_order(symbol, amount, price)
                                        # persist pending order to file
                                        add_pending_order(order)
                                        console.print(f"SELL order passed: {order}")
                                        # Deduct sell_amount from recorded purchases
                                        remove_recorded_purchases(symbol, amount)
                                        # update balance
                                        _balance = market_utils.fetch_balance(exchange, console=console)
                                        # plot SELL
                                        try:
                                            plt_ascii.clf()
                                            plt_ascii.theme('dark')
                                            plt_ascii.subplots(1, 1)
                                            # prepare data
                                            timestamps = df_candles['timestamp'].astype(int).tolist()
                                            dates = [datetime.fromtimestamp(int(ts)/1000).strftime('%d/%m %H:%M') for ts in timestamps]
                                            opens = df_candles['open'].astype(float).tolist()
                                            highs = df_candles['high'].astype(float).tolist()
                                            lows = df_candles['low'].astype(float).tolist()
                                            closes = df_candles['close'].astype(float).tolist()
                                            volumes = df_candles['volume'].astype(float).tolist()
                                            data = {"Open": opens, "High": highs, "Low": lows, "Close": closes}
                                            x = list(range(len(dates)))
                                            # 1 plot containing both: the candlesticks on top, volume bars below
                                            plt_ascii.title(f"{symbol} - SELL")
                                            plt_ascii.subplot(1, 1)
                                            plt_ascii.candlestick(x, data)
                                            # draw volumes on the same subplot as short vertical lines anchored below the candles
                                            max_volume = max(volumes) if volumes else 1
                                            min_price = min(lows) if lows else 0
                                            max_price = max(highs) if highs else 1
                                            price_range = max_price - min_price if max_price != min_price else max_price
                                            # base position below the lowest low, and a height factor for volumes
                                            base = min_price - price_range * 0.02
                                            height_factor = price_range * 0.64
                                            # draw vertical dots for each volume data
                                            for i, v in enumerate(volumes):
                                                h = (v / max_volume) * height_factor if max_volume else 0
                                                plt_ascii.plot([i, i], [base, base + h], color='yellow')
                                            plt_ascii.scatter([latest_idx], [closes[latest_idx]], marker='o', color='red')
                                            # set x ticks as human-readable dates on bottom plot
                                            step = max(1, len(dates) // 8)
                                            x_ticks = x[::step]
                                            x_labels = [dates[i] for i in x_ticks]
                                            plt_ascii.xticks(x_ticks, x_labels)
                                            plt_ascii.show()
                                        except Exception as e:
                                            console.print(f"Plot failed for SELL {symbol}: {e}")
                                except Exception as e:
                                    console.print(f"Sell order failed for {symbol}: {e}")

                    time.sleep(1.0)

                # Periodic background tasks: dump pending orders every 30 minutes,
                # check candle coherence, and compute profit for recent SELL orders.
                try:
                    now_ts = time.time()
                    if now_ts - last_pending_fetch >= 30 * 60:
                        _markets = loadMarkets(exchange, "markets.json")
                        # update balance
                        _balance = market_utils.fetch_balance(exchange, console=console)
                        # batch symbole au hasard - choisir correctement quand _markets est un dict
                        for _ in range(15):
                            market_sample = random.choice(list(_markets.values()))
                            symbolChoose = market_sample.get('symbol')
                            console.print(f"Chose to update symbol: {symbolChoose}")
                            _count = symbols_utils.updateTradingCount(symbolChoose, exchange=exchange, console=console)
                            if _count >= miniCount and symbolChoose not in availablePairs:
                                availablePairs.append(symbolChoose)
                                console.print(f"Appended {symbolChoose} to tracked pairs.")
                            elif _count < miniCount and symbolChoose in availablePairs:
                                expiry_ts = int(time.time()) + (4 * 3600)
                                pausedForBuy[symbolChoose] = expiry_ts
                                try:
                                    try:
                                        safe_json.atomic_write_json(PAUSE_FILE, pausedForBuy, backup=True)
                                    except Exception:
                                        with open(PAUSE_FILE, 'w') as f:
                                            json.dump(pausedForBuy, f)
                                except Exception as ex:
                                    console.print(f"Failed to persist pausedForBuy: {ex}")
                                console.print(f"Paused buys for {symbolChoose} until {datetime.fromtimestamp(expiry_ts)} due to trading count below minimum.")
                        last_pending_fetch = now_ts
                        console.print(f"[yellow]Periodic task: fetching open orders at {datetime.fromtimestamp(now_ts)}[/yellow]")
                        recent = []
                        try:
                            # https://docs.ccxt.com/docs/exchanges/kraken#fetchordersbyids
                            recent = exchange.fetchOpenOrders()
                            # console.print(f"DEBUG recent={recent}")
                        except Exception:
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
                                console.print(f"Error sorting recent orders: {e}")
                        # TODO match order ids with the dump file
                except Exception as e:
                    console.print(f"Periodic background task failed: {e}")

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

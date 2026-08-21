# init start
from rich.console import Console
console = Console()

with console.status("Bot init. Please wait some time, or expect a random error if you break.", spinner="dots"):
    import ccxt
    import asyncio
    import logging
    import time
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
    import pandas as pd
    import pandas_ta as ta
    import threading
    import queue
    from collections import deque
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
    forbidAssets = ['AKE', 'ALLO', 'USDT', 'WEMIX', 'XMR']
    previousPairs = []
    availablePairs = []
    maxNumPairs = 100

    # ccxt markets keyword is used
    _markets = []
    _balance = None
    _positions = {}  # key: symbol, value: {'amount': float, 'avg_price': float}

    # periodic pending orders dump / candle consistency check
    last_pending_fetch = 0
    PENDING_DUMP_FILE = 'pending_orders_dump.json'

    miniCount = 400
    # monnaies d'usage pour considérer les paires à leur quote asset
    baseAssets = ["USD", "EUR", "BTC", "CHF", "GBP", "USDC"]

    def get_5_week_sma(exchange, symbol, _id, df_candles_1m, paused_for_buy=None, console=None):
        """
        Calculate the 5-week SMA.
        If df_candles_1m (which contains the full 1m history) has at least 50,400 candles,
        compute SMA from the last 50,400 candles.
        Otherwise, fetch 276 candles of 4-hour timeframe, cache them, and compute SMA from the last 210 candles.
        """
        try:
            # Check if 1-minute candle dataframe has at least 5 weeks of history (50,400 candles)
            if df_candles_1m is not None and len(df_candles_1m) >= 50400:
                return float(df_candles_1m['close'].tail(50400).mean())

            # If not, fetch 276 of 4h candles
            if _id is None:
                _id = symbol.replace('/', '')

            df_4h = market_utils.fetch_ohlcv_data(
                exchange=exchange,
                _id=_id,
                symbol=symbol,
                pausedForBuy=paused_for_buy,
                console=console,
                timeframe='4h',
                limit=276
            )

            if df_4h is not None and not df_4h.empty:
                # 5 weeks in 4-hour candles = 5 * 7 * 6 = 210 candles
                if len(df_4h) >= 210:
                    return float(df_4h['close'].tail(210).mean())
                else:
                    return float(df_4h['close'].mean())
        except Exception as e:
            msg_err = f"[{symbol}] Failed to compute/fetch 5-week SMA via 4h candles: {e}"
            if console:
                console.print(msg_err)
            else:
                print(msg_err)

        # Fallback to whatever 1m history we have
        if df_candles_1m is not None and len(df_candles_1m) > 0:
            return float(df_candles_1m['close'].mean())
        return None

    def scan_market_and_add_pairs_for_quote(quote_asset, exchange, markets, forbid_assets, base_assets, mini_count, available_pairs, console=None, max_uncached_evals=5, volumes_file='volumes_trades_data.json'):
        """
        Scans the market for pairs compatible with quote_asset and adds them to available_pairs
        if they satisfy the standard conditions (e.g. not forbidden, trading count >= mini_count).
        Limits uncached exchange API evaluation calls to max_uncached_evals per scan to avoid blocking trade execution.
        """
        try:
            msg = f"Starting market scan for compatible pairs with quote asset {quote_asset} (Driver)..."
            if console:
                console.print(msg)
            else:
                print(msg)

            added_pairs = []
            market_items = []
            if isinstance(markets, dict):
                market_items = list(markets.values())
            elif isinstance(markets, list):
                market_items = markets

            # Load volumes cache to identify cached vs uncached symbols
            cached_symbols = set()
            now_minus_4h = int(time.time()) - (4 * 3600)
            if os.path.exists(volumes_file):
                try:
                    with open(volumes_file, 'r') as f:
                        _vdata = json.load(f)
                    for _v in _vdata:
                        if isinstance(_v, dict) and _v.get('symbol'):
                            _ts = _v.get('timestamp')
                            if _ts is not None and int(_ts) > now_minus_4h:
                                cached_symbols.add(_v.get('symbol'))
                except Exception:
                    pass

            uncached_eval_count = 0

            for m in market_items:
                if not isinstance(m, dict):
                    continue
                m_symbol = m.get('symbol')
                m_id = m.get('id')
                m_base = m.get('base')
                m_quote = m.get('quote')

                if not m_symbol or not m_base or not m_quote:
                    continue

                # Standard conditions
                if m_quote == quote_asset:
                    if m_base not in forbid_assets and m_quote not in forbid_assets:
                        # Check if already in availablePairs
                        already_exists = False
                        for p in available_pairs:
                            if isinstance(p, (list, tuple)) and len(p) > 0 and p[0] == m_symbol:
                                already_exists = True
                                break
                            elif isinstance(p, str) and p == m_symbol:
                                already_exists = True
                                break
                        if already_exists:
                            continue

                        # If pair is uncached and we hit max_uncached_evals limit, skip to avoid blocking execution
                        is_cached = m_symbol in cached_symbols
                        if not is_cached and uncached_eval_count >= max_uncached_evals:
                            continue

                        if not is_cached:
                            uncached_eval_count += 1

                        is_opt = False
                        try:
                            is_opt, _ = symbols_utils.get_only_optimal(exchange, m_symbol, volumes_file=volumes_file)
                        except Exception as ex_opt:
                            if console:
                                console.print(f"[Driver] Error evaluating optimal for {m_symbol}: {ex_opt}")
                            is_opt = False

                        if is_opt:
                            min_amount = m.get('limits', {}).get('amount', {}).get('min')
                            price_precision = m.get('precision', {}).get('price')
                            amount_precision = m.get('precision', {}).get('amount')
                            pair_info = [m_symbol, m_id, m_base, m_quote, min_amount, price_precision, amount_precision]
                            available_pairs.append(pair_info)
                            added_pairs.append(m_symbol)

            if added_pairs:
                msg = f"[Driver] Added {len(added_pairs)} compatible pairs with quote {quote_asset} to tracked pairs: {', '.join(added_pairs)}"
                if console:
                    console.print(msg)
                else:
                    print(msg)
            else:
                msg = f"[Driver] No new compatible pairs found for quote {quote_asset}."
                if console:
                    console.print(msg)
                else:
                    print(msg)
        except Exception as e:
            msg_err = f"[Driver] Exception during market scan: {e}"
            if console:
                console.print(msg_err)
            else:
                print(msg_err)

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

    def cleanup_open_orders(exchange, symbol, new_price, side, df_candles, last_close, new_amount=None, _id=None): # TODO rename?
        """Fetches open orders for the symbol and either edits or cancels them.
        Returns the edited order dict if edit_order is used successfully, otherwise returns None.
        """
        try:
            console.print(f"[{symbol}] Running open orders check before placing/editing order (side {side}) at price {new_price}...")
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
                    return None

            if not open_orders:
                console.print(f"[{symbol}] No open orders found.")
                return None

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
            mc_engine = MonteCarloEngine(num_simulations=1000, timeframe_candles=240)

            # Get the probability threshold from config
            threshold = 0.96
            if config and isinstance(config, dict):
                threshold = config.get('monte_carlo', {}).get('sufficient_probability', 0.96)

            # Calculate 5-week SMA (SMA_840) to check for crest high on BUY orders
            sma_840 = get_5_week_sma(exchange, symbol, _id, df_candles, pausedForBuy, console)

            edited_order = None

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

                # Cancel BUY order if we are on a crest high (current close or target/order price > sma_840)
                if o_side_lower == 'buy' and sma_840 is not None and (last_close > sma_840 or o_price_float > sma_840):
                    console.print(f"[{symbol}] Cancelling open BUY order {oid}: Price is on a crest high (last close {last_close:.8f}, order price {o_price_float:.8f}) and 5-week SMA {sma_840:.8f} is not reached.")
                    try:
                        time.sleep(exchange.rateLimit / 1000)
                        try:
                            exchange.cancel_order(oid, symbol)
                        except TypeError:
                            exchange.cancel_order(oid)
                        console.print(f"[{symbol}] Order {oid} successfully cancelled.")
                    except Exception as e:
                        console.print(f"[{symbol}] Failed to cancel order {oid}: {e}")
                    continue

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

                console.print(f"[{symbol}] Found previous open order {oid} ({o_side} at {o_price}). Execution probability: {prob:.4f} (threshold: {threshold})")

                # Check if we can edit this order
                side_changed = (o_side_lower != side_lower)
                has_edit_order = False
                if hasattr(exchange, 'has') and isinstance(exchange.has, dict):
                    has_edit_order = exchange.has.get('editOrder', False)

                if not side_changed and has_edit_order:
                    edit_amount = new_amount if (new_amount is not None and new_amount > 0) else float(o.get('amount', 0))
                    old_amount = float(o.get('amount', 0))
                    price_changed = (abs(new_price - o_price_float) > 1e-9)
                    amount_changed = (abs(edit_amount - old_amount) > 1e-9)

                    if edit_amount > 0:
                        if not price_changed and not amount_changed:
                            console.print(f"[{symbol}] Existing order {oid} is already at price={new_price} and amount={edit_amount}. No edit needed.")
                            edited_order = o
                            break
                        else:
                            try:
                                console.print(f"[{symbol}] Attempting to edit existing order {oid} (price change: {price_changed}, amount change: {amount_changed}) to price={new_price} amount={edit_amount}...")
                                time.sleep(exchange.rateLimit / 1000)
                                res = exchange.edit_order(oid, symbol, 'limit', side_lower, edit_amount, new_price)
                                console.print(f"[{symbol}] Order {oid} successfully edited: {res}")
                                if side_lower == 'buy':
                                    remove_edited_buy_order_purchase(symbol, old_amount, o_price_float)
                                edited_order = res
                                break  # Only edit one order (it must be for the second order only that we launch this procedure)
                            except Exception as e:
                                console.print(f"[{symbol}] edit_order failed for {oid}: {e}. Falling back to cancel and replace.")

                # If the probability is insufficient, cancel it
                if insufficient_prob:
                    console.print(f"[{symbol}] Cancelling order {oid}: Execution probability ({prob:.4f}) is no longer sufficient (< {threshold})")
                    try:
                        time.sleep(exchange.rateLimit / 1000)
                        try:
                            exchange.cancel_order(oid, symbol)
                        except TypeError:
                            exchange.cancel_order(oid)
                        console.print(f"[{symbol}] Order {oid} successfully cancelled.")
                    except Exception as e:
                        console.print(f"[{symbol}] Failed to cancel order {oid}: {e}")

            return edited_order

        except Exception as e:
            console.print(f"[{symbol}] Exception in cleanup_open_orders: {e}")
            return None


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
        """Check if selling sell_amount at sell_price is profitable against recorded purchases across all pairs sharing the same base asset.
        If there are no recorded purchases, we default to True to allow the sell.
        Returns (is_profitable, details_str).
        """
        try:
            base_asset = symbol.split('/')[0] if '/' in symbol else symbol
            current_quote = symbol.split('/')[1] if '/' in symbol else ''

            # Aggregate all recorded purchases for any pair sharing the same base asset
            all_purchases = []
            for s, purchases in recorded_purchases.items():
                s_base = s.split('/')[0] if '/' in s else s
                if s_base == base_asset:
                    s_quote = s.split('/')[1] if '/' in s else ''
                    for p in purchases:
                        all_purchases.append((s_quote, p))

            if not all_purchases:
                return True, f"No recorded purchases found for base asset {base_asset}, allowing sell by default."

            # Calculate the weighted average price of our remaining recorded purchases, converting each to current_quote
            total_amount = 0.0
            weighted_sum = 0.0

            for p_quote, p in all_purchases:
                amount = float(p['amount'])
                price_in_p_quote = float(p['price'])

                # Convert price from p_quote to current_quote
                conversion_rate = 1.0
                if p_quote != current_quote and p_quote and current_quote:
                    symbol1 = f"{p_quote}/{current_quote}"
                    symbol2 = f"{current_quote}/{p_quote}"
                    rate_found = False
                    if isinstance(_markets, dict):
                        if symbol1 in _markets:
                            try:
                                ticker = exchange.fetch_ticker(symbol1)
                                conversion_rate = float(ticker.get('close') or ticker.get('last') or 0.0)
                                rate_found = True
                            except Exception:
                                pass
                        if not rate_found and symbol2 in _markets:
                            try:
                                ticker = exchange.fetch_ticker(symbol2)
                                ticker_price = float(ticker.get('close') or ticker.get('last') or 0.0)
                                if ticker_price > 0:
                                    conversion_rate = 1.0 / ticker_price
                                    rate_found = True
                            except Exception:
                                pass
                    if not rate_found:
                        # Fallbacks
                        if p_quote == 'EUR' and current_quote == 'USD':
                            conversion_rate = 1.13
                        elif p_quote == 'USD' and current_quote == 'EUR':
                            conversion_rate = 1.0 / 1.13
                        elif p_quote == 'BTC' and current_quote == 'USD':
                            conversion_rate = 64000.0
                        elif p_quote == 'USD' and current_quote == 'BTC':
                            conversion_rate = 1.0 / 64000.0
                        elif p_quote == 'BTC' and current_quote == 'EUR':
                            conversion_rate = 56000.0
                        elif p_quote == 'EUR' and current_quote == 'BTC':
                            conversion_rate = 1.0 / 56000.0

                converted_price = price_in_p_quote * conversion_rate
                total_amount += amount
                weighted_sum += amount * converted_price

            if total_amount <= 0:
                return True, "No remaining recorded purchase amount, allowing sell by default."

            avg_purchase_price = weighted_sum / total_amount

            # A sell is profitable if sell_price > avg_purchase_price with a 0.3% margin added (sell_price > avg_purchase_price * 1.003)
            target_price = avg_purchase_price * 1.003
            profitable = float(sell_price) > target_price
            details = f"Sell Price: {sell_price:.8f} vs Avg Purchase Price (converted to {current_quote}) with 0.3% margin: {target_price:.8f} (Raw Avg: {avg_purchase_price:.8f}, Remaining Amount: {total_amount:.6f})"
            return profitable, details
        except Exception as e:
            console.print(f"[{symbol}] Exception in is_sell_profitable check: {e}")
            return True, f"Error in profitability check: {e}. Allowing sell by default."

    def remove_recorded_purchases(symbol, sell_amount):
        """Delete all entries of buys from recorded purchases for the given base asset after a successful SELL."""
        try:
            base_asset = symbol.split('/')[0] if '/' in symbol else symbol
            wiped_symbols = []
            for s in list(recorded_purchases.keys()):
                s_base = s.split('/')[0] if '/' in s else s
                if s_base == base_asset:
                    recorded_purchases[s] = []
                    wiped_symbols.append(s)
            try:
                safe_json.atomic_write_json(PURCHASES_FILE, recorded_purchases, backup=True, indent=2)
            except Exception:
                with open(PURCHASES_FILE, 'w') as f:
                    json.dump(recorded_purchases, f, indent=2)
            console.print(f"[{symbol}] Deleted all buy entries from recorded purchases for base asset {base_asset} ({', '.join(wiped_symbols)}) because a sale has been made.")
        except Exception as e:
            console.print(f"[{symbol}] Failed to update recorded purchases after sell: {e}")

    def remove_edited_buy_order_purchase(symbol, previous_amount, previous_price):
        """Remove a recorded purchase matching the previous buy order's amount and price after it was edited."""
        try:
            purchases = recorded_purchases.get(symbol, [])
            if not purchases:
                console.print(f"[{symbol}] Warning: No recorded purchases found to remove for edited buy order.")
                return

            found = False
            for i, p in enumerate(purchases):
                if float(p['amount']) == float(previous_amount) and float(p['price']) == float(previous_price):
                    purchases.pop(i)
                    found = True
                    break

            if found:
                recorded_purchases[symbol] = purchases
                try:
                    safe_json.atomic_write_json(PURCHASES_FILE, recorded_purchases, backup=True, indent=2)
                except Exception:
                    with open(PURCHASES_FILE, 'w') as f:
                        json.dump(recorded_purchases, f, indent=2)
                console.print(f"[{symbol}] Deleted previous buy order purchase from recorded purchases: price={previous_price}, amount={previous_amount}")
            else:
                console.print(f"[{symbol}] Warning: No matching recorded purchase found for edited buy order with price={previous_price}, amount={previous_amount}")
        except Exception as e:
            console.print(f"[{symbol}] Failed to update recorded purchases after buy order edit: {e}")

    def count_buyings_for_base_asset(base_asset):
        """Count the number of recorded purchase entries across all symbols sharing the given base asset."""
        count = 0
        try:
            for s, purchases in recorded_purchases.items():
                if '/' in s:
                    s_base = s.split('/')[0]
                    if s_base == base_asset:
                        count += len(purchases)
        except Exception as e:
            console.print(f"Failed to count buyings for base asset {base_asset}: {e}")
        return count

    REDLIST_FILE = 'redlisted_pairs.json'

    def load_redlist():
        if os.path.exists(REDLIST_FILE):
            try:
                with open(REDLIST_FILE, 'r') as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save_redlist(redlist):
        try:
            safe_json.atomic_write_json(REDLIST_FILE, redlist, backup=True, indent=2)
        except Exception:
            with open(REDLIST_FILE, 'w') as f:
                json.dump(redlist, f, indent=2)

    def get_eur_conversion_rate(exchange, quote, markets):
        if quote == 'EUR':
            return 1.0
        symbol1 = f"{quote}/EUR"
        symbol2 = f"EUR/{quote}"
        if isinstance(markets, dict):
            if symbol1 in markets:
                try:
                    ticker = exchange.fetch_ticker(symbol1)
                    return float(ticker.get('close') or ticker.get('last') or 0.0)
                except Exception:
                    pass
            if symbol2 in markets:
                try:
                    ticker = exchange.fetch_ticker(symbol2)
                    ticker_price = float(ticker.get('close') or ticker.get('last') or 0.0)
                    if ticker_price > 0:
                        return 1.0 / ticker_price
                except Exception:
                    pass
        # Fallback values
        if quote == 'USD':
            return 1.0 / 1.13
        elif quote == 'BTC':
            return 56000.0
        elif quote == 'CHF':
            return 1.0
        elif quote == 'GBP':
            return 1.2
        elif quote == 'USDC':
            return 1.0 / 1.13
        return 1.0

    def should_place_order(symbol, side, price, last_close, df_candles, console=None):
        """
        Estimates the hit probability of an order prior to placing it.
        Returns (should_place, prob) where should_place is True if prob > 0.96, and False otherwise.
        """
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
                if console:
                    console.print(f"[{symbol}] Error computing volatility for Monte Carlo: {ve}")

        from monte_carlo2 import MonteCarloEngine
        mc_engine = MonteCarloEngine(num_simulations=1000, timeframe_candles=480)
        mode = "below" if side.lower() == "buy" else "above"
        prob = mc_engine.estimate_hit_probability(
            current_price=last_close,
            target_price=price,
            volatility=volatility,
            drift=drift,
            mode=mode
        )
        return (prob > 0.96), prob

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

    def calibrate_window_by_non_repetition(df_candles, target_active=480, epsilon=1e-5):
        """
        Calibre automatiquement la taille de la fenêtre temporelle pour s'assurer
        que l'on dispose de target_active chandelles non-répétitives (c'est-à-dire
        non quasi-identiques à leur prédécesseur).
        """
        if df_candles is None or df_candles.empty:
            return 0
        N = len(df_candles)
        if N <= 1:
            return N

        active_count = 0
        scanned_count = 0

        # Parcourir à l'envers depuis la dernière bougie
        for i in range(N - 1, 0, -1):
            scanned_count += 1
            c1 = df_candles.iloc[i]
            c2 = df_candles.iloc[i - 1]

            is_rep = False
            try:
                p1, p2 = float(c1['close']), float(c2['close'])
                o1, o2 = float(c1['open']), float(c2['open'])
                h1, h2 = float(c1['high']), float(c2['high'])
                l1, l2 = float(c1['low']), float(c2['low'])

                diff_c = abs(p1 - p2) / max(p1, p2, 1e-9)
                diff_o = abs(o1 - o2) / max(o1, o2, 1e-9)
                diff_h = abs(h1 - h2) / max(h1, h2, 1e-9)
                diff_l = abs(l1 - l2) / max(l1, l2, 1e-9)

                if diff_c <= epsilon and diff_o <= epsilon and diff_h <= epsilon and diff_l <= epsilon:
                    is_rep = True
            except Exception:
                pass

            if not is_rep:
                active_count += 1

            if active_count >= target_active:
                break

        # La taille de la fenêtre correspond à scanned_count + 1 (pour inclure la bougie i-1 de la dernière comparaison)
        window_size = scanned_count + 1
        window_size = min(window_size, N)
        window_size = max(window_size, target_active)
        return window_size

# boucle principale du bot
if __name__ == '__main__':
    # Check for temporary (.tmp) files indicating an interrupted write
    import os
    import sys
    tmp_files = [f for f in os.listdir('.') if f.endswith('.tmp')]
    if tmp_files:
        console.print(f"[bold red]Error: Temporary files found: {', '.join(tmp_files)}. This indicates an interrupted write operation, which means a backup (.bak) file is better. Please restore the backup and remove the tmp files before restarting. Exiting.[/bold red]")
        sys.exit(1)

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
                    console=console,
                    exchange=exchange
                )
                redlist = load_redlist()
                # durant la maintenance ou ailleurs, ne pas éliminer de paire dont l'asset base se trouve en balance et > min_amount.
                filtered_pairs = []
                for p in availablePairs:
                    symbol = p[0]
                    base = p[2]
                    try:
                        min_amt = float(p[4]) if p[4] is not None else 0.0
                    except (ValueError, TypeError):
                        min_amt = 0.0

                    base_balance = 0.0
                    if _balance and isinstance(_balance, dict):
                        free_bal = _balance.get('free') or {}
                        total_bal = _balance.get('total') or {}
                        try:
                            base_balance = float(free_bal.get(base, 0.0) or total_bal.get(base, 0.0) or 0.0)
                        except (ValueError, TypeError):
                            base_balance = 0.0

                    if symbol in redlist and not (base_balance > min_amt):
                        continue
                    filtered_pairs.append(p)
                availablePairs = filtered_pairs
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
                    full_df_candles = None
                    calibrated_size = 0
                    if exchange.has.get('fetchOHLCV'):
                        try:
                            full_df_candles = market_utils.fetch_ohlcv_data(
                                exchange=exchange,
                                _id=_id,
                                symbol=symbol,
                                pausedForBuy=pausedForBuy,
                                PAUSE_FILE=PAUSE_FILE,
                                console=console
                            )
                            try:
                                # vérifier la cohérence des chandelles immédiatement après le fetch
                                market_utils.check_candles_consistency(symbol, console=console)
                            except Exception as e:
                                console.print(f"check_candles_consistency failed for {symbol}: {e}")
                            # Calibrer automatiquement le temps sur la non-répétition de chandelles
                            calibrated_size = calibrate_window_by_non_repetition(full_df_candles, target_active=480)
                            # console.print(f"[{symbol}] Dynamically calibrated window size to {calibrated_size} based on non-repetition of quasi-identical candles")
                            candles_per_pair[symbol] = full_df_candles.tail(calibrated_size) if full_df_candles is not None else None
                        except Exception as e:
                            console.print(f"Failed to fetch OHLCV for {symbol}: {e}")
                    df_candles = candles_per_pair.get(symbol)
                    # check
                    if df_candles is None or df_candles.empty:
                        continue

                    # Check redlisting condition based on last_close
                    last_close = float(df_candles.iloc[-1]['close']) if df_candles is not None and not df_candles.empty else 0.0
                    if last_close > 0:
                        rate = get_eur_conversion_rate(exchange, quote, _markets)
                        market_min_expense_eur = float(min_amount) * last_close * rate
                        if market_min_expense_eur > 12.23:
                            # durant la maintenance ou ailleurs, ne pas éliminer de paire dont l'asset base se trouve en balance et > min_amount.
                            base_balance = 0.0
                            if _balance and isinstance(_balance, dict):
                                free_bal = _balance.get('free') or {}
                                total_bal = _balance.get('total') or {}
                                try:
                                    base_balance = float(free_bal.get(base, 0.0) or total_bal.get(base, 0.0) or 0.0)
                                except (ValueError, TypeError):
                                    base_balance = 0.0

                            try:
                                min_amt_float = float(min_amount) if min_amount is not None else 0.0
                            except (ValueError, TypeError):
                                min_amt_float = 0.0

                            if base_balance > min_amt_float:
                                console.print(f"[{symbol}] Retaining pair despite high EUR cost ({market_min_expense_eur:.2f} EUR) because base balance is > min_amount ({base_balance} > {min_amount}).")
                            else:
                                console.print(f"[{symbol}] Redlisting pair: market minimum transaction cost ({market_min_expense_eur:.2f} EUR) exceeds 12.23 EUR (min_amount: {min_amount}, last_close: {last_close}, rate: {rate}).")
                                redlist = load_redlist()
                                redlist[symbol] = {
                                    "symbol": symbol,
                                    "min_amount": float(min_amount),
                                    "last_close": last_close
                                }
                                save_redlist(redlist)
                                availablePairs = [p for p in availablePairs if p[0] != symbol]
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
                    sma_840 = get_5_week_sma(exchange, symbol, _id, full_df_candles, pausedForBuy, console)
                    is_bullish = last_close > sma_840 if sma_840 is not None else True
                    trend_str = 'Bullish' if is_bullish else 'Bearish'
                    console.print(f"[{symbol}] Tendance détectée: {trend_str} (Last Close: {last_close:.8f} vs SMA 840: {(sma_840 if sma_840 is not None else 0.0):.8f})")

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
                    # Base offsets:
                    base_buy_offset = 0.0006
                    base_sell_offset = 0.0006

                    if regime_str == 'Trend Following':
                        if is_bullish:
                            base_buy_offset = 0.0003
                            base_sell_offset = 0.0010
                        else:
                            base_buy_offset = 0.0010
                            base_sell_offset = 0.0003
                    else:  # Mean Reversion
                        if is_bullish:
                            base_buy_offset = 0.0006
                            base_sell_offset = 0.0006
                        else:
                            base_buy_offset = 0.0005
                            base_sell_offset = 0.0005

                    from monte_carlo2 import MonteCarloEngine
                    mc_engine_m = MonteCarloEngine(num_simulations=1000, timeframe_candles=240)
                    mc_score = mc_engine_m.validate_strategy(df_candles)

                    buy_offset = base_buy_offset * 2 * mc_score
                    sell_offset = base_sell_offset * 2 * mc_score

                    buy_multiplier = 1.0 - buy_offset
                    sell_multiplier = 1.0 + sell_offset

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
                        mc_engine = MonteCarloEngine(num_simulations=1000, timeframe_candles=240)

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
                        elif len(df_candles) < calibrated_size:
                            console.print(f"[{symbol}] Skipping BUY order: need at least {calibrated_size} candles of 1 minute (has {0 if df_candles is None else len(df_candles)})")
                        elif count_buyings_for_base_asset(base) >= 4:
                            console.print(f"[{symbol}] Skipping BUY order: Already reached the limit of 4 buyings for base asset {base}.")
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
                            if True:
                                # calculate desired amount that equals package at the current price
                                if price > 0:
                                    rate = get_eur_conversion_rate(exchange, quote, _markets)
                                    min_amount_to_use = max(min_amount, 5.07 / (price * rate))
                                    max_amount_limit = 12.23 / (price * rate)
                                    desired_amount = min_amount_to_use * 1.1
                                    if desired_amount > max_amount_limit:
                                        desired_amount = max_amount_limit

                                    decimals = int(-math.log10(amount_precision))
                                    amount = math.floor(desired_amount * (10 ** decimals)) / (10 ** decimals)
                                    if amount < min_amount_to_use:
                                        amount = math.ceil(min_amount_to_use * (10 ** decimals)) / (10 ** decimals)
                                    if amount > max_amount_limit:
                                        amount = math.floor(max_amount_limit * (10 ** decimals)) / (10 ** decimals)
                                else:
                                    amount = 0
                                    min_amount_to_use = min_amount
                                # final amount check
                                if amount < min_amount_to_use:
                                    console.print(f"Calculated buy amount ({amount}) is below minimum amount of {min_amount_to_use} for {symbol}")
                                else:
                                    # Wind-choice check for first BUY signal of base asset
                                    pass_on_buy = False
                                    if count_buyings_for_base_asset(base) == 0:
                                        other_pairs = [p for p in availablePairs if p[2] == base and p[3] != quote]
                                        if other_pairs:
                                            quote_free = float(_balance.get('free', {}).get(quote, 0.0)) if _balance else 0.0
                                            for p in other_pairs:
                                                p_symbol = p[0]
                                                p_quote = p[3]
                                                other_quote_free = float(_balance.get('free', {}).get(p_quote, 0.0)) if _balance else 0.0

                                                conversion_rate = 1.0
                                                if p_quote != quote:
                                                    symbol1 = f"{p_quote}/{quote}"
                                                    symbol2 = f"{quote}/{p_quote}"
                                                    rate_found = False
                                                    if isinstance(_markets, dict):
                                                        if symbol1 in _markets:
                                                            try:
                                                                ticker = exchange.fetch_ticker(symbol1)
                                                                conversion_rate = float(ticker.get('close') or ticker.get('last') or 0.0)
                                                                rate_found = True
                                                            except Exception:
                                                                pass
                                                        if not rate_found and symbol2 in _markets:
                                                            try:
                                                                ticker = exchange.fetch_ticker(symbol2)
                                                                ticker_price = float(ticker.get('close') or ticker.get('last') or 0.0)
                                                                if ticker_price > 0:
                                                                    conversion_rate = 1.0 / ticker_price
                                                                    rate_found = True
                                                            except Exception:
                                                                pass
                                                    if not rate_found:
                                                        if p_quote == 'EUR' and quote == 'USD':
                                                            conversion_rate = 1.13
                                                        elif p_quote == 'USD' and quote == 'EUR':
                                                            conversion_rate = 1.0 / 1.13
                                                        elif p_quote == 'BTC' and quote == 'USD':
                                                            conversion_rate = 64000.0
                                                        elif p_quote == 'USD' and quote == 'BTC':
                                                            conversion_rate = 1.0 / 64000.0
                                                        elif p_quote == 'BTC' and quote == 'EUR':
                                                            conversion_rate = 56000.0
                                                        elif p_quote == 'EUR' and quote == 'BTC':
                                                            conversion_rate = 1.0 / 56000.0

                                                other_quote_free_converted = other_quote_free * conversion_rate
                                                if other_quote_free_converted > quote_free:
                                                    console.print(f"[{symbol}] Wind-choice: Passing on buy because {p_symbol} has more available money ({other_quote_free_converted:.2f} {quote} equivalent vs {quote_free:.2f} {quote}).")
                                                    pass_on_buy = True
                                                    break

                                    if pass_on_buy:
                                        pass
                                    else:
                                        try:
                                            # Check if we are on a crest high where the 5-week average (SMA_840) is not reached
                                            if sma_840 is not None and (last_close > sma_840 or price > sma_840):
                                                console.print(f"[{symbol}] Skipping/Cancelling BUY order: Price is on a crest high (last close {last_close:.8f}, limit price {price:.8f}) and 5-week SMA {(sma_840 if sma_840 is not None else 0.0):.8f} is not reached.")
                                                # Call cleanup_open_orders to cancel any existing open buy orders
                                                cleanup_open_orders(exchange, symbol, price, 'buy', full_df_candles, last_close, amount, _id=_id)
                                            else:
                                                should_place, prob = should_place_order(symbol, 'buy', price, last_close, df_candles, console)
                                                if not should_place:
                                                    console.print(f"[{symbol}] Skipping/Cancelling BUY order: Estimated hit probability ({prob:.4f}) is not > 0.96")
                                                else:
                                                    edited_order = cleanup_open_orders(exchange, symbol, price, 'buy', full_df_candles, last_close, amount, _id=_id)
                                                    if edited_order:
                                                        order = edited_order
                                                        add_pending_order(order)
                                                        console.print(f"BUY order successfully updated via edit_order: {order}")
                                                    else:
                                                        # We need to place a NEW limit buy order.
                                                        # Fetch fresh balance and check if we have enough quote balance.
                                                        _balance = market_utils.fetch_balance(exchange, console=console)
                                                        _b = _balance.get('free').get(quote)
                                                        quote_free = float(_b) if _b is not None else 0.0
                                                        if quote_free <= 0:
                                                            console.print(f"No quote balance available for {symbol} to BUY ({quote_free} <= 0)")
                                                            # Raise exception to skip other steps and enter the outer try-except handler
                                                            raise ValueError(f"Insufficient quote balance ({quote_free} <= 0)")
                                                        else:
                                                            console.print(f"Placing LIMIT BUY {symbol} amount={amount} price={price}")
                                                            order = exchange.create_limit_buy_order(symbol, amount, price)
                                                            # persist pending order to file
                                                            add_pending_order(order)
                                                            console.print(f"BUY order passed: {order}")
                                                    # Record purchase to ensure that SELL events can check profitability later
                                                    record_purchase(symbol, amount, price)
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
                                                        plt_ascii.scatter([latest_idx], [closes[latest_idx]], marker='o', color='red')
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
                                            # detect specific errors and pause buys for this symbol
                                            err = str(e).lower()
                                            expiry_ts = None
                                            if ('invalid permissions' in err):
                                                expiry_ts = int(time.time()) + (366 * 24 * 3600)
                                            elif ('invalid arguments' in err):
                                                expiry_ts = int(time.time()) + (7 * 24 * 3600)
                                            elif ('insufficient funds' in err) or ('minimum' in err and 'not met' in err) or ('must be greater than minimum' in err):
                                                expiry_ts = int(time.time()) + (4 * 3600)

                                            if expiry_ts is not None:
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
                        if True:
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
                            decimals = int(-math.log10(amount_precision))

                            # calculate min/max amount for sell based on transaction cost
                            rate = get_eur_conversion_rate(exchange, quote, _markets)
                            min_amount_to_use = max(min_amount, 5.07 / (price * rate)) if price > 0 else min_amount
                            max_amount_limit = 12.23 / (price * rate) if price > 0 else 0.0

                            _b = _balance.get('free').get(base)
                            base_free = float(_b) if _b is not None else 0.0
                            desired_amount = min(base_free, max_amount_limit)
                            amount = math.floor(desired_amount * (10 ** decimals)) / (10 ** decimals)

                            if True:
                                try:
                                    # 2/ Record purchases to ensure that SELL events are ignored if they are unprofitable.
                                    profitable, details_str = is_sell_profitable(symbol, price, amount)
                                    if not profitable:
                                        console.print(f"[{symbol}] Ignoring SELL event because it is unprofitable: {details_str}")
                                    else:
                                        should_place, prob = should_place_order(symbol, 'sell', price, last_close, df_candles, console)
                                        if not should_place:
                                            console.print(f"[{symbol}] Skipping/Cancelling SELL order: Estimated hit probability ({prob:.4f}) is not > 0.96")
                                        else:
                                            edited_order = cleanup_open_orders(exchange, symbol, price, 'sell', full_df_candles, last_close, amount, _id=_id)
                                            if edited_order:
                                                order = edited_order
                                                add_pending_order(order)
                                                console.print(f"SELL order successfully updated via edit_order: {order}")
                                                # Call market scan driver for quote asset
                                                scan_market_and_add_pairs_for_quote(quote, exchange, _markets, forbidAssets, baseAssets, miniCount, availablePairs, console)
                                            else:
                                                # We need to place a NEW limit sell order.
                                                # Fetch fresh balance and check if we have enough base balance.
                                                _balance = market_utils.fetch_balance(exchange, console=console)
                                                _b = _balance.get('free').get(base)
                                                base_free = float(_b) if _b is not None else 0.0
                                                desired_amount = min(base_free, max_amount_limit)
                                                amount = math.floor(desired_amount * (10 ** decimals)) / (10 ** decimals)

                                                if amount < min_amount_to_use:
                                                    console.print(f"Calculated sell amount of {amount} is below minimum required of {min_amount_to_use} for {symbol}. Cannot place new SELL order.")
                                                    raise ValueError(f"Insufficient base balance ({amount} <= {min_amount_to_use})")
                                                else:
                                                    console.print(f"Placing LIMIT SELL {symbol} amount={amount} price={price}")
                                                    order = exchange.create_limit_sell_order(symbol, amount, price)
                                                    # persist pending order to file
                                                    add_pending_order(order)
                                                    console.print(f"SELL order passed: {order}")
                                                    # Call market scan driver for quote asset
                                                    scan_market_and_add_pairs_for_quote(quote, exchange, _markets, forbidAssets, baseAssets, miniCount, availablePairs, console)
                                            # Deduct sell_amount from recorded purchases
                                            remove_recorded_purchases(symbol, amount)
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
                                                plt_ascii.scatter([latest_idx], [closes[latest_idx]], marker='x', color='green')
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

                # Periodic background tasks: dump pending orders every 42 minutes,
                # check candle coherence, and compute profit for recent SELL orders.
                try:
                    now_ts = time.time()
                    if now_ts - last_pending_fetch >= 42 * 60:
                        _markets = loadMarkets(exchange, "markets.json")
                        # update balance
                        _balance = market_utils.fetch_balance(exchange, console=console)

                        # Re-evaluate redlisted pairs if market min_amount was adjusted
                        redlist = load_redlist()
                        if redlist:
                            console.print("[yellow]Checking redlisted pairs during 42-minute maintenance batch...[/yellow]")
                            for symbol, info in list(redlist.items()):
                                market_sample = None
                                if isinstance(_markets, dict) and symbol in _markets:
                                    market_sample = _markets[symbol]

                                if market_sample:
                                    new_min_amount = market_sample.get('limits', {}).get('amount', {}).get('min')
                                    # Fetch ticker to get the latest close price
                                    try:
                                        time.sleep(exchange.rateLimit / 1000)
                                        ticker = exchange.fetch_ticker(symbol)
                                        latest_close = float(ticker.get('close') or ticker.get('last') or 0.0)
                                    except Exception as e:
                                        console.print(f"Failed to fetch ticker for redlisted {symbol} during maintenance: {e}")
                                        latest_close = info.get('last_close', 0.0)

                                    if latest_close > 0 and new_min_amount is not None:
                                        quote = market_sample.get('quote')
                                        rate = get_eur_conversion_rate(exchange, quote, _markets)
                                        market_min_expense_eur = float(new_min_amount) * latest_close * rate
                                        if market_min_expense_eur <= 12.23:
                                            console.print(f"[green]Pair {symbol} fits back! Removing from redlist.[/green]")
                                            redlist.pop(symbol, None)
                                            save_redlist(redlist)

                                            # Consider back the pair: add to availablePairs if count is high enough
                                            try:
                                                is_opt, _ = symbols_utils.get_only_optimal(exchange, symbol)
                                            except Exception:
                                                is_opt = False
                                            if is_opt:
                                                _a = [
                                                    market_sample.get('symbol'),
                                                    market_sample.get('id'),
                                                    market_sample.get('base'),
                                                    market_sample.get('quote'),
                                                    market_sample.get('limits', {}).get('amount', {}).get('min'),
                                                    market_sample.get('precision', {}).get('price'),
                                                    market_sample.get('precision', {}).get('amount')
                                                ]
                                                if not any(p[0] == symbol for p in availablePairs):
                                                    availablePairs.append(_a)
                                                    console.print(f"Added {symbol} back to availablePairs.")

                        # batch symbole au hasard - choisir correctement quand _markets est un dict
                        for _ in range(36):
                            market_sample = random.choice(list(_markets.values()))
                            symbolChoose = market_sample.get('symbol')
                            base_asset = market_sample.get('base')
                            quote_asset = market_sample.get('quote')
                            if symbolChoose in forbidAssets or base_asset in forbidAssets or quote_asset in forbidAssets:
                                console.print(f"Skipping forbidden symbol: {symbolChoose}")
                                continue
                            console.print(f"Chose to update symbol: {symbolChoose}")
                            try:
                                is_opt, _ = symbols_utils.get_only_optimal(exchange, symbolChoose)
                            except Exception:
                                is_opt = False
                            # check if symbolChoose is in availablePairs (as a list/tuple or string)
                            pair_index = -1
                            for idx, pair in enumerate(availablePairs):
                                if isinstance(pair, (list, tuple)) and len(pair) > 0 and pair[0] == symbolChoose:
                                    pair_index = idx
                                    break
                                elif isinstance(pair, str) and pair == symbolChoose:
                                    pair_index = idx
                                    break

                            if is_opt and pair_index == -1:
                                _a = [
                                    market_sample.get('symbol'),
                                    market_sample.get('id'),
                                    market_sample.get('base'),
                                    market_sample.get('quote'),
                                    market_sample.get('limits', {}).get('amount', {}).get('min'),
                                    market_sample.get('precision', {}).get('price'),
                                    market_sample.get('precision', {}).get('amount')
                                ]
                                availablePairs.append(_a)
                                console.print(f"Appended {_a} to tracked pairs.")
                            elif not is_opt and pair_index != -1:
                                # Only remove/pause if the pair is not found in _balance (i.e. is dust or missing)
                                base = market_sample.get('base')
                                min_amount_val = market_sample.get('limits', {}).get('amount', {}).get('min')
                                try:
                                    min_amount = float(min_amount_val) if min_amount_val is not None else 0.0
                                except (ValueError, TypeError):
                                    min_amount = 0.0

                                base_balance = 0.0
                                if _balance and isinstance(_balance, dict):
                                    free_bal = _balance.get('free') or {}
                                    total_bal = _balance.get('total') or {}
                                    try:
                                        base_balance = float(free_bal.get(base, 0.0) or total_bal.get(base, 0.0) or 0.0)
                                    except (ValueError, TypeError):
                                        base_balance = 0.0

                                # durant la maintenance ou ailleurs, ne pas éliminer de paire dont l'asset base se trouve en balance et > min_amount.
                                if base_balance > min_amount:
                                    console.print(f"Retained {symbolChoose} in tracked pairs despite low trading count because non-dust balance exists ({base_balance} > {min_amount}).")
                                elif base_balance < min_amount:
                                    availablePairs.pop(pair_index)
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
                                    console.print(f"Paused buys for {symbolChoose} until {datetime.fromtimestamp(expiry_ts)} due to trading count below minimum and balance being dust or missing ({base_balance} < {min_amount}). Removed from availablePairs.")
                                else:
                                    # This branch handles base_balance == min_amount exactly. We can retain or treat as needed, but the instruction says strictly "> min_amount" for retention, or maybe ">=" is also ok, but we stick to the prompt's instruction: "et > min_amount". Let's retain it if >= min_amount, or as the previous line did:
                                    console.print(f"Retained {symbolChoose} in tracked pairs despite low trading count because non-dust balance exists ({base_balance} >= {min_amount}).")
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

                                # Fetch candles for symbol_o to compute mc_score
                                df_candles_o = None
                                if exchange.has.get('fetchOHLCV'):
                                    try:
                                        _id_o = _markets.get(symbol_o, {}).get('id') if isinstance(_markets, dict) else None
                                        if not _id_o:
                                            _id_o = symbol_o.replace('/', '')
                                        full_df_o = market_utils.fetch_ohlcv_data(
                                            exchange=exchange,
                                            _id=_id_o,
                                            symbol=symbol_o,
                                            pausedForBuy=pausedForBuy,
                                            PAUSE_FILE=PAUSE_FILE,
                                            console=console
                                        )
                                        calibrated_size_o = calibrate_window_by_non_repetition(full_df_o, target_active=480)
                                        df_candles_o = full_df_o.tail(calibrated_size_o) if full_df_o is not None else None
                                    except Exception as ce:
                                        console.print(f"Failed to fetch candles for open order {symbol_o}: {ce}")

                                if df_candles_o is not None and not df_candles_o.empty:
                                    from monte_carlo2 import MonteCarloEngine
                                    mc_engine_o = MonteCarloEngine(num_simulations=1000, timeframe_candles=240)
                                    mc_score_o = mc_engine_o.validate_strategy(df_candles_o)
                                    console.print(f"Order {o.get('id')} for {symbol_o} has mc_score={mc_score_o:.4f}")
                                    if mc_score_o < 0.42:
                                        console.print(f"Cancelling order {o.get('id')} ({symbol_o}) due to low mc_score ({mc_score_o:.4f} < 0.42)")
                                        try:
                                            time.sleep(exchange.rateLimit / 1000)
                                            try:
                                                exchange.cancel_order(o.get('id'), symbol_o)
                                            except TypeError:
                                                exchange.cancel_order(o.get('id'))
                                            console.print(f"Order {o.get('id')} cancelled successfully.")
                                        except Exception as ce:
                                            console.print(f"Failed to cancel order {o.get('id')} for {symbol_o}: {ce}")
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

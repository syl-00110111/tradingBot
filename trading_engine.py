# Cryptocurrencies multiplatform trading bot - Trading Engine
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

import logging
import time
import pandas as pd
import torch
from utils import format_price, format_amount, get_base_currency, play_sound
from exchange_handler import MockExchange
from monte_carlo import MonteCarloEngine
from indicators import get_signals

def calculate_average_price(order_book, side, amount):
    """Calculates average execution price from order book depth."""
    if not order_book: return 0
    # Bids for selling (hitting the bids), Asks for buying (hitting the asks)
    orders = order_book['bids'] if side == 'sell' else order_book['asks']

    remaining = amount
    total_cost = 0
    for price, vol in orders:
        if remaining <= 0: break
        exec_vol = min(remaining, vol)
        total_cost += exec_vol * price
        remaining -= exec_vol

    if remaining > 0:
        # If order book isn't deep enough, use the last available price for the rest
        total_cost += remaining * (orders[-1][0] if orders else 0)

    return total_cost / amount if amount > 0 else 0

class TradingEngine:
    def __init__(self, config):
        self.config = config

    def get_dynamic_settings(self, adx, volatility):
        """Returns dynamic strategy settings based on market conditions."""
        settings = {
            "ema_fast": 9, "ema_slow": 21, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
            "rsi_period": 14, "rsi_buy": 30, "rsi_sell": 70,
            "label": "balanced"
        }
        if adx > 25:
            settings.update({
                "ema_fast": 10, "ema_slow": 30,
                "rsi_buy": 40, "rsi_sell": 60,
                "label": "aggressive"
            })
        elif volatility > 0.01:
            settings.update({
                "ema_fast": 30, "ema_slow": 100,
                "rsi_buy": 20, "rsi_sell": 80,
                "label": "conservative"
            })
        return settings

    def calculate_position_size(self, balance, current_price, quote_currency, win_streak=0, exchange=None, symbol=None, data_manager=None):
        """Dynamic position sizing based on risk and wallet state."""
        total_balance = balance.get('total', balance)
        free_balance = balance.get('free', balance)

        # Risk management parameters
        max_bet_pct = self.config.get('max_bet_percentage', 0.1) # Max 10% of balance per trade
        max_total_exposure = self.config.get('max_total_exposure', 0.8) # Max 80% total exposure

        quote_total = total_balance.get(quote_currency, 0)
        quote_free = free_balance.get(quote_currency, 0)

        # If no budget left
        if quote_free <= 0: return 0

        # Max we can spend on this specific trade
        trade_amount_quote = quote_total * max_bet_pct

        # Adjust for current total exposure across all pairs
        if data_manager:
            open_positions = data_manager.get_open_positions()
            # Calculate exposure using each position's entry price as a proxy for current value
            # if we don't want to fetch all prices now.
            current_exposure = sum(p['amount'] * p['entry_price'] for p_list in open_positions.values() for p in p_list)
            if current_exposure > quote_total * max_total_exposure:
                return 0

            # Limit exposure per symbol
            symbol_positions = data_manager.get_positions(symbol)
            symbol_exposure = sum(p['amount'] * current_price for p in symbol_positions)
            remaining_budget = (quote_total * max_bet_pct) - symbol_exposure
        else:
            remaining_budget = trade_amount_quote

        # Aggr multiplier
        self.risk_multiplier = 1.0
        if self.config.get('aggressivity') == 'aggressive': self.risk_multiplier = 1.5
        elif self.config.get('aggressivity') == 'conservative': self.risk_multiplier = 0.5

        trade_amount_quote *= self.risk_multiplier

        ws_config = self.config.get('win_streak_bonus', {})
        if ws_config.get('enabled') and win_streak >= ws_config.get('threshold', 2):
             multiplier = ws_config.get('multiplier', 1.3)
             trade_amount_quote *= multiplier
             logging.info(f"[{symbol or 'N/A'}] Win streak detected ({win_streak}), applying {multiplier}x multiplier. New target: {trade_amount_quote:.2f} {quote_currency}")

        # Final amount is the minimum between the desired bet and the remaining budget for this symbol
        final_trade_amount_quote = min(trade_amount_quote, remaining_budget)

        if final_trade_amount_quote <= 0:
            if remaining_budget <= 0:
                logging.info(f"[{symbol}] Max symbol bet reached ({max_bet_pct*100}% of {quote_currency} balance).")
            return 0

        if current_price > 0: return final_trade_amount_quote / current_price
        return 0

    def is_profitable(self, exit_price, entry_price, fee_rate=0.0015):
        """Conservative profitability check accounting for fees on both sides."""
        min_exit_price = entry_price * (1 + fee_rate * 2)
        return exit_price > min_exit_price

    async def check_sure_profit(self, exchange, symbol, amount, entry_price, fee_rate=0.0015):
        """Perfect trader logic: verifies that a sell RIGHT NOW would be profitable."""
        order_book = await exchange.fetch_order_book(symbol)
        effective_sell_price = calculate_average_price(order_book, 'sell', amount)
        return self.is_profitable(effective_sell_price, entry_price, fee_rate)

    def validate_trade_mc(self, symbol, data, config):
        """Perform Monte Carlo sanity check before trade execution."""
        if config.get('mode') == 'sell': return True # Skip for manual sell mode
        try:
            strategy = data.get('strategy')
            if not strategy: return True

            last_100 = data.get('last_100_candles')
            if not last_100 or not last_100.get('prices'):
                return True

            df_mc = pd.DataFrame(last_100)
            if 'close' not in df_mc.columns:
                df_mc['close'] = df_mc['prices']
            if 'volume' not in df_mc.columns:
                df_mc['volume'] = df_mc['volumes']

            device = config.get('device', torch.device('cpu'))
            from indicators import STRATEGIES
            mc_engine = MonteCarloEngine(num_simulations=100)
            mc_engine.set_device(device)
            score = mc_engine.validate_strategy(df_mc)
            data['mc_score'] = score
            return score >= 0.7 # Conservative threshold
        except Exception as e:
            logging.error(f"Monte Carlo validation failed for {symbol}: {e}")
            return True # Fallback to true to not block signals on error

async def execute_buy(exchange, data_manager, engine, symbol, data, config, available_assets, suspended_pairs, balance=None):
    balance = await exchange.fetch_balance()
    if not balance:
        logging.error(f"[{symbol}] Buy aborted: Failed to fetch fresh balance.")
        return False

    win_streak = data_manager.get_win_streak(symbol)
    quote_curr = symbol.split('/')[1]

    # Use fetch_ohlcv to get latest price
    ohlcv = await exchange.fetch_ohlcv(symbol, '1m', limit=1)
    initial_price = ohlcv[0][4] if ohlcv else 0

    amount = engine.calculate_position_size(balance, initial_price, quote_curr, win_streak=win_streak, exchange=exchange, symbol=symbol, data_manager=data_manager)

    if amount <= 0:
        logging.warning(f"[{symbol}] Buy aborted: Calculated amount is zero.")
        return False

    # Get effective price from order book
    order_book = await exchange.fetch_order_book(symbol)
    effective_buy_price = calculate_average_price(order_book, 'buy', amount)

    if effective_buy_price <= 0:
        return False
    base_currency = symbol.split('/')[1]

    # Check if balance is sufficient before attempting order
    cost = amount * effective_buy_price
    base_asset = base_currency
    free_balance = balance.get(base_asset, {}).get('free', 0) if isinstance(balance, dict) and 'free' in balance else balance.get(base_asset, 0)

    if free_balance < cost:
        logging.warning(f"[{symbol}] Buy aborted: Insufficient {base_asset} balance ({format_price(free_balance)} < {format_price(cost)}). Suspending pair.")
        suspended_pairs.add(symbol)
        return False

    # Check NOTIONAL / Minimum Cost filter
    try:
        markets = exchange.markets if hasattr(exchange, 'markets') and exchange.markets else await exchange.load_markets()
        if symbol in markets:
            min_cost = markets[symbol]['limits']['cost']['min'] or 0
            if cost < min_cost:
                logging.warning(f"[{symbol}] Buy aborted: Order cost {format_price(cost)} is below minimum notional limit {format_price(min_cost)}. Suspending pair.")
                suspended_pairs.add(symbol)
                return False
    except Exception as e:
        logging.debug(f"[{symbol}] Could not verify notional limit: {e}")

    order = await exchange.create_order(symbol, 'buy', amount)
    if order:
        exec_price = order.get('average', order.get('price', effective_buy_price))
        exec_amount = order.get('filled', order.get('amount', amount))
        fee = order.get('calculated_fee', 0)

        total_paid = (exec_amount * exec_price) + fee
        logging.info(f"[{symbol}] Executing buy of amount {format_amount(exec_amount)} at {format_price(exec_price)}, final price paid: {format_price(total_paid)} {get_base_currency(symbol, config)}")
        data_manager.add_position(symbol, exec_price, exec_amount, fee, data.get('trigger_data', {}), time.time(), total_base=total_paid)

        data['amt'] = (data.get('amt', 0) or 0) + exec_amount
        asset = symbol.split('/')[0]
        if asset not in available_assets:
            available_assets.append(asset)
            available_assets.sort()
        return True
    else:
        logging.warning(f"[{symbol}] Buy execution failed: Exchange rejected order.")
    return False

async def execute_sell(exchange, data_manager, engine, symbol, data, config, position_idx=0, force=False):
    positions = data_manager.get_positions(symbol)
    if not positions or position_idx >= len(positions):
        return False

    position = positions[position_idx]
    balance = await exchange.fetch_balance()
    if not balance:
        logging.error(f"[{symbol}] Sell aborted: Failed to fetch fresh balance.")
        return False

    order_book = await exchange.fetch_order_book(symbol)
    effective_sell_price = calculate_average_price(order_book, 'sell', position['amount'])

    # Estimate fee (0.1% default)
    fee_rate = 0.001

    if not force and not engine.is_profitable(effective_sell_price, position['entry_price'], fee_rate=fee_rate):
        logging.info(f"[{symbol}] Sell signal ignored: Profit not sure yet (Entry: {position['entry_price']}, Est. Exit: {effective_sell_price})")
        return False

    base_asset = symbol.split('/')[0]
    is_simulation = isinstance(exchange, MockExchange)
    free_balance = balance.get(base_asset, {}).get('free', 0) if isinstance(balance, dict) and 'free' in balance else balance.get(base_asset, 0)

    sell_amount = position['amount']
    if not is_simulation:
        if free_balance < sell_amount:
            if free_balance >= sell_amount * 0.95: # 5% tolerance
                sell_amount = free_balance
            else:
                logging.warning(f"[{symbol}] Sell aborted: Insufficient balance ({format_amount(free_balance)} < {format_amount(sell_amount)}).")
                return False

    order = await exchange.create_order(symbol, 'sell', sell_amount)
    if order:
        exec_price = order.get('average', order.get('price', effective_sell_price))
        exec_amount = order.get('filled', order.get('amount', sell_amount))
        fee = order.get('calculated_fee', 0)

        logging.info(f"[{symbol}] Executing sell of amount {format_amount(exec_amount)} at {format_price(exec_price)}")
        data_manager.close_position(symbol, exec_price, exec_amount, fee, data.get('trigger_data', {}), time.time(), position_idx=position_idx)

        data['amt'] = max(0, (data.get('amt', 0) or 0) - exec_amount)
        return True
    else:
        logging.warning(f"[{symbol}] Sell execution failed: Exchange rejected order.")
    return False

async def get_sellable_assets_with_amounts(exchange, config=None):
    balance = await exchange.fetch_balance()
    if not balance: return {}
    result = {}
    base_currencies = config.get('base_currencies', ['USDT', 'USDC', 'EUR']) if config else ['USDT', 'USDC', 'EUR']
    total_balances = balance.get('total', balance)
    for asset, amount in total_balances.items():
        if not isinstance(amount, (int, float)) or amount <= 0: continue
        if asset in base_currencies or asset == 'USDT': continue
        symbol = None
        for bc in base_currencies:
            candidate = f"{asset}/{bc}"
            if config and candidate in config.get('pairs', {}):
                symbol = candidate
                break
        if not symbol: continue

        ohlcv = await exchange.fetch_ohlcv(symbol, '1m', limit=1)
        price = ohlcv[0][4] if ohlcv else 0
        if amount * price > 1.0: # Filter dust
            result[asset] = amount
    return result

async def get_sellable_assets(exchange, config=None):
    amounts = await get_sellable_assets_with_amounts(exchange, config)
    return sorted(list(amounts.keys()))


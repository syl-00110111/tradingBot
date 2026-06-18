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

class TradingEngine:
    def __init__(self, config):
        self.config = config
        self.risk_multiplier = float(config.get('global_risk_multiplier', 1.2))

    def parse_base_bet(self):
        """
        Parses base_bet as a percentage of the available balance.
        Example: "10%" or 10.0 -> returns 0.10.
        """
        raw_val = self.config.get('base_trade_amount', self.config.get('base_bet', '10%'))
        if isinstance(raw_val, str):
            try:
                val = float(raw_val.replace('%', '').strip())
                return val / 100.0 if val >= 1.0 else val
            except ValueError:
                return 0.10
        return float(raw_val) / 100.0 if float(raw_val) >= 1.0 else float(raw_val)

    def get_dynamic_settings(self, adx, volatility):
        settings = {
            "ema_fast": 9, "ema_slow": 21,
            "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
            "rsi_period": 14, "rsi_buy": 30, "rsi_sell": 70,
        }
        if adx > 25:
            settings.update({
                "ema_fast": 10, "ema_slow": 30,
                "rsi_buy": 40, "rsi_sell": 60,
            })
        elif volatility > self.config.get('profit_thresholds', {}).get('min_pattern_profit', 0.01):
            settings.update({
                "ema_fast": 30, "ema_slow": 100,
                "rsi_buy": 20, "rsi_sell": 80,
            })
        return settings

    def is_profitable(self, current_price, entry_price, fee_rate=0.001):
        min_exit_price = entry_price * (1 + fee_rate * 2)
        return current_price > min_exit_price

    def check_profitability(self, current_price, entry_price, symbol, fee_rate=0.001):
        return self.is_profitable(current_price, entry_price, fee_rate)

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
            mc_engine = MonteCarloEngine(num_simulations=1000, timeframe_candles=20)
            mc_engine.set_device(device)

            temp_cfg = {'strategy': strategy, 'device': device}
            df_mc = get_signals(df_mc, temp_cfg, is_backtest=False)

            mc_score = mc_engine.validate_strategy(df_mc)
            hurdle = config.get('profit_thresholds', {}).get('mc_validation_hurdle', 0.0015)

            if mc_score > 1.0 + hurdle:
                return True
            else:
                logging.warning(f"[{symbol}] Trade rejected by Monte Carlo validation (Score: {mc_score:.4f} <= Hurdle: {1.0+hurdle:.4f})")
                return False
        except Exception as e:
            logging.debug(f"MC validation failed for {symbol}, defaulting to True: {e}")
            return True

    def calculate_position_size(self, balance, current_price, base_currency, win_streak=0, exchange=None, symbol=None):
        """
        Calculates position size based on a percentage of the available balance of the quote asset.
        """
        base_balance = 0
        if isinstance(balance, dict):
            if 'free' in balance and isinstance(balance['free'], dict):
                base_balance = balance['free'].get(base_currency, 0)
            else:
                base_balance = balance.get(base_currency, 0)
                if isinstance(base_balance, dict):
                    base_balance = base_balance.get('free', 0)
        else:
            base_balance = 0

        base_percentage = self.parse_base_bet()
        trade_amount_base = base_balance * base_percentage
        trade_amount_base *= self.risk_multiplier

        ws_config = self.config.get('win_streak_bonus', {})
        if ws_config.get('enabled') and win_streak >= ws_config.get('threshold', 2):
             multiplier = ws_config.get('multiplier', 1.3)
             trade_amount_base *= multiplier
             logging.info(f"[{symbol or 'N/A'}] Win streak detected ({win_streak}), applying {multiplier}x multiplier. New target: {trade_amount_base:.2f} {base_currency}")

        if trade_amount_base > base_balance: trade_amount_base = base_balance
        if current_price > 0: return trade_amount_base / current_price
        return 0

def execute_buy(exchange, data_manager, engine, symbol, data, config, bot_lock, available_assets, suspended_pairs, balance=None, term="short"):
    if balance is None:
        balance = exchange.fetch_balance()
    if not balance:
        logging.error(f"[{symbol}] Buy aborted: Failed to fetch balance.")
        return False
    win_streak = data_manager.get_win_streak(symbol)

    # Refresh price for Spot market accuracy (prevent NOTIONAL filters)
    fresh_ticker = exchange.fetch_ticker(symbol)
    current_price = fresh_ticker['last'] if fresh_ticker else data['price']

    base_curr = symbol.split('/')[1]
    amount = engine.calculate_position_size(balance, current_price, base_curr, win_streak=win_streak, exchange=exchange, symbol=symbol)
    base_currency = symbol.split('/')[1]
    if amount > 0:
        # Check if balance is sufficient before attempting order
        cost = amount * current_price
        base_asset = base_currency
        free_balance = balance.get(base_asset, {}).get('free', 0) if isinstance(balance, dict) and 'free' in balance else balance.get(base_asset, 0)

        if free_balance < cost:
            logging.warning(f"[{symbol}] Buy aborted: Insufficient {base_asset} balance ({format_price(free_balance)} < {format_price(cost)})")
            return False

        # Check NOTIONAL / Minimum Cost filter
        try:
            markets = exchange.markets if hasattr(exchange, 'markets') and exchange.markets else exchange.load_markets()
            if symbol in markets:
                min_cost = markets[symbol]['limits']['cost']['min'] or 0
                if cost < min_cost:
                    logging.warning(f"[{symbol}] Buy aborted: Order cost {format_price(cost)} is below minimum notional limit {format_price(min_cost)}.")
                    return False
        except Exception as e:
            logging.debug(f"[{symbol}] Could not verify notional limit: {e}")

        order = exchange.create_order(symbol, 'buy', amount)
        if isinstance(order, dict) and 'insufficient balance' in str(order.get('message', '')).lower():
            logging.error(f"[{symbol}] Buy failed: Insufficient balance. Suspending pair.")
            suspended_pairs.add(symbol)
            return False
        if isinstance(order, dict) and 'code' in str(order) and 'Filter failure: NOTIONAL' in str(order):
            logging.error(f"[{symbol}] Buy failed: Filter failure NOTIONAL. Suspending pair.")
            suspended_pairs.add(symbol)
            return False
        if order:
            # Use executed values if available
            exec_price = order.get('average', order.get('price', current_price))
            exec_amount = order.get('filled', order.get('amount', amount))
            fee = order.get('calculated_fee', 0)

            total_paid = (exec_amount * exec_price) + fee
            logging.info(f"[{symbol}] Executing buy of amount {format_amount(exec_amount)} at {format_price(exec_price)}, final price paid: {format_price(total_paid)} {get_base_currency(symbol, config)}")
            data_manager.add_position(symbol, exec_price, exec_amount, fee, data.get('trigger_data', {}), time.time(), total_base=total_paid, term=term)

            # Immediately update Sellable list
            asset = symbol.split('/')[0]
            with bot_lock:
                if asset not in available_assets:
                    available_assets.append(asset)
                    available_assets.sort()

            return True
        else:
            logging.warning(f"[{symbol}] Buy execution failed: Exchange rejected order for amount {format_amount(amount)}")
    else:
        logging.warning(f"[{symbol}] Buy aborted: Calculated amount is zero or negative.")
    return False

def execute_sell(exchange, data_manager, engine, symbol, data, config, position_idx=0):
    positions = data_manager.get_positions(symbol)
    if not positions or position_idx >= len(positions):
        return False

    position = positions[position_idx]
    should_execute = True

    # Instruction 7: check for "guaranteed" sale price
    ticker = exchange.fetch_ticker(symbol)
    guaranteed_price = ticker['bid'] if ticker and 'bid' in ticker else data['price']

    if should_execute:
        base_asset = symbol.split('/')[0]

        # Bypass balance check for simulation mode
        # In simulation, we trust the internal DataManager state
        is_simulation = isinstance(exchange, MockExchange)

        balance = exchange.fetch_balance()
        if not balance:
            logging.error(f"[{symbol}] Sell aborted: Failed to fetch balance.")
            return False
        free_balance = balance.get(base_asset, {}).get('free', 0) if isinstance(balance, dict) and 'free' in balance else balance.get(base_asset, 0)

        # Tolerance for small balance discrepancies (e.g., due to fees or rounding)
        sell_amount = position['amount']
        if not is_simulation:
            if free_balance < sell_amount:
                if free_balance >= sell_amount * 0.95: # 5% tolerance
                    sell_amount = free_balance
                    logging.info(f"[{symbol}] Adjusting sell amount from {position['amount']} to {sell_amount} due to balance discrepancy (within 5% tolerance).")
                else:
                    logging.warning(f"[{symbol}] Sell aborted: Insufficient balance ({format_amount(free_balance)} is significantly less than tracked {format_amount(sell_amount)}).")
                    return False
            # Ensure we don't sell more than tracked even if balance is higher (unless we want to clear the asset)
            # For simplicity, we stick to tracked amount if balance is sufficient.

        if is_simulation or free_balance >= sell_amount:
            order = exchange.create_order(symbol, 'sell', sell_amount)
            if isinstance(order, dict) and order.get('error') == 'dust_limit':
                logging.warning(f"[{symbol}] Sell aborted: Balance is dust/below precision. Ignoring future sell signals for this position.")
                data_manager.flag_ignore_sell(symbol)
                return False
            if order:
                # Use executed values if available
                exec_price = order.get('average', order.get('price', guaranteed_price))
                exec_amount = order.get('filled', order.get('amount', sell_amount))
                fee = order.get('calculated_fee', 0)

                total_received = (exec_amount * exec_price) - fee
                logging.info(f"[{symbol}] Executing sell of amount {format_amount(exec_amount)} at {format_price(exec_price)}, final price received: {format_price(total_received)} {get_base_currency(symbol, config)}")
                profit = total_received - position.get('entry_total_base', 0)
                data_manager.close_position(symbol, exec_price, fee, profit, data.get('trigger_data', {}), time.time(), total_base=total_received, position_idx=position_idx)
                return True
            else:
                logging.error(f"[{symbol}] Sell failed: Exchange rejected order for amount {format_amount(sell_amount)}")
                return False
        else:
            logging.warning(f"[{symbol}] Sell aborted: Insufficient balance ({format_amount(free_balance)} < {format_amount(sell_amount)})")
            return False
    return False

def initialize_simulation(exchange, data_manager, pattern_manager, engine, config, bot_state):
    """Syncs real wallet assets into the simulation engine's tracked positions."""
    logging.info("Initializing Simulation positions from real wallet inventory...")
    balance = exchange.fetch_balance()
    if not balance:
        logging.error("Failed to fetch balance for simulation initialization.")
        return

    free_balances = balance.get('free', balance)
    base_currencies = config.get('base_currencies', ['USDT', 'USDC', 'EUR'])

    sellable_found = False
    for asset, amount in free_balances.items():
        if asset in base_currencies or asset == 'USDT' or not isinstance(amount, (float, int)) or amount <= 0:
            continue

        symbol = None
        for bc in base_currencies:
            candidate = f"{asset}/{bc}"
            if candidate in config.get('pairs', {}):
                symbol = candidate
                break

        if not symbol: continue

        # Check if it's dust
        is_dust = False
        try:
            markets = exchange.markets if hasattr(exchange, 'markets') and exchange.markets else exchange.load_markets()
            if symbol in markets:
                m = markets[symbol]
                min_amt = m['limits']['amount']['min']
                min_cost = m['limits']['cost']['min'] or 10
                ticker = exchange.fetch_ticker(symbol)
                if ticker and (amount < min_amt or (amount * ticker['last']) < min_cost):
                    is_dust = True
            elif amount <= 0.000001: is_dust = True
        except:
            if amount <= 0.000001: is_dust = True

        if is_dust: continue
        sellable_found = True

        # Try to find purchase price
        entry_price = 0
        trades = exchange.fetch_my_trades(symbol, limit=20)
        if trades:
            buy_trades = [t for t in trades if t['side'] == 'buy']
            if buy_trades:
                entry_price = buy_trades[-1]['price']

        if entry_price > 0:
            # Avoid duplicate sync if already tracked
            existing = data_manager.get_positions(symbol)
            if not any(p['amount'] == amount and p['entry_price'] == entry_price for p in existing):
                logging.info(f"[{symbol}] Found purchase price: {entry_price}. Adding to tracking.")
                data_manager.add_position(symbol, entry_price, amount, 0, {}, time.time(), term="short")
        else:
            logging.warning(f"[{symbol}] Asset found in wallet but no purchase record found via API. Please connect to exchange and sell manually or manage this asset.")

    if not sellable_found and any(v > 0 for k, v in free_balances.items() if k not in base_currencies):
        has_base_balance = any(free_balances.get(bc, 0) > 10 for bc in base_currencies)
        if not has_base_balance:
            logging.warning("No sellable assets found. Your wallet contains only 'dust' (amounts below exchange limits). Please add funds or use the exchange website to convert dust to a base currency.")
        else:
            logging.info("No non-base sellable assets found, but base currency balance is available.")

def sync_live_positions(exchange, data_manager, config):
    """Verifies that all tracked positions in DataManager still exist in the exchange wallet."""
    balance = exchange.fetch_balance()
    if not balance: return
    free_balances = balance.get('free', balance)

    tracked_positions = data_manager.get_open_positions()
    for symbol, positions in tracked_positions.items():
        base_asset = symbol.split('/')[0]
        actual_balance = free_balances.get(base_asset, 0)

        total_tracked_amount = sum(p['amount'] for p in positions)

        # If real balance is significantly lower than tracked amount, prune tracking
        if actual_balance < total_tracked_amount * 0.95:
             logging.warning(f"[{symbol}] Tracked amount ({total_tracked_amount}) exceeds real balance ({actual_balance}). Pruning tracking.")
             # We clear and wait for next sync or manual intervention
             for _ in range(len(positions)):
                  data_manager.close_position(symbol, 0, 0, 0, {"note": "Pruned during sync"}, time.time())

def get_sellable_assets_sim(data_manager):
    positions = data_manager.get_open_positions()
    return sorted([s.split('/')[0] for s in positions.keys()])

def get_sellable_assets(exchange, config=None):
    balance = exchange.fetch_balance()
    if not balance:
        return []
    assets = []
    base_currencies = config.get('base_currencies', ['USDT', 'USDC', 'EUR']) if config else ['USDT', 'USDC', 'EUR']
    free_balances = balance.get('free', balance)

    for asset, amount in free_balances.items():
        if not isinstance(amount, (int, float)) or amount <= 0: continue
        if asset in base_currencies or asset == 'USDT': continue

        # Find pair
        symbol = None
        for bc in base_currencies:
            candidate = f"{asset}/{bc}"
            if config and candidate in config.get('pairs', {}):
                symbol = candidate
                break
        if not symbol: continue

        try:
            markets = exchange.markets if hasattr(exchange, 'markets') and exchange.markets else exchange.load_markets()
            if symbol in markets:
                market = markets[symbol]
                min_amount = market['limits']['amount']['min']
                min_cost = market['limits']['cost']['min'] or 10
                ticker = exchange.fetch_ticker(symbol)
                if ticker and (amount < min_amount or (amount * ticker['last']) < min_cost): continue
            elif amount <= 0.000001: continue
            assets.append(asset)
        except Exception:
            if amount > 0.000001: assets.append(asset)
    return sorted(assets)

def interactive_sell(exchange, data_manager, engine, config, console):
    console.print("\n[bold magenta]=== Interactive Sell Mode (Real Wallet) ===[/]")
    play_sound("sell", config)
    balance = exchange.fetch_balance()
    if not balance:
        console.print("[red]Error: Failed to fetch balance.[/]")
        return
    free_balances = balance.get('free', balance)
    base_currencies = config.get('base_currencies', ['USDT', 'USDC', 'EUR'])

    sellable_found = False
    for asset, amount in free_balances.items():
        if asset in base_currencies or asset == 'USDT' or not isinstance(amount, (float, int)) or amount <= 0:
            continue

        symbol = None
        for bc in base_currencies:
            candidate = f"{asset}/{bc}"
            if candidate in config.get('pairs', {}):
                symbol = candidate
                break
        if not symbol: continue

        markets = {}
        if hasattr(exchange, 'exchange') and exchange.exchange.markets:
            markets = exchange.exchange.markets
        elif hasattr(exchange, 'markets'):
            markets = exchange.markets

        if not markets or symbol not in markets:
            continue

        market = markets[symbol]
        min_amount = market['limits']['amount']['min']
        min_cost = market['limits']['cost']['min'] or 10

        ticker = exchange.fetch_ticker(symbol)
        if not ticker:
            continue

        price = ticker['last']
        cost = amount * price

        if amount < min_amount or cost < min_cost:
            continue

        sellable_found = True
        quote = get_base_currency(symbol, config)
        console.print(f"\n[bold cyan]Asset:[/] {asset} | [bold cyan]Balance:[/] {format_amount(amount)} | [bold cyan]Value:[/] {format_price(cost)} {quote}")

        # Automatic execution (interactive mode replaced with auto-mode)
        quote = get_base_currency(symbol, config)
        console.print(f"[yellow]Selling {format_amount(amount)} {asset} at ~{format_price(price)} {quote}...[/]")
        order = exchange.create_order(symbol, 'sell', amount)
        if order:
            fee = order.get('calculated_fee', 0)
            total_received = (amount * price) - fee
            quote = get_base_currency(symbol, config)
            logging.info(f"[{symbol}] Executing sell of amount {format_amount(amount)} at {format_price(price)}, final price received: {format_price(total_received)} {quote}")
            console.print(f"[bold green]Successfully sold {asset}! Final received: {format_price(total_received)} {quote}[/]")
            play_sound("sell", None)
            pos_list = data_manager.get_positions(symbol)
            if pos_list:
                pos = pos_list[0]
                profit = total_received - pos.get('entry_total_base', 0)
                data_manager.close_position(symbol, price, fee, profit, {}, time.time(), total_base=total_received, position_idx=0)
        else:
            console.print(f"[bold red]Failed to sell {asset}.[/]")

    if not sellable_found:
        msg = "No sellable assets (above dust threshold) found in your real wallet."
        console.print(f"[yellow]{msg}[/]")
        logging.info(msg)

def show_balance(exchange, config, console, table_class):
    console.print("\n[bold magenta]=== Real Wallet Balance (All Assets) ===[/]")
    balance = exchange.fetch_balance()
    if not balance:
        console.print("[red]Error: Failed to fetch balance.[/]")
        return

    table = table_class(title="Asset Inventory", expand=True)
    base_bet_curr = get_base_currency(None, config)
    table.add_column("Asset", style="cyan")
    table.add_column("Free", justify="right")
    table.add_column("Used", justify="right")
    table.add_column("Total", justify="right")
    table.add_column(f"Estimated Value ({base_bet_curr})", justify="right", style="green")

    total_balances = balance.get('total', balance)
    free_balances = balance.get('free', {})
    used_balances = balance.get('used', {})

    total_value_base = 0

    for asset in sorted(total_balances.keys()):
        total = total_balances[asset]
        if not isinstance(total, (int, float)) or total == 0:
            continue

        free = free_balances.get(asset, 0)
        used = used_balances.get(asset, 0)

        val_in_base = 0
        if asset in [base_bet_curr, 'USDT', 'USDC']:
            val_in_base = total
        else:
            ticker = None
            for bc in [base_bet_curr, 'USDT', 'USDC']:
                candidate = f"{asset}/{bc}"
                ticker = exchange.fetch_ticker(candidate)
                if ticker and ticker.get('last', 0) > 0:
                     val_in_base = total * ticker['last']
                     break
            if not ticker or ticker.get('last', 0) <= 0:
                ticker_usdt = exchange.fetch_ticker(f"{asset}/USDT")
                ticker_base_usdt = exchange.fetch_ticker(f"{base_bet_curr}/USDT")
                if ticker_usdt and ticker_base_usdt and ticker_base_usdt['last'] > 0:
                    val_in_base = (total * ticker_usdt['last']) / ticker_base_usdt['last']

        total_value_base += val_in_base
        val_str = format_price(val_in_base) if val_in_base > 0 else "N/A"

        table.add_row(
            asset,
            format_amount(free),
            format_amount(used),
            format_amount(total),
            val_str
        )

    console.print(table)
    console.print(f"\n[bold yellow]Estimated Total Wallet Value: {format_price(total_value_base)} {base_bet_curr}[/]\n")

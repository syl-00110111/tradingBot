import asyncio
import json
import os
import ccxt.pro as ccxtpro
from rich.console import Console
from rich.logging import RichHandler
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True)]
)
log = logging.getLogger("rich")
console = Console()

class TradingBot:
    def __init__(self, config, pairs):
        self.config = config
        self.pairs = pairs
        self.exchange_id = config.get('exchange_id', 'binance')

        # Supporting both CCXT and api.json naming conventions
        api_key = config.get('apiKey') or config.get('api_key')
        secret = config.get('secret') or config.get('api_secret')

        self.exchange = getattr(ccxtpro, self.exchange_id)({
            'apiKey': api_key,
            'secret': secret,
            'enableRateLimit': True,
            'options': config.get('options', {'defaultType': 'spot'})
        })

        self.balances = {}  # asset -> free balance
        self.lots = {pair: [] for pair in pairs}  # pair -> list of {'amount': float, 'price': float}
        self.last_action_prices = {pair: None for pair in pairs} # pair -> float

        # Pairs and assets tracking
        self.base_assets = [p.split('/')[0] for p in pairs]
        # Map each symbol to its specific quote asset
        self.symbol_to_quote = {p: p.split('/')[1] for p in pairs}
        self.fees = {pair: 0.001 for pair in pairs} # Default 0.1%

    async def initialize(self):
        log.info("Initialisation des marchés et des frais...")
        await self.exchange.load_markets()
        for symbol in self.pairs:
            try:
                if self.exchange.apiKey:
                    fees = await self.exchange.fetch_trading_fee(symbol)
                    self.fees[symbol] = fees.get('taker', 0.001)
            except:
                pass

        # Initial balance fetch
        if self.exchange.apiKey:
            try:
                balance = await self.exchange.fetch_balance()
                self.update_local_balances(balance)
            except Exception as e:
                log.error(f"Erreur lors de la récupération initiale de la balance: {e}")

    def update_local_balances(self, balance):
        if 'free' in balance:
            self.balances = {asset: amt for asset, amt in balance['free'].items() if amt > 0}
            # Log base asset balances specifically as requested
            relevant_assets = set(self.base_assets + list(self.symbol_to_quote.values()))
            relevant_balances = {asset: self.balances.get(asset, 0) for asset in relevant_assets}
            log.debug(f"Balances pertinentes: {relevant_balances}")

    async def watch_balance_loop(self):
        log.info("Démarrage de la boucle watchBalance...")
        while True:
            try:
                balance = await self.exchange.watch_balance()
                self.update_local_balances(balance)
            except Exception as e:
                log.error(f"Erreur dans watch_balance: {e}")
                await asyncio.sleep(5)

    async def watch_ohlcv_loop(self):
        log.info(f"Démarrage de la boucle watchOHLCVForSymbols pour {self.pairs}...")
        timeframe = '1m'
        while True:
            try:
                ohlcv_dict = await self.exchange.watch_ohlcv_for_symbols(self.pairs, timeframe)

                for symbol, candles in ohlcv_dict.items():
                    if not candles:
                        continue

                    latest_candle = candles[-1]
                    # [timestamp, open, high, low, close, volume]
                    current_price = latest_candle[4]

                    await self.process_price_update(symbol, current_price)

            except Exception as e:
                log.error(f"Erreur dans watch_ohlcv: {e}")
                await asyncio.sleep(5)

    async def process_price_update(self, symbol, current_price):
        last_price = self.last_action_prices.get(symbol)

        if last_price is None:
            self.last_action_prices[symbol] = current_price
            log.info(f"[{symbol}] Prix initial: {current_price}")
            return

        quote_asset = self.symbol_to_quote[symbol]

        if current_price < last_price:
            # BUY Logic: 1/10th of available quote asset
            quote_balance = self.balances.get(quote_asset, 0)
            amount_to_spend = quote_balance / 10.0

            if amount_to_spend > 0:
                log.info(f"[{symbol}] Prix en baisse ({current_price} < {last_price}). Tentative d'achat...")

                try:
                    raw_amount = amount_to_spend / current_price

                    # Place real order if API keys are present
                    if self.exchange.apiKey and "YOUR_API_KEY" not in self.exchange.apiKey:
                        # Apply exchange precision
                        amount_to_buy = float(self.exchange.amount_to_precision(symbol, raw_amount))
                        if amount_to_buy > 0:
                            order = await self.exchange.create_market_buy_order(symbol, amount_to_buy)
                            executed_price = order.get('price') or current_price
                            executed_amount = order.get('amount') or amount_to_buy
                            cost = order.get('cost') or (executed_price * executed_amount)
                        else:
                            log.warning(f"[{symbol}] Montant calculé trop petit après précision.")
                            return
                    else:
                        # Simulation
                        executed_price = current_price
                        executed_amount = raw_amount
                        cost = amount_to_spend

                    self.lots[symbol].append({'amount': executed_amount, 'price': executed_price})
                    self.last_action_prices[symbol] = executed_price
                    console.print(f"[bold green][{symbol}] ACHAT effectué. Prix payé: {executed_price} (Total: {cost:.2f} {quote_asset})[/]")
                except Exception as e:
                    log.error(f"[{symbol}] Échec de l'achat: {e}")
            else:
                log.debug(f"[{symbol}] Prix en baisse mais balance {quote_asset} insuffisante.")

        elif current_price > last_price:
            # SELL Logic: check profitability for each lot (> 1% including fees)
            fee_rate = self.fees.get(symbol, 0.001)
            any_sold = False

            remaining_lots = []
            for lot in self.lots[symbol]:
                # Profitability check: 1% minimum profit AFTER fees
                sell_net = current_price * (1 - fee_rate)
                buy_total = lot['price'] * (1 + fee_rate)
                profit_pct = (sell_net - buy_total) / buy_total

                if profit_pct >= 0.01:
                    log.info(f"[{symbol}] Lot profitable ({profit_pct:.2%}). Tentative de vente...")
                    try:
                        if self.exchange.apiKey and "YOUR_API_KEY" not in self.exchange.apiKey:
                            # Apply exchange precision
                            amount_to_sell = float(self.exchange.amount_to_precision(symbol, lot['amount']))
                            if amount_to_sell > 0:
                                order = await self.exchange.create_market_sell_order(symbol, amount_to_sell)
                                executed_price = order.get('price') or current_price
                                revenue = order.get('cost') or (executed_price * amount_to_sell)
                            else:
                                log.warning(f"[{symbol}] Montant de lot trop petit pour la vente.")
                                remaining_lots.append(lot)
                                continue
                        else:
                            # Simulation
                            executed_price = current_price
                            revenue = current_price * lot['amount']

                        any_sold = True
                        console.print(f"[bold red][{symbol}] VENTE effectuée. Prix encaissé: {executed_price} (Profit: {revenue - (lot['price'] * lot['amount']):.2f} {quote_asset})[/]")
                    except Exception as e:
                        log.error(f"[{symbol}] Échec de la vente du lot: {e}")
                        remaining_lots.append(lot)
                else:
                    remaining_lots.append(lot)

            self.lots[symbol] = remaining_lots
            if any_sold:
                self.last_action_prices[symbol] = current_price

    async def stop(self):
        await self.exchange.close()

async def load_config():
    if os.path.exists('api.json'):
        with open('api.json', 'r') as f:
            return json.load(f)
    elif os.path.exists('api.json.example'):
        return json.load(open('api.json.example'))
    return {}

async def load_pairs():
    if os.path.exists('pairs.txt'):
        with open('pairs.txt', 'r') as f:
            return [line.strip() for line in f if line.strip()]
    return []

async def main():
    config = await load_config()
    pairs = await load_pairs()

    if not pairs:
        log.error("Aucune paire trouvée dans pairs.txt")
        return

    bot = TradingBot(config, pairs)
    try:
        await bot.initialize()
        log.info(f"Bot démarré sur {bot.exchange_id} avec {len(pairs)} paires.")
        await asyncio.gather(
            bot.watch_balance_loop(),
            bot.watch_ohlcv_loop()
        )
    finally:
        await bot.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.exception(f"Erreur fatale: {e}")

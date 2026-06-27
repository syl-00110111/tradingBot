import asyncio
import json
import os
import ccxt.pro as ccxtpro
from rich.console import Console
from rich.logging import RichHandler
import logging

# Configuration du logging avec Rich pour une sortie propre
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True)]
)
log = logging.getLogger("rich")
console = Console()

class TradingBot:
    def __init__(self, config, raw_pairs):
        self.config = config
        self.raw_pairs = raw_pairs if isinstance(raw_pairs, list) else [raw_pairs]
        self.pairs = [] # Liste des symboles validés
        self.exchange_id = config.get('exchange_id', 'binance')

        # Support des deux conventions de nommage pour les clés API
        api_key = config.get('apiKey') or config.get('api_key')
        secret = config.get('secret') or config.get('api_secret')

        # Initialisation de l'échange CCXT Pro
        self.exchange = getattr(ccxtpro, self.exchange_id)({
            'apiKey': api_key if api_key and "YOUR_API_KEY" not in api_key else None,
            'secret': secret if secret and "YOUR_API_SECRET" not in secret else None,
            'enableRateLimit': True,
            'options': config.get('options', {'defaultType': 'spot'})
        })

        self.balances = {}  # Actif -> Solde disponible
        self.lots = {}  # Symbole -> Liste des lots achetés {'amount': float, 'price': float}
        self.last_action_prices = {} # Symbole -> Dernier prix d'action
        self.symbol_to_quote = {} # Symbole -> Devise de cotation (ex: USDC)
        self.base_assets = [] # Liste des actifs de base
        self.fees = {} # Symbole -> Taux de commission

    async def initialize(self):
        log.info(f"Connexion à l'échange {self.exchange_id}...")
        await self.exchange.load_markets()

        log.info("Validation des paires de trading...")
        for p in self.raw_pairs:
            if not p or not isinstance(p, str): continue
            symbol = p.strip()

            # Vérification de l'existence du symbole sur l'échange
            if symbol in self.exchange.markets:
                market = self.exchange.market(symbol)
                validated_symbol = market['symbol']
                self.pairs.append(validated_symbol)
                self.symbol_to_quote[validated_symbol] = market['quote']
                self.base_assets.append(market['base'])
                self.lots[validated_symbol] = []
                self.last_action_prices[validated_symbol] = None
                self.fees[validated_symbol] = 0.001
                log.info(f"Paire validée : [bold green]{validated_symbol}[/]")
            else:
                log.warning(f"La paire '{symbol}' n'est pas supportée par {self.exchange_id}. Ignorée.")

        if not self.pairs:
            raise Exception("Aucune paire valide n'a été trouvée. Vérifiez votre fichier pairs.txt.")

        # Récupération optionnelle des frais réels
        if self.exchange.apiKey:
            log.info("Récupération des taux de commission réels...")
            for symbol in self.pairs:
                try:
                    fees = await self.exchange.fetch_trading_fee(symbol)
                    self.fees[symbol] = fees.get('taker', 0.001)
                except: pass

            try:
                balance = await self.exchange.fetch_balance()
                self.update_local_balances(balance)
            except Exception as e:
                log.error(f"Erreur lors de la récupération du solde : {e}")

    def update_local_balances(self, balance):
        if 'free' in balance:
            self.balances = {asset: float(amt) for asset, amt in balance['free'].items() if amt > 0}
            relevant = {a: self.balances.get(a, 0) for a in set(self.base_assets + list(self.symbol_to_quote.values()))}
            log.debug(f"Soldes mis à jour : {relevant}")

    async def watch_balance_loop(self):
        if not self.exchange.apiKey:
            log.warning("Mode Simulation : watchBalance désactivé (pas de clés API).")
            return

        log.info("Démarrage du suivi du solde (watchBalance)...")
        while True:
            try:
                balance = await self.exchange.watch_balance()
                self.update_local_balances(balance)
            except Exception as e:
                log.error(f"Erreur watchBalance : {e}")
                await asyncio.sleep(10)

    async def watch_ohlcv_loop(self):
        log.info(f"Démarrage des watchers OHLCV pour {len(self.pairs)} paires...")
        # On lance un watcher indépendant par paire pour une meilleure robustesse
        await asyncio.gather(*[self.single_symbol_watcher(s) for s in self.pairs])

    async def single_symbol_watcher(self, symbol):
        timeframe = '1m'
        log.info(f"Watcher OHLCV actif pour {symbol}")
        while True:
            try:
                # Utilisation de watch_ohlcv (singulier) pour éviter les erreurs de parsing de symboles
                candles = await self.exchange.watch_ohlcv(symbol, timeframe)
                if candles:
                    latest_price = candles[-1][4] # Prix de clôture
                    await self.process_price_update(symbol, latest_price)
            except Exception as e:
                log.error(f"Erreur watcher {symbol} : {e}")
                await asyncio.sleep(5)

    async def process_price_update(self, symbol, current_price):
        last_price = self.last_action_prices.get(symbol)

        if last_price is None:
            self.last_action_prices[symbol] = current_price
            log.info(f"[{symbol}] Prix de référence initial : {current_price}")
            return

        quote_asset = self.symbol_to_quote[symbol]

        # Logique d'ACHAT : le prix baisse par rapport au dernier prix d'action
        if current_price < last_price:
            quote_balance = self.balances.get(quote_asset, 0)

            # Injection de budget fictif en simulation si nécessaire
            if quote_balance == 0 and not self.exchange.apiKey:
                 quote_balance = 1000.0
                 self.balances[quote_asset] = 1000.0
                 log.info(f"Simulation : Attribution d'un budget fictif de 1000 {quote_asset}.")

            amount_to_spend = quote_balance / 10.0

            if amount_to_spend > 0:
                log.info(f"[{symbol}] Baisse détectée ({current_price} < {last_price}). Tentative d'achat...")
                try:
                    raw_qty = amount_to_spend / current_price

                    if self.exchange.apiKey:
                        qty = float(self.exchange.amount_to_precision(symbol, raw_qty))
                        if qty > 0:
                            order = await self.exchange.create_market_buy_order(symbol, qty)
                            price = order.get('price') or current_price
                            final_qty = order.get('amount') or qty
                        else: return
                    else:
                        price, final_qty = current_price, raw_qty
                        self.balances[quote_asset] -= (price * final_qty)

                    self.lots[symbol].append({'amount': final_qty, 'price': price})
                    self.last_action_prices[symbol] = price
                    console.print(f"[bold green][{symbol}] ACHAT à {price} (Total : {price * final_qty:.2f} {quote_asset})[/]")
                except Exception as e:
                    log.error(f"[{symbol}] Échec de l'achat : {e}")

        # Logique de VENTE : le prix monte, on vérifie la rentabilité des lots
        elif current_price > last_price:
            fee = self.fees.get(symbol, 0.001)
            any_sold = False
            remaining_lots = []

            for lot in self.lots[symbol]:
                # Calcul du profit net (frais inclus)
                sell_net = current_price * (1 - fee)
                buy_total = lot['price'] * (1 + fee)
                profit_pct = (sell_net - buy_total) / buy_total

                if profit_pct >= 0.01: # Seuil de 1%
                    log.info(f"[{symbol}] Lot profitable détecté ({profit_pct:.2%}). Vente...")
                    try:
                        if self.exchange.apiKey:
                            qty_to_sell = float(self.exchange.amount_to_precision(symbol, lot['amount']))
                            await self.exchange.create_market_sell_order(symbol, qty_to_sell)
                        else:
                            # Simulation
                            self.balances[quote_asset] += (lot['amount'] * current_price * (1 - fee))

                        any_sold = True
                        console.print(f"[bold red][{symbol}] VENTE à {current_price} (Profit : {profit_pct:.2%})[/]")
                    except Exception as e:
                        log.error(f"[{symbol}] Échec de la vente : {e}")
                        remaining_lots.append(lot)
                else:
                    remaining_lots.append(lot)

            self.lots[symbol] = remaining_lots
            if any_sold:
                self.last_action_prices[symbol] = current_price

    async def stop(self):
        await self.exchange.close()

async def load_config():
    paths = ['api.json', 'api.json.example']
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r') as f: return json.load(f)
    return {}

async def load_pairs():
    if os.path.exists('pairs.txt'):
        with open('pairs.txt', 'r') as f:
            return [line.strip() for line in f if line.strip()]
    return []

async def main():
    config = await load_config()
    pairs_list = await load_pairs()

    if not pairs_list:
        log.error("Fichier pairs.txt vide ou manquant.")
        return

    bot = TradingBot(config, pairs_list)
    try:
        await bot.initialize()
        log.info(f"Bot opérationnel avec {len(bot.pairs)} paires.")
        await asyncio.gather(
            bot.watch_balance_loop(),
            bot.watch_ohlcv_loop()
        )
    finally:
        await bot.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt: pass
    except Exception as e:
        log.exception(f"Erreur fatale : {e}")

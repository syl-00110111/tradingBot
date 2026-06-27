import asyncio
import ccxt.pro as ccxtpro
import json
from typing import Dict, List, Any

# --- Configuration ---
PAIRS_FILE = 'pairs.txt'
API_FILE = 'api.json'
TRADE_FEE = 0.001  # Frais de trading (0.1%)
TIMEFRAME = '1s'   # Timeframe pour les bougies (1 seconde)

# --- Variables globales ---
SYMBOLS: List[str] = []
BASE_ASSETS: set = set()
last_ohlcv: Dict[str, Dict[str, float]] = {}
buy_prices: Dict[str, float] = {}
balances: Dict[str, float] = {}

# --- Fonctions utilitaires ---
def load_api_credentials() -> Dict[str, Any]:
    """Charge les crédentials API depuis api.json."""
    with open(API_FILE, 'r') as f:
        return json.load(f)

def load_pairs() -> None:
    """Charge les paires depuis pairs.txt."""
    global SYMBOLS, BASE_ASSETS
    with open(PAIRS_FILE, 'r') as f:
        SYMBOLS = [line.strip() for line in f if line.strip()]
    BASE_ASSETS = {symbol.split('/')[0] for symbol in SYMBOLS}

def init_exchange() -> ccxtpro.Exchange:
    """Initialise l'exchange avec les crédentials API."""
    credentials = load_api_credentials()
    exchange_class = getattr(ccxtpro, credentials['exchange_id'])
    return exchange_class({
        'apiKey': credentials['api_key'],
        'secret': credentials['api_secret'],
        'enableRateLimit': True,
        'options': {
            **credentials.get('options', {}),
            'adjustForTimeDifference': True
        }
    })

# --- Surveillance des balances (WebSocket uniquement) ---
async def watch_balance(exchange: ccxtpro.Exchange) -> None:
    """Surveille les soldes en temps réel via WebSocket."""
    global balances
    while True:
        try:
            balance_stream = await exchange.watch_balance()
            while True:
                try:
                    balance_update = await balance_stream()
                    if balance_update is None:
                        break
                    balances = balance_update['free']
                    print(f"[BALANCE] {balances}")
                except Exception as e:
                    print(f"[ERROR] Erreur dans watch_balance: {e}")
                    break
        except Exception as e:
            print(f"[ERROR] watch_balance: {e}. Relance dans 5s...")
            await asyncio.sleep(5)

# --- Surveillance OHLCV (WebSocket uniquement) ---
async def watch_ohlcv(exchange: ccxtpro.Exchange, symbol: str) -> None:
    """Surveille les OHLCV en temps réel via WebSocket."""
    global last_ohlcv, buy_prices, balances
    while True:
        try:
            print(f"[OHLCV] Démarrage pour {symbol} ({TIMEFRAME})...")
            ohlcv_stream = await exchange.watch_ohlcv(symbol, timeframe=TIMEFRAME)

            # Gérer le flux WebSocket
            while True:
                try:
                    # Récupérer les données (peut être une liste ou un objet asynchrone)
                    ohlcv_data = ohlcv_stream  # Certains exchanges retournent directement la liste
                    if isinstance(ohlcv_stream, list):
                        await process_ohlcv(symbol, ohlcv_stream)
                        # Attendre la prochaine mise à jour (simuler un flux continu)
                        await asyncio.sleep(1)
                    else:
                        # Cas où c'est un générateur asynchrone
                        ohlcv_update = await ohlcv_stream()
                        if ohlcv_update is None:
                            break
                        await process_ohlcv(symbol, ohlcv_update)
                except Exception as e:
                    print(f"[ERROR] Erreur dans le flux OHLCV pour {symbol}: {e}")
                    break
        except Exception as e:
            print(f"[ERROR] Erreur initiale pour {symbol}: {e}. Relance dans 5s...")
            await asyncio.sleep(5)

async def process_ohlcv(symbol: str, ohlcv_data: List[List[Any]]) -> None:
    """Traite les données OHLCV."""
    global last_ohlcv, buy_prices, balances

    if not ohlcv_data or len(ohlcv_data) == 0:
        return

    last_candle = ohlcv_data[-1]
    if len(last_candle) < 6:
        return

    close_price = float(last_candle[4])
    prev_ohlcv = last_ohlcv.get(symbol, {})
    prev_close = prev_ohlcv.get('close', close_price)

    last_ohlcv[symbol] = {
        'open': float(last_candle[1]),
        'high': float(last_candle[2]),
        'low': float(last_candle[3]),
        'close': close_price,
        'volume': float(last_candle[5])
    }

    base_asset, quote_asset = symbol.split('/')
    base_balance = balances.get(base_asset, 0)
    quote_balance = balances.get(quote_asset, 0)

    # Logique de vente
    if symbol in buy_prices and close_price > prev_close:
        buy_price = buy_prices[symbol]
        sell_threshold = buy_price * (1 + 2 * TRADE_FEE + 0.01)
        if close_price >= sell_threshold and base_balance > 0:
            sell_amount = base_balance
            estimated_receive = sell_amount * close_price * (1 - TRADE_FEE)
            print(f"[SELL] {symbol} | {sell_amount:.8f} {base_asset} @ {close_price:.8f} {quote_asset} (Est. {estimated_receive:.8f})")

    # Logique d'achat
    elif close_price < prev_close and quote_balance > 0:
        amount_to_spend = quote_balance * 0.1
        amount_to_buy = amount_to_spend / close_price * (1 - TRADE_FEE)
        if amount_to_buy > 0:
            print(f"[BUY] {symbol} | {amount_to_buy:.8f} {base_asset} @ {close_price:.8f} {quote_asset} (Spend {amount_to_spend:.8f})")
            buy_prices[symbol] = close_price

# --- Fonction principale ---
async def main() -> None:
    """Fonction principale du bot."""
    load_pairs()
    if not SYMBOLS:
        print("[ERROR] Aucune paire dans pairs.txt")
        return

    exchange = init_exchange()
    print(f"[START] Exchange: {exchange.id} | Paires: {SYMBOLS}")

    # Récupérer les soldes initiaux
    try:
        balance = await exchange.fetch_balance()
        balances = balance['free']
        print(f"[INIT] Soldes initiaux: {balances}")
    except Exception as e:
        print(f"[ERROR] Impossible de récupérer les soldes initiaux: {e}")

    # Lancer les tâches
    tasks = [
        asyncio.create_task(watch_balance(exchange)),
        *[asyncio.create_task(watch_ohlcv(exchange, symbol)) for symbol in SYMBOLS]
    ]

    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[STOP] Bot arrêté par l'utilisateur")
    except Exception as e:
        print(f"[FATAL] {e}")
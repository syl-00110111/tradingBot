import ccxt
import logging

# Activez les logs
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Initialisez l'exchange
exchange = ccxt.kraken()

# Testez la requête
try:
    ticker = exchange.fetch_ticker("0G/EUR")
    print(ticker)
except Exception as e:
    logger.error(f"Erreur lors de la récupération du ticker : {e}")

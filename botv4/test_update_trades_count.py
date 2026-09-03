import json
import os
from rich.console import Console
console = Console()
import sys
import time
import ccxt
sourceAssets = []
balance = None
miniCount = 400
baseAssets = ["USD", "EUR", "BTC"]
forbidAssets = ['AKE', 'ALLO', 'USDT', 'WEMIX', 'XMR']
availablePairs = []

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

_exchange = loadExchange()
_markets = loadMarkets(_exchange)

def updateTradingCount(symbol, _id, exchange):
    _vol = {'symbol': symbol, 'id': _id, 'trades_count': 0, 'timestamp': 0}
    # compute 'since' timestamp for last 4 heures
    since_4h = int(time.time()) * 1000 - (4*3600*1000)
    # fetch trades only from the last 4 heures using 'since'
    time.sleep(exchange.rateLimit / 1000)
    trades = exchange.fetch_trades(symbol, since_4h)
    trades_count = len(trades) if trades is not None else 0
    console.print(f"New fetched trades count (last 4h): {trades_count}")
    # mettre à jour avec le nouveau volume
    _vol['trades_count'] = trades_count
    _vol['timestamp'] = int(time.time()) * 1000
    return _vol

_volumes = []

for _market in _markets.items():
    console.print(f"Updating trades count for {_market[1].get('symbol')}... {_market[1].get('id')}...")
    _volumes.append(updateTradingCount(_market[1].get('symbol'), _market[1].get('id'), _exchange))

with open("volumes_trades_data.json", 'w') as f:
    json.dump(_volumes, f, indent=4)

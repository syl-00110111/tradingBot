import json
import os
from rich.console import Console
console = Console()
import sys
import time
import ccxt
sourceAssets = []
balance = None
miniCount = 800
baseAssets = ["USD"]
forbidAssets = ['USDT']
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

def fetch_balance(exchange):
    balance = exchange.fetch_balance()
    balance['timestamp'] = int(time.time())
    try:
        with open("balance.json", 'w') as f: json.dump(balance, f, indent=4)
    except Exception as e:
        console.print(f"Balance backup file exception: {e}")
    return balance

exchange = loadExchange()
balance = fetch_balance(exchange)

def updateTradingCount(symbol):
    try:
        with open('volumes_trades_data.json','r') as f: _volumes = json.load(f)
    except Exception as e:
        console.print(f"volume trades data file problem: {e}")
    trades_count = 0
    for _vol in _volumes:
        if symbol == _vol.get('symbol'):
            # compute 'since' timestamp for last 4 heures
            since_4h = int(time.time()) * 1000 - (4*3600*1000)
            # fetch trades only from the last 4 heures using 'since'
            time.sleep(exchange.rateLimit / 1000)
            trades = exchange.fetch_trades(symbol, since_4h)
            trades_count = len(trades) if trades is not None else 0
            console.print(f"Old trades count for {symbol}: {_vol['trades_count']}")
            console.print(f"New fetched trades count (last 4h) for {symbol}: {trades_count}")
            # mettre à jour avec le nouveau volume
            _vol['trades_count'] = trades_count
            break
    try:
        with open('volumes_trades_data.json', 'w') as f:
            json.dump(_volumes, f, indent=4)
        console.print(f"Fichier volumes_trades_data.json mis à jour pour le symbole {symbol}.")
    except Exception as e:
        console.print(f"Impossible de mettre à jour le fichier volumes_trades_data.json: {e} pour le symbole {symbol}")

def computeSymbols():
    __symbols = []
    # balance existante
    for _balance in balance.get('free').items():
        if float(_balance[1]) > 0:
            sourceAssets.append(_balance[0])
            #console.print(f"source asset: {_balance[0]} {_balance[1]}")
    try:
        with open('markets.json','r') as f: _markets = json.load(f)
        with open('volumes_trades_data.json','r') as f: _volumes = json.load(f)
        _g = {'id':[]}
        for _v in _volumes:
            if _v.get('trades_count') > miniCount:
                # tri du volume à part
                _g['id'].append(_v.get('id'))
        _a = []
        for _m in _markets.items():
            _a = [_m[1].get('symbol'), _m[1].get('id'), _m[1].get('base'), _m[1].get('quote'), _m[1].get('limits').get('amount').get('min'), _m[1].get('precision').get('price'), _m[1].get('precision').get('amount')]
            # si pas interdit dans notre zone
            if (_m[1].get('base') not in forbidAssets) and (_m[1].get('quote') not in forbidAssets):
                # et que l'identifiant se trouve dans le groupe des volumes importants mais tout en restant dans les assets gérées (pour restreindre)
                if (_m[1].get('id') in _g.get('id')) and (_m[1].get('quote') in baseAssets):
                    __symbols.append(_a)
                    #console.print(f"volume add: {_m[1].get('symbol')}")
                # ou bien si la base comme la quote sont dans les assets gérées (sourceAssets à vendre puisqu'il s'agit de la balance disponible, et baseAssets monnaies d'usage pour acheter)
                elif (_m[1].get('base') in sourceAssets) and (_m[1].get('quote') in baseAssets):
                    __symbols.append(_a)
                    #console.print(f"source asset add: {_m[1].get('symbol')}")
        return __symbols
    except Exception as e:
        console.print(f"Exception {e}")

updateTradingCount("1INCH/EUR")
availablePairs = computeSymbols()
console.print(f"computed symbols: {availablePairs}")

# init start
from rich.console import Console
console = Console()

with console.status("Bot init. Please wait or expect an random error.", spinner="dots"):

    import ccxt
    import os
    import sys
    import json
    import time
    import math

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
            markets = exchange.load_markets()
        except Exception as e:
            console.print(f"Markets fetch exception: {e}")
        try:
            with open(file, 'w') as f: json.dump(markets, f, indent=4)
        except Exception as e:
            console.print(f"Markets backup file exception: {e}")
        return markets

    def readMarkets(file):
        try:
            with open(file, 'r') as f: markets = json.load(f)
            return markets
        except Exception as e:
            console.print(f"Error loading markets.json file: {e}")

    # balance part
    def readBalances(balanceContent):
        result = balanceContent.get('info').get('result')
        assets = []
        for asset in result:
            assets.append(asset)
        _return = []
        for asset in assets:
            balance = result.get(asset).get('balance')
            hold_trade = result.get(asset).get('hold_trade')
            console.print(f"Asset: {asset}, balance: {balance}, hold for trades: {hold_trade}")
            _ret = [asset, balance]
            _return.append(_ret)
        return _return

    def saveBalance(exchange, file, timestamp):
        try:
            time.sleep(exchange.rateLimit / 1000)
            balance = exchange.fetch_balance()
        except Exception as e:
            console.print(f"Balance fetch exception: {e}")
        balance['timestamp'] = int(time.time())
        try:
            with open(file, 'w') as f: json.dump(balance, f, indent=4)
        except Exception as e:
            console.print(f"Balance backup file exception: {e}")
        return balance

    def compareStamp(file):
        _ret = False
        with open(file, 'r') as f:
            balance = json.load(f)
            timestamp = balance.get('timestamp')
            if timestamp is None:
                console.print(f"Balance backup file présent mais sans timestamp.")
            else:
                diff = int(time.time()) - timestamp
                if diff < 3600:
                    _ret = True
                    console.print(f"Balance: timestamp récent de {diff} secondes détecté.")
        return _ret

    balance_file = 'balance.json'
    recentBalance = False
    balance = None
    exchange = loadExchange()
    if os.path.exists(balance_file):
        recentBalance = compareStamp(balance_file)
    if recentBalance == False:
        balance = readBalances(saveBalance(exchange, balance_file, time.time()))
    else:
        with open(balance_file, 'r') as f: balance = json.load(f)
        balance = readBalances(balance)

    # trades part
    def readTrades(file, pair):
        trades = None
        with open(file, 'r') as f: trades_content = json.load(f)
        trades = []
        for item in trades_content:
            trades.append(item)
        _return = []
        for trade in trades:
            timestamp = trade.get('timestamp')
            symbol = trade.get('symbol')
            info = trade.get('info')
            pair = info.get('pair')
            _type = info.get('type')
            ordertype = info.get('ordertype')
            fee = info.get('fee')
            price = info.get('price')
            volume = info.get('vol')
            ret = [timestamp, symbol, pair, _type, ordertype, fee, price, volume]
            _return.append(ret)
        return _return

    def saveTrades(file, pair, since):
        try:
            time.sleep(exchange.rateLimit / 1000)
            trades = exchange.fetch_my_trades(pair, since)
        except Exception as e:
            console.print(f"Trades fetch exception: {e}")
        try:
            with open(file, 'w') as f: json.dump(trades, f, indent=4)
        except Exception as e:
            console.print(f"Trades backup file exception: {e}")
        return readTrades(file, pair)


    # fetch trades pour obtenir prix de revente acceptable sur chaque achat+fee -> prix avec 1%
    # balance - buys + fees + 1%
    # si balance contient un ou plusieurs trades
    # balance = 43, prix = 5, fee = 0.05, trade 1 = achat 21*4.58, trade 2 = achat 22=5.05
    # balance 43 (215) trade 1 (96.23) - trade 2 (111.25)

    # TODO manque des trades; faire un ratio balance - trades dispos % 2%

    balances = balance
    assets = []
    for _balance in balances:
        if float(_balance[1]) > 0:
            assets.append(_balance[0])

    # markets precision
    markets_file = "markets.json"
    if os.path.exists(markets_file):
        _markets = readMarkets(markets_file)
    else:
        _markets = loadMarkets(exchange, markets_file)

    # fetch paires correspondant aux assets ayant une balance
    pairs = []
    for _market in _markets.items():
        amountPrecision = _market[1].get('precision').get('amount')
        pricePrecision = _market[1].get('precision').get('price')
        base = _market[1].get('info').get('base')
        quote = _market[1].get('info').get('quote')
        _id = _market[1].get('id')
        if base in assets and quote in assets:
            if _id not in pairs:
                pairs.append([_id, base, quote, amountPrecision, pricePrecision])
    
    for pair in pairs:
        trades_file = 'trades_' + pair[0] + '.json'
        trades = None
        if os.path.exists(trades_file):
            trades = readTrades(trades_file, pair[0])
        if trades is None:
            trades = saveTrades(trades_file, pair[0], int(time.time()*1000)-(3600*24*12*1000))
        amountPrecision = pair[3]
        pricePrecision = pair[4]
        types = []
        b = 0.0
        b_s = 0.0
        b_b = 0.0
        len_trades = 0
        for trade in trades:
            if trade[2] == pair[0]:
                # timestamp, symbol, pair, _type, ordertype, fee, price, volume
                #console.print(trade[2]) # pair (XXBTZEUR)
                #console.print(trade[3]) # buy or sell trade
                #console.print(trade[5]) # fee
                #console.print(trade[6]) # prix
                #console.print(trade[7]) # volume
                _type = trade[3]
                fee = float(trade[5])
                prix = float(trade[6])
                volume = float(trade[7])
                total = fee + (prix * volume)
                if _type == "buy":
                    b += total
                    b_b += total
                    len_trades += 1
                elif _type == "sell":
                    b -= total
                    b_s += total
                    len_trades += 1
                else:
                    console.print(f"Incohérence sur un type d'ordre.")
        # arrondi peut-être à l'inférieur
        b = round(b, int(-math.log10(pricePrecision)))
        console.print(f"Balance from trades available for {pair[0]}: {b} sur {len_trades} trades")

        # caulcu diff balance - trades
        #balance - trades

        # la balance des trades
        b_b = round(b_b, int(-math.log10(pricePrecision)))
        b_s = round(b_s, int(-math.log10(pricePrecision)))

        # si balance trade négative mais balance poussière -> rien

        # si balance trade nulle mais balance positive -> prix moyen depuis 01/07

        # si balance trade négative mais balance positive -> prix moyen pour le reste

        for balance in balances:
            if balance[0] in pair[1]:
                console.print(balance[0])
                console.print(balance[1])

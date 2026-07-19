            for _m in _markets:
                if _m in availablePairs:
                    random_choice = random.choice(availablePairs)
                    # skip if buys are paused for this symbol
                    now_ts = int(time.time())
                    symbol = random_choice[0]
                    expiry = pausedForBuy.get(symbol)
                    len_paused = len(pausedForBuy)
                    # console.print(f"len_paused: {len_paused}, expiry: {expiry}")
                    console.print(f"Test 2: len_paused: {len_paused}, expiry: {expiry}")
                    if expiry and now_ts > int(expiry):
                        console.print(f"{symbol} paused until {datetime.fromtimestamp(int(expiry))}")
                    else:
                        if updateTradingCount(symbol) < miniCount:
                            try:
                                pausedForBuy[symbol] = expiry
                                with open(PAUSE_FILE, 'w') as f: json.dump(pausedForBuy, f)
                            except Exception as e:
                                console.print(f"Failed to persist pausedForBuy: {e}")
                        # console.print(f"maxNumPairs: {maxNumPairs}, len(availablePairs)-1: {len(availablePairs)-1}")
                        if maxNumPairs > len(availablePairs)-1:
                            _m = random.choice(_markets)
                            # symbol, id, base, quote, amount, price_precision, amount_precision
                            _a = [_m[1].get('symbol'), _m[1].get('id'), _m[1].get('base'), _m[1].get('quote'), _m[1].get('amount'), _m[1].get('price_precision'), _m[1].get('amount_precision')]
                        
                            with open('volumes_trades_data.json','r') as f: _volumes = json.load(f)
                            _g = {'symbol':[]}
                            for _v in _volumes:
                                console.print(f"Test update from volume timestamp: {_v.get('timestamp')} < {now_ts+(4*3600)} ?")
                                if _v.get('timestamp') < now_ts+(4*3600) and _v.get('trades_count') > miniCount:
                                    _g['symbol'].append(_v.get('symbol'))

                            for __g in _g:
                                if updateTradingCount(__g[0])> miniCount and _a not in availablePairs:
                                    console.print(f"test add: {_a[0]} {miniCount}")
                                    availablePairs.append(_a)
                                    break

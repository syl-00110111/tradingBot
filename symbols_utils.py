"""
Symbol and trading-count utilities extracted from botv4.py
Provides:
- updateTradingCount(symbol, exchange, volumes_file='volumes_trades_data.json') -> int
- computeSymbols(balance, previousPairs=None, markets_file='markets.json', volumes_file='volumes_trades_data.json', forbidAssets=None, baseAssets=None, maxNumPairs=50, miniCount=600)

These functions use market_utils for logging, rate limit and JSON helpers.
"""
from typing import Any, List, Dict, Optional
import json
import os
import random
import time
import safe_json

import market_utils

def updateTradingCount(symbol):
    try:
        with open('volumes_trades_data.json','r') as f: _volumes = json.load(f)
    except Exception as e:
        raise ValueError(f"volume trades data file problem: {e}")
    trades_count = 0
    for _vol in _volumes:
        if symbol == _vol.get('symbol') and symbol is not None:
            _since = _vol.get('timestamp')
            # normalize _since to an integer timestamp (ms);
            # if missing/invalid, consider it as current time (so we won't fetch historical trades)
            now_minus_4h = int(time.time()) * 1000 - (4*3600*1000)
            if _since is None:
                _since_int = now_minus_4h
            else:
                _since_int = int(_since)
            # if now < since by 4 hours
            if (_since_int >= now_minus_4h) or _vol.get('trades_count') == 1000:
                #console.print(f"_since: {_since_int}, since_4h: {now_minus_4h}, int(time.time()): {int(time.time())}")
                time.sleep(exchange.rateLimit / 1000)
                trades = exchange.fetch_trades(symbol, now_minus_4h)
                trades_count = len(trades) if trades is not None else 0
                # console.print(f"Old trades count for {symbol}: {_vol['trades_count']}")
                console.print(f"New fetched trades count (last 4h) for {symbol}: {trades_count}")
                # mettre à jour avec le nouveau volume
                _vol['trades_count'] = trades_count
                _vol['timestamp'] = int(time.time())
                break
    try:
        try:
            safe_json.atomic_write_json('volumes_trades_data.json', _volumes, backup=True, indent=4)
        except Exception:
            with open('volumes_trades_data.json', 'w') as f:
                json.dump(_volumes, f, indent=4)
        # console.print(f"Fichier volumes_trades_data.json mis à jour pour le symbole {symbol}.")
    except Exception as e:
        console.print(f"Impossible de mettre à jour le fichier volumes_trades_data.json: {e} pour le symbole {symbol}")
    return trades_count

def computeSymbols(balance, previousPairs):
    __symbols = []
    # balance existante
    # # Vérifier que 'balance' est un dictionnaire valide
    if not isinstance(balance, dict):
        console.print("[ERROR] La structure 'balance' est invalide.")
    elif 'free' not in balance:
        console.print("[ERROR] La clé 'free' est manquante dans 'balance'.")
    else:
        free_balances = balance.get('free')
        if not isinstance(free_balances, dict):
            console.print("[ERROR] La clé 'free' ne contient pas un dictionnaire valide.")
        else:
            for asset, amount in free_balances.items():
                try:
                    # Convertir le montant en float
                    amount_float = float(amount)
                    # Ajouter l'actif à la liste si le montant est supérieur à 0
                    if amount_float > 0:
                        sourceAssets.append(asset)
                        # console.print(f"source asset: {asset} {amount_float}")
                except (ValueError, TypeError) as e:
                    console.print(f"[WARNING] Impossible de convertir le montant pour l'actif '{asset}' : {e}")
    try:
        with open('markets.json','r') as f: _markets = json.load(f)
        with open('volumes_trades_data.json','r') as f: _volumes = json.load(f)
        # build a set of existing symbols (normalized) to compare reliably
        try:
            existing_symbols = {str(p[0]).upper() for p in previousPairs if isinstance(p, (list, tuple)) and len(p) > 0 and p[0] is not None}
        except Exception:
            existing_symbols = set()
        _g = {'id':[]}
        for _v in _volumes:
            if _v.get('trades_count') > miniCount:
                # tri du volume à part
                _g['id'].append(_v.get('id'))
        # construire deux listes distinctes : prioriser les paires à vendre (base dans sourceAssets), puis les paires volume
        sell_candidates = []
        volume_candidates = []
        _a = []
        for _m in _markets.items():
            _a = [_m[1].get('symbol'), _m[1].get('id'), _m[1].get('base'), _m[1].get('quote'), _m[1].get('limits').get('amount').get('min'), _m[1].get('precision').get('price'), _m[1].get('precision').get('amount')]
            # si pas interdit dans notre zone
            if (_m[1].get('base') not in forbidAssets) and (_m[1].get('quote') not in forbidAssets):
                # paire présente dans les volumes importants et quote dans monnaies d'usage
                if (_m[1].get('id') in _g.get('id')) and (_m[1].get('quote') in baseAssets):
                    # si la base est dans la balance -> priorité vente
                    if (_m[1].get('base') in sourceAssets) and (str(_a[0]).upper() not in existing_symbols):
                        sell_candidates.append(_a)
                        existing_symbols.add(str(_a[0]).upper())
                        console.print(f"balance add: {_m[1].get('symbol')}")
                    # sinon, c'est une paire volume
                    elif (str(_a[0]).upper() not in existing_symbols):
                        volume_candidates.append(_a)
                        existing_symbols.add(str(_a[0]).upper())
                        console.print(f"volume add: {_a[0]}")
        # combiner en respectant maxNumPairs : priorité aux ventes
        combined = []
        # ajouter d'abord les ventes
        for item in sell_candidates:
            if len(combined) >= maxNumPairs:
                break
            combined.append(item)
        # compléter avec les paires volume si besoin
        for item in volume_candidates:
            if len(combined) >= maxNumPairs:
                break
            combined.append(item)
        __symbols.extend(combined)
    except Exception as e:
        console.print(f"Exception computeSymbols: {e}")
    return __symbols

"""
Symbol and trading-count utilities extracted from botv4.py
Provides:
- updateTradingCount(symbol, exchange, console=None, volumes_file='volumes_trades_data.json') -> int
- computeSymbols(balance, previousPairs=None, source_assets=None, forbid_assets=None, base_assets=None, max_num_pairs=50, mini_count=600, markets_file='markets.json', volumes_file='volumes_trades_data.json', console=None)
"""
from typing import Any, List, Dict, Optional
import json
import os
import time
import safe_json


def updateTradingCount(symbol: str, exchange: Any, console: Optional[Any] = None, volumes_file: str = 'volumes_trades_data.json') -> int:
    try:
        with open(volumes_file, 'r') as f:
            _volumes = json.load(f)
    except Exception as e:
        raise ValueError(f"volume trades data file problem: {e}")

    trades_count = 0
    for _vol in _volumes:
        if symbol == _vol.get('symbol') and symbol is not None:
            _since = _vol.get('timestamp')
            # normalize _since to an integer timestamp (ms);
            # if missing/invalid, consider it as current time (so we won't fetch historical trades)
            now_minus_4h = int(time.time()) * 1000 - (4 * 3600 * 1000)
            if _since is None:
                _since_int = now_minus_4h
            else:
                _since_int = int(_since)
            # if now < since by 4 hours
            if (_since_int >= now_minus_4h) or _vol.get('trades_count') == 1000:
                rate_limit_ms = getattr(exchange, 'rateLimit', 1000) or 1000
                time.sleep(rate_limit_ms / 1000)
                trades = exchange.fetch_trades(symbol, now_minus_4h)
                trades_count = len(trades) if trades is not None else 0
                msg = f"New fetched trades count (last 4h) for {symbol}: {trades_count}"
                if console:
                    console.print(msg)
                else:
                    print(msg)
                # mettre à jour avec le nouveau volume
                _vol['trades_count'] = trades_count
                _vol['timestamp'] = int(time.time())
                break
    try:
        try:
            safe_json.atomic_write_json(volumes_file, _volumes, backup=True, indent=4)
        except Exception:
            with open(volumes_file, 'w') as f:
                json.dump(_volumes, f, indent=4)
    except Exception as e:
        msg_err = f"Impossible de mettre à jour le fichier {volumes_file}: {e} pour le symbole {symbol}"
        if console:
            console.print(msg_err)
        else:
            print(msg_err)
    return trades_count


def computeSymbols(
    balance: Dict[str, Any],
    previousPairs: Optional[List[Any]] = None,
    source_assets: Optional[List[str]] = None,
    forbid_assets: Optional[List[str]] = None,
    base_assets: Optional[List[str]] = None,
    max_num_pairs: int = 50,
    mini_count: int = 600,
    markets_file: str = 'markets.json',
    volumes_file: str = 'volumes_trades_data.json',
    console: Optional[Any] = None
) -> List[Any]:
    if source_assets is None:
        source_assets = []
    if forbid_assets is None:
        forbid_assets = ['USDT']
    if base_assets is None:
        base_assets = ["USD", "EUR"]

    __symbols = []
    # balance existante
    # # Vérifier que 'balance' est un dictionnaire valide
    if not isinstance(balance, dict):
        msg_err = "[ERROR] La structure 'balance' est invalide."
        if console:
            console.print(msg_err)
        else:
            print(msg_err)
    elif 'free' not in balance:
        msg_err = "[ERROR] La clé 'free' est manquante dans 'balance'."
        if console:
            console.print(msg_err)
        else:
            print(msg_err)
    else:
        free_balances = balance.get('free')
        if not isinstance(free_balances, dict):
            msg_err = "[ERROR] La clé 'free' ne contient pas un dictionnaire valide."
            if console:
                console.print(msg_err)
            else:
                print(msg_err)
        else:
            for asset, amount in free_balances.items():
                try:
                    # Convertir le montant en float
                    amount_float = float(amount)
                    # Ajouter l'actif à la liste si le montant est supérieur à 0
                    if amount_float > 0:
                        if asset not in source_assets:
                            source_assets.append(asset)
                except (ValueError, TypeError) as e:
                    msg_warn = f"[WARNING] Impossible de convertir le montant pour l'actif '{asset}' : {e}"
                    if console:
                        console.print(msg_warn)
                    else:
                        print(msg_warn)
    try:
        with open(markets_file, 'r') as f:
            _markets = json.load(f)
        with open(volumes_file, 'r') as f:
            _volumes = json.load(f)

        # build a set of existing symbols (normalized) to compare reliably
        try:
            if previousPairs:
                existing_symbols = {str(p[0]).upper() for p in previousPairs if isinstance(p, (list, tuple)) and len(p) > 0 and p[0] is not None}
            else:
                existing_symbols = set()
        except Exception:
            existing_symbols = set()

        _g = {'id': []}
        for _v in _volumes:
            if _v.get('trades_count', 0) > mini_count:
                # tri du volume à part
                _g['id'].append(_v.get('id'))

        # construire deux listes distinctes : prioriser les paires à vendre (base dans source_assets), puis les paires volume
        sell_candidates = []
        volume_candidates = []
        for _m in _markets.items():
            _a = [
                _m[1].get('symbol'),
                _m[1].get('id'),
                _m[1].get('base'),
                _m[1].get('quote'),
                _m[1].get('limits', {}).get('amount', {}).get('min'),
                _m[1].get('precision', {}).get('price'),
                _m[1].get('precision', {}).get('amount')
            ]
            # si pas interdit dans notre zone
            if (_m[1].get('base') not in forbid_assets) and (_m[1].get('quote') not in forbid_assets):
                # paire présente dans les volumes importants et quote dans monnaies d'usage
                if (_m[1].get('id') in _g.get('id')) and (_m[1].get('quote') in base_assets):
                    # si la base est dans la balance -> priorité vente
                    if (_m[1].get('base') in source_assets) and (str(_a[0]).upper() not in existing_symbols):
                        sell_candidates.append(_a)
                        existing_symbols.add(str(_a[0]).upper())
                        msg_add = f"balance add: {_m[1].get('symbol')}"
                        if console:
                            console.print(msg_add)
                        else:
                            print(msg_add)
                    # sinon, c'est une paire volume
                    elif str(_a[0]).upper() not in existing_symbols:
                        volume_candidates.append(_a)
                        existing_symbols.add(str(_a[0]).upper())
                        msg_add2 = f"volume add: {_a[0]}"
                        if console:
                            console.print(msg_add2)
                        else:
                            print(msg_add2)

        # combiner en respectant max_num_pairs : priorité aux ventes
        combined = []
        # ajouter d'abord les ventes
        for item in sell_candidates:
            if len(combined) >= max_num_pairs:
                break
            combined.append(item)
        # compléter avec les paires volume si besoin
        for item in volume_candidates:
            if len(combined) >= max_num_pairs:
                break
            combined.append(item)
        __symbols.extend(combined)
    except Exception as e:
        msg_exc = f"Exception computeSymbols: {e}"
        if console:
            console.print(msg_exc)
        else:
            print(msg_exc)
    return __symbols

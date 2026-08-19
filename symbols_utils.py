"""
Symbol and trading-count utilities extracted from botv4.py
Provides:
- get_only_optimal(exchange, symbol, config=None, volumes_file='volumes_trades_data.json') -> tuple
- updateTradingCount(symbol, exchange, console=None, volumes_file='volumes_trades_data.json', config=None) -> float
- computeSymbols(balance, previousPairs=None, source_assets=None, forbid_assets=None, base_assets=None, max_num_pairs=50, mini_count=600, markets_file='markets.json', volumes_file='volumes_trades_data.json', console=None, exchange=None, config=None) -> List[Any]
"""
from typing import Any, List, Dict, Optional
import json
import os
import time
import inspect
import logging
import safe_json


def fetch_symbol_characteristics(exchange: Any, symbol: str) -> dict:
    """Helper to fetch ticker, ohlcv, trades and calculate characteristics."""
    def _exec_call(fn, *args, **kwargs):
        res = fn(*args, **kwargs)
        if inspect.isawaitable(res):
            try:
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        import nest_asyncio
                        nest_asyncio.apply()
                        return loop.run_until_complete(res)
                    else:
                        return loop.run_until_complete(res)
                except Exception:
                    return asyncio.run(res)
            except Exception:
                return res
        return res

    ticker = _exec_call(exchange.fetch_ticker, symbol)
    if not ticker:
        raise ValueError("Ticker returned None")

    ohlcv = _exec_call(exchange.fetch_ohlcv, symbol, '1h', limit=60)
    trades = _exec_call(exchange.fetch_trades, symbol, limit=1000)

    # 1. Volume 48h
    volume_48h = ticker.get('quoteVolume', 0) or ticker.get('baseVolume', 0) * ticker.get('last', 1)

    # 2. Spread
    spread_pct = 0.5
    if ticker.get('ask') and ticker.get('bid') and ticker['bid'] > 0:
        spread = ticker['ask'] - ticker['bid']
        spread_pct = (spread / ticker['bid']) * 100

    # 3. Volatility
    volatility_pct = 0.05
    if ohlcv is not None and len(ohlcv) > 0:
        closes = [candle[4] for candle in ohlcv]
        min_close = min(closes)
        volatility_pct = (max(closes) - min_close) / max(min_close, 1e-9)

    # 4. Trades per minute
    if trades:
        times = [t['timestamp'] for t in trades if isinstance(t, dict) and 'timestamp' in t]
        if times:
            duration_mins = (max(times) - min(times)) / 60000
            trades_per_min = len(trades) / duration_mins if duration_mins > 0 else 0
        else:
            trades_per_min = len(trades)
    else:
        trades_per_min = 0

    return {
        'volume_48h': volume_48h,
        'spread_pct': spread_pct,
        'volatility_pct': volatility_pct,
        'trades_per_minute': trades_per_min
    }


def evaluate_scoring(chars: dict, config: Optional[dict] = None) -> tuple:
    if config is None:
        config = {}
    thresholds = config.get('timeframe_thresholds', {})

    volume_48h = chars.get('volume_48h', 0)
    spread_pct = chars.get('spread_pct', 0.5)
    volatility_pct = chars.get('volatility_pct', 0.05)
    trades_per_min = chars.get('trades_per_minute', 0)

    vol_low = thresholds.get('volume_48h', {}).get('low', 1000)
    vol_high = thresholds.get('volume_48h', {}).get('high', 120000)

    spr_low = thresholds.get('spread_pct', {}).get('low', 0.001)
    spr_high = thresholds.get('spread_pct', {}).get('high', 0.04)

    vlt_low = thresholds.get('volatility_pct', {}).get('low', 0.01)
    vlt_high = thresholds.get('volatility_pct', {}).get('high', 0.1)

    tpm_low = thresholds.get('trades_per_minute', {}).get('low', 1)
    tpm_high = thresholds.get('trades_per_minute', {}).get('high', 40)

    score = 0
    reasons = []

    if volume_48h > vol_high:
        score += 1
        reasons.append("High Vol")
    elif volume_48h < vol_low:
        score -= 1
        reasons.append("Low Vol")

    if spread_pct < spr_low:
        score += 1
        reasons.append("Tight Spread")
    elif spread_pct > spr_high:
        score -= 1
        reasons.append("Wide Spread")

    if volatility_pct < vlt_low:
        score += 1
        reasons.append("Stable")
    elif volatility_pct > vlt_high:
        score -= 1
        reasons.append("Volatile")

    if trades_per_min > tpm_high:
        score += 1
        reasons.append("Active")
    elif trades_per_min < tpm_low:
        score -= 1
        reasons.append("Inactive")

    if score > 0:
        return True, reasons
    else:
        return False, reasons


def get_only_optimal(
    exchange: Any,
    symbol: str,
    config: Optional[Dict[str, Any]] = None,
    volumes_file: str = 'volumes_trades_data.json'
) -> tuple:
    """
    Dynamically determines the optimal pair.

    The decision is based on 48h volume, spread, volatility, and trades per minute,
    comparing them against thresholds defined.

    Utilizes 4-hour caching guard logic from volumes_trades_data.json if present.
    """
    if config is None:
        config = {}

    now_sec = int(time.time())
    now_minus_4h = now_sec - (4 * 3600)

    # Check 4-hour cached result in volumes_trades_data.json
    try:
        if os.path.exists(volumes_file):
            with open(volumes_file, 'r') as f:
                _volumes = json.load(f)
            for _v in _volumes:
                if isinstance(_v, dict) and _v.get('symbol') == symbol:
                    _ts = _v.get('timestamp')
                    if _ts is not None and int(_ts) > now_minus_4h:
                        if 'volume_48h' in _v and 'spread_pct' in _v and 'volatility_pct' in _v and 'trades_per_minute' in _v:
                            return evaluate_scoring(_v, config)
    except Exception as e:
        logging.warning(f"Failed to read volumes cache for {symbol}: {e}")

    try:
        if exchange is None:
            return True, ["No exchange provided"]

        chars = fetch_symbol_characteristics(exchange, symbol)

        # Update cache file with fresh characteristics
        try:
            _volumes = []
            if os.path.exists(volumes_file):
                try:
                    with open(volumes_file, 'r') as f:
                        _volumes = json.load(f)
                except Exception:
                    _volumes = []

            found = False
            for _v in _volumes:
                if isinstance(_v, dict) and _v.get('symbol') == symbol:
                    _v['timestamp'] = now_sec
                    _v['volume_48h'] = chars['volume_48h']
                    _v['spread_pct'] = chars['spread_pct']
                    _v['volatility_pct'] = chars['volatility_pct']
                    _v['trades_per_minute'] = chars['trades_per_minute']
                    _v.pop('trades_count', None)
                    found = True
                    break

            if not found:
                _volumes.append({
                    'symbol': symbol,
                    'id': symbol.replace('/', ''),
                    'timestamp': now_sec,
                    'volume_48h': chars['volume_48h'],
                    'spread_pct': chars['spread_pct'],
                    'volatility_pct': chars['volatility_pct'],
                    'trades_per_minute': chars['trades_per_minute']
                })

            try:
                safe_json.atomic_write_json(volumes_file, _volumes, backup=True, indent=4)
            except Exception:
                with open(volumes_file, 'w') as f:
                    json.dump(_volumes, f, indent=4)
        except Exception as ve:
            logging.warning(f"Failed to save characteristics for {symbol}: {ve}")

        return evaluate_scoring(chars, config)

    except Exception as e:
        err_msg = str(e)
        logging.warning(f"Error on {symbol}: {err_msg}. Defaulting to False.")
        return False, 0


def updateTradingCount(
    symbol: str,
    exchange: Any,
    console: Optional[Any] = None,
    volumes_file: str = 'volumes_trades_data.json',
    config: Optional[Dict[str, Any]] = None
) -> float:
    try:
        with open(volumes_file, 'r') as f:
            _volumes = json.load(f)
    except Exception as e:
        _volumes = []

    trades_per_min = 0.0
    now_sec = int(time.time())
    now_minus_4h = now_sec - (4 * 3600)

    found_vol = None
    for _vol in _volumes:
        if isinstance(_vol, dict) and symbol == _vol.get('symbol'):
            found_vol = _vol
            break

    _since = found_vol.get('timestamp') if found_vol else None
    _since_int = int(_since) if _since is not None else 0

    if found_vol is None or _since_int <= now_minus_4h or found_vol.get('trades_count') == 1000:
        try:
            rate_limit_ms = getattr(exchange, 'rateLimit', 1000) or 1000
            time.sleep(rate_limit_ms / 1000)
            chars = fetch_symbol_characteristics(exchange, symbol)
            trades_per_min = chars['trades_per_minute']

            msg = f"New fetched characteristics (last 48h/trades) for {symbol}: {chars}"
            if console:
                console.print(msg)
            else:
                print(msg)

            if found_vol is not None:
                found_vol['timestamp'] = now_sec
                found_vol['volume_48h'] = chars['volume_48h']
                found_vol['spread_pct'] = chars['spread_pct']
                found_vol['volatility_pct'] = chars['volatility_pct']
                found_vol['trades_per_minute'] = chars['trades_per_minute']
                found_vol.pop('trades_count', None)
            else:
                _volumes.append({
                    'symbol': symbol,
                    'id': symbol.replace('/', ''),
                    'timestamp': now_sec,
                    'volume_48h': chars['volume_48h'],
                    'spread_pct': chars['spread_pct'],
                    'volatility_pct': chars['volatility_pct'],
                    'trades_per_minute': chars['trades_per_minute']
                })
        except Exception as e:
            msg_err = f"Failed to fetch characteristics for {symbol}: {e}"
            if console:
                console.print(msg_err)
            else:
                print(msg_err)
    else:
        trades_per_min = found_vol.get('trades_per_minute', found_vol.get('trades_count', 0))

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

    return trades_per_min


def computeSymbols(
    balance: Dict[str, Any],
    previousPairs: Optional[List[Any]] = None,
    source_assets: Optional[List[str]] = None,
    forbid_assets: Optional[List[str]] = None,
    base_assets: Optional[List[str]] = None,
    max_num_pairs: int = 100,
    mini_count: int = 400,
    markets_file: str = 'markets.json',
    volumes_file: str = 'volumes_trades_data.json',
    console: Optional[Any] = None,
    exchange: Optional[Any] = None,
    config: Optional[Dict[str, Any]] = None
) -> List[Any]:
    if source_assets is None:
        source_assets = []
    if forbid_assets is None:
        forbid_assets = ['AKE', 'ALLO', 'USDT', 'WEMIX', 'XMR']
    if base_assets is None:
        base_assets = ["USD", "EUR", "BTC", "CHF", "GBP", "USDC"]

    __symbols = []
    if not isinstance(balance, dict):
        msg_err = "[ERROR] La structure 'balance' est invalide."
        if console:
            console.print(msg_err)
        else:
            print(msg_err)
    else:
        has_any_balance_key = False
        for b_key in ['free', 'total']:
            b_dict = balance.get(b_key)
            if isinstance(b_dict, dict):
                has_any_balance_key = True
                for asset, amount in b_dict.items():
                    try:
                        amount_float = float(amount)
                        if amount_float > 0:
                            if asset not in source_assets:
                                source_assets.append(asset)
                    except (ValueError, TypeError) as e:
                        msg_warn = f"[WARNING] Impossible de convertir le montant pour l'actif '{asset}' : {e}"
                        if console:
                            console.print(msg_warn)
                        else:
                            print(msg_warn)

        if not has_any_balance_key:
            msg_err = "[ERROR] Les clés 'free' ou 'total' sont manquantes ou invalides dans 'balance'."
            if console:
                console.print(msg_err)
            else:
                print(msg_err)

        for asset in source_assets:
            if asset not in forbid_assets and asset not in base_assets:
                base_assets.append(asset)
    try:
        with open(markets_file, 'r') as f:
            _markets = json.load(f)
        with open(volumes_file, 'r') as f:
            _volumes = json.load(f)

        try:
            if previousPairs:
                existing_symbols = {str(p[0]).upper() for p in previousPairs if isinstance(p, (list, tuple)) and len(p) > 0 and p[0] is not None}
            else:
                existing_symbols = set()
        except Exception:
            existing_symbols = set()

        _g = {'id': []}
        for _v in _volumes:
            if isinstance(_v, dict):
                tpm = _v.get('trades_per_minute')
                if tpm is None:
                    tpm = _v.get('trades_count', 0)
                if tpm >= mini_count:
                    _g['id'].append(_v.get('id'))

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
            if (_m[1].get('base') not in forbid_assets) and (_m[1].get('quote') not in forbid_assets):
                if (_m[1].get('quote') in base_assets):
                    if exchange is not None:
                        is_optimal, reasons = get_only_optimal(exchange, _a[0], config=config, volumes_file=volumes_file)
                        if not is_optimal:
                            msg_opt = f"Skipping non-optimal pair {_a[0]}: {reasons}"
                            if console:
                                console.print(msg_opt)
                            else:
                                print(msg_opt)
                            continue

                    if (_m[1].get('id') in _g.get('id')):
                        volume_candidates.append(_a)
                        existing_symbols.add(str(_a[0]).upper())
                        msg_add2 = f"volume add: {_a[0]}"
                        if console:
                            console.print(msg_add2)
                        else:
                            print(msg_add2)
                    elif (_m[1].get('base') in source_assets):
                        base = _m[1].get('base')
                        min_amount_val = _m[1].get('limits', {}).get('amount', {}).get('min')
                        try:
                            min_amount = float(min_amount_val) if min_amount_val is not None else 0.0
                        except (ValueError, TypeError):
                            min_amount = 0.0

                        base_balance = 0.0
                        if isinstance(balance, dict):
                            free_bal = balance.get('free') or {}
                            total_bal = balance.get('total') or {}
                            try:
                                base_balance = float(free_bal.get(base, 0.0) or total_bal.get(base, 0.0) or 0.0)
                            except (ValueError, TypeError):
                                base_balance = 0.0

                        if base_balance > 0 and base_balance < min_amount:
                            pass
                        else:
                            sell_candidates.append(_a)
                            existing_symbols.add(str(_a[0]).upper())
                            msg_add = f"balance add: {_m[1].get('symbol')}"
                            if console:
                                console.print(msg_add)
                            else:
                                print(msg_add)

        combined = []
        for item in sell_candidates:
            if len(combined) >= max_num_pairs:
                break
            combined.append(item)
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

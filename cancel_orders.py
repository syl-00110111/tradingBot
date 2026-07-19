#!/usr/bin/env python3
"""
Script indépendant pour annuler les ordres ouverts.
Inspiré de botv4.py. Utiliser api.json pour les clés API.

Usage:
  python cancel_orders.py [--symbol SYMBOL] [--dry-run]

Exemples:
  python cancel_orders.py --dry-run
  python cancel_orders.py --symbol BTC/USDT
"""
from rich.console import Console
console = Console()

import ccxt
import time
import json
import sys
import os
import argparse


def load_exchange_from_apifile(path='api.json'):
    if not os.path.exists(path):
        console.print("Fichier api.json introuvable. Créez-le à partir de api.json.example avec vos clés.")
        sys.exit(1)
    try:
        with open(path, 'r') as f:
            api_creds = json.load(f)
    except Exception as e:
        console.print(f"Erreur lecture {path}: {e}")
        sys.exit(1)
    exchange_id = api_creds.get('exchange_id')
    if not exchange_id:
        console.print("exchange_id manquant dans api.json")
        sys.exit(1)
    options = api_creds.get('options', {}) or {}
    defaultType = options.get('defaultType')
    exchange_config = {
        'apiKey': api_creds.get('api_key'),
        'secret': api_creds.get('api_secret'),
        'enableRateLimit': True,
        'options': {'defaultType': defaultType} if defaultType else {}
    }
    try:
        exchange = getattr(ccxt, exchange_id)(exchange_config)
    except Exception as e:
        console.print(f"Erreur initialisation exchange {exchange_id}: {e}")
        sys.exit(1)
    return exchange


def cancel_open_orders(exchange, symbol=None, execute=False, interactive=True, auto_confirm=False):
    try:
        # fetch_open_orders may accept symbol argument on some exchanges
        if symbol:
            open_orders = exchange.fetch_open_orders(symbol)
        else:
            open_orders = exchange.fetch_open_orders()
    except Exception as e:
        console.print(f"Erreur fetch_open_orders: {e}")
        return 0

    if not open_orders:
        console.print("Aucun ordre ouvert trouvé.")
        return 0

    cancelled = 0
    total = len(open_orders)
    mode = 'execute' if execute else 'dry-run'
    console.print(f"{total} ordre(s) ouvert(s) trouvé(s). Mode: {mode}")

    execute_all = False
    for idx, o in enumerate(open_orders, start=1):
        oid = o.get('id') or o.get('orderId')
        osymbol = o.get('symbol')
        status = o.get('status')
        side = o.get('side')
        price = o.get('price')
        amount = o.get('amount')
        if not oid:
            console.print(f"Ignoré ordre sans id: {o}")
            continue
        if symbol and osymbol and osymbol.upper() != symbol.upper():
            continue

        # récupérer le prix courant pour comparaison
        current_price = None
        price_diff_pct = None
        if osymbol:
            try:
                time.sleep(getattr(exchange, 'rateLimit', 200) / 1000)
                tk = exchange.fetch_ticker(osymbol)
                # tenter différentes clefs possibles
                current_price = tk.get('last') if tk.get('last') is not None else tk.get('close')
                if current_price is None and isinstance(tk.get('info'), dict):
                    # fallback: chercher un champ numérique dans info
                    info = tk.get('info')
                    for k, v in info.items():
                        try:
                            fv = float(v)
                            current_price = fv
                            break
                        except Exception:
                            continue
                if current_price is not None and price is not None:
                    try:
                        price_diff_pct = (float(price) - float(current_price)) / float(current_price) * 100.0
                    except Exception:
                        price_diff_pct = None
            except Exception:
                current_price = None
                price_diff_pct = None

        diff_str = ''
        if current_price is not None:
            diff_str = f" current={current_price:.8g}"
            if price_diff_pct is not None:
                sign = '+' if price_diff_pct >= 0 else ''
                diff_str += f" ({sign}{price_diff_pct:.2f}%)"

        console.print(f"[{idx}/{total}] id={oid} symbol={osymbol} side={side} price={price} amount={amount} status={status}{diff_str}")

        # decide action
        if interactive and not execute_all and not auto_confirm and not execute:
            # dry-run interactive default
            prompt = "Action? [s]imuler/[e]xécuter/[k]skip/[a]exécuter tout/[q]quit (défaut s): "
            try:
                ans = input(prompt).strip().lower()
            except (KeyboardInterrupt, EOFError):
                console.print("Interrompu par l'utilisateur.")
                break
            if ans == 'q':
                break
            if ans == 'k':
                continue
            if ans == 'a':
                # ask confirm to actually perform
                confirm = input('Confirmer exécution réelle de tous les ordres restants? (y/N): ').strip().lower()
                if confirm == 'y':
                    execute_all = True
                    execute = True
                else:
                    console.print('Rester en dry-run.')
                    execute_all = False
                    execute = False
                    continue
            if ans == 'e':
                # execute this one only
                do_execute = True
            else:
                # default simulate (s or empty)
                do_execute = False
        else:
            # non-interactive or explicit execute
            do_execute = execute or execute_all or auto_confirm

        if not do_execute:
            console.print(f"Simulé: annulation id={oid}")
            cancelled += 1
            continue

        # perform actual cancel
        try:
            time.sleep(getattr(exchange, 'rateLimit', 200) / 1000)
            try:
                res = exchange.cancel_order(oid, osymbol)
            except TypeError:
                res = exchange.cancel_order(oid)
            console.print(f"Annulé: {res.get('id', oid)}")
            cancelled += 1
        except Exception as e:
            console.print(f"Erreur annulation {oid}: {e}")

    console.print(f"Total (simulés/annulés comptés): {cancelled}")
    return cancelled


def main():
    parser = argparse.ArgumentParser(description='Annuler les ordres ouverts (script indépendant)')
    parser.add_argument('--symbol', help='Filtrer par paire (ex: BTC/USDT)', default=None)
    parser.add_argument('--execute', action='store_true', help='Exécuter réellement les annulations (par défaut dry-run)')
    parser.add_argument('--yes', action='store_true', help='Accepter sans confirmation (utile avec --execute)')
    parser.add_argument('--no-interactive', action='store_true', help="Désactiver l'interaction (utile pour scripts).")
    parser.add_argument('--api', help='Chemin vers api.json (défaut: api.json)', default='api.json')
    args = parser.parse_args()

    exchange = load_exchange_from_apifile(args.api)
    cancel_open_orders(exchange, symbol=args.symbol, execute=args.execute, interactive=not args.no_interactive, auto_confirm=args.yes)


if __name__ == '__main__':
    main()

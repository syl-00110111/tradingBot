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


def cancel_open_orders(exchange, symbol=None, dry_run=False):
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
    console.print(f"{len(open_orders)} ordre(s) ouvert(s) trouvé(s).{' (dry-run)' if dry_run else ''}")
    for o in open_orders:
        oid = o.get('id') or o.get('orderId')
        osymbol = o.get('symbol')
        if not oid:
            console.print(f"Ignoré ordre sans id: {o}")
            continue
        if symbol and osymbol and osymbol.upper() != symbol.upper():
            # Skip if symbol filter provided and doesn't match
            continue
        console.print(f"Annulation: id={oid} symbol={osymbol}")
        if dry_run:
            cancelled += 1
            continue
        try:
            # respect rate limit
            time.sleep(getattr(exchange, 'rateLimit', 200) / 1000)
            # cancel_order signature: id, symbol (optional)
            try:
                res = exchange.cancel_order(oid, osymbol)
            except TypeError:
                # some exch. implementations accept only id
                res = exchange.cancel_order(oid)
            console.print(f"Annulé: {res.get('id', oid)}")
            cancelled += 1
        except Exception as e:
            console.print(f"Erreur annulation {oid}: {e}")

    console.print(f"Total annulés: {cancelled}")
    return cancelled


def main():
    parser = argparse.ArgumentParser(description='Annuler les ordres ouverts (script indépendant)')
    parser.add_argument('--symbol', help='Filtrer par paire (ex: BTC/USDT)', default=None)
    parser.add_argument('--dry-run', action='store_true', help='Ne pas exécuter, afficher seulement')
    parser.add_argument('--api', help='Chemin vers api.json (défaut: api.json)', default='api.json')
    args = parser.parse_args()

    exchange = load_exchange_from_apifile(args.api)
    cancel_open_orders(exchange, symbol=args.symbol, dry_run=args.dry_run)


if __name__ == '__main__':
    main()

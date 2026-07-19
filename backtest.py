# init start
import json
import os
import sys
import time

from rich.console import Console
console = Console()

import argparse
from datetime import datetime
import pandas as pd
import plotext as plt

# Initialisez l'exchange
import ccxt
import pandas as pd
import safe_json

def fetch_ohlcv_data(_id, symbol):
    # console.print(f"Fetching OHLCV data for {symbol}...")
    dataFile = 'ohlcv_data_'+ _id + '_1m' + '.json'
    data2 = []
    # Charger les données existantes si le fichier existe
    if os.path.exists(dataFile):
        with open(dataFile, 'r') as f:
            try:
                data2 = json.load(f)
            except json.JSONDecodeError as e:
                raise ValueError(f"Erreur lors de la lecture du fichier cache des chandelles du symbole {symbol} : {e}")
            try:
                lastTimestamp = int(data2[-1][0])  # Utilisation de [-1] pour le dernier élément
            except (IndexError, TypeError, ValueError) as e:
                raise ValueError(f"Le dernier élément du fichier n'est pas un timestamp valide : {e}")
        # ohlcv: [ [ts, open, high, low, close, volume], ... ]
        return pd.DataFrame(data2, columns=['timestamp','open','high','low','close','volume'])

candles_per_pair = {}
df_candles = None

def main(symbol: str, _id: str):
    import torch
    # Hardware Acceleration Detection
    device = None
    try:
        candles_per_pair[symbol] = fetch_ohlcv_data(_id, symbol)
    except Exception as e:
        console.print(f"Failed to fetch OHLCV for {symbol}: {e}")
    df_candles = candles_per_pair.get(symbol)

    if torch.cuda.is_available():
        device = torch.device('cuda')
    elif torch.backends.mkldnn.is_available():
        device = torch.device('cpu')
        torch.backends.mkldnn.enabled = True
        console.print("MKL-DNN is available and enabled.")
    elif hasattr(torch, 'vulkan') and torch.vulkan.is_available():
        device = torch.device('vulkan')
    elif torch.cuda.is_available() and hasattr(torch.version, 'hip') and torch.version.hip:
        device = torch.device('cuda')
    elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
        device = torch.device('mps')
    else:
        try:
            import intel_extension_for_pytorch as ipex
            if torch.xpu.is_available():
                device = torch.device('xpu')
            else: raise Exception()
        except:
            device = torch.device('cpu')

    def deep_merge(base, override):
        """
        Recursively merges override into base.
        """
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                deep_merge(base[key], value)
            else:
                base[key] = value
        return base

    def load_config_from_path(path):
        if not os.path.exists(path):
            console.print(f"[bold red]Error: Configuration file '{path}' not found.[/]")
            sys.exit(1)
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except Exception as e:
            console.print(f"[bold red]Error parsing configuration file '{path}': {e}[/]")
            sys.exit(1)

    def validate_config(config):
        required_sections = ['timeouts', 'exchange', 'monte_carlo', 'ui', 'trading', 'strategies', 'audio']
        missing_sections = [s for s in required_sections if s not in config]
        if missing_sections:
            console.print(f"Configuration is missing sections: {', '.join(missing_sections)}. Using internal fallbacks.")

        # Check for critical parameters
        critical_keys = [
            ('exchange', 'default_fee'),
            ('trading', 'base_target_pct'),
            ('monte_carlo', 'num_simulations')
        ]
        for section, key in critical_keys:
            if section in config and key not in config[section]:
                console.print(f"Critical configuration parameter missing: {section}.{key}. Using internal fallback.")

    def load_config():
        config = {}
        if os.path.exists('config.default.json'):
            config = load_config_from_path('config.default.json')

        if os.path.exists('config.json'):
            override = load_config_from_path('config.json')
            if not config:
                config = override
            else:
                config = deep_merge(config, override)

        if not config:
            console.print(f"[bold red]Error: No configuration file found (config.json or config.default.json).[/]")
            sys.exit(1)

        validate_config(config)
        return config

    config = load_config()

    # back tests
    from indicators2 import get_signals, STRATEGIES, STRATEGY_GROUPS
    import random
    # lire le fichier de données OHLCV et effectuer un backtest simple
    import pandas
    import plotext as plt

    # Charger les bougies une seule fois (toutes les bougies disponibles)
    new_candles_df = pandas.DataFrame(df_candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    from datetime import datetime

    import logging
    logging.basicConfig(level=logging.DEBUG)

    # Faire un plot par stratégie
    for i, strat in enumerate(STRATEGIES):
        aggr = 'dynamic'
        settings = {
            'device': device,
            'strategy': strat,
            'aggr': aggr,
            'ema_fast': config.get('ema_fast'),
            'ema_slow': config.get('ema_slow'),
            'macd_fast': config.get('macd_fast'),
            'macd_slow': config.get('macd_slow'),
            'macd_signal': config.get('macd_signal'),
            'rsi_period': config.get('rsi_period'),
            'tema_length': config.get('tema_length')
        }

        # Compute signals for all candles (is_scan=True) so MC strategies populate full series
        df = get_signals(new_candles_df.copy(), settings, is_scan=True, global_config=config)
        buys = []
        sells = []
        if df is None or df.empty:
            console.print(f"[bold yellow]No signals for strategy: {strat} - plotting candles only[/]")
        else:
            df = df.reset_index(drop=True)
            for j in range(len(df)):
                row = df.iloc[j]
                latest = row.to_dict()
                if latest.get('buy_signal', False):
                    buys.append((j, float(latest.get('close', float('nan')))))
                if latest.get('sell_signal', False):
                    sells.append((j, float(latest.get('close', float('nan')))))

        # Préparer les données pour le tracé
        timestamps = df_candles['timestamp'].astype(int).tolist()
        opens = df_candles['open'].astype(float).tolist()
        highs = df_candles['high'].astype(float).tolist()
        lows = df_candles['low'].astype(float).tolist()
        closes = df_candles['close'].astype(float).tolist()
        dates = [datetime.fromtimestamp(int(ts) / 1000).strftime('%d/%m %H:%M') for ts in timestamps]

        plt.clear_figure()
        plt.theme('dark')
        plt.title(str(strat))
        plt.xlabel('Date')
        plt.ylabel('Prix (EUR)')

        data = {"Open": opens, "High": highs, "Low": lows, "Close": closes}
        x = list(range(len(dates)))
        plt.candlestick(x, data)

        # tracer signaux pour cette stratégie
        if highs and lows:
            price_range = max(highs) - min(lows)
        else:
            price_range = 0

        for (pos, price) in buys:
            plt.scatter([pos], [price], marker='x', color='green')
            offset = price + (price_range * 0.0002 if price_range else 0)
            plt.text('BUY', pos, offset, color='green')

        for (pos, price) in sells:
            plt.scatter([pos], [price], marker='o', color='red')
            offset = price - (price_range * 0.0002 if price_range else 0)
            plt.text('SELL', pos, offset, color='red')

        # Définir des labels d'axe X échantillonnés pour lisibilité
        step = max(1, len(dates) // 8)
        x_ticks = x[::step]
        x_labels = [dates[i] for i in x_ticks]
        plt.xticks(x_ticks, x_labels)

        plt.show()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('symbol', help='Trading pair symbol, e.g. LTC/EUR.')
    parser.add_argument('id', help='Trading pair id, e.g. XLTC/ZEUR.')
    args = parser.parse_args()
    main(args.symbol, args.id)

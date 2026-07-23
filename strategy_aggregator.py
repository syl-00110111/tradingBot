"""
Fonctions utilitaires pour agréger les signaux de stratégies.
Ce module centralise la logique partagée entre strategie.py et botv4.py.
"""
import os
import json
from datetime import datetime
import pandas as pd

def load_config():
    """Charger config.default.json puis optionnellement config.json (merge)."""
    cfg = {}
    try:
        if os.path.exists('config.default.json'):
            with open('config.default.json', 'r') as f:
                cfg = json.load(f)
    except Exception:
        cfg = {}
    try:
        if os.path.exists('config.json'):
            with open('config.json', 'r') as f:
                override = json.load(f)
                if cfg:
                    cfg.update(override)
                else:
                    cfg = override
    except Exception:
        pass
    return cfg


def consecutive_count(series, window=None):
    """Compte des signaux True terminant à chaque position.

    Comportements:
    - si window est None: comportement historique = nombre consécutif de True se terminant à la position i.
    - si window est un entier W > 0: retourne pour chaque position i le nombre de True dans la fenêtre mobile
      des W dernières valeurs (non-consécutifs autorisés).
    """
    out = [0] * len(series)
    if window is None:
        count = 0
        for i, v in enumerate(series):
            if bool(v):
                count += 1
            else:
                count = 0
            out[i] = count
        return out

    # fenêtre mobile: compter les True dans les W dernières valeurs (incluant la position courante)
    try:
        w = int(window)
        if w <= 0:
            raise ValueError()
    except Exception:
        # fallback au comportement historique si window invalide
        w = None
    if w is None:
        return consecutive_count(series, None)

    from collections import deque
    dq = deque()
    cnt = 0
    for i, v in enumerate(series):
        is_true = bool(v)
        dq.append(is_true)
        if is_true:
            cnt += 1
        if len(dq) > w:
            popped = dq.popleft()
            if popped:
                cnt -= 1
        out[i] = cnt
    return out


def aggregate_signals(df_candles, global_config=None, strats=None, window=20, score_buy_threshold=3, score_sell_threshold=2,
                      ichimoku_buy_threshold=2, ichimoku_sell_threshold=3,
                      williams_buy_threshold=1, williams_sell_threshold=1,
                      vwap_buy_threshold=5, vwap_sell_threshold=5,
                      pairs_buy_threshold=1, pairs_sell_threshold=1,
                      signal_frames=None):
    """
    Calcule les signaux agrégés à partir des stratégies listées dans `strats`.
    Retourne un dict contenant: N, signal_frames, score_buy, score_sell, global_buy, global_sell
    """
    try:
        from indicators2 import get_signals
    except Exception:
        raise

    if global_config is None:
        global_config = load_config()

    STRATS = strats if strats is not None else [
        'ichimoku_cloud',
        'williams_r',
        'vwap_momentum',
        'pairs_trading_proxy'
    ]

    N = len(df_candles)
    signal_frames = {}
    for strat in STRATS:
        settings = {'strategy': strat, 'device': None}
        df_sign = get_signals(df_candles.copy(), settings, is_scan=True, global_config=global_config)
        if df_sign is None:
            df_sign = pd.DataFrame(index=df_candles.index)
        signal_frames[strat] = df_sign

    score_buy = [0.0] * N
    score_sell = [0.0] * N

    # ichimoku_cloud régulier mais sensible
    # williams_r lent mais régulier
    # vwap_momentum crêtes à l'envers

    # 1) ichimoku_cloud
    pt = signal_frames.get('ichimoku_cloud')
    if pt is not None and not pt.empty:
        # à l'envers
        sells = consecutive_count(pt.get('sell_signal', pd.Series([False] * N)).fillna(False).tolist(), window=window)
        buys = consecutive_count(pt.get('buy_signal', pd.Series([False] * N)).fillna(False).tolist(), window=window)
        for i in range(N):
            if buys[i] >= ichimoku_sell_threshold:
                score_sell[i] += 1
            if sells[i] >= ichimoku_buy_threshold:
                score_buy[i] += 1

    # 2) williams_r
    pt = signal_frames.get('williams_r')
    if pt is not None and not pt.empty:
        # à l'endroit
        sells = consecutive_count(pt.get('sell_signal', pd.Series([False] * N)).fillna(False).tolist(), window=window)
        buys = consecutive_count(pt.get('buy_signal', pd.Series([False] * N)).fillna(False).tolist(), window=window)
        for i in range(N):
            if buys[i] >= williams_buy_threshold:
                score_buy[i] += 1
            if sells[i] >= williams_sell_threshold:
                score_sell[i] += 1

    # 3) vwap_momentum
    pt = signal_frames.get('vwap_momentum')
    if pt is not None and not pt.empty:
        # à l'envers
        buys = consecutive_count(pt.get('buy_signal', pd.Series([False] * N)).fillna(False).tolist(), window=window)
        sells = consecutive_count(pt.get('sell_signal', pd.Series([False] * N)).fillna(False).tolist(), window=window)
        for i in range(N):
            if buys[i] >= vwap_sell_threshold:
                score_sell[i] += 1
            if sells[i] >= vwap_buy_threshold:
                score_buy[i] += 1

    # 4) pairs_trading_proxy
    pt = signal_frames.get('pairs_trading_proxy')
    if pt is not None and not pt.empty:
        # à l'endroit
        sells = consecutive_count(pt.get('sell_signal', pd.Series([False] * N)).fillna(False).tolist(), window=60)
        buys = consecutive_count(pt.get('buy_signal', pd.Series([False] * N)).fillna(False).tolist(), window=60)
        for i in range(N):
            if buys[i] >= pairs_buy_threshold:
                score_buy[i] += 1
            if sells[i] >= pairs_sell_threshold:
                score_sell[i] += 1

    global_buy = [s >= score_buy_threshold for s in score_buy]
    global_sell = [s >= score_sell_threshold for s in score_sell]

    # print(f"DEBUG sell= {global_sell}")

    return {
        'N': N,
        'signal_frames': signal_frames,
        'score_buy': score_buy,
        'score_sell': score_sell,
        'global_buy': global_buy,
        'global_sell': global_sell,
    }

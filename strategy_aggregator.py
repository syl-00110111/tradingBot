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


def aggregate_signals(df_candles, global_config=None, strats=None):
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
        'williams_r'
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
    
    # 1) williams_r
    pt = signal_frames.get('williams_r')
    if pt is not None and not pt.empty:
        # utiliser une fenêtre pour compter les signaux non-consécutifs
        buys = consecutive_count(pt.get('buy_signal', pd.Series([False] * N)).fillna(False).tolist(), window=6)
        sells = consecutive_count(pt.get('sell_signal', pd.Series([False] * N)).fillna(False).tolist(), window=6)
        for i in range(N):
            if buys[i] >= 2:
                score_buy[i] += 1
            if sells[i] >= 2:
                score_sell[i] += 1

    global_buy = [s >= 1 for s in score_buy]
    global_sell = [s >= 1 for s in score_sell]

    # print(f"DEBUG sell= {global_sell}")

    return {
        'N': N,
        'signal_frames': signal_frames,
        'score_buy': score_buy,
        'score_sell': score_sell,
        'global_buy': global_buy,
        'global_sell': global_sell,
    }

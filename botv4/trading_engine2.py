# Binance Trading Bot - Trading Engine
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

"""
Moteur de trading principal pour le dimensionnement des positions et la gestion des risques.

Ce module gère le calcul des montants de transaction et l'ajustement dynamique
des paramètres techniques en fonction des conditions du marché.
"""

import logging

class TradingEngine:
    """
    Moteur principal pour la logique de trading, y compris le dimensionnement des positions
    et l'ajustement dynamique des risques.

    Paramètres
    ----------
    config : dict
        La configuration du bot.
    """
    def __init__(self, config):
        self.config = config
        self.risk_multiplier = float(config.get('global_risk_multiplier', 1.0))
        self.min_profit_margin = float(config.get('min_profit_margin', 0.01))

    def get_dynamic_settings(self, adx, volatility, aggr='normal'):
        """
        Ajuste les paramètres des indicateurs techniques en fonction des régimes de marché
        actuels et de l'agressivité.

        Paramètres
        ----------
        adx : float
            Indice Directionnel Moyen actuel (force de la tendance).
        volatility : float
            Volatilité actuelle du marché (écart-type des rendements logarithmiques).
        aggr : str
            Profil d'agressivité : 'normal', 'aggressive', ou 'dynamic'.

        Retourne
        -------
        dict
            Un dictionnaire de paramètres d'indicateurs techniques.
        """
        # Paramètres de base (Normal)
        settings = {
            "ema_fast": self.config.get('ema_fast'),
            "ema_slow": self.config.get('ema_slow'),
            "tema_length": self.config.get('tema_length'),
            "macd_fast": self.config.get('macd_fast'),
            "macd_slow": self.config.get('macd_slow'),
            "macd_signal": self.config.get('macd_signal'),
            "rsi_period": self.config.get('rsi_period'),
            "rsi_buy": self.config.get('rsi_buy'),
            "rsi_sell": self.config.get('rsi_sell'),
            "effective_aggr": aggr
        }

        trading_cfg = self.config.get('trading', {})
        if aggr == 'aggressive':
            settings.update(trading_cfg.get('aggressive_settings', {
                "ema_fast": 10, "ema_slow": 30,
                "rsi_buy": 40, "rsi_sell": 60
            }))
        elif aggr == 'dynamic':
            regime_cfg = trading_cfg.get('dynamic_regime', {})
            if adx > regime_cfg.get('adx_threshold', 25):
                settings.update(regime_cfg.get('trending', {
                    "ema_fast": 10, "ema_slow": 30,
                    "rsi_buy": 40, "rsi_sell": 60
                }))
                settings['effective_aggr'] = "aggressive"
            elif volatility > regime_cfg.get('volatility_threshold', 0.015):
                settings.update(regime_cfg.get('volatile', {
                    "ema_fast": 30, "ema_slow": 100,
                    "rsi_buy": 20, "rsi_sell": 80
                }))
                settings['effective_aggr'] = "conservative"
            else:
                settings["effective_aggr"] = "normal"

        return settings

    def get_min_exit_price(self, entry_price, fee_rate=None, min_profit=0):
        if fee_rate is None:
            fee_rate = self.config.get('exchange', {}).get('default_fee', 0.001)
        """
        Calcule le prix de sortie minimum requis pour atteindre un profit cible, frais inclus.

        Utilise la formule précise :
        Prix_sortie * (1 - f) = Prix_entrée * (1 + f) * (1 + profit)
        Prix_sortie = Prix_entrée * (1 + f) * (1 + profit) / (1 - f)

        Paramètres
        ----------
        entry_price : float
            Le prix auquel l'actif a été acheté.
        fee_rate : float, optional
            Le taux de commission de l'échange (par défaut 0.001 pour 0,1%).
        min_profit : float, optional
            Marge de profit net cible (par défaut 0).

        Retourne
        -------
        float
            Le prix de sortie cible.
        """
        # Équilibre précis : Price_exit * (1 - f) = Price_entry * (1 + f) * (1 + profit)
        # Price_exit = Price_entry * (1 + f) * (1 + profit) / (1 - f)
        return entry_price * (1 + fee_rate) * (1 + min_profit) / (1 - fee_rate)

    def is_profitable(self, current_price, entry_price, fee_rate=None, entry_total_base=0, amount=0):
        if fee_rate is None:
            fee_rate = self.config.get('exchange', {}).get('default_fee', 0.001)
        """
        Vérifie si la clôture d'une position au prix actuel dépasse la marge de profit cible.

        Paramètres
        ----------
        current_price : float
            Le prix actuel du marché.
        entry_price : float
            Le prix d'entrée de la position.
        fee_rate : float, optional
            Le taux de commission de l'échange.
        entry_total_base : float, optional
            Le coût total d'entrée en devise de base (incluant les frais).
        amount : float, optional
            La quantité de l'actif.

        Retourne
        -------
        bool
            True si le profit net atteint la marge cible après frais, False sinon.
        """
        if entry_total_base > 0 and amount > 0:
            # Net_Proceeds = (Price_exit * Amount * (1 - f))
            # Target = Entry_total_base * (1 + min_profit_margin)
            return (current_price * amount * (1 - fee_rate)) >= (entry_total_base * (1 + self.min_profit_margin))
        return current_price >= self.get_min_exit_price(entry_price, fee_rate, self.min_profit_margin)

    def check_profitability(self, current_price, entry_price, symbol, fee_rate=None):
        """
        Alias pour is_profitable.
        """
        return self.is_profitable(current_price, entry_price, fee_rate)

    def calculate_position_size(self, balance, current_price, base_currency, win_streak=0, max_lots=1):
        """
        Calcule la quantité d'un actif à acheter en fonction du solde du portefeuille et du risque.
        Applique un multiplicateur pour les paires dynamiques (1s).
        """
        # Helper function to safely convert to float
        def to_float(value):
            if value is None:
                return 0.0
            try:
                return float(value)
            except (ValueError, TypeError):
                return 0.0
        
        # Ensure current_price is a float
        current_price = to_float(current_price)
        # Normalize currency key checks to be case-insensitive
        def find_key(d, key):
            if not isinstance(d, dict):
                return None
            if key in d:
                return key
            up = key.upper()
            low = key.lower()
            if up in d:
                return up
            if low in d:
                return low
            return None
        
        base_balance = 0.0
        if balance and isinstance(balance, dict):
            # Try multiple ways to get the balance, in order of preference
            
            # 1. Kraken format: balance[currency] = {free, used, total}
            bkey = find_key(balance, base_currency)
            if bkey:
                currency_data = balance[bkey]
                if isinstance(currency_data, dict):
                    base_balance = to_float(currency_data.get('total')) or to_float(currency_data.get('free')) or 0.0
                else:
                    base_balance = to_float(currency_data)
            
            # 2. If not found, try balance['total'][currency]
            if base_balance == 0 and 'total' in balance and balance['total'] is not None:
                total_data = balance['total']
                if isinstance(total_data, dict):
                    tkey = find_key(total_data, base_currency)
                    raw_balance = total_data.get(tkey) if tkey else None
                    base_balance = to_float(raw_balance)
                else:
                    base_balance = to_float(total_data)
            
            # 3. If still 0, try balance['free'][currency]
            if base_balance == 0 and 'free' in balance and balance['free'] is not None:
                free_data = balance['free']
                if isinstance(free_data, dict):
                    fkey = find_key(free_data, base_currency)
                    raw_balance = free_data.get(fkey) if fkey else None
                    base_balance = to_float(raw_balance)
                else:
                    base_balance = to_float(free_data)
            
            # 4. Direct fallback
            if base_balance == 0:
                # Last resort: try keys with different casing
                try_val = None
                direct_key = find_key(balance, base_currency)
                if direct_key:
                    try_val = balance.get(direct_key)
                else:
                    try_val = balance.get(base_currency)
                base_balance = to_float(try_val)

        # Ensure max_lots is a float
        max_lots = to_float(max_lots) if max_lots is not None else 1.0
        if max_lots <= 0:
            max_lots = 1.0

        # 1. Déterminer le plafond strict pour cet actif de base
        cfg_val = self.config.get('max_trade_percentage')
        if cfg_val is None:
            max_pct = 10.0  # Default 10% if not configured
        elif isinstance(cfg_val, dict):
            max_pct = to_float(cfg_val.get(base_currency, cfg_val.get('default', 10.0)))
        else:
            max_pct = to_float(cfg_val)

        ceiling_pct = max_pct / 100.0 if max_pct >= 1.0 else max_pct

        # Le plafond est divisé par le nombre de lots maximum pour cette paire
        max_allowed_base = (base_balance * ceiling_pct) / max_lots

        # 2. Calculer le montant de base initial en partant d'en bas
        # Nous commençons avec une base configurable (défaut 75%) du plafond autorisé par lot
        trading_config = self.config.get('trading', {})
        base_target_multiplier = to_float(trading_config.get('base_target_pct', 0.75)) if trading_config else 0.75
        base_target_pct = (ceiling_pct * base_target_multiplier) / max_lots
        trade_amount_base = base_balance * base_target_pct

        # 3. Appliquer le multiplicateur de risque
        trade_amount_base *= self.risk_multiplier

        # Multiplicateur pour les paires dynamiques (1s) - Toujours actif car 1s est le seul TF désormais
        dynamic_multiplier = to_float(self.config.get('dynamic_pair_multiplier', 1.0))
        trade_amount_base *= dynamic_multiplier

        # 4. Appliquer le bonus de série de victoires
        ws_config = self.config.get('win_streak_bonus', {})
        if ws_config and ws_config.get('enabled') and win_streak >= to_float(ws_config.get('threshold', 2)):
            multiplier = to_float(ws_config.get('multiplier', 1.2))
            trade_amount_base *= multiplier

        # 5. Appliquer le plafond strict
        if trade_amount_base > max_allowed_base:
            trade_amount_base = max_allowed_base

        if trade_amount_base > base_balance: 
            trade_amount_base = base_balance
        if current_price > 0: 
            return trade_amount_base / current_price
        return 0

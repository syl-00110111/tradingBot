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
        self.risk_multiplier = float(config.get('global_risk_multiplier', 1.1))

    def get_dynamic_settings(self, adx, volatility, aggr='dynamic'):
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
            "ema_fast": 20, "ema_slow": 50,
            "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
            "rsi_period": 14, "rsi_buy": 30, "rsi_sell": 70,
            "confirmation_window": 1
        }

        if aggr == 'aggressive':
            settings.update({
                "ema_fast": 10, "ema_slow": 30,
                "rsi_buy": 40, "rsi_sell": 60
            })
        elif aggr == 'dynamic':
            if adx > 25:
                settings.update({
                    "ema_fast": 10, "ema_slow": 30,
                    "rsi_buy": 40, "rsi_sell": 60
                })
            elif volatility > 0.015:
                settings.update({
                    "ema_fast": 30, "ema_slow": 100,
                    "rsi_buy": 20, "rsi_sell": 80
                })

        # La haute volatilité ajoute un signal de confirmation supplémentaire globalement
        if volatility > 0.1:
            settings["confirmation_window"] = 2

        return settings

    def get_min_exit_price(self, entry_price, fee_rate=0.001):
        """
        Calcule le prix de sortie minimum requis pour atteindre le seuil de rentabilité, frais inclus.

        Utilise la formule précise :
        Prix_sortie * (1 - f) = Prix_entrée * (1 + f)
        Prix_sortie = Prix_entrée * (1 + f) / (1 - f)

        Paramètres
        ----------
        entry_price : float
            Le prix auquel l'actif a été acheté.
        fee_rate : float, optional
            Le taux de commission de l'échange (par défaut 0.001 pour 0,1%).

        Retourne
        -------
        float
            Le prix de sortie d'équilibre.
        """
        # Équilibre précis : Price_exit * (1 - f) = Price_entry * (1 + f)
        # Price_exit = Price_entry * (1 + f) / (1 - f)
        return entry_price * (1 + fee_rate) / (1 - fee_rate)

    def is_profitable(self, current_price, entry_price, fee_rate=0.001, entry_total_base=0, amount=0):
        """
        Vérifie si la clôture d'une position au prix actuel serait profitable.

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
            True si le profit net est positif après frais, False sinon.
        """
        if entry_total_base > 0 and amount > 0:
            # Profit = (Price_exit * Amount * (1 - f)) - Entry_total_base
            return (current_price * amount * (1 - fee_rate)) > entry_total_base
        return current_price > self.get_min_exit_price(entry_price, fee_rate)

    def check_profitability(self, current_price, entry_price, symbol, fee_rate=0.001):
        """
        Alias pour is_profitable.
        """
        return self.is_profitable(current_price, entry_price, fee_rate)

    def calculate_position_size(self, balance, current_price, base_currency, win_streak=0, max_lots=1):
        """
        Calcule la quantité d'un actif à acheter en fonction du solde du portefeuille et du risque.
        """
        base_balance = 0
        if balance and isinstance(balance, dict):
            if 'free' in balance:
                free_data = balance['free']
                base_balance = free_data.get(base_currency, 0) if isinstance(free_data, dict) else 0
            else:
                base_balance = balance.get(base_currency, 0)

        # 1. Déterminer le plafond strict pour cet actif de base
        cfg_val = self.config.get('max_trade_percentage', 12.0)
        if isinstance(cfg_val, dict):
             max_pct = float(cfg_val.get(base_currency, cfg_val.get('default', 12.0)))
        else:
             max_pct = float(cfg_val)

        ceiling_pct = max_pct / 100.0 if max_pct >= 1.0 else max_pct

        # Le plafond est divisé par le nombre de lots maximum pour cette paire
        max_allowed_base = (base_balance * ceiling_pct) / max_lots

        # 2. Calculer le montant de base initial en partant d'en bas
        # Nous commençons avec une base de 75% du plafond autorisé par lot
        base_target_pct = (ceiling_pct * 0.75) / max_lots
        trade_amount_base = base_balance * base_target_pct

        # 3. Appliquer le multiplicateur de risque
        trade_amount_base *= self.risk_multiplier

        # 4. Appliquer le bonus de série de victoires
        ws_config = self.config.get('win_streak_bonus', {})
        if ws_config.get('enabled') and win_streak >= ws_config.get('threshold', 2):
             multiplier = ws_config.get('multiplier', 1.2)
             trade_amount_base *= multiplier
             # logging.info(f"Série de victoires détectée ({win_streak}), application d'un multiplicateur {multiplier}x.")

        # 5. Appliquer le plafond strict
        if trade_amount_base > max_allowed_base:
             trade_amount_base = max_allowed_base
             # logging.info(f"Montant de la transaction plafonné au plafond strict : {max_pct}% du solde {base_currency}.")

        if trade_amount_base > base_balance: trade_amount_base = base_balance
        if current_price > 0: return trade_amount_base / current_price
        return 0

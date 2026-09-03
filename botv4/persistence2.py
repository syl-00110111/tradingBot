# Binance Trading Bot - Persistence & State Management
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you

"""
Persistence et gestion de l'état pour le bot de trading.

Ce module gère le stockage en mémoire et potentiellement persistant de l'historique
des transactions, des positions ouvertes, des modèles découverts et des caches à court terme.
"""

import json
import os
import time
import logging

class PatternManager:
    """
    Gestionnaire pour le stockage et la récupération de modèles techniques pour la correspondance de similarité.
    """
    def __init__(self):
        self.data = {}

    def set_patterns(self, symbol, patterns):
        """
        Stocke les 4 meilleurs modèles réussis pour un symbole.

        Paramètres
        ----------
        symbol : str
            Le symbole de la paire de trading.
        patterns : list of dict
            Une liste de modèles découverts.
        """
        self.data[symbol] = patterns[:4]

    def get_patterns(self, symbol):
        """
        Récupère les modèles stockés pour un symbole.

        Paramètres
        ----------
        symbol : str
            Le symbole de la paire de trading.

        Retourne
        -------
        list of dict
            La liste des modèles stockés.
        """
        return self.data.get(symbol, [])

class DataManager:
    """
    Gestionnaire pour l'état de trading du bot, y compris les positions ouvertes et l'historique des transactions.
    """
    def __init__(self):
        self.data = {"open_positions": {}, "trade_history": []}

    def clear_history(self):
        """
        Réinitialise l'historique des transactions et les positions ouvertes.
        """
        self.data = {"open_positions": {}, "trade_history": []}

    def add_position(self, symbol, entry_price, amount, fee, trigger_data, timestamp, total_base=0):
        """
        Enregistre un nouveau lot (position) ouvert.

        Paramètres
        ----------
        symbol : str
            Le symbole de la paire de trading.
        entry_price : float
            Le prix auquel l'actif a été acheté.
        amount : float
            La quantité d'actif achetée.
        fee : float
            Les frais payés pour l'ordre d'achat.
        trigger_data : dict
            Données des indicateurs techniques au moment de l'achat.
        timestamp : float
            L'horodatage de l'achat.
        total_base : float, optional
            Le coût total en devise de base (par défaut 0).
        """
        if symbol not in self.data["open_positions"]:
            self.data["open_positions"][symbol] = []

        self.data["open_positions"][symbol].append({
            "entry_price": entry_price, "amount": amount, "entry_fee": fee,
            "entry_total_base": total_base, "trigger_data": trigger_data,
            "timestamp": timestamp, "sell_signals_received": 0, "last_sell_signal_candle_ts": None
        })

    def increment_sell_signals(self, symbol, candle_ts):
        """
        Incrémente le compte des signaux de vente consécutifs pour toutes les positions d'un symbole.

        Paramètres
        ----------
        symbol : str
            Le symbole de la paire de trading.
        candle_ts : int or float
            L'horodatage de la bougie actuelle.

        Retourne
        -------
        bool
            True si le compte de signaux a été incrémenté pour au moins une position.
        """
        updated = False
        if symbol in self.data["open_positions"]:
            for pos in self.data["open_positions"][symbol]:
                if pos.get("last_sell_signal_candle_ts") != candle_ts:
                    pos["sell_signals_received"] = pos.get("sell_signals_received", 0) + 1
                    pos["last_sell_signal_candle_ts"] = candle_ts
                    updated = True
        return updated

    def flag_ignore_sell(self, symbol, value=True):
        """
        Marque toutes les positions d'un symbole pour ignorer les futurs signaux de vente.

        Paramètres
        ----------
        symbol : str
            Le symbole de la paire de trading.
        value : bool, optional
            La valeur du drapeau (par défaut True).
        """
        if symbol in self.data["open_positions"]:
            for pos in self.data["open_positions"][symbol]:
                pos["ignore_sell"] = value

    def close_position(self, symbol, exit_price, exit_fee, profit, trigger_data, timestamp, total_base=0, lot_index=0, amount=None):
        """
        Ferme tout ou partie d'un lot spécifique et le déplace vers l'historique des transactions.

        Paramètres
        ----------
        symbol : str
            Le symbole de la paire de trading.
        exit_price : float
            Le prix auquel l'actif a été vendu.
        exit_fee : float
            Les frais payés pour l'ordre de vente.
        profit : float
            Le profit/perte net pour la transaction.
        trigger_data : dict
            Données des indicateurs techniques au moment de la vente.
        timestamp : float
            L'horodatage de la vente.
        total_base : float, optional
            Le montant total reçu en devise de base (par défaut 0).
        lot_index : int, optional
            L'index du lot à fermer (par défaut 0).
        amount : float, optional
            La quantité spécifique à fermer (si None, ferme tout le lot).

        Retourne
        -------
        dict or None
            Les données de la transaction enregistrée, ou None si aucun lot n'a été trouvé.
        """
        if symbol in self.data["open_positions"] and len(self.data["open_positions"][symbol]) > lot_index:
            lot = self.data["open_positions"][symbol][lot_index]

            close_amount = amount if amount is not None else lot["amount"]

            # Use epsilon to avoid float precision issues when checking if lot is fully closed
            if amount is None or abs(close_amount - lot["amount"]) < 1e-10:
                # Full closure
                position = self.data["open_positions"][symbol].pop(lot_index)
                if not self.data["open_positions"][symbol]:
                    self.data["open_positions"].pop(symbol)

                trade_entry_fee = position.get("entry_fee", 0)
                trade_entry_total_base = position.get("entry_total_base", 0)
            else:
                # Partial closure
                proportion = close_amount / lot["amount"]
                trade_entry_fee = lot.get("entry_fee", 0) * proportion
                trade_entry_total_base = lot.get("entry_total_base", 0) * proportion

                # Update remaining lot
                lot["amount"] -= close_amount
                # Re-calculating entry_fee and entry_total_base for the remaining part
                if "entry_fee" in lot: lot["entry_fee"] -= trade_entry_fee
                if "entry_total_base" in lot: lot["entry_total_base"] -= trade_entry_total_base

                position = lot # for the trade history below

            trade = {
                "symbol": symbol, "entry_price": position["entry_price"], "exit_price": exit_price,
                "amount": close_amount, "entry_fee": trade_entry_fee,
                "entry_total_base": trade_entry_total_base, "exit_fee": exit_fee,
                "exit_total_base": total_base, "profit": profit, "entry_trigger": position.get("trigger_data", {}),
                "exit_trigger": trigger_data, "entry_timestamp": position["timestamp"], "exit_timestamp": timestamp,
                "sell_signals_received": position.get("sell_signals_received", 0)
            }
            self.data["trade_history"].append(trade)
            return trade
        return None

    def get_open_positions(self):
        """
        Récupère toutes les positions actuellement ouvertes.

        Retourne
        -------
        dict
            Un dictionnaire des positions ouvertes (listes de lots par symbole).
        """
        return self.data["open_positions"]

    def get_position(self, symbol):
        """
        Récupère les lots ouverts pour un symbole spécifique.

        Paramètres
        ----------
        symbol : str
            Le symbole de la paire de trading.

        Retourne
        -------
        list or None
            La liste des lots pour ce symbole, ou None si aucun n'est trouvé.
        """
        return self.data["open_positions"].get(symbol)

    def clear_positions(self, symbol):
        """
        Supprime toutes les positions ouvertes pour un symbole (utile pour le nettoyage de poussière).
        """
        if symbol in self.data["open_positions"]:
            del self.data["open_positions"][symbol]

    def get_win_streak(self, symbol):
        """
        Calcule la série de victoires actuelle pour un symbole.

        Paramètres
        ----------
        symbol : str
            Le symbole de la paire de trading.

        Retourne
        -------
        int
            Le nombre de transactions profitables consécutives.
        """
        streak = 0
        history = [t for t in self.data.get("trade_history", []) if t.get("symbol") == symbol]
        for trade in reversed(history):
            if trade.get("profit", 0) > 0: streak += 1
            else: break
        return streak

    def get_last_trade_exit_timestamp(self, symbol):
        """
        Récupère l'horodatage de la dernière transaction clôturée pour un symbole.

        Paramètres
        ----------
        symbol : str
            Le symbole de la paire de trading.

        Retourne
        -------
        float
            L'horodatage de clôture (Unix timestamp), ou 0 si aucune transaction trouvée.
        """
        history = self.data.get("trade_history", [])
        for trade in reversed(history):
            if trade.get("symbol") == symbol:
                return trade.get("exit_timestamp", 0)
        return 0

class CacheManager:
    """
    Gestionnaire pour la mise en cache à court terme en mémoire des résultats de découverte.
    """
    def __init__(self):
        self.cache = {}

    def get(self, symbol, timeframe, max_age_seconds):
        """
        Récupère les données mises en cache si elles ne sont pas plus anciennes que max_age_seconds.

        Paramètres
        ----------
        symbol : str
            Le symbole de la paire de trading.
        timeframe : str
            L'unité de temps.
        max_age_seconds : int
            Âge maximum du cache en secondes.

        Retourne
        -------
        any or None
            Les données mises en cache, ou None si non trouvées ou expirées.
        """
        key = f"{symbol}_{timeframe}"
        if key in self.cache:
            entry = self.cache[key]
            if time.time() - entry['timestamp'] < max_age_seconds: return entry['data']
        return None

    def set(self, symbol, timeframe, data):
        """
        Stocke les données dans le cache.

        Paramètres
        ----------
        symbol : str
            Le symbole de la paire de trading.
        timeframe : str
            L'unité de temps.
        data : any
            Les données à mettre en cache.
        """
        key = f"{symbol}_{timeframe}"
        self.cache[key] = {'timestamp': time.time(), 'data': data}

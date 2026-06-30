# Bot de Trading Binance - Moteur de Monte Carlo
# Copyleft © 2026 Jules, Ecosia, Sylvain, le World-Wide-Web et vous

"""
Moteur de simulation de Monte Carlo pour l'estimation des probabilités et la validation des stratégies.

Ce module utilise le mouvement brownien géométrique et des simulations accélérées par PyTorch
pour évaluer le succès potentiel des stratégies de trading.
"""

import numpy as np
import torch

class MonteCarloEngine:
    """
    Moteur de Monte Carlo pour simuler les trajectoires de prix futures et estimer les probabilités.

    Utilise le mouvement brownien géométrique (GBM) pour les simulations, accéléré via PyTorch.

    Paramètres
    ----------
    num_simulations : int, optional
        Nombre de trajectoires à simuler (par défaut 5000).
    timeframe_candles : int, optional
        Nombre d'étapes (bougies) à simuler dans le futur (par défaut 100).
    """
    def __init__(self, num_simulations=500, timeframe_candles=100):
        self.num_simulations = num_simulations
        self.timeframe_candles = timeframe_candles
        # Le device sera mis à jour par le bot au moment de l'exécution, par défaut sur CPU
        self.device = torch.device("cpu")

    def set_device(self, device):
        """
        Met à jour le périphérique de calcul (CPU ou GPU).

        Paramètres
        ----------
        device : torch.device
            Le périphérique à utiliser pour les opérations sur les tenseurs.
        """
        self.device = device

    def simulate_paths(self, current_price, volatility, drift=0):
        """
        Simule les trajectoires de prix en utilisant le mouvement brownien géométrique.

        Vectorisé avec PyTorch pour l'accélération matérielle.

        Paramètres
        ----------
        current_price : float
            Le prix de départ pour la simulation.
        volatility : float
            L'écart-type des rendements logarithmiques.
        drift : float, optional
            La moyenne des rendements logarithmiques (par défaut 0).

        Retourne
        -------
        torch.Tensor
            Un tenseur 2D de forme (num_simulations, timeframe_candles + 1)
            contenant les trajectoires simulées.
        """
        # S'assurer que les entrées sont des tenseurs et déplacées sur le périphérique
        curr_p = torch.tensor(current_price, device=self.device, dtype=torch.float64)
        vol = torch.tensor(volatility, device=self.device, dtype=torch.float64)
        drft = torch.tensor(drift, device=self.device, dtype=torch.float64)

        # Génération des rendements aléatoires normalement distribués
        returns = torch.randn((self.num_simulations, self.timeframe_candles), device=self.device) * vol + drft

        # Somme cumulative pour la simulation de trajectoire
        price_paths = curr_p * torch.exp(torch.cumsum(returns, dim=1))

        # Ajouter le prix actuel au début de chaque trajectoire
        ones = torch.ones((self.num_simulations, 1), device=self.device) * curr_p
        price_paths = torch.cat((ones, price_paths), dim=1)
        return price_paths

    def estimate_hit_probability(self, current_price, target_price, volatility, drift=0, mode="above"):
        """
        Estime la probabilité que le prix atteigne une cible dans le délai imparti.

        Paramètres
        ----------
        current_price : float
            Prix de départ.
        target_price : float
            Le prix cible à atteindre.
        volatility : float
            Volatilité des rendements logarithmiques.
        drift : float, optional
            Dérive des rendements logarithmiques.
        mode : str, optional
            Indique s'il faut vérifier si le prix atteint une valeur "above" (au-dessus) ou "below" (en-dessous) de la cible.

        Retourne
        -------
        float
            La probabilité estimée (0.0 à 1.0).
        """
        if volatility == 0:
            return 1.0 if (mode == "above" and target_price <= current_price) or (mode == "below" and target_price >= current_price) else 0.0

        paths = self.simulate_paths(current_price, volatility, drift)

        if mode == "above":
            hits = torch.any(paths >= target_price, dim=1)
        else:
            hits = torch.any(paths <= target_price, dim=1)

        return torch.mean(hits.double()).item()

    def validate_strategy(self, df):
        """
        Valide le potentiel d'une stratégie en simulant les trajectoires futures.

        Calcule la volatilité historique et la dérive à partir des données fournies,
        puis détermine la probabilité que le prix dépasse un seuil de profit de 0,15%.

        Paramètres
        ----------
        df : pandas.DataFrame
            Données OHLCV historiques.

        Retourne
        -------
        float
            Un score de facteur d'échelle entre 0,5 et 1,5 basé sur la probabilité de profit.
        """
        if len(df) < 20: return 1.0

        close = df["close"].values
        valid_indices = ~np.isnan(close)
        close = close[valid_indices]

        if len(close) < 2: return 1.0

        # Calcul des rendements
        price_ratios = close[1:] / close[:-1]
        price_ratios = np.where(price_ratios <= 0, 1.0, price_ratios)
        returns = np.log(price_ratios)

        volatility = np.std(returns)
        drift = np.mean(returns)
        current_price = close[-1]

        if volatility == 0: return 1.0

        paths = self.simulate_paths(current_price, volatility, drift)

        # Validation : vérifie combien de trajectoires se terminent avec un profit > frais attendus (0,15%)
        final_prices = paths[:, -1]
        profit_prob = torch.mean((final_prices > current_price * 1.0015).double()).item()

        # Transformation de la probabilité en un facteur d'échelle [0,5, 1,5]
        score = 0.5 + profit_prob
        return score

    def price_option(self, current_price, strike_price, volatility, drift=0, option_type="call"):
        """
        Estime le prix d'une option en utilisant la simulation de Monte Carlo.

        Paramètres
        ----------
        current_price : float
            Prix actuel de l'actif.
        strike_price : float
            Prix d'exercice de l'option.
        volatility : float
            Volatilité des rendements logarithmiques.
        drift : float, optional
            Dérive des rendements logarithmiques.
        option_type : str, optional
            Type d'option ("call" ou "put").

        Retourne
        -------
        float
            Le prix équitable estimé de l'option.
        """
        paths = self.simulate_paths(current_price, volatility, drift)
        final_prices = paths[:, -1]
        if option_type == "call":
            payoffs = torch.maximum(final_prices - strike_price, torch.tensor(0.0, device=self.device))
        else:
            payoffs = torch.maximum(torch.tensor(strike_price, device=self.device) - final_prices, torch.tensor(0.0, device=self.device))

        return torch.mean(payoffs).item()

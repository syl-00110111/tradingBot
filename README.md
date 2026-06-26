# 🛸 Bot de Trading CCXT Pro

Un bot de trading universel de crypto-monnaies implémenté en Python, tirant parti du traitement multi-cœurs, de l'accélération GPU et de stratégies basées sur des preuves. Il prend en charge **n'importe quel échange** fourni par la bibliothèque CCXT (Binance, Kraken, OKX, Coinbase, etc.).

---

## 🔬 Fondements Scientifiques
Ce bot implémente des stratégies et une logique recommandées par des études empiriques de premier plan sur les marchés de crypto-monnaies :

- **Success Pattern Matching (SPM)** : Le bot analyse les bougies historiques pour identifier les motifs de succès. Il utilise ensuite la corrélation de Pearson accélérée par GPU et la similarité des états techniques (RSI/ADX) pour n'activer le trading que lorsque les conditions actuelles du marché correspondent à ces fenêtres éprouvées.
- **Scoring Multi-Techniques** : Agrège les signaux de plusieurs stratégies et profils d'agressivité. Le score du signal est pondéré par le nombre de techniques et le score de l'unité de temps optimale du symbole.
- **Détection du Régime de Marché** : Utilise une commutation basée sur la volatilité entre le retour à la moyenne (Mean-Reversion) et le suivi de tendance (Trend-Following).
- **Validation Monte Carlo** : Simulations vectorisées pour estimer la probabilité de succès pour chaque signal, pénalisant les configurations à haut risque.
- **Optimisation SIMD Matérielle** : Détection et utilisation automatique des jeux d'instructions CPU (**MMX**, **SSE**, **AVX**, **AVX2**, **AVX512**) pour des performances optimisées via PyTorch.

---

## 🛠 Fonctionnalités Principales

### ⚡ Performance & Fiabilité
- **Accélération GPU** : Les calculs sont déportés sur la puce graphique via PyTorch. Backends supportés : **CUDA**, **MPS**, **Vulkan**, **oneDNN**, **IPEX** et **ROCm**.
- **Trading Multi-Lots** : Possibilité d'ouvrir plusieurs lots (achats successifs) pour une même paire, permettant de moyenner le prix d'entrée tout en gérant la sortie de chaque lot individuellement.
- **Sortie Sélective Profitable** : Lors d'un signal de vente, le bot identifie et ne revend que les lots qui sont actuellement profitables (frais inclus), conservant les autres en attente de rentabilité.
- **Tableau de Bord Interactif** : Navigation dans les paires de trading avec les flèches et visualisation des graphiques en chandeliers ASCII en temps réel en appuyant sur **ENTRÉE**.
- **Découverte Auto des Positions** : Identifie automatiquement les actifs existants dans votre portefeuille et les peuple comme des positions gérées.
- **Synchronisation API** : Le mode Live utilise exclusivement les données de l'API de l'échange pour les soldes et les positions.
- **Sélection Dynamique du Timeframe** : Détermine l'unité de temps optimale (1m, 3m, 5m, 15m, 30m) pour chaque paire en fonction du volume 48h, du spread, de la volatilité et de l'activité.

### 🛡 Gestion des Risques
- **Logique de Confirmation** : Nécessite des signaux identiques consécutifs pour l'exécution. La fenêtre de confirmation s'élargit automatiquement en cas de haute volatilité (> 0.1).
- **Suspension Intelligente** : Suspend automatiquement le trading pour les symboles où les ordres échouent ou si le budget est insuffisant. Reprend uniquement lorsque 1.5x le budget requis devient disponible.
- **Résilience HTTP 500** : Implémente un refroidissement de 21 minutes pour les symboles rencontrant des erreurs serveur de l'échange.
- **Dimensionnement Dynamique** : Les tailles de position sont calculées comme un pourcentage de votre solde disponible, divisé par le nombre maximum de lots autorisés pour maintenir une exposition contrôlée.

---

## 📈 Stratégies Supportées
Le bot propose plus de 30 stratégies distinctes, incluant :

- **Suivi de Tendance** : `ichimoku_cloud`, `parabolic_sar`, `adx_trend_strength`, `halving_cycle_proxy`.
- **Retour à la Moyenne & Plage** : `bollinger_bands`, `rsi_support_resistance`, `pairs_trading_proxy`.
- **Breakout & Momentum** : `breakout_volume`, `donchian_channels`, `atr_breakout`, `stochastic_rsi`, `williams_r`, `vwap_momentum`.
- **Scalping & Flux d'Ordres** : `order_flow_proxy`, `renko_proxy`, `tick_proxy`, `ema_rsi_volume`.
- **Proxies Avancés** : `scientific_ensemble`, `whale_detection_proxy`, `pump_dump_proxy`, `market_regime_proxy`, `sentiment_momentum_proxy`, `liquidation_cascade_proxy`.
- **Moteurs Monte Carlo** : `mc_mean_reversion`, `mc_momentum`, `mc_dynamic_allocation`, `mc_market_making`, `mc_stop_loss_eval`.

---

## ⚙️ Configuration

### 🛠 `config.json`
Paramètres principaux du bot.

*   **`max_lots_per_symbol`** : (int) Nombre maximum de lots d'achat autorisés par symbole (par défaut : `3`).
*   **`max_open_positions`** : (int) Nombre maximum de paires de trading distinctes ouvertes simultanément.
*   **`max_trade_percentage`** : (float | object) Pourcentage maximum de votre solde total à exposer par symbole (tous lots confondus).
*   **`global_risk_multiplier`** : (float) Multiplicateur pour le dimensionnement des positions et les confirmations techniques.

---

## 🚀 Démarrage Rapide

### Installation

1. Créer un environnement virtuel : `python -m venv venv && source venv/bin/activate`
2. Installer les dépendances : `pip install -r requirements.txt`

### Modes d'Exécution
- **Simulation** : `python bot.py --mode simulation --exchange kraken`
- **Live** : `python bot.py --mode live --exchange binance`
- **Balance** : `python bot.py --mode balance`

---

## ⚖️ Avertissement
Le trading comporte des risques significatifs. Utilisez ce bot à vos propres risques. Sous licence **GPL**.

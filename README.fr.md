# 🛸 Bot de trading multiplateforme pour crypto-monnaies

Un bot de trading implémenté en Python, exploitant le traitement multicœur, l'accélération GPU et des stratégies basées sur des preuves empiriques. Il prend en charge **Binance**, **Kraken**, **Bitvavo**, **Coinbase**, **Gemini**, **Mercado Bitcoin**, **Bitso**, **Bitstamp**, **WhiteBIT**, **Indodax**, **Upbit**, **Luno**, **Independent Reserve** et **Btc Markets**.

---

## 🔬 Fondements
Ce bot implémente des stratégies et une logique recommandées par les principales études empiriques sur les marchés des crypto-monnaies :

- **Success Pattern Matching (SPM)** : Le bot scanne les bougies historiques à rebours pour identifier des modèles de réussite. Il utilise ensuite la corrélation de Pearson accélérée par GPU et la similitude des états techniques (RSI/ADX) pour n'activer le trading que lorsque les conditions actuelles du marché correspondent à ces fenêtres éprouvées.
- **Stratégie BTC (MACD/RSI)** : Le MACD et le RSI fournissent des signaux fiables pour l'action des prix du Bitcoin (*Urquhart, 2016* ; *Zhang et al., 2020*).
- **Stratégie ETH (Stochastic RSI)** : Optimisée pour la volatilité de l'Ethereum, suivant les conclusions de *Zhang et al. (2020)*.
- **Détection du régime de marché** : Utilise une commutation basée sur la volatilité entre le retour à la moyenne (Mean-Reversion) et le suivi de tendance (Trend-Following) (*Baur & Dimpfl, 2021*).
- **Validation Monte Carlo (ACHAT uniquement)** : Simulations vectorisées pour estimer la probabilité de succès de chaque signal d'ACHAT, pénalisant les configurations à haut risque. Les ordres de VENTE ignorent cette vérification.
- **Balayage adaptatif et Backoff** : Ajuste automatiquement la fréquence de balayage et les unités de temps (de court à long terme) en fonction de la disponibilité des modèles de marché, évitant ainsi les appels d'API et les traitements redondants.
- **Téléchargeur de bougies prioritaire** : Thread d'arrière-plan dédié avec une file d'attente prioritaire pour les données OHLCV, garantissant que les paires avec des positions ouvertes sont mises à jour en premier.
- **Mode Live optimisé pour la mémoire** : En mode réel, les données OHLCV sont conservées en mémoire pour éviter les entrées/sorties de disque inutiles et les problèmes de pagination.

---

## 🛠 Caractéristiques principales

### ⚡ Performance et Fiabilité
- **Accélération GPU** : Les calculs sont déportés sur la puce graphique via PyTorch. Backends pris en charge : **CUDA**, **MPS**, **Vulkan**, **oneDNN**, **IPEX** et **ROCm**.
- **Gestion de la mémoire** : Collecte agressive des déchets (garbage collection) et purge du cache GPU pour assurer une stabilité à long terme et prévenir les fuites de mémoire lors de calculs intensifs.
- **Benchmark multiprocessus** : L'optimisation des stratégies est parallélisée sur tous les cœurs du processeur.
- **Prix du Ticker frais** : Récupère un prix frais sur l'échange immédiatement avant de passer un ordre d'Achat pour garantir la conformité avec les limites NOTIONAL du marché Spot et réduire les erreurs de "Filter failure".
- **Synchronisation API** : Le mode Live utilise exclusivement les données de l'API de l'échange pour les soldes et les positions.

### 🛡 Gestion des risques
- **Dimensionnement dynamique des positions** : La taille des positions est calculée comme un **pourcentage** de votre devise de base disponible (ex : 10.0 = 10%).

---

## 📈 Stratégies prises en charge
Le bot propose plus de 35 stratégies de trading distinctes, notamment :

- **Suivi de tendance** : `simple_ema`, `simple_sma`, `moving_closes`, `ichimoku_cloud`, `parabolic_sar`, `double_ema`, `double_ema_macd_rsi`, `adx_trend_strength`, `halving_cycle_proxy`.
- **Retour à la moyenne et Range** : `rsi_support_resistance`, `bollinger_bands`, `macd_range`, `macd_bollinger_bands`, `pairs_trading_proxy`.
- **Breakout** : `breakout_volume`, `donchian_channels`, `atr_breakout`, `listing_surge_proxy`.
- **Momentum** : `stochastic_rsi`, `williams_r`, `vwap_momentum`, `sentiment_momentum_proxy`.
- **Scalping et Order Flow** : `order_flow_proxy`, `renko_proxy`, `tick_proxy`, `ema_rsi_volume`.
- **Basées sur Monte Carlo** : `mc_mean_reversion`, `mc_momentum`, `mc_dynamic_allocation`, `mc_market_making`, `mc_stop_loss_eval`, `mc_options_pricing`.
- **Proxies avancés** : `whale_detection_proxy`, `pump_dump_proxy`, `market_regime_proxy`, `scientific_ensemble`, `liquidation_cascade_proxy`, `mvrv_proxy`.

---

## ⚙️ Configuration

### `pairs.txt`
Les paires de trading sont désormais définies dans un simple fichier `pairs.txt` (une par ligne, ex : `BTC/USDT`). Les devises de base sont automatiquement identifiées à partir de cette liste.

### `api.json`
Stockez vos identifiants et l'échange préféré :
```json
{
  "api_key": "VOTRE_CLE",
  "api_secret": "VOTRE_SECRET",
  "exchange": "binance"
}
```
*Options : `binance`, `kraken`, `bitvavo`, `coinbase`, `gemini`, `mercado`, `bitso`, `bitstamp`, `whitebit`, `indodax`, `upbit`, `luno`, `independentreserve`, `btcmarkets`.*

### `config.json`
```json
{
    "max_open_positions": 10,
    "// Note": "base_bet: % de l'actif de cotation disponible (USDT/USDC/etc) par transaction. '10%' utilise 10% du solde.",
    "base_bet": "10%",
    "global_risk_multiplier": 1.2,
    "profit_thresholds": {
        "bench_avg_threshold": 0.05
    }
}
```

---

## 📈 Principes de trading et bonnes pratiques
Le bot est conçu avec les principes empiriques suivants à l'esprit :
- **Préservation du capital** : Utilisez le `base_bet` et le `global_risk_multiplier` pour contrôler l'exposition.
- **Sélectivité** : Le bot n'entre en transaction que lorsque les seuils de similitude technique et de validation Monte Carlo sont atteints.
- **Exécution automatique** : Supprime les biais émotionnels en automatisant les actions d'achat et de vente basées sur des modèles historiques éprouvés.
- **Adaptation de l'unité de temps** : Passage d'unités de temps courtes à des unités plus longues lorsque la volatilité locale est faible, comme recommandé dans *Cryptocurrency - A Trader's Handbook*.
- **Surveillance Experte** : Utilisez la Vue Experte (**[X]**) pour surveiller les indicateurs techniques en temps réel comme l'EMA, le MACD, le RSI, l'ADX et les scores système. Voir [DOCUMENTATION_VUE_EXPERTE.md](DOCUMENTATION_VUE_EXPERTE.md) pour plus de détails.

---

## 🚀 Mise en route

### Installation

**Linux/macOS :**
1. Créer un environnement virtuel : `python -m venv venv`
2. Activer l'environnement virtuel : `source venv/bin/activate`
3. Installer les dépendances : `pip install -r requirements.txt`

**Windows :**
1. Créer un environnement virtuel : `python -m venv venv`
2. Définir une politique d'exécution : `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Current`
2. Activer l'environnement virtuel : `.\venv\Scripts\activate`
3. Installer les dépendances : `pip install -r requirements.txt`

**Tous :**

4. Toujours activer l'environnement virtuel avant d'utiliser le bot si vous l'avez créé.
5. Travailler avec le bot : `python bot.py --mode live`
6. Après le travail : `deactivate` puis `exit`

*Note : Sur Windows, vous devrez peut-être utiliser **Python 3.13** et installer le **Visual C++ 2015-2022 Redistributable (x64)** disponible sur [https://aka.ms/vs/17/release/vc_redist.x64.exe](https://aka.ms/vs/17/release/vc_redist.x64.exe) en raison d'exigences spécifiques de la dépendance llvmlite sur cette plateforme.*

**Maintenance régulière :**
Pour rester à jour avec les changements d'appels API : `pip install --upgrade ccxt` ou `pip install --upgrade -r requirements.txt` pour déclencher tout le processus de mise à jour des dépendances.

### Modes d'exécution
- **Portefeuille et positions virtuels, AUCUNE API REQUISE, prix du marché réels** : `python bot.py --mode virtual --wallet "100 USDC"`
- **Simulation** : `python bot.py --mode simulation --term short`
- **Live** : `python bot.py --mode live --term medium`
- **Benchmark** : `python bot.py --mode benchmark --every-symbol`
- **Backtest** : `python bot.py --mode backtest --symbol BTC/USDT --strategy moving_averages`
- **Balance** : `python bot.py --mode balance`
- **Clôtures interactives** : `python bot.py --mode sell`

---

## 📜 Persistance des données
Le bot maintient une archive consolidée `bot_data_backup.zip`. Les fichiers JSON/Pickle d'exécution sont envoyés dans cette archive et supprimés du disque pour éviter toute perte accidentelle de données. Le bot restaure son état à partir de cette archive au démarrage.

---

## ⚖️ Avis de non-responsabilité
Le trading comporte des risques importants. À utiliser à vos propres risques. Sous licence **GPL**.

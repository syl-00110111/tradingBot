# 🛸 Bot de Trading CCXT Pro

Un bot de trading universel de crypto-monnaies implémenté en Python, tirant parti du traitement multi-cœurs, de l'accélération GPU et de stratégies basées sur des preuves. Il prend en charge **n'importe quel échange** fourni par la bibliothèque CCXT (Binance, Kraken, OKX, Coinbase, etc.).

---

## 🔬 Fondements Scientifiques
Ce bot implémente des stratégies et une logique recommandées par des études empiriques de premier plan sur les marchés de crypto-monnaies :

- **Scoring Multi-Techniques** : Agrège les signaux de plusieurs stratégies et profils d'agressivité. Le score du signal est pondéré par le nombre de techniques et le score de l'unité de temps optimale du symbole.
- **Détection du Régime de Marché** : Utilise une commutation dynamique entre le **Retour à la Moyenne** (Mean-Reversion) et le **Suivi de Tendance** (Trend-Following) basée sur la volatilité et l'ADX.
- **Validation Monte Carlo** : Simulations vectorisées pour estimer la probabilité de succès pour chaque signal, pénalisant les configurations à haut risque.

---

## 🛠 Fonctionnalités Principales

### 🛡 Gestion des Risques
- **Logique de Confirmation** : Nécessite des signaux persistants pour l'exécution. La fenêtre de confirmation s'élargit automatiquement en cas de haute volatilité (> 0.1).
- **Suspension Intelligente** : Suspend automatiquement le trading pour les symboles où les ordres échouent ou si le budget est insuffisant. Reprend uniquement lorsque 1.2x le budget requis devient disponible.
- **Dimensionnement Dynamique** : Les tailles de position sont calculées comme un pourcentage de votre solde disponible, divisé par le nombre maximum de lots autorisés pour maintenir une exposition contrôlée.

---

### ⚡ Performance
- **Accélération GPU** : Les calculs sont déportés sur la puce graphique via PyTorch. Backends supportés : **CUDA**, **MPS**, **Vulkan**, **oneDNN**, **IPEX** et **ROCm**.
- **Optimisation SIMD Matérielle** : Détection et utilisation automatique des jeux d'instructions CPU (**MMX**, **SSE**, **AVX**, **AVX2**, **AVX512**) pour des performances optimisées via PyTorch.

---

## 📈 Stratégies Supportées
Le bot propose plus de 30 stratégies distinctes, classées par régime de marché :

- **Suivi de Tendance** : `ichimoku_cloud`, `parabolic_sar`, `vwap_momentum`, `renko_proxy`, `ema_rsi_volume`, `mc_momentum`, `adx_trend_strength`, `halving_cycle_proxy`, `tema_crossover`, `heikin_ashi`, `donchian_channels`.
- **Retour à la Moyenne** : `bollinger_bands`, `stochastic_rsi`, `williams_r`, `mc_mean_reversion`, `mc_market_making`, `pairs_trading_proxy`, `sinewave_cycle`.
- **Proxies Spécialisés & Autres** : `mc_dynamic_allocation`, `mc_stop_loss_eval`, `mc_options_pricing`, `whale_detection_proxy`, `pump_dump_proxy`, `scientific_ensemble`, `sentiment_momentum_proxy`, `liquidation_cascade_proxy`, `listing_surge_proxy`, `candle_patterns`.

---

## ⚙️ Configuration

### 🛠 `config.json`
Paramètres principaux du bot.

*   **`max_lots_per_symbol`** : (int) Nombre maximum de lots d'achat autorisés par symbole (par défaut : `1`).
*   **`max_open_positions`** : (int) Nombre maximum de paires de trading distinctes ouvertes simultanément (par défaut : `10`).
*   **`max_trade_percentage`** : (float | object) Pourcentage maximum de votre solde total à exposer par symbole (tous lots confondus) (par défaut : `10.0`).
    ```json
    "max_trade_percentage": {
        "BTC": 5.0,
        "USDT": 12.0,
        "default": 10.0
    }
    ```
*   **`global_risk_multiplier`** : (float) Multiplicateur pour le dimensionnement des positions et les confirmations techniques (par défaut : `1.1`).
*   **`dynamic_pair_multiplier`** : (float) Multiplicateur appliqué spécifiquement aux paires en unité de temps 1s (par défaut : `2.0`).
*   **`max_analysis_workers`** : (int) Nombre de workers en parallèle pour l'analyse technique (par défaut : `4`).
*   **`no_signal_threshold`** : (int) Nombre de bougies sans signal avant de déclencher l'optimisation en arrière-plan (par défaut : `48`).

---

### 📄 `pairs.txt`
Définissez les paires de trading que vous souhaitez que le bot surveille (une par ligne).
Exemple :
```text
BTC/USDC
ETH/USDC
SOL/USDC
```

### 🔑 `api.json`
Stockez vos identifiants API et l'échange préféré.
```json
{
  "api_key": "VOTRE_CLE",
  "api_secret": "VOTRE_SECRET",
  "exchange_id": "binance"
}
```

---

## 🚀 Démarrage Rapide

### Installation

**Linux/macOS:**
1. Créer un environnement virtuel et l'activer: `python -m venv venv && source venv/bin/activate`

**Windows:**
1. Créer un environnement virtuel: `python -m venv venv`, puis l'activer: `.\venv\Scripts\Activate.ps1`

2. Installer les dépendances : `pip install --upgrade -r requirements.txt`

### Exécution
- **Live**: `python bot2.py`
- **Options**:
    - `--no-gpu`: Force l'exécution sur CPU.
    - `--fast-start`: Saute la récupération initiale des bougies pour un démarrage plus rapide.

---

## ⚖️ Avertissement
Le trading comporte des risques significatifs. Utilisez ce bot à vos propres risques. Sous licence **GPL**.

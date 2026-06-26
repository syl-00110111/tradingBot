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
- **Sélection Dynamique du Timeframe** : Détermine l'unité de temps optimale (1m, 3m, 5m, 15m, 30m) pour chaque paire en fonction du volume 48h, du spread, de la volatilité et de l'activité, afin de tempérer chaque opportunité.

### 🛡 Gestion des Risques
- **Logique de Confirmation** : Nécessite des signaux identiques consécutifs pour l'exécution. La fenêtre de confirmation s'élargit automatiquement en cas de haute volatilité (> 0.1).
- **Suspension Intelligente** : Suspend automatiquement le trading pour les symboles où les ordres échouent ou si le budget est insuffisant. Reprend uniquement lorsque 1.5x le budget requis devient disponible.
- **Résilience HTTP** : Implémente un refroidissement pour les symboles rencontrant des erreurs serveur lors de l'échange.
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

*   **`max_lots_per_symbol`** : (int) Nombre maximum de lots d'achat autorisés par symbole (par défaut : `1`).
*   **`max_open_positions`** : (int) Nombre maximum de paires de trading distinctes ouvertes simultanément.
*   **`max_trade_percentage`** : (float | object) Pourcentage maximum de votre solde total à exposer par symbole (tous lots confondus).
*   * Attention, il s'agit de DIX pourcent par défaut ce qui peut être beaucoup !! Bien vérifier les options avant lancement live, lancer simulation !!
*   **Per-Base-Asset Configuration**: You can define different maximums for different base currencies:
    ```json
    "max_trade_percentage": {
        "BTC": 5.0,
        "USDT": 12.0,
        "USDC": 10.0,
        "default": 12.0
    }
    ```
*   **`global_risk_multiplier`** : (float) Multiplicateur pour le dimensionnement des positions et les confirmations techniques.

#### Advanced Overrides (Optional)
*   **`force_strategy_to_all_pairs`**: (string) Force the bot to use a specific strategy for every pair.
*   **`force_agressivity_to_all_pairs`**: (string) Force a specific aggressiveness level (e.g., `dynamic`, `normal`, `aggressive`).
*   **`pairs`**: (Object) Allows per-pair configuration with multiple techniques.
    Example:
    ```json
    "pairs": {
        "BTC/USDC": {
            "techniques": [
                {"strategy": "ichimoku_cloud", "aggr": ["normal", "aggressive"]},
                {"strategy": "bollinger_bands", "aggr": ["normal"]}
            ]
        }
    }
    ```
### 📄 `pairs.txt`
Define the trading pairs you want the bot to monitor (one per line).
Example:
```text
BTC/USDC
ETH/USDC
SOL/USDC
```
*Base currencies (e.g., USDC) are automatically detected.*

### 🔑 `api.json`
Store your API credentials and preferred exchange.
```json
{
  "api_key": "YOUR_KEY",
  "api_secret": "YOUR_SECRET",
  "exchange_id": "binance"
}
```
*   **`exchange_id`**: The CCXT ID of the exchange (e.g., `binance`, `kraken`, `okx`, `coinbase`, `gateio`).

---

## 🚀 Démarrage Rapide

### Installation

**Linux/macOS:**
1. Créer un environnement virtuel et l'activer: `python -m venv venv && source venv/bin/activate`

**Windows:**
1. Créer un environnement virtuel: `python -m venv venv`, puis l'activer: `.\venv\Scripts\Activate.ps1`

2. Installer les dépendances : `pip install --upgrade -r requirements.txt`

*Note: pour Windows il faudra exécuter la commande `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser` pour la sécurité, utiliser la révision **3.13** de Python, et installer le **Visual C++ 2015-2022 Redistributable (x64)** que vous pouvez trouver ici [https://aka.ms/vs/17/release/vc_redist.x64.exe] à cause de dépendances spécifiques à cette plateforme.*

**Maintenance régulière:**

Pour rester à jour concernant les modifications des appels API : `pip install --upgrade ccxt` ou `pip install --upgrade -r requirements.txt` pour lancer la procédure complète de mise à jour des dépendances. Assurez-vous également que **l'horloge** de votre ordinateur est synchronisée.

### Modes d'exécution
- **Simulation**: `python bot.py --mode simulation --exchange kraken`
- **Live**: `python bot.py --mode live`
- **Balance**: `python bot.py --mode balance --exchange binance`

---

## ⚖️ Avertissement
Le trading comporte des risques significatifs. Utilisez ce bot à vos propres risques. Sous licence **GPL**.

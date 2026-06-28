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
*   **`max_open_positions`** : (int) Nombre maximum de paires de trading distinctes ouvertes simultanément (par défaut : `10`).
*   **`max_trade_percentage`** : (float | object) Pourcentage maximum de votre solde total à exposer par symbole (tous lots confondus) (par défaut : `10`).
*   * Attention, il s'agit de DIX pourcent par défaut ce qui peut être beaucoup ! Bien vérifier les options avant lancement !
*   **Configuration par devise de base** : Vous pouvez définir différents maximums pour différentes devises de base :
    ```json
    "max_trade_percentage": {
        "BTC": 5.0,
        "USDT": 12.0,
        "USDC": 10.0,
        "default": 12.0
    }
    ```
*   **`global_risk_multiplier`** : (float) Multiplicateur pour le dimensionnement des positions et les confirmations techniques (par défaut : `1.1`).
*   **`dynamic_pair_multiplier`** : (float) Multiplicateur appliqué spécifiquement aux paires en unité de temps 1s (par défaut : `2.0`).
*   **`max_analysis_workers`** : (int) Nombre de workers en parallèle pour l'analyse technique (par défaut : `4`).
*   **`no_signal_threshold`** : (int) Nombre de bougies sans signal avant de déclencher l'optimisation en arrière-plan (par défaut : `48`).
*   **`timeframe_thresholds`** : (Object) Configuration du moteur de sélection dynamique de l'unité de temps (timeframe). Le bot évalue quatre métriques de marché pour choisir l'unité de temps la plus appropriée (de `1s` à `15m`) :
    *   **`volume_48h`** : (low/high) Volume de cotation sur 48 heures. Un volume élevé favorise les unités de temps rapides.
    *   **`spread_pct`** : (low/high) Pourcentage d'écart entre l'achat et la vente (spread). Un spread serré favorise les unités de temps rapides.
    *   **`volatility_pct`** : (low/high) Écart en pourcentage entre le haut et le bas sur les dernières 60 heures. Une faible volatilité (stabilité) favorise les unités de temps rapides.
    *   **`trades_per_minute`** : (low/high) Fréquence des transactions. Une activité élevée favorise les unités de temps rapides.
    *   *Logique de Scoring* : Le bot attribue des points en fonction de ces seuils. Un score ≥ 3 déclenche le `1s`, tandis que des scores inférieurs sélectionnent progressivement `1m`, `3m`, `5m` ou `15m`.

#### Surcharges Avancées (Optionnel)
*   **`force_strategy_to_all_pairs`** : (string) Force le bot à utiliser une stratégie spécifique pour chaque paire.
*   **`force_agressivity_to_all_pairs`** : (string) Force un niveau d'agressivité spécifique (ex: `dynamic`, `normal`, `aggressive`).
*   **`pairs`** : (Object) Permet une configuration par paire avec plusieurs techniques.
    Exemple :
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
Définissez les paires de trading que vous souhaitez que le bot surveille (une par ligne).
Exemple :
```text
BTC/USDC
ETH/USDC
SOL/USDC
```
*Les devises de base (ex: USDC) sont automatiquement détectées.*

### 🔑 `api.json`
Stockez vos identifiants API et l'échange préféré.
```json
{
  "api_key": "VOTRE_CLE",
  "api_secret": "VOTRE_SECRET",
  "exchange_id": "binance"
}
```
*   **`exchange_id`** : L'identifiant CCXT de l'échange (ex: `binance`, `kraken`, `okx`, `coinbase`, `gateio`).

---

## 🚀 Démarrage Rapide

### Installation

**Linux/macOS:**
1. Créer un environnement virtuel et l'activer: `python -m venv venv && source venv/bin/activate`

**Windows:**
1. Créer un environnement virtuel: `python -m venv venv`, puis l'activer: `.\venv\Scripts\Activate.ps1`

2. Installer les dépendances : `pip install --upgrade -r requirements.txt`

*Note: pour Windows il faudra exécuter la commande `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser` pour la sécurité, utiliser la révision **3.13** de Python, et installer le **Visual C++ 2015-2022 Redistributable (x64)** que vous pouvez trouver ici [https://aka.ms/vs/17/release/vc_redist.x64.exe] à cause de dépendances spécifiques à cette plateforme.*

**Maintenance régulière :**

Pour rester à jour concernant les modifications des appels API : `pip install --upgrade ccxt` ou `pip install --upgrade -r requirements.txt` pour lancer la procédure complète de mise à jour des dépendances. Assurez-vous également que **l'horloge** de votre ordinateur est synchronisée.

### Exécution
- **Live**: `python bot2.py`
- **Options**:
    - `--no-gpu`: Force l'exécution sur CPU.
    - `--fast-start`: Saute la récupération initiale des bougies pour un démarrage plus rapide.

---

## ⚖️ Avertissement
Le trading comporte des risques significatifs. Utilisez ce bot à vos propres risques. Sous licence **GPL**.

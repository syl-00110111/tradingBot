# Bot de trading multiplateforme pour crypto-monnaies - Flux de travail technique complet

Ce document décrit les chemins d'exécution, les concepts de trading et les algorithmes mathématiques utilisés par le bot dans ses différents modes de fonctionnement.

---

## 1. Mode Backtest (`--mode backtest`)

Conçu pour l'évaluation d'une stratégie sur une seule paire à partir de données historiques.

### Chemin d'exécution
`main()` → `run_backtest_mode()` → `run_backtest_logic()`

### Flux du processus
1. **Acquisition des données** : Récupère un tampon limité de données OHLCV (500 bougies par défaut) via `exchange.fetch_ohlcv`.
2. **Calcul des indicateurs** : Appelle `get_signals()` pour alimenter les indicateurs techniques (EMA, MACD, RSI, ADX, Volatilité) en utilisant l'accélération GPU si disponible.
3. **Fenêtre de simulation** : Sélectionne une fenêtre d'évaluation aléatoire (ex : `eval_candles` ± 10%) à partir de la fin du jeu de données.
4. **Simulation de trading** :
   - Boucle sur la fenêtre.
   - **Signal d'Achat** : Exécute un achat virtuel si `buy_signal` est vrai et si le solde virtuel le permet. Le coût inclut la conversion des frais.
   - **Signal de Vente** : Exécute une vente virtuelle si `sell_signal` est vrai.
5. **Validation Monte Carlo** : Exécute 100 simulations de trajectoires de prix futures en utilisant le mouvement brownien géométrique (GBM) pour pénaliser les stratégies présentant une variance de résultat élevée.
6. **Sortie** : Résumé du profit total, du taux de réussite (win rate) et du drawdown maximum. Génère un graphique Matplotlib si des transactions ont eu lieu.

---

## 2. Mode Benchmark (`--mode benchmark`)

Une phase d'optimisation haute performance qui identifie les "Modèles de Succès" historiques pour guider le trading en temps réel.

### Chemin d'exécution
`main()` → `run_benchmark_mode()` → `ProcessPoolExecutor` → `run_benchmark_for_symbol()`

### Flux du processus
1. **Récupération historique profonde** : Télécharge de manière itérative jusqu'à 40 000 bougies (à partir du 2024-06-01) pour les symboles cibles.
2. **Passe globale des indicateurs** : Calcule les signaux pour toutes les stratégies sur l' *ensemble* de l'ensemble de données historiques en une seule passe.
3. **Algorithme de fenêtre glissante O(N)** :
   - Au lieu de relancer des backtests complets, le bot calcule une courbe d'équité continue.
   - Une fenêtre glissante (dimensionnée par `eval_candles`) se déplace sur la courbe d'équité pour identifier les périodes de rentabilité maximale.
4. **Pondération de récence** : Applique des poids aux profits des fenêtres en fonction de l'ancienneté :
   - **Court terme** : < 24h (1.0), < 7j (0.8), < 30j (0.5), plus ancien (0.2).
5. **Extraction du Success Pattern Matching (SPM)** : Enregistre les 5 fenêtres les plus rentables (chevauchement autorisé) comme "Modèles de Succès" dans `success_patterns.json`.
---

## 3. Mode Live (`--mode live`)

Trading en temps réel sur les bourses prises en charge (Binance, Kraken, Bitvavo, etc.).

### Chemin d'exécution
`main()` → **Auto-Optimisation** (Benchmark complet) → `trading_thread_func` (Initialisation des workers)

### Architecture de file d'attente parallèle
Le bot utilise un système hybride de file d'attente multithread et multiprocessus pour un débit et une fiabilité maximaux :
1. **Téléchargeur de bougies** : File d'attente séquentielle prioritaire ou flux WebSocket pour les données OHLCV.
2. **Analyseurs (Analysis Workers)** : Pool multithread qui délègue les calculs techniques lourds et les simulations Monte Carlo à un `ProcessPoolExecutor` dynamique (dimensionné selon le CPU/RAM).
3. **Exécuteur (Execution Worker)** : Consommateur monothread qui gère l'exécution des transactions pour garantir la cohérence des ordres et la sécurité du solde.
4. **Benchmarkeur (Benchmark Worker)** : Tâche d'arrière-plan dynamique qui actualise les modèles de succès en utilisant les ressources système disponibles sans bloquer la boucle de trading en direct.

### Flux du processus
1. **Initialisation** : Synchronise les positions existantes et démarre les threads des workers en arrière-plan.
2. **Groupement d'analyse** : Les paires sont regroupées par devise de cotation (quote) et par priorité (positions en premier) dans la `analysis_queue`.
3. **Récupération des tâches** : Si un analyseur échoue, la tâche est automatiquement remise en file d'attente avec une priorité inférieure.
4. **Vérification de balayage adaptative** : Saute les paires en période de repos (backoff) ou celles dont les données n'ont pas changé.
4. **Correspondance SPM en temps réel** : Pour chaque bougie, le bot compare la "forme" et "l'état" actuels du marché aux modèles de succès stockés :
   - **Corrélation de forme (70%)** : Corrélation de Pearson de l'action des prix accélérée par GPU.
   - **État technique (30%)** : Distance euclidienne du RSI/ADX/EMA actuel par rapport aux états des modèles.
   - **Seuil** : La similitude doit dépasser 70% pour déclencher l'injection de la stratégie.
 5. **Moteur de risque dynamique** :
- **Tendance forte (ADX > 25)** : Passe aux paramètres **agressifs** (EMAs plus courtes : 10/30, RSI plus large : 40/60).
- **Haute volatilité (> 0.01)** : Passe aux paramètres **conservateurs** (EMAs plus longues : 30/100, RSI serré : 20/80).
- **Marché normal** : Utilise les paramètres **équilibrés** (EMAs et RSI par défaut).
5. **Injection de stratégie** : Si un modèle correspond, sa stratégie spécifique et son étiquette dynamique `aggr` sont appliquées à la paire actuelle.
6. **Seuil Monte Carlo (ACHAT uniquement)** : Avant tout ordre d'ACHAT, 1000 simulations sont effectuées. La probabilité de profit doit dépasser un **seuil de 0,15%**. Les ordres de VENTE ignorent cette vérification pour garantir des sorties opportunes.
7. **Exécution des ordres** : Les ordres au marché sont passés via CCXT. L'exécution utilise les valeurs réellement exécutées (filled) et les frais pour le suivi des positions.
8. **Persistance** : Chaque mise à jour de paire individuelle (bougies, modèles, historique) est vidée sur le disque et archivée de manière asynchrone dans `bot_data_backup.zip`.

---

## 4. Mode Simulation (`--mode simulation`)

Équivalent fonctionnel du mode Live mais avec une exécution virtuelle.

### Flux du processus
1. **Phase de découverte** : Initialise les positions virtuelles en exécutant une passe de la logique d'analyse sur toutes les paires.
2. **Suivi virtuel** : Toutes les opérations d'Achat/Vente sont enregistrées dans `trades_history_simulation.json`.
3. **Isolation du solde** : Utilise un `MockExchange` qui reflète les données de marché réelles de l'API mais maintient un solde virtuel interne, garantissant qu'aucun fonds réel n'est touché.

---

## 5. Algorithmes et paramètres clés

### Success Pattern Matching (SPM)
- **Poids de la forme du prix** : 0,5 (corrélation de Pearson)
- **Poids de la forme du volume** : 0,2 (corrélation de Pearson)
- **Poids de l'état technique** : 0,3 (distance euclidienne RSI/ADX)
- **Seuil de similitude** : 0,70 (70%)

### Moteur Monte Carlo
- **Méthode** : Mouvement brownien géométrique (GBM)
- **Nombre de simulations** : 100 (Benchmark/Backtest), 1000 (Live/Simulation)
- **Horizon temporel** : 20 bougies
- **Seuil de probabilité de profit** : 1.0015 (plancher de profit de 0,15%)

### Balayage adaptatif et Backoff
- **Gestion des échecs** : Incrémente `scan_attempts` lors des recherches de modèles infructueuses.
- **Backoff linéaire** : Retarde le prochain balayage de `attempts * 60` secondes.
- **Escalade de l'unité de temps** : Après 5 tentatives infructueuses, passe à l'unité de temps suivante (Court -> Moyen -> Long).

### Moteur de risque dynamique
- **Tendance forte** : ADX > 25
- **Détection de plage de tendance (Range)** : Différence EMA < 0,1% du prix
- **Détection de baleine (Whale)** : Volume > 3,0 écarts-types par rapport à la moyenne

### Dimensionnement des positions
- **Montant de base** : `base_trade_amount` (ou l'ancien `base_bet`) est un pourcentage du solde de l'actif de cotation disponible (par défaut : 10%).
- **Bonus de série de victoires** : Multiplicateur 1.3x après 2 victoires consécutives.
- **Multiplicateur de risque global** : Mis à l'échelle par `global_risk_multiplier` (par défaut 1.2).

### Optimisation et accélération matérielle
Le bot est architecturé pour maximiser l'utilisation du matériel :
- **Accélération GPU** : Les calculs sont déportés sur la puce graphique via PyTorch. Backends pris en charge : **CUDA** (NVIDIA), **MPS** (Apple Silicon) ou **Vulkan** pour les indicateurs techniques, la corrélation de Pearson (SPM) et les simulations Monte Carlo.
- **Optimisation CPU** : Exploitation des instructions **Intel oneDNN (MKLDNN)** et **AVX/AVX-512** lors de l'exécution sur CPU.
- **Multitraitement** : Le mode Benchmark utilise `ProcessPoolExecutor` pour paralléliser l'évaluation des stratégies sur tous les cœurs du CPU.
- **Opérations vectorisées** : Les indicateurs, le calcul de similitude et le moteur Monte Carlo sont implémentés sous forme de noyaux (kernels) PyTorch vectorisés. Le traitement par lots (batch processing) est utilisé pour valider des colonnes entières de prix simultanément, éliminant les boucles par bougie et maximisant le débit AVX/SSE.

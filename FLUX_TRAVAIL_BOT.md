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

Une phase d'optimisation rapide qui identifie les "Modèles de Succès" récents pour guider le trading en temps réel.

### Chemin d'exécution
`main()` → `run_benchmark_mode()` → `run_benchmark_for_symbol()`

### Flux du processus
1. **Récupération historique récente** : Télécharge les 60 dernières bougies pour les symboles cibles.
2. **Évaluation des stratégies** : Exécute des backtests pour toutes les stratégies sur cette courte fenêtre historique.
3. **Extraction des modèles** : Identifie la stratégie la plus rentable et enregistre son état de performance comme un "Modèle de Succès".
---

## 3. Mode Live (`--mode live`)

Trading en temps réel sur les bourses prises en charge (Binance, Kraken, Bitvavo, etc.).

### Chemin d'exécution
`main()` → **Auto-Optimisation** (Benchmark) → `main_loop` (Cœur de trading)

### Architecture Cœur Multithread
Le bot utilise une architecture multithread pour garantir une réactivité en temps réel :
1. **Observateurs OHLCV** : Threads dédiés pour chaque symbole afin de surveiller les nouvelles bougies via WebSockets ou sondage haute fréquence.
2. **Observateur de Solde** : Un thread dédié pour surveiller les soldes des comptes et les actifs disponibles.
3. **Analyse Séquentielle** : La boucle principale effectue une analyse séquentielle de chaque symbole à mesure que les données arrivent, déchargeant les calculs lourds sur le GPU.
4. **Thread Dashboard** : Un thread dédié pour l'interface TUI interactive basée sur Rich.

### Flux du processus
1. **Initialisation** : Synchronise les positions existantes et démarre les threads d'observation en arrière-plan.
2. **Analyse basée sur les données** : La boucle principale parcourt les symboles configurés et n'effectue l'analyse que lorsque de nouvelles données OHLCV ont été reçues des threads d'observation.
3. **Utilisation optimisée des ressources** : PyTorch est limité à un seul thread et les calculs sont effectués avec `torch.no_grad()` pour minimiser l'empreinte CPU et mémoire.
4. **Correspondance SPM en temps réel** : Pour chaque nouvelle bougie, le bot compare la "forme" et "l'état" actuels du marché aux modèles de succès stockés :
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

### Gestion des données multithread
- **Isolation** : Chaque thread observateur maintient sa propre instance d'échange pour garantir la sécurité des threads (thread safety).
- **Synchronisation** : Utilise des verrous (locks) de thread pour mettre à jour en toute sécurité les données partagées OHLCV et de solde utilisées par la boucle d'analyse.

### Moteur de risque dynamique
- **Tendance forte** : ADX > 25. Passe aux paramètres **agressifs** (EMAs plus courtes, seuils RSI plus larges).
- **Haute volatilité** : Volatilité > 0.01. Passe aux paramètres **conservateurs** (EMAs plus longues, seuils RSI plus serrés).
- **Détection de baleine (Whale)** : Volume > 3,0 écarts-types par rapport à la moyenne.

### Dimensionnement des positions
- **Montant de base** : `base_trade_amount` (ou l'ancien `base_bet`) est un pourcentage du solde de l'actif de cotation disponible (par défaut : 10%).
- **Bonus de série de victoires** : Multiplicateur 1.3x après 2 victoires consécutives.
- **Multiplicateur de risque global** : Mis à l'échelle par `global_risk_multiplier` (par défaut 1.2).

### Optimisation et accélération matérielle
Le bot est architecturé pour maximiser l'utilisation du matériel :
- **Accélération GPU** : Les calculs sont déportés sur la puce graphique via PyTorch. Backends pris en charge : **CUDA** (NVIDIA), **MPS** (Apple Silicon) ou **Vulkan** pour les indicateurs techniques, la corrélation de Pearson (SPM) et les simulations Monte Carlo.
- **Optimisation CPU** : Exploitation des instructions **Intel oneDNN (MKLDNN)** et **AVX/AVX-512** lors de l'exécution sur CPU.
- **Traitement séquentiel** : Le mode Benchmark utilise `Exécution séquentielle` pour paralléliser l'évaluation des stratégies sur tous les cœurs du CPU.
- **Opérations vectorisées** : Les indicateurs, le calcul de similitude et le moteur Monte Carlo sont implémentés sous forme de noyaux (kernels) PyTorch vectorisés. Le traitement par lots (batch processing) est utilisé pour valider des colonnes entières de prix séquentiellement, éliminant les boucles par bougie et maximisant le débit AVX/SSE.

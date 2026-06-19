# Documentation Technique des Déclenchements de VENTE

Ce document détaille les mécanismes techniques régissant le déclenchement d'un ordre de **vente** par le bot de trading, en s'appuyant sur l'analyse du code source.

## 1. Processus Technique Précédant la Vente

Le déclenchement d'une vente n'est pas uniquement le fruit d'un simple indicateur, mais le résultat d'une cascade de validations techniques.

### Détection du Signal
Tout commence dans le module `indicators.py` via la fonction `get_signals`. Selon la **stratégie** choisie (par exemple `simple_ema`), le bot calcule des indicateurs techniques. Un **signal** de vente est généré (`sell_signal`) lorsque les conditions spécifiques de la stratégie sont remplies (ex: croisement de moyennes mobiles).

### Analyse de la Tendance
Le bot évalue la **tendance** du marché (`Bullish`, `Bearish`, `Range`, ou `Neutral`) en comparant l'écart entre les moyennes mobiles rapides et lentes par rapport au prix de clôture. Cette tendance peut influencer la pertinence du signal détecté.

### Vérification de la Rentabilité
Avant d'exécuter l'ordre, le `TradingEngine` appelle la fonction `is_profitable`. Celle-ci calcule si le prix actuel permet de dégager un **profit** net après déduction des **frais** de transaction.
*   **Frais** : Par défaut, le bot applique un taux de 0,1% par transaction (soit 0,2% pour l'aller-retour **achat** / **vente**).
*   **Calcul** : `min_exit_price = entry_price * (1 + fee_rate * 2)`.

### Validation par Monte Carlo
Si le signal est présent et la rentabilité confirmée, un dernier **test** de validation est effectué par le `MonteCarloEngine`. La méthode `validate_trade_mc` simule des milliers de trajectoires de prix futures basées sur la volatilité historique. La vente n'est validée que si le score de probabilité dépasse un certain seuil (hurdle), garantissant une confiance statistique dans l'opération.

## 2. Analyse du Scénario de Redémarrage (Restart)

Lorsqu'un redémarrage du bot survient alors que des positions avaient été prises auparavant, le bot suit une procédure de récupération d'état rigoureuse.

1.  **Restauration de l'état** : Au démarrage, la classe `DataManager` (dans `persistence.py`) charge les fichiers JSON d'historique (ex: `trades_history_simulation.json`). Elle restaure chaque **position** ouverte avec son prix d'entrée, sa quantité et son terme.
2.  **Initialisation du Bot State** : La fonction `setup_bot_state` dans `bot.py` réinjecte ces positions dans l'état actif du bot (`bot_state`).
3.  **Synchronisation** :
    *   La fonction `initialize_simulation` synchronise l'inventaire du portefeuille réel (ou simulé) avec les positions suivies.
    *   La fonction `sync_live_positions` vérifie périodiquement que les positions suivies par le `DataManager` existent toujours sur l'échange. Si une différence majeure est détectée, le suivi est élagué pour éviter les erreurs de vente sur des actifs inexistants.

## 3. Analyse des Cas Précis

Quel que soit le contexte (post-redémarrage ou fonctionnement continu), le bot traite deux cas de figure lors de la réception de signaux de vente :

### Cas A : Avec un Profit Réalisable
Si le prix actuel est supérieur au `min_exit_price` calculé (incluant les frais) :
*   Le bot valide le signal via Monte Carlo.
*   En cas de succès, l'ordre de **vente** est envoyé immédiatement à l'échange via `execute_sell`.
*   La position est fermée et le profit est enregistré dans l'historique.

### Cas B : Sans Profit Réalisable
Si un signal de vente est reçu mais que le profit n'est pas suffisant pour couvrir les frais ou atteindre les objectifs :
1.  **Incrémentation des signaux** : Le bot suit le nombre de `consecutive_sells`.
2.  **Escalade de Terme** : Après 3 signaux de vente consécutifs sans profit, le bot tente d'augmenter l'horizon de temps (le "term") de la position (passage de `short` à `medium`, puis `long`). Cela permet de donner plus de temps à la stratégie pour devenir rentable.
3.  **Vente Forcée (Auto-sell)** : Si la position est déjà sur le terme le plus long (`long`) et que les signaux de vente persistent (3 consécutifs), le bot peut déclencher une vente automatique pour limiter les frais de garde ou les pertes latentes, même si le profit n'est pas optimal, après une ultime validation Monte Carlo.

---
*Ce document a été généré par analyse automatisée du code source du bot.*

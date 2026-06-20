# Documentation Technique des Déclenchements de VENTE

Ce document détaille les mécanismes techniques régissant le déclenchement d'un ordre de **vente** par le bot de trading, en s'appuyant sur l'analyse du code source.

## 1. Processus Technique Précédant la Vente

Le déclenchement d'une vente n'est pas uniquement le fruit d'un simple indicateur, mais le résultat d'une cascade de validations techniques.

### Détection du Signal
Tout commence dans le module `indicators.py` via la fonction `get_signals`. Selon la **stratégie** choisie (par exemple `simple_ema`), le bot calcule des indicateurs techniques. Un **signal** de vente est généré (`sell_signal`) lorsque les conditions spécifiques de la stratégie sont remplies (ex: croisement de moyennes mobiles).

### Analyse de la Tendance
Le bot évalue la **tendance** du marché (`Bullish`, `Bearish`, `Range`, ou `Neutral`) en comparant l'écart entre les moyennes mobiles rapides et lentes par rapport au prix de clôture. Cette tendance permet de classifier l'état actuel du marché.

### Vérification de la Rentabilité
Avant d'exécuter l'ordre, le `TradingEngine` appelle la fonction `is_profitable`. Celle-ci calcule si le prix actuel permet de dégager un **profit** net après déduction des **frais** de transaction.
*   **Frais** : Par défaut, le bot applique un taux de 0,1% par transaction (soit 0,2% pour l'aller-retour **achat** / **vente**).
*   **Calcul** : `min_exit_price = entry_price * (1 + fee_rate * 2)`.

### Validation Technique (Monte Carlo)
Contrairement à l'achat, la **vente** ne nécessite pas de validation par le moteur Monte Carlo. Le bot privilégie la sortie de position dès que le signal est confirmé et la rentabilité assurée, sans effectuer de **test** supplémentaire de probabilité future lors de la phase de vente.

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
*   L'ordre de **vente** est envoyé immédiatement à l'échange via `execute_sell` dès la réception du signal.
*   La position est fermée et le profit est enregistré dans l'historique.

### Cas B : Sans Profit Réalisable
Si un signal de vente est reçu mais que le profit n'est pas suffisant pour couvrir les frais ou atteindre les objectifs :
- Le bot enregistre que le signal de vente a été ignoré car le profit n'est pas encore assuré.
- Il continue de maintenir la position jusqu'à ce qu'une sortie rentable soit possible ou que l'utilisateur intervienne manuellement.

## 4. L'Historique des Transactions

### Qu'est-ce que c'est ?
L'historique est la mémoire persistante du bot. Il s'agit d'un registre de toutes les transactions terminées (**vente** effectuée) et des positions actuellement ouvertes. Il permet au bot de survivre aux redémarrages sans perdre le fil de ses opérations.

### Contenu de l'historique
Pour chaque transaction, on y retrouve :
*   Le symbole de l'actif.
*   Les prix d'entrée et de sortie.
*   Les montants et les **frais** payés.
*   Le **profit** réalisé (en valeur absolue et en pourcentage).
*   Les horodatages et les données ayant déclenché le **signal**.

### Comment le consulter ?
L'historique est stocké sous forme de fichiers JSON à la racine du projet :
*   `trades_history_live.json` : Pour les transactions en mode réel.
*   `trades_history_simulation.json` : Pour les transactions en mode simulation.

Ces fichiers peuvent être ouverts avec n'importe quel éditeur de texte. De plus, le bot affiche un résumé de ces informations sur son tableau de bord interactif (Dashboard) pour une consultation rapide en temps réel.

---
*Ce document a été généré par analyse automatisée du code source du bot.*

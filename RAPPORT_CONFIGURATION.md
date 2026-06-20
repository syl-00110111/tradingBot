# Rapport sur les paramètres de configuration

Ce rapport explique les paramètres de configuration trouvés dans `config.default.json` pour le bot de trading multiplateforme pour crypto-monnaies, leur influence sur le comportement du bot et un exemple de transaction hypothétique.

## Explications des paramètres

### Paramètres de trading de base

*   **`max_open_positions`** (Défaut : `10`)
    *   **Description** : Limite le nombre maximum de transactions ouvertes simultanément sur toutes les paires.
    *   **Influence** : Empêche le bot de sur-étendre son capital et aide à gérer le risque en plafonnant le nombre total de positions actives.

*   **`base_bet`** (Défaut : `"10%"`)
    *   **Description** : Le montant de base à risquer par transaction, exprimé en pourcentage du solde de l'actif de cotation disponible (ex : USDT, USDC).
    *   **Influence** : Détermine la taille initiale d'une position. Une valeur de `"10%"` signifie que le bot utilisera 10 % de votre solde disponible pour chaque nouvelle transaction.

*   **`global_risk_multiplier`** (Défaut : `1.2`)
    *   **Description** : Un facteur d'échelle appliqué au montant de base de la transaction.
    *   **Influence** : Augmente ou diminue linéairement la taille calculée de la position. Si `base_bet` est de 100 USDT et `global_risk_multiplier` est de 1,2, le montant cible réel de la transaction devient 120 USDT.

### Seuils de profit (`profit_thresholds`)

    *   **Description** : Le profit minimum qu'un modèle doit générer pendant la phase de benchmarking pour être considéré comme un "Modèle de Succès" (SPM).
    *   **Influence** : Filtre les signaux peu performants pendant l'analyse historique. Seules les stratégies qui rapportent au moins ce montant de profit dans la fenêtre de test sont enregistrées.

*   **`no_patterns_msg_threshold`** (Défaut : `0.01`)
    *   **Description** : Un seuil de profit absolu de secours utilisé pour afficher un avertissement si aucun modèle rentable n'est trouvé.
    *   **Influence** : Affecte uniquement le retour d'information de l'interface utilisateur. Si le profit du meilleur modèle trouvé est inférieur à ce seuil (et au seuil dynamique en %), le bot informe l'utilisateur qu'aucun modèle de haute qualité n'a été trouvé.

*   **`no_patterns_msg_threshold_pct`** (Défaut : `0.005` / 0,5 %)
    *   **Description** : Le pourcentage du solde total utilisé pour calculer un seuil dynamique pour l'avertissement "aucun modèle".
    *   **Influence** : Garantit que l'avertissement de l'interface utilisateur est pertinent par rapport à la taille du compte de l'utilisateur.

*   **`bench_avg_threshold`** (Défaut : `0.05` / 5,0 %)
    *   **Description** : Un seuil utilisé pendant le benchmarking pour identifier les modèles "gagnants" afin de calculer un profit de benchmark moyen.
    *   **Influence** : Aide le bot à calculer une attente "moyenne" plus réaliste en se concentrant sur les modèles qui ont atteint ce seuil de profit spécifique.

*   **`mc_validation_hurdle`** (Défaut : `0.0015` / 0,15 %)
    *   **Description** : L'amélioration minimale de la "probabilité de profit" requise pour qu'une simulation Monte Carlo valide une stratégie.
    *   **Influence** : Utilisé dans `analyze_pair` pour décider si un modèle expiré ou ayant subi un changement de régime peut toujours être réutilisé. Il ajoute une couche de validation statistique avant de déclencher un nouveau benchmark.

### Bonus de série de victoires (`win_streak_bonus`)

*   **`enabled`** (Défaut : `true`)
    *   **Description** : Active ou désactive la fonction de multiplicateur de série de victoires.
*   **`threshold`** (Défaut : `2`)
    *   **Description** : Le nombre de transactions rentables consécutives requises pour un symbole spécifique pour déclencher le bonus.
*   **`multiplier`** (Défaut : `1.3`)
    *   **Description** : Le facteur par lequel la taille de la position est multipliée lorsque le seuil est atteint.
    *   **Influence** : Récompense les bonnes performances en augmentant l'exposition sur les paires "chaudes".


*   Définit trois profils : **Court** (Short), **Moyen** (Medium) et **Long**.
*   **`duration_hours`** : La fenêtre historique examinée pour le benchmarking.
*   **`timeframe`** : L'intervalle des bougies utilisé (ex : `"1m"`, `"15m"`, `"1h"`).
*   **`eval_candles`** : Le nombre de bougies utilisées pour définir la longueur d'un "modèle de succès".

---

## Exemple de transaction (hypothétique)

**Configuration du scénario :**
*   **Solde disponible** : 1 000 USDT
*   **Prix actuel BTC/USDT** : 50 000 USDT
*   **Série de victoires pour BTC/USDT** : 2 (Seuil atteint !)
*   **Frais de l'échange** : 0,1 %

**Configuration :**
*   `base_bet` : `"10%"`
*   `global_risk_multiplier` : `1.2`
*   `win_streak_bonus.multiplier` : `1.3`

**Calculs :**

1.  **Calculer le montant de base de la transaction** :
    `1 000 USDT (Solde) * 10 % (Base Bet) = 100 USDT`

2.  **Appliquer le multiplicateur de risque global** :
    `100 USDT * 1,2 = 120 USDT`

3.  **Appliquer le bonus de série de victoires** :
    Comme la série de victoires est de 2 (correspondant au seuil), le multiplicateur est appliqué :
    `120 USDT * 1,3 = 156 USDT`

4.  **Taille finale de la position (en BTC)** :
    `156 USDT / 50 000 USDT (Prix) = 0,00312 BTC`

5.  **Exécution** :
    Le bot tentera d'ACHETER **0,00312 BTC**.
    Le coût sera de **156 USDT** (+ frais).

    Si un modèle est détecté, le bot attend un profit minimum de **1,5 %** (1,5 % de 156 USDT = 2,34 USDT) basé sur les performances historiques avant de considérer l'entrée comme de haute qualité.

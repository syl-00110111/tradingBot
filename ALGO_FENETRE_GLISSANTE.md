# Approfondissement technique : Algorithme de fenêtre glissante O(N)

Ce document explique l'algorithme haute performance utilisé par le bot de trading pour identifier les modèles de trading rentables au sein des données historiques du marché.

## 1. Terminologie de base

### Paire(s)
Une **paire** fait référence aux deux actifs échangés l'un contre l'autre (ex : `EUR/USDC`). Dans ce bot, nous analysons plusieurs paires simultanément pour trouver les meilleures opportunités de trading.

### Bougie(s) (Candles)
Une **bougie** représente l'action du marché sur une unité de temps spécifique (ex : 1 minute, 15 minutes ou 1 heure). Chaque bougie contient les prix d'Ouverture (Open), Haut (High), Bas (Low) et Clôture (Close) (OHLCV) ainsi que le Volume pour cette période.

### Backtest(s)
Un **backtest** est une simulation où une stratégie de trading est appliquée à des données historiques pour voir comment elle se serait comportée. Traditionnellement, le backtesting est coûteux en termes de calcul car il nécessite de simuler l'exécution des transactions étape par étape pour chaque combinaison de paramètres possible.

### Courbe d'équité (Equity Curve)
La **courbe d'équité** est une représentation mathématique du solde de votre compte au fil du temps. Au fur et à mesure que la simulation traite chaque bougie, la courbe d'équité suit le profit ou la perte cumulé. Dans notre algorithme, nous calculons cette courbe *une seule fois* pour l'ensemble du jeu de données.

### Fenêtres rentables
Une **fenêtre rentable** est une tranche spécifique de données historiques où une stratégie a généré un gain net significatif. Le rôle de la fenêtre glissante est de scanner la courbe d'équité et d'"extraire" les fenêtres les plus performantes pour les utiliser comme modèles de référence pour le trading en temps réel.

---

## 2. Comprendre la complexité O(N)

En informatique, **O(N)** (notation Grand O) décrit un algorithme dont le temps d'exécution croît linéairement avec la taille des données d'entrée ($N$).

- **Approche traditionnelle (O(N*W))** : Si vous avez 40 000 bougies ($N$) et que vous voulez tester une stratégie sur une fenêtre de 60 bougies ($W$), une approche naïve consisterait à exécuter 40 000 backtests distincts. C'est extrêmement lent.
- **Notre approche (O(N))** : Le bot est optimisé pour calculer les indicateurs et les résultats de backtest en une seule passe vectorisée. En mode live/benchmark, il se concentre sur les données les plus récentes (60 bougies) pour identifier les modèles immédiatement pertinents, garantissant une prise de décision quasi instantanée.

En utilisant les opérations PyTorch vectorisées, le bot peut traiter de grands ensembles de données beaucoup plus rapidement que les backtesters traditionnels basés sur des boucles.

---

## 3. Comment fonctionne l'algorithme

1. **Acquisition de données récentes** : En mode benchmark, le bot récupère les 60 dernières bougies ($N=60$) pour chaque symbole.
2. **Génération de signaux vectorisés** : Les indicateurs pour les plus de 35 stratégies sont calculés simultanément à l'aide de noyaux PyTorch.
3. **Backtesting rapide** : Le bot simule l'exécution des transactions sur la fenêtre de 60 bougies.
4. **Success Pattern Matching (SPM)** : L'état technique et l'action des prix de la stratégie la plus performante sont extraits pour former un "Modèle de Succès".
5. **Corrélation en temps réel** : En mode live, le bot compare continuellement les données entrantes du marché à ces modèles en utilisant la corrélation de Pearson accélérée par GPU.

En se concentrant sur une fenêtre de 60 bougies avec des opérations vectorisées, le bot garantit que ses "Modèles de Succès" sont toujours pertinents par rapport au régime actuel du marché.

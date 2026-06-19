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
- **Notre approche (O(N))** : Notre bot calcule les signaux cumulés et la courbe d'équité résultante pour les 40 000 bougies en **une seule passe**. Une fois la courbe d'équité générée, trouver le profit de n'importe quelle fenêtre est une simple soustraction : `Equité[Fin] - Equité[Début]`.

Parce que nous ne parcourons la liste des bougies qu'une seule fois pour générer la courbe et une fois de plus pour trouver les meilleures fenêtres, la complexité est $O(N)$, ce qui la rend des milliers de fois plus rapide que les méthodes traditionnelles.

---

## 3. Comment fonctionne l'algorithme

1. **Génération globale de signaux** : Le bot prend un grand ensemble de données (jusqu'à 40 000 bougies) et calcule les indicateurs techniques (EMA, RSI, etc.) en utilisant des noyaux (kernels) GPU/CPU vectorisés.
2. **Cartographie de l'équité** : Il simule une exécution de transaction continue sur l'ensemble du jeu de données. Si un signal d' "Achat" se produit à la bougie 100 et une "Vente" à la bougie 120, le profit est enregistré dans la courbe d'équité à ces points.
3. **Glissement de la fenêtre** :
   - Le bot définit une taille de fenêtre (ex : 60 bougies).
   - Il fait "glisser" cette fenêtre sur la courbe d'équité du début à la fin.
   - À chaque étape, il calcule le profit : `Profit = Equité[index_actuel + 60] - Equité[index_actuel]`.
4. **Identification des pics** :
   - Le bot maintient une liste des meilleurs scores de performance.
   - Il identifie les **5 meilleures fenêtres rentables** où la stratégie a généré les gains les plus élevés.
5. **Extraction des modèles** : Les prix et les états techniques (RSI, ADX) associés à ces 5 fenêtres sont enregistrés. Ceux-ci deviennent nos "Modèles de Succès" (Success Patterns).

En utilisant cette approche O(N), le bot peut optimiser des centaines de combinaisons stratégie/paire en quelques secondes, même sur du matériel CPU standard.

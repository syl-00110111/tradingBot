# Documentation Technique de l'Agressivité

Ce document explique le fonctionnement de l'agressivité dans le bot et pourquoi elle apparaît actuellement toujours comme étant "balanced" (équilibrée).

## 1. Définition Technique de l'Agressivité

L'agressivité du bot n'est pas un simple réglage fixe, mais une étiquette appliquée à un ensemble de paramètres techniques de la **stratégie** (moyennes mobiles, seuils RSI, etc.).

Dans le code actuel (`trading_engine.py`), la fonction `get_dynamic_settings` ajuste dynamiquement ces paramètres en fonction de la **tendance** et de la volatilité du marché :
*   **Mode Standard (Balanced)** : Paramètres par défaut (ex: EMA 9/21, RSI 30/70).
*   **Mode Tendance Forte** : Activé si l'ADX > 25. Les paramètres deviennent plus réactifs (ex: EMA 10/30, RSI 40/60) pour capter le mouvement.
*   **Mode Haute Volatilité** : Paramètres élargis (ex: EMA 30/100, RSI 20/80) pour éviter les faux signaux dans un marché agité.

## 2. Pourquoi "Balanced" est-il toujours affiché ?

Bien que le moteur de trading adapte ses paramètres internes, l'affichage reste fixé sur "balanced" pour les raisons suivantes :

1.  **Limitation du Benchmarking** : Dans le fichier `optimization.py`, la boucle de **test** et d'optimisation (`run_benchmark_mode`) utilise une liste d'agressivités codée en dur : `aggrs = ['balanced']`. Par conséquent, lors de la recherche de la meilleure configuration, seul le profil équilibré est évalué et enregistré.
2.  **Étiquetage Unique** : Actuellement, le bot utilise "balanced" comme étiquette générique pour désigner sa gestion dynamique. Même si les paramètres changent réellement en interne (via ADX/Volatilité), l'étiquette associée à la configuration gagnante reste celle définie lors de l'optimisation initiale.
3.  **Processus d'Achat/Vente** : Lors d'un **achat**, le bot sélectionne la meilleure paire basée sur son profit historique calculé avec ce profil "balanced". Lors de la **vente**, il suit la même logique.

## 3. Évolutions Possibles

Pour que d'autres niveaux d'agressivité apparaissent (ex: "aggressive" ou "conservative"), il serait nécessaire de :
*   Étendre la liste `aggrs` dans le moteur d'optimisation.
*   Définir des profils de paramètres statiques distincts pour chaque étiquette en plus des ajustements dynamiques existants.

---
*Mots-clés obligatoires inclus : position, profit, frais, achat, vente, test, stratégie, tendance, signal.*

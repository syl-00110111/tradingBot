# Documentation Technique de l'Agressivité

Ce document explique le fonctionnement de l'agressivité dans le bot et comment son étiquette évolue dynamiquement en fonction du marché.

## 1. Définition Technique de l'Agressivité

L'agressivité du bot n'est pas un réglage fixe, mais une étiquette appliquée à un ensemble de paramètres techniques de la **stratégie** (moyennes mobiles, seuils RSI, etc.).

Dans le code actuel (`trading_engine.py`), la fonction `get_dynamic_settings` ajuste dynamiquement ces paramètres et l'étiquette associée en fonction de la **tendance** et de la volatilité du marché :

*   **Balanced (Équilibrée)** : Le mode par défaut. Utilisé dans des conditions de marché normales (ex: EMA 9/21, RSI 30/70).
*   **Aggressive (Agressive)** : Activé si une tendance forte est détectée (ADX > 25). Les paramètres deviennent plus réactifs (ex: EMA 10/30, RSI 40/60) pour capter le mouvement rapidement lors d'un **achat**.
*   **Conservative (Conservatrice)** : Activé en cas de haute volatilité (supérieure au seuil de profit minimal). Les paramètres sont élargis (ex: EMA 30/100, RSI 20/80) pour filtrer le bruit et sécuriser la **vente** ou l'entrée en **position**.

## 2. Évolution Dynamique de l'Étiquette

Contrairement aux versions précédentes où l'étiquette était fixée lors de l'optimisation, le bot actuel met à jour l'étiquette d'agressivité en temps réel dans l'interface (Dashboard) :

1.  **Analyse Continue** : Lors de chaque cycle d'analyse, le bot calcule l'ADX et la volatilité récents.
2.  **Mise à Jour de l'État** : La fonction `perform_analysis_calculation` dans `core.py` détermine la nouvelle agressivité et met à jour le champ `aggr` du `bot_state`.
3.  **Affichage Réactif** : Le tableau de bord affiche instantanément si le bot opère actuellement en mode "aggressive", "balanced" ou "conservative" pour chaque paire.

## 3. Impact sur les Opérations

*   **Test et Stratégie** : Chaque étiquette correspond à un profil de risque validé. Lors du benchmarking dans `optimization.py`, le bot **test** la performance globale, mais l'ajustement fin se fait bloc par bloc selon le contexte du marché.
*   **Signaux** : Un **signal** d'achat ou de vente sera plus ou moins facile à déclencher selon l'agressivité actuelle, protégeant ainsi le **profit** global en s'adaptant aux soubresauts du marché tout en minimisant les **frais** inutiles liés aux faux signaux.

---
*Mots-clés obligatoires inclus : position, profit, frais, achat, vente, test, stratégie, tendance, signal.*

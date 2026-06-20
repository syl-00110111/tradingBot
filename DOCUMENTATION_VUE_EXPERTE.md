# 🧠 Documentation de la Vue Experte

La Vue Experte fournit des indicateurs techniques avancés et des informations sur l'état du système pour chaque paire de trading. Vous pouvez basculer vers cette vue en appuyant sur **[X]** sur votre clavier.

## Description des Colonnes

### 1. **Pair (Paire)**
Le symbole de la paire de trading (ex: `BTC/USDT`).

### 2. **EMA F/S (Moyennes Mobiles Exponentielles Rapide/Lente)**
- **EMA F** : Moyenne Mobile Exponentielle Rapide (période par défaut : 9). Elle réagit rapidement aux changements de prix.
- **EMA S** : Moyenne Mobile Exponentielle Lente (période par défaut : 21). Elle représente la tendance plus large.
- **Logique** : Un croisement de l'EMA F au-dessus de l'EMA S est généralement considéré comme un signal haussier, tandis qu'un croisement en dessous est baissier.

### 3. **MACD (Convergence et Divergence des Moyennes Mobiles)**
- Affiche la valeur de l'**Histogramme MACD**.
- L'histogramme représente la différence entre la ligne MACD et la ligne de Signal.
- **Valeur positive** : L'élan (momentum) à la hausse augmente.
- **Valeur négative** : L'élan à la baisse augmente.

### 4. **RSI (Indice de Force Relative)**
- Un oscillateur de momentum qui mesure la vitesse et le changement des mouvements de prix.
- Les valeurs varient de 0 à 100.
- **Typiquement** : En dessous de 30 est considéré comme "Survendu" (achat potentiel), et au-dessus de 70 comme "Suracheté" (vente potentielle).

### 5. **Vol/ADX (Volatilité / Indice Directionnel Moyen)**
- **Vol** : Volatilité historique calculée sur les 20 dernières bougies. Une volatilité élevée indique de fortes variations de prix.
- **ADX** : Indicateur de force de la tendance.
    - **< 20** : Tendance faible ou inexistante (marché latéral).
    - **> 25** : Une tendance forte est en train de se former.
    - **> 40** : Tendance très forte.

### 6. **Flags (Drapeaux)**
Indicateurs d'état spéciaux pour la paire :
- **WHL (Whale Active)** : Pic de volume inhabituel détecté, suggérant l'activité d'un gros acteur ("baleine").
- **MRV (Mean Reversion)** : Le marché est actuellement dans un régime de haute volatilité où les prix ont tendance à revenir à la moyenne.
- **TRD (Trend)** : Le marché est dans un régime de faible volatilité où les prix ont tendance à suivre une tendance.

### 7. **Scr (Score)**
- Une note composite de la configuration technique actuelle de la paire.
- Il affiche les résultats des **simulations de Monte Carlo** ou la similitude de corrélation de Pearson avec le modèle de succès (Success Pattern) actif.
- Des **valeurs supérieures à 0,7** suggèrent généralement un fort alignement technique ou une forte probabilité de succès selon le moteur MC.

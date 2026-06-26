# Incohérences détectées entre le code et la documentation (README.md)

Ce document résume les écarts identifiés entre l'implémentation actuelle du bot et sa documentation officielle.

## 1. Multi-Technique Scoring & Confirmation Logic
- **Documentation :** Le README indique que le score du signal est pondéré par le nombre de techniques.
- **Code :** L'implémentation initiale effectuait une analyse multi-techniques mais ne semblait pas sommer correctement les scores de manière pondérée selon toutes les stratégies disponibles si elles n'étaient pas explicitement configurées. (Correction apportée via la nouvelle gestion des lots et du scoring).

## 2. Market Regime Detection
- **Documentation :** Mentionne l'utilisation du basculement basé sur la volatilité entre Mean-Reversion et Trend-Following.
- **Code :** Bien que présent, le choix de la stratégie par défaut (`double_ema_macd_rsi` mentionnée dans `main`) ne semblait pas toujours s'aligner sur ce basculement automatique à moins d'utiliser `market_regime_proxy`.

## 3. Dynamic Position Sizing
- **Documentation :** Indique que les tailles de position sont calculées comme un pourcentage du solde disponible (ex: 9.0 = 9%).
- **Code :** Dans `calculate_position_size`, le code utilisait initialement 75% du plafond comme base, puis appliquait des multiplicateurs. Cela pouvait conduire à dépasser le pourcentage cible si le multiplicateur de risque global était > 1.0, bien qu'un plafond final soit appliqué. L'introduction de la gestion multi-lots divise désormais ce plafond par le nombre de lots, ce qui change la dynamique décrite.

## 4. Hardware SIMD Optimization
- **Documentation :** Mentionne la détection automatique et l'utilisation de jeux d'instructions (SSE, AVX, etc.).
- **Code :** La détection est présente via `cpuinfo`, mais l'utilisation effective de ces instructions dépend entièrement de la bibliothèque PyTorch et de sa compilation. Le bot lui-même n'appelle pas directement d'instructions SIMD spécifiques en dehors de ce que PyTorch gère.

## 5. Budget-Aware Suspension
- **Documentation :** Indique une reprise quand 1.5x le budget requis est disponible.
- **Code :** Implémenté correctement pour les erreurs de budget, mais la détection de "l'erreur de budget" dépendait fortement du formatage des erreurs JSON de l'échange, qui peut varier.

## 6. Stratégies mentionnées vs Implémentées
- **Documentation :** Liste `ichimoku_cloud`, `bollinger_bands`, etc.
- **Code :** Certaines stratégies comme `double_ema_macd_rsi` sont référencées dans le code comme défauts mais ne sont pas explicitement définies dans la liste `STRATEGIES` ou le dispatcher `get_signals` sous ce nom exact (souvent combinées ou nommées différemment).

## 7. Mode Simulation vs Live
- **Documentation :** Le mode Live utilise exclusivement les données API de l'échange.
- **Code :** Le code mélangeait parfois l'utilisation du cache local OHLCV (mis à jour par WebSocket) et des appels directs, ce qui est une bonne chose pour la performance mais s'écarte de la description "exclusivement API" si on considère le cache comme une donnée intermédiaire.

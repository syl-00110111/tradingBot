# Rapport d'étude de marché : Intégration mondiale des échanges

## Aperçu
Ce rapport détaille la recherche sur 1 à 3 plateformes d'échange de crypto-monnaies par continent, y compris l'Indonésie et l'Australie, et leur aptitude à être intégrées dans le bot de trading via la bibliothèque CCXT.

## Étude de marché continentale

### Amérique du Nord
1. **Coinbase (ID CCXT : `coinbaseexchange`)**
   - **Caractéristiques clés :** Hautement réglementé, liquidité élevée, large sélection d'actifs.
   - **Statut :** Pris en charge via CCXT.
2. **Gemini (ID CCXT : `gemini`)**
   - **Caractéristiques clés :** Accent mis sur la sécurité et la conformité, forte présence sur les marchés institutionnels.
   - **Statut :** Pris en charge via CCXT.

### Amérique du Sud
1. **Mercado Bitcoin (ID CCXT : `mercado`)**
   - **Caractéristiques clés :** La plus grande bourse du Brésil, se concentre sur les paires de devises fiduciaires locales (BRL).
   - **Statut :** Pris en charge via CCXT.
2. **Bitso (ID CCXT : `bitso`)**
   - **Caractéristiques clés :** Acteur majeur au Mexique et en Argentine, fort accent sur les paiements transfrontaliers.
   - **Statut :** Pris en charge via CCXT.

### Europe
1. **Bitstamp (ID CCXT : `bitstamp`)**
   - **Caractéristiques clés :** L'une des bourses les plus anciennes, hautement fiable, forte liquidité EUR/USD.
   - **Statut :** Pris en charge via CCXT.
2. **WhiteBIT (ID CCXT : `whitebit`)**
   - **Caractéristiques clés :** Bourse européenne avec une grande variété de paires de trading et des performances élevées.
   - **Statut :** Pris en charge via CCXT.

### Asie (y compris l'Indonésie)
1. **Indodax (ID CCXT : `indodax`)**
   - **Caractéristiques clés :** Bourse leader en Indonésie, agréée par la Bappebti.
   - **Statut :** Pris en charge via CCXT.
2. **Upbit (ID CCXT : `upbit`)**
   - **Caractéristiques clés :** Bourse sud-coréenne majeure avec un volume et une présence significatifs dans toute l'Asie du Sud-Est.
   - **Statut :** Pris en charge via CCXT.

### Afrique
1. **Luno (ID CCXT : `luno`)**
   - **Caractéristiques clés :** Forte présence en Afrique du Sud, au Nigeria et sur d'autres marchés en développement. Axé sur la simplicité.
   - **Statut :** Pris en charge via CCXT.

### Australie
1. **Independent Reserve (ID CCXT : `independentreserve`)**
   - **Caractéristiques clés :** Basé en Australie, réglementé, excellentes rampes d'accès/sortie AUD.
   - **Statut :** Pris en charge via CCXT.
2. **BTC Markets (ID CCXT : `btcmarkets`)**
   - **Caractéristiques clés :** Une autre bourse australienne de premier plan avec une forte confiance et liquidité locale.
   - **Statut :** Pris en charge via CCXT.

### Antarctique
1. **Échanges mondiaux accessibles par satellite (ex : Binance, Kraken via Starlink)**
   - **Caractéristiques clés :** Il n'existe pas de bourses physiques locales en Antarctique. Les chercheurs et les résidents s'appuient sur des plateformes mondiales accessibles via des services Internet par satellite comme Starlink.
   - **Statut :** Pris en charge via les implémentations `Binance` et `Kraken` existantes.

## Notes d'implémentation
Pour intégrer ces bourses, mettez à jour `exchange_handler.py` pour inclure des sous-classes spécifiques si elles nécessitent une logique personnalisée (comme `KrakenExchange` ou `BitvavoExchange`). Pour la plupart, la logique de base de `CCXTExchange` (qui utilise les méthodes standard de CCXT) peut être adaptée en changeant l'ID de la bourse.

```python
class CoinbaseExchange(CCXTExchange):
    def __init__(self, api_key, api_secret):
        super().__init__('coinbaseexchange', api_key, api_secret)
```

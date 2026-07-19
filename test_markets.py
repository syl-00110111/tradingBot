import json
from shlex import quote
from rich.console import Console
console = Console()

with open('markets.json', 'r') as file:
    data = json.load(file)

availablePairs = []
# liste d'assets sources, de leur valeur minimale de transaction, et de leur nom complexe
sourceAssets = [['BTC', 0.00005, 'XXBT'], ['ETH', 0.001, 'XETH'], ['USDC', 5, 'USDC'], ['USD', 5, 'ZUSD'], ['EUR', 5, 'ZEUR']]

validAssets = [asset[2] for asset in sourceAssets]
console.print(validAssets)

# id, baseId, quoteId, spot (bool), active (bool)
for market in data.items():
    # si spot, actif et quote dans les validAssets
    if market[1].get('active') == True and market[1].get('spot') == True and market[1].get('info').get('quote') in validAssets:
        availablePairs.append([market[1].get('info').get('wsname'), market[1].get('info').get('base'), market[1].get('info').get('quote')])

console.print(availablePairs)

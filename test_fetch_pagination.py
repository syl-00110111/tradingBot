import requests
import time
import hashlib
import hmac
import base64

# Clés API Kraken
api_key = "q92iLcDsrlcrlsVmX7ZbjAqD8a3aC+X+qego7oGZ7jn6aC3bPaiVjnnq"
api_secret = "/QX0Fp8jziKwvKrAZW6ddD91UEPU7ykVaEBKIPXpDjF9Kabj/qISz0knwYf83tV1kl375sMh8HDF7yggLdnsfw==".encode()

# Endpoint privé
url = "https://api.kraken.com/0/private/GetTradesHistory"
url2 = "https://api.kraken.com/0/private/TradesHistory"




import urllib.parse
import hashlib
import hmac
import base64
import json

import time

api_nonce = str(int(time.time() * 1000))

def get_kraken_signature(urlpath, data, secret):

    if isinstance(data, str):
        encoded = (str(json.loads(data)["nonce"]) + data).encode()
    else:
        encoded = (str(data["nonce"]) + urllib.parse.urlencode(data)).encode()
    message = urlpath.encode() + hashlib.sha256(encoded).digest()

    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    sigdigest = base64.b64encode(mac.digest())
    return sigdigest.decode()

api_sec = "/QX0Fp8jziKwvKrAZW6ddD91UEPU7ykVaEBKIPXpDjF9Kabj/qISz0knwYf83tV1kl375sMh8HDF7yggLdnsfw=="

payload = {
        "nonce": "1616492376594",
        "ordertype": "limit",
        "pair": "XBTUSD",
        "price": 37500,
        "type": "buy",
        "volume": 1.25
        }

signature = get_kraken_signature("/0/private/TradesHistory", payload, api_sec)
print("API-Sign: {}".format(signature))


# Paramètres de la requête
params = {
    "nonce": str(int(time.time() * 1000)),
    "start": "1709459200",  # Timestamp de début (optionnel)
    "end": "1799545600",    # Timestamp de fin (optionnel)
    "ofs": 0                # Offset pour la pagination (optionnel)
}

# Convertir le nonce en bytes avant de l'encoder
nonce_bytes = str(params["nonce"]).encode()  # Convertit en bytes
message = nonce_bytes + b""  # Concaténation avec des bytes vides

# Calculer le hash HMAC-SHA512
#api_secret = b"/QX0Fp8jziKwvKrAZW6ddD91UEPU7ykVaEBKIPXpDjF9Kabj/qISz0knwYf83tV1kl375sMh8HDF7yggLdnsfw=="  # Doit être en bytes
#signature = hmac.new(api_secret, message, hashlib.sha512).digest()

# Encoder en base64
# api_sign = base64.b64encode(signature).decode()

# Préparer la requête
headers = {
    "API-Key": api_key,
    "API-Sign": signature
}

# Envoyer la requête
response = requests.post(url2, data=params, headers=headers)
data = response.json()

print(data)

# Vérifier si la réponse contient l'en-tête CB-AFTER
if "CB-AFTER" in response.headers:
    next_cursor = response.headers["CB-AFTER"]
    print("Prochain curseur :", next_cursor)
else:
    print("Aucun curseur CB-AFTER trouvé dans les en-têtes.")



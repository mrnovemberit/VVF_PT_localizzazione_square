"""
Esegui questo script UNA VOLTA sola, dopo aver fatto il deploy su Render,
per dire a Telegram dove mandare gli update (il tuo URL pubblico).

Uso:
    python setup_webhook.py https://tuo-servizio.onrender.com

Richiede le variabili d'ambiente:
    TELEGRAM_BOT_TOKEN
    WEBHOOK_SECRET (facoltativo ma consigliato, la stessa stringa usata in app.py)
"""

import os
import sys
import requests

if len(sys.argv) != 2:
    print("Uso: python setup_webhook.py https://tuo-servizio.onrender.com")
    sys.exit(1)

BASE_URL = sys.argv[1].rstrip("/")
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

webhook_url = f"{BASE_URL}/telegram-webhook"

payload = {"url": webhook_url}
if WEBHOOK_SECRET:
    payload["secret_token"] = WEBHOOK_SECRET

resp = requests.post(
    f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
    data=payload,
    timeout=15,
)
print(resp.status_code, resp.json())

# Verifica finale
info = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo", timeout=15)
print("\nStato webhook attuale:")
print(info.json())

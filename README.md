# Telegram → ArcGIS Bridge

Riceve le posizioni live condivise su Telegram e le scrive su un Feature Layer ArcGIS Online.

## 1. Test in locale (opzionale, consigliato prima del deploy)

```bash
pip install -r requirements.txt
cp .env.example .env   # poi compila i valori veri
export $(cat .env | xargs)   # carica le variabili in shell (linux/mac)
python app.py
```

In un altro terminale, esponi la porta 5000 con **ngrok**:

```bash
ngrok http 5000
```

Prendi l'URL https che ngrok ti dà (es. `https://abcd1234.ngrok-free.app`) e registralo come webhook:

```bash
python setup_webhook.py https://abcd1234.ngrok-free.app
```

Ora apri Telegram, avvia una chat col bot, condividi la posizione live e controlla i log del terminale dove gira `app.py`.

## 2. Deploy su Render

1. Crea un repository Git (GitHub/GitLab) con questi file, **escludendo** `.env` (aggiungi un `.gitignore` con `.env`)
2. Su [render.com](https://render.com) → New → Web Service → collega il repository
3. Runtime: Python 3
4. Build command: `pip install -r requirements.txt`
5. Start command: `gunicorn app:app`
6. Nella sezione "Environment", aggiungi le stesse variabili di `.env.example` con i valori reali
7. Deploy. Render ti darà un URL pubblico tipo `https://tuo-servizio.onrender.com`

## 3. Registra il webhook definitivo

Una volta che il servizio su Render è attivo:

```bash
export TELEGRAM_BOT_TOKEN=il_tuo_token
export WEBHOOK_SECRET=la_stessa_stringa_messa_su_render
python setup_webhook.py https://tuo-servizio.onrender.com
```

Verifica che la risposta contenga `"ok": true` e che `getWebhookInfo` mostri l'URL corretto.

## 4. Campi attesi sul Feature Layer

| Campo | Tipo | Note |
|---|---|---|
| OperatorID | Text | ID Telegram dell'operatore (chiave per trovare/aggiornare la feature) |
| OperatorName | Text | Nome visualizzato |
| Data_ora | Date | Ultimo aggiornamento posizione |
| LiveUntil | Date | Scadenza prevista della sessione live (facoltativo, calcolato da live_period) |
| Status | Text | "live" (puoi aggiungere logica per "scaduto" con un job periodico, non incluso qui) |

## Note importanti

- **Cold start**: il piano free di Render sospende il servizio dopo 15 minuti di inattività. Il primo update dopo una pausa lunga può impiegare 30-60 secondi prima di essere processato.
- **Nessuno storage esterno**: la mappa "operatore → feature esistente" viene ricalcolata ad ogni update interrogando ArcGIS (query su `OperatorID`). Per il volume di questo progetto è sufficiente e non richiede database aggiuntivi.
- **Sicurezza**: mai committare `.env` o il file con Client Secret / Bot Token nel repository.

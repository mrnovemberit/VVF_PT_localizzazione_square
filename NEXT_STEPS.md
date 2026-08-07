# Prossimi passi

In ordine di priorità:

1. **Deploy su Render**
   - Creare repository Git (GitHub) con i file del progetto (escluso `.env`)
   - Collegare il repo su Render come Web Service, piano free
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app`
   - Impostare le environment variable su Render (stessi valori di `.env` locale)

2. **Registrare il webhook definitivo**
   - Una volta attivo il servizio Render, usare `setup_webhook.py` con
     l'URL Render (non più ngrok)
   - Verificare con `getWebhookInfo` che sia tutto corretto

3. **Test end-to-end su Render**
   - Ripetere il test di condivisione posizione live da Telegram
   - Verificare nei log di Render (dashboard, sezione "Logs") che gli
     update arrivino e vengano scritti su ArcGIS
   - Controllare il comportamento del cold start (primo update dopo
     inattività)

4. **Test con più operatori**
   - Verificare che 2+ persone che condividono contemporaneamente
     producano punti distinti (non si sovrascrivano)

5. **(Eventuale, non urgente) Endpoint /cleanup**
   - Solo se in futuro serve più della semplice pulizia visiva via
     filtro webmap — es. se serve uno storico "chi ha condiviso quando"
     con stati espliciti invece che solo l'ultima posizione nota

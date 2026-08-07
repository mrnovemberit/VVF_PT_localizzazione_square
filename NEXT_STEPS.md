# Prossimi passi

In ordine di priorità:

1. ~~**Deploy su Render**~~ — fatto 08/08/2026, live su
   `https://vvf-pt-localizzazione-square.onrender.com`

2. ~~**Registrare il webhook definitivo**~~ — fatto 08/08/2026,
   `getWebhookInfo` verificato

3. **Cold start**
   - Non ancora osservato: il servizio è stato testato solo "caldo"
     (appena distribuito). Aspettare 15-20 min di inattività, poi
     condividere di nuovo la posizione e controllare quanto impiega
     il primo update ad arrivare

4. **Test con più operatori**
   - Verificare che 2+ persone che condividono contemporaneamente
     producano punti distinti (non si sovrascrivano)

5. **(Eventuale, non urgente) Endpoint /cleanup**
   - Solo se in futuro serve più della semplice pulizia visiva via
     filtro webmap — es. se serve uno storico "chi ha condiviso quando"
     con stati espliciti invece che solo l'ultima posizione nota

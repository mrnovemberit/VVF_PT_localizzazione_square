# Progetto: Tracking posizione VVF via Telegram → ArcGIS

## Scopo

Sistema per tracciare in tempo reale la posizione GPS di 4-5 operatori
(telefoni cellulari) durante gli interventi, usando la funzione "Posizione
in tempo reale" di Telegram come sorgente dati e una Feature Layer ArcGIS
Online come destinazione, visualizzata su una webmap.

## Architettura

```
[Operatore] --condivide posizione live--> [Bot Telegram]
                                                 |
                                    (webhook HTTPS)
                                                 v
                                    [app.py - Flask, Python]
                                                 |
                              (OAuth2 + REST applyEdits)
                                                 v
                                    [Feature Layer ArcGIS Online]
                                                 |
                                                 v
                                          [Webmap]
```

- Il bot Telegram è solo un "recapito": non elabora nulla, inoltra gli
  update a `app.py` via webhook.
- `app.py` distingue `message.location` (prima condivisione, contiene
  `live_period`) da `edited_message.location` (aggiornamenti successivi).
- Per ogni update, l'app cerca se esiste già una feature per quell'
  `OperatorID` (query diretta su ArcGIS, niente storage esterno) e fa
  `applyEdits` di add o update di conseguenza — un solo punto per
  operatore, che si sposta, non si accumula.
- Autenticazione ArcGIS: OAuth2 **app authentication** (client_credentials),
  non serve login umano interattivo.

## Stato attuale del progetto

- [x] Feature Layer creato su ArcGIS Online (`Posizione_partenze_PT`)
- [x] Credenziali OAuth 2.0 "per l'autenticazione via app" create
- [x] Bot Telegram creato via BotFather
- [x] `app.py` scritto e testato in locale (con ngrok) end-to-end
- [x] Verificato: scrittura e aggiornamento posizione funzionano
- [x] Filtro temporale sulla webmap ("nell'ultimi 5 minuti" su `Data_ora`)
      per nascondere chi ha smesso di condividere
- [ ] Deploy su Render (produzione, senza dover tenere il PC acceso)
- [ ] Registrazione webhook definitivo (URL Render, non più ngrok)
- [ ] Eventuale endpoint `/cleanup` per aggiornare `Status` delle feature
      scadute (opzione scartata per ora a favore del filtro webmap — vedi
      sotto, potrebbe tornare utile in futuro)
- [ ] Test con più operatori in contemporanea

## Decisioni prese (e perché)

- **Niente database esterno per la mappa operatore→feature**: si
  interroga ArcGIS ad ogni update (`find_existing_object_id`). Per il
  volume atteso (4-5 operatori, update ogni ~25-30s) è ampiamente
  sufficiente e evita il problema della persistenza (il filesystem di
  Render free non è persistente tra riavvii).
- **Pulizia "scaduti" via filtro webmap, non lato codice**: invece di un
  cron job che aggiorna `Status`, si usa un filtro Map Viewer
  (`Data_ora` "nell'ultimi 5 minuti") — zero manutenzione, si basa
  sull'ultimo aggiornamento reale ricevuto, comportamento più
  affidabile in caso di telefono senza segnale.
- **Render free accettato nonostante il cold start**: 30-60s di
  risveglio dopo 15 min di inattività è stato validato come accettabile
  per l'uso previsto.
- **Nomi campo sul layer**: attenzione, `Data_ora` non `Timestamp`
  (il nome "Timestamp" è riservato/sconsigliato in ArcGIS). Vedi sezione
  "Campi Feature Layer" sotto per l'elenco completo e aggiornato.

## Campi Feature Layer (`Posizione_partenze_PT`)

| Campo | Tipo | Note |
|---|---|---|
| OperatorID | Text | ID Telegram dell'operatore (chiave di ricerca) |
| OperatorName | Text | Nome visualizzato |
| Data_ora | Date | Ultimo aggiornamento posizione (usato dal filtro webmap) |
| LiveUntil | Date | Scadenza prevista sessione live (calcolato da `live_period`, non ancora sfruttato attivamente) |
| Status | Text | Sempre "live" per ora (nessuna logica di scadenza lato codice) |

URL REST del layer (indice `/0` incluso, importante):
```
https://services3.arcgis.com/MfVi0khS4tCyLmo3/arcgis/rest/services/Posizione_partenze2_PT/FeatureServer/0
```

## Stack tecnico

- Python 3, Flask, libreria `requests`
- Deploy previsto: Render (piano free, servizio web)
- Nessun framework ORM: chiamate REST dirette alle API ArcGIS
- Test locale: `ngrok` per esporre temporaneamente il webhook

## Convenzioni

- Commenti e log in italiano (coerente con il resto del progetto)
- Variabili sensibili (token, secret) sempre da environment variable,
  mai hardcoded — vedi `.env.example`
- `.env` reale non va mai committato (già in `.gitignore`)

## Contesto sull'utente

Massi è un Vigile del Fuoco (Pistoia, Toscana), sviluppa questo progetto
da solo, non è uno sviluppatore professionista ma ha già esperienza
pratica con Python, Docker, Make.com, e altri progetti di automazione.
Preferisce spiegazioni chiare passo-passo con verifica tramite
screenshot quando lavora su interfacce web (ArcGIS, Render), e comandi
precisi e testati per PowerShell su Windows (non assumere sintassi
bash/Mac).

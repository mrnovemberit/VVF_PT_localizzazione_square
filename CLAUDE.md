# Progetto: Dashboard operativa VVF Pistoia su ArcGIS

## Scopo

Dashboard ArcGIS del comando provinciale, alimentata da due flussi
indipendenti e complementari:

1. **Posizioni delle partenze** — posizione GPS in tempo reale di 4-5
   operatori tramite la funzione "Posizione in tempo reale" di Telegram.
2. **Interventi e chiamate** — stato operativo provinciale letto dagli XML
   che il software Oracle del comando rigenera ad ogni modifica, invece che
   dal dato redistribuito dal centro nazionale (inaffidabile: interventi
   chiusi che restano appesi, buchi temporali, probabili crash a monte).

## Architettura — flusso 1: posizioni Telegram

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
  non serve login umano interattivo. La logica di token e `applyEdits` è
  condivisa fra i due flussi in `arcgis_client.py`.

## Architettura — flusso 2: interventi da XML Oracle

```
[Software Oracle] --rigenera ad ogni modifica--> [2 XML in una cartella]
                                                          |
                                          (sync_interventi.py, in polling)
                                                          v
                                       parsing + riallineamento del layer
                                                          |
                                              (OAuth2 + REST applyEdits)
                                                          v
                                    [Feature Layer Interventi_chiamate_PT]
```

- Ogni XML è una **fotografia completa** dello stato corrente, non un flusso
  di eventi. Ad ogni ciclo il layer viene **riallineato** alla fotografia:
  ciò che c'è nel file viene creato o aggiornato, ciò che non c'è più viene
  cancellato. È questo che rende strutturalmente impossibile ritrovarsi con
  interventi chiusi appesi sulla mappa — il difetto del dato del centro.
- Il riallineamento è **per fase, non globale**: i due XML sono file distinti
  e possono essere rigenerati in momenti diversi, quindi elaborando le
  chiamate non si toccano le feature degli interventi e viceversa.
- **Guardia anti-svuotamento**: se un XML parsa a zero elementi mentre sul
  layer ce n'erano, serve una seconda lettura vuota consecutiva prima di
  cancellare. Un file troncato o scritto a metà non può svuotare la mappa.
  Un file assente non comporta mai cancellazioni.
- File suddivisi per responsabilità: `parser_xml.py` (solo lettura e
  normalizzazione, nessuna rete, collaudabile a secco), `sync_interventi.py`
  (rilevazione modifiche, riallineamento, ciclo), `arcgis_client.py` (rete).
- Vedi **`LAYER_INTERVENTI.md`** per la creazione del layer, i 25 campi da
  creare a mano, la configurazione e la messa in esercizio.

## Stato attuale del progetto

- [x] Feature Layer creato su ArcGIS Online (`Posizione_partenze_PT`)
- [x] Credenziali OAuth 2.0 "per l'autenticazione via app" create
- [x] Bot Telegram creato via BotFather
- [x] `app.py` scritto e testato in locale (con ngrok) end-to-end
- [x] Verificato: scrittura, aggiornamento e movimento in tempo reale
      visibili sulla webmap (confermato sia in locale che in produzione)
- [x] Filtro temporale sulla webmap ("nell'ultimi 5 minuti" su `Data_ora`)
      per nascondere chi ha smesso di condividere
- [x] Deploy su Render (produzione, senza dover tenere il PC acceso) —
      live su `https://vvf-pt-localizzazione-square.onrender.com`
- [x] Registrazione webhook definitivo (URL Render, non più ngrok)
- [ ] Eventuale endpoint `/cleanup` per aggiornare `Status` delle feature
      scadute (opzione scartata per ora a favore del filtro webmap — vedi
      sotto, potrebbe tornare utile in futuro)
- [ ] Test con più operatori in contemporanea
- [ ] Verificare comportamento cold start (primo update dopo 15+ min
      di inattività) — non ancora osservato, servizio testato solo "caldo"
- [ ] Campo `Moving_status` (fermo/in movimento) aggiunto in `app.py`
      (18/08/2026) — manca ancora: creare il campo Text sul Feature Layer
      su ArcGIS Online e impostare il renderer "Valori unici" sulla webmap

### Flusso 2 — interventi da XML Oracle (avviato 19/08/2026)

- [x] Struttura dei due XML analizzata sugli esempi reali
- [x] `parser_xml.py`, `sync_interventi.py`, `arcgis_client.py` scritti
- [x] `app.py` rifattorizzato per usare `arcgis_client.py` condiviso
- [x] Collaudo a secco superato: parsing, accenti iso-8859-1, passaggio di
      mezzanotte, aggregazione dei mezzi, riallineamento, guardie
      (`prova_riallineamento.py`, 7 prove su ArcGIS simulato)
- [ ] Individuare la cartella reale in cui il software Oracle scrive gli XML
- [ ] Chiedere il via libera al referente informatico per far girare lo
      script sulla macchina del comando (tecnicamente non servono privilegi
      di amministratore, ma l'autorizzazione va chiesta comunque)
- [ ] Creare il Feature Layer `Interventi_chiamate_PT` su ArcGIS Online
      (25 campi, vedi `LAYER_INTERVENTI.md`) — **privato**
- [ ] Prima scrittura reale e test di riallineamento sul campo
- [ ] Vestizione webmap a valori unici su `Stato_operativo`
- [ ] Ottenere la tabella di decodifica di `COD_TIPOLOGIA` (le chiamate in
      attesa mostrano per ora "Codice NN")
- [ ] Verificare il vocabolario di `STATUS` (`A` = aperto è un'ipotesi) e
      valorizzare `STATI_CHIUSI` se emergono altri codici

### Completato

- **Deploy su Render e verifica end-to-end in produzione** il 08/08/2026:
  repo GitHub privato creato, deploy Render riuscito, webhook definitivo
  registrato, scrittura/aggiornamento/movimento confermati sia da query
  diretta ArcGIS che visivamente sulla webmap.

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
- **XML locali invece del dato del centro nazionale**: il software Oracle del
  comando è la fonte autoritativa per la provincia e scrive uno snapshot
  completo ad ogni modifica. Usarlo elimina in un colpo solo i tre difetti del
  dato ridistribuito (interventi appesi, buchi temporali, crash a monte) e
  toglie una dipendenza da un sistema su cui non abbiamo nessun controllo.
- **Riallineamento invece di inseguire gli eventi**: poiché ogni XML è una
  fotografia completa, non serve ricostruire cosa è cambiato — si porta il
  layer a coincidere col file. Anche saltando dei cicli, il layer non può
  divergere dalla realtà. Stessa filosofia del "niente storage esterno" già
  adottata per le posizioni.
- **Dati personali esclusi in fase di parsing, non filtrati sulla mappa**: un
  dato mai caricato non può essere esposto da un errore di condivisione.
- **Un solo layer per chiamate e interventi**, distinti dal campo `Fase`:
  la vestizione a valori unici e i filtri della webmap fanno il resto, come
  già si fa con `Moving_status`.
- **Repository GitHub privato**: `CLAUDE.md` contiene l'URL reale del
  Feature Layer ArcGIS, quindi il repo è privato invece che pubblico
  (riguarda posizioni in tempo reale di operatori durante interventi).

## Campi Feature Layer 1 (`Posizione_partenze_PT`)

| Campo | Tipo | Note |
|---|---|---|
| OperatorID | Text | ID Telegram dell'operatore (chiave di ricerca) |
| OperatorName | Text | Nome visualizzato |
| Data_ora | Date | Ultimo aggiornamento posizione (usato dal filtro webmap) |
| LiveUntil | Date | Scadenza prevista sessione live (calcolato da `live_period`, non ancora sfruttato attivamente) |
| Status | Text | Sempre "live" per ora (nessuna logica di scadenza lato codice) |
| Moving_status | Text | "in movimento" / "fermo", calcolato in `app.py` confrontando la nuova posizione con l'ultima nota (soglia dinamica: 15m o 2× `Precisione_m` se più larga) |
| Direzione | Double | Direzione di marcia in gradi (0-360, da `heading` Telegram, solo se il telefono si muove) |
| Precisione_m | Double | Raggio di incertezza GPS in metri (da `horizontal_accuracy` Telegram) |
| Latitudine | Double | Latitudine (duplicata anche in `geometry`, comoda per lettura/export in tabella) |
| Longitudine | Double | Longitudine (duplicata anche in `geometry`, comoda per lettura/export in tabella) |

URL REST del layer (indice `/0` incluso, importante):
```
https://services3.arcgis.com/MfVi0khS4tCyLmo3/arcgis/rest/services/Posizione_partenze2_PT/FeatureServer/0
```

## Campi Feature Layer 2 (`Interventi_chiamate_PT`)

Elenco completo, tipi e lunghezze in `LAYER_INTERVENTI.md`. I campi chiave da
tenere a mente: `Chiave` (chiave di riallineamento, `C-<n>` / `I-<n>`), `Fase`
(chiamata in attesa / intervento in corso) e `Stato_operativo` (in attesa / in
uscita / sul posto / in rientro), che è il campo su cui si veste la mappa e che
**negli XML non esiste**: viene derivato dagli orari valorizzati.

Dati personali degli XML (`RICHIEDENTE`, `TELE_NUMERO`, `NOME`, `COGNOME`,
`COGNOME_NOME`) **non vengono letti** da `parser_xml.py`: l'esclusione è alla
fonte, non un filtro sulla mappa. Restano indirizzo e note libere, operativamente
necessari — per questo il layer va tenuto privato.

## Stack tecnico

- Python 3, Flask, libreria `requests`
- Deploy: Render (piano free, servizio web), region Frankfurt — live su
  `https://vvf-pt-localizzazione-square.onrender.com`
- Repository: `https://github.com/mrnovemberit/VVF_PT_localizzazione_square`
  (privato)
- Nessun framework ORM: chiamate REST dirette alle API ArcGIS
- Test locale: `ngrok` per esporre temporaneamente il webhook
- `sync_interventi.py` gira invece sulla macchina del comando che vede la
  cartella degli XML (Utilità di pianificazione di Windows), non su Render:
  serve solo traffico HTTPS in uscita, nessuna porta aperta

## Link Notion

- Pagina progetto: https://www.notion.so/VVF_PT-Localizzazione-squadre-3b5f8600d25180e3800cfa04b8b4087d
- Diario sessioni: https://app.notion.com/p/3b5f8600d2518152b176cc4a02c17aed

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

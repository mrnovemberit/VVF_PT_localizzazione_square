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

- Il bot Telegram è solo un "recapito": inoltra gli update a `app.py` via
  webhook, che distingue `message.location` (prima condivisione, con
  `live_period`) da `edited_message.location` (aggiornamenti successivi).
- Per ogni update, l'app cerca la feature per `OperatorID` (query diretta su
  ArcGIS, niente storage esterno) e fa `applyEdits` di add o update — un
  solo punto per operatore, che si sposta, non si accumula.
- Autenticazione OAuth2 **app authentication**, logica condivisa con il
  flusso 2 in `arcgis_client.py`.

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

- Riallineamento e guardie: vedi **Decisioni prese** sotto.
- File suddivisi per responsabilità: `parser_xml.py` (solo lettura e
  normalizzazione, nessuna rete, collaudabile a secco), `sync_interventi.py`
  (rilevazione modifiche, riallineamento, ciclo, log su file), `arcgis_client.py`
  (rete: token, query, applyEdits, descrivi_layer), `verifica_campi.py`
  (controlla i 25 campi del layer prima di scriverci).
- Vedi **`LAYER_INTERVENTI.md`** per la creazione del layer, i 25 campi da
  creare a mano, la configurazione e l'installazione sul PC del comando.

## Stato attuale del progetto

- [x] Feature Layer, credenziali OAuth e bot Telegram creati; `app.py`
      testato end-to-end (locale con ngrok e produzione)
- [x] Filtro webmap "ultimi 5 minuti" su `Data_ora` per nascondere chi ha
      smesso di condividere
- [x] Deploy su Render, webhook definitivo registrato — live su
      `https://vvf-pt-localizzazione-square.onrender.com`
- [ ] Endpoint `/cleanup` per `Status` scaduti — scartato per ora a favore
      del filtro webmap, potrebbe servire in futuro
- [ ] Test con più operatori in contemporanea
- [ ] Verificare il cold start (mai osservato, servizio testato solo "caldo")
- [ ] Campo `Moving_status` (18/08/2026): creare il campo sul layer e il
      renderer "Valori unici" sulla webmap

### Flusso 2 — interventi da XML Oracle (in produzione dal 21/08/2026)

- [x] `parser_xml.py`, `sync_interventi.py`, `arcgis_client.py`,
      `verifica_campi.py` scritti; `app.py` rifattorizzato per usare
      `arcgis_client.py` condiviso; layer creato (25 campi, privato)
- [x] Installato sul PC di sala operativa (`C:\VVF_sync_interventi`),
      Attività pianificata configurata e verificata attiva
- [x] Prima scrittura reale: 30 chiamate + 7 interventi sul layer,
      confermati sulla webmap
- [ ] Riavviare il PC di sala operativa per confermare che l'Attività
      pianificata riparte da sola senza intervento manuale
- [ ] Vestizione webmap a valori unici su `Stato_operativo`
- [ ] Tabella di decodifica di `COD_TIPOLOGIA` (le chiamate mostrano per
      ora "Codice NN")
- [ ] Vocabolario di `STATUS` (`A`/`P`/`S` visti finora; `Stato_operativo`
      non dipende da `STATUS` quindi non è bloccante)
- [ ] Log duplicato per interventi senza coordinate (cosmetico)
- [ ] Geocodifica delle chiamate senza coordinate (14/30 nella prima
      estrazione reale restano fuori mappa per design, non è un bug)

### Completato

- **Flusso 2 in produzione sul PC di sala operativa** il 21/08/2026: due bug
  reali scoperti sui dati veri e risolti (Chiave non univoca — il numero
  chiamata riparte ogni notte — e orari con suffisso `-s` che si perdevano
  nel parsing), scrittura e Attività pianificata verificate attive.
  Dettagli nel diario.
- **Flusso 2 funzionante end-to-end in locale** il 19/08/2026: parser,
  sincronizzatore, layer creato, primo test di scrittura sugli esempi.
- **Deploy su Render e verifica end-to-end in produzione** il 08/08/2026:
  repo GitHub privato creato, deploy Render riuscito, webhook definitivo
  registrato, scrittura/aggiornamento/movimento confermati sia da query
  diretta ArcGIS che visivamente sulla webmap.

## Decisioni prese (e perché)

- **Niente database esterno per la mappa operatore→feature**: si interroga
  ArcGIS ad ogni update. Per 4-5 operatori è sufficiente ed evita il problema
  della persistenza (il filesystem di Render free non sopravvive ai riavvii).
- **Pulizia "scaduti" via filtro webmap** (`Data_ora` "ultimi 5 minuti"),
  non un cron job lato codice: zero manutenzione, più affidabile in caso di
  telefono senza segnale.
- **Render free accettato nonostante il cold start**: 30-60s di risveglio
  dopo 15 min di inattività, validato come accettabile per l'uso previsto.
- **Nomi campo sul layer**: `Data_ora`, non `Timestamp` (riservato in ArcGIS).
- **XML locali invece del dato del centro nazionale**: il software Oracle del
  comando scrive uno snapshot completo ad ogni modifica. Elimina i difetti
  del dato ridistribuito (interventi appesi, buchi temporali, crash a monte).
- **Riallineamento invece di inseguire gli eventi**: poiché ogni XML è una
  fotografia completa, non serve ricostruire cosa è cambiato — si porta il
  layer a coincidere col file (`adds`/`updates`/`deletes`). Anche saltando
  dei cicli, il layer non può divergere dalla realtà. Stessa filosofia del
  "niente storage esterno" già adottata per le posizioni. Due guardie: il
  riallineamento è **per fase** (i due XML sono file distinti, elaborando le
  chiamate non si toccano le feature degli interventi), e serve una
  **seconda lettura vuota consecutiva** prima di cancellare tutto — un file
  troncato mentre Oracle lo riscrive non può svuotare la mappa.
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
| Moving_status | Text | "in movimento" / "fermo" (soglia dinamica: 15m o 2× `Precisione_m` se più larga) |
| Direzione | Double | Gradi 0-360, da `heading` Telegram, solo se il telefono si muove |
| Precisione_m | Double | Raggio di incertezza GPS in metri (`horizontal_accuracy`) |
| Latitudine / Longitudine | Double | Duplicate rispetto a `geometry`, comode per lettura/export in tabella |

URL REST del layer (indice `/0` incluso, importante):
```
https://services3.arcgis.com/MfVi0khS4tCyLmo3/arcgis/rest/services/Posizione_partenze2_PT/FeatureServer/0
```

## Campi Feature Layer 2 (`Interventi_chiamate_PT`)

Elenco completo, tipi e lunghezze in `LAYER_INTERVENTI.md`. Campi chiave:
`Chiave` (chiave di riallineamento, `C-<n>-<GGMMAAAA>` / `I-<n>-<GGMMAAAA>` —
la data è necessaria perché il numero di chiamata riparte da 1 ogni notte,
vedi diario 21/08/2026), `Fase` (chiamata in attesa / intervento in corso),
`Stato_operativo` (in attesa / in uscita / sul posto / in rientro — non
esiste negli XML, è derivato dagli orari ed è il campo su cui si veste la
mappa). Dati personali degli XML (`RICHIEDENTE`, `TELE_NUMERO`, `NOME`,
`COGNOME`, `COGNOME_NOME`) non vengono letti dal
parser — esclusione alla fonte, non un filtro sulla mappa.

## Stack tecnico

- Python 3, Flask, libreria `requests`
- Deploy: Render (piano free, servizio web), region Frankfurt — live su
  `https://vvf-pt-localizzazione-square.onrender.com`
- Repository: `https://github.com/mrnovemberit/VVF_PT_localizzazione_square`
  (privato)
- Nessun framework ORM: chiamate REST dirette alle API ArcGIS
- Test locale: `ngrok` per esporre temporaneamente il webhook
- `sync_interventi.py` gira invece sul PC del comando (Attività pianificata
  di Windows), non su Render: solo HTTPS in uscita, nessuna porta aperta

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

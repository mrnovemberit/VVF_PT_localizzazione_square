# Diario di sessione

## 19/08/2026 — Feature Layer interventi da XML Oracle: parser, sync, primo test riuscito

- **Obiettivo della sessione**: un secondo Feature Layer ArcGIS per la
  dashboard, alimentato dagli XML che il software Oracle di gestione
  interventi del comando rigenera ad ogni modifica, per bypassare il dato
  del centro nazionale (interventi chiusi appesi, buchi temporali, probabili
  crash a monte).
- **Analisi degli XML di esempio** (`chiamate_interventi.XML`, `interventi.XML`,
  forniti dall'utente): encoding `iso-8859-1`, `COORD_X` = longitudine e
  `COORD_Y` = latitudine, decimali con virgola, campi nulli espressi come
  `<TAG NULL="TRUE"/>`, orari in tag separati senza data (tranne
  `DATA_CHIAMATA`), e soprattutto: `interventi.XML` ha **una riga per ogni
  coppia intervento × mezzo** (`3109 /1` compariva due volte, una per
  `DELTA 1+APS MAN` e una per `DELTA 1+CA BOSCHI_3`).
- **Decisione architetturale chiave: riallineamento, non inseguimento degli
  eventi.** Ogni XML è uno snapshot completo dello stato provinciale, non un
  flusso di eventi. Invece di ricostruire cosa è cambiato, ad ogni ciclo il
  layer viene portato a coincidere con la fotografia: `adds` per le chiavi
  nuove, `updates` per quelle esistenti, `deletes` per quelle sparite dal
  file. Questo rende strutturalmente impossibile ritrovarsi con interventi
  chiusi appesi sulla mappa, anche saltando dei cicli — è il difetto esatto
  del dato del centro nazionale che si voleva evitare.
- **Due protezioni aggiunte al riallineamento**, entrambe collaudate:
  - *Per fase, non globale*: i due XML sono file distinti, rigenerati in
    momenti diversi. Elaborando le chiamate si toccano solo le feature con
    `Fase='chiamata in attesa'`, mai quelle degli interventi.
  - *Guardia anti-svuotamento*: se un XML parsa a zero elementi mentre il
    layer ne aveva, serve una seconda lettura vuota **consecutiva** prima di
    cancellare. Un file troncato mentre Oracle lo sta riscrivendo (parsing
    fallito → un solo retry, poi si salta il ciclo) o un file assente non
    possono mai svuotare la mappa.
- **Dati personali esclusi in fase di parsing**, non con un filtro sulla
  mappa: `RICHIEDENTE`, `TELE_NUMERO`, `NOME`, `COGNOME`, `COGNOME_NOME`
  degli XML non vengono letti da `parser_xml.py`, quindi non possono finire
  sul layer per un errore di condivisione. Decisione presa dopo un primo
  giro in cui li avevo inclusi contando sulla privacy del layer — l'utente
  ha corretto l'approccio: un dato mai caricato è più sicuro di un dato
  filtrato. Restano indirizzo e note libere (operativamente necessari), per
  cui il layer va comunque tenuto privato.
- **File creati**:
  - `parser_xml.py` — parsing e normalizzazione, nessuna rete, collaudabile
    a secco. Aggrega le righe per `INTERVENTO`, deriva `Stato_operativo`
    (in attesa / in uscita / sul posto / in rientro) dagli orari valorizzati
    — campo che non esiste negli XML ma serve alla vestizione della mappa.
  - `arcgis_client.py` — token OAuth2 con cache, `query_features`,
    `apply_edits`, `descrivi_layer`. Estratto da `app.py` per essere
    condiviso fra i due flussi; `app.py` rifattorizzato di conseguenza
    (verificato che il comportamento verso ArcGIS resti identico: stessa
    URL, stessi nomi di campo, stessa logica — confrontato il diff con
    `origin/main` prima del push per escludere ripercussioni sul layer
    delle posizioni).
  - `sync_interventi.py` — ciclo di sorveglianza cartella, rilevazione
    modifiche per hash (non rielabora un file invariato), riallineamento,
    log su file con rotazione (necessario perché l'esecuzione finale userà
    `pythonw.exe`, senza console).
  - `verifica_campi.py` — legge la definizione del layer via
    `descrivi_layer` e segnala nomi di campo mancanti o con maiuscole
    sbagliate, tipi errati, stringhe troppo corte, modifica non abilitata.
    Nato dal fatto che ArcGIS scarta in silenzio gli attributi che non
    trovano un campo corrispondente — un typo su 25 campi creati a mano
    altrimenti passerebbe inosservato, con il campo semplicemente sempre
    vuoto sulla mappa.
  - `prova_riallineamento.py` — 8 prove su un `arcgis_client` simulato in
    memoria (nessuna rete, nessuna credenziale), su una copia usa e getta
    degli XML in `esempi/`: primo caricamento, intervento sparito dal file,
    intervento con `ORA_RIENTRO` valorizzata (con verifica esplicita della
    guardia anti-svuotamento quando è l'ultimo aperto), file troncato, file
    vuoto (prima e seconda lettura consecutiva), file assente. Tutte e 8
    superate.
  - `esempi/` — i due XML dell'utente, anonimizzati (nomi e cognomi
    sostituiti con segnaposto), usati dalle prove.
  - `avvia_sync.bat`, `requirements-sync.txt` (solo `requests` + `tzdata`,
    niente Flask/gunicorn sul PC del comando) — preparazione per
    l'installazione.
  - `LAYER_INTERVENTI.md` — guida completa: creazione del layer (25 campi,
    tipi, lunghezze), verifica automatica, installazione passo-passo sul PC
    del comando (Python "solo per l'utente corrente", niente privilegi di
    amministratore richiesti tecnicamente), configurazione dell'Attività
    pianificata di Windows.
- **Verifiche fatte sul codice, non solo sulla carta**: accenti iso-8859-1
  decodificati correttamente (`città`, `perché`, `più`), passaggio di
  mezzanotte gestito (un intervento uscito alle 23:50 e arrivato alle 00:10
  finisce sul giorno dopo, non 11 ore prima), coordinate nell'ordine giusto
  (verificate contro la posizione reale in provincia di Pistoia),
  aggregazione dei mezzi su `3109 /1` e `3109 /2` corretta.
- **Layer creato dall'utente su ArcGIS Online** con i 25 campi. Primo test
  di scrittura reale: `python sync_interventi.py --once --cartella esempi`
  ha scritto 3 feature (1 chiamata a Pistoia, 2 interventi a Buggiano),
  confermate visibili sulla webmap dall'utente ("Per ora è tutto
  operativo!!!").
- **Domanda dell'utente su dove installare i file**: chiarito che vanno in
  una cartella propria del progetto (es. `C:\VVF_sync_interventi\`), non
  dentro la cartella di Oracle — `XML_CARTELLA` nel `.env` punta *a* quella
  cartella, lo script la legge da fuori. Motivo: lo script non deve
  convivere con backup/permessi/antivirus del gestionale che non
  controlliamo, anche se in lettura.
- **Domanda dell'utente sul rischio di compromettere i file Oracle**:
  verificato nel codice (non a memoria) che nessuna riga in tutto il
  progetto apre un file XML in scrittura — `sync_interventi.py` li apre solo
  `"rb"` per l'hash, `parser_xml.py` usa `ET.parse()` in sola lettura.
  L'unica scrittura su disco di tutto il progetto è il file di log.
- **Rimandata l'installazione sul PC del comando a domani**: l'utente
  procurerà prima l'autorizzazione del referente informatico. Concordato che
  il `--dry-run` sui file XML veri (non solo gli esempi statici) sarà il
  momento per scoprire varianti non ancora viste — altri valori di `STATUS`,
  campi mancanti, formati diversi.
- **Push su GitHub e possibile confusione con il layer posizioni**: chiarito
  che no — `app.py` è cambiato solo nella forma (stesso URL, stessi campi,
  stessa logica OAuth/applyEdits, solo spostata in `arcgis_client.py`),
  `sync_interventi.py` non gira mai su Render (resta `gunicorn app:app`),
  nessuna variabile d'ambiente nuova richiesta su Render. L'unico effetto
  del push è il solito riavvio del servizio di qualche decina di secondi,
  già validato come accettabile.
- **Workflow di iterazione concordato per le prossime sessioni**: le
  modifiche all'elaborazione dei dati si fanno in `parser_xml.py`
  (nessuna rete), si verificano con `--dry-run --cartella esempi` e
  `prova_riallineamento.py`, si committano, e solo poi si copiano sul PC
  del comando. L'utente non sa ancora cosa cambierà esattamente — emergerà
  lavorandoci.
- **Lavoro sul branch `interventi-da-xml-oracle`**, non ancora mergiato su
  `main` all'inizio della sessione — il merge e il push avvengono in
  chiusura di sessione (vedi commit).
- **Incognite lasciate aperte, non bloccanti**: la tabella di decodifica di
  `COD_TIPOLOGIA` (le chiamate mostrano "Codice NN" invece del testo), e il
  vocabolario di `STATUS` (`A` = aperto è un'ipotesi verificata solo sui due
  esempi; lo script logga da sé ogni valore diverso che incontra sul campo).

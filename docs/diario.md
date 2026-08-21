# Diario di sessione

## 21/08/2026 — Installazione in produzione sul PC del comando, due bug reali scoperti e risolti

- **Autorizzazioni ottenute**: ok del comandante e del referente informatico. Discusso e
  scartato con l'utente il suggerimento dell'informatico di un server con Docker — non
  necessario per un loop di polling leggero (una chiamata HTTP ogni 20s), e avrebbe reso
  di rete l'accesso alla cartella XML invece che locale. Confermato l'uso del PC di sala
  operativa, sempre acceso e con accesso diretto alla cartella.
- **Trasferimento file**: chiavetta USB. Preparata `VVF_sync_interventi_per_chiavetta/`
  (Desktop) con i 6 file minimi (`sync_interventi.py`, `parser_xml.py`,
  `arcgis_client.py`, `verifica_campi.py`, `requirements-sync.txt`, `avvia_sync.bat`) —
  aggiornata ad ogni fix fatto durante l'installazione, così l'utente ha sempre ricopiato
  solo il singolo file cambiato.
- **Installazione sul PC di sala operativa** (`C:\VVF_sync_interventi`): Python 3.11.9 già
  presente (launcher `py` assente ma `python` funzionante, nessun problema — gli script
  usano sempre il percorso diretto, mai `py`), venv creato, `requirements-sync.txt`
  installato, `.env` con le 4 variabili copiate a mano dal `.env` del PC principale
  (deliberatamente non incollate in chat). Cartella XML reale:
  `C:\users\pianificazione\Temp\ONLINE`.
- **Bug scoperto durante l'installazione**: `verifica_campi.py` andava in
  `FileNotFoundError` senza la cartella `esempi/`, che invece la guida descriveva come
  "non indispensabile" — non era mai stata inclusa nel pacchetto minimo. Sistemato
  rendendo quel controllo opzionale con un avviso invece di un crash (commit `4de9668`).
- **`verifica_campi.py` → "Tutto a posto"**, poi primo `--dry-run` sui file XML **veri**
  del gestionale (non più solo gli esempi): qui sono emersi due bug reali, entrambi
  scoperti solo grazie ai dati di produzione, non riproducibili dai due esempi statici.
  - **Bug 1 (serio) — `CHIAMATA` non è un identificatore univoco.** Nei dati reali
    `C-27` compariva due volte con indirizzi completamente diversi (Via Vacchereccia a
    Massa e Cozzile del 20/08 21:11, e Via Gabbellini a Serravalle Pistoiese del 21/08
    01:51). L'utente ha spiegato la causa: il contatore del software Oracle riparte da 1
    ogni mezzanotte, e il file può contenere ancora chiamate del giorno prima rimaste
    aperte. Rischio concreto: due chiamate reali che si sovrascrivono a vicenda sul
    layer, o che il riallineamento ne cancelli una scambiandola per un doppione. Risolto
    aggiungendo la data alla `Chiave` (`C-<numero>-<GGMMAAAA>` /
    `I-<numero>-<GGMMAAAA>`, presa da `DATA_CHIAMATA`, stabile per tutta la vita del
    record anche se prosegue oltre mezzanotte) — commit `ab6be35`.
    `prova_riallineamento.py` aggiornato alle nuove chiavi, tutte e 8 le prove
    riconfermate. Verificato anche con un caso sintetico riproducente esattamente lo
    scenario delle due `C-27`.
  - **Bug 2 — suffisso su `ORA_USCITA`.** Il software scrive a volte `'05:49 -s'` invece
    di `'05:49'`. Il parsing con `split(":")` andava in `ValueError` sull'intero valore,
    e l'orario spariva silenziosamente dagli attributi — con effetto a cascata su
    `Stato_operativo` (calcolato dagli orari valorizzati: un intervento già uscito
    poteva risultare ancora "in attesa"). Confermato l'impatto reale: `I-3173 /1` prima
    del fix sarebbe risultato "in attesa" invece di "in uscita". Risolto con una regex
    che legge solo l'HH:MM iniziale e logga il suffisso a livello INFO per curiosità
    (non è chiaro cosa significhi "-s", ma non blocca più) — commit `73eb832`.
  - Non è chiaro il significato di `STATUS` = "P"/"S" oltre "A", ma non è bloccante:
    `Stato_operativo` non dipende da `STATUS`, solo dagli orari. Lasciato come
    osservazione loggata, non richiede azione.
  - Rimandati a un secondo momento, non bloccanti: log duplicato per interventi senza
    coordinate (cosmetico, non funzionale), decodifica di `COD_TIPOLOGIA`, geocodifica
    delle chiamate senza coordinate (14 su 30 nella prima estrazione reale — restano
    fuori mappa per design, non è un bug).
  - **Nota privacy confermata sul campo**: una chiamata reale aveva nel campo
    `NOTE_INTERVENTO` il nome di un residente ("Baldacci Alessandro è il residente...").
    Non più un'ipotesi teorica della guida — l'utente ha scelto di mantenere il layer
    privato con condivisione ristretta invece di togliere il campo `Note`.
- **Prima scrittura reale sul Feature Layer** `Interventi_chiamate_PT`: 30 chiamate + 7
  interventi (`python sync_interventi.py --once`), confermate visibili sulla webmap
  dall'utente. Le 3 feature di prova della sessione precedente (`C-14`, `I-3109 /1`,
  `I-3109 /2`) correttamente rimosse dal riallineamento.
- **Attività pianificata di Windows configurata**: "Sync Interventi VVF", trigger
  "All'accesso", azione `avvia_sync.bat`, riavvio automatico ogni 5 minuti in caso di
  fallimento. Verificata in tre modi indipendenti dopo l'avvio manuale di prova:
  processo `pythonw.exe` vivo, riga "Sorveglio ... ogni 20 secondi" presente nel log,
  e un ciclo reale a 13 minuti di distanza con `0 nuovi, 7 aggiornati, 0 rimossi` — prova
  che il polling stava davvero girando e non si era fermato dopo il primo giro. Spiegato
  all'utente perché `LastTaskResult: 0` e il processo ancora vivo non sono in
  contraddizione: `avvia_sync.bat` usa `start`, che stacca il processo Python dall'attività
  che l'ha lanciata.
- **Non ancora fatto**: riavvio del PC per confermare che l'attività riparte da sola senza
  intervento manuale (consigliato ma non urgente, l'utente può farlo con calma).
- **Aiuto fornito su PowerShell durante la sessione**: sbloccato un prompt `>>` dovuto a
  virgolette tipografiche incollate al posto di quelle dritte.

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

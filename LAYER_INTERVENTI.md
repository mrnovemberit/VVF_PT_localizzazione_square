# Feature Layer `Interventi_chiamate_PT` — creazione e messa in esercizio

Guida operativa per il secondo layer della dashboard: quello alimentato dagli
XML del software Oracle di gestione interventi, non dal centro nazionale.

## 1. Creare il layer su ArcGIS Online

**Accedi con lo stesso account ArcGIS che possiede le credenziali OAuth già
in uso** per il layer delle posizioni. Le credenziali "per l'autenticazione via
app" scrivono sugli elementi privati del loro proprietario: se il layer viene
creato da un altro account, il token dell'app non riuscirà a scriverci e
l'errore arriverà solo al primo `applyEdits`, non prima.

Contenuti → Nuovo elemento → **Feature Layer** → *Definisci il tuo layer*.

- Tipo di geometria: **Punto**
- Nome: `Interventi_chiamate_PT`
- Condivisione: **privato**, poi condiviso al solo gruppo del comando.
  Mai "Tutti", mai "Organizzazione" — vedi la nota sui dati in fondo.

Poi, in **Impostazioni** dell'elemento, verificare che **la modifica sia
abilitata** (Abilita modifica, con aggiunta/aggiornamento/eliminazione). Senza
questo `applyEdits` risponde "Operation not allowed": è la causa più comune di
un layer che sembra creato bene ma non riceve nulla.

## 2. Aggiungere i 29 campi

Dalla scheda **Dati → Campi → Aggiungi**. Tre avvertenze prima di cominciare:

- Conta il **"Nome campo"** (quello tecnico), non il "Nome visualizzato": è il
  primo che il codice usa. Il nome visualizzato può essere quello che vuoi.
- I nomi devono coincidere **esattamente, maiuscole comprese**. ArcGIS scarta
  in silenzio gli attributi che non trovano un campo: un `Stato_Operativo` con
  la O maiuscola non dà errore, semplicemente resta sempre vuoto sulla mappa.
- Per i campi data scegliere il tipo **Data** (data e ora), non "Solo data":
  servono anche le ore e i minuti.

Lasciare "Consenti valori Null" attivo su tutti: molti campi valgono solo per
le chiamate o solo per gli interventi, e restano vuoti nell'altro caso.

| Campo | Tipo | Lunghezza | Contenuto |
|---|---|---|---|
| `Chiave` | Stringa | 50 | `C-<numero>-<GGMMAAAA>` o `I-<numero>-<GGMMAAAA>`. È la chiave di riallineamento: senza questo campo il sistema non funziona. La data è necessaria perché il numero di chiamata/intervento riparte da 1 ogni notte |
| `Fase` | Stringa | 30 | `chiamata in attesa` / `intervento in corso` |
| `Numero` | Stringa | 30 | Numero chiamata o intervento, es. `3109 /1` |
| `Tipologia` | Stringa | 120 | Testo per gli interventi, `Codice NN` per le chiamate |
| `Dettaglio_tipologia` | Stringa | 255 | Solo chiamate |
| `Indirizzo` | Stringa | 255 | |
| `Civico_km` | Stringa | 30 | Civico o progressiva chilometrica |
| `Comune` | Stringa | 80 | |
| `Provincia` | Stringa | 5 | |
| `Data_chiamata` | Data | | Momento della chiamata |
| `Ora_uscita` | Data | | Uscita della squadra |
| `Ora_arrivo` | Data | | Arrivo sul posto |
| `Ora_partenza_luogo` | Data | | Partenza dal luogo dell'intervento |
| `Ora_rientro` | Data | | Rientro fisico in caserma. Dato grezzo, non influenza più `Stato_operativo` né la presenza sulla mappa |
| `Stato_oracle` | Stringa | 10 | `STATUS` così come arriva dal software: `A`/`P`/`S`, corrisponde rispettivamente ad `assegnato`/`sul posto`/`sospeso` (verificato sui dati reali, non usato per calcolare `Stato_operativo` — solo per un log di controllo se le due letture non coincidono) |
| `Stato_operativo` | Stringa | 30 | **Campo per la vestizione**: `in attesa`, `assegnato`, `sul posto`, `sospeso`. `sospeso` = nessuna squadra è più fisicamente presente (tutte hanno fatto `ORA_PARTENZA_LUOGO`) ma l'intervento non è ancora sparito dal file Oracle: resta visibile, non viene rimosso dalla mappa |
| `Squadre_mezzi` | Stringa | 500 | Solo i mezzi **attualmente presenti** sull'intervento (non hanno ancora fatto `ORA_PARTENZA_LUOGO`), separati da `; ` — è la fotografia di chi c'è ora, non uno storico. Vuoto quando l'intervento è `sospeso` |
| `Num_mezzi` | Numero intero | | Quanti mezzi sono impegnati |
| `Enti_intervenuti` | Stringa | 10 | |
| `Priorita` | Numero intero | | Solo chiamate |
| `Note` | Stringa | 1000 | Note libere dell'operatore di sala. Per gli interventi: `interventi.XML` non ha un campo nota proprio, quindi è ereditata dalla chiamata d'origine (stesso `DATA_CHIAMATA`+`ORA_CHIAMATA`+`COMUNE`) finché il sincronizzatore la ricorda — resta vuota se il processo è stato riavviato nel frattempo |
| `Tag` | Stringa | 255 | Etichette tipo `#autoscala` o `@trid` estratte dalla nota, minuscole, separate da `; ` — per filtrare interventi/chiamate che richiedono una risorsa specifica. Stessa eredità di `Note` per gli interventi |
| `Minuti_apertura` | Numero intero | | Minuti trascorsi dalla chiamata: utile per far risaltare gli interventi lunghi |
| `Ultimo_agg` | Data | | Quando lo script ha scritto per l'ultima volta questa feature |
| `Latitudine` | Numero decimale | | Duplicata rispetto alla geometria, comoda in tabella |
| `Longitudine` | Numero decimale | | Come sopra |
| `Posizione_stimata` | Stringa | 10 | Vuoto per posizione esatta; `Sì` quando l'XML non aveva COORD_X/COORD_Y e il punto è il centroide del Comune (vedi §7 per la vestizione) |
| `Zona_competenza` | Stringa | 50 | Nome della zona (poligoni disegnati dal comando, non i confini comunali) in cui cade il punto, o `Fuori zona`. Vuoto se `ARCGIS_ZONA_LAYER_URL` non è configurato — funzionalità del tutto facoltativa |
| `Area_emergenza` | Stringa | 10 | Sigla radio della zona (`ALFA`/`MIKE`/`DELTA`/`SIERRA`/`FUORI ZONA` — vedi `MAPPA_AREA_EMERGENZA` in `parser_xml.py` per aggiungerne). Derivato da `Zona_competenza`, stessa condizione per essere vuoto |

Copiare infine l'URL REST del layer, **indice `/0` compreso**, e metterlo in
`.env` alla voce `ARCGIS_INTERVENTI_LAYER_URL`. L'URL si trova in fondo alla
pagina dell'elemento, riquadro "URL": quello mostrato è il FeatureServer, il
`/0` finale (il primo layer del servizio) va aggiunto a mano.

## 3. Configurare lo script

Copiare `.env.example` in `.env` e valorizzare almeno:

```
ARCGIS_CLIENT_ID / ARCGIS_CLIENT_SECRET     (le stesse già in uso)
ARCGIS_INTERVENTI_LAYER_URL                 (URL del nuovo layer, con /0)
XML_CARTELLA                                (dove Oracle scrive i due XML)
```

`sync_interventi.py` legge da solo il `.env` che trova accanto a sé, quindi
funziona anche lanciato dall'Utilità di pianificazione, dove non c'è una shell
che abbia già impostato le variabili.

**Zone di competenza (facoltativo)**: se esiste già un layer poligonale con le
aree di competenza del comando, aggiungere anche:

```
ARCGIS_ZONA_LAYER_URL=https://.../FeatureServer/0   (il layer POLIGONALE delle zone, non quello di chiamate/interventi)
```

Il layer deve avere un campo testo `ZONA_COMPETENZA` con il nome della zona.
Le coordinate vengono convertite automaticamente da Web Mercator (EPSG
3857/102100) — la proiezione predefinita per un layer disegnato in ArcGIS
Online, verificata sul layer reale del comando. Un layer in un'altra
proiezione non è supportato: lo script se ne accorge da solo e logga un
avviso invece di caricare coordinate sbagliate. Senza questa variabile,
`Zona_competenza` resta semplicemente vuoto — nessun errore.

## 4. Controllare che il layer sia a posto

Prima di scriverci sopra, un controllo automatico dei 29 campi:

```
python verifica_campi.py
```

Legge la definizione del layer e segnala nomi mancanti o scritti con le
maiuscole sbagliate, tipi errati, stringhe troppo corte e modifica non
abilitata — cioè tutti gli errori che ArcGIS non segnalerebbe da solo. Se
risponde "Tutto a posto", il layer è pronto.

## 5. Provare prima di collegare ArcGIS

```
python sync_interventi.py --dry-run --cartella "C:\percorso\xml"
```

Non tocca la rete: stampa esattamente le feature che scriverebbe. Da guardare:
coordinate plausibili (latitudine ~43,9 e longitudine ~10,9 per la provincia di
Pistoia), accenti leggibili, orari giusti, e ogni intervento presente **una
sola volta** con tutti i mezzi in `Squadre_mezzi`.

Poi un ciclo vero, uno solo:

```
python sync_interventi.py --once
```

E la verifica della logica di riallineamento, che gira su un ArcGIS simulato e
non richiede né credenziali né rete:

```
python prova_riallineamento.py
```

## 6. Installare sul PC del comando

Sulla macchina che vede la cartella degli XML. Niente di quanto segue richiede
privilegi di amministratore — resta però da chiedere il via libera a chi
gestisce quella macchina.

**a) Python.** Verificare se c'è già:

```
py --version
```

Se manca, installarlo da python.org scegliendo **"Install for me only"** (solo
per l'utente corrente, non serve l'amministratore) e spuntando **"Add
python.exe to PATH"**.

**b) Copiare il progetto.** Servono `sync_interventi.py`, `parser_xml.py`,
`arcgis_client.py`, `requirements-sync.txt` e `avvia_sync.bat`. La cartella
`esempi/` è utile per una prova preliminare ma non indispensabile.

**c) Ambiente virtuale e dipendenze**, dalla cartella del progetto:

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-sync.txt
```

`requirements-sync.txt` contiene solo `requests` e `tzdata`: Flask e gunicorn
riguardano il ponte Telegram e su questa macchina non servono.

**d) Il file `.env`**, da creare nella cartella del progetto con quattro sole
righe. Niente token Telegram: qui non serve, e meno segreti stanno su questa
macchina meglio è.

```
ARCGIS_CLIENT_ID=...
ARCGIS_CLIENT_SECRET=...
ARCGIS_INTERVENTI_LAYER_URL=...   (con /0 finale)
XML_CARTELLA=C:\percorso\vero\degli\xml
```

**e) Provare, in quest'ordine.** Prima a secco, che non tocca la rete:

```
.venv\Scripts\python.exe sync_interventi.py --dry-run
```

È il momento in cui si scopre se gli XML veri assomigliano davvero agli
esempi. Poi un ciclo vero, e si guarda la webmap:

```
.venv\Scripts\python.exe sync_interventi.py --once
```

**f) Avvio automatico.** Utilità di pianificazione → Crea attività:

- *Generale*: "Esegui solo se l'utente ha effettuato l'accesso" — evita di
  dover salvare la password di Windows, e va bene se il PC resta sempre
  connesso come tipicamente in sala operativa
- *Attivazione*: **All'accesso**
- *Azioni*: avvia programma → il percorso completo di **`avvia_sync.bat`**
- *Impostazioni*: "Riavvia l'attività se non riesce" ogni 5 minuti, e
  "Se l'attività è già in esecuzione, non avviarne una nuova"

`avvia_sync.bat` lancia `pythonw.exe`, che non apre nessuna finestra. Per
questo lo script scrive tutto in **`sync_interventi.log`**, nella cartella del
progetto: è lì che si va a guardare se qualcosa non torna. Il file ruota da
solo a 1 MB, non serve manutenzione.

Lo script sopravvive da sé agli errori di rete e ai file scritti a metà: se
qualcosa va storto in un ciclo, riprova al successivo senza toccare il layer.

## 7. Vestizione della webmap

- Simbologia **Valori unici** su `Stato_operativo` (`in attesa`, `assegnato`,
  `sul posto`, `sospeso`) — è il campo pensato apposta. Vale la pena rendere
  `sospeso` visivamente distinto (es. colore spento/grigio): significa che
  nessuna squadra è più fisicamente lì, ma l'intervento non è ancora chiuso
  in Oracle.
- Filtro su `Fase` per accendere o spegnere le chiamate ancora in attesa.
- Nessun filtro temporale, a differenza del layer delle posizioni Telegram: qui
  il riallineamento garantisce già che ci sia solo ciò che è realmente aperto
  (compreso un intervento sospeso, che resta finché non sparisce dal file).
- `Minuti_apertura` per un filtro tipo "aperti da più di 60 minuti", o come
  dimensione del simbolo.
- **`Posizione_stimata`**: aggiungere una seconda regola di simbologia (es.
  "Valori unici" su questo campo, oppure una regola con arcade
  `$feature.Posizione_stimata = 'Sì'`) che disegni quei punti in modo
  visibilmente diverso — es. contorno tratteggiato o icona a forma di comune
  invece che di pin puntuale. Quei punti sono sul centroide del Comune, non
  sull'indirizzo reale: senza un segnale visivo un operatore potrebbe fidarsi
  della posizione come se fosse precisa.
- **`Tag`**: per filtrare chiamate/interventi che richiedono una risorsa
  specifica, filtro testuale tipo `Tag contiene autoscala` (il campo è
  `"; "`-separato, quindi "contiene" funziona anche con più tag sulla stessa
  feature). Non è un campo adatto a "Valori unici" — il vocabolario è libero,
  deciso di volta in volta da chi scrive la nota, non un elenco fisso.
- **`Zona_competenza`**: questo sì un campo a vocabolario chiuso (i nomi delle
  zone disegnate, più `Fuori zona`), adatto a "Valori unici" per un colpo
  d'occhio su quale zona ha più traffico in questo momento, o come filtro
  dedicato per "mostrami solo la mia zona".

## Nota sui dati

Lo script **non legge** `RICHIEDENTE`, `TELE_NUMERO`, `NOME`, `COGNOME` e
`COGNOME_NOME`: quei dati non entrano nemmeno in memoria, quindi non possono
finire sul layer per errore.

Restano comunque sul layer due informazioni che riguardano persone:
l'**indirizzo dell'intervento**, che è spesso l'abitazione di un privato, e le
**note libere**, in cui l'operatore di sala può aver scritto un nome. Per
questo il layer va tenuto privato e condiviso al solo gruppo del comando. Se le
note si rivelassero problematiche, basta togliere `"Note"` dagli attributi in
`parser_xml.py` senza toccare altro.

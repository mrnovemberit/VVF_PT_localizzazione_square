# Feature Layer `Interventi_chiamate_PT` — creazione e messa in esercizio

Guida operativa per il secondo layer della dashboard: quello alimentato dagli
XML del software Oracle di gestione interventi, non dal centro nazionale.

## 1. Creare il layer su ArcGIS Online

Contenuti → Nuovo elemento → **Feature Layer** → *Definisci il tuo layer*.

- Tipo di geometria: **Punto**
- Nome: `Interventi_chiamate_PT`
- Condivisione: **privato**, poi condiviso al solo gruppo del comando.
  Mai "Tutti", mai "Organizzazione" — vedi la nota sui dati in fondo.

Poi, dalla scheda **Dati → Campi**, aggiungere i 25 campi qui sotto. I nomi
devono coincidere esattamente (maiuscole comprese): sono gli stessi che
`parser_xml.py` scrive negli attributi, e ArcGIS scarta silenziosamente gli
attributi che non trovano un campo corrispondente.

| Campo | Tipo | Lunghezza | Contenuto |
|---|---|---|---|
| `Chiave` | Stringa | 50 | `C-<numero>` o `I-<numero>`. È la chiave di riallineamento: senza questo campo il sistema non funziona |
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
| `Ora_rientro` | Data | | Normalmente vuoto: i rientrati non vengono scritti sul layer. Il campo serve se un domani si vorranno tenere |
| `Stato_oracle` | Stringa | 10 | `STATUS` così come arriva dal software |
| `Stato_operativo` | Stringa | 30 | **Campo per la vestizione**: `in attesa`, `in uscita`, `sul posto`, `in rientro` |
| `Squadre_mezzi` | Stringa | 500 | Tutti i mezzi dell'intervento, separati da `; ` |
| `Num_mezzi` | Numero intero | | Quanti mezzi sono impegnati |
| `Enti_intervenuti` | Stringa | 10 | |
| `Priorita` | Numero intero | | Solo chiamate |
| `Note` | Stringa | 1000 | Note libere dell'operatore di sala |
| `Minuti_apertura` | Numero intero | | Minuti trascorsi dalla chiamata: utile per far risaltare gli interventi lunghi |
| `Ultimo_agg` | Data | | Quando lo script ha scritto per l'ultima volta questa feature |
| `Latitudine` | Numero decimale | | Duplicata rispetto alla geometria, comoda in tabella |
| `Longitudine` | Numero decimale | | Come sopra |

Copiare infine l'URL REST del layer, **indice `/0` compreso**, e metterlo in
`.env` alla voce `ARCGIS_INTERVENTI_LAYER_URL`.

## 2. Configurare lo script

Copiare `.env.example` in `.env` e valorizzare almeno:

```
ARCGIS_CLIENT_ID / ARCGIS_CLIENT_SECRET     (le stesse già in uso)
ARCGIS_INTERVENTI_LAYER_URL                 (URL del nuovo layer, con /0)
XML_CARTELLA                                (dove Oracle scrive i due XML)
```

`sync_interventi.py` legge da solo il `.env` che trova accanto a sé, quindi
funziona anche lanciato dall'Utilità di pianificazione, dove non c'è una shell
che abbia già impostato le variabili.

## 3. Provare prima di collegare ArcGIS

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

## 4. Farlo girare in continuo

```
python sync_interventi.py
```

Per l'esercizio, Utilità di pianificazione di Windows: attività **all'avvio del
computer**, "Esegui indipendentemente dalla connessione dell'utente", azione
`pythonw.exe` con argomento il percorso completo di `sync_interventi.py` e
"Inizio" impostato sulla cartella del progetto. Lo script si riavvia da solo
dopo un errore di rete, quindi non serve altro.

## 5. Vestizione della webmap

- Simbologia **Valori unici** su `Stato_operativo` — è il campo pensato apposta.
- Filtro su `Fase` per accendere o spegnere le chiamate ancora in attesa.
- Nessun filtro temporale, a differenza del layer delle posizioni Telegram: qui
  il riallineamento garantisce già che ci sia solo ciò che è realmente aperto.
- `Minuti_apertura` per un filtro tipo "aperti da più di 60 minuti", o come
  dimensione del simbolo.

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

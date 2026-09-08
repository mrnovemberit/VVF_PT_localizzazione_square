"""
Sincronizzatore XML Oracle -> Feature Layer ArcGIS "Interventi_chiamate_PT".

Sorveglia la cartella in cui il software di gestione interventi del comando
rigenera i due XML e riallinea il Feature Layer alla fotografia che contengono.

Il principio è il riallineamento, non l'inseguimento degli eventi: ogni XML è
uno stato completo, quindi ad ogni ciclo ciò che è nel file viene creato o
aggiornato e ciò che non c'è più viene cancellato. È questo che rende
impossibile ritrovarsi con interventi chiusi rimasti appesi sulla mappa.

Uso:
    python sync_interventi.py --dry-run --cartella C:\\percorso   (prova a secco)
    python sync_interventi.py --once                             (un solo ciclo)
    python sync_interventi.py                                    (in continuo)
"""

import os
import sys
import json
import time
import hashlib
import requests
import logging
import logging.handlers
import argparse
import xml.etree.ElementTree as ET
from functools import partial

from parser_xml import (
    leggi_chiamate,
    leggi_interventi,
    note_da_chiamate,
    poligoni_da_query,
    FASE_CHIAMATA,
    FASE_INTERVENTO,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("sync")

CARTELLA_SCRIPT = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------
def carica_env_locale(nome_file=".env"):
    """
    Carica le variabili dal file indicato (di norma .env) accanto allo
    script, se presente.

    Serve per l'esecuzione come Attività pianificata di Windows, dove non c'è
    una shell che abbia già impostato l'ambiente. Le variabili già presenti
    nell'ambiente hanno comunque la precedenza. Il nome del file è
    parametrizzabile per poter tenere più configurazioni affiancate (es.
    ".env.test" per un layer sperimentale) senza doverle rinominare a turno.
    """
    percorso = os.path.join(CARTELLA_SCRIPT, nome_file)
    if not os.path.exists(percorso):
        return
    with open(percorso, encoding="utf-8") as f:
        for riga in f:
            riga = riga.strip()
            if not riga or riga.startswith("#") or "=" not in riga:
                continue
            chiave, valore = riga.split("=", 1)
            os.environ.setdefault(chiave.strip(), valore.strip())


NOME_FILE_CHIAMATE = os.environ.get("XML_FILE_CHIAMATE", "chiamate_interventi.XML")
NOME_FILE_INTERVENTI = os.environ.get("XML_FILE_INTERVENTI", "interventi.XML")

# Codici STATUS che indicano un intervento concluso. Vuoto finché non conosciamo
# il vocabolario del software Oracle: la chiusura si riconosce comunque
# dall'orario di rientro valorizzato.
STATI_CHIUSI = tuple(
    s.strip() for s in os.environ.get("STATI_CHIUSI", "").split(",") if s.strip()
)

# Memoria fra un ciclo e l'altro: hash dei file già elaborati, conteggio delle
# letture vuote consecutive (vedi la guardia anti-svuotamento in riallinea), le
# note delle chiamate ancora in coda (vedi aggiorna_note_cache), i poligoni
# delle zone di competenza (vedi carica_poligoni_zona) e i punti trovati via
# geocoding per Chiave (vedi affina_posizioni_stimate). "poligoni_zona": None
# significa "non ancora caricati", [] significa "disattivato/URL non impostato".
_stato = {"hash": {}, "vuoti_consecutivi": {}, "note_cache": {}, "poligoni_zona": None,
          "geocoding_cache": {}}

# Da quanto tempo una nota può restare in cache senza che la chiamata
# corrispondente sia mai diventata un intervento, prima di essere scartata.
NOTE_CACHE_MAX_ORE = 48


# ---------------------------------------------------------------------------
# Lettura dei file
# ---------------------------------------------------------------------------
def impronta_file(percorso):
    """Hash del contenuto, per non rielaborare un file che non è cambiato."""
    try:
        with open(percorso, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError as errore:
        log.warning("Impossibile leggere %s: %s", percorso, errore)
        return None


def leggi_con_retry(lettore, percorso):
    """
    Esegue il parsing tollerando il caso in cui il software Oracle stia
    riscrivendo il file proprio mentre lo leggiamo: in quel caso l'XML risulta
    troncato. Si riprova una volta dopo un istante, poi si rinuncia per questo
    ciclo lasciando il layer com'è.
    """
    for tentativo in (1, 2):
        try:
            return lettore(percorso)
        except ET.ParseError as errore:
            log.warning("XML incompleto in %s (tentativo %s): %s", percorso, tentativo, errore)
            time.sleep(1)
        except OSError as errore:
            log.warning("Errore di lettura su %s: %s", percorso, errore)
            return None
    log.error("Parsing di %s fallito due volte, ciclo saltato", percorso)
    return None


def aggiorna_note_cache(percorso_chiamate):
    """
    Ricorda le note (e i tag) delle chiamate ancora in coda.

    L'XML degli interventi non riporta più la nota una volta che la chiamata
    viene assegnata: senza questa cache, un tag come #autoscala scritto in
    sala mentre la chiamata aspetta sparirebbe non appena esce una squadra.
    Tollera un file troncato allo stesso modo di leggi_con_retry: la lettura
    delle chiamate vere e proprie, poco prima nello stesso ciclo, ha già
    accertato che il file esiste ed è (stato) leggibile.
    """
    try:
        fresche = note_da_chiamate(percorso_chiamate)
    except ET.ParseError:
        return

    adesso = int(time.time() * 1000)
    for chiave, dati in fresche.items():
        _stato["note_cache"][chiave] = dict(dati, vista_ms=adesso)

    soglia = adesso - NOTE_CACHE_MAX_ORE * 3600 * 1000
    scadute = [c for c, d in _stato["note_cache"].items() if d["vista_ms"] < soglia]
    for chiave in scadute:
        del _stato["note_cache"][chiave]


def carica_poligoni_zona():
    """
    Carica una sola volta i poligoni delle zone di competenza da
    ARCGIS_ZONA_LAYER_URL: i confini non cambiano da un ciclo all'altro, non
    ha senso reinterrogare ArcGIS ogni volta.

    Se la variabile non è impostata la funzionalità è semplicemente
    disattivata (Zona_competenza resta vuoto ovunque). Se la query fallisce
    (rete, permessi) _stato["poligoni_zona"] resta None e si riprova al
    ciclo successivo, senza far fallire il resto del ciclo corrente.
    """
    if _stato["poligoni_zona"] is not None:
        return

    layer_url = os.environ.get("ARCGIS_ZONA_LAYER_URL", "").rstrip("/")
    if not layer_url:
        _stato["poligoni_zona"] = []
        return

    try:
        from arcgis_client import query_poligoni
        grezzi = query_poligoni(layer_url, "ZONA_COMPETENZA")
        _stato["poligoni_zona"] = poligoni_da_query(grezzi)
        log.info("Zone di competenza caricate: %s poligoni", len(_stato["poligoni_zona"]))
    except Exception:
        log.exception("Impossibile caricare le zone di competenza, riprovo al prossimo ciclo")


# ---------------------------------------------------------------------------
# Geocoding di riserva (Nominatim/OpenStreetMap)
# ---------------------------------------------------------------------------
# Affina il centroide del Comune (già calcolato da parser_xml.py) con un
# punto più preciso quando possibile, usando l'indirizzo. Nominatim: gratuito,
# nessuna chiave, ma con una politica d'uso rigida (max 1 richiesta al
# secondo) — rispettata qui con un'attesa minima fra una chiamata e l'altra.
# Non è un requisito: se il servizio non risponde o non trova l'indirizzo,
# resta il centroide del Comune già presente, non si perde mai la feature.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = "VVF-PT-sync-interventi/1.0 (uso interno comando VVF Pistoia)"
GEOCODING_INTERVALLO_MINIMO_S = 1.1

_ultimo_geocoding_ts = [0.0]


def _geocodifica(indirizzo, comune, provincia):
    """Un punto (lat, lon) da Nominatim per indirizzo+comune, o None."""
    attesa = GEOCODING_INTERVALLO_MINIMO_S - (time.time() - _ultimo_geocoding_ts[0])
    if attesa > 0:
        time.sleep(attesa)
    _ultimo_geocoding_ts[0] = time.time()

    query = ", ".join(p for p in (indirizzo, comune, provincia, "Italia") if p)
    try:
        risposta = requests.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "it"},
            headers={"User-Agent": NOMINATIM_USER_AGENT},
            timeout=5,
        )
        risposta.raise_for_status()
        risultati = risposta.json()
    except Exception:
        log.warning("Geocoding non riuscito per '%s', resto sul centroide del comune", query)
        return None

    if not risultati:
        return None
    return float(risultati[0]["lat"]), float(risultati[0]["lon"])


def affina_posizioni_stimate(feature):
    """
    Per le feature senza coordinate valide nell'XML (Posizione_stimata="Sì",
    già sul centroide del Comune), prova a sostituirlo con un punto più
    preciso via indirizzo. Il centroide resta comunque il fallback: non
    toglie mai una posizione, la migliora quando ci riesce.

    Risultato (anche un fallimento) tenuto in cache per Chiave: lo stesso
    indirizzo, mentre la chiamata/l'intervento resta in coda, non va
    ri-geocodificato ad ogni ciclo — e un indirizzo che oggi non si trova
    difficilmente si troverà al giro successivo.
    """
    for f in feature:
        attributi = f["attributes"]
        if attributi.get("Posizione_stimata") != "Sì":
            continue

        chiave = attributi["Chiave"]
        if chiave in _stato["geocoding_cache"]:
            punto = _stato["geocoding_cache"][chiave]
        else:
            punto = _geocodifica(
                attributi.get("Indirizzo"), attributi.get("Comune"), attributi.get("Provincia")
            )
            _stato["geocoding_cache"][chiave] = punto

        if punto is None:
            continue
        lat, lon = punto
        attributi["Latitudine"], attributi["Longitudine"] = lat, lon
        f["geometry"] = {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}


# ---------------------------------------------------------------------------
# Riallineamento del layer
# ---------------------------------------------------------------------------
def _nome_breve_layer(layer_url):
    """'Interventi_chiamate2_PT' invece dell'URL intero, solo per i log."""
    parti = layer_url.rstrip("/").split("/")
    for i, parte in enumerate(parti):
        if parte == "FeatureServer" and i > 0:
            return parti[i - 1]
    return layer_url


def riallinea(layer_url, fase, feature_attese):
    """
    Porta il layer a coincidere con le feature lette dall'XML, limitatamente
    alla fase indicata.

    Il riallineamento è per fase e non globale: i due XML sono file distinti e
    possono essere rigenerati in momenti diversi, quindi elaborando le chiamate
    non si devono toccare le feature degli interventi (e viceversa).

    Non modifica gli oggetti in feature_attese (costruisce copie con
    l'OBJECTID per gli aggiornamenti): la stessa lista viene passata a più
    layer in sequenza quando sono configurati più target (vedi elabora()), e
    l'OBJECTID di un layer non deve sporcare quello di un altro.
    """
    from arcgis_client import query_features, apply_edits

    nome = _nome_breve_layer(layer_url)
    presenti = query_features(
        layer_url,
        where="Fase='{}'".format(fase),
        out_fields="OBJECTID,Chiave",
    )

    # Guardia anti-svuotamento (per layer + fase: due layer diversi non
    # devono condividere lo stesso conteggio): un file troncato o
    # momentaneamente vuoto non deve poter cancellare la mappa. Serve una
    # seconda lettura vuota di seguito prima di credere che non ci sia più
    # nulla.
    chiave_guardia = (layer_url, fase)
    if not feature_attese and presenti:
        _stato["vuoti_consecutivi"][chiave_guardia] = _stato["vuoti_consecutivi"].get(chiave_guardia, 0) + 1
        if _stato["vuoti_consecutivi"][chiave_guardia] < 2:
            log.warning(
                "%s (%s): l'XML non contiene nulla ma sul layer ci sono %s feature. "
                "Non cancello, attendo conferma al prossimo ciclo.",
                fase, nome, len(presenti),
            )
            return
        log.warning("%s (%s): seconda lettura vuota consecutiva, procedo con la pulizia", fase, nome)
    else:
        _stato["vuoti_consecutivi"][chiave_guardia] = 0

    # Chiave -> OBJECTID. Un eventuale duplicato sulla stessa chiave (non
    # dovrebbe accadere) viene rimosso tenendo la prima occorrenza.
    per_chiave = {}
    doppioni = []
    for attributi in presenti:
        chiave = attributi.get("Chiave")
        if chiave in per_chiave:
            doppioni.append(attributi["OBJECTID"])
        else:
            per_chiave[chiave] = attributi["OBJECTID"]

    if doppioni:
        log.warning("%s (%s): trovate %s feature duplicate, le rimuovo", fase, nome, len(doppioni))

    da_aggiungere, da_aggiornare = [], []
    chiavi_nel_file = set()

    for feature in feature_attese:
        chiave = feature["attributes"]["Chiave"]
        chiavi_nel_file.add(chiave)
        if chiave in per_chiave:
            da_aggiornare.append({
                "attributes": dict(feature["attributes"], OBJECTID=per_chiave[chiave]),
                "geometry": feature["geometry"],
            })
        else:
            da_aggiungere.append(feature)

    da_cancellare = doppioni + [
        oid for chiave, oid in per_chiave.items() if chiave not in chiavi_nel_file
    ]

    if not (da_aggiungere or da_aggiornare or da_cancellare):
        log.info("%s (%s): nessuna differenza da applicare", fase, nome)
        return

    apply_edits(
        layer_url,
        adds=da_aggiungere or None,
        updates=da_aggiornare or None,
        deletes=da_cancellare or None,
    )
    log.info(
        "%s (%s): %s nuovi, %s aggiornati, %s rimossi",
        fase, nome, len(da_aggiungere), len(da_aggiornare), len(da_cancellare),
    )


# ---------------------------------------------------------------------------
# Ciclo
# ---------------------------------------------------------------------------
def elabora(cartella, layer_urls, forza=False, dry_run=False):
    """
    Elabora i due XML, saltando quelli non cambiati dall'ultimo giro.

    Un file assente non comporta mai cancellazioni: se il software Oracle non
    lo ha (ancora) scritto, la fase corrispondente resta com'è sul layer.

    layer_urls: un URL singolo, o una lista di URL per scrivere le stesse
    feature su più layer (es. produzione + un layer sperimentale in
    parallelo, per confrontarli senza interrompere l'uno o l'altro).
    """
    if isinstance(layer_urls, str):
        layer_urls = [layer_urls]

    if not dry_run:
        # Il --dry-run non deve toccare la rete per nessun motivo (si usa
        # anche prima di aver configurato le credenziali): niente zone lì.
        carica_poligoni_zona()

    percorso_chiamate = os.path.join(cartella, NOME_FILE_CHIAMATE)
    lavori = [
        (FASE_CHIAMATA, percorso_chiamate,
         partial(leggi_chiamate, poligoni=_stato["poligoni_zona"])),
        (FASE_INTERVENTO,
         os.path.join(cartella, NOME_FILE_INTERVENTI),
         partial(leggi_interventi, stati_chiusi=STATI_CHIUSI,
                 note_cache=_stato["note_cache"], poligoni=_stato["poligoni_zona"])),
    ]

    for fase, percorso, lettore in lavori:
        if not os.path.exists(percorso):
            log.warning("File non trovato, fase '%s' lasciata invariata: %s", fase, percorso)
            continue

        impronta = impronta_file(percorso)
        saltabile = (
            not forza
            and impronta is not None
            and _stato["hash"].get(percorso) == impronta
            # Un file rimasto stabilmente vuoto ha byte identici da un ciclo
            # all'altro: senza questa eccezione la guardia anti-svuotamento
            # (che ha bisogno di rileggere una seconda volta per confermare)
            # non riceverebbe mai quella seconda lettura, e resterebbe in
            # attesa per sempre invece di pulire un file genuinamente vuoto.
            and not any(
                _stato["vuoti_consecutivi"].get((url, fase), 0) > 0
                for url in layer_urls
            )
        )
        if saltabile:
            continue  # file immutato, niente da fare

        feature = leggi_con_retry(lettore, percorso)
        if feature is None:
            continue  # parsing fallito: il layer resta com'è

        if fase == FASE_CHIAMATA:
            # Prima di elaborare gli interventi, che leggono da questa stessa
            # cache: le chiamate vanno aggiornate per prime nell'elenco sopra.
            aggiorna_note_cache(percorso)

        log.info("%s: lette %s feature da %s", fase, len(feature), os.path.basename(percorso))

        if dry_run:
            print(json.dumps(feature, ensure_ascii=False, indent=1))
        else:
            affina_posizioni_stimate(feature)
            for layer_url in layer_urls:
                riallinea(layer_url, fase, feature)

        # L'impronta si registra solo dopo che TUTTI i layer sono stati
        # riallineati con successo: se uno fallisce, un'eccezione interrompe
        # il ciclo prima di questa riga, e si riprova al giro successivo su
        # entrambi (il layer già aggiornato risulterà solo "senza differenze").
        _stato["hash"][percorso] = impronta


def configura_log_su_file():
    """
    Aggiunge un file di log accanto allo script.

    Lanciato dall'Utilita' di pianificazione con pythonw.exe non c'e' nessuna
    console: senza questo, in caso di problemi non resterebbe traccia di
    niente. Il file ruota da solo, non serve manutenzione.
    """
    percorso = os.environ.get("LOG_FILE") or os.path.join(
        CARTELLA_SCRIPT, "sync_interventi.log"
    )
    handler = logging.handlers.RotatingFileHandler(
        percorso, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    return percorso


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cartella", help="Cartella dei due XML (default: XML_CARTELLA)")
    parser.add_argument("--once", action="store_true", help="Esegue un solo ciclo e termina")
    parser.add_argument("--dry-run", action="store_true",
                        help="Legge gli XML e stampa cosa scriverebbe, senza toccare ArcGIS")
    parser.add_argument("--intervallo", type=int, default=None,
                        help="Secondi fra un controllo e l'altro (default 20, o "
                             "INTERVALLO_SECONDI dal file di configurazione)")
    parser.add_argument("--env-file", default=".env",
                        help="File di configurazione da caricare, accanto allo script "
                             "(default: .env; utile per puntare a un layer di prova, "
                             "es. --env-file .env.charlie)")
    args = parser.parse_args()

    carica_env_locale(args.env_file)
    if args.intervallo is None:
        args.intervallo = int(os.environ.get("INTERVALLO_SECONDI", "20"))

    cartella = args.cartella or os.environ.get("XML_CARTELLA")
    if not cartella:
        parser.error("Indica la cartella degli XML con --cartella o la variabile XML_CARTELLA")
    if not os.path.isdir(cartella):
        parser.error("Cartella inesistente: {}".format(cartella))

    layer_url = os.environ.get("ARCGIS_INTERVENTI_LAYER_URL", "").rstrip("/")
    if not args.dry_run and not layer_url:
        parser.error("Manca la variabile d'ambiente ARCGIS_INTERVENTI_LAYER_URL")

    # Layer aggiuntivi facoltativi (es. produzione + un layer sperimentale in
    # parallelo), separati da virgola: le stesse feature vengono scritte su
    # tutti, ognuno riallineato indipendentemente.
    extra = os.environ.get("ARCGIS_INTERVENTI_LAYER_URL_EXTRA", "")
    layer_urls = [layer_url] + [u.strip().rstrip("/") for u in extra.split(",") if u.strip()]
    if len(layer_urls) > 1:
        log.info("Scrittura su %s layer: %s", len(layer_urls), ", ".join(layer_urls))

    if args.dry_run or args.once:
        elabora(cartella, layer_urls, forza=True, dry_run=args.dry_run)
        return

    percorso_log = configura_log_su_file()
    log.info("Sorveglio %s ogni %s secondi (log in %s)",
             cartella, args.intervallo, percorso_log)
    while True:
        try:
            elabora(cartella, layer_urls)
        except Exception:
            # Un errore di rete o un timeout non devono fermare il servizio:
            # si logga e si riprova al giro dopo.
            log.exception("Ciclo fallito, riprovo fra %s secondi", args.intervallo)
        time.sleep(args.intervallo)


if __name__ == "__main__":
    sys.exit(main())

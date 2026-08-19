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
import logging
import argparse
import xml.etree.ElementTree as ET
from functools import partial

from parser_xml import (
    leggi_chiamate,
    leggi_interventi,
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
def carica_env_locale():
    """
    Carica le variabili dal file .env accanto allo script, se presente.

    Serve per l'esecuzione come Attività pianificata di Windows, dove non c'è
    una shell che abbia già impostato l'ambiente. Le variabili già presenti
    nell'ambiente hanno comunque la precedenza.
    """
    percorso = os.path.join(CARTELLA_SCRIPT, ".env")
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

# Memoria fra un ciclo e l'altro: hash dei file già elaborati e conteggio delle
# letture vuote consecutive (vedi la guardia anti-svuotamento in riallinea).
_stato = {"hash": {}, "vuoti_consecutivi": {}}


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


# ---------------------------------------------------------------------------
# Riallineamento del layer
# ---------------------------------------------------------------------------
def riallinea(layer_url, fase, feature_attese):
    """
    Porta il layer a coincidere con le feature lette dall'XML, limitatamente
    alla fase indicata.

    Il riallineamento è per fase e non globale: i due XML sono file distinti e
    possono essere rigenerati in momenti diversi, quindi elaborando le chiamate
    non si devono toccare le feature degli interventi (e viceversa).
    """
    from arcgis_client import query_features, apply_edits

    presenti = query_features(
        layer_url,
        where="Fase='{}'".format(fase),
        out_fields="OBJECTID,Chiave",
    )

    # Guardia anti-svuotamento: un file troncato o momentaneamente vuoto non
    # deve poter cancellare la mappa. Serve una seconda lettura vuota di
    # seguito prima di credere che davvero non ci sia più nulla.
    if not feature_attese and presenti:
        _stato["vuoti_consecutivi"][fase] = _stato["vuoti_consecutivi"].get(fase, 0) + 1
        if _stato["vuoti_consecutivi"][fase] < 2:
            log.warning(
                "%s: l'XML non contiene nulla ma sul layer ci sono %s feature. "
                "Non cancello, attendo conferma al prossimo ciclo.",
                fase, len(presenti),
            )
            return
        log.warning("%s: seconda lettura vuota consecutiva, procedo con la pulizia", fase)
    else:
        _stato["vuoti_consecutivi"][fase] = 0

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
        log.warning("%s: trovate %s feature duplicate, le rimuovo", fase, len(doppioni))

    da_aggiungere, da_aggiornare = [], []
    chiavi_nel_file = set()

    for feature in feature_attese:
        chiave = feature["attributes"]["Chiave"]
        chiavi_nel_file.add(chiave)
        if chiave in per_chiave:
            feature["attributes"]["OBJECTID"] = per_chiave[chiave]
            da_aggiornare.append(feature)
        else:
            da_aggiungere.append(feature)

    da_cancellare = doppioni + [
        oid for chiave, oid in per_chiave.items() if chiave not in chiavi_nel_file
    ]

    if not (da_aggiungere or da_aggiornare or da_cancellare):
        log.info("%s: nessuna differenza da applicare", fase)
        return

    apply_edits(
        layer_url,
        adds=da_aggiungere or None,
        updates=da_aggiornare or None,
        deletes=da_cancellare or None,
    )
    log.info(
        "%s: %s nuovi, %s aggiornati, %s rimossi",
        fase, len(da_aggiungere), len(da_aggiornare), len(da_cancellare),
    )


# ---------------------------------------------------------------------------
# Ciclo
# ---------------------------------------------------------------------------
def elabora(cartella, layer_url, forza=False, dry_run=False):
    """
    Elabora i due XML, saltando quelli non cambiati dall'ultimo giro.

    Un file assente non comporta mai cancellazioni: se il software Oracle non
    lo ha (ancora) scritto, la fase corrispondente resta com'è sul layer.
    """
    lavori = [
        (FASE_CHIAMATA,
         os.path.join(cartella, NOME_FILE_CHIAMATE),
         leggi_chiamate),
        (FASE_INTERVENTO,
         os.path.join(cartella, NOME_FILE_INTERVENTI),
         partial(leggi_interventi, stati_chiusi=STATI_CHIUSI)),
    ]

    for fase, percorso, lettore in lavori:
        if not os.path.exists(percorso):
            log.warning("File non trovato, fase '%s' lasciata invariata: %s", fase, percorso)
            continue

        impronta = impronta_file(percorso)
        if not forza and impronta is not None and _stato["hash"].get(percorso) == impronta:
            continue  # file immutato, niente da fare

        feature = leggi_con_retry(lettore, percorso)
        if feature is None:
            continue  # parsing fallito: il layer resta com'è

        log.info("%s: lette %s feature da %s", fase, len(feature), os.path.basename(percorso))

        if dry_run:
            print(json.dumps(feature, ensure_ascii=False, indent=1))
        else:
            riallinea(layer_url, fase, feature)

        # L'impronta si registra solo dopo un'elaborazione andata a buon fine,
        # così un errore di rete fa riprovare al ciclo successivo.
        _stato["hash"][percorso] = impronta


def main():
    carica_env_locale()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cartella", help="Cartella dei due XML (default: XML_CARTELLA)")
    parser.add_argument("--once", action="store_true", help="Esegue un solo ciclo e termina")
    parser.add_argument("--dry-run", action="store_true",
                        help="Legge gli XML e stampa cosa scriverebbe, senza toccare ArcGIS")
    parser.add_argument("--intervallo", type=int,
                        default=int(os.environ.get("INTERVALLO_SECONDI", "20")),
                        help="Secondi fra un controllo e l'altro (default 20)")
    args = parser.parse_args()

    cartella = args.cartella or os.environ.get("XML_CARTELLA")
    if not cartella:
        parser.error("Indica la cartella degli XML con --cartella o la variabile XML_CARTELLA")
    if not os.path.isdir(cartella):
        parser.error("Cartella inesistente: {}".format(cartella))

    layer_url = os.environ.get("ARCGIS_INTERVENTI_LAYER_URL", "").rstrip("/")
    if not args.dry_run and not layer_url:
        parser.error("Manca la variabile d'ambiente ARCGIS_INTERVENTI_LAYER_URL")

    if args.dry_run or args.once:
        elabora(cartella, layer_url, forza=True, dry_run=args.dry_run)
        return

    log.info("Sorveglio %s ogni %s secondi", cartella, args.intervallo)
    while True:
        try:
            elabora(cartella, layer_url)
        except Exception:
            # Un errore di rete o un timeout non devono fermare il servizio:
            # si logga e si riprova al giro dopo.
            log.exception("Ciclo fallito, riprovo fra %s secondi", args.intervallo)
        time.sleep(args.intervallo)


if __name__ == "__main__":
    sys.exit(main())

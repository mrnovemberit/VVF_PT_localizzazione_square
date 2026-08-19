"""
Lettura e normalizzazione dei due XML prodotti dal software Oracle di gestione
interventi del comando.

Ogni file è una fotografia completa dello stato corrente (non un flusso di
eventi): questo modulo lo traduce nella lista di feature da scrivere sul
Feature Layer, senza toccare la rete. È tenuto separato da sync_interventi.py
proprio per poterlo collaudare a secco sui file di esempio.

Dati personali (RICHIEDENTE, TELE_NUMERO, NOME, COGNOME, COGNOME_NOME) non
vengono mai letti: non entrano nemmeno in memoria, così non possono finire
sul layer per errore.
"""

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

log = logging.getLogger("parser")
log.addHandler(logging.NullHandler())

# Gli orari negli XML sono ora locale italiana; ArcGIS vuole epoch in ms UTC.
try:
    from zoneinfo import ZoneInfo
    FUSO_LOCALE = ZoneInfo("Europe/Rome")
except Exception:  # se manca il pacchetto tzdata ripieghiamo sul fuso di sistema
    log.warning("zoneinfo non disponibile, uso il fuso orario di sistema")
    FUSO_LOCALE = None

FASE_CHIAMATA = "chiamata in attesa"
FASE_INTERVENTO = "intervento in corso"

# Gli orari di un intervento, nell'ordine in cui possono succedere. Serve per
# ricostruire la data di quelli che negli XML hanno solo l'ora (vedi _orari).
SEQUENZA_ORARI = [
    ("ORA_CHIAMATA", "Data_chiamata"),
    ("ORA_USCITA", "Ora_uscita"),
    ("ORA_ARRIVO", "Ora_arrivo"),
    ("ORA_PARTENZA_LUOGO", "Ora_partenza_luogo"),
    ("ORA_RIENTRO", "Ora_rientro"),
]


# ---------------------------------------------------------------------------
# Estrazione dei valori dai tag
# ---------------------------------------------------------------------------
def _testo(elemento, tag):
    """
    Valore testuale di un tag figlio, oppure None.

    Il software Oracle esprime i campi vuoti come elemento vuoto con
    l'attributo NULL="TRUE" (es. <ORA_RIENTRO NULL="TRUE"/>): vanno trattati
    come assenti, non come stringa vuota.
    """
    figlio = elemento.find(tag)
    if figlio is None or figlio.get("NULL") == "TRUE":
        return None
    valore = (figlio.text or "").strip()
    return valore or None


def _numero(elemento, tag):
    """Valore numerico di un tag, con la virgola decimale italiana."""
    valore = _testo(elemento, tag)
    if valore is None:
        return None
    try:
        return float(valore.replace(",", "."))
    except ValueError:
        log.warning("Valore numerico non interpretabile in <%s>: %r", tag, valore)
        return None


def _intero(elemento, tag):
    valore = _numero(elemento, tag)
    return int(valore) if valore is not None else None


def _epoch_ms(momento: datetime) -> int:
    """Converte un datetime locale in epoch millisecondi UTC (formato ArcGIS)."""
    if FUSO_LOCALE is not None:
        momento = momento.replace(tzinfo=FUSO_LOCALE)
    else:
        momento = momento.astimezone()
    return int(momento.timestamp() * 1000)


def _orari(elemento):
    """
    Ricostruisce i timestamp degli eventi dell'intervento.

    Negli XML solo la chiamata ha la data (GG/MM/AAAA); uscita, arrivo,
    partenza dal luogo e rientro hanno la sola ora (HH:MM). Li si colloca in
    sequenza: ogni orario anteriore al precedente appartiene al giorno
    successivo (un intervento iniziato alle 23:50 e rientrato alle 00:30 è
    rientrato l'indomani, non undici ore prima).
    """
    data_testo = _testo(elemento, "DATA_CHIAMATA")
    if not data_testo:
        return {}

    try:
        giorno = datetime.strptime(data_testo, "%d/%m/%Y")
    except ValueError:
        log.warning("Data chiamata non interpretabile: %r", data_testo)
        return {}

    risultato = {}
    precedente = None
    for tag, campo in SEQUENZA_ORARI:
        ora_testo = _testo(elemento, tag)
        if not ora_testo:
            continue
        try:
            ora, minuti = (int(p) for p in ora_testo.split(":")[:2])
        except ValueError:
            log.warning("Orario non interpretabile in <%s>: %r", tag, ora_testo)
            continue

        momento = giorno + timedelta(hours=ora, minutes=minuti)
        if precedente is not None and momento < precedente:
            momento += timedelta(days=1)
            giorno += timedelta(days=1)
        precedente = momento
        risultato[campo] = _epoch_ms(momento)

    return risultato


def _geometria(elemento):
    """Punto WGS84 dalle coordinate del record. COORD_X è la longitudine."""
    lon = _numero(elemento, "COORD_X")
    lat = _numero(elemento, "COORD_Y")
    if lat is None or lon is None:
        return None, None, None
    return lat, lon, {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}


def _minuti_da(epoch_ms, adesso_ms):
    if epoch_ms is None:
        return None
    return max(0, int((adesso_ms - epoch_ms) / 60000))


def _adesso_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------------------
# Stato operativo (derivato: negli XML non esiste)
# ---------------------------------------------------------------------------
def stato_operativo(orari):
    """
    Traduce gli orari valorizzati nella fase in cui si trova la squadra.
    È il campo su cui si basa la vestizione della webmap.
    """
    if orari.get("Ora_rientro"):
        return "rientrato"
    if orari.get("Ora_partenza_luogo"):
        return "in rientro"
    if orari.get("Ora_arrivo"):
        return "sul posto"
    if orari.get("Ora_uscita"):
        return "in uscita"
    return "in attesa"


# ---------------------------------------------------------------------------
# Chiamate in attesa
# ---------------------------------------------------------------------------
def leggi_chiamate(percorso, adesso_ms=None):
    """Legge chiamate_interventi.XML e restituisce le feature pronte."""
    adesso_ms = adesso_ms or _adesso_ms()
    radice = ET.parse(percorso).getroot()
    feature = []

    for chiamata in radice.findall("chiamata"):
        numero = _testo(chiamata, "CHIAMATA")
        if not numero:
            log.warning("Chiamata senza numero, saltata")
            continue

        lat, lon, geometria = _geometria(chiamata)
        if geometria is None:
            log.warning("Chiamata %s senza coordinate, saltata", numero)
            continue

        orari = _orari(chiamata)
        # Nelle chiamate la tipologia è un codice numerico: finché non abbiamo
        # la tabella di decodifica del software Oracle lo mostriamo come tale.
        codice = _testo(chiamata, "COD_TIPOLOGIA")

        attributi = {
            "Chiave": "C-{}".format(numero),
            "Fase": FASE_CHIAMATA,
            "Numero": numero,
            "Tipologia": "Codice {}".format(codice) if codice else None,
            "Dettaglio_tipologia": _testo(chiamata, "DETTAGLIO_TIPOLOGIA"),
            "Indirizzo": _testo(chiamata, "LOC_IND"),
            "Civico_km": _testo(chiamata, "CIV_KM"),
            "Comune": _testo(chiamata, "COMUNE"),
            "Provincia": _testo(chiamata, "SIGLA_PROVINCIA"),
            "Data_chiamata": orari.get("Data_chiamata"),
            "Stato_operativo": "in attesa",
            "Priorita": _intero(chiamata, "PRIORITA"),
            "Note": _testo(chiamata, "NOTE_INTERVENTO"),
            "Minuti_apertura": _minuti_da(orari.get("Data_chiamata"), adesso_ms),
            "Ultimo_agg": adesso_ms,
            "Latitudine": lat,
            "Longitudine": lon,
        }
        feature.append({"attributes": attributi, "geometry": geometria})

    return feature


# ---------------------------------------------------------------------------
# Interventi
# ---------------------------------------------------------------------------
def leggi_interventi(percorso, adesso_ms=None, stati_chiusi=()):
    """
    Legge interventi.XML e restituisce le feature pronte, una sola per
    intervento.

    Il file ha una riga per ogni coppia intervento x mezzo (lo stesso
    "3109 /1" compare una volta per l'APS e una per il CA boschi): qui le
    righe vengono aggregate, con i mezzi raccolti in un unico campo.

    Gli interventi conclusi vengono scartati: sulla mappa deve restare solo
    ciò che è realmente in corso.
    """
    adesso_ms = adesso_ms or _adesso_ms()
    radice = ET.parse(percorso).getroot()

    aggregati = {}
    stati_visti = set()

    for riga in radice.findall("intervento"):
        numero = _testo(riga, "INTERVENTO")
        if not numero:
            log.warning("Intervento senza numero, saltato")
            continue

        stato_oracle = _testo(riga, "STATUS")
        if stato_oracle:
            stati_visti.add(stato_oracle)

        orari = _orari(riga)
        chiuso = bool(orari.get("Ora_rientro")) or (
            stato_oracle is not None and stato_oracle in stati_chiusi
        )
        if chiuso:
            continue

        mezzo = _testo(riga, "SQUADRA_MEZZO")

        if numero in aggregati:
            # Righe successive dello stesso intervento: ci interessa solo il mezzo
            if mezzo:
                aggregati[numero]["mezzi"].add(mezzo)
            continue

        lat, lon, geometria = _geometria(riga)
        if geometria is None:
            log.warning("Intervento %s senza coordinate, saltato", numero)
            continue

        attributi = {
            "Chiave": "I-{}".format(numero),
            "Fase": FASE_INTERVENTO,
            "Numero": numero,
            "Tipologia": _testo(riga, "TIPOLOGIA"),
            "Indirizzo": _testo(riga, "INDIRIZZO"),
            "Civico_km": _testo(riga, "CIV_KM"),
            "Comune": _testo(riga, "COMUNE"),
            "Provincia": _testo(riga, "SIGLA_PROVINCIA"),
            "Stato_oracle": stato_oracle,
            "Stato_operativo": stato_operativo(orari),
            "Enti_intervenuti": _testo(riga, "ENTI_INTERVENUTI"),
            "Minuti_apertura": _minuti_da(orari.get("Data_chiamata"), adesso_ms),
            "Ultimo_agg": adesso_ms,
            "Latitudine": lat,
            "Longitudine": lon,
        }
        attributi.update(orari)

        aggregati[numero] = {
            "attributes": attributi,
            "geometry": geometria,
            "mezzi": {mezzo} if mezzo else set(),
        }

    if stati_visti - {"A"}:
        # Sappiamo che "A" indica un intervento aperto; se ne compaiono altri
        # vale la pena accorgersene, potrebbero richiedere di essere filtrati.
        log.warning("Valori di STATUS diversi da 'A' trovati negli XML: %s",
                    ", ".join(sorted(stati_visti - {"A"})))

    feature = []
    for dati in aggregati.values():
        mezzi = sorted(dati.pop("mezzi"))
        dati["attributes"]["Squadre_mezzi"] = "; ".join(mezzi) or None
        dati["attributes"]["Num_mezzi"] = len(mezzi)
        feature.append(dati)

    return feature

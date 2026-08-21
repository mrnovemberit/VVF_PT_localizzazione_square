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

import re
import math
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

# Quando più squadre dello stesso intervento sono uscite in tempi diversi,
# l'Oracle usa lo stesso numero con un suffisso "/N" diverso per ogni invio
# (es. "3171 /1" e "3171 /2"): è lo stesso evento fisico, non due interventi.
# Raggruppando per stato più "presente" invece che per ultimo invio, un
# intervento con anche una sola squadra ancora sul posto non risulta mai
# "sospeso" solo perché un'altra squadra ha già lasciato il luogo.
#
# "abbandonata" è lo stato di una singola riga/squadra che ha fatto
# ORA_PARTENZA_LUOGO: se TUTTE le righe di un intervento sono "abbandonata",
# l'intervento nel suo insieme diventa "sospeso" (vedi leggi_interventi).
PRIORITA_RIGA = {
    "sul posto": 3,
    "assegnato": 2,
    "in attesa": 1,
    "abbandonata": 0,
}

# Corrispondenza verificata sui dati reali fra il nostro stato calcolato e il
# campo STATUS di Oracle (A=assegnato, P=sul posto, S=sospeso): usata solo
# per un log di controllo, mai per decidere lo stato — non vogliamo dipendere
# da un vocabolario Oracle non documentato ufficialmente.
STATUS_ATTESO = {
    "assegnato": "A",
    "sul posto": "P",
    "sospeso": "S",
}

# Centroidi approssimati dei comuni della provincia di Pistoia (WGS84), usati
# solo come fallback quando l'XML non riporta COORD_X/COORD_Y: meglio un pin
# sul comune giusto, segnalato come stimato, che un intervento invisibile
# sulla mappa. Non sono coordinate catastali: se si nota un errore vistoso,
# vanno corrette qui.
COMUNE_CENTROIDI = {
    "abetone cutigliano": (44.1200, 10.6900),
    "agliana": (43.8794, 11.0000),
    "buggiano": (43.8697, 10.7147),
    "chiesina uzzanese": (43.8347, 10.7211),
    "lamporecchio": (43.8347, 10.8867),
    "larciano": (43.8347, 10.8656),
    "marliana": (43.9531, 10.7972),
    "massa e cozzile": (43.8994, 10.7508),
    "monsummano terme": (43.8722, 10.8156),
    "montale": (43.9317, 11.0006),
    "montecatini-terme": (43.8833, 10.7717),
    "pescia": (43.9022, 10.6875),
    "pieve a nievole": (43.8992, 10.8078),
    "pistoia": (43.9333, 10.9167),
    "ponte buggianese": (43.8189, 10.7481),
    "quarrata": (43.8500, 11.0167),
    "sambuca pistoiese": (44.1181, 10.9317),
    "san marcello piteglio": (44.0872, 10.7908),
    "serravalle pistoiese": (43.8833, 10.8333),
    "uzzano": (43.8686, 10.6986),
}

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
        # Il software Oracle a volte appende un suffisso all'orario, es.
        # "05:49 -s": si legge comunque l'ora, il resto si logga per
        # curiosità ma non blocca il parsing (prima andava perso l'intero
        # orario, con effetti a cascata su Stato_operativo).
        corrispondenza = re.match(r"^(\d{1,2}):(\d{2})", ora_testo)
        if not corrispondenza:
            log.warning("Orario non interpretabile in <%s>: %r", tag, ora_testo)
            continue
        ora, minuti = int(corrispondenza.group(1)), int(corrispondenza.group(2))
        suffisso = ora_testo[corrispondenza.end():].strip()
        if suffisso:
            log.info("Orario in <%s> con suffisso ignorato: %r (letto %02d:%02d)",
                      tag, suffisso, ora, minuti)

        momento = giorno + timedelta(hours=ora, minutes=minuti)
        if precedente is not None and momento < precedente:
            momento += timedelta(days=1)
            giorno += timedelta(days=1)
        precedente = momento
        risultato[campo] = _epoch_ms(momento)

    return risultato


def _suffisso_data(elemento):
    """
    'GGMMAAAA' dalla DATA_CHIAMATA, da usare nella Chiave.

    Il numero di chiamata dell'Oracle riparte da 1 ogni notte: senza la data,
    una C-27 di oggi e una C-27 di ieri (ancora nel file perché rimasta
    aperta) si scontrerebbero sulla stessa Chiave. Restituisce None se la
    data non è leggibile — il chiamante deve gestire il caso.
    """
    testo = _testo(elemento, "DATA_CHIAMATA")
    if not testo:
        return None
    try:
        giorno = datetime.strptime(testo, "%d/%m/%Y")
    except ValueError:
        return None
    return giorno.strftime("%d%m%Y")


# ---------------------------------------------------------------------------
# Tag nelle note (es. "#autoscala", "@trid") e corrispondenza chiamata/intervento
# ---------------------------------------------------------------------------
_TAG_REGEX = re.compile(r"[#@](\w+)", re.UNICODE)


def estrai_tag(nota):
    """Etichette tipo #autoscala o @trid trovate nel testo, minuscole e senza duplicati."""
    if not nota:
        return []
    return sorted({m.group(1).lower() for m in _TAG_REGEX.finditer(nota)})


def _chiave_nota(elemento):
    """
    Chiave di corrispondenza fra una chiamata e l'intervento in cui diventa,
    basata su DATA_CHIAMATA + ORA_CHIAMATA + COMUNE: sono gli unici tre campi
    presenti con lo stesso significato in entrambi gli XML. Non si può usare
    il numero (CHIAMATA e INTERVENTO sono due numerazioni indipendenti).
    """
    data = _testo(elemento, "DATA_CHIAMATA")
    ora = _testo(elemento, "ORA_CHIAMATA")
    comune = _testo(elemento, "COMUNE")
    if not (data and ora and comune):
        return None
    return "{}|{}|{}".format(data, ora, comune.strip().casefold())


# ---------------------------------------------------------------------------
# Zone di competenza (poligoni disegnati dal comando, non i confini comunali)
# ---------------------------------------------------------------------------
def _da_web_mercator(x, y):
    """Converte una coppia di coordinate da Web Mercator (EPSG:3857) a WGS84."""
    raggio = 20037508.342789244
    lon = x / raggio * 180
    lat = 180 / math.pi * (2 * math.atan(math.exp(y / raggio * math.pi)) - math.pi / 2)
    return lon, lat


def poligoni_da_query(risposta, campo_nome="ZONA_COMPETENZA"):
    """
    Converte la risposta grezza di una query ArcGIS su un layer poligonale
    (query_poligoni(), con "features" e "spatialReference") nel formato usato
    da zona_competenza(): una lista di {"nome": ..., "anelli": [[(lon, lat),
    ...]]} in WGS84, pronta per il punto-in-poligono.

    Le coordinate vengono convertite da Web Mercator (EPSG 3857/102100), la
    proiezione con cui ArcGIS Online salva di norma un layer disegnato nella
    webmap. Se il layer risultasse in un'altra proiezione, la conversione
    darebbe coordinate sbagliate: viene solo loggato un avviso, perché
    supportare proiezioni arbitrarie servirebbe una libreria dedicata
    (pyproj) che oggi non serve.
    """
    sr = (risposta.get("spatialReference") or {})
    wkid = sr.get("wkid") or sr.get("latestWkid")
    if wkid not in (3857, 102100):
        log.warning(
            "Layer zone di competenza in proiezione %s, non Web Mercator: "
            "le coordinate convertite sarebbero sbagliate, zone non caricate",
            wkid,
        )
        return []

    poligoni = []
    for feature in risposta.get("features", []):
        nome = feature.get("attributes", {}).get(campo_nome)
        rings = feature.get("geometry", {}).get("rings")
        if not nome or not rings:
            continue
        anelli = [[_da_web_mercator(x, y) for x, y in anello] for anello in rings]
        poligoni.append({"nome": nome, "anelli": anelli})
    return poligoni


def _punto_in_poligono(lon, lat, anelli):
    """
    Punto-in-poligono con la regola even-odd (ray casting) su tutti gli anelli
    insieme: gestisce correttamente sia i buchi sia le forme spezzate in più
    parti separate, che è come ArcGIS rappresenta un singolo poligono.
    """
    dentro = False
    for anello in anelli:
        n = len(anello)
        j = n - 1
        for i in range(n):
            xi, yi = anello[i]
            xj, yj = anello[j]
            if (yi > lat) != (yj > lat):
                x_intersezione = (xj - xi) * (lat - yi) / (yj - yi) + xi
                if lon < x_intersezione:
                    dentro = not dentro
            j = i
    return dentro


def zona_competenza(lat, lon, poligoni):
    """
    Nome della zona di competenza in cui cade il punto, o "Fuori zona" se non
    rientra in nessuna delle forme caricate. None se i poligoni non sono
    (ancora) disponibili — da non confondere con "Fuori zona": quello è un
    risultato calcolato, questo è "non abbiamo potuto calcolarlo".
    """
    if not poligoni:
        return None
    for poligono in poligoni:
        if _punto_in_poligono(lon, lat, poligono["anelli"]):
            return poligono["nome"]
    return "Fuori zona"


# Sigla radio usata in sala operativa per ciascuna zona di competenza. Due
# zone diverse (Centrale/Pistoia e Montemurlo) condividono la stessa sigla:
# non è un errore, è così che le usa il comando.
MAPPA_AREA_EMERGENZA = {
    "centrale": "ALFA",
    "montemurlo": "ALFA",
    "montecatini": "MIKE",
    "pescia": "DELTA",
    "san marcello": "SIERRA",
}


def area_emergenza(zona):
    """
    Traduce il nome della zona (da zona_competenza) nella sigla radio.

    None se la zona non è calcolabile (stesso significato di zona_competenza
    con poligoni non caricati). "FUORI ZONA" per "Fuori zona" e per qualunque
    nome di zona non ancora presente in MAPPA_AREA_EMERGENZA — es. se un
    domani il comando aggiunge una sesta zona, questa va aggiunta qui sopra,
    e nel frattempo lo si nota da un log invece che da una sigla sbagliata.
    """
    if zona is None:
        return None
    sigla = MAPPA_AREA_EMERGENZA.get(zona.strip().casefold())
    if sigla is None and zona.strip().casefold() != "fuori zona":
        log.warning("Zona di competenza '%s' senza sigla radio nota, trattata come fuori zona", zona)
    return sigla or "FUORI ZONA"


def _geometria(elemento):
    """
    Punto WGS84 dalle coordinate del record. COORD_X è la longitudine.

    Se mancano COORD_X/COORD_Y (capita per ~1 chiamata su 3), ripiega sul
    centroide del Comune invece di scartare l'elemento: altrimenti chiamate e
    interventi realmente attivi sparirebbero del tutto dalla mappa. Il
    fallback viene segnalato in "Posizione_stimata" così la webmap può
    vestirlo in modo riconoscibile e non farlo sembrare un posto preciso.

    Restituisce (lat, lon, geometria, stimata) dove stimata è "Sì" se il
    punto è il centroide del comune, None se è la posizione esatta.
    """
    lon = _numero(elemento, "COORD_X")
    lat = _numero(elemento, "COORD_Y")
    if lat is not None and lon is not None:
        return lat, lon, {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}, None

    comune = _testo(elemento, "COMUNE")
    centro = COMUNE_CENTROIDI.get(comune.strip().casefold()) if comune else None
    if centro is None:
        return None, None, None, None
    lat, lon = centro
    return lat, lon, {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}, "Sì"


def _numero_base(numero):
    """
    Numero intervento senza il suffisso "/N" del sotto-invio.

    "3171 /1" e "3171 /2" sono lo stesso evento fisico (stesso indirizzo,
    stesse coordinate) con squadre uscite in momenti diversi: raggrupparli
    per il numero letterale intero genera due pin sovrapposti con stati
    anche contraddittori (uno "sul posto", l'altro "sospeso").
    """
    return re.sub(r"\s*/\s*\d+\s*$", "", numero).strip()


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

    Per le chiamate è sempre "in attesa" (impostato direttamente in
    leggi_chiamate). Per gli interventi, "abbandonata" è la sola risposta
    valida per una singola riga/squadra: è compito del chiamante (vedi
    leggi_interventi) tradurla in "sospeso" quando TUTTE le squadre di un
    intervento hanno abbandonato il luogo — un intervento con anche una
    sola squadra ancora sul posto non deve mai risultare sospeso.
    """
    if orari.get("Ora_partenza_luogo"):
        return "abbandonata"
    if orari.get("Ora_arrivo"):
        return "sul posto"
    if orari.get("Ora_uscita"):
        return "assegnato"
    return "in attesa"


# ---------------------------------------------------------------------------
# Chiamate in attesa
# ---------------------------------------------------------------------------
def leggi_chiamate(percorso, adesso_ms=None, poligoni=None):
    """Legge chiamate_interventi.XML e restituisce le feature pronte."""
    adesso_ms = adesso_ms or _adesso_ms()
    radice = ET.parse(percorso).getroot()
    feature = []

    for chiamata in radice.findall("chiamata"):
        numero = _testo(chiamata, "CHIAMATA")
        if not numero:
            log.warning("Chiamata senza numero, saltata")
            continue

        lat, lon, geometria, stimata = _geometria(chiamata)
        if geometria is None:
            log.warning(
                "Chiamata %s senza coordinate e senza comune riconosciuto, saltata",
                numero,
            )
            continue
        if stimata:
            log.info("Chiamata %s senza coordinate: posizionata sul centroide del comune", numero)

        suffisso_data = _suffisso_data(chiamata)
        if suffisso_data is None:
            log.warning(
                "Chiamata %s senza DATA_CHIAMATA leggibile: la Chiave non "
                "include la data, rischio di collisione con altri giorni",
                numero,
            )
        chiave = "C-{}-{}".format(numero, suffisso_data) if suffisso_data else "C-{}".format(numero)

        orari = _orari(chiamata)
        # Nelle chiamate la tipologia è un codice numerico: finché non abbiamo
        # la tabella di decodifica del software Oracle lo mostriamo come tale.
        codice = _testo(chiamata, "COD_TIPOLOGIA")
        nota = _testo(chiamata, "NOTE_INTERVENTO")
        zona = zona_competenza(lat, lon, poligoni)

        attributi = {
            "Chiave": chiave,
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
            "Note": nota,
            "Tag": "; ".join(estrai_tag(nota)) or None,
            "Zona_competenza": zona,
            "Area_emergenza": area_emergenza(zona),
            "Minuti_apertura": _minuti_da(orari.get("Data_chiamata"), adesso_ms),
            "Ultimo_agg": adesso_ms,
            "Latitudine": lat,
            "Longitudine": lon,
            "Posizione_stimata": stimata,
        }
        feature.append({"attributes": attributi, "geometry": geometria})

    return feature


def note_da_chiamate(percorso):
    """
    Note delle chiamate (con eventuali tag), indicizzate per _chiave_nota.

    Serve a "ricordare" la nota mentre la chiamata è ancora in coda: appena
    diventa un intervento, Oracle smette di riportarla da qualunque parte, e
    senza questo lo sganciamento della squadra ne farebbe perdere il
    contenuto (compresi i tag tipo #autoscala usati per il filtro).
    """
    radice = ET.parse(percorso).getroot()
    risultato = {}
    for chiamata in radice.findall("chiamata"):
        nota = _testo(chiamata, "NOTE_INTERVENTO")
        if not nota:
            continue
        chiave = _chiave_nota(chiamata)
        if chiave is None:
            continue
        risultato[chiave] = {"nota": nota, "tag": estrai_tag(nota)}
    return risultato


# ---------------------------------------------------------------------------
# Interventi
# ---------------------------------------------------------------------------
def leggi_interventi(percorso, adesso_ms=None, stati_chiusi=(), note_cache=None, poligoni=None):
    """
    Legge interventi.XML e restituisce le feature pronte, una sola per
    intervento fisico.

    Il file ha una riga per ogni combinazione intervento x mezzo x sotto-invio
    (lo stesso "3171" può comparire come "3171 /1" e "3171 /2" se sono uscite
    squadre in momenti diversi, e ognuna di queste righe si ripete una volta
    per mezzo): qui tutte le righe con lo stesso numero base vengono raccolte
    in un'unica feature, con lo stato calcolato sulla squadra più "presente"
    (vedi PRIORITA_RIGA), non sull'ultima letta. Un intervento con anche una
    sola squadra ancora sul posto non deve mai risultare "sospeso" solo
    perché un'altra squadra ha già lasciato il luogo.

    Un intervento resta scritto sul layer anche quando TUTTE le squadre hanno
    abbandonato il luogo (Stato_operativo diventa "sospeso", Squadre_mezzi
    resta vuoto): non sappiamo se verrà chiuso o riassegnato, e sparire dalla
    mappa toglierebbe visibilità su qualcosa che potrebbe tornare attivo. La
    rimozione avviene solo quando l'intervento sparisce del tutto dal file —
    se ne occupa già il riallineamento in sync_interventi.py, non serve
    scartare nulla qui (stati_chiusi resta solo una valvola manuale per un
    futuro codice STATUS confermato come chiusura definitiva).

    note_cache (opzionale): dizionario prodotto da note_da_chiamate() e
    mantenuto da sync_interventi.py fra un ciclo e l'altro. L'XML degli
    interventi non ha un campo nota: se la chiamata d'origine è ancora in
    cache (vedi _chiave_nota), Note e Tag vengono ereditati da lì.

    poligoni (opzionale): lista prodotta da poligoni_da_query(), usata per
    calcolare Zona_competenza. Senza poligoni il campo resta None.
    """
    adesso_ms = adesso_ms or _adesso_ms()
    note_cache = note_cache or {}
    radice = ET.parse(percorso).getroot()

    gruppi = {}

    for riga in radice.findall("intervento"):
        numero = _testo(riga, "INTERVENTO")
        if not numero:
            log.warning("Intervento senza numero, saltato")
            continue

        stato_oracle = _testo(riga, "STATUS")

        orari = _orari(riga)
        stato_riga = stato_operativo(orari)
        mezzo = _testo(riga, "SQUADRA_MEZZO")

        numero_base = _numero_base(numero)
        gruppo = gruppi.setdefault(numero_base, {"righe": [], "numeri": set()})
        gruppo["righe"].append({
            "riga": riga,
            "orari": orari,
            "stato": stato_riga,
            "stato_oracle": stato_oracle,
            "mezzo": mezzo,
        })
        gruppo["numeri"].add(numero)

    feature = []
    for numero_base, gruppo in gruppi.items():
        righe = gruppo["righe"]

        # Valvola manuale: se un domani si scopre con certezza un codice
        # STATUS che significa chiusura definitiva, lo si aggiunge a
        # STATI_CHIUSI (env var) senza toccare il codice. Oggi è vuota, quindi
        # questo controllo non scarta mai nulla.
        stati_oracle_gruppo = {r["stato_oracle"] for r in righe if r["stato_oracle"]}
        if stati_oracle_gruppo and stati_oracle_gruppo <= set(stati_chiusi):
            continue

        if len(gruppo["numeri"]) > 1:
            log.info(
                "Intervento %s: %d sotto-invii raggruppati (%s)",
                numero_base, len(gruppo["numeri"]), ", ".join(sorted(gruppo["numeri"])),
            )

        migliore = max(righe, key=lambda r: PRIORITA_RIGA.get(r["stato"], 0))
        stato_gruppo = "sospeso" if migliore["stato"] == "abbandonata" else migliore["stato"]

        atteso = STATUS_ATTESO.get(stato_gruppo)
        if atteso and migliore["stato_oracle"] and migliore["stato_oracle"] != atteso:
            log.warning(
                "Intervento %s: stato calcolato '%s' (atteso STATUS='%s') ma Oracle riporta STATUS='%s'",
                numero_base, stato_gruppo, atteso, migliore["stato_oracle"],
            )

        lat = lon = geometria = stimata = None
        for r in righe:
            lat, lon, geometria, stimata = _geometria(r["riga"])
            if geometria is not None and not stimata:
                break  # posizione esatta trovata, non serve cercare oltre
        if geometria is None:
            log.warning(
                "Intervento %s senza coordinate e senza comune riconosciuto, saltato",
                numero_base,
            )
            continue
        if stimata:
            log.info("Intervento %s senza coordinate: posizionato sul centroide del comune", numero_base)

        riga_rif = migliore["riga"]
        suffisso_data = _suffisso_data(riga_rif)
        if suffisso_data is None:
            log.warning(
                "Intervento %s senza DATA_CHIAMATA leggibile: la Chiave non "
                "include la data, rischio di collisione con altri giorni",
                numero_base,
            )
        chiave = "I-{}-{}".format(numero_base, suffisso_data) if suffisso_data else "I-{}".format(numero_base)

        nota_ereditata = note_cache.get(_chiave_nota(riga_rif), {})
        zona = zona_competenza(lat, lon, poligoni)

        attributi = {
            "Chiave": chiave,
            "Fase": FASE_INTERVENTO,
            "Numero": numero_base,
            "Tipologia": _testo(riga_rif, "TIPOLOGIA"),
            "Indirizzo": _testo(riga_rif, "INDIRIZZO"),
            "Civico_km": _testo(riga_rif, "CIV_KM"),
            "Comune": _testo(riga_rif, "COMUNE"),
            "Provincia": _testo(riga_rif, "SIGLA_PROVINCIA"),
            "Stato_oracle": migliore["stato_oracle"],
            "Stato_operativo": stato_gruppo,
            "Enti_intervenuti": _testo(riga_rif, "ENTI_INTERVENUTI"),
            "Note": nota_ereditata.get("nota"),
            "Tag": "; ".join(nota_ereditata.get("tag", [])) or None,
            "Zona_competenza": zona,
            "Area_emergenza": area_emergenza(zona),
            "Minuti_apertura": _minuti_da(migliore["orari"].get("Data_chiamata"), adesso_ms),
            "Ultimo_agg": adesso_ms,
            "Latitudine": lat,
            "Longitudine": lon,
            "Posizione_stimata": stimata,
        }
        attributi.update(migliore["orari"])

        # Solo le squadre ancora presenti: è la fotografia di chi c'è ora,
        # non lo storico di chi è mai intervenuto. Se sospeso, resta vuoto.
        mezzi = sorted({r["mezzo"] for r in righe if r["stato"] != "abbandonata" and r["mezzo"]})
        attributi["Squadre_mezzi"] = "; ".join(mezzi) or None
        attributi["Num_mezzi"] = len(mezzi)

        feature.append({"attributes": attributi, "geometry": geometria})

    return feature

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
# Corrispondenza chiamata/intervento (per far ereditare la nota)
# ---------------------------------------------------------------------------
_SUFFISSO_INDIRIZZO = re.compile(r"\s*\([^)]*\)\s*$")


def _normalizza_indirizzo(testo):
    """
    Toglie annotazioni fra parentesi (es. "Via Dante Alighieri (*)", marcatura
    di Oracle non riportata sull'intervento corrispondente) e uniforma spazi e
    maiuscole, solo per il confronto — l'Indirizzo mostrato in mappa resta
    quello originale.
    """
    if not testo:
        return None
    pulito = _SUFFISSO_INDIRIZZO.sub("", testo).strip().casefold()
    pulito = re.sub(r"\s+", " ", pulito)
    return pulito or None


def _chiave_nota(elemento):
    """
    Chiave di corrispondenza fra una chiamata e l'intervento in cui diventa,
    basata su DATA_CHIAMATA + ORA_CHIAMATA + INDIRIZZO: il comune da solo era
    troppo grezzo (più chiamate diverse nello stesso comune alla stessa ora
    non sono rari nelle notti di allerta). Il tag dell'indirizzo cambia nome
    fra i due XML (LOC_IND nelle chiamate, INDIRIZZO negli interventi), quindi
    si cercano entrambi. Non si può usare il numero (CHIAMATA e INTERVENTO
    sono due numerazioni indipendenti).
    """
    data = _testo(elemento, "DATA_CHIAMATA")
    ora = _testo(elemento, "ORA_CHIAMATA")
    indirizzo = _normalizza_indirizzo(_testo(elemento, "INDIRIZZO") or _testo(elemento, "LOC_IND"))
    if not (data and ora and indirizzo):
        return None
    return "{}|{}|{}".format(data, ora, indirizzo)


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


# Tabella di decodifica COD_TIPOLOGIA del software Oracle del comando.
COD_TIPOLOGIA_DESCRIZIONI = {
    "1": "Incendio normale (generico)",
    "2": "Incendio ed esplosione",
    "3": "Esplosione da sostanza esplodente",
    "4": "Esplosione (escluso esplosione da sostanza esplodente)",
    "301": "Incendio bosco, sterpaglie, colture",
    "302": "Incidente con incendio di mezzo trasport. merci pericolose",
    "365": "Incendio Rifiuti",
    "5": "Incendio aeromobile",
    "7": "Emergenza carrello aeromobile bloccato",
    "8": "Recupero aeromobile",
    "9": "Ricerca aeromobile",
    "303": "Incendio in sedime aeroportuale",
    "304": "Emergenza aeroportuale di altro genere",
    "305": "Incidente aeromobile di altro genere",
    "6": "Incendio nave o traghetto",
    "306": "Incendi natanti diversi da navi e traghetti",
    "307": "Recupero di piccoli natanti",
    "308": "Soccorso a imbarcazione senza governo",
    "309": "Ricerca/soccorso in amb. acquatico di imbarcazioni o natanti",
    "310": "Operazioni antinquinamento",
    "311": "Ricerca e/o soccorso a persona in mare (SAR)",
    "312": "Recupero di ostacoli alla navigazione",
    "313": "Supporto nautico per operazioni a terra",
    "362": "Incidente nautico",
    "10": "Incidente stradale generico",
    "11": "Incidente stradale con mezzo trasportante merci pericolose",
    "12": "Ribaltamento di mezzo trasportante merci pericolose",
    "13": "Rimozione ostacoli non dovuti al traffico",
    "314": "Incidente stradale in galleria",
    "14": "Recupero merci e beni",
    "15": "Recupero sostanze radioattive",
    "16": "Valanghe",
    "315": "Recupero parafulmini radioattivi",
    "348": "Recupero sostanza pericolosa",
    "358": "Recupero autovetture e veicoli",
    "18": "Recupero merci avariate",
    "19": "Cedimento terreno, voragine",
    "20": "Cedimento sede stradale",
    "21": "Dissesto statico di elementi costruttivi",
    "316": "Frane",
    "317": "Valanghe, slavine",
    "318": "Crollo parziale di elementi strutturali",
    "319": "Crollo generalizzato di opere e costruzioni",
    "351": "Smontaggio controllato di elementi costruttivi",
    "352": "Demolizioni",
    "353": "Rimozione macerie",
    "354": "Verifiche stabilità edifici manufatti frane voragini",
    "363": "Verifiche statiche per compilazione schede AEDES",
    "364": "Verifiche statiche speditive (TRIAGE)",
    "22": "Danni d'acqua in genere",
    "23": "Straripamenti, inondazioni, mareggiate",
    "24": "Rifornimento idrico",
    "25": "Prosciugamento in genere",
    "320": "Danni d'acqua per rott./fuoriusc. da tubaz., canali e simili",
    "29": "Recupero salme",
    "30": "Recupero animali morti",
    "34": "Apertura porte e finestre",
    "35": "Incidenti sul lavoro",
    "36": "Servizio di assistenza (generico)",
    "37": "Fuga gas",
    "38": "Lavaggio strada",
    "321": "Incidente ferroviario",
    "322": "Scoppio (cedimento meccanico)",
    "346": "Alberi pericolanti",
    "349": "Rimozione neve dai tetti",
    "350": "Coperture tetti",
    "355": "Progettazione opere provisionali",
    "356": "Opere provisionali senza progettazione",
    "357": "Opere provisionali con progettazione",
    "359": "Messa in sicurezza serbatoi GPL",
    "360": "Interventi impianti di servizio acqua, energia elettr. gas",
    "366": "Attivita' di polizia giudiziaria",
    "26": "Soccorso a persone",
    "27": "Salvataggio persone",
    "28": "Salvataggio animali",
    "31": "Trasporto ammalati o infortunati",
    "33": "Ascensori bloccati",
    "324": "Ricerca persona (SAR)",
    "325": "Recupero animali",
    "327": "Assistenza Trattamento Sanitario Obbl. (TSO)",
    "328": "Bonifica da insetti",
    "329": "Assistenza per abbandono locali e/o ambienti",
    "330": "Recupero con verricello elicottero",
    "331": "Ricerca e ricognizione aerea",
    "332": "Ricerca da allarme COSPAS e simili",
    "333": "Trasporto al gancio baricentrico elicottero",
    "334": "Mancata adozione di dispositivi di protezione individuale",
    "347": "Messa in sicurezza di imbarcazioni, natanti e/o galleggianti",
    "335": "Fuoriuscita di sostanza infiammabile/combustibile",
    "336": "Fuoriuscita di sostanza comburente",
    "337": "Fuoriuscita di sostanza tossica",
    "338": "Fuoriuscita di sostanza asfissiante",
    "339": "Fuoriuscita di sostanza radioattiva",
    "340": "Fuoriuscita di sostanza biologica",
    "341": "Fuoriuscita di sostanza combustibile in polvere",
    "342": "Fuoriuscita di sostanza infiammabile seguita da incendio",
    "343": "Fuoriuscita di altro tipo di sostanza",
    "344": "Inquinamento acque superficiali o di falda",
    "345": "Inquinamento di aria",
    "361": "Monitoraggio strumentale presenza di sostanze pericolose",
    "400": "Socc., salvataggi persone (anche recupero vittime) - Sisma",
    "401": "Socc., salvataggi animali (anche recupero carcasse) - Sisma",
    "402": "Recupero beni privati e pubblici-Sisma",
    "403": "Recupero autovetture e veicoli - Sisma",
    "404": "Rimozione alberi - Sisma",
    "405": "Ass. popolo, trasporti viveri, accesso aree peric. - Sisma",
    "406": "Rimozione parti pericolanti, tegole, camini ecc. - Sisma",
    "407": "Rimozione neve dai tetti - Sisma",
    "408": "Coperture tetti - Sisma",
    "409": "Smontaggio controllato di elementi costruttivi - Sisma",
    "410": "Demolizioni - Sisma",
    "411": "Rimozione macerie - Sisma",
    "412": "Verifiche stabilità edifici manufatti frane voragini - Sisma",
    "413": "Ripristino viabilità, frane, cedimenti, voragini - Sisma",
    "414": "Progettazione opere provisionali - Sisma",
    "415": "Opere provisionali senza progettazione - Sisma",
    "416": "Opere provisionali con progettazione - Sisma",
    "417": "Recupero merci avariate - Sisma",
    "418": "Messa in sicurezza serbatoi GPL - Sisma",
    "419": "Recupero e messa in sicurezza materiali pericolosi - Sisma",
    "420": "Interv. impianti servizio acqua, energia elettr. gas - Sisma",
    "421": "Monitoraggio strumentale presenza sost. pericolose - Sisma",
    "422": "Assistenza tecnica in genere - Sisma",
    "423": "Supporto sistemazione logistica della popolazione - Sisma",
    "424": "Spegnimento incendio - Sisma",
    "425": "Svuotamento aree allagate, danni d'acqua, maltempo - Sisma",
    "90": "Emergenza protezione civile",
    "900": "Supporto ai Sindaci per attivita' di informazione",
    "901": "Assistenza attivita' di Protezione Civile e Ospedali",
    "902": "Igienizzazione locali pubblici o aree esterne",
    "903": "Supporto trasporto materiale urgente",
    "904": "Supporto gestione comunale persone senza dimora",
}


def descrivi_tipologia(codice):
    """
    Traduce COD_TIPOLOGIA nella descrizione testuale della tabella del
    comando. "Codice NN" per un codice non ancora presente in
    COD_TIPOLOGIA_DESCRIZIONI — se il comando ne introduce uno nuovo, lo si
    nota da un log invece che da un valore silenziosamente sbagliato.
    """
    if not codice:
        return None
    descrizione = COD_TIPOLOGIA_DESCRIZIONI.get(codice.strip())
    if descrizione is None:
        log.warning("COD_TIPOLOGIA '%s' senza descrizione nota, mostrato come codice", codice)
        return "Codice {}".format(codice)
    return descrizione


def _geometria(elemento):
    """
    Punto WGS84 dalle coordinate del record. COORD_X è la longitudine.

    Se mancano COORD_X/COORD_Y (capita per ~1 chiamata su 3), ripiega su un
    centroide invece di scartare l'elemento: altrimenti chiamate e interventi
    realmente attivi sparirebbero del tutto dalla mappa. Tre livelli, dal più
    preciso al più approssimativo: comune della provincia di Pistoia
    (COMUNE_CENTROIDI), comune italiano qualunque per il mutuo soccorso fuori
    provincia (_centroide_comune_italia, in fondo al file), e se anche quello
    non trova nulla (comune assente o scritto in modo irriconoscibile) il
    comando di Pistoia stesso — un pin nel posto sbagliato segnalato come
    stimato è sempre meglio di un intervento invisibile. Il fallback viene
    segnalato in "Posizione_stimata" così la webmap può vestirlo in modo
    riconoscibile e non farlo sembrare un posto preciso.

    Restituisce (lat, lon, geometria, stimata) dove stimata è "Sì" se il
    punto non è la posizione esatta del record, None se lo è.
    """
    lon = _numero(elemento, "COORD_X")
    lat = _numero(elemento, "COORD_Y")
    if lat is not None and lon is not None:
        return lat, lon, {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}, None

    comune = _testo(elemento, "COMUNE")
    centro = COMUNE_CENTROIDI.get(comune.strip().casefold()) if comune else None

    if centro is None and comune:
        provincia = _testo(elemento, "SIGLA_PROVINCIA")
        centro = _centroide_comune_italia(comune, provincia)

    if centro is None:
        log.warning(
            "Comune '%s' non riconosciuto in nessun elenco: pin sul comando di Pistoia",
            comune,
        )
        centro = COMUNE_CENTROIDI["pistoia"]

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
    Traduce gli orari valorizzati nella fase in cui si trova una singola
    riga/squadra di un intervento (uso interno, per scegliere la riga
    "migliore" fra sotto-invii — non è più scritto come attributo del layer,
    dove il campo di stato è ormai solo Stato_oracle).

    "abbandonata" è la sola risposta valida per una singola riga/squadra: è
    compito del chiamante (vedi leggi_interventi) tradurla in "sospeso"
    quando TUTTE le squadre di un intervento hanno abbandonato il luogo — un
    intervento con anche una sola squadra ancora sul posto non deve mai
    risultare sospeso.
    """
    if orari.get("Ora_partenza_luogo"):
        return "abbandonata"
    if orari.get("Ora_arrivo"):
        return "sul posto"
    if orari.get("Ora_uscita"):
        return "assegnato"
    return "in attesa"


def _formatta_squadre_mezzi(mezzi_grezzi):
    """
    Raggruppa "SQUADRA+MEZZO" per squadra, così una squadra con più mezzi
    compare una sola volta invece di ripetersi per ognuno (es. "DELTA
    1+APS MAN" e "DELTA 1+CA BOSCHI_3" diventano "DELTA 1; APS MAN + CA
    BOSCHI_3"). Squadre diverse sono separate da " | " per non confondersi
    con il "; " già usato dentro il singolo blocco.
    """
    per_squadra = {}
    for grezzo in mezzi_grezzi:
        squadra, sep, mezzo = grezzo.partition("+")
        squadra = squadra.strip()
        mezzo = mezzo.strip() if sep else ""
        elenco = per_squadra.setdefault(squadra, [])
        if mezzo and mezzo not in elenco:
            elenco.append(mezzo)
    blocchi = [
        "{}; {}".format(squadra, " + ".join(mezzi)) if mezzi else squadra
        for squadra, mezzi in sorted(per_squadra.items())
    ]
    return " | ".join(blocchi)


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
        codice = _testo(chiamata, "COD_TIPOLOGIA")
        nota = _testo(chiamata, "NOTE_INTERVENTO")
        zona = zona_competenza(lat, lon, poligoni)

        attributi = {
            "Chiave": chiave,
            "Fase": FASE_CHIAMATA,
            "Numero": numero,
            "Tipologia": descrivi_tipologia(codice),
            "Dettaglio_tipologia": _testo(chiamata, "DETTAGLIO_TIPOLOGIA"),
            "Indirizzo": _testo(chiamata, "LOC_IND"),
            "Civico_km": _testo(chiamata, "CIV_KM"),
            "Comune": _testo(chiamata, "COMUNE"),
            "Provincia": _testo(chiamata, "SIGLA_PROVINCIA"),
            "Data_chiamata": orari.get("Data_chiamata"),
            "Priorita": _intero(chiamata, "PRIORITA"),
            "Note": nota,
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
    Note delle chiamate, indicizzate per _chiave_nota.

    Serve a "ricordare" la nota mentre la chiamata è ancora in coda: appena
    diventa un intervento, Oracle smette di riportarla da qualunque parte, e
    senza questo lo sganciamento della squadra ne farebbe perdere il
    contenuto.
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
        risultato[chiave] = {"nota": nota}
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
    abbandonato il luogo (Stato_oracle diventa "S", Squadre_mezzi resta
    vuoto): non sappiamo se verrà chiuso o riassegnato, e sparire dalla mappa
    toglierebbe visibilità su qualcosa che potrebbe tornare attivo. La
    rimozione avviene solo quando l'intervento sparisce del tutto dal file —
    se ne occupa già il riallineamento in sync_interventi.py, non serve
    scartare nulla qui (stati_chiusi resta solo una valvola manuale per un
    futuro codice STATUS confermato come chiusura definitiva).

    note_cache (opzionale): dizionario prodotto da note_da_chiamate() e
    mantenuto da sync_interventi.py fra un ciclo e l'altro. L'XML degli
    interventi non ha un campo nota: se la chiamata d'origine è ancora in
    cache (vedi _chiave_nota), Note viene ereditata da lì.

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
            "Enti_intervenuti": _testo(riga_rif, "ENTI_INTERVENUTI"),
            "Note": nota_ereditata.get("nota"),
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
        attributi["Squadre_mezzi"] = _formatta_squadre_mezzi(mezzi) or None
        attributi["Num_mezzi"] = len(mezzi)

        feature.append({"attributes": attributi, "geometry": geometria})

    return feature


# ---------------------------------------------------------------------------
# Centroidi di riserva per comuni fuori dalla provincia di Pistoia
# ---------------------------------------------------------------------------
# Usati solo quando un intervento/chiamata di mutuo soccorso fuori provincia
# arriva senza COORD_X/COORD_Y e il comune non è in COMUNE_CENTROIDI sopra
# (vedi _geometria). Dataset ISTAT (comune, provincia, centroide WGS84),
# fonte github.com/opendatasicilia/comuni-italiani, compattato come testo
# invece che come dizionario per restare leggero da leggere nel sorgente — il
# parsing avviene una sola volta, all'importazione del modulo.
_COMUNI_ITALIA_TESTO = """
agliè|to|45.367055|7.766918
airasca|to|44.917042|7.484321
ala di stura|to|45.31515|7.305909
albiano d'ivrea|to|45.432959|7.950621
almese|to|45.116876|7.39455
alpette|to|45.408916|7.578978
alpignano|to|45.095724|7.525403
andezeno|to|45.037748|7.870527
andrate|to|45.52736|7.880318
angrogna|to|44.843582|7.223816
arignano|to|45.040275|7.901594
avigliana|to|45.077944|7.39839
azeglio|to|45.423459|7.993922
bairo|to|45.386587|7.755447
balangero|to|45.275323|7.520282
baldissero canavese|to|45.410256|7.744217
baldissero torinese|to|45.068636|7.816531
balme|to|45.302504|7.215432
banchette|to|45.453558|7.856318
barbania|to|45.291252|7.630317
bardonecchia|to|45.078339|6.703205
barone canavese|to|45.325527|7.873063
beinasco|to|45.022097|7.579397
bibiana|to|44.798937|7.288219
bobbio pellice|to|44.807952|7.116764
bollengo|to|45.470859|7.94202
borgaro torinese|to|45.152145|7.657973
borgiallo|to|45.417055|7.668515
borgofranco d'ivrea|to|45.51286|7.858017
borgomasino|to|45.363257|7.987523
borgone susa|to|45.122844|7.239712
bosconero|to|45.26755|7.7666
brandizzo|to|45.176606|7.838004
bricherasio|to|44.823409|7.305296
brosso|to|45.492502|7.803288
brozolo|to|45.117436|8.073603
bruino|to|45.021152|7.468551
brusasco|to|45.155492|8.060103
bruzolo|to|45.142226|7.195648
buriasco|to|44.87364|7.41032
burolo|to|45.48116|7.93392
busano|to|45.329438|7.655732
bussoleno|to|45.139271|7.147717
buttigliera alta|to|45.068804|7.434488
cafasse|to|45.24596|7.517235
caluso|to|45.304955|7.895722
cambiano|to|44.971416|7.778698
campiglione fenile|to|44.804598|7.322313
candia canavese|to|45.327755|7.885121
candiolo|to|44.959244|7.601722
canischio|to|45.373987|7.596126
cantalupa|to|44.945941|7.330317
cantoira|to|45.343349|7.380124
caprie|to|45.118845|7.331914
caravino|to|45.398258|7.960322
carema|to|45.583302|7.810406
carignano|to|44.906943|7.673225
carmagnola|to|44.846947|7.717874
casalborgone|to|45.130451|7.940426
cascinette d'ivrea|to|45.480159|7.905619
caselette|to|45.104746|7.480817
caselle torinese|to|45.17749|7.646367
castagneto po|to|45.159251|7.889425
castagnole piemonte|to|44.898142|7.566123
castellamonte|to|45.381955|7.712117
castelnuovo nigra|to|45.438156|7.694615
castiglione torinese|to|45.11905|7.807124
cavagnolo|to|45.152137|8.051479
cavour|to|44.785499|7.373841
cercenasco|to|44.861291|7.50127
ceres|to|45.313377|7.389688
ceresole reale|to|45.431952|7.235706
cesana torinese|to|44.952236|6.790805
chialamberto|to|45.363669|7.341471
chianocco|to|45.148094|7.16933
chiaverano|to|45.49886|7.902619
chieri|to|45.013884|7.822354
chiesanuova|to|45.417886|7.655795
chiomonte|to|45.119542|6.985306
chiusa di san michele|to|45.103913|7.326972
chivasso|to|45.190959|7.887186
ciconio|to|45.329915|7.759492
cintano|to|45.428556|7.688115
cinzano|to|45.09425|7.925627
ciriè|to|45.235277|7.600308
claviere|to|44.938799|6.750294
coassolo torinese|to|45.297583|7.460458
coazze|to|45.052194|7.297902
collegno|to|45.077547|7.572426
colleretto castelnuovo|to|45.422356|7.679815
colleretto giacosa|to|45.431887|7.799591
condove|to|45.117419|7.309455
corio|to|45.313499|7.532087
cossano canavese|to|45.387758|7.991523
cuceglio|to|45.359435|7.815187
cumiana|to|44.979742|7.376717
cuorgnè|to|45.38972|7.649817
druento|to|45.134748|7.576618
exilles|to|45.097383|6.929065
favria|to|45.331154|7.690617
feletto|to|45.303853|7.744719
fenestrelle|to|45.036095|7.049562
fiano|to|45.216871|7.522685
fiorano canavese|to|45.467758|7.833718
foglizzo|to|45.271272|7.822265
forno canavese|to|45.345353|7.591015
frassinetto|to|45.437142|7.606743
front|to|45.280852|7.663618
frossasco|to|44.932944|7.350218
garzigliana|to|44.837239|7.37442
gassino torinese|to|45.13165|7.827624
germagnano|to|45.263872|7.469483
giaglione|to|45.140239|7.014756
giaveno|to|45.042005|7.351994
givoletto|to|45.162248|7.496416
gravere|to|45.125342|7.017507
groscavallo|to|45.369225|7.233967
grosso|to|45.257502|7.557907
grugliasco|to|45.068046|7.57762
ingria|to|45.466468|7.570917
inverso pinasca|to|44.94564|7.218414
isolabella|to|44.906727|7.910151
issiglio|to|45.446934|7.752122
ivrea|to|45.467379|7.874797
la cassa|to|45.180348|7.516316
la loggia|to|44.957744|7.667324
lanzo torinese|to|45.27335|7.477411
lauriano|to|45.158252|7.991627
leini|to|45.184582|7.713271
lemie|to|45.228199|7.292044
lessolo|to|45.478258|7.814717
levone|to|45.318202|7.606187
locana|to|45.417654|7.458814
lombardore|to|45.235452|7.74032
lombriasco|to|44.840941|7.635925
loranzè|to|45.441657|7.812718
luserna san giovanni|to|44.815437|7.251318
lusernetta|to|44.803137|7.246718
lusigliè|to|45.318401|7.766652
macello|to|44.851139|7.39802
maglione|to|45.346657|8.013324
marentino|to|45.058032|7.874623
massello|to|44.967557|7.026199
mathi|to|45.255172|7.541984
mattie|to|45.118143|7.115109
mazzè|to|45.300255|7.932723
meana di susa|to|45.122043|7.064708
mercenasco|to|45.357182|7.882359
mezzenile|to|45.295129|7.394459
mombello di torino|to|45.045749|7.920827
mompantero|to|45.142043|7.064609
monastero di lanzo|to|45.301873|7.439681
moncalieri|to|45.000461|7.684754
moncenisio|to|45.205285|6.9825
montaldo torinese|to|45.065487|7.849601
montalenghe|to|45.337455|7.83882
montalto dora|to|45.490759|7.862818
montanaro|to|45.233553|7.853323
monteu da po|to|45.1509|8.016847
moriondo torinese|to|45.037649|7.940028
nichelino|to|44.995518|7.646618
noasca|to|45.453972|7.314554
nole|to|45.243991|7.572752
nomaglio|to|45.53616|7.860317
none|to|44.933143|7.540121
novalesa|to|45.18964|7.015498
oglianico|to|45.342554|7.693717
orbassano|to|45.007263|7.536919
orio canavese|to|45.329255|7.861721
osasco|to|44.849368|7.343243
osasio|to|44.871442|7.608024
oulx|to|45.033052|6.83251
ozegna|to|45.34929|7.742524
palazzo canavese|to|45.459259|7.978921
pancalieri|to|44.833441|7.585924
parella|to|45.430657|7.793217
pavarolo|to|45.072466|7.834671
pavone canavese|to|45.437559|7.851715
pecetto torinese|to|45.016883|7.749949
perosa argentina|to|44.95744|7.190914
perosa canavese|to|45.397057|7.830819
perrero|to|44.918212|7.129109
pertusio|to|45.356788|7.638501
pessinetto|to|45.289093|7.404162
pianezza|to|45.105847|7.543418
pinasca|to|44.94264|7.229115
pinerolo|to|44.887309|7.331945
pino torinese|to|45.039547|7.777125
piobesi torinese|to|44.932943|7.610123
piossasco|to|44.990555|7.463704
piscina|to|44.919442|7.426212
piverone|to|45.446259|8.005722
poirino|to|44.920845|7.846928
pomaretto|to|44.955983|7.182779
pont canavese|to|45.420526|7.596855
porte|to|44.887293|7.267971
pragelato|to|44.981486|6.93916
prali|to|44.881073|7.052439
pralormo|to|44.859744|7.901431
pramollo|to|44.905039|7.206508
prarostino|to|44.865792|7.268223
prascorsano|to|45.367754|7.616915
pratiglione|to|45.352753|7.596015
quagliuzzo|to|45.426757|7.781617
quassolo|to|45.52286|7.832317
quincinetto|to|45.56296|7.807315
reano|to|45.052056|7.430403
ribordone|to|45.43204|7.50195
rivalba|to|45.11835|7.888125
rivalta di torino|to|45.034045|7.518419
riva presso chieri|to|44.984974|7.872319
rivara|to|45.332553|7.632916
rivarolo canavese|to|45.331216|7.717134
rivarossa|to|45.250965|7.717845
rivoli|to|45.069715|7.517676
robassomero|to|45.200049|7.568917
rocca canavese|to|45.308854|7.575863
roletto|to|44.924691|7.331324
romano canavese|to|45.388657|7.86652
ronco canavese|to|45.499737|7.546855
rondissone|to|45.246854|7.963125
rorà|to|44.792136|7.199017
roure|to|45.001413|7.129432
rosta|to|45.067945|7.465117
rubiana|to|45.137946|7.38483
rueglio|to|45.468258|7.754616
salassa|to|45.358526|7.689238
salbertrand|to|45.07204|6.883205
salerano canavese|to|45.458058|7.851118
salza di pinerolo|to|44.934635|7.034467
samone|to|45.450554|7.845243
san benigno canavese|to|45.227052|7.785021
san carlo canavese|to|45.247826|7.610843
san colombano belmonte|to|45.382254|7.621015
san didero|to|45.135444|7.213011
san francesco al campo|to|45.226467|7.657164
sangano|to|45.026144|7.448118
san germano chisone|to|44.89855|7.241175
san gillio|to|45.142748|7.535817
san giorgio canavese|to|45.334155|7.796819
san giorio di susa|to|45.127982|7.177985
san giusto canavese|to|45.315154|7.80912
san martino canavese|to|45.39435|7.816559
san maurizio canavese|to|45.216676|7.631241
san mauro torinese|to|45.104412|7.761996
san pietro val lemina|to|44.90654|7.310917
san ponso|to|45.350754|7.670316
san raffaele cimena|to|45.146651|7.849324
san sebastiano da po|to|45.167552|7.957226
san secondo di pinerolo|to|44.867222|7.298167
sant'ambrogio di torino|to|45.099445|7.362315
sant'antonino di susa|to|45.107044|7.273613
santena|to|44.949111|7.771811
sauze di cesana|to|44.940736|6.859007
sauze d'oulx|to|45.027027|6.858346
scalenghe|to|44.891741|7.492521
scarmagno|to|45.384456|7.841119
sciolze|to|45.09325|7.880426
sestriere|to|44.958453|6.878686
settimo rottaro|to|45.407758|7.993522
settimo torinese|to|45.137335|7.771015
settimo vittone|to|45.54976|7.832216
sparone|to|45.413854|7.543412
strambinello|to|45.422696|7.768436
strambino|to|45.381457|7.88572
susa|to|45.137212|7.053979
tavagnasco|to|45.543965|7.822041
torino|to|45.067755|7.682489
torrazza piemonte|to|45.215422|7.974503
torre canavese|to|45.392056|7.759818
torre pellice|to|44.820768|7.223715
trana|to|45.039044|7.421817
traversella|to|45.508458|7.749415
traves|to|45.270612|7.428858
trofarello|to|44.983646|7.745225
usseaux|to|45.048941|7.028508
usseglio|to|45.233197|7.217735
vaie|to|45.102344|7.289413
val della torre|to|45.155147|7.445015
valgioie|to|45.076144|7.340715
vallo torinese|to|45.224745|7.495566
valperga|to|45.370616|7.656916
valprato soana|to|45.521481|7.549355
varisella|to|45.209941|7.488606
vauda canavese|to|45.279952|7.620217
venaus|to|45.15491|7.01257
venaria reale|to|45.134811|7.626866
verolengo|to|45.190053|7.968226
verrua savoia|to|45.156707|8.092761
vestignè|to|45.385757|7.954822
vialfrè|to|45.381019|7.817828
vidracco|to|45.430657|7.757417
vigone|to|44.843067|7.494438
villafranca piemonte|to|44.778939|7.500424
villanova canavese|to|45.242575|7.554506
villarbasse|to|45.045145|7.468418
villar dora|to|45.114646|7.383515
villareggia|to|45.310056|7.976624
villar focchiardo|to|45.111389|7.224725
villar pellice|to|44.807903|7.15758
villar perosa|to|44.919056|7.250286
villastellone|to|44.918612|7.744347
vinovo|to|44.94839|7.633929
virle piemonte|to|44.864541|7.570123
vische|to|45.335589|7.944818
vistrorio|to|45.442344|7.768145
viù|to|45.23916|7.37584
volpiano|to|45.201451|7.778222
volvera|to|44.956464|7.511052
mappano|to|45.147801|7.706099
val di chy|to|45.461125|7.79283
valchiusa|to|45.497263|7.741327
alagna valsesia|vc|45.851211|7.937177
albano vercellese|vc|45.425307|8.380994
alice castello|vc|45.365158|8.072925
arborio|vc|45.494964|8.385329
asigliano vercellese|vc|45.261578|8.408501
balmuccia|vc|45.818849|8.138102
balocco|vc|45.455262|8.280128
bianzè|vc|45.308457|8.122527
boccioleto|vc|45.830189|8.112881
borgo d'ale|vc|45.349435|8.053102
borgosesia|vc|45.716668|8.276857
borgo vercelli|vc|45.357862|8.463033
buronzo|vc|45.480862|8.267128
campertogno|vc|45.798953|8.032153
carcoforo|vc|45.90875|8.049537
caresana|vc|45.221263|8.506924
caresanablot|vc|45.357613|8.388842
carisio|vc|45.40936|8.199027
casanova elvo|vc|45.400961|8.293529
san giacomo vercellese|vc|45.497859|8.326139
cervatto|vc|45.882966|8.162347
cigliano|vc|45.308356|8.021025
civiasco|vc|45.80819|8.294116
collobiano|vc|45.396683|8.348284
costanzana|vc|45.238158|8.368134
cravagliana|vc|45.848385|8.20269
crescentino|vc|45.190454|8.099929
crova|vc|45.330659|8.210828
desana|vc|45.268822|8.356728
fobello|vc|45.889425|8.15868
fontanetto po|vc|45.193464|8.191925
formigliana|vc|45.429262|8.291928
gattinara|vc|45.614242|8.371138
ghislarengo|vc|45.528823|8.386128
greggio|vc|45.450663|8.38303
guardabosone|vc|45.701868|8.249322
lamporo|vc|45.231855|8.101628
lenta|vc|45.557466|8.384128
lignana|vc|45.28553|8.346124
livorno ferraris|vc|45.281912|8.080548
lozzolo|vc|45.625643|8.321774
mollia|vc|45.816669|8.030315
moncrivello|vc|45.332357|7.995524
motta de' conti|vc|45.193343|8.520934
olcenengo|vc|45.36346|8.30983
oldenico|vc|45.402755|8.380327
palazzolo vercellese|vc|45.186255|8.232632
pertengo|vc|45.236394|8.417401
pezzana|vc|45.261959|8.482279
pila|vc|45.769944|8.08108
piode|vc|45.770751|8.051076
postua|vc|45.713604|8.23059
prarolo|vc|45.282293|8.478118
quarona|vc|45.757869|8.265721
quinto vercellese|vc|45.379661|8.361731
rassa|vc|45.768473|8.013068
rimella|vc|45.907567|8.182504
rive|vc|45.212906|8.417704
roasio|vc|45.608019|8.288012
ronsecco|vc|45.253126|8.27781
rossa|vc|45.833501|8.125529
rovasenda|vc|45.538765|8.317527
salasco|vc|45.325559|8.26423
sali vercellese|vc|45.309859|8.328931
saluggia|vc|45.237554|8.011026
san germano vercellese|vc|45.350031|8.248779
santhià|vc|45.366259|8.172627
scopa|vc|45.792678|8.113367
scopello|vc|45.772756|8.094058
serravalle sesia|vc|45.685568|8.310924
stroppiana|vc|45.230246|8.453732
tricerro|vc|45.235757|8.325333
trino|vc|45.19394|8.296987
tronzano vercellese|vc|45.341258|8.173027
valduggia|vc|45.728369|8.329923
varallo|vc|45.813731|8.259211
vercelli|vc|45.555383|8.346284
villarboit|vc|45.437362|8.337329
villata|vc|45.387762|8.432632
vocca|vc|45.832585|8.194602
alto sermenza|vc|45.872658|8.038157
cellio con breia|vc|45.760606|8.309276
agrate conturbia|no|45.675837|8.559812
ameno|no|45.788656|8.441311
armeno|no|45.822457|8.439199
arona|no|45.759837|8.560408
barengo|no|45.57545|8.51395
bellinzago novarese|no|45.568768|8.643233
biandrate|no|45.452664|8.462432
boca|no|45.678969|8.408726
bogogno|no|45.667273|8.534066
bolzano novarese|no|45.762786|8.445175
borgolavezzaro|no|45.319663|8.699539
borgomanero|no|45.698911|8.462367
borgo ticino|no|45.689268|8.60183
briga novarese|no|45.731871|8.456726
briona|no|45.542466|8.47973
caltignaga|no|45.516976|8.587782
cameri|no|45.499674|8.660451
carpignano sesia|no|45.534635|8.418519
casalbeltrame|no|45.438064|8.466532
casaleggio novara|no|45.488765|8.491931
casalino|no|45.102415|8.248872
casalvolone|no|45.400198|8.463421
castellazzo novarese|no|45.513366|8.486931
castelletto sopra ticino|no|45.712169|8.642977
cavaglietto|no|45.602168|8.50213
cavaglio d'agogna|no|45.613168|8.486129
cavallirio|no|45.663368|8.394526
cerano|no|45.410966|8.780039
colazza|no|45.792573|8.500326
comignago|no|45.714871|8.564029
cressa|no|45.647072|8.50815
cureggio|no|45.674869|8.459727
divignano|no|45.66237|8.601631
dormelletto|no|45.736272|8.566628
fara novarese|no|45.555159|8.458941
fontaneto d'agogna|no|45.643469|8.480828
galliate|no|45.478267|8.696903
garbagna novarese|no|45.388764|8.661237
gargallo|no|45.72877|8.425325
ghemme|no|45.598346|8.420904
gozzano|no|45.746457|8.436067
granozzo con monticello|no|45.371139|8.587767
grignasco|no|45.684099|8.337853
invorio|no|45.756972|8.488326
landiona|no|45.496357|8.42308
lesa|no|45.829644|8.564857
maggiora|no|45.689369|8.422326
mandello vitta|no|45.495665|8.459831
marano ticino|no|45.62987|8.631632
massino visconti|no|45.824206|8.540942
meina|no|45.788773|8.538827
mezzomerico|no|45.618687|8.605518
miasino|no|45.801972|8.429724
momo|no|45.574889|8.556139
nebbiuno|no|45.803573|8.527126
nibbiola|no|45.371483|8.654978
novara|no|45.58425|8.545997
oleggio|no|45.597231|8.637068
oleggio castello|no|45.748872|8.527127
orta san giulio|no|45.798081|8.406951
paruzzaro|no|45.750241|8.517599
pella|no|45.801625|8.384779
pettenasco|no|45.816693|8.406617
pisano|no|45.792298|8.508975
pogno|no|45.759039|8.383426
pombia|no|45.65083|8.632293
prato sesia|no|45.646849|8.372939
recetto|no|45.460364|8.435731
romagnano sesia|no|45.631067|8.388627
romentino|no|45.462553|8.720777
san maurizio d'opaglio|no|45.773453|8.390395
san nazzaro sesia|no|45.438273|8.424073
san pietro mosezzo|no|45.455881|8.54419
sillavengo|no|45.520665|8.44103
sizzano|no|45.577467|8.437329
soriso|no|45.741737|8.409703
sozzago|no|45.398565|8.720338
suno|no|45.633369|8.54283
terdobbiate|no|45.376564|8.695238
tornaco|no|45.357564|8.715939
trecate|no|45.431965|8.735293
vaprio d'agogna|no|45.603513|8.553431
varallo pombia|no|45.665904|8.632693
vespolate|no|45.350871|8.668921
vicolungo|no|45.477564|8.462731
vinzaglio|no|45.322961|8.519135
gattico-veruno|no|45.702046|8.52983
acceglio|cn|44.474627|6.990918
aisone|cn|44.313925|7.219626
alba|cn|44.700724|8.035779
albaretto della torre|cn|44.595911|8.064064
alto|cn|44.108946|8.003074
argentera|cn|44.374629|6.954209
arguello|cn|44.58274|8.11084
bagnasco|cn|44.301333|8.043144
bagnolo piemonte|cn|44.760336|7.31392
baldissero d'alba|cn|44.759519|7.914349
barbaresco|cn|44.725447|8.080696
barge|cn|44.727536|7.321021
barolo|cn|44.610841|7.942845
bastia mondovì|cn|44.441935|7.894438
battifollo|cn|44.319833|8.010943
beinette|cn|44.364835|7.646733
bellino|cn|44.582338|6.990826
belvedere langhe|cn|44.493036|7.974639
bene vagienna|cn|44.543336|7.828335
benevello|cn|44.629841|8.104139
bergolo|cn|44.54784|8.183142
bernezzo|cn|44.385329|7.436329
bonvicino|cn|44.503537|8.01754
borgomale|cn|44.620341|8.13224
borgo san dalmazzo|cn|44.328652|7.487456
bosia|cn|44.602359|8.147252
bossolasco|cn|44.529338|8.05094
boves|cn|44.329626|7.551894
bra|cn|44.697678|7.854271
briaglia|cn|44.395676|7.876004
briga alta|cn|44.14893|7.73051
brondello|cn|44.600734|7.405925
brossasco|cn|44.569837|7.361615
busca|cn|44.518132|7.474328
camerana|cn|44.424036|8.141444
canale|cn|44.798544|7.997334
canosio|cn|44.455827|7.082721
caprauna|cn|44.118902|7.950257
caraglio|cn|44.416989|7.43259
caramagna piemonte|cn|44.782241|7.739429
cardè|cn|44.744438|7.477624
carrù|cn|44.481035|7.874437
cartignano|cn|44.477654|7.287771
casalgrasso|cn|44.819641|7.624625
castagnito|cn|44.755443|8.031335
casteldelfino|cn|44.589638|7.07032
castelletto stura|cn|44.441789|7.638649
castelletto uzzone|cn|44.494338|8.186743
castellinaldo d'alba|cn|44.774951|8.029984
castellino tanaro|cn|44.427835|7.98124
castelmagno|cn|44.409323|7.207187
castelnuovo di ceva|cn|44.353635|8.128945
castiglione falletto|cn|44.623699|7.977453
castiglione tinella|cn|44.725344|8.190139
castino|cn|44.617841|8.182441
cavallerleone|cn|44.740139|7.663928
cavallermaggiore|cn|44.709839|7.686329
celle di macra|cn|44.482936|7.179706
centallo|cn|44.501533|7.58783
ceresole alba|cn|44.799742|7.82243
cerretto langhe|cn|44.574839|8.09794
cervasca|cn|44.380929|7.47123
cervere|cn|44.635438|7.791932
ceva|cn|44.387276|8.033048
cherasco|cn|44.652219|7.858015
chiusa di pesio|cn|44.32643|7.675436
cigliè|cn|44.436535|7.926539
cissone|cn|44.563139|8.030439
clavesana|cn|44.481939|7.912079
corneliano d'alba|cn|44.735937|7.959851
cortemilia|cn|44.579471|8.193302
cossano belbo|cn|44.668989|8.198579
costigliole saluzzo|cn|44.564534|7.485427
cravanzana|cn|44.57462|8.126185
crissolo|cn|44.699166|7.157719
cuneo|cn|44.45807|7.558137
demonte|cn|44.315264|7.297508
diano d'alba|cn|44.65031|8.027508
dogliani|cn|44.530782|7.947233
dronero|cn|44.465942|7.367256
elva|cn|44.540329|7.090319
entracque|cn|44.241425|7.399331
envie|cn|44.682235|7.371123
farigliano|cn|44.512031|7.914245
faule|cn|44.80624|7.585125
feisoglio|cn|44.543739|8.104941
fossano|cn|44.549349|7.725016
frabosa soprana|cn|44.287769|7.806567
frabosa sottana|cn|44.302165|7.798008
frassino|cn|44.571632|7.276023
gaiola|cn|44.335245|7.406784
gambasca|cn|44.628734|7.347323
garessio|cn|44.202931|8.019253
genola|cn|44.588936|7.66453
gorzegno|cn|44.512838|8.135142
gottasecca|cn|44.460137|8.167544
govone|cn|44.804445|8.094036
grinzane cavour|cn|44.65334|7.995036
guarene|cn|44.739486|8.034836
igliano|cn|44.442936|8.013341
isasca|cn|44.587633|7.381625
lagnasco|cn|44.624928|7.555576
la morra|cn|44.63848|7.932582
lequio berria|cn|44.60584|8.098139
lequio tanaro|cn|44.559937|7.882136
lesegno|cn|44.402234|7.97084
levice|cn|44.538239|8.155042
limone piemonte|cn|44.202726|7.576336
lisio|cn|44.307132|7.978742
macra|cn|44.500248|7.17926
magliano alfieri|cn|44.769244|8.070136
magliano alpi|cn|44.459634|7.805336
mango|cn|44.686443|8.150239
manta|cn|44.615872|7.48702
marene|cn|44.661972|7.729157
margarita|cn|44.403432|7.684934
marmora|cn|44.439247|7.123515
marsaglia|cn|44.453858|7.981246
martiniana po|cn|44.627234|7.362823
melle|cn|44.562048|7.32049
moiola|cn|44.321127|7.38973
mombarcaro|cn|44.467637|8.088242
mombasiglio|cn|44.366633|7.967941
monastero di vasco|cn|44.330752|7.823675
monasterolo casotto|cn|44.328232|7.943241
monasterolo di savigliano|cn|44.686338|7.619528
monchiero|cn|44.570538|7.920236
mondovì|cn|44.390028|7.820472
monesiglio|cn|44.465037|8.118542
monforte d'alba|cn|44.582438|7.969637
montà|cn|44.813644|7.957233
montaldo di mondovì|cn|44.321731|7.86714
montaldo roero|cn|44.768719|7.924151
montanera|cn|44.462233|7.665633
montelupo albese|cn|44.62144|8.048038
montemale di cuneo|cn|44.43743|7.375327
monterosso grana|cn|44.408228|7.322927
monteu roero|cn|44.780686|7.931189
montezemolo|cn|44.377035|8.141144
monticello d'alba|cn|44.718182|7.948204
moretta|cn|44.763839|7.537125
morozzo|cn|44.423932|7.713335
murazzano|cn|44.474649|8.021336
murello|cn|44.752139|7.600926
narzole|cn|44.595438|7.869835
neive|cn|44.72605|8.115186
neviglie|cn|44.691942|8.116838
niella belbo|cn|44.512738|8.079341
niella tanaro|cn|44.413112|7.922952
novello|cn|44.587706|7.927116
nucetto|cn|44.340334|8.060343
oncino|cn|44.676262|7.190376
ormea|cn|44.148871|7.912606
ostana|cn|44.692521|7.188312
paesana|cn|44.684564|7.273841
pagno|cn|44.611734|7.425725
pamparato|cn|44.277029|7.913714
paroldo|cn|44.432036|8.072442
perletto|cn|44.599341|8.213042
perlo|cn|44.32202|8.094944
peveragno|cn|44.330015|7.617468
pezzolo valle uzzone|cn|44.53874|8.193943
pianfei|cn|44.372031|7.711435
piasco|cn|44.568001|7.457775
pietraporzio|cn|44.343768|7.033462
piobesi d'alba|cn|44.735019|7.979668
piozzo|cn|44.515655|7.892257
pocapaglia|cn|44.715241|7.882933
polonghera|cn|44.80234|7.595725
pontechianale|cn|44.622646|7.02998
pradleves|cn|44.418346|7.280699
prazzo|cn|44.482898|7.055396
priero|cn|44.376035|8.093343
priocca|cn|44.787185|8.061645
priola|cn|44.245575|8.022422
prunetto|cn|44.488441|8.14375
racconigi|cn|44.769819|7.678887
revello|cn|44.654948|7.389914
rifreddo|cn|44.650734|7.346423
rittana|cn|44.351028|7.398429
roaschia|cn|44.270105|7.455976
roascio|cn|44.417138|8.022343
robilante|cn|44.294182|7.511178
roburent|cn|44.306331|7.89224
roccabruna|cn|44.47453|7.343726
rocca cigliè|cn|44.445235|7.950939
rocca de' baldi|cn|44.423633|7.761136
roccaforte mondovì|cn|44.31794|7.744742
roccasparvera|cn|44.341583|7.440791
roccavione|cn|44.314628|7.482432
rocchetta belbo|cn|44.636042|8.17534
roddi|cn|44.679641|7.975435
roddino|cn|44.573849|8.019119
rodello|cn|44.62854|8.056838
rossana|cn|44.543333|7.433826
ruffia|cn|44.706638|7.604027
sale delle langhe|cn|44.395235|8.080143
sale san giovanni|cn|44.399902|8.078891
saliceto|cn|44.413336|8.168644
salmour|cn|44.578037|7.791833
saluzzo|cn|44.644605|7.492601
sambuco|cn|44.336292|7.079338
sampeyre|cn|44.579684|7.188889
san benedetto belbo|cn|44.490137|8.057841
san damiano macra|cn|44.48873|7.258424
sanfrè|cn|44.749384|7.803374
sanfront|cn|44.647034|7.321022
san michele mondovì|cn|44.377031|7.911036
sant'albano stura|cn|44.508934|7.722333
santa vittoria d'alba|cn|44.698641|7.937334
santo stefano belbo|cn|44.70774|8.230731
santo stefano roero|cn|44.788443|7.940933
savigliano|cn|44.644037|7.655929
scagnello|cn|44.333333|7.986442
scarnafigi|cn|44.67881|7.56595
serralunga d'alba|cn|44.610686|8.000556
serravalle langhe|cn|44.559239|8.058639
sinio|cn|44.600339|8.019938
somano|cn|44.535338|8.007839
sommariva del bosco|cn|44.768741|7.78533
sommariva perno|cn|44.745642|7.900633
stroppo|cn|44.506858|7.125828
tarantasca|cn|44.493679|7.543677
torre bormida|cn|44.56274|8.154441
torre mondovì|cn|44.352832|7.89964
torre san giorgio|cn|44.735739|7.528128
torresina|cn|44.433639|8.036745
treiso|cn|44.689151|8.086845
trezzo tinella|cn|44.677049|8.107468
trinità|cn|44.508277|7.757897
valdieri|cn|44.277626|7.397631
valgrana|cn|44.411229|7.380128
valloriate|cn|44.338067|7.374371
venasca|cn|44.562163|7.396865
verduno|cn|44.66614|7.930735
vernante|cn|44.245027|7.533834
verzuolo|cn|44.602899|7.474941
vezza d'alba|cn|44.76364|7.996134
vicoforte|cn|44.374442|7.872552
vignolo|cn|44.362229|7.472731
villafalletto|cn|44.543734|7.540129
villanova mondovì|cn|44.346932|7.766338
villanova solaro|cn|44.729938|7.574426
villar san costanzo|cn|44.484731|7.382226
vinadio|cn|44.309025|7.176025
viola|cn|44.290744|7.966471
vottignasco|cn|44.564134|7.579129
agliano terme|at|44.791012|8.249988
albugnano|at|45.077966|7.971154
antignano|at|44.845059|8.13484
aramengo|at|45.101082|8.000528
asti|at|44.826013|8.202686
azzano d'asti|at|44.874588|8.267791
baldichieri d'asti|at|44.905948|8.091638
belveglio|at|44.830438|8.330191
berzano di san pietro|at|45.094813|7.953012
bruno|at|44.793248|8.440543
bubbio|at|44.663673|8.295134
buttigliera d'asti|at|45.022638|7.950799
calamandrana|at|44.737016|8.334154
calliano|at|45.007826|8.25694
calosso|at|44.739837|8.227501
camerano casasco|at|44.991013|8.091198
canelli|at|44.719278|8.287138
cantarana|at|44.900516|8.030217
capriglio|at|45.004136|8.007991
casorzo|at|45.02339|8.334503
cassinasco|at|44.689092|8.303238
castagnole delle lanze|at|44.75114|8.150245
castagnole monferrato|at|44.961596|8.306235
castel boglione|at|44.720396|8.380106
castell'alfero|at|44.982097|8.209929
castellero|at|44.924338|8.074544
castelletto molina|at|44.750955|8.433094
castello di annone|at|44.879039|8.31389
castelnuovo belbo|at|44.801534|8.412187
castelnuovo calcea|at|44.788106|8.2853
castelnuovo don bosco|at|45.040849|7.963128
castel rocchero|at|44.718346|8.415344
cellarengo|at|44.866689|7.944725
celle enomondo|at|44.857347|8.122392
cerreto d'asti|at|45.05215|8.035071
cerro tanaro|at|44.872416|8.360249
cessole|at|44.648944|8.24364
chiusano d'asti|at|44.985716|8.117159
cinaglio|at|44.975249|8.100033
cisterna d'asti|at|44.825007|8.004618
coazzolo|at|44.72837|8.145331
cocconato|at|45.087459|8.039221
corsione|at|45.000907|8.146056
cortandone|at|44.958967|8.057802
cortanze|at|45.015018|8.08938
cortazzone|at|44.979636|8.061662
cortiglione|at|44.822849|8.358889
cossombrato|at|44.99432|8.136651
costigliole d'asti|at|44.786418|8.182223
cunico|at|45.03995|8.096731
dusino san michele|at|44.925493|7.97219
ferrere|at|44.875446|7.994732
fontanile|at|44.753259|8.422351
frinco|at|45.004901|8.168806
grana|at|44.998351|8.299436
grazzano badoglio|at|45.039582|8.313553
incisa scapaccino|at|44.808456|8.373444
isola d'asti|at|44.829367|8.177452
loazzolo|at|44.669043|8.258442
maranzana|at|44.760563|8.478257
maretto|at|44.944711|8.034271
moasca|at|44.762719|8.277822
mombaldone|at|44.569556|8.330186
mombaruzzo|at|44.771349|8.448909
mombercelli|at|44.81771|8.294669
monale|at|44.93605|8.071859
monastero bormida|at|44.648543|8.326643
moncalvo|at|45.050222|8.26522
moncucco torinese|at|45.068107|7.932533
mongardino|at|44.849233|8.218475
montabone|at|44.698745|8.390644
montafia|at|44.989289|8.024872
montaldo scarampi|at|44.830345|8.259052
montechiaro d'asti|at|45.007816|8.112152
montegrosso d'asti|at|44.823943|8.239782
montemagno|at|44.983701|8.323579
moransengo|at|45.11483|8.025051
nizza monferrato|at|44.77331|8.353709
olmo gentile|at|44.58582|8.247201
passerano marmorito|at|45.05585|8.018729
penango|at|45.033201|8.250253
piea|at|45.025113|8.071827
pino d'asti|at|45.05745|7.986229
piovà massaia|at|45.054532|8.049276
portacomaro|at|44.957405|8.258145
quaranti|at|44.750947|8.449144
refrancore|at|44.936297|8.343661
revigliasco d'asti|at|44.85854|8.158222
roatto|at|44.951609|8.027015
robella|at|45.101652|8.10193
rocca d'arazzo|at|44.873212|8.285806
roccaverano|at|44.592176|8.271898
rocchetta palafea|at|44.706723|8.346003
rocchetta tanaro|at|44.858671|8.345706
san damiano d'asti|at|44.833445|8.063534
san giorgio scarampi|at|44.611329|8.242425
san martino alfieri|at|44.818627|8.109185
san marzano oliveto|at|44.754021|8.295757
san paolo solbrito|at|44.950565|7.970582
scurzolengo|at|44.965799|8.278499
serole|at|44.553887|8.259555
sessame|at|44.6695|8.337265
settime|at|44.962466|8.11397
soglio|at|44.996298|8.078514
tigliole|at|44.886952|8.075355
tonco|at|45.024451|8.190036
tonengo|at|45.117473|8.00196
vaglio serra|at|44.796406|8.339014
valfenera|at|44.901746|7.964331
vesime|at|44.636851|8.22817
viale|at|45.000745|8.05012
viarigi|at|44.980508|8.358455
vigliano d'asti|at|44.835433|8.231949
villafranca d'asti|at|44.91329|8.032501
villanova d'asti|at|44.941889|7.937502
villa san secondo|at|45.005075|8.135249
vinchio|at|44.807318|8.317386
montiglio monferrato|at|45.06447|8.098861
acqui terme|al|44.675145|8.467546
albera ligure|al|44.702351|9.066158
alessandria|al|44.834953|8.74503
alfiano natta|al|45.048552|8.208334
alice bel colle|al|44.726046|8.450745
altavilla monferrato|al|44.993552|8.375938
alzano scrivia|al|45.018057|8.880649
arquata scrivia|al|44.687845|8.885338
avolasca|al|44.802832|8.965144
balzola|al|45.183005|8.402652
basaluzzo|al|44.767905|8.704785
bassignana|al|45.001634|8.734624
belforte monferrato|al|44.625046|8.661251
bergamasco|al|44.827249|8.454743
berzano di tortona|al|44.876356|8.949685
bistagno|al|44.661518|8.369525
borghetto di borbera|al|44.730892|8.945331
borgoratto alessandrino|al|44.83625|8.538545
borgo san martino|al|45.090963|8.525829
bosco marengo|al|44.825551|8.677248
bosio|al|44.650033|8.792389
bozzole|al|45.070192|8.606488
brignano-frascata|al|44.810065|9.042552
cabella ligure|al|44.673751|9.09576
camagna monferrato|al|45.017653|8.429739
camino|al|45.162142|8.287403
cantalupo ligure|al|44.718152|9.044958
capriata d'orba|al|44.727849|8.69085
carbonara scrivia|al|44.8494|8.869654
carentino|al|44.828739|8.471299
carezzano|al|44.806893|8.900179
carpeneto|al|44.677202|8.606409
carrega ligure|al|44.619046|9.175637
carrosio|al|44.658248|8.831554
cartosio|al|44.591842|8.420131
casal cermelli|al|44.83495|8.624946
casaleggio boiro|al|44.633547|8.730452
casale monferrato|al|45.136266|8.449813
casalnoceto|al|44.913177|8.981156
casasco|al|44.828347|9.005811
cassine|al|44.749948|8.527846
cassinelle|al|44.601544|8.563449
castellania coppi|al|44.798268|8.930478
castellar guidobono|al|44.905726|8.948289
castellazzo bormida|al|44.84505|8.577245
castelletto d'erro|al|44.62559|8.394288
castelletto d'orba|al|44.684548|8.703951
castelletto merli|al|45.074454|8.240564
castelletto monferrato|al|44.981148|8.564545
castelnuovo bormida|al|44.742648|8.548547
castelnuovo scrivia|al|44.980249|8.880356
castelspina|al|44.806149|8.583146
cavatore|al|44.630344|8.454247
cella monte|al|45.074868|8.391185
cereseto|al|45.087654|8.319935
cerreto grue|al|44.842216|8.931119
cerrina monferrato|al|45.121354|8.213832
coniolo|al|45.146852|8.371519
conzano|al|45.019717|8.456369
costa vescovato|al|44.81643|8.926872
cremolino|al|44.635945|8.585249
denice|al|44.598842|8.333145
dernice|al|44.766872|9.05076
fabbrica curone|al|44.784954|9.147559
felizzano|al|44.899964|8.436212
fraconalto|al|44.591047|8.878456
francavilla bisio|al|44.734649|8.731251
frascaro|al|44.826449|8.532245
frassinello monferrato|al|45.031753|8.387638
frassineto po|al|45.133411|8.533943
fresonara|al|44.78295|8.686149
frugarolo|al|44.837151|8.681548
fubine monferrato|al|44.962752|8.42994
gabiano|al|45.156754|8.195231
gamalero|al|44.809049|8.541645
garbagna|al|44.780453|8.997856
gavi|al|44.687649|8.809353
giarole|al|45.060555|8.566541
gremiasco|al|44.796254|9.106858
grognardo|al|44.63055|8.492931
grondona|al|44.69645|8.965456
guazzora|al|45.0138|8.843573
isola sant'antonio|al|45.030501|8.848734
lerma|al|44.636047|8.715452
malvicino|al|44.559342|8.413147
masio|al|44.869649|8.407741
melazzo|al|44.643944|8.424946
merana|al|44.518798|8.298076
mirabello monferrato|al|45.035552|8.523249
molare|al|44.618645|8.60055
molino dei torti|al|45.023436|8.892237
mombello monferrato|al|45.132854|8.250333
momperone|al|44.838445|9.035317
moncestino|al|45.15476|8.161422
mongiardino ligure|al|44.640139|9.061854
monleale|al|44.887066|8.980251
montacuto|al|44.766453|9.104958
montaldeo|al|44.667147|8.730152
montaldo bormida|al|44.682647|8.587848
montecastello|al|44.949452|8.685741
montechiaro d'acqui|al|44.59494|8.37957
montegioco|al|44.841776|8.963008
montemarzino|al|44.848338|8.99361
morano sul po|al|45.164222|8.365692
morbello|al|44.590801|8.518387
mornese|al|44.639247|8.756153
morsasco|al|44.665346|8.550848
murisengo|al|45.083776|8.137233
novi ligure|al|44.76085|8.789551
occimiano|al|45.060255|8.50764
odalengo grande|al|45.10976|8.166903
odalengo piccolo|al|45.071652|8.206033
olivola|al|45.037388|8.367527
orsara bormida|al|44.690146|8.562748
ottiglio|al|45.054321|8.338375
ovada|al|44.638346|8.64525
oviglio|al|44.86125|8.487743
ozzano monferrato|al|45.108923|8.37023
paderna|al|44.820713|8.89163
pareto|al|44.516402|8.382161
parodi ligure|al|44.669748|8.758652
pasturana|al|44.75085|8.749251
pecetto di valenza|al|44.989755|8.670945
pietra marazzi|al|44.943799|8.67014
pomaro monferrato|al|45.062915|8.594743
pontecurone|al|44.960475|8.933609
pontestura|al|45.143284|8.333425
ponti|al|44.627476|8.36539
ponzano monferrato|al|45.084853|8.265534
ponzone|al|44.588185|8.458654
pozzol groppo|al|44.878055|9.029854
pozzolo formigaro|al|44.795154|8.784849
prasco|al|44.639245|8.551948
predosa|al|44.752637|8.658324
quargnento|al|44.944966|8.487386
quattordio|al|44.89635|8.404941
ricaldone|al|44.731935|8.467775
rivalta bormida|al|44.711375|8.550068
rivarone|al|44.976855|8.715646
roccaforte ligure|al|44.677486|9.028695
rocca grimalda|al|44.671447|8.64845
rocchetta ligure|al|44.706551|9.050458
rosignano monferrato|al|45.079878|8.398478
sala monferrato|al|45.075154|8.358736
sale|al|44.980656|8.809348
san cristoforo|al|44.692248|8.750152
san giorgio monferrato|al|45.107685|8.413055
san salvatore monferrato|al|44.994154|8.565942
san sebastiano curone|al|44.785816|9.064654
sant'agata fossili|al|44.784752|8.921154
sardigliano|al|44.752885|8.896325
sarezzano|al|44.867612|8.912663
serralunga di crea|al|45.100254|8.282034
serravalle scrivia|al|44.72558|8.8547
sezzadio|al|44.784849|8.572546
silvano d'orba|al|44.686915|8.67496
solero|al|44.918498|8.511902
solonghello|al|45.129054|8.283234
spigno monferrato|al|44.543141|8.333946
spineto scrivia|al|44.837353|8.873752
stazzano|al|44.727394|8.867728
strevi|al|44.698146|8.521847
tagliolo monferrato|al|44.638346|8.665851
tassarolo|al|44.728549|8.771552
terruggia|al|45.082255|8.443238
terzo|al|44.670445|8.421645
ticineto|al|45.098026|8.555658
tortona|al|44.89666|8.864128
treville|al|45.096854|8.359536
trisobbio|al|44.661646|8.586149
valenza|al|45.014025|8.645821
valmacca|al|45.100786|8.582789
vignale monferrato|al|45.011253|8.396138
vignole borbera|al|44.709948|8.891262
viguzzolo|al|44.905507|8.918616
villadeati|al|45.072252|8.167932
villalvernia|al|44.815352|8.857252
villamiroglio|al|45.134725|8.170291
villanova monferrato|al|45.182857|8.479237
villaromagnano|al|44.849653|8.887752
visone|al|44.661845|8.500747
volpedo|al|44.890375|8.9823
volpeglino|al|44.892495|8.96006
voltaggio|al|44.620354|8.842748
cassano spinola|al|44.766166|8.863175
alluvioni piovera|al|44.982464|8.752398
lu e cuccaro monferrato|al|44.996613|8.474265
ailoche|bi|45.698291|8.221213
andorno micca|bi|45.609264|8.05272
benna|bi|45.513662|8.126623
biella|bi|45.566954|8.086912
bioglio|bi|45.606808|8.129709
borriana|bi|45.506161|8.038621
brusnengo|bi|45.593865|8.251924
callabiana|bi|45.631365|8.09722
camandona|bi|45.644165|8.09972
camburzano|bi|45.543668|8.003131
candelo|bi|45.546208|8.112599
caprile|bi|45.689752|8.21626
casapinta|bi|45.616096|8.195713
castelletto cervo|bi|45.516218|8.226654
cavaglià|bi|45.407734|8.093429
cerrione|bi|45.468361|8.068123
coggiola|bi|45.688139|8.176264
cossato|bi|45.569912|8.179988
crevacuore|bi|45.686868|8.246722
curino|bi|45.627597|8.23728
donato|bi|45.528192|7.908557
dorzano|bi|45.42606|8.098524
gaglianico|bi|45.537862|8.075022
gifflenga|bi|45.493063|8.232226
graglia|bi|45.558562|7.978019
magnano|bi|45.462058|8.004502
massazza|bi|45.491362|8.164524
masserano|bi|45.594632|8.224249
mezzana mortigliengo|bi|45.626566|8.189522
miagliano|bi|45.613464|8.044719
mongrando|bi|45.527732|8.010583
mottalciata|bi|45.505063|8.206725
muzzano|bi|45.560762|7.988919
netro|bi|45.537929|7.945516
occhieppo inferiore|bi|45.55612|8.021519
occhieppo superiore|bi|45.565062|8.001319
pettinengo|bi|45.613064|8.104221
piatto|bi|45.589864|8.135322
piedicavallo|bi|45.689803|7.95479
pollone|bi|45.579763|8.005919
ponderano|bi|45.538462|8.055921
portula|bi|45.675283|8.179365
pralungo|bi|45.592148|8.037924
pray|bi|45.676196|8.208714
ronco biellese|bi|45.581354|8.098778
roppolo|bi|45.420559|8.069724
rosazza|bi|45.675765|7.977117
sagliano micca|bi|45.625764|8.043319
sala biellese|bi|45.50816|7.95842
salussola|bi|45.449324|8.109157
sandigliano|bi|45.518562|8.077522
sordevolo|bi|45.573638|7.974245
sostegno|bi|45.652854|8.269701
strona|bi|45.616565|8.169122
tavigliano|bi|45.623258|8.052881
ternengo|bi|45.589748|8.116028
tollegno|bi|45.593049|8.051676
torrazzo|bi|45.49838|7.953648
valdengo|bi|45.567322|8.136866
vallanzengo|bi|45.603265|8.150422
valle san nicolao|bi|45.606969|8.141182
veglio|bi|45.640565|8.11412
verrone|bi|45.503662|8.119023
vigliano biellese|bi|45.564475|8.104537
villa del bosco|bi|45.620566|8.279624
villanova biellese|bi|45.481562|8.194325
viverone|bi|45.425554|8.051215
zimone|bi|45.44926|8.035808
zubiena|bi|45.492752|7.997712
zumaglia|bi|45.593864|8.089421
lessona|bi|45.58727|8.194337
campiglia cervo|bi|45.662808|7.999942
quaregna cerreto|bi|45.575018|8.1614
valdilana|bi|45.666126|8.144302
antrona schieranco|vb|46.05688|8.09646
anzola d'ossola|vb|45.988976|8.344519
arizzano|vb|45.957377|8.582925
arola|vb|45.808356|8.357465
aurano|vb|45.999111|8.588311
baceno|vb|46.260882|8.320213
bannio anzino|vb|45.959966|8.155128
baveno|vb|45.908916|8.505017
bee|vb|45.963268|8.575854
belgirate|vb|45.838874|8.571427
beura-cardezza|vb|46.079578|8.299716
bognanco|vb|46.126378|8.201113
brovello-carpugnino|vb|45.851801|8.539622
calasca-castiglione|vb|45.99173|8.193377
cambiasca|vb|45.962377|8.544624
cannero riviera|vb|46.02125|8.681398
cannobio|vb|46.061808|8.697639
caprezzo|vb|45.981978|8.562824
casale corte cerro|vb|45.915843|8.413678
ceppo morelli|vb|45.971773|8.067713
cesara|vb|45.834472|8.366422
cossogno|vb|45.964077|8.509623
craveggia|vb|46.140377|8.489429
crevoladossola|vb|46.148584|8.297416
crodo|vb|46.223301|8.323936
domodossola|vb|46.115245|8.292046
druogno|vb|46.134537|8.436344
formazza|vb|46.374475|8.421044
germagno|vb|45.892281|8.388294
ghiffa|vb|45.959122|8.619823
gignese|vb|45.860769|8.509671
gravellona toce|vb|45.930175|8.431222
gurro|vb|46.085676|8.570615
intragna|vb|45.994151|8.572928
loreglia|vb|45.906581|8.37119
macugnaga|vb|45.969872|7.966011
madonna del sasso|vb|45.788014|8.346854
malesco|vb|46.126618|8.500919
masera|vb|46.139979|8.323415
massiola|vb|45.91243|8.320078
mergozzo|vb|45.96128|8.449525
miazzina|vb|45.976377|8.522223
montecrestese|vb|46.162833|8.329842
montescheno|vb|46.06792|8.225245
nonio|vb|45.845773|8.377522
oggebbio|vb|45.997679|8.650425
omegna|vb|45.879774|8.408022
ornavasso|vb|45.962083|8.393624
pallanzeno|vb|46.041976|8.258116
piedimulera|vb|46.024238|8.259263
pieve vergonte|vb|46.012976|8.260816
premeno|vb|45.978099|8.5961
premia|vb|46.268144|8339
premosello-chiovenda|vb|46.003956|8.32759
quarna sopra|vb|45.871379|8.374771
quarna sotto|vb|45.868538|8.364781
re|vb|46.129855|8.54415
san bernardino verbano|vb|45.995222|8.456146
santa maria maggiore|vb|46.134955|8.467278
stresa|vb|45.883728|8.539027
toceno|vb|46.144097|8.468442
trarego viggiona|vb|46.038692|8.646128
trasquera|vb|46.21338|8.212711
trontano|vb|46.122479|8.333216
valstrona|vb|45.922954|8.256255
vanzone con san carlo|vb|45.983239|8.110339
varzo|vb|46.20668|8.252412
verbania|vb|45.937663|8.52653
vignone|vb|45.959773|8.566617
villadossola|vb|46.066663|8.260717
villette|vb|46.132203|8.535317
vogogna|vb|46.008976|8.293017
borgomezzavalle|vb|46.047315|8.199445
valle cannobina|vb|46.103002|8.599741
allein|ao|45.807839|7.272512
antey-saint-andré|ao|45.806525|7.598199
aosta|ao|45.737088|7.319665
arnad|ao|45.644475|7.722183
arvier|ao|45.702451|7.166471
avise|ao|45.708589|7.139746
ayas|ao|45.862633|7.731892
aymavilles|ao|45.701736|7.247656
bard|ao|45.609651|7.745729
bionaz|ao|45.913204|7.497951
brissogne|ao|45.72852|7.406663
brusson|ao|45.759342|7.730447
challand-saint-anselme|ao|45.715462|7.763044
challand-saint-victor|ao|45.683852|7.735185
chambave|ao|45.710478|7.550656
chamois|ao|45.838158|7.619692
champdepraz|ao|45.685332|7.657765
champorcher|ao|45.623874|7.621017
charvensod|ao|45.720879|7.326089
châtillon|ao|45.748786|7.612529
cogne|ao|45.608742|7.356023
courmayeur|ao|45.787425|6.973062
donnas|ao|45.603763|7.769697
doues|ao|45.819391|7.306582
emarèse|ao|45.724202|7.690159
etroubles|ao|45.820582|7.230574
fénis|ao|45.736013|7.494371
fontainemore|ao|45.64826|7.860256
gaby|ao|45.710652|7.890131
gignod|ao|45.779194|7.295678
gressan|ao|45.719173|7.285097
gressoney-la-trinité|ao|45.829612|7.824127
gressoney-saint-jean|ao|45.779297|7.825032
hône|ao|45.613726|7.737831
introd|ao|45.690502|7.186625
issime|ao|45.686623|7.853814
issogne|ao|45.654378|7.684603
jovençan|ao|45.715086|7.276343
la magdeleine|ao|45.814054|7.621875
la salle|ao|45.745437|7.070565
la thuile|ao|45.716288|6.948612
lillianes|ao|45.632642|7.844605
montjovet|ao|45.712361|7.659041
morgex|ao|45.758022|7.035433
nus|ao|45.74124|7.469617
ollomont|ao|45.849655|7.310644
oyace|ao|45.850765|7.382866
perloz|ao|45.613845|7.808409
pollein|ao|45.727967|7.352221
pontboset|ao|45.607614|7.686379
pontey|ao|45.71744|7.583269
pont-saint-martin|ao|45.59944|7.799438
pré-saint-didier|ao|45.763942|6.985347
quart|ao|45.740958|7.414362
rhêmes-notre-dame|ao|45.568883|7.118171
rhêmes-saint-georges|ao|45.654901|7.155412
roisan|ao|45.784571|7.311283
saint-christophe|ao|45.74742|7.356168
saint-denis|ao|45.764714|7.571331
saint-marcel|ao|45.73456|7.446196
saint-nicolas|ao|45.716078|7.167982
saint-oyen|ao|45.824644|7.213175
saint-pierre|ao|45.709481|7.229142
saint-rhémy-en-bosses|ao|45.823744|7.182189
saint-vincent|ao|45.750386|7.647884
sarre|ao|45.716798|7.259154
torgnon|ao|45.82396|7.565728
valgrisenche|ao|45.631169|7.064741
valpelline|ao|45.82446|7.324153
valsavarenche|ao|45.562003|7.226291
valtournenche|ao|45.911551|7.619255
verrayes|ao|45.769282|7.523777
verrès|ao|45.668718|7.690758
villeneuve|ao|45.702417|7.207496
agra|va|46.035046|8.77172
albizzate|va|45.723943|8.803131
angera|va|45.772188|8.575122
arcisate|va|45.860392|8.859618
arsago seprio|va|45.688158|8.731452
azzate|va|45.780222|8.793769
azzio|va|45.885194|8.710637
barasso|va|45.840476|8.756831
bardello|va|45.834865|8.698036
bedero valcuvia|va|45.914271|8.794198
besano|va|45.889039|8.890388
besnate|va|45.700165|8.768315
besozzo|va|45.845797|8.666495
biandronno|va|45.821512|8.713238
bisuschio|va|45.874873|8.871723
bodio lomnago|va|45.791229|8.751004
brebbia|va|45.828539|8.650052
bregano|va|45.825077|8.688786
brenta|va|45.89318|8.685101
brezzo di bedero|va|45.977696|8.717908
brinzio|va|45.88911|8.790413
brissago-valtravaglia|va|45.948567|8.747233
brunello|va|45.765371|8.797365
brusimpiano|va|45.947756|8.889216
buguggiate|va|45.785997|8.80499
busto arsizio|va|45.611906|8.852052
cadegliano-viconago|va|45.9646|8.830903
cairate|va|45.690919|8.872461
cantello|va|45.819946|8.894712
caravate|va|45.877893|8.651989
cardano al campo|va|45.645717|8.772498
carnago|va|45.722849|8.835848
caronno pertusella|va|45.594935|9.050761
caronno varesino|va|45.738636|8.831226
casale litta|va|45.767332|8.740087
casalzuigno|va|45.911648|8.704052
casciago|va|45.832797|8.784327
casorate sempione|va|45.673376|8.740521
cassano magnago|va|45.675221|8.825584
cassano valcuvia|va|45.932378|8.768429
castellanza|va|45.612374|8.897038
castello cabiaglio|va|45.893405|8.75597
castelseprio|va|45.715977|8.862656
castelveccana|va|45.944923|8.659784
castiglione olona|va|45.755476|8.866883
castronno|va|45.74683|8.814162
cavaria con premezzo|va|45.692682|8.796529
cazzago brabbia|va|45.797686|8.73319
cislago|va|45.658776|8.971932
cittiglio|va|45.898911|8.660738
clivio|va|45.864206|8.929329
cocquio-trevisago|va|45.863436|8.695665
comabbio|va|45.77196|8.674392
comerio|va|45.839617|8.746441
cremenaga|va|45.988923|8.802184
crosio della valle|va|45.759974|8.770432
cuasso al monte|va|45.914773|8.87914
cugliate-fabiasco|va|45.943544|8.818079
cunardo|va|45.937476|8.801245
curiglia con monteviasco|va|46.059392|8.826491
cuveglio|va|45.911293|8.726136
cuvio|va|45.896177|8.734129
daverio|va|45.778403|8.771828
dumenza|va|46.019164|8.78712
duno|va|45.914064|8.737194
fagnano olona|va|45.668948|8.872771
ferno|va|45.614147|8.754548
ferrera di varese|va|45.934525|8.785824
gallarate|va|45.659895|8.793201
galliate lombardo|va|45.784582|8.770894
gavirate|va|45.842826|8.717671
gazzada schianno|va|45.781043|8.832404
gemonio|va|45.879217|8.675397
gerenzano|va|45.639516|8.998218
germignaga|va|45.993507|8.724159
golasecca|va|45.696899|8.65565
gorla maggiore|va|45.664137|8.890948
gorla minore|va|45.644331|8.897896
gornate olona|va|45.739598|8.859798
grantola|va|45.949179|8.774329
inarzo|va|45.785316|8.734475
induno olona|va|45.852448|8.839608
ispra|va|45.815457|8.610138
jerago con orago|va|45.708033|8.793964
lavena ponte tresa|va|45.959734|8.861647
laveno-mombello|va|45.9095|8.611986
leggiuno|va|45.875366|8.620454
lonate ceppino|va|45.704503|8.874286
lonate pozzolo|va|45.596772|8.754731
lozza|va|45.775675|8.857734
luino|va|45.998908|8.739907
luvinate|va|45.839307|8.769093
malgesso|va|45.828215|8.679464
malnate|va|45.800092|8.878168
marchirolo|va|45.95005|8.832261
marnate|va|45.629323|8.901095
marzio|va|45.938681|8.859197
masciago primo|va|45.917778|8.78113
mercallo|va|45.749866|8.669572
mesenzana|va|45.947817|8.757834
montegrino valtravaglia|va|45.973207|8.768819
monvalle|va|45.856932|8.63373
morazzone|va|45.764961|8.8315
mornago|va|45.747541|8.752299
oggiona con santo stefano|va|45.699435|8.818381
olgiate olona|va|45631|8.889841
origgio|va|45.597693|9.018061
orino|va|45.881634|8.715103
porto ceresio|va|45.903067|8.904946
porto valtravaglia|va|45.96137|8.681809
rancio valcuvia|va|45.916006|8.771028
ranco|va|45.797591|8.571135
saltrio|va|45.873908|8.921668
samarate|va|45.628182|8.784305
saronno|va|45.625676|9.037328
sesto calende|va|45.723955|8.634362
solbiate arno|va|45.719056|8.814344
solbiate olona|va|45.651379|8.887751
somma lombardo|va|45.683621|8.707087
sumirago|va|45.739225|8.781004
taino|va|45.764344|8.615061
ternate|va|45.782286|8.690318
tradate|va|45.714293|8.904146
travedona-monate|va|45.805066|8.674199
tronzano lago maggiore|va|46.088991|8.733941
uboldo|va|45.610905|9.003388
valganna|va|45.900037|8.820143
varano borghi|va|45.77463|8.703666
varese|va|45.839713|8.754158
vedano olona|va|45.776916|8.885596
venegono inferiore|va|45.738376|8.901107
venegono superiore|va|45.751383|8.90476
vergiate|va|45.724847|8.694023
viggiù|va|45.872238|8.905862
vizzola ticino|va|45.625599|8.696243
sangiano|va|45.875076|8.633327
maccagno con pino e veddasca|va|46.073685|8.776408
cadrezzate con osmate|va|45.789909|8.649289
albavilla|co|45.803117|9.188483
albese con cassano|co|45.797299|9.168256
albiolo|co|45.805577|8.939235
alserio|co|45.779103|9.199529
alzate brianza|co|45.769778|9.182041
anzano del parco|co|45.768478|9.195442
appiano gentile|co|45.734637|8.979101
argegno|co|45.943274|9.128165
arosio|co|45.716977|9.208043
asso|co|45.860669|9.269749
barni|co|45.911283|9.26514
bene lario|co|46.023388|9.171111
beregazzo con figliaro|co|45.771006|8.958279
binago|co|45.784412|8.922159
bizzarone|co|45.834278|8.942735
blessagno|co|45.959522|9.09749
blevio|co|45.839942|9.10247
bregnano|co|45.698661|9.060961
brenna|co|45.742978|9.187142
brienno|co|45.91144|9.131272
brunate|co|45.81947|9.09599
bulgarograsso|co|45.747076|9.006438
cabiate|co|45.674176|9.173743
cadorago|co|45.724609|9.038219
caglio|co|45.87165|9.238268
campione d'italia|co|45.968081|8.971033
cantù|co|45.739535|9.129103
canzo|co|45.850641|9.274802
capiago intimiano|co|45.770802|9.130032
carate urio|co|45.88052|9.110496
carbonate|co|45.683657|8.940016
carimate|co|45.702976|9.110641
carlazzo|co|46.050003|9.1545
carugo|co|45.709225|9.194574
caslino d'erba|co|45.837589|9.23087
casnate con bernate|co|45.760483|9.074278
cassina rizzardi|co|45.75055|9.0312
castelmarte|co|45.833229|9.232703
castelnuovo bozzente|co|45.764576|8.943536
cavargna|co|46.090086|9.111234
cerano d'intelvi|co|45.943282|9.088836
cermenate|co|45.70008|9.080088
cernobbio|co|45.841394|9.075905
cirimido|co|45.699775|9.012539
claino con osteno|co|46.009489|9.09009
colonno|co|45.957602|9.155571
como|co|45.939476|9.14941
corrido|co|46.045832|9.139771
cremia|co|46.087285|9.27148
cucciago|co|45.739876|9.095489
cusino|co|46.074584|9.152503
dizzasco|co|45.943782|9.099736
domaso|co|46.15099|9.328142
dongo|co|46.122936|9.279079
dosso del liro|co|46.163989|9.272936
erba|co|45.80958|9.231242
eupilio|co|45.818252|9.264711
faggeto lario|co|45.858079|9.170904
faloppio|co|45.808891|8.971173
fenegrò|co|45.701637|8.999679
figino serenza|co|45.70978|9.128939
fino mornasco|co|45.742876|9.047639
garzeno|co|46.134274|9.248345
gera lario|co|46.171099|9.371899
grandate|co|45.775177|9.057838
grandola ed uniti|co|46.050863|9.189892
griante|co|45.995284|9.235538
guanzate|co|45.723878|9.010799
inverigo|co|45.740484|9.223612
laglio|co|45.880967|9.138031
laino|co|45.984483|9.075435
lambrugo|co|45.759191|9.241845
lasnigo|co|45.882245|9.267118
lezzeno|co|45.945075|9.194112
limido comasco|co|45.689174|8.979838
lipomo|co|45.794503|9.121646
livo|co|46.168845|9.30451
locate varesino|co|45.688851|8.930679
lomazzo|co|45.69924|9.03436
longone al segrino|co|45.819244|9.250424
luisago|co|45.762877|9.035438
lurago d'erba|co|45.74864|9.213197
lurago marinone|co|45.705775|8.981338
lurate caccivio|co|45.764583|8.991472
magreglio|co|45.920747|9.262095
mariano comense|co|45.697149|9.178344
maslianico|co|45.842827|9.047196
menaggio|co|46.022565|9.241433
merone|co|45.78774|9.246188
moltrasio|co|45.85998|9.098438
monguzzo|co|45.782867|9.232579
montano lucino|co|45.790331|9.027521
montemezzo|co|46.178181|9.374485
montorfano|co|45.785458|9.146079
mozzate|co|45.675188|8.955762
musso|co|46.114256|9.276373
nesso|co|45.911422|9.157113
novedrate|co|45.698946|9.120498
olgiate comasco|co|45.785326|8.967774
oltrona di san mamette|co|45.756962|8.976296
orsenigo|co|45.777379|9.181741
peglio|co|46.160389|9.294736
pianello del lario|co|46.102769|9.276633
pigra|co|45.957563|9.127695
plesio|co|46.046392|9.230285
pognana lario|co|45.881934|9.164584
ponna|co|45.995654|9.110843
ponte lambro|co|45.82658|9.225441
porlezza|co|46.036072|9.118392
proserpio|co|45.82828|9.245542
pusiano|co|45.81388|9.281843
rezzago|co|45.866781|9.249741
rodero|co|45.823977|8.915234
ronago|co|45.832278|8.983636
rovellasca|co|45.669411|9.050151
rovello porro|co|45.654152|9.041161
sala comacina|co|45.964424|9.166346
san bartolomeo val cavargna|co|46.083486|9.149127
san fermo della battaglia|co|45.808678|9.047438
san nazzaro val cavargna|co|46.089386|9.127434
schignano|co|45.926245|9.103091
senna comasco|co|45.762977|9.11104
sorico|co|46.172005|9.379666
sormano|co|45.877782|9.248741
stazzona|co|46.138485|9.274682
tavernerio|co|45.802426|9.146436
torno|co|45.857591|9.116294
trezzone|co|46.171143|9.351494
turate|co|45.654802|9.002287
uggiate-trevano|co|45.823859|8.964901
valbrona|co|45.881055|9.301631
valmorea|co|45.817455|8.932232
val rezzo|co|46.070258|9.100887
valsolda|co|46.029095|9.043912
veleso|co|45.908382|9.182139
veniano|co|45.71558|8.983074
vercana|co|46.159489|9.335137
vertemate con minoprio|co|45.728215|9.078415
villa guardia|co|45.773784|9.025297
zelbio|co|45.904582|9.180539
san siro|co|45.4782|9.123964
gravedona ed uniti|co|46.157675|9.184376
bellagio|co|45.987255|9.2613
colverde|co|45.804718|9.001526
tremezzina|co|45.983524|9.191491
alta valle intelvi|co|45.980864|9.041875
centro valle intelvi|co|45.950461|9.063741
solbiate con cagno|co|45.800442|8.915405
albaredo per san marco|so|46.10349|9.590044
albosaggia|so|46.147232|9.85317
andalo valtellino|so|46.133822|9.473806
aprica|so|46.152697|10.151656
ardenno|so|46.164292|9.644944
bema|so|46.108201|9.564166
berbenno di valtellina|so|46.168878|9.742367
bianzone|so|46.188197|10.109354
bormio|so|46.469244|10.372216
buglio in monte|so|46.182788|9.674828
caiolo|so|46.149994|9.814548
campodolcino|so|46.405986|9.351942
caspoggio|so|46.263526|9.862576
castello dell'acqua|so|46.145995|10.013553
castione andevenno|so|46.172794|9.800247
cedrasco|so|46.149293|9.767047
cercino|so|46.158091|9.508141
chiavenna|so|46.319979|9.398122
chiesa in valmalenco|so|46.265097|9.848647
chiuro|so|46.170821|9.989355
cino|so|46.158149|9.486137
civo|so|46.154191|9.560642
colorina|so|46.153693|9.729346
cosio valtellino|so|46.133741|9.529154
dazio|so|46.16127|9.600839
delebio|so|46.13609|9.46064
dubino|so|46.153343|9.461512
faedo valtellino|so|46.152995|9.90605
forcola|so|46.158774|9.670848
fusine|so|46.149041|9.749098
gerola alta|so|46.059382|9.550802
gordona|so|46.292293|9.365335
grosio|so|46.29893|10.274516
grosotto|so|46.279862|10.258441
madesimo|so|46.436316|9.358401
lanzada|so|46.269297|9.869247
livigno|so|46.537801|10.136316
lovero|so|46.2314|10.228556
mantello|so|46.152491|9.488941
mazzo di valtellina|so|46.2586|10.256656
mello|so|46.155291|9.547242
mese|so|46.306294|9.382335
montagna in valtellina|so|46.178495|9.90295
morbegno|so|46.132419|9.569474
novate mezzola|so|46.221592|9.449839
pedesina|so|46.082574|9.550664
piantedo|so|46.13419|9.42934
piateda|so|46.159595|9.934951
piuro|so|46.330143|9.420908
poggiridenti|so|46.174795|9.92605
ponte in valtellina|so|46.174996|9.977851
postalesio|so|46.174094|9.775447
prata camportaccio|so|46.307194|9.395236
rasura|so|46.10059|9.552743
rogolo|so|46.135746|9.487317
samolaco|so|46.24616|9.395079
san giacomo filippo|so|46.337694|9.371235
sernio|so|46.224535|10.204097
sondalo|so|46.331392|10.324863
sondrio|so|46.323362|10.258414
spriana|so|46.220302|9.864005
talamona|so|46.138492|9.612944
tartano|so|46.105891|9.678646
teglio|so|46.172675|10.064126
tirano|so|46.215672|10.173178
torre di santa maria|so|46.233529|9.851604
tovo di sant'agata|so|46.244779|10.246453
traona|so|46.14962|9.525796
tresivio|so|46.175695|9.942551
valdidentro|so|46.489037|10.292304
valdisotto|so|46.425215|10.357418
valfurva|so|46.416517|10.512516
val masino|so|46.245866|9.637997
verceia|so|46.198692|9.454739
vervio|so|46.252835|10.240226
villa di chiavenna|so|46.324494|9.493954
villa di tirano|so|46.203898|10.133854
abbiategrasso|mi|45.398733|8.91623
albairate|mi|45.421126|8.938136
arconate|mi|45.540664|8.848162
arese|mi|45.54904|9.077895
arluno|mi|45.50507|8.941541
assago|mi|45.405397|9.131355
bareggio|mi|45.477726|8.995973
basiano|mi|45.573588|9.467854
basiglio|mi|45.349152|9.164589
bellinzago lombardo|mi|45.542075|9.445951
bernate ticino|mi|45.479342|8.818039
besate|mi|45.312965|8.969645
binasco|mi|45.33197|9.099987
boffalora sopra ticino|mi|45.467278|8.830608
bollate|mi|45.543835|9.117708
bresso|mi|45.53668|9.18773
bubbiano|mi|45.327266|9.014546
buccinasco|mi|45.41715|9.109788
buscate|mi|45.544916|8.812943
bussero|mi|45.536676|9.372003
busto garolfo|mi|45.545558|8.88327
calvignasco|mi|45.325966|9.027746
cambiago|mi|45.572544|9.426842
canegrate|mi|45.570538|8.926792
carpiano|mi|45.340572|9.272733
carugate|mi|45.549774|9.343749
casarile|mi|45.317888|9.104903
casorezzo|mi|45.52437|8.90154
cassano d'adda|mi|45.526333|9.521491
cassina de' pecchi|mi|45.519189|9.361976
cassinetta di lugagnano|mi|45.423576|8.908314
castano primo|mi|45.551594|8.776992
cernusco sul naviglio|mi|45.524558|9.330925
cerro al lambro|mi|45.329969|9.339053
cerro maggiore|mi|45.593012|8.951392
cesano boscone|mi|45.447112|9.094054
cesate|mi|45.596474|9.077308
cinisello balsamo|mi|45.556059|9.214317
cisliano|mi|45.444669|8.990243
cologno monzese|mi|45.528554|9.278224
colturano|mi|45.37977|9.337852
corbetta|mi|45.465937|8.92009
cormano|mi|45.544513|9.167621
cornaredo|mi|45.501685|9.025746
corsico|mi|45.430722|9.110233
cuggiono|mi|45.506239|8.815266
cusago|mi|45.446245|9.033528
cusano milanino|mi|45.552184|9.183685
dairago|mi|45.565972|8.863174
dresano|mi|45.37297|9.360642
gaggiano|mi|45.406461|9.034418
garbagnate milanese|mi|45.577145|9.080025
gessate|mi|45.554388|9.435192
gorgonzola|mi|45.531574|9.404651
grezzago|mi|45.591865|9.495203
gudo visconti|mi|45.372872|8.998828
inveruno|mi|45.51383|8.851954
inzago|mi|45.540475|9.482052
lacchiarella|mi|45.321848|9.137047
lainate|mi|45.57173|9.028461
legnano|mi|45.594698|8.91836
liscate|mi|45.481549|9.409012
locate di triulzi|mi|45.357932|9.224641
magenta|mi|45.465393|8.883843
magnago|mi|45.578731|8.801851
marcallo con casone|mi|45.48722|8.873276
masate|mi|45.567547|9.463821
mediglia|mi|45.39537|9.331652
melegnano|mi|45.358789|9.323976
melzo|mi|45.498585|9.424251
mesero|mi|45.501314|8.854202
milano|mi|45.4668|9.1905
morimondo|mi|45.352442|8.954811
motta visconti|mi|45.288179|8.992487
nerviano|mi|45.553647|8.970719
nosate|mi|45.550869|8.726535
novate milanese|mi|45.531939|9.140832
noviglio|mi|45.360159|9.050375
opera|mi|45.373069|9.211149
ossona|mi|45.506211|8.901266
ozzero|mi|45.365848|8.924514
paderno dugnano|mi|45.571903|9.168131
pantigliate|mi|45.439548|9.352631
parabiago|mi|45.558611|8.947888
paullo|mi|45.416787|9.398241
pero|mi|45.510236|9.089457
peschiera borromeo|mi|45.431795|9.31182
pessano con bornago|mi|45.548535|9.388324
pieve emanuele|mi|45.355913|9.202072
pioltello|mi|45.501022|9.326335
pogliano milanese|mi|45.537305|8.991417
pozzo d'adda|mi|45.575666|9.500377
pozzuolo martesana|mi|45.513975|9.456252
pregnana milanese|mi|45.515849|9.008012
rescaldina|mi|45.615974|8.948931
rho|mi|45.528532|9.04018
robecchetto con induno|mi|45.532481|8.764558
robecco sul naviglio|mi|45.436507|8.887279
rodano|mi|45.477658|9.353461
rosate|mi|45.351992|9.017478
rozzano|mi|45.383237|9.154131
san colombano al lambro|mi|45.181716|9.488578
san donato milanese|mi|45.41807|9.27165
san giorgio su legnano|mi|45.573771|8.913739
san giuliano milanese|mi|45.39427|9.291851
santo stefano ticino|mi|45.487263|8.918626
san vittore olona|mi|45.586091|8.942696
san zenone al lambro|mi|45.327269|9.355253
sedriano|mi|45.488333|8.968731
segrate|mi|45.490735|9.29482
senago|mi|45.576272|9.124548
sesto san giovanni|mi|45.535722|9.237655
settala|mi|45.454413|9.386766
settimo milanese|mi|45.480309|9.055356
solaro|mi|45.617526|9.081021
trezzano rosa|mi|45.582777|9.486652
trezzano sul naviglio|mi|45.417988|9.069116
trezzo sull'adda|mi|45.608718|9.519844
tribiano|mi|45.413371|9.378952
truccazzano|mi|45.483874|9.468553
turbigo|mi|45.52915|8.736666
vanzago|mi|45.526826|8.994955
vaprio d'adda|mi|45.576576|9.529174
vernate|mi|45.316147|9.060968
vignate|mi|45.496573|9.376651
vimodrone|mi|45.512832|9.284672
vittuone|mi|45.487969|8.952442
vizzolo predabissi|mi|45.35636|9.349762
zibido san giacomo|mi|45.36|9.106028
villa cortese|mi|45.566752|8.888903
vanzaghello|mi|45.579253|8.782372
baranzate|mi|45.527222|9.117601
vermezzo con zelo|mi|45.390781|8.982192
adrara san martino|bg|45.699945|9.948935
adrara san rocco|bg|45.713986|9.958705
albano sant'alessandro|bg|45.686482|9.770656
albino|bg|45.760814|9.79683
almè|bg|45.73701|9.614785
almenno san bartolomeo|bg|45.742681|9.582251
almenno san salvatore|bg|45.749956|9.596368
alzano lombardo|bg|45.733603|9.730494
ambivere|bg|45.71958|9.55015
antegnate|bg|45.486477|9.79146
arcene|bg|45.575577|9.614054
ardesio|bg|45.937512|9.931158
arzago d'adda|bg|45.481513|9.561678
averara|bg|45.98868|9.631398
aviatico|bg|45.800307|9.771656
azzano san paolo|bg|45.661039|9.67518
azzone|bg|45.978518|10.112647
bagnatica|bg|45.660881|9.781057
barbata|bg|45.474977|9.77736
bariano|bg|45.512646|9.704925
barzana|bg|45.733986|9.567264
bedulita|bg|45.791652|9.55155
berbenno|bg|45.814783|9.571049
bergamo|bg|45.756656|9.754219
berzo san fermo|bg|45.719384|9.903158
bianzano|bg|45.774503|9.920848
blello|bg|45.837784|9.571049
bolgare|bg|45.635081|9.812858
boltiere|bg|45.600878|9.578453
bonate sopra|bg|45.68338|9.559751
bonate sotto|bg|45.666784|9.561388
borgo di terzo|bg|45.721786|9.894848
bossico|bg|45.828388|10.045059
bottanuco|bg|45.639878|9.506451
bracca|bg|45.823886|9.707871
branzi|bg|46.005377|9.759974
brembate|bg|45.603928|9.555598
brembate di sopra|bg|45.716763|9.579416
brignano gera d'adda|bg|45.545519|9.648953
brumano|bg|45.855083|9.500547
brusaporto|bg|45.670846|9.761354
calcinate|bg|45.619721|9.798877
calcio|bg|45.50921|9.850979
calusco d'adda|bg|45.691379|9.472649
calvenzano|bg|45.494981|9.601286
camerata cornello|bg|45.898686|9.656349
canonica d'adda|bg|45.575677|9.541653
capizzone|bg|45.784482|9.56775
capriate san gervasio|bg|45.612578|9.528752
caprino bergamasco|bg|45.74528|9.481748
caravaggio|bg|45.49726|9.64374
carobbio degli angeli|bg|45.665182|9.829158
carona|bg|46.141084|10.090007
carvico|bg|45.703779|9.482249
casazza|bg|45.749184|9.907258
casirate d'adda|bg|45.494124|9.567808
casnigo|bg|45.815986|9.868556
cassiglio|bg|45.966787|9.612147
castelli calepio|bg|45.631966|9.896063
castel rozzone|bg|45.552277|9.619855
castione della presolana|bg|45.907455|10.035059
castro|bg|45.802987|10.06706
cavernago|bg|45.625097|9.76164
cazzano sant'andrea|bg|45.812733|9.884704
cenate sopra|bg|45.711865|9.823789
cenate sotto|bg|45.698864|9.825711
cene|bg|45.78367|9.82546
cerete|bg|45.854348|10.011019
chignolo d'isola|bg|45.667579|9.527651
chiuduno|bg|45.650819|9.848016
cisano bergamasco|bg|45.74248|9.470248
ciserano|bg|45.585281|9.60085
cividate al piano|bg|45.554879|9.83096
clusone|bg|45.887088|9.949656
colere|bg|45.974292|10.082757
cologno al serio|bg|45.579772|9.707725
colzate|bg|45.816027|9.855865
comun nuovo|bg|45.622911|9.663127
corna imagna|bg|45.830683|9.544848
cortenuova|bg|45.538978|9.787859
costa di mezzate|bg|45.661981|9.793957
costa valle imagna|bg|45.80501|9.502141
costa volpino|bg|45.829888|10.098161
covo|bg|45.500322|9.770107
credaro|bg|45.660582|9.93106
curno|bg|45.691514|9.610966
cusio|bg|45.990888|9.601746
dalmine|bg|45.649279|9.603953
dossena|bg|45.880286|9.696751
endine gaiano|bg|45.792486|9.969058
entratico|bg|45.705615|9.874626
fara gera d'adda|bg|45.555676|9.537853
fara olivana con sola|bg|45.49172|9.737362
filago|bg|45.637578|9.556352
fino del monte|bg|45.892689|9.994057
fiorano al serio|bg|45.799841|9.842294
fontanella|bg|45.469877|9.801961
fonteno|bg|45.757803|10.020207
foppolo|bg|46.04189|9.757449
foresto sparso|bg|45.690883|9.920059
fornovo san giovanni|bg|45.497925|9.676906
fuipiano valle imagna|bg|45.854284|9.528447
gandellino|bg|45.99155|9.946597
gandino|bg|45.811533|9.903118
gandosso|bg|45.658882|9.888859
gaverina terme|bg|45.755784|9.886657
gazzaniga|bg|45.796642|9.834955
ghisalba|bg|45.594193|9.753914
gorlago|bg|45.676703|9.827054
gorle|bg|45.703735|9.719022
gorno|bg|45.862895|9.843416
grassobbio|bg|45.65723|9.723107
gromo|bg|45.96399|9.927054
grone|bg|45.727504|9.911202
grumello del monte|bg|45.637181|9.876759
isola di fondra|bg|45.978491|9.746387
isso|bg|45.476787|9.758419
lallio|bg|45.66638|9.630053
leffe|bg|45.800686|9.885456
lenna|bg|45.943983|9.678696
levate|bg|45.625179|9.624154
locatello|bg|45.834502|9.53326
lovere|bg|45.816882|10.075214
lurano|bg|45.565677|9.640155
luzzana|bg|45.716393|9.880496
madone|bg|45.649957|9.547963
mapello|bg|45.708965|9.547513
martinengo|bg|45.570368|9.767376
mezzoldo|bg|46.012403|9.664874
misano di gera d'adda|bg|45.468775|9.621057
moio de' calvi|bg|45.952003|9.700212
monasterolo del castello|bg|45.763185|9.931658
montello|bg|45.672082|9.805657
morengo|bg|45.533604|9.705278
mornico al serio|bg|45.59138|9.809259
mozzanica|bg|45.476076|9.689258
mozzo|bg|45.69868|9.608652
nembro|bg|45.743974|9.759275
olmo al brembo|bg|45.969676|9.648649
oltre il colle|bg|45.889487|9.769252
oltressenda alta|bg|45.916699|9.944232
oneta|bg|45.871487|9.819354
onore|bg|45.891169|10.009936
orio al serio|bg|45.676227|9.692956
ornica|bg|45.988971|9.579479
osio sopra|bg|45.630787|9.591063
osio sotto|bg|45.616078|9.589053
pagazzano|bg|45.534277|9.671157
paladina|bg|45.730674|9.607994
palazzago|bg|45.752115|9.535027
palosco|bg|45.587549|9.835698
parre|bg|45.874587|9.890855
parzanica|bg|45.738026|10.034515
pedrengo|bg|45.695982|9.734955
peia|bg|45.799761|9.900355
pianico|bg|45.809887|10.04306
piario|bg|45.896488|9.927255
piazza brembana|bg|45.947786|9.675623
piazzatorre|bg|45.989717|9.683798
piazzolo|bg|45.979783|9.67197
pognano|bg|45.586378|9.639955
ponte nossa|bg|45.866062|9.882974
ponteranica|bg|45.732077|9.651987
ponte san pietro|bg|45.697819|9.587857
pontida|bg|45.732311|9.498642
pontirolo nuovo|bg|45.569175|9.569345
pradalunga|bg|45.746733|9.783129
predore|bg|45.680784|10.014361
premolo|bg|45.870187|9.874655
presezzo|bg|45.69208|9.570351
pumenengo|bg|45.479947|9.870128
ranica|bg|45.726338|9.714663
ranzanico|bg|45.789229|9.935725
riva di solto|bg|45.77357|10.039343
rogno|bg|45.857665|10.133545
romano di lombardia|bg|45.518376|9.755087
roncobello|bg|45.955688|9.75235
roncola|bg|45.768007|9.561298
rota d'imagna|bg|45.830854|9.512792
rovetta|bg|45.892989|9.983457
san giovanni bianco|bg|45.874075|9.653022
san paolo d'argon|bg|45.688557|9.804066
san pellegrino terme|bg|45.838631|9.664731
santa brigida|bg|45.984547|9.621791
sarnico|bg|45.670143|9.963513
scanzorosciate|bg|45.710782|9.735055
schilpario|bg|46.008793|10.157759
sedrina|bg|45.781783|9.624051
selvino|bg|45.781144|9.751675
seriate|bg|45.684371|9.717998
serina|bg|45.871941|9.729336
solto collina|bg|45.781784|10.025203
songavazzo|bg|45.879525|9.989516
sorisole|bg|45.744579|9.661195
sotto il monte giovanni xxiii|bg|45.705467|9.503737
sovere|bg|45.820724|10.023098
spinone al lago|bg|45.767485|9.924158
spirano|bg|45.582078|9.668856
stezzano|bg|45.65138|9.65351
strozza|bg|45.773082|9.57745
suisio|bg|45.655902|9.500154
taleggio|bg|45.894385|9.562672
tavernola bergamasca|bg|45.709385|10.046262
telgate|bg|45.628881|9.851659
terno d'isola|bg|45.685479|9.530951
torre boldone|bg|45.716272|9.708189
torre de' busi|bg|45.774566|9.480377
torre de' roveri|bg|45.700822|9.773514
torre pallavicina|bg|45.446377|9.877063
trescore balneario|bg|45.696483|9.842557
treviglio|bg|45.521724|9.593148
treviolo|bg|45.671543|9.611742
ubiale clanezzo|bg|45.780936|9.605822
urgnano|bg|45.597197|9.694856
valbondione|bg|46.034193|10.005855
valbrembo|bg|45.717833|9.607536
valgoglio|bg|45.97489|9.913554
valleve|bg|46.02819|9.743749
valnegra|bg|45.94941|9.690018
valtorta|bg|45.977787|9.534945
vedeseta|bg|45.89087|9.540057
verdellino|bg|45.603136|9.61561
verdello|bg|45.604778|9.628954
vertova|bg|45.810219|9.853507
viadanica|bg|45.684342|9.962284
vigano san martino|bg|45.726531|9.89399
vigolo|bg|45.715775|10.022717
villa d'adda|bg|45.71398|9.461649
villa d'almè|bg|45.749682|9.614451
villa di serio|bg|45.722665|9.732378
villa d'ogna|bg|45.904689|9.930855
villongo|bg|45.673042|9.933834
vilminore di scalve|bg|45.997675|10.09248
zandobbio|bg|45.687882|9.853358
zanica|bg|45.63946|9.684126
zogno|bg|45.793706|9.666203
costa serina|bg|45.83242|9.741937
algua|bg|45.826185|9.722152
cornalba|bg|45.851729|9.743234
medolago|bg|45.668579|9.49565
solza|bg|45.677143|9.488937
sant'omobono terme|bg|45.811687|9.535963
val brembilla|bg|45.824643|9.601566
acquafredda|bs|45.307378|10.414677
adro|bs|45.622402|9.961376
agnosine|bs|45.650676|10.353335
alfianello|bs|45.266975|10.148172
anfo|bs|45.76639|10.493771
angolo terme|bs|45.891036|10.146099
artogne|bs|45.848982|10.164556
azzano mella|bs|45.454722|10.116823
bagnolo mella|bs|45.429079|10.18437
bagolino|bs|45.823561|10.462731
barbariga|bs|45.405077|10.054367
barghe|bs|45.681887|10.41217
bassano bresciano|bs|45.327176|10.12837
bedizzole|bs|45.510571|10.423032
berlingo|bs|45.50298|10.043665
berzo demo|bs|46.093418|10.336266
berzo inferiore|bs|45.931693|10.280587
bienno|bs|45.935562|10.294791
bione|bs|45.672786|10.338869
borgo san giacomo|bs|45.346975|9.968467
borgosatollo|bs|45.477262|10.240811
borno|bs|45.94733|10.206404
botticino|bs|45.543262|10.323184
bovegno|bs|45.793307|10.273756
bovezzo|bs|45.593137|10.24348
brandico|bs|45.454178|10.052666
braone|bs|45.990034|10.34319
breno|bs|45.956324|10.30404
brescia|bs|45.77958|10.425873
brione|bs|45.642923|10.146156
caino|bs|45.612677|10.315085
calcinato|bs|45.457182|10.413574
calvagese della riviera|bs|45.54004|10.445833
calvisano|bs|45.348052|10.345921
capo di ponte|bs|46.030996|10.345862
capovalle|bs|45.754435|10.54469
capriano del colle|bs|45.454869|10.130348
capriolo|bs|45.637256|9.933605
carpenedolo|bs|45.364606|10.429385
castegnato|bs|45.560714|10.115309
castelcovati|bs|45.498413|9.943282
castel mella|bs|45.499592|10.145008
castenedolo|bs|45.471576|10.29831
casto|bs|45.69455|10.321243
castrezzato|bs|45.512505|9.977855
cazzago san martino|bs|45.580508|10.026215
cedegolo|bs|46.076646|10.350068
cellatica|bs|45.584106|10.180245
cerveno|bs|46.002509|10.325791
ceto|bs|46.002595|10.351963
cevo|bs|46.081397|10.368562
chiari|bs|45.536462|9.928846
cigole|bs|45.308636|10.189375
cimbergo|bs|46.024096|10.365663
cividate camuno|bs|45.942493|10.277462
coccaglio|bs|45.563448|9.978272
collebeato|bs|45.581315|10.210271
collio|bs|45.809312|10.332872
cologne|bs|45.58152|9.941172
comezzano-cizzago|bs|45.471278|9.950064
concesio|bs|45.605184|10.216967
corte franca|bs|45.637417|10.006468
corteno golgi|bs|46.166198|10.243457
corzano|bs|45.443778|10.007366
darfo boario terme|bs|45.883486|10.180707
dello|bs|45.41811|10.075203
desenzano del garda|bs|45.469485|10.538947
edolo|bs|46.179375|10.330757
erbusco|bs|45.5985|9.972216
esine|bs|45.925692|10.250862
fiesse|bs|45.231475|10.324177
flero|bs|45.482887|10.175008
gambara|bs|45.255246|10.293411
gardone riviera|bs|45.618785|10.559101
gardone val trompia|bs|45.687914|10.183724
gargnano|bs|45.688138|10.663501
gavardo|bs|45.584627|10.438237
ghedi|bs|45.401081|10.279276
gianico|bs|45.86453|10.183653
gottolengo|bs|45.291033|10.269829
gussago|bs|45.592664|10.15345
idro|bs|45.737745|10.478326
incudine|bs|46.2203|10.358759
irma|bs|45.771317|10.284168
iseo|bs|45.659555|10.051049
isorella|bs|45.309153|10.322164
lavenone|bs|45.739689|10.43907
leno|bs|45.367012|10.218452
limone sul garda|bs|45.814119|10.791579
lodrino|bs|45.719687|10.277667
lograto|bs|45.484238|10.053276
lonato del garda|bs|45.461039|10.484535
longhena|bs|45.437678|10.059667
losine|bs|45.983494|10.316163
lozio|bs|45.986103|10.26036
lumezzane|bs|45.648258|10.264546
maclodio|bs|45.476998|10.042715
magasa|bs|45.781392|10.615973
mairano|bs|45.449179|10.079567
malegno|bs|45.950602|10.273339
malonno|bs|46.122581|10.319212
manerba del garda|bs|45.556043|10.562593
manerbio|bs|45.355777|10.13517
marcheno|bs|45.709374|10.215326
marmentino|bs|45.75545|10.287315
marone|bs|45.736086|10.093762
mazzano|bs|45.518804|10.354162
milzano|bs|45.273575|10.199273
moniga del garda|bs|45.528523|10.537573
monno|bs|46.211571|10.339878
monte isola|bs|45.716536|10.080176
monticelli brusati|bs|45.63219|10.09857
montichiari|bs|45.415181|10.390675
montirone|bs|45.44638|10.228771
mura|bs|45.71398|10.34365
muscoline|bs|45.562485|10.461874
nave|bs|45.587727|10.289235
niardo|bs|45.975994|10.333263
nuvolento|bs|45.545784|10.386772
nuvolera|bs|45.532183|10.369172
odolo|bs|45.64685|10.388448
offlaga|bs|45.386133|10.117457
ome|bs|45.627283|10.121365
ono san pietro|bs|46.016895|10.328362
orzinuovi|bs|45.403976|9.921564
orzivecchi|bs|45.42066|9.961729
ospitaletto|bs|45.555222|10.073946
ossimo|bs|45.946192|10.230561
padenghe sul garda|bs|45.509584|10.506376
paderno franciacorta|bs|45.589187|10.076411
paisco loveno|bs|46.078305|10.290733
paitone|bs|45.553076|10.401255
palazzolo sull'oglio|bs|45.599069|9.884966
paratico|bs|45.659876|9.95507
paspardo|bs|46.030996|10.370963
passirano|bs|45.599487|10.064918
pavone del mella|bs|45.301256|10.208401
san paolo|bs|45.370576|10.026767
pertica alta|bs|45.744402|10.343936
pertica bassa|bs|45.754489|10.37467
pezzaze|bs|45.774127|10.237377
pian camuno|bs|45.843752|10.157887
pisogne|bs|45.805731|10.10819
polaveno|bs|45.661484|10.123964
polpenazze del garda|bs|45.551185|10.504875
pompiano|bs|45.431977|9.989265
poncarale|bs|45.460663|10.173909
ponte di legno|bs|46.258603|10.508662
pontevico|bs|45.274374|10.092471
pontoglio|bs|45.56938|9.85346
pozzolengo|bs|45.40597|10.631373
pralboino|bs|45.267999|10.217413
preseglie|bs|45.668287|10.39677
prevalle|bs|45.551482|10.421857
provaglio d'iseo|bs|45.635971|10.044167
provaglio val sabbia|bs|45.684864|10.436601
puegnago del garda|bs|45.565692|10.51147
quinzano d'oglio|bs|45.310875|10.008568
remedello|bs|45.279498|10.371743
rezzato|bs|45.514182|10.316671
roccafranca|bs|45.4643|9.914056
rodengo saiano|bs|45.597646|10.109468
roè volciano|bs|45.616764|10.49587
roncadelle|bs|45.527608|10.149701
rovato|bs|45.567038|9.998601
rudiano|bs|45.488578|9.887062
sabbio chiese|bs|45.658587|10.420571
sale marasino|bs|45.715453|10.111139
salò|bs|45.605678|10.520069
san felice del benaco|bs|45.589546|10.551733
san gervasio bresciano|bs|45.307376|10.148671
san zeno naviglio|bs|45.491572|10.217058
sarezzo|bs|45.66187|10.195876
saviore dell'adamello|bs|46.080497|10.399663
sellero|bs|46.055989|10.34399
seniga|bs|45.242674|10.177973
serle|bs|45.570284|10.366071
sirmione|bs|45.468849|10.607824
soiano del lago|bs|45.527784|10.512675
sonico|bs|46.165499|10.35286
sulzano|bs|45.690385|10.100963
tavernole sul mella|bs|45.746987|10.240565
temù|bs|46.249214|10.46805
tignale|bs|45.739781|10.721765
torbole casaglia|bs|45.50531|10.102464
toscolano-maderno|bs|45.63453|10.609521
travagliato|bs|45.523906|10.079783
tremosine sul garda|bs|45.771079|10.760773
trenzano|bs|45.477279|10.010665
treviso bresciano|bs|45.713126|10.463664
urago d'oglio|bs|45.515978|9.869661
vallio terme|bs|45.610193|10.390175
valvestino|bs|45.762092|10.594646
verolanuova|bs|45.328581|10.076086
verolavecchia|bs|45.327012|10.057256
vestone|bs|45.710641|10.403471
vezza d'oglio|bs|46.240213|10.399732
villa carcina|bs|45.632214|10.194442
villachiara|bs|45.355375|9.930966
villanuova sul clisi|bs|45.60168|10.453872
vione|bs|46.248402|10.447161
visano|bs|45.318378|10.372376
vobarno|bs|45.643643|10.499454
zone|bs|45.763445|10.115882
piancogno|bs|45.919692|10.225262
alagna|pv|45.169861|8.889846
albaredo arnaboldi|pv|45.115325|9.247959
albonese|pv|45.294438|8.706322
albuzzano|pv|45.186626|9.27504
arena po|pv|45.096606|9.362846
badia pavese|pv|45.120328|9.468797
bagnaria|pv|44.827262|9.122605
barbianello|pv|45.076662|9.205155
bascapè|pv|45.308268|9.313453
bastida pancarana|pv|45.086061|9.085152
battuda|pv|45.274665|9.078048
belgioioso|pv|45.158565|9.313756
bereguardo|pv|45.257378|9.027957
borgarello|pv|45.240665|9.14055
borgo priolo|pv|44.966449|9.148504
borgoratto mormorolo|pv|44.930658|9.193257
borgo san siro|pv|45.234969|8.913361
bornasco|pv|45.266566|9.217951
bosnasco|pv|45.064363|9.358477
brallo di pregola|pv|44.738186|9.28137
breme|pv|45.126922|8.623567
bressana bottarone|pv|45.083601|9.122253
broni|pv|45.062162|9.261191
calvignano|pv|44.982744|9.168163
campospinoso|pv|45.094424|9.244955
candia lomellina|pv|45.178758|8.59484
canneto pavese|pv|45.050462|9.278757
carbonara al ticino|pv|45.165422|9.061957
casanova lonati|pv|45.094464|9.213452
casatisma|pv|45.04906|9.131954
casei gerola|pv|45.006146|8.926989
casorate primo|pv|45.312366|9.016846
cassolnovo|pv|45.36556|8.810088
castana|pv|45.026761|9.272457
casteggio|pv|45.014114|9.123578
castelletto di branduzzo|pv|45.071839|9.090044
castello d'agogna|pv|45.234561|8.68714
castelnovetto|pv|45.25476|8.611838
cava manara|pv|45.140173|9.10686
cecima|pv|44.850969|9.080238
ceranova|pv|45.260266|9.243152
ceretto lomellina|pv|45.244961|8.67244
cergnago|pv|45.198201|8.772843
certosa di pavia|pv|45.257015|9.148114
cervesina|pv|45.061259|9.016151
chignolo po|pv|45.148166|9.481559
cigognola|pv|45.032761|9.244856
cilavegna|pv|45.309752|8.743348
codevilla|pv|44.963288|9.057385
confienza|pv|45.332162|8.556336
copiano|pv|45.196266|9.322455
corana|pv|45.061659|8.96835
corvino san quirico|pv|45.010186|9.162397
costa de' nobili|pv|45.132054|9.378904
cozzo|pv|45.192659|8.61104
cura carpignano|pv|45.212072|9.255463
dorno|pv|45.155085|8.951882
ferrera erbognone|pv|45.115059|8.864347
filighera|pv|45.177065|9.315955
fortunago|pv|44.921176|9.183875
frascarolo|pv|45.046656|8.682044
galliavola|pv|45.097559|8.818846
gambarana|pv|45.028959|8.762538
gambolò|pv|45.25903|8.85906
garlasco|pv|45.197362|8.923746
gerenzago|pv|45.206566|9.359656
giussago|pv|45.284766|9.140749
godiasco salice terme|pv|44.890183|9.065673
golferenzo|pv|44.962021|9.306952
gravellona lomellina|pv|45.327664|8.76414
gropello cairoli|pv|45.178609|8.994031
inverno e monteleone|pv|45.197866|9.385256
landriano|pv|45.311968|9.260052
langosco|pv|45.214367|8.56373
lardirago|pv|45.235341|9.233213
linarolo|pv|45.160981|9.27091
lirio|pv|44.993982|9.255568
lomello|pv|45.121059|8.796245
lungavilla|pv|45.04066|9.081353
magherno|pv|45.223566|9.329355
marcignago|pv|45.253065|9.079349
marzano|pv|45.247967|9.295054
mede|pv|45.097858|8.735944
menconico|pv|44.796364|9.27928
mezzana bigli|pv|45.059858|8.848447
mezzana rabattone|pv|45.094863|9.031254
mezzanino|pv|45.124654|9.205338
miradolo terme|pv|45.171966|9.445558
montalto pavese|pv|44.979273|9.209761
montebello della battaglia|pv|45.000959|9.103454
montecalvo versiggia|pv|44.971268|9.285541
montescano|pv|45.032644|9.296167
montesegale|pv|44.906251|9.127073
monticelli pavese|pv|45.110731|9.515049
montù beccaria|pv|45.036946|9.315538
mornico losana|pv|45.010331|9.205113
mortara|pv|45.247922|8.736385
nicorvo|pv|45.285662|8.667939
olevano di lomellina|pv|45.21296|8.717269
oliva gessi|pv|45.00356|9.179355
ottobiano|pv|45.152816|8.829209
palestro|pv|45.301761|8.533436
pancarana|pv|45.074928|9.05079
parona|pv|45.282602|8.750845
pavia|pv|45.036855|9.137825
pietra de' giorgi|pv|45.020704|9.230576
pieve albignola|pv|45.113111|8.9588
pieve del cairo|pv|45.049957|8.804146
pieve porto morone|pv|45.109364|9.435759
pinarolo po|pv|45.069861|9.167254
pizzale|pv|45.036505|9.048212
ponte nizza|pv|44.851548|9.097243
portalbera|pv|45.097563|9.318857
rea|pv|45.113365|9.154658
redavalle|pv|45.037446|9.203918
retorbido|pv|44.9493|9.036174
rivanazzano terme|pv|44.930767|9.014159
robbio|pv|45.289971|8.593278
robecco pavese|pv|45.048364|9.15016
rocca de' giorgi|pv|44.951562|9.253644
rocca susella|pv|44.919153|9.094131
rognano|pv|45.288466|9.090248
romagnese|pv|44.83948|9.330068
roncaro|pv|45.228166|9.275453
rosasco|pv|45.25106|8.578938
rovescala|pv|45.011421|9.348753
san cipriano po|pv|45.108679|9.282905
san damiano al colle|pv|45.024862|9.346559
san genesio ed uniti|pv|45.234765|9.178551
san giorgio di lomellina|pv|45.174413|8.790098
san martino siccomario|pv|45.160963|9.135952
sannazzaro de' burgondi|pv|45.102259|8.906348
santa cristina e bissone|pv|45.154392|9.418996
santa giuletta|pv|45.033836|9.181503
sant'alessio con vialone|pv|45.224687|9.229855
santa margherita di staffora|pv|44.76484|9.254617
santa maria della versa|pv|44.986177|9.300198
sant'angelo lomellina|pv|45.247061|8.644439
san zenone al po|pv|45.108564|9.361758
sartirana lomellina|pv|45.113845|8.664244
scaldasole|pv|45.124984|8.909653
semiana|pv|45.137475|8.729267
silvano pietra|pv|45.04037|8.947606
siziano|pv|45.314367|9.20315
sommo|pv|45.13065|9.083384
spessa|pv|45.112728|9.34943
stradella|pv|45.077356|9.295017
suardi|pv|45.034656|8.742945
torrazza coste|pv|44.975948|9.087823
torre beretti e castellaro|pv|45.075614|8.690528
torre d'arese|pv|45.242767|9.317354
torre de' negri|pv|45.149665|9.334556
torre d'isola|pv|45.217464|9.076149
torrevecchia pia|pv|45.283242|9.29665
torricella verzate|pv|45.017926|9.175582
travacò siccomario|pv|45.149067|9.159647
trivolzio|pv|45.258464|9.042748
tromello|pv|45.20913|8.871731
trovo|pv|45.282965|9.035447
val di nizza|pv|44.870977|9.175823
valeggio|pv|45.15066|8.859946
valle lomellina|pv|45.153058|8.668542
valle salimbene|pv|45.171612|9.234493
varzi|pv|44.82354|9.196904
velezzo lomellina|pv|45.163359|8.737243
vellezzo bellini|pv|45.270665|9.099249
verretto|pv|45.039234|9.108476
verrua po|pv|45.109649|9.17556
vidigulfo|pv|45.292267|9.236651
vigevano|pv|45.317094|8.858603
villa biscossi|pv|45.090658|8.787145
villanova d'ardenghi|pv|45.171662|9.042349
villanterio|pv|45.219467|9.364356
vistarino|pv|45.210766|9.308254
voghera|pv|44.99284|9.008593
volpara|pv|44.953036|9.297097
zavattarello|pv|44.868476|9.26523
zeccone|pv|45.257869|9.201144
zeme|pv|45.197302|8.66592
zenevredo|pv|45.054262|9.325658
zerbo|pv|45.110464|9.396058
zerbolò|pv|45.207166|9.012955
zinasco|pv|45.112799|9.003176
cornale e bastida|pv|45.045405|8.912614
corteolona e genzone|pv|45.166777|9.364813
colli verdi|pv|44.916917|9.24869
acquanegra cremonese|cr|45.168436|9.890147
agnadello|cr|45.444374|9.557056
annicco|cr|45.244572|9.879466
azzanello|cr|45.312869|9.921597
bagnolo cremasco|cr|45.361172|9.612958
bonemerse|cr|45.11367|10.077473
bordolano|cr|45.293985|9.98671
calvatone|cr|45.128574|10.441281
camisano|cr|45.443275|9.74576
campagnola cremasca|cr|45.398474|9.669459
capergnanica|cr|45.337572|9.64336
cappella cantone|cr|45.246858|9.838366
cappella de' picenardi|cr|45.158573|10.229976
capralba|cr|45.444075|9.643258
casalbuttano ed uniti|cr|45.245645|9.973919
casale cremasco-vidolasco|cr|45.436968|9.720895
casaletto ceredano|cr|45.318071|9.617559
casaletto di sopra|cr|45.419175|9.782561
casaletto vaprio|cr|45.408174|9.628758
casalmaggiore|cr|44.986386|10.41535
casalmorano|cr|45.286373|9.899666
casteldidone|cr|45.069472|10.406281
castel gabbiano|cr|45.468276|9.716459
castelleone|cr|45.293813|9.764302
castelverde|cr|45.188771|9.99557
castelvisconti|cr|45.305374|9.941067
cella dati|cr|45.095271|10.221477
chieve|cr|45.340472|9.616459
cicognolo|cr|45.164573|10.194575
cingia de' botti|cr|45.085071|10.275478
corte de' cortesi con cignone|cr|45.267444|10.003095
corte de' frati|cr|45.220029|10.096684
credera rubbiano|cr|45.296063|9.650529
crema|cr|45.362828|9.687457
cremona|cr|45.220864|10.037038
cremosano|cr|45.394917|9.639553
crotta d'adda|cr|45.158491|9.854158
cumignano sul naviglio|cr|45.355503|9.835562
derovere|cr|45.109972|10.247977
dovera|cr|45.365153|9.541688
fiesco|cr|45.336361|9.77761
formigara|cr|45.223221|9.771767
gabbioneta-binanuova|cr|45.218437|10.207747
gadesco-pieve delmona|cr|45.167703|10.114948
genivolta|cr|45.332574|9.877265
gerre de' caprioli|cr|45.090209|10.051407
gombito|cr|45.261556|9728
grontardo|cr|45.201873|10.150773
grumello cremonese ed uniti|cr|45.20073|9.866095
gussola|cr|45.011263|10.350137
isola dovarese|cr|45.176269|10.312957
izano|cr|45.355385|9.750043
madignano|cr|45.3443|9.726703
malagnino|cr|45.134771|10.114774
martignana di po|cr|45.011112|10.379751
monte cremasco|cr|45.373482|9.572766
montodine|cr|45.285171|9.707262
moscazzano|cr|45.292271|9.683461
motta baluffi|cr|45.055097|10.259247
offanengo|cr|45.378874|9.742361
olmeneta|cr|45.235873|10.02227
ostiano|cr|45.220775|10.252075
paderno ponchielli|cr|45.238872|9.930268
palazzo pignano|cr|45.390073|9.569557
pandino|cr|45.407073|9.553456
persico dosimo|cr|45.181365|10.076639
pescarolo ed uniti|cr|45.195148|10.187717
pessina cremonese|cr|45.185474|10.248676
pianengo|cr|45.402174|9.694759
pieranica|cr|45.425794|9.610376
pieve d'olmi|cr|45.08957|10.123675
pieve san giacomo|cr|45.130272|10.186275
pizzighettone|cr|45.18907|9.789865
pozzaglio ed uniti|cr|45.211016|10.0462
quintano|cr|45.420474|9.618557
ricengo|cr|45.405674|9.72396
ripalta arpina|cr|45.301872|9.728962
ripalta cremasca|cr|45.32856|9.679244
ripalta guerina|cr|45.306008|9.70514
rivarolo del re ed uniti|cr|45.027972|10.469783
rivolta d'adda|cr|45.470659|9.512551
robecco d'oglio|cr|45.259374|10.076571
romanengo|cr|45.377177|9.784985
salvirola|cr|45.355543|9.780243
san bassano|cr|45.243546|9.807876
san daniele po|cr|45.06377|10.182276
san giovanni in croce|cr|45.078872|10.37518
san martino del lago|cr|45.073248|10.31686
scandolara ravara|cr|45.05312|10.3017
scandolara ripa d'oglio|cr|45.221074|10.159773
sergnano|cr|45.428875|9.701659
sesto ed uniti|cr|45.186436|9.935128
solarolo rainerio|cr|45.081572|10.35628
soncino|cr|45.398975|9.874424
soresina|cr|45.286473|9.856965
sospiro|cr|45.108571|10.157675
spinadesco|cr|45.148751|9.929672
spineda|cr|45.060301|10.513381
spino d'adda|cr|45.398872|9.492255
stagno lombardo|cr|45.073157|10.089205
ticengo|cr|45.369274|9.827663
torlino vimercati|cr|45.417962|9.594702
tornata|cr|45.104373|10.430781
torre de' picenardi|cr|45.143573|10.286877
torricella del pizzo|cr|45.019602|10.292653
trescore cremasco|cr|45.401573|9.624958
trigolo|cr|45.329394|9.816401
vaiano cremasco|cr|45.371972|9.587158
vailate|cr|45.46331|9.602816
vescovato|cr|45.175073|10.164574
volongo|cr|45.212247|10.301874
voltido|cr|45.111973|10.332979
piadena drizzona|cr|45.135779|10.358068
acquanegra sul chiese|mn|45.162475|10.43468
asola|mn|45.22112|10.413065
bagnolo san vito|mn|45.092253|10.880429
bozzolo|mn|45.102474|10.483982
canneto sull'oglio|mn|45.149405|10.379736
casalmoro|mn|45.259577|10.404478
casaloldo|mn|45.254777|10.474379
casalromano|mn|45.200675|10.369378
castelbelforte|mn|45.214712|10.892761
castel d'ario|mn|45.189008|10.97513
castel goffredo|mn|45.295278|10.472982
castellucchio|mn|45.147276|10.648485
castiglione delle stiviere|mn|45.389677|10.488874
cavriana|mn|45.347559|10.598522
ceresara|mn|45.26513|10.567035
commessaggio|mn|45.034443|10.545345
curtatone|mn|45.109553|10.715842
dosolo|mn|44.953372|10.637588
gazoldo degli ippoliti|mn|45.200277|10.579383
gazzuolo|mn|45.069175|10.583113
goito|mn|45.254507|10.672941
gonzaga|mn|44.952488|10.822623
guidizzolo|mn|45.32068|10.580481
magnacavallo|mn|45.005578|11.1811
mantova|mn|45.169362|10.670616
marcaria|mn|45.120575|10.532783
mariana mantovana|mn|45.192876|10.486881
marmirolo|mn|45.220563|10.756393
medole|mn|45.324254|10.514405
moglia|mn|44.937174|10.913395
monzambano|mn|45.386139|10.692404
motteggiana|mn|45.03494|10.764023
ostiglia|mn|45.07039|11.13641
pegognaga|mn|44.995988|10.858772
piubega|mn|45.227309|10.534508
poggio rusco|mn|44.975558|11.114649
pomponesco|mn|44.928756|10.591879
ponti sul mincio|mn|45.412515|10.687221
porto mantovano|mn|45.192526|10.755221
quingentole|mn|45.039077|11.045796
quistello|mn|45.006579|10.98046
redondesco|mn|45.166376|10.512282
rivarolo mantovano|mn|45.070309|10.432104
rodigo|mn|45.199577|10.624984
roncoferraro|mn|45.135929|10.951354
roverbella|mn|45.26578|10.767527
sabbioneta|mn|44.998436|10.488587
san benedetto po|mn|45.041924|10.928295
san giacomo delle segnate|mn|44.972575|11.033097
san giorgio bigarello|mn|45.18053|10.871789
san giovanni del dosso|mn|44.966893|11.081617
san martino dall'argine|mn|45.099056|10.518348
schivenoglia|mn|44.995094|11.072981
sermide e felonica|mn|44.97833|11.29043
serravalle a po|mn|45.072885|11.100535
solferino|mn|45.368581|10.566679
sustinente|mn|45.069272|11.019604
suzzara|mn|44.991873|10.74309
viadana|mn|44.926786|10.520223
villimpenta|mn|45.144747|11.030232
volta mantovana|mn|45.321302|10.659224
borgo virgilio|mn|45.088107|10.785801
borgo mantovano|mn|45.026484|11.121971
borgocarbonara|mn|45.034383|11.214786
abbadia lariana|lc|45.900045|9.334056
airuno|lc|45.75388|9.424247
annone di brianza|lc|45.807621|9.332414
ballabio|lc|45.898184|9.423444
barzago|lc|45.755479|9.314244
barzanò|lc|45.733474|9.312813
barzio|lc|45.946095|9.468765
bellano|lc|46.042957|9.304032
bosisio parini|lc|45.80228|9.290743
brivio|lc|45.741969|9.444142
bulciago|lc|45.754703|9.291047
calco|lc|45.724868|9.415905
calolziocorte|lc|45.801472|9.432979
carenno|lc|45.803482|9.462047
casargo|lc|46.03952|9.388244
casatenovo|lc|45.696378|9.311646
cassago brianza|lc|45.738206|9.291088
cassina valsassina|lc|45.932085|9.478945
castello di brianza|lc|45.756471|9.345355
cernusco lombardone|lc|45.69286|9.400698
cesana brianza|lc|45.816781|9.299543
civate|lc|45.828081|9.342944
colico|lc|46.116215|9.314998
colle brianza|lc|45.761661|9.362425
cortenova|lc|45.999922|9.383165
costa masnaga|lc|45.767002|9.278316
crandola valsassina|lc|46.022892|9.378763
cremella|lc|45.74168|9.299885
cremeno|lc|45.93485|9.473729
dervio|lc|46.076574|9.304528
dolzago|lc|45.76698|9.339345
dorio|lc|46.101812|9.31927
ello|lc|45.78568|9.365345
erve|lc|45.821582|9.452846
esino lario|lc|45.993939|9.3332
galbiate|lc|45.817534|9.375015
garbagnate monastero|lc|45.772926|9.302566
garlate|lc|45.80604|9.40197
imbersago|lc|45.705873|9.444549
introbio|lc|45.972641|9.45307
lecco|lc|45.900548|9.412025
lierna|lc|45.959202|9.302478
lomagna|lc|45.667978|9.376047
malgrate|lc|45.8526|9.375298
mandello del lario|lc|45.919183|9.319341
margno|lc|46.030987|9.381641
merate|lc|45.698679|9.411648
missaglia|lc|45.708717|9.335754
moggio|lc|45.932266|9.487907
molteno|lc|45.78038|9.307444
monte marenzo|lc|45.770781|9.454647
montevecchia|lc|45.705744|9.375436
monticello brianza|lc|45.70878|9.314141
morterone|lc|45.879515|9.486227
nibionno|lc|45.746479|9.268944
oggiono|lc|45.79188|9.346745
olgiate molgora|lc|45.730279|9.403347
olginate|lc|45.800938|9.413279
oliveto lario|lc|45.929247|9.285912
osnago|lc|45.675678|9.391148
paderno d'adda|lc|45.681458|9.44456
pagnona|lc|46.059741|9.402893
parlasco|lc|46.017886|9.34494
pasturo|lc|45.951783|9.442169
perledo|lc|46.012835|9.29632
pescate|lc|45.833482|9.393945
premana|lc|46.05173|9.423463
primaluna|lc|45.986971|9.429608
robbiate|lc|45.691134|9.441322
rogeno|lc|45.782379|9.274043
santa maria hoè|lc|45.744389|9.375011
sirone|lc|45.77268|9.322144
sirtori|lc|45.736579|9.330845
sueglio|lc|46.086061|9.33236
suello|lc|45.816781|9.311543
taceno|lc|46.024486|9.36394
valgreghentino|lc|45.769871|9.412168
valmadrera|lc|45.846282|9.358244
varenna|lc|46.009978|9.283159
vercurago|lc|45.812946|9.424433
viganò|lc|45.724635|9.32456
verderio|lc|45.665698|9.43761
la valletta brianza|lc|45.728714|9.3692
valvarrone|lc|46.08489|9.361448
abbadia cerreto|lo|45.311912|9.594212
bertonico|lo|45.23307|9.668062
boffalora d'adda|lo|45.359771|9.494156
borghetto lodigiano|lo|45.213268|9.499683
borgo san giovanni|lo|45.278042|9.435836
brembio|lo|45.214568|9.57146
casaletto lodigiano|lo|45.293568|9.361954
casalmaiocco|lo|45.35387|9.374353
casalpusterlengo|lo|45.179329|9.648396
caselle landi|lo|45.102568|9.795567
caselle lurani|lo|45.280388|9.360225
castelnuovo bocca d'adda|lo|45.112842|9.867959
castiglione d'adda|lo|45.218912|9.694111
castiraga vidardo|lo|45.264809|9.392875
cavenago d'adda|lo|45.28401|9.601442
cervignano d'adda|lo|45.373871|9.424054
codogno|lo|45.160066|9.70437
comazzo|lo|45.440973|9.464654
cornegliano laudense|lo|45.286169|9.487957
corno giovine|lo|45.133291|9.758512
cornovecchio|lo|45.136668|9.799367
corte palasio|lo|45.30961|9.564308
crespiatica|lo|45.356672|9.575158
fombio|lo|45.136629|9.684479
galgagnano|lo|45.357971|9.445355
graffignana|lo|45.209188|9.452577
guardamiglio|lo|45.109902|9.679791
livraga|lo|45.192268|9.54646
lodi|lo|45.26131|9.491678
lodi vecchio|lo|45.301075|9.416599
maccastorna|lo|45.14433|9.855215
mairago|lo|45.251769|9.57836
maleo|lo|45.168938|9.7635
marudo|lo|45.252967|9.377155
massalengo|lo|45.26472|9.490245
meleti|lo|45.118868|9.836968
merlino|lo|45.433372|9.429853
montanaso lombardo|lo|45.33577|9.468656
mulazzano|lo|45.373492|9.397993
orio litta|lo|45.160313|9.554207
ospedaletto lodigiano|lo|45.168767|9.578661
ossago lodigiano|lo|45.245669|9.537059
pieve fissiraga|lo|45.263268|9.458357
salerano sul lambro|lo|45.296569|9.385155
san fiorano|lo|45.136368|9.722165
san martino in strada|lo|45.271532|9.528032
san rocco al porto|lo|45.081966|9.697165
sant'angelo lodigiano|lo|45.238067|9.408656
santo stefano lodigiano|lo|45.120066|9.735614
secugnago|lo|45.232869|9.59346
senna lodigiana|lo|45.151667|9.593862
somaglia|lo|45.14672|9.637931
sordio|lo|45.341143|9.365693
tavazzano con villavesco|lo|45.334092|9.403652
terranova dei passerini|lo|45.19832|9.677121
turano lodigiano|lo|45.24887|9.621461
valera fratta|lo|45.256932|9.336008
villanova del sillaro|lo|45.237968|9.481858
zelo buon persico|lo|45.41106|9.431757
castelgerundo|lo|45.200297|9.743802
agrate brianza|mb|45.576173|9.350948
aicurzio|mb|45.639977|9.414949
albiate|mb|45.65899|9.255796
arcore|mb|45.626556|9.321465
barlassina|mb|45.656416|9.130101
bellusco|mb|45.618748|9.418633
bernareggio|mb|45.647508|9.406138
besana in brianza|mb|45.701804|9.288298
biassono|mb|45.630987|9.27512
bovisio-masciago|mb|45.6109|9.151857
briosco|mb|45.710077|9.240644
brugherio|mb|45.550749|9.301222
burago di molgora|mb|45.59666|9.377706
camparada|mb|45.655977|9.319646
carate brianza|mb|45.676549|9.237162
carnate|mb|45.647281|9.379087
cavenago di brianza|mb|45.584917|9.416059
ceriano laghetto|mb|45.628349|9.080797
cesano maderno|mb|45.628189|9.14624
cogliate|mb|45.64548|9.080732
concorezzo|mb|45.589675|9.335948
correzzana|mb|45.665689|9.303626
desio|mb|45.61831|9.208124
giussano|mb|45.699381|9.209729
lazzate|mb|45.672326|9.08522
lesmo|mb|45.648974|9.305652
limbiate|mb|45.598605|9.119731
lissone|mb|45.61077|9.240841
macherio|mb|45.640116|9.269371
meda|mb|45.662505|9.156753
mezzago|mb|45.628977|9.44485
misinto|mb|45.662375|9.083941
monza|mb|45.583442|9.273526
muggiò|mb|45.593195|9.227138
nova milanese|mb|45.58929|9.20034
ornago|mb|45.598404|9.422214
renate|mb|45.724378|9.279944
ronco briantino|mb|45.667563|9.400698
seregno|mb|45.649723|9.205392
seveso|mb|45.643466|9.137379
sovico|mb|45.648556|9.264837
sulbiate|mb|45.633999|9.420684
triuggio|mb|45.661364|9.267441
usmate velate|mb|45.652458|9.354342
varedo|mb|45.597444|9.156293
vedano al lambro|mb|45.611508|9.275072
veduggio con colzano|mb|45.73363|9.262513
verano brianza|mb|45.688186|9.230477
villasanta|mb|45.603981|9.300571
vimercate|mb|45.613963|9.37006
busnago|mb|45.616184|9.46567
caponago|mb|45.565438|9.375441
cornate d'adda|mb|45.646699|9.465939
lentate sul seveso|mb|45.678405|9.11817
roncello|mb|45.602443|9.457611
aldino|bz|46.368675|11.354324
andriano|bz|46.517616|11.231373
anterivo|bz|46.279747|11.367961
appiano sulla strada del vino|bz|46.461908|11.260867
avelengo|bz|46.644615|11.224384
badia|bz|46.610124|11.893487
barbiano|bz|46.603257|11.519816
bolzano|bz|46.655942|11.229637
braies|bz|46.688625|12.103757
brennero|bz|47.00378|11.504557
bressanone|bz|46.716549|11.657851
bronzolo|bz|46.404262|11.320055
brunico|bz|46.796319|11.935512
caines|bz|46.69948|11.169487
caldaro sulla strada del vino|bz|46.413259|11.246202
campo di trens|bz|46.857187|11.508955
campo tures|bz|46.919236|11.955182
castelbello-ciardes|bz|46.622161|10.923999
castelrotto|bz|46.547539|11.608297
cermes|bz|46.634619|11.146564
chienes|bz|46.809381|11.840872
chiusa|bz|46.639687|11.566643
cornedo all'isarco|bz|46.489912|11.408388
cortaccia sulla strada del vino|bz|46.313473|11.223501
cortina sulla strada del vino|bz|46.268702|11.222685
corvara in badia|bz|46.549574|11.874034
curon venosta|bz|46.803738|10.555045
dobbiaco|bz|46.735636|12.223958
egna|bz|46.316916|11.27257
falzes|bz|46.81198|11.884006
fiè allo sciliar|bz|46.517598|11.502614
fortezza|bz|46.789626|11.609977
funes|bz|46.6451|11.696841
gais|bz|46.833156|11.947484
gargazzone|bz|46.584781|11.20273
glorenza|bz|46.671492|10.553913
laces|bz|46.617344|10.859539
lagundo|bz|46.682519|11.123069
laion|bz|46.609229|11.564263
laives|bz|46.422819|11.334771
lana|bz|46.616019|11.144859
lasa|bz|46.616614|10.700159
lauregno|bz|46.454414|11.062015
luson|bz|46.746466|11.761084
magrè sulla strada del vino|bz|46.287388|11.209846
malles venosta|bz|46.687919|10.546549
marebbe|bz|46.648879|12.026938
marlengo|bz|46.655561|11.140291
martello|bz|46.515319|10.721431
meltina|bz|46.586392|11.256156
merano|bz|46.669555|11.159418
monguelfo-tesido|bz|46.781145|12.118922
montagna|bz|46.330836|11.304634
moso in passiria|bz|46.831832|11.167293
nalles|bz|46.543487|11.204627
naturno|bz|46.649886|11.004195
naz-sciaves|bz|46.763074|11.677935
nova levante|bz|46.430234|11.53895
nova ponente|bz|46.413993|11.425181
ora|bz|46.346595|11.298634
ortisei|bz|46.590462|11.684557
parcines|bz|46.683848|11.07352
perca|bz|46.794307|11.982383
plaus|bz|46.656575|11.041498
ponte gardena|bz|46.598216|11.53251
postal|bz|46.607583|11.193324
prato allo stelvio|bz|46.603767|10.581909
predoi|bz|47.040741|12.105891
proves|bz|46.477426|11.022437
racines|bz|46.880128|11.376056
rasun-anterselva|bz|46.839015|12.111611
renon|bz|46.562935|11.441435
rifiano|bz|46.70364|11.181443
rio di pusteria|bz|46.795946|11.666827
rodengo|bz|46.780775|11.689757
salorno sulla strada del vino|bz|46.239608|11.211075
san candido|bz|46.732863|12.282416
san genesio atesino|bz|46.535035|11.331384
san leonardo in passiria|bz|46.811893|11.247136
san lorenzo di sebato|bz|46.783424|11.90264
san martino in badia|bz|46.650928|11.841677
san martino in passiria|bz|46.783939|11.226799
san pancrazio|bz|46.586514|11.086361
santa cristina valgardena|bz|46.558875|11.720036
sarentino|bz|46.695259|11.387379
scena|bz|46.690046|11.186563
selva dei molini|bz|46.890747|11.85666
selva di val gardena|bz|46.552588|11.766541
senales|bz|46.727724|10.862756
sesto|bz|46.702743|12.350544
silandro|bz|46.628069|10.773476
sluderno|bz|46.664538|10.584817
stelvio|bz|46.542439|10.557829
terento|bz|46.8288|11.776016
terlano|bz|46.529301|11.248678
termeno sulla strada del vino|bz|46.341512|11.242272
tesimo|bz|46.563898|11.169497
tires|bz|46.467348|11.52748
tirolo|bz|46.708518|11.142386
trodena nel parco naturale|bz|46.322143|11.349812
tubre|bz|46.645612|10.464153
ultimo|bz|46.511994|10.899493
vadena|bz|46.413995|11.305077
valdaora|bz|46.759829|12.030987
val di vizze|bz|46.948993|11.577499
valle aurina|bz|46.988356|11.942898
valle di casies|bz|46.824096|12.232504
vandoies|bz|46.876036|11.720904
varna|bz|46.740386|11.634569
verano|bz|46.605366|11.226271
villabassa|bz|46.739482|12.170006
villandro|bz|46.632934|11.540101
vipiteno|bz|46.896324|11.43194
velturno|bz|46.668674|11.598439
la valle|bz|46.657719|11.924326
senale-san felice|bz|46.509531|11.137327
ala|tn|45.757901|11.001818
albiano|tn|46.144906|11.193479
aldeno|tn|45.97678|11.090857
andalo|tn|46.166136|11.003402
arco|tn|45.918133|10.886095
avio|tn|45.733793|10.937181
baselga di pinè|tn|46.131281|11.256837
bedollo|tn|46.167698|11.301589
besenello|tn|45.941922|11.109544
bieno|tn|46.080679|11.554041
bleggio superiore|tn|46.006942|10.789644
bocenago|tn|46.119103|10.758862
bondone|tn|45.80623|10.551537
borgo valsugana|tn|46.053346|11.4566
brentonico|tn|45.818774|10.956013
bresimo|tn|46.423298|10.92869
caderzone terme|tn|46.130202|10.75747
calceranica al lago|tn|46.004241|11.242883
caldes|tn|46.365389|10.94258
caldonazzo|tn|45.991523|11.262133
calliano|tn|45.933851|11.093758
campitello di fassa|tn|46.476168|11.741525
campodenno|tn|46.257827|11.0332
canal san bovo|tn|46.154846|11.732955
canazei|tn|46.476881|11.771075
capriana|tn|46.261913|11.338584
carisolo|tn|46.168776|10.759159
carzano|tn|46.070325|11.493343
castel condino|tn|45.914395|10.60277
castello-molina di fiemme|tn|46.256056|11.404439
castello tesino|tn|46.064175|11.632171
castelnuovo|tn|46.052859|11.493282
cavalese|tn|46.291187|11.460468
cavareno|tn|46.408412|11.138073
cavedago|tn|46.184698|11.031416
cavedine|tn|45.9952|10.973577
cavizzana|tn|46.367501|10.957923
cimone|tn|45.97985|11.063017
cinte tesino|tn|46.056768|11.614868
cis|tn|46.399187|11.00296
civezzano|tn|46.089367|11.180026
cles|tn|46.365262|11.033245
commezzadura|tn|46.31299|10.832832
croviana|tn|46.344779|10.903423
dambel|tn|46.404712|11.093072
denno|tn|46.275008|11.049074
drena|tn|45.969299|10.944477
dro|tn|45.961471|10.91111
fai della paganella|tn|46.179331|11.06893
fiavè|tn|46.00458|10.842052
fierozzo|tn|46.108087|11.34631
folgaria|tn|45.914973|11.173015
fornace|tn|46.118196|11.206501
frassilongo|tn|46.080854|11.319519
garniga terme|tn|46.002649|11.087054
giovo|tn|46.185824|11.146814
giustino|tn|46.14844|10.769479
grigno|tn|46.017035|11.635942
imer|tn|46.15106|11.796303
isera|tn|45.88527|11.009311
lavarone|tn|45.941289|11.288638
lavis|tn|46.139742|11.110227
levico terme|tn|46.009126|11.301777
livo|tn|46.404711|11.019271
lona-lases|tn|46.149818|11.219168
luserna|tn|45.921665|11.32429
malé|tn|46.351607|10.912876
massimeno|tn|46.141756|10.773134
mazzin|tn|46.468805|11.700375
mezzana|tn|46.317316|10.801007
mezzano|tn|46.154712|11.807993
mezzocorona|tn|46.215436|11.120786
mezzolombardo|tn|46.212094|11.093509
moena|tn|46.376943|11.659266
molveno|tn|46.142106|10.963777
mori|tn|45.852444|10.978275
nago-torbole|tn|45.851159|10.885079
nogaredo|tn|45.912089|11.023046
nomi|tn|45.928599|11.071881
novaledo|tn|46.022818|11.366434
ospedaletto|tn|46.043569|11.552958
ossana|tn|46.306453|10.737701
palù del fersina|tn|46.130737|11.366488
panchià|tn|46.285653|11.542587
ronzo-chienis|tn|45.889597|10.950079
peio|tn|46.362886|10.673244
pellizzano|tn|46.309824|10.760451
pelugo|tn|46.0885|10.72387
pergine valsugana|tn|46.060529|11.240675
pieve tesino|tn|46.069309|11.608091
pinzolo|tn|46.161732|10.765004
pomarolo|tn|45.928099|11.04308
predazzo|tn|46.312361|11.602694
rabbi|tn|46.400901|10.824948
riva del garda|tn|45.884771|10.839694
romeno|tn|46.393997|11.120273
roncegno terme|tn|46.051623|11.410302
ronchi valsugana|tn|46.068406|11.435186
ronzone|tn|46.423073|11.148899
roverè della luna|tn|46.249988|11.169348
rovereto|tn|45.886548|11.045237
ruffrè-mendola|tn|46.42463|11.1941
rumo|tn|46.461057|10.988246
sagron mis|tn|46.191396|11.933561
samone|tn|46.081607|11.522588
san michele all'adige|tn|46.192907|11.133577
sant'orsola terme|tn|46.108138|11.302706
sanzeno|tn|46.379233|11.071511
sarnonico|tn|46.418728|11.140792
scurelle|tn|46.064466|11.505931
segonzano|tn|46.192637|11.281873
sfruz|tn|46.337155|11.115757
soraga di fassa|tn|46.394607|11.666168
sover|tn|46.221709|11.315681
spiazzo|tn|46.103601|10.73937
spormaggiore|tn|46.217752|11.048268
sporminore|tn|46.236807|11.030274
stenico|tn|46.052168|10.854089
storo|tn|45.849075|10.579914
strembo|tn|46.119912|10.751452
telve|tn|46.071328|11.479592
telve di sopra|tn|46.071352|11.471672
tenna|tn|46.015703|11.264283
tenno|tn|45.918797|10.832775
terragnolo|tn|45.865249|11.177422
terzolas|tn|46.361605|10.926088
tesero|tn|46.290416|11.511977
tione di trento|tn|46.036304|10.726909
ton|tn|46.259156|11.101655
torcegno|tn|46.074307|11.449787
trambileno|tn|45.834845|11.139211
trento|tn|46.066423|11.12576
valfloriana|tn|46.246883|11.348673
vallarsa|tn|45.803355|11.10511
vermiglio|tn|46.296905|10.691365
vignola-falesina|tn|46.05522|11.281637
villa lagarina|tn|45.91499|11.033002
volano|tn|45.917179|11.060803
ziano di fiemme|tn|46.286517|11.565976
comano terme|tn|46.009118|10.883621
ledro|tn|45.886414|10.715385
predaia|tn|46.335273|11.0897
san lorenzo dorsino|tn|46.112129|10.900078
valdaone|tn|46.024913|10.52513
dimaro folgarida|tn|46.302192|10.886908
pieve di bono-prezzo|tn|45.9452|10.673201
altavalle|tn|46.216653|11.25295
altopiano della vigolana|tn|45.984533|11.199754
amblar-don|tn|46.385024|11.172159
borgo chiese|tn|45.892362|10.561974
borgo lares|tn|46.00461|10.72732
castel ivano|tn|46.063059|11.541825
cembra lisignago|tn|46.180668|11.200776
contà|tn|46.295114|11.018913
madruzzo|tn|46.032884|11.003484
porte di rendena|tn|46.073237|10.673098
primiero san martino di castrozza|tn|46.243723|11.836028
sella giudicarie|tn|46.022676|10.633704
tre ville|tn|46.074324|10.7711
vallelaghi|tn|46.099827|11.016219
ville d'anaunia|tn|46.273817|10.940222
san giovanni di fassa|tn|46.422876|11.72076
terre d'adige|tn|46.163901|11.06924
borgo d'anaunia|tn|46.463185|11.149124
novella|tn|46.436999|11.078127
ville di fiemme|tn|46.31643|11.445446
affi|vr|45.554091|10.776424
albaredo d'adige|vr|45.317046|11.274488
angiari|vr|45.218142|11.283908
arcole|vr|45.358211|11.286092
badia calavena|vr|45.566086|11.152393
bardolino|vr|45.54756|10.724214
belfiore|vr|45.379623|11.208976
bevilacqua|vr|45.231785|11.3925
bonavigo|vr|45.257573|11.279332
boschi sant'anna|vr|45.21891|11.358328
bosco chiesanuova|vr|45.622102|11.029517
bovolone|vr|45.25781|11.120366
brentino belluno|vr|45.667941|10.881419
brenzone sul garda|vr|45.692861|10.772012
bussolengo|vr|45.474131|10.846248
buttapietra|vr|45.34209|10.99859
caldiero|vr|45.414388|11.177392
caprino veronese|vr|45.605938|10.795132
casaleone|vr|45.170669|11.199365
castagnaro|vr|45.120755|11.41075
castel d'azzano|vr|45.355884|10.952988
castelnuovo del garda|vr|45.4384|10.760479
cavaion veronese|vr|45.540293|10.770187
cazzano di tramigna|vr|45.473089|11.202892
cerea|vr|45.192043|11.212924
cerro veronese|vr|45.575292|11.041086
cologna veneta|vr|45.310287|11.384399
colognola ai colli|vr|45.434631|11.186701
concamarise|vr|45.207283|11.138501
costermano sul garda|vr|45.585664|10.739974
dolcè|vr|45.600689|10.852482
erbè|vr|45.240929|10.968485
erbezzo|vr|45.639788|11.00141
ferrara di monte baldo|vr|45.676916|10.85434
fumane|vr|45.542953|10.883974
garda|vr|45.575651|10.70853
gazzo veronese|vr|45.130336|11.096079
grezzana|vr|45.518531|11.016159
illasi|vr|45.465933|11.180021
isola della scala|vr|45.273105|11.006889
isola rizza|vr|45.291198|11.19818
lavagno|vr|45.447398|11.134941
lazise|vr|45.5052|10.733024
legnago|vr|45.192498|11.311127
malcesine|vr|45.764344|10.81006
marano di valpolicella|vr|45.556047|10.915684
mezzane di sotto|vr|45.482006|11.128153
minerbe|vr|45.240862|11.334839
montecchia di crosara|vr|45.484518|11.253425
monteforte d'alpone|vr|45.417254|11.283666
mozzecane|vr|45.309983|10.818531
negrar di valpolicella|vr|45.529671|10.940022
nogara|vr|45.179012|11.063733
nogarole rocca|vr|45.291882|10.883788
oppeano|vr|45.306214|11.18234
palù|vr|45.325834|11.155173
pastrengo|vr|45.492457|10.799713
pescantina|vr|45.483372|10.869101
peschiera del garda|vr|45.438945|10.692017
povegliano veronese|vr|45.348398|10.882438
pressana|vr|45.284749|11.40209
rivoli veronese|vr|45.571982|10.811761
roncà|vr|45.478892|11.288918
ronco all'adige|vr|45.337766|11.242452
roverchiara|vr|45.272316|11.2478
roveredo di guà|vr|45.273751|11.444988
roverè veronese|vr|45.593326|11.065709
salizzole|vr|45.24279|11.084444
san bonifacio|vr|45.395642|11.270082
san giovanni ilarione|vr|45.524054|11.234894
san giovanni lupatoto|vr|45.381968|11.044886
sanguinetto|vr|45.184501|11.151136
san martino buon albergo|vr|45.421687|11.09689
san mauro di saline|vr|45.565796|11.114376
san pietro di morubio|vr|45.242986|11.228187
san pietro in cariano|vr|45.520913|10.886377
sant'ambrogio di valpolicella|vr|45.522113|10.834443
sant'anna d'alfaedo|vr|45.627457|10.95155
san zeno di montagna|vr|45.631445|10.725449
selva di progno|vr|45.61139|11.138603
soave|vr|45.42023|11.247722
sommacampagna|vr|45.407784|10.840853
sona|vr|45.433438|10.832737
sorgà|vr|45.2134|10.979362
terrazzo|vr|45.173309|11.398613
torri del benaco|vr|45.609449|10.687387
tregnago|vr|45.513977|11.165641
trevenzuolo|vr|45.268649|10.932184
valeggio sul mincio|vr|45.353751|10.734546
velo veronese|vr|45.60515|11.095691
verona|vr|45.438496|10.992412
veronella|vr|45.324969|11.322261
vestenanova|vr|45.573467|11.227722
vigasio|vr|45.31703|10.942198
villa bartolomea|vr|45.158717|11.353377
villafranca di verona|vr|45.350928|10.845996
zevio|vr|45.375131|11.135882
zimella|vr|45.343627|11.352305
agugliaro|vi|45.325389|11.584203
albettone|vi|45.35815|11.581474
alonte|vi|45.365037|11.427363
altavilla vicentina|vi|45.508429|11.470686
altissimo|vi|45.614739|11.251258
arcugnano|vi|45.500472|11.535699
arsiero|vi|45.803517|11.350404
arzignano|vi|45.519292|11.338594
asiago|vi|45.875377|11.5107
asigliano veneto|vi|45.304512|11.446266
bassano del grappa|vi|45.766911|11.734347
bolzano vicentino|vi|45.60071|11.622101
breganze|vi|45.708144|11.564767
brendola|vi|45.472122|11.44697
bressanvido|vi|45.645976|11.632999
brogliano|vi|45.588786|11.364543
caldogno|vi|45.611836|11.50758
caltrano|vi|45.770615|11.460801
calvene|vi|45.766846|11.513017
camisano vicentino|vi|45.522021|11.713344
campiglia dei berici|vi|45.336242|11.539489
carrè|vi|45.748384|11.459298
cartigliano|vi|45.713054|11.694064
cassola|vi|45.732407|11.794256
castegnero|vi|45.442475|11.584448
castelgomberto|vi|45.585285|11.394497
chiampo|vi|45.544573|11.280896
chiuppano|vi|45.762173|11.465076
cogollo del cengio|vi|45.785221|11.426095
cornedo vicentino|vi|45.611106|11.343405
costabissara|vi|45.585542|11.487744
creazzo|vi|45.532053|11.478581
crespadoro|vi|45.620193|11.22589
dueville|vi|45.635517|11.548598
enego|vi|45.940493|11.709612
fara vicentino|vi|45.739593|11.548287
foza|vi|45.896266|11.630726
gallio|vi|45.892626|11.547102
gambellara|vi|45.462509|11.33984
gambugliano|vi|45.588874|11.440015
grisignano di zocco|vi|45.47534|11.69895
grumolo delle abbadesse|vi|45.516264|11.658742
isola vicentina|vi|45.628668|11.443148
laghi|vi|45.824387|11.272312
lastebasse|vi|45.915101|11.273886
longare|vi|45.474634|11.611049
lonigo|vi|45.388116|11.38799
lugo di vicenza|vi|45.751391|11.522901
malo|vi|45.660296|11.407493
marano vicentino|vi|45.692549|11.428756
marostica|vi|45.745392|11.656894
montebello vicentino|vi|45.457216|11.385366
montecchio maggiore|vi|45.505079|11.408445
montecchio precalcino|vi|45.665781|11.563302
monte di malo|vi|45.660464|11.362566
montegalda|vi|45.443775|11.673211
montegaldella|vi|45.436355|11.670826
monteviale|vi|45.560525|11.458072
monticello conte otto|vi|45.59518|11.580579
montorso vicentino|vi|45.489834|11.362271
mussolente|vi|45.778952|11.801272
nanto|vi|45.421995|11.605006
nogarole vicentino|vi|45.55936|11.28843
nove|vi|45.725675|11.680058
noventa vicentina|vi|45.290628|11.539884
orgiano|vi|45.351043|11.466029
pedemonte|vi|45.503234|10.91872
pianezze|vi|45.739997|11.626747
piovene rocchette|vi|45.758826|11.430127
pojana maggiore|vi|45.291381|11.501347
posina|vi|45.791088|11.262517
pove del grappa|vi|45.799117|11.729102
pozzoleone|vi|45.648956|11.67909
quinto vicentino|vi|45.573798|11.627719
recoaro terme|vi|45.705171|11.224935
roana|vi|45.875619|11.462687
romano d'ezzelino|vi|45.796237|11.758391
rosà|vi|45.724416|11.762686
rossano veneto|vi|45.705273|11.799787
rotzo|vi|45.863467|11.399382
salcedo|vi|45.75878|11.564978
sandrigo|vi|45.660608|11.602928
san pietro mussolino|vi|45.586304|11.256077
santorso|vi|45.737299|11.390321
san vito di leguzzano|vi|45.681552|11.37629
sarcedo|vi|45.707921|11.526038
sarego|vi|45.408121|11.405559
schiavon|vi|45.697709|11.644283
schio|vi|45.711351|11.355359
solagna|vi|45.816721|11.71817
sossano|vi|45.358777|11.510029
sovizzo|vi|45.528028|11.445634
tezze sul brenta|vi|45.686178|11.704184
thiene|vi|45.705767|11.480256
tonezza del cimone|vi|45.85719|11.346038
torrebelvicino|vi|45.718939|11.308834
torri di quartesolo|vi|45.519266|11.61712
trissino|vi|45.564041|11.374852
valdagno|vi|45.641321|11.30406
valdastico|vi|45.882447|11.344561
valli del pasubio|vi|45.740774|11.262167
velo d'astico|vi|45.788399|11.36769
vicenza|vi|45.634859|11.406354
villaga|vi|45.402477|11.53372
villaverla|vi|45.65121|11.494074
zanè|vi|45.720119|11.460827
zermeghedo|vi|45.474991|11.369496
zovencedo|vi|45.428491|11.503199
zugliano|vi|45.733953|11.524736
val liona|vi|45.410202|11.472079
barbarano mossano|vi|45.408342|11.567659
valbrenta|vi|45.881372|11.701764
colceresa|vi|45.718116|11.607036
lusiana conco|vi|45.808073|11.587935
agordo|bl|46.282147|12.034981
alano di piave|bl|45.907803|11.908195
alleghe|bl|46.406637|12.021464
arsiè|bl|45.983519|11.75778
auronzo di cadore|bl|46.551169|12.443206
belluno|bl|46.280541|12.078914
borca di cadore|bl|46.436423|12.219598
calalzo di cadore|bl|46.446715|12.380385
cencenighe agordino|bl|46.352309|11.967348
cesiomaggiore|bl|46.088816|11.987639
chies d'alpago|bl|46.165328|12.392472
cibiana di cadore|bl|46.391613|12.2908
colle santa lucia|bl|46.447046|12.014025
comelico superiore|bl|46.626931|12.469051
cortina d'ampezzo|bl|46.538333|12.137351
danta di cadore|bl|46.566029|12.517302
domegge di cadore|bl|46.460376|12.415824
falcade|bl|46.358029|11.872136
feltre|bl|46.016376|11.906254
fonzaso|bl|46.017903|11.799754
canale d'agordo|bl|46.360668|11.914832
gosaldo|bl|46.212841|11.974516
lamon|bl|46.047151|11.748632
la valle agordina|bl|46.282273|12.06951
limana|bl|46.102814|12.186303
livinallongo del col di lana|bl|46.506634|11.936812
lorenzago di cadore|bl|46.479626|12.459403
lozzo di cadore|bl|46.484572|12.443341
ospitale di cadore|bl|46.330524|12.322888
pedavena|bl|46.039624|11.880433
perarolo di cadore|bl|46.397031|12.35555
pieve di cadore|bl|46.428709|12.375297
ponte nelle alpi|bl|46.16178|12.298805
rivamonte agordino|bl|46.251887|12.023608
rocca pietore|bl|46.433868|11.977162
san gregorio nelle alpi|bl|46.103065|12.025584
san nicolò di comelico|bl|46.582529|12.527302
san pietro di cadore|bl|46.571176|12.586118
santa giustina|bl|46.083884|12.043238
san tomaso agordino|bl|46.388481|11.972806
santo stefano di cadore|bl|46.557938|12.549142
san vito di cadore|bl|46.458966|12.205742
sedico|bl|46.11098|12.097154
selva di cadore|bl|46.451386|12.034525
seren del grappa|bl|45.989755|11.8449
sospirolo|bl|46.142372|12.074176
soverzene|bl|46.203575|12.303055
sovramonte|bl|46.056824|11.788162
taibon agordino|bl|46.298782|12.012654
tambre|bl|46.129473|12.422713
vallada agordina|bl|46.375603|11.936955
valle di cadore|bl|46.417356|12.331983
vigo di cadore|bl|46.500273|12.472522
vodo cadore|bl|46.420006|12.246864
voltago agordino|bl|46.272016|12.005196
zoppè di cadore|bl|46.386081|12.17394
quero vas|bl|45.954615|11.913206
longarone|bl|46.265786|12.299937
alpago|bl|46.130668|12.354894
val di zoldo|bl|46.342294|12.156312
borgo valbelluna|bl|46.061795|12.079349
altivole|tv|45.754003|11.955362
arcade|tv|45.785053|12.219408
asolo|tv|45.800726|11.914244
borso del grappa|tv|45.821025|11.799316
breda di piave|tv|45.723727|12.331235
caerano di san marco|tv|45.78591|12.001038
cappella maggiore|tv|45.96954|12.361669
carbonera|tv|45.684838|12.285944
casale sul sile|tv|45.597766|12.325734
casier|tv|45.628336|12.26516
castelcucco|tv|45.833324|11.883969
castelfranco veneto|tv|45.672811|11.925245
castello di godego|tv|45.69238|11.881044
cavaso del tomba|tv|45.8584|11.90898
cessalto|tv|45.712596|12.612314
chiarano|tv|45.730041|12.581279
cimadolmo|tv|45.787138|12.361633
cison di valmarino|tv|45.969987|12.143137
codognè|tv|45.866795|12.433908
colle umberto|tv|45.940984|12.340972
conegliano|tv|45.886217|12.297757
cordignano|tv|45.94897|12.41602
cornuda|tv|45.831731|12.007666
crocetta del montello|tv|45.833996|12.032327
farra di soligo|tv|45.905381|12.12543
follina|tv|45.953073|12.118137
fontanelle|tv|45.831333|12.467546
fonte|tv|45.787158|11.867507
fregona|tv|46.000218|12.338962
gaiarine|tv|45.880504|12.481591
giavera del montello|tv|45.793974|12.169567
godega di sant'urbano|tv|45.929848|12.39811
gorgo al monticano|tv|45.786953|12.55042
istrana|tv|45.678616|12.099794
loria|tv|45.728424|11.865585
mansuè|tv|45.82239|12.537712
mareno di piave|tv|45.840856|12.351255
maser|tv|45.807541|11.972487
maserada sul piave|tv|45.749411|12.319333
meduna di livenza|tv|45.806443|12.612685
miane|tv|45.944321|12.093856
mogliano veneto|tv|45.561289|12.237697
monastier di treviso|tv|45.650076|12.43377
monfumo|tv|45.830373|11.921118
montebelluna|tv|45.775996|12.045065
morgano|tv|45.643398|12.112628
moriago della battaglia|tv|45.867358|12.103767
motta di livenza|tv|45.77663|12.610548
nervesa della battaglia|tv|45.825368|12.209202
oderzo|tv|45.783371|12.493778
ormelle|tv|45.779103|12.420337
orsago|tv|45.930861|12.423587
paese|tv|45.672731|12.153611
pederobba|tv|45.86952|11.969551
pieve di soligo|tv|45.899761|12.173081
ponte di piave|tv|45.716153|12.465227
ponzano veneto|tv|45.723851|12.194218
portobuffolè|tv|45.853838|12.538337
possagno|tv|45.854879|11.88157
povegliano|tv|45.759192|12.2085
preganziol|tv|45.60253|12.235314
quinto di treviso|tv|45.64201|12.149845
refrontolo|tv|45.924858|12.20783
resana|tv|45.634024|11.955293
revine lago|tv|46.001033|12.225549
riese pio x|tv|45.729222|11.917005
roncade|tv|45.627908|12.37478
salgareda|tv|45.704123|12.492587
san biagio di callalta|tv|45.684872|12.377347
san fior|tv|45.920753|12.358151
san pietro di feletto|tv|45.908889|12.240595
san polo di piave|tv|45.790108|12.394775
santa lucia di piave|tv|45.849186|12.284264
san vendemiano|tv|45.890746|12.333844
san zenone degli ezzelini|tv|45.778944|11.840513
sarmede|tv|45.978878|12.386088
segusino|tv|45.916678|11.953895
sernaglia della battaglia|tv|45.874731|12.133162
silea|tv|45.65335|12.296098
spresiano|tv|45.780391|12.259346
susegana|tv|45.850913|12.250013
tarzo|tv|45.973522|12.23161
trevignano|tv|45.733501|12.094016
treviso|tv|45.806691|12.206316
valdobbiadene|tv|45.901413|11.995512
vazzola|tv|45.837767|12.384064
vedelago|tv|45.686695|12.018037
vidor|tv|45.861641|12.038749
villorba|tv|45.729249|12.256246
vittorio veneto|tv|45.989665|12.296436
volpago del montello|tv|45.778058|12.119815
zenson di piave|tv|45.678973|12.492099
zero branco|tv|45.601701|12.165212
pieve del grappa|tv|45.827223|11.838844
annone veneto|ve|45.79551|12.68422
campagna lupia|ve|45.354994|12.096814
campolongo maggiore|ve|45.330536|12.048313
camponogara|ve|45.385068|12.072122
caorle|ve|45.599004|12.888008
cavarzere|ve|45.136099|12.081285
ceggia|ve|45.686437|12.637529
chioggia|ve|45.218894|12.27858
cinto caomaggiore|ve|45.825893|12.785572
cona|ve|45.180071|12.078695
concordia sagittaria|ve|45.756104|12.844956
dolo|ve|45.424996|12.076313
eraclea|ve|45.577034|12.673818
fiesso d'artico|ve|45.417348|12.033181
fossalta di piave|ve|45.645747|12.513256
fossalta di portogruaro|ve|45.791747|12.909023
fossò|ve|45.38565|12.048577
gruaro|ve|45.833496|12.843709
jesolo|ve|45.536709|12.638334
marcon|ve|45.554328|12.299382
martellago|ve|45.546645|12.157678
meolo|ve|45.619429|12.453057
mira|ve|45.437774|12.132805
mirano|ve|45.49277|12.109877
musile di piave|ve|45.617359|12.561887
noale|ve|45.550123|12.072088
noventa di piave|ve|45.661888|12.529942
pianiga|ve|45.456096|12.030173
portogruaro|ve|45.775598|12.837457
pramaggiore|ve|45.814565|12.738813
quarto d'altino|ve|45.580451|12.3705
salzano|ve|45.521344|12.10637
san donà di piave|ve|45.629481|12.564106
san michele al tagliamento|ve|45.763918|12.995425
santa maria di sala|ve|45.505036|12.033868
san stino di livenza|ve|45.731078|12.680008
scorzè|ve|45.571354|12.108383
spinea|ve|45.49116|12.165018
stra|ve|45.416845|12.002304
teglio veneto|ve|45.817512|12.885024
torre di mosto|ve|45.690543|12.71042
venezia|ve|45.437191|12.33459
vigonovo|ve|45.387011|12.007514
cavallino-treporti|ve|45.479217|12.515257
abano terme|pd|45.360314|11.789783
agna|pd|45.170164|11.960731
albignasego|pd|45.347492|11.867209
anguillara veneta|pd|45.139355|11.887746
arquà petrarca|pd|45.270044|11.716515
arre|pd|45.216319|11.930495
arzergrande|pd|45.27286|12.053944
bagnoli di sopra|pd|45.184841|11.883486
baone|pd|45.243863|11.687946
barbona|pd|45.111681|11.682407
battaglia terme|pd|45.288143|11.781834
boara pisani|pd|45.108085|11.782711
borgoricco|pd|45.533561|11.965879
bovolenta|pd|45.270375|11.935086
brugine|pd|45.296592|11.995113
cadoneghe|pd|45.458648|11.920175
campodarsego|pd|45.502855|11.907496
campodoro|pd|45.490294|11.752004
camposampiero|pd|45.57176|11.931929
campo san martino|pd|45.544396|11.810404
candiana|pd|45.22109|11.989114
carceri|pd|45.195001|11.620841
carmignano di brenta|pd|45.628771|11.699861
cartura|pd|45.26839|11.85681
casale di scodosia|pd|45.187494|11.468636
casalserugo|pd|45.316226|11.913545
castelbaldo|pd|45.121172|11.453502
cervarese santa croce|pd|45.424392|11.687904
cinto euganeo|pd|45.289467|11.672376
cittadella|pd|45.648794|11.78358
codevigo|pd|45.267352|12.101301
conselve|pd|45.233744|11.874013
correzzola|pd|45.235382|12.067762
curtarolo|pd|45.522255|11.832017
este|pd|45.224007|11.659796
fontaniva|pd|45.637629|11.752198
galliera veneta|pd|45.66284|11.828488
galzignano terme|pd|45.306808|11.733968
gazzo|pd|45581|11.706548
grantorto|pd|45.600775|11.730956
granze|pd|45.154289|11.713485
legnaro|pd|45.345648|11.963935
limena|pd|45.474376|11.844933
loreggia|pd|45.594524|11.944612
lozzo atestino|pd|45.290918|11.630411
maserà di padova|pd|45.320711|11.870985
masi|pd|45.108819|11.491912
massanzago|pd|45.555512|12.007434
megliadino san vitale|pd|45.19214|11.524237
merlara|pd|45.167068|11.441878
mestrino|pd|45.442758|11.758254
monselice|pd|45.241768|11.750853
montagnana|pd|45.233321|11.465778
montegrotto terme|pd|45.331491|11.791208
noventa padovana|pd|45.414232|11.9508
ospedaletto euganeo|pd|45.222695|11.610901
padova|pd|45.407717|11.873446
pernumia|pd|45.258989|11.787209
piacenza d'adige|pd|45.127462|11.547701
piazzola sul brenta|pd|45.541496|11.784404
piombino dese|pd|45.60752|11.999415
piove di sacco|pd|45.297692|12.036814
polverara|pd|45.309392|11.956012
ponso|pd|45.195057|11.588255
pontelongo|pd|45.245489|12.025183
ponte san nicolò|pd|45.365239|11.93727
pozzonovo|pd|45.196597|11.79265
rovolon|pd|45.378027|11.644403
rubano|pd|45.428729|11.788274
saccolongo|pd|45.403105|11.747553
san giorgio delle pertiche|pd|45.54167|11.911237
san giorgio in bosco|pd|45.591851|11.805959
san martino di lupari|pd|45.648659|11.85698
san pietro in gu|pd|45.614086|11.669394
san pietro viminario|pd|45.245244|11.818641
santa giustina in colle|pd|45.563698|11.906406
sant'angelo di piove di sacco|pd|45.345315|12.007022
sant'elena|pd|45.187341|11.71205
sant'urbano|pd|45.13305|11.635956
saonara|pd|45.370221|11.984066
selvazzano dentro|pd|45.38979|11.785892
solesino|pd|45.177332|11.743372
stanghella|pd|45.134624|11.756048
teolo|pd|45.365421|11.688606
terrassa padovana|pd|45.245268|11.903381
tombolo|pd|45.647028|11.830422
torreglia|pd|45.335667|11.73364
trebaseleghe|pd|45.591812|12.050999
tribano|pd|45.21001|11.832142
urbana|pd|45.194131|11.445349
veggiano|pd|45.448322|11.710822
vescovana|pd|45.134514|11.708435
vighizzolo d'este|pd|45.177003|11.625568
vigodarzere|pd|45.458193|11.882864
vigonza|pd|45.442954|11.983149
villa del conte|pd|45.586598|11.859005
villa estense|pd|45.172108|11.666979
villafranca padovana|pd|45.494851|11.797524
villanova di camposampiero|pd|45.490456|11.97302
vo'|pd|45.328899|11.641735
due carrare|pd|45.290505|11.821753
borgo veneto|pd|45.213452|11.541421
adria|ro|45.053123|12.0572
ariano nel polesine|ro|44.946084|12.124922
arquà polesine|ro|45.010388|11.740941
badia polesine|ro|45.094056|11.493462
bagnolo di po|ro|45.016008|11.501192
bergantino|ro|45.060169|11.2529
bosaro|ro|45.0008|11.766536
calto|ro|44.991498|11.356471
canaro|ro|44.935472|11.676738
canda|ro|45.034356|11.505648
castelguglielmo|ro|45.025181|11.537334
castelmassa|ro|45.016852|11.310498
castelnovo bariano|ro|45.027827|11.287445
ceneselli|ro|45.013695|11.369156
ceregnano|ro|45.049471|11.870854
corbola|ro|45.005685|12.07852
costa di rovigo|ro|45.048688|11.694531
crespino|ro|44.982558|11.886188
ficarolo|ro|44.953799|11.435729
fiesso umbertiano|ro|44.961184|11.604642
frassinelle polesine|ro|44.995182|11.698811
fratta polesine|ro|45.029282|11.64501
gaiba|ro|44.946179|11.480207
gavello|ro|45.021915|11.914413
giacciano con baruchella|ro|45.060423|11.433427
guarda veneta|ro|44.980399|11.802852
lendinara|ro|45.083259|11.604164
loreo|ro|45.061995|12.188925
lusia|ro|45.100535|11.66278
melara|ro|45.063185|11.198458
occhiobello|ro|44.92125|11.5814
papozze|ro|44.988248|12.031812
pettorazza grimani|ro|45.136088|11.986816
pincara|ro|44.991381|11.608809
polesella|ro|44.964082|11.750177
pontecchio polesine|ro|45.019111|11.811926
porto tolle|ro|44.917713|12.402186
rosolina|ro|45.075572|12.246084
rovigo|ro|44.977206|12.27419
salara|ro|44.982537|11.427149
san bellino|ro|45.029552|11.589812
san martino di venezze|ro|45.131203|11.868523
stienta|ro|44.937837|11.544413
taglio di po|ro|45.005777|12.210798
trecenta|ro|45.030944|11.460521
villadose|ro|45.068916|11.893738
villamarzana|ro|45.014404|11.693308
villanova del ghebbo|ro|45.059183|11.639009
villanova marchesana|ro|44.992284|11.964718
porto viro|ro|45.025311|12.225237
aiello del friuli|ud|45.872922|13.363628
amaro|ud|46.374031|13.09536
ampezzo|ud|46.417086|12.790274
aquileia|ud|45.769614|13.369798
arta terme|ud|46.47238|13.026675
artegna|ud|46.239096|13.155651
attimis|ud|46.189326|13.307128
bagnaria arsa|ud|45.88359|13.284961
basiliano|ud|46.016319|13.107126
bertiolo|ud|45.942667|13.04918
bicinicco|ud|45.935713|13.251166
bordano|ud|46.315327|13.104621
buja|ud|46.206677|13.118557
buttrio|ud|46.010421|13.333831
camino al tagliamento|ud|45.927016|12.945124
campoformido|ud|46.01932|13.162427
carlino|ud|45.80225|13.188223
cassacco|ud|46.173595|13.187934
castions di strada|ud|45.908309|13.185337
cavazzo carnico|ud|46.36749|13.039965
cercivento|ud|46.52727|12.989234
cervignano del friuli|ud|45.822834|13.336382
chiopris-viscone|ud|45.931403|13.38111
chiusaforte|ud|46.408334|13.309025
cividale del friuli|ud|46.093623|13.430328
codroipo|ud|45.961117|12.978952
colloredo di monte albano|ud|46.166405|13.139017
comeglians|ud|46.515234|12.867804
corno di rosazzo|ud|45.997061|13.441595
coseano|ud|46.097007|13.019388
dignano|ud|46.085028|12.939294
dogna|ud|46.447932|13.315023
drenchia|ud|46.174173|13.636537
enemonzo|ud|46.410902|12.878716
faedis|ud|46.150325|13.346429
fagagna|ud|46.11344|13.084572
flaibano|ud|46.058333|12.983642
forni avoltri|ud|46.586831|12.776908
forni di sopra|ud|46.420227|12.585596
forni di sotto|ud|46.395045|12.670226
gemona del friuli|ud|46.277007|13.140105
gonars|ud|45.895917|13.237131
grimacco|ud|46.167449|13.581181
latisana|ud|45.778488|12.996219
lauco|ud|46.424042|12.933029
lestizza|ud|45.954947|13.141735
lignano sabbiadoro|ud|45.689368|13.138566
lusevera|ud|46.275828|13.269325
magnano in riviera|ud|46.23133|13.176769
majano|ud|46.185205|13.068214
malborghetto valbruna|ud|46.497849|13.43315
manzano|ud|45.990512|13.381504
marano lagunare|ud|45.764993|13.167501
martignacco|ud|46.097922|13.136825
mereto di tomba|ud|46.049752|13.042751
moggio udinese|ud|46.409703|13.195142
moimacco|ud|46.091324|13.377031
montenars|ud|46.255026|13.178824
mortegliano|ud|45.945806|13.172796
moruzzo|ud|46.120241|13.123152
muzzana del turgnano|ud|45.818109|13.129263
nimis|ud|46.200671|13.266254
osoppo|ud|46.255978|13.08086
ovaro|ud|46.482943|12.865389
pagnacco|ud|46.12428|13.186975
palazzolo dello stella|ud|45.804827|13.079579
palmanova|ud|45.905464|13.309932
paluzza|ud|46.530815|13.018206
pasian di prato|ud|46.048513|13.187597
paularo|ud|46.530346|13.11656
pavia di udine|ud|45.996317|13.304137
pocenia|ud|45.835515|13.100829
pontebba|ud|46.506531|13.306426
porpetto|ud|45.858174|13.217853
povoletto|ud|46.118179|13.298797
pozzuolo del friuli|ud|45.986019|13.195029
pradamano|ud|46.033822|13.30263
prato carnico|ud|46.520705|12.809361
precenicco|ud|45.788913|13.077429
premariacco|ud|46.060687|13.394778
preone|ud|46.393684|12.866201
prepotto|ud|46.045723|13.479434
pulfero|ud|46.179275|13.477993
ragogna|ud|46.173522|12.98242
ravascletto|ud|46.522936|12.922115
raveo|ud|46.434428|12.871113
reana del rojale|ud|46.145736|13.23512
remanzacco|ud|46.085584|13.324322
resia|ud|46.347373|13.336066
resiutta|ud|46.393086|13.218578
rigolato|ud|46.552831|12.85211
rive d'arcano|ud|46.126065|13.031579
ronchis|ud|45.80661|12.997825
ruda|ud|45.838231|13.40193
san daniele del friuli|ud|46.153241|13.009034
san giorgio di nogaro|ud|45.832024|13.211145
san giovanni al natisone|ud|45.97792|13.401081
san leonardo|ud|46.127459|13.538374
san pietro al natisone|ud|46.126725|13.485233
santa maria la longa|ud|45.933052|13.288023
san vito al torre|ud|45.895798|13.370587
san vito di fagagna|ud|46.090621|13.067024
sauris|ud|46.465366|12.708717
savogna|ud|46.180223|13.552163
sedegliano|ud|46.014371|12.97743
socchieve|ud|46.39707|12.848054
stregna|ud|46.131549|13.603598
sutrio|ud|46.512002|12.990757
taipana|ud|46.258683|13.345916
talmassons|ud|45.929558|13.117064
tarcento|ud|46.215273|13.221658
tarvisio|ud|46.505262|13.578373
tavagnacco|ud|46.127394|13.2141
terzo d'aquileia|ud|45.800855|13.345754
tolmezzo|ud|46.429503|13.056167
torreano|ud|46.128525|13.431132
torviscosa|ud|45.822216|13.276933
trasaghis|ud|46.28218|13.075445
treppo grande|ud|46.190624|13.158124
tricesimo|ud|46.160562|13.212536
trivignano udinese|ud|45.943415|13.340641
udine|ud|46.063463|13.235838
varmo|ud|45.887015|12.987726
venzone|ud|46.333676|13.1394
verzegnis|ud|46.385147|12.987202
villa santina|ud|46.414096|12.920748
visco|ud|45.892172|13.346861
zuglio|ud|46.461796|13.02604
forgaria nel friuli|ud|46.222822|12.973486
campolongo tapogliano|ud|45.86849|13.403755
rivignano teor|ud|45.8613|13.047569
sappada|ud|46.567794|12.68638
fiumicello villa vicentina|ud|45.783608|13.413019
treppo ligosullo|ud|46.544332|13.058904
capriva del friuli|go|45.941416|13.513629
cormons|go|45.957791|13.470954
doberdò del lago|go|45.84463|13.540142
dolegna del collio|go|46.031323|13.479335
farra d'isonzo|go|45.90802|13.516538
fogliano redipuglia|go|45.856486|13.492933
gorizia|go|45.944128|13.625229
gradisca d'isonzo|go|45.889551|13.500329
grado|go|45.677692|13.386443
mariano del friuli|go|45.916061|13.458748
medea|go|45.917715|13.422532
monfalcone|go|45.804945|13.533059
moraro|go|45.930571|13.49653
mossa|go|45.938593|13.54824
romans d'isonzo|go|45.890606|13.438219
ronchi dei legionari|go|45.827808|13.501845
sagrado|go|45.875843|13.485523
san canzian d'isonzo|go|45.798617|13.466138
san floriano del collio|go|45.983108|13.587083
san lorenzo isontino|go|45.931217|13.528549
san pier d'isonzo|go|45.846918|13.460137
savogna d'isonzo|go|45.905882|13.574864
staranzano|go|45.805821|13.50026
turriaco|go|45.819981|13.446686
villesse|go|45.863908|13.44023
duino aurisina|ts|45.770377|13.675305
monrupino|ts|45.717812|13.797445
muggia|ts|45.595848|13.782856
san dorligo della valle|ts|45.612602|13.853506
sgonico|ts|45.736024|13.748245
trieste|ts|45.650481|13.793126
andreis|pn|46.2005|12.61367
arba|pn|46.14498|12.7914
aviano|pn|46.068544|12.589114
azzano decimo|pn|45.88142|12.714314
barcis|pn|46.190251|12.55821
brugnera|pn|45.90351|12.525989
budoia|pn|46.042962|12.532586
caneva|pn|45.968662|12.448097
casarsa della delizia|pn|45.955937|12.842693
castelnovo del friuli|pn|46.213832|12.883104
cavasso nuovo|pn|46.196556|12.770308
chions|pn|45.847186|12.711062
cimolais|pn|46.287639|12.438053
claut|pn|46.266675|12.515709
clauzetto|pn|46.229901|12.915554
cordenons|pn|46.003731|12.718653
cordovado|pn|45.845777|12.880665
erto e casso|pn|46.277092|12.359555
fanna|pn|46.187118|12.753083
fiume veneto|pn|45.927943|12.732176
fontanafredda|pn|45.973397|12.569123
frisanco|pn|46.212776|12.727062
maniago|pn|46.170974|12.707399
meduno|pn|46.216779|12.785867
montereale valcellina|pn|46.160509|12.662136
morsano al tagliamento|pn|45.858141|12.929336
pasiano di pordenone|pn|45.849754|12.626509
pinzano al tagliamento|pn|46.183104|12.945448
polcenigo|pn|46.0307|12.501626
porcia|pn|45.946555|12.601781
pordenone|pn|45.95625|12.65972
prata di pordenone|pn|45.893311|12.596294
pravisdomini|pn|45.818909|12.694425
roveredo in piano|pn|46.011058|12.620291
sacile|pn|45.95386|12.503368
san giorgio della richinvelda|pn|46.039508|12.882333
san martino al tagliamento|pn|46.020829|12.863761
san quirino|pn|46.036167|12.68004
san vito al tagliamento|pn|45.914504|12.856596
sequals|pn|46.165522|12.828776
sesto al reghena|pn|45.847642|12.815531
spilimbergo|pn|46.111335|12.901629
tramonti di sopra|pn|46.308862|12.788497
tramonti di sotto|pn|46.28493|12.795646
travesio|pn|46.196536|12.870994
vito d'asio|pn|46.227815|12.938303
vivaro|pn|46.078002|12.775846
zoppola|pn|45.966254|12.771564
vajont|pn|46.146136|12.697447
valvasone arzene|pn|45.995211|12.86631
airole|im|43.871038|7.554181
apricale|im|43.880389|7.66021
aquila d'arroscia|im|44.085927|8.005247
armo|im|44.087965|7.914783
aurigo|im|43.982724|7.922447
badalucco|im|43.915557|7.847295
bajardo|im|43.903197|7.716921
bordighera|im|43.785051|7.659109
borghetto d'arroscia|im|44.057726|7.981747
borgomaro|im|43.974924|7.945147
camporosso|im|43.815101|7.628418
caravonica|im|43.993025|7.958047
castellaro|im|43.865221|7.868748
castel vittorio|im|43.928557|7.674434
ceriana|im|43.880703|7.774564
cervo|im|43.925858|8.113929
cesio|im|44.007296|7.973536
chiusanico|im|43.972565|7.99088
chiusavecchia|im|43.968424|7.983548
cipressa|im|43.851016|7.93095
civezza|im|43.879822|7.951649
cosio d'arroscia|im|44.077122|7.833008
costarainera|im|43.855421|7.938749
diano arentino|im|43.949625|8.04155
diano castello|im|43.923741|8.067186
diano marina|im|43.908014|8.066675
diano san pietro|im|43.931424|8.071651
dolceacqua|im|43.851718|7.622643
dolcedo|im|43.907439|7.951098
imperia|im|43.958375|7.866743
isolabona|im|43.878988|7.639846
lucinasco|im|43.968021|7.96398
mendatica|im|44.076379|7.805641
molini di triora|im|43.990607|7.774463
montegrosso pian latte|im|44.066889|7.817209
olivetta san michele|im|43.869399|7.523978
ospedaletti|im|43.80208|7.718166
perinaldo|im|43.86744|7.666548
pietrabruna|im|43.889722|7.902948
pieve di teco|im|44.047026|7.914946
pigna|im|43.932268|7.662644
pompeiana|im|43.853721|7.889848
pontedassio|im|43.940324|8.01385
pornassio|im|44.069878|7.871289
prelà|im|43.926158|7.936869
ranzo|im|44.059627|8.014947
rezzo|im|44.021425|7.871445
riva ligure|im|43.837921|7.879649
rocchetta nervina|im|43.890619|7.599642
san bartolomeo al mare|im|43.922924|8.101152
san biagio della cima|im|43.818242|7.64882
san lorenzo al mare|im|43.854711|7.963708
sanremo|im|43.819825|7.774883
santo stefano al mare|im|43.838321|7.893249
seborga|im|43.826419|7.694545
soldano|im|43.829118|7.654844
taggia|im|43.869607|7.836458
terzorio|im|43.852321|7.898349
triora|im|43.992523|7.766243
vallebona|im|43.812718|7.666544
vallecrosia|im|43.788827|7.643411
vasia|im|43.932023|7.952948
ventimiglia|im|43.791762|7.603313
vessalico|im|44.045937|7.960853
villa faraldi|im|43.968528|8.085229
montalto carpasio|im|43.947721|7.861919
alassio|sv|44.008011|8.173027
albenga|sv|44.04925|8.213124
albissola marina|sv|44.327279|8.503102
albisola superiore|sv|44.339838|8.509353
altare|sv|44.335982|8.339931
andora|sv|43.951929|8.145649
arnasco|sv|44.078228|8.107449
balestrino|sv|44.12513|8.17152
bardineto|sv|44.193123|8.132452
bergeggi|sv|44.249735|8.443353
boissano|sv|44.135768|8.221754
borghetto santo spirito|sv|44.114446|8.235328
borgio verezzi|sv|44.164968|8.306662
bormida|sv|44.278261|8.234199
cairo montenotte|sv|44.399268|8.274561
calice ligure|sv|44.204933|8.295351
calizzano|sv|44.236881|8.11447
carcare|sv|44.357473|8.290162
casanova lerrone|sv|44.032116|8.050223
castelbianco|sv|44.111817|8.070908
castelvecchio di rocca barbena|sv|44.129829|8.116648
celle ligure|sv|44.346322|8.544321
cengio|sv|44.390259|8.207952
ceriale|sv|44.091629|8.223251
cisano sul neva|sv|44.086402|8.147418
cosseria|sv|44.368314|8.234866
dego|sv|44.443038|8.305947
erli|sv|44.137529|8.103148
finale ligure|sv|44.168905|8.343652
garlenda|sv|44.034227|8.09565
giustenice|sv|44.172551|8.247188
giusvalla|sv|44.447939|8.394149
laigueglia|sv|43.978826|8.157351
loano|sv|44.128754|8.259371
magliolo|sv|44.191486|8.250396
mallare|sv|44.290982|8.296649
massimino|sv|44.299575|8.07062
millesimo|sv|44.367536|8.201746
mioglia|sv|44.49046|8.412473
murialdo|sv|44.31658|8.164297
nasino|sv|44.11375|8.031713
noli|sv|44.205883|8.416419
onzo|sv|44.070289|8.050019
orco feglino|sv|44.221643|8.323557
ortovero|sv|44.053427|8.096649
osiglia|sv|44.282632|8.202144
pallare|sv|44.327275|8.275435
piana crixia|sv|44.486179|8.306101
pietra ligure|sv|44.152931|8.286552
plodio|sv|44.351194|8.256452
pontinvrea|sv|44.444626|8.43446
quiliano|sv|44.292536|8.412652
rialto|sv|44.225818|8.263108
roccavignale|sv|44.347172|8.164682
sassello|sv|44.47897|8.490171
savona|sv|44.233424|8.252573
spotorno|sv|44.227257|8.419177
stella|sv|44.404721|8.501565
stellanello|sv|44.00042|8.058039
testico|sv|44.005626|8.029249
toirano|sv|44.12753|8.20485
tovo san giacomo|sv|44.175932|8.270451
urbe|sv|44.477256|8.631393
vado ligure|sv|44.27144|8.43822
varazze|sv|44.3593|8.575327
vendone|sv|44.074706|8.07393
vezzi portio|sv|44.229643|8.365219
villanova d'albenga|sv|44.046836|8.142846
zuccarello|sv|44.111029|8.115849
arenzano|ge|44.402518|8.683132
avegno|ge|44.391336|9.156316
bargagli|ge|44.447802|9.087661
bogliasco|ge|44.379056|9.067951
borzonasca|ge|44.422248|9.38657
busalla|ge|44.573483|8.9433
camogli|ge|44.349247|9.158863
campo ligure|ge|44.537644|8.698153
campomorone|ge|44.507337|8.89011
carasco|ge|44.349756|9.34565
casarza ligure|ge|44.273546|9.449771
casella|ge|44.535586|8.996786
castiglione chiavarese|ge|44.27409|9.514675
ceranesi|ge|44.51127|8.878941
chiavari|ge|44.316449|9.323338
cicagna|ge|44.409323|9.235533
cogoleto|ge|44.389342|8.64286
cogorno|ge|44.333081|9.372461
coreglia ligure|ge|44.3829|9.264314
crocefieschi|ge|44.579748|9.023965
davagna|ge|44.466953|9.090958
fascia|ge|44.571968|9.23564
favale di malvaro|ge|44.452997|9.25956
fontanigorda|ge|44.54605|9.304466
genova|ge|44.40726|8.933862
gorreto|ge|44.604846|9.291264
isola del cantone|ge|44.645644|8.957558
lavagna|ge|44.312154|9.341706
leivi|ge|44.354465|9.315179
lorsica|ge|44.445276|9.284311
lumarzo|ge|44.452888|9.13262
masone|ge|44.501336|8.716348
mele|ge|44.44755|8.747769
mezzanego|ge|44.392008|9.407722
mignanego|ge|44.54833|8.915657
moconesi|ge|44.4376|9.214708
moneglia|ge|44.2385|9.489977
montebruno|ge|44.525742|9.247567
montoggio|ge|44.515436|9.044274
ne|ge|44.351926|9.440257
neirone|ge|44.454664|9.190511
orero|ge|44.417212|9.289038
pieve ligure|ge|44.376248|9.086161
portofino|ge|44.303665|9.209345
propata|ge|44.56557|9.18594
rapallo|ge|44.351147|9.229671
recco|ge|44.36524|9.1463
rezzoaglio|ge|44.525997|9.387267
ronco scrivia|ge|44.613299|8.952368
rondanina|ge|44.563245|9.218071
rossiglione|ge|44.563961|8.668513
rovegno|ge|44.576748|9.279162
san colombano certenoli|ge|44.399253|9.328805
santa margherita ligure|ge|44.333976|9.211127
sant'olcese|ge|44.490864|8.97054
santo stefano d'aveto|ge|44.54733|9.450177
savignone|ge|44.564143|8.989596
serra riccò|ge|44.518713|8.951623
sestri levante|ge|44.271453|9.395863
sori|ge|44.373374|9.104447
tiglieto|ge|44.524558|8.619257
torriglia|ge|44.519395|9.158352
tribogna|ge|44.416146|9.195587
uscio|ge|44.413343|9.162563
valbrevenna|ge|44.560577|9.091603
vobbia|ge|44.601245|9.038668
zoagli|ge|44.336762|9.266956
ameglia|sp|44.065929|9.95741
arcola|sp|44.114382|9.905967
beverino|sp|44.205951|9.78573
bolano|sp|44.187649|9.895616
bonassola|sp|44.183546|9.582526
borghetto di vara|sp|44.223847|9.721518
brugnato|sp|44.237097|9.724035
calice al cornoviglio|sp|44.243152|9.836671
carro|sp|44.272824|9.607479
carrodano|sp|44.241962|9.655873
castelnuovo magra|sp|44.099852|10.016942
deiva marina|sp|44.218231|9.520046
follo|sp|44.172831|9.832864
framura|sp|44.208745|9.554544
la spezia|sp|44.238366|9.691233
lerici|sp|44.073607|9.909914
levanto|sp|44.169906|9.61204
maissana|sp|44.337313|9.536328
monterosso al mare|sp|44.146522|9.655558
luni|sp|44.077827|10.030189
pignone|sp|44.178185|9.723508
portovenere|sp|44.05066|9.834458
riccò del golfo di spezia|sp|44.153719|9.763862
riomaggiore|sp|44.098975|9.73867
rocchetta di vara|sp|44.251273|9.757284
santo stefano di magra|sp|44.165099|9.91733
sarzana|sp|44.111872|9.958746
sesta godano|sp|44.293966|9.675716
varese ligure|sp|44.376053|9.592699
vernazza|sp|44.134941|9.682776
vezzano ligure|sp|44.146775|9.876327
zignago|sp|44.297246|9.749064
agazzano|pc|44.94437|9.517685
alseno|pc|44.898364|9.961374
besenzone|pc|44.986432|9.954909
bettola|pc|44.776446|9.60628
bobbio|pc|44.769273|9.386281
borgonovo val tidone|pc|45.014362|9.443461
cadeo|pc|44.975365|9.83107
calendasco|pc|45.087265|9.596063
caorso|pc|45.0496|9.874375
carpaneto piacentino|pc|44.915212|9.788095
castell'arquato|pc|44.85118|9.867118
castel san giovanni|pc|45.059067|9.434178
castelvetro piacentino|pc|45.100335|9.986716
cerignale|pc|44.677853|9.350765
coli|pc|44.744756|9.414565
corte brugnatella|pc|44.715767|9.360289
cortemaggiore|pc|44.996353|9.931072
farini|pc|44.712837|9.569286
ferriere|pc|44.644144|9.497457
fiorenzuola d'arda|pc|44.928084|9.910722
gazzola|pc|44.960062|9.548964
gossolengo|pc|44.99996|9.613788
gragnano trebbiense|pc|45.013163|9.568964
gropparello|pc|44.832706|9.729894
lugagnano val d'arda|pc|44.821994|9.828482
monticelli d'ongina|pc|45.089511|9.933165
morfasso|pc|44.722749|9.702861
ottone|pc|44.623249|9.33266
piacenza|pc|44.847635|9.666531
pianello val tidone|pc|44.94666|9.405161
piozzano|pc|44.926261|9.496064
podenzano|pc|44.956363|9.684967
ponte dell'olio|pc|44.867361|9.642268
pontenure|pc|44.999165|9.788569
rivergaro|pc|44.910251|9.596521
rottofreno|pc|45.057464|9.547763
san giorgio piacentino|pc|44.954163|9.736869
san pietro in cerro|pc|45.021367|9.949872
sarmato|pc|45.059814|9.491398
travo|pc|44.86246|9.545366
vernasca|pc|44.799438|9.830668
vigolzone|pc|44.914662|9.669368
villanova sull'arda|pc|45.026005|9.997061
zerba|pc|44.666153|9.287164
ziano piacentino|pc|45.001462|9.40156
alta val tidone|pc|44.919977|9.347182
albareto|pr|44.446551|9.699977
bardi|pr|44.632211|9.731203
bedonia|pr|44.503631|9.629885
berceto|pr|44.510608|9.989655
bore|pr|44.718658|9.792074
borgo val di taro|pr|44.487053|9.765777
busseto|pr|44.980567|10.042075
calestano|pr|44.6007|10.122994
collecchio|pr|44.75202|10.216691
colorno|pr|44.926532|10.377117
compiano|pr|44.496049|9.662079
corniglio|pr|44.4759|10.088926
felino|pr|44.696587|10.237205
fidenza|pr|44.866522|10.061194
fontanellato|pr|44.881666|10.172179
fontevivo|pr|44.858765|10.17518
fornovo di taro|pr|44.691962|10.100985
langhirano|pr|44.613064|10.266114
lesignano de' bagni|pr|44.643428|10.300484
medesano|pr|44.756699|10.140737
monchio delle corti|pr|44.412955|10.12419
montechiarugolo|pr|44.693401|10.42248
neviano degli arduini|pr|44.582365|10.315689
noceto|pr|44.809901|10.17726
palanzano|pr|44.437437|10.194462
parma|pr|44.801368|10.328083
pellegrino parmense|pr|44.733168|9.927556
roccabianca|pr|45.009369|10.219378
sala baganza|pr|44.71395|10.226334
salsomaggiore terme|pr|44.814762|9.979576
san secondo parmense|pr|44.92016|10.229558
solignano|pr|44.613057|9.97548
soragna|pr|44.926266|10.124178
terenzo|pr|44.609958|10.089682
tizzano val parma|pr|44.520544|10.199199
tornolo|pr|44.485251|9.625274
torrile|pr|44.89885|10.339497
traversetolo|pr|44.639212|10.381795
valmozzola|pr|44.585508|9.869151
varano de' melegari|pr|44.68806|10.011479
varsi|pr|44.662157|9.846676
sissa trecasali|pr|44.945594|10.279947
polesine zibello|pr|45.010809|10.098713
sorbolo mezzani|pr|44.900097|10.44216
albinea|re|44.620182|10.601926
bagnolo in piano|re|44.762967|10.673293
baiso|re|44.496827|10.601678
bibbiano|re|44.66343|10.472719
boretto|re|44.90187|10.554287
brescello|re|44.901224|10.515728
cadelbosco di sopra|re|44.763319|10.595182
campagnola emilia|re|44.83927|10.757093
campegine|re|44.781734|10.528419
carpineti|re|44.455961|10.519698
casalgrande|re|44.588009|10.737321
casina|re|44.51026|10.501593
castellarano|re|44.510281|10.72864
castelnovo di sotto|re|44.810863|10.563295
castelnovo ne' monti|re|44.435562|10.403295
cavriago|re|44.694194|10.526898
canossa|re|44.55356|10.411914
correggio|re|44.770561|10.779783
fabbrico|re|44.872091|10.810062
gattatico|re|44.795684|10.444557
gualtieri|re|44.905013|10.630226
guastalla|re|44.921152|10.653998
luzzara|re|44.961655|10.687068
montecchio emilia|re|44.700451|10.446413
novellara|re|44.84297|10.727492
poviglio|re|44.84167|10.541615
quattro castella|re|44.635605|10.472425
reggiolo|re|44.918534|10.804211
reggio nell'emilia|re|44.608664|10.594797
rio saliceto|re|44.81001|10.803099
rolo|re|44.886656|10.858208
rubiera|re|44.653201|10.781081
san martino in rio|re|44.732802|10.783988
san polo d'enza|re|44.625862|10.422287
sant'ilario d'enza|re|44.760505|10.449892
scandiano|re|44.599345|10.687838
toano|re|44.376357|10.561197
vetto|re|44.484158|10.33749
vezzano sul crostolo|re|44.602584|10.546853
viano|re|44.543729|10.62162
villa minozzo|re|44.365056|10.465995
ventasso|re|44.36883|10.26931
bastiglia|mo|44.726969|10.9986
bomporto|mo|44.72977|11.038401
campogalliano|mo|44.690512|10.840734
camposanto|mo|44.790101|11.137808
carpi|mo|44.78357|10.885452
castelfranco emilia|mo|44.595024|11.052901
castelnuovo rangone|mo|44.551334|10.935016
castelvetro di modena|mo|44.503462|10.9433
cavezzo|mo|44.8365|11.029435
concordia sulla secchia|mo|44.913174|10.983297
fanano|mo|44.207737|10.793837
finale emilia|mo|44.834574|11.293905
fiorano modenese|mo|44.539143|10.811848
fiumalbo|mo|44.179951|10.647733
formigine|mo|44.575143|10.847028
frassinoro|mo|44.295555|10.570198
guiglia|mo|44.424132|10.962119
lama mocogno|mo|44.307263|10.731777
maranello|mo|44.525522|10.866361
marano sul panaro|mo|44.455898|10.966384
medolla|mo|44.846673|11.0676
mirandola|mo|44.887207|11.066106
modena|mo|44.538473|10.935961
montecreto|mo|44.245955|10.717602
montefiorino|mo|44.352857|10.625599
montese|mo|44.268506|10.941189
nonantola|mo|44.677608|11.043624
novi di modena|mo|44.893439|10.901254
palagano|mo|44.320957|10.6461
pavullo nel frignano|mo|44.339694|10.834113
pievepelago|mo|44.204754|10.616601
polinago|mo|44.344758|10.723501
prignano sulla secchia|mo|44.43836|10.691899
ravarino|mo|44.72117|11.098403
riolunato|mo|44.229974|10.651565
san cesario sul panaro|mo|44.561136|11.033218
san felice sul panaro|mo|44.839415|11.143165
san possidonio|mo|44.892089|10.996136
san prospero|mo|44.789671|11.0221
sassuolo|mo|44.540977|10.782902
savignano sul panaro|mo|44.480538|11.035109
serramazzoni|mo|44.422462|10.789147
sestola|mo|44.230455|10.771404
soliera|mo|44.738103|10.924393
spilamberto|mo|44.533413|11.024083
vignola|mo|44.478414|11.009093
zocca|mo|44.347264|10.990407
anzola dell'emilia|bo|44.546684|11.193326
argelato|bo|44.64157|11.34781
baricella|bo|44.646165|11.533295
bentivoglio|bo|44.635792|11.417193
bologna|bo|44.493758|11.343031
borgo tossignano|bo|44.27569|11.589844
budrio|bo|44.537863|11.535498
calderara di reno|bo|44.563052|11.271693
camugnano|bo|44.170457|11.088812
casalecchio di reno|bo|44.476018|11.275686
casalfiumanese|bo|44.296864|11.624022
castel d'aiano|bo|44.275859|10.999408
castel del rio|bo|44.214292|11.504082
castel di casio|bo|44.162956|11.035811
castel guelfo di bologna|bo|44.43114|11.67741
castello d'argile|bo|44.681321|11.296169
castel maggiore|bo|44.575685|11.36481
castel san pietro terme|bo|44.40127|11.585499
castenaso|bo|44.50887|11.469811
castiglione dei pepoli|bo|44.142505|11.166508
crevalcore|bo|44.72057|11.149804
dozza|bo|44.359391|11.628879
fontanelice|bo|44.260098|11.559736
gaggio montano|bo|44.196956|10.932908
galliera|bo|44.749163|11.426533
granarolo dell'emilia|bo|44.55321|11.443208
grizzana morandi|bo|44.25736|11.15289
imola|bo|44.353514|11.714123
lizzano in belvedere|bo|44.161829|10.894044
loiano|bo|44.266157|11.325534
malalbergo|bo|44.718674|11.532212
marzabotto|bo|44.340988|11.204782
medicina|bo|44.476674|11.638338
minerbio|bo|44.624199|11.490697
molinella|bo|44.620853|11.670237
monghidoro|bo|44.219859|11.318747
monterenzio|bo|44.326882|11.406144
monte san pietro|bo|44.428404|11.15122
monzuno|bo|44.279263|11.266408
mordano|bo|44.397426|11.810857
ozzano dell'emilia|bo|44.44498|11.47605
pianoro|bo|44.392964|11.342314
pieve di cento|bo|44.713209|11.306598
sala bolognese|bo|44.629603|11.272748
san benedetto val di sambro|bo|44.215259|11.232914
san giorgio di piano|bo|44.646793|11.375449
san giovanni in persiceto|bo|44.638978|11.187131
san lazzaro di savena|bo|44.471567|11.404859
san pietro in casale|bo|44.699052|11.402926
sant'agata bolognese|bo|44.664169|11.134004
sasso marconi|bo|44.399632|11.247554
vergato|bo|44.28266|11.10731
zola predosa|bo|44.489665|11.218309
valsamoggia|bo|44.439824|11.059275
alto reno terme|bo|44.123954|10.937306
argenta|fe|44.614338|11.833842
bondeno|fe|44.886177|11.410807
cento|fe|44.72744|11.290303
codigoro|fe|44.831581|12.109524
comacchio|fe|44.696379|12.180427
copparo|fe|44.894183|11.827889
ferrara|fe|44.766764|11.827939
jolanda di savoia|fe|44.882408|11.979349
lagosanto|fe|44.76198|12.142825
masi torello|fe|44.794578|11.801817
mesola|fe|44.920782|12.229224
ostellato|fe|44.745678|11.943521
poggio renatico|fe|44.765974|11.490111
portomaggiore|fe|44.699258|11.804078
vigarano mainarda|fe|44.843422|11.494436
voghiera|fe|44.759074|11.751132
goro|fe|44.850883|12.294927
fiscaglia|fe|44.794133|12.029515
terre del reno|fe|44.812155|11.404388
riva del po|fe|44.96047|11.894394
tresignana|fe|44.828937|11.878917
alfonsine|ra|44.505385|12.04118
bagnacavallo|ra|44.416148|11.97655
bagnara di romagna|ra|44.389169|11.826451
brisighella|ra|44.22214|11.772226
casola valsenio|ra|44.223187|11.62311
castel bolognese|ra|44.320487|11.799258
cervia|ra|44.260984|12.349546
conselice|ra|44.512758|11.829174
cotignola|ra|44.383843|11.940217
faenza|ra|44.285556|11.883205
fusignano|ra|44.467503|11.960192
lugo|ra|44.418878|11.908231
massa lombarda|ra|44.447386|11.826091
ravenna|ra|44.364061|12.059009
riolo terme|ra|44.277084|11.72452
russi|ra|44.370054|12.030417
sant'agata sul santerno|ra|44.44217|11.860124
solarolo|ra|44.360368|11.844525
bagno di romagna|fc|43.835227|11.959598
bertinoro|fc|44.148665|12.134235
borghi|fc|44.031591|12.354389
castrocaro terme e terra del sole|fc|44.182365|11.955631
cesena|fc|44.136352|12.242244
cesenatico|fc|44.199928|12.396969
civitella di romagna|fc|44.007171|11.941346
dovadola|fc|44.122574|11.888255
forlì|fc|44.222728|12.041273
forlimpopoli|fc|44.187305|12.125906
galeata|fc|43.996663|11.912006
gambettola|fc|44.116946|12.338764
gatteo|fc|44.109466|12.386042
longiano|fc|44.074378|12.327473
meldola|fc|44.126721|12.061153
mercato saraceno|fc|43.956744|12.195766
modigliana|fc|44.161163|11.793528
montiano|fc|44.082965|12.30444
portico e san benedetto|fc|44.003714|11.720184
predappio|fc|44.102215|11.98162
premilcuore|fc|43.975908|11.777527
rocca san casciano|fc|44.059096|11.842061
roncofreddo|fc|44.04224|12.31826
san mauro pascoli|fc|44.106463|12.414898
santa sofia|fc|43.947267|11.908451
sarsina|fc|43.918758|12.143258
savignano sul rubicone|fc|44.092512|12.399131
sogliano al rubicone|fc|44.004663|12.300541
tredozio|fc|44.081042|11.744208
verghereto|fc|43.792956|12.003938
bellaria-igea marina|rn|44.142108|12.469694
cattolica|rn|43.963993|12.744152
coriano|rn|43.965186|12.602374
gemmano|rn|43.904162|12.583058
misano adriatico|rn|43.977495|12.698695
mondaino|rn|43.855706|12.671392
montefiore conca|rn|43.890236|12.610315
montegridolfo|rn|43859|12.688511
morciano di romagna|rn|43.913748|12.646067
riccione|rn|43.998274|12.647437
rimini|rn|43.947098|12.630769
saludecio|rn|43.872685|12.668507
san clemente|rn|43.932205|12.625693
san giovanni in marignano|rn|43.938668|12.712494
santarcangelo di romagna|rn|44.063382|12.446702
verucchio|rn|43.983611|12.421163
casteldelci|rn|43.79115|12.155126
maiolo|rn|43.87396|12.311144
novafeltria|rn|43.895727|12.290398
pennabilli|rn|43.817054|12.265276
san leo|rn|43.896961|12.343644
sant'agata feltria|rn|43.864302|12.207999
talamello|rn|43.90499|12.285898
poggio torriana|rn|43.999848|12.39266
montescudo-monte colombo|rn|43.931376|12.54223
aulla|ms|44.214548|9.967787
bagnone|ms|44.314739|9.994655
carrara|ms|44.07918|10.102353
casola in lunigiana|ms|44.199961|10.17546
comano|ms|44.29354|10.131165
filattiera|ms|44.330791|9.934823
fivizzano|ms|44.23835|10.12599
fosdinovo|ms|44.13382|10.018869
licciana nardi|ms|44.2649|10.038437
massa|ms|44.035932|10.139552
montignoso|ms|44.02405|10.172591
mulazzo|ms|44.315926|9.889626
podenzana|ms|44.206248|9.941986
pontremoli|ms|44.375151|9.878882
tresana|ms|44.254248|9.912885
villafranca in lunigiana|ms|44.29467|9.949656
zeri|ms|44.353717|9.763228
altopascio|lu|43.815041|10.674396
bagni di lucca|lu|44.009982|10.590633
barga|lu|44.073918|10.484263
borgo a mozzano|lu|43.978948|10.545703
camaiore|lu|43.938061|10.303955
camporgiano|lu|44.158056|10.335151
capannori|lu|43.842039|10.573525
careggine|lu|44.119949|10.325396
castelnuovo di garfagnana|lu|44.110022|10.411786
castiglione di garfagnana|lu|44.14995|10.410897
coreglia antelminelli|lu|44.063653|10.525509
forte dei marmi|lu|43.959589|10.169939
fosciandora|lu|44.116476|10.459489
gallicano|lu|44.060651|10.435668
lucca|lu|44.017764|10.45443
massarosa|lu|43.86765|10.339716
minucciano|lu|44.170449|10.207592
molazzana|lu|44.071749|10.417799
montecarlo|lu|43.851186|10.667621
pescaglia|lu|43.966246|10.4111
piazza al serchio|lu|44.183484|10.300924
pietrasanta|lu|43.958903|10.233203
pieve fosciana|lu|44.13227|10.410797
porcari|lu|43.840902|10.617621
san romano in garfagnana|lu|44.169669|10.347051
seravezza|lu|43.994721|10.225772
stazzema|lu|44.011555|10.313534
vagli sotto|lu|44.109248|10.288795
viareggio|lu|43.867239|10.250605
villa basilica|lu|43.927516|10.644575
villa collemandina|lu|44.158759|10.397742
fabbriche di vergemoli|lu|44.014836|10.391052
sillano giuncugnano|lu|44.254506|10.315717
agliana|pt|43.903816|11.003396
buggiano|pt|43.899935|10.726569
lamporecchio|pt|43.81638|10.896283
larciano|pt|43.82504|10.856367
marliana|pt|43.9337|10.769276
massa e cozzile|pt|43.91741|10.753727
monsummano terme|pt|43.870744|10.814016
montale|pt|43.93524|11.015879
montecatini-terme|pt|43.883179|10.770953
pescia|pt|43.90171|10.689781
pieve a nievole|pt|43.879112|10.796112
pistoia|pt|43.974096|10.868708
ponte buggianese|pt|43.840935|10.747548
quarrata|pt|43.8475|10.977701
sambuca pistoiese|pt|44.054873|11.002508
serravalle pistoiese|pt|43.90585|10.83302
uzzano|pt|43.892628|10.706057
chiesina uzzanese|pt|43.8391|10.719342
abetone cutigliano|pt|44.124864|10.711228
san marcello piteglio|pt|44.067673|10.812002
bagno a ripoli|fi|43.752172|11.319995
barberino di mugello|fi|44.000541|11.238381
borgo san lorenzo|fi|43.954248|11.386468
calenzano|fi|43.865249|11.167368
campi bisenzio|fi|43.821634|11.136678
capraia e limite|fi|43.759563|10.9997
castelfiorentino|fi|43.605607|10.971248
cerreto guidi|fi|43.758922|10.879013
certaldo|fi|43.547853|11.041199
dicomano|fi|43.891962|11.52365
empoli|fi|43.719488|10.945856
fiesole|fi|43.806669|11.293074
firenze|fi|43.769871|11.255576
firenzuola|fi|44.119079|11.379227
fucecchio|fi|43.729202|10.808205
gambassi terme|fi|43.539113|10.954075
greve in chianti|fi|43.58246|11.316873
impruneta|fi|43.685345|11.254664
lastra a signa|fi|43.770985|11.107215
londa|fi|43.860567|11.566812
marradi|fi|44.075738|11.612896
montaione|fi|43.552937|10.912773
montelupo fiorentino|fi|43.733153|11.020877
montespertoli|fi|43.64355|11.075141
palazzuolo sul senio|fi|44.112297|11.54774
pelago|fi|43.771824|11.50463
pontassieve|fi|43.775426|11.439991
reggello|fi|43.683088|11.536896
rignano sull'arno|fi|43.72105|11.451427
rufina|fi|43.826668|11.490078
san casciano in val di pesa|fi|43.65696|11.185668
san godenzo|fi|43.925556|11.618428
scandicci|fi|43.75671|11.184762
sesto fiorentino|fi|43.831721|11.199224
signa|fi|43.780693|11.096453
vaglia|fi|43.907149|11.281814
vicchio|fi|43.933235|11.46529
vinci|fi|43.787397|10.927106
figline e incisa valdarno|fi|43.630795|11.462946
scarperia e san piero|fi|44.010965|11.33135
barberino tavarnelle|fi|43.545562|11.204681
bibbona|li|43.269784|10.59727
campiglia marittima|li|43.060628|10.615496
campo nell'elba|li|42.758772|10.19897
capoliveri|li|42.743452|10.37878
capraia isola|li|43.04818|9.843401
castagneto carducci|li|43.160742|10.611006
cecina|li|43.311345|10.517344
collesalvetti|li|43.588912|10.476264
livorno|li|42.790219|10.340281
marciana|li|42.789422|10.168535
marciana marina|li|42.803608|10.196768
piombino|li|42.923166|10.526819
porto azzurro|li|42.766298|10.395753
portoferraio|li|42.811582|10.313053
rosignano marittimo|li|43.406786|10.473366
san vincenzo|li|43.100695|10.538791
sassetta|li|43.128991|10.643908
suvereto|li|43.079015|10.678681
rio|li|42.832548|10.411777
bientina|pi|43.710184|10.620321
buti|pi|43.727302|10.587717
calci|pi|43.725799|10.515281
calcinaia|pi|43.683201|10.615308
capannoli|pi|43.586731|10.674457
casale marittimo|pi|43.297061|10.615308
cascina|pi|43.676674|10.549224
castelfranco di sotto|pi|43.699715|10.745178
castellina marittima|pi|43.412569|10.576177
castelnuovo di val di cecina|pi|43.207998|10.906051
chianni|pi|43.48612|10.642362
fauglia|pi|43.570638|10.514409
guardistallo|pi|43.312708|10.63284
lajatico|pi|43.475418|10.728284
montecatini val di cecina|pi|43.392816|10.749093
montescudaio|pi|43.326278|10.626995
monteverdi marittimo|pi|43.177439|10.714408
montopoli in val d'arno|pi|43.670299|10.760832
orciano pisano|pi|43.494436|10.51151
palaia|pi|43.605041|10.772014
peccioli|pi|43.548027|10.720848
pisa|pi|43.71594|10.401862
pomarance|pi|43.299237|10.872401
ponsacco|pi|43.620713|10.630829
pontedera|pi|43.663454|10.635485
riparbella|pi|43.364625|10.599529
san giuliano terme|pi|43.762996|10.44104
san miniato|pi|43.67975|10.850095
santa croce sull'arno|pi|43.710108|10.781941
santa luce|pi|43.472336|10.563512
santa maria a monte|pi|43.698064|10.692998
terricciola|pi|43.524738|10.680114
vecchiano|pi|43.783342|10.384403
vicopisano|pi|43.699832|10.584695
volterra|pi|43.400337|10.860019
casciana terme lari|pi|43.552159|10.594206
crespina lorenzana|pi|43.587912|10.554653
anghiari|ar|43.540966|12.054582
arezzo|ar|43.517202|11.763879
badia tedalda|ar|43.709208|12.184876
bibbiena|ar|43.695548|11.817341
bucine|ar|43.479199|11.616975
capolona|ar|43.563998|11.858786
caprese michelangelo|ar|43.639852|11.98484
castel focognano|ar|43.654028|11.788439
castel san niccolò|ar|43.722917|11.656598
castiglion fibocchi|ar|43.527965|11.763176
castiglion fiorentino|ar|43.341715|11.92378
cavriglia|ar|43.521874|11.488972
chitignano|ar|43.662352|11.881338
chiusi della verna|ar|43.695024|11.938632
civitella in val di chiana|ar|43.402527|11.735586
cortona|ar|43.275298|11.985062
foiano della chiana|ar|43.252454|11.81876
loro ciuffenna|ar|43.592286|11.633007
lucignano|ar|43.274463|11746
marciano della chiana|ar|43.305227|11.786366
montemignaio|ar|43.739895|11.620012
monterchi|ar|43.485631|12.111681
monte san savino|ar|43.331423|11.725351
montevarchi|ar|43.5235|11.567591
ortignano raggiolo|ar|43.672777|11.715607
pieve santo stefano|ar|43.669357|12.041589
poppi|ar|43.72276|11.76607
san giovanni valdarno|ar|43.56493|11.530595
sansepolcro|ar|43.570078|12.140377
sestino|ar|43.70835|12.296534
subbiano|ar|43.574848|11.870369
talla|ar|43.601541|11.78802
terranuova bracciolini|ar|43.551208|11.585827
castelfranco piandiscò|ar|43.639512|11.564623
pratovecchio stia|ar|43.822039|11.699291
laterina pergine valdarno|ar|43.492534|11.68808
abbadia san salvatore|si|42.880972|11.67242
asciano|si|43.234857|11.55946
buonconvento|si|43.137583|11.48132
casole d'elsa|si|43.341697|11.042949
castellina in chianti|si|43.469179|11.287286
castelnuovo berardenga|si|43.346341|11.503334
castiglione d'orcia|si|43.007117|11.615541
cetona|si|42.964739|11.900411
chianciano terme|si|43.058255|11.828429
chiusdino|si|43.154634|11.084228
chiusi|si|43.016047|11.947524
colle di val d'elsa|si|43.422646|11.126394
gaiole in chianti|si|43.468295|11.4342
montepulciano|si|43.092723|11.780972
monteriggioni|si|43.390059|11.223389
monteroni d'arbia|si|43.229334|11.420933
monticiano|si|43.13967|11.180126
murlo|si|43.160765|11.387924
piancastagnaio|si|42.848967|11.692881
pienza|si|43.076546|11.678908
poggibonsi|si|43.465028|11.148337
radda in chianti|si|43.487052|11.3747
radicofani|si|42.896507|11.769706
radicondoli|si|43.26125|11.042082
rapolano terme|si|43.287251|11.602927
san casciano dei bagni|si|42.871756|11.874953
san gimignano|si|43.467799|11.043212
san quirico d'orcia|si|43.059335|11.604409
sarteano|si|42.989911|11.868732
siena|si|43.167225|11.467181
sinalunga|si|43.212124|11.736483
sovicille|si|43.280602|11.228176
torrita di siena|si|43.16676|11.771647
trequanda|si|43.188939|11.667385
montalcino|si|43.0583|11.490066
arcidosso|gr|42.871631|11.538343
campagnatico|gr|42.883729|11.273537
capalbio|gr|42.454879|11.419515
castel del piano|gr|42.893235|11.53838
castell'azzara|gr|42.77163|11.699248
castiglione della pescaia|gr|42.76393|10.879092
cinigiano|gr|42.890407|11.392892
civitella paganico|gr|43.024126|11.28931
follonica|gr|42.926326|10.761625
gavorrano|gr|42.925374|10.905803
grosseto|gr|42.77511|11.287804
isola del giglio|gr|42.353169|10.896463
magliano in toscana|gr|42.599442|11.291739
manciano|gr|42.587336|11.515988
massa marittima|gr|43.050103|10.888059
monte argentario|gr|42.407352|11.150384
montieri|gr|43.130677|11.016279
orbetello|gr|42.438094|11.210743
pitigliano|gr|42.634827|11.673549
roccalbegna|gr|42.785975|11.507682
roccastrada|gr|43.009931|11.166532
santa fiora|gr|42.831581|11.585866
scansano|gr|42.688369|11.335453
scarlino|gr|42.907402|10.851975
seggiano|gr|42.928832|11.558242
sorano|gr|42.681466|11.713572
monterotondo marittimo|gr|43.145553|10.855834
semproniano|gr|42.730658|11.540346
cantagallo|po|44.022193|11.079774
carmignano|po|43.814065|11.01903
montemurlo|po|43.926966|11.03744
poggio a caiano|po|43.816089|11.05465
prato|po|43.935718|11.094147
vaiano|po|43.969012|11.124072
vernio|po|44.044051|11.14988
assisi|pg|43.071195|12.614667
bastia umbra|pg|43.067755|12.549582
bettona|pg|43.013614|12.484756
bevagna|pg|42.934075|12.609121
campello sul clitunno|pg|42.822868|12.776263
cannara|pg|42.994742|12.579363
cascia|pg|42.717008|13.013502
castel ritaldi|pg|42.82326|12.672287
castiglione del lago|pg|43.127141|12.04525
cerreto di spoleto|pg|42.821042|12.917629
citerna|pg|43.498407|12.115516
città della pieve|pg|42.952734|12.004326
città di castello|pg|43.457421|12.240312
collazzone|pg|42.900468|12.435186
corciano|pg|43.129027|12.287718
costacciaro|pg|43.360409|12.712378
deruta|pg|42.98229|12.419588
foligno|pg|42.956183|12.703334
fossato di vico|pg|43.297628|12.75988
fratta todina|pg|42.857978|12.364672
giano dell'umbria|pg|42.833467|12.577711
gualdo cattaneo|pg|42.909409|12.555816
gualdo tadino|pg|43.231652|12.780962
gubbio|pg|43.351788|12.577267
lisciano niccone|pg|43.246844|12.14305
magione|pg|43.142737|12.203496
marsciano|pg|42.910028|12.337042
massa martana|pg|42.773137|12.524966
monte castello di vibio|pg|42.840694|12.352286
montefalco|pg|42.891874|12.650067
monteleone di spoleto|pg|42.651006|12.95156
monte santa maria tiberina|pg|43.437781|12.162544
montone|pg|43.362337|12.326328
nocera umbra|pg|43.113619|12.788608
norcia|pg|42.792412|13.092928
paciano|pg|43.022434|12.070523
panicale|pg|43.029059|12.098364
passignano sul trasimeno|pg|43.184742|12.136838
perugia|pg|43.107032|12.402996
piegaro|pg|42.969772|12.086347
pietralunga|pg|43.442205|12.437194
poggiodomo|pg|42.711239|12.933876
preci|pg|42.878489|13.039082
san giustino|pg|43.54898|12.17583
sant'anatolia di narco|pg|42.733758|12.835586
scheggia e pascelupo|pg|43.418266|12.712678
scheggino|pg|42.712556|12.831366
sellano|pg|42.888409|12.926329
sigillo|pg|43.33135|12.742284
spello|pg|42.990945|12.671775
spoleto|pg|42.735512|12.736336
todi|pg|42.782443|12.406255
torgiano|pg|43.028542|12.44136
trevi|pg|42.877497|12.747455
tuoro sul trasimeno|pg|43.206848|12.074615
umbertide|pg|43.305636|12.336622
valfabbrica|pg|43.158897|12.600896
vallo di nera|pg|42.754621|12.86376
valtopina|pg|43.058283|12.754656
acquasparta|tr|42.691172|12.546451
allerona|tr|42.812033|11.973053
alviano|tr|42.588208|12.295936
amelia|tr|42.553528|12.416776
arrone|tr|42.583433|12.769865
attigliano|tr|42.514334|12.292508
baschi|tr|42.670284|12.216135
calvi dell'umbria|tr|42.403124|12.567432
castel giorgio|tr|42.707894|11.979391
castel viscardo|tr|42.754674|12.001303
fabro|tr|42.86348|12.012882
ferentillo|tr|42.620768|12.788726
ficulle|tr|42.836274|12.065609
giove|tr|43.081478|12.775591
guardea|tr|42.623382|12.297664
lugnano in teverina|tr|42.574226|12.330785
montecastrilli|tr|42.652434|12.488567
montecchio|tr|42.663533|12.286862
montefranco|tr|42.597735|12.76526
montegabbione|tr|42.921006|12.092364
monteleone d'orvieto|tr|42.918494|12.051145
narni|tr|42.519589|12.515226
orvieto|tr|42.718615|12.108791
otricoli|tr|42.423023|12.477686
parrano|tr|42.863933|12.106128
penna in teverina|tr|42.492952|12.355648
polino|tr|42.584824|12.842802
porano|tr|42.68171|12.102062
san gemini|tr|42.612701|12.546505
san venanzo|tr|42.869337|12.269859
stroncone|tr|42.502332|12.659973
terni|tr|42.653752|12.439812
avigliano umbro|tr|42.654834|12.426165
acqualagna|pu|43.625884|12.675974
apecchio|pu|43.558554|12.420951
belforte all'isauro|pu|43.716553|12.376048
borgo pace|pu|43.657084|12.293588
cagli|pu|43.545737|12.651788
cantiano|pu|43.474309|12.628815
carpegna|pu|43.781434|12.336446
cartoceto|pu|43.765663|12.883458
fano|pu|43.842417|13.014663
fermignano|pu|43.676758|12.645373
fossombrone|pu|43.690797|12.812612
fratte rosa|pu|43.631954|12.901843
frontino|pu|43.764558|12.376747
frontone|pu|43.513723|12.734482
gabicce mare|pu|43.966874|12.75631
gradara|pu|43.938866|12.771253
isola del piano|pu|43.737761|12.784057
lunano|pu|43.730475|12.441417
macerata feltria|pu|43.803364|12.443415
mercatello sul metauro|pu|43.647355|12.336648
mercatino conca|pu|43.870162|12.491148
mombaroccio|pu|43.795063|12.854757
mondavio|pu|43.674205|12.968771
mondolfo|pu|43.750769|13.096891
montecalvo in foglia|pu|43.811562|12.631052
monte cerignone|pu|43.84046|12.414747
montecopiolo|rn|43.841314|12.370434
montefelcino|pu|43.734761|12.833958
monte grimano terme|pu|43.867661|12.471247
montelabbate|pu|43.848864|12.787955
monte porzio|pu|43.689697|13.04569
peglio|pu|43.696458|12.497851
pergola|pu|43.56321|12.836317
pesaro|pu|43.909811|12.913123
petriano|pu|43.779162|12.733355
piandimeleto|pu|43.724158|12.411048
pietrarubbia|pu|43.797747|12.382665
piobbico|pu|43.589272|12.50943
san costanzo|pu|43.766164|13.072763
san lorenzo in campo|pu|43.604117|12.945724
sant'angelo in vado|pu|43.663161|12.405361
sant'ippolito|pu|43.685715|12.871689
sassofeltrio|rn|43.891962|12.510183
serra sant'abbondio|pu|43.491387|12.772197
tavoleto|pu|43.84385|12.593924
tavullia|pu|43.897756|12.75479
urbania|pu|43.668257|12.524113
urbino|pu|43.726261|12.636314
vallefoglia|pu|43.828405|12.7247
colli al metauro|pu|43.742315|12.908919
terre roveresche|pu|43.700587|12.957708
sassocorvaro auditore|pu|43.810262|12.540175
agugliano|an|43.543375|13.386587
ancona|an|43.48012|13.218791
arcevia|an|43.49945|12.940096
barbara|an|43.581154|13.026481
belvedere ostrense|an|43.580495|13.166127
camerano|an|43.531268|13.551574
camerata picena|an|43.578135|13.352735
castelbellino|an|43.486758|13.144668
castelfidardo|an|43.464574|13.5467
castelleone di suasa|an|43.607765|12.976126
castelplanio|an|43.493058|13.081867
cerreto d'esi|an|43.316789|12.987124
chiaravalle|an|43.602662|13.324571
corinaldo|an|43.648816|13.047659
cupramontana|an|43.445554|13.1156
fabriano|an|43.335841|12.904619
falconara marittima|an|43.629397|13.396676
filottrano|an|43.436659|13.354474
genga|an|43.429655|12.934965
jesi|an|43.52198|13.244376
loreto|an|43.439498|13.607021
maiolati spontini|an|43.477658|13.121768
mergo|an|43.472357|13.036766
monsano|an|43.562561|13.25007
montecarotto|an|43.526311|13.06301
montemarciano|an|43.638863|13.30747
monte roberto|an|43.48135|13.138378
monte san vito|an|43.600562|13.26817
morro d'alba|an|43.601097|13.213846
numana|an|43.510344|13.622637
offagna|an|43.527682|13.441472
osimo|an|43.486136|13.482407
ostra|an|43.612527|13.158061
ostra vetere|an|43.603749|13.058186
poggio san marcello|an|43.511158|13.073667
polverigi|an|43.525561|13.392073
rosora|an|43.483258|13.068867
san marcello|an|43.576061|13.206068
san paolo di jesi|an|43.454558|13.17277
santa maria nuova|an|43.49406|13.311672
sassoferrato|an|43.432655|12.857263
senigallia|an|43.714906|13.217742
serra de' conti|an|43.54331|13.037191
serra san quirico|an|43.447683|13.01386
sirolo|an|43.524689|13.617846
staffolo|an|43.433074|13.1862
trecastelli|an|43.686813|13.109822
apiro|mc|43.392256|13.131157
appignano|mc|43.363732|13.346596
belforte del chienti|mc|43.162552|13.245376
bolognola|mc|42.994056|13.227769
caldarola|mc|43.138534|13.224835
camerino|mc|43.135764|13.068309
camporotondo di fiastrone|mc|43.132451|13.265977
castelraimondo|mc|43.208151|13.053671
castelsantangelo sul nera|mc|42.894492|13.15393
cessapalombo|mc|43.108789|13.258026
cingoli|mc|43.373841|13.216313
civitanova marche|mc|43.305523|13.72293
colmurano|mc|43.16385|13.356982
corridonia|mc|43.248458|13.509212
esanatoglia|mc|43.252208|12.947827
fiastra|mc|43.036141|13.155455
fiuminata|mc|43.18745|12.932168
gagliole|mc|43.238301|13.067304
gualdo|mc|43.066828|13.341016
loro piceno|mc|43.16612|13.416403
macerata|mc|43.152978|13.150882
matelica|mc|43.25656|13.010514
mogliano|mc|43.186461|13.48097
montecassiano|mc|43.363832|13.435709
monte cavallo|mc|42.955117|12.973869
montecosaro|mc|43.317009|13.635306
montefano|mc|43.411159|13.438376
montelupone|mc|43.34419|13.567594
monte san giusto|mc|43.236431|13.595124
monte san martino|mc|43.03195|13.438682
morrovalle|mc|43.314553|13.579508
muccia|mc|43.081701|13.042968
penna san giovanni|mc|43.056915|13.425452
petriolo|mc|43.221693|13.465335
pieve torina|mc|43.043287|13.048911
pioraco|mc|43.17905|12.98567
poggio san vicino|mc|43.375155|13.079069
pollenza|mc|43.267775|13.347792
porto recanati|mc|43.435082|13.663008
potenza picena|mc|43.366409|13.620695
recanati|mc|43.403508|13.548602
ripe san ginesio|mc|43.142983|13.367329
san ginesio|mc|43.53009|12.961252
san severino marche|mc|43.229021|13.177966
sant'angelo in pontano|mc|43.098644|13.396756
sarnano|mc|43.03538|13.29831
sefro|mc|43.147246|12.94863
serrapetrona|mc|43.17743|13.189752
serravalle di chienti|mc|43.071675|12.952386
tolentino|mc|43.208755|13.28441
treia|mc|43.310556|13.311475
urbisaglia|mc|43.196608|13.376571
ussita|mc|42.94376|13.137908
visso|mc|42.931232|13.087449
valfornace|mc|43.066925|13.114974
acquasanta terme|ap|42.769944|13.410185
acquaviva picena|ap|42.944936|13.815114
appignano del tronto|ap|42.89867|13.666387
arquata del tronto|ap|42.772361|13.296583
ascoli piceno|ap|42.88342|13.539593
carassai|ap|43.032052|13.684987
castel di lama|ap|42.869382|13.713294
castignano|ap|42.93935|13.623987
castorano|ap|42.898087|13.728042
colli del tronto|ap|42.876974|13.748355
comunanza|ap|42.956852|13.412872
cossignano|ap|42.984251|13.689988
cupra marittima|ap|43.025023|13.858764
folignano|ap|42.820247|13.634289
force|ap|42.963257|13.490948
grottammare|ap|42.990497|13.86876
maltignano|ap|42.832248|13.68799
massignano|ap|43.05065|13.797209
monsampolo del tronto|ap|42.89695|13.793892
montalto delle marche|ap|42.989851|13.609186
montedinove|ap|42.970733|13.587305
montefiore dell'aso|ap|43.051895|13.75279
montegallo|ap|42.847389|13.343496
montemonaco|ap|42.899155|13.326859
monteprandone|ap|42.919939|13.834816
offida|ap|42.934821|13.691044
palmiano|ap|42.899338|13.458377
ripatransone|ap|42.996178|13.761796
roccafluvione|ap|42.859947|13.476785
rotella|ap|42.954173|13.559359
san benedetto del tronto|ap|42.952533|13.880714
spinetoli|ap|42.889126|13.772983
venarotta|ap|42.882947|13.492285
altidona|fm|43.109225|13.799878
amandola|fm|42.979965|13.356085
belmonte piceno|fm|43.091441|13.539652
campofilone|fm|43.079469|13.814786
falerone|fm|43.107052|13.471682
fermo|fm|43.092249|13.638768
francavilla d'ete|fm|43.187355|13.540682
grottazzolina|fm|43.111711|13.602981
lapedona|fm|43.109981|13.772857
magliano di tenna|fm|43.137954|13.587184
massa fermana|fm|43.149772|13.475407
monsampietro morico|fm|43.067752|13.555684
montappone|fm|43.136453|13.468481
montefalcone appennino|fm|42.990049|13.456883
montefortino|fm|42.943095|13.344831
monte giberto|fm|43.091253|13.630685
montegiorgio|fm|43.132253|13.536383
montegranaro|fm|43.232523|13.63158
monteleone di fermo|fm|43.047904|13.528534
montelparo|fm|43.017838|13.537432
monte rinaldo|fm|43.02826|13.580155
monterubbiano|fm|43.084754|13.719887
monte san pietrangeli|fm|43.189355|13.575383
monte urano|fm|43.202656|13.672885
monte vidon combatte|fm|43.050066|13.631002
monte vidon corrado|fm|43.120653|13.487582
montottone|fm|43.062052|13.590085
moresco|fm|43.085554|13.730988
ortezzano|fm|43.030052|13.605986
pedaso|fm|43.099164|13.840777
petritoli|fm|43.067741|13.659214
ponzano di fermo|fm|43.101077|13.656781
porto san giorgio|fm|43.182656|13.793386
porto sant'elpidio|fm|43.258962|13.756983
rapagnano|fm|43.161253|13.592905
santa vittoria in matenano|fm|43.019379|13.496035
sant'elpidio a mare|fm|43.23474|13.687852
servigliano|fm|43.081621|13.493764
smerillo|fm|43.00515|13.444982
torre san patrizio|fm|43.182855|13.612784
acquapendente|vt|42.743996|11.864988
arlena di castro|vt|42.462824|11.820655
bagnoregio|vt|42.62698|12.090872
barbarano romano|vt|42.249834|12.067374
bassano romano|vt|42.218392|12.193006
bassano in teverina|vt|42.466074|12.313034
blera|vt|42.272522|12.031662
bolsena|vt|42.644107|11.984955
bomarzo|vt|42.481928|12.248764
calcata|vt|42.219726|12.425942
canepina|vt|42.380961|12.233652
canino|vt|42.463963|11.749509
capodimonte|vt|42.546527|11.904755
capranica|vt|42.256492|12.177611
caprarola|vt|42.326625|12.235766
carbognano|vt|42.331625|12.264366
castel sant'elia|vt|42.251776|12.371796
castiglione in teverina|vt|42.644972|12.203898
celleno|vt|42.559768|12.125758
cellere|vt|42.510425|11.771653
civita castellana|vt|42.295226|12.40917
civitella d'agliano|vt|42.605362|12.187658
corchiano|vt|42.344926|12.355668
fabrica di roma|vt|42.335126|12.299767
faleria|vt|42.226179|12.443181
farnese|vt|42.549426|11.725652
gallese|vt|42.373287|12.402804
gradoli|vt|42.643692|11.854888
graffignano|vt|42.574812|12.204398
grotte di castro|vt|42.674529|11.872253
ischia di castro|vt|42.544655|11.754014
latera|vt|42.629028|11.827453
lubriano|vt|42.636231|12.108759
marta|vt|42.533911|11.924912
montalto di castro|vt|42.35346|11.606312
montefiascone|vt|42.537925|12.030997
monte romano|vt|42.266521|11.893759
monterosi|vt|42.195823|12.308469
nepi|vt|42.242824|12.34557
onano|vt|42.692856|11.8164
oriolo romano|vt|42.159303|12.138349
orte|vt|42.460598|12.385606
piansano|vt|42.517932|11.828273
proceno|vt|42.75716|11.830261
ronciglione|vt|42.290242|12.213806
villa san giovanni in tuscia|vt|42.282669|12.052525
san lorenzo nuovo|vt|42.68673|11.906653
soriano nel cimino|vt|42.418761|12.234308
sutri|vt|42.247023|12.215067
tarquinia|vt|42.253239|11.759175
tessennano|vt|42.478206|11.790486
tuscania|vt|42.420214|11.870261
valentano|vt|42.569098|11.81833
vallerano|vt|42.38397|12.26279
vasanello|vt|42.413728|12.346567
vejano|vt|42.216757|12.095202
vetralla|vt|42.320534|12.0575
vignanello|vt|42.383826|12.276766
viterbo|vt|42.493689|11.945068
vitorchiano|vt|42.465427|12.172462
accumoli|ri|42.694141|13.247683
amatrice|ri|42.629237|13.289176
antrodoco|ri|42.416134|13.078683
ascrea|ri|42.196228|12.996584
belmonte in sabina|ri|42.317654|12.892779
borbona|ri|42.512536|13.135983
borgorose|ri|42.193354|13.233818
borgo velino|ri|42.406033|13.059283
cantalice|ri|42.467388|12.903792
cantalupo in sabina|ri|42.304732|12.650053
casaprota|ri|42.251028|12.804279
casperia|ri|42.339729|12.670775
castel di tora|ri|42.215328|12.964183
castelnuovo di farfa|ri|42.231524|12.742999
castel sant'angelo|ri|42.393869|13.023455
cittaducale|ri|42.386834|12.948341
cittareale|ri|42.618254|13.159684
collalto sabino|ri|42.135952|13.04798
colle di tora|ri|42.211628|12.947983
collegiove|ri|42.175528|13.038986
collevecchio|ri|42.334977|12.552953
colli sul velino|ri|42.498223|12.778689
concerviano|ri|42.322961|12.98551
configni|ri|42.42353|12.642173
contigliano|ri|42.409359|12.766539
cottanello|ri|42.405966|12.685971
fara in sabina|ri|42.209558|12.72937
fiamignano|ri|42.265033|13.126363
forano|ri|42.296584|12.595375
frasso sabino|ri|42.229827|12.80558
greccio|ri|42.444796|12.751808
labro|ri|42.526012|12.800053
leonessa|ri|42.56315|12.96385
longone sabino|ri|42.27313|12.967383
magliano sabina|ri|42.361357|12.48117
marcetelli|ri|42.226602|13.046944
micigliano|ri|42.451339|13.054728
mompeo|ri|42.24831|12.752236
montasola|ri|42.38643|12.681875
montebuono|ri|42.366655|12.597059
monteleone sabino|ri|42.234336|12.856155
montenero sabino|ri|42.281229|12.813579
monte san giovanni in sabina|ri|42.328516|12.777371
montopoli di sabina|ri|42.245927|12.692077
morro reatino|ri|42.526651|12.832321
nespolo|ri|42.156158|13.070082
orvinio|ri|42.132126|12.939684
paganico sabino|ri|42.189628|12.996885
pescorocchiano|ri|42.208895|13.181641
petrella salto|ri|42.295431|13.068285
poggio bustone|ri|42.501834|12.888178
poggio catino|ri|42.294999|12.691922
poggio mirteto|ri|42.264368|12.684788
poggio moiano|ri|42.202627|12.879382
poggio nativo|ri|42.215427|12.79658
poggio san lorenzo|ri|42.252428|12.84418
posta|ri|42.523551|13.096014
pozzaglia sabina|ri|42.158771|12.964833
rieti|ri|42.414736|12.885888
rivodutri|ri|42.516434|12.854977
roccantica|ri|42.320728|12.693976
rocca sinibalda|ri|42.274429|12.925582
salisano|ri|42.257251|12.745518
scandriglia|ri|42.165533|12.841925
selci|ri|42.320677|12.621188
stimigliano|ri|42.29933|12.565727
tarano|ri|42.355928|12.594973
toffia|ri|42.212404|12.751474
torricella in sabina|ri|42.263329|12.870081
torri in sabina|ri|42.353029|12.638574
turania|ri|42.138027|13.009286
vacone|ri|42.384836|12.644458
varco sabino|ri|42.239829|13.019884
affile|rm|41.884122|13.095391
agosta|rm|41.981624|13.032688
albano laziale|rm|41.728889|12.658269
allumiere|rm|42.156218|11.903832
anguillara sabazia|rm|42.083709|12.282982
anticoli corrado|rm|42.009524|12.989087
anzio|rm|41.447101|12.628562
arcinazzo romano|rm|41.879722|13.114392
ariccia|rm|41.720709|12.671348
arsoli|rm|42.040525|13.018387
artena|rm|41.741915|12.912174
bellegra|rm|41.883422|13.02679
bracciano|rm|42.101095|12.173857
camerata nuova|rm|42.019071|13.107942
campagnano di roma|rm|42.141078|12.383669
canale monterano|rm|42.136043|12.103066
canterano|rm|41.942076|13.037315
capena|rm|42.14015|12.540201
capranica prenestina|rm|41.862997|12.951802
carpineto romano|rm|41.604413|13.084796
casape|rm|41.906517|12.885287
castel gandolfo|rm|41.742522|12.648877
castel madama|rm|41.976936|12.86919
castelnuovo di porto|rm|42.125399|12.500737
castel san pietro romano|rm|41.84572|12.894887
cave|rm|41.817923|12.928367
cerreto laziale|rm|41.944403|12.982805
cervara di roma|rm|41.988224|13.067989
cerveteri|rm|41.99836|12.098851
ciciliano|rm|41.961617|12.940932
cineto romano|rm|42.04981|12.963073
civitavecchia|rm|42.093752|11.792246
civitella san paolo|rm|42.198525|12.576475
colleferro|rm|41.727033|13.004158
colonna|rm|41.833718|12.753184
fiano romano|rm|42.171913|12.592726
filacciano|rm|42.254826|12.597675
formello|rm|42.082521|12.400273
frascati|rm|41.808245|12.680368
gallicano nel lazio|rm|41.87207|12.818534
gavignano|rm|41.699118|13.051793
genazzano|rm|41.829673|12.972246
genzano di roma|rm|41.706989|12.691118
gerano|rm|41.933119|12.993776
gorga|rm|41.655017|13.109095
grottaferrata|rm|41.788616|12.666741
guidonia montecelio|rm|41.984273|12.695319
jenne|rm|41.888823|13.169593
labico|rm|41.789519|12.880888
lanuvio|rm|41.674528|12.697243
licenza|rm|42.073113|12.901468
magliano romano|rm|42.159893|12.4369
mandela|rm|42.030807|12.923323
manziana|rm|42.130344|12.130271
marano equo|rm|41.994024|13.015988
marcellina|rm|42.02369|12.807045
marino|rm|41.769788|12.658608
mazzano romano|rm|42.204652|12.399631
mentana|rm|42.035022|12.642272
monte compatri|rm|41.808918|12.737784
monteflavio|rm|42.108178|12.830382
montelanico|rm|41.651117|13.039893
montelibretti|rm|42.140933|12.735332
monte porzio catone|rm|41.816426|12.715341
monterotondo|rm|42.051799|12.618397
montorio romano|rm|42.138411|12.807477
moricone|rm|42.117096|12.771639
morlupo|rm|42.148856|12.503013
nazzano|rm|42.229257|12.597639
nemi|rm|41.720022|12.713395
nerola|rm|42.160548|12.78668
nettuno|rm|41.457538|12.661425
olevano romano|rm|41.860621|13.03309
palestrina|rm|41.838966|12.891249
palombara sabina|rm|42.066633|12.765812
percile|rm|42.094568|12.908485
pisoniano|rm|41.906052|12.958888
poli|rm|41.887327|12.893126
pomezia|rm|41.667529|12.504786
ponzano romano|rm|42.257797|12.571627
riano|rm|42.100122|12.505675
rignano flaminio|rm|42.210014|12.481122
riofreddo|rm|42.060517|13.000334
rocca canterano|rm|41.956523|13.021788
rocca di cave|rm|41.84642|12.945488
rocca di papa|rm|41.760761|12.709614
roccagiovine|rm|42.049802|12.900257
rocca priora|rm|41.791089|12.761993
rocca santo stefano|rm|41.910522|13.023789
roiate|rm|41.872322|13.067291
roma|rm|41.89332|12.482932
roviano|rm|42.0259|12.994384
sacrofano|rm|42.104946|12.44546
sambuci|rm|41.986123|12.937986
san gregorio da sassola|rm|41.917121|12.870786
san polo dei cavalieri|rm|42.009533|12.838404
santa marinella|rm|42.03534|11.868308
sant'angelo romano|rm|42.035122|12.712681
sant'oreste|rm|42.234792|12.518905
san vito romano|rm|41.879821|12.979389
saracinesco|rm|42.003863|12.95287
segni|rm|41.692025|13.023349
subiaco|rm|41.926715|13.094432
tivoli|rm|41.960922|12.798884
tolfa|rm|42.149613|11.938195
torrita tiberina|rm|42.23591|12.618977
trevignano romano|rm|42.155917|12.246177
vallepietra|rm|41.926989|13.230667
vallinfreda|rm|42.084282|12.996612
valmontone|rm|41.778819|12.917489
velletri|rm|41.686702|12.777043
vicovaro|rm|42.017941|12.89723
vivaro romano|rm|42.099826|13.006686
zagarolo|rm|41.838384|12.83105
lariano|rm|41.725114|12.831276
ladispoli|rm|41.953722|12.073465
ardea|rm|41.608712|12.519382
ciampino|rm|41.800289|12.600471
san cesareo|rm|41.819196|12.800334
fiumicino|rm|41.767566|12.233947
fonte nuova|rm|41.996254|12.623621
aprilia|lt|41.594202|12.648576
bassiano|lt|41.549755|13.032233
campodimele|lt|41.390446|13.530336
castelforte|lt|41.299016|13.824015
cisterna di latina|lt|41.590639|12.829048
cori|lt|41.643104|12.915851
fondi|lt|41.357285|13.430031
formia|lt|41.259308|13.608021
gaeta|lt|41.218312|13.56281
itri|lt|41.297359|13.551421
latina|lt|41.459526|13.012591
lenola|lt|41.406115|13.459406
maenza|lt|41.523361|13.180549
minturno|lt|41.262974|13.746522
monte san biagio|lt|41.352513|13.352204
norma|lt|41.588015|12.969392
pontinia|lt|41.408456|13.044337
ponza|lt|40.893237|12.963062
priverno|lt|41.472616|13.179839
prossedi|lt|41.516216|13.2607
roccagorga|lt|41.525315|13.155925
rocca massima|lt|41.679415|12.919415
roccasecca dei volsci|lt|41.479872|13.212363
sabaudia|lt|41.301567|13.025873
san felice circeo|lt|41.232617|13.089398
santi cosma e damiano|lt|41.892085|12.48748
sermoneta|lt|41.549314|12.984893
sezze|lt|41.498414|13.059217
sonnino|lt|41.414272|13.24153
sperlonga|lt|41.260212|13.433107
spigno saturnia|lt|41.311515|13.735713
terracina|lt|41.28712|13.249201
ventotene|lt|40.796002|13.431213
acquafondata|fr|41.542822|13.952815
acuto|fr|41.791521|13.174394
alatri|fr|41.726503|13.342303
alvito|fr|41.688464|13.743264
amaseno|fr|41.467815|13.336002
anagni|fr|41.74472|13.152694
aquino|fr|41.491119|13.70471
arce|fr|41.587716|13.575429
arnara|fr|41.584518|13.388402
arpino|fr|41.647983|13.610141
atina|fr|41.620199|13.799978
ausonia|fr|41.354461|13.748748
belmonte castello|fr|41.577421|13.815811
boville ernica|fr|41.64252|13.472703
broccostella|fr|41.699723|13.635305
campoli appennino|fr|41.737917|13.67848
casalattico|fr|41.622412|13.725559
casalvieri|fr|41.631402|13.712993
cassino|fr|41.492558|13.83053
castelliri|fr|41.679221|13.551404
castelnuovo parano|fr|41.378874|13.755385
castrocielo|fr|41.529283|13.693938
castro dei volsci|fr|41.508217|13.406303
ceccano|fr|41.567717|13.333301
ceprano|fr|41.540418|13.512205
cervaro|fr|41.481158|13.905113
colfelice|fr|41.554919|13.603407
collepardo|fr|41.764322|13.369499
colle san magno|fr|41.55122|13.694909
coreno ausonio|fr|41.346321|13.779672
esperia|fr|41.383949|13.68061
falvaterra|fr|41.505118|13.523806
ferentino|fr|41.692932|13.253422
filettino|fr|41.891424|13.327496
fiuggi|fr|41.798561|13.220489
fontana liri|fr|41.61002|13.550305
fontechiari|fr|41.667966|13.675576
frosinone|fr|41.628259|13.576332
fumone|fr|41.72734|13.289909
gallinaro|fr|41.653423|13.79781
giuliano di roma|fr|41.539323|13.279647
guarcino|fr|41.799022|13.313397
isola del liri|fr|41.679248|13.573077
monte san giovanni campano|fr|41.64062|13.513704
morolo|fr|41.638218|13.197097
paliano|fr|41.806023|13.058068
pastena|fr|41.468244|13.491737
patrica|fr|41.592217|13.243998
pescosolido|fr|41.748124|13.656605
picinisco|fr|41.646263|13.867783
pico|fr|41.450817|13.559607
piedimonte san germano|fr|41.498319|13.752311
piglio|fr|41.83085|13.146399
pignataro interamna|fr|41.439218|13.786212
pofi|fr|41.565274|13.415532
pontecorvo|fr|41.461618|13.666709
posta fibreno|fr|41.690323|13.697407
ripi|fr|41.612256|13.424749
rocca d'arce|fr|41.58712|13.585106
roccasecca|fr|41.552321|13.667258
san biagio saracinisco|fr|41.613272|13.927877
san donato val di comino|fr|41.708124|13.812609
san giorgio a liri|fr|41.404117|13.761312
san giovanni incarico|fr|41.501718|13.557906
sant'ambrogio sul garigliano|fr|41.389018|13.871515
sant'andrea del garigliano|fr|41.367217|13.840915
sant'apollinare|fr|41.402718|13.831614
sant'elia fiumerapido|fr|41.539107|13.866147
santopadre|fr|41.603066|13.63496
san vittore del lazio|fr|41.461148|13.931401
serrone|fr|41.842121|13.095192
settefrati|fr|41.669624|13.850011
sgurgola|fr|41.670948|13.148909
sora|fr|41.720336|13.613958
strangolagalli|fr|41.600719|13.493904
supino|fr|41.610209|13.224731
terelle|fr|41.552121|13.778411
torre cajetani|fr|41.78772|13.267297
torrice|fr|41.629719|13.398401
trevi nel lazio|fr|41.862959|13.246313
trivigliano|fr|41.775721|13.272296
vallecorsa|fr|41.444615|13.405204
vallemaio|fr|41.366239|13.811748
vallerotonda|fr|41.549691|13.914585
veroli|fr|41.691272|13.417632
vicalvi|fr|41.676723|13.706407
vico nel lazio|fr|41.77715|13.341924
villa latina|fr|41.619354|13.836161
villa santa lucia|fr|41.51242|13.770011
villa santo stefano|fr|41.517416|13.310201
viticuso|fr|41.524321|13.971115
acciano|aq|42.1765|13.7179
aielli|aq|42.080283|13.590543
alfedena|aq|41.733938|14.034394
anversa degli abruzzi|aq|41.994018|13.804052
ateleta|aq|41.85543|14.197516
avezzano|aq|42.034796|13.426505
balsorano|aq|41.809824|13.559802
barete|aq|42.448525|13.282023
barisciano|aq|42.32513|13.591609
barrea|aq|41.75849|13.99073
bisegna|aq|41.921128|13.757605
bugnara|aq|42.022132|13.861598
cagnano amiterno|aq|42.455168|13.232189
calascio|aq|42.32595|13.69736
campo di giove|aq|42.010132|14.03941
campotosto|aq|42.558839|13.367987
canistro|aq|41.932679|13.392085
cansano|aq|42.004432|14.01191
capestrano|aq|42.267579|13.768656
capistrello|aq|41.972727|13.399096
capitignano|aq|42.520203|13.300948
caporciano|aq|42.250317|13.673536
cappadocia|aq|41.981572|13.276609
carapelle calvisio|aq|42.298647|13.683958
carsoli|aq|42.098927|13.084888
castel del monte|aq|42.364995|13.726572
castel di ieri|aq|42.115232|13.742602
castel di sangro|aq|41.784273|14.108064
castellafiume|aq|41.989126|13.334395
castelvecchio calvisio|aq|42.310649|13.688615
castelvecchio subequo|aq|42.130233|13.727801
celano|aq|42.084324|13.546207
cerchio|aq|42.063785|13.600135
civita d'antino|aq|41.886105|13.471126
civitella alfedena|aq|41.765326|13.942711
civitella roveto|aq|41.916026|13.426898
cocullo|aq|42.032892|13.774668
collarmele|aq|42.05903|13.6257
collelongo|aq|41.886298|13.583343
collepietro|aq|42.221617|13.78006
corfinio|aq|42.124175|13.842489
fagnano alto|aq|42.25418|13.597459
fontecchio|aq|42.229943|13.605891
fossa|aq|42.292921|13.487868
gagliano aterno|aq|42.126532|13.700001
gioia dei marsi|aq|41.954369|13.693415
goriano sicoli|aq|42.080832|13.774803
introdacqua|aq|42.007431|13.898307
l'aquila|aq|42.136885|13.610341
lecce nei marsi|aq|41.937718|13.684116
luco dei marsi|aq|41.957027|13.473598
lucoli|aq|42.251554|13.376882
magliano de' marsi|aq|42.091529|13.363794
massa d'albe|aq|42.107229|13.394294
molina aterno|aq|42.149033|13.735401
montereale|aq|42.525096|13.244072
morino|aq|41.864425|13.456999
navelli|aq|42.237864|13.729027
ocre|aq|42.282501|13.470576
ofena|aq|42.326791|13.75869
opi|aq|41.781026|13.829726
oricola|aq|42.049225|13.039387
ortona dei marsi|aq|41.99733|13.728103
ortucchio|aq|41.955028|13.645802
ovindoli|aq|42.137031|13.516097
pacentro|aq|42.049964|13.991073
pereto|aq|42.057815|13.101625
pescasseroli|aq|41.808401|13.789282
pescina|aq|42.02543|13.657901
pescocostanzo|aq|41.88633|14.065612
pettorano sul gizio|aq|41.974531|13.958709
pizzoli|aq|42.436075|13.301272
poggio picenze|aq|42.320636|13.541087
prata d'ansidonia|aq|42.277818|13.608972
pratola peligna|aq|42.098314|13.87398
prezza|aq|42.058131|13.834474
raiano|aq|42.102554|13.814304
rivisondoli|aq|41.870896|14.06743
roccacasale|aq|42.122934|13.888105
rocca di botte|aq|42.026225|13.067688
rocca di cambio|aq|42.235035|13.489963
rocca di mezzo|aq|42.205131|13.519269
rocca pia|aq|41.93343|13.97701
roccaraso|aq|41.850261|14.078257
san benedetto dei marsi|aq|42.008229|13.622701
san benedetto in perillis|aq|42.183943|13.770532
san demetrio ne' vestini|aq|42.288377|13.558641
san pio delle camere|aq|42.286172|13.657972
sante marie|aq|42.102278|13.204774
sant'eusanio forconese|aq|42.288218|13.524417
santo stefano di sessanio|aq|42.343876|13.644177
san vincenzo valle roveto|aq|41.828184|13.527427
scanno|aq|41.903861|13.880213
scontrone|aq|41.746078|14.038684
scoppito|aq|42.372612|13.255359
scurcola marsicana|aq|42.063128|13.339194
secinaro|aq|42.152933|13.6807
sulmona|aq|42.047423|13.927011
tagliacozzo|aq|42.069327|13.254692
tione degli abruzzi|aq|42.204066|13.635396
tornimparte|aq|42.29054|13.266619
trasacco|aq|41.958441|13.536102
villalago|aq|41.934729|13.842007
villa santa lucia degli abruzzi|aq|42.333462|13.777436
villa sant'angelo|aq|42.269937|13.537486
villavallelonga|aq|41.866996|13.62451
villetta barrea|aq|41.775827|13.938511
vittorito|aq|42.127033|13.816703
alba adriatica|te|42.834342|13.927816
ancarano|te|42.836048|13.739392
arsita|te|42.502162|13.782832
atri|te|42.580137|13.976412
basciano|te|42.594401|13.739647
bellante|te|42.741847|13.810195
bisenti|te|42.529128|13.801283
campli|te|42.726231|13.686284
canzano|te|42.646784|13.803143
castel castagna|te|42.542941|13.716395
castellalto|te|42.678042|13.821589
castelli|te|42.48794|13.711696
castiglione messer raimondo|te|42.531915|13.881545
castilenti|te|42.533705|13.917011
cellino attanasio|te|42.587311|13.850525
cermignano|te|42.592912|13.797398
civitella del tronto|te|42.773446|13.672291
colledara|te|42.540868|13.678938
colonnella|te|42.872628|13.865016
controguerra|te|42.855716|13.819336
corropoli|te|42.826814|13.835185
cortino|te|42.622397|13.508815
crognaleto|te|42.587941|13.48959
fano adriano|te|42.552131|13.537962
giulianova|te|42.753486|13.966701
isola del gran sasso d'italia|te|42.503058|13.659516
montefino|te|42.54432|13.884238
montorio al vomano|te|42.582706|13.632519
morro d'oro|te|42.663744|13.919497
mosciano sant'angelo|te|42.746847|13.888296
nereto|te|42.818945|13.816055
notaresco|te|42.654145|13.894698
penna sant'andrea|te|42.593443|13.772196
pietracamela|te|42.52354|13.555392
pineto|te|42.607771|14.067103
rocca santa maria|te|42.686313|13.528171
roseto degli abruzzi|te|42.677577|14.014464
sant'egidio alla vibrata|te|42.821048|13.717891
sant'omero|te|42.786672|13.801764
silvi|te|42.548792|14.118769
teramo|te|42.658118|13.699625
torano nuovo|te|42.823048|13.777293
torricella sicura|te|42.657567|13.65552
tortoreto|te|42.803984|13.912405
tossicia|te|42.544941|13.647494
valle castellana|te|42.736176|13.4971
martinsicuro|te|42.885144|13.915535
abbateggio|pe|42.225137|14.011106
alanno|pe|42.295082|13.971316
bolognano|pe|42.2181|13.960887
brittoli|pe|42.317338|13.861602
bussi sul tirino|pe|42.213035|13.826202
cappelle sul tavo|pe|42.464742|14.101977
caramanico terme|pe|42.157038|14.002555
carpineto della nora|pe|42.333038|13.860802
castiglione a casauria|pe|42.236378|13.896037
catignano|pe|42.34626|13.950114
cepagatti|pe|42.364302|14.071795
città sant'angelo|pe|42.518144|14.059203
civitaquana|pe|42.325991|13.896744
civitella casanova|pe|42.364923|13.889909
collecorvino|pe|42.458598|14.01428
corvara|pe|42.274875|13.872694
cugnoli|pe|42.307138|13.932703
elice|pe|42.518275|13.968282
farindola|pe|42.441146|13.823437
lettomanoppello|pe|42.23664|14.036921
loreto aprutino|pe|42.434634|13.983232
manoppello|pe|42.25778|14.059958
montebello di bertona|pe|42.417944|13.871901
montesilvano|pe|42.513603|14.15162
moscufo|pe|42.428144|14.054309
nocciano|pe|42.333041|13.984102
penne|pe|42.458215|13.927326
pescara|pe|42.310262|13.95759
pescosansonesco|pe|42.25446|13.884693
pianella|pe|42.396011|14.04627
picciano|pe|42.475642|13.989302
pietranico|pe|42.276237|13.910403
popoli|pe|42.171311|13.832817
roccamorice|pe|42.213499|14.024589
rosciano|pe|42.320692|14.042931
salle|pe|42.176935|13.961206
sant'eufemia a maiella|pe|42.126135|14.026508
san valentino in abruzzo citeriore|pe|42.233037|13.985606
scafa|pe|42.267138|13.999806
serramonacesca|pe|42.249033|14.093233
spoltore|pe|42.453456|14.140982
tocco da casauria|pe|42.213694|13.913472
torre de' passeri|pe|42.241891|13.930967
turrivalignani|pe|42.261938|14.026706
vicoli|pe|42.341438|13.897102
villa celiera|pe|42.381842|13.858599
altino|ch|42.099337|14.331615
archi|ch|42.089937|14.381217
ari|ch|42.29234|14.263011
arielli|ch|42.382444|14.359279
atessa|ch|42.071806|14.448902
bomba|ch|42.034335|14.367117
borrello|ch|41.918932|14.304417
bucchianico|ch|42.30484|14.181009
montebello sul sangro|ch|41.987272|14.325315
canosa sannita|ch|42.295461|14.300705
carpineto sinello|ch|42.012036|14.50502
carunchio|ch|41.916634|14.526722
casacanditella|ch|42.247439|14.19931
casalanguida|ch|42.036634|14.499355
casalbordino|ch|42.150601|14.584798
casalincontrada|ch|42.290153|14.134233
casoli|ch|42.114637|14.290214
castel frentano|ch|42.197139|14.355214
castelguidone|ch|41.823647|14.524079
castiglione messer marino|ch|41.868255|14.453058
celenza sul trigno|ch|41.87229|14.577907
chieti|ch|42.102718|14.415935
civitaluparella|ch|41.944945|14.303153
civitella messer raimondo|ch|42.088742|14.217162
colledimacine|ch|42.004433|14.200614
colledimezzo|ch|41.985659|14.38114
crecchio|ch|42.298544|14.326899
cupello|ch|42.069241|14.667807
dogliola|ch|41.941896|14.636275
fara filiorum petri|ch|42.248697|14.185918
fara san martino|ch|42.089815|14.205333
filetto|ch|42.228224|14.245095
fossacesia|ch|42.244633|14.482904
fraine|ch|41.905434|14.486421
francavilla al mare|ch|42.420643|14.28701
fresagrandinaria|ch|41.978949|14.663436
frisa|ch|42.26124|14.369414
furci|ch|42.007936|14.587875
gamberale|ch|41.906031|14.207315
gessopalena|ch|42.056635|14.273115
gissi|ch|42.021227|14.545748
giuliano teatino|ch|42.307041|14.278811
guardiagrele|ch|42.195476|14.219712
guilmi|ch|41.997035|14.47842
lama dei peligni|ch|42.041534|14.186213
lanciano|ch|42.230532|14.390779
lentella|ch|41.996262|14.67751
lettopalena|ch|42.004033|14.156813
liscia|ch|41.955235|14.555322
miglianico|ch|42.359239|14.292074
montazzoli|ch|41.948334|14.42912
monteferrante|ch|41.954734|14.388419
montelapiano|ch|41.96344|14.341565
montenerodomo|ch|41.975633|14.252815
monteodorisio|ch|42.085897|14.652681
mozzagrogna|ch|42.212756|14.444745
orsogna|ch|42.218939|14.279112
ortona|ch|42.349643|14.403913
paglieta|ch|42.163039|14.497318
palena|ch|41.984359|14.138041
palmoli|ch|41.939435|14.581423
palombaro|ch|42.124836|14.230613
pennadomo|ch|42.004834|14.324316
pennapiedimonte|ch|42.153076|14.194577
perano|ch|42.103137|14.394017
pizzoferrato|ch|41.921432|14.235716
poggiofiorito|ch|42.25494|14.322313
pollutri|ch|42.137839|14.591721
pretoro|ch|42.217225|14.141552
quadri|ch|41.924932|14.288217
rapino|ch|42.210738|14.18391
ripa teatina|ch|42.358641|14.234109
roccamontepiano|ch|42.244281|14.12719
rocca san giovanni|ch|42.25077|14.464841
roccascalegna|ch|42.062536|14.306815
roccaspinalveti|ch|41.937178|14.469959
roio del sangro|ch|41.911533|14.373819
rosello|ch|41.900832|14.349818
san buono|ch|41.980836|14.569922
san giovanni lipioni|ch|41.842854|14.561734
san giovanni teatino|ch|42.410541|14.20103
san martino sulla marrucina|ch|42.223373|14.21481
san salvo|ch|42.043326|14.72723
santa maria imbaro|ch|42.21774|14.449216
sant'eusanio del sangro|ch|42.168938|14.327114
san vito chietino|ch|42.296042|14.444515
scerni|ch|42.111798|14.570095
schiavi di abruzzo|ch|41.815132|14.485823
taranta peligna|ch|42.020834|14.170513
tollo|ch|42.347997|14.318084
torino di sangro|ch|42.18814|14.540819
tornareccio|ch|42.038136|14.416118
torrebruna|ch|41.868133|14.540823
torrevecchia teatina|ch|42.381764|14.209267
torricella peligna|ch|42.024534|14.259115
treglio|ch|42.267741|14.425515
tufillo|ch|41.916463|14.626006
vacri|ch|42.29624|14.23031
vasto|ch|42.12434|14.705923
villalfonsina|ch|42.16054|14.57162
villamagna|ch|42.328941|14.23611
villa santa maria|ch|41.952433|14.351818
pietraferrazzana|ch|41.968745|14.375353
fallo|ch|41.938333|14.323617
acquaviva collecroce|cb|41.867591|14.746793
baranello|cb|41.526026|14.559428
bojano|cb|41.485724|14.472927
bonefro|cb|41.704533|14.933334
busso|cb|41.556251|14.558531
campobasso|cb|41.717265|14.826227
campochiaro|cb|41.447524|14.508928
campodipietra|cb|41.557028|14.746032
campolieto|cb|41.632479|14.765437
campomarino|cb|41.956269|15.034344
casacalenda|cb|41.735812|14.846709
casalciprano|cb|41.580358|14.528117
castelbottaccio|cb|41.753125|14.706608
castellino del biferno|cb|41.701631|14.73113
castelmauro|cb|41.829051|14.710291
castropignano|cb|41.618328|14.559827
cercemaggiore|cb|41.461326|14.723133
cercepiccola|cb|41.460125|14.665431
civitacampomarano|cb|41.780398|14.689977
colle d'anchise|cb|41.506325|14.516827
colletorto|cb|41.662919|14.973364
duronia|cb|41.658928|14.458424
ferrazzano|cb|41.530947|14.671047
fossalto|cb|41.672929|14.545726
gambatesa|cb|41.509889|14.912052
gildone|cb|41.510127|14.739232
guardialfiera|cb|41.804234|14.79293
guardiaregia|cb|41.435024|14.541729
guglionesi|cb|41.913888|14.913613
jelsi|cb|41.517928|14.798734
larino|cb|41.805567|14.921098
limosano|cb|41.67633|14.622428
lucito|cb|41.731931|14.687228
lupara|cb|41.762731|14.733009
macchia valfortore|cb|41.59503|14.911735
mafalda|cb|41.941736|14.713226
matrice|cb|41.614098|14.710208
mirabello sannitico|cb|41.515969|14.672509
molise|cb|41.630828|14.492925
monacilioni|cb|41.60993|14.808533
montagano|cb|41.644929|14.675129
montecilfone|cb|41.902536|14.836429
montefalcone nel sannio|cb|41.866634|14.638225
montelongo|cb|41.73664|14.952977
montemitro|cb|41.888027|14.64675
montenero di bisaccia|cb|42.06545|14.789569
montorio nei frentani|cb|41.759634|14.932733
morrone del sannio|cb|41.711532|14.779831
oratino|cb|41.584765|14.587502
palata|cb|41.890515|14.785357
petacciato|cb|42.011439|14.860928
petrella tifernina|cb|41.691756|14.695449
pietracatella|cb|41.58116|14.873732
pietracupa|cb|41.682529|14.519325
portocannone|cb|41.915141|15.009268
provvidenti|cb|41.71878|14.822378
riccia|cb|41.483976|14.835084
ripabottoni|cb|41.689231|14.808331
ripalimosani|cb|41.611029|14.662729
roccavivara|cb|41.834233|14.599925
rotello|cb|41.746873|15.00639
salcito|cb|41.74753|14.510124
san biase|cb|41.715511|14.588776
san felice del molise|cb|41.891035|14.700526
san giacomo degli schiavoni|cb|41.963238|14.946431
san giovanni in galdo|cb|41.590029|14.750332
san giuliano del sannio|cb|41.456409|14.641194
san giuliano di puglia|cb|41.684524|14.964406
san martino in pensilis|cb|41.870437|15.011151
san massimo|cb|41.492924|14.410225
san polo matese|cb|41.459324|14.493428
santa croce di magliano|cb|41.71241|14.993378
sant'angelo limosano|cb|41.69333|14.603427
sant'elia a pianisi|cb|41.62043|14.875234
sepino|cb|41.408024|14.618331
spinete|cb|41.544926|14.486226
tavenna|cb|41.908636|14.762527
termoli|cb|41.998327|14.993937
torella del sannio|cb|41.639828|14.519526
toro|cb|41.572428|14.764932
trivento|cb|41.781407|14.551125
tufara|cb|41.481675|14.94645
ururi|cb|41.816536|15.016134
vinchiaturo|cb|41.492925|14.590729
acquaviva d'isernia|is|41.672126|14.148917
agnone|is|41.809561|14.377046
bagnoli del trigno|is|41.703029|14.456523
belmonte del sannio|is|41.823931|14.422121
cantalupo nel sannio|is|41.521825|14.392925
capracotta|is|41.833903|14.264244
carovilli|is|41.714248|14294
carpinone|is|41.590426|14.323322
castel del giudice|is|41.854635|14.230975
castelpetroso|is|41.559425|14.345023
castelpizzuto|is|41.521724|14.292322
castel san vincenzo|is|41.655383|14.062427
castelverrino|is|41.76643|14.397621
cerro al volturno|is|41.655925|14.102316
chiauci|is|41.677847|14.385538
civitanova del sannio|is|41.667928|14.403823
colli a volturno|is|41.598724|14.104917
conca casale|is|41.495121|14.006816
filignano|is|41.545332|14.056175
forlì del sannio|is|41.695927|14.179517
fornelli|is|41.606727|14.139489
frosolone|is|41.600627|14.446525
isernia|is|41.649468|14.208148
longano|is|41.521623|14.245621
macchia d'isernia|is|41.561624|14.167119
macchiagodena|is|41.559826|14.407824
miranda|is|41.645126|14.24632
montaquila|is|41.566723|14.112618
montenero val cocchiara|is|41.716826|14.069415
monteroduni|is|41.521123|14.17702
pesche|is|41.611068|14.281951
pescolanciano|is|41.678427|14.337221
pescopennataro|is|41.878971|14.29332
pettoranello del molise|is|41.573125|14.277521
pietrabbondante|is|41.747129|14.384421
pizzone|is|41.666936|14.035567
poggio sannita|is|41.778834|14.4142
pozzilli|is|41.511422|14.062517
rionero sannitico|is|41.711927|14.138616
roccamandolfi|is|41.497124|14.353824
roccasicura|is|41.69682|14.233532
rocchetta a volturno|is|41.624626|14.086261
san pietro avellana|is|41.790629|14.182216
sant'agapito|is|41.544597|14.222944
santa maria del molise|is|41.552725|14.368224
sant'angelo del pesco|is|41.882631|14.254917
sant'elena sannita|is|41.575842|14.469593
scapoli|is|41.615524|14.058716
sessano del molise|is|41.638162|14.331565
sesto campano|is|41.42042|14.077819
vastogirardi|is|41.774129|14.260418
venafro|is|41.483425|14.045112
ailano|ce|41.390624|14.203199
alife|ce|41.32612|14.334026
alvignano|ce|41.219342|14.352737
arienzo|ce|41.024257|14.497052
aversa|ce|40.973212|14.204928
baia e latina|ce|41.302919|14.252124
bellona|ce|41.160016|14.232326
caianello|ce|41.300234|14.088442
caiazzo|ce|41.178322|14.364948
calvi risorta|ce|41.216538|14.128731
camigliano|ce|41.179816|14.209525
cancello ed arnone|ce|41.074112|14.027423
capodrise|ce|41.04414|14.303337
capriati a volturno|ce|41.468622|14.14542
capua|ce|41.107998|14.209718
carinaro|ce|40.983584|14.218991
carinola|ce|41.170677|14.015459
casagiove|ce|41.074715|14.307729
casal di principe|ce|41.011412|14.124825
casaluce|ce|41.001912|14.198027
casapulla|ce|41.075314|14.289728
caserta|ce|41.203483|14.117058
castel campagnano|ce|41.181553|14.452504
castel di sasso|ce|41.192288|14.280644
castello del matese|ce|41.366721|14.377726
castel morrone|ce|41.121016|14.354729
castel volturno|ce|41.034313|13.940835
cervino|ce|41.040015|14.424032
cesa|ce|40.964412|14.230128
ciorlano|ce|41.450221|14.15832
conca della campania|ce|41.352332|13.996595
curti|ce|41.072914|14.273928
dragoni|ce|41.27024|14.309773
fontegreca|ce|41.455522|14.183621
formicola|ce|41.21044|14.232798
francolise|ce|41.185015|14.056722
frignano|ce|40.997933|14.179376
gallo matese|ce|41.464522|14.225022
galluccio|ce|41.352318|13.953717
giano vetusto|ce|41.202716|14.193424
gioia sannitica|ce|41.30012|14.444129
grazzanise|ce|41.089413|14.100324
gricignano di aversa|ce|40.980092|14.233711
letino|ce|41.453022|14.255422
liberi|ce|41.227818|14.290026
lusciano|ce|40.970012|14.190427
macerata campania|ce|41.063314|14.273728
maddaloni|ce|41.039924|14.376687
marcianise|ce|41.032997|14.298423
marzano appio|ce|41.320038|14.052279
mignano monte lungo|ce|41.405919|13.985517
mondragone|ce|41.114112|13.893519
orta di atella|ce|40.966512|14.268029
parete|ce|40.958911|14.161927
pastorano|ce|41.180816|14.198225
piana di monte verna|ce|41.167117|14.333728
piedimonte matese|ce|41.350819|14.368032
pietramelara|ce|41.271728|14.188005
pietravairano|ce|41.325219|14.165922
pignataro maggiore|ce|41.190816|14.172324
pontelatone|ce|41.193517|14.251726
portico di caserta|ce|41.056514|14.279428
prata sannita|ce|41.432821|14.202722
pratella|ce|41.405825|14.178097
presenzano|ce|41.376619|14.07642
raviscanina|ce|41.379398|14.233533
recale|ce|41.055114|14.304029
riardo|ce|41.262717|14.151923
rocca d'evandro|ce|41.389018|13.908116
roccamonfina|ce|41.288404|13.982838
roccaromana|ce|41.273963|14.222289
rocchetta e croce|ce|41.234172|14.169838
ruviano|ce|41.210318|14.409229
san cipriano d'aversa|ce|40.998812|14.131726
san felice a cancello|ce|41.012394|14.482993
san gregorio matese|ce|41.385228|14.37505
san marcellino|ce|40.986454|14.175332
san nicola la strada|ce|41.051814|14.331329
san pietro infine|ce|41.44592|13.961516
san potito sannitico|ce|41.337736|14.392902
san prisco|ce|41.087115|14.278228
santa maria a vico|ce|41.026857|14.465186
santa maria capua vetere|ce|41.079922|14.256397
santa maria la fossa|ce|41.091714|14.128224
san tammaro|ce|41.075902|14.231671
sant'angelo d'alife|ce|41.3592|14.263586
sant'arpino|ce|40.959512|14.248929
sessa aurunca|ce|41.236934|13.93473
sparanise|ce|41.186815|14.093722
succivo|ce|40.968012|14.255629
teano|ce|41.251084|14.067151
teverola|ce|40.995572|14.207373
tora e piccilli|ce|41.338609|14.022498
trentola ducenta|ce|40.97623|14.177166
vairano patenora|ce|41.337019|14.131121
valle agricola|ce|41.425021|14.255023
valle di maddaloni|ce|41.079515|14.415631
villa di briano|ce|41.000012|14.160726
villa literno|ce|41.009912|14.080525
vitulazio|ce|41.164116|14.216925
falciano del massico|ce|41.165122|13.949453
cellole|ce|41.204814|13.854317
casapesenna|ce|40.991512|14.136126
san marco evangelista|ce|41.037018|14.336294
airola|bn|41.061022|14.55843
amorosi|bn|41.202918|14.461636
apice|bn|41.118424|14.914866
apollosa|bn|41.094118|14.705337
arpaia|bn|41.036126|14.546974
arpaise|bn|41.029817|14.744083
baselice|bn|41.393726|14.972839
benevento|bn|41.247688|14.705941
bonea|bn|41.075206|14.618001
bucciano|bn|41.076113|14.572337
buonalbergo|bn|41.223077|14.979821
calvi|bn|41.071619|14.865241
campolattaro|bn|41.286622|14.729435
campoli del monte taburno|bn|41.131518|14.645935
casalduni|bn|41.260229|14.69609
castelfranco in miscano|bn|41.297337|15.085522
castelpagano|bn|41.401725|14.808035
castelpoto|bn|41.140927|14.701937
castelvenere|bn|41.23482|14.546532
castelvetere in val fortore|bn|41.441965|14.939563
cautano|bn|41.149313|14.638083
ceppaloni|bn|41.044533|14.760051
cerreto sannita|bn|41.284472|14.558945
circello|bn|41.357518|14.809056
colle sannita|bn|41.364683|14.833232
cusano mutri|bn|41.339082|14.507318
dugenta|bn|41.132217|14.452431
durazzano|bn|41.062481|14.447478
faicchio|bn|41.278031|14.478745
foglianise|bn|41.160133|14.671322
foiano di val fortore|bn|41.351825|14.97734
forchia|bn|41.030225|14.537343
fragneto l'abate|bn|41.256187|14.782426
fragneto monforte|bn|41.246039|14.760604
frasso telesino|bn|41.156818|14.527832
ginestra degli schiavoni|bn|41.279924|15.043742
guardia sanframondi|bn|41.256353|14.599527
limatola|bn|41.140817|14.39293
melizzano|bn|41.16057|14.50531
moiano|bn|41.079609|14.543006
molinara|bn|41.29063|14.910343
montefalcone di val fortore|bn|41.325991|15.009059
montesarchio|bn|41.063923|14.639788
morcone|bn|41.341101|14.663626
paduli|bn|41.167202|14.883597
pago veiano|bn|41.247922|14.871339
pannarano|bn|41.010215|14.703342
paolisi|bn|41.037084|14.578152
paupisi|bn|41.19532|14.665235
pesco sannita|bn|41.233594|14.811333
pietraroja|bn|41.347822|14.54963
pietrelcina|bn|41.199421|14.844139
ponte|bn|41.214732|14.696327
pontelandolfo|bn|41.287114|14.692288
puglianello|bn|41.223419|14.45043
reino|bn|41.291903|14.823823
san bartolomeo in galdo|bn|41.409682|15.015391
san giorgio del sannio|bn|41.069327|14.852458
san giorgio la molara|bn|41.270908|14.920083
san leucio del sannio|bn|41.074418|14.757438
san lorenzello|bn|41.277366|14.542832
san lorenzo maggiore|bn|41.251227|14.626944
san lupo|bn|41.260105|14.635629
san marco dei cavoti|bn|41.310603|14.878952
san martino sannita|bn|41.066218|14.83584
san nazzaro|bn|41.05239|14.856356
san nicola manfredi|bn|41.071768|14.824534
san salvatore telesino|bn|41.236019|14.499131
santa croce del sannio|bn|41.387456|14.732203
sant'agata de' goti|bn|41.088538|14.503793
sant'angelo a cupolo|bn|41.069118|14.80374
sassinoro|bn|41.374724|14.664532
solopaca|bn|41.194566|14.553363
telese terme|bn|41.216414|14527
tocco caudio|bn|41.125159|14.634268
torrecuso|bn|41.18974|14.680055
vitulano|bn|41.173979|14.645624
sant'arcangelo trimonte|bn|41.165512|14.939038
acerra|na|40.946053|14.375008
afragola|na|40.926012|14.310831
agerola|na|40.637707|14.538839
anacapri|na|40.55577|14.213604
arzano|na|40.91602|14.268018
bacoli|na|40.796613|14.077783
barano d'ischia|na|40.709018|13.918602
boscoreale|na|40.77404|14.477432
boscotrecase|na|40.77431|14.462036
brusciano|na|40.923212|14.423933
caivano|na|40.957112|14.31023
calvizzano|na|40.905696|14.19329
camposano|na|40.953743|14.529154
capri|na|40.548775|14.228087
carbonara di nola|na|40.874228|14.57863
cardito|na|40.942012|14.29943
casalnuovo di napoli|na|40.914819|14.351125
casamarciano|na|40.931618|14.553477
casamicciola terme|na|40.747356|13.907512
casandrino|na|40.935864|14.248805
casavatore|na|40.899211|14.27663
casola di napoli|na|40.696037|14.533447
casoria|na|40.904557|14.290122
castellammare di stabia|na|40.694305|14.480481
castello di cisterna|na|40.915112|14.406233
cercola|na|40.857682|14.355158
cicciano|na|40.964714|14.538935
cimitile|na|40.940513|14.525435
comiziano|na|40.952142|14.551436
crispano|na|40.952512|14.28993
forio|na|40.736842|13.858135
frattamaggiore|na|40.941426|14.272209
frattaminore|na|40.95574|14.270948
giugliano in campania|na|40.928687|14.200699
gragnano|na|40.69|14.521065
grumo nevano|na|40.938154|14.259269
ischia|na|40.734205|13.947125
lacco ameno|na|40.75097|13.89006
lettere|na|40.704909|14.544939
liveri|na|40.904713|14.565937
marano di napoli|na|40.89761|14.190528
mariglianella|na|40.929513|14.435833
marigliano|na|40.924914|14.456867
massa lubrense|na|40.611036|14.345089
melito di napoli|na|40.920511|14.230029
meta|na|40.639206|14.418237
monte di procida|na|40.800907|14.051827
mugnano di napoli|na|40.9093|14.206835
napoli|na|40.835934|14.248783
nola|na|40.921083|14.53274
ottaviano|na|40.849818|14.490127
palma campania|na|40.864917|14.548733
piano di sorrento|na|40.632645|14.411091
pimonte|na|40.673608|14.509838
poggiomarino|na|40.803111|14.539037
pollena trocchia|na|40.854635|14.379468
pomigliano d'arco|na|40.907912|14.386233
pompei|na|40.749127|14.500642
portici|na|40.818973|14.338745
pozzuoli|na|40.822643|14.121911
procida|na|40.764999|14.023906
qualiano|na|40.918836|14.153611
quarto|na|40.877194|14.140159
ercolano|na|40.805361|14.347104
roccarainola|na|40.971255|14.561026
san gennaro vesuviano|na|40.8604|14.529168
san giorgio a cremano|na|40.829296|14.334168
san giuseppe vesuviano|na|40.829811|14.504436
san paolo bel sito|na|40.914813|14.546736
san sebastiano al vesuvio|na|40.843565|14.36995
sant'agnello|na|40.630006|14.397136
sant'anastasia|na|40.866911|14.404833
sant'antimo|na|40.945711|14.231429
sant'antonio abate|na|40.723709|14.546538
san vitaliano|na|40.927213|14.480134
saviano|na|40.905613|14.505035
scisciano|na|40.916013|14.485735
serrara fontana|na|40.717945|13.893526
somma vesuviana|na|40.871615|14.436142
sorrento|na|40.624906|14.374836
striano|na|40.815855|14.576468
terzigno|na|40.812442|14.496484
torre annunziata|na|40.752524|14.455004
torre del greco|na|40.787921|14.368281
tufino|na|40.954114|14.565936
vico equense|na|40.662296|14.426382
villaricca|na|40.921611|14.189828
visciano|na|40.925414|14.582837
volla|na|40.878411|14.342932
santa maria la carità|na|40.721003|14.51282
trecase|na|40.770891|14.439651
massa di somma|na|40.847631|14.374302
aiello del sabato|av|40.887194|14.819448
altavilla irpina|av|41.00616|14.777679
andretta|av|40.938476|15.32775
aquilonia|av|40.986721|15.474455
ariano irpino|av|41.152667|15.088563
atripalda|av|40.920812|14.833499
avella|av|40.958914|14.600537
avellino|av|40.996545|15.140569
bagnoli irpino|av|40.833115|15.072549
baiano|av|40.952514|14.616737
bisaccia|av|41.012121|15.374753
bonito|av|41.09772|15.001344
cairano|av|40.896819|15.370354
calabritto|av|40.782215|15.223953
calitri|av|40.902119|15.430556
candida|av|40.9423|14.875183
caposele|av|40.816374|15.223939
capriglia irpina|av|40.960316|14.77724
carife|av|41.028106|15.208662
casalbore|av|41.233923|15.006742
cassano irpino|av|40.870416|15.025947
castel baronia|av|41.047421|15.188748
castelfranci|av|40.931317|15.043247
castelvetere sul calore|av|40.928717|14.986145
cervinara|av|41.021928|14.613291
cesinali|av|40.895526|14.828328
chianche|av|41.046157|14.789589
chiusano di san domenico|av|40.933116|14.916344
contrada|av|40.866527|14.776031
conza della campania|av|40.858592|15.336066
domicella|av|40.880013|14.587737
flumeri|av|41.077821|15.147747
fontanarosa|av|41.018361|15.02099
forino|av|40.861973|14.734796
frigento|av|41.011319|15.099747
gesualdo|av|41.006719|15.072446
greci|av|41.251825|15.169145
grottaminarda|av|41.06972|15.058945
grottolella|av|40.972716|14.78704
guardia lombardi|av|40.953719|15.20885
lacedonia|av|41.051222|15.423854
lapio|av|40.981117|14.946344
lauro|av|40.879968|14.629467
lioni|av|40.876517|15.187751
luogosano|av|40.985631|14.991956
manocalzati|av|40.941865|14.849804
marzano di nola|av|40.90272|14.584093
melito irpino|av|41.103253|15.052042
mercogliano|av|40.920162|14.738605
mirabella eclano|av|41.044919|14.998744
montaguto|av|41.248725|15.248447
montecalvo irpino|av|41.195623|15.033443
montefalcione|av|40.961038|14.882215
monteforte irpino|av|40.891922|14.713905
montefredane|av|40.962395|14.810959
montefusco|av|41.037618|14.854941
montella|av|40.843856|15.017955
montemarano|av|40.915216|14.997646
montemiletto|av|41.011342|14.908704
monteverde|av|40.998922|15.534157
morra de sanctis|av|40.929019|15.242851
moschiano|av|40.874246|14.658358
mugnano del cardinale|av|40.943014|14.635638
nusco|av|40.88695|15.084004
ospedaletto d'alpinolo|av|40.938671|14.743934
pago del vallo di lauro|av|40.897213|14.607638
parolise|av|40.930941|14.88177
paternopoli|av|40.97332|15.032045
petruro irpino|av|41.031917|14.79754
pietradefusi|av|41.044191|14.883407
pietrastornina|av|40.991741|14.729245
prata di principato ultra|av|40.985973|14.837514
pratola serra|av|40.987017|14.852642
quadrelle|av|40.946996|14.63826
quindici|av|40.863313|14.647539
roccabascerana|av|41.018443|14.716318
rocca san felice|av|40.95146|15.164394
rotondi|av|41.03118|14.596753
salza irpina|av|40.920063|14.88935
san mango sul calore|av|40.958117|14.972745
san martino valle caudina|av|41.023025|14.662237
san michele di serino|av|40.877296|14.856309
san nicola baronia|av|41.058521|15.199148
san potito ultra|av|40.928116|14.873051
san sossio baronia|av|41.071221|15.200548
santa lucia di serino|av|40.870415|14.875944
sant'andrea di conza|av|40.844818|15.369655
sant'angelo all'esca|av|41.006618|14.993045
sant'angelo a scala|av|40.974575|14.740186
sant'angelo dei lombardi|av|40.92703|15.177524
santa paolina|av|41.021718|14.847241
santo stefano del sole|av|40.893915|14.868543
savignano irpino|av|41.228524|15.179246
scampitella|av|41.092522|15.29995
senerchia|av|40.74039|15.200979
serino|av|40.856714|14.870566
sirignano|av|40.951014|14.631594
solofra|av|40.829131|14.845652
sorbo serpico|av|40.916928|14.887097
sperone|av|40.952359|14.60328
sturno|av|41.019827|15.111318
summonte|av|40.949488|14.744804
taurano|av|40.884069|14.634006
taurasi|av|41.008718|14.958644
teora|av|40.852817|15.253352
torella dei lombardi|av|40.939718|15.115048
torre le nocelle|av|41.023118|14.909343
torrioni|av|41.033453|14.813023
trevico|av|41.048421|15.233449
tufo|av|41.010298|14.823317
vallata|av|41.037221|15.25215
vallesaccarda|av|41.06276|15.251783
venticano|av|41.049124|14.917637
villamaina|av|40.969418|15.090047
villanova del battista|av|41.120822|15.158747
volturara irpina|av|40.878104|14.917085
zungoli|av|41.125322|15.203648
montoro|av|40.832876|14.791777
acerno|sa|40.737713|15.056949
agropoli|sa|40.346905|14.996553
albanella|sa|40.478422|15.116608
alfano|sa|40.177505|15.425165
altavilla silentina|sa|40.530241|15.131031
amalfi|sa|40.63367|14.60261
angri|sa|40.737659|14.572283
aquara|sa|40.443909|15.253857
ascea|sa|40.141602|15.18576
atena lucana|sa|40.455111|15.557264
atrani|sa|40.636908|14.607541
auletta|sa|40.558841|15.42489
baronissi|sa|40.746938|14.769987
battipaglia|sa|40.609254|14.982144
bellosguardo|sa|40.423309|15.314159
bracigliano|sa|40.823512|14.705441
buccino|sa|40.632913|15.369658
buonabitacolo|sa|40.271308|15.619968
caggiano|sa|40.567813|15.493361
calvanico|sa|40.775212|14.827944
camerota|sa|40.032501|15.372765
campagna|sa|40.664729|15.107145
campora|sa|40.305406|15.29236
cannalonga|sa|40.244298|15.293675
capaccio paestum|sa|40.443862|15.040038
casalbuono|sa|40.21575|15.681587
casaletto spartano|sa|40.150705|15.618669
casal velino|sa|40.190907|15.109374
caselle in pittari|sa|40.172539|15.545752
castelcivita|sa|40.49421|15.234456
castellabate|sa|40.276127|14.958119
castelnuovo cilento|sa|40.219535|15.177151
castelnuovo di conza|sa|40.820617|15.318754
castel san giorgio|sa|40.783712|14.698841
castel san lorenzo|sa|40.418308|15.227157
castiglione del genovesi|sa|40.725511|14.848445
cava de' tirreni|sa|40.700462|14.706263
celle di bulgheria|sa|40.095489|15.402705
centola|sa|40.066502|15.311963
ceraso|sa|40.194304|15.256061
cetara|sa|40.648214|14.700415
cicerale|sa|40.343801|15.129201
colliano|sa|40.726115|15.291155
conca dei marini|sa|40.618907|14.56784
controne|sa|40.50961|15.205556
contursi terme|sa|40.649513|15.239355
corbara|sa|40.724782|14.592763
corleto monforte|sa|40.437209|15.37956
cuccaro vetere|sa|40.161682|15.308547
eboli|sa|40.617515|15.056944
felitto|sa|40.373507|15.243058
fisciano|sa|40.772812|14.799443
furore|sa|40.618936|14.548137
futani|sa|40.150703|15.323163
giffoni sei casali|sa|40.719141|14.905972
giffoni valle piana|sa|40.718112|14.942347
gioi|sa|40.289705|15.218059
giungano|sa|40.394707|15.107155
ispani|sa|40.087304|15.558269
laureana cilento|sa|40.299156|15.038455
laurino|sa|40.336507|15.337161
laurito|sa|40.168604|15.405964
laviano|sa|40.785128|15.307312
lustra|sa|40.288304|15.068255
magliano vetere|sa|40.347007|15.236658
maiori|sa|40.648908|14.640141
mercato san severino|sa|40.783303|14.755728
minori|sa|40.652708|14.624941
moio della civitella|sa|40.247105|15.26886
montano antilia|sa|40.162304|15.365463
montecorice|sa|40.234024|14.984681
montecorvino pugliano|sa|40.658056|14.944609
montecorvino rovella|sa|40.695913|14.976105
monteforte cilento|sa|40.364807|15.194857
monte san giacomo|sa|40.344409|15.540465
montesano sulla marcellana|sa|40.275889|15.701375
morigerati|sa|40.139949|15.555182
nocera inferiore|sa|40.747213|14.64332
nocera superiore|sa|40.74156|14.671504
novi velia|sa|40.225105|15.288361
ogliastro cilento|sa|40.352405|15.043154
olevano sul tusciano|sa|40.668904|15.032615
oliveto citra|sa|40.690808|15.231782
omignano|sa|40.248204|15.085156
orria|sa|40.299705|15.171157
ottati|sa|40.462856|15.315234
padula|sa|40.341642|15.658254
pagani|sa|40.749888|14.613323
palomonte|sa|40.662513|15.291856
pellezzano|sa|40.725211|14.757443
perdifumo|sa|40.265986|15.015499
perito|sa|40.297705|15.147657
pertosa|sa|40.54288|15.448966
petina|sa|40.532111|15.373459
piaggine|sa|40.344808|15.378661
pisciotta|sa|40.108902|15.234561
polla|sa|40.517112|15.497962
pollica|sa|40.190702|15.057156
pontecagnano faiano|sa|40.640904|14.881882
positano|sa|40.628658|14.485495
postiglione|sa|40.559011|15.232356
praiano|sa|40.611107|14.53134
prignano cilento|sa|40.330905|15.068655
ravello|sa|40.649664|14.612275
ricigliano|sa|40.668456|15.481459
roccadaspide|sa|40.425364|15.191785
roccagloriosa|sa|40.108662|15.433349
roccapiemonte|sa|40.762465|14.692375
rofrano|sa|40.211405|15.427864
romagnano al monte|sa|40.627188|15.435959
roscigno|sa|40.39903|15.345926
rutino|sa|40.299704|15.072755
sacco|sa|40.378108|15.377961
sala consilina|sa|40.398221|15.596369
salento|sa|40.248604|15.190058
salerno|sa|40.419442|15.310756
salvitelle|sa|40.590913|15.45876
san cipriano picentino|sa|40.720003|14.870013
san giovanni a piro|sa|40.050302|15.450967
san gregorio magno|sa|40.656523|15.403609
san mango piemonte|sa|40.701911|14.833745
san marzano sul sarno|sa|40.775215|14.587362
san mauro cilento|sa|40.226355|15.044077
san mauro la bruca|sa|40.122937|15.28975
san pietro al tanagro|sa|40.456811|15.480662
san rufo|sa|40.43451|15.463662
santa marina|sa|40.104304|15.540468
sant'angelo a fasanella|sa|40.45681|15.341459
sant'arsenio|sa|40.472911|15.484562
sant'egidio del monte albino|sa|40.733842|14.597044
santomenna|sa|40.808317|15.321454
san valentino torio|sa|40.793011|14.598039
sanza|sa|40.244607|15.552666
sapri|sa|40.075078|15.629775
sarno|sa|40.810811|14.619839
sassano|sa|40.337709|15.563066
scafati|sa|40.749989|14.526949
scala|sa|40.653614|14.607792
serramezzana|sa|40.243948|15.032059
serre|sa|40.582511|15.185554
sessa cilento|sa|40.259204|15.075756
siano|sa|40.804026|14.688432
sicignano degli alburni|sa|40.581042|15.300396
stella cilento|sa|40.231803|15.092857
stio|sa|40.310006|15.252679
teggiano|sa|40.379209|15.540465
torchiara|sa|40.322967|15.052395
torraca|sa|40.112532|15.635237
torre orsaia|sa|40.133304|15.473266
tortorella|sa|40.142205|15.605669
tramonti|sa|40.699733|14.641537
trentinara|sa|40.400395|15.107466
valle dell'angelo|sa|40.343808|15.368461
vallo della lucania|sa|40.229704|15.26576
valva|sa|40.738215|15.269454
vibonati|sa|40.100248|15.583115
vietri sul mare|sa|40.671548|14.7286
bellizzi|sa|40.62018|14.948453
accadia|fg|41.158824|15.33145
alberona|fg|41.432728|15.123042
anzano di puglia|fg|41.122523|15.28445
apricena|fg|41.783938|15.443744
ascoli satriano|fg|41.20612|15.56161
biccari|fg|41.396128|15.194444
bovino|fg|41.249926|15.341249
cagnano varano|fg|41.828742|15.773151
candela|fg|41.137925|15.514654
carapelle|fg|41.365313|15.693676
carlantino|fg|41.592118|14.976765
carpino|fg|41.844242|15.856153
casalnuovo monterotaro|fg|41.620232|15.103839
casalvecchio di puglia|fg|41.593832|15.110539
castelluccio dei sauri|fg|41.303915|15.479226
castelluccio valmaggiore|fg|41.342327|15.199045
castelnuovo della daunia|fg|41.582631|15.12084
celenza valfortore|fg|41.56173|14.978837
celle di san vito|fg|41.325936|15.180998
cerignola|fg|41.264812|15.899665
chieuti|fg|41.844637|15.165937
deliceto|fg|41.223026|15.38585
faeto|fg|41.325426|15.159844
foggia|fg|41.502811|15.4529
ischitella|fg|41.904344|15.899453
isole tremiti|fg|42.1148|15.487124
lesina|fg|41.859439|15.352741
lucera|fg|41.508273|15.337646
manfredonia|fg|41.625473|15.909593
mattinata|fg|41.710141|16.051559
monteleone di puglia|fg|41.165424|15.258048
monte sant'angelo|fg|41.70544|15.967657
motta montecorvino|fg|41.50743|15.114141
orsara di puglia|fg|41.281626|15.264447
orta nova|fg|41.32993|15.710656
panni|fg|41.222025|15.274648
peschici|fg|41.94743|16.013883
pietramontecorvino|fg|41.541631|15.127541
poggio imperiale|fg|41.823324|15.365418
rignano garganico|fg|41.677013|15.587517
rocchetta sant'antonio|fg|41.103524|15.461754
rodi garganico|fg|41.928745|15.881052
roseto valfortore|fg|41.372427|15.096142
san giovanni rotondo|fg|41.705055|15.730646
san marco in lamis|fg|41.713138|15.63985
san marco la catola|fg|41.524829|15.005938
san nicandro garganico|fg|41.838446|15.565338
san paolo di civitate|fg|41.738336|15.260241
san severo|fg|41.689776|15.377621
sant'agata di puglia|fg|41.151824|15.380251
serracapriola|fg|41.811937|15.160238
stornara|fg|41.288625|15.768058
stornarella|fg|41.25638|15.729788
torremaggiore|fg|41.687477|15.292956
troia|fg|41.363128|15.312347
vico del gargano|fg|41.898344|15.957554
vieste|fg|41.88276|16.179179
volturara appula|fg|41.496029|15.052239
volturino|fg|41.477229|15.124241
ordona|fg|41.315529|15.627355
zapponeta|fg|41.456964|15.956532
acquaviva delle fonti|ba|40.897829|16.842487
adelfia|ba|41.004265|16.871653
alberobello|ba|40.784124|17.237703
altamura|ba|40.82856|16.552731
bari|ba|41.125784|16.862029
binetto|ba|41.024165|16.709581
bitetto|ba|41.041408|16.748699
bitonto|ba|41.108497|16.691548
bitritto|ba|41.044532|16.825185
capurso|ba|41.05032|16.916742
casamassima|ba|40.957131|16.920388
cassano delle murge|ba|40.89101|16.768619
castellana grotte|ba|40.886431|17.165495
cellamare|ba|41.019333|16.928288
conversano|ba|40.968396|17.113771
corato|ba|41.152761|16.411464
gioia del colle|ba|40.796728|16.92359
giovinazzo|ba|41.187501|16.671666
gravina in puglia|ba|40.819725|16.422779
grumo appula|ba|41.013531|16.708483
locorotondo|ba|40.789167|17.299348
modugno|ba|41.082564|16.780552
mola di bari|ba|41.061323|17.091711
molfetta|ba|41.199246|16.596828
monopoli|ba|40.952237|17.299948
noci|ba|40.79468|17.123557
noicattaro|ba|41.032287|16.987712
palo del colle|ba|41.057532|16.700782
poggiorsini|ba|40.917126|16.254774
polignano a mare|ba|40.996118|17.219586
putignano|ba|40.85183|17.121294
rutigliano|ba|41.011159|17.005479
ruvo di puglia|ba|41.114087|16.486397
sammichele di bari|ba|40.887987|16.949456
sannicandro di bari|ba|41.00052|16.797697
santeramo in colle|ba|40.793027|16.755286
terlizzi|ba|41.130234|16.542471
toritto|ba|40.99823|16.679779
triggiano|ba|41.063448|16.921698
turi|ba|40.917331|17.021191
valenzano|ba|41.045333|16.885086
avetrana|ta|40.349942|17.727374
carosino|ta|40.465824|17.397605
castellaneta|ta|40.630424|16.934678
crispiano|ta|40.60548|17.232135
faggiano|ta|40.419823|17.386605
fragagnano|ta|40.431924|17.472207
ginosa|ta|40.579751|16.756208
grottaglie|ta|40.536193|17.434221
laterza|ta|40.629524|16.799689
leporano|ta|40.383922|17.334904
lizzano|ta|40.391023|17.447607
manduria|ta|40.401825|17.633511
martina franca|ta|40.704238|17.339991
maruggio|ta|40.32226|17.572366
massafra|ta|40.588831|17.11699
monteiasi|ta|40.499725|17.381007
montemesola|ta|40.56302|17.339228
monteparano|ta|40.444024|17.415105
mottola|ta|40.633225|17.037195
palagianello|ta|40.611891|16.975118
palagiano|ta|40.578224|17.037395
pulsano|ta|40.384693|17.35567
roccaforzata|ta|40.437424|17.389605
san giorgio ionico|ta|40.456624|17.378704
san marzano di san giuseppe|ta|40.454425|17.505607
sava|ta|40.403424|17.558409
taranto|ta|40.548816|17.08058
torricella|ta|40.354062|17.499392
statte|ta|40.563225|17.209799
brindisi|br|40.636227|17.688501
carovigno|br|40.707185|17.656848
ceglie messapica|br|40.646044|17.517057
cellino san marco|br|40.472228|17.966318
cisternino|br|40.742985|17.425745
erchie|br|40.436042|17.734629
fasano|br|40.834432|17.358399
francavilla fontana|br|40.530162|17.582677
latiano|br|40.553783|17.718222
mesagne|br|40.558829|17.808313
oria|br|40.497327|17.64131
ostuni|br|40.727631|17.576406
san donaci|br|40.44938|17.922606
san michele salentino|br|40.630386|17.633576
san pancrazio salentino|br|40.419133|17.839385
san pietro vernotico|br|40.491529|17.998918
san vito dei normanni|br|40.656248|17.703752
torchiarolo|br|40.484029|18.051619
torre santa susanna|br|40.465627|17.735612
villa castelli|br|40.584527|17.474605
alessano|le|39.889678|18.331208
alezio|le|40.061321|18.058224
alliste|le|39.948618|18.088826
andrano|le|39.983019|18.382202
aradeo|le|40.130922|18.129625
arnesano|le|40.3352|18.091628
bagnolo del salento|le|40.150624|18.35263
botrugno|le|40.064722|18.32443
calimera|le|40.250022|18.280727
campi salentina|le|40.398496|18.018319
cannole|le|40.166225|18.36603
caprarica di lecce|le|40.258126|18.249026
carmiano|le|40.344826|18.043021
carpignano salentino|le|40.197781|18.340827
casarano|le|40.01082|18.161765
castri di lecce|le|40.273826|18.263327
castrignano de' greci|le|40.173897|18.297011
castrignano del capo|le|39.832318|18.351233
cavallino|le|40.312527|18.200425
collepasso|le|40.07302|18.163073
copertino|le|40.271681|18.050252
corigliano d'otranto|le|40.159624|18.259428
corsano|le|39.889219|18.369333
cursi|le|40.149624|18.314829
cutrofiano|le|40.126323|18.201627
diso|le|40.009522|18.392532
gagliano del capo|le|39.844218|18.369534
galatina|le|40.174278|18.167839
galatone|le|40.142922|18.070424
gallipoli|le|40.05672|17.996423
giuggianello|le|40.094423|18.368931
giurdignano|le|40.121524|18.432732
guagnano|le|40.401227|17.949018
lecce|le|40.152217|18.226063
lequile|le|40.305826|18.140223
leverano|le|40.289425|17.999414
lizzanello|le|40.304127|18.222525
maglie|le|40.118463|18.298853
martano|le|40.203621|18.30198
martignano|le|40.237425|18.255227
matino|le|40.03251|18.133853
melendugno|le|40.271701|18.336912
melissano|le|39.971065|18.125779
melpignano|le|40.157081|18.293078
miggiano|le|39.95972|18.311631
minervino di lecce|le|40.088424|18.420432
monteroni di lecce|le|40.324398|18.09759
montesano salentino|le|39.975621|18.321431
morciano di leuca|le|39.847818|18.312432
muro leccese|le|40.102323|18.33813
nardò|le|40.176261|18.030637
neviano|le|40.107722|18.114025
nociglia|le|40.039222|18.326031
novoli|le|40.378527|18.04922
ortelle|le|40.033896|18.39112
otranto|le|40.147825|18.485933
palmariggi|le|40.131322|18.381176
parabita|le|40.051921|18.129526
patù|le|39.841218|18.338933
poggiardo|le|40.054223|18.379132
racale|le|39.963119|18.093826
ruffano|le|39.984092|18.247476
salice salentino|le|40.385662|17.963748
salve|le|39.861318|18.293332
sanarica|le|40.089123|18.348731
san cesario di lecce|le|40.302961|18.159865
san donato di lecce|le|40.268126|18.184225
sannicola|le|40.091921|18.066124
san pietro in lama|le|40.308426|18.128623
santa cesarea terme|le|40.036323|18.456834
scorrano|le|40.091623|18.299629
seclì|le|40.130126|18.100507
sogliano cavour|le|40.148823|18.195226
soleto|le|40.187708|18.207372
specchia|le|39.93712|18.297631
spongano|le|40.017822|18.365632
squinzano|le|40.436528|18.04182
sternatia|le|40.222125|18.225226
supersano|le|40.017721|18.243229
surano|le|40.028322|18.345131
surbo|le|40.396028|18.133422
taurisano|le|39.956619|18.214129
taviano|le|39.983519|18.086826
tiggiano|le|39.905819|18.364433
trepuzzi|le|40.405472|18.07062
tricase|le|39.93042|18.355332
tuglie|le|40.073821|18.098125
ugento|le|39.926319|18.157428
uggiano la chiesa|le|40.101724|18.449333
veglie|le|40.335126|17.965219
vernole|le|40.289527|18.303527
zollino|le|40.206006|18.250589
san cassiano|le|40.056287|18.333936
castro|le|40.007022|18.425733
porto cesareo|le|40.262824|17.898418
presicce-acquarica|le|39.903241|18.254798
andria|bt|41.227646|16.295435
barletta|bt|41.321498|16.28687
bisceglie|bt|41.240274|16.501011
canosa di puglia|bt|41.219631|16.067466
margherita di savoia|bt|41.374235|16.150166
minervino murge|bt|41.085052|16.077776
san ferdinando di puglia|bt|41.302632|16.070465
spinazzola|bt|40.969168|16.088007
trani|bt|41.275088|16.416258
trinitapoli|bt|41.358853|16.087436
abriola|pz|40.507882|15.81376
acerenza|pz|40.792121|15.936568
albano di lucania|pz|40.584445|16.036716
anzi|pz|40.516615|15.924571
armento|pz|40.307712|16.064977
atella|pz|40.880321|15.650161
avigliano|pz|40.729718|15.720264
balvano|pz|40.650315|15.514161
banzi|pz|40.861559|16.013768
baragiano|pz|40.679738|15.593966
barile|pz|40.946322|15.67266
bella|pz|40.760917|15.54056
brienza|pz|40.477849|15.630151
brindisi montagna|pz|40.609517|15.939771
calvello|pz|40.476014|15.84857
calvera|pz|40.149509|16.143881
campomaggiore|pz|40.566012|16.073217
cancellara|pz|40.730219|15.925769
carbone|pz|40.142409|16.08938
san paolo albanese|pz|40.036708|16.334586
castelgrande|pz|40.786617|15.430157
castelluccio inferiore|pz|40.003905|15.980879
castelluccio superiore|pz|40.010305|15.975079
castelmezzano|pz|40.530237|16.046551
castelsaraceno|pz|40.163708|15.991877
castronuovo di sant'andrea|pz|40.19071|16.185481
cersosimo|pz|40.047009|16.349386
chiaromonte|pz|40.124009|16.213835
corleto perticara|pz|40.383513|16.040676
episcopia|pz|40.074807|16.098181
fardella|pz|40.113809|16.169782
filiano|pz|40.831879|15.716292
forenza|pz|40.859583|15.855268
francavilla in sinni|pz|40.080192|16.203224
gallicchio|pz|40.289012|16.139079
genzano di lucania|pz|40.849102|16.032463
grumento nova|pz|40.28281|15.888773
guardia perticara|pz|40.360513|16.098877
lagonegro|pz|40.128306|15.761472
latronico|pz|40.089807|16.011778
laurenzana|pz|40.460514|15.970973
lauria|pz|40.047351|15.836595
lavello|pz|41.045625|15.790762
maratea|pz|39.996703|15.722373
marsico nuovo|pz|40.423012|15.734868
marsicovetere|pz|40.376601|15.824493
maschito|pz|40.908922|15.829464
melfi|pz|40.996127|15.655654
missanello|pz|40.282012|16.16598
moliterno|pz|40.241932|15.86902
montemilone|pz|41.030026|15.969266
montemurro|pz|40.296911|15.991275
muro lucano|pz|40.752517|15.485759
nemoli|pz|40.067505|15.800274
noepoli|pz|40.086809|16.329685
oppido lucano|pz|40.76182|15.98667
palazzo san gervasio|pz|40.931324|15.980767
pescopagano|pz|40.835618|15.398856
picerno|pz|40.638456|15.635874
pietragalla|pz|40.747522|15.880648
pietrapertosa|pz|40.518017|16.063207
pignola|pz|40.573637|15.783504
potenza|pz|40.518319|15.82096
rapolla|pz|40.975723|15.67126
rapone|pz|40.846719|15.501158
rionero in vulture|pz|40.926188|15.669441
ripacandida|pz|40.911781|15.724783
rivello|pz|40.078005|15.756073
roccanova|pz|40.212011|16.203181
rotonda|pz|39.952786|16.039088
ruoti|pz|40.718017|15.678963
ruvo del monte|pz|40.848819|15.541359
san chirico nuovo|pz|40.676747|16.078534
san chirico raparo|pz|40.190715|16.076348
san costantino albanese|pz|40.035808|16.304686
san fele|pz|40.819326|15.538428
san martino d'agri|pz|40.23971|16.053078
san severino lucano|pz|40.020607|16.137882
sant'angelo le fratte|pz|40.545913|15.558963
sant'arcangelo|pz|40.247312|16.271182
sarconi|pz|40.247891|15.890397
sasso di castalda|pz|40.488266|15.677963
satriano di lucania|pz|40.543914|15.638265
savoia di lucania|pz|40.569813|15.552562
senise|pz|40.147158|16.289501
spinoso|pz|40.269694|15.966769
teana|pz|40.124809|16.151781
terranova di pollino|pz|39.978107|16.296286
tito|pz|40.583615|15.675465
tolve|pz|40.694219|16.016271
tramutola|pz|40.31661|15.791071
trecchina|pz|40.025658|15.776787
trivigno|pz|40.581017|15.989872
vaglio basilicata|pz|40.665873|15.92007
venosa|pz|40.961323|15.814863
vietri di potenza|pz|40.598647|15.507246
viggianello|pz|39.973195|16.085353
viggiano|pz|40.340711|15.900773
ginestra|pz|40.929822|15.734562
paterno|pz|40.378311|15.732769
accettura|mt|40.491016|16.157977
aliano|mt|40.313827|16.229961
bernalda|mt|40.412918|16.691097
calciano|mt|40.588018|16.192376
cirigliano|mt|40.394414|16.172578
colobraro|mt|40.187312|16.424986
craco|mt|40.379916|16.437084
ferrandina|mt|40.501218|16.454683
garaguso|mt|40.548418|16.227178
gorgoglione|mt|40.394414|16.144578
grassano|mt|40.63262|16.282778
grottole|mt|40.60102|16.37928
irsina|mt|40.749622|16.235775
matera|mt|40.447642|16.473574
miglionico|mt|40.56842|16.502484
montalbano jonico|mt|40.28841|16.568171
montescaglioso|mt|40.552221|16.666887
nova siri|mt|40.148212|16.538889
oliveto lucano|mt|40.535317|16.184577
pisticci|mt|40.390527|16.556732
policoro|mt|40.21046|16.676376
pomarico|mt|40.517944|16.545945
rotondella|mt|40.171312|16.525589
salandra|mt|40.527518|16.31918
san giorgio lucano|mt|40.11191|16.387986
san mauro forte|mt|40.487117|16.248079
stigliano|mt|40.401238|16.231594
tricarico|mt|40.620076|16.143617
tursi|mt|40.245213|16.469687
valsinni|mt|40.169912|16.443487
scanzano jonico|mt|40.249792|16.69804
acquaformosa|cs|39.7231|16.089885
acquappesa|cs|39.493695|15.952084
acri|cs|39.493005|16.38495
aiello calabro|cs|39.117469|16.166663
aieta|cs|39.928575|15.82398
albidona|cs|39.924121|16.469869
alessandria del carretto|cs|39.958107|16.379488
altilia|cs|39.13049|16.252695
altomonte|cs|39.697882|16.127128
amantea|cs|39.133189|16.074591
amendolara|cs|39.952438|16.582284
aprigliano|cs|39.241693|16.341796
belmonte calabro|cs|39.162203|16.082536
belsito|cs|39.177191|16.288295
belvedere marittimo|cs|39.617973|15.861853
bianchi|cs|39.100891|16.410099
bisignano|cs|39.500197|16.274461
bocchigliero|cs|39.419799|16.749002
bonifati|cs|39.585296|15.900282
buonvicino|cs|39.690177|15.883999
calopezzati|cs|39.560673|16.801158
caloveto|cs|39.50585|16.759549
campana|cs|39.412448|16.822564
canna|cs|40.093911|16.504289
cariati|cs|39.49625|16.947448
carolei|cs|39.256292|16.220093
carpanzano|cs|39.147491|16.303296
cassano all'ionio|cs|39.785022|16.315928
castiglione cosentino|cs|39.351639|16.284431
castrolibero|cs|39.308093|16.194392
castroregio|cs|39.992408|16.47759
castrovillari|cs|39.815494|16.202772
celico|cs|39.310094|16.339895
cellara|cs|39.218492|16.334796
cerchiara di calabria|cs|39.860405|16.382789
cerisano|cs|39.276092|16.175392
cervicati|cs|39.542497|16.128287
cerzeto|cs|39.507796|16.114988
cetraro|cs|39.516595|15.941584
civita|cs|39.828804|16.312488
cleto|cs|39.090189|16.158393
colosimi|cs|39.119091|16.399698
cosenza|cs|39.596685|16.333056
cropalati|cs|39.517201|16.726501
crosia|cs|39.567602|16.772801
diamante|cs|39.67773|15.821128
dipignano|cs|39.237492|16.253094
domanico|cs|39.219691|16.206093
fagnano castello|cs|39.565697|16.053673
falconara albanese|cs|39.275892|16.09179
figline vegliaturo|cs|39.225192|16.330296
firmo|cs|39.721401|16.170186
fiumefreddo bruzio|cs|39.234427|16.069517
francavilla marittima|cs|39.817846|16.386644
frascineto|cs|39.836404|16.261987
fuscaldo|cs|39.414294|16.029787
grimaldi|cs|39.14199|16.235694
grisolia|cs|39.724499|15.854179
guardia piemontese|cs|39.466163|15.999259
lago|cs|39.166767|16.149135
laino borgo|cs|39.95352|15.973719
laino castello|cs|39.936304|15.97658
lappano|cs|39.319506|16.312166
lattarico|cs|39.463378|16.137829
longobardi|cs|39.20709|16.07649
longobucco|cs|39.448299|16.610499
lungro|cs|39.742265|16.122825
luzzi|cs|39.446105|16.287255
maierà|cs|39.717518|15.849327
malito|cs|39.15499|16.248595
malvito|cs|39.599698|16.053285
mandatoriccio|cs|39.467201|16.834804
mangone|cs|39.205592|16.331996
marano marchesato|cs|39.315993|16.173791
marano principato|cs|39.299593|16.175091
marzi|cs|39.170791|16.306396
mendicino|cs|39.263596|16.195352
mongrassano|cs|39.526897|16.110587
montalto uffugo|cs|39.404795|16.15799
montegiordano|cs|40.04301|16.534391
morano calabro|cs|39.843941|16.135613
mormanno|cs|39.888303|15.98678
mottafollone|cs|39.648699|16.063385
nocara|cs|40.099411|16.481889
oriolo|cs|40.053809|16.446088
orsomarso|cs|39.800172|15.909528
paludi|cs|39.529394|16.681569
panettieri|cs|39.05999|16.4567
paola|cs|39.359893|16.039588
papasidero|cs|39.871915|15.905552
parenti|cs|39.162192|16.410498
paterno calabro|cs|39.228492|16.264894
pedivigliano|cs|39.10949|16.302896
piane crati|cs|39.234093|16.325495
pietrafitta|cs|39.261393|16.338595
pietrapaola|cs|39.487501|16.815803
plataci|cs|39.900406|16.43199
praia a mare|cs|39.900902|15.779276
rende|cs|39.331993|16.184383
rocca imperiale|cs|40.109711|16.580191
roggiano gravina|cs|39.618199|16.159687
rogliano|cs|39.178764|16.323409
rose|cs|39.398495|16.287193
roseto capo spulico|cs|39.987009|16.601693
rota greca|cs|39.468496|16.112588
rovito|cs|39.307994|16.321094
san basile|cs|39.809603|16.163285
san benedetto ullano|cs|39.427095|16.123789
san cosmo albanese|cs|39.5831|16.420193
san demetrio corone|cs|39.569899|16.361492
san donato di ninea|cs|39.7116|16.048491
san fili|cs|39.338893|16.14349
sangineto|cs|39.604797|15.914882
san giorgio albanese|cs|39.581818|16.454187
san giovanni in fiore|cs|39.252976|16.697377
san lorenzo bellizzi|cs|39.889205|16.330288
san lorenzo del vallo|cs|39.668201|16.29739
san lucido|cs|39.308892|16.050788
san marco argentano|cs|39.557408|16.119089
san martino di finita|cs|39.490596|16.109788
san nicola arcella|cs|39.848174|15.794971
san pietro in amantea|cs|39.136389|16.111592
san pietro in guarano|cs|39.342094|16.311247
san sosti|cs|39.660948|16.028228
santa caterina albanese|cs|39.586119|16.070479
santa domenica talao|cs|39.819501|15.854578
sant'agata di esaro|cs|39.622798|15.983783
santa maria del cedro|cs|39.746199|15.836379
santa sofia d'epiro|cs|39.54709|16.329117
santo stefano di rogliano|cs|39.192492|16.318196
san vincenzo la costa|cs|39.365194|16.15169
saracena|cs|39.776837|16.156472
scala coeli|cs|39.449801|16.885405
scalea|cs|39.81666|15.790547
scigliano|cs|39.128688|16.308213
serra d'aiello|cs|39.090288|16.127493
spezzano albanese|cs|39.668801|16.31049
spezzano della sila|cs|39.299411|16.33945
tarsia|cs|39.6209|16.27169
terranova da sibari|cs|39.656901|16.339491
terravecchia|cs|39.464901|16.946006
torano castello|cs|39.506296|16.145588
tortora|cs|39.941303|15.805176
trebisacce|cs|39.870107|16.530667
vaccarizzo albanese|cs|39.5854|16.432894
verbicaro|cs|39.7556|15.91358
villapiana|cs|39.846505|16.455191
zumpano|cs|39.310894|16.292094
casali del manco|cs|39.294833|16.450109
corigliano-rossano|cs|39.616315|16.577896
albi|cz|39.02499|16.596404
amaroni|cz|38.793085|16.448103
amato|cz|38.941788|16.462202
andali|cz|39.013391|16.768407
argusto|cz|38.680783|16.435404
badolato|cz|38.568681|16.524607
belcastro|cz|39.017992|16.786408
borgia|cz|38.826186|16.508804
botricello|cz|38.93199|16.85831
caraffa di catanzaro|cz|38.881287|16.485603
cardinale|cz|38.642336|16.38748
carlopoli|cz|39.05669|16.4549
catanzaro|cz|38.82996|16.431557
cenadi|cz|38.719483|16.417203
centrache|cz|38.728984|16.432103
cerva|cz|39.018824|16.742749
chiaravalle centrale|cz|38.680383|16.410603
cicala|cz|39.02219|16.486101
conflenti|cz|39.071689|16.284496
cortale|cz|38.840086|16.409602
cropani|cz|38.967791|16.781308
curinga|cz|38.826185|16.3134
davoli|cz|38.647383|16.486205
decollatura|cz|39.047789|16.355098
falerna|cz|38.995559|16.162568
feroleto antico|cz|38.961095|16.388327
fossato serralta|cz|38.99629|16.579604
gagliato|cz|38.676978|16.462646
gasperina|cz|38.73965|16.507381
gimigliano|cz|38.974489|16.530503
girifalco|cz|38.823355|16.426384
gizzeria|cz|38.979987|16.203296
guardavalle|cz|38.50498|16.503507
isca sullo ionio|cz|38.601282|16.519607
jacurso|cz|38.84618|16.380042
magisano|cz|39.01339|16.627904
maida|cz|38.858841|16.364824
marcedusa|cz|39.027792|16.836309
marcellinara|cz|38.928093|16.493077
martirano|cz|39.081748|16.248479
martirano lombardo|cz|39.074949|16.232759
miglierina|cz|38.948986|16.471932
montauro|cz|38.74877|16.51291
montepaone|cz|38.721458|16.498972
motta santa lucia|cz|39.09189|16.292396
nocera terinese|cz|39.036893|16.160718
olivadi|cz|38.725584|16.423703
palermiti|cz|38.748534|16.452216
pentone|cz|38.98579|16.582504
petrizzi|cz|38.701383|16.473005
petronà|cz|39.043692|16.755907
pianopoli|cz|38.953917|16.388125
platania|cz|39.005388|16.320098
san floro|cz|38.837986|16.518704
san mango d'aquino|cz|39.060284|16.193707
san pietro a maida|cz|38.846804|16.343454
san pietro apostolo|cz|39.004389|16.468401
san sostene|cz|38.637882|16.487106
santa caterina dello ionio|cz|38.532681|16.522007
sant'andrea apostolo dello ionio|cz|38.622582|16.528307
san vito sullo ionio|cz|38.711783|16.409703
satriano|cz|38.666883|16.481205
sellia|cz|38.982284|16.629684
sellia marina|cz|38.899625|16.74433
serrastretta|cz|39.013189|16.4163
sersale|cz|39.010891|16.726907
settingiano|cz|38.911388|16.513903
simeri crichi|cz|38.909644|16.656018
sorbo san basile|cz|39.01939|16.569103
soverato|cz|38.687717|16.550044
soveria mannelli|cz|39.08289|16.371898
soveria simeri|cz|38.946589|16.680106
squillace|cz|38.779485|16.512205
stalettì|cz|38.765485|16.537605
taverna|cz|39.02179|16.581803
tiriolo|cz|38.943288|16.508303
torre di ruggiero|cz|38.653382|16.372803
vallefiorita|cz|38.776385|16.461003
zagarise|cz|38.99919|16.663205
lamezia terme|cz|38.964507|16.302145
africo|rc|38.050669|16.133204
agnana calabra|rc|38.302674|16.222904
anoia|rc|38.436192|16.080633
antonimina|rc|38.272273|16.148402
ardore|rc|38.19097|16.168222
bagaladi|rc|38.026667|15.821398
bagnara calabra|rc|38.288471|15.807795
benestare|rc|38.185372|16.137003
bianco|rc|38.09067|16.149304
bivongi|rc|38.48317|16.453054
bova|rc|37.995067|15.931901
bovalino|rc|38.148971|16.174004
bova marina|rc|37.930625|15.917452
brancaleone|rc|37.962968|16.100405
bruzzano zeffirio|rc|38.013268|16.082604
calanna|rc|38.183669|15.723094
camini|rc|38.432257|16.481958
campo calabro|rc|38.216069|15.659192
candidoni|rc|38.505477|16.086298
canolo|rc|38.315043|16.199783
caraffa del bianco|rc|38.09267|16.086403
cardeto|rc|38.084567|15.766596
careri|rc|38.176571|16.117803
casignana|rc|38.10187|16.089903
caulonia|rc|38.381377|16.409007
ciminà|rc|38.246819|16.138812
cinquefrondi|rc|38.415957|16.097429
cittanova|rc|38.353949|16.080602
condofuri|rc|38.004659|15.857397
cosoleto|rc|38.275372|15.927997
delianuova|rc|38.234071|15.917798
feroleto della chiesa|rc|38.465776|16.064198
ferruzzano|rc|38.038715|16.087049
fiumara|rc|38.212669|15.693693
galatro|rc|38.45974|16.110777
gerace|rc|38.27233|16.219753
giffone|rc|38.438176|16.15
gioia tauro|rc|38.423974|15.898995
gioiosa ionica|rc|38.322175|16.302605
grotteria|rc|38.365476|16.264904
laganadi|rc|38.173369|15.741395
laureana di borrello|rc|38.492577|16.082098
locri|rc|38.238537|16.265509
mammola|rc|38.378051|16.203148
marina di gioiosa ionica|rc|38.300663|16.330362
maropati|rc|38.440632|16.099161
martone|rc|38.353976|16.288304
melicuccà|rc|38.303772|15.881296
melicucco|rc|38.433713|16.060471
melito di porto salvo|rc|37.920384|15.777857
molochio|rc|38.308445|16.031862
monasterace|rc|38.453679|16.552009
montebello jonico|rc|37.982766|15.757997
motta san giovanni|rc|38.003165|15.695096
oppido mamertina|rc|38.293593|15.98414
palizzi|rc|37.966667|15.986702
palmi|rc|38.357449|15.846565
pazzano|rc|38.475763|16.452501
placanica|rc|38.411878|16.452507
platì|rc|38.219972|16.046501
polistena|rc|38.406509|16.077455
portigliola|rc|38.227673|16.202204
reggio di calabria|rc|38.103539|15.639756
riace|rc|38.418295|16.481189
rizziconi|rc|38.411996|15.95924
roccaforte del greco|rc|38.047268|15.894699
roccella ionica|rc|38.321976|16.400907
roghudi|rc|37.927565|15.765998
rosarno|rc|38.487283|15.976495
samo|rc|38.073769|16.058302
san giorgio morgeto|rc|38.386958|16.108164
san giovanni di gerace|rc|38.365076|16.277704
san lorenzo|rc|38.010967|15.834398
san luca|rc|38.148371|16.066002
san pietro di caridà|rc|38.523678|16.135299
san procopio|rc|38.281172|15.890397
san roberto|rc|38.211269|15.736594
santa cristina d'aspromonte|rc|38.255372|15.970499
sant'agata del bianco|rc|38.092788|16.083088
sant'alessio in aspromonte|rc|38.172569|15.757795
sant'eufemia d'aspromonte|rc|38.263471|15.854796
sant'ilario dello ionio|rc|38.218773|16.195604
santo stefano in aspromonte|rc|38.169169|15.789196
scido|rc|38.243571|15.932698
scilla|rc|38.25067|15.718993
seminara|rc|38.335093|15.871255
serrata|rc|38.512877|16.100699
siderno|rc|38.276163|16.29816
sinopoli|rc|38.263771|15.878497
staiti|rc|38.000068|16.032803
stignano|rc|38.416078|16.470308
stilo|rc|38.476612|16.467263
taurianova|rc|38.355054|16.014778
terranova sappo minulio|rc|38.322673|16.007499
varapodio|rc|38.317073|15.984398
villa san giovanni|rc|38.220112|15.637381
san ferdinando|rc|38.48317|15.916271
belvedere di spinello|kr|39.211944|16.889318
caccuri|kr|39.227416|16.778569
carfizzi|kr|39.30636|16.973792
casabona|kr|39.250284|16.960659
castelsilano|kr|39.269034|16.767453
cerenzia|kr|39.244086|16.788333
cirò|kr|39.381732|17.061353
cirò marina|kr|39.369092|17.132857
cotronei|kr|39.15835|16.773505
crotone|kr|39.187389|16.878282
crucoli|kr|39.423701|17.002308
cutro|kr|39.035445|16.979263
isola di capo rizzuto|kr|38.959993|17.093315
melissa|kr|39.307373|17.025987
mesoraca|kr|39.080127|16.789599
pallagorio|kr|39.307693|16.910984
petilia policastro|kr|39.10941|16.788654
roccabernarda|kr|39.136087|16.869791
rocca di neto|kr|39.184386|17.009067
san mauro marchesato|kr|39.104406|16.924439
san nicola dell'alto|kr|39.291348|16.972244
santa severina|kr|39.147901|16.912776
savelli|kr|39.310837|16.778523
scandale|kr|39.122213|16.964123
strongoli|kr|39.263959|17.052026
umbriatico|kr|39.354501|16.91871
verzino|kr|39.311098|16.860406
acquaro|vv|38.554779|16.1872
arena|vv|38.562779|16.2099
briatico|vv|38.725981|16.032895
brognaturo|vv|38.601281|16.341403
capistrano|vv|38.692282|16.289601
cessaniti|vv|38.66368|16.026795
dasà|vv|38.564879|16196
dinami|vv|38.527776|16.147958
drapia|vv|38.664524|15.910363
fabrizia|vv|38.487078|16.301003
filadelfia|vv|38.783764|16.291088
filandari|vv|38.615079|16.031396
filogaso|vv|38.681481|16.228699
francavilla angitola|vv|38.780184|16.271699
francica|vv|38.615679|16.099297
gerocarne|vv|38.587478|16.219813
ionadi|vv|38.636334|16.059571
joppolo|vv|38.58384|15.897318
limbadi|vv|38.557477|15.965895
maierato|vv|38.705048|16.19007
mileto|vv|38.609262|16.069491
mongiana|vv|38.512879|16.319503
monterosso calabro|vv|38.716999|16.291059
nardodipace|vv|38.472278|16.342504
nicotera|vv|38.554577|15.936695
parghelia|vv|38.681736|15.921798
pizzo|vv|38.735436|16.162085
pizzoni|vv|38.623081|16.251901
polia|vv|38.751183|16.3122
ricadi|vv|38.625483|15.86643
rombiolo|vv|38.591778|16.001696
san calogero|vv|38.575578|16.020296
san costantino calabro|vv|38.632279|16.073897
san gregorio d'ippona|vv|38.642334|16.101976
san nicola da crissa|vv|38.663636|16.284679
sant'onofrio|vv|38.694981|16.144097
serra san bruno|vv|38.574608|16.326074
simbario|vv|38.612109|16.334919
sorianello|vv|38.59328|16.231901
soriano calabro|vv|38.596934|16.230355
spadola|vv|38.603802|16.337075
spilinga|vv|38.628412|15.906178
stefanaconi|vv|38.677881|16.124197
tropea|vv|38.675379|15.894792
vallelonga|vv|38.647281|16.294901
vazzano|vv|38.632881|16248
vibo valentia|vv|38.626656|16.098693
zaccanopoli|vv|38.664779|15.929793
zambrone|vv|38.69918|15.990094
zungri|vv|38.65569|15.983246
alcamo|tp|37.97665|12.962674
buseto palizzolo|tp|38.012506|12.711586
calatafimi-segesta|tp|37.913638|12.861893
campobello di mazara|tp|37.635838|12.744338
castellammare del golfo|tp|38.018846|12.885837
castelvetrano|tp|37.682851|12.795534
custonaci|tp|38.078163|12.675229
erice|tp|38.03823|12.587182
favignana|tp|37.931402|12.327429
gibellina|tp|37.807042|12.869539
marsala|tp|37.797922|12.434209
mazara del vallo|tp|37.653729|12.588691
paceco|tp|37.981297|12.557309
pantelleria|tp|36.831458|11.94504
partanna|tp|37.727841|12.88854
poggioreale|tp|37.762096|13.034318
salaparuta|tp|37.757942|13.009742
salemi|tp|37.817371|12.800945
santa ninfa|tp|37.771342|12.877639
san vito lo capo|tp|38.172347|12.734932
trapani|tp|38.017432|12.515992
valderice|tp|38.038984|12.616979
vita|tp|37.868575|12.814479
petrosino|tp|37.71624|12.494547
misiliscemi|tp|37.925714|12.537552
alia|pa|37.779948|13.713856
alimena|pa|37.693476|14.114651
aliminusa|pa|37.864509|13.78116
altavilla milicia|pa|38.042734|13.550893
altofonte|pa|38.044567|13.298702
bagheria|pa|38.079351|13.509349
balestrate|pa|38.051867|13.004266
baucina|pa|37.925429|13.53771
belmonte mezzagno|pa|38.048449|13.390554
bisacquino|pa|37.705431|13.258761
bolognetta|pa|37.964742|13.45607
bompietro|pa|37.746863|14.099838
borgetto|pa|38.047103|13.142684
caccamo|pa|37.9318|13.66349
caltavuturo|pa|37.821066|13.890224
campofelice di fitalia|pa|37.826685|13.485936
campofelice di roccella|pa|37.992334|13.886183
campofiorito|pa|37.753636|13.268767
camporeale|pa|37.897145|13.093842
capaci|pa|38.170718|13.23917
carini|pa|38.131172|13.181885
castelbuono|pa|37.931953|14.088562
casteldaccia|pa|38.054118|13.530226
castellana sicula|pa|37.783179|14.041051
castronovo di sicilia|pa|37.678945|13.603455
cefalà diana|pa|37.915175|13.463549
cefalù|pa|38.034957|14.024456
cerda|pa|37.903579|13.81547
chiusa sclafani|pa|37.67722|13.270521
ciminna|pa|37.896816|13.562345
cinisi|pa|38.154943|13.109841
collesano|pa|37.921477|13.937486
contessa entellina|pa|37.73098|13.183144
corleone|pa|37.812078|13.301438
ficarazzi|pa|38.088458|13.463786
gangi|pa|37.795627|14.20672
geraci siculo|pa|37.859137|14.154416
giardinello|pa|38.088049|13.157041
giuliana|pa|37.670591|13.237804
godrano|pa|37.903344|13.428924
gratteri|pa|37.967377|13.97393
isnello|pa|37.943953|14.005761
isola delle femmine|pa|38.198295|13.247344
lascari|pa|38.000053|13.941459
lercara friddi|pa|37.746946|13.604854
marineo|pa|37.952893|13.415024
mezzojuso|pa|37.864532|13.464195
misilmeri|pa|38.03522|13.451404
monreale|pa|38.082632|13.291993
montelepre|pa|38.088964|13.171343
montemaggiore belsito|pa|37.847752|13.762048
palazzo adriano|pa|37.681254|13.379266
palermo|pa|38.111227|13.352443
partinico|pa|38.043896|13.119821
petralia soprana|pa|37.79634|14.108468
petralia sottana|pa|37.808225|14.092513
piana degli albanesi|pa|37.994836|13.282689
polizzi generosa|pa|37.812819|13.999683
pollina|pa|37.992894|14.145799
prizzi|pa|37.721453|13.431265
roccamena|pa|37.836028|13.153858
roccapalumba|pa|37.805948|13.638455
san cipirello|pa|37.96296|13.17538
san giuseppe jato|pa|37.970486|13.182489
san mauro castelverde|pa|37.914628|14.190378
santa cristina gela|pa|37.984932|13.328096
santa flavia|pa|38.081331|13.52544
sciara|pa|37.914892|13.762343
sclafani bagni|pa|37.821589|13.85478
termini imerese|pa|37.983651|13.695554
terrasini|pa|38.152822|13.082122
torretta|pa|38.130061|13.233501
trabia|pa|37.994386|13.65651
trappeto|pa|38.067008|13.03655
ustica|pa|38.710381|13.191874
valledolmo|pa|37.747448|13.827859
ventimiglia di sicilia|pa|37.923341|13.569255
vicari|pa|37.825092|13.571302
villabate|pa|38.076528|13.443667
villafrati|pa|37.908012|13.48635
scillato|pa|37.856724|13.906854
blufi|pa|37.751474|14.074827
alcara li fusi|me|38.021318|14.701396
alì|me|38.027364|15.417689
alì terme|me|38.005064|15.42269
antillo|me|37.977262|15.244786
barcellona pozzo di gotto|me|38.148306|15.217552
basicò|me|38.061393|15.062248
brolo|me|38.155362|14.829575
capizzi|me|37.848909|14.478473
capo d'orlando|me|38.159573|14.745073
capri leone|me|38.085874|14.730118
caronia|me|38.024707|14.440584
casalvecchio siculo|me|37.958162|15.322588
castel di lucio|me|37.886454|14.312168
castell'umberto|me|38.08437|14.807122
castelmola|me|37.858348|15.277316
castroreale|me|38.099272|15.210686
cesarò|me|37.844756|14.713076
condrò|me|38.173177|15.326943
falcone|me|38.117482|15.079611
ficarra|me|38.109061|14.828976
fiumedinisi|me|38.026238|15.382083
floresta|me|37.987351|14.909714
fondachelli-fantina|me|38.000627|15.182419
forza d'agrò|me|37.91372|15.335449
francavilla di sicilia|me|37.901129|15.136208
frazzanò|me|38.072269|14.744953
furci siculo|me|37.961204|15.381735
furnari|me|38.104671|15.125223
gaggi|me|37.860091|15.221412
galati mamertino|me|38.031559|14.772517
gallodoro|me|37.90183|15.293695
giardini-naxos|me|37.835441|15.271845
gioiosa marea|me|38.173269|14.895164
graniti|me|37.890173|15.224898
gualtieri sicaminò|me|38.163249|15.316544
itala|me|38.051059|15.436903
leni|me|38.554469|14.825471
letojanni|me|37.880276|15.306866
librizzi|me|38.096866|14.95834
limina|me|37.940774|15.272668
lipari|me|38.468185|14.95472
longi|me|38.027271|14.752656
malfa|me|38.57597|14.835471
malvagna|me|37.918393|15.055383
mandanici|me|38.003586|15.317095
mazzarrà sant'andrea|me|38.088463|15.133583
merì|me|38.166817|15.25037
messina|me|38.193757|15.554208
milazzo|me|38.220805|15.241513
militello rosmarino|me|38.045917|14.676017
mirto|me|38.08466|14.751475
mistretta|me|37.929885|14.362873
moio alcantara|me|37.89967|15.049043
monforte san giorgio|me|38.156401|15.383256
mongiuffi melia|me|37.905877|15.255718
montagnareale|me|38.132354|14.946482
montalbano elicona|me|38.023409|15.013672
motta camastra|me|37.894319|15.170271
motta d'affermo|me|37.981173|14.302061
naso|me|38.122118|14.78898
nizza di sicilia|me|37.992622|15.411913
novara di sicilia|me|38.016082|15.13183
oliveri|me|38.123481|15.060849
pace del mela|me|38.179625|15.305332
pagliara|me|37.985463|15.359689
patti|me|38.138901|14.964816
pettineo|me|37.966364|14.29191
piraino|me|38.161243|14.861943
raccuja|me|38.055261|14.909578
reitano|me|37.97284|14.34429
roccafiorita|me|37.930197|15.268096
roccalumera|me|37.976263|15.395189
roccavaldina|me|38.181484|15.372556
roccella valdemone|me|37.932983|15.008455
rodì milici|me|38.068965|15.174781
rometta|me|38.171115|15.41455
san filippo del mela|me|38.169727|15.272527
san fratello|me|38.013442|14.597392
san marco d'alunzio|me|38.073514|14.699355
san pier niceto|me|38.158918|15.351874
san piero patti|me|38.05215|14.967069
san salvatore di fitalia|me|38.071713|14.777616
santa domenica vittoria|me|37.916744|14.962592
sant'agata di militello|me|38.068062|14.633334
sant'alessio siculo|me|37.924494|15.349288
santa lucia del mela|me|38.143461|15.280676
santa marina salina|me|38.56117|14.870772
sant'angelo di brolo|me|38.113879|14.882663
santa teresa di riva|me|37.946962|15.367689
san teodoro|me|37.847208|14.7001
santo stefano di camastra|me|38.015444|14.349027
saponara|me|38.19382|15.434458
savoca|me|37.955859|15.339322
scaletta zanclea|me|38.045591|15.462916
sinagra|me|38.083051|14.849338
spadafora|me|38.223767|15.379286
taormina|me|37.851792|15.285652
torregrotta|me|38.191637|15.352439
tortorici|me|38.030103|14.82419
tripi|me|38.048625|15.098804
tusa|me|37.983855|14.236065
ucria|me|38.045919|14.881112
valdina|me|38.193739|15.370704
venetico|me|38.219944|15.366707
villafranca tirrena|me|38.240068|15.437387
terme vigliatore|me|38.136764|15.162675
acquedolci|me|38.05604|14.581901
torrenova|me|38.089846|14.678879
agrigento|ag|37.312299|13.57465
alessandria della rocca|ag|37.570042|13.454609
aragona|ag|37.403199|13.617948
bivona|ag|37.617397|13.439769
burgio|ag|37.599442|13.29015
calamonaci|ag|37.52614|13.289851
caltabellotta|ag|37.577341|13.216548
camastra|ag|37.252294|13.7936
cammarata|ag|37.633825|13.637745
campobello di licata|ag|37.25844|13.918266
canicattì|ag|37.358942|13.850364
casteltermini|ag|37.540835|13.643187
castrofilippo|ag|37.348585|13.750096
cattolica eraclea|ag|37.44084|13.392054
cianciana|ag|37.519696|13.43408
comitini|ag|37.407941|13.642959
favara|ag|37.316197|13.662417
grotte|ag|37.402853|13.70067
joppolo giancaxio|ag|37.38704|13.555258
lampedusa e linosa|ag|35.511134|12.596291
licata|ag|37.101636|13.937615
lucca sicula|ag|37.579049|13.306568
menfi|ag|37.606639|12.967843
montallegro|ag|37.392939|13.350753
montevago|ag|37.702241|12.985842
naro|ag|37.29622|13.791567
palma di montechiaro|ag|37.191664|13.763714
porto empedocle|ag|37.288313|13.52755
racalmuto|ag|37.40894|13.732402
raffadali|ag|37.404053|13.532076
ravanusa|ag|37.267914|13.973255
realmonte|ag|37.308393|13.461567
ribera|ag|37.502787|13.266188
sambuca di sicilia|ag|37.64774|13.111031
san biagio platani|ag|37.509014|13.524282
san giovanni gemini|ag|37.629936|13.641375
santa elisabetta|ag|37.43246|13.556235
santa margherita di belice|ag|37.691841|13.020143
sant'angelo muxaro|ag|37.479015|13.544316
santo stefano quisquina|ag|37.625027|13.49076
sciacca|ag|37.506479|13.082332
siculiana|ag|37.338086|13.421996
villafranca sicula|ag|37.588141|13.28925
acquaviva platani|cl|37.571999|13.702891
bompensiere|cl|37.473829|13.780745
butera|cl|37.190354|14.183489
caltanissetta|cl|37.489941|14.063162
campofranco|cl|37.510479|13.711044
delia|cl|37.356036|13.927289
gela|cl|37.066436|14.250244
marianopoli|cl|37.597446|13.915763
mazzarino|cl|37.305024|14.214078
milena|cl|37.471521|13.736437
montedoro|cl|37.453508|13.816238
mussomeli|cl|37.577845|13.751259
niscemi|cl|37.14645|14.39418
resuttano|cl|37.678648|14.029464
riesi|cl|37.281961|14.082414
san cataldo|cl|37.491446|13.993516
santa caterina villarmosa|cl|37.591147|14.036265
serradifalco|cl|37.453843|13.880463
sommatino|cl|37.334022|13.992959
sutera|cl|37.525228|13.731316
vallelunga pratameno|cl|37.682047|13.83156
villalba|cl|37.654142|13.843423
agira|en|37.656151|14.521575
aidone|en|37.415421|14.445276
assoro|en|37.627905|14.423886
barrafranca|en|37.372843|14.203313
calascibetta|en|37.590191|14.271972
catenanuova|en|37.567971|14.691819
centuripe|en|37.622952|14.739079
cerami|en|37.810654|14.507273
enna|en|37.566757|14.280747
gagliano castelferrato|en|37.71139|14.536661
leonforte|en|37.639212|14.392427
nicosia|en|37.747452|14.397271
nissoria|en|37.655277|14.449468
piazza armerina|en|37.385946|14.367174
pietraperzia|en|37.421564|14.137026
regalbuto|en|37.653547|14.639636
sperlinga|en|37.765519|14.351626
troina|en|37.785354|14.598475
valguarnera caropepe|en|37.49542|14.390791
villarosa|en|37.587017|14.173589
aci bonaccorsi|ct|37.597687|15.107937
aci castello|ct|37.554211|15.14856
aci catena|ct|37.60257|15.14196
acireale|ct|37.612777|15.16588
aci sant'antonio|ct|37.606392|15.126895
adrano|ct|37.661777|14.833099
belpasso|ct|37.589187|14.978239
biancavilla|ct|37.646276|14.861411
bronte|ct|37.78716|14.833845
calatabiano|ct|37.822445|15.227636
caltagirone|ct|37.237201|14.513202
camporotondo etneo|ct|37.56759|15.005131
castel di iudica|ct|37.494173|14.649715
castiglione di sicilia|ct|37.882761|15.122225
catania|ct|37.502236|15.08738
fiumefreddo di sicilia|ct|37.792355|15.206946
giarre|ct|37.726857|15.187688
grammichele|ct|37.214682|14.636542
gravina di catania|ct|37.560369|15.061763
licodia eubea|ct|37.157144|14.706084
linguaglossa|ct|37.842643|15.142131
maletto|ct|37.829022|14.865664
mascali|ct|37.760285|15.197431
mascalucia|ct|37.573605|15.051727
militello in val di catania|ct|37.274584|14.793878
milo|ct|37.722445|15.115739
mineo|ct|37.264875|14.690849
mirabella imbaccari|ct|37.323804|14.445939
misterbianco|ct|37.519172|15.007936
motta sant'anastasia|ct|37.513391|14.967072
nicolosi|ct|37.614711|15.026349
palagonia|ct|37.327355|14.745332
paternò|ct|37.56707|14.905352
pedara|ct|37.616809|15.060211
piedimonte etneo|ct|37.806707|15.178193
raddusa|ct|37.472556|14.53252
ramacca|ct|37.388048|14.692681
randazzo|ct|37.877934|14.946248
riposto|ct|37.73189|15.204221
san cono|ct|37.289942|14.364986
san giovanni la punta|ct|37.577407|15.094215
san gregorio di catania|ct|37.565254|15.111788
san michele di ganzaria|ct|37.279563|14.42602
san pietro clarenza|ct|37.568853|15.022886
sant'agata li battiati|ct|37.558273|15.080813
sant'alfio|ct|37.74158|15.142905
santa maria di licodia|ct|37.61642|14.887493
santa venerina|ct|37.688971|15.142661
scordia|ct|37.296429|14.846239
trecastagni|ct|37.616116|15.079231
tremestieri etneo|ct|37.576488|15.07322
valverde|ct|37.576954|15.124188
viagrande|ct|37.610166|15.096759
vizzini|ct|37.161016|14.748468
zafferana etnea|ct|37.693285|15.10646
mazzarrone|ct|37.083393|14.561674
maniace|ct|37.862423|14.807835
ragalna|ct|37.636958|14.947876
acate|rg|37.02504|14.492981
chiaramonte gulfi|rg|37.031342|14.701285
comiso|rg|36.950734|14.607455
giarratana|rg|37.048112|14.793797
ispica|rg|36.785832|14.907637
modica|rg|36.858921|14.761302
monterosso almo|rg|37.088874|14.762708
pozzallo|rg|36.72633|14.846343
ragusa|rg|36.921983|14.721345
santa croce camerina|rg|36.827897|14.525538
scicli|rg|36.79357|14.706964
vittoria|rg|36.951496|14.530487
augusta|sr|37.236936|15.219658
avola|sr|36.909516|15.135001
buccheri|sr|37.126491|14.85038
buscemi|sr|37.085471|14.884201
canicattini bagni|sr|37.034928|15.062337
carlentini|sr|37.275881|15.014978
cassaro|sr|37.106937|14.94761
ferla|sr|37.118582|14.941138
floridia|sr|37.086946|15.151894
francofonte|sr|37.226582|14.881881
lentini|sr|37.284685|14.998778
melilli|sr|37.178288|15.126621
noto|sr|36.890886|15.070645
pachino|sr|36.715209|15.091477
palazzolo acreide|sr|37.061913|14.90391
rosolini|sr|36.820499|14.952628
siracusa|sr|37.064614|15.29072
solarino|sr|37.101214|15.119039
sortino|sr|37.156854|15.027716
portopalo di capo passero|sr|36.683845|15.13329
priolo gargallo|sr|37.15793|15.186028
aggius|ss|40.929345|9.065222
alà dei sardi|ss|40.650066|9.327829
alghero|ss|40.56002|8.315147
anela|ss|40.44176|9.057427
ardara|ss|40.621761|8.810319
arzachena|ss|41.079898|9.387612
banari|ss|40.572927|8.697371
benetutti|ss|40.456461|9.168829
berchidda|ss|40.784067|9.164524
bessude|ss|40.553852|8.729952
bonnanaro|ss|40.533359|8.76442
bono|ss|40.41683|9.029993
bonorva|ss|40.418757|8.768121
bortigiadas|ss|40.891351|9.043058
borutta|ss|40.522359|8.743519
bottidda|ss|40.391459|9.008527
buddusò|ss|40.578064|9.258129
bultei|ss|40.456211|9.064444
bulzi|ss|40.847366|8.830016
burgos|ss|40.389978|8.995335
calangianus|ss|40.919774|9.194819
cargeghe|ss|40.669461|8.614515
castelsardo|ss|40.910466|8.718313
cheremule|ss|40.506358|8.725319
chiaramonti|ss|40.749064|8.818618
codrongianos|ss|40.657261|8.679816
cossoine|ss|40.43099|8.715016
esporlatu|ss|40.386607|8.989713
florinas|ss|40.649561|8.665116
giave|ss|40.451958|8.752221
illorai|ss|40.353858|9.001027
ittireddu|ss|40.543961|8.902322
ittiri|ss|40.593046|8.568353
laerru|ss|40.817065|8.835417
la maddalena|ss|41.212783|9.40666
luogosanto|ss|41.054656|9.20409
luras|ss|40.934858|9.175447
mara|ss|40.409556|8.638819
martis|ss|40.778664|8.808117
monteleone rocca doria|ss|40.471622|8.560299
monti|ss|40.805969|9.325527
mores|ss|40.54836|8.831221
muros|ss|40.678061|8.617315
nughedu san nicolò|ss|40.556508|9.021463
nule|ss|40.461561|9.189829
nulvi|ss|40.783764|8.743716
olbia|ss|40.923266|9.502744
olmedo|ss|40.651558|8.38021
oschiri|ss|40.719766|9.101024
osilo|ss|40.744063|8.671115
ossi|ss|40.67486|8.591714
ozieri|ss|40.585937|9.001043
padria|ss|40.396355|8.629919
palau|ss|41.181076|9.382042
pattada|ss|40.581763|9.112926
perfugas|ss|40.833366|8.882818
ploaghe|ss|40.664262|8.741817
porto torres|ss|40.83444|8.403146
pozzomaggiore|ss|40.398756|8.660219
putifigari|ss|40.562257|8.461113
romana|ss|40.484057|8.587417
aglientu|ss|41.079573|9.112719
santa teresa gallura|ss|41.241377|9.188518
sassari|ss|40.7778|8.921997
sedini|ss|40.852266|8.815716
semestene|ss|40.398456|8.725421
sennori|ss|40.789446|8.591659
siligo|ss|40.57648|8.727273
sorso|ss|40.796595|8.57785
tempio pausania|ss|40.897382|9.103516
thiesi|ss|40.524859|8.716419
tissi|ss|40.677673|8.5627
torralba|ss|40.512959|8.76532
trinità d'agultu e vignola|ss|40.983769|8.915616
tula|ss|40.732465|8.983921
uri|ss|40.639459|8.485412
usini|ss|40.66406|8.540513
villanova monteleone|ss|40.503656|8.470807
valledoria|ss|40.928283|8.824584
telti|ss|40.876371|9.352827
badesi|ss|40.965069|8.884216
viddalba|ss|40.916777|8.894363
golfo aranci|ss|41.006476|9.61453
loiri porto san paolo|ss|40.876305|9.62949
sant'antonio di gallura|ss|40.991772|9.301041
tergu|ss|40.867465|8.716714
santa maria coghinas|ss|40.904267|8.868616
erula|ss|40.791166|8.942219
stintino|ss|40.939162|8.225503
padru|ss|40.76577|9.520232
budoni|ss|40.707631|9.69949
san teodoro|ss|40.770511|9.673923
aritzo|nu|39.956777|9.196562
arzana|nu|39.919463|9527
atzara|nu|39.992252|9.075734
austis|nu|40.070653|9.087933
bari sardo|nu|39.842053|9.644147
baunei|nu|40.032361|9.663405
belvì|nu|39.962601|9.185185
birori|nu|40.26486|8.817474
bitti|nu|40.479008|9.384038
bolotana|nu|40.325757|8.961127
borore|nu|40.216854|8.803725
bortigali|nu|40.282855|8.836225
desulo|nu|40.010756|9.228296
dorgali|nu|40.292802|9.588832
dualchi|nu|40.228855|8.896427
elini|nu|39.8999|9.531347
fonni|nu|40.118804|9.251971
gadoni|nu|39.913451|9.185837
gairo|nu|39.847052|9.507244
galtellì|nu|40.386589|9.614322
gavoi|nu|40.161756|9.195134
girasole|nu|39.951719|9.66196
ilbono|nu|39.892654|9.548945
irgoli|nu|40.408833|9.630121
jerzu|nu|39.791151|9.518445
lanusei|nu|39.87857|9.541077
lei|nu|40.3044|8.918038
loceri|nu|39.856635|9.582209
loculi|nu|40.407278|9.61116
lodè|nu|40.591387|9.539795
lotzorai|nu|39.969155|9.663715
lula|nu|40.470364|9.486435
macomer|nu|40.26519|8.781387
mamoiada|nu|40.215657|9.280435
meana sardo|nu|39.944251|9.072334
noragugume|nu|40.224555|8.920727
nuoro|nu|40.127683|9.342323
oliena|nu|40.27226|9.403036
ollolai|nu|40.168503|9.178467
olzai|nu|40.182856|9.148332
onanì|nu|40.485326|9.441168
onifai|nu|40.406964|9.651339
oniferi|nu|40.271958|9.170332
orani|nu|40.248344|9.182835
orgosolo|nu|40.204758|9.354936
orosei|nu|40.37918|9.695014
orotelli|nu|40.305803|9.112838
ortueri|nu|40.035642|8.98725
orune|nu|40.407595|9.369739
osidda|nu|40.523863|9.219329
osini|nu|39.822752|9.496244
ottana|nu|40.233499|9.044599
ovodda|nu|40.095654|9.162334
perdasdefogu|nu|39.679218|9.440049
posada|nu|40.631969|9.716437
sarule|nu|40.22912|9.167342
silanus|nu|40.288864|8.890493
sindia|nu|40.296216|8.656256
siniscola|nu|40.571868|9.692238
sorgono|nu|40.025952|9.102034
talana|nu|40.042056|9.496541
tertenia|nu|39.696757|9.577684
teti|nu|40.095754|9.118133
tiana|nu|40.068454|9.147334
tonara|nu|40.025134|9.173144
torpè|nu|40.627969|9.677337
tortolì|nu|39.926604|9.659155
triei|nu|40.037127|9.640503
ulassai|nu|39.810552|9.499045
urzulei|nu|40.093338|9.508049
ussassai|nu|39.810827|9.394786
villagrande strisaili|nu|39.96139|9.507272
cardedu|nu|39.797184|9.62473
lodine|nu|40.14808|9.218294
assemini|ca|39.291538|9.001542
cagliari|ca|39.216953|9.112851
capoterra|ca|39.175313|8.97195
decimomannu|ca|39.311738|8.968841
maracalagonis|ca|39.28534|9.229446
pula|ca|39.007433|9.001646
quartu sant'elena|ca|39.239973|9.188097
sarroch|ca|39.067234|9.009445
selargius|ca|39.25695|9.167554
sestu|ca|39.294248|9.092972
settimo san pietro|ca|39.29174|9.185145
sinnai|ca|39.30424|9.203538
uta|ca|39.290979|8.95426
villa san pietro|ca|39.034048|8.995484
quartucciu|ca|39.254139|9.178846
elmas|ca|39.268086|9.049631
monserrato|ca|39.25951|9.145856
abbasanta|or|40.126752|8.818027
aidomaggiore|or|40.170753|8.856527
albagiara|or|39.787646|8.862032
ales|or|39.768245|8.815632
allai|or|39.957173|8.863076
arborea|or|39.773846|8.581753
ardauli|or|40.084652|8.911529
assolo|or|39.809947|8.918233
asuni|or|39.872448|8.946433
baradili|or|39.721345|8.897434
baratili san pietro|or|39.992831|8.555784
baressa|or|39.712745|8.875334
bauladu|or|40.020549|8.671925
bidonì|or|40.113053|8.936429
bonarcado|or|40.09585|8.657124
boroneddu|or|40.113052|8.871028
busachi|or|40.032651|8.896729
cabras|or|39.930314|8.532054
cuglieri|or|40.188472|8.567405
fordongianus|or|39.994549|8.809828
ghilarza|or|40.119652|8.834927
gonnoscodina|or|39.699996|8.835508
gonnosnò|or|39.761176|8.872328
gonnostramatza|or|39.681169|8.833845
marrubiu|or|39.752143|8.637228
masullas|or|39.69956|8.782724
milis|or|40.050449|8.637024
mogorella|or|39.864598|8.857931
mogoro|or|39.68471|8.777088
morgongiori|or|39.746644|8.770131
narbolia|or|40.04765|8.575169
neoneli|or|40.064752|8.94673
norbello|or|40.134752|8.832027
nughedu santa vittoria|or|40.102075|8.953508
nurachi|or|39.973394|8.540165
nureci|or|39.823348|8.973834
ollastra|or|39.950948|8.734227
oristano|or|40.026568|8.679642
palmas arborea|or|39.876646|8.643127
pau|or|39.791546|8.802331
paulilatino|or|40.084032|8.763937
pompu|or|39.724544|8.795432
riola sardo|or|39.994102|8.538673
ruinas|or|39.907148|8.897631
samugheo|or|39.94615|8.940532
san nicolò d'arcidano|or|39.683392|8.646875
santa giusta|or|39.878178|8.609973
villa sant'antonio|or|39.859148|8.901532
santu lussurgiu|or|40.141686|8.651038
san vero milis|or|40.014548|8.598224
scano di montiferro|or|40.21633|8.584092
sedilo|or|40.173151|8.919122
seneghe|or|40.082249|8.615723
senis|or|39.824147|8.940033
sennariolo|or|40.212673|8.555178
siamaggiore|or|39.950147|8.636025
siamanna|or|39.920748|8.761028
simala|or|39.721644|8.826732
simaxis|or|39.930347|8.692127
sini|or|39.754146|8.904334
siris|or|39.712644|8.774832
solarussa|or|39.955747|8.671526
sorradile|or|40.106453|8.933029
tadasuni|or|40.110458|8.883744
terralba|or|39.720301|8.636125
tramatza|or|40.003748|8.649537
tresnuraghes|or|40.252104|8.51996
ulà tirso|or|40.045951|8.904029
uras|or|39.700643|8.70003
usellus|or|39.809646|8.850732
villanova truschedu|or|39.988649|8.752027
villaurbana|or|39.885647|8.777429
villa verde|or|39.795246|8.821131
zeddiani|or|39.989947|8.595324
zerfaliu|or|39.958844|8.708555
siapiccia|or|39.927948|8.762128
curcuris|or|39.746145|8.830632
soddì|or|40.130453|8.878428
bosa|or|40.297006|8.498612
flussio|or|40.267452|8.540219
laconi|or|39.852649|9.050435
magomadas|or|40.264752|8.522719
modolo|or|40.275047|8.52918
montresta|or|40.373654|8.499617
sagama|or|40.261552|8.57752
suni|or|40.280853|8.549419
tinnura|or|40.269052|8.54822
arbus|su|39.525839|8.597831
armungia|su|39.521745|9.381846
ballao|su|39.549746|9.361545
barrali|su|39.475289|9.101273
barumini|su|39.701546|9.002736
buggerru|su|39.397397|8.402182
burcei|su|39.342642|9.359548
calasetta|su|39.110814|8.368264
carbonia|su|39.165332|8.527834
carloforte|su|39.142698|8.302664
castiadas|su|39.236335|9.501016
collinas|su|39.640631|8.839875
decimoputzu|su|39.335238|8.91524
dolianova|su|39.378241|9.178644
domus de maria|su|38.944031|8.863544
domusnovas|su|39.324736|8.648734
donori|su|39.431797|9.127153
escalaplano|su|39.626647|9.357144
escolca|su|39.698446|9.121539
esterzili|su|39.779149|9.284741
fluminimaggiore|su|39.439237|8.49793
furtei|su|39.562369|8.944739
genoni|su|39.794647|9.008035
genuri|su|39.744246|8.923334
gergei|su|39.699646|9.100438
gesico|su|39.616337|9.106457
gesturi|su|39.739991|9.024004
giba|su|39.071831|8.635638
goni|su|39.578128|9.285879
gonnesa|su|39.266626|8.47174
gonnosfanadiga|su|39.495915|8.662398
guamaggiore|su|39.568644|9.074039
guasila|su|39.561843|9.045439
guspini|su|39.54134|8.633131
iglesias|su|39.31254|8.534111
isili|su|39.740405|9.10859
las plassas|su|39.680145|8.984936
lunamatrona|su|39.650544|8.899335
mandas|su|39.656283|9.129785
masainas|su|39.051624|8.630598
monastir|su|39.38474|9.044841
muravera|su|39.420945|9.573551
musei|su|39.302285|8.667406
narcao|su|39.168165|8.674714
nuragus|su|39.776247|9.038136
nurallao|su|39.790278|9.080737
nuraminis|su|39.442941|9.01474
nurri|su|39.712048|9.229841
nuxis|su|39.152933|8.738139
orroli|su|39.693547|9.250341
ortacesus|su|39.539148|9.083526
pabillonis|su|39.591159|8.720953
pauli arbarei|su|39.660744|8.922035
perdaxius|su|39.161033|8.611436
pimentel|su|39.485542|9.06554
piscinas|su|39.074431|8.665938
portoscuso|su|39.207031|8.380931
sadali|su|39.81325|9.27194
samassi|su|39.481341|8.907637
samatzai|su|39.483242|9.03464
san basilio|su|39.537144|9.198242
san gavino monreale|su|39.550941|8.791934
san giovanni suergiu|su|39.110404|8.521999
san nicolò gerrei|su|39.497444|9.307145
san sperate|su|39.358739|9.006741
san vito|su|39.443245|9.540751
sanluri|su|39.562642|8.898636
santadi|su|39.091832|8.716439
sant'andrea frius|su|39.477858|9.170254
sant'anna arresi|su|39.00183|8.646339
sant'antioco|su|39.064829|8.456296
sardara|su|39.616442|8.820634
segariu|su|39.564443|8.981638
selegas|su|39.567844|9.10414
senorbì|su|39.535044|9.130141
serdiana|su|39.374241|9.158944
serramanna|su|39.42404|8.923138
serrenti|su|39.492771|8.975228
serri|su|39.701747|9.144139
setzu|su|39.722945|8.939235
seui|su|39.840564|9.32123
seulo|su|39.870412|9.235736
siddi|su|39.672198|8.888371
siliqua|su|39.300237|8.808738
silius|su|39.516745|9.293545
siurgus donigala|su|39.600345|9.189041
soleminis|su|39.347963|9.181708
suelli|su|39.562744|9.132141
teulada|su|38.96743|8.772242
tratalias|su|39.10353|8.57896
tuili|su|39.713988|8.960342
turri|su|39.704845|8.916534
ussana|su|39.394641|9.073242
ussaramanna|su|39.692845|8.907634
vallermosa|su|39.364438|8.796337
villacidro|su|39.456992|8.736722
villamar|su|39.618044|8.958837
villamassargia|su|39.275899|8.640475
villanova tulo|su|39.780302|9.213205
villanovaforru|su|39.631843|8.869035
villanovafranca|su|39.645183|9.001398
villaperuccio|su|39.111832|8.670038
villaputzu|su|39.441045|9.573951
villasalto|su|39.492145|9.393047
villasimius|su|39.14314|9.520854
villasor|su|39.380439|8.942839
villaspeciosa|su|39.312238|8.92514
"""


def _carica_comuni_italia():
    diretti = {}
    per_nome = {}
    for riga in _COMUNI_ITALIA_TESTO.strip().split("\n"):
        comune, provincia, lat, lon = riga.split("|")
        punto = (float(lat), float(lon))
        diretti[(comune, provincia)] = punto
        per_nome.setdefault(comune, punto)  # comuni omonimi: tiene il primo
    return diretti, per_nome


_COMUNI_ITALIA, _COMUNI_ITALIA_PER_NOME = _carica_comuni_italia()


def _centroide_comune_italia(comune, provincia):
    """
    Centroide di un comune italiano qualunque (non solo provincia di
    Pistoia): serve per gli interventi di mutuo soccorso fuori provincia che
    arrivano senza coordinate. Cerca prima comune+provincia (da
    SIGLA_PROVINCIA nell'XML), poi il solo nome se la provincia manca o non
    combacia con nessuna voce — più impreciso in caso di comuni omonimi in
    province diverse (sceglie il primo trovato), ma sempre meglio di niente.
    """
    chiave = comune.strip().casefold()
    if provincia:
        punto = _COMUNI_ITALIA.get((chiave, provincia.strip().casefold()))
        if punto is not None:
            return punto
    return _COMUNI_ITALIA_PER_NOME.get(chiave)

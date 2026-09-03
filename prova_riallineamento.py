"""
Prova la logica di riallineamento di sync_interventi.py con un finto ArcGIS
in memoria: nessuna chiamata di rete, nessun token, nessuna credenziale.

Lavora su una copia usa e getta degli XML in esempi/, quindi si puo' lanciare
in qualsiasi momento senza toccare niente:

    python prova_riallineamento.py
"""
import os, sys, shutil, tempfile, types

REPO = os.path.dirname(os.path.abspath(__file__))
ESEMPI = os.path.join(REPO, "esempi")
sys.path.insert(0, REPO)

# --- finto ArcGIS: un dizionario per layer_url, ognuno OBJECTID -> attributi -
# Consapevole del layer_url (a differenza di un singolo store globale) per
# poter testare la scrittura in parallelo su più layer senza che l'uno
# interferisca con l'altro.
LAYERS = {}
_prossimo_oid = {}

def _store(layer_url):
    return LAYERS.setdefault(layer_url, {})

def query_features(layer_url, where, out_fields):
    fase = where.split("'")[1]
    return [dict(a, OBJECTID=oid) for oid, a in _store(layer_url).items() if a["Fase"] == fase]

def apply_edits(layer_url, adds=None, updates=None, deletes=None):
    store = _store(layer_url)
    prossimo = _prossimo_oid.get(layer_url, 1)
    for f in adds or []:
        store[prossimo] = dict(f["attributes"])
        prossimo += 1
    for f in updates or []:
        store[f["attributes"]["OBJECTID"]] = dict(f["attributes"])
    for oid in deletes or []:
        store.pop(oid, None)
    _prossimo_oid[layer_url] = prossimo
    return {"ok": True}

# Il layer usato da tutti i test 1-10 (single-layer, invariati)
LAYER = _store("http://finto")

finto = types.ModuleType("arcgis_client")
finto.query_features = query_features
finto.apply_edits = apply_edits
sys.modules["arcgis_client"] = finto

import sync_interventi as sync

CARTELLA = tempfile.mkdtemp(prefix="xml_prova_")
CHIAMATE = os.path.join(CARTELLA, "chiamate_interventi.XML")
INTERVENTI = os.path.join(CARTELLA, "interventi.XML")
shutil.copy(os.path.join(ESEMPI, "chiamate_interventi.XML"), CHIAMATE)
shutil.copy(os.path.join(ESEMPI, "interventi.XML"), INTERVENTI)

def chiavi():
    return sorted(a["Chiave"] for a in LAYER.values())

# Le chiavi ora includono la data (DATA_CHIAMATA negli esempi è 19/08/2026),
# per non scontrarsi con chiamate/interventi omonimi di un altro giorno.
DATA = "19082026"

def controlla(descrizione, atteso):
    ottenuto = chiavi()
    esito = "OK " if ottenuto == sorted(atteso) else "FALLITO"
    print(f"[{esito}] {descrizione}\n         layer: {ottenuto}")
    if ottenuto != sorted(atteso):
        print(f"         atteso: {sorted(atteso)}")
        globals()["FALLIMENTI"] = globals().get("FALLIMENTI", 0) + 1


def attributi_di(chiave):
    for a in LAYER.values():
        if a["Chiave"] == chiave:
            return a
    return None


def controlla_attributo(descrizione, chiave, campo, atteso):
    attributi = attributi_di(chiave)
    ottenuto = attributi.get(campo) if attributi else None
    esito = "OK " if ottenuto == atteso else "FALLITO"
    print(f"[{esito}] {descrizione}\n         {campo}={ottenuto!r}")
    if ottenuto != atteso:
        print(f"         atteso: {campo}={atteso!r}")
        globals()["FALLIMENTI"] = globals().get("FALLIMENTI", 0) + 1

print("\n--- 1. Primo caricamento ---")
sync.elabora(CARTELLA, "http://finto", forza=True)
controlla("tutto quello che c'è negli XML finisce sul layer",
          [f"C-14-{DATA}", f"I-3109-{DATA}", f"I-3110-{DATA}"])

print("\n--- 2. Un intervento si chiude e sparisce dall'XML ---")
testo = open(INTERVENTI, encoding="iso-8859-1").read()
inizio = testo.index('<intervento num="3110 /1">')
fine = testo.rindex("</intervento>") + len("</intervento>")
open(INTERVENTI, "w", encoding="iso-8859-1").write(testo[:inizio] + testo[fine:])
sync.elabora(CARTELLA, "http://finto")
controlla("l'intervento sparito dall'XML viene rimosso dal layer",
          [f"C-14-{DATA}", f"I-3109-{DATA}"])

print("\n--- 3. Tutte le squadre abbandonano il luogo: l'intervento diventa sospeso, non sparisce ---")
testo = open(INTERVENTI, encoding="iso-8859-1").read()
testo = testo.replace('<ORA_PARTENZA_LUOGO NULL="TRUE"/>', "<ORA_PARTENZA_LUOGO>18:40</ORA_PARTENZA_LUOGO>")
testo = testo.replace("<STATUS>P</STATUS>", "<STATUS>S</STATUS>")
open(INTERVENTI, "w", encoding="iso-8859-1").write(testo)
sync.elabora(CARTELLA, "http://finto")
controlla("nessuna squadra più presente, ma l'intervento resta sul layer",
          [f"C-14-{DATA}", f"I-3109-{DATA}"])
controlla_attributo("Stato_oracle segnala 'S' (sospeso)",
                     f"I-3109-{DATA}", "Stato_oracle", "S")
controlla_attributo("Squadre_mezzi si svuota: nessuno è più presente",
                     f"I-3109-{DATA}", "Squadre_mezzi", None)

print("\n--- 4. Solo quando l'intervento sparisce del tutto dal file, il pin viene rimosso ---")
testo = open(INTERVENTI, encoding="iso-8859-1").read()
inizio = testo.index('<intervento num="3109 /1">')
fine = testo.rindex("</intervento>") + len("</intervento>")
open(INTERVENTI, "w", encoding="iso-8859-1").write(testo[:inizio] + testo[fine:])
sync.elabora(CARTELLA, "http://finto")
# Era l'ultimo intervento rimasto: la stessa guardia anti-svuotamento dei
# test 6-7 (serve una seconda lettura vuota consecutiva) si applica anche
# qui, non è un comportamento nuovo di oggi.
controlla("prima lettura vuota: la guardia chiede conferma", [f"C-14-{DATA}", f"I-3109-{DATA}"])
sync.elabora(CARTELLA, "http://finto", forza=True)
controlla("confermato due volte: l'intervento sospeso sparito dal file viene rimosso", [f"C-14-{DATA}"])

print("\n--- 5. XML troncato mentre Oracle lo riscrive ---")
testo = open(CHIAMATE, encoding="iso-8859-1").read()
open(CHIAMATE, "w", encoding="iso-8859-1").write(testo[: len(testo) // 2])
sync.elabora(CARTELLA, "http://finto")
controlla("un file troncato non tocca il layer", [f"C-14-{DATA}"])

print("\n--- 6. XML valido ma vuoto: prima lettura ---")
open(CHIAMATE, "w", encoding="iso-8859-1").write(
    "<?xml version = '1.0' encoding = 'iso-8859-1'?>\n<ROWSET>\n</ROWSET>")
sync.elabora(CARTELLA, "http://finto", forza=True)
controlla("la guardia impedisce lo svuotamento al primo colpo", [f"C-14-{DATA}"])

print("\n--- 7. XML ancora vuoto: seconda lettura consecutiva ---")
sync.elabora(CARTELLA, "http://finto", forza=True)
controlla("confermato due volte, la pulizia avviene", [])

print("\n--- 8. File sparito dalla cartella ---")
LAYER[99] = {"Chiave": "C-77", "Fase": "chiamata in attesa"}
os.remove(CHIAMATE)
sync.elabora(CARTELLA, "http://finto", forza=True)
controlla("un file assente non cancella nulla", ["C-77"])

print("\n--- 9. Sotto-invii /1 e /2 dello stesso intervento: un solo pin, stato più presente ---")
import parser_xml as px

CARTELLA_9 = tempfile.mkdtemp(prefix="xml_prova9_")
XML_9 = os.path.join(CARTELLA_9, "interventi.XML")
open(XML_9, "w", encoding="iso-8859-1").write("""<?xml version = '1.0' encoding = 'iso-8859-1'?>
<ROWSET>
 <intervento num="9001 /1">
  <INTERVENTO>9001 /1</INTERVENTO>
  <DATA_CHIAMATA>19/08/2026</DATA_CHIAMATA>
  <ORA_CHIAMATA>10:00</ORA_CHIAMATA>
  <ORA_USCITA>10:05</ORA_USCITA>
  <ORA_ARRIVO>10:20</ORA_ARRIVO>
  <ORA_PARTENZA_LUOGO NULL="TRUE"/>
  <ORA_RIENTRO NULL="TRUE"/>
  <COORD_X>10,9</COORD_X>
  <COORD_Y>43,9</COORD_Y>
  <TIPOLOGIA>Prova</TIPOLOGIA>
  <COMUNE>Pistoia</COMUNE>
  <SIGLA_PROVINCIA>PT</SIGLA_PROVINCIA>
  <STATUS>P</STATUS>
  <ENTI_INTERVENUTI>N</ENTI_INTERVENUTI>
  <INDIRIZZO>Via Prova</INDIRIZZO>
  <CIV_KM NULL="TRUE"/>
  <RICHIEDENTE>X</RICHIEDENTE>
  <COGNOME_NOME>X - X</COGNOME_NOME>
  <DATA>19/08/2026 21:20:28</DATA>
  <NOME_HOST>PISTOIA</NOME_HOST>
  <SQUADRA_MEZZO>MEZZO UNO</SQUADRA_MEZZO>
 </intervento>
 <intervento num="9001 /2">
  <INTERVENTO>9001 /2</INTERVENTO>
  <DATA_CHIAMATA>19/08/2026</DATA_CHIAMATA>
  <ORA_CHIAMATA>10:00</ORA_CHIAMATA>
  <ORA_USCITA>10:05</ORA_USCITA>
  <ORA_ARRIVO>10:20</ORA_ARRIVO>
  <ORA_PARTENZA_LUOGO>11:00</ORA_PARTENZA_LUOGO>
  <ORA_RIENTRO>11:20</ORA_RIENTRO>
  <COORD_X NULL="TRUE"/>
  <COORD_Y NULL="TRUE"/>
  <TIPOLOGIA>Prova</TIPOLOGIA>
  <COMUNE>Pistoia</COMUNE>
  <SIGLA_PROVINCIA>PT</SIGLA_PROVINCIA>
  <STATUS>A</STATUS>
  <ENTI_INTERVENUTI>N</ENTI_INTERVENUTI>
  <INDIRIZZO>Via Prova</INDIRIZZO>
  <CIV_KM NULL="TRUE"/>
  <RICHIEDENTE>X</RICHIEDENTE>
  <COGNOME_NOME>X - X</COGNOME_NOME>
  <DATA>19/08/2026 21:20:28</DATA>
  <NOME_HOST>PISTOIA</NOME_HOST>
  <SQUADRA_MEZZO>MEZZO DUE</SQUADRA_MEZZO>
 </intervento>
</ROWSET>""")

risultato = px.leggi_interventi(XML_9)
if len(risultato) != 1:
    print(f"[FALLITO] attesa 1 sola feature per l'intervento 9001, ottenute {len(risultato)}")
    FALLIMENTI = globals().get("FALLIMENTI", 0) + 1
else:
    a = risultato[0]["attributes"]
    problemi = []
    if a["Chiave"] != "I-9001-19082026":
        problemi.append(f"Chiave errata: {a['Chiave']}")
    if a["Stato_oracle"] != "P":
        problemi.append(f"Stato_oracle errato: {a['Stato_oracle']!r} (atteso 'P', dalla riga /1 ancora presente)")
    if a["Squadre_mezzi"] != "MEZZO UNO":
        problemi.append(f"Squadre_mezzi dovrebbe contenere solo chi è ancora presente: {a['Squadre_mezzi']}")
    if a["Posizione_stimata"] is not None:
        problemi.append("Posizione_stimata valorizzata ma /1 aveva coordinate esatte")
    if problemi:
        print("[FALLITO] " + "; ".join(problemi))
        globals()["FALLIMENTI"] = globals().get("FALLIMENTI", 0) + 1
    else:
        print("[OK ] /1 ancora sul posto e /2 ha già abbandonato: un solo pin, stato 'sul posto', solo MEZZO UNO nel roster")

shutil.rmtree(CARTELLA_9, ignore_errors=True)

print("\n--- 10. La nota di una chiamata si ritrova sull'intervento corrispondente ---")
open(CHIAMATE, "w", encoding="iso-8859-1").write("""<?xml version = '1.0' encoding = 'iso-8859-1'?>
<ROWSET>
 <chiamata num="500">
  <CHIAMATA>500</CHIAMATA>
  <LOC_IND>Via dei Test</LOC_IND>
  <CIV_KM NULL="TRUE"/>
  <COMUNE>Prova</COMUNE>
  <COORD_X>10,9</COORD_X>
  <COORD_Y>43,9</COORD_Y>
  <SIGLA_PROVINCIA>PT</SIGLA_PROVINCIA>
  <COD_TIPOLOGIA>1</COD_TIPOLOGIA>
  <DETTAGLIO_TIPOLOGIA NULL="TRUE"/>
  <NOTE_INTERVENTO>tetto pericolante, serve #autoscala</NOTE_INTERVENTO>
  <DATA_CHIAMATA>19/08/2026</DATA_CHIAMATA>
  <ORA_CHIAMATA>12:00</ORA_CHIAMATA>
  <RICHIEDENTE>x</RICHIEDENTE>
  <TELE_NUMERO NULL="TRUE"/>
  <NOME>X</NOME>
  <COGNOME>Y</COGNOME>
  <PRIORITA>1</PRIORITA>
 </chiamata>
</ROWSET>""")
sync.elabora(CARTELLA, "http://finto", forza=True)
controlla_attributo("la nota compare sulla chiamata stessa",
                     f"C-500-{DATA}", "Note", "tetto pericolante, serve #autoscala")

# La stessa chiamata diventa un intervento (stesso DATA_CHIAMATA/ORA_CHIAMATA/
# INDIRIZZO): interventi.XML non ha un campo nota, ma il sincronizzatore se la
# ricorda da quando era ancora una chiamata (vedi aggiorna_note_cache).
open(INTERVENTI, "w", encoding="iso-8859-1").write("""<?xml version = '1.0' encoding = 'iso-8859-1'?>
<ROWSET>
 <intervento num="9500 /1">
  <INTERVENTO>9500 /1</INTERVENTO>
  <DATA_CHIAMATA>19/08/2026</DATA_CHIAMATA>
  <ORA_CHIAMATA>12:00</ORA_CHIAMATA>
  <ORA_USCITA>12:05</ORA_USCITA>
  <ORA_ARRIVO NULL="TRUE"/>
  <ORA_PARTENZA_LUOGO NULL="TRUE"/>
  <ORA_RIENTRO NULL="TRUE"/>
  <COORD_X>10,9</COORD_X>
  <COORD_Y>43,9</COORD_Y>
  <TIPOLOGIA>Tetto pericolante</TIPOLOGIA>
  <COMUNE>Prova</COMUNE>
  <SIGLA_PROVINCIA>PT</SIGLA_PROVINCIA>
  <STATUS>A</STATUS>
  <ENTI_INTERVENUTI>N</ENTI_INTERVENUTI>
  <INDIRIZZO>Via dei Test</INDIRIZZO>
  <CIV_KM NULL="TRUE"/>
  <RICHIEDENTE>x</RICHIEDENTE>
  <COGNOME_NOME>Y - X</COGNOME_NOME>
  <DATA>19/08/2026 21:20:28</DATA>
  <NOME_HOST>PISTOIA</NOME_HOST>
  <SQUADRA_MEZZO>MIKE 3+APS</SQUADRA_MEZZO>
 </intervento>
</ROWSET>""")
sync.elabora(CARTELLA, "http://finto", forza=True)
controlla_attributo("la nota viene ereditata dall'intervento",
                     f"I-9500-{DATA}", "Note", "tetto pericolante, serve #autoscala")

shutil.rmtree(CARTELLA, ignore_errors=True)

print("\n--- 11. Zone di competenza: punto dentro, punto fuori, nessun poligono caricato ---")
# Un quadrato finto attorno a Pistoia, così non dipende dal servizio ArcGIS
# vero: dentro dovrebbe cadere il centro di Pistoia, fuori un punto a Firenze.
quadrato = [{"nome": "ZonaFinta", "anelli": [[
    (10.5, 43.7), (11.3, 43.7), (11.3, 44.1), (10.5, 44.1), (10.5, 43.7),
]]}]

problemi_zona = []
dentro = px.zona_competenza(43.9333, 10.9167, quadrato)  # Pistoia centro
if dentro != "ZonaFinta":
    problemi_zona.append(f"punto dentro il quadrato dato '{dentro}', atteso 'ZonaFinta'")

fuori = px.zona_competenza(41.9028, 12.4964, quadrato)  # Roma, ben fuori dal quadrato
if fuori != "Fuori zona":
    problemi_zona.append(f"punto fuori dal quadrato dato '{fuori}', atteso 'Fuori zona'")

senza_poligoni = px.zona_competenza(43.9333, 10.9167, None)
if senza_poligoni is not None:
    problemi_zona.append(f"senza poligoni caricati atteso None, dato '{senza_poligoni}'")

# La sigla radio: due zone diverse sulla stessa sigla, "Fuori zona" e nomi non
# ancora mappati sulla stessa "FUORI ZONA", None resta None.
if px.area_emergenza("Centrale") != "ALFA":
    problemi_zona.append(f"Centrale dovrebbe tradursi in ALFA, dato '{px.area_emergenza('Centrale')}'")
if px.area_emergenza("Montemurlo") != "ALFA":
    problemi_zona.append(f"Montemurlo dovrebbe tradursi in ALFA, dato '{px.area_emergenza('Montemurlo')}'")
if px.area_emergenza("Fuori zona") != "FUORI ZONA":
    problemi_zona.append(f"'Fuori zona' dovrebbe tradursi in 'FUORI ZONA', dato '{px.area_emergenza('Fuori zona')}'")
if px.area_emergenza("ZonaFinta") != "FUORI ZONA":
    problemi_zona.append(f"una zona non mappata dovrebbe tradursi in 'FUORI ZONA', dato '{px.area_emergenza('ZonaFinta')}'")
if px.area_emergenza(None) is not None:
    problemi_zona.append(f"area_emergenza(None) dovrebbe restare None, dato '{px.area_emergenza(None)}'")

if problemi_zona:
    print("[FALLITO] " + "; ".join(problemi_zona))
    globals()["FALLIMENTI"] = globals().get("FALLIMENTI", 0) + 1
else:
    print("[OK ] punto in poligono e sigle radio (incluse due zone sulla stessa sigla) corrette")

print("\n--- 12. Stesse feature scritte su due layer in parallelo, senza interferenze ---")
CARTELLA_12 = tempfile.mkdtemp(prefix="xml_prova12_")
CHIAMATE_12 = os.path.join(CARTELLA_12, "chiamate_interventi.XML")
INTERVENTI_12 = os.path.join(CARTELLA_12, "interventi.XML")
open(CHIAMATE_12, "w", encoding="iso-8859-1").write("""<?xml version = '1.0' encoding = 'iso-8859-1'?>
<ROWSET>
 <chiamata num="700">
  <CHIAMATA>700</CHIAMATA>
  <LOC_IND>Via Doppia</LOC_IND>
  <CIV_KM NULL="TRUE"/>
  <COMUNE>Pistoia</COMUNE>
  <COORD_X>10,9</COORD_X>
  <COORD_Y>43,9</COORD_Y>
  <SIGLA_PROVINCIA>PT</SIGLA_PROVINCIA>
  <COD_TIPOLOGIA>1</COD_TIPOLOGIA>
  <DETTAGLIO_TIPOLOGIA NULL="TRUE"/>
  <NOTE_INTERVENTO NULL="TRUE"/>
  <DATA_CHIAMATA>19/08/2026</DATA_CHIAMATA>
  <ORA_CHIAMATA>15:00</ORA_CHIAMATA>
  <RICHIEDENTE>x</RICHIEDENTE>
  <TELE_NUMERO NULL="TRUE"/>
  <NOME>X</NOME>
  <COGNOME>Y</COGNOME>
  <PRIORITA>0</PRIORITA>
 </chiamata>
</ROWSET>""")
open(INTERVENTI_12, "w", encoding="iso-8859-1").write(
    "<?xml version = '1.0' encoding = 'iso-8859-1'?>\n<ROWSET>\n</ROWSET>")

sync.elabora(CARTELLA_12, ["http://layerA", "http://layerB"], forza=True)

problemi_multi = []
attesa = ["C-700-19082026"]
chiavi_a = sorted(a["Chiave"] for a in LAYERS["http://layerA"].values())
chiavi_b = sorted(a["Chiave"] for a in LAYERS["http://layerB"].values())
if chiavi_a != attesa:
    problemi_multi.append(f"layer A ha {chiavi_a}, atteso {attesa}")
if chiavi_b != attesa:
    problemi_multi.append(f"layer B ha {chiavi_b}, atteso {attesa}")

# Un secondo giro sullo stesso file (forzato) non deve duplicare la feature
# né far leakare l'OBJECTID di un layer nell'altro (il bug che si sarebbe
# preso se riallinea() avesse modificato in-place gli oggetti condivisi).
sync.elabora(CARTELLA_12, ["http://layerA", "http://layerB"], forza=True)
if len(LAYERS["http://layerA"]) != 1:
    problemi_multi.append(f"layer A ha {len(LAYERS['http://layerA'])} feature dopo un secondo giro, atteso 1")
if len(LAYERS["http://layerB"]) != 1:
    problemi_multi.append(f"layer B ha {len(LAYERS['http://layerB'])} feature dopo un secondo giro, atteso 1")

if problemi_multi:
    print("[FALLITO] " + "; ".join(problemi_multi))
    globals()["FALLIMENTI"] = globals().get("FALLIMENTI", 0) + 1
else:
    print("[OK ] entrambi i layer ricevono le stesse feature, nessuna interferenza fra gli OBJECTID")

shutil.rmtree(CARTELLA_12, ignore_errors=True)

print("\n--- 13. File rimasto stabilmente vuoto (senza --forza): la guardia deve comunque pulire al secondo giro ---")
# Riproduce esattamente il ciclo continuo (main() non passa mai forza=True):
# un file che resta byte-per-byte identico fra un ciclo e l'altro non deve
# impedire alla guardia anti-svuotamento di fare la sua seconda lettura.
CARTELLA_13 = tempfile.mkdtemp(prefix="xml_prova13_")
CHIAMATE_13 = os.path.join(CARTELLA_13, "chiamate_interventi.XML")
INTERVENTI_13 = os.path.join(CARTELLA_13, "interventi.XML")
shutil.copy(os.path.join(ESEMPI, "chiamate_interventi.XML"), CHIAMATE_13)
open(INTERVENTI_13, "w", encoding="iso-8859-1").write(
    "<?xml version = '1.0' encoding = 'iso-8859-1'?>\n<ROWSET>\n</ROWSET>")

sync.elabora(CARTELLA_13, "http://layerC")  # primo giro: popola (mai forzato, file mai visto prima)
prima = len(LAYERS["http://layerC"])

open(CHIAMATE_13, "w", encoding="iso-8859-1").write(
    "<?xml version = '1.0' encoding = 'iso-8859-1'?>\n<ROWSET>\n</ROWSET>")
sync.elabora(CARTELLA_13, "http://layerC")  # prima lettura vuota: non deve pulire ancora
dopo_prima_vuota = len(LAYERS["http://layerC"])

# Secondo giro SENZA toccare il file: stessi byte di prima. È esattamente lo
# scenario del ciclo continuo in produzione (main() non passa mai forza).
sync.elabora(CARTELLA_13, "http://layerC")
dopo_seconda_vuota = len(LAYERS["http://layerC"])

problemi_13 = []
if prima == 0:
    problemi_13.append("il primo giro non ha popolato nulla, test non significativo")
if dopo_prima_vuota != prima:
    problemi_13.append(f"la prima lettura vuota ha già modificato il layer ({prima} -> {dopo_prima_vuota}), doveva aspettare")
if dopo_seconda_vuota != 0:
    problemi_13.append(f"dopo la seconda lettura vuota (file invariato, nessun --forza) il layer ha ancora {dopo_seconda_vuota} feature, atteso 0")

if problemi_13:
    print("[FALLITO] " + "; ".join(problemi_13))
    globals()["FALLIMENTI"] = globals().get("FALLIMENTI", 0) + 1
else:
    print("[OK ] pulito al secondo giro anche senza --forza e senza che il file cambiasse")

shutil.rmtree(CARTELLA_13, ignore_errors=True)

falliti = globals().get("FALLIMENTI", 0)
print(f"\n=== {'TUTTO OK' if not falliti else str(falliti) + ' PROVE FALLITE'} ===")
sys.exit(1 if falliti else 0)

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

# --- finto ArcGIS: un dizionario OBJECTID -> attributi -----------------------
LAYER = {}
_prossimo_oid = [1]

def query_features(layer_url, where, out_fields):
    fase = where.split("'")[1]
    return [dict(a, OBJECTID=oid) for oid, a in LAYER.items() if a["Fase"] == fase]

def apply_edits(layer_url, adds=None, updates=None, deletes=None):
    for f in adds or []:
        LAYER[_prossimo_oid[0]] = dict(f["attributes"])
        _prossimo_oid[0] += 1
    for f in updates or []:
        LAYER[f["attributes"]["OBJECTID"]] = dict(f["attributes"])
    for oid in deletes or []:
        LAYER.pop(oid, None)
    return {"ok": True}

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

def controlla(descrizione, atteso):
    ottenuto = chiavi()
    esito = "OK " if ottenuto == sorted(atteso) else "FALLITO"
    print(f"[{esito}] {descrizione}\n         layer: {ottenuto}")
    if ottenuto != sorted(atteso):
        print(f"         atteso: {sorted(atteso)}")
        globals()["FALLIMENTI"] = globals().get("FALLIMENTI", 0) + 1

print("\n--- 1. Primo caricamento ---")
sync.elabora(CARTELLA, "http://finto", forza=True)
controlla("tutto quello che c'è negli XML finisce sul layer",
          ["C-14", "I-3109 /1", "I-3109 /2"])

print("\n--- 2. Un intervento si chiude e sparisce dall'XML ---")
testo = open(INTERVENTI, encoding="iso-8859-1").read()
inizio = testo.index('<intervento num="3109 /2">')
fine = testo.rindex("</intervento>") + len("</intervento>")
open(INTERVENTI, "w", encoding="iso-8859-1").write(testo[:inizio] + testo[fine:])
sync.elabora(CARTELLA, "http://finto")
controlla("l'intervento sparito dall'XML viene rimosso dal layer",
          ["C-14", "I-3109 /1"])

print("\n--- 3. Intervento chiuso con ORA_RIENTRO valorizzata ---")
testo = open(INTERVENTI, encoding="iso-8859-1").read()
testo = testo.replace('<ORA_RIENTRO NULL="TRUE"/>', "<ORA_RIENTRO>19:40</ORA_RIENTRO>")
open(INTERVENTI, "w", encoding="iso-8859-1").write(testo)
sync.elabora(CARTELLA, "http://finto")
controlla("era l'ultimo aperto: la guardia chiede conferma prima di svuotare",
          ["C-14", "I-3109 /1"])
sync.elabora(CARTELLA, "http://finto", forza=True)
controlla("al ciclo successivo l'intervento rientrato sparisce", ["C-14"])

print("\n--- 4. XML troncato mentre Oracle lo riscrive ---")
testo = open(CHIAMATE, encoding="iso-8859-1").read()
open(CHIAMATE, "w", encoding="iso-8859-1").write(testo[: len(testo) // 2])
sync.elabora(CARTELLA, "http://finto")
controlla("un file troncato non tocca il layer", ["C-14"])

print("\n--- 5. XML valido ma vuoto: prima lettura ---")
open(CHIAMATE, "w", encoding="iso-8859-1").write(
    "<?xml version = '1.0' encoding = 'iso-8859-1'?>\n<ROWSET>\n</ROWSET>")
sync.elabora(CARTELLA, "http://finto", forza=True)
controlla("la guardia impedisce lo svuotamento al primo colpo", ["C-14"])

print("\n--- 6. XML ancora vuoto: seconda lettura consecutiva ---")
sync.elabora(CARTELLA, "http://finto", forza=True)
controlla("confermato due volte, la pulizia avviene", [])

print("\n--- 7. File sparito dalla cartella ---")
LAYER[99] = {"Chiave": "C-77", "Fase": "chiamata in attesa"}
os.remove(CHIAMATE)
sync.elabora(CARTELLA, "http://finto", forza=True)
controlla("un file assente non cancella nulla", ["C-77"])

shutil.rmtree(CARTELLA, ignore_errors=True)
falliti = globals().get("FALLIMENTI", 0)
print(f"\n=== {'TUTTO OK' if not falliti else str(falliti) + ' PROVE FALLITE'} ===")
sys.exit(1 if falliti else 0)

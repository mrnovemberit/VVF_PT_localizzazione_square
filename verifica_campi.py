"""
Controlla che il Feature Layer sia stato creato come si deve, prima di
lanciare il sincronizzatore per davvero.

Verifica tre cose che, se sbagliate, non danno errore ma fanno perdere ore:

1. che tutti i campi esistano con il nome esatto — ArcGIS scarta in silenzio
   gli attributi che non trovano un campo corrispondente, quindi un
   "Stato_Operativo" con la O maiuscola non protesta, resta solo sempre vuoto;
2. che i tipi siano quelli giusti e le stringhe abbastanza lunghe;
3. che la modifica sia abilitata, altrimenti applyEdits verrà respinto.

Uso:
    python verifica_campi.py
"""

import os
import sys

from arcgis_client import descrivi_layer
from sync_interventi import carica_env_locale

# Nome del campo -> (tipi ArcGIS ammessi, lunghezza minima per le stringhe)
CAMPI_ATTESI = {
    "Chiave": (("esriFieldTypeString",), 50),
    "Fase": (("esriFieldTypeString",), 30),
    "Numero": (("esriFieldTypeString",), 30),
    "Tipologia": (("esriFieldTypeString",), 120),
    "Dettaglio_tipologia": (("esriFieldTypeString",), 255),
    "Indirizzo": (("esriFieldTypeString",), 255),
    "Civico_km": (("esriFieldTypeString",), 30),
    "Comune": (("esriFieldTypeString",), 80),
    "Provincia": (("esriFieldTypeString",), 5),
    "Data_chiamata": (("esriFieldTypeDate",), None),
    "Ora_uscita": (("esriFieldTypeDate",), None),
    "Ora_arrivo": (("esriFieldTypeDate",), None),
    "Ora_partenza_luogo": (("esriFieldTypeDate",), None),
    "Ora_rientro": (("esriFieldTypeDate",), None),
    "Stato_oracle": (("esriFieldTypeString",), 10),
    "Stato_operativo": (("esriFieldTypeString",), 30),
    "Squadre_mezzi": (("esriFieldTypeString",), 500),
    "Num_mezzi": (("esriFieldTypeInteger", "esriFieldTypeSmallInteger"), None),
    "Enti_intervenuti": (("esriFieldTypeString",), 10),
    "Priorita": (("esriFieldTypeInteger", "esriFieldTypeSmallInteger"), None),
    "Note": (("esriFieldTypeString",), 1000),
    "Minuti_apertura": (("esriFieldTypeInteger", "esriFieldTypeSmallInteger"), None),
    "Ultimo_agg": (("esriFieldTypeDate",), None),
    "Latitudine": (("esriFieldTypeDouble",), None),
    "Longitudine": (("esriFieldTypeDouble",), None),
}


def campi_prodotti_dal_codice():
    """
    I campi che il parser scrive davvero, letti dagli XML di esempio.
    Serve a non far divergere questo elenco dal codice: se un domani si
    aggiunge un campo al parser e non qui, il controllo lo segnala.
    """
    import parser_xml as px

    cartella = os.path.join(os.path.dirname(os.path.abspath(__file__)), "esempi")
    campi = {campo for _, campo in px.SEQUENZA_ORARI}
    for feature in (
        px.leggi_chiamate(os.path.join(cartella, "chiamate_interventi.XML"))
        + px.leggi_interventi(os.path.join(cartella, "interventi.XML"))
    ):
        campi |= set(feature["attributes"])
    return campi


def main():
    carica_env_locale()

    layer_url = os.environ.get("ARCGIS_INTERVENTI_LAYER_URL", "").rstrip("/")
    if not layer_url:
        print("Manca ARCGIS_INTERVENTI_LAYER_URL (nel .env o nell'ambiente)")
        return 1

    definizione = descrivi_layer(layer_url)
    sul_layer = {c["name"]: c for c in definizione.get("fields", [])}
    problemi = []

    print("Layer: {}".format(definizione.get("name", "?")))
    print("Geometria: {}".format(definizione.get("geometryType", "?")))
    print("Campi trovati: {}\n".format(len(sul_layer)))

    if definizione.get("geometryType") != "esriGeometryPoint":
        problemi.append("Il layer non è di tipo Punto")

    capacita = definizione.get("capabilities", "")
    for necessaria in ("Create", "Update", "Delete"):
        if necessaria not in capacita:
            problemi.append(
                "Modifica non abilitata: manca '{}' fra le capacità ({}). "
                "Vai in Impostazioni dell'elemento e abilita la modifica.".format(
                    necessaria, capacita or "nessuna"
                )
            )

    for nome, (tipi_ammessi, lunghezza_minima) in CAMPI_ATTESI.items():
        campo = sul_layer.get(nome)
        if campo is None:
            # Nome sbagliato o solo maiuscole diverse? Meglio dirlo subito.
            simile = [n for n in sul_layer if n.lower() == nome.lower()]
            if simile:
                problemi.append(
                    "Campo '{}' scritto come '{}': i nomi fanno differenza fra "
                    "maiuscole e minuscole, va rinominato".format(nome, simile[0])
                )
            else:
                problemi.append("Campo '{}' mancante".format(nome))
            continue

        if campo["type"] not in tipi_ammessi:
            problemi.append(
                "Campo '{}' di tipo {}, atteso {}".format(
                    nome, campo["type"], " o ".join(tipi_ammessi)
                )
            )
        elif lunghezza_minima and (campo.get("length") or 0) < lunghezza_minima:
            problemi.append(
                "Campo '{}' lungo {}, servono almeno {} caratteri".format(
                    nome, campo.get("length"), lunghezza_minima
                )
            )

    try:
        mancanti_nel_controllo = campi_prodotti_dal_codice() - set(CAMPI_ATTESI)
    except FileNotFoundError:
        # La cartella esempi/ è comoda per collaudare in locale ma non
        # indispensabile: sul PC del comando può mancare, senza che questo
        # impedisca il controllo dei campi (che è la parte che conta qui).
        print("(cartella esempi/ non trovata, salto il confronto con il parser)\n")
        mancanti_nel_controllo = set()
    if mancanti_nel_controllo:
        problemi.append(
            "Il parser produce campi non previsti da questo controllo: {}. "
            "Vanno aggiunti sia al layer sia a CAMPI_ATTESI.".format(
                ", ".join(sorted(mancanti_nel_controllo))
            )
        )

    if problemi:
        print("Da sistemare:\n")
        for problema in problemi:
            print("  - {}".format(problema))
        return 1

    print("Tutto a posto: i {} campi ci sono con nome e tipo giusti, "
          "e la modifica è abilitata.".format(len(CAMPI_ATTESI)))
    print("Puoi lanciare: python sync_interventi.py --once")
    return 0


if __name__ == "__main__":
    sys.exit(main())

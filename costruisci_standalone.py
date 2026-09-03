"""
Genera i file da copiare sul PC di sala operativa, unendo arcgis_client.py +
parser_xml.py + sync_interventi.py in un solo sync_interventi.py, e adattando
verifica_campi.py di conseguenza (importa da sync_interventi invece che da
arcgis_client, che a quel punto non esiste più come file a sé).

Il progetto resta diviso in tre moduli per motivi di collaudo (parser_xml.py
si testa a secco senza rete, arcgis_client.py si sostituisce con un finto
modulo in prova_riallineamento.py): questo script produce, a partire da
quegli stessi file, le versioni equivalenti da installare dove serve un solo
file invece di tre. Non toccare mai a mano i file generati: si perdono al
prossimo lancio di questo script.

Le uniche differenze rispetto alla semplice concatenazione sono la rimozione
degli import fra i tre moduli (non servono più, le funzioni sono nello stesso
file) — gli import duplicati di libreria standard (os, logging, ecc.) restano
e sono innocui.

Scrive sempre in una cartella DIVERSA da quella dei sorgenti (di default
dist/, accanto a questo script): sync_interventi.py e verifica_campi.py sono
anche i nomi dei file sorgente modulari, sovrascriverli per errore
romperebbe il repository di sviluppo.

Uso:
    python costruisci_standalone.py                 (scrive in dist/, accanto a questo script)
    python costruisci_standalone.py C:\\percorso    (scrive nella cartella indicata, es. sulla chiavetta)
"""
import os
import sys

REPO = os.path.dirname(os.path.abspath(__file__))


def leggi(nome):
    with open(os.path.join(REPO, nome), encoding="utf-8") as f:
        return f.read()


def rimuovi(testo, frammento, obbligatorio=True):
    if frammento not in testo:
        if obbligatorio:
            sys.exit(f"Atteso ma non trovato in sync_interventi.py:\n{frammento}")
        return testo
    return testo.replace(frammento, "")


def costruisci_sync():
    arcgis = leggi("arcgis_client.py")
    parser = leggi("parser_xml.py")
    sync = leggi("sync_interventi.py")

    sync = rimuovi(sync, """from parser_xml import (
    leggi_chiamate,
    leggi_interventi,
    note_da_chiamate,
    poligoni_da_query,
    FASE_CHIAMATA,
    FASE_INTERVENTO,
)

""")
    sync = rimuovi(sync, "        from arcgis_client import query_poligoni\n")
    sync = rimuovi(sync, "    from arcgis_client import query_features, apply_edits\n")

    intestazione = '''"""
File generato da costruisci_standalone.py: unisce arcgis_client.py,
parser_xml.py e sync_interventi.py in un unico script, per installare sul PC
di sala operativa un solo file invece di tre.

NON MODIFICARE A MANO — le modifiche vanno fatte nei tre file sorgente nel
repository di sviluppo, poi si rilancia costruisci_standalone.py.
"""

'''

    divisore = "\n\n# " + "=" * 76 + "\n# {}\n# " + "=" * 76 + "\n\n"

    return (
        intestazione
        + divisore.format("Da arcgis_client.py")
        + arcgis
        + divisore.format("Da parser_xml.py")
        + parser
        + divisore.format("Da sync_interventi.py")
        + sync
    )


def costruisci_verifica_campi():
    """
    verifica_campi.py normalmente importa descrivi_layer da arcgis_client e
    carica_env_locale da sync_interventi: qui arcgis_client non esiste più
    come file a sé, ma entrambe le funzioni sono nell'unico sync_interventi.py
    generato sopra.
    """
    testo = leggi("verifica_campi.py")
    testo = rimuovi(testo, "from arcgis_client import descrivi_layer\n")
    testo = testo.replace(
        "from sync_interventi import carica_env_locale",
        "from sync_interventi import carica_env_locale, descrivi_layer",
    )
    if "from sync_interventi import carica_env_locale, descrivi_layer" not in testo:
        sys.exit("Atteso ma non trovato in verifica_campi.py: import di carica_env_locale")
    intestazione = (
        '"""\n'
        "File generato da costruisci_standalone.py a partire da verifica_campi.py:\n"
        "importa da sync_interventi (unico file) invece che da arcgis_client.\n\n"
        "NON MODIFICARE A MANO — le modifiche vanno fatte in verifica_campi.py nel\n"
        "repository di sviluppo, poi si rilancia costruisci_standalone.py.\n"
        '"""\n\n'
    )
    return intestazione + testo


if __name__ == "__main__":
    cartella = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "dist")
    if os.path.abspath(cartella) == REPO:
        sys.exit(
            "Rifiuto di scrivere direttamente nella cartella del repository: "
            "sovrascriverebbe i sorgenti modulari (sync_interventi.py, "
            "verifica_campi.py). Indica un'altra cartella, es. la chiavetta."
        )
    os.makedirs(cartella, exist_ok=True)
    for nome, costruisci in (
        ("sync_interventi.py", costruisci_sync),
        ("verifica_campi.py", costruisci_verifica_campi),
    ):
        contenuto = costruisci()
        destinazione = os.path.join(cartella, nome)
        with open(destinazione, "w", encoding="utf-8") as f:
            f.write(contenuto)
        print(f"Scritto {destinazione} ({len(contenuto.splitlines())} righe)")

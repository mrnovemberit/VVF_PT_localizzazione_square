"""
Genera l'eseguibile .exe da installare direttamente nella cartella dove il
software Oracle scrive i due XML, sul PC di sala operativa: un solo file,
senza `.env` separato, senza `pip install` sulla macchina di destinazione.

Le credenziali e l'URL del layer vengono letti da `.env.charlie` (mai
committato) e incorporati nello script prima di compilarlo con PyInstaller.
Per questo:
  - va rilanciato ogni volta che le credenziali o il layer di destinazione
    cambiano;
  - il file generato in `dist/` (sorgente **e** .exe) non va MAI committato
    né condiviso al di fuori del PC di destinazione: chiunque lo apra con un
    editor esadecimale può estrarne client id/secret. `dist/` è già in
    `.gitignore`.

L'eseguibile usa come cartella degli XML quella in cui si trova lui stesso:
va installato lì, non altrove — non c'è modo di indicargliene un'altra senza
ricompilare.

Uso:
    python costruisci_exe.py
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from costruisci_standalone import costruisci_sync  # noqa: E402


def leggi_env_charlie():
    valori = {}
    percorso = os.path.join(REPO, ".env.charlie")
    if not os.path.exists(percorso):
        sys.exit(f"Non trovato: {percorso}")
    with open(percorso, encoding="utf-8") as f:
        for riga in f:
            riga = riga.strip()
            if not riga or riga.startswith("#") or "=" not in riga:
                continue
            chiave, valore = riga.split("=", 1)
            valori[chiave.strip()] = valore.strip()
    return valori


def costruisci_sorgente_embedded():
    env = leggi_env_charlie()
    richieste = ["ARCGIS_CLIENT_ID", "ARCGIS_CLIENT_SECRET", "ARCGIS_INTERVENTI_LAYER_URL"]
    mancanti = [k for k in richieste if k not in env]
    if mancanti:
        sys.exit(f".env.charlie manca di: {', '.join(mancanti)}")

    sorgente = costruisci_sync()

    # In un .exe --onefile, __file__ punta alla cartella temporanea di
    # estrazione (sparisce alla chiusura): sys.executable è l'unico modo
    # affidabile di sapere dove sta davvero l'eseguibile. Da qui dipendono
    # sia il file di log sia (sotto) la cartella XML di default.
    vecchia_riga = "CARTELLA_SCRIPT = os.path.dirname(os.path.abspath(__file__))"
    nuova_riga = (
        'CARTELLA_SCRIPT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)\n'
        "                    else os.path.dirname(os.path.abspath(__file__)))"
    )
    if vecchia_riga not in sorgente:
        sys.exit("Atteso ma non trovato: la riga CARTELLA_SCRIPT è cambiata, aggiorna costruisci_exe.py")
    sorgente = sorgente.replace(vecchia_riga, nuova_riga, 1)

    blocco_default = """
# Credenziali e destinazione incorporate al momento della build (lette da
# .env.charlie, mai committate): questo file gira da solo, senza .env
# separato. La cartella degli XML è quella dove sta l'eseguibile stesso.
os.environ.setdefault("ARCGIS_CLIENT_ID", {client_id!r})
os.environ.setdefault("ARCGIS_CLIENT_SECRET", {client_secret!r})
os.environ.setdefault("ARCGIS_INTERVENTI_LAYER_URL", {layer_url!r})
os.environ.setdefault("ARCGIS_ZONA_LAYER_URL", {zona_url!r})
os.environ.setdefault("XML_CARTELLA", CARTELLA_SCRIPT)
""".format(
        client_id=env["ARCGIS_CLIENT_ID"],
        client_secret=env["ARCGIS_CLIENT_SECRET"],
        layer_url=env["ARCGIS_INTERVENTI_LAYER_URL"],
        zona_url=env.get("ARCGIS_ZONA_LAYER_URL", ""),
    )
    sorgente = sorgente.replace(nuova_riga, nuova_riga + "\n" + blocco_default, 1)
    return sorgente


if __name__ == "__main__":
    sorgente = costruisci_sorgente_embedded()

    dist = os.path.join(REPO, "dist")
    os.makedirs(dist, exist_ok=True)
    percorso_sorgente = os.path.join(dist, "sync_interventi_embedded.py")
    with open(percorso_sorgente, "w", encoding="utf-8") as f:
        f.write(sorgente)
    print(f"Scritto {percorso_sorgente}")

    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            "--onefile", "--noconsole", "--clean",
            "--name", "sync_interventi",
            "--collect-all", "tzdata",
            "--distpath", dist,
            "--workpath", os.path.join(dist, "build"),
            "--specpath", dist,
            percorso_sorgente,
        ],
        check=True,
        cwd=REPO,
    )
    print(f"Eseguibile pronto in {os.path.join(dist, 'sync_interventi.exe')}")

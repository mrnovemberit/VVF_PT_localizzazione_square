"""
Client ArcGIS condiviso.

Raccoglie le operazioni comuni a tutti gli script del progetto:
autenticazione OAuth2 (app authentication) con cache del token, query e
applyEdits sul Feature Layer. Usato sia dal ponte Telegram (app.py) sia dal
sincronizzatore degli interventi (sync_interventi.py).
"""

import os
import json
import time
import logging

import requests

log = logging.getLogger("arcgis")

# Cache in memoria del token OAuth (evitiamo di richiederlo ad ogni chiamata)
_token_cache = {"access_token": None, "expires_at": 0}

TIMEOUT = 30


# ---------------------------------------------------------------------------
# Autenticazione (OAuth2 - app authentication, client_credentials)
# ---------------------------------------------------------------------------
def get_arcgis_token() -> str:
    """Restituisce un token ArcGIS valido, rinnovandolo solo se necessario."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    # Le credenziali si leggono qui e non a livello di modulo, così importare
    # questo file non fallisce se le variabili non sono ancora impostate
    # (utile per il --dry-run di sync_interventi.py, che non tocca ArcGIS).
    resp = requests.post(
        "https://www.arcgis.com/sharing/rest/oauth2/token",
        data={
            "client_id": os.environ["ARCGIS_CLIENT_ID"],
            "client_secret": os.environ["ARCGIS_CLIENT_SECRET"],
            "grant_type": "client_credentials",
            "f": "json",
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(f"Errore token ArcGIS: {data['error']}")

    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + data["expires_in"]
    log.info("Nuovo token ArcGIS ottenuto, valido %s secondi", data["expires_in"])
    return _token_cache["access_token"]


# ---------------------------------------------------------------------------
# Operazioni sul Feature Layer
# ---------------------------------------------------------------------------
def query_features(layer_url: str, where: str, out_fields: str) -> list:
    """
    Interroga il layer e restituisce la lista degli attributi delle feature
    trovate (senza geometria). Solleva un'eccezione se ArcGIS risponde errore.
    """
    resp = requests.get(
        f"{layer_url.rstrip('/')}/query",
        params={
            "where": where,
            "outFields": out_fields,
            "returnGeometry": "false",
            "f": "json",
            "token": get_arcgis_token(),
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Errore query ArcGIS: {data['error']}")
    return [f["attributes"] for f in data.get("features", [])]


def query_poligoni(layer_url: str, out_fields: str) -> dict:
    """
    Come query_features, ma con la geometria: serve per i layer poligonali
    (es. le zone di competenza), che query_features scarta apposta perché i
    punti di chiamate/interventi non ne hanno bisogno.

    Restituisce l'intera risposta (non solo "features"): poligoni_da_query()
    ha bisogno anche di "spatialReference" per sapere in che proiezione sono
    le coordinate.
    """
    resp = requests.get(
        f"{layer_url.rstrip('/')}/query",
        params={
            "where": "1=1",
            "outFields": out_fields,
            "returnGeometry": "true",
            "f": "json",
            "token": get_arcgis_token(),
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Errore query ArcGIS: {data['error']}")
    return data


def descrivi_layer(layer_url: str) -> dict:
    """Definizione del layer (campi, capacita' di modifica, tipo di geometria)."""
    resp = requests.get(
        layer_url.rstrip("/"),
        params={"f": "json", "token": get_arcgis_token()},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Errore leggendo il layer: {data['error']}")
    return data


def apply_edits(layer_url: str, adds=None, updates=None, deletes=None) -> dict:
    """
    Applica in un'unica chiamata inserimenti, aggiornamenti e cancellazioni.
    `deletes` è una lista di OBJECTID. Solleva un'eccezione se ArcGIS risponde
    errore a livello di operazione (i singoli fallimenti per feature vengono
    invece loggati, per non far cadere l'intero ciclo per un record storto).
    """
    payload = {"f": "json", "token": get_arcgis_token()}
    if adds:
        payload["adds"] = json.dumps(adds)
    if updates:
        payload["updates"] = json.dumps(updates)
    if deletes:
        payload["deletes"] = json.dumps(list(deletes))

    if not any(k in payload for k in ("adds", "updates", "deletes")):
        return {}

    resp = requests.post(
        f"{layer_url.rstrip('/')}/applyEdits", data=payload, timeout=TIMEOUT
    )
    resp.raise_for_status()
    result = resp.json()

    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"Errore applyEdits ArcGIS: {result['error']}")

    for operazione in ("addResults", "updateResults", "deleteResults"):
        for esito in result.get(operazione, []):
            if not esito.get("success", True):
                log.error("%s fallito per una feature: %s", operazione, esito.get("error"))

    return result

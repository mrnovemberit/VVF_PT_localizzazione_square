"""
Ponte Telegram -> ArcGIS Feature Layer
Riceve le posizioni live condivise via bot Telegram e le scrive
sul Feature Layer ArcGIS Online tramite l'endpoint REST applyEdits.
"""

import os
import time
import logging
from flask import Flask, request, jsonify
import requests

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bridge")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configurazione (letta da variabili d'ambiente, mai hardcoded nel codice)
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ARCGIS_CLIENT_ID = os.environ["ARCGIS_CLIENT_ID"]
ARCGIS_CLIENT_SECRET = os.environ["ARCGIS_CLIENT_SECRET"]
ARCGIS_FEATURE_LAYER_URL = os.environ["ARCGIS_FEATURE_LAYER_URL"].rstrip("/")
# Es: https://services.arcgis.com/XXXX/arcgis/rest/services/Posizione_partenze2_PT/FeatureServer/0

# Un token segreto a tua scelta, usato per verificare che le richieste al
# webhook arrivino davvero da Telegram e non da terzi (vedi setWebhook più sotto)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# Cache in memoria del token OAuth ArcGIS (evitiamo di richiederlo ad ogni update)
_token_cache = {"access_token": None, "expires_at": 0}


# ---------------------------------------------------------------------------
# Autenticazione ArcGIS (OAuth2 - app authentication, client_credentials)
# ---------------------------------------------------------------------------
def get_arcgis_token() -> str:
    """Restituisce un token ArcGIS valido, rinnovandolo solo se necessario."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    resp = requests.post(
        "https://www.arcgis.com/sharing/rest/oauth2/token",
        data={
            "client_id": ARCGIS_CLIENT_ID,
            "client_secret": ARCGIS_CLIENT_SECRET,
            "grant_type": "client_credentials",
            "f": "json",
        },
        timeout=15,
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
def find_existing_object_id(operator_id: str):
    """
    Cerca se esiste già una feature per questo operatore.
    Interroghiamo direttamente ArcGIS ad ogni update (niente storage
    esterno da mantenere) - per il volume di richieste di questo progetto
    è pienamente sufficiente.
    """
    token = get_arcgis_token()
    resp = requests.get(
        f"{ARCGIS_FEATURE_LAYER_URL}/query",
        params={
            "where": f"OperatorID='{operator_id}'",
            "outFields": "OBJECTID",
            "f": "json",
            "token": token,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    features = data.get("features", [])
    if features:
        return features[0]["attributes"]["OBJECTID"]
    return None


def upsert_position(operator_id: str, operator_name: str, lat: float, lon: float, live_period=None):
    """Crea o aggiorna la posizione dell'operatore sul Feature Layer."""
    token = get_arcgis_token()
    now_ms = int(time.time() * 1000)
    live_until_ms = now_ms + (live_period * 1000) if live_period else None

    attributes = {
        "OperatorID": operator_id,
        "OperatorName": operator_name,
        "Data_ora": now_ms,
        "Status": "live",
    }
    if live_until_ms:
        attributes["LiveUntil"] = live_until_ms

    geometry = {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}

    existing_oid = find_existing_object_id(operator_id)

    if existing_oid:
        payload = {
            "updates": [{
                "attributes": {**attributes, "OBJECTID": existing_oid},
                "geometry": geometry,
            }]
        }
        edit_url = f"{ARCGIS_FEATURE_LAYER_URL}/applyEdits"
    else:
        payload = {
            "adds": [{
                "attributes": attributes,
                "geometry": geometry,
            }]
        }
        edit_url = f"{ARCGIS_FEATURE_LAYER_URL}/applyEdits"

    resp = requests.post(
        edit_url,
        data={
            "f": "json",
            "token": token,
            **{k: __import__("json").dumps(v) for k, v in payload.items()},
        },
        timeout=15,
    )
    log.info("applyEdits status HTTP: %s", resp.status_code)
    log.info("applyEdits URL chiamato: %s", edit_url)
    log.info("applyEdits risposta grezza: %s", resp.text)
    resp.raise_for_status()
    result = resp.json()
    if isinstance(result, dict) and "error" in result:
        log.error("ArcGIS ha risposto con un errore: %s", result["error"])
    log.info("Risultato applyEdits per %s: %s", operator_id, result)
    return result


# ---------------------------------------------------------------------------
# Webhook Telegram
# ---------------------------------------------------------------------------
@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    # Verifica che la richiesta arrivi davvero da Telegram (header segreto
    # impostato in fase di setWebhook, vedi setup_webhook.py)
    if WEBHOOK_SECRET:
        header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header_secret != WEBHOOK_SECRET:
            log.warning("Richiesta webhook con secret non valido, ignorata")
            return jsonify({"ok": False}), 403

    update = request.get_json(silent=True) or {}
    log.info("Update ricevuto: %s", update)

    message = update.get("message") or update.get("edited_message")
    if not message:
        return jsonify({"ok": True})  # update non rilevante, ignoralo

    location = message.get("location")
    if not location:
        return jsonify({"ok": True})  # non è un update di posizione

    from_user = message.get("from", {})
    operator_id = str(from_user.get("id", "unknown"))
    operator_name = from_user.get("first_name", "Operatore")
    if from_user.get("last_name"):
        operator_name += f" {from_user['last_name']}"

    lat = location["latitude"]
    lon = location["longitude"]
    live_period = location.get("live_period")  # presente solo nel primo messaggio

    try:
        upsert_position(operator_id, operator_name, lat, lon, live_period)
    except Exception:
        log.exception("Errore scrivendo su ArcGIS")
        return jsonify({"ok": False}), 500

    return jsonify({"ok": True})


@app.route("/", methods=["GET"])
def health_check():
    """Endpoint di verifica - utile anche per 'svegliare' il servizio su Render."""
    return jsonify({"status": "ok", "service": "telegram-arcgis-bridge"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
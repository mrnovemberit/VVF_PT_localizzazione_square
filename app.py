"""
Ponte Telegram -> ArcGIS Feature Layer
Riceve le posizioni live condivise via bot Telegram e le scrive
sul Feature Layer ArcGIS Online tramite l'endpoint REST applyEdits.
"""

import os
import time
import math
import logging
from flask import Flask, request, jsonify

from arcgis_client import query_features, apply_edits

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bridge")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configurazione (letta da variabili d'ambiente, mai hardcoded nel codice)
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ARCGIS_FEATURE_LAYER_URL = os.environ["ARCGIS_FEATURE_LAYER_URL"].rstrip("/")
# Es: https://services.arcgis.com/XXXX/arcgis/rest/services/Posizione_partenze2_PT/FeatureServer/0

# Le credenziali OAuth le legge arcgis_client al momento del bisogno; qui
# controlliamo solo che siano impostate, così un errore di configurazione
# emerge all'avvio del servizio e non al primo update ricevuto.
for _chiave in ("ARCGIS_CLIENT_ID", "ARCGIS_CLIENT_SECRET"):
    if not os.environ.get(_chiave):
        raise RuntimeError(f"Variabile d'ambiente mancante: {_chiave}")

# Un token segreto a tua scelta, usato per verificare che le richieste al
# webhook arrivino davvero da Telegram e non da terzi (vedi setWebhook più sotto)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


# ---------------------------------------------------------------------------
# Operazioni sul Feature Layer
# ---------------------------------------------------------------------------
def find_existing_feature(operator_id: str):
    """
    Cerca se esiste già una feature per questo operatore, e in tal caso
    restituisce anche la sua ultima posizione nota (serve per calcolare
    lo spostamento rispetto al nuovo update). Interroghiamo direttamente
    ArcGIS ad ogni update (niente storage esterno da mantenere) - per il
    volume di richieste di questo progetto è pienamente sufficiente.
    """
    trovate = query_features(
        ARCGIS_FEATURE_LAYER_URL,
        where=f"OperatorID='{operator_id}'",
        out_fields="OBJECTID,Latitudine,Longitudine",
    )
    return trovate[0] if trovate else None


def distanza_metri(lat1, lon1, lat2, lon2):
    """Distanza approssimata in metri tra due coordinate (formula di Haversine)."""
    raggio_terra = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * raggio_terra * math.asin(math.sqrt(a))


def upsert_position(operator_id: str, operator_name: str, lat: float, lon: float,
                     live_period=None, heading=None, horizontal_accuracy=None):
    """Crea o aggiorna la posizione dell'operatore sul Feature Layer."""
    now_ms = int(time.time() * 1000)
    live_until_ms = now_ms + (live_period * 1000) if live_period else None

    existing = find_existing_feature(operator_id)

    # Soglia dinamica: almeno 15m, o il doppio della precisione GPS se questa
    # è più larga (evita falsi "in movimento" per il solo rumore del GPS)
    if existing and existing.get("Latitudine") is not None and existing.get("Longitudine") is not None:
        soglia = max(15.0, 2 * horizontal_accuracy) if horizontal_accuracy else 15.0
        distanza = distanza_metri(existing["Latitudine"], existing["Longitudine"], lat, lon)
        moving_status = "in movimento" if distanza > soglia else "fermo"
        log.info("Operatore %s: distanza %.1fm, soglia %.1fm -> %s",
                  operator_id, distanza, soglia, moving_status)
    else:
        # Prima posizione ricevuta, non c'è ancora un punto precedente da confrontare
        moving_status = "fermo"

    attributes = {
        "OperatorID": operator_id,
        "OperatorName": operator_name,
        "Data_ora": now_ms,
        "Status": "live",
        "Moving_status": moving_status,
        "Latitudine": lat,
        "Longitudine": lon,
    }
    if live_until_ms:
        attributes["LiveUntil"] = live_until_ms
    if heading is not None:
        attributes["Direzione"] = heading
    if horizontal_accuracy is not None:
        attributes["Precisione_m"] = horizontal_accuracy

    geometry = {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}

    feature = {"attributes": attributes, "geometry": geometry}
    if existing:
        feature["attributes"]["OBJECTID"] = existing["OBJECTID"]
        result = apply_edits(ARCGIS_FEATURE_LAYER_URL, updates=[feature])
    else:
        result = apply_edits(ARCGIS_FEATURE_LAYER_URL, adds=[feature])

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
    heading = location.get("heading")
    horizontal_accuracy = location.get("horizontal_accuracy")

    try:
        upsert_position(operator_id, operator_name, lat, lon, live_period,
                         heading, horizontal_accuracy)
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
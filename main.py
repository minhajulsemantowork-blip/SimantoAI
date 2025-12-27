import os
import json
import logging
import traceback
from flask import Flask, request
import firebase_admin
from firebase_admin import credentials, firestore

# --------------------------------------------------
# LOGGING
# --------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --------------------------------------------------
# FLASK APP
# --------------------------------------------------
app = Flask(__name__)

# --------------------------------------------------
# FIREBASE INIT
# --------------------------------------------------
db = None

try:
    service_account_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

    if not service_account_json:
        raise ValueError("FIREBASE_SERVICE_ACCOUNT missing")

    cred = credentials.Certificate(json.loads(service_account_json))
    firebase_admin.initialize_app(cred)
    db = firestore.client()

    logger.info("🔥 Firebase initialized successfully")

except Exception as e:
    logger.error("❌ Firebase init failed")
    logger.error(e)

# --------------------------------------------------
# CACHES
# --------------------------------------------------
_client_cache = {}
_page_to_client_cache = {}
_subcollection_cache = {}

# --------------------------------------------------
# FIXED: SUBCOLLECTION DOCUMENT READER
# --------------------------------------------------
def get_subcollection_document(client_id, subcollection_name, document_id):
    """
    Correct Firestore path:
    clients/{clientId}/{subcollection}/{document}
    """
    cache_key = f"{client_id}/{subcollection_name}/{document_id}"

    if cache_key in _subcollection_cache:
        return _subcollection_cache[cache_key]

    try:
        doc_ref = (
            db.collection("clients")
            .document(client_id)
            .collection(subcollection_name)
            .document(document_id)
        )

        doc = doc_ref.get()

        if doc.exists:
            data = doc.to_dict()
            _subcollection_cache[cache_key] = data
            return data

        _subcollection_cache[cache_key] = None
        return None

    except Exception:
        logger.error(traceback.format_exc())
        return None

# --------------------------------------------------
# CLIENT DATA LOADER (SUBCOLLECTION AWARE)
# --------------------------------------------------
def get_client_data(client_id):
    if client_id in _client_cache:
        return _client_cache[client_id]

    try:
        client_ref = db.collection("clients").document(client_id)
        client_doc = client_ref.get()

        if not client_doc.exists:
            return None

        data = client_doc.to_dict() or {}

        # integrations
        integrations = {}
        for doc in client_ref.collection("integrations").stream():
            integrations[doc.id] = doc.to_dict()

        if integrations:
            data["integrations"] = integrations

        # settings
        settings = {}
        for doc in client_ref.collection("settings").stream():
            settings.update(doc.to_dict())

        if settings:
            data["settings"] = settings

        _client_cache[client_id] = data
        return data

    except Exception:
        logger.error(traceback.format_exc())
        return None

# --------------------------------------------------
# FIXED: FIND CLIENT BY FACEBOOK PAGE ID
# --------------------------------------------------
def find_client_by_page_id(page_id):
    page_id = str(page_id)

    if page_id in _page_to_client_cache:
        return _page_to_client_cache[page_id]

    try:
        clients = db.collection("clients").stream()

        for client in clients:
            fb_data = get_subcollection_document(
                client.id,
                "integrations",
                "facebook"
            )

            if not fb_data:
                continue

            if str(fb_data.get("pageId")) == page_id:
                _page_to_client_cache[page_id] = client.id
                return client.id

        return None

    except Exception:
        logger.error(traceback.format_exc())
        return None

# --------------------------------------------------
# FIXED: FACEBOOK TOKEN GETTER
# --------------------------------------------------
def get_facebook_token(client_id):
    fb_data = get_subcollection_document(
        client_id,
        "integrations",
        "facebook"
    )

    if not fb_data:
        return None

    return fb_data.get("pageAccessToken")

# --------------------------------------------------
# FIXED: GEMINI KEY GETTER
# --------------------------------------------------
def get_gemini_key(client_id):
    settings = get_subcollection_document(
        client_id,
        "settings",
        "apiKeys"
    )

    if not settings:
        return None

    return settings.get("geminiApiKey")

# --------------------------------------------------
# FACEBOOK WEBHOOK VERIFY
# --------------------------------------------------
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token:
        logger.info("Webhook verified")
        return challenge, 200

    return "Forbidden", 403

# --------------------------------------------------
# FACEBOOK WEBHOOK MESSAGE HANDLER
# --------------------------------------------------
@app.route("/webhook", methods=["POST"])
def handle_webhook():
    data = request.get_json()

    try:
        entry = data["entry"][0]
        messaging = entry["messaging"][0]
        page_id = entry["id"]

        client_id = find_client_by_page_id(page_id)

        if not client_id:
            logger.error("❌ Client not found for page")
            return "OK", 200

        fb_token = get_facebook_token(client_id)
        gemini_key = get_gemini_key(client_id)

        if not fb_token or not gemini_key:
            logger.error("❌ Missing token or API key")
            return "OK", 200

        # ---- Your existing message handling logic here ----

        logger.info(f"✅ Message processed for client {client_id}")
        return "OK", 200

    except Exception:
        logger.error(traceback.format_exc())
        return "OK", 200

# --------------------------------------------------
# RUN
# --------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

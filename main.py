import os
import json
import requests
from datetime import datetime

from flask import Flask, request
from google import genai
from langdetect import detect

import firebase_admin
from firebase_admin import credentials, firestore

# ================= CONFIG =================
BOT_NAME = "Simanto"

# ================= FIREBASE INIT =================
FIREBASE_SERVICE_ACCOUNT = os.getenv("FIREBASE_SERVICE_ACCOUNT")
cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT))
firebase_admin.initialize_app(cred)
db = firestore.client()

# ================= FLASK APP =================
app = Flask(__name__)

# ================= HELPERS =================
def get_client_id_by_page(page_id):
    docs = (
        db.collection("clients")
        .where("integrations.facebook.pageId", "==", page_id)
        .limit(1)
        .get()
    )
    if docs:
        return docs[0].id
    return None


def get_facebook_token(client_id):
    doc = db.collection("clients").document(client_id).get()
    if not doc.exists:
        return None
    return (
        doc.to_dict()
        .get("integrations", {})
        .get("facebook", {})
        .get("pageAccessToken")
    )


def get_gemini_key(client_id):
    doc = db.collection("clients").document(client_id).get()
    if not doc.exists:
        return None
    return (
        doc.to_dict()
        .get("settings", {})
        .get("apiKeys", {})
        .get("geminiApiKey")
    )


def load_memory(client_id):
    ref = (
        db.collection("clients")
        .document(client_id)
        .collection("chat_history")
        .document("history")
    )
    doc = ref.get()
    if doc.exists:
        return doc.to_dict().get("history", [])
    return []


def save_memory(client_id, history):
    ref = (
        db.collection("clients")
        .document(client_id)
        .collection("chat_history")
        .document("history")
    )
    ref.set({"history": history})


def send_facebook_message(page_token, sender_id, text):
    url = "https://graph.facebook.com/v18.0/me/messages"
    payload = {
        "messaging_type": "RESPONSE",
        "recipient": {"id": sender_id},
        "message": {"text": text},
    }
    params = {"access_token": page_token}
    r = requests.post(url, params=params, json=payload)
    print("📨 FB SEND:", r.status_code, r.text)
    return r.status_code == 200


def chat_with_gemini(client_id, user_text):
    history = load_memory(client_id)

    try:
        lang = detect(user_text)
        language = "bn" if lang.startswith("bn") else "en"
    except:
        language = "en"

    convo = ""
    for h in history[-10:]:
        convo += f"User: {h['user']}\n{BOT_NAME}: {h['bot']}\n"
    convo += f"User: {user_text}\n{BOT_NAME}:"

    gemini_key = get_gemini_key(client_id)
    if not gemini_key:
        return "⚠️ Gemini API key সেট করা নেই।"

    client = genai.Client(api_key=gemini_key)

    prompt = f"""
You are a friendly and persuasive sales assistant.
Reply politely and naturally.
Maintain {language} language.

Conversation:
{convo}
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )

    reply = response.text.strip()

    history.append({"user": user_text, "bot": reply})
    save_memory(client_id, history)

    return reply

# ================= ROUTES =================
@app.route("/", methods=["GET"])
def home():
    return "🤖 Simanto AI Bot is running!"

# ---------- WEBHOOK VERIFY ----------
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token:
        clients = db.collection("clients").get()
        for c in clients:
            fb = c.to_dict().get("integrations", {}).get("facebook", {})
            if fb.get("verifyToken") == token:
                print("✅ Webhook verified")
                return challenge, 200

        return "Invalid verify token", 403

    return "Invalid request", 400

# ---------- WEBHOOK POST ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print("===== WEBHOOK RECEIVED =====")
    print(json.dumps(data, indent=2))

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):

            sender_id = event.get("sender", {}).get("id")
            page_id = event.get("recipient", {}).get("id")
            message = event.get("message", {})
            user_text = message.get("text")

            print("Sender:", sender_id)
            print("Page:", page_id)
            print("Message:", user_text)

            if not sender_id or not page_id or not user_text:
                print("⛔ Invalid message payload")
                continue

            client_id = get_client_id_by_page(page_id)
            print("Client ID:", client_id)

            if not client_id:
                print("⛔ No client found for page")
                continue

            page_token = get_facebook_token(client_id)
            if not page_token:
                print("⛔ Page token missing")
                continue

            reply = chat_with_gemini(client_id, user_text)
            send_facebook_message(page_token, sender_id, reply)

    return "EVENT_RECEIVED", 200

# ================= RUN =================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

import os
import json
from datetime import datetime

from flask import Flask, request, jsonify
from google import genai
from langdetect import detect

import firebase_admin
from firebase_admin import credentials, firestore


# ================== CONFIG ==================
BOT_NAME = "Simanto"
PORT = int(os.getenv("PORT", 5000))


# ================== FIREBASE INIT ==================
firebase_json = os.getenv("FIREBASE_SERVICE_ACCOUNT")
if not firebase_json:
    raise RuntimeError("FIREBASE_SERVICE_ACCOUNT env variable not set")

cred_dict = json.loads(firebase_json)
cred = credentials.Certificate(cred_dict)
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

db = firestore.client()


# ================== GEMINI ==================
def get_gemini_client(api_key: str):
    if not api_key:
        raise RuntimeError("Gemini API key missing for this client")
    return genai.Client(api_key=api_key)


def get_client_gemini_key(client_id: str):
    """Fetch Gemini API key from subcollection settings/apiKeys"""
    api_doc = (
        db.collection("clients")
        .document(client_id)
        .collection("settings")
        .document("apiKeys")
        .get()
    )
    if api_doc.exists:
        data = api_doc.to_dict()
        return data.get("geminiApiKey")
    return None


# ================== MEMORY ==================
def load_memory(client_id):
    ref = (
        db.collection("clients")
        .document(client_id)
        .collection("chat_history")
        .document("history")
    )
    doc = ref.get()
    return doc.to_dict() if doc.exists else {"history": []}


def save_memory(client_id, memory):
    ref = (
        db.collection("clients")
        .document(client_id)
        .collection("chat_history")
        .document("history")
    )
    ref.set(memory)


# ================== CLIENT SETTINGS ==================
def get_client_settings(client_id):
    doc = db.collection("clients").document(client_id).get()
    data = doc.to_dict() or {}
    return (
        data.get("botSettings", {}),
        data.get("businessSettings", {}),
        data.get("faqs", []),
        data.get("products", []),
    )


def get_client_id_by_page(page_id):
    q = (
        db.collection("clients")
        .where("facebookPageId", "==", page_id)
        .limit(1)
        .get()
    )
    return q[0].id if q else None


# ================== ORDER ==================
def generate_order_id():
    today = datetime.now().strftime("%Y-%m-%d")
    return f"ORD-{today}-{int(datetime.now().timestamp())}"


def save_order(client_id, order_data):
    order_id = generate_order_id()
    order_data.update(
        {
            "order_id": order_id,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "status": "confirmed",
        }
    )
    db.collection("clients").document(client_id).collection("orders").add(order_data)
    return order_id


def extract_order_nlp(_text):
    """Simple placeholder for order extraction"""
    return {
        "product_name": "Unknown",
        "quantity": "1",
        "product_details": "",
        "customer_name": "",
        "phone": "",
        "address": "",
    }


# ================== CHAT CORE ==================
def chat_with_gemini(client_id, user_text):
    gemini_key = get_client_gemini_key(client_id)
    if not gemini_key:
        return (
            "⚠️ Gemini API key সেট করা নেই। "
            "দয়া করে আপনার ড্যাশবোর্ড থেকে API key যোগ করুন।"
        )

    memory = load_memory(client_id)
    _, _, _, products = get_client_settings(client_id)

    try:
        lang = detect(user_text)
        language = "bn" if lang.startswith("bn") else "en"
    except Exception:
        language = "en"

    conversation = ""
    for h in memory["history"]:
        conversation += f"User: {h['user']}\n{BOT_NAME}: {h['bot']}\n"

    product_context = ""
    if products:
        product_context = "Products:\n"
        for p in products:
            product_context += f"- {p.get('name')} ৳{p.get('price')}\n"

    prompt = f"""
    You are {BOT_NAME}, a polite and persuasive sales assistant.
    Language: {language}

    {product_context}

    Conversation:
    {conversation}
    User: {user_text}
    {BOT_NAME}:
    """

    client = get_gemini_client(gemini_key)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )

    bot_reply = response.text.strip()

    memory["history"].append({"user": user_text, "bot": bot_reply})
    save_memory(client_id, memory)

    if any(p in user_text.lower() for p in ["order confirm", "confirm order", "অর্ডার কনফার্ম"]):
        order_data = extract_order_nlp(user_text)
        order_id = save_order(client_id, order_data)
        bot_reply += (
            f"\n✅ অর্ডার কনফার্ম হয়েছে! ID: {order_id}"
            if language == "bn"
            else f"\n✅ Order confirmed! ID: {order_id}"
        )

    return bot_reply


# ================== FLASK SERVER ==================
app = Flask(__name__)


@app.route("/", methods=["GET"])
def health():
    return "Simanto AI Bot is running 🚀"


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    page_id = data.get("page_id")
    message = data.get("message")

    if not page_id or not message:
        return jsonify({"error": "Invalid payload"}), 400

    client_id = get_client_id_by_page(page_id)
    if not client_id:
        return jsonify({"reply": "Page not connected with any client"}), 404

    reply = chat_with_gemini(client_id, message)
    return jsonify({"reply": reply})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)

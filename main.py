import os
import json
from google import genai
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime
from langdetect import detect
from flask import Flask, request, jsonify, Response

# ----- CONFIG -----
BOT_NAME = "Simanto"

# ----- FIREBASE SETUP -----
FIREBASE_SERVICE_ACCOUNT = os.getenv("FIREBASE_SERVICE_ACCOUNT")
cred_dict = json.loads(FIREBASE_SERVICE_ACCOUNT)
cred = credentials.Certificate(cred_dict)
firebase_admin.initialize_app(cred)
db = firestore.client()

# ----- LOAD MEMORY -----
def load_memory(client_id):
    doc_ref = db.collection("clients").document(client_id).collection("chat_history").document("history")
    doc = doc_ref.get()
    if doc.exists:
        return doc.to_dict()
    return {"history": []}

def save_memory(client_id, memory):
    doc_ref = db.collection("clients").document(client_id).collection("chat_history").document("history")
    doc_ref.set(memory)

# ----- FETCH GEMINI API KEY -----
def get_gemini_key(client_id):
    try:
        doc = db.collection("clients").document(client_id).get()
        return doc.to_dict().get("settings", {}).get("apiKeys", {}).get("geminiApiKey")
    except:
        return None

# ----- FETCH CLIENT SETTINGS -----
def get_client_settings(client_id):
    try:
        doc = db.collection("clients").document(client_id).get()
        data = doc.to_dict()
        return (
            data.get("botSettings", {}),
            data.get("businessSettings", {}),
            data.get("faqs", []),
            data.get("products", [])
        )
    except:
        return {}, {}, [], []

# ----- FIND CLIENT BY FACEBOOK PAGE ID -----
def get_client_id_by_page(page_id):
    clients_ref = db.collection("clients")
    query = clients_ref.where("integrations.facebook.pageId", "==", page_id).limit(1).get()
    if query:
        return query[0].id
    return None

# ----- GENERATE UNIQUE ORDER ID -----
def generate_order_id(client_id):
    today = datetime.now().strftime("%Y%m%d")
    orders_ref = db.collection("clients").document(client_id).collection("orders")
    existing_orders = orders_ref.where("date", "==", today).get()
    counter = len(existing_orders) + 1
    return f"ORD{today}-{counter:03d}"

# ----- SAVE ORDER -----
def save_order(client_id, order_data):
    order_id = generate_order_id(client_id)
    order_doc = {
        "order_id": order_id,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "product_name": order_data.get("product_name", ""),
        "quantity": order_data.get("quantity", ""),
        "product_details": order_data.get("product_details", ""),
        "customer_name": order_data.get("customer_name", ""),
        "phone": order_data.get("phone", ""),
        "address": order_data.get("address", ""),
        "status": "confirmed"
    }
    db.collection("clients").document(client_id).collection("orders").add(order_doc)
    return order_id

# ----- NLP PLACEHOLDER -----
def extract_order_nlp(user_text, language="en"):
    return {
        "product_name": "Unknown Product",
        "quantity": "1",
        "product_details": "",
        "customer_name": "",
        "phone": "",
        "address": ""
    }

# ----- PERSUASION MODULE -----
def persuasion_suggestions(user_text, language="en", context=None, gemini_api_key=None):
    if not gemini_api_key:
        return ""
    client = genai.Client(api_key=gemini_api_key)

    prompt = f"""
    You are a friendly, polite, persuasive sales assistant.
    User message: "{user_text}"
    Context: "{context}"
    Reply shortly in {language}.
    """

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text.strip()

# ----- CHAT -----
def chat_with_gemini(client_id, user_text):
    memory = load_memory(client_id)
    bot_settings, business_settings, faqs, products = get_client_settings(client_id)

    try:
        lang = detect(user_text)
        language = "bn" if lang.startswith("bn") else "en"
    except:
        language = "en"

    conversation = ""
    for msg in memory["history"]:
        conversation += f"User: {msg['user']}\n{BOT_NAME}: {msg['bot']}\n"
    conversation += f"User: {user_text}\n{BOT_NAME}:"

    context_text = ""
    if bot_settings.get("autoReplyEnabled"):
        context_text += bot_settings.get("autoReplyMessage", "") + "\n"

    if faqs:
        context_text += "FAQs:\n" + "\n".join(
            [f"Q:{f.get('question')}\nA:{f.get('answer')}" for f in faqs]
        ) + "\n"

    if products:
        context_text += "Products:\n" + "\n".join(
            [f"{p.get('name')} - ৳{p.get('price')}" for p in products]
        ) + "\n"

    conversation = context_text + conversation

    GEMINI_API_KEY = get_gemini_key(client_id)
    if not GEMINI_API_KEY:
        return "⚠️ Gemini API key সেট করা নেই।"

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=conversation
    )

    bot_reply = response.text.strip()
    memory["history"].append({"user": user_text, "bot": bot_reply})
    save_memory(client_id, memory)

    return bot_reply

# ----- FLASK APP -----
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "🤖 Simanto AI Bot is running!"

# =================================================
# 🔐 WEBHOOK VERIFY (SaaS CORRECT - FIXED TOKEN)
# =================================================
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    VERIFY_TOKEN = os.getenv("MASTER_VERIFY_TOKEN")

    if token == VERIFY_TOKEN:
        return Response(challenge, status=200)

    return Response("Invalid verify token", status=403)

# =================================================
# 📩 FACEBOOK MESSAGE HANDLER
# =================================================
@app.route("/webhook", methods=["POST"])
def on_message_received():
    data = request.get_json()
    page_id = data.get("page_id")
    user_text = data.get("message")

    client_id = get_client_id_by_page(page_id)
    if not client_id:
        return jsonify({"reply": "Page not connected."})

    reply = chat_with_gemini(client_id, user_text)
    return jsonify({"reply": reply})

# ----- MAIN -----
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

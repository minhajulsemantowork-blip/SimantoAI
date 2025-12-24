import os
import json
from google import genai
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime
from langdetect import detect  # Language detection
from flask import Flask, request, jsonify

# ----- CONFIG -----
BOT_NAME = "Simanto"

# Firebase setup
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
        key = doc.to_dict().get("settings", {}).get("apiKeys", {}).get("geminiApiKey")
        return key  # No fallback
    except:
        return None

# ----- FETCH CLIENT SETTINGS -----
def get_client_settings(client_id):
    try:
        doc = db.collection("clients").document(client_id).get()
        data = doc.to_dict()
        bot_settings = data.get("botSettings", {})
        business_settings = data.get("businessSettings", {})
        faqs = data.get("faqs", [])
        products = data.get("products", [])
        return bot_settings, business_settings, faqs, products
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

# ----- EXTRACT ORDER DATA (placeholder) -----
def extract_order_nlp(user_text, language="en"):
    return {
        "product_name": "Unknown Product",
        "quantity": "1",
        "product_details": "",
        "customer_name": "",
        "phone": "",
        "address": ""
    }

# ----- SMART PERSUASION MODULE -----
def persuasion_suggestions(user_text, language="en", context=None, gemini_api_key=None):
    if not gemini_api_key:
        return ""
    client = genai.Client(api_key=gemini_api_key)

    prompt = f"""
    You are a friendly, polite, and persuasive sales assistant.
    User may hesitate to place an order.
    Based on the following user message and context, generate a short, friendly suggestion to encourage order confirmation.
    Maintain {language} language.
    User message: "{user_text}"
    Conversation context: "{context}"
    Respond only with the suggestion, no explanations.
    """

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text.strip()

# ----- CHAT FUNCTION -----
def chat_with_gemini(client_id, user_text):
    memory = load_memory(client_id)
    bot_settings, business_settings, faqs, products = get_client_settings(client_id)

    # Language detection
    try:
        lang = detect(user_text)
        language = "bn" if lang.startswith("bn") else "en"
    except:
        language = "en"

    # Prepare conversation context
    conversation = ""
    for msg in memory["history"]:
        conversation += f"User: {msg['user']}\n{BOT_NAME}: {msg['bot']}\n"
    conversation += f"User: {user_text}\n{BOT_NAME}:"

    context_text = ""
    if bot_settings.get("autoReplyEnabled"):
        context_text += f"AutoReplyMessage: {bot_settings.get('autoReplyMessage', '')}\n"
    context_text += f"Business Hours: {business_settings.get('businessHours', '')}\n"
    context_text += f"Timezone: {business_settings.get('timezone', '')}\n"
    if faqs:
        context_text += "FAQs:\n" + "\n".join([f"Q:{f.get('question','')}\nA:{f.get('answer','')}" for f in faqs]) + "\n"
    if products:
        context_text += "Products:\n" + "\n".join([f"{p.get('name','')} - ৳{p.get('price',0)}" for p in products]) + "\n"

    conversation = context_text + "\n" + conversation

    # Gemini API
    GEMINI_API_KEY = get_gemini_key(client_id)
    if not GEMINI_API_KEY:
        return "⚠️ Gemini API key সেট করা নেই। দয়া করে আপনার ড্যাশবোর্ড থেকে API key যোগ করুন।"

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    You are a friendly, persuasive sales assistant.
    Reply politely to the user and encourage order confirmation.
    Maintain {language} language.
    Conversation so far:
    {conversation}
    """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    bot_reply = response.text.strip()

    # Save memory
    memory["history"].append({"user": user_text, "bot": bot_reply})
    save_memory(client_id, memory)

    # ----- CHECK IF ORDER DETAILS PROVIDED -----
    order_confirm_phrases = ["order confirm", "confirm my order", "অর্ডার কনফার্ম", "অর্ডার নিশ্চিত"]
    if any(phrase in user_text.lower() for phrase in order_confirm_phrases):
        order_data = extract_order_nlp(user_text, language)
        order_id = save_order(client_id, order_data)
        if language == "bn":
            bot_reply += f"\n✅ অর্ডার কনফার্ম হয়েছে! আপনার অর্ডার আইডি: {order_id}"
        else:
            bot_reply += f"\n✅ Order confirmed! Your Order ID is {order_id}"
    else:
        suggestion = persuasion_suggestions(user_text, language, context=conversation, gemini_api_key=GEMINI_API_KEY)
        if suggestion:
            bot_reply += f"\n💡 {suggestion}"

    return bot_reply

# ----- HANDLE INCOMING MESSAGE -----
app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def on_message_received():
    data = request.get_json()
    page_id = data.get("page_id")
    user_text = data.get("message")

    client_id = get_client_id_by_page(page_id)
    if not client_id:
        return jsonify({"reply": "Sorry, your page is not connected with any client."})

    reply = chat_with_gemini(client_id, user_text)
    return jsonify({"reply": reply})

# ----- RUN SERVER ONLY -----
if __name__ == "__main__":
    import sys
    # Check if running on Render (no stdin)
    if os.environ.get("RENDER") == "true":
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
    else:
        # Local testing loop
        print(f"🤖 {BOT_NAME} is now running locally... (type 'exit' to quit)\n")
        while True:
            page_id = input("Page ID: ")
            user_input = input("You: ")

            if user_input.lower() in ["exit", "quit", "bye"]:
                print(f"{BOT_NAME}: Bye ❤️")
                break

            reply = chat_with_gemini(client_id=get_client_id_by_page(page_id), user_text=user_input)
            print(f"{BOT_NAME}: {reply}\n")

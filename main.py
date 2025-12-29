import os
import json
import requests
import logging
import traceback
from datetime import datetime
from flask import Flask, request
from openai import OpenAI  # Groq uses OpenAI client structure
from langdetect import detect
from supabase import create_client, Client

# ================= LOGGING SETUP =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ================= CONFIG =================
BOT_NAME = "Simanto"

# ================= SUPABASE INIT =================
try:
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
    supabase: Client = create_client(supabase_url, supabase_key)
    logger.info("✅ Supabase initialized successfully")
except Exception as e:
    logger.error(f"❌ Failed to initialize Supabase: {str(e)}")
    supabase = None

app = Flask(__name__)
_page_to_client_cache = {}

# ================= HELPER FUNCTIONS =================
def find_client_by_page_id(page_id):
    page_id_str = str(page_id)
    if page_id_str in _page_to_client_cache:
        return _page_to_client_cache[page_id_str]
    
    response = supabase.table("facebook_integrations").select("user_id").eq("page_id", page_id_str).eq("is_connected", True).execute()
    if response.data:
        admin_id = str(response.data[0]["user_id"])
        _page_to_client_cache[page_id_str] = admin_id
        return admin_id
    return None

def get_facebook_token(admin_id):
    response = supabase.table("facebook_integrations").select("page_access_token").eq("user_id", admin_id).eq("is_connected", True).execute()
    return response.data[0]["page_access_token"] if response.data else None

def get_groq_key(admin_id):
    # আমরা ডাটাবেসের কলাম নাম gemini_api_key-ই রাখছি যাতে আপনার টেবিল পরিবর্তন করতে না হয়
    response = supabase.table("api_keys").select("gemini_api_key").eq("user_id", admin_id).execute()
    return response.data[0]["gemini_api_key"] if response.data else None

def load_chat_history(admin_id, customer_id):
    try:
        response = supabase.table("chat_history").select("messages").eq("user_id", admin_id).eq("customer_id", customer_id).execute()
        return response.data[0]["messages"] if response.data else []
    except: return []

def save_chat_history(admin_id, customer_id, history):
    supabase.table("chat_history").upsert({
        "user_id": admin_id, "customer_id": customer_id, "messages": history, "last_updated": datetime.utcnow().isoformat()
    }).execute()

def send_facebook_message(page_token, customer_id, message_text):
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={page_token}"
    payload = {"recipient": {"id": customer_id}, "message": {"text": message_text}}
    requests.post(url, json=payload, timeout=30)

# ================= AI RESPONSE (GROQ POWERED) =================
def generate_ai_response(admin_id, user_message, customer_id):
    try:
        api_key = get_groq_key(admin_id)
        if not api_key: return "⚠️ AI service not configured."

        # Groq Client Initialization
        client = OpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=api_key
        )

        history = load_chat_history(admin_id, customer_id)
        
        try: language = "bangla" if detect(user_message) == "bn" else "english"
        except: language = "english"

        system_msg = {"role": "system", "content": f"You are {BOT_NAME}, a helpful assistant. Answer in {language}."}
        
        messages = [system_msg]
        for msg in history[-6:]: # শেষ ৬টি মেসেজ কন্টেক্সট হিসেবে
            messages.append({"role": "user", "content": msg.get('user', '')})
            messages.append({"role": "assistant", "content": msg.get('bot', '')})
        messages.append({"role": "user", "content": user_message})

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.7,
            max_tokens=800
        )

        ai_response = response.choices[0].message.content.strip()

        history.append({"user": user_message, "bot": ai_response, "timestamp": datetime.utcnow().isoformat()})
        save_chat_history(admin_id, customer_id, history[-15:])
        
        return ai_response

    except Exception as e:
        logger.error(f"❌ Groq Error: {str(e)}")
        return "দুঃখিত, আমি এখন উত্তর দিতে পারছি না।"

# ================= WEBHOOK ROUTES =================
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    challenge = request.args.get("hub.challenge")
    return challenge, 200

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    try:
        data = request.get_json()
        for entry in data.get("entry", []):
            for event in entry.get("messaging", []):
                sender_id = event.get("sender", {}).get("id")
                recipient_id = event.get("recipient", {}).get("id")
                message_text = event.get("message", {}).get("text")
                
                if message_text:
                    admin_id = find_client_by_page_id(recipient_id)
                    if admin_id:
                        page_token = get_facebook_token(admin_id)
                        ai_response = generate_ai_response(admin_id, message_text, sender_id)
                        send_facebook_message(page_token, sender_id, ai_response)
        return "EVENT_RECEIVED", 200
    except: return "ERROR", 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

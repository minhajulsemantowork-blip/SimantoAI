import os
import json
import requests
import logging
import traceback
from datetime import datetime
from flask import Flask, request
import google.generativeai as genai # Stable Library
from langdetect import detect
from supabase import create_client, Client

# ================= LOGGING SETUP =================
logging.basicConfig(
    level=logging.DEBUG,
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

# ================= FLASK APP =================
app = Flask(__name__)

# ================= CACHE =================
_page_to_client_cache = {}
_chat_history_cache = {}

# ================= HELPER FUNCTIONS =================
def find_client_by_page_id(page_id):
    try:
        page_id_str = str(page_id)
        if page_id_str in _page_to_client_cache:
            return _page_to_client_cache[page_id_str]
        
        response = supabase.table("facebook_integrations") \
            .select("user_id") \
            .eq("page_id", page_id_str) \
            .eq("is_connected", True) \
            .execute()
        
        if response.data:
            admin_id = str(response.data[0]["user_id"])
            _page_to_client_cache[page_id_str] = admin_id
            return admin_id
        return None
    except Exception as e:
        logger.error(f"❌ Error finding client: {str(e)}")
        return None

def get_facebook_token(admin_id):
    response = supabase.table("facebook_integrations").select("page_access_token").eq("user_id", admin_id).eq("is_connected", True).execute()
    return response.data[0]["page_access_token"] if response.data else None

def get_gemini_key(admin_id):
    response = supabase.table("api_keys").select("gemini_api_key").eq("user_id", admin_id).execute()
    return response.data[0]["gemini_api_key"] if response.data else None

def load_chat_history(admin_id, customer_id):
    try:
        response = supabase.table("chat_history").select("messages").eq("user_id", admin_id).eq("customer_id", customer_id).execute()
        return response.data[0]["messages"] if response.data else []
    except: return []

def save_chat_history(admin_id, customer_id, history):
    try:
        supabase.table("chat_history").upsert({
            "user_id": admin_id,
            "customer_id": customer_id,
            "messages": history,
            "last_updated": datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        logger.error(f"❌ Error saving history: {str(e)}")

def send_facebook_message(page_token, customer_id, message_text):
    url = "https://graph.facebook.com/v18.0/me/messages"
    payload = {"recipient": {"id": customer_id}, "message": {"text": message_text}, "messaging_type": "RESPONSE"}
    requests.post(url, params={"access_token": page_token}, json=payload, timeout=30)

# ================= AI RESPONSE (SaaS FIXED) =================
def generate_ai_response(admin_id, user_message, customer_id):
    try:
        gemini_key = get_gemini_key(admin_id)
        if not gemini_key:
            return "⚠️ AI service is not configured."

        # গ্লোবাল কনফিগারেশনের বদলে প্রতিবার ক্লায়েন্ট ডিক্লেয়ার করা (Safe for SaaS)
        genai.configure(api_key=gemini_key)
        
        # মডেলের নাম 'models/gemini-1.5-flash' এর বদলে শুধু 'gemini-1.5-flash'
        model = genai.GenerativeModel("gemini-1.5-flash")

        history = load_chat_history(admin_id, customer_id)

        try:
            language = "bangla" if detect(user_message) == "bn" else "english"
        except:
            language = "english"

        system_prompt = f"You are {BOT_NAME}, a helpful AI assistant. Respond in {language}."
        context = ""
        for msg in history[-8:]:
            context += f"User: {msg.get('user','')}\nBot: {msg.get('bot','')}\n"

        prompt = f"{system_prompt}\n\n{context}User: {user_message}\nAssistant:"

        # এপিআই রিকোয়েস্ট
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.7,
                max_output_tokens=1000
            )
        )

        ai_response = response.text.strip() if response.text else "আমি দুঃখিত, আমি বুঝতে পারিনি।"

        # চ্যাট হিস্ট্রি আপডেট
        history.append({"user": user_message, "bot": ai_response, "timestamp": datetime.utcnow().isoformat()})
        save_chat_history(admin_id, customer_id, history[-20:])
        
        return ai_response

    except Exception as e:
        logger.error(f"❌ AI Error: {str(e)}")
        # যদি 404 আসে তবে লগ এ আরও ডিটেইল দেখাবে
        return "দুঃখিত, আমি এখন উত্তর দিতে পারছি পারছি না।"

# ================= ROUTES =================
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
                
                if not message_text: continue
                
                admin_id = find_client_by_page_id(recipient_id)
                if admin_id:
                    page_token = get_facebook_token(admin_id)
                    ai_response = generate_ai_response(admin_id, message_text, sender_id)
                    send_facebook_message(page_token, sender_id, ai_response)
                    
        return "EVENT_RECEIVED", 200
    except Exception:
        return "ERROR", 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

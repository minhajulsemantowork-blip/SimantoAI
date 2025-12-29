import os
import json
import requests
import logging
import traceback
from datetime import datetime
from flask import Flask, request
from google import genai # google-genai 1.52.0 এর জন্য
from langdetect import detect
from supabase import create_client, Client

# ================= LOGGING SETUP =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ================= CONFIG =================
BOT_NAME = "Simanto"

# ================= SUPABASE INIT =================
try:
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
    supabase: Client = create_client(supabase_url, supabase_key)
    logger.info("✅ Supabase initialized")
except Exception as e:
    logger.error(f"❌ Supabase Init Error: {str(e)}")
    supabase = None

# ================= FLASK APP =================
app = Flask(__name__)

# ================= CACHE =================
_page_to_admin_cache = {}

# ================= HELPER FUNCTIONS =================
def find_admin_by_page_id(page_id):
    """SaaS logic: Find which admin owns this Facebook page"""
    try:
        page_id_str = str(page_id)
        if page_id_str in _page_to_admin_cache:
            return _page_to_admin_cache[page_id_str]
        
        response = supabase.table("facebook_integrations")\
            .select("user_id")\
            .eq("page_id", page_id_str)\
            .eq("is_connected", True)\
            .execute()
        
        if response.data:
            admin_id = response.data[0]["user_id"]
            _page_to_admin_cache[page_id_str] = str(admin_id)
            return str(admin_id)
        return None
    except: return None

def get_facebook_token(admin_id):
    """Load specific FB token for this admin (user_id)"""
    response = supabase.table("facebook_integrations")\
        .select("page_access_token")\
        .eq("user_id", admin_id)\
        .eq("is_connected", True)\
        .execute()
    return response.data[0]["page_access_token"] if response.data else None

def get_gemini_key(admin_id):
    """Load specific Gemini key for this admin (user_id)"""
    response = supabase.table("api_keys")\
        .select("gemini_api_key")\
        .eq("user_id", admin_id)\
        .execute()
    return response.data[0]["gemini_api_key"] if response.data else None

def load_chat_history(admin_id, customer_id):
    """Load history using user_id and customer_id"""
    try:
        response = supabase.table("chat_history")\
            .select("messages")\
            .eq("user_id", admin_id)\
            .eq("customer_id", customer_id)\
            .execute()
        return response.data[0]["messages"] if response.data else []
    except: return []

def save_chat_history(admin_id, customer_id, history):
    """Save history using your admin user_id and customer_id"""
    try:
        chat_data = {
            "user_id": admin_id,
            "customer_id": customer_id,
            "messages": history,
            "last_updated": datetime.utcnow().isoformat()
        }
        supabase.table("chat_history").upsert(chat_data).execute()
    except Exception as e:
        logger.error(f"❌ DB Save Error: {str(e)}")

def send_facebook_message(page_token, customer_id, message_text):
    """Send message via FB Graph API"""
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={page_token}"
    payload = {"recipient": {"id": customer_id}, "message": {"text": message_text}}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"❌ FB Send Error: {str(e)}")

def generate_ai_response(admin_id, user_message, customer_id):
    """SaaS logic: Multi-tenant AI generation"""
    try:
        gemini_key = get_gemini_key(admin_id)
        if not gemini_key: return "⚠️ AI service not configured for this user."

        history = load_chat_history(admin_id, customer_id)
        
        # Language detection
        try: language = "bangla" if detect(user_message) == "bn" else "english"
        except: language = "english"

        # Initialize Client for each request (SaaS Safe)
        client = genai.Client(api_key=gemini_key)
        
        system_prompt = f"You are {BOT_NAME}, a helpful AI assistant. Respond in {language}."
        
        # Build context from history
        context = ""
        for msg in history[-10:]:
            context += f"User: {msg.get('user','')}\nBot: {msg.get('bot','')}\n"

        full_prompt = f"{system_prompt}\n\n{context}User: {user_message}\nAssistant:"
        
        # Generating content with correct naming for google-genai 1.x
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=full_prompt
        )
        
        ai_response = response.text.strip() if response.text else "I'm sorry, I couldn't process that."
        
        # Update and save history
        history.append({
            "user": user_message, 
            "bot": ai_response, 
            "timestamp": datetime.utcnow().isoformat()
        })
        save_chat_history(admin_id, customer_id, history[-30:])
        
        return ai_response
    except Exception as e:
        logger.error(f"❌ AI Error: {str(e)}")
        return "দুঃখিত, আমি এখন উত্তর দিতে পারছি না।"

# ================= WEBHOOK ROUTES =================
@app.route("/webhook", methods=["GET"])
def verify():
    # Facebook Webhook Verification
    challenge = request.args.get("hub.challenge")
    return challenge, 200

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    try:
        data = request.get_json()
        if data.get("object") == "page":
            for entry in data.get("entry", []):
                for event in entry.get("messaging", []):
                    sender_id = event.get("sender", {}).get("id")
                    recipient_id = event.get("recipient", {}).get("id")
                    msg_text = event.get("message", {}).get("text")
                    
                    if not msg_text: continue
                    
                    # ১. পেজ আইডি দিয়ে অ্যাডমিন খুঁজে বের করা
                    admin_id = find_admin_by_page_id(recipient_id)
                    if not admin_id: continue
                    
                    # ২. ওই অ্যাডমিনের ফেসবুক টোকেন এবং এপিআই কী দিয়ে প্রসেস করা
                    token = get_facebook_token(admin_id)
                    ai_res = generate_ai_response(admin_id, msg_text, sender_id)
                    
                    # ৩. রিপ্লাই পাঠানো
                    if token:
                        send_facebook_message(token, sender_id, ai_res)
                    
        return "OK", 200
    except Exception:
        return "Error", 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

import os
import json
import requests
import logging
import traceback
from datetime import datetime
from flask import Flask, request
import google.generativeai as genai
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
    
    if not supabase_url or not supabase_key:
        raise ValueError("Supabase configuration missing in environment variables")
    
    supabase: Client = create_client(supabase_url, supabase_key)
    logger.info("✅ Supabase initialized successfully")
except Exception as e:
    logger.error(f"❌ Failed to initialize Supabase: {str(e)}")
    supabase = None

# ================= FLASK APP =================
app = Flask(__name__)

# ================= CACHE =================
# শুধুমাত্র Page ID mappings ক্যাশ রাখছি পারফরম্যান্সের জন্য
# Chat History ক্যাশ করা বন্ধ করেছি RAM সেভ করার জন্য (SaaS Safe)
_page_to_client_cache = {}

# ================= HELPER FUNCTIONS =================
def find_client_by_page_id(page_id):
    try:
        page_id_str = str(page_id)
        if page_id_str in _page_to_client_cache:
            return _page_to_client_cache[page_id_str]

        if not supabase: return None
        
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
    try:
        response = supabase.table("facebook_integrations") \
            .select("page_access_token") \
            .eq("user_id", admin_id) \
            .eq("is_connected", True) \
            .execute()
        return response.data[0]["page_access_token"] if response.data else None
    except Exception as e:
        logger.error(f"❌ Error getting token: {str(e)}")
        return None

def get_gemini_key(admin_id):
    try:
        response = supabase.table("api_keys") \
            .select("gemini_api_key") \
            .eq("user_id", admin_id) \
            .execute()
        return response.data[0]["gemini_api_key"] if response.data else None
    except Exception as e:
        logger.error(f"❌ Error getting Gemini key: {str(e)}")
        return None

def load_chat_history(admin_id, customer_id):
    try:
        # SaaS প্রোডাকশনে সরাসরি DB থেকে লোড করা নিরাপদ (Memory Leak রোধ করতে)
        response = supabase.table("chat_history") \
            .select("messages") \
            .eq("user_id", admin_id) \
            .eq("customer_id", customer_id) \
            .execute()
        return response.data[0]["messages"] if response.data else []
    except Exception:
        return []

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
    try:
        url = "https://graph.facebook.com/v18.0/me/messages"
        params = {"access_token": page_token}
        payload = {
            "recipient": {"id": customer_id},
            "message": {"text": message_text},
            "messaging_type": "RESPONSE"
        }
        requests.post(url, params=params, json=payload, timeout=30)
    except Exception as e:
        logger.error(f"❌ Error sending FB message: {str(e)}")

# ================= AI RESPONSE (SaaS & THREAD SAFE) =================
def generate_ai_response(admin_id, user_message, customer_id):
    try:
        gemini_key = get_gemini_key(admin_id)
        if not gemini_key:
            return "⚠️ AI service is not configured."

        # Thread Safety: প্রত্যেক রিকোয়েস্টের জন্য আলাদা কনফিগারেশন
        genai.configure(api_key=gemini_key)
        # মডেল কল করার সময় 'models/' প্রিফিক্স ছাড়াই স্ট্রিং হিসেবে দেওয়া নিরাপদ
        model = genai.GenerativeModel("gemini-1.5-flash")

        history = load_chat_history(admin_id, customer_id)

        try:
            detected_lang = detect(user_message)
            language = "bangla" if detected_lang == "bn" else "english"
        except:
            language = "english"

        system_prompt = f"You are {BOT_NAME}, a helpful AI assistant. Respond in {language}."
        
        # কন্টেক্সট হিসেবে শেষ ৮টি মেসেজ (টোকেন সাশ্রয় ও গতির জন্য)
        context = ""
        for msg in history[-8:]:
            context += f"User: {msg.get('user','')}\nBot: {msg.get('bot','')}\n"

        prompt = f"{system_prompt}\n\n{context}User: {user_message}\nAssistant:"

        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.7,
                max_output_tokens=800
            )
        )

        ai_response = response.text.strip() if (response and response.text) else "I'm sorry, I couldn't process that."

        # হিস্ট্রি আপডেট ও সেভ (সর্বশেষ ১৫টি মেসেজ ডাটাবেসে রাখছি স্টোরেজ সেভ করতে)
        history.append({
            "user": user_message,
            "bot": ai_response,
            "timestamp": datetime.utcnow().isoformat()
        })
        save_chat_history(admin_id, customer_id, history[-15:])
        
        return ai_response

    except Exception as e:
        logger.error(f"❌ AI Error (Admin: {admin_id}): {str(e)}")
        # ৪‌০৪ এরর আসলে এখানে ডিটেইল লগ পাওয়া যাবে
        return "দুঃখিত, বর্তমানে আমি উত্তর দিতে পারছি না।"

# ================= ROUTES =================
@app.route("/", methods=["GET"])
def home():
    return json.dumps({"status": "online", "bot": BOT_NAME}), 200

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    if mode == "subscribe" and token:
        # DB থেকে ভেরিফিকেশন টোকেন চেক করা (Security)
        response = supabase.table("facebook_integrations") \
            .select("user_id") \
            .eq("verify_token", token) \
            .execute()
        if response.data:
            return challenge, 200
    return "Invalid", 403

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    try:
        data = request.get_json()
        if data.get("object") != "page": return "Not a page", 400
        
        for entry in data.get("entry", []):
            for event in entry.get("messaging", []):
                sender_id = event.get("sender", {}).get("id")
                recipient_id = event.get("recipient", {}).get("id") # দিস ইজ পেজ আইডি
                message_text = event.get("message", {}).get("text") or event.get("postback", {}).get("payload")
                
                if not message_text: continue
                
                # ১. পেজ আইডি দিয়ে অ্যাডমিন খুঁজে বের করা
                admin_id = find_client_by_page_id(recipient_id)
                if not admin_id: continue
                
                # ২. ফেসবুক টোকেন লোড করা
                page_token = get_facebook_token(admin_id)
                if not page_token: continue
                
                # ৩. AI রিপ্লাই তৈরি ও পাঠানো
                ai_response = generate_ai_response(admin_id, message_text, sender_id)
                send_facebook_message(page_token, sender_id, ai_response)
                
        return "EVENT_RECEIVED", 200
    except Exception as e:
        logger.error(f"❌ Webhook Error: {str(e)}")
        return "ERROR", 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

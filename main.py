import os
import json
import requests
import logging
import traceback
from datetime import datetime
from flask import Flask, request
from google import genai
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
    logger.info("Initializing Supabase...")
    
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
    
    if not supabase_url or not supabase_key:
        logger.error("SUPABASE_URL or SUPABASE_SERVICE_KEY not set!")
        raise ValueError("Supabase not configured")
    
    supabase: Client = create_client(supabase_url, supabase_key)
    
    # Test connection
    try:
        supabase.table("profiles").select("count").limit(1).execute()
        logger.info("✅ Supabase connection test successful")
    except:
        logger.warning("⚠️ Supabase connected but tables might not exist yet")
    
    logger.info("✅ Supabase initialized successfully")
    
except Exception as e:
    logger.error(f"❌ Failed to initialize Supabase: {str(e)}")
    logger.error(traceback.format_exc())
    supabase = None

# ================= FLASK APP =================
app = Flask(__name__)

# ================= CACHE =================
_page_to_client_cache = {}
_chat_history_cache = {}

# ================= HELPER FUNCTIONS =================
def find_client_by_page_id(page_id):
    """Find admin (user_id) by Facebook page ID"""
    try:
        page_id_str = str(page_id)
        logger.info(f"🔍 Searching for page ID: {page_id_str}")
        
        if page_id_str in _page_to_client_cache:
            return _page_to_client_cache[page_id_str]
        
        if not supabase:
            return None
        
        # 'user_id' is the admin's ID in your database
        response = supabase.table("facebook_integrations")\
            .select("user_id, page_name")\
            .eq("page_id", page_id_str)\
            .eq("is_connected", True)\
            .execute()
        
        if response.data and len(response.data) > 0:
            admin_id = response.data[0]["user_id"]
            _page_to_client_cache[page_id_str] = str(admin_id)
            return str(admin_id)
        
        return None
        
    except Exception as e:
        logger.error(f"❌ Error finding client: {str(e)}")
        return None

def get_facebook_token(admin_id):
    """Get Facebook page access token using admin's user_id"""
    try:
        if not supabase: return None
        
        response = supabase.table("facebook_integrations")\
            .select("page_access_token")\
            .eq("user_id", admin_id)\
            .eq("is_connected", True)\
            .execute()
        
        if response.data and len(response.data) > 0:
            return response.data[0]["page_access_token"]
        return None
    except Exception as e:
        logger.error(f"❌ Error getting token: {str(e)}")
        return None

def get_gemini_key(admin_id):
    """Get Gemini API key using admin's user_id"""
    try:
        if not supabase: return None
        
        response = supabase.table("api_keys")\
            .select("gemini_api_key")\
            .eq("user_id", admin_id)\
            .execute()
        
        if response.data and len(response.data) > 0:
            return response.data[0]["gemini_api_key"]
        return None
    except Exception as e:
        logger.error(f"❌ Error getting Gemini key: {str(e)}")
        return None

def load_chat_history(admin_id, customer_id):
    """Load chat history"""
    cache_key = f"{admin_id}_{customer_id}"
    if cache_key in _chat_history_cache:
        return _chat_history_cache[cache_key]
    
    try:
        if not supabase: return []
        
        # Querying with 'user_id' (admin) and 'customer_id' (FB user)
        response = supabase.table("chat_history")\
            .select("messages")\
            .eq("user_id", admin_id)\
            .eq("customer_id", customer_id)\
            .execute()
        
        if response.data and len(response.data) > 0:
            history = response.data[0]["messages"]
            _chat_history_cache[cache_key] = history
            return history
        return []
    except Exception:
        return []

def save_chat_history(admin_id, customer_id, history):
    """Save chat history to database"""
    try:
        if not supabase: return
        _chat_history_cache[f"{admin_id}_{customer_id}"] = history
        
        chat_data = {
            "user_id": admin_id,        # Your admin ID (matches other tables)
            "customer_id": customer_id,  # Facebook sender ID
            "messages": history,
            "last_updated": datetime.utcnow().isoformat()
        }
        # Upsert based on user_id and customer_id
        supabase.table("chat_history").upsert(chat_data).execute()
    except Exception as e:
        logger.error(f"❌ Error saving history: {str(e)}")

def send_facebook_message(page_token, customer_id, message_text):
    """Send message via Facebook Graph API"""
    try:
        if not page_token: return False
        url = "https://graph.facebook.com/v18.0/me/messages"
        payload = {
            "recipient": {"id": customer_id},
            "message": {"text": message_text},
            "messaging_type": "RESPONSE"
        }
        response = requests.post(url, params={"access_token": page_token}, json=payload, timeout=30)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"❌ Error sending FB message: {str(e)}")
        return False

def generate_ai_response(admin_id, user_message, customer_id):
    """Generate AI response using Gemini"""
    try:
        gemini_key = get_gemini_key(admin_id)
        if not gemini_key:
            return "⚠️ AI service is not configured for this page."

        history = load_chat_history(admin_id, customer_id)
        
        try:
            detected_lang = detect(user_message)
            language = "bangla" if detected_lang == "bn" else "english"
        except:
            language = "english"

        system_prompt = f"You are {BOT_NAME}, a helpful AI assistant. Respond in {language}."
        
        context = ""
        for msg in history[-10:]:
            context += f"User: {msg.get('user','')}\nBot: {msg.get('bot','')}\n"

        prompt = f"{system_prompt}\n\n{context}User: {user_message}\nAssistant:"
        
        client = genai.Client(api_key=gemini_key)
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
            config={"temperature": 0.7, "max_output_tokens": 1000}
        )
        
        ai_response = response.text.strip() or "I'm sorry, I couldn't process that."
        
        history.append({
            "user": user_message,
            "bot": ai_response,
            "timestamp": datetime.utcnow().isoformat(),
            "language": language
        })
        save_chat_history(admin_id, customer_id, history[-30:])
        return ai_response
    except Exception as e:
        logger.error(f"❌ AI Error: {str(e)}")
        return "দুঃখিত, আমি এখন উত্তর দিতে পারছি না।"

# ================= ROUTES =================
@app.route("/", methods=["GET"])
def home():
    return json.dumps({"status": "online", "bot": BOT_NAME}, indent=2)

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    if mode == "subscribe" and token:
        response = supabase.table("facebook_integrations")\
            .select("user_id")\
            .eq("verify_token", token)\
            .eq("is_connected", True)\
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
                recipient_id = event.get("recipient", {}).get("id")
                message_text = event.get("message", {}).get("text") or event.get("postback", {}).get("payload")
                
                if not message_text: continue
                
                # admin_id is the user_id from your DB
                admin_id = find_client_by_page_id(recipient_id)
                if not admin_id: continue
                
                page_token = get_facebook_token(admin_id)
                if not page_token: continue
                
                ai_response = generate_ai_response(admin_id, message_text, sender_id)
                send_facebook_message(page_token, sender_id, ai_response)
                
        return "EVENT_RECEIVED", 200
    except Exception:
        return "ERROR", 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

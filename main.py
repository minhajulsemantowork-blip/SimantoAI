import os
import json
import requests
import logging
from flask import Flask, request
from google import genai
from langdetect import detect
import firebase_admin
from firebase_admin import credentials, firestore

# ================= LOGGING SETUP =================
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ================= CONFIG =================
BOT_NAME = "Simanto"

# ================= FIREBASE INIT =================
try:
    FIREBASE_SERVICE_ACCOUNT = os.getenv("FIREBASE_SERVICE_ACCOUNT")
    logger.info(f"Firebase env var exists: {bool(FIREBASE_SERVICE_ACCOUNT)}")
    
    if FIREBASE_SERVICE_ACCOUNT:
        firebase_cred_json = json.loads(FIREBASE_SERVICE_ACCOUNT)
        cred = credentials.Certificate(firebase_cred_json)
        firebase_admin.initialize_app(cred)
        logger.info("✅ Firebase initialized successfully")
    else:
        logger.error("❌ FIREBASE_SERVICE_ACCOUNT environment variable not set")
        raise ValueError("Firebase service account not configured")
except Exception as e:
    logger.error(f"❌ Firebase init error: {str(e)}")
    raise

db = firestore.client()

# ================= FLASK APP =================
app = Flask(__name__)

# ================= HELPERS =================
def get_client_id_by_page(page_id):
    """Find client ID by Facebook page ID"""
    try:
        logger.info(f"🔍 Searching client for page ID: {page_id}")
        
        # Query with correct field path
        docs = db.collection("clients").where("integrations.facebook.pageId", "==", str(page_id)).limit(1).get()
        
        logger.info(f"📊 Found {len(docs)} matching documents")
        
        for doc in docs:
            logger.info(f"✅ Client found: {doc.id}")
            return doc.id
        
        logger.warning(f"❌ No client found for page ID: {page_id}")
        return None
        
    except Exception as e:
        logger.error(f"❌ Error in get_client_id_by_page: {str(e)}", exc_info=True)
        return None

def get_facebook_token(client_id):
    """Get Facebook page access token for client"""
    try:
        logger.info(f"🔑 Getting Facebook token for client: {client_id}")
        
        doc = db.collection("clients").document(client_id).get()
        
        if not doc.exists:
            logger.error(f"❌ Client document {client_id} does not exist")
            return None
        
        client_data = doc.to_dict()
        
        # Get token from integrations.facebook.pageAccessToken
        if 'integrations' in client_data and 'facebook' in client_data['integrations']:
            fb_data = client_data['integrations']['facebook']
            token = fb_data.get('pageAccessToken')
            
            if token:
                logger.info(f"✅ Found Facebook token (length: {len(token)})")
                return token
            else:
                logger.error(f"❌ No pageAccessToken in Facebook data")
        else:
            logger.error(f"❌ No Facebook integration found")
        
        return None
        
    except Exception as e:
        logger.error(f"❌ Error in get_facebook_token: {str(e)}", exc_info=True)
        return None

def get_gemini_key(client_id):
    """Get Gemini API key for client"""
    try:
        logger.info(f"🔐 Getting Gemini key for client: {client_id}")
        
        doc = db.collection("clients").document(client_id).get()
        if not doc.exists:
            logger.error(f"❌ Client document {client_id} does not exist")
            return None
        
        client_data = doc.to_dict()
        
        # Path: settings.apiKeys.geminiApiKey
        if 'settings' in client_data:
            settings = client_data['settings']
            
            if 'apiKeys' in settings:
                api_keys = settings['apiKeys']
                gemini_key = api_keys.get('geminiApiKey')
                
                if gemini_key:
                    logger.info(f"✅ Found Gemini API key")
                    return gemini_key
        
        logger.error(f"❌ No Gemini API key found for client {client_id}")
        return None
        
    except Exception as e:
        logger.error(f"❌ Error in get_gemini_key: {str(e)}")
        return None

def load_memory(client_id):
    """Load chat history from Firebase"""
    try:
        ref = db.collection("clients").document(client_id).collection("chat_history").document("history")
        doc = ref.get()
        
        if doc.exists:
            history = doc.to_dict().get("history", [])
            logger.info(f"✅ Loaded {len(history)} messages from history")
            return history
        else:
            logger.info(f"📝 No chat history found for client {client_id}")
            return []
    except Exception as e:
        logger.error(f"❌ Error loading memory: {str(e)}")
        return []

def save_memory(client_id, history):
    """Save chat history to Firebase"""
    try:
        ref = db.collection("clients").document(client_id).collection("chat_history").document("history")
        ref.set({"history": history})
        logger.info(f"💾 Saved {len(history)} messages to history")
    except Exception as e:
        logger.error(f"❌ Error saving memory: {str(e)}")

def send_facebook_message(page_token, sender_id, text):
    """Send message via Facebook Graph API"""
    try:
        if not page_token or not sender_id or not text:
            logger.error("Missing parameters for sending message")
            return False
        
        url = "https://graph.facebook.com/v18.0/me/messages"
        payload = {
            "messaging_type": "RESPONSE",
            "recipient": {"id": sender_id},
            "message": {"text": text},
        }
        params = {"access_token": page_token}
        
        logger.info(f"📨 Sending message to {sender_id}")
        logger.debug(f"Message: {text[:50]}...")
        
        r = requests.post(url, params=params, json=payload, timeout=10)
        
        logger.info(f"📨 FB Response Status: {r.status_code}")
        
        if r.status_code == 200:
            logger.info("✅ Message sent successfully")
            return True
        else:
            logger.error(f"❌ Failed to send message: {r.text}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Error in send_facebook_message: {str(e)}")
        return False

def chat_with_gemini(client_id, user_text):
    """Generate response using Gemini"""
    try:
        logger.info(f"🤖 Processing message: {user_text}")
        
        # Get history
        history = load_memory(client_id)
        
        # Detect language
        try:
            lang = detect(user_text)
            language = "bn" if lang.startswith("bn") else "en"
            logger.info(f"🌐 Detected language: {language}")
        except:
            language = "en"
        
        # Prepare conversation context
        convo = ""
        for h in history[-10:]:
            convo += f"User: {h['user']}\n{BOT_NAME}: {h['bot']}\n"
        convo += f"User: {user_text}\n{BOT_NAME}:"
        
        # Get Gemini API key
        gemini_key = get_gemini_key(client_id)
        if not gemini_key:
            error_msg = "⚠️ Gemini API key সেট করা নেই। দয়া অ্যাডমিনকে জানান।"
            logger.error(error_msg)
            return error_msg
        
        # Initialize Gemini client
        client = genai.Client(api_key=gemini_key)
        
        # Prepare prompt
        prompt = f"""
You are {BOT_NAME}, a friendly and persuasive sales assistant.
Always reply in {language} language.
Be helpful, polite and professional.

Previous conversation:
{convo}

Now reply to the user naturally:
"""
        
        # Generate response
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt
        )
        
        reply = response.text.strip()
        logger.info(f"🤖 Gemini reply: {reply}")
        
        # Save to history
        history.append({"user": user_text, "bot": reply})
        save_memory(client_id, history)
        
        return reply
        
    except Exception as e:
        logger.error(f"❌ Error in chat_with_gemini: {str(e)}", exc_info=True)
        return "দুঃখিত, কিছু সমস্যা হয়েছে। দয়া করে আবার চেষ্টা করুন।"

# ================= ROUTES =================
@app.route("/", methods=["GET"])
def home():
    logger.info("🏠 Home page accessed")
    return "🤖 Simanto AI Bot is running!"

# ---------- WEBHOOK VERIFY ----------
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    logger.info(f"🔧 Webhook verification - Mode: {mode}, Token: {token}")
    
    if mode == "subscribe" and token:
        try:
            clients = db.collection("clients").get()
            logger.info(f"📋 Total clients in DB: {len(clients)}")
            
            for c in clients:
                client_data = c.to_dict()
                fb = client_data.get("integrations", {}).get("facebook", {})
                stored_token = fb.get("verifyToken")
                
                if stored_token == token:
                    logger.info(f"✅ Webhook verified for client: {c.id}")
                    return challenge, 200
            
            logger.error("❌ No matching verify token found")
            return "Invalid verify token", 403
            
        except Exception as e:
            logger.error(f"❌ Error during verification: {str(e)}")
            return "Server error", 500
    
    return "Invalid request", 400

# ---------- WEBHOOK POST ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        logger.info("📩 Webhook POST received")
        
        data = request.get_json()
        if not data:
            logger.error("❌ No JSON data received")
            return "No data", 400
        
        logger.debug(f"Webhook data: {json.dumps(data, indent=2)}")
        
        # Verify it's a page event
        if data.get("object") != "page":
            logger.error("❌ Invalid object type")
            return "Not a page event", 400
        
        # Process each entry
        for entry in data.get("entry", []):
            for event in entry.get("messaging", []):
                sender_id = event.get("sender", {}).get("id")
                recipient_id = event.get("recipient", {}).get("id")
                message = event.get("message", {})
                user_text = message.get("text")
                
                # Check for postback
                if not user_text and event.get("postback"):
                    user_text = event.get("postback", {}).get("payload")
                
                logger.info(f"👤 Sender: {sender_id}")
                logger.info(f"📱 Page: {recipient_id}")
                logger.info(f"💬 Message: {user_text}")
                
                # Skip if no text
                if not user_text or not sender_id or not recipient_id:
                    logger.warning("⚠️ Skipping - missing data")
                    continue
                
                # Find client
                client_id = get_client_id_by_page(recipient_id)
                if not client_id:
                    logger.error(f"❌ No client found for page: {recipient_id}")
                    continue
                
                logger.info(f"✅ Client ID: {client_id}")
                
                # Get Facebook token
                page_token = get_facebook_token(client_id)
                if not page_token:
                    logger.error(f"❌ No Facebook token for client")
                    continue
                
                logger.info(f"✅ Got Facebook token")
                
                # Get AI response
                reply = chat_with_gemini(client_id, user_text)
                
                # Send response
                success = send_facebook_message(page_token, sender_id, reply)
                
                if success:
                    logger.info("✅ Message sent successfully")
                else:
                    logger.error("❌ Failed to send message")
        
        return "EVENT_RECEIVED", 200
        
    except Exception as e:
        logger.error(f"🔥 Unexpected error in webhook: {str(e)}", exc_info=True)
        return "ERROR", 500

# ================= DEBUG ROUTES =================
@app.route("/debug", methods=["GET"])
def debug_info():
    """Debug endpoint to check system status"""
    try:
        # Check Firebase
        clients_count = len(list(db.collection("clients").limit(10).get()))
        
        return json.dumps({
            "status": "running",
            "bot_name": BOT_NAME,
            "firebase": {
                "connected": True,
                "clients_count": clients_count
            },
            "endpoints": {
                "webhook_verify": "/webhook [GET]",
                "webhook_receive": "/webhook [POST]",
                "home": "/ [GET]",
                "debug": "/debug [GET]",
                "check_client": "/check-client/<page_id> [GET]"
            }
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2), 500

@app.route("/check-client/<page_id>", methods=["GET"])
def check_client(page_id):
    """Check client data for a page"""
    try:
        client_id = get_client_id_by_page(page_id)
        
        if not client_id:
            return json.dumps({
                "success": False,
                "message": f"No client found for page: {page_id}"
            }, indent=2)
        
        # Get client data
        doc = db.collection("clients").document(client_id).get()
        client_data = doc.to_dict()
        
        # Extract relevant info
        result = {
            "success": True,
            "page_id": page_id,
            "client_id": client_id,
            "has_facebook_token": bool(get_facebook_token(client_id)),
            "has_gemini_key": bool(get_gemini_key(client_id)),
            "data_structure": {
                "keys": list(client_data.keys())
            }
        }
        
        # Add Facebook data if exists
        if 'integrations' in client_data and 'facebook' in client_data['integrations']:
            fb_data = client_data['integrations']['facebook']
            result["facebook"] = {
                "pageName": fb_data.get('pageName'),
                "has_pageAccessToken": 'pageAccessToken' in fb_data,
                "keys": list(fb_data.keys())
            }
        
        # Add settings data if exists
        if 'settings' in client_data:
            result["settings"] = {
                "keys": list(client_data['settings'].keys())
            }
        
        return json.dumps(result, indent=2, default=str)
        
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2), 500

# ================= RUN =================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"🚀 Starting {BOT_NAME} AI Bot on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

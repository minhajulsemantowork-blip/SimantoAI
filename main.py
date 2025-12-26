import os
import json
import requests
from datetime import datetime
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
        # Parse the JSON string
        firebase_cred_json = json.loads(FIREBASE_SERVICE_ACCOUNT)
        cred = credentials.Certificate(firebase_cred_json)
        firebase_admin.initialize_app(cred)
        logger.info("✅ Firebase initialized successfully")
    else:
        logger.error("❌ FIREBASE_SERVICE_ACCOUNT environment variable not set")
        # Fallback for local testing
        cred = credentials.Certificate("service-account.json")
        firebase_admin.initialize_app(cred)
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
        
        # Try different field paths
        docs = db.collection("clients").where("integrations.facebook.pageId", "==", page_id).limit(1).get()
        
        if not docs:
            # Try alternative path
            docs = db.collection("clients").where("facebookPageId", "==", page_id).limit(1).get()
        
        if not docs:
            # Try with integer conversion
            try:
                page_id_int = int(page_id)
                docs = db.collection("clients").where("integrations.facebook.pageId", "==", page_id_int).limit(1).get()
            except:
                pass
        
        logger.info(f"📊 Found {len(docs)} matching documents")
        
        for doc in docs:
            client_data = doc.to_dict()
            logger.info(f"✅ Client found: {doc.id}")
            logger.debug(f"Client data: {json.dumps(client_data, indent=2, default=str)}")
            return doc.id
        
        logger.warning(f"❌ No client found for page ID: {page_id}")
        
        # List all clients for debugging
        all_clients = db.collection("clients").limit(5).get()
        logger.info(f"📋 First 5 clients in database:")
        for client in all_clients:
            client_data = client.to_dict()
            logger.info(f"  Client ID: {client.id}")
            logger.info(f"  Facebook data: {client_data.get('integrations', {}).get('facebook', {})}")
        
        return None
        
    except Exception as e:
        logger.error(f"❌ Error in get_client_id_by_page: {str(e)}", exc_info=True)
        return None


def get_facebook_token(client_id):
    """Get Facebook page access token for client"""
    try:
        if not client_id:
            logger.error("No client ID provided")
            return None
            
        doc_ref = db.collection("clients").document(client_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            logger.error(f"Client document {client_id} does not exist")
            return None
        
        client_data = doc.to_dict()
        logger.debug(f"Client data for token: {json.dumps(client_data, indent=2, default=str)}")
        
        # Try different possible paths
        token = None
        
        # Path 1: integrations.facebook.pageAccessToken
        if 'integrations' in client_data and 'facebook' in client_data['integrations']:
            token = client_data['integrations']['facebook'].get('pageAccessToken')
        
        # Path 2: facebook.pageAccessToken
        if not token and 'facebook' in client_data:
            token = client_data['facebook'].get('pageAccessToken')
        
        # Path 3: direct field
        if not token and 'pageAccessToken' in client_data:
            token = client_data['pageAccessToken']
        
        if token:
            logger.info(f"✅ Found Facebook token for client {client_id}")
            logger.debug(f"Token (first 10 chars): {token[:10]}...")
        else:
            logger.error(f"❌ No Facebook token found for client {client_id}")
            
        return token
        
    except Exception as e:
        logger.error(f"❌ Error in get_facebook_token: {str(e)}", exc_info=True)
        return None


def get_gemini_key(client_id):
    """Get Gemini API key for client"""
    try:
        doc = db.collection("clients").document(client_id).get()
        if not doc.exists:
            return None
        
        client_data = doc.to_dict()
        
        # Try different possible paths
        gemini_key = None
        
        # Path 1: settings.apiKeys.geminiApiKey
        if 'settings' in client_data and 'apiKeys' in client_data['settings']:
            gemini_key = client_data['settings']['apiKeys'].get('geminiApiKey')
        
        # Path 2: direct field
        if not gemini_key and 'geminiApiKey' in client_data:
            gemini_key = client_data['geminiApiKey']
        
        if gemini_key:
            logger.info(f"✅ Found Gemini key for client {client_id}")
            logger.debug(f"Gemini key (first 10 chars): {gemini_key[:10]}...")
        else:
            logger.warning(f"⚠️ No Gemini key found for client {client_id}")
            
        return gemini_key
        
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
        logger.debug(f"Message text: {text}")
        logger.debug(f"Token (first 10 chars): {page_token[:10]}...")
        
        r = requests.post(url, params=params, json=payload, timeout=10)
        
        logger.info(f"📨 FB Response Status: {r.status_code}")
        logger.debug(f"FB Response Text: {r.text}")
        
        if r.status_code != 200:
            logger.error(f"❌ Failed to send message: {r.text}")
            return False
            
        logger.info("✅ Message sent successfully")
        return True
        
    except Exception as e:
        logger.error(f"❌ Error in send_facebook_message: {str(e)}", exc_info=True)
        return False


def chat_with_gemini(client_id, user_text):
    """Generate response using Gemini"""
    try:
        logger.info(f"🤖 Processing message for client {client_id}: {user_text}")
        
        # Get history
        history = load_memory(client_id)
        
        # Detect language
        try:
            lang = detect(user_text)
            language = "bn" if lang.startswith("bn") else "en"
            logger.info(f"🌐 Detected language: {language}")
        except:
            language = "en"
            logger.warning("⚠️ Could not detect language, defaulting to English")
        
        # Prepare conversation context
        convo = ""
        for h in history[-10:]:  # Last 10 messages
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
        
        logger.debug(f"📝 Prompt sent to Gemini: {prompt}")
        
        # Generate response
        response = client.models.generate_content(
            model="gemini-2.0-flash-exp",
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
    
    logger.info(f"🔧 Webhook verification attempt - Mode: {mode}, Token: {token}")
    
    if mode == "subscribe" and token:
        try:
            clients = db.collection("clients").get()
            logger.info(f"📋 Total clients in DB: {len(clients)}")
            
            for c in clients:
                client_data = c.to_dict()
                fb = client_data.get("integrations", {}).get("facebook", {})
                stored_token = fb.get("verifyToken")
                logger.info(f"Client {c.id}: verifyToken = {stored_token}")
                
                if stored_token == token:
                    logger.info(f"✅ Webhook verified for client: {c.id}")
                    return challenge, 200
            
            logger.error("❌ No matching verify token found")
            return "Invalid verify token", 403
            
        except Exception as e:
            logger.error(f"❌ Error during verification: {str(e)}", exc_info=True)
            return "Server error", 500
    
    return "Invalid request", 400


# ---------- WEBHOOK POST ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        logger.info("📩 Webhook POST received")
        
        # Get the JSON data
        data = request.get_json()
        if not data:
            logger.error("❌ No JSON data received in webhook")
            return "No data", 400
        
        logger.info("===== RAW WEBHOOK DATA =====")
        logger.info(json.dumps(data, indent=2))
        
        # Verify it's a page event
        if data.get("object") != "page":
            logger.error("❌ Invalid object type, expected 'page'")
            return "Not a page event", 400
        
        # Process each entry
        for entry in data.get("entry", []):
            page_id = entry.get("id")
            logger.info(f"📄 Processing entry for Page ID: {page_id}")
            
            # Process messaging events
            for event in entry.get("messaging", []):
                # Extract event data
                sender_id = event.get("sender", {}).get("id")
                recipient_id = event.get("recipient", {}).get("id")
                timestamp = event.get("timestamp")
                
                logger.info(f"🕒 Event timestamp: {timestamp}")
                logger.info(f"👤 Sender ID: {sender_id}")
                logger.info(f"📱 Recipient ID: {recipient_id}")
                
                # Skip delivery/read receipts
                if event.get("delivery") or event.get("read"):
                    logger.info("📨 Delivery/Read receipt, skipping")
                    continue
                
                # Check for message
                message = event.get("message", {})
                user_text = message.get("text")
                
                # Check for postback (button clicks)
                if not user_text and event.get("postback"):
                    payload = event.get("postback", {}).get("payload")
                    logger.info(f"🔄 Postback received: {payload}")
                    user_text = payload
                
                # Check for quick reply
                if not user_text and message.get("quick_reply"):
                    user_text = message.get("quick_reply", {}).get("payload")
                
                logger.info(f"💬 User message: {user_text}")
                
                # Validate required data
                if not sender_id or not recipient_id:
                    logger.error("⛔ Missing sender or recipient ID")
                    continue
                
                if not user_text:
                    logger.warning("⚠️ No text message or payload, skipping")
                    continue
                
                # Find client by page ID
                client_id = get_client_id_by_page(recipient_id)
                if not client_id:
                    logger.error(f"❌ No client found for page: {recipient_id}")
                    # Send a fallback message if possible
                    continue
                
                # Get Facebook page token
                page_token = get_facebook_token(client_id)
                if not page_token:
                    logger.error(f"❌ No Facebook token for client: {client_id}")
                    continue
                
                # Generate AI response
                reply = chat_with_gemini(client_id, user_text)
                
                # Send response back to Facebook
                success = send_facebook_message(page_token, sender_id, reply)
                
                if success:
                    logger.info("✅ Message processing completed successfully")
                else:
                    logger.error("❌ Failed to send message to Facebook")
        
        logger.info("✅ Webhook processed successfully")
        return "EVENT_RECEIVED", 200
        
    except Exception as e:
        logger.error(f"🔥 Unexpected error in webhook: {str(e)}", exc_info=True)
        return "ERROR", 500


# ================= DEBUG ROUTES =================
@app.route("/debug/clients", methods=["GET"])
def debug_clients():
    """Debug endpoint to list all clients"""
    try:
        clients = db.collection("clients").limit(20).get()
        result = []
        
        for client in clients:
            client_data = client.to_dict()
            result.append({
                "id": client.id,
                "name": client_data.get("name", "No Name"),
                "facebook": client_data.get("integrations", {}).get("facebook", {}),
                "has_gemini_key": bool(get_gemini_key(client.id)),
                "has_facebook_token": bool(get_facebook_token(client.id))
            })
        
        return json.dumps({"clients": result}, indent=2, default=str)
        
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2), 500


@app.route("/test/message", methods=["POST"])
def test_message():
    """Test endpoint to send a message"""
    try:
        data = request.get_json()
        client_id = data.get("client_id")
        user_text = data.get("message")
        
        if not client_id or not user_text:
            return json.dumps({"error": "Missing client_id or message"}), 400
        
        reply = chat_with_gemini(client_id, user_text)
        
        return json.dumps({
            "success": True,
            "client_id": client_id,
            "user_message": user_text,
            "ai_reply": reply
        }, indent=2)
        
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2), 500


# ================= RUN =================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"🚀 Starting Simanto AI Bot on port {port}")
    logger.info(f"🤖 Bot Name: {BOT_NAME}")
    app.run(host="0.0.0.0", port=port, debug=True)

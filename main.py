import os
import json
import requests
import logging
from datetime import datetime
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
    logger.info("Initializing Firebase...")
    
    service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT")
    
    if not service_account_json:
        logger.error("FIREBASE_SERVICE_ACCOUNT environment variable is not set!")
        raise ValueError("Firebase service account not configured")
    
    service_account = json.loads(service_account_json)
    cred = credentials.Certificate(service_account)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    
    logger.info("✅ Firebase initialized successfully")
    
except Exception as e:
    logger.error(f"❌ Failed to initialize Firebase: {str(e)}")
    db = None

# ================= FLASK APP =================
app = Flask(__name__)

# ================= CACHE =================
_client_cache = {}
_page_to_client_cache = {}

# ================= HELPER FUNCTIONS =================
def get_all_clients():
    """Get all clients from Firebase"""
    if not db:
        return []
    
    try:
        clients_ref = db.collection("clients")
        docs = clients_ref.stream()
        
        clients = []
        for doc in docs:
            client_data = doc.to_dict()
            clients.append({
                'id': doc.id,
                'data': client_data
            })
        
        logger.info(f"📊 Loaded {len(clients)} clients from database")
        return clients
        
    except Exception as e:
        logger.error(f"❌ Error getting clients: {str(e)}")
        return []

def find_client_by_page_id(page_id):
    """Find client by Facebook page ID - Pure Multi-Tenant"""
    try:
        page_id_str = str(page_id)
        
        # Check cache first
        if page_id_str in _page_to_client_cache:
            return _page_to_client_cache[page_id_str]
        
        # Get all clients
        all_clients = get_all_clients()
        
        for client in all_clients:
            client_data = client['data']
            
            # Check if client has Facebook integration
            if 'integrations' in client_data and 'facebook' in client_data['integrations']:
                fb_data = client_data['integrations']['facebook']
                
                if 'pageId' in fb_data:
                    db_page_id = str(fb_data['pageId'])
                    
                    if db_page_id == page_id_str:
                        # Cache the result
                        _page_to_client_cache[page_id_str] = client['id']
                        _client_cache[client['id']] = client_data
                        
                        logger.info(f"✅ Client found: {client['id']} for page {page_id_str}")
                        return client['id']
        
        logger.error(f"❌ No client found for page: {page_id_str}")
        return None
        
    except Exception as e:
        logger.error(f"❌ Error finding client: {str(e)}")
        return None

def get_client_data(client_id):
    """Get client data with caching"""
    if client_id in _client_cache:
        return _client_cache[client_id]
    
    if not db:
        return None
    
    try:
        doc_ref = db.collection("clients").document(client_id)
        doc = doc_ref.get()
        
        if doc.exists:
            data = doc.to_dict()
            _client_cache[client_id] = data
            return data
        return None
            
    except Exception as e:
        logger.error(f"❌ Error getting client data: {str(e)}")
        return None

def get_facebook_token(client_id):
    """Get Facebook page access token"""
    client_data = get_client_data(client_id)
    
    if not client_data:
        return None
    
    if 'integrations' in client_data and 'facebook' in client_data['integrations']:
        fb_data = client_data['integrations']['facebook']
        token = fb_data.get('pageAccessToken')
        
        if token:
            logger.info(f"✅ Facebook token found for {client_id}")
            return token
    
    logger.error(f"❌ No Facebook token for {client_id}")
    return None

def get_gemini_key(client_id):
    """Get Gemini API key"""
    client_data = get_client_data(client_id)
    
    if not client_data:
        return None
    
    gemini_key = None
    
    # Try multiple locations
    if 'settings' in client_data and 'apiKeys' in client_data['settings']:
        api_keys = client_data['settings']['apiKeys']
        gemini_key = api_keys.get('geminiApiKey')
    
    if not gemini_key and 'settings' in client_data:
        gemini_key = client_data['settings'].get('geminiApiKey')
    
    if not gemini_key and 'geminiApiKey' in client_data:
        gemini_key = client_data['geminiApiKey']
    
    if gemini_key:
        logger.info(f"✅ Gemini key found for {client_id}")
        return gemini_key
    
    logger.error(f"❌ No Gemini key for {client_id}")
    return None

def load_chat_history(client_id, user_id):
    """Load chat history"""
    if not db:
        return []
    
    try:
        doc_ref = db.collection("clients").document(client_id)\
            .collection("chat_history").document(user_id)
        
        doc = doc_ref.get()
        
        if doc.exists:
            history = doc.to_dict().get("messages", [])
            return history
        return []
        
    except Exception as e:
        logger.error(f"❌ Error loading chat history: {str(e)}")
        return []

def save_chat_history(client_id, user_id, history):
    """Save chat history"""
    if not db:
        return
    
    try:
        doc_ref = db.collection("clients").document(client_id)\
            .collection("chat_history").document(user_id)
        
        doc_ref.set({
            "messages": history,
            "last_updated": datetime.utcnow().isoformat()
        }, merge=True)
        
    except Exception as e:
        logger.error(f"❌ Error saving chat history: {str(e)}")

def send_facebook_message(page_token, user_id, message_text):
    """Send message via Facebook Graph API"""
    try:
        if not page_token:
            return False
        
        url = "https://graph.facebook.com/v18.0/me/messages"
        
        payload = {
            "recipient": {"id": user_id},
            "message": {"text": message_text},
            "messaging_type": "RESPONSE"
        }
        
        params = {"access_token": page_token}
        
        response = requests.post(url, params=params, json=payload, timeout=10)
        
        if response.status_code == 200:
            logger.info(f"✅ Message sent to {user_id[:10]}...")
            return True
        else:
            logger.error(f"❌ Failed to send: {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Error sending message: {str(e)}")
        return False

def generate_ai_response(client_id, user_message, user_id):
    """Generate AI response using Gemini"""
    try:
        gemini_key = get_gemini_key(client_id)
        
        if not gemini_key:
            return "⚠️ AI service is not configured. Please contact administrator."
        
        history = load_chat_history(client_id, user_id)
        
        try:
            detected_lang = detect(user_message)
            language = "bangla" if detected_lang == "bn" else "english"
        except:
            language = "english"
        
        context = ""
        for msg in history[-6:]:
            if 'user' in msg and 'bot' in msg:
                context += f"User: {msg['user']}\nBot: {msg['bot']}\n"
        
        client = genai.Client(api_key=gemini_key)
        
        prompt = f"""You are {BOT_NAME}, a helpful AI assistant.
Respond in {language} language.
Be friendly and helpful.

Previous conversation:
{context}
User: {user_message}

Assistant:"""
        
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt
        )
        
        ai_response = response.text.strip()
        
        history.append({
            "user": user_message,
            "bot": ai_response,
            "timestamp": datetime.utcnow().isoformat()
        })
        
        if len(history) > 20:
            history = history[-20:]
        
        save_chat_history(client_id, user_id, history)
        
        return ai_response
        
    except Exception as e:
        logger.error(f"❌ Error generating AI response: {str(e)}")
        return "Sorry, I'm having trouble. Please try again."

# ================= ROUTES =================
@app.route("/", methods=["GET"])
def home():
    """Home page"""
    return json.dumps({
        "status": "online",
        "service": "Simanto AI - Multi-Tenant Messenger Bot",
        "timestamp": datetime.utcnow().isoformat()
    }, indent=2)

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Facebook webhook verification"""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    if mode == "subscribe" and token:
        try:
            all_clients = get_all_clients()
            
            for client in all_clients:
                client_data = client['data']
                
                if 'integrations' in client_data and 'facebook' in client_data['integrations']:
                    fb_data = client_data['integrations']['facebook']
                    verify_token = fb_data.get('verifyToken')
                    
                    if verify_token == token:
                        logger.info(f"✅ Webhook verified for client: {client['id']}")
                        return challenge, 200
            
            logger.error("❌ No matching verify token")
            return "Invalid verify token", 403
            
        except Exception as e:
            logger.error(f"❌ Verification error: {str(e)}")
            return "Server error", 500
    
    return "Invalid request", 400

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    """Handle incoming Facebook messages"""
    try:
        data = request.get_json()
        
        if not data:
            return "No data", 400
        
        if data.get("object") != "page":
            return "Not a page event", 400
        
        for entry in data.get("entry", []):
            for event in entry.get("messaging", []):
                sender_id = event.get("sender", {}).get("id")
                recipient_id = event.get("recipient", {}).get("id")
                
                message_text = None
                if "message" in event:
                    message_text = event["message"].get("text")
                elif "postback" in event:
                    message_text = event["postback"].get("payload")
                
                if not message_text or not sender_id or not recipient_id:
                    continue
                
                logger.info(f"💬 Message from {sender_id[:10]}... to page {recipient_id}")
                
                # MULTI-TENANT: Find client dynamically
                client_id = find_client_by_page_id(recipient_id)
                
                if not client_id:
                    logger.error(f"❌ No client for page {recipient_id}")
                    continue
                
                logger.info(f"✅ Client: {client_id}")
                
                page_token = get_facebook_token(client_id)
                
                if not page_token:
                    logger.error(f"❌ No Facebook token for {client_id}")
                    continue
                
                ai_response = generate_ai_response(client_id, message_text, sender_id)
                
                send_facebook_message(page_token, sender_id, ai_response)
        
        return "EVENT_RECEIVED", 200
        
    except Exception as e:
        logger.error(f"🔥 Webhook error: {str(e)}")
        return "ERROR", 500

# ================= ADMIN/MONITORING ROUTES =================
@app.route("/api/status", methods=["GET"])
def system_status():
    """System status (Multi-Tenant)"""
    all_clients = get_all_clients()
    
    return json.dumps({
        "status": "online",
        "total_clients": len(all_clients),
        "cache_size": len(_client_cache),
        "timestamp": datetime.utcnow().isoformat()
    }, indent=2)

@app.route("/api/clients", methods=["GET"])
def list_clients():
    """List all clients (Multi-Tenant)"""
    try:
        all_clients = get_all_clients()
        
        clients_list = []
        for client in all_clients:
            client_id = client['id']
            client_data = client['data']
            
            client_info = {
                "id": client_id,
                "has_facebook": False,
                "has_gemini": False,
                "page_id": None,
                "page_name": None
            }
            
            if 'integrations' in client_data and 'facebook' in client_data['integrations']:
                fb_data = client_data['integrations']['facebook']
                client_info['has_facebook'] = True
                client_info['page_id'] = fb_data.get('pageId')
                client_info['page_name'] = fb_data.get('pageName')
            
            if get_gemini_key(client_id):
                client_info['has_gemini'] = True
            
            clients_list.append(client_info)
        
        return json.dumps({
            "total_clients": len(clients_list),
            "clients": clients_list
        }, indent=2)
        
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2), 500

@app.route("/api/client/<client_id>", methods=["GET"])
def get_client_info(client_id):
    """Get specific client info (Multi-Tenant)"""
    try:
        client_data = get_client_data(client_id)
        
        if not client_data:
            return json.dumps({"error": "Client not found"}, indent=2), 404
        
        response = {
            "client_id": client_id,
            "has_facebook_token": get_facebook_token(client_id) is not None,
            "has_gemini_key": get_gemini_key(client_id) is not None,
            "keys": list(client_data.keys())
        }
        
        if 'integrations' in client_data and 'facebook' in client_data['integrations']:
            fb_data = client_data['integrations']['facebook']
            response["facebook"] = {
                "page_id": fb_data.get('pageId'),
                "page_name": fb_data.get('pageName')
            }
        
        return json.dumps(response, indent=2, default=str)
        
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2), 500

@app.route("/api/search/<page_id>", methods=["GET"])
def search_client_by_page(page_id):
    """Search client by page ID (Multi-Tenant)"""
    client_id = find_client_by_page_id(page_id)
    
    if client_id:
        return json.dumps({
            "found": True,
            "page_id": page_id,
            "client_id": client_id
        }, indent=2)
    else:
        return json.dumps({
            "found": False,
            "page_id": page_id,
            "message": "No client found with this page ID"
        }, indent=2), 404

@app.route("/api/test-firebase", methods=["GET"])
def test_firebase_connection():
    """Test Firebase connection"""
    try:
        if not db:
            return {"error": "Firebase not initialized"}, 500
        
        # Count total clients
        clients = list(db.collection("clients").limit(5).get())
        
        # Try specific client
        specific_doc = db.collection("clients").document("e6L1ttGY8pPundmDoZb2gqgjJaw2").get()
        
        return {
            "firebase_connected": True,
            "total_clients": len(clients),
            "specific_client_exists": specific_doc.exists,
            "clients_collection_exists": len(clients) > 0
        }
            
    except Exception as e:
        return {"error": str(e), "type": type(e).__name__}, 500

# ================= APPLICATION START =================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    
    logger.info("=" * 50)
    logger.info(f"🚀 {BOT_NAME} AI - Multi-Tenant Messenger Bot")
    logger.info(f"🌐 Port: {port}")
    logger.info(f"🔧 Firebase: {'✅ Connected' if db else '❌ Disconnected'}")
    logger.info("=" * 50)
    
    app.run(host="0.0.0.0", port=port, debug=False)

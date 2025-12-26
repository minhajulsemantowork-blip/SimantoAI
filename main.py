import os
import json
import requests
import logging
import traceback
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
    logger.error(traceback.format_exc())
    db = None

# ================= FLASK APP =================
app = Flask(__name__)

# ================= CACHE =================
_client_cache = {}
_page_to_client_cache = {}

# ================= HELPER FUNCTIONS =================
def get_all_clients():
    """Get all clients from Firebase - FIXED VERSION"""
    if not db:
        logger.error("Firebase not initialized")
        return []
    
    try:
        # FIX: Use get() instead of stream() for better reliability
        docs = db.collection("clients").get()
        
        clients = []
        for doc in docs:
            client_data = doc.to_dict()
            clients.append({
                'id': doc.id,
                'data': client_data
            })
        
        logger.info(f"📊 Loaded {len(clients)} clients from database")
        
        # DEBUG: Show first few clients
        for i, client in enumerate(clients[:3]):
            logger.info(f"  Client {i+1}: {client['id']}")
            logger.info(f"    Keys: {list(client['data'].keys())}")
            
            if 'integrations' in client['data']:
                logger.info(f"    Has integrations: Yes")
                if 'facebook' in client['data']['integrations']:
                    fb_data = client['data']['integrations']['facebook']
                    logger.info(f"    Facebook keys: {list(fb_data.keys())}")
                    if 'pageId' in fb_data:
                        logger.info(f"    pageId: {fb_data['pageId']}")
        
        return clients
        
    except Exception as e:
        logger.error(f"❌ Error getting clients: {str(e)}")
        logger.error(traceback.format_exc())
        return []

def find_client_by_page_id(page_id):
    """Find client by Facebook page ID - WITH DETAILED DEBUG"""
    try:
        page_id_str = str(page_id)
        logger.info(f"🔍 Searching for page ID: {page_id_str}")
        
        # Check cache first
        if page_id_str in _page_to_client_cache:
            logger.info(f"📦 Cache hit for page {page_id_str}")
            return _page_to_client_cache[page_id_str]
        
        # Get all clients
        all_clients = get_all_clients()
        
        logger.info(f"🔎 Scanning {len(all_clients)} clients for page {page_id_str}")
        
        if not all_clients:
            logger.error("❌ No clients found in database")
            return None
        
        for client in all_clients:
            client_id = client['id']
            client_data = client['data']
            
            logger.debug(f"  Checking client: {client_id}")
            
            # Check if client has Facebook integration
            if 'integrations' not in client_data:
                logger.debug(f"    No integrations found")
                continue
            
            integrations = client_data['integrations']
            
            if 'facebook' not in integrations:
                logger.debug(f"    No facebook integration")
                continue
            
            fb_data = integrations['facebook']
            
            # Check for pageId
            if 'pageId' not in fb_data:
                logger.debug(f"    No pageId in facebook data")
                continue
            
            db_page_id = fb_data['pageId']
            db_page_id_str = str(db_page_id)
            
            logger.info(f"    Client {client_id} has pageId: {db_page_id_str}")
            logger.info(f"    Types - Search: {type(page_id_str).__name__}, DB: {type(db_page_id).__name__}")
            
            if db_page_id_str == page_id_str:
                logger.info(f"✅ MATCH FOUND! Client {client_id} for page {page_id_str}")
                
                # Cache the result
                _page_to_client_cache[page_id_str] = client_id
                _client_cache[client_id] = client_data
                
                return client_id
        
        logger.error(f"❌ No client found for page: {page_id_str}")
        
        # Show all pageIds found for debugging
        logger.info("📋 All page IDs found in database:")
        page_ids_found = []
        for client in all_clients:
            client_data = client['data']
            if 'integrations' in client_data and 'facebook' in client_data['integrations']:
                fb_data = client_data['integrations']['facebook']
                if 'pageId' in fb_data:
                    page_ids_found.append(f"{client['id']}: {fb_data['pageId']}")
        
        if page_ids_found:
            for item in page_ids_found:
                logger.info(f"  {item}")
        else:
            logger.info("  No pageIds found in any client")
        
        return None
        
    except Exception as e:
        logger.error(f"❌ Error finding client: {str(e)}")
        logger.error(traceback.format_exc())
        return None

def get_client_data(client_id):
    """Get client data with caching"""
    if client_id in _client_cache:
        return _client_cache[client_id]
    
    if not db:
        logger.error("Firebase not initialized")
        return None
    
    try:
        doc_ref = db.collection("clients").document(client_id)
        doc = doc_ref.get()
        
        if doc.exists:
            data = doc.to_dict()
            _client_cache[client_id] = data
            logger.debug(f"📄 Loaded client data for {client_id}")
            return data
        else:
            logger.error(f"❌ Client document {client_id} does not exist")
            return None
            
    except Exception as e:
        logger.error(f"❌ Error getting client data: {str(e)}")
        return None

def get_facebook_token(client_id):
    """Get Facebook page access token"""
    client_data = get_client_data(client_id)
    
    if not client_data:
        logger.error(f"❌ No client data for {client_id}")
        return None
    
    if 'integrations' in client_data and 'facebook' in client_data['integrations']:
        fb_data = client_data['integrations']['facebook']
        token = fb_data.get('pageAccessToken')
        
        if token:
            # Show token preview (first and last 10 chars)
            token_preview = f"{token[:15]}...{token[-10:]}" if len(token) > 25 else token
            logger.info(f"✅ Facebook token found for {client_id}: {token_preview}")
            return token
        else:
            logger.error(f"❌ No pageAccessToken in facebook data for {client_id}")
    else:
        logger.error(f"❌ No facebook integration for {client_id}")
    
    return None

def get_gemini_key(client_id):
    """Get Gemini API key"""
    client_data = get_client_data(client_id)
    
    if not client_data:
        logger.error(f"❌ No client data for {client_id}")
        return None
    
    gemini_key = None
    
    # Try multiple locations
    if 'settings' in client_data and 'apiKeys' in client_data['settings']:
        api_keys = client_data['settings']['apiKeys']
        gemini_key = api_keys.get('geminiApiKey')
        if gemini_key:
            logger.info(f"✅ Found Gemini key in settings.apiKeys for {client_id}")
    
    if not gemini_key and 'settings' in client_data:
        gemini_key = client_data['settings'].get('geminiApiKey')
        if gemini_key:
            logger.info(f"✅ Found Gemini key in direct settings for {client_id}")
    
    if not gemini_key and 'geminiApiKey' in client_data:
        gemini_key = client_data['geminiApiKey']
        if gemini_key:
            logger.info(f"✅ Found Gemini key in root for {client_id}")
    
    if gemini_key:
        # Show key preview (first 10 chars only)
        key_preview = f"{gemini_key[:10]}..."
        logger.info(f"🔑 Gemini key found for {client_id}: {key_preview}")
        return gemini_key
    
    logger.error(f"❌ No Gemini key found for {client_id}")
    logger.info(f"   Available keys in client data: {list(client_data.keys())}")
    if 'settings' in client_data:
        logger.info(f"   Settings keys: {list(client_data['settings'].keys())}")
    
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
            logger.info(f"📚 Loaded {len(history)} messages for {client_id}/{user_id}")
            return history
        
        # If no history exists
        logger.info(f"📝 No chat history for {client_id}/{user_id}")
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
            "last_updated": datetime.utcnow().isoformat(),
            "client_id": client_id,
            "user_id": user_id
        }, merge=True)
        
        logger.info(f"💾 Saved {len(history)} messages for {client_id}/{user_id}")
        
    except Exception as e:
        logger.error(f"❌ Error saving chat history: {str(e)}")

def send_facebook_message(page_token, user_id, message_text):
    """Send message via Facebook Graph API"""
    try:
        if not page_token:
            logger.error("❌ No page token provided")
            return False
        
        url = "https://graph.facebook.com/v18.0/me/messages"
        
        payload = {
            "recipient": {"id": user_id},
            "message": {"text": message_text},
            "messaging_type": "RESPONSE"
        }
        
        params = {"access_token": page_token}
        
        logger.info(f"📨 Sending message to user {user_id[:10]}...")
        logger.debug(f"Message: {message_text[:50]}...")
        
        response = requests.post(url, params=params, json=payload, timeout=10)
        
        logger.info(f"📤 Facebook API Status: {response.status_code}")
        
        if response.status_code == 200:
            logger.info("✅ Message sent successfully")
            return True
        else:
            logger.error(f"❌ Failed to send message: {response.status_code}")
            logger.error(f"   Error: {response.text[:200]}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Error sending Facebook message: {str(e)}")
        return False

def generate_ai_response(client_id, user_message, user_id):
    """Generate AI response using Gemini"""
    try:
        logger.info(f"🤖 Generating AI response for client {client_id}")
        
        # Get Gemini API key for this client
        gemini_key = get_gemini_key(client_id)
        
        if not gemini_key:
            error_msg = "⚠️ AI service is not configured for this page. Please contact the administrator."
            logger.error(error_msg)
            return error_msg
        
        # Load chat history
        history = load_chat_history(client_id, user_id)
        
        # Detect language
        try:
            detected_lang = detect(user_message)
            language = "bangla" if detected_lang == "bn" else "english"
            logger.info(f"🌐 Detected language: {language}")
        except:
            language = "english"
            logger.info("🌐 Using default language: english")
        
        # Prepare conversation context
        context = ""
        for msg in history[-6:]:  # Last 6 messages
            if 'user' in msg and 'bot' in msg:
                context += f"User: {msg['user']}\nBot: {msg['bot']}\n"
        
        # Initialize Gemini client with THIS client's API key
        client = genai.Client(api_key=gemini_key)
        
        # Create prompt
        prompt = f"""You are {BOT_NAME}, a helpful AI assistant.
Respond in {language} language.
Be friendly, professional, and helpful.

Previous conversation:
{context}
User: {user_message}

Assistant:"""
        
        logger.debug(f"📝 Prompt: {prompt[:100]}...")
        
        # Generate response
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt
        )
        
        ai_response = response.text.strip()
        logger.info(f"🤖 Generated response: {ai_response[:50]}...")
        
        # Save to history
        history.append({
            "user": user_message,
            "bot": ai_response,
            "timestamp": datetime.utcnow().isoformat()
        })
        
        # Keep only last 20 messages
        if len(history) > 20:
            history = history[-20:]
        
        save_chat_history(client_id, user_id, history)
        
        return ai_response
        
    except Exception as e:
        logger.error(f"❌ Error generating AI response: {str(e)}")
        logger.error(traceback.format_exc())
        return "দুঃখিত, আমি এখন আপনার বার্তা প্রক্রিয়া করতে পারছি না। দয়া করে আবার চেষ্টা করুন।"

# ================= ROUTES =================
@app.route("/", methods=["GET"])
def home():
    """Home page"""
    return json.dumps({
        "status": "online",
        "service": "Simanto AI - Multi-Tenant Messenger Bot",
        "timestamp": datetime.utcnow().isoformat(),
        "firebase_connected": db is not None
    }, indent=2)

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Facebook webhook verification"""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    logger.info(f"🔧 Webhook verification request - Mode: {mode}")
    
    if mode == "subscribe" and token:
        try:
            # Search all clients for matching verify token
            all_clients = get_all_clients()
            
            logger.info(f"🔍 Checking verify token across {len(all_clients)} clients")
            
            for client in all_clients:
                client_data = client['data']
                
                # Check your Firebase structure: integrations.facebook.verifyToken
                if 'integrations' in client_data and 'facebook' in client_data['integrations']:
                    fb_data = client_data['integrations']['facebook']
                    verify_token = fb_data.get('verifyToken')
                    
                    if verify_token:
                        logger.debug(f"  Client {client['id']} has verify token: {verify_token}")
                        
                        if verify_token == token:
                            logger.info(f"✅ Webhook verified for client: {client['id']}")
                            return challenge, 200
            
            logger.error("❌ No matching verify token found")
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
            logger.error("❌ No JSON data received")
            return "No data", 400
        
        logger.info("📩 Received Facebook webhook")
        
        # Verify it's a page event
        if data.get("object") != "page":
            logger.error("❌ Not a page event")
            return "Not a page event", 400
        
        # Process messages
        for entry in data.get("entry", []):
            for event in entry.get("messaging", []):
                # Extract message info
                sender_id = event.get("sender", {}).get("id")
                recipient_id = event.get("recipient", {}).get("id")
                
                # Get message text
                message_text = None
                if "message" in event:
                    message_text = event["message"].get("text")
                elif "postback" in event:
                    message_text = event["postback"].get("payload")
                
                # Skip if no text
                if not message_text or not sender_id or not recipient_id:
                    logger.debug("⚠️ Skipping - no text or missing IDs")
                    continue
                
                logger.info(f"💬 Message from {sender_id[:10]}... to page {recipient_id}")
                logger.debug(f"Message text: {message_text}")
                
                # MULTI-TENANT: Find which client owns this page
                client_id = find_client_by_page_id(recipient_id)
                
                if not client_id:
                    logger.error(f"❌ No client found for page {recipient_id}")
                    # Don't send error message - just log it
                    continue
                
                logger.info(f"✅ Client found: {client_id}")
                
                # Get Facebook token for THIS client
                page_token = get_facebook_token(client_id)
                
                if not page_token:
                    logger.error(f"❌ No Facebook token for client {client_id}")
                    # Try to send error message
                    send_facebook_message(
                        "dummy_token",
                        sender_id,
                        "⚠️ Facebook integration is not configured for this page."
                    )
                    continue
                
                # Generate AI response using THIS client's Gemini key
                ai_response = generate_ai_response(client_id, message_text, sender_id)
                
                # Send response
                success = send_facebook_message(page_token, sender_id, ai_response)
                
                if success:
                    logger.info(f"✅ Successfully replied for client {client_id}")
                else:
                    logger.error(f"❌ Failed to send reply for client {client_id}")
        
        return "EVENT_RECEIVED", 200
        
    except Exception as e:
        logger.error(f"🔥 Webhook error: {str(e)}")
        logger.error(traceback.format_exc())
        return "ERROR", 500

# ================= ADMIN/MONITORING ROUTES =================
@app.route("/api/status", methods=["GET"])
def system_status():
    """System status"""
    all_clients = get_all_clients()
    
    return json.dumps({
        "status": "online",
        "firebase_connected": db is not None,
        "total_clients": len(all_clients),
        "cache_size": len(_client_cache),
        "timestamp": datetime.utcnow().isoformat()
    }, indent=2)

@app.route("/api/clients", methods=["GET"])
def list_clients():
    """List all clients"""
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
    """Get specific client info"""
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
                "page_name": fb_data.get('pageName'),
                "has_pageAccessToken": 'pageAccessToken' in fb_data,
                "has_verifyToken": 'verifyToken' in fb_data
            }
        
        return json.dumps(response, indent=2, default=str)
        
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2), 500

@app.route("/api/search/<page_id>", methods=["GET"])
def search_client_by_page(page_id):
    """Search client by page ID"""
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
            return json.dumps({"error": "Firebase not initialized"}, indent=2), 500
        
        # Count total clients
        clients = list(db.collection("clients").limit(10).get())
        
        # Try specific client
        specific_doc = db.collection("clients").document("e6L1ttGY8pPundmDoZb2gqgjJaw2").get()
        
        return json.dumps({
            "firebase_connected": True,
            "total_clients": len(clients),
            "specific_client_exists": specific_doc.exists,
            "clients_collection_exists": len(clients) > 0,
            "sample_client_ids": [doc.id for doc in clients[:3]] if clients else []
        }, indent=2)
            
    except Exception as e:
        return json.dumps({
            "error": str(e),
            "type": type(e).__name__,
            "traceback": traceback.format_exc()
        }, indent=2), 500

@app.route("/api/clear-cache", methods=["POST"])
def clear_cache():
    """Clear all caches"""
    global _client_cache, _page_to_client_cache
    
    _client_cache.clear()
    _page_to_client_cache.clear()
    
    return json.dumps({
        "success": True,
        "message": "Cache cleared",
        "client_cache_size": len(_client_cache),
        "page_cache_size": len(_page_to_client_cache)
    }, indent=2)

# ================= APPLICATION START =================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    
    logger.info("=" * 50)
    logger.info(f"🚀 {BOT_NAME} AI - Multi-Tenant Messenger Bot")
    logger.info(f"🌐 Port: {port}")
    logger.info(f"🔧 Firebase: {'✅ Connected' if db else '❌ Disconnected'}")
    logger.info("=" * 50)
    
    # Pre-load clients on startup
    if db:
        logger.info("🔄 Pre-loading clients on startup...")
        clients = get_all_clients()
        logger.info(f"📦 Pre-loaded {len(clients)} clients")
    
    app.run(host="0.0.0.0", port=port, debug=False)

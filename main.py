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
_subcollection_cache = {}

# ================= FIXED FIRESTORE READ FUNCTIONS =================
def get_client_document(client_id):
    """Get main client document"""
    if not db:
        logger.error("Firebase not initialized")
        return None
    
    try:
        doc_ref = db.collection("clients").document(client_id)
        doc = doc_ref.get()
        
        if doc.exists:
            data = doc.to_dict()
            logger.debug(f"✅ Loaded main client document for {client_id}")
            return data
        else:
            logger.error(f"❌ Client document {client_id} does not exist")
            return None
            
    except Exception as e:
        logger.error(f"❌ Error getting client document: {str(e)}")
        logger.error(traceback.format_exc())
        return None

def get_subcollection_document(client_id, subcollection_path, document_id):
    """Get document from subcollection with caching"""
    cache_key = f"{client_id}/{subcollection_path}/{document_id}"
    
    if cache_key in _subcollection_cache:
        logger.debug(f"📦 Cache hit for {cache_key}")
        return _subcollection_cache.get(cache_key)
    
    if not db:
        logger.error("Firebase not initialized")
        return None
    
    try:
        # Build document reference
        doc_ref = db.collection("clients").document(client_id)
        
        # Navigate through subcollection path
        path_parts = subcollection_path.split("/")
        for part in path_parts:
            doc_ref = doc_ref.collection(part)
        
        doc_ref = doc_ref.document(document_id)
        doc = doc_ref.get()
        
        if doc.exists:
            data = doc.to_dict()
            _subcollection_cache[cache_key] = data
            logger.debug(f"✅ Loaded {subcollection_path}/{document_id} for {client_id}")
            return data
        else:
            logger.warning(f"⚠️ Document not found: {subcollection_path}/{document_id} for {client_id}")
            _subcollection_cache[cache_key] = None  # Cache None to prevent repeated lookups
            return None
            
    except Exception as e:
        logger.error(f"❌ Error getting subcollection document: {str(e)}")
        logger.error(traceback.format_exc())
        return None

def get_client_data(client_id):
    """
    Get all client data by aggregating from main document and subcollections.
    Maintains same return structure for backward compatibility.
    """
    if client_id in _client_cache:
        return _client_cache[client_id]
    
    # Get main client document
    main_data = get_client_document(client_id)
    if not main_data:
        return None
    
    # Initialize aggregated data structure
    aggregated_data = {}
    aggregated_data.update(main_data)  # Start with main document data
    
    # Get Facebook integration data from subcollection
    facebook_data = get_subcollection_document(client_id, "integrations", "facebook")
    if facebook_data:
        aggregated_data["integrations"] = {"facebook": facebook_data}
        logger.info(f"📱 Facebook integration found for {client_id}")
    else:
        logger.warning(f"⚠️ No Facebook integration for {client_id}")
        aggregated_data["integrations"] = {}
    
    # Get settings data
    settings_data = get_subcollection_document(client_id, "settings", "apiKeys")
    if settings_data:
        aggregated_data["settings"] = {"apiKeys": settings_data}
        logger.info(f"⚙️ Settings found for {client_id}")
    else:
        logger.warning(f"⚠️ No settings found for {client_id}")
        aggregated_data["settings"] = {}
    
    # Check for direct settings document (fallback)
    if not settings_data:
        settings_direct = get_subcollection_document(client_id, "settings", "general")
        if settings_direct and 'geminiApiKey' in settings_direct:
            aggregated_data["settings"] = {"apiKeys": {"geminiApiKey": settings_direct['geminiApiKey']}}
            logger.info(f"⚙️ Found Gemini key in general settings for {client_id}")
    
    # Cache the aggregated data
    _client_cache[client_id] = aggregated_data
    
    logger.info(f"✅ Aggregated client data for {client_id}")
    logger.debug(f"   Keys: {list(aggregated_data.keys())}")
    
    return aggregated_data

def find_client_by_page_id(page_id):
    """Find client by Facebook page ID - FIXED for subcollections"""
    try:
        page_id_str = str(page_id)
        logger.info(f"🔍 Searching for page ID: {page_id_str}")
        
        # Check cache first
        if page_id_str in _page_to_client_cache:
            logger.info(f"📦 Cache hit for page {page_id_str}")
            return _page_to_client_cache[page_id_str]
        
        if not db:
            logger.error("Firebase not initialized")
            return None
        
        # Get all client documents
        clients_ref = db.collection("clients")
        clients_docs = clients_ref.get()
        
        logger.info(f"🔎 Scanning {len(clients_docs)} clients for page {page_id_str}")
        
        if not clients_docs:
            logger.error("❌ No clients found in database")
            return None
        
        for client_doc in clients_docs:
            client_id = client_doc.id
            logger.debug(f"  Checking client: {client_id}")
            
            # Get Facebook integration from subcollection
            facebook_data = get_subcollection_document(client_id, "integrations", "facebook")
            
            if not facebook_data:
                logger.debug(f"    No Facebook integration document")
                continue
            
            # Check for pageId
            if 'pageId' not in facebook_data:
                logger.debug(f"    No pageId in Facebook data")
                continue
            
            db_page_id = facebook_data['pageId']
            db_page_id_str = str(db_page_id)
            
            logger.debug(f"    Client {client_id} has pageId: {db_page_id_str}")
            
            if db_page_id_str == page_id_str:
                logger.info(f"✅ MATCH FOUND! Client {client_id} for page {page_id_str}")
                
                # Cache the result
                _page_to_client_cache[page_id_str] = client_id
                
                # Pre-load client data into cache
                get_client_data(client_id)
                
                return client_id
        
        logger.error(f"❌ No client found for page: {page_id_str}")
        
        # Show all pageIds found for debugging
        logger.info("📋 All page IDs found in database:")
        page_ids_found = []
        for client_doc in clients_docs:
            client_id = client_doc.id
            facebook_data = get_subcollection_document(client_id, "integrations", "facebook")
            if facebook_data and 'pageId' in facebook_data:
                page_ids_found.append(f"{client_id}: {facebook_data['pageId']}")
        
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

def get_facebook_token(client_id):
    """Get Facebook page access token - FIXED for subcollections"""
    logger.info(f"🔑 Getting Facebook token for {client_id}")
    
    # Get Facebook integration data directly from subcollection (more efficient)
    facebook_data = get_subcollection_document(client_id, "integrations", "facebook")
    
    if not facebook_data:
        logger.error(f"❌ No Facebook integration document for {client_id}")
        
        # Fallback: try from aggregated cache
        client_data = get_client_data(client_id)
        if client_data and 'integrations' in client_data and 'facebook' in client_data['integrations']:
            facebook_data = client_data['integrations']['facebook']
        else:
            return None
    
    token = facebook_data.get('pageAccessToken')
    
    if token:
        # Show token preview (first and last chars)
        token_preview = f"{token[:10]}...{token[-10:]}" if len(token) > 20 else "[short_token]"
        logger.info(f"✅ Facebook token found for {client_id}: {token_preview}")
        return token
    else:
        logger.error(f"❌ No pageAccessToken in Facebook data for {client_id}")
        logger.debug(f"   Available keys in Facebook data: {list(facebook_data.keys())}")
        return None

def get_gemini_key(client_id):
    """Get Gemini API key - FIXED for subcollections"""
    logger.info(f"🔑 Getting Gemini key for {client_id}")
    
    # Try to get from apiKeys subcollection first
    api_keys_data = get_subcollection_document(client_id, "settings", "apiKeys")
    
    gemini_key = None
    
    # Priority 1: apiKeys subcollection
    if api_keys_data:
        gemini_key = api_keys_data.get('geminiApiKey')
        if gemini_key:
            logger.info(f"✅ Found Gemini key in settings/apiKeys for {client_id}")
            return gemini_key
    
    # Priority 2: Check general settings document
    if not gemini_key:
        settings_data = get_subcollection_document(client_id, "settings", "general")
        if settings_data:
            gemini_key = settings_data.get('geminiApiKey')
            if gemini_key:
                logger.info(f"✅ Found Gemini key in settings/general for {client_id}")
                return gemini_key
    
    # Priority 3: Fallback to aggregated data
    if not gemini_key:
        client_data = get_client_data(client_id)
        if client_data:
            # Check various possible locations
            if 'settings' in client_data and 'apiKeys' in client_data['settings']:
                gemini_key = client_data['settings']['apiKeys'].get('geminiApiKey')
                if gemini_key:
                    logger.info(f"✅ Found Gemini key in cached settings for {client_id}")
                    return gemini_key
            
            if 'settings' in client_data:
                gemini_key = client_data['settings'].get('geminiApiKey')
                if gemini_key:
                    logger.info(f"✅ Found Gemini key in direct settings for {client_id}")
                    return gemini_key
    
    logger.error(f"❌ No Gemini key found for {client_id}")
    
    # Debug: List available data
    client_data = get_client_data(client_id)
    if client_data:
        logger.info(f"   Available data keys: {list(client_data.keys())}")
        if 'settings' in client_data:
            logger.info(f"   Settings structure: {client_data['settings']}")
    
    return None

# ================= REMAINING FUNCTIONS (UNCHANGED) =================
def load_chat_history(client_id, user_id):
    """Load chat history from subcollection"""
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
        
        logger.info(f"📝 No chat history for {client_id}/{user_id}")
        return []
        
    except Exception as e:
        logger.error(f"❌ Error loading chat history: {str(e)}")
        logger.error(traceback.format_exc())
        return []

def save_chat_history(client_id, user_id, history):
    """Save chat history to subcollection"""
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
        logger.error(traceback.format_exc())

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
        logger.error(traceback.format_exc())
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

# ================= ROUTES (UNCHANGED) =================
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
            if not db:
                return "Firebase not initialized", 500
            
            clients_ref = db.collection("clients")
            clients_docs = clients_ref.get()
            
            logger.info(f"🔍 Checking verify token across {len(clients_docs)} clients")
            
            for client_doc in clients_docs:
                client_id = client_doc.id
                
                # Get Facebook integration from subcollection
                facebook_data = get_subcollection_document(client_id, "integrations", "facebook")
                
                if facebook_data:
                    verify_token = facebook_data.get('verifyToken')
                    
                    if verify_token:
                        logger.debug(f"  Client {client_id} has verify token: {verify_token}")
                        
                        if verify_token == token:
                            logger.info(f"✅ Webhook verified for client: {client_id}")
                            return challenge, 200
            
            logger.error("❌ No matching verify token found")
            return "Invalid verify token", 403
            
        except Exception as e:
            logger.error(f"❌ Verification error: {str(e)}")
            logger.error(traceback.format_exc())
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
                    continue
                
                logger.info(f"✅ Client found: {client_id}")
                
                # Get Facebook token for THIS client
                page_token = get_facebook_token(client_id)
                
                if not page_token:
                    logger.error(f"❌ No Facebook token for client {client_id}")
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

# ================= ADMIN/MONITORING ROUTES (ADAPTED) =================
@app.route("/api/status", methods=["GET"])
def system_status():
    """System status"""
    try:
        if not db:
            return json.dumps({"firebase_connected": False}, indent=2)
        
        clients_ref = db.collection("clients")
        clients_count = len(list(clients_ref.limit(1000).get()))
        
        return json.dumps({
            "status": "online",
            "firebase_connected": True,
            "total_clients": clients_count,
            "client_cache_size": len(_client_cache),
            "subcollection_cache_size": len(_subcollection_cache),
            "timestamp": datetime.utcnow().isoformat()
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2), 500

@app.route("/api/test-subcollection/<client_id>", methods=["GET"])
def test_subcollection(client_id):
    """Test subcollection access"""
    try:
        facebook_data = get_subcollection_document(client_id, "integrations", "facebook")
        api_keys_data = get_subcollection_document(client_id, "settings", "apiKeys")
        
        return json.dumps({
            "client_id": client_id,
            "facebook_data": facebook_data,
            "api_keys_data": api_keys_data,
            "facebook_exists": facebook_data is not None,
            "api_keys_exists": api_keys_data is not None
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2), 500

# ================= CLEAR CACHE (UPDATED) =================
@app.route("/api/clear-cache", methods=["POST"])
def clear_cache():
    """Clear all caches"""
    global _client_cache, _page_to_client_cache, _subcollection_cache
    
    client_cache_size = len(_client_cache)
    page_cache_size = len(_page_to_client_cache)
    subcollection_cache_size = len(_subcollection_cache)
    
    _client_cache.clear()
    _page_to_client_cache.clear()
    _subcollection_cache.clear()
    
    return json.dumps({
        "success": True,
        "message": "Cache cleared",
        "client_cache_cleared": client_cache_size,
        "page_cache_cleared": page_cache_size,
        "subcollection_cache_cleared": subcollection_cache_size
    }, indent=2)

# ================= APPLICATION START =================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    
    logger.info("=" * 50)
    logger.info(f"🚀 {BOT_NAME} AI - Multi-Tenant Messenger Bot (FIXED)")
    logger.info(f"🌐 Port: {port}")
    logger.info(f"🔧 Firebase: {'✅ Connected' if db else '❌ Disconnected'}")
    logger.info(f"📁 Using Firestore Subcollections")
    logger.info("=" * 50)
    
    # Pre-load clients on startup
    if db:
        logger.info("🔄 Testing subcollection access on startup...")
        try:
            clients_ref = db.collection("clients")
            client_docs = list(clients_ref.limit(5).get())
            
            for doc in client_docs[:3]:  # Test first 3 clients
                client_id = doc.id
                logger.info(f"  Testing client: {client_id}")
                
                # Test Facebook integration
                fb_data = get_subcollection_document(client_id, "integrations", "facebook")
                if fb_data:
                    logger.info(f"    Facebook: ✅ Found (pageId: {fb_data.get('pageId', 'N/A')})")
                else:
                    logger.info(f"    Facebook: ❌ Not found")
                
                # Test API keys
                api_data = get_subcollection_document(client_id, "settings", "apiKeys")
                if api_data:
                    has_key = 'geminiApiKey' in api_data
                    logger.info(f"    API Keys: ✅ Found (has Gemini key: {has_key})")
                else:
                    logger.info(f"    API Keys: ❌ Not found")
        
        except Exception as e:
            logger.error(f"❌ Startup test failed: {str(e)}")
            logger.error(traceback.format_exc())
    
    app.run(host="0.0.0.0", port=port, debug=False)

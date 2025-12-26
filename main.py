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
    
    # Get Firebase service account from environment variable
    service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT")
    
    if not service_account_json:
        logger.error("FIREBASE_SERVICE_ACCOUNT environment variable is not set!")
        raise ValueError("Firebase service account not configured")
    
    # Parse the JSON string
    service_account = json.loads(service_account_json)
    
    # Initialize Firebase
    cred = credentials.Certificate(service_account)
    firebase_admin.initialize_app(cred)
    
    # Get Firestore client
    db = firestore.client()
    
    logger.info("✅ Firebase initialized successfully")
    
except Exception as e:
    logger.error(f"❌ Failed to initialize Firebase: {str(e)}")
    # Create a dummy db object to prevent crashes
    db = None

# ================= FLASK APP =================
app = Flask(__name__)

# ================= HELPER FUNCTIONS =================
def find_client_by_page_id(page_id):
    """
    Find client by Facebook page ID
    This function handles multiple search strategies
    """
    if not db:
        logger.error("Firebase not initialized")
        return None
    
    try:
        page_id_str = str(page_id)
        logger.info(f"🔍 Searching for page ID: {page_id_str}")
        
        # STRATEGY 1: Try direct query with common field paths
        field_paths = [
            "integrations.facebook.pageId",
            "facebook.pageId",
            "pageId",
            "fb_page_id",
            "fbPageId"
        ]
        
        for field_path in field_paths:
            try:
                logger.info(f"  Trying field path: {field_path}")
                docs = db.collection("clients").where(field_path, "==", page_id_str).limit(1).get()
                
                if docs:
                    for doc in docs:
                        logger.info(f"✅ Found client using {field_path}: {doc.id}")
                        return doc.id
            except Exception as e:
                logger.debug(f"  Query failed for {field_path}: {str(e)}")
                continue
        
        # STRATEGY 2: Try with integer if page_id is numeric
        if page_id_str.isdigit():
            try:
                page_id_int = int(page_id_str)
                for field_path in field_paths:
                    try:
                        docs = db.collection("clients").where(field_path, "==", page_id_int).limit(1).get()
                        if docs:
                            for doc in docs:
                                logger.info(f"✅ Found client using {field_path} (as int): {doc.id}")
                                return doc.id
                    except:
                        continue
            except ValueError:
                pass
        
        # STRATEGY 3: Get ALL clients and search manually
        logger.info("  Getting all clients for manual search...")
        all_clients = db.collection("clients").limit(50).get()
        
        for client in all_clients:
            client_data = client.to_dict()
            
            # Function to search for pageId in nested dict
            def search_for_page_id(data, path=""):
                if isinstance(data, dict):
                    # Check current level
                    for key, value in data.items():
                        # If key contains 'page' or 'id' (case insensitive)
                        if 'page' in key.lower() and 'id' in key.lower():
                            if str(value) == page_id_str:
                                return True, f"{path}.{key}" if path else key
                        
                        # Search recursively
                        if isinstance(value, (dict, list)):
                            found, found_path = search_for_page_id(value, f"{path}.{key}" if path else key)
                            if found:
                                return True, found_path
                
                elif isinstance(data, list):
                    for i, item in enumerate(data):
                        if isinstance(item, (dict, list)):
                            found, found_path = search_for_page_id(item, f"{path}[{i}]")
                            if found:
                                return True, found_path
                
                return False, ""
            
            # Search in this client's data
            found, found_path = search_for_page_id(client_data)
            if found:
                logger.info(f"✅ Found client {client.id} via manual search at {found_path}")
                return client.id
        
        logger.error(f"❌ No client found for page ID: {page_id_str}")
        return None
        
    except Exception as e:
        logger.error(f"❌ Error finding client: {str(e)}")
        return None

def get_client_data(client_id):
    """Get client document data"""
    if not db:
        return None
    
    try:
        doc_ref = db.collection("clients").document(client_id)
        doc = doc_ref.get()
        
        if doc.exists:
            return doc.to_dict()
        else:
            logger.error(f"Client document {client_id} does not exist")
            return None
    except Exception as e:
        logger.error(f"Error getting client data: {str(e)}")
        return None

def get_facebook_token(client_id):
    """Extract Facebook page access token from client data"""
    client_data = get_client_data(client_id)
    
    if not client_data:
        return None
    
    # Look for token in common locations
    token = None
    
    # Location 1: integrations.facebook.pageAccessToken
    if 'integrations' in client_data:
        integrations = client_data['integrations']
        if 'facebook' in integrations:
            facebook_data = integrations['facebook']
            token = facebook_data.get('pageAccessToken')
    
    # Location 2: facebook.pageAccessToken (direct)
    if not token and 'facebook' in client_data:
        facebook_data = client_data['facebook']
        token = facebook_data.get('pageAccessToken')
    
    # Location 3: pageAccessToken (root level)
    if not token and 'pageAccessToken' in client_data:
        token = client_data['pageAccessToken']
    
    if token:
        logger.info(f"✅ Found Facebook token for client {client_id}")
        return token
    else:
        logger.error(f"❌ No Facebook token found for client {client_id}")
        return None

def get_gemini_key(client_id):
    """Extract Gemini API key from client data"""
    client_data = get_client_data(client_id)
    
    if not client_data:
        return None
    
    # Look for Gemini key in common locations
    gemini_key = None
    
    # Location 1: settings.apiKeys.geminiApiKey
    if 'settings' in client_data:
        settings = client_data['settings']
        if 'apiKeys' in settings:
            api_keys = settings['apiKeys']
            gemini_key = api_keys.get('geminiApiKey')
    
    # Location 2: geminiApiKey (direct in settings)
    if not gemini_key and 'settings' in client_data:
        gemini_key = client_data['settings'].get('geminiApiKey')
    
    # Location 3: geminiApiKey (root level)
    if not gemini_key and 'geminiApiKey' in client_data:
        gemini_key = client_data['geminiApiKey']
    
    if gemini_key:
        logger.info(f"✅ Found Gemini key for client {client_id}")
        return gemini_key
    else:
        logger.error(f"❌ No Gemini key found for client {client_id}")
        return None

def load_chat_history(client_id, user_id=None):
    """Load chat history from Firebase"""
    if not db:
        return []
    
    try:
        if user_id:
            # Store history per user
            doc_ref = db.collection("clients").document(client_id)\
                .collection("chat_sessions").document(user_id)
        else:
            # Store general history
            doc_ref = db.collection("clients").document(client_id)\
                .collection("chat_history").document("general")
        
        doc = doc_ref.get()
        
        if doc.exists:
            history = doc.to_dict().get("messages", [])
            logger.info(f"📚 Loaded {len(history)} messages for {client_id}")
            return history
        return []
        
    except Exception as e:
        logger.error(f"Error loading chat history: {str(e)}")
        return []

def save_chat_history(client_id, history, user_id=None):
    """Save chat history to Firebase"""
    if not db:
        return
    
    try:
        if user_id:
            doc_ref = db.collection("clients").document(client_id)\
                .collection("chat_sessions").document(user_id)
        else:
            doc_ref = db.collection("clients").document(client_id)\
                .collection("chat_history").document("general")
        
        doc_ref.set({
            "messages": history,
            "last_updated": datetime.utcnow().isoformat()
        })
        
        logger.info(f"💾 Saved {len(history)} messages for {client_id}")
        
    except Exception as e:
        logger.error(f"Error saving chat history: {str(e)}")

def send_facebook_message(page_token, recipient_id, message_text):
    """Send message via Facebook Graph API"""
    try:
        if not page_token:
            logger.error("No page token provided")
            return False
        
        url = f"https://graph.facebook.com/v18.0/me/messages"
        
        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": message_text},
            "messaging_type": "RESPONSE"
        }
        
        params = {
            "access_token": page_token
        }
        
        logger.info(f"📨 Sending message to user {recipient_id[:10]}...")
        
        response = requests.post(url, params=params, json=payload, timeout=10)
        
        logger.info(f"Facebook API response: {response.status_code}")
        
        if response.status_code == 200:
            logger.info("✅ Message sent successfully")
            return True
        else:
            logger.error(f"❌ Failed to send message: {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Error sending Facebook message: {str(e)}")
        return False

def generate_ai_response(client_id, user_message, user_id=None):
    """Generate AI response using Gemini"""
    try:
        # Get Gemini API key
        gemini_key = get_gemini_key(client_id)
        if not gemini_key:
            return "⚠️ AI service is not configured. Please contact the page administrator."
        
        # Load chat history
        history = load_chat_history(client_id, user_id)
        
        # Detect language
        try:
            detected_lang = detect(user_message)
            language = "bangla" if detected_lang == "bn" else "english"
        except:
            language = "english"
        
        # Prepare conversation context
        context = ""
        for msg in history[-6:]:  # Last 6 messages
            if 'user' in msg and 'bot' in msg:
                context += f"User: {msg['user']}\nBot: {msg['bot']}\n"
        
        # Initialize Gemini client
        client = genai.Client(api_key=gemini_key)
        
        # Create prompt
        prompt = f"""You are {BOT_NAME}, a helpful AI assistant.
Respond in {language} language.
Be friendly, professional, and helpful.

Previous conversation:
{context}
User: {user_message}

Assistant:"""
        
        # Generate response
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt
        )
        
        ai_response = response.text.strip()
        
        # Save to history
        history.append({
            "user": user_message,
            "bot": ai_response,
            "timestamp": datetime.utcnow().isoformat()
        })
        
        # Keep only last 20 messages
        if len(history) > 20:
            history = history[-20:]
        
        save_chat_history(client_id, history, user_id)
        
        logger.info(f"🤖 Generated response: {ai_response[:50]}...")
        return ai_response
        
    except Exception as e:
        logger.error(f"❌ Error generating AI response: {str(e)}")
        return "Sorry, I'm having trouble processing your request. Please try again."

# ================= ROUTES =================
@app.route("/", methods=["GET"])
def home():
    """Home page - check if server is running"""
    return {
        "status": "online",
        "service": "Simanto AI Messenger Bot",
        "timestamp": datetime.utcnow().isoformat()
    }

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Facebook webhook verification"""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    logger.info(f"🔧 Webhook verification request: mode={mode}")
    
    if mode == "subscribe" and token:
        # Search for client with matching verify token
        if db:
            try:
                clients = db.collection("clients").limit(20).get()
                
                for client in clients:
                    client_data = client.to_dict()
                    
                    # Look for verify token
                    verify_token = None
                    
                    # Check integrations.facebook.verifyToken
                    if 'integrations' in client_data:
                        integrations = client_data['integrations']
                        if 'facebook' in integrations:
                            facebook_data = integrations['facebook']
                            verify_token = facebook_data.get('verifyToken')
                    
                    # Check other locations
                    if not verify_token and 'facebook' in client_data:
                        facebook_data = client_data['facebook']
                        verify_token = facebook_data.get('verifyToken')
                    
                    if not verify_token and 'verifyToken' in client_data:
                        verify_token = client_data['verifyToken']
                    
                    if verify_token == token:
                        logger.info(f"✅ Verified webhook for client: {client.id}")
                        return challenge, 200
                
                logger.error("❌ No matching verify token found")
                return "Invalid verify token", 403
                
            except Exception as e:
                logger.error(f"❌ Error during verification: {str(e)}")
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
            return "Not a page event", 400
        
        # Process each entry
        for entry in data.get("entry", []):
            page_id = entry.get("id", "unknown")
            
            for event in entry.get("messaging", []):
                # Extract sender and recipient info
                sender_id = event.get("sender", {}).get("id")
                recipient_id = event.get("recipient", {}).get("id")
                
                # Skip if no sender or recipient
                if not sender_id or not recipient_id:
                    continue
                
                # Extract message text
                message_text = None
                
                # Check for regular message
                if "message" in event and "text" in event["message"]:
                    message_text = event["message"]["text"]
                
                # Check for postback (button clicks)
                elif "postback" in event:
                    message_text = event["postback"].get("payload")
                
                # Skip if no text
                if not message_text:
                    continue
                
                logger.info(f"💬 Message from {sender_id} to page {recipient_id}: {message_text}")
                
                # Find which client this page belongs to
                client_id = find_client_by_page_id(recipient_id)
                
                if not client_id:
                    logger.error(f"❌ No client found for page {recipient_id}")
                    # Try to send error message using any available token
                    try:
                        if db:
                            # Get first client to send error from
                            clients = db.collection("clients").limit(1).get()
                            if clients:
                                first_client = clients[0].id
                                token = get_facebook_token(first_client)
                                if token:
                                    send_facebook_message(
                                        token,
                                        sender_id,
                                        "⚠️ This page is not properly configured with Simanto AI. Please contact the administrator."
                                    )
                    except:
                        pass
                    continue
                
                logger.info(f"✅ Processing for client: {client_id}")
                
                # Get Facebook page access token
                page_token = get_facebook_token(client_id)
                
                if not page_token:
                    logger.error(f"❌ No Facebook token for client {client_id}")
                    # Send error to user
                    send_facebook_message(
                        "dummy_token",  # Will fail but logs error
                        sender_id,
                        "⚠️ Facebook integration is not properly configured. Please contact the page administrator."
                    )
                    continue
                
                # Generate AI response
                ai_response = generate_ai_response(client_id, message_text, sender_id)
                
                # Send response back to user
                success = send_facebook_message(page_token, sender_id, ai_response)
                
                if success:
                    logger.info("✅ Successfully processed and replied to message")
                else:
                    logger.error("❌ Failed to send response to user")
        
        return "EVENT_RECEIVED", 200
        
    except Exception as e:
        logger.error(f"🔥 Error in webhook handler: {str(e)}")
        return "ERROR", 500

@app.route("/api/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "firebase_connected": db is not None,
        "timestamp": datetime.utcnow().isoformat()
    }

@app.route("/api/test/client/<client_id>", methods=["GET"])
def test_client(client_id):
    """Test if a specific client is configured correctly"""
    try:
        client_data = get_client_data(client_id)
        
        if not client_data:
            return {"error": "Client not found"}, 404
        
        # Check configurations
        has_facebook_token = get_facebook_token(client_id) is not None
        has_gemini_key = get_gemini_key(client_id) is not None
        
        return {
            "client_id": client_id,
            "has_facebook_token": has_facebook_token,
            "has_gemini_key": has_gemini_key,
            "facebook_page_ids": [],
            "keys_in_data": list(client_data.keys())
        }
        
    except Exception as e:
        return {"error": str(e)}, 500

@app.route("/api/test/page/<page_id>", methods=["GET"])
def test_page_search(page_id):
    """Test searching for a page ID"""
    client_id = find_client_by_page_id(page_id)
    
    if client_id:
        return {
            "found": True,
            "page_id": page_id,
            "client_id": client_id
        }
    else:
        return {
            "found": False,
            "page_id": page_id,
            "message": "No client found with this page ID"
        }, 404

# ================= APPLICATION START =================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    
    logger.info("=" * 50)
    logger.info(f"🚀 Starting {BOT_NAME} AI Messenger Bot")
    logger.info(f"🌐 Port: {port}")
    logger.info(f"🔧 Firebase: {'✅ Connected' if db else '❌ Disconnected'}")
    logger.info("=" * 50)
    
    app.run(host="0.0.0.0", port=port, debug=False)

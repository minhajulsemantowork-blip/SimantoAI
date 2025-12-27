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
    
    # Test connection with a simple query
    try:
        supabase.table("clients").select("count").limit(1).execute()
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
    """Find client by Facebook page ID"""
    try:
        page_id_str = str(page_id)
        logger.info(f"🔍 Searching for page ID: {page_id_str}")
        
        # Check cache first
        if page_id_str in _page_to_client_cache:
            logger.info(f"📦 Cache hit for page {page_id_str}")
            return _page_to_client_cache[page_id_str]
        
        if not supabase:
            logger.error("Supabase not initialized")
            return None
        
        # Query Facebook integrations table
        response = supabase.table("facebook_integrations")\
            .select("client_id, page_name")\
            .eq("page_id", page_id_str)\
            .eq("is_connected", True)\
            .execute()
        
        if response.data and len(response.data) > 0:
            client_id = response.data[0]["client_id"]
            page_name = response.data[0].get("page_name", "Unknown")
            
            logger.info(f"✅ Found client {client_id} for page {page_id_str} ({page_name})")
            
            # Cache the result
            _page_to_client_cache[page_id_str] = str(client_id)
            return str(client_id)
        
        logger.error(f"❌ No client found for page: {page_id_str}")
        return None
        
    except Exception as e:
        logger.error(f"❌ Error finding client: {str(e)}")
        logger.error(traceback.format_exc())
        return None

def get_facebook_token(client_id):
    """Get Facebook page access token"""
    try:
        logger.info(f"🔑 Getting Facebook token for {client_id}")
        
        if not supabase:
            logger.error("Supabase not initialized")
            return None
        
        # Query Facebook integrations table
        response = supabase.table("facebook_integrations")\
            .select("page_access_token, page_name")\
            .eq("client_id", client_id)\
            .eq("is_connected", True)\
            .execute()
        
        if response.data and len(response.data) > 0:
            token = response.data[0]["page_access_token"]
            page_name = response.data[0].get("page_name", "Unknown")
            
            # Show token preview (security)
            if len(token) > 20:
                token_preview = f"{token[:10]}...{token[-10:]}"
            else:
                token_preview = "[short_token]"
                
            logger.info(f"✅ Facebook token found for {client_id} ({page_name}): {token_preview}")
            return token
        
        logger.error(f"❌ No Facebook integration found for client {client_id}")
        return None
        
    except Exception as e:
        logger.error(f"❌ Error getting Facebook token: {str(e)}")
        logger.error(traceback.format_exc())
        return None

def get_gemini_key(client_id):
    """Get Gemini API key"""
    try:
        logger.info(f"🔑 Getting Gemini key for {client_id}")
        
        if not supabase:
            logger.error("Supabase not initialized")
            return None
        
        # Query API keys table
        response = supabase.table("api_keys")\
            .select("gemini_api_key")\
            .eq("client_id", client_id)\
            .execute()
        
        if response.data and len(response.data) > 0:
            gemini_key = response.data[0]["gemini_api_key"]
            
            if gemini_key and gemini_key.strip():
                # Show key preview (security)
                key_preview = f"{gemini_key[:8]}..." if len(gemini_key) > 10 else "***"
                logger.info(f"✅ Gemini key found for {client_id}: {key_preview}")
                return gemini_key
        
        logger.error(f"❌ No Gemini key found for client {client_id}")
        return None
        
    except Exception as e:
        logger.error(f"❌ Error getting Gemini key: {str(e)}")
        logger.error(traceback.format_exc())
        return None

def load_chat_history(client_id, user_id):
    """Load chat history"""
    cache_key = f"{client_id}_{user_id}"
    
    # Check cache first
    if cache_key in _chat_history_cache:
        history = _chat_history_cache[cache_key]
        logger.debug(f"📦 Chat history cache hit for {client_id}/{user_id}")
        return history
    
    try:
        if not supabase:
            logger.error("Supabase not initialized")
            return []
        
        response = supabase.table("chat_history")\
            .select("messages")\
            .eq("client_id", client_id)\
            .eq("user_id", user_id)\
            .execute()
        
        if response.data and len(response.data) > 0:
            history = response.data[0]["messages"]
            if not isinstance(history, list):
                history = []
                
            logger.info(f"📚 Loaded {len(history)} messages for {client_id}/{user_id}")
            
            # Cache the history
            _chat_history_cache[cache_key] = history
            return history
        
        logger.info(f"📝 No chat history for {client_id}/{user_id}")
        return []
        
    except Exception as e:
        logger.error(f"❌ Error loading chat history: {str(e)}")
        logger.error(traceback.format_exc())
        return []

def save_chat_history(client_id, user_id, history):
    """Save chat history"""
    cache_key = f"{client_id}_{user_id}"
    
    try:
        if not supabase:
            logger.error("Supabase not initialized")
            return
        
        # Update cache
        _chat_history_cache[cache_key] = history
        
        # Prepare data for Supabase
        chat_data = {
            "client_id": client_id,
            "user_id": user_id,
            "messages": history,
            "last_updated": datetime.utcnow().isoformat()
        }
        
        # Upsert chat history
        supabase.table("chat_history").upsert(chat_data).execute()
        
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
        
        if not user_id or not message_text:
            logger.error("❌ Missing user_id or message_text")
            return False
        
        url = "https://graph.facebook.com/v18.0/me/messages"
        
        payload = {
            "recipient": {"id": user_id},
            "message": {"text": message_text},
            "messaging_type": "RESPONSE"
        }
        
        params = {"access_token": page_token}
        
        logger.info(f"📨 Sending message to user {user_id[:10]}...")
        logger.debug(f"Message: {message_text[:100]}...")
        
        response = requests.post(
            url, 
            params=params, 
            json=payload, 
            timeout=30,
            headers={'Content-Type': 'application/json'}
        )
        
        logger.info(f"📤 Facebook API Status: {response.status_code}")
        
        if response.status_code == 200:
            logger.info("✅ Message sent successfully")
            return True
        else:
            logger.error(f"❌ Failed to send message: {response.status_code}")
            logger.error(f"   Error: {response.text[:200]}")
            return False
            
    except requests.exceptions.Timeout:
        logger.error("❌ Facebook API timeout")
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
            if detected_lang == "bn":
                language = "bangla"
                system_prompt = f"আপনি {BOT_NAME}, একজন সহায়ক AI সহকারী। বন্ধুত্বপূর্ণ, পেশাদার এবং সহায়ক হন। বাংলায় উত্তর দিন।"
            else:
                language = "english"
                system_prompt = f"You are {BOT_NAME}, a helpful AI assistant. Be friendly, professional, and helpful. Respond in English."
            logger.info(f"🌐 Detected language: {language}")
        except Exception as lang_err:
            language = "english"
            system_prompt = f"You are {BOT_NAME}, a helpful AI assistant. Be friendly, professional, and helpful. Respond in English."
            logger.info(f"🌐 Using default language: english (Error: {lang_err})")
        
        # Prepare conversation context (last 5 exchanges)
        context = ""
        recent_history = history[-10:] if len(history) > 10 else history
        
        for msg in recent_history:
            if isinstance(msg, dict):
                user_msg = msg.get('user', '')
                bot_msg = msg.get('bot', '')
                if user_msg and bot_msg:
                    context += f"User: {user_msg}\nBot: {bot_msg}\n"
        
        # Create prompt
        prompt = f"""{system_prompt}

Previous conversation:
{context}
User: {user_message}

Assistant:"""
        
        logger.debug(f"📝 Prompt length: {len(prompt)} chars")
        
        # Initialize Gemini client
        client = genai.Client(api_key=gemini_key)
        
        # Generate response with timeout handling
        try:
            response = client.models.generate_content(
                model="gemini-1.5-flash",
                contents=prompt,
                config={
                    "temperature": 0.7,
                    "max_output_tokens": 1000,
                }
            )
            
            ai_response = response.text.strip()
            
            if not ai_response:
                ai_response = "দুঃখিত, আমি এখন উত্তর দিতে পারছি না। দয়া করে আবার চেষ্টা করুন।"
                
        except Exception as gemini_error:
            logger.error(f"❌ Gemini API error: {gemini_error}")
            ai_response = "দুঃখিত, AI সার্ভিসে সমস্যা হচ্ছে। দয়া করে কিছুক্ষণ পর আবার চেষ্টা করুন।"
        
        logger.info(f"🤖 Generated response: {ai_response[:100]}...")
        
        # Save to history
        history.append({
            "user": user_message,
            "bot": ai_response,
            "timestamp": datetime.utcnow().isoformat(),
            "language": language
        })
        
        # Keep only last 30 messages
        if len(history) > 30:
            history = history[-30:]
        
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
        "service": f"{BOT_NAME} AI Messenger Bot",
        "version": "2.0.0",
        "database": "Supabase",
        "timestamp": datetime.utcnow().isoformat(),
        "endpoints": {
            "webhook": "/webhook",
            "health": "/health",
            "status": "/api/status",
            "test": "/api/test"
        }
    }, indent=2)

@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint"""
    return json.dumps({
        "status": "healthy" if supabase else "degraded",
        "supabase_connected": supabase is not None,
        "timestamp": datetime.utcnow().isoformat()
    }, indent=2)

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Facebook webhook verification"""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    logger.info(f"🔧 Webhook verification request - Mode: {mode}, Token: {token[:20] if token else 'None'}...")
    
    if mode == "subscribe" and token:
        try:
            if not supabase:
                return "Supabase not initialized", 500
            
            # Query Facebook integrations for verify token
            response = supabase.table("facebook_integrations")\
                .select("client_id, page_name, page_id")\
                .eq("verify_token", token)\
                .eq("is_connected", True)\
                .execute()
            
            if response.data and len(response.data) > 0:
                client_id = response.data[0]["client_id"]
                page_name = response.data[0].get("page_name", "Unknown")
                page_id = response.data[0].get("page_id", "Unknown")
                
                logger.info(f"✅ Webhook verified for client {client_id} (Page: {page_name}, ID: {page_id})")
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
            page_id = entry.get("id", "")
            
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
                logger.debug(f"Message: {message_text[:100]}...")
                
                # Find which client owns this page
                client_id = find_client_by_page_id(recipient_id)
                
                if not client_id:
                    logger.error(f"❌ No client found for page {recipient_id}")
                    continue
                
                logger.info(f"✅ Client found: {client_id}")
                
                # Get Facebook token
                page_token = get_facebook_token(client_id)
                
                if not page_token:
                    logger.error(f"❌ No Facebook token for client {client_id}")
                    # Send error message to user
                    error_msg = "⚠️ This page is not properly configured. Please contact the administrator."
                    send_facebook_message("dummy_token", sender_id, error_msg)
                    continue
                
                # Generate AI response
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

# ================= ADMIN API ROUTES =================
@app.route("/api/status", methods=["GET"])
def api_status():
    """API status endpoint"""
    try:
        clients_count = 0
        fb_integrations_count = 0
        api_keys_count = 0
        
        if supabase:
            try:
                clients_resp = supabase.table("clients").select("*", count="exact").execute()
                clients_count = clients_resp.count or 0
                
                fb_resp = supabase.table("facebook_integrations").select("*", count="exact").execute()
                fb_integrations_count = fb_resp.count or 0
                
                api_resp = supabase.table("api_keys").select("*", count="exact").execute()
                api_keys_count = api_resp.count or 0
            except:
                pass
        
        return json.dumps({
            "status": "online",
            "bot_name": BOT_NAME,
            "supabase_connected": supabase is not None,
            "cache": {
                "page_cache": len(_page_to_client_cache),
                "chat_cache": len(_chat_history_cache)
            },
            "database_stats": {
                "clients": clients_count,
                "facebook_integrations": fb_integrations_count,
                "api_keys": api_keys_count
            },
            "timestamp": datetime.utcnow().isoformat()
        }, indent=2)
        
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2), 500

@app.route("/api/test", methods=["GET"])
def test_api():
    """Test API endpoint"""
    return json.dumps({
        "message": f"{BOT_NAME} AI Bot is running",
        "status": "operational",
        "timestamp": datetime.utcnow().isoformat()
    }, indent=2)

@app.route("/api/search-page/<page_id>", methods=["GET"])
def search_page(page_id):
    """Search for a page ID"""
    client_id = find_client_by_page_id(page_id)
    
    return json.dumps({
        "page_id": page_id,
        "found": client_id is not None,
        "client_id": client_id,
        "cached": page_id in _page_to_client_cache
    }, indent=2)

@app.route("/api/clear-cache", methods=["POST"])
def clear_cache():
    """Clear all caches"""
    global _page_to_client_cache, _chat_history_cache
    
    page_cache_size = len(_page_to_client_cache)
    chat_cache_size = len(_chat_history_cache)
    
    _page_to_client_cache.clear()
    _chat_history_cache.clear()
    
    return json.dumps({
        "success": True,
        "message": "Cache cleared",
        "cleared_items": {
            "page_cache": page_cache_size,
            "chat_cache": chat_cache_size
        }
    }, indent=2)

@app.route("/api/test-message", methods=["POST"])
def test_message_endpoint():
    """Test message sending"""
    try:
        data = request.get_json()
        
        client_id = data.get("client_id")
        test_user_id = data.get("user_id", "1234567890")
        message = data.get("message", "Hello, this is a test message from Simanto AI!")
        
        if not client_id:
            return json.dumps({"error": "client_id is required"}, indent=2), 400
        
        # Get Facebook token
        page_token = get_facebook_token(client_id)
        
        if not page_token:
            return json.dumps({"error": "No Facebook token found for this client"}, indent=2), 404
        
        # Send test message
        success = send_facebook_message(page_token, test_user_id, message)
        
        return json.dumps({
            "success": success,
            "client_id": client_id,
            "user_id": test_user_id,
            "message_sent": message[:50] + "..." if len(message) > 50 else message
        }, indent=2)
        
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2), 500

# ================= ERROR HANDLERS =================
@app.errorhandler(404)
def not_found(error):
    return json.dumps({
        "error": "Not found",
        "message": "The requested endpoint does not exist"
    }, indent=2), 404

@app.errorhandler(500)
def internal_error(error):
    return json.dumps({
        "error": "Internal server error",
        "message": "Something went wrong on our end"
    }, indent=2), 500

# ================= APPLICATION START =================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    
    logger.info("=" * 60)
    logger.info(f"🚀 {BOT_NAME} AI Messenger Bot - Supabase Edition")
    logger.info(f"🌐 Port: {port}")
    logger.info(f"🗄️  Database: {'✅ Supabase Connected' if supabase else '❌ Not Connected'}")
    logger.info(f"🤖 AI: Google Gemini")
    logger.info("=" * 60)
    
    if not supabase:
        logger.warning("⚠️ WARNING: Supabase is not connected. Bot will not function properly!")
        logger.warning("   Set SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables.")
    
    app.run(host="0.0.0.0", port=port, debug=False)

import os
import json
import requests
import logging
import traceback
import random
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from flask import Flask, request, jsonify
from openai import OpenAI
from supabase import create_client, Client

# ================= LOGGING SETUP =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ================= CONFIG =================
BOT_NAME = "Simanto"

# ================= SUPABASE INIT =================
try:
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
    supabase: Client = create_client(supabase_url, supabase_key)
    logger.info("✅ Supabase initialized successfully")
except Exception as e:
    logger.error(f"❌ Failed to initialize Supabase: {str(e)}")
    supabase = None

app = Flask(__name__)
_page_to_client_cache = {}
_product_cache = {}
_user_memory_cache = {}

# ================= MEMORY SYSTEM =================
class UserMemory:
    """User conversation memory management"""
    
    def __init__(self, admin_id: str, customer_id: str):
        self.admin_id = admin_id
        self.customer_id = customer_id
        self.memory_key = f"memory_{admin_id}_{customer_id}"
        
    def get_memory(self) -> Dict:
        """Get user memory from cache or database"""
        if self.memory_key in _user_memory_cache:
            return _user_memory_cache[self.memory_key]
        
        try:
            response = supabase.table("user_memories")\
                .select("*")\
                .eq("admin_id", self.admin_id)\
                .eq("customer_id", self.customer_id)\
                .execute()
            
            if response.data:
                memory = response.data[0]
                _user_memory_cache[self.memory_key] = {
                    "name": memory.get("customer_name", ""),
                    "preferences": memory.get("preferences", {}),
                    "last_interaction": memory.get("last_interaction", ""),
                    "conversation_summary": memory.get("conversation_summary", ""),
                    "products_viewed": memory.get("products_viewed", []),
                    "created_at": memory.get("created_at")
                }
                return _user_memory_cache[self.memory_key]
            
            # Create new memory if doesn't exist
            new_memory = {
                "name": "",
                "preferences": {},
                "last_interaction": datetime.utcnow().isoformat(),
                "conversation_summary": "",
                "products_viewed": []
            }
            _user_memory_cache[self.memory_key] = new_memory
            return new_memory
            
        except Exception as e:
            logger.error(f"Get memory error: {str(e)}")
            return {
                "name": "",
                "preferences": {},
                "last_interaction": datetime.utcnow().isoformat(),
                "conversation_summary": "",
                "products_viewed": []
            }
    
    def update_memory(self, user_message: str, bot_response: str, mentioned_products: List[str] = None):
        """Update user memory with new interaction"""
        try:
            memory = self.get_memory()
            
            # Update last interaction
            memory["last_interaction"] = datetime.utcnow().isoformat()
            
            # Update products viewed
            if mentioned_products:
                for product in mentioned_products:
                    if product not in memory["products_viewed"]:
                        memory["products_viewed"].append(product)
                
                # Keep only last 10 products
                if len(memory["products_viewed"]) > 10:
                    memory["products_viewed"] = memory["products_viewed"][-10:]
            
            # Extract potential name from conversation - FIXED
            if not memory["name"]:
                name_patterns = [
                    r"আমার নাম (\w+)",
                    r"name is (\w+)",
                    r"ami (\w+)",
                    r"I am (\w+)"
                ]
                for pattern in name_patterns:
                    matches = re.findall(pattern, user_message, re.IGNORECASE)
                    if matches:
                        memory["name"] = matches[0]
                        break
            
            # Update conversation summary (simplified)
            interaction = f"User: {user_message[:100]} | Bot: {bot_response[:100]}"
            if len(memory["conversation_summary"]) > 500:
                memory["conversation_summary"] = memory["conversation_summary"][-500:] + "\n" + interaction
            else:
                if memory["conversation_summary"]:
                    memory["conversation_summary"] += "\n" + interaction
                else:
                    memory["conversation_summary"] = interaction
            
            # Save to cache
            _user_memory_cache[self.memory_key] = memory
            
            # Save to database
            supabase.table("user_memories").upsert({
                "admin_id": self.admin_id,
                "customer_id": self.customer_id,
                "customer_name": memory["name"],
                "preferences": memory["preferences"],
                "last_interaction": memory["last_interaction"],
                "conversation_summary": memory["conversation_summary"],
                "products_viewed": memory["products_viewed"],
                "updated_at": datetime.utcnow().isoformat()
            }).execute()
            
        except Exception as e:
            logger.error(f"Update memory error: {str(e)}")
    
    def get_memory_context(self) -> str:
        """Get memory context for AI prompt"""
        memory = self.get_memory()
        context = ""
        
        if memory["name"]:
            context += f"গ্রাহকের নাম: {memory['name']}\n"
        
        if memory["products_viewed"]:
            context += f"পূর্বে দেখা পণ্য: {', '.join(memory['products_viewed'][-3:])}\n"
        
        if memory["conversation_summary"]:
            # Get last 3 interactions
            lines = memory["conversation_summary"].strip().split('\n')
            last_interactions = lines[-3:] if len(lines) > 3 else lines
            if last_interactions:
                context += "সাম্প্রতিক কথোপকথন:\n"
                for line in last_interactions:
                    context += f"- {line}\n"
        
        return context.strip()

# ================= HELPER FUNCTIONS =================
def find_client_by_page_id(page_id: str) -> Optional[Dict]:
    """Page ID থেকে user খুঁজে বের করো"""
    page_id_str = str(page_id)
    
    if page_id_str in _page_to_client_cache:
        return _page_to_client_cache[page_id_str]
    
    try:
        response = supabase.table("facebook_integrations")\
            .select("*")\
            .eq("page_id", page_id_str)\
            .eq("is_connected", True)\
            .execute()
        
        if response.data:
            admin_id = str(response.data[0]["user_id"])
            cached_data = {
                "admin_id": admin_id,
                "page_info": response.data[0]
            }
            _page_to_client_cache[page_id_str] = cached_data
            return cached_data
    except Exception as e:
        logger.error(f"Find client error: {str(e)}")
    
    return None

def get_facebook_token(admin_id: str) -> Optional[str]:
    try:
        response = supabase.table("facebook_integrations")\
            .select("page_access_token")\
            .eq("user_id", admin_id)\
            .eq("is_connected", True)\
            .execute()
        return response.data[0]["page_access_token"] if response.data else None
    except Exception as e:
        logger.error(f"Get Facebook token error: {str(e)}")
        return None

def get_groq_key(admin_id: str) -> Optional[str]:
    try:
        response = supabase.table("api_keys")\
            .select("gemini_api_key")\
            .eq("user_id", admin_id)\
            .execute()
        return response.data[0]["gemini_api_key"] if response.data else None
    except Exception as e:
        logger.error(f"Get Groq key error: {str(e)}")
        return None

def get_products(admin_id: str) -> List[Dict]:
    """প্রোডাক্টের তালিকা আনো"""
    cache_key = f"products_{admin_id}"
    
    if cache_key in _product_cache:
        return _product_cache[cache_key]
    
    try:
        response = supabase.table("products")\
            .select("*")\
            .eq("user_id", admin_id)\
            .order("created_at", desc=True)\
            .execute()
        
        products = response.data if response.data else []
        
        formatted_products = []
        for product in products:
            formatted_products.append({
                "id": product.get("id"),
                "name": product.get("name", ""),
                "description": product.get("description", ""),
                "price": product.get("price", 0),
                "category": product.get("category", ""),
                "stock": product.get("stock", 0),
                "in_stock": product.get("in_stock", False)
            })
        
        _product_cache[cache_key] = formatted_products
        return formatted_products
        
    except Exception as e:
        logger.error(f"Get products error: {str(e)}")
        return []

def detect_language(text: str) -> str:
    """ভাষা ডিটেক্ট করো - FIXED VERSION"""
    if not text or not isinstance(text, str):
        return 'bangla'
    
    text_lower = text.lower().strip()
    if not text_lower:
        return 'bangla'
    
    # বাংলা ইউনিকোড রেঞ্জ
    bangla_pattern = re.compile(r'[\u0980-\u09FF]')
    has_bangla = bool(bangla_pattern.search(text))
    
    if has_bangla:
        return 'bangla'
    
    # Check for Banglish keywords
    banglish_keywords = ['ki', 'kemon', 'achen', 'acha', 'valo', 'kothay', 'kot', 'dam', 'ase', 'nei']
    if any(keyword in text_lower for keyword in banglish_keywords):
        return 'bangla'
    
    # Check for pure English - FIXED
    # Remove punctuation and split
    clean_text = re.sub(r'[^\w\s]', '', text_lower)
    english_words = [word for word in clean_text.split() if word]
    
    if len(english_words) >= 2:
        # Check if most words are English
        english_word_count = sum(1 for word in english_words if re.match(r'^[a-z]+$', word))
        if english_word_count / len(english_words) > 0.7:  # 70% English words
            return 'english'
    
    return 'bangla'

def extract_mentioned_products(text: str, products: List[Dict]) -> List[str]:
    """Extract mentioned product names from text"""
    mentioned = []
    if not text or not products:
        return mentioned
    
    text_lower = text.lower()
    
    for product in products:
        product_name = product.get("name", "")
        if not product_name:
            continue
            
        product_name_lower = product_name.lower()
        if product_name_lower in text_lower:
            mentioned.append(product_name)
        else:
            # Check partial matches
            product_words = product_name_lower.split()
            for word in product_words:
                if len(word) > 3 and word in text_lower:
                    mentioned.append(product_name)
                    break
    
    return mentioned

def send_facebook_message(page_token: str, customer_id: str, message_text: str):
    """Facebook-এ মেসেজ পাঠাও"""
    try:
        url = f"https://graph.facebook.com/v18.0/me/messages?access_token={page_token}"
        payload = {
            "recipient": {"id": customer_id},
            "message": {"text": message_text},
            "messaging_type": "RESPONSE"
        }
        response = requests.post(url, json=payload, timeout=30)
        
        if response.status_code != 200:
            logger.error(f"Facebook API error: {response.status_code} - {response.text}")
        else:
            logger.info(f"✅ Message sent to {customer_id[:10]}...")
            
    except Exception as e:
        logger.error(f"❌ Send message error: {str(e)}")

def typing_on(token: str, recipient_id: str) -> bool:
    """Typing indicator চালু করো"""
    try:
        url = f"https://graph.facebook.com/v18.0/me/messages?access_token={token}"
        payload = {
            "recipient": {"id": recipient_id},
            "sender_action": "typing_on"
        }
        response = requests.post(url, json=payload, timeout=5)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Typing on error: {str(e)}")
        return False

def typing_off(token: str, recipient_id: str) -> bool:
    """Typing indicator বন্ধ করো"""
    try:
        url = f"https://graph.facebook.com/v18.0/me/messages?access_token={token}"
        payload = {
            "recipient": {"id": recipient_id},
            "sender_action": "typing_off"
        }
        response = requests.post(url, json=payload, timeout=5)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Typing off error: {str(e)}")
        return False

# ================= AI RESPONSE WITH MEMORY =================
def generate_ai_response(admin_id: str, user_message: str, customer_id: str, page_name: str = "আমাদের ব্যবসা") -> str:
    try:
        # API key চেক
        api_key = get_groq_key(admin_id)
        if not api_key:
            logger.error(f"No API key for admin: {admin_id}")
            return "আপনার AI সার্ভিস কনফিগার করা হয়নি। অনুগ্রহ করে পৃষ্ঠা প্রশাসকের সাথে যোগাযোগ করুন।"
        
        # Groq client
        client = OpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=api_key
        )
        
        # ভাষা ডিটেক্ট
        language = detect_language(user_message)
        
        # Products লোড করি
        products = get_products(admin_id)
        
        # User memory লোড করি
        memory = UserMemory(admin_id, customer_id)
        memory_context = memory.get_memory_context()
        
        # Mentioned products extract করি
        mentioned_products = extract_mentioned_products(user_message, products)
        
        # Product context তৈরি
        product_context = ""
        if mentioned_products:
            product_context = "উল্লেখিত পণ্য:\n"
            for product_name in mentioned_products[:2]:
                # Find product details
                for prod in products:
                    if prod.get("name", "").lower() == product_name.lower():
                        price = prod.get("price", 0)
                        stock = prod.get("stock", 0)
                        in_stock = prod.get("in_stock", False)
                        status = "স্টকে আছে" if in_stock else "স্টকে নেই"
                        product_context += f"- {product_name}: ৳{price:,} ({status})\n"
                        break
        
        # Conversation history লোড করি (last 5 messages)
        try:
            response = supabase.table("chat_history")\
                .select("messages")\
                .eq("user_id", admin_id)\
                .eq("customer_id", customer_id)\
                .execute()
            
            if response.data and response.data[0].get("messages"):
                history = response.data[0]["messages"]
            else:
                history = []
        except Exception as e:
            logger.error(f"Load history error: {str(e)}")
            history = []
        
        recent_history = history[-5:] if len(history) > 5 else history
        
        # History context তৈরি
        history_context = ""
        if recent_history:
            history_context = "সাম্প্রতিক কথোপকথন:\n"
            for msg in recent_history[-3:]:
                user_msg = msg.get('user', '')
                bot_msg = msg.get('bot', '')
                if user_msg:
                    history_context += f"গ্রাহক: {user_msg[:50]}...\n"
                if bot_msg:
                    history_context += f"আপনি: {bot_msg[:50]}...\n"
        
        # সিস্টেম প্রম্পট
        if language == 'bangla':
            system_prompt = f"""তুমি {BOT_NAME}, {page_name}-এর একজন বন্ধুত্বপূর্ণ কিন্তু প্রফেশনাল সহকারী। তোমার বৈশিষ্ট্য:

১. **সম্মানজনক বাংলায় কথা বলো** - "আপনি/আপনার" ব্যবহার করো
২. **গ্রাহকের কথা মনে রাখো** - পূর্বের আলোচনা সম্পর্কে জানো
৩. **সংক্ষিপ্ত ও স্পষ্ট উত্তর দাও** - ২-৩ লাইনের বেশি না
৪. **প্রফেশনাল কিন্তু উষ্ণ** - অতিরিক্ত আনুষ্ঠানিক না
৫. **প্রাসঙ্গিক উত্তর দাও** - গ্রাহকের পূর্ববর্তী কথার সাথে সম্পর্ক রাখো
৬. **যদি জানো না, সৎভাবে বলো** - "এ বিষয়ে আমার জানা নেই"
৭. **পণ্য সম্পর্কে জানলে নির্ভুল তথ্য দাও**

**গ্রাহকের তথ্য:**
{memory_context if memory_context else 'নতুন গ্রাহক'}

**পণ্য তথ্য:**
{product_context if product_context else 'নির্দিষ্ট পণ্য উল্লেখ নেই'}

**কথোপকথন ইতিহাস:**
{history_context if history_context else 'শুরুতে নতুন আলাপ'}

গ্রাহকের বর্তমান প্রশ্ন: "{user_message}"

তুমি (সম্মানজনক, প্রাসঙ্গিক, সংক্ষিপ্ত উত্তর):"""
        else:
            system_prompt = f"""You are {BOT_NAME}, a friendly but professional assistant for {page_name}. Your characteristics:

1. **Speak respectfully in English** - Use professional but warm tone
2. **Remember customer context** - Recall previous conversations
3. **Give concise answers** - 2-3 lines maximum
4. **Be professional but approachable** - Not overly formal
5. **Stay relevant** - Connect to customer's previous messages
6. **If you don't know, admit it** - "I don't have information about that"
7. **Provide accurate product information when available**

**Customer Information:**
{memory_context if memory_context else 'New customer'}

**Product Information:**
{product_context if product_context else 'No specific products mentioned'}

**Conversation History:**
{history_context if history_context else 'Beginning new conversation'}

Customer's current question: "{user_message}"

You (respectful, relevant, concise response):"""
        
        # Messages প্রস্তুত
        messages = [{"role": "system", "content": system_prompt}]
        
        # Recent history যোগ করি
        for msg in recent_history:
            if msg.get('user'):
                messages.append({"role": "user", "content": msg['user']})
            if msg.get('bot'):
                messages.append({"role": "assistant", "content": msg['bot']})
        
        messages.append({"role": "user", "content": user_message})
        
        # AI call
        try:
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages,
                temperature=0.7,
                max_tokens=200,
                top_p=0.9
            )
            
            ai_response = response.choices[0].message.content.strip()
            logger.info(f"✅ AI Response ({len(ai_response)} chars)")
            
        except Exception as e:
            logger.error(f"Groq API error: {str(e)}")
            ai_response = "দুঃখিত, এখন উত্তর দিতে পারছি না। অনুগ্রহ করে আবার চেষ্টা করুন।"
        
        # Memory update
        memory.update_memory(user_message, ai_response, mentioned_products)
        
        # Chat history save
        history.append({
            "user": user_message,
            "bot": ai_response,
            "timestamp": datetime.utcnow().isoformat()
        })
        
        # Keep only last 20 messages
        if len(history) > 20:
            history = history[-20:]
        
        supabase.table("chat_history").upsert({
            "user_id": admin_id,
            "customer_id": customer_id,
            "messages": history,
            "last_updated": datetime.utcnow().isoformat()
        }).execute()
        
        return ai_response
        
    except Exception as e:
        logger.error(f"❌ AI Response Error: {str(e)}\n{traceback.format_exc()}")
        return "দুঃখিত, কিছু সমস্যা হয়েছে। অনুগ্রহ করে আবার চেষ্টা করুন।"

# ================= WEBHOOK ROUTES =================
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Facebook webhook verification"""
    try:
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        
        logger.info(f"🔐 Verification attempt: mode={mode}, token={token[:20] if token else None}")
        
        if mode and token:
            response = supabase.table("facebook_integrations")\
                .select("*")\
                .eq("verify_token", token)\
                .execute()
            
            if response.data:
                page_name = response.data[0].get('page_name', 'Unknown')
                logger.info(f"✅ Verification successful for page: {page_name}")
                return challenge, 200
            else:
                logger.warning(f"❌ Invalid verify token")
                return jsonify({"error": "Invalid verify token"}), 403
        else:
            logger.warning("Missing verification parameters")
            return jsonify({"error": "Missing parameters"}), 400
            
    except Exception as e:
        logger.error(f"❌ Verification error: {str(e)}")
        return jsonify({"error": "Server error"}), 500

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    """Handle incoming Facebook messages"""
    try:
        data = request.get_json()
        
        if not data:
            logger.warning("Empty webhook data")
            return jsonify({"status": "no_data"}), 200
        
        entries = data.get('entry', [])
        
        for entry in entries:
            page_id = entry.get('id')
            messaging_events = entry.get('messaging', [])
            
            for event in messaging_events:
                sender_id = event.get('sender', {}).get('id')
                recipient_id = event.get('recipient', {}).get('id')
                
                if not sender_id or not recipient_id:
                    continue
                
                # Message event
                if 'message' in event and 'text' in event['message']:
                    message_text = event['message']['text']
                    
                    if not message_text or message_text.strip() == '':
                        continue
                    
                    logger.info(f"💬 Message from {sender_id[:10]}...: {message_text[:100]}")
                    
                    # Find client
                    client_info = find_client_by_page_id(recipient_id)
                    
                    if client_info:
                        admin_id = client_info["admin_id"]
                        page_info = client_info["page_info"]
                        page_name = page_info.get("page_name", "আমাদের ব্যবসা")
                        page_token = page_info.get("page_access_token")
                        
                        if page_token:
                            # Typing indicator
                            typing_on(page_token, sender_id)
                            
                            # Generate response with memory
                            ai_response = generate_ai_response(admin_id, message_text, sender_id, page_name)
                            
                            # Stop typing
                            typing_off(page_token, sender_id)
                            
                            # Send response
                            send_facebook_message(page_token, sender_id, ai_response)
                            
                            logger.info(f"✅ Response sent ({len(ai_response)} chars)")
                        else:
                            logger.error(f"❌ No page token for admin {admin_id}")
                    else:
                        logger.error(f"❌ No client found for page {recipient_id}")
        
        return jsonify({"status": "processed"}), 200
        
    except Exception as e:
        logger.error(f"❌ Webhook processing error: {str(e)}\n{traceback.format_exc()}")
        return jsonify({"error": "processing_error"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"🚀 Starting Facebook AI Bot '{BOT_NAME}' on port {port}")
    logger.info(f"🧠 Memory system enabled")
    logger.info(f"🤖 Using Groq API with Llama 3.3 70B")
    app.run(host="0.0.0.0", port=port, debug=False)

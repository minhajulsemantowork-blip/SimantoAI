import os
import json
import requests
import logging
import traceback
import random
import re
from datetime import datetime
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

def get_products(admin_id: str, force_refresh: bool = False) -> List[Dict]:
    """প্রোডাক্টের তালিকা আনো"""
    cache_key = f"products_{admin_id}"
    
    # Cache check
    if not force_refresh and cache_key in _product_cache:
        return _product_cache[cache_key]
    
    try:
        response = supabase.table("products")\
            .select("*")\
            .eq("user_id", admin_id)\
            .order("created_at", desc=True)\
            .execute()
        
        products = response.data if response.data else []
        
        # Format products for easier use
        formatted_products = []
        for product in products:
            formatted_products.append({
                "id": product.get("id"),
                "name": product.get("name", ""),
                "description": product.get("description", ""),
                "price": product.get("price", 0),
                "category": product.get("category", ""),
                "image_url": product.get("image_url", ""),
                "stock": product.get("stock", 0),
                "in_stock": product.get("in_stock", False),
                "keywords": f"{product.get('name', '')} {product.get('category', '')}"
            })
        
        # Cache the results
        _product_cache[cache_key] = formatted_products
        logger.info(f"📦 Loaded {len(formatted_products)} products for admin {admin_id}")
        return formatted_products
        
    except Exception as e:
        logger.error(f"Get products error: {str(e)}")
        return []

def search_products(admin_id: str, query: str, category: str = None) -> List[Dict]:
    """প্রোডাক্ট সার্চ করো"""
    products = get_products(admin_id)
    
    if not query:
        return []
    
    query_lower = query.lower().strip()
    results = []
    
    for product in products:
        score = 0
        
        # Name match (highest priority)
        name = product.get("name", "").lower()
        if query_lower in name:
            score += 10
        elif any(word in name for word in query_lower.split()):
            score += 5
        
        # Description match
        description = product.get("description", "").lower()
        if query_lower in description:
            score += 3
        
        # Category match
        product_category = product.get("category", "").lower()
        if category and category.lower() in product_category:
            score += 2
        elif query_lower in product_category:
            score += 2
        
        # Keywords match
        keywords = product.get("keywords", "").lower()
        if query_lower in keywords:
            score += 1
        
        if score > 0:
            product["relevance_score"] = score
            results.append(product)
    
    # Sort by relevance score
    results.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    return results[:3]  # Return top 3 results

def get_product_details(admin_id: str, product_name: str) -> Optional[Dict]:
    """নির্দিষ্ট প্রোডাক্টের ডিটেইলস আনো"""
    products = get_products(admin_id)
    
    product_name_lower = product_name.lower().strip()
    
    for product in products:
        if product_name_lower in product.get("name", "").lower():
            return product
        
        # Partial match
        product_words = product.get("name", "").lower().split()
        query_words = product_name_lower.split()
        if any(word in product_words for word in query_words):
            return product
    
    return None

def format_product_info(product: Dict, language: str = "bangla") -> str:
    """প্রোডাক্টের ইনফো ফরম্যাট করো"""
    name = product.get("name", "Unknown Product")
    description = product.get("description", "")
    price = product.get("price", 0)
    category = product.get("category", "")
    stock = product.get("stock", 0)
    in_stock = product.get("in_stock", False)
    
    if language == "bangla":
        stock_status = "স্টকে আছে ✅" if in_stock else "স্টকে নেই ❌"
        price_text = f"৳{price:,.2f}" if price > 0 else "দাম জানানো হয়নি"
        
        info = f"🎯 {name}\n"
        info += f"📝 {description}\n" if description else ""
        info += f"💰 দাম: {price_text}\n"
        if category:
            info += f"🏷️ ক্যাটাগরি: {category}\n"
        info += f"📦 স্ট্যাটাস: {stock_status}"
        if in_stock and stock > 0:
            info += f" ({stock} পিস)"
        
        return info.strip()
    
    else:
        stock_status = "In stock ✅" if in_stock else "Out of stock ❌"
        price_text = f"৳{price:,.2f}" if price > 0 else "Price not available"
        
        info = f"🎯 {name}\n"
        info += f"📝 {description}\n" if description else ""
        info += f"💰 Price: {price_text}\n"
        if category:
            info += f"🏷️ Category: {category}\n"
        info += f"📦 Status: {stock_status}"
        if in_stock and stock > 0:
            info += f" ({stock} pieces)"
        
        return info.strip()

def get_faqs(admin_id: str) -> List[Dict]:
    """FAQ তালিকা আনো - faqs টেবিল থেকে"""
    try:
        # প্রথমে faqs টেবিল চেষ্টা করি
        try:
            response = supabase.table("faqs")\
                .select("*")\
                .eq("user_id", admin_id)\
                .order("created_at", desc=False)\
                .execute()
            
            if response.data:
                logger.info(f"✅ Found FAQs in 'faqs' table: {len(response.data)} items")
                return response.data
            
            # যদি faqs-ও না থাকে, targets টেবিল চেষ্টা করি
            response = supabase.table("targets")\
                .select("*")\
                .eq("user_id", admin_id)\
                .order("created_at", desc=False)\
                .execute()
            
            if response.data:
                logger.info(f"✅ Found FAQs in 'targets' table: {len(response.data)} items")
                return response.data
            
            return []
                
        except Exception as e:
            logger.error(f"FAQ fetch error (faqs/targets): {str(e)}")
            return []
            
    except Exception as e:
        logger.error(f"Overall FAQ fetch error: {str(e)}")
        return []

def load_chat_history(admin_id: str, customer_id: str) -> List[Dict]:
    try:
        response = supabase.table("chat_history")\
            .select("messages")\
            .eq("user_id", admin_id)\
            .eq("customer_id", customer_id)\
            .execute()
        
        if response.data and response.data[0].get("messages"):
            return response.data[0]["messages"]
        return []
    except Exception as e:
        logger.error(f"Chat history load error: {str(e)}")
        return []

def save_chat_history(admin_id: str, customer_id: str, history: List[Dict]):
    try:
        supabase.table("chat_history").upsert({
            "user_id": admin_id,
            "customer_id": customer_id,
            "messages": history,
            "last_updated": datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        logger.error(f"Chat history save error: {str(e)}")

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

def detect_language(text: str) -> str:
    """ভাষা ডিটেক্ট করো - Simplified version"""
    if not text or not text.strip():
        return 'english'
    
    # বাংলা ইউনিকোড রেঞ্জ
    bangla_pattern = re.compile(r'[\u0980-\u09FF]')
    has_bangla = bool(bangla_pattern.search(text))
    
    # বাংলা শব্দের লিস্ট (Banglish এর জন্য)
    bangla_keywords = [
        'আসসালামু', 'আলাইকুম', 'হ্যালো', 'হাই', 'কেমন', 'আছেন', 'আছো', 'আছে',
        'ধন্যবাদ', 'জি', 'না', 'হ্যাঁ', 'ঠিক', 'আচ্ছা', 'ওকে', 'তোমার', 'আপনার',
        'কি', 'কেন', 'কখন', 'কোথায়', 'কিভাবে', 'কত', 'দাম', 'স্টক', 'আছে',
        'নেই', 'পণ্য', 'প্রোডাক্ট', 'অর্ডার', 'বুক', 'কিনব', 'কিনতে'
    ]
    
    # Banglish/English keywords
    banglish_keywords = [
        'ki', 'obostha', 'kemon', 'achen', 'acha', 'thik', 'acha', 'valo',
        'kothay', 'kot', 'dam', 'stock', 'ase', 'nei', 'order', 'korbo',
        'kinbo', 'kichu', 'jan', 'chai', 'bol', 'paro', 'help', 'dorkar'
    ]
    
    text_lower = text.lower()
    
    # যদি বাংলা অক্ষর থাকে
    if has_bangla:
        bangla_count = len(bangla_pattern.findall(text))
        bangla_ratio = bangla_count / len(text)
        
        # যদি ৫০%+ বাংলা অক্ষর থাকে
        if bangla_ratio > 0.5:
            return 'bangla'
        else:
            return 'banglish'
    
    # যদি বাংলা/বাংলিশ keywords থাকে
    if any(keyword in text_lower for keyword in bangla_keywords + banglish_keywords):
        return 'banglish'
    
    # Check if it's pure English sentence
    words = text_lower.split()
    if len(words) > 3 and all(re.match(r'^[a-z\s\.,!?]+$', text_lower)):
        return 'english'
    
    # Default to banglish for mixed or unknown
    return 'banglish'

def get_contextual_info(admin_id: str, user_message: str) -> Dict:
    """ইউজার মেসেজ থেকে প্রাসঙ্গিক তথ্য সংগ্রহ করো"""
    result = {
        "products": [],
        "faqs": [],
        "intent": "general"
    }
    
    user_lower = user_message.lower().strip()
    
    if not user_lower:
        return result
    
    # Detect intent
    product_keywords = ['প্রোডাক্ট', 'পণ্য', 'দাম', 'price', 'product', 'কত', 'কোথায়', 'কি']
    order_keywords = ['অর্ডার', 'order', 'ক্রয়', 'কিনব', 'কিনতে', 'buy']
    
    if any(keyword in user_lower for keyword in product_keywords):
        result["intent"] = "product_inquiry"
    elif any(keyword in user_lower for keyword in order_keywords):
        result["intent"] = "order_inquiry"
    
    # Search for products
    result["products"] = search_products(admin_id, user_lower)
    
    # Get FAQs
    faqs = get_faqs(admin_id)
    for faq in faqs:
        question = faq.get("question", "").lower()
        if any(word in user_lower for word in question.split()[:3]):
            result["faqs"].append(faq)
    
    return result

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

# ================= AI RESPONSE (GROQ POWERED) =================
def generate_ai_response(admin_id: str, user_message: str, customer_id: str, page_name: str = "আমাদের ব্যবসা") -> str:
    try:
        # API key চেক
        api_key = get_groq_key(admin_id)
        if not api_key:
            logger.error(f"No API key for admin: {admin_id}")
            return "⚠️ AI service is not configured yet. Please contact the page admin."
        
        # Groq client
        client = OpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=api_key
        )
        
        # ভাষা ডিটেক্ট (সিম্পল ভার্সন)
        language = detect_language(user_message)
        
        # প্রাসঙ্গিক তথ্য সংগ্রহ
        context_info = get_contextual_info(admin_id, user_message)
        products = context_info["products"]
        faqs = context_info["faqs"]
        
        # চ্যাট হিস্ট্রি
        history = load_chat_history(admin_id, customer_id)
        
        # প্রোডাক্ট কনটেক্সট তৈরি
        product_context = ""
        if products:
            if language in ["bangla", "banglish"]:
                product_context = "পণ্য:\n"
                for i, product in enumerate(products[:2], 1):
                    product_context += f"{i}. {format_product_info(product, 'bangla')}\n"
            else:
                product_context = "Products:\n"
                for i, product in enumerate(products[:2], 1):
                    product_context += f"{i}. {format_product_info(product, 'english')}\n"
        
        # FAQ কনটেক্সট
        faq_context = ""
        if faqs:
            if language in ["bangla", "banglish"]:
                faq_context = "তথ্য:\n"
                for faq in faqs[:1]:
                    faq_context += f"প্র: {faq.get('question', '')}\nউ: {faq.get('answer', '')}\n"
            else:
                faq_context = "Info:\n"
                for faq in faqs[:1]:
                    faq_context += f"Q: {faq.get('question', '')}\nA: {faq.get('answer', '')}\n"
        
        # সিস্টেম প্রম্পট (ভাষা অনুযায়ী)
        if language in ["bangla", "banglish"]:
            system_prompt = f"""তুমি {BOT_NAME}, {page_name}-এর বন্ধুত্বপূর্ণ সহকারী। নিয়ম:
1. **সবসময় বাংলায় উত্তর দেবে** (Banglish থাকলেও)
2. **সংক্ষিপ্ত এবং স্পষ্ট উত্তর** (max 2-3 লাইন)
3. **বন্ধুত্বপূর্ণ কিন্তু formal না** ("তুমি" ব্যবহার করো)
4. **যদি জানো না, সরাসরি বলো** "জানি না, অন্য কিছু জানতে চান?"
5. **ইমোজি ব্যবহার করো** 😊, 👍, 🙏
6. **কোনো ভাষা অনুবাদ করো না**
7. **টোকেন সেভ করতে সংক্ষিপ্ত কথা বলো**

পণ্য তথ্য:
{product_context if product_context else 'কোন পণ্য না'}

অন্যান্য তথ্য:
{faq_context if faq_context else ''}

গ্রাহক: "{user_message}"
তুমি (সংক্ষিপ্ত, বাংলায়, বন্ধুত্বপূর্ণ উত্তর):"""
            
        else:
            # শুধু pure English এর জন্য English উত্তর
            system_prompt = f"""You are {BOT_NAME}, friendly assistant of {page_name}. Rules:
1. **Respond in English only if customer writes full English sentences**
2. **Keep responses short and clear** (max 2-3 lines)
3. **Be friendly but not overly formal**
4. **If you don't know, say "I don't know, can I help with something else?"**
5. **Use emojis sometimes** 😊, 👍
6. **Save tokens - be concise**

Product info:
{product_context if product_context else 'No products'}

Other info:
{faq_context if faq_context else ''}

Customer: "{user_message}"
You (short, friendly response):"""
        
        # মেসেজেস প্রস্তুত (শুধু সাম্প্রতিক ২টি)
        messages = [{"role": "system", "content": system_prompt}]
        
        # সাম্প্রতিক হিস্ট্রি (শেষ ২টি)
        for msg in history[-2:]:
            if msg.get('user'):
                messages.append({"role": "user", "content": msg['user']})
            if msg.get('bot'):
                messages.append({"role": "assistant", "content": msg['bot']})
        
        messages.append({"role": "user", "content": user_message})
        
        # AI কল
        try:
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages,
                temperature=0.7,
                max_tokens=150,  # কম টোকেন
                top_p=0.9
            )
            
            ai_response = response.choices[0].message.content.strip()
            logger.info(f"✅ AI Response ({len(ai_response)} chars)")
            
        except Exception as e:
            logger.error(f"Groq API error: {str(e)}")
            # Fallback response
            if language in ["bangla", "banglish"]:
                ai_response = "দুঃখিত, সমস্যা হয়েছে। আবার চেষ্টা করুন? 😊"
            else:
                ai_response = "Sorry, having trouble. Try again? 😊"
        
        # Add simple human touch
        if language in ["bangla", "banglish"]:
            if random.random() < 0.2:
                ai_response += " 😊"
        else:
            if random.random() < 0.2:
                ai_response += " 👍"
        
        # হিস্ট্রি সেভ
        history.append({
            "user": user_message,
            "bot": ai_response,
            "timestamp": datetime.utcnow().isoformat(),
            "language": language
        })
        
        if len(history) > 15:
            history = history[-15:]
        
        save_chat_history(admin_id, customer_id, history)
        
        return ai_response
        
    except Exception as e:
        logger.error(f"❌ AI Response Error: {str(e)}\n{traceback.format_exc()}")
        
        # Simple error message
        if detect_language(user_message) in ["bangla", "banglish"]:
            return "দুঃখিত, সমস্যা হয়েছে। 😊"
        else:
            return "Sorry, something went wrong. 😊"

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
                            
                            # Generate response
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
    logger.info(f"📦 Product integration enabled")
    logger.info(f"🤖 Using Groq API with Llama 3.3 70B")
    app.run(host="0.0.0.0", port=port, debug=False)

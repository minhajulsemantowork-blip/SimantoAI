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
from langdetect import detect, DetectorFactory
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
    return results[:5]  # Return top 5 results

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
        # বাংলা ফরম্যাট
        stock_status = "স্টকে আছে ✅" if in_stock else "স্টকে নেই ❌"
        price_text = f"৳{price:,.2f}" if price > 0 else "দাম জানানো হয়নি"
        
        info = f"🎯 **{name}**\n"
        info += f"📝 {description}\n\n" if description else ""
        info += f"💰 দাম: {price_text}\n"
        info += f"🏷️ ক্যাটাগরি: {category}\n" if category else ""
        info += f"📦 স্ট্যাটাস: {stock_status}"
        if in_stock and stock > 0:
            info += f" ({stock} পিস)\n"
        else:
            info += "\n"
        
        return info.strip()
    
    elif language == "banglish":
        # বাংলিশ ফরম্যাট
        stock_status = "In stock ✅" if in_stock else "Out of stock ❌"
        price_text = f"৳{price:,.2f}" if price > 0 else "Price not set"
        
        info = f"🎯 **{name}**\n"
        info += f"📝 {description}\n\n" if description else ""
        info += f"💰 Price: {price_text}\n"
        info += f"🏷️ Category: {category}\n" if category else ""
        info += f"📦 Status: {stock_status}"
        if in_stock and stock > 0:
            info += f" ({stock} pcs)\n"
        else:
            info += "\n"
        
        return info.strip()
    
    else:
        # English format
        stock_status = "In stock ✅" if in_stock else "Out of stock ❌"
        price_text = f"৳{price:,.2f}" if price > 0 else "Price not available"
        
        info = f"🎯 **{name}**\n"
        info += f"📝 {description}\n\n" if description else ""
        info += f"💰 Price: {price_text}\n"
        info += f"🏷️ Category: {category}\n" if category else ""
        info += f"📦 Status: {stock_status}"
        if in_stock and stock > 0:
            info += f" ({stock} pieces available)\n"
        else:
            info += "\n"
        
        return info.strip()

def get_page_faqs(admin_id: str) -> List[Dict]:
    """পেজের জন্য সংরক্ষিত FAQ/Products ডাটা আনো"""
    try:
        response = supabase.table("page_faqs")\
            .select("*")\
            .eq("user_id", admin_id)\
            .order("created_at", desc=False)\
            .execute()
        return response.data if response.data else []
    except Exception as e:
        logger.error(f"FAQ fetch error: {str(e)}")
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

def detect_language_and_style(text: str) -> tuple:
    """ভাষা এবং লেভেল (formal/informal) ডিটেক্ট করো"""
    try:
        if not text or not text.strip():
            return 'english', 'friendly'
        
        # Set seed for consistency
        DetectorFactory.seed = 0
        
        # Check for Bangla characters
        bangla_pattern = re.compile(r'[\u0980-\u09FF]')
        has_bangla = bool(bangla_pattern.search(text))
        
        if has_bangla:
            # Count Bangla characters percentage
            bangla_chars = len(bangla_pattern.findall(text))
            bangla_ratio = bangla_chars / len(text)
            
            if bangla_ratio > 0.7:
                return 'bangla', 'informal'
            else:
                return 'banglish', 'informal'
        else:
            # Try langdetect for non-Bangla text
            try:
                lang = detect(text)
                if lang == 'bn':
                    return 'bangla', 'informal'
                else:
                    return 'english', 'friendly'
            except:
                return 'english', 'friendly'
                
    except Exception as e:
        logger.error(f"Language detection error: {str(e)}")
        return 'english', 'friendly'

def get_contextual_info(admin_id: str, user_message: str) -> Dict:
    """ইউজার মেসেজ থেকে প্রাসঙ্গিক তথ্য সংগ্রহ করো"""
    result = {
        "products": [],
        "faqs": [],
        "categories": set(),
        "intent": "general"
    }
    
    user_lower = user_message.lower().strip()
    
    if not user_lower:
        return result
    
    # Detect intent based on keywords
    product_keywords = ['প্রোডাক্ট', 'পণ্য', 'দাম', 'price', 'product', 'কত', 'কোথায়', 'কি', 'জিনিস', 'বস্তু']
    category_keywords = ['ক্যাটাগরি', 'ধরন', 'টাইপ', 'category', 'type', 'ধরণ']
    stock_keywords = ['স্টক', 'আছে', 'নেই', 'stock', 'available', 'পাওয়া']
    
    if any(keyword in user_lower for keyword in product_keywords):
        result["intent"] = "product_inquiry"
    
    # Search for products
    result["products"] = search_products(admin_id, user_lower)
    
    # Extract possible categories
    products = get_products(admin_id)
    for product in products:
        category = product.get("category", "").lower()
        if category and category in user_lower:
            result["categories"].add(category)
    
    # Get FAQs
    faqs = get_page_faqs(admin_id)
    for faq in faqs:
        question = faq.get("question", "").lower()
        keywords = faq.get("keywords", "").lower()
        
        if any(word in user_lower for word in question.split()[:3]):
            result["faqs"].append(faq)
        elif keywords and any(keyword in user_lower for keyword in keywords.split(',')):
            result["faqs"].append(faq)
    
    return result

def add_human_touches(response: str, language: str, style: str) -> str:
    """রেসপন্সে হিউম্যান টাচ যোগ করো"""
    if not response:
        return response
    
    rand_val = random.random()
    
    if language == 'bangla':
        greetings = ["হ্যালো!", "আসসালামু আলাইকুম!", "শুভেচ্ছা!", "কেমন আছেন?"]
        emotions = [" 😊", " 👍", " 🙏", " 😃", " 💫"]
        closings = [
            "আর কোন প্রশ্ন থাকলে বলবেন!",
            "আর কিছু জানতে চাইলে বলুন।",
            "আপনার দিনটি শুভ হোক!",
            "ধন্যবাদ! 😊",
            "সবসময় আপনার সেবায় আছি।"
        ]
        
        if rand_val < 0.3:
            response = f"{random.choice(greetings)} {response}"
        if rand_val < 0.25:
            response += random.choice(emotions)
        if rand_val < 0.4:
            response += f" {random.choice(closings)}"
            
    elif language == 'banglish':
        greetings = ["Hello!", "Hi!", "Ki obostha?", "Kemon achen?"]
        emotions = [" 😊", " 👍", " 🙏", " 😄"]
        closings = [
            "Aro kichu jante chan?",
            "Aro question thakle bolben.",
            "Thank you! 😊",
            "Apnar din valo katuk."
        ]
        
        if rand_val < 0.3:
            response = f"{random.choice(greetings)} {response}"
        if rand_val < 0.25:
            response += random.choice(emotions)
        if rand_val < 0.4:
            response += f" {random.choice(closings)}"
            
    else:
        greetings = ["Hello!", "Hi there!", "Greetings!", "Hey!", "How can I help?"]
        emotions = [" 😊", " 👍", " 😄", " 🙌", " 💫"]
        closings = [
            "Let me know if you have more questions!",
            "Feel free to ask anything else!",
            "Have a great day!",
            "Thanks! 😊",
            "I'm here if you need anything!"
        ]
        
        if rand_val < 0.3:
            response = f"{random.choice(greetings)} {response}"
        if rand_val < 0.25:
            response += random.choice(emotions)
        if rand_val < 0.4:
            response += f" {random.choice(closings)}"
    
    return response.strip()

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
        
        # ভাষা ডিটেক্ট
        language, style = detect_language_and_style(user_message)
        
        # প্রাসঙ্গিক তথ্য সংগ্রহ
        context_info = get_contextual_info(admin_id, user_message)
        products = context_info["products"]
        faqs = context_info["faqs"]
        intent = context_info["intent"]
        
        # চ্যাট হিস্ট্রি
        history = load_chat_history(admin_id, customer_id)
        
        # প্রোডাক্ট কনটেক্সট তৈরি
        product_context = ""
        if products:
            if language == "bangla":
                product_context = "**প্রাসঙ্গিক পণ্য তালিকা:**\n\n"
                for i, product in enumerate(products[:3], 1):
                    product_context += f"{i}. {format_product_info(product, 'bangla')}\n\n"
            elif language == "banglish":
                product_context = "**Relevant products:**\n\n"
                for i, product in enumerate(products[:3], 1):
                    product_context += f"{i}. {format_product_info(product, 'banglish')}\n\n"
            else:
                product_context = "**Relevant products:**\n\n"
                for i, product in enumerate(products[:3], 1):
                    product_context += f"{i}. {format_product_info(product, 'english')}\n\n"
        
        # FAQ কনটেক্সট
        faq_context = ""
        if faqs:
            if language == "bangla":
                faq_context = "**প্রাসঙ্গিক তথ্য:**\n"
                for faq in faqs[:2]:
                    faq_context += f"প্রশ্ন: {faq.get('question', '')}\n"
                    faq_context += f"উত্তর: {faq.get('answer', '')}\n\n"
            else:
                faq_context = "**Relevant information:**\n"
                for faq in faqs[:2]:
                    faq_context += f"Q: {faq.get('question', '')}\n"
                    faq_context += f"A: {faq.get('answer', '')}\n\n"
        
        # সিস্টেম প্রম্পট (ভাষা অনুযায়ী)
        if language == 'bangla':
            system_prompt = f"""তুমি {BOT_NAME}, {page_name}-এর একজন বন্ধুত্বপূর্ণ সহকারী। তোমার বৈশিষ্ট্য:

১. **প্রাণবন্ত ও প্রাকৃতিক বাংলায় কথা বলো** - "তুমি/তোমার" ব্যবহার করো, খুবই বন্ধুত্বপূর্ণ হও
২. **পণ্য সম্পর্কে জানলে নির্ভুল তথ্য দাও** - নিচের তথ্য ব্যবহার করো
৩. **আন্তরিকতা দেখাও** - গ্রাহকের প্রশ্নের গুরুত্ব বুঝে উত্তর দাও
৪. **ইমোজি ব্যবহার করো** - 😊, 👍, 🙏 দিয়ে কথায় প্রাণ সঞ্চার করো
৫. **যদি জানো না, সৎ থাকো** - "এটা এখন আমার জানা নেই, কিন্তু খোঁজ নিতে পারি" বলো
৬. **স্পষ্ট ও সহজ ভাষা** - জটিলতা এড়িয়ে চলো

**পণ্য সম্পর্কে তথ্য (যদি থাকে):**
{product_context if product_context else 'কোন নির্দিষ্ট পণ্য মিলে নি। সাধারণভাবে সাহায্য করার চেষ্টা করো।'}

**অন্যান্য তথ্য:**
{faq_context if faq_context else 'আরও নির্দিষ্ট তথ্যের প্রয়োজন হলে বলুন।'}

গ্রাহকের প্রশ্ন: "{user_message}"
তুমি (বন্ধুত্বপূর্ণ, প্রাণবন্ত, সহায়ক উত্তর দেবে):"""
            
        elif language == 'banglish':
            system_prompt = f"""You are {BOT_NAME}, friendly assistant of {page_name}. Your style:

1. **Mix Bangla and English naturally** - Be very friendly
2. **Give accurate product info if available** - Use below information
3. **Show you care** - Understand customer needs
4. **Use emoticons** - 😊, 👍, 🙏 to express
5. **If don't know, be honest** - Say "I don't know but can find out"
6. **Keep it simple** - Easy to understand

**Product information (if available):**
{product_context if product_context else 'No specific product found. Help generally.'}

**Other information:**
{faq_context if faq_context else 'Ask for more specific info if needed.'}

Customer question: "{user_message}"
You (friendly, helpful response):"""
            
        else:
            system_prompt = f"""You are {BOT_NAME}, the warm and human-like assistant for {page_name}. Your personality:

1. **Exceptionally friendly and natural** - Talk like a real person, use contractions
2. **Show genuine empathy** - Acknowledge customer's feelings and needs
3. **Provide accurate product information** - Use the context below if available
4. **Use conversational tone** - Natural flow, like talking to a friend
5. **Occasional friendly emoticons** - 😊, 👍, 🙏 when appropriate
6. **Be proactive and helpful** - Anticipate follow-up questions
7. **If unsure, admit it honestly** - "I'm not sure about that, but I can help you find out!"

**Product context (if available):**
{product_context if product_context else 'No specific product information available. Respond helpfully based on general knowledge.'}

**Additional information:**
{faq_context if faq_context else 'Feel free to ask for more specific details.'}

Remember: You're not just a bot - you're {BOT_NAME}, a helpful friend who cares. Sound human, be warm, make customers feel valued.

Customer says: "{user_message}"
Your response (warm, human, helpful):"""
        
        # মেসেজেস প্রস্তুত
        messages = [{"role": "system", "content": system_prompt}]
        
        # সাম্প্রতিক হিস্ট্রি (শেষ ৩টি)
        for msg in history[-3:]:
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
                temperature=0.8,
                max_tokens=800,
                top_p=0.9
            )
            
            ai_response = response.choices[0].message.content.strip()
            logger.info(f"✅ AI Response generated ({len(ai_response)} chars)")
            
        except Exception as e:
            logger.error(f"Groq API error: {str(e)}")
            # Fallback response
            if language == 'bangla':
                ai_response = "দুঃখিত, এখন উত্তর তৈরি করতে সমস্যা হচ্ছে। একটু পরে আবার চেষ্টা করুন। 😊"
            elif language == 'banglish':
                ai_response = "Sorry, ekta problem hoise. Arokom try korben? 😊"
            else:
                ai_response = "Sorry, I'm having trouble generating a response. Please try again in a moment! 😊"
        
        # হিউম্যান টাচ যোগ
        ai_response = add_human_touches(ai_response, language, style)
        
        # হিস্ট্রি সেভ
        history.append({
            "user": user_message,
            "bot": ai_response,
            "timestamp": datetime.utcnow().isoformat(),
            "language": language,
            "intent": intent
        })
        
        if len(history) > 20:
            history = history[-20:]
        
        save_chat_history(admin_id, customer_id, history)
        
        return ai_response
        
    except Exception as e:
        logger.error(f"❌ AI Response Error: {str(e)}\n{traceback.format_exc()}")
        
        error_responses = {
            'bangla': "ওহো! কিছু সমস্যা হয়েছে। একটু পরে আবার চেষ্টা করবেন? 😊",
            'banglish': "Oops! Ekta problem hoise. Arokom try korben? 😊",
            'english': "Oops! Something went wrong. Could you try again in a moment? 😊"
        }
        
        language, _ = detect_language_and_style(user_message)
        return error_responses.get(language, "Sorry, I'm having trouble. Please try again!")

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
                
                # Optional: Handle read/delivery events
                elif 'read' in event:
                    logger.debug(f"👁️ Message read by {sender_id[:10]}...")
                elif 'delivery' in event:
                    logger.debug(f"✓ Message delivered to {sender_id[:10]}...")
        
        return jsonify({"status": "processed"}), 200
        
    except Exception as e:
        logger.error(f"❌ Webhook processing error: {str(e)}\n{traceback.format_exc()}")
        return jsonify({"error": "processing_error"}), 500

@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    try:
        # Test database connection
        if supabase:
            supabase.table("products").select("count", count="exact").limit(1).execute()
        
        return jsonify({
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "service": "facebook-ai-bot",
            "bot_name": BOT_NAME,
            "database": "connected" if supabase else "disconnected"
        }), 200
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }), 500

@app.route("/products/<admin_id>", methods=["GET"])
def list_products(admin_id: str):
    """প্রোডাক্ট লিস্ট এপিআই (ডিবাগিং এর জন্য)"""
    try:
        products = get_products(admin_id, force_refresh=True)
        return jsonify({
            "status": "success",
            "count": len(products),
            "products": products[:10]  # First 10 only
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"🚀 Starting Facebook AI Bot '{BOT_NAME}' on port {port}")
    logger.info(f"📦 Product integration enabled")
    logger.info(f"🤖 Using Groq API with Llama 3.3 70B")
    app.run(host="0.0.0.0", port=port, debug=False)

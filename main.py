import os
import json
import requests
import logging
import traceback
import random
import re
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
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
_order_sessions = {}  # Store order collection sessions
_first_message_cache = {}  # Track first messages
_conversation_states = {}  # Track natural conversation states

# ================= HELPER FUNCTIONS =================
def is_first_message(admin_id: str, customer_id: str) -> bool:
    """চেক করো এই গ্রাহকের প্রথম মেসেজ কিনা"""
    cache_key = f"first_{admin_id}_{customer_id}"
    
    if cache_key in _first_message_cache:
        return _first_message_cache[cache_key]
    
    try:
        response = supabase.table("chat_history")\
            .select("messages")\
            .eq("user_id", admin_id)\
            .eq("customer_id", customer_id)\
            .execute()
        
        is_first = not (response.data and response.data[0].get("messages"))
        _first_message_cache[cache_key] = is_first
        return is_first
        
    except Exception as e:
        logger.error(f"Check first message error: {str(e)}")
        return True

def get_products_with_details(admin_id: str) -> List[Dict]:
    """সকল প্রোডাক্টের বিস্তারিত তথ্য নিয়ে আসো"""
    products = get_products(admin_id)
    
    if not products:
        return []
    
    detailed_products = []
    for product in products:
        detailed_products.append({
            "id": product.get("id"),
            "name": product.get("name", "").strip(),
            "price": product.get("price", 0),
            "stock": product.get("stock", 0),
            "in_stock": product.get("in_stock", False),
            "description": product.get("description", "কোনো বিবরণ নেই।").strip(),
            "category": product.get("category", "সাধারণ").strip(),
            "features": product.get("features", "").strip() or "উচ্চমানের উপকরণ, টেকসই ও নির্ভরযোগ্য",
            "benefits": product.get("benefits", "").strip() or "দীর্ঘস্থায়ী ব্যবহার, মানসম্মত ও সাশ্রয়ী"
        })
    
    return detailed_products

def get_products(admin_id: str) -> List[Dict]:
    """Get products from database"""
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
        _product_cache[cache_key] = products
        return products
        
    except Exception as e:
        logger.error(f"Get products error: {str(e)}")
        return []

def detect_language(text: str) -> str:
    """Detect language"""
    if not text:
        return 'bangla'
    
    text_lower = text.lower()
    bangla_pattern = re.compile(r'[\u0980-\u09FF]')
    
    if bangla_pattern.search(text):
        return 'bangla'
    
    banglish_keywords = ['ki', 'kemon', 'achen', 'acha', 'valo', 'kothay', 'kot', 'dam']
    if any(keyword in text_lower for keyword in banglish_keywords):
        return 'bangla'
    
    return 'english'

def check_order_keywords(text: str) -> bool:
    """Check order keywords"""
    order_keywords = ['অর্ডার', 'order', 'কিনব', 'buy', 'নিব', 'চাই', 'পurchase', 'খরিদ']
    return any(keyword in text.lower() for keyword in order_keywords)

# ================= PRODUCT RECOMMENDATION ENGINE =================
class ProductRecommender:
    """প্রোডাক্ট রিকমেন্ডেশন ইঞ্জিন"""
    
    def __init__(self, products: List[Dict]):
        self.products = products
    
    def recommend_by_context(self, context: Dict) -> List[Dict]:
        """কনটেক্সট অনুযায়ী প্রোডাক্ট রিকমেন্ড করো"""
        available_products = [p for p in self.products if p.get("in_stock", False) and p.get("stock", 0) > 0]
        
        if not available_products:
            return []
        
        customer_type = context.get("customer_type", "personal")
        priorities = context.get("priorities", [])
        
        scored_products = []
        
        for product in available_products:
            score = 0
            
            # কাস্টমার টাইপ অনুযায়ী স্কোর
            if customer_type == "gift_buyer":
                if self._is_good_for_gift(product):
                    score += 3
            elif customer_type == "business":
                if self._is_good_for_business(product):
                    score += 3
            
            # প্রায়োরিটি অনুযায়ী স্কোর
            if "budget" in priorities:
                price = product.get("price", 0)
                if price < 1000:
                    score += 2
                elif price < 3000:
                    score += 1
            elif "quality" in priorities:
                if self._has_quality_keywords(product):
                    score += 2
            
            # র‍্যান্ডম কিছু ভ্যারিয়েশন
            score += random.random()  # 0-1 র‍্যান্ডম স্কোর
            
            scored_products.append((score, product))
        
        # স্কোর অনুযায়ী সাজাও
        scored_products.sort(key=lambda x: x[0], reverse=True)
        
        # শুধু ১-২টা প্রোডাক্ট রিটার্ন করো
        return [p[1] for p in scored_products[:2]]
    
    def _is_good_for_gift(self, product: Dict) -> bool:
        """গিফটের জন্য ভালো কিনা চেক করো"""
        product_text = f"{product.get('name', '')} {product.get('description', '')} {product.get('category', '')}".lower()
        gift_keywords = ['gift', 'উপহার', 'প্রেজেন্ট', 'সৌজন্য', 'বক্স', 'প্যাকেজ', 'উপহার', 'gifting']
        return any(keyword in product_text for keyword in gift_keywords)
    
    def _is_good_for_business(self, product: Dict) -> bool:
        """বিজনেসের জন্য ভালো কিনা চেক করো"""
        product_text = f"{product.get('name', '')} {product.get('description', '')} {product.get('category', '')}".lower()
        business_keywords = ['office', 'অফিস', 'বিজনেস', 'কোম্পানি', 'professional', 'corporate', 'executive']
        return any(keyword in product_text for keyword in business_keywords)
    
    def _has_quality_keywords(self, product: Dict) -> bool:
        """কোয়ালিটি কি-ওয়ার্ড আছে কিনা চেক করো"""
        product_text = f"{product.get('name', '')} {product.get('description', '')} {product.get('features', '')}".lower()
        quality_keywords = ['premium', 'হাইকোয়ালিটি', 'best', 'টেকসই', 'durable', 'উচ্চমান', 'quality', 'standard']
        return any(keyword in product_text for keyword in quality_keywords)

# ================= NATURAL CONVERSATION MANAGER =================
class NaturalConversationManager:
    """প্রাকৃতিক কথোপকথন ম্যানেজার"""
    
    def __init__(self, admin_id: str, customer_id: str):
        self.admin_id = admin_id
        self.customer_id = customer_id
        self.state_key = f"conv_{admin_id}_{customer_id}"
        self.products = get_products_with_details(admin_id)
        self.recommender = ProductRecommender(self.products)
        
        if self.state_key not in _conversation_states:
            _conversation_states[self.state_key] = {
                "step": "greeting",
                "intent": None,
                "customer_type": None,
                "priorities": [],
                "last_recommended": None,
                "conversation_history": []
            }
        
        self.state = _conversation_states[self.state_key]
    
    def process_message(self, user_message: str, page_name: str) -> str:
        """ইউজার মেসেজ প্রসেস করো"""
        user_message_clean = user_message.strip()
        self.state["conversation_history"].append({"role": "user", "content": user_message_clean})
        
        # ইমারজেন্সি কেস: সরাসরি অর্ডার
        if check_order_keywords(user_message_clean) and self.state.get("last_recommended"):
            return self._handle_direct_order()
        
        current_step = self.state["step"]
        
        if current_step == "greeting":
            return self._handle_greeting(user_message_clean, page_name)
        elif current_step == "understanding":
            return self._handle_understanding(user_message_clean)
        elif current_step == "recommendation":
            return self._handle_recommendation(user_message_clean)
        elif current_step == "explanation":
            return self._handle_explanation(user_message_clean)
        elif current_step == "soft_cta":
            return self._handle_soft_cta(user_message_clean)
        elif current_step == "objection":
            return self._handle_objection(user_message_clean)
        else:
            return self._handle_ai_fallback(user_message_clean, page_name)
    
    def _handle_greeting(self, message: str, page_name: str) -> str:
        """গ্রিটিং হ্যান্ডেল করো"""
        # যদি প্রথম বার্তা হয়
        if len(self.state["conversation_history"]) <= 1:
            greetings = [
                f"হাই 👋 {page_name}-এ আপনাকে স্বাগতম! আমি {BOT_NAME}, আপনার সহায়ক 😊\n\nকীভাবে সাহায্য করতে পারি আজকে?",
                f"সালাম 🙂 {page_name} পেজে স্বাগতম! আমি {BOT_NAME} 🫶\n\nআজকে কি প্রয়োজন?",
                f"নমস্কার 🙏 {page_name}-এ স্বাগতম! আমি {BOT_NAME}, আপনার পাশে আছি 😊\n\nকী খুঁজছেন আজকে?"
            ]
            response = random.choice(greetings)
        else:
            # গ্রিটিং এর জবাব
            greeting_responses = ['হাই', 'হ্যালো', 'সালাম', 'ভালো', 'আছি', 'hello', 'hi', 'fine']
            if any(greet in message.lower() for greet in greeting_responses):
                responses = [
                    "ভালো লাগলো আপনার সাথে কথা বলে 😊 তাহলে বলুন, আজকে কি খুঁজছেন?",
                    "বেশ ভালো আছি, ধন্যবাদ 🫶 আপনার কী দরকার আজ? একটু আমাকে বলুন...",
                    "শুভেচ্ছা রইলো 🙏 আপনার কী প্রয়োজন, আমি এখানেই আছি সাহায্য করতে 😌"
                ]
                response = random.choice(responses)
            else:
                # সাধারণ কথোপকথন শুরু করো
                response = "দেখি, কীভাবে সাহায্য করতে পারি? 😊 আপনি কি কিছু খুঁজছেন নাকি জানতে চাচ্ছেন?"
        
        self.state["step"] = "understanding"
        self.state["conversation_history"].append({"role": "assistant", "content": response})
        return response
    
    def _handle_understanding(self, message: str) -> str:
        """ইউজার ইন্টেন্ট বোঝো"""
        message_lower = message.lower()
        
        # ইন্টেন্ট এক্সট্র্যাক্ট করো
        intent_recognized = False
        
        # ক্রেতার টাইপ চিহ্নিত করো
        if any(word in message_lower for word in ['গিফট', 'উপহার', 'দেব', 'present', 'gift']):
            self.state["customer_type"] = "gift_buyer"
            intent_recognized = True
        
        elif any(word in message_lower for word in ['অফিস', 'বিজনেস', 'কোম্পানি', 'office', 'business', 'corporate']):
            self.state["customer_type"] = "business"
            intent_recognized = True
        
        elif any(word in message_lower for word in ['নিজের', 'আমার', 'মার', 'my', 'personal', 'আমি']):
            self.state["customer_type"] = "personal"
            intent_recognized = True
        
        # প্রায়োরিটি চিহ্নিত করো
        if any(word in message_lower for word in ['দাম', 'কম', 'সস্তা', 'cheap', 'low', 'বাজেট']):
            self.state["priorities"].append("budget")
            intent_recognized = True
        
        if any(word in message_lower for word in ['ভালো', 'quality', 'টেকসই', 'durable', 'প্রিমিয়াম']):
            self.state["priorities"].append("quality")
            intent_recognized = True
        
        if any(word in message_lower for word in ['দ্রুত', 'quick', 'আজকে', 'urgent', 'তাড়াতাড়ি']):
            self.state["priorities"].append("urgency")
            intent_recognized = True
        
        if intent_recognized:
            self.state["step"] = "recommendation"
            
            # কনটেক্সট অনুযায়ী ওয়ান ফোলো-আপ প্রশ্ন
            follow_up = self._generate_follow_up_question()
            self.state["conversation_history"].append({"role": "assistant", "content": follow_up})
            return follow_up
        else:
            # যদি ইন্টেন্ট বোঝা না যায়, AI ব্যবহার করো
            self.state["step"] = "understanding"
            ai_response = self._get_ai_response(message, "আমি আপনার need বুঝতে চেষ্টা করছি। একটু আরো বলবেন?")
            self.state["conversation_history"].append({"role": "assistant", "content": ai_response})
            return ai_response
    
    def _generate_follow_up_question(self) -> str:
        """ওয়ান ফোলো-আপ প্রশ্ন জেনারেট করো"""
        customer_type = self.state.get("customer_type", "personal")
        priorities = self.state.get("priorities", [])
        
        if customer_type == "gift_buyer":
            questions = [
                "গিফটের জন্য! ভালো তো 👍 বয়স কত যার জন্য?",
                "উপহার দেবেন? 🎁 recipient এর age group টা একটু বলবেন?",
                "গিফটিং এর জন্য! 😊 কি occasion এর জন্য? birthday, anniversary নাকি general?"
            ]
        
        elif customer_type == "business":
            questions = [
                "বিজনেসের জন্য? office setup নাকি client gift?",
                "কর্পোরেট ইউজ? 💼 office decoration নাকি employee gift?",
                "বিজনেস প্রয়োজন? professional look priority নাকি bulk order?"
            ]
        
        else:  # personal
            if "budget" in priorities:
                questions = [
                    "বাজেট friendly option চান? আনুমানিক range টা কেমন?",
                    "দাম priority? 👍 আনুমানিক কত রাখতে চান?",
                    "বাজেট conscious? 💸 specific range বললে perfect match খুঁজে দেব"
                ]
            elif "quality" in priorities:
                questions = [
                    "quality priority? দাম একটু বেশি হলেও চলবে?",
                    "টেকসই জিনিস চান? 👍 premium segment দেখাবো?",
                    "quality focus? 😊 high-end options prefer করবেন?"
                ]
            else:
                questions = [
                    "কেমন ধরনের জিনিস পছন্দ করেন? modern look নাকি classic style?",
                    "কোন vibe prefer করেন? 😊 minimal নাকি vibrant?",
                    "স্টাইলের preference টা একটু বলবেন? contemporary নাকি traditional?"
                ]
        
        return random.choice(questions)
    
    def _handle_recommendation(self, message: str) -> str:
        """মাইক্রো রিকমেন্ডেশন দাও"""
        # ইউজারের উত্তর থেকে আরো ইনফো এক্সট্র্যাক্ট করো
        self._update_context_from_response(message)
        
        # প্রোডাক্ট রিকমেন্ড করো
        context = {
            "customer_type": self.state.get("customer_type", "personal"),
            "priorities": self.state.get("priorities", [])
        }
        
        recommended_products = self.recommender.recommend_by_context(context)
        
        if not recommended_products:
            self.state["step"] = "explanation"
            response = "দুঃখিত, এখন স্টকে আপনার জন্য matching product নেই 😔\n\nঅন্যান্য option নিয়ে ভাবতে চান?"
            self.state["conversation_history"].append({"role": "assistant", "content": response})
            return response
        
        # শুধু ১টা প্রোডাক্ট সিলেক্ট করো
        selected_product = recommended_products[0]
        self.state["last_recommended"] = selected_product
        self.state["step"] = "explanation"
        
        # প্রোডাক্ট রিকমেন্ডেশন মেসেজ
        product_name = selected_product.get("name", "")
        price = selected_product.get("price", 0)
        
        recommendation_phrases = [
            f"আপনার কথাটা শুনে এই option টা সবচেয়ে ভালো match করবে মনে হচ্ছে 👇\n\n✨ **{product_name}**",
            f"আমার মনে হচ্ছে এটা আপনার জন্য পারফেক্ট হবে 😊\n\n🔥 **{product_name}**",
            f"অনেক customer আপনার মত need এর জন্য এটা নেয় 👍\n\n🌟 **{product_name}**"
        ]
        
        response = random.choice(recommendation_phrases)
        self.state["conversation_history"].append({"role": "assistant", "content": response})
        return response
    
    def _handle_explanation(self, message: str) -> str:
        """ইমোশনাল এক্সপ্লেনেশন দাও"""
        product = self.state.get("last_recommended")
        
        if not product:
            self.state["step"] = "soft_cta"
            response = "দুঃখিত, পণ্যটি এখন খুঁজে পাচ্ছি না 😔 অন্য কিছু দেখতে চান?"
            self.state["conversation_history"].append({"role": "assistant", "content": response})
            return response
        
        # AI দিয়ে ইমোশনাল এক্সপ্লেনেশন জেনারেট করো
        ai_response = self._get_product_explanation(product, self.state)
        
        self.state["step"] = "soft_cta"
        self.state["conversation_history"].append({"role": "assistant", "content": ai_response})
        return ai_response
    
    def _handle_soft_cta(self, message: str) -> str:
        """সফট কল টু অ্যাকশন"""
        message_lower = message.lower()
        
        # AI দিয়ে রেসপন্স জেনারেট করো
        ai_response = self._get_soft_cta_response(message, self.state)
        
        self.state["conversation_history"].append({"role": "assistant", "content": ai_response})
        return ai_response
    
    def _handle_objection(self, message: str) -> str:
        """অবজেকশন হ্যান্ডেল করো"""
        # AI দিয়ে অবজেকশন হ্যান্ডলিং রেসপন্স জেনারেট করো
        ai_response = self._get_objection_response(message, self.state)
        
        self.state["step"] = "soft_cta"
        self.state["conversation_history"].append({"role": "assistant", "content": ai_response})
        return ai_response
    
    def _handle_direct_order(self) -> str:
        """সরাসরি অর্ডার হ্যান্ডেল করো"""
        product = self.state.get("last_recommended")
        if product:
            product_name = product.get("name", "")
            price = product.get("price", 0)
            
            order_phrases = [
                f"একদম! 😊 **{product_name}** এর অর্ডার নিচ্ছি।\n\nদাম: ৳{price:,}\n\nআপনার নাম বলবেন শুরু করতে?",
                f"বেশ তো! 🫶 **{product_name}** order শুরু করছি।\n\nPrice: ৳{price:,}\n\nYour name please?",
                f"Perfect! 👍 **{product_name}** এর জন্য order process start করি।\n\nCost: ৳{price:,}\n\nName দিবেন?"
            ]
            
            response = random.choice(order_phrases)
            self.state["conversation_history"].append({"role": "assistant", "content": response})
            return response
        
        # যদি প্রোডাক্ট না থাকে
        return "কোন পণ্য সিলেক্ট করা নেই 😔 প্রথমে কিছু দেখে নিন।"
    
    def _handle_ai_fallback(self, message: str, page_name: str) -> str:
        """AI ফলব্যাক রেসপন্স"""
        ai_response = self._get_ai_response(message, page_name)
        self.state["conversation_history"].append({"role": "assistant", "content": ai_response})
        return ai_response
    
    def _update_context_from_response(self, message: str):
        """ইউজারের রেসপন্স থেকে কনটেক্সট আপডেট করো"""
        message_lower = message.lower()
        
        # বয়স ডিটেক্ট করো (গিফটের জন্য)
        age_pattern = r'(\d+)\s*(বছর|year|yr)'
        age_match = re.search(age_pattern, message)
        if age_match:
            self.state["age"] = age_match.group(1)
        
        # বাজেট রেঞ্জ ডিটেক্ট করো
        budget_patterns = [
            r'(\d+)\s*(-|থেকে|to)\s*(\d+)',
            r'(\d+)\s*(হাজার|হা|thousand|k)',
            r'৳?\s*(\d+)'
        ]
        
        for pattern in budget_patterns:
            match = re.search(pattern, message)
            if match:
                self.state["budget_range"] = message
                break
    
    def _get_ai_response(self, user_message: str, context: str = "") -> str:
        """AI রেসপন্স জেনারেট করো"""
        try:
            api_key = get_groq_key(self.admin_id)
            if not api_key:
                return self._get_fallback_response()
            
            client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=api_key)
            
            # কনভারসেশন হিস্ট্রি তৈরি করো
            messages = self._prepare_conversation_messages(user_message, context)
            
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages,
                temperature=0.7,
                max_tokens=250,
                top_p=0.9
            )
            
            ai_response = response.choices[0].message.content.strip()
            return ai_response
            
        except Exception as e:
            logger.error(f"AI Response Error in conversation: {str(e)}")
            return self._get_fallback_response()
    
    def _get_product_explanation(self, product: Dict, context: Dict) -> str:
        """প্রোডাক্টের ইমোশনাল এক্সপ্লেনেশন জেনারেট করো"""
        try:
            api_key = get_groq_key(self.admin_id)
            if not api_key:
                return self._get_fallback_product_explanation(product)
            
            client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=api_key)
            
            product_name = product.get("name", "")
            description = product.get("description", "")
            price = product.get("price", 0)
            features = product.get("features", "")
            benefits = product.get("benefits", "")
            
            customer_type = context.get("customer_type", "personal")
            
            system_prompt = f"""তুমি একজন ফ্রেন্ডলি শপ অ্যাসিস্ট্যান্ট। গ্রাহককে প্রোডাক্টের emotional benefits বোঝাও।

নিয়ম:
1. প্রোডাক্টের technical specs বেশি বলো না
2. emotional experience ও use cases বলো
3. ৩-৪ লাইনের মধ্যে বলো
4. সফট ইমোজি ব্যবহার করো (😊, ✨, 🫶)
5. দাম প্রাকৃতিকভাবে mention করো
6. {customer_type} টাইপের ক্রেতার জন্য relevant benefits highlight করো

প্রোডাক্ট: {product_name}
বিবরণ: {description}
দাম: ৳{price}
ফিচার: {features}
সুবিধা: {benefits}

একটি আকর্ষণীয় emotional explanation দাও:"""
            
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "এই প্রোডাক্টটা আমার জন্য কেমন হবে?"}
            ]
            
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages,
                temperature=0.8,
                max_tokens=200,
                top_p=0.9
            )
            
            explanation = response.choices[0].message.content.strip()
            
            # দাম যোগ করো যদি AI না যোগে
            if f"৳{price}" not in explanation and f"{price}" not in explanation:
                explanation += f"\n\nদাম: ৳{price:,} ✨"
            
            return explanation
            
        except Exception as e:
            logger.error(f"Product explanation error: {str(e)}")
            return self._get_fallback_product_explanation(product)
    
    def _get_soft_cta_response(self, user_message: str, context: Dict) -> str:
        """সফট CTA রেসপন্স জেনারেট করো"""
        try:
            api_key = get_groq_key(self.admin_id)
            if not api_key:
                return "কেমন লাগলো আপনার? 😊 আগ্রহ থাকলে জানাবেন।"
            
            client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=api_key)
            
            product = context.get("last_recommended")
            product_name = product.get("name", "") if product else ""
            
            system_prompt = f"""তুমি একজন ফ্রেন্ডলি শপ অ্যাসিস্ট্যান্ট। গ্রাহককে প্রেশার ছাড়া gently invite করো।

নিয়ম:
1. কখনো pressure দিও না
2. open-ended প্রশ্ন করো
3. ২-৩ লাইনের মধ্যে বলো
4. সফট ইমোজি ব্যবহার করো (😊, 🫶, 👍)
5. গ্রাহককে control দাও
6. alternative option এর mention করো

গ্রাহক বলেছেন: "{user_message}"
প্রোডাক্ট: {product_name}

একটি gentle, pressure-free response দাও:"""
            
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ]
            
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages,
                temperature=0.7,
                max_tokens=150,
                top_p=0.9
            )
            
            return response.choices[0].message.content.strip()
            
        except Exception as e:
            logger.error(f"Soft CTA error: {str(e)}")
            return "কেমন লাগলো? 😊 আগ্রহ থাকলে জানাবেন।"
    
    def _get_objection_response(self, user_message: str, context: Dict) -> str:
        """অবজেকশন হ্যান্ডলিং রেসপন্স জেনারেট করো"""
        try:
            api_key = get_groq_key(self.admin_id)
            if not api_key:
                return "বুঝতে পারছি 🫶 চিন্তা করবেন না, আপনার time নিয়ে decide করুন।"
            
            client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=api_key)
            
            system_prompt = """তুমি একজন এমপ্যাথেটিক শপ অ্যাসিস্ট্যান্ট। গ্রাহকের concerns validate করো এবং gently reassure করো।

নিয়ম:
1. প্রথমে তাদের concern acknowledge করো
2. কখনো argue করো না
3. positive aspects gently mention করো
4. সফট ইমোজি ব্যবহার করো (🫶, 😊, 👍)
5. ৩-৪ লাইনের মধ্যে বলো
6. গ্রাহককে comfortable feeling দাও

গ্রাহকের concern: """
            
            messages = [
                {"role": "system", "content": system_prompt + user_message},
                {"role": "user", "content": "এই concern টা handle করো"}
            ]
            
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages,
                temperature=0.7,
                max_tokens=200,
                top_p=0.9
            )
            
            return response.choices[0].message.content.strip()
            
        except Exception as e:
            logger.error(f"Objection handling error: {str(e)}")
            return "বুঝতে পারছি 🫶 আপনার decision আমি respect করি 😊"
    
    def _prepare_conversation_messages(self, user_message: str, page_name: str) -> List[Dict]:
        """কনভারসেশন মেসেজ প্রিপেয়ার করো"""
        # বেইস সিস্টেম প্রম্পট
        system_prompt = f"""তুমি {BOT_NAME}, একজন ফ্রেন্ডলি, হিউম্যান-লাইক সেলস অ্যাসিস্ট্যান্ট {page_name}-এর জন্য।

IMPORTANT BEHAVIOR RULES:
1. কখনো robotic বা systematic sound করো না
2. কথোপকথন natural এবং casual রাখো
3. শুধুমাত্র ONE question করো একবারে
4. গ্রাহকের intent বুঝে নাও আগে selling শুরু করার
5. emotions, use-cases, এবং benefits দিয়ে sell করো - শুধু features দিয়ে না
6. গ্রাহককে overwhelm করো না too many options দিয়ে
7. সবসময় গ্রাহকের control এবং comfort maintain করো

TONE & STYLE:
- Friendly, caring, এবং conversational
- হালকা ইমোজি occasionally ব্যবহার করো (🙂✨👍) but not too many
- এই phrases ব্যবহার করো: "বুঝলাম 😊", "ভালো প্রশ্ন 👍", "একটু বুঝে নিচ্ছি...", "আমি এখানেই আছি, চিন্তা নেই"

কখনো বলো না:
- "Please select a category"
- "ক্যাটাগরি দেখতে ক্যাটাগরি লিখুন"
- "YES লিখুন / 1 চাপুন"
- menus দেখাবে না unless গ্রাহক asks

গ্রাহকের শেষ মেসেজ: "{user_message}"

তুমি:"""
        
        messages = [{"role": "system", "content": system_prompt}]
        
        # কনভারসেশন হিস্ট্রি যোগ করো (শেষের ৪টা মেসেজ)
        history = self.state.get("conversation_history", [])
        for msg in history[-4:]:  # শেষের ৪টা মেসেজ
            messages.append(msg)
        
        return messages
    
    def _get_fallback_response(self) -> str:
        """ফলব্যাক রেসপন্স"""
        fallbacks = [
            "একটু ভাবছি আপনার কথাটা নিয়ে... 😊 আসলে আমার মনে হচ্ছে আপনি যা খুঁজছেন, তা আমাদের কাছে আছে। একটু বলবেন কী ধরনের জিনিস?",
            "বুঝলাম... 🫶 আমি আপনাকে সেরা option টা suggest করতে চাই। একটু বলবেন, আপনার priority কী?",
            "আমি এখানেই আছি, চিন্তা নেই 🙂 আপনার কী দরকার সেটা একটু clear করলে আমি ভালোভাবে help করতে পারব।"
        ]
        return random.choice(fallbacks)
    
    def _get_fallback_product_explanation(self, product: Dict) -> str:
        """ফলব্যাক প্রোডাক্ট এক্সপ্লেনেশন"""
        product_name = product.get("name", "")
        price = product.get("price", 0)
        description = product.get("description", "")
        
        explanations = [
            f"এই জিনিসটা থাকলে আপনার daily routine অনেক সহজ হবে 😊 ব্যবহারে comfortable, দেখতেও stylish ✨\n\nদাম: ৳{price:,}",
            f"{product_name} নিলে long-term এ ভালো value পাবেন 👍 quality ভালো, maintenance কম 🫶\n\nPrice: ৳{price:,}",
            f"এই option টা অনেকের favorite কারণ practical ও aesthetic both 😌 {description[:80]}...\n\nCost: ৳{price:,} ✨"
        ]
        
        return random.choice(explanations)
    
    def cleanup(self):
        """কনভারসেশন ক্লিনআপ"""
        if self.state_key in _conversation_states:
            del _conversation_states[self.state_key]

# ================= ORDER SESSION MANAGEMENT =================
class OrderSession:
    """Manage order collection session for a customer"""
    
    def __init__(self, admin_id: str, customer_id: str):
        self.admin_id = admin_id
        self.customer_id = customer_id
        self.session_id = f"order_{admin_id}_{customer_id}"
        self.step = 0
        self.data = {
            "name": "",
            "phone": "",
            "product": "",
            "quantity": "",
            "address": "",
            "status": "pending",
            "total": 0
        }
        self.products = get_products_with_details(admin_id)
    
    def start_order(self):
        """Start order collection naturally"""
        self.step = 1
        _order_sessions[self.session_id] = self
        
        start_phrases = [
            "একদম, অর্ডার শুরু করছি! 😊 প্রথমে আপনার নামটা বলবেন?",
            "বেশ তো, অর্ডার নেওয়া যাক 👍 আপনার নাম কী?",
            "ভালো সিদ্ধান্ত! 🫶 একটু তথ্য নিই—আপনার নাম বলবেন?"
        ]
        
        return random.choice(start_phrases)
    
    def process_response(self, user_message: str) -> Tuple[str, bool]:
        """Process user response"""
        completed = False
        
        if self.step == 1:  # Name
            self.data["name"] = user_message.strip()
            self.step = 2
            
            name_phrases = [
                f"ধন্যবাদ {self.data['name']}! 😊 এখন আপনার ফোন নম্বর দিন (যেমন: 017XXXXXXXX):",
                f"নামটা ভালো লাগলো! 👍 এখন ফোন নম্বরটা বলবেন?",
                f"ঠিক আছে {self.data['name']} 🫶 ফোন নম্বরটা একটু দিবেন?"
            ]
            return random.choice(name_phrases), False
            
        elif self.step == 2:  # Phone
            phone = user_message.strip()
            if self.validate_phone(phone):
                self.data["phone"] = phone
                self.step = 3
                
                # যদি আগে থেকেই প্রোডাক্ট সিলেক্টেড থাকে
                if self.data["product"]:
                    product = self.find_product_by_name(self.data["product"])
                    if product:
                        self.step = 4
                        stock = product.get("stock", 0)
                        price = product.get("price", 0)
                        
                        product_phrases = [
                            f"ফোন নম্বর সেভ করলাম! 😊\n\n**{product['name']}** এর order continue করছি।\n\nদাম: ৳{price:,}\nস্টক: {stock} পিস\n\nকত পিস চান?",
                            f"ঠিক আছে! 👍\n\n**{product['name']}** এর জন্য quantity বলবেন?\n\nPrice: ৳{price:,}\nAvailable: {stock} pieces"
                        ]
                        return random.choice(product_phrases), False
                
                # প্রোডাক্ট সিলেকশনের জন্য AI ব্যবহার করো
                ai_response = self._get_product_suggestion()
                return ai_response, False
            else:
                return "দুঃখিত, সঠিক ফোন নম্বর দিন (১১ ডিজিট, যেমন: 01712345678):", False
                
        elif self.step == 3:  # Product
            selected_product = self.find_product(user_message)
            if selected_product:
                self.data["product"] = selected_product["name"]
                self.data["product_id"] = selected_product.get("id")
                self.step = 4
                stock = selected_product.get("stock", 0)
                price = selected_product.get("price", 0)
                
                selection_phrases = [
                    f"✅ **{selected_product['name']}** নির্বাচিত!\n\n💰 দাম: ৳{price:,}\n📦 স্টক: {stock} পিস\n\nকত পিস চান?",
                    f"একদম! 👍 **{selected_product['name']}** তো ভালো choice!\n\nPrice: ৳{price:,}\nAvailable: {stock}\n\nQuantity কত হবে?"
                ]
                return random.choice(selection_phrases), False
            else:
                products_text = self._get_available_products_formatted()
                return f"পণ্যটি খুঁজে পাইনি 😔\n\n{products_text}\n\nআবার চেষ্টা করুন:", False
                
        elif self.step == 4:  # Quantity
            if user_message.isdigit():
                quantity = int(user_message)
                if quantity > 0:
                    product = self.find_product_by_name(self.data["product"])
                    if product:
                        stock = product.get("stock", 0)
                        if stock >= quantity:
                            self.data["quantity"] = quantity
                            price = product.get("price", 0)
                            self.data["total"] = price * quantity
                            self.step = 5
                            
                            quantity_phrases = [
                                f"✅ {quantity} পিস ঠিক আছে! 😊\n💰 মোট মূল্য: ৳{self.data['total']:,}\n\nএখন ডেলিভারি ঠিকানা দিন (বিস্তারিত):",
                                f"{quantity} pieces noted! 👍\nTotal: ৳{self.data['total']:,}\n\nএখন delivery address বলবেন?"
                            ]
                            return random.choice(quantity_phrases), False
                        else:
                            return f"দুঃখিত, স্টকে মাত্র {stock} পিস আছে 😔 কম সংখ্যক দিন:", False
                else:
                    return "দুঃখিত, ১ বা তার বেশি সংখ্যা দিন:", False
            else:
                return "দুঃখিত, সংখ্যা দিন (যেমন: 1, 2, 3):", False
                
        elif self.step == 5:  # Address
            self.data["address"] = user_message.strip()
            self.step = 6
            summary = self.get_order_summary()
            
            summary_phrases = [
                f"ঠিকানা সংরক্ষিত! 😊\n\n{summary}\n\nঅর্ডার কনফার্ম করতে 'confirm' লিখুন 🫶",
                f"Address saved! 👍\n\n{summary}\n\nConfirmation এর জন্য 'confirm' লিখুন 😊"
            ]
            return random.choice(summary_phrases), False
            
        elif self.step == 6:  # Confirm
            response_lower = user_message.lower().strip()
            
            if response_lower == 'confirm':
                order_saved = self.save_order()
                if order_saved:
                    completed = True
                    order_id = self.data.get("order_id", "")
                    
                    success_phrases = [
                        f"✅ অর্ডার কনফার্ম্ড! 😊\n\nOrder ID: {order_id}\nআমরা শীঘ্রই আপনার সাথে যোগাযোগ করব 🫶\nধন্যবাদ!",
                        f"🎉 Order confirmed successfully! 👍\n\nID: {order_id}\nআমরা contact করব very soon 😊\nThank you!",
                        f"✅ Perfect! Order placed 🫶\n\nReference: {order_id}\nOur team will contact you shortly 😊\nঅনেক ধন্যবাদ!"
                    ]
                    return random.choice(success_phrases), True
                else:
                    return "❌ অর্ডার সেভ করতে সমস্যা হয়েছে 😔 দয়া করে আবার চেষ্টা করুন।", True
            else:
                completed = True
                cancel_phrases = [
                    "অর্ডার বাতিল করা হয়েছে 😊 যখন ইচ্ছা হবে আবার অর্ডার দিতে পারেন 🫶",
                    "Order cancelled 👍 No problem, আপনি যখন ready হবেন আবার বলবেন 😊",
                    "ঠিক আছে, order cancel করলাম 🫶 প্রয়োজনে আবার জানাবেন ✨"
                ]
                return random.choice(cancel_phrases), True
        
        return "কিছু সমস্যা হয়েছে 😔 আবার চেষ্টা করুন।", True
    
    def _get_product_suggestion(self) -> str:
        """AI দিয়ে প্রোডাক্ট সাজেশন জেনারেট করো"""
        try:
            api_key = get_groq_key(self.admin_id)
            if not api_key:
                return self._get_available_products_formatted()
            
            client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=api_key)
            
            # available products এর তালিকা তৈরি করো
            available_products = []
            for product in self.products:
                if product.get("in_stock", False) and product.get("stock", 0) > 0:
                    name = product.get("name", "").strip()
                    price = product.get("price", 0)
                    description = product.get("description", "")[:60]
                    if name:
                        available_products.append(f"- {name} (৳{price:,}): {description}")
            
            if not available_products:
                return "দুঃখিত, এখন কোনো পণ্য স্টকে নেই 😔"
            
            products_text = "\n".join(available_products[:5])
            
            system_prompt = f"""তুমি একজন শপ অ্যাসিস্ট্যান্ট। গ্রাহককে প্রোডাক্ট সাজেশন দাও naturally।

নিয়ম:
1. ২-৩ লাইনের মধ্যে বলো
2. friendly tone ব্যবহার করো
3. কখনো overwhelming list দেখাবে না
4. গ্রাহককে choose করতে invite করো
5. ইমোজি ব্যবহার করো (😊, 👍)

Available products:
{products_text}

একটি natural product suggestion দাও:"""
            
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "কোন পণ্য নেবো?"}
            ]
            
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages,
                temperature=0.7,
                max_tokens=150,
                top_p=0.9
            )
            
            return response.choices[0].message.content.strip()
            
        except Exception as e:
            logger.error(f"Product suggestion error: {str(e)}")
            return self._get_available_products_formatted()
    
    def _get_available_products_formatted(self) -> str:
        """Get available products formatted"""
        available = []
        for product in self.products:
            if product.get("in_stock", False) and product.get("stock", 0) > 0:
                name = product.get("name", "").strip()
                if name:
                    price = product.get("price", 0)
                    stock = product.get("stock", 0)
                    description = product.get("description", "")[:50]
                    available.append(f"• {name} - ৳{price:,} (স্টক: {stock})\n  {description}...")
        
        if available:
            return "স্টকে থাকা পণ্য:\n\n" + "\n\n".join(available[:4])
        return "দুঃখিত, এখন কোনো পণ্য স্টকে নেই 😔"
    
    def validate_phone(self, phone: str) -> bool:
        """Validate phone number"""
        phone_clean = re.sub(r'\D', '', phone)
        return len(phone_clean) == 11 and phone_clean.startswith('01')
    
    def find_product(self, query: str) -> Optional[Dict]:
        """Find product"""
        if not query:
            return None
            
        query_lower = query.lower().strip()
        
        for product in self.products:
            name = product.get("name", "").lower().strip()
            if name and (query_lower in name or name in query_lower):
                if product.get("in_stock", False) and product.get("stock", 0) > 0:
                    return product
        
        return None
    
    def find_product_by_name(self, name: str) -> Optional[Dict]:
        """Find product by name"""
        for product in self.products:
            if product.get("name", "").lower().strip() == name.lower().strip():
                return product
        return None
    
    def get_order_summary(self) -> str:
        """Get order summary"""
        return f"""📦 অর্ডার সামারি:
👤 নাম: {self.data['name']}
📱 ফোন: {self.data['phone']}
🛒 পণ্য: {self.data['product']}
🔢 পরিমাণ: {self.data['quantity']} পিস
💰 মোট: ৳{self.data['total']:,}
🏠 ঠিকানা: {self.data['address']}"""
    
    def save_order(self) -> bool:
        """Save order"""
        try:
            order_data = {
                "user_id": self.admin_id,
                "customer_name": self.data["name"],
                "customer_phone": self.data["phone"],
                "product": self.data["product"],
                "quantity": int(self.data["quantity"]),
                "address": self.data["address"],
                "total": float(self.data["total"]),
                "status": "pending",
                "created_at": datetime.utcnow().isoformat()
            }
            
            response = supabase.table("orders").insert(order_data).execute()
            
            if response.data:
                self.data["order_id"] = response.data[0].get("id", "")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Save order error: {str(e)}")
            return False
    
    def cancel(self):
        """Cancel session"""
        if self.session_id in _order_sessions:
            del _order_sessions[self.session_id]

# ================= CORE FUNCTIONS =================
def find_client_by_page_id(page_id: str) -> Optional[Dict]:
    """Find client by page ID"""
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

def send_facebook_message(page_token: str, customer_id: str, message_text: str):
    """Send Facebook message"""
    try:
        url = f"https://graph.facebook.com/v18.0/me/messages?access_token={page_token}"
        payload = {
            "recipient": {"id": customer_id},
            "message": {"text": message_text}
        }
        response = requests.post(url, json=payload, timeout=30)
        
        if response.status_code != 200:
            logger.error(f"Facebook API error: {response.status_code} - {response.text}")
        else:
            logger.info(f"✅ Message sent to {customer_id[:10]}...")
            
    except Exception as e:
        logger.error(f"❌ Send message error: {str(e)}")

def typing_on(token: str, recipient_id: str) -> bool:
    """Typing on"""
    try:
        url = f"https://graph.facebook.com/v18.0/me/messages?access_token={token}"
        payload = {"recipient": {"id": recipient_id}, "sender_action": "typing_on"}
        response = requests.post(url, json=payload, timeout=5)
        return response.status_code == 200
    except:
        return False

def typing_off(token: str, recipient_id: str) -> bool:
    """Typing off"""
    try:
        url = f"https://graph.facebook.com/v18.0/me/messages?access_token={token}"
        payload = {"recipient": {"id": recipient_id}, "sender_action": "typing_off"}
        response = requests.post(url, json=payload, timeout=5)
        return response.status_code == 200
    except:
        return False

# ================= MAIN RESPONSE GENERATOR =================
def generate_ai_response(admin_id: str, user_message: str, customer_id: str, page_name: str = "আমাদের দোকান") -> str:
    """Generate natural human-like response"""
    try:
        # Check if in order session
        session_id = f"order_{admin_id}_{customer_id}"
        if session_id in _order_sessions:
            session = _order_sessions[session_id]
            response, completed = session.process_response(user_message)
            if completed:
                session.cancel()
                # কনভারসেশন স্টেট ক্লিনআপ করো
                conv_key = f"conv_{admin_id}_{customer_id}"
                if conv_key in _conversation_states:
                    del _conversation_states[conv_key]
            return response
        
        # ন্যাচারাল কনভারসেশন ম্যানেজার ব্যবহার করো
        conv_manager = NaturalConversationManager(admin_id, customer_id)
        response = conv_manager.process_message(user_message, page_name)
        
        return response
        
    except Exception as e:
        logger.error(f"AI Response Error: {str(e)}")
        
        error_responses = [
            "ওহ, একটু সমস্যা হয়েছে 😔 আপনি কী বলছিলেন? আবার একটু বলবেন?",
            "দুঃখিত, একটু technical issue 😊 আবার চেষ্টা করি... আপনি কী বলতে চেয়েছিলেন?",
            "আমার side এ একটু problem 🫶 আপনি আবার বলবেন? ধন্যবাদ 😊"
        ]
        
        return random.choice(error_responses)

# ================= WEBHOOK ROUTES =================
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Facebook verification"""
    try:
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        
        if mode and token:
            response = supabase.table("facebook_integrations")\
                .select("*")\
                .eq("verify_token", token)\
                .execute()
            
            if response.data:
                return challenge, 200
            else:
                return jsonify({"error": "Invalid token"}), 403
        else:
            return jsonify({"error": "Missing parameters"}), 400
            
    except Exception as e:
        logger.error(f"Verification error: {str(e)}")
        return jsonify({"error": "Server error"}), 500

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    """Handle messages"""
    try:
        data = request.get_json()
        
        if not data:
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
                
                if 'message' in event and 'text' in event['message']:
                    message_text = event['message']['text']
                    
                    if not message_text.strip():
                        continue
                    
                    logger.info(f"💬 Message from {sender_id[:10]}...: {message_text[:100]}")
                    
                    client_info = find_client_by_page_id(recipient_id)
                    
                    if client_info:
                        admin_id = client_info["admin_id"]
                        page_info = client_info["page_info"]
                        page_name = page_info.get("page_name", "আমাদের দোকান")
                        page_token = page_info.get("page_access_token")
                        
                        if page_token:
                            typing_on(page_token, sender_id)
                            ai_response = generate_ai_response(admin_id, message_text, sender_id, page_name)
                            typing_off(page_token, sender_id)
                            send_facebook_message(page_token, sender_id, ai_response)
        
        return jsonify({"status": "processed"}), 200
        
    except Exception as e:
        logger.error(f"Webhook error: {str(e)}")
        return jsonify({"error": "processing_error"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"🚀 Starting Facebook AI Bot on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

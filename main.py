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

# ================= NATURAL CONVERSATION MANAGER =================
class NaturalConversationManager:
    """প্রাকৃতিক কথোপকথন ম্যানেজার"""
    
    def __init__(self, admin_id: str, customer_id: str):
        self.admin_id = admin_id
        self.customer_id = customer_id
        self.state_key = f"conv_{admin_id}_{customer_id}"
        self.products = get_products_with_details(admin_id)
        
        if self.state_key not in _conversation_states:
            _conversation_states[self.state_key] = {
                "step": "greeting",
                "intent": None,
                "customer_type": None,
                "priorities": [],
                "last_recommended": None,
                "history": []
            }
        
        self.state = _conversation_states[self.state_key]
    
    def process_message(self, user_message: str) -> str:
        """ইউজার মেসেজ প্রসেস করো"""
        user_message_clean = user_message.strip()
        self.state["history"].append(user_message_clean)
        
        current_step = self.state["step"]
        
        if current_step == "greeting":
            return self._handle_greeting(user_message_clean)
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
            return self._handle_fallback(user_message_clean)
    
    def _handle_greeting(self, message: str) -> str:
        """গ্রিটিং হ্যান্ডেল করো"""
        greetings = [
            "হাই 👋 কেমন আছেন আজ? 😊 কিছু খুঁজছেন নাকি শুধু একটু ঘুরছেন?",
            "নমস্কার 🙏 আপনাকে দেখে ভালো লাগছে! আজকে কীভাবে সাহায্য করতে পারি?",
            "সালাম 🙂 আমাদের পেজে স্বাগতম! কোনো বিশেষ কারণে আসছেন, নাকি হালকা ব্রাউজ করবেন?"
        ]
        
        # যদি জবাব গ্রিটিং হয়
        greeting_responses = ['হাই', 'হ্যালো', 'সালাম', 'ভালো', 'আছি', 'hello', 'hi', 'fine']
        if any(greet in message.lower() for greet in greeting_responses):
            return "ভালো লাগলো আপনার সাথে কথা বলে 😊 তাহলে বলুন, আজকে কি খুঁজছেন? নিজের জন্য নাকি কাউকে গিফট দেবেন?"
        
        self.state["step"] = "understanding"
        return random.choice(greetings)
    
    def _handle_understanding(self, message: str) -> str:
        """ইউজার ইন্টেন্ট বোঝো"""
        message_lower = message.lower()
        
        # ক্রেতার টাইপ চিহ্নিত করো
        if any(word in message_lower for word in ['গিফট', 'উপহার', 'দেব', 'present', 'gift']):
            self.state["customer_type"] = "gift_buyer"
            self.state["step"] = "recommendation"
            return "গিফটের জন্য! ভালো তো 👍 বয়স কত যার জন্য?"
        
        elif any(word in message_lower for word in ['নিজের', 'আমার', 'মার', 'my', 'personal']):
            self.state["customer_type"] = "personal"
            self.state["step"] = "recommendation"
            return "নিজের জন্য! ভালো তো 😊 সাধারণ quality priority নাকি budget friendly option খুঁজছেন?"
        
        elif any(word in message_lower for word in ['অফিস', 'বিজনেস', 'কোম্পানি', 'office', 'business']):
            self.state["customer_type"] = "business"
            self.state["step"] = "recommendation"
            return "বিজনেসের জন্য? office setup নাকি client gift?"
        
        # প্রায়োরিটি চিহ্নিত করো
        elif any(word in message_lower for word in ['দাম', 'কম', 'সস্তা', 'cheap', 'low']):
            self.state["priorities"].append("budget")
            self.state["step"] = "recommendation"
            return "বাজেট friendly option চান? আনুমানিক কত রাখতে চান?"
        
        elif any(word in message_lower for word in ['ভালো', 'quality', 'টেকসই', 'durable']):
            self.state["priorities"].append("quality")
            self.state["step"] = "recommendation"
            return "quality priority? দাম একটু বেশি হলেও চলবে?"
        
        else:
            # সাধারণ ইন্টেন্ট
            self.state["step"] = "recommendation"
            return "একটু বুঝে নিচ্ছি... আপনি কি কোনো নির্দিষ্ট জিনিস খুঁজছেন, নাকি ideas চাচ্ছেন? 😊"
    
    def _handle_recommendation(self, message: str) -> str:
        """মাইক্রো রিকমেন্ডেশন দাও (১-২টা)"""
        # প্রোডাক্ট ফিল্টার করো
        available_products = [p for p in self.products if p.get("in_stock", False) and p.get("stock", 0) > 0]
        
        if not available_products:
            self.state["step"] = "explanation"
            return "দুঃখিত, এখন স্টকে কোনো পণ্য নেই 😔 কিছুক্ষণ পরে আবার চেষ্টা করুন।"
        
        # ক্রেতার টাইপ অনুযায়ী ফিল্টার করো
        filtered_products = self._filter_by_customer_type(available_products)
        
        if not filtered_products:
            filtered_products = available_products[:2]  # max 2 products
        
        # ১ বা ২টা প্রোডাক্ট নির্বাচন করো
        selected_product = random.choice(filtered_products[:2])
        self.state["last_recommended"] = selected_product["name"]
        self.state["step"] = "explanation"
        
        recommendation_phrases = [
            f"আপনার কথাটা শুনে এই option টা সবচেয়ে ভালো match করবে মনে হচ্ছে 👇\n\n✨ **{selected_product['name']}**",
            f"আমার মনে হচ্ছে এটা আপনার জন্য পারফেক্ট হবে 😊\n\n🔥 **{selected_product['name']}**",
            f"অনেক customer আপনার মত need এর জন্য এটা নেয় 👍\n\n🌟 **{selected_product['name']}**"
        ]
        
        return random.choice(recommendation_phrases)
    
    def _handle_explanation(self, message: str) -> str:
        """ইমোশনাল এক্সপ্লেনেশন দাও"""
        product_name = self.state["last_recommended"]
        product = None
        
        for p in self.products:
            if p.get("name") == product_name:
                product = p
                break
        
        if not product:
            self.state["step"] = "soft_cta"
            return "দুঃখিত, পণ্যটি এখন খুঁজে পাচ্ছি না 😔 অন্য কিছু দেখতে চান?"
        
        # প্রোডাক্টের বিবরণ নাও
        description = product.get("description", "")
        price = product.get("price", 0)
        category = product.get("category", "")
        
        # ক্রেতার টাইপ অনুযায়ী ইমোশনাল অ্যাঙ্গেল
        customer_type = self.state.get("customer_type", "personal")
        
        if customer_type == "gift_buyer":
            emotional_angles = [
                f"এই জিনিসটা গিফট দিলে receiver খুব খুশি হবে 😊\nquality ও ভালো, দেখতেও সুন্দর ✨",
                f"উপহার হিসেবে পারফেক্ট—দেখতে unique, ব্যবহারেও practical 🎁",
                f"গিফট হিসেবে অনেক ভালো choice, memory হিসেবে থাকবে দীর্ঘদিন 💝"
            ]
        elif customer_type == "business":
            emotional_angles = [
                f"professional look এ অনেক ভালো যায় 👍\noffice environment এর জন্য suitable",
                f"clients দিলে impression ভালো হয় 💼 quality ও দীর্ঘস্থায়ী",
                f"বিজনেসের জন্য পারফেক্ট—দেখতে premium, ব্যবহারে reliable 😌"
            ]
        else:  # personal
            emotional_angles = [
                f"নিজের জন্য নিলে daily use এ অনেক সুবিধা পাবেন 😊\nলং টার্ম investment",
                f"quality ভালো থাকায় মনও ভালো থাকবে ✨ দীর্ঘদিন service দেবে",
                f"এই জিনিসটা থাকলে routine কাজগুলো সহজ হয়ে যাবে 👍 practical ও stylish"
            ]
        
        # দাম প্রাকৃতিকভাবে যোগ করো
        price_phrase = ""
        if price > 0:
            if "budget" in self.state["priorities"]:
                price_phrase = f"\n\nদামটাও reasonable—৳{price:,} এর জন্য value অনেক 👍"
            else:
                price_phrase = f"\n\nদাম ৳{price:,}—quality এর তুলনায় worth it মনে করি ✨"
        
        self.state["step"] = "soft_cta"
        return f"{random.choice(emotional_angles)}{price_phrase}\n\nকেমন লাগলো আপনার?"
    
    def _handle_soft_cta(self, message: str) -> str:
        """সফট কল টু অ্যাকশন"""
        message_lower = message.lower()
        
        positive_words = ['ভাল', 'লাগল', 'সুন্দর', 'দারুণ', 'চমৎকার', 'good', 'nice', 'like', 'অসাধারণ']
        negative_words = ['দাম', 'expensive', 'কস্টলি', 'বেশি', 'not now', 'পরে', 'later']
        order_words = ['অর্ডার', 'order', 'কিনব', 'buy', 'নিব']
        
        if any(word in message_lower for word in positive_words):
            cta_phrases = [
                "ভালো লাগলো জেনে খুশি হলাম! 😊 আগ্রহ থাকলে আমি অর্ডারটা করে দিতে পারি 🫶",
                "পছন্দ হলে বলবেন, details নিয়ে নিই ✨ আর চাইলে আরেকটা option দেখাবো?",
                "একদম! 👍 যখন ready হবেন, অর্ডার শুরু করে দেব 😊"
            ]
            return random.choice(cta_phrases)
        
        elif any(word in message_lower for word in negative_words):
            self.state["step"] = "objection"
            return "বুঝতে পারছি 🫶 দামটা একটু বেশি লাগছে নাকি?"
        
        elif any(word in message_lower for word in order_words):
            # অর্ডার সেশন শুরু করো
            product_name = self.state["last_recommended"]
            if product_name:
                session = OrderSession(self.admin_id, self.customer_id)
                # প্রোডাক্ট সেট করো
                for p in self.products:
                    if p.get("name") == product_name:
                        session.data["product"] = p["name"]
                        session.data["product_id"] = p.get("id")
                        session.step = 4  # পরিমাণ স্টেপে যাও
                        stock = p.get("stock", 0)
                        price = p.get("price", 0)
                        
                        order_phrases = [
                            f"একদম! 😊\n\n**{p['name']}** এর অর্ডার নিচ্ছি।\n\nদাম: ৳{price:,}\nস্টকে আছে: {stock} পিস\n\nকত পিস চান?",
                            f"বেশ তো! 🫶\n\n**{p['name']}** এর order শুরু করছি।\n\nPrice: ৳{price:,}\nStock: {stock} pieces\n\nQuantity কত হবে?"
                        ]
                        return random.choice(order_phrases)
            
            # যদি প্রোডাক্ট না মিলে
            session = OrderSession(self.admin_id, self.customer_id)
            return session.start_order()
        
        else:
            neutral_phrases = [
                "কী ভাবছেন? 😊 আগ্রহ আছে নাকি অন্য কিছু দেখতে চান?",
                "চিন্তা করবেন না, আপনার সময় নিয়ে বলবেন 🫶",
                "যখন decision নেবেন, আমাকে জানাবেন 👍 আমি এখানেই আছি"
            ]
            return random.choice(neutral_phrases)
    
    def _handle_objection(self, message: str) -> str:
        """অবজেকশন হ্যান্ডেল করো"""
        message_lower = message.lower()
        
        if any(word in message_lower for word in ['দাম', 'মূল্য', 'কস্ট', 'expensive', 'বেশি']):
            objection_responses = [
                "বুঝতে পারছি 🫶 অনেকে প্রথমে এমনটাই ভাবেন। কিন্তু এই জিনিসটা একবার নিলে বারবার change করতে হয় না—এই কারণেই বেশিরভাগ customer এটা নেয় 🙂",
                "দামটা একটু বেশি লাগতে পারে, কিন্তু quality ও durability এর জন্য worth it 👍 দীর্ঘদিন ব্যবহার করলে per day cost খুব কম হয়",
                "আপনার চিন্তা স্বাভাবিক 😊 আমাদের অনেক customer শুরুতে এমনটা ভাবলেও পরে feedback দিয়েছে value for money পেয়েছে"
            ]
        
        elif any(word in message_lower for word in ['কোয়ালিটি', 'ভালো', 'টেকসই', 'quality', 'durable']):
            objection_responses = [
                "ভালো প্রশ্ন! 👍 আমাদের প্রোডাক্টগুলো customer feedback এর উপর base করে select করা। বেশিরভাগই ১+ বছর ভালোভাবে ব্যবহার করছে 😊",
                "quality নিয়ে চিন্তা করাটা ঠিক আছে ✨ আমরা reliable suppliers এর থেকে materials নিই, তাই durability নিশ্চিত",
                "একবার try করলেই quality টা feel করতে পারবেন 🫶 আমাদের return policy ও আছে যদি不满意 হন"
            ]
        
        elif any(word in message_lower for word in ['বিশ্বাস', 'ট্রাস্ট', 'trust', 'confidence']):
            objection_responses = [
                "নতুন জায়গায় চিন্তা হতেই পারে 🫶 আমরা অনেকদিন ধরে reliable service দিয়ে আসছি 😊",
                "আমাদের page এ অনেক reviews আছে, দেখতে পারেন 👍 delivery ও service নিয়ে positive feedback পাই regular",
                "order করলে দেখবেন আমরা কতটা serious service দেই ✨ customer satisfaction আমাদের priority"
            ]
        
        else:
            objection_responses = [
                "ঠিক আছে, কোন সমস্যা নেই 😊 যখন ইচ্ছা হবে, আবার কথা বলবেন। আমি এখানেই আছি 🫶",
                "চিন্তা করবেন না, আপনার convenient time এ 👍 শুভকামনা রইলো! ✨",
                "আপনার decision আমি respect করি 🙏 প্রয়োজন হলে আবার জানাবেন 😊"
            ]
        
        self.state["step"] = "soft_cta"
        return random.choice(objection_responses)
    
    def _handle_fallback(self, message: str) -> str:
        """ফলব্যাক রেসপন্স"""
        fallback_responses = [
            "একটু ভাবছি আপনার কথাটা নিয়ে... 😊 আসলে আমার মনে হচ্ছে আপনি যা খুঁজছেন, তা আমাদের কাছে আছে। একটু বলবেন কী ধরনের জিনিস?",
            "বুঝলাম... 🫶 আমি আপনাকে সেরা option টা suggest করতে চাই। একটু বলবেন, আপনার priority কী?",
            "আমি এখানেই আছি, চিন্তা নেই 🙂 আপনার কী দরকার সেটা একটু clear করলে আমি ভালোভাবে help করতে পারব।"
        ]
        
        self.state["step"] = "understanding"
        return random.choice(fallback_responses)
    
    def _filter_by_customer_type(self, products: List[Dict]) -> List[Dict]:
        """ক্রেতার টাইপ অনুযায়ী প্রোডাক্ট ফিল্টার করো"""
        customer_type = self.state.get("customer_type", "personal")
        priorities = self.state.get("priorities", [])
        
        filtered = []
        
        for product in products:
            product_text = f"{product.get('name', '')} {product.get('description', '')} {product.get('category', '')}".lower()
            
            # ক্রেতার টাইপ অনুযায়ী ম্যাচিং
            if customer_type == "gift_buyer":
                gift_keywords = ['gift', 'উপহার', 'প্রেজেন্ট', 'সৌজন্য', 'বক্স', 'প্যাকেজ']
                if any(keyword in product_text for keyword in gift_keywords):
                    filtered.append(product)
            
            elif customer_type == "business":
                business_keywords = ['office', 'অফিস', 'বিজনেস', 'কোম্পানি', 'professional', 'corporate']
                if any(keyword in product_text for keyword in business_keywords):
                    filtered.append(product)
            
            else:  # personal
                # প্রায়োরিটি অনুযায়ী
                if "budget" in priorities:
                    price = product.get("price", 0)
                    if price < 1000:  # কমদামি প্রোডাক্ট
                        filtered.append(product)
                elif "quality" in priorities:
                    quality_keywords = ['premium', 'হাইকোয়ালিটি', 'best', 'টেকসই', 'durable']
                    if any(keyword in product_text for keyword in quality_keywords):
                        filtered.append(product)
                else:
                    filtered.append(product)
        
        return filtered[:3]  # সর্বোচ্চ ৩টা প্রোডাক্ট
    
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
                
                # প্রোডাক্ট লিস্ট দেখাও
                products_text = self.get_available_products_formatted()
                return f"ফোন নম্বর সংরক্ষিত! 😊\n\n{products_text}\n\nকোন পণ্য নেবেন?", False
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
                products_text = self.get_available_products_formatted()
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
    
    def validate_phone(self, phone: str) -> bool:
        """Validate phone number"""
        phone_clean = re.sub(r'\D', '', phone)
        return len(phone_clean) == 11 and phone_clean.startswith('01')
    
    def get_available_products_formatted(self) -> str:
        """Get available products formatted naturally"""
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
            return "স্টকে থাকা পণ্য:\n\n" + "\n\n".join(available[:5])
        return "দুঃখিত, এখন কোনো পণ্য স্টকে নেই 😔"
    
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

# ================= AI RESPONSE =================
def generate_ai_response(admin_id: str, user_message: str, customer_id: str, page_name: str = "আমাদের দোকান") -> str:
    """Generate natural human-like response"""
    try:
        # Check if first message
        first_message = is_first_message(admin_id, customer_id)
        
        # Detect language
        language = detect_language(user_message)
        
        # Get products
        products = get_products_with_details(admin_id)
        
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
        
        # যদি ইউজার সরাসরি অর্ডার বলতে চায়
        if check_order_keywords(user_message):
            # প্রথমে কনভারসেশন ম্যানেজার দিয়ে চেক করো
            conv_key = f"conv_{admin_id}_{customer_id}"
            if conv_key in _conversation_states:
                state = _conversation_states[conv_key]
                if state.get("last_recommended"):
                    # রিকমেন্ডেড প্রোডাক্ট দিয়ে অর্ডার শুরু করো
                    product_name = state["last_recommended"]
                    session = OrderSession(admin_id, customer_id)
                    for p in products:
                        if p.get("name") == product_name:
                            session.data["product"] = p["name"]
                            session.data["product_id"] = p.get("id")
                            session.step = 2  # ফোন নম্বর স্টেপে যাও (নাম বাদ)
                            return f"একদম! 😊\n\n**{p['name']}** এর অর্ডার নিচ্ছি।\n\nপ্রথমে আপনার নাম বলবেন?", False
            else:
                # সাধারণ অর্ডার শুরু করো
                session = OrderSession(admin_id, customer_id)
                return session.start_order()
        
        # ন্যাচারাল কনভারসেশন ম্যানেজার ব্যবহার করো
        conv_manager = NaturalConversationManager(admin_id, customer_id)
        
        # প্রথম বার্তা হলে বিশেষ গ্রিটিং
        if first_message:
            if language == "bangla":
                first_greetings = [
                    f"হাই 👋 {page_name}-এ আপনাকে স্বাগতম! আমি {BOT_NAME}, আপনার সহায়ক 😊\n\nকীভাবে সাহায্য করতে পারি আজকে?",
                    f"সালাম 🙂 {page_name} পেজে স্বাগতম! আমি {BOT_NAME} 🫶\n\nআজকে কি প্রয়োজন?",
                    f"নমস্কার 🙏 {page_name}-এ স্বাগতম! আমি {BOT_NAME}, আপনার পাশে আছি 😊\n\nকী খুঁজছেন আজকে?"
                ]
            else:
                first_greetings = [
                    f"Hi 👋 Welcome to {page_name}! I'm {BOT_NAME}, your assistant 😊\n\nHow can I help you today?",
                    f"Hello 🙂 Welcome to our page! I'm {BOT_NAME} 🫶\n\nWhat brings you here today?",
                    f"Greetings 🙏 Welcome to {page_name}! I'm {BOT_NAME} here to help 😊\n\nWhat are you looking for?"
                ]
            
            return random.choice(first_greetings)
        
        # নর্মাল কনভারসেশন প্রসেস করো
        response = conv_manager.process_message(user_message)
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

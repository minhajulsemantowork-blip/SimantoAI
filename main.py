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

# ================= HELPER FUNCTIONS =================
def is_first_message(admin_id: str, customer_id: str) -> bool:
    """চেক করো এই গ্রাহকের প্রথম মেসেজ কিনা"""
    cache_key = f"first_{admin_id}_{customer_id}"
    
    if cache_key in _first_message_cache:
        return _first_message_cache[cache_key]
    
    try:
        # Check if chat history exists
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

def get_welcome_response(page_name: str, language: str = "bangla") -> str:
    """Welcome message তৈরি করো"""
    greetings_bangla = [
        "আসসালামু আলাইকুম! 😊",
        "হ্যালো! স্বাগতম! 😊", 
        "নমস্কার! 😊",
        "শুভেচ্ছা! 😊"
    ]
    
    greetings_english = [
        "Hello! Welcome! 😊",
        "Hi there! 😊",
        "Greetings! 😊",
        "Welcome! 😊"
    ]
    
    if language == "bangla":
        greeting = random.choice(greetings_bangla)
        return f"{greeting}\n\n{page_name}-এ আপনাকে স্বাগতম! আমি {BOT_NAME}, আপনার সহায়ক।\n\nকিভাবে সাহায্য করতে পারি?"
    else:
        greeting = random.choice(greetings_english)
        return f"{greeting}\n\nWelcome to {page_name}! I'm {BOT_NAME}, your assistant.\n\nHow can I help you today?"

def handle_greeting_message(user_message: str, page_name: str, language: str) -> Optional[str]:
    """গ্রিটিং মেসেজ handle করো"""
    greetings_bangla = ['হ্যালো', 'হাই', 'আসসালামু', 'সালাম', 'নমস্কার', 'কেমন আছেন', 'কি অবস্থা']
    greetings_english = ['hello', 'hi', 'hey', 'good morning', 'good afternoon', 'good evening']
    
    message_lower = user_message.lower()
    
    if language == "bangla":
        if any(greet in message_lower for greet in greetings_bangla):
            responses = [
                f"আসসালামু আলাইকুম! 😊 {page_name}-এ আপনাকে স্বাগতম! কিভাবে সাহায্য করতে পারি?",
                f"হ্যালো! 😊 {page_name}-এর পণ্য সম্পর্কে জানতে চান?",
                f"নমস্কার! 😊 আমি {BOT_NAME}, আপনার সহায়ক। কিভাবে সাহায্য করতে পারি?"
            ]
            return random.choice(responses)
    else:
        if any(greet in message_lower for greet in greetings_english):
            responses = [
                f"Hello! 😊 Welcome to {page_name}! How can I assist you today?",
                f"Hi there! 😊 I'm {BOT_NAME} from {page_name}. How can I help?",
                f"Greetings! 😊 Welcome to our page. What can I do for you?"
            ]
            return random.choice(responses)
    
    return None

def get_all_products_formatted(admin_id: str) -> str:
    """সকল প্রোডাক্টের লিস্ট ফরম্যাট করে রিটার্ন করো"""
    products = get_products(admin_id)
    
    if not products:
        return "দুঃখিত, এখন কোনো পণ্য পাওয়া যাচ্ছে না।"
    
    # Get only in-stock products for quick view
    in_stock_products = []
    for product in products:
        if product.get("in_stock", False) and product.get("stock", 0) > 0:
            name = product.get("name", "").strip()
            price = product.get("price", 0)
            stock = product.get("stock", 0)
            if name:  # Ensure product name is not empty
                in_stock_products.append(f"• {name} - ৳{price:,} (স্টক: {stock})")
    
    if not in_stock_products:
        return "দুঃখিত, এখন স্টকে কোনো পণ্য নেই। দয়া করে কিছুক্ষণ পরে চেষ্টা করুন।"
    
    response = "🛒 **স্টকে থাকা পণ্য:**\n\n"
    response += "\n".join(in_stock_products[:8])  # Show max 8 products
    
    if len(in_stock_products) > 8:
        response += f"\n\n... আরও {len(in_stock_products) - 8}টি পণ্য স্টকে আছে"
    
    response += "\n\n🔍 নির্দিষ্ট পণ্যের দাম জানতে পণ্যের নাম লিখুন\n🛒 অর্ডার দিতে 'অর্ডার' লিখুন\n📞 আরও তথ্যের জন্য আমাদের কল করুন"
    return response

def check_price_query(text: str, products: List[Dict]) -> Tuple[bool, Optional[str]]:
    """চেক করো গ্রাহক দাম জানতে চাচ্ছে কিনা"""
    price_keywords = ['দাম', 'price', 'কত', 'কোস্ট', 'cost', 'টাকা', 'মূল্য']
    text_lower = text.lower()
    
    # First check if it's a general price query
    if any(keyword in text_lower for keyword in price_keywords):
        # Check if specific product is mentioned
        for product in products:
            product_name = product.get("name", "").lower().strip()
            if product_name and product_name in text_lower:
                price = product.get("price", 0)
                stock = product.get("stock", 0)
                in_stock = product.get("in_stock", False)
                
                if in_stock and stock > 0:
                    return True, f"{product['name']} এর দাম ৳{price:,}। স্টকে আছে {stock} পিস। অর্ডার দিতে চান?"
                else:
                    return True, f"{product['name']} এর দাম ৳{price:,}। কিন্তু এখন স্টকে নেই।"
        
        # If no specific product mentioned, show all products
        return True, None
    
    return False, None

def find_product_in_query(text: str, products: List[Dict]) -> Optional[Dict]:
    """কোয়েরিতে পণ্য খুঁজে বের করো"""
    text_lower = text.lower().strip()
    
    for product in products:
        product_name = product.get("name", "").lower().strip()
        
        if not product_name:
            continue
            
        # Exact match
        if product_name in text_lower:
            return product
        
        # Partial match
        product_words = product_name.split()
        if any(word in text_lower for word in product_words if len(word) > 3):
            return product
    
    return None

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
        self.products = get_products(admin_id)
    
    def start_order(self):
        """Start order collection"""
        self.step = 1
        _order_sessions[self.session_id] = self
        return "অর্ডার নেওয়া শুরু করছি! প্রথমে আপনার নাম বলুন:"
    
    def process_response(self, user_message: str) -> Tuple[str, bool]:
        """Process user response"""
        completed = False
        
        if self.step == 1:  # Name
            self.data["name"] = user_message.strip()
            self.step = 2
            return "ধন্যবাদ! এখন আপনার ফোন নম্বর দিন (যেমন: 017XXXXXXXX):", False
            
        elif self.step == 2:  # Phone
            phone = user_message.strip()
            if self.validate_phone(phone):
                self.data["phone"] = phone
                self.step = 3
                products_text = self.get_available_products()
                return f"ফোন নম্বর সংরক্ষিত! কোন পণ্য অর্ডার করতে চান?\n\n{products_text}\n\nপণ্যের নাম লিখুন:", False
            else:
                return "দুঃখিত, সঠিক ফোন নম্বর দিন (যেমন: 017XXXXXXXX):", False
                
        elif self.step == 3:  # Product
            selected_product = self.find_product(user_message)
            if selected_product:
                self.data["product"] = selected_product["name"]
                self.data["product_id"] = selected_product.get("id")
                self.step = 4
                stock = selected_product.get("stock", 0)
                price = selected_product.get("price", 0)
                return f"{selected_product['name']} নির্বাচিত! (৳{price:,})\n\nকত পিস চান? (স্টকে আছে: {stock} পিস):", False
            else:
                products_text = self.get_available_products()
                return f"পণ্যটি খুঁজে পাইনি। আবার চেষ্টা করুন:\n\n{products_text}\n\nপণ্যের নাম লিখুন:", False
                
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
                            return f"{quantity} পিস নির্বাচিত! মোট মূল্য: ৳{self.data['total']:,}\n\nএখন আপনার ডেলিভারি ঠিকানা দিন (বিস্তারিত):", False
                        else:
                            return f"দুঃখিত, স্টকে মাত্র {stock} পিস আছে। কম সংখ্যক দিন:", False
                else:
                    return "দুঃখিত, ১ বা তার বেশি সংখ্যা দিন:", False
            else:
                return "দুঃখিত, সংখ্যা দিন (যেমন: 1, 2, 3):", False
                
        elif self.step == 5:  # Address
            self.data["address"] = user_message.strip()
            self.step = 6
            summary = self.get_order_summary()
            return f"ঠিকানা সংরক্ষিত!\n\n{summary}\n\nঅর্ডার কনফার্ম করতে শুধুমাত্র 'confirm' লিখুন।\nঅন্য কিছু লিখলে অর্ডার বাতিল হবে।", False
            
        elif self.step == 6:  # Confirm
            response_lower = user_message.lower().strip()
            # শুধুমাত্র 'confirm' লিখলেই অর্ডার কনফার্ম হবে
            if response_lower == 'confirm':
                order_saved = self.save_order()
                if order_saved:
                    completed = True
                    order_id = self.data.get("order_id", "")
                    return f"✅ অর্ডার সফলভাবে কনফার্ম হয়েছে!\n\nঅর্ডার আইডি: {order_id}\n\nআমরা শীঘ্রই আপনার সাথে যোগাযোগ করব। ধন্যবাদ! 😊\n\nঅন্যান্য পণ্য দেখতে যেকোনো মেসেজ দিন।", True
                else:
                    return "❌ অর্ডার সেভ করতে সমস্যা হয়েছে। দয়া করে আবার চেষ্টা করুন।", True
            else:
                completed = True
                return "অর্ডার বাতিল হয়েছে। আবার অর্ডার দিতে 'অর্ডার' লিখুন।", True
        
        return "কিছু সমস্যা হয়েছে। আবার চেষ্টা করুন।", True
    
    def validate_phone(self, phone: str) -> bool:
        """Validate phone number"""
        phone_clean = re.sub(r'\D', '', phone)
        return len(phone_clean) == 11 and phone_clean.startswith('01')
    
    def get_available_products(self) -> str:
        """Get available products"""
        available = []
        for product in self.products:
            if product.get("in_stock", False) and product.get("stock", 0) > 0:
                name = product.get("name", "").strip()
                if name:  # Check if name is not empty
                    price = product.get("price", 0)
                    stock = product.get("stock", 0)
                    available.append(f"- {name} (৳{price:,}, স্টক: {stock})")
        
        if available:
            return "স্টকে থাকা পণ্য:\n" + "\n".join(available[:6])
        return "দুঃখিত, এখন কোনো পণ্য স্টকে নেই।"
    
    def find_product(self, query: str) -> Optional[Dict]:
        """Find product"""
        if not query:
            return None
            
        query_lower = query.lower().strip()
        
        for product in self.products:
            name = product.get("name", "").lower().strip()
            if name and query_lower in name:
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

def get_products(admin_id: str) -> List[Dict]:
    """Get products"""
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
                "price": product.get("price", 0),
                "stock": product.get("stock", 0),
                "in_stock": product.get("in_stock", False)
            })
        
        _product_cache[cache_key] = formatted_products
        return formatted_products
        
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
    order_keywords = ['অর্ডার', 'order', 'কিনব', 'buy', 'নিব', 'চাই']
    return any(keyword in text.lower() for keyword in order_keywords)

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
    try:
        # Check if first message
        first_message = is_first_message(admin_id, customer_id)
        
        # Detect language
        language = detect_language(user_message)
        
        # Get products
        products = get_products(admin_id)
        
        # Handle first message
        if first_message:
            greeting_response = handle_greeting_message(user_message, page_name, language)
            if greeting_response:
                return greeting_response
            
            # If not greeting, show welcome
            return get_welcome_response(page_name, language)
        
        # Check if in order session
        session_id = f"order_{admin_id}_{customer_id}"
        if session_id in _order_sessions:
            session = _order_sessions[session_id]
            response, completed = session.process_response(user_message)
            if completed:
                session.cancel()
            return response
        
        # Check price query
        is_price_query, price_response = check_price_query(user_message, products)
        if is_price_query:
            if price_response:
                return price_response
            else:
                # General price query - show all products
                return get_all_products_formatted(admin_id)
        
        # Check specific product query
        product = find_product_in_query(user_message, products)
        if product:
            price = product.get("price", 0)
            stock = product.get("stock", 0)
            in_stock = product.get("in_stock", False)
            
            if language == "bangla":
                if in_stock and stock > 0:
                    return f"{product['name']} এর দাম ৳{price:,}। স্টকে আছে {stock} পিস।\n\nঅর্ডার দিতে 'অর্ডার' লিখুন।\nআরও তথ্যের জন্য আমাদের কল করুন।\nঅন্যান্য পণ্য দেখতে পণ্যের নাম লিখুন।"
                else:
                    return f"{product['name']} এর দাম ৳{price:,}। কিন্তু এখন স্টকে নেই।\n\nঅন্যান্য পণ্য দেখতে পণ্যের নাম লিখুন।"
            else:
                if in_stock and stock > 0:
                    return f"{product['name']} price is ৳{price:,}. Stock: {stock} pieces.\n\nType 'order' to purchase.\nCall us for more information.\nType product name for other products."
                else:
                    return f"{product['name']} price is ৳{price:,}. Currently out of stock.\n\nType product name for other products."
        
        # Check order request
        if check_order_keywords(user_message):
            session = OrderSession(admin_id, customer_id)
            return session.start_order()
        
        # Normal AI response
        api_key = get_groq_key(admin_id)
        if not api_key:
            return "দুঃখিত, সেবা সাময়িকভাবে বন্ধ আছে। দয়া করে কিছুক্ষণ পরে আবার চেষ্টা করুন।"
        
        client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=api_key)
        
        # Count products for context
        total_products = len(products)
        in_stock_count = sum(1 for p in products if p.get("in_stock", False) and p.get("stock", 0) > 0)
        
        if language == 'bangla':
            system_prompt = f"""তুমি {BOT_NAME}, {page_name}-এর সহকারী।

নিয়মাবলী:
১. বন্ধুত্বপূর্ণ ও সহায়ক হও
২. পণ্যের দাম ও স্টক জানালে বলো
৩. অর্ডার নিতে সাহায্য করো
৪. ৪-৫ লাইনের মধ্যে উত্তর দাও
৫. সংক্ষিপ্ত কিন্তু পূর্ণাঙ্গ উত্তর দাও

মোট পণ্য: {total_products}টি (স্টকে: {in_stock_count}টি)

গ্রাহক: "{user_message}"
তুমি:"""
        else:
            system_prompt = f"""You are {BOT_NAME}, assistant of {page_name}.

Rules:
1. Be friendly and helpful
2. Provide product prices and stock
3. Help with orders
4. Give answers in 4-5 lines
5. Keep answers short but complete

Total products: {total_products} (In stock: {in_stock_count})

Customer: "{user_message}"
You:"""
        
        messages = [{"role": "system", "content": system_prompt}]
        messages.append({"role": "user", "content": user_message})
        
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.7,
            max_tokens=250,  # Increased from 100 to 250 for longer responses
            top_p=0.9
        )
        
        ai_response = response.choices[0].message.content.strip()
        return ai_response
        
    except Exception as e:
        logger.error(f"AI Response Error: {str(e)}")
        return "দুঃখিত, সমস্যা হয়েছে। আবার চেষ্টা করুন। আমাদের কল করুন সরাসরি কথা বলতে।"

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

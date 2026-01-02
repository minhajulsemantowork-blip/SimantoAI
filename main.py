import os
import re
import json
import logging
import time
from typing import Optional, Dict, List, Tuple, Any, Union
from datetime import datetime, timedelta
from functools import lru_cache
from flask import Flask, request, jsonify
from openai import OpenAI
from supabase import create_client, Client
from difflib import SequenceMatcher
import requests
from urllib.parse import quote

# ================= CONFIG =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
app = Flask(__name__)

# Configuration - NO HARDCODED BUSINESS DATA
class Config:
    # Groq API Configuration ONLY
    GROQ_BASE_URL = "https://api.groq.com/openai/v1"
    GROQ_MODEL = "llama-3.1-8b-instant"  # Free tier compatible model
    MAX_TOKENS = 800  # Reduced for free tier limits
    TEMPERATURE = 0.2
    
    # Sales optimization settings
    MAX_CHAT_HISTORY = 10  # Reduced for free tier
    MAX_MESSAGE_LENGTH = 400  # Reduced to avoid long responses
    MAX_SYSTEM_PROMPT_TOKENS = 800  # Reduced for free tier
    SESSION_TIMEOUT_HOURS = 3
    ABANDONED_CART_REMINDER_HOURS = 6
    
    # Sales psychology triggers
    URGENCY_KEYWORDS = ['আজ', 'এখন', 'দ্রুত', 'সীমিত', 'শেষ', 'অফার']
    SOCIAL_PROOF_KEYWORDS = ['জনপ্রিয়', 'বিক্রিত', 'রিভিউ', 'রেটিং', 'ক্রেতা']
    SCARCITY_KEYWORDS = ['স্টক শেষ', 'সীমিত সময়', 'শেষ চান্স', 'ফিনিশিং']

# ================= SUPABASE =================
try:
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    
    if not supabase_url or not supabase_key:
        logger.error("Supabase credentials missing")
        supabase = None
    else:
        supabase: Client = create_client(supabase_url, supabase_key)
        
        # Test connection
        supabase.table("products").select("id").limit(1).execute()
        logger.info("Supabase client initialized successfully")
        
except Exception as e:
    logger.error(f"Supabase Client Error: {e}")
    supabase = None

# ================= API VALIDATION =================
def validate_groq_api_key(api_key: str) -> Tuple[bool, str]:
    """Validate Groq API key format and test"""
    if not api_key or not isinstance(api_key, str):
        return False, "Invalid API key type"
    
    if not api_key.startswith('gsk_'):
        logger.warning(f"Invalid API key format: {api_key[:10]}...")
        return False, "Invalid API key format"
    
    # Test the API key
    try:
        test_client = OpenAI(
            base_url=Config.GROQ_BASE_URL,
            api_key=api_key
        )
        test_client.chat.completions.create(
            model=Config.GROQ_MODEL,
            messages=[{"role": "user", "content": "test"}],
            max_tokens=5,
            timeout=5
        )
        return True, "Valid API key"
    except Exception as e:
        logger.error(f"API key test failed: {e}")
        return False, f"API key test failed: {str(e)[:50]}"

def call_groq_with_retry(client, messages, tools=None, max_retries=3):
    """Call Groq API with retry mechanism - optimized for free tier"""
    for attempt in range(max_retries):
        try:
            params = {
                "model": Config.GROQ_MODEL,
                "messages": messages,
                "temperature": Config.TEMPERATURE,
                "max_tokens": Config.MAX_TOKENS,
                "timeout": 15,
                "stream": False
            }
            
            if tools and attempt == 0:
                params["tools"] = tools
                params["tool_choice"] = "auto"
            
            response = client.chat.completions.create(**params)
            
            # Log token usage for optimization
            if hasattr(response, 'usage'):
                logger.info(f"Token usage: {response.usage}")
            
            return response
        except requests.exceptions.Timeout:
            logger.warning(f"Groq API timeout (attempt {attempt + 1}/{max_retries})")
            if attempt == max_retries - 1:
                raise
            time.sleep(1.5 ** attempt)
        except Exception as e:
            logger.error(f"Groq API error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                raise
            time.sleep(1)
    
    return None

# ================= HELPERS =================
def get_page_client_cached(page_id: str) -> Optional[Dict]:
    """Fetch Facebook page integration details"""
    if supabase is None:
        return None
    
    try:
        res = supabase.table("facebook_integrations") \
            .select("*") \
            .eq("page_id", str(page_id)) \
            .eq("is_connected", True) \
            .eq("is_active", True) \
            .single().execute()
        
        if res.data:
            logger.info(f"Page found: {res.data.get('page_name')}")
            return res.data
        return None
    except Exception as e:
        logger.error(f"Error fetching page client: {e}")
        return None

def send_message_with_buttons(token: str, user_id: str, text: str, buttons: List[Dict] = None) -> bool:
    """Send message with quick reply buttons for better UX"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            url = f"https://graph.facebook.com/v18.0/me/messages?access_token={token}"
            
            payload = {
                "recipient": {"id": user_id},
                "message": {"text": text[:640]}
            }
            
            if buttons and len(buttons) > 0:
                quick_replies = []
                for btn in buttons[:11]:
                    quick_replies.append({
                        "content_type": "text",
                        "title": btn.get("title", "Button")[:20],
                        "payload": btn.get("payload", "BUTTON")
                    })
                
                payload["message"]["quick_replies"] = quick_replies
            
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            
            result = response.json()
            if 'error' in result:
                logger.error(f"Facebook API error: {result['error']}")
                return False
            
            logger.info(f"Message sent to {user_id}")
            return True
            
        except requests.exceptions.Timeout:
            logger.warning(f"Facebook API timeout (attempt {attempt + 1}/{max_retries})")
            if attempt == max_retries - 1:
                return False
            time.sleep(1)
        except Exception as e:
            logger.error(f"Facebook API Error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                return False
            time.sleep(1)
    return False

def send_message(token: str, user_id: str, text: str) -> bool:
    """Send simple text message"""
    return send_message_with_buttons(token, user_id, text)

def get_products_with_details_cached(admin_id: str) -> List[Dict]:
    """Fetch all products for a specific business"""
    if supabase is None:
        return []
    
    try:
        res = supabase.table("products") \
            .select("id, name, price, stock, category, description, in_stock, image_url, tags, is_featured, discount_price, unit") \
            .eq("user_id", admin_id) \
            .eq("in_stock", True) \
            .eq("is_active", True) \
            .order("is_featured", desc=True) \
            .order("category") \
            .execute()
        
        products = res.data or []
        
        logger.info(f"Fetched {len(products)} products for admin {admin_id}")
        return products
    except Exception as e:
        logger.error(f"Error fetching products: {e}")
        return []

def get_product_by_name(admin_id: str, product_name: str) -> Optional[Dict]:
    """Find product by name with fuzzy matching"""
    try:
        products = get_products_with_details_cached(admin_id)
        if not products:
            return None
        
        query = product_name.strip().lower()
        
        for product in products:
            if product["name"].strip().lower() == query:
                return product
        
        featured_matches = []
        regular_matches = []
        
        for product in products:
            prod_name = product["name"].lower()
            if query in prod_name:
                if product.get("is_featured"):
                    featured_matches.append(product)
                else:
                    regular_matches.append(product)
        
        if featured_matches:
            return featured_matches[0]
        elif regular_matches:
            return regular_matches[0]
        
        best_match = None
        best_ratio = 0
        
        for product in products:
            ratio = SequenceMatcher(None, query, product["name"].lower()).ratio()
            if ratio > best_ratio and ratio > 0.5:
                best_ratio = ratio
                best_match = product
        
        return best_match
    except Exception as e:
        logger.error(f"Error finding product: {e}")
        return None

def find_faq_cached(admin_id: str, user_msg: str) -> Optional[str]:
    """Find FAQ answer using similarity matching"""
    if supabase is None:
        return None
    
    try:
        res = supabase.table("faqs") \
            .select("question, answer, priority") \
            .eq("user_id", admin_id) \
            .eq("is_active", True) \
            .order("priority", desc=True) \
            .execute()

        faqs = res.data or []
        best_ratio = 0.6
        best_answer = None
        
        for faq in faqs:
            ratio = SequenceMatcher(None, user_msg.lower(), faq["question"].lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_answer = faq["answer"]
        
        return best_answer
    except Exception as e:
        logger.error(f"Error finding FAQ: {e}")
        return None

def get_business_settings_cached(admin_id: str) -> Optional[Dict]:
    """Fetch business settings from database"""
    if supabase is None:
        return None
    
    try:
        res = supabase.table("business_settings") \
            .select("*") \
            .eq("user_id", admin_id) \
            .limit(1) \
            .execute()
        
        if res.data:
            logger.info(f"Business settings loaded for {admin_id}")
            return res.data[0]
        return None
    except Exception as e:
        logger.error(f"Error fetching business settings: {e}")
        return None

def get_sales_promotions_cached(admin_id: str) -> List[Dict]:
    """Get active sales promotions"""
    if supabase is None:
        return []
    
    try:
        now = datetime.utcnow().isoformat()
        res = supabase.table("promotions") \
            .select("*") \
            .eq("user_id", admin_id) \
            .eq("is_active", True) \
            .lte("start_date", now) \
            .gte("end_date", now) \
            .order("priority", desc=True) \
            .execute()
        
        return res.data or []
    except Exception as e:
        logger.error(f"Error fetching promotions: {e}")
        return []

def get_business_address_from_db(admin_id: str) -> Optional[str]:
    """Get business address from business_settings table"""
    try:
        business = get_business_settings_cached(admin_id)
        if business and business.get("address"):
            return business["address"]
        return None
    except Exception as e:
        logger.error(f"Error getting business address: {e}")
        return None

def parse_delivery_info(delivery_info_text: str) -> Optional[Dict]:
    """Parse delivery info from text field"""
    if not delivery_info_text:
        return None
    
    try:
        result = {}
        
        if delivery_info_text.startswith('{') and delivery_info_text.endswith('}'):
            try:
                parsed = json.loads(delivery_info_text)
                result.update(parsed)
                return result
            except:
                pass
        
        lines = delivery_info_text.split('\n')
        for line in lines:
            if ':' in line:
                key, value = line.split(':', 1)
                key_lower = key.strip().lower()
                value = value.strip()
                
                if any(word in key_lower for word in ['charge', 'ফি', 'মূল্য', 'ডেলিভারি', 'চার্জ']):
                    numbers = re.findall(r'\d+', value.replace(',', ''))
                    if numbers:
                        result['delivery_charge'] = int(numbers[0])
                
                elif any(word in key_lower for word in ['free', 'ফ্রি', 'থ্রেশহোল্ড', 'থ্রেসহোল্ড']):
                    numbers = re.findall(r'\d+', value.replace(',', ''))
                    if numbers:
                        result['free_delivery_threshold'] = int(numbers[0])
                
                elif any(word in key_lower for word in ['area', 'এলাকা', 'সার্ভিস', 'সেবা']):
                    areas = [area.strip() for area in value.split(',') if area.strip()]
                    if areas:
                        result['delivery_areas'] = areas
                
                elif any(word in key_lower for word in ['time', 'সময়', 'সাময়', 'ঘন্টা', 'hour']):
                    result['delivery_time'] = value
                
                elif any(word in key_lower for word in ['payment', 'পেমেন্ট', 'পরিশোধ', 'টাকা']):
                    methods = [method.strip() for method in value.split(',') if method.strip()]
                    if methods:
                        result['payment_methods'] = methods
                
                elif any(word in key_lower for word in ['min', 'minimum', 'ন্যূনতম', 'কমপক্ষে']):
                    numbers = re.findall(r'\d+', value.replace(',', ''))
                    if numbers:
                        result['minimum_order'] = int(numbers[0])
        
        return result if result else None
    except Exception as e:
        logger.error(f"Error parsing delivery info: {e}")
        return None

def parse_opening_hours(opening_hours_text: str) -> Optional[Dict]:
    """Parse opening hours from text field"""
    if not opening_hours_text:
        return None
    
    try:
        hours = {}
        lines = opening_hours_text.split('\n')
        
        for line in lines:
            if ':' in line:
                parts = line.split(':', 1)
                if len(parts) == 2:
                    day = parts[0].strip()
                    time = parts[1].strip()
                    hours[day] = time
        
        return hours if hours else None
    except Exception as e:
        logger.error(f"Error parsing opening hours: {e}")
        return None

def get_delivery_info_from_db(admin_id: str) -> Optional[Dict]:
    """Get delivery information from business_settings table"""
    try:
        business = get_business_settings_cached(admin_id)
        if not business or not business.get("delivery_info"):
            return None
        
        info = parse_delivery_info(business["delivery_info"])
        
        if info:
            if 'delivery_charge' not in info:
                info['delivery_charge'] = 0
            if 'delivery_time' not in info:
                info['delivery_time'] = '১-৩ ঘন্টা'
        
        return info
    except Exception as e:
        logger.error(f"Error getting delivery info: {e}")
        return None

def get_opening_hours_from_db(admin_id: str) -> Optional[Dict]:
    """Get opening hours from business_settings table"""
    try:
        business = get_business_settings_cached(admin_id)
        if not business or not business.get("opening_hours"):
            return None
        
        return parse_opening_hours(business["opening_hours"])
    except Exception as e:
        logger.error(f"Error getting opening hours: {e}")
        return None

def get_payment_methods_from_db(admin_id: str) -> List[str]:
    """Get payment methods from business_settings table"""
    try:
        business = get_business_settings_cached(admin_id)
        payment_methods = []
        
        if business and business.get("payment_methods"):
            if isinstance(business["payment_methods"], list):
                payment_methods = business["payment_methods"]
            elif isinstance(business["payment_methods"], str):
                payment_methods = [method.strip() for method in business["payment_methods"].split(',') if method.strip()]
        
        if not payment_methods:
            delivery_info = get_delivery_info_from_db(admin_id)
            if delivery_info and delivery_info.get("payment_methods"):
                payment_methods = delivery_info["payment_methods"]
        
        if not payment_methods:
            payment_methods = ["ক্যাশ অন ডেলিভারি", "বিকাশ", "নগদ"]
        
        return payment_methods
    except Exception as e:
        logger.error(f"Error getting payment methods: {e}")
        return ["ক্যাশ অন ডেলিভারি", "বিকাশ", "নগদ"]

# ================= ORDER SESSION MANAGEMENT =================
def get_or_create_order_session(admin_id: str, customer_id: str) -> Optional[Dict]:
    """Get existing order session or create new"""
    if supabase is None:
        return None
    
    try:
        res = supabase.table("order_sessions") \
            .select("*") \
            .eq("user_id", admin_id) \
            .eq("customer_id", customer_id) \
            .neq("status", "completed") \
            .neq("status", "cancelled") \
            .gt("expires_at", datetime.utcnow().isoformat()) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        
        if res.data and res.data[0]:
            logger.info(f"Existing session found for {customer_id}")
            return res.data[0]
        
        expires_at = datetime.utcnow() + timedelta(hours=Config.SESSION_TIMEOUT_HOURS)
        
        new_session = {
            "user_id": admin_id,
            "customer_id": customer_id,
            "status": "collecting_info",
            "current_step": "greeting",
            "collected_data": {},
            "cart_items": [],
            "total_amount": 0,
            "delivery_charge": 0,
            "expires_at": expires_at.isoformat(),
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
            "message_count": 0
        }
        
        res = supabase.table("order_sessions").insert(new_session).execute()
        
        if res.data and res.data[0]:
            logger.info(f"New session created for {customer_id}")
            return res.data[0]
        
        return None
        
    except Exception as e:
        logger.error(f"Order session error: {e}")
        return None

def update_order_session(session_id: str, updates: Dict) -> bool:
    """Update order session"""
    if supabase is None:
        return False
    
    try:
        updates["updated_at"] = datetime.utcnow().isoformat()
        
        res = supabase.table("order_sessions") \
            .update(updates) \
            .eq("id", session_id) \
            .execute()
        
        return bool(res.data)
    except Exception as e:
        logger.error(f"Update session error: {e}")
        return False

def complete_order_session(admin_id: str, customer_id: str) -> bool:
    """Mark order session as completed"""
    if supabase is None:
        return False
    
    try:
        res = supabase.table("order_sessions") \
            .update({
                "status": "completed",
                "updated_at": datetime.utcnow().isoformat()
            }) \
            .eq("user_id", admin_id) \
            .eq("customer_id", customer_id) \
            .neq("status", "completed") \
            .neq("status", "cancelled") \
            .execute()
        
        logger.info(f"Order session completed for {customer_id}")
        return bool(res.data)
    except Exception as e:
        logger.error(f"Complete session error: {e}")
        return False

def save_order_to_db(order_data: Dict) -> Optional[Dict]:
    """Save order to database"""
    if supabase is None:
        return None
    
    try:
        # Add order number
        order_count = supabase.table("orders") \
            .select("id", count="exact") \
            .eq("user_id", order_data["user_id"]) \
            .execute()
        
        order_number = f"ORD-{order_data['user_id'][:4]}-{order_count.count + 1:04d}"
        order_data["order_number"] = order_number
        
        res = supabase.table("orders").insert(order_data).execute()
        
        if res.data and res.data[0]:
            logger.info(f"Order saved: {order_number}")
            
            # Update product stock
            items = order_data.get("items", [])
            if isinstance(items, str):
                try:
                    items = json.loads(items)
                except:
                    items = []
            
            for item in items:
                if isinstance(item, dict) and "product_id" in item:
                    supabase.table("products") \
                        .update({"stock": supabase.table("products").column("stock") - item.get("quantity", 1)}) \
                        .eq("id", item["product_id"]) \
                        .execute()
            
            return res.data[0]
        
        return None
    except Exception as e:
        logger.error(f"Save order error: {e}")
        return None

def check_abandoned_carts():
    """Check and notify about abandoned carts"""
    if supabase is None:
        return
    
    try:
        cutoff_time = datetime.utcnow() - timedelta(hours=Config.ABANDONED_CART_REMINDER_HOURS)
        
        abandoned_sessions = supabase.table("order_sessions") \
            .select("user_id, customer_id, cart_items, total_amount") \
            .eq("status", "items_added") \
            .lt("updated_at", cutoff_time.isoformat()) \
            .execute()
        
        for session in abandoned_sessions.data or []:
            if session.get("cart_items") and len(session["cart_items"]) > 0:
                integrations = supabase.table("facebook_integrations") \
                    .select("page_access_token") \
                    .eq("user_id", session["user_id"]) \
                    .single() \
                    .execute()
                
                if integrations.data:
                    token = integrations.data["page_access_token"]
                    message = "🛒 আপনার কার্টে কিছু পণ্য রেখে গেছেন! এখনই অর্ডার কমপ্লিট করুন।"
                    send_message(token, session["customer_id"], message)
                    
                    supabase.table("order_sessions") \
                        .update({"status": "abandoned_reminded"}) \
                        .eq("user_id", session["user_id"]) \
                        .eq("customer_id", session["customer_id"]) \
                        .execute()
    
    except Exception as e:
        logger.error(f"Abandoned cart check error: {e}")

# ================= CHAT MEMORY MANAGEMENT =================
def get_chat_memory(admin_id: str, customer_id: str) -> List[Dict]:
    """Get recent chat history for context"""
    if supabase is None:
        return []
    
    try:
        res = supabase.table("chat_history") \
            .select("messages") \
            .eq("user_id", admin_id) \
            .eq("customer_id", customer_id) \
            .limit(1) \
            .execute()
        
        if res.data and res.data[0] and res.data[0].get("messages"):
            messages = res.data[0]["messages"]
            
            user_messages = [msg for msg in messages if msg.get("role") != "system"]
            return user_messages[-Config.MAX_CHAT_HISTORY:]
        
        return []
    except Exception as e:
        logger.error(f"Error fetching chat memory: {e}")
        return []

def save_chat_memory(admin_id: str, customer_id: str, messages: List[Dict]):
    """Save chat history"""
    if supabase is None:
        return
    
    try:
        messages = messages[-(Config.MAX_CHAT_HISTORY * 2):]
        
        total_size = sum(len(str(msg.get('content', ''))) for msg in messages)
        if total_size > 8000:
            messages = messages[-Config.MAX_CHAT_HISTORY:]
        
        data = {
            "user_id": admin_id,
            "customer_id": customer_id,
            "messages": messages,
            "last_updated": datetime.utcnow().isoformat()
        }
        
        try:
            supabase.table("chat_history").upsert(
                data,
                on_conflict="user_id,customer_id"
            ).execute()
        except:
            supabase.table("chat_history") \
                .delete() \
                .eq("user_id", admin_id) \
                .eq("customer_id", customer_id) \
                .execute()
            
            supabase.table("chat_history").insert(data).execute()
        
    except Exception as e:
        logger.error(f"Memory Error: {e}")

# ================= ORDER VALIDATION =================
def validate_order_data(order_data: Dict, admin_id: str) -> Tuple[bool, str]:
    """Validate order data before saving"""
    try:
        phone = order_data.get("phone", "").strip()
        if not re.match(r'^01[3-9]\d{8}$', phone):
            return False, "দুঃখিত, সঠিক ১১ ডিজিটের মোবাইল নম্বর দিন"
        
        address = order_data.get("address", "").strip()
        if len(address) < 10:
            return False, "দুঃখিত, পূর্ণাঙ্গ ঠিকানা দিন"
        
        name = order_data.get("name", "").strip()
        if len(name) < 2:
            return False, "দুঃখিত, পূর্ণ নাম দিন"
        
        items = order_data.get("items", [])
        if not items or len(items) == 0:
            return False, "দুঃখিত, পণ্য প্রয়োজন"
        
        delivery_info = get_delivery_info_from_db(admin_id)
        if delivery_info and delivery_info.get("delivery_areas"):
            delivery_areas = delivery_info.get("delivery_areas", [])
            area_found = False
            
            address_lower = address.lower()
            for area in delivery_areas:
                if area and area.lower() in address_lower:
                    area_found = True
                    break
            
            if not area_found and delivery_areas:
                areas_str = ", ".join(delivery_areas[:3])
                if len(delivery_areas) > 3:
                    areas_str += f" এবং {len(delivery_areas) - 3} টি এলাকা"
                return False, f"দুঃখিত, আমরা {areas_str}-এ ডেলিভারি দেই।"
        
        if delivery_info and delivery_info.get("minimum_order"):
            total_amount = order_data.get("total_amount", 0)
            if total_amount < delivery_info.get("minimum_order", 0):
                return False, f"ন্যূনতম অর্ডার {delivery_info['minimum_order']} টাকা।"
        
        return True, "ভালিডেশন সফল"
        
    except Exception as e:
        logger.error(f"Validation error: {e}")
        return False, "ভেরিফিকেশনে সমস্যা হয়েছে"

def calculate_delivery_charge(admin_id: str, total_amount: int) -> int:
    """Calculate delivery charge based on business settings"""
    try:
        delivery_info = get_delivery_info_from_db(admin_id)
        if not delivery_info:
            return 0
        
        delivery_charge = delivery_info.get("delivery_charge", 0)
        free_threshold = delivery_info.get("free_delivery_threshold")
        
        if free_threshold and total_amount >= free_threshold:
            return 0
        
        return delivery_charge
    except Exception as e:
        logger.error(f"Error calculating delivery charge: {e}")
        return 0

# ================= SALES OPTIMIZATION =================
def apply_sales_psychology(text: str, admin_id: str) -> str:
    """Apply sales psychology principles to responses"""
    try:
        promotions = get_sales_promotions_cached(admin_id)
        
        for keyword in Config.URGENCY_KEYWORDS:
            if keyword in text.lower():
                if promotions:
                    text += f"\n\n⚡ {promotions[0].get('title', '')}"
                else:
                    text += "\n\n⚡ দ্রুত অর্ডার করুন!"
                break
        
        return text
    except Exception as e:
        logger.error(f"Sales psychology error: {e}")
        return text

def suggest_related_products(admin_id: str, current_product: Dict = None) -> str:
    """Suggest related products for upselling"""
    try:
        products = get_products_with_details_cached(admin_id)
        if len(products) < 2:
            return ""
        
        if current_product:
            category = current_product.get("category")
            related = [p for p in products if p.get("category") == category and p["id"] != current_product["id"]]
        else:
            related = [p for p in products if p.get("is_featured")]
        
        if not related or len(related) == 0:
            return ""
        
        related = related[:3]
        
        suggestion = "\n\n🛍️ **এগুলিও দেখুন:**\n"
        for i, prod in enumerate(related, 1):
            price = prod.get('price', 0)
            discount = prod.get('discount_price')
            
            if discount and discount < price:
                suggestion += f"{i}. {prod['name']} - ~~৳{price}~~ **৳{discount}**\n"
            else:
                suggestion += f"{i}. {prod['name']} - ৳{price}\n"
        
        suggestion += "\n(পণ্যের নাম লিখুন বিস্তারিত জানতে)"
        return suggestion
    except Exception as e:
        logger.error(f"Related products error: {e}")
        return ""

# ================= TOKEN MANAGEMENT =================
def truncate_messages_for_tokens(messages: List[Dict]) -> List[Dict]:
    """Truncate messages to stay within token limits"""
    truncated = []
    total_chars = 0
    max_chars = 2000
    
    for msg in reversed(messages):
        content = msg.get("content", "")
        role = msg.get("role", "")
        
        if role == "system":
            continue
        
        if len(content) > Config.MAX_MESSAGE_LENGTH:
            content = content[:Config.MAX_MESSAGE_LENGTH] + "..."
        
        truncated.insert(0, {"role": role, "content": content})
        total_chars += len(content)
        
        if total_chars > max_chars:
            break
    
    return truncated

def create_system_prompt_from_db(admin_id: str) -> str:
    """Create system prompt ONLY from database"""
    try:
        business = get_business_settings_cached(admin_id)
        delivery_info = get_delivery_info_from_db(admin_id)
        payment_methods = get_payment_methods_from_db(admin_id)
        promotions = get_sales_promotions_cached(admin_id)
        business_address = get_business_address_from_db(admin_id)
        
        prompt_parts = []
        
        business_name = business.get("name") if business else None
        if business_name:
            prompt_parts.append(f"তুমি {business_name}-এর সহায়ক।")
        else:
            prompt_parts.append("তুমি একটি দোকানের সহায়ক।")
        
        # Add business address if available
        if business_address:
            prompt_parts.append(f"দোকানের ঠিকানা: {business_address}")
        
        prompt_parts.append("""
**নিয়ম:**
১. শুধু বাংলায় কথা বলবে
২. ডাটাবেসের তথ্য ব্যবহার করবে
৩. সংক্ষিপ্ত উত্তর দিবে (২০০ শব্দের বেশি না)
৪. গ্রাহকের নাম জিজ্ঞাসা করবে
৫. সহজ ভাষা ব্যবহার করবে
        """)
        
        if promotions:
            promo_text = "\n**চলমান অফার:**"
            for promo in promotions[:1]:
                promo_text += f"\n• {promo.get('title')}"
            prompt_parts.append(promo_text)
        
        if delivery_info:
            delivery_text = "\n**ডেলিভারি তথ্য:**"
            if delivery_info.get('delivery_charge') is not None:
                delivery_text += f"\n• চার্জ: ৳{delivery_info['delivery_charge']}"
            if delivery_info.get('free_delivery_threshold') is not None:
                delivery_text += f"\n• ফ্রি: ৳{delivery_info['free_delivery_threshold']} এর উপর"
            if delivery_info.get('delivery_time'):
                delivery_text += f"\n• সময়: {delivery_info['delivery_time']}"
            prompt_parts.append(delivery_text)
        
        if payment_methods:
            prompt_parts.append(f"**পেমেন্ট:** {', '.join(payment_methods[:3])}")
        
        prompt_parts.append("""
**অর্ডার নেওয়ার নিয়ম:**
১. নাম → ফোন → ঠিকানা → পণ্য
২. সব তথ্য পাওয়ার পর সামারি দেখাবে
৩. "Confirm" শুনলে অর্ডার সেভ করবে
        """)
        
        final_prompt = "\n".join(prompt_parts)
        return final_prompt[:Config.MAX_SYSTEM_PROMPT_TOKENS]
    
    except Exception as e:
        logger.error(f"Error creating system prompt: {e}")
        return "তুমি একজন দোকান সহায়ক। শুধু বাংলায় উত্তর দেবে। সংক্ষিপ্ত উত্তর দিবে।"

# ================= AI ORDER PROCESSING =================
def process_order_with_ai(admin_id: str, customer_id: str, user_msg: str) -> str:
    """Process order with AI using database-only information"""
    if supabase is None:
        return "সিস্টেমে সমস্যা হচ্ছে। পরে চেষ্টা করুন।"
    
    try:
        key_res = supabase.table("api_keys") \
            .select("groq_api_key") \
            .eq("user_id", admin_id) \
            .execute()

        if not key_res.data or not key_res.data[0].get("groq_api_key"):
            return "দুঃখিত, AI সার্ভিস কনফিগার করা হয়নি।"

        api_key = key_res.data[0]["groq_api_key"]
        
        is_valid, validation_msg = validate_groq_api_key(api_key)
        if not is_valid:
            logger.error(f"Invalid API key: {validation_msg}")
            return "দুঃখিত, AI সার্ভিসে সমস্যা হয়েছে।"
        
        client = OpenAI(
            base_url=Config.GROQ_BASE_URL,
            api_key=api_key
        )

        products = get_products_with_details_cached(admin_id)
        business = get_business_settings_cached(admin_id)
        delivery_info = get_delivery_info_from_db(admin_id)
        payment_methods = get_payment_methods_from_db(admin_id)
        history = get_chat_memory(admin_id, customer_id)
        
        session = get_or_create_order_session(admin_id, customer_id)
        
        user_msg_lower = user_msg.lower()
        confirm_keywords = ['confirm', 'কনফার্ম', 'হ্যাঁ', 'ঠিক আছে', 'ঠিক', 'সাবমিট', 'অর্ডার দিচ্ছি']
        cancel_keywords = ['cancel', 'বাতিল', 'না', 'বাদ', 'থাক']
        
        if any(keyword in user_msg_lower for keyword in confirm_keywords):
            if session and session.get("status") == "ready_for_confirmation":
                collected_data = session.get("collected_data", {})
                cart_items = session.get("cart_items", [])
                
                if not collected_data or not cart_items:
                    return "দুঃখিত, অর্ডার তথ্য সম্পূর্ণ নেই।"
                
                items_list = []
                total_qty = 0
                total_amount = 0
                
                for item in cart_items:
                    item_str = f"{item.get('name')} x{item.get('quantity', 1)}"
                    items_list.append(item_str)
                    
                    total_qty += item.get('quantity', 1)
                    subtotal = item.get('subtotal', 0) or (item.get('price', 0) * item.get('quantity', 1))
                    total_amount += subtotal
                
                items_text = ", ".join(items_list)
                
                delivery_charge = calculate_delivery_charge(admin_id, total_amount)
                final_total = total_amount + delivery_charge
                
                # CHANGE 1: customer_address changed to address
                order_data = {
                    "user_id": admin_id,
                    "customer_name": collected_data.get("name", ""),
                    "customer_phone": collected_data.get("phone", ""),
                    "address": collected_data.get("address", ""),  # Changed from customer_address to address
                    "product": items_text,
                    "quantity": total_qty,
                    "subtotal": total_amount,
                    "delivery_charge": delivery_charge,
                    "total": final_total,
                    "status": "pending",
                    "items": cart_items,
                    "created_at": datetime.utcnow().isoformat()
                }
                
                is_valid, validation_msg = validate_order_data(order_data, admin_id)
                if not is_valid:
                    return validation_msg
                
                saved_order = save_order_to_db(order_data)
                if not saved_order:
                    return "অর্ডার সেভ করতে সমস্যা হয়েছে।"
                
                complete_order_session(admin_id, customer_id)
                
                business_name = business.get("name", "আমাদের দোকান") if business else "আমাদের দোকান"
                contact_number = business.get("contact_number", "") if business else ""
                
                greeting = f"""✅ **অর্ডার গ্রহণ করা হয়েছে!**

**অর্ডার আইডি:** #{saved_order.get('order_number', saved_order.get('id', 'N/A'))}

আমরা শীঘ্রই আপনার সাথে যোগাযোগ করব।

ধন্যবাদান্তে,
**{business_name}**
{contact_number}"""
                
                return greeting
            else:
                return "দুঃখিত, কনফার্ম করার জন্য অর্ডার প্রস্তুত নেই।"
        
        if any(keyword in user_msg_lower for keyword in cancel_keywords):
            if session:
                complete_order_session(admin_id, customer_id)
                return "অর্ডার বাতিল করা হয়েছে। 'হ্যালো' লিখুন।"
        
        system_prompt = create_system_prompt_from_db(admin_id)
        
        if products:
            product_info = "\n\n**পণ্যের তালিকা:**"
            for i, p in enumerate(products[:8], 1):
                price = p.get('price', 0)
                discount = p.get('discount_price')
                stock = p.get('stock', 0)
                
                if discount and discount < price:
                    price_text = f"~~৳{price}~~ **৳{discount}**"
                else:
                    price_text = f"৳{price}"
                
                stock_text = f"({stock} পিছ)" if stock > 0 else "(স্টক নেই)"
                
                product_info += f"\n{i}. {p['name']} - {price_text} {stock_text}"
            
            if len(products) > 8:
                product_info += f"\n... এবং আরও {len(products) - 8} টি পণ্য"
            
            system_prompt += product_info
        
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "collect_customer_info",
                    "description": "গ্রাহকের তথ্য সংগ্রহ করুন",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "গ্রাহকের পূর্ণ নাম"},
                            "phone": {"type": "string", "description": "১১ ডিজিটের মোবাইল নম্বর (01xxxxxxxxx)"},
                            "address": {"type": "string", "description": "পূর্ণাঙ্গ ডেলিভারি ঠিকানা"}
                        },
                        "required": ["name", "phone", "address"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "add_to_cart",
                    "description": "পণ্য কার্টে যোগ করুন",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "product_name": {"type": "string", "description": "পণ্যের নাম"},
                            "quantity": {"type": "integer", "description": "পরিমাণ", "minimum": 1}
                        },
                        "required": ["product_name", "quantity"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "show_order_summary",
                    "description": "অর্ডার সামারি দেখান",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "গ্রাহকের নাম"},
                            "phone": {"type": "string", "description": "মোবাইল নম্বর"},
                            "address": {"type": "string", "description": "ঠিকানা"},
                            "items": {"type": "string", "description": "পণ্যের তালিকা"},
                            "subtotal": {"type": "integer", "description": "পণ্যের মোট মূল্য"},
                            "delivery_charge": {"type": "integer", "description": "ডেলিভারি চার্জ"},
                            "total_amount": {"type": "integer", "description": "সর্বমোট টাকা"}
                        },
                        "required": ["name", "phone", "address", "items", "subtotal", "delivery_charge", "total_amount"]
                    }
                }
            }
        ]

        messages = [
            {"role": "system", "content": system_prompt}
        ]
        
        if session:
            session_status = session.get("status", "collecting_info")
            collected_data = session.get("collected_data", {})
            
            context_msg = f"**সেশন:** {session_status}\n"
            if collected_data.get("name"):
                context_msg += f"নাম: {collected_data['name']}\n"
            if collected_data.get("phone"):
                context_msg += f"ফোন: {collected_data['phone']}\n"
            if collected_data.get("address"):
                context_msg += f"ঠিকানা: {collected_data['address']}\n"
            
            cart_items = session.get("cart_items", [])
            if cart_items:
                context_msg += "\n**কার্ট:**\n"
                for item in cart_items:
                    context_msg += f"- {item.get('name')} x{item.get('quantity', 1)} = ৳{item.get('subtotal', 0)}\n"
            
            messages.append({"role": "assistant", "content": context_msg})
        
        if history:
            truncated_history = truncate_messages_for_tokens(history)
            messages.extend(truncated_history)
        
        messages.append({"role": "user", "content": user_msg[:Config.MAX_MESSAGE_LENGTH]})

        response = call_groq_with_retry(
            client=client,
            messages=messages,
            tools=tools,
            max_retries=2
        )

        if not response or not response.choices:
            return "সার্ভারে সময়সীমা শেষ। আবার চেষ্টা করুন।"
        
        msg = response.choices[0].message
        reply = msg.content if msg.content else "আপনার বার্তা পাওয়া গেছে। কিভাবে সাহায্য করতে পারি?"

        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            for tool_call in msg.tool_calls:
                function_name = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments)
                except:
                    args = {}
                
                if function_name == "collect_customer_info" and session:
                    updates = {
                        "collected_data": {
                            "name": args.get("name", ""),
                            "phone": args.get("phone", ""),
                            "address": args.get("address", "")
                        },
                        "status": "info_collected",
                        "current_step": "select_products"
                    }
                    update_order_session(session["id"], updates)
                    
                    reply = "ধন্যবাদ! আপনার তথ্য সংরক্ষিত হয়েছে। এখন কোন পণ্য কিনতে চান?"
                
                elif function_name == "add_to_cart" and session:
                    product_name = args.get("product_name", "")
                    quantity = args.get("quantity", 1)
                    
                    product = get_product_by_name(admin_id, product_name)
                    if product:
                        stock = product.get('stock', 0)
                        if stock <= 0:
                            reply = f"দুঃখিত, {product['name']} এর স্টক নেই।"
                        elif quantity > stock:
                            reply = f"দুঃখিত, {product['name']} এর মাত্র {stock} পিছ স্টকে আছে।"
                        else:
                            price = product.get('discount_price') or product.get('price', 0)
                            cart_item = {
                                "product_id": product["id"],
                                "name": product["name"],
                                "quantity": quantity,
                                "price": price,
                                "subtotal": price * quantity
                            }
                            
                            cart_items = session.get("cart_items", [])
                            cart_items.append(cart_item)
                            
                            updates = {
                                "cart_items": cart_items,
                                "total_amount": session.get("total_amount", 0) + cart_item["subtotal"],
                                "status": "items_added"
                            }
                            update_order_session(session["id"], updates)
                            
                            reply = f"✅ {product['name']} x{quantity} কার্টে যোগ করা হয়েছে!\nমূল্য: ৳{cart_item['subtotal']}"
                    
                    else:
                        reply = f"দুঃখিত, '{product_name}' নামে পণ্য পাইনি। 'পণ্য লিস্ট' লিখুন।"
                
                elif function_name == "show_order_summary" and session:
                    delivery_charge = calculate_delivery_charge(admin_id, args.get("subtotal", 0))
                    updates = {
                        "status": "ready_for_confirmation",
                        "delivery_charge": delivery_charge
                    }
                    update_order_session(session["id"], updates)
                    
                    summary = f"""📋 **অর্ডার সামারি:**

**গ্রাহক:**
• নাম: {args.get('name')}
• ফোন: {args.get('phone')}
• ঠিকানা: {args.get('address')}

**পণ্য:**
{args.get('items')}

**মূল্য:** ৳{args.get('subtotal')}
**ডেলিভারি:** ৳{args.get('delivery_charge')}
**সর্বমোট:** ৳{args.get('total_amount')}

✅ **অর্ডার সম্পূর্ণ করতে 'Confirm' লিখুন**
❌ **বাতিল করতে 'Cancel' লিখুন**"""
                    
                    reply = summary
        
        reply = apply_sales_psychology(reply, admin_id)
        
        history.append({"role": "user", "content": user_msg[:200]})
        history.append({"role": "assistant", "content": reply[:400]})
        save_chat_memory(admin_id, customer_id, history)
        
        return reply

    except requests.exceptions.Timeout:
        logger.error("Groq API timeout")
        return "সার্ভারে সময়সীমা শেষ। আবার চেষ্টা করুন।"
    except Exception as e:
        logger.error(f"AI processing error: {e}")
        return "দুঃখিত, সাময়িক সমস্যা হচ্ছে। পরে চেষ্টা করুন।"

# ================= QUICK RESPONSES =================
def get_quick_response(admin_id: str, user_msg: str) -> Optional[str]:
    """Quick responses for common queries without AI"""
    user_msg_lower = user_msg.lower()
    
    if any(word in user_msg_lower for word in ['হাই', 'হেলো', 'হ্যালো', 'শুরু', 'hello', 'hi', 'hey']):
        business = get_business_settings_cached(admin_id)
        business_name = business.get("name", "আমাদের দোকান") if business else "আমাদের দোকান"
        
        greeting = f"""👋 **{business_name} এ স্বাগতম!**

আমি আপনার শপিং সহায়ক। কিভাবে সাহায্য করতে পারি?

✨ **দ্রুত অপশনস:**
• পণ্য লিস্ট - সব পণ্য দেখুন
• অর্ডার - নতুন অর্ডার দিন
• ডেলিভারি - ডেলিভারি তথ্য
• সময় - খোলার সময়

👉 **সহযোগিতা চাইলে লিখুন!**"""
        return greeting
    
    if any(word in user_msg_lower for word in ['পণ্য', 'প্রোডাক্ট', 'লিস্ট', 'দ্রব্য', 'কি কি', 'সব পণ্য']):
        products = get_products_with_details_cached(admin_id)
        if not products:
            return "দুঃখিত, ডাটাবেসে পণ্য নেই।"
        
        response = "🛍️ **পণ্যের তালিকা:**\n\n"
        for i, product in enumerate(products[:8], 1):
            price = product.get('price', 0)
            discount = product.get('discount_price')
            stock = product.get('stock', 0)
            
            if discount and discount < price:
                price_text = f"~~৳{price}~~ **৳{discount}**"
            else:
                price_text = f"৳{price}"
            
            stock_text = f"({stock} পিছ)" if stock > 0 else "স্টক নেই"
            featured = "⭐ " if product.get('is_featured') else ""
            
            response += f"{i}. {featured}{product['name']} - {price_text} {stock_text}\n"
        
        if len(products) > 8:
            response += f"\n... এবং আরও {len(products) - 8} টি পণ্য"
        
        response += "\n\n📝 **অর্ডার করতে:** পণ্যের নাম লিখুন"
        return response
    
    price_match = re.search(r'(?:দাম|মূল্য|কত|কি দাম|কেমন)\s*(?:কি|কত|হচ্ছে|করে)\s*(.*)', user_msg, re.IGNORECASE)
    if not price_match:
        price_match = re.search(r'(.*?)\s+(?:এর|রে)\s+(?:দাম|মূল্য|কত)', user_msg, re.IGNORECASE)
    
    if price_match:
        product_name = price_match.group(1).strip()
        if product_name and len(product_name) > 1:
            product = get_product_by_name(admin_id, product_name)
            if product:
                price = product.get('price', 0)
                discount = product.get('discount_price')
                stock = product.get('stock', 0)
                
                if discount and discount < price:
                    price_text = f"~~৳{price}~~ **৳{discount}**"
                else:
                    price_text = f"৳{price}"
                
                stock_text = f"{stock} পিছ স্টকে আছে" if stock > 0 else "স্টক শেষ"
                
                response = f"📊 **{product['name']}**\n"
                response += f"দাম: {price_text}\n"
                response += f"স্টক: {stock_text}\n"
                
                response += f"\n🛒 **কার্টে যোগ করতে:** '{product['name']} ১' লিখুন"
                
                return response
            else:
                return f"দুঃখিত, '{product_name}' পণ্য পাইনি।"
    
    if any(word in user_msg_lower for word in ['ডেলিভারি', 'চার্জ', 'ফি', 'কত', 'এলাকা', 'সময়']):
        delivery_info = get_delivery_info_from_db(admin_id)
        if not delivery_info:
            return "দুঃখিত, ডেলিভারি তথ্য নেই।"
        
        response = "🚚 **ডেলিভারি তথ্য:**\n"
        if delivery_info.get('delivery_charge') is not None:
            response += f"• চার্জ: ৳{delivery_info['delivery_charge']}\n"
        if delivery_info.get('free_delivery_threshold') is not None:
            response += f"• ফ্রি ডেলিভারি: ৳{delivery_info['free_delivery_threshold']} এর উপর\n"
        if delivery_info.get('delivery_time'):
            response += f"• সময়: {delivery_info['delivery_time']}\n"
        
        areas = delivery_info.get('delivery_areas', [])
        if areas:
            response += f"• এলাকা: {', '.join(areas[:3])}"
        
        return response
    
    if any(word in user_msg_lower for word in ['খোলার সময়', 'সময়', 'খোলা', 'বন্ধ', 'কখন']):
        opening_hours = get_opening_hours_from_db(admin_id)
        if not opening_hours:
            return "দুঃখিত, খোলার সময় নেই।"
        
        response = "🕒 **খোলার সময়:**\n"
        days = ['শনিবার', 'রবিবার', 'সোমবার', 'মঙ্গলবার', 'বুধবার', 'বৃহস্পতিবার', 'শুক্রবার']
        
        for day in days[:7]:
            if day in opening_hours:
                response += f"• {day}: {opening_hours[day]}\n"
        
        return response
    
    if any(word in user_msg_lower for word in ['পেমেন্ট', 'পরিশোধ', 'টাকা দেব', 'পেমেন্ট সিস্টেম']):
        payment_methods = get_payment_methods_from_db(admin_id)
        
        response = "💳 **পেমেন্ট পদ্ধতি:**\n"
        for i, method in enumerate(payment_methods[:4], 1):
            response += f"{i}. {method}\n"
        
        return response
    
    if any(word in user_msg_lower for word in ['অর্ডার', 'ওর্ডার', 'কিনব', 'কিনতে']):
        return "🛒 **অর্ডার প্রসেস:**\n\n১. 'অর্ডার' লিখুন\n২. নাম দিন\n৩. ফোন নম্বর দিন\n৪. ঠিকানা দিন\n৫. পণ্যের নাম লিখুন\n\n👉 **শুরু করতে 'অর্ডার' লিখুন**"
    
    return None

# ================= WEBHOOK ENDPOINT =================
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    """Facebook webhook endpoint"""
    if request.method == "GET":
        verify_token = os.getenv("FACEBOOK_VERIFY_TOKEN", "")
        if request.args.get("hub.mode") == "subscribe":
            if request.args.get("hub.verify_token") == verify_token:
                logger.info("Webhook verified successfully")
                return request.args.get("hub.challenge"), 200
            logger.warning("Verification token mismatch")
            return "Verification token mismatch", 403
        return "OK", 200

    try:
        data = request.get_json()
        
        if not data or "entry" not in data:
            logger.warning("Invalid webhook data received")
            return jsonify({"status": "error", "message": "Invalid data"}), 400
        
        logger.info(f"Webhook received: {len(data.get('entry', []))} entries")
        
        for entry in data.get("entry", []):
            page_id = entry.get("id")
            if not page_id:
                continue
            
            page = get_page_client_cached(page_id)
            
            if not page:
                logger.warning(f"No connected page found for ID: {page_id}")
                continue
            
            admin_id = page["user_id"]
            page_token = page["page_access_token"]
            page_name = page.get("page_name", "Unknown")
            
            logger.info(f"Processing messages for page: {page_name} (Admin: {admin_id})")
            
            for messaging in entry.get("messaging", []):
                sender_id = messaging.get("sender", {}).get("id")
                message = messaging.get("message", {})
                text = message.get("text", "").strip()
                
                if not text or not sender_id:
                    continue
                
                logger.info(f"Message from {sender_id[:10]}...: {text[:100]}...")
                
                if message.get("quick_reply"):
                    payload = message["quick_reply"].get("payload", "")
                    if payload == "VIEW_PRODUCTS":
                        text = "পণ্য লিস্ট"
                    elif payload == "START_ORDER":
                        text = "অর্ডার"
                    elif payload == "DELIVERY_INFO":
                        text = "ডেলিভারি"
                
                cancel_keywords = ['cancel', 'বাতিল', 'না', 'বাদ দাও']
                if any(keyword in text.lower() for keyword in cancel_keywords):
                    complete_order_session(admin_id, sender_id)
                    send_message_with_buttons(page_token, sender_id, "অর্ডার বাতিল করা হয়েছে। নতুন অর্ডার দিতে 'অর্ডার' লিখুন।", [
                        {"title": "অর্ডার করুন", "payload": "START_ORDER"},
                        {"title": "পণ্য দেখুন", "payload": "VIEW_PRODUCTS"}
                    ])
                    continue
                
                faq_answer = find_faq_cached(admin_id, text)
                if faq_answer:
                    send_message(page_token, sender_id, faq_answer)
                    continue
                
                quick_response = get_quick_response(admin_id, text)
                if quick_response:
                    buttons = []
                    if "পণ্য" in text.lower() or "লিস্ট" in text.lower():
                        buttons = [
                            {"title": "অর্ডার করুন", "payload": "START_ORDER"},
                            {"title": "ডেলিভারি", "payload": "DELIVERY_INFO"}
                        ]
                    elif "হাই" in text.lower() or "হেলো" in text.lower():
                        buttons = [
                            {"title": "পণ্য দেখুন", "payload": "VIEW_PRODUCTS"},
                            {"title": "অর্ডার করুন", "payload": "START_ORDER"}
                        ]
                    
                    send_message_with_buttons(page_token, sender_id, quick_response, buttons)
                    continue
                
                ai_response = process_order_with_ai(admin_id, sender_id, text)
                
                buttons = []
                if "কনফার্ম" in ai_response.lower() or "সামারি" in ai_response.lower():
                    buttons = [
                        {"title": "Confirm ✅", "payload": "CONFIRM_ORDER"},
                        {"title": "Cancel ❌", "payload": "CANCEL_ORDER"}
                    ]
                elif "পণ্য" in ai_response.lower():
                    buttons = [
                        {"title": "পণ্য লিস্ট", "payload": "VIEW_PRODUCTS"},
                        {"title": "ডেলিভারি", "payload": "DELIVERY_INFO"}
                    ]
                
                send_message_with_buttons(page_token, sender_id, ai_response, buttons)
        
        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

# ================= HEALTH CHECK =================
@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    try:
        if supabase is None:
            return jsonify({
                "status": "unhealthy",
                "error": "Database connection failed",
                "timestamp": datetime.utcnow().isoformat()
            }), 500
        
        db_test = supabase.table("products").select("id").limit(1).execute()
        
        fb_status = "unknown"
        try:
            fb_test = requests.get("https://graph.facebook.com/v18.0/me", timeout=5)
            fb_status = "reachable" if fb_test.status_code != 403 else "token_needed"
        except:
            fb_status = "unreachable"
        
        check_abandoned_carts()
        
        return jsonify({
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "service": "facebook-chatbot",
            "database": "connected",
            "facebook_api": fb_status,
            "config": {
                "model": Config.GROQ_MODEL,
                "max_tokens": Config.MAX_TOKENS,
                "temperature": Config.TEMPERATURE
            }
        }), 200
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }), 500

# ================= MAIN =================
if __name__ == "__main__":
    required_vars = ["SUPABASE_URL", "SUPABASE_SERVICE_KEY", "FACEBOOK_VERIFY_TOKEN"]
    missing_vars = []
    
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        logger.error(f"❌ Missing environment variables: {missing_vars}")
        exit(1)
    
    port = int(os.getenv("PORT", 10000))
    debug = os.getenv("DEBUG", "false").lower() == "true"
    
    logger.info("=" * 50)
    logger.info("🚀 FACEBOOK CHATBOT STARTING")
    logger.info("=" * 50)
    logger.info(f"📦 Port: {port}")
    logger.info(f"🤖 AI Model: {Config.GROQ_MODEL} (Free tier optimized)")
    logger.info(f"💬 Max Tokens: {Config.MAX_TOKENS}")
    logger.info("=" * 50)
    logger.info("✅ All systems ready!")
    logger.info("=" * 50)
    
    app.run(host="0.0.0.0", port=port, debug=debug)

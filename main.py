import os
import re
import json
import logging
import time
from typing import Optional, Dict, List, Tuple, Any
from datetime import datetime
from functools import lru_cache
from flask import Flask, request, jsonify
from openai import OpenAI
from supabase import create_client, Client
from difflib import SequenceMatcher
import requests

# ================= CONFIG =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
app = Flask(__name__)

# Configuration - NO HARDCODED BUSINESS DATA
class Config:
    # Groq API Configuration ONLY
    GROQ_BASE_URL = "https://api.groq.com/openai/v1"
    GROQ_MODEL = "llama-3.3-70b-versatile"
    MAX_TOKENS = 800
    TEMPERATURE = 0.1
    
    # Chat memory limits
    MAX_CHAT_HISTORY = 10
    MAX_MESSAGE_LENGTH = 500
    MAX_SYSTEM_PROMPT_TOKENS = 1000

# ================= SUPABASE =================
try:
    supabase: Client = create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_SERVICE_KEY")
    )
    logger.info("Supabase client initialized successfully")
except Exception as e:
    logger.error(f"Supabase Client Error: {e}")
    supabase = None

# ================= API VALIDATION =================
def validate_groq_api_key(api_key: str) -> bool:
    """Validate Groq API key format"""
    if not api_key or not isinstance(api_key, str):
        return False
    
    if not api_key.startswith('gsk_'):
        logger.warning(f"Invalid API key format: {api_key[:10]}...")
        return False
    
    return True

def call_groq_with_retry(client, messages, tools=None, max_retries=3):
    """Call Groq API with retry mechanism"""
    for attempt in range(max_retries):
        try:
            params = {
                "model": Config.GROQ_MODEL,
                "messages": messages,
                "temperature": Config.TEMPERATURE,
                "max_tokens": Config.MAX_TOKENS,
                "timeout": 30
            }
            
            if tools and attempt == 0:
                params["tools"] = tools
                params["tool_choice"] = "auto"
            
            response = client.chat.completions.create(**params)
            return response
        except requests.exceptions.Timeout:
            logger.warning(f"Groq API timeout (attempt {attempt + 1}/{max_retries})")
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** attempt)
        except Exception as e:
            logger.error(f"Groq API error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                raise
            time.sleep(1)
    
    return None

# ================= HELPERS =================
@lru_cache(maxsize=128, ttl=300)
def get_page_client_cached(page_id: str) -> Optional[Dict]:
    """Fetch Facebook page integration details with caching"""
    try:
        res = supabase.table("facebook_integrations") \
            .select("*") \
            .eq("page_id", str(page_id)) \
            .eq("is_connected", True) \
            .single().execute()
        return res.data if res.data else None
    except Exception as e:
        logger.error(f"Error fetching page client: {e}")
        return None

def send_message(token: str, user_id: str, text: str) -> bool:
    """Send message via Facebook Messenger API with retry"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            url = f"https://graph.facebook.com/v18.0/me/messages?access_token={token}"
            payload = {
                "recipient": {"id": user_id},
                "message": {"text": text[:1000]}
            }
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
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

@lru_cache(maxsize=128, ttl=300)
def get_products_with_details_cached(admin_id: str) -> List[Dict]:
    """Fetch all products for a specific business with caching"""
    try:
        res = supabase.table("products") \
            .select("id, name, price, stock, category, description, in_stock, image_url") \
            .eq("user_id", admin_id) \
            .eq("in_stock", True) \
            .order("category") \
            .execute()
        return res.data or []
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
        
        # First try exact match
        for product in products:
            if product["name"].strip().lower() == query:
                return product
        
        # Then try partial match
        for product in products:
            if query in product["name"].lower():
                return product
        
        # Finally try fuzzy matching
        best_match = None
        best_ratio = 0
        
        for product in products:
            ratio = SequenceMatcher(None, query, product["name"].lower()).ratio()
            if ratio > best_ratio and ratio > 0.6:
                best_ratio = ratio
                best_match = product
        
        return best_match
    except Exception as e:
        logger.error(f"Error finding product: {e}")
        return None

@lru_cache(maxsize=128, ttl=300)
def find_faq_cached(admin_id: str, user_msg: str) -> Optional[str]:
    """Find FAQ answer using similarity matching with caching"""
    try:
        res = supabase.table("faqs") \
            .select("question, answer") \
            .eq("user_id", admin_id) \
            .execute()

        best_ratio = 0.65
        best_answer = None
        
        for faq in res.data or []:
            ratio = SequenceMatcher(None, user_msg.lower(), faq["question"].lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_answer = faq["answer"]
        
        return best_answer
    except Exception as e:
        logger.error(f"Error finding FAQ: {e}")
        return None

@lru_cache(maxsize=128, ttl=300)
def get_business_settings_cached(admin_id: str) -> Optional[Dict]:
    """Fetch business settings from database with caching"""
    try:
        res = supabase.table("business_settings") \
            .select("*") \
            .eq("user_id", admin_id) \
            .limit(1) \
            .execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"Error fetching business settings: {e}")
        return None

def parse_delivery_info(delivery_info_text: str) -> Optional[Dict]:
    """Parse delivery info from text field - NO DEFAULT VALUES"""
    if not delivery_info_text:
        return None
    
    try:
        result = {}
        
        # Try to parse as JSON
        if delivery_info_text.startswith('{'):
            parsed = json.loads(delivery_info_text)
            result.update(parsed)
            return result
        
        # Parse text format
        lines = delivery_info_text.split('\n')
        for line in lines:
            if ':' in line:
                key, value = line.split(':', 1)
                key_lower = key.strip().lower()
                value = value.strip()
                
                if any(word in key_lower for word in ['charge', 'ফি', 'মূল্য', 'ডেলিভারি']):
                    numbers = re.findall(r'\d+', value)
                    if numbers:
                        result['delivery_charge'] = int(numbers[0])
                
                elif any(word in key_lower for word in ['free', 'ফ্রি', 'থ্রেশহোল্ড']):
                    numbers = re.findall(r'\d+', value)
                    if numbers:
                        result['free_delivery_threshold'] = int(numbers[0])
                
                elif any(word in key_lower for word in ['area', 'এলাকা', 'সার্ভিস']):
                    areas = [area.strip() for area in value.split(',') if area.strip()]
                    if areas:
                        result['delivery_areas'] = areas
                
                elif any(word in key_lower for word in ['time', 'সময়', 'সাময়', 'ঘন্টা']):
                    result['delivery_time'] = value
                
                elif any(word in key_lower for word in ['payment', 'পেমেন্ট', 'পরিশোধ']):
                    methods = [method.strip() for method in value.split(',') if method.strip()]
                    if methods:
                        result['payment_methods'] = methods
        
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
                day, time = line.split(':', 1)
                hours[day.strip()] = time.strip()
        
        return hours if hours else None
    except Exception as e:
        logger.error(f"Error parsing opening hours: {e}")
        return None

def get_delivery_info_from_db(admin_id: str) -> Optional[Dict]:
    """Get delivery information from business_settings table - NO DEFAULT"""
    try:
        business = get_business_settings_cached(admin_id)
        if not business or not business.get("delivery_info"):
            return None
        
        return parse_delivery_info(business["delivery_info"])
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

def get_payment_methods_from_db(admin_id: str) -> Optional[List[str]]:
    """Get payment methods from business_settings table - NO DEFAULT"""
    try:
        business = get_business_settings_cached(admin_id)
        
        if business and business.get("payment_methods"):
            return business["payment_methods"]
        
        if business and business.get("delivery_info"):
            delivery_info = parse_delivery_info(business["delivery_info"])
            if delivery_info and delivery_info.get("payment_methods"):
                return delivery_info["payment_methods"]
        
        return None
    except Exception as e:
        logger.error(f"Error getting payment methods: {e}")
        return None

# ================= ORDER SESSION MANAGEMENT =================
def get_or_create_order_session(admin_id: str, customer_id: str) -> Optional[Dict]:
    """Get existing order session or create new"""
    try:
        # Check for active session (not completed, not expired)
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
        
        if res.data:
            return res.data[0]
        
        # Create new session
        new_session = {
            "user_id": admin_id,
            "customer_id": customer_id,
            "status": "collecting_info",
            "current_step": "ask_name",
            "collected_data": {},
            "cart_items": [],
            "total_amount": 0,
            "delivery_charge": 0,
            "expires_at": (datetime.utcnow() + timedelta(hours=2)).isoformat(),
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }
        
        res = supabase.table("order_sessions").insert(new_session).execute()
        return res.data[0] if res.data else None
        
    except Exception as e:
        logger.error(f"Order session error: {e}")
        return None

def update_order_session(session_id: str, updates: Dict) -> bool:
    """Update order session"""
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
        
        return bool(res.data)
    except Exception as e:
        logger.error(f"Complete session error: {e}")
        return False

def save_order_to_db(order_data: Dict) -> Optional[Dict]:
    """Save order to database"""
    try:
        res = supabase.table("orders").insert(order_data).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"Save order error: {e}")
        return None

# ================= CHAT MEMORY MANAGEMENT =================
def get_chat_memory(admin_id: str, customer_id: str) -> List[Dict]:
    """Get recent chat history for context"""
    try:
        res = supabase.table("chat_history") \
            .select("messages") \
            .eq("user_id", admin_id) \
            .eq("customer_id", customer_id) \
            .limit(1) \
            .execute()
        
        if res.data and res.data[0].get("messages"):
            messages = res.data[0]["messages"]
            return messages[-Config.MAX_CHAT_HISTORY:]
        return []
    except Exception as e:
        logger.error(f"Error fetching chat memory: {e}")
        return []

def save_chat_memory(admin_id: str, customer_id: str, messages: List[Dict]):
    """Save chat history"""
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
        
        supabase.table("chat_history").upsert(
            data,
            on_conflict="user_id,customer_id"
        ).execute()
        
    except Exception as e:
        logger.error(f"Memory Error: {e}")

# ================= ORDER VALIDATION =================
def validate_order_data(order_data: Dict, admin_id: str) -> Tuple[bool, str]:
    """Validate order data before saving"""
    try:
        # Validate phone number (Bangladeshi)
        phone = order_data.get("phone", "").strip()
        if not re.match(r'^01[3-9]\d{8}$', phone):
            return False, "দুঃখিত, সঠিক ১১ ডিজিটের মোবাইল নম্বর দিতে হবে (০১৩-০১৯ পরিসর)"
        
        # Validate address
        address = order_data.get("address", "").strip()
        if len(address) < 10:
            return False, "দুঃখিত, পূর্ণাঙ্গ ঠিকানা দিন (অন্তত ১০ অক্ষর)"
        
        # Validate name
        name = order_data.get("name", "").strip()
        if len(name) < 2:
            return False, "দুঃখিত, পূর্ণ নাম দিন"
        
        # Validate items
        items = order_data.get("items", "")
        if not items or len(items.strip()) < 3:
            return False, "দুঃখিত, অর্ডারে পণ্যের নাম প্রয়োজন"
        
        # Check delivery area if specified in database
        delivery_info = get_delivery_info_from_db(admin_id)
        if delivery_info and delivery_info.get("delivery_areas"):
            delivery_areas = delivery_info.get("delivery_areas", [])
            area_found = False
            
            for area in delivery_areas:
                if area.lower() in address.lower():
                    area_found = True
                    break
            
            if not area_found and delivery_areas:
                areas_str = ", ".join(delivery_areas[:3])
                if len(delivery_areas) > 3:
                    areas_str += f" এবং {len(delivery_areas) - 3} টি এলাকা"
                return False, f"দুঃখিত, আমরা শুধুমাত্র {areas_str}-এ ডেলিভারি দেই।"
        
        return True, "ভালিডেশন সফল"
        
    except Exception as e:
        logger.error(f"Validation error: {e}")
        return False, "ভেরিফিকেশনে সমস্যা হয়েছে"

def calculate_delivery_charge(admin_id: str, total_amount: int) -> int:
    """Calculate delivery charge based on business settings"""
    try:
        delivery_info = get_delivery_info_from_db(admin_id)
        if not delivery_info:
            return 0  # No delivery info in database
        
        delivery_charge = delivery_info.get("delivery_charge")
        free_threshold = delivery_info.get("free_delivery_threshold")
        
        if delivery_charge is None:
            return 0
        
        if free_threshold and total_amount >= free_threshold:
            return 0
        
        return delivery_charge
    except Exception as e:
        logger.error(f"Error calculating delivery charge: {e}")
        return 0

# ================= TOKEN MANAGEMENT =================
def truncate_messages_for_tokens(messages: List[Dict]) -> List[Dict]:
    """Truncate messages to stay within token limits"""
    truncated = []
    total_chars = 0
    
    for msg in messages:
        content = msg.get("content", "")
        role = msg.get("role", "")
        
        if role == "system":
            truncated.append(msg)
            total_chars += len(content)
            continue
        
        if len(content) > Config.MAX_MESSAGE_LENGTH:
            content = content[:Config.MAX_MESSAGE_LENGTH] + "..."
        
        truncated.append({"role": role, "content": content})
        total_chars += len(content)
        
        if total_chars > 2000:
            break
    
    return truncated

def create_system_prompt_from_db(admin_id: str) -> str:
    """Create system prompt ONLY from database - NO HARDCODED DATA"""
    try:
        business = get_business_settings_cached(admin_id)
        delivery_info = get_delivery_info_from_db(admin_id)
        payment_methods = get_payment_methods_from_db(admin_id)
        
        prompt_parts = []
        
        # Business name
        business_name = business.get("name") if business else None
        if business_name:
            prompt_parts.append(f"তুমি {business_name}-এর সহায়ক।")
        else:
            prompt_parts.append("তুমি একটি দোকানের সহায়ক।")
        
        # Basic rules
        prompt_parts.append("""
**নিয়ম:**
১. শুধু বাংলায় কথা বলবে
২. শুধু ডাটাবেসের তথ্য ব্যবহার করবে
৩. ডাটাবেসে নেই এমন কিছু বলবে না ("জানি না" বলবে)
৪. অনুমান করবে না
        """)
        
        # Delivery info from database ONLY
        if delivery_info:
            delivery_text = "\n**ডেলিভারি তথ্য:**"
            if delivery_info.get('delivery_charge') is not None:
                delivery_text += f"\n• ডেলিভারি চার্জ: ৳{delivery_info['delivery_charge']}"
            if delivery_info.get('free_delivery_threshold') is not None:
                delivery_text += f"\n• ফ্রি ডেলিভারি: ৳{delivery_info['free_delivery_threshold']} এর উপর"
            if delivery_info.get('delivery_time'):
                delivery_text += f"\n• ডেলিভারি সময়: {delivery_info['delivery_time']}"
            if delivery_info.get('delivery_areas'):
                areas = delivery_info['delivery_areas'][:3]
                areas_text = ", ".join(areas)
                if len(delivery_info['delivery_areas']) > 3:
                    areas_text += f" এবং {len(delivery_info['delivery_areas']) - 3} টি এলাকা"
                delivery_text += f"\n• ডেলিভারি এলাকা: {areas_text}"
            prompt_parts.append(delivery_text)
        
        # Payment methods from database ONLY
        if payment_methods:
            prompt_parts.append(f"**পেমেন্ট পদ্ধতি:** {', '.join(payment_methods[:3])}")
        
        # Order collection instructions
        prompt_parts.append("""
**অর্ডার নেওয়ার নিয়ম:**
১. প্রথমে গ্রাহকের নাম জিজ্ঞাসা করবে
২. তারপর মোবাইল নম্বর (০১xxxxxxxxx)
৩. তারপর পূর্ণাঙ্গ ঠিকানা
৪. তারপর পণ্যের নাম ও পরিমাণ
৫. সব তথ্য পাওয়ার পর অর্ডার সামারি দেখাবে
৬. গ্রাহক "Confirm" লিখলে তবেই অর্ডার সেভ করবে
        """)
        
        final_prompt = "\n".join(prompt_parts)
        return final_prompt[:Config.MAX_SYSTEM_PROMPT_TOKENS]
    
    except Exception as e:
        logger.error(f"Error creating system prompt: {e}")
        return "তুমি একজন দোকান সহায়ক। শুধু বাংলায় উত্তর দেবে। ডাটাবেসে নেই এমন কিছু বলবে না।"

# ================= AI ORDER PROCESSING =================
def process_order_with_ai(admin_id: str, customer_id: str, user_msg: str) -> str:
    """Process order with AI using database-only information"""
    try:
        # Get Groq API key
        key_res = supabase.table("api_keys") \
            .select("groq_api_key") \
            .eq("user_id", admin_id) \
            .execute()

        if not key_res.data or not key_res.data[0].get("groq_api_key"):
            return "দুঃখিত, AI সার্ভিস কনফিগার করা হয়নি। ব্যবসা মালিককে জানান।"

        api_key = key_res.data[0]["groq_api_key"]
        
        if not validate_groq_api_key(api_key):
            return "দুঃখিত, AI সার্ভিসে সমস্যা হয়েছে। ব্যবসা মালিককে জানান।"
        
        # Initialize Groq client
        client = OpenAI(
            base_url=Config.GROQ_BASE_URL,
            api_key=api_key
        )

        # Get data from database ONLY
        products = get_products_with_details_cached(admin_id)
        business = get_business_settings_cached(admin_id)
        delivery_info = get_delivery_info_from_db(admin_id)
        payment_methods = get_payment_methods_from_db(admin_id)
        history = get_chat_memory(admin_id, customer_id)
        
        # Get or create order session
        session = get_or_create_order_session(admin_id, customer_id)
        
        # Check if user is confirming order
        user_msg_lower = user_msg.lower()
        if any(confirm_word in user_msg_lower for confirm_word in ['confirm', 'কনফার্ম', 'হ্যাঁ', 'ঠিক আছে', 'ঠিক', 'সাবমিট']):
            if session and session.get("status") == "ready_for_confirmation":
                # Save order to database
                collected_data = session.get("collected_data", {})
                cart_items = session.get("cart_items", [])
                
                if not collected_data or not cart_items:
                    return "দুঃখিত, অর্ডার তথ্য সম্পূর্ণ নেই। আবার চেষ্টা করুন।"
                
                # Format items string
                items_list = []
                total_qty = 0
                total_amount = 0
                
                for item in cart_items:
                    items_list.append(f"{item.get('name')} x{item.get('quantity', 1)}")
                    total_qty += item.get('quantity', 1)
                    total_amount += item.get('subtotal', 0)
                
                items_text = ", ".join(items_list)
                
                # Calculate delivery charge
                delivery_charge = calculate_delivery_charge(admin_id, total_amount)
                final_total = total_amount + delivery_charge
                
                # Prepare order data
                order_data = {
                    "user_id": admin_id,
                    "customer_name": collected_data.get("name", ""),
                    "customer_phone": collected_data.get("phone", ""),
                    "customer_address": collected_data.get("address", ""),
                    "product": items_text,
                    "quantity": total_qty,
                    "delivery_charge": delivery_charge,
                    "total": final_total,
                    "status": "pending",
                    "created_at": datetime.utcnow().isoformat()
                }
                
                # Save to orders table
                saved_order = save_order_to_db(order_data)
                if not saved_order:
                    return "অর্ডার সেভ করতে সমস্যা হয়েছে। অনুগ্রহ করে আবার চেষ্টা করুন।"
                
                # Complete session
                complete_order_session(admin_id, customer_id)
                
                # Greeting message
                business_name = business.get("name", "আমাদের দোকান") if business else "আমাদের দোকান"
                contact_number = business.get("contact_number", "") if business else ""
                
                greeting = f"""✅ **অর্ডার সফলভাবে গ্রহণ করা হয়েছে!**

**অর্ডার আইডি:** #{saved_order.get('id', 'প্রক্রিয়াধীন')}

আমরা শীঘ্রই আপনার সাথে যোগাযোগ করব।

ধন্যবাদান্তে,
**{business_name}** টিম
{contact_number}"""
                
                return greeting
            else:
                return "দুঃখিত, কনফার্ম করার জন্য কোনো অর্ডার প্রস্তুত নেই।"
        
        # Create system prompt from database ONLY
        system_prompt = create_system_prompt_from_db(admin_id)
        
        # Add product information if available
        if products:
            product_info = "\n\n**পণ্যের তথ্য:**"
            for i, p in enumerate(products[:5], 1):
                stock = p.get('stock', 0)
                price = p.get('price', 0)
                stock_status = "স্টকে আছে" if stock > 0 else "স্টকে নেই"
                product_info += f"\n{i}. {p['name']} - ৳{price} ({stock_status})"
            
            if len(products) > 5:
                product_info += f"\n... এবং আরও {len(products) - 5} টি পণ্য"
            
            system_prompt += product_info
        
        # Define tools for order processing
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "collect_order_info",
                    "description": "গ্রাহকের অর্ডার তথ্য সংগ্রহ করুন",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "গ্রাহকের পূর্ণ নাম"},
                            "phone": {"type": "string", "description": "১১ ডিজিটের মোবাইল নম্বর"},
                            "address": {"type": "string", "description": "পূর্ণাঙ্গ ডেলিভারি ঠিকানা"},
                            "product_name": {"type": "string", "description": "পণ্যের নাম (ডাটাবেসের মতোই)"},
                            "quantity": {"type": "integer", "description": "পণ্যের পরিমাণ"}
                        },
                        "required": ["name", "phone", "address", "product_name", "quantity"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "show_order_summary",
                    "description": "অর্ডার সামারি দেখান এবং কনফার্মেশনের জন্য অপেক্ষা করুন",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "গ্রাহকের নাম"},
                            "phone": {"type": "string", "description": "মোবাইল নম্বর"},
                            "address": {"type": "string", "description": "ঠিকানা"},
                            "items": {"type": "string", "description": "পণ্যের তালিকা"},
                            "total_qty": {"type": "integer", "description": "মোট পরিমাণ"},
                            "subtotal": {"type": "integer", "description": "পণ্যের মোট মূল্য"},
                            "delivery_charge": {"type": "integer", "description": "ডেলিভারি চার্জ"},
                            "total_amount": {"type": "integer", "description": "সর্বমোট টাকা"}
                        },
                        "required": ["name", "phone", "address", "items", "total_qty", "subtotal", "delivery_charge", "total_amount"]
                    }
                }
            }
        ]

        # Prepare messages
        messages = [
            {"role": "system", "content": system_prompt}
        ]
        
        # Add session context if exists
        if session:
            session_status = session.get("status", "collecting_info")
            collected_data = session.get("collected_data", {})
            
            if collected_data:
                context_msg = f"**বর্তমান সেশন:** {session_status}\n"
                if collected_data.get("name"):
                    context_msg += f"নাম: {collected_data['name']}\n"
                if collected_data.get("phone"):
                    context_msg += f"ফোন: {collected_data['phone']}\n"
                if collected_data.get("address"):
                    context_msg += f"ঠিকানা: {collected_data['address']}\n"
                
                cart_items = session.get("cart_items", [])
                if cart_items:
                    context_msg += "\n**কার্টের পণ্য:**\n"
                    for item in cart_items:
                        context_msg += f"- {item.get('name')} x{item.get('quantity', 1)} (৳{item.get('subtotal', 0)})\n"
                
                messages.append({"role": "assistant", "content": context_msg})
        
        # Add chat history
        if history:
            messages.extend(truncate_messages_for_tokens(history))
        
        # Add current message
        messages.append({"role": "user", "content": user_msg[:Config.MAX_MESSAGE_LENGTH]})

        # Call Groq API
        response = call_groq_with_retry(
            client=client,
            messages=messages,
            tools=tools,
            max_retries=2
        )

        if not response:
            return "সার্ভারে সময়সীমা শেষ। অনুগ্রহ করে আবার চেষ্টা করুন।"
        
        msg = response.choices[0].message

        # Handle tool calls
        if msg.tool_calls:
            for tool_call in msg.tool_calls:
                function_name = tool_call.function.name
                args = json.loads(tool_call.function.arguments)
                
                if function_name == "collect_order_info":
                    # Update session with collected info
                    if session:
                        updates = {
                            "collected_data": {
                                "name": args.get("name", ""),
                                "phone": args.get("phone", ""),
                                "address": args.get("address", "")
                            },
                            "status": "items_added"
                        }
                        
                        # Check if product exists
                        product = get_product_by_name(admin_id, args.get("product_name", ""))
                        if product:
                            cart_item = {
                                "product_id": product["id"],
                                "name": product["name"],
                                "quantity": args.get("quantity", 1),
                                "price": product.get("price", 0),
                                "subtotal": product.get("price", 0) * args.get("quantity", 1)
                            }
                            
                            cart_items = session.get("cart_items", [])
                            cart_items.append(cart_item)
                            updates["cart_items"] = cart_items
                            updates["total_amount"] = session.get("total_amount", 0) + cart_item["subtotal"]
                        
                        update_order_session(session["id"], updates)
                    
                    reply = "তথ্য সংগৃহীত হয়েছে। আরও পণ্য যোগ করতে চাইলে বলুন, নাহলে 'সম্পন্ন' লিখুন।"
                
                elif function_name == "show_order_summary":
                    if session:
                        # Update session status
                        update_order_session(session["id"], {
                            "status": "ready_for_confirmation",
                            "delivery_charge": args.get("delivery_charge", 0)
                        })
                    
                    # Show order summary
                    summary = f"""📋 **অর্ডার সামারি:**

**গ্রাহক তথ্য:**
• নাম: {args.get('name')}
• ফোন: {args.get('phone')}
• ঠিকানা: {args.get('address')}

**পণ্য:**
{args.get('items')}

**পরিমাণ:** {args.get('total_qty')} পিছ
**পণ্যের মূল্য:** ৳{args.get('subtotal')}
**ডেলিভারি চার্জ:** ৳{args.get('delivery_charge')}
**সর্বমোট:** ৳{args.get('total_amount')}

✅ **অর্ডার সম্পূর্ণ করতে 'Confirm' লিখুন**
❌ **বাতিল করতে 'Cancel' লিখুন**"""
                    
                    reply = summary
        
        else:
            # No tool call, return AI response
            reply = msg.content if msg.content else "আপনার বার্তা পাওয়া গেছে। কিভাবে সাহায্য করতে পারি?"
        
        # Save to chat history
        history.append({"role": "user", "content": user_msg[:200]})
        history.append({"role": "assistant", "content": reply[:500]})
        save_chat_memory(admin_id, customer_id, history)
        
        return reply

    except requests.exceptions.Timeout:
        logger.error("Groq API timeout")
        return "সার্ভারে সময়সীমা শেষ। অনুগ্রহ করে আবার চেষ্টা করুন।"
    except Exception as e:
        logger.error(f"AI processing error: {e}")
        return "দুঃখিত, সাময়িক কারিগরি সমস্যা হচ্ছে। অনুগ্রহ করে কিছুক্ষণ পর আবার চেষ্টা করুন।"

# ================= SIMPLE RESPONSES =================
def get_quick_response(admin_id: str, user_msg: str) -> Optional[str]:
    """Quick responses for common queries without AI"""
    user_msg_lower = user_msg.lower()
    
    # Product list request
    if any(word in user_msg_lower for word in ['পণ্য', 'প্রোডাক্ট', 'লিস্ট', 'দ্রব্য', 'কি কি', 'সব পণ্য']):
        products = get_products_with_details_cached(admin_id)
        if not products:
            return "দুঃখিত, ডাটাবেসে কোনো পণ্য নেই।"
        
        response = "**পণ্যের তালিকা:**\n\n"
        for i, product in enumerate(products[:8], 1):
            price = product.get('price', 0)
            stock = product.get('stock', 0)
            stock_text = f"({stock} পিছ)" if stock > 0 else "(স্টকে নেই)"
            response += f"{i}. {product['name']} - ৳{price} {stock_text}\n"
        
        if len(products) > 8:
            response += f"\n... এবং আরও {len(products) - 8} টি পণ্য"
        
        return response
    
    # Price request
    price_match = re.search(r'(?:দাম|মূল্য|কত|কি দাম)\s*(?:কি|কত|হচ্ছে|করে)\s*(.*)', user_msg)
    if not price_match:
        price_match = re.search(r'(.*)\s*(?:এর|রে)\s*(?:দাম|মূল্য|কত)', user_msg)
    
    if price_match:
        product_name = price_match.group(1).strip()
        if product_name and len(product_name) > 1:
            product = get_product_by_name(admin_id, product_name)
            if product:
                price = product.get('price', 0)
                stock = product.get('stock', 0)
                stock_text = f"{stock} পিছ স্টকে আছে" if stock > 0 else "স্টকে নেই"
                return f"{product['name']} এর দাম: ৳{price}\nস্টক: {stock_text}"
            else:
                return "দুঃখিত, এই পণ্য ডাটাবেসে নেই।"
    
    # Delivery info request
    if any(word in user_msg_lower for word in ['ডেলিভারি', 'চার্জ', 'ফি', 'কত', 'এলাকা', 'সময়']):
        delivery_info = get_delivery_info_from_db(admin_id)
        if not delivery_info:
            return "দুঃখিত, ডেলিভারি তথ্য ডাটাবেসে নেই।"
        
        response = "**ডেলিভারি তথ্য:**\n"
        if delivery_info.get('delivery_charge') is not None:
            response += f"• চার্জ: ৳{delivery_info['delivery_charge']}\n"
        if delivery_info.get('free_delivery_threshold') is not None:
            response += f"• ফ্রি ডেলিভারি: ৳{delivery_info['free_delivery_threshold']} টাকার উপর\n"
        if delivery_info.get('delivery_time'):
            response += f"• সময়: {delivery_info['delivery_time']}\n"
        
        areas = delivery_info.get('delivery_areas', [])
        if areas:
            response += f"• এলাকা: {', '.join(areas[:3])}"
            if len(areas) > 3:
                response += f" এবং আরও {len(areas) - 3} এলাকা"
        
        return response
    
    # Opening hours request
    if any(word in user_msg_lower for word in ['খোলার সময়', 'সময়', 'খোলা', 'বন্ধ', 'কখন']):
        opening_hours = get_opening_hours_from_db(admin_id)
        if not opening_hours:
            return "দুঃখিত, খোলার সময় ডাটাবেসে নেই।"
        
        response = "**খোলার সময়:**\n"
        for day, hours in list(opening_hours.items())[:7]:
            response += f"• {day}: {hours}\n"
        return response
    
    # Payment methods request
    if any(word in user_msg_lower for word in ['পেমেন্ট', 'পরিশোধ', 'টাকা দেব', 'পেমেন্ট সিস্টেম']):
        payment_methods = get_payment_methods_from_db(admin_id)
        if not payment_methods:
            return "দুঃখিত, পেমেন্ট পদ্ধতি ডাটাবেসে নেই।"
        
        return f"**পেমেন্ট পদ্ধতি:** {', '.join(payment_methods)}"
    
    return None

# ================= WEBHOOK ENDPOINT =================
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    """Facebook webhook endpoint"""
    if request.method == "GET":
        if request.args.get("hub.mode") == "subscribe":
            if request.args.get("hub.verify_token") == os.getenv("FACEBOOK_VERIFY_TOKEN"):
                return request.args.get("hub.challenge"), 200
            return "Verification token mismatch", 403
        return "OK", 200

    try:
        data = request.get_json()
        
        if not data or "entry" not in data:
            return jsonify({"status": "error", "message": "Invalid data"}), 400
        
        for entry in data.get("entry", []):
            page_id = entry.get("id")
            page = get_page_client_cached(page_id)
            
            if not page:
                logger.warning(f"No connected page found for ID: {page_id}")
                continue
            
            admin_id = page["user_id"]
            page_token = page["page_access_token"]
            
            for messaging in entry.get("messaging", []):
                sender_id = messaging.get("sender", {}).get("id")
                message = messaging.get("message", {})
                text = message.get("text", "").strip()
                
                if not text or not sender_id:
                    continue
                
                logger.info(f"Message from {sender_id} (Page: {page_id}): {text}")
                
                # Check for order cancellation
                if text.lower() in ['cancel', 'বাতিল', 'না']:
                    # Cancel order session
                    complete_order_session(admin_id, sender_id)
                    send_message(page_token, sender_id, "অর্ডার বাতিল করা হয়েছে। আবার অর্ডার দিতে চাইলে বলুন।")
                    continue
                
                # First try FAQ
                faq_answer = find_faq_cached(admin_id, text)
                if faq_answer:
                    send_message(page_token, sender_id, faq_answer)
                    continue
                
                # Try quick response (no AI)
                quick_response = get_quick_response(admin_id, text)
                if quick_response:
                    send_message(page_token, sender_id, quick_response)
                    continue
                
                # Process with AI
                ai_response = process_order_with_ai(admin_id, sender_id, text)
                send_message(page_token, sender_id, ai_response)
        
        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ================= HEALTH CHECK =================
@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    try:
        supabase.table("products").select("id").limit(1).execute()
        
        return jsonify({
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "service": "facebook-chatbot",
            "config": {
                "max_tokens": Config.MAX_TOKENS,
                "model": Config.GROQ_MODEL
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
    port = int(os.getenv("PORT", 10000))
    debug = os.getenv("DEBUG", "false").lower() == "true"
    
    logger.info(f"Starting Facebook Chatbot on port {port}")
    logger.info(f"Groq Model: {Config.GROQ_MODEL}, Max Tokens: {Config.MAX_TOKENS}")
    logger.info("No hardcoded business data - all from database only")
    
    app.run(host="0.0.0.0", port=port, debug=debug)

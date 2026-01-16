import os
import re
import logging
import requests
import json
import time
from typing import Optional, Dict, Tuple, List, Any
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
from openai import OpenAI
from supabase import create_client, Client
import concurrent.futures
from functools import lru_cache

# ================= CONFIG =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
app = Flask(__name__)

# Global in-memory cache with TTL
class CacheWithTTL:
    def __init__(self, ttl_seconds=300):
        self.cache = {}
        self.ttl = ttl_seconds
    
    def get(self, key):
        if key in self.cache:
            value, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                return value
            else:
                del self.cache[key]
        return None
    
    def set(self, key, value):
        self.cache[key] = (value, time.time())
    
    def clear_old(self):
        current = time.time()
        self.cache = {k: v for k, v in self.cache.items() if current - v[1] < self.ttl}

processed_messages = CacheWithTTL(300)  # 5 minutes TTL

# Supabase Client Setup with connection pooling
supabase: Optional[Client] = None
try:
    supabase = create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_SERVICE_KEY"),
        options={"postgrest_client_timeout": 10, "storage_client_timeout": 10}
    )
except Exception as e:
    logger.error(f"Supabase connection failed: {e}")

# Thread pool for async operations
thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=10)

# ================= CACHED DATA FETCHERS =================
@lru_cache(maxsize=100)
def get_cached_subscription_status(user_id: str) -> Tuple[bool, float]:
    """Cache subscription status for 30 seconds"""
    try:
        res = supabase.table("subscriptions")\
            .select("status, trial_end, end_date, paid_until")\
            .eq("user_id", user_id)\
            .limit(1)\
            .execute()
        
        if not res.data:
            return False, time.time()
        
        sub = res.data[0]
        status = sub.get("status")
        
        if status not in ["active", "trial"]:
            return False, time.time()
        
        expiry_str = sub.get("paid_until") or sub.get("end_date") or sub.get("trial_end")
        if expiry_str:
            try:
                # Simplified date parsing
                clean_expiry = expiry_str.strip().replace(' ', 'T')
                if '+00' in clean_expiry and ':00' not in clean_expiry:
                    clean_expiry = clean_expiry.replace('+00', '+00:00')
                
                expiry_date = datetime.fromisoformat(clean_expiry)
                if datetime.now(timezone.utc) > expiry_date:
                    supabase.table("subscriptions")\
                        .update({"status": "expired"})\
                        .eq("user_id", user_id)\
                        .execute()
                    return False, time.time()
            except Exception as e:
                logger.error(f"Date parsing error: {e}")
                return False, time.time()
        
        return True, time.time()
    except Exception as e:
        logger.error(f"Subscription check error: {e}")
        return False, time.time()

def check_subscription_status(user_id: str) -> bool:
    """Wrapper with cache"""
    cached = get_cached_subscription_status(user_id)
    if time.time() - cached[1] > 30:  # Cache expired
        get_cached_subscription_status.cache_clear()
        cached = get_cached_subscription_status(user_id)
    return cached[0]

@lru_cache(maxsize=100)
def get_cached_bot_settings(user_id: str) -> Dict:
    """Cache bot settings for 60 seconds"""
    try:
        res = supabase.table("bot_settings")\
            .select("*")\
            .eq("user_id", user_id)\
            .limit(1)\
            .execute()
        if res.data:
            return res.data[0]
    except Exception as e:
        logger.error(f"Error fetching bot settings: {e}")
    return {
        "ai_reply_enabled": True,
        "hybrid_mode": True,
        "faq_only_mode": False,
        "typing_delay": 0,
        "welcome_message": ""
    }

def get_bot_settings(user_id: str) -> Dict:
    """Wrapper with cache"""
    return get_cached_bot_settings(user_id)

@lru_cache(maxsize=100)
def get_cached_products(user_id: str) -> List[Dict]:
    """Cache products for 120 seconds"""
    try:
        res = supabase.table("products")\
            .select("*")\
            .eq("user_id", user_id)\
            .execute()
        return res.data or []
    except Exception as e:
        logger.error(f"Error fetching products: {e}")
        return []

def get_products_with_details(user_id: str) -> List[Dict]:
    """Wrapper with cache"""
    return get_cached_products(user_id)

# ================= SESSION DB HELPERS (OPTIMIZED) =================
class OrderSession:
    def __init__(self, user_id: str, customer_id: str):
        self.user_id = user_id
        self.customer_id = customer_id
        self.session_id = f"order_{user_id}_{customer_id}"
        self.step = 0 
        self.data = {"name": "", "phone": "", "product": "", "items": [], "address": "", "delivery_charge": 0, "total": 0}

    def save_order(self, product_total: float, delivery_charge: float) -> bool:
        try:
            res = supabase.table("orders").insert({
                "user_id": self.user_id,
                "customer_name": self.data.get("name"),
                "customer_phone": self.data.get("phone"),
                "product": self.data.get("product"), 
                "address": self.data.get("address"),
                "total": float(product_total + delivery_charge),
                "delivery_charge": float(delivery_charge),
                "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat()
            }).execute()
            return True if res.data else False
        except Exception as e:
            logger.error(f"DB Save Error: {e}")
            return False

def get_session_from_db(session_id: str) -> Optional[OrderSession]:
    try:
        res = supabase.table("order_sessions").select("*").eq("id", session_id).execute()
        if res.data:
            row = res.data[0]
            session = OrderSession(row['user_id'], row['customer_id'])
            session.step = row['step']
            default_data = {"name": "", "phone": "", "product": "", "items": [], "address": "", "delivery_charge": 0, "total": 0}
            default_data.update(row['data']) 
            session.data = default_data
            return session
    except Exception as e:
        logger.error(f"Error getting session: {e}")
    return None

def save_session_to_db(session: OrderSession):
    try:
        supabase.table("order_sessions").upsert({
            "id": session.session_id,
            "user_id": session.user_id,
            "customer_id": session.customer_id,
            "step": session.step,
            "data": session.data,
            "last_updated": datetime.now(timezone.utc).isoformat()
        }).execute()
    except Exception as e:
        logger.error(f"Error saving session: {e}")

def delete_session_from_db(session_id: str):
    try:
        supabase.table("order_sessions").delete().eq("id", session_id).execute()
    except Exception as e:
        logger.error(f"Error deleting session: {e}")

# ================= OPTIMIZED HELPERS =================
def get_page_client(page_id):
    res = supabase.table("facebook_integrations").select("*").eq("page_id", str(page_id)).eq("is_connected", True).execute()
    return res.data[0] if res.data else None

def send_message_async(token, user_id, text):
    if not text: return
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={token}"
    
    def send():
        try:
            requests.post(url, json={"recipient": {"id": user_id}, "message": {"text": text}}, timeout=3)
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
    
    thread_pool.submit(send)

def send_image_async(token, user_id, image_url):
    if not image_url: return
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={token}"
    payload = {
        "recipient": {"id": user_id},
        "message": {
            "attachment": {
                "type": "image",
                "payload": {"url": image_url, "is_reusable": True}
            }
        }
    }
    
    def send():
        try:
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            logger.error(f"Failed to send image: {e}")
    
    thread_pool.submit(send)

# ================= CACHED DATA FETCHERS =================
@lru_cache(maxsize=100)
def get_cached_faqs(user_id: str):
    try:
        res = supabase.table("faqs").select("question, answer").eq("user_id", user_id).execute()
        return res.data or []
    except Exception as e:
        logger.error(f"Error fetching FAQs: {e}")
        return []

def get_faqs(user_id: str):
    return get_cached_faqs(user_id)

@lru_cache(maxsize=100)
def get_cached_business_settings(user_id: str) -> Optional[Dict]:
    try:
        res = supabase.table("business_settings").select("*").eq("user_id", user_id).limit(1).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"Error fetching business settings: {e}")
        return None

def get_business_settings(user_id: str) -> Optional[Dict]:
    return get_cached_business_settings(user_id)

def get_chat_memory(user_id: str, customer_id: str, limit: int = 10) -> List[Dict]:
    try:
        res = supabase.table("chat_history").select("messages").eq("user_id", user_id).eq("customer_id", customer_id).limit(1).execute()
        return res.data[0].get("messages", [])[-limit:] if res.data else []
    except Exception as e:
        logger.error(f"Error fetching chat memory: {e}")
        return []

def save_chat_memory(user_id: str, customer_id: str, messages: List[Dict]):
    now = datetime.now(timezone.utc).isoformat()
    
    def save():
        try:
            existing = supabase.table("chat_history").select("id").eq("user_id", user_id).eq("customer_id", customer_id).execute()
            if existing.data:
                supabase.table("chat_history").update({"messages": messages, "last_updated": now}).eq("id", existing.data[0]["id"]).execute()
            else:
                supabase.table("chat_history").insert({"user_id": user_id, "customer_id": customer_id, "messages": messages, "created_at": now, "last_updated": now}).execute()
        except Exception as e:
            logger.error(f"Error saving chat memory: {e}")
    
    thread_pool.submit(save)

# ================= PRODUCT QUANTITY UPDATER =================
def update_product_quantity(user_id: str, product_name: str, quantity_sold: int) -> bool:
    """Update product quantity in database after order confirmation"""
    try:
        logger.info(f"Updating quantity for product '{product_name}' for user {user_id}, quantity: {quantity_sold}")
        
        # Get products from cache
        products = get_cached_products(user_id)
        
        if not products:
            logger.warning(f"No products found for user {user_id}")
            return False
        
        # Use the improved matching function
        matched_product = find_best_product_match(product_name, products)
        
        if not matched_product:
            logger.warning(f"Product '{product_name}' not found in database.")
            return False
        
        product_id = matched_product["id"]
        current_quantity = matched_product.get("quantity", 0)
        matched_product_name = matched_product.get("name", "")
        in_stock = matched_product.get("in_stock", True)
        
        logger.info(f"Found product: '{matched_product_name}', Current quantity: {current_quantity}, In stock: {in_stock}, Requested: {quantity_sold}")
        
        # CRITICAL FIX: Check both quantity AND in_stock status
        if not in_stock:
            logger.error(f"Product '{matched_product_name}' is marked as out of stock (in_stock: {in_stock})")
            return False
        
        # Check if enough stock is available
        if current_quantity < quantity_sold:
            logger.error(f"Insufficient stock for product '{matched_product_name}': {current_quantity} available, {quantity_sold} requested")
            return False
        
        # Calculate new quantity
        new_quantity = max(0, current_quantity - quantity_sold)
        
        # Update quantity AND in_stock status if quantity becomes 0
        update_data = {"quantity": new_quantity}
        if new_quantity == 0:
            update_data["in_stock"] = False
        
        # Update quantity in database
        update_res = supabase.table("products")\
            .update(update_data)\
            .eq("id", product_id)\
            .execute()
        
        if update_res.data:
            logger.info(f"Successfully updated quantity for product '{matched_product_name}' (ID: {product_id}): {current_quantity} -> {new_quantity}")
            # Clear cache for this user's products
            get_cached_products.cache_clear()
            return True
        else:
            logger.error(f"Failed to update quantity for product ID {product_id}")
            return False
            
    except Exception as e:
        logger.error(f"Error updating product quantity: {str(e)}", exc_info=True)
    return False

# ================= FULL AI LOGIC (YOUR ORIGINAL STYLE) =================
def generate_ai_reply_with_retry(user_id, customer_id, user_msg, current_session_data, max_retries=2):
    # Get data from cache
    business = get_cached_business_settings(user_id)
    products = get_cached_products(user_id)
    faqs = get_cached_faqs(user_id)
    
    biz_phone = business.get('contact_number', '') if business else ""
    business_name = business.get('name', 'আমাদের শপ') if business else "আমাদের শপ"
    business_address = business.get('address', 'ঠিকানা উপলব্ধ নয়') if business else "ঠিকানা উপলব্ধ নয়"
    
    session_charge = current_session_data.get('delivery_charge', 0)
    
    opening_hours = business.get('opening_hours', 'তথ্য নেই') if business else "তথ্য নেই"
    delivery_info = business.get('delivery_info', 'তথ্য নেই') if business else "তথ্য নেই"
    payment_methods = business.get('payment_methods', []) if business else []

    categories = sorted(list(set([p.get('category') for p in products if p.get('category')])))
    category_list_str = ", ".join(categories) if categories else "তথ্য নেই"

    # Product list with EXACT names as in database
    product_list_short = "\n".join([f"- {p.get('name')}: ৳{p.get('price')} (Category: {p.get('category', 'N/A')})" for p in products if p.get("in_stock")])
    product_details_full = "\n".join([f"পণ্য: {p.get('name')}\nদাম: ৳{p.get('price')}\nক্যাটাগরি: {p.get('category')}\nবিবরণ: {p.get('description')}" for p in products if p.get("in_stock")])
    
    faq_text = "\n".join([f"Q: {f['question']} | A: {f['answer']}" for f in faqs])

    known_info_str = f"প্রাপ্ত তথ্য - নাম: {current_session_data.get('name', 'নেই')}, ফোন: {current_session_data.get('phone', 'নেই')}, ঠিকানা: {current_session_data.get('address', 'নেই')}."

    # YOUR EXACT ORIGINAL SYSTEM PROMPT
    system_prompt = ( f"""
তুমি '{business_name}'-এর একজন বাস্তব মানুষ, তোমার নাম সিমান্ত। তুমি অত্যন্ত নম্র এবং বন্ধুসুলভ সেলস এক্সপার্ট।

তোমার কথা বলার ধরন:
- খুব স্বাভাবিক, কথ্য বাংলা
- ছোট ও পরিষ্কার বাক্য (Must Follow This Rule)
- WhatsApp / Messenger এ যেভাবে মানুষ কথা বলে সেভাবে
- বেশি formal বা বইয়ের ভাষা ব্যবহার করবে না

তোমার আচরণ:
- আগে গ্রাহকের কথা বুঝবে
- এক উত্তরে বেশি তথ্য দেবে না
- দরকার হলে পাল্টা প্রশ্ন করবে

তোমার বিক্রয় কৌশল (Strict Rules):
- যখনই গ্রাহক সব পণ্য দেখতে চাইবে, তুমি একসাথে সব পণ্যের লিস্ট দিবে না। এটা বিরক্তিকর। 
- প্রথমে তুমি {category_list_str} দেখে আমাদের কাছে কি ধরনের পণ্য আছে তা নিজের ভাষায় সুন্দর করে বলবে
- গ্রাহককে জিজ্ঞেস করো সে কোন ধরনের পণ্য খুঁজছে।
- গ্রাহক যখন নির্দিষ্ট কিছু চাইবে, তখন আমাদের ডাটাবেস থেকে মিল আছে এমন মাত্র ২-৩টি সেরা পণ্য দেখাবে।
- গ্রাহক কোনো একটা পণ্যের কোন নির্দিষ্ট তথ্য জানতে চাইলে, ডাটাবেস দেখে নির্দিষ্ট তথ্যটি নিজের ভাষায় সুন্দর করে বলবে

**স্টক সম্পর্কে কঠোর নিয়ম (NEW - VERY IMPORTANT):**
- যখন কোনো পণ্যের stock 0 (শূন্য) থাকে, তখন সেই পণ্যটি গ্রাহককে দেখাবে না।
- যদি ডাটাবেসে কোনো পণ্যের quantity 0 থাকে, তুমি সেটি recommend করবে না, suggest করবে না, বা mention করবে না।
- শুধুমাত্র 'in_stock' status True আছে এবং quantity > 0 আছে এমন পণ্যই গ্রাহককে দেখাবে।
- যদি কোনো পণ্য out of stock হয়, তুমি বলবে: "দুঃখিত, এই পণ্যটি এখন স্টক নেই।"

পণ্য সংক্রান্ত নিয়ম (EXTREMELY STRICT - কঠোরভাবে মেনে চলতে হবে):
1. **পণ্যের নাম সম্পর্কে ABSOLUTE RULE (সবচেয়ে গুরুত্বপূর্ণ নিয়ম):** 
   - তুমি পণ্যের নাম কখনোই অনুবাদ করবে না, পরিবর্তন করবে না, বা বাংলায় বলবে না।
   - পণ্যের নাম ডাটাবেসে যেভাবে আছে (English/Bangla/Mixed) ঠিক সেভাবেই বলবে।
   - উদাহরণ: ডাটাবেসে "iPhone 15 Pro" থাকলে তুমি "আইফোন ১৫ প্রো" বলবে না, "iPhone 15 Pro" বলবে।
   - উদাহরণ: ডাটাবেসে "আলুর চিপস" থাকলে তুমি "Potato Chips" বলবে না, "আলুর চিপস" বলবে।
   - নামটা হুবহু ডাটাবেসের মতো বলতে হবে।

2. লিস্ট চাইলে শুধু নাম ও দাম দেখাবে (নাম ডাটাবেসের মতো)
3. নির্দিষ্ট পণ্য জিজ্ঞেস করলে সেই পণ্যের ডাটাবেস দেখে তথ্য গুলা নিজের ভাষায় সুন্দর করে বোঝাবে 
4. পণ্য সম্পর্কে কোনোরকম মিথ্যা প্রতিশ্রুতি দিবেনা 
5. গ্রাহক কোন নির্দিষ্ট তথ্য জানতে চাইলে, ডাটাবেস দেখে নির্দিষ্ট তথ্যটি নিজের ভাষায় সুন্দর করে বলবে 

ডেলিভারি চার্জ সংক্রান্ত কঠোর নিয়ম:
- আমাদের ডেলিভারি পলিসি: {delivery_info}
- যখনই গ্রাহক ঠিকানা দিবে, তুমি সাথে সাথে ওই ঠিকানার জন্য ডেলিভারি চার্জ কত হবে তা আমাদের পলিসি দেখে গ্রাহককে জানাবে।
- গ্রাহক চার্জ মেনে নিলে বা চার্জ জানানো হলে তবেই পরবর্তী ধাপে যাবে। 
- অর্ডার সামারি দেখানোর আগে অবশ্যই ডেলিভারি চার্জসহ মোট কত টাকা হয়েছে তা পরিষ্কার করে বলবে।

অর্ডার আচরণ (Very Strict Rules - মনোযোগ দিয়ে শোনো):
- যতক্ষণ পর্যন্ত গ্রাহকের **নাম (Name)** এবং **ফোন নম্বর (Phone)** এবং **ঠিকানা (Address)** না পাচ্ছ, ততক্ষণ পর্যন্ত ভুলেও "Confirm" বা "কনফার্ম" শব্দটি ব্যবহার করবে না।
- যদি নাম বা ফোন নম্বর না থাকে, তবে সুন্দর করে সেটি চাও। অর্ডার সামারি দেখাবে না।
- শুধুমাত্র নাম, ফোন এবং ঠিকানা পাওয়ার পরেই তুমি অর্ডার সামারি দেখাবে এবং গ্রাহককে কনফার্ম করতে বলবে।
- তুমি নিজে কখনো বলবে না যে অর্ডার কনফার্ম হয়েছে বা সফল হয়েছে। তুমি শুধু গ্রাহককে তথ্য দিয়ে সাহায্য করবে এবং সব তথ্য পেলে বলবে যে অর্ডারটি কনফার্ম করতে 'Confirm' লিখতে।
- **গুরুত্বপূর্ণ নিয়ম: ব্যবসার তথ্য (business details) কখনোই গ্রাহকের তথ্য (customer details) হিসাবে নিবে না।** 
  - যদি গ্রাহক ব্যবসার নাম/ঠিকানা/নম্বর জিজ্ঞেস করে, তুমি সেটা উত্তর দিবে কিন্তু সেটাকে গ্রাহকের নিজের তথ্য হিসাবে গণ্য করবে না।
  - শুধুমাত্র যখন গ্রাহক সরাসরি বলে "আমার নাম X", "আমার ফোন Y", "আমার ঠিকানা Z" - তখনই সেটাকে গ্রাহকের তথ্য হিসাবে নিবে।

তোমার জন্য কঠোর নিয়মাবলী:
১. শুধুমাত্র বাংলা ভাষা: তুমি গ্রাহকের সাথে সর্বদা এবং বাধ্যতামূলকভাবে বাংলায় কথা বলবে। কোনো ইংরেজি বাক্য বা মিশ্র ভাষা ব্যবহার করবে না কিন্তু পণ্যের নাম ডাটাবেসে যেভাবে আছে, ঠিক সেভাবেই বলবে। নামের অনুবাদ করবে না।
২. পণ্যের গুণগান: গ্রাহক যখনই কোনো পণ্য নিয়ে কথা বলবে, তুমি ডাটাবেস থেকে ওই পণ্যের 'Description' দেখে তার ভালো দিক ও সুবিধাগুলো চমৎকারভাবে কথার মাঝে বারবার তুলে ধরবে যাতে গ্রাহক পণ্যটি নিতে আগ্রহী হয়।
৩. জোর করবে না: গ্রাহককে অর্ডার করার জন্য বা নাম, ফোন নম্বর, ঠিকানা দেওয়ার জন্য বারবার অনুরোধ বা জোর করবে না। গ্রাহক নিজে থেকে কিনতে আগ্রহী হলে তখন তথ্য চাইবেন।
৪. ছবি পাঠানোর নিয়ম (Strict Image Logic): প্রতি মেসেজে ছবি পাঠাবেন না। যদি গ্রাহক নিজে থেকে ছবি দেখতে চায় ("chobi", "pic", "image" লিখে), শুধুমাত্র তখনই একবার ছবি দেখাবেন।
৫. কথা বলার ধরন: ছোট ও পরিষ্কার বাক্যে হোয়াটসঅ্যাপের মতো স্বাভাবিক বাংলায় কথা বলবে।

**সবচেয়ে গুরুত্বপূর্ণ নিয়ম (MOST STRICT RULE):** পণ্যের নাম ডাটাবেসে যেভাবে আছে, ঠিক সেভাবেই বলবে। কখনোই পণ্যের নামের অনুবাদ করবে না। নামটা হুবহু ডাটাবেসের মতো বলতে হবে।

ব্যবসায়িক তথ্য (শুধু উত্তর দেওয়ার জন্য, গ্রাহকের তথ্য হিসাবে নয়):
- খোলা থাকে: {opening_hours}
- ডেলিভারি তথ্য: {delivery_info}
- পেমেন্ট মাধ্যম: {payment_methods}
- শপের ঠিকানা: {business_address}
- কল করুন: {biz_phone}
- ডেলিভারি চার্জ: (উপরের 'ডেলিভারি তথ্য' অনুযায়ী গ্রাহককে জানাও)

জানা তথ্য: {known_info_str}
উপলব্ধ ক্যাটাগরি: {category_list_str}
পণ্য তালিকা: {product_list_short}
পণ্যের বিস্তারিত (এখান থেকে গুণগান করবে): {product_details_full}
FAQ: {faq_text}

সব উত্তর ২–৪ লাইনের মধ্যে রাখবে।
"""
    )

    memory = get_chat_memory(user_id, customer_id)
    
    # Get API keys
    try:
        api_key_res = supabase.table("api_keys").select("groq_api_key, groq_api_key_2, groq_api_key_3, groq_api_key_4, groq_api_key_5").eq("user_id", user_id).execute()
        if not api_key_res.data:
            logger.error(f"No API keys found for user {user_id}")
            return None, None
        
        row = api_key_res.data[0]
        keys = [row.get('groq_api_key'), row.get('groq_api_key_2'), row.get('groq_api_key_3'), row.get('groq_api_key_4'), row.get('groq_api_key_5')]
        valid_keys = [k for k in keys if k and k.strip()]
    except Exception as e:
        logger.error(f"Error fetching API keys: {e}")
        return None, None

    if not valid_keys:
        return None, None

    for key in valid_keys:
        client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=key)
        try:
            res = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": system_prompt}] + memory + [{"role": "user", "content": user_msg}],
                temperature=0.5, 
                timeout=5.0 
            )
            reply = res.choices[0].message.content.strip()
            save_chat_memory(user_id, customer_id, (memory + [{"role": "user", "content": user_msg}, {"role": "assistant", "content": reply}])[-10:])
            
            # --- FEATURE 2: STRICT IMAGE LOGIC ---
            matched_image = None
            
            # 1. Check if user explicitly asked for image
            image_request_keywords = ['chobi', 'photo', 'image', 'dekhan', 'dekhi', 'ছবি', 'দেখাও', 'দেখি', 'pic']
            wants_to_see_image = any(word in user_msg.lower() for word in image_request_keywords)
            
            # 2. Check if ANY image was sent previously in this conversation
            already_sent_image = any("image_url" in str(m) or "attachment" in str(m) for m in memory)

            # 3. Check product mentions
            mentioned_products = [p for p in products if p.get('name') and p.get('name').lower() in reply.lower()]

            # Logic: Send ONLY if user asks OR (Single product discussed AND No image sent before)
            if len(mentioned_products) == 1:
                product = mentioned_products[0]
                if wants_to_see_image or not already_sent_image:
                    matched_image = product.get('image_url')
            
            return reply, matched_image
        except Exception as e:
            logger.error(f"AI Generation Error: {e}")
            continue 
    
    return None, None

# ================= ORDER EXTRACTION (DYNAMIC SAAS VERSION) =================
def extract_order_data_with_retry(user_id, messages, delivery_policy_text, max_retries=2):
    try:
        api_key_res = supabase.table("api_keys").select("groq_api_key, groq_api_key_2, groq_api_key_3, groq_api_key_4, groq_api_key_5").eq("user_id", user_id).execute()
        if not api_key_res.data: return None
        
        row = api_key_res.data[0]
        keys = [row.get('groq_api_key'), row.get('groq_api_key_2'), row.get('groq_api_key_3'), row.get('groq_api_key_4'), row.get('groq_api_key_5')]
        valid_keys = [k for k in keys if k and k.strip()]

        if not valid_keys: return None

        # --- DYNAMIC PROMPT FOR SAAS (NO HARDCODING) ---
        prompt = (
            "Extract order details from the conversation into JSON. "
            "Keys: name, phone, address, items (product_name, quantity), delivery_charge (number or null). "
            f"CONTEXT (Strictly use this policy): '{delivery_policy_text}'. "
            "IMPORTANT RULES: "
            "1. Extract ONLY customer details, NOT business details. "
            "2. If customer asks about business address/phone, DO NOT treat it as customer address/phone. "
            "3. Extract customer name ONLY if explicitly stated by customer (e.g., 'আমার নাম X', 'নাম X', 'I am X'). "
            "4. Extract customer phone ONLY if explicitly stated by customer (e.g., 'আমার ফোন X', 'ফোন X', 'মোবাইল X'). "
            "5. Extract customer address ONLY if explicitly stated by customer (e.g., 'আমার ঠিকানা X', 'ঠিকানা X', 'পাঠাবো X'). "
            "6. Identify the delivery charge BY COMPARING the user's address with the provided 'CONTEXT'. "
            "7. Do NOT use any pre-set values. Only use values found in the CONTEXT. "
            "8. If the user's address matches a location in the policy, extract the specific numeric charge. "
            "9. If you cannot find a match or the address is missing, set delivery_charge to null. "
            "10. Return ONLY a valid JSON object."
        )

        for key in valid_keys:
            client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=key)
            try:
                res = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "system", "content": prompt}] + messages[-8:], 
                    response_format={"type": "json_object"},
                    temperature=0,
                    timeout=4.0
                )
                content = res.choices[0].message.content
                cleaned_content = re.sub(r"```json|```", "", content).strip()
                extracted_json = json.loads(cleaned_content)
                
                # Ensure delivery_charge is returned as a number (0 if null)
                if 'delivery_charge' in extracted_json:
                    try:
                        extracted_json['delivery_charge'] = float(extracted_json['delivery_charge'])
                    except (TypeError, ValueError):
                        extracted_json['delivery_charge'] = 0.0  # Default to 0 instead of None
                        
                return extracted_json
            except Exception as e:
                logger.error(f"Extraction Error: {e}")
                continue
    except Exception as e:
        logger.error(f"Error in order extraction: {e}")
    return None

# ================= IMPROVED PRODUCT MATCHING =================
def find_best_product_match(product_name: str, products_db: List[Dict]) -> Optional[Dict]:
    """
    Find the best matching product using exact matching first, then word boundary matching
    Solves the issue where 'iPhone 15' might incorrectly match 'iPhone 15 Pro'
    """
    if not product_name or not products_db:
        return None
    
    product_name_lower = product_name.lower().strip()
    
    # 1. Try exact case-insensitive match first
    for product in products_db:
        if product.get('name') and product['name'].lower() == product_name_lower:
            return product
    
    # 2. Try word boundary matching (product name as whole word)
    # This prevents "iPhone 15" matching "iPhone 15 Pro"
    for product in products_db:
        db_name = product.get('name', '').lower()
        if db_name:
            # Check if product_name appears as a whole word in db_name
            pattern = r'\b' + re.escape(product_name_lower) + r'\b'
            if re.search(pattern, db_name):
                return product
    
    # 3. Try if db_name appears as whole word in product_name
    for product in products_db:
        db_name = product.get('name', '').lower()
        if db_name:
            pattern = r'\b' + re.escape(db_name) + r'\b'
            if re.search(pattern, product_name_lower):
                return product
    
    # 4. Fallback to substring matching (original logic)
    for product in products_db:
        db_name = product.get('name', '').lower()
        if db_name and (product_name_lower in db_name or db_name in product_name_lower):
            return product
    
    return None

# ================= SMART ORDER CONFIRMATION DETECTION =================
def detect_order_confirmation_intent(text: str, session_data: Dict) -> Tuple[bool, str]:
    """
    Smart detection of order confirmation intent.
    Returns (is_confirmation, intent_type)
    intent_type: 'confirm', 'delay', 'deny', or 'neutral'
    """
    text_lower = text.lower().strip()
    
    # Confirmation patterns - ALL positive confirmations
    confirm_patterns = [
        r'^(confirm|conform|Hae confirm koren|Okay confirm koren|confirm kore den)$',
        r'^(yes|yep|yeah|ha|haa|haan|হ্যা|হ্যাঁ|হা|হাঃ|হে)$',
        r'^(ঠিক আছে|ঠিক|ঠিকহ|okay|ok|okay confirm)$',
        r'^(order confirm|অর্ডার কনফার্ম|অর্ডার কনফার্ম কর)$',
        r'^(done|দাও|নিশ্চিত কর)$',
        r'^(পাঠাও|পাঠান|send)$',
        r'^(চলুক|চলবে|go ahead)$',
        r'^(agreed|agree|এগ্রি)$',
        r'^(\+1|\👍|\✅|\✔️|\😊)$'  # Positive emojis
    ]
    
    # Delay patterns - Customer wants to confirm later
    delay_patterns = [
        r'(পরে|পর্য|later|আগে|after|wait|hold on|দেরি)',
        r'(আরেকটু.*পর্য|wait.*bit)',
        r'(think.*করব|think.*করি|ভেবে.*দেখি)',
        r'(not.*now|now.*not|এখন.*না)',
        r'(কিছুক্ষন.*পর্য|few.*minutes)'
    ]
    
    # Denial patterns - Customer doesn't want to order
    deny_patterns = [
        r'^(no|না|নাহ|না ধন্যবাদ|no thanks|not now)$',
        r'^(cancel|বাতিল|stop|স্টপ)$',
        r'^(don\'t.*want|চাইনা|চাই না)$',
        r'^(maybe.*later|maybe.*পর্য)'
    ]
    
    # Check for confirmation
    for pattern in confirm_patterns:
        if re.search(pattern, text_lower, re.IGNORECASE):
            # Additional check: if customer has all required info
            has_required_info = all([
                session_data.get("name"),
                session_data.get("phone"), 
                session_data.get("address"),
                session_data.get("items")
            ])
            
            if has_required_info:
                return True, 'confirm'
            else:
                # Customer said confirm but missing info
                return False, 'incomplete'
    
    # Check for delay
    for pattern in delay_patterns:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return False, 'delay'
    
    # Check for denial
    for pattern in deny_patterns:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return False, 'deny'
    
    return False, 'neutral'

# ================= OPTIMIZED WEBHOOK HANDLER =================
def process_message_batch(msg_event: Dict, page: Dict):
    """Process single message efficiently"""
    try:
        sender = msg_event["sender"]["id"]
        raw_text = msg_event["message"].get("text", "")
        user_id = page["user_id"]
        token = page["page_access_token"]
        page_id = page["page_id"]
        
        if not raw_text:
            return
        
        # Check subscription
        if not check_subscription_status(user_id):
            logger.info(f"Subscription inactive for user {user_id}")
            return
        
        # Get bot settings
        bot_settings = get_bot_settings(user_id)
        if not bot_settings.get("ai_reply_enabled", True):
            return
        
        # Typing delay
        delay_ms = bot_settings.get("typing_delay", 0)
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)
        
        # Session management
        session_id = f"order_{user_id}_{sender}"
        current_session = get_session_from_db(session_id)
        if not current_session:
            current_session = OrderSession(user_id, sender)
        
        # Update page_id for follow-up
        try:
            supabase.table("order_sessions")\
                .update({"page_id": page_id})\
                .eq("id", session_id)\
                .execute()
        except Exception:
            pass
        
        # Get memory
        memory = get_chat_memory(user_id, sender)
        
        # Welcome message for new conversations
        welcome_msg = bot_settings.get("welcome_message")
        if not memory and welcome_msg:
            send_message_async(token, sender, welcome_msg)
            save_chat_memory(user_id, sender, [{"role": "assistant", "content": welcome_msg}])
        
        # Order extraction
        temp_memory = memory + [{"role": "user", "content": raw_text}]
        business = get_cached_business_settings(user_id)
        delivery_policy = business.get('delivery_info', "তথ্য নেই") if business else "তথ্য নেই"
        
        extracted = extract_order_data_with_retry(user_id, temp_memory, delivery_policy)
        
        if extracted:
            # --- FIXED: NOTIFY USER ABOUT DELIVERY CHARGE IMMEDIATELY ---
            had_address = bool(current_session.data.get("address"))
            
            # Only update if extracted data is NOT business details
            business_address = business.get('address', '') if business else ''
            business_phone = business.get('contact_number', '') if business else ''
            
            # Check if extracted address is actually business address
            if extracted.get("address") and business_address:
                if business_address.lower() in extracted.get("address", "").lower():
                    logger.info(f"Ignoring business address as customer address: {extracted.get('address')}")
                else:
                    if extracted.get("address"): 
                        current_session.data["address"] = extracted["address"]
            
            # Check if extracted phone is actually business phone
            if extracted.get("phone") and business_phone:
                if business_phone in extracted.get("phone", ""):
                    logger.info(f"Ignoring business phone as customer phone: {extracted.get('phone')}")
                else:
                    if extracted.get("phone"): 
                        current_session.data["phone"] = extracted["phone"]
            
            # For name, always update if extracted
            if extracted.get("name"): 
                current_session.data["name"] = extracted["name"]
            
            if extracted.get("items"): 
                current_session.data["items"] = extracted["items"]
            
            if "delivery_charge" in extracted and isinstance(extracted["delivery_charge"], (int, float)):
                 current_session.data["delivery_charge"] = extracted["delivery_charge"]
                 # Send notification only if address was just now extracted/updated
                 if not had_address and extracted.get("address"):
                     send_message_async(token, sender, f"আপনার ঠিকানায় ডেলিভারি চার্জ ৳{extracted['delivery_charge']}")
            
            # Reset follow-up status when customer speaks
            try:
                supabase.table("order_sessions").update({"last_followup_sent": None}).eq("id", session_id).execute()
            except Exception:
                pass
            
            save_session_to_db(current_session)

        s_data = current_session.data
        has_all_info = all([s_data.get("name"), s_data.get("phone"), s_data.get("address"), s_data.get("items")])
        
        # SMART ORDER CONFIRMATION DETECTION
        is_confirmation, intent_type = detect_order_confirmation_intent(raw_text, s_data)
        
        # Handle cancellation
        text_lower = raw_text.lower()
        if "cancel" in text_lower or "বাতিল" in text_lower:
            delete_session_from_db(session_id)
            send_message_async(token, sender, "অর্ডার সেশনটি বাতিল করা হয়েছে।")
            save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": "অর্ডার সেশনটি বাতিল করা হয়েছে।"}])
            return
        
        # Handle confirmation intent
        if is_confirmation:
            if has_all_info:
                products_db = get_cached_products(user_id)
                final_delivery_charge = float(s_data.get("delivery_charge", 0))
                
                items_total = 0
                summary_list = []
                order_success = True
                insufficient_stock_products = []
                out_of_stock_products = []
                
                # FIRST: Check stock for ALL items BEFORE processing
                for item in s_data.get('items', []):
                    product_name = item.get('product_name')
                    qty = int(item.get('quantity', 1))
                    
                    if not product_name:
                        order_success = False
                        continue
                    
                    matched_product = find_best_product_match(product_name, products_db)
                    
                    if matched_product:
                        current_stock = matched_product.get('quantity', 0)
                        in_stock_status = matched_product.get('in_stock', True)
                        
                        # Check both conditions
                        if not in_stock_status:
                            order_success = False
                            out_of_stock_products.append(f"{matched_product['name']} (স্টক নেই)")
                        elif current_stock < qty:
                            order_success = False
                            insufficient_stock_products.append(f"{matched_product['name']} (স্টক: {current_stock}, চাহিদা: {qty})")
                    else:
                        order_success = False
                        send_message_async(token, sender, f"❌ দুঃখিত, '{product_name}' পণ্যটি সনাক্ত করা যায়নি।")
                        save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": f"❌ দুঃখিত, '{product_name}' পণ্যটি সনাক্ত করা যায়নি।"}])
                
                # Send appropriate error messages
                if out_of_stock_products:
                    stock_msg = "❌ নিম্নলিখিত পণ্যগুলোর স্টক নেই:\n" + "\n".join(out_of_stock_products)
                    send_message_async(token, sender, stock_msg)
                    save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": stock_msg}])
                    return
                
                if insufficient_stock_products:
                    stock_msg = "❌ নিম্নলিখিত পণ্যগুলোর পর্যাপ্ত স্টক নেই:\n" + "\n".join(insufficient_stock_products)
                    send_message_async(token, sender, stock_msg)
                    save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": stock_msg}])
                    return
                
                # If stock check passed, calculate total and process order
                if order_success:
                    for item in s_data.get('items', []):
                        product_name = item.get('product_name')
                        qty = int(item.get('quantity', 1))
                        
                        matched_product = find_best_product_match(product_name, products_db)
                        if matched_product:
                            items_total += matched_product['price'] * qty
                            summary_list.append(f"{matched_product['name']} x{qty}")
                            current_session.data['product'] = matched_product['name']
                    
                    if items_total > 0:
                        # Update product quantities
                        all_quantity_updates_successful = True
                        failed_products = []
                        
                        for item in s_data.get('items', []):
                            product_name = item.get('product_name')
                            qty = int(item.get('quantity', 1))
                            if product_name:
                                quantity_updated = update_product_quantity(user_id, product_name, qty)
                                if not quantity_updated:
                                    failed_products.append(product_name)
                                    all_quantity_updates_successful = False
                        
                        if not all_quantity_updates_successful:
                            error_msg = f"❌ দুঃখিত, নিম্নলিখিত পণ্যগুলোর স্টক আপডেট করতে সমস্যা হয়েছে: {', '.join(failed_products)}"
                            send_message_async(token, sender, error_msg)
                            save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": error_msg}])
                            return
                        
                        # Save order to database
                        if current_session.save_order(product_total=items_total, delivery_charge=final_delivery_charge):
                            confirm_msg = (
                                f"✅ আপনার অর্ডারটি গ্রহণ করা হয়েছে,\n\n"
                                f"অর্ডার সামারি:\n{', '.join(summary_list)}\n"
                                f"মোট: ৳{items_total + final_delivery_charge} (ডেলিভারি চার্জ: ৳{final_delivery_charge})\n\n"
                                f"আমরা খুব শীঘ্রই আপনার সাথে যোগাযোগ করবো। ধন্যবাদ। ❤️"
                            )
                            send_message_async(token, sender, confirm_msg)
                            save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": confirm_msg}])
                            delete_session_from_db(session_id)
                        else:
                            logger.error(f"Order Save Failed for customer {sender}")
                            error_msg = "❌ দুঃখিত, অর্ডার সেভ করতে সমস্যা হয়েছে। দয়া করে আবার চেষ্টা করুন।"
                            send_message_async(token, sender, error_msg)
                            save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": error_msg}])
                    else:
                        error_msg = "❌ দুঃখিত, কোনো পণ্য সনাক্ত করা যায়নি।"
                        send_message_async(token, sender, error_msg)
                        save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": error_msg}])
                
                return
            else:
                # Customer said confirm but info is not complete
                missing = []
                if not s_data.get("name"): missing.append("নাম")
                if not s_data.get("phone"): missing.append("ফোন নম্বর")
                if not s_data.get("address"): missing.append("ঠিকানা")
                if not s_data.get("items"): missing.append("পণ্য")
                needed_info = " ও ".join(missing)
                response_msg = f"দুঃখিত, আপনার {needed_info} এখনো পাওয়া যায়নি। অর্ডার নিশ্চিত করতে এই তথ্যগুলো দিন।"
                send_message_async(token, sender, response_msg)
                save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": response_msg}])
                return
        
        # Handle delay intent
        elif intent_type == 'delay':
            delay_msg = "বেশ তো, কোনো সমস্যা নেই। যখনই ঠিক করবেন আমাকে জানাবেন। আমি অপেক্ষায় থাকব। 😊"
            send_message_async(token, sender, delay_msg)
            save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": delay_msg}])
            return
        
        # Handle denial intent
        elif intent_type == 'deny':
            deny_msg = "ঠিক আছে, কোনো সমস্যা নেই। যখনই প্রয়োজন হবে, আমরা আছি। ধন্যবাদ! 😊"
            send_message_async(token, sender, deny_msg)
            save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": deny_msg}])
            delete_session_from_db(session_id)
            return

        # If not a confirmation/delay/denial intent, proceed with AI reply
        if bot_settings.get("hybrid_mode", True):
            reply, product_image = generate_ai_reply_with_retry(user_id, sender, raw_text, current_session.data)
            if reply:
                if product_image:
                    send_image_async(token, sender, product_image)
                send_message_async(token, sender, reply)

        elif bot_settings.get("faq_only_mode", False):
            faqs = get_cached_faqs(user_id)
            faq_reply = None
            for f in faqs:
                if f['question'] and f['question'].lower() in text_lower:
                    faq_reply = f['answer']
                    break
            if faq_reply:
                send_message_async(token, sender, faq_reply)
                save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": faq_reply}])
                
    except Exception as e:
        logger.error(f"Error processing message: {e}")

# ================= OPTIMIZED WEBHOOK =================
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        verify_token = os.getenv("VERIFY_TOKEN")
        
        if mode == "subscribe" and token == verify_token:
            logger.info("Webhook verified successfully!")
            return challenge, 200
        return "Verification failed", 403

    data = request.get_json()
    if not data: return jsonify({"status": "error"}), 400

    if data.get("object") == "page":
        # Clean old processed messages
        processed_messages.clear_old()
        
        for entry in data.get("entry", []):
            page_id = entry.get("id")
            page = get_page_client(page_id)
            if not page: continue
            user_id, token = page["user_id"], page["page_access_token"]

            for msg_event in entry.get("messaging", []):
                sender = msg_event["sender"]["id"]
                if "message" not in msg_event: continue
                if "text" not in msg_event["message"]: continue
                
                msg_id = msg_event["message"].get("mid")
                if not msg_id: continue
                if processed_messages.get(msg_id): continue
                processed_messages.set(msg_id, True)

                # Submit for async processing
                thread_pool.submit(process_message_batch, msg_event, page)

    return jsonify({"ok": True}), 200

# ================= FOLLOW-UP SYSTEM =================
@app.route("/send-followup", methods=["POST"])
def send_followup():
    """Background task to send follow-up messages to inactive customers"""
    try:
        # 1. Find sessions that haven't been updated for 1 hour AND no follow-up sent yet
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        
        res = supabase.table("order_sessions")\
            .select("*")\
            .lt("last_updated", one_hour_ago)\
            .is_("last_followup_sent", "null")\
            .execute()
        
        if not res.data:
            return jsonify({"status": "no_sessions_found"}), 200
        
        for session in res.data:
            user_id = session['user_id']
            customer_id = session['customer_id']
            page_id = session.get('page_id')
            
            # Skip if subscription is not active
            if not check_subscription_status(user_id):
                continue
                
            page = get_page_client(page_id) if page_id else None
            if page:
                token = page["page_access_token"]
                
                # Check current data status to customize message
                s_data = session.get('data', {})
                if not s_data.get('name') or not s_data.get('address'):
                    msg = "আপনি কি আমাদের পণ্যটি নিয়ে এখনো ভাবছেন? আপনার নাম ও ঠিকানা দিলে আমি অর্ডারটি রেডি করে দিতে পারতাম। 😊"
                else:
                    msg = "আপনি আপনার সব তথ্য দিয়েছেন, অর্ডারটি কি আমি কনফার্ম করে দেব? কনফার্ম করতে শুধু 'Confirm' লিখুন।"
                
                send_message_async(token, customer_id, msg)
                
                # Update DB to mark follow-up as sent
                supabase.table("order_sessions").update({"last_followup_sent": True}).eq("id", session['id']).execute()
                
        return jsonify({"status": "success", "processed": len(res.data)}), 200
    except Exception as e:
        logger.error(f"Follow-up execution error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ================= HEALTH CHECK =================
@app.route("/health", methods=["GET"])
def health_check():
    """Lightweight health check endpoint"""
    try:
        # Check database connection
        supabase.table("subscriptions").select("count", count="exact").limit(1).execute()
        return jsonify({
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cache_size": len(processed_messages.cache)
        }), 200
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({"status": "unhealthy", "error": str(e)}), 500

# ================= CLEANUP =================
@app.teardown_appcontext
def cleanup(exception=None):
    """Cleanup resources"""
    thread_pool.shutdown(wait=False)
    get_cached_subscription_status.cache_clear()
    get_cached_bot_settings.cache_clear()
    get_cached_products.cache_clear()
    get_cached_faqs.cache_clear()
    get_cached_business_settings.cache_clear()

# ================= MAIN =================
if __name__ == "__main__":
    # Production settings
    port = int(os.getenv("PORT", 10000))
    
    # For production, use production WSGI server
    if os.getenv("ENVIRONMENT") == "production":
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=50)
    else:
        app.run(host="0.0.0.0", port=port, debug=False)

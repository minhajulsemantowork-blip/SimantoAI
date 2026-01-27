import os
import re
import logging
import requests
import json
import time
import threading
from typing import Optional, Dict, Tuple, List, Any
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
from openai import OpenAI
from supabase import create_client, Client

# ================= CONFIG & CACHING =================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)

# --- Smart Caching Variables (NEW) ---
bot_data_cache = {}        # { "user_id_key": (data, timestamp) }
api_key_status = {}        # { "api_key": blocked_until_timestamp }
CACHE_EXPIRY = 600         # ১০ মিনিট (ডাটা রিফ্রেশ টাইম)
KEY_BLOCK_DURATION = 300   # ৫ মিনিট (API Key ব্লক থাকার সময়)

processed_messages = {}
user_queues = {}  
user_timers = {}

# Supabase Client Setup
try:
    supabase: Client = create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_SERVICE_KEY")
    )
except Exception as e:
    logger.error(f"Supabase connection failed: {e}")

# ================= SMART CACHING HELPERS (NEW) =================
def get_cached_data(user_id: str, suffix: str, fetch_func):
    """
    Retrieves data from cache or fetches fresh from DB if expired.
    """
    now = time.time()
    cache_key = f"{user_id}_{suffix}"
    
    if cache_key in bot_data_cache:
        data, timestamp = bot_data_cache[cache_key]
        if now - timestamp < CACHE_EXPIRY:
            return data
            
    # Fetch fresh data
    try:
        fresh_data = fetch_func()
        bot_data_cache[cache_key] = (fresh_data, now)
        logger.info(f"Cache updated for: {cache_key}")
        return fresh_data
    except Exception as e:
        logger.error(f"Error fetching data for {cache_key}: {e}")
        # If fetch fails, try to return old cache if exists
        if cache_key in bot_data_cache:
            return bot_data_cache[cache_key][0]
        return None

def block_api_key(api_key: str):
    """Blocks an API key for a specific duration due to rate limits."""
    logger.warning(f"Rate limit hit! Blocking key for {KEY_BLOCK_DURATION} seconds.")
    api_key_status[api_key] = time.time() + KEY_BLOCK_DURATION

# ================= SUBSCRIPTION CHECKER =================
def check_subscription_status(user_id: str) -> bool:
    try:
        # Subscription status is critical, usually not cached or cached for very short time
        # Here we keep it real-time as per original logic to avoid access issues
        res = supabase.table("subscriptions").select("status, trial_end, end_date, paid_until").eq("user_id", user_id).execute()
        
        if res.data and len(res.data) > 0:
            sub = res.data[0]
            status = sub.get("status")
            
            if status not in ["active", "trial"]:
                return False

            expiry_str = sub.get("paid_until") or sub.get("end_date") or sub.get("trial_end")
            
            if expiry_str:
                now = datetime.now(timezone.utc)
                try:
                    clean_expiry = expiry_str.strip().replace(' ', 'T')
                    if clean_expiry.endswith('+00'):
                        clean_expiry = clean_expiry.replace('+00', '+00:00')
                    
                    # Parsing logic same as before
                    try:
                        expiry_date = datetime.fromisoformat(clean_expiry)
                    except ValueError:
                        clean_date_str = expiry_str.strip()
                        if clean_date_str.endswith('+00'):
                            clean_date_str = clean_date_str.replace('+00', '+0000')
                        try:
                            expiry_date = datetime.strptime(clean_date_str, "%Y-%m-%d %H:%M:%S.%f%z")
                        except ValueError:
                            clean_date_no_tz = expiry_str.split('+')[0].strip()
                            try:
                                expiry_date = datetime.strptime(clean_date_no_tz, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
                            except ValueError:
                                expiry_date = datetime.strptime(clean_date_no_tz, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                
                except Exception as e:
                    logger.error(f"Date Parsing Error: {e}")
                    return False

                if now > expiry_date:
                    supabase.table("subscriptions").update({"status": "expired"}).eq("user_id", user_id).execute()
                    return False
            
            return True
        return False
    except Exception as e:
        logger.error(f"Subscription Check Error for user {user_id}: {e}")
        return False

# ================= DATA FETCHERS (UPDATED WITH CACHE) =================
def get_bot_settings(user_id: str) -> Dict:
    def fetch():
        res = supabase.table("bot_settings").select("*").eq("user_id", user_id).limit(1).execute()
        if res.data:
            return res.data[0]
        return {
            "ai_reply_enabled": True, "hybrid_mode": True, "faq_only_mode": False,
            "typing_delay": 0, "welcome_message": ""
        }
    return get_cached_data(user_id, "bot_settings", fetch) or {}

def get_business_settings(user_id: str) -> Optional[Dict]:
    def fetch():
        res = supabase.table("business_settings").select("*").eq("user_id", user_id).limit(1).execute()
        return res.data[0] if res.data else {}
    return get_cached_data(user_id, "biz_settings", fetch)

def get_products_with_details(user_id: str, use_cache=True):
    """
    Fetches products.
    use_cache=True -> Returns cached data (Fast, for Chat)
    use_cache=False -> Returns fresh data (Slow, for Order Confirmation/Stock Check)
    """
    def fetch():
        res = supabase.table("products").select("*").eq("user_id", user_id).execute()
        return res.data or []
    
    if use_cache:
        return get_cached_data(user_id, "products", fetch) or []
    return fetch()

def get_faqs(user_id: str):
    def fetch():
        res = supabase.table("faqs").select("question, answer").eq("user_id", user_id).execute()
        return res.data or []
    return get_cached_data(user_id, "faqs", fetch) or []

def get_valid_api_keys(user_id: str):
    """Fetches API keys from DB (cached) and filters out blocked ones."""
    def fetch():
        res = supabase.table("api_keys").select("groq_api_key, groq_api_key_2, groq_api_key_3, groq_api_key_4, groq_api_key_5").eq("user_id", user_id).execute()
        if res.data:
            row = res.data[0]
            keys = [row.get('groq_api_key'), row.get('groq_api_key_2'), row.get('groq_api_key_3'), row.get('groq_api_key_4'), row.get('groq_api_key_5')]
            return [k for k in keys if k and k.strip()]
        return []
    
    all_keys = get_cached_data(user_id, "api_keys", fetch) or []
    
    # Filter out blocked keys
    now = time.time()
    valid_keys = [k for k in all_keys if api_key_status.get(k, 0) < now]
    return valid_keys

# ================= SESSION DB HELPERS =================
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

# ================= HELPERS (IMAGE & MSG) =================
def get_page_client(page_id):
    # This is lightweight, but could be cached if needed. Keeping real-time for safety.
    res = supabase.table("facebook_integrations").select("*").eq("page_id", str(page_id)).eq("is_connected", True).execute()
    return res.data[0] if res.data else None

def send_message(token, user_id, text):
    if not text: return
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={token}"
    try:
        requests.post(url, json={"recipient": {"id": user_id}, "message": {"text": text}})
    except Exception as e:
        logger.error(f"Failed to send message: {e}")

def send_image(token, user_id, image_url):
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
    try:
        requests.post(url, json=payload)
    except Exception as e:
        logger.error(f"Failed to send image: {e}")

def send_sender_action(token, user_id, action):
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={token}"
    payload = {
        "recipient": {"id": user_id},
        "sender_action": action
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        logger.error(f"Failed to send sender action {action}: {e}")

def get_chat_memory(user_id: str, customer_id: str, limit: int = 10) -> List[Dict]:
    res = supabase.table("chat_history").select("messages").eq("user_id", user_id).eq("customer_id", customer_id).limit(1).execute()
    return res.data[0].get("messages", [])[-limit:] if res.data else []

def save_chat_memory(user_id: str, customer_id: str, messages: List[Dict]):
    now = datetime.now(timezone.utc).isoformat()
    existing = supabase.table("chat_history").select("id").eq("user_id", user_id).eq("customer_id", customer_id).execute()
    if existing.data:
        supabase.table("chat_history").update({"messages": messages, "last_updated": now}).eq("id", existing.data[0]["id"]).execute()
    else:
        supabase.table("chat_history").insert({"user_id": user_id, "customer_id": customer_id, "messages": messages, "created_at": now, "last_updated": now}).execute()

# ================= PRODUCT STOCK UPDATER =================
def update_product_stock(user_id: str, product_name: str, quantity_sold: int) -> bool:
    """Update product stock in database after order confirmation (Always Real-time)"""
    try:
        logger.info(f"Updating stock for product '{product_name}' for user {user_id}, quantity: {quantity_sold}")
        
        # ALWAYS fetch fresh data here (Bypass Cache)
        res = supabase.table("products").select("id, stock, name, in_stock").eq("user_id", user_id).execute()
        
        if not res.data:
            return False
        
        matched_product = find_best_product_match(product_name, res.data)
        
        if not matched_product:
            return False
        
        product_id = matched_product["id"]
        current_stock = matched_product.get("stock", 0)
        in_stock = matched_product.get("in_stock", True)
        
        if not in_stock or current_stock < quantity_sold:
            return False
        
        new_stock = max(0, current_stock - quantity_sold)
        update_data = {"stock": new_stock}
        if new_stock == 0:
            update_data["in_stock"] = False
        
        update_res = supabase.table("products").update(update_data).eq("id", product_id).execute()
        return bool(update_res.data)
            
    except Exception as e:
        logger.error(f"Error updating product stock: {str(e)}", exc_info=True)
    return False

# ================= AI LOGIC (UPDATED WITH SMART API & CACHE) =================
def generate_ai_reply_with_retry(user_id, customer_id, user_msg, current_session_data):
    # Fetch cached data
    business = get_business_settings(user_id)
    products = get_products_with_details(user_id, use_cache=True)
    faqs = get_faqs(user_id)
    
    biz_phone = business.get('contact_number', '') if business else ""
    business_name = business.get('name', 'আমাদের শপ') if business else "আমাদের শপ"
    business_address = business.get('address', 'ঠিকানা উপলব্ধ নয়') if business else "ঠিকানা উপলব্ধ নয়"
    
    opening_hours = business.get('opening_hours', 'তথ্য নেই') if business else "তথ্য নেই"
    delivery_info = business.get('delivery_info', 'তথ্য নেই') if business else "তথ্য নেই"
    payment_methods = business.get('payment_methods', []) if business else []

    categories = sorted(list(set([p.get('category') for p in products if p.get('category')])))
    category_list_str = ", ".join(categories) if categories else "তথ্য নেই"

    product_list_with_stock = []
    for p in products:
        stock = p.get("stock", 0)
        in_stock_status = p.get("in_stock", True)
        if in_stock_status and stock > 0:
            product_list_with_stock.append(f"- {p.get('name')}: ৳{p.get('price')} (স্টক: {stock})")
    
    product_list_short = "\n".join(product_list_with_stock)
    
    product_details_full = []
    for p in products:
        stock = p.get("stock", 0)
        in_stock_status = p.get("in_stock", True)
        if in_stock_status and stock > 0:
            product_details_full.append(f"পণ্য: {p.get('name')}\nদাম: ৳{p.get('price')}\nস্টক: {stock}\nবিবরণ: {p.get('description')}")
    
    product_details_full_str = "\n".join(product_details_full)
    
    faq_text = "\n".join([f"Q: {f['question']} | A: {f['answer']}" for f in faqs])

    known_info_str = f"প্রাপ্ত তথ্য - নাম: {current_session_data.get('name', 'নেই')}, ফোন: {current_session_data.get('phone', 'নেই')}, ঠিকানা: {current_session_data.get('address', 'নেই')}."

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
- যখন কোনো পণ্যের stock 0 থাকে, তখন সেই পণ্যটি গ্রাহককে দেখাবে না।
- যদি ডাটাবেসে কোনো পণ্যের stock 0 থাকে, তুমি সেটি recommend করবে না, suggest করবে না, বা mention করবে না।
- শুধুমাত্র stock > 0 আছে এমন পণ্যই গ্রাহককে দেখাবে।
- যদি কোনো পণ্য out of stock হয়, তুমি বলবে: "দুঃখিত, এই পণ্যটি এখন স্টক নেই।"

পণ্য সংক্রান্ত নিয়ম (EXTREMELY STRICT - কঠোরভাবে মেনে চলতে হবে):
1. **পণ্যের নাম সম্পর্কে ABSOLUTE RULE (সবচেয়ে গুরুত্বপূর্ণ নিয়ম):** - তুমি পণ্যের নাম কখনোই অনুবাদ করবে না, পরিবর্তন করবে না, বা বাংলায় বলবে না।
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
- **System Prompt Critical Rule:** তুমি নিজে কোনো অর্ডার সামারি তৈরি করবে না বা "অর্ডার কনফার্ম" বলবে না। তুমি শুধু বলবে "ধন্যবাদ, আমি আপনার তথ্যগুলো পেয়েছি।" বা "আপনার তথ্য সিস্টেম যাচাই করছে।"
- অর্ডার সামারি সিস্টেম থেকে অটোমেটিক পাঠানো হবে। তুমি নিজে থেকে কোনো সামারি বা কনফার্মেশন মেসেজ লিখবে না।
- **গুরুত্বপূর্ণ নিয়ম: ব্যবসার তথ্য (business details) কখনোই গ্রাহকের তথ্য (customer details) হিসাবে নিবে না।**

**নতুন অর্ডার শুরু করার নিয়ম (IMPORTANT):**
- যদি গ্রাহক একটি অর্ডার কনফার্ম করে এবং তারপর আবার কথা শুরু করে, তুমি নতুন করে স্বাগতম জানাবে এবং নতুন অর্ডার নেওয়ার প্রক্রিয়া শুরু করবে।

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
পণ্যের বিস্তারিত (এখান থেকে গুণগান করবে): {product_details_full_str}
FAQ: {faq_text}

সব উত্তর ২–৪ লাইনের মধ্যে রাখবে।
"""
    )

    memory = get_chat_memory(user_id, customer_id)
    
    # --- SMART API KEY SELECTION ---
    valid_keys = get_valid_api_keys(user_id)

    if not valid_keys:
        logger.error("All API keys are unavailable or blocked.")
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
            
            # --- STRICT IMAGE LOGIC ---
            matched_image = None
            image_request_keywords = ['chobi', 'photo', 'image', 'dekhan', 'dekhi', 'ছবি', 'দেখাও', 'দেখি', 'pic']
            wants_to_see_image = any(word in user_msg.lower() for word in image_request_keywords)
            already_sent_image = any("image_url" in str(m) or "attachment" in str(m) for m in memory)
            mentioned_products = [p for p in products if p.get('name') and p.get('name').lower() in reply.lower()]

            if len(mentioned_products) == 1:
                product = mentioned_products[0]
                if wants_to_see_image or not already_sent_image:
                    matched_image = product.get('image_url')
            
            return reply, matched_image

        except Exception as e:
            error_msg = str(e).lower()
            # If Rate Limit Error, Block the Key
            if "rate_limit" in error_msg or "429" in error_msg:
                block_api_key(key)
                continue # Try next key
            
            logger.error(f"AI Generation Error: {e}")
            continue 
    
    return None, None

# ================= ORDER EXTRACTION (UPDATED) =================
def extract_order_data_with_retry(user_id, messages, delivery_policy_text, max_retries=2):
    valid_keys = get_valid_api_keys(user_id)
    if not valid_keys: return None

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
            
            if 'delivery_charge' in extracted_json:
                try:
                    val = extracted_json['delivery_charge']
                    if val is None or str(val).lower() == 'null':
                        extracted_json['delivery_charge'] = 0.0
                    else:
                        extracted_json['delivery_charge'] = float(val)
                except (TypeError, ValueError):
                    extracted_json['delivery_charge'] = 0.0
                    
            return extracted_json
        except Exception as e:
            error_msg = str(e).lower()
            if "rate_limit" in error_msg or "429" in error_msg:
                block_api_key(key)
                continue
            logger.error(f"Extraction Error: {e}")
            continue
    return None

# ================= IMPROVED PRODUCT MATCHING =================
def find_best_product_match(product_name: str, products_db: List[Dict]) -> Optional[Dict]:
    if not product_name or not products_db: return None
    product_name_lower = product_name.lower().strip()
    
    # 1. Exact match
    for product in products_db:
        if product.get('name') and product['name'].lower() == product_name_lower:
            return product
    
    # 2. Word boundary match
    for product in products_db:
        db_name = product.get('name', '').lower()
        if db_name:
            pattern = r'\b' + re.escape(product_name_lower) + r'\b'
            if re.search(pattern, db_name): return product
    
    # 3. Inverse word match
    for product in products_db:
        db_name = product.get('name', '').lower()
        if db_name:
            pattern = r'\b' + re.escape(db_name) + r'\b'
            if re.search(pattern, product_name_lower): return product
    
    # 4. Substring match
    for product in products_db:
        db_name = product.get('name', '').lower()
        if db_name and (product_name_lower in db_name or db_name in product_name_lower):
            return product
    return None

# ================= SMART ORDER CONFIRMATION DETECTION =================
def detect_order_confirmation_intent(text: str, session_data: Dict) -> Tuple[bool, str]:
    text_lower = text.lower().strip()
    
    confirm_patterns = [r'confirm', r'কনফার্ম', r'ঠিক আছে', r'ok', r'okay', r'hae', r'ji', r'হ্যা', r'জি', r'yes', r'done', r'agreed', r'নিশ্চিত', r'পাঠান', r'send', r'\+1', r'\👍', r'\✅']
    delay_patterns = [r'(পরে|পর্য|later|আগে|after|wait|hold on|দেরি)', r'(আরেকটু.*পর্য|wait.*bit)', r'(think.*করব|think.*করি|ভেবে.*দেখি)', r'(not.*now|now.*not|এখন.*না)', r'(কিছুক্ষন.*পর্য|few.*minutes)']
    deny_patterns = [r'^(no|না|নাহ|না ধন্যবাদ|no thanks|not now)$', r'^(cancel|বাতিল|stop|স্টপ)$', r'^(don\'t.*want|চাইনা|চাই না)$', r'^(maybe.*later|maybe.*পর্য)']
    
    for pattern in confirm_patterns:
        if re.search(pattern, text_lower, re.IGNORECASE): return True, 'confirm'
    for pattern in delay_patterns:
        if re.search(pattern, text_lower, re.IGNORECASE): return False, 'delay'
    for pattern in deny_patterns:
        if re.search(pattern, text_lower, re.IGNORECASE): return False, 'deny'
    
    return False, 'neutral'

# ================= ORDER SUMMARY DISPLAY =================
def show_order_summary(token, customer_id, session_data, business_name):
    items = session_data.get('items', [])
    delivery_charge = session_data.get('delivery_charge', 0)
    
    # Cached products are fine for summary display (price mostly static)
    user_id = session_data.get('user_id_from_session', '')
    products_db = get_products_with_details(user_id, use_cache=True) if user_id else []
    
    summary_lines = []
    items_total = 0
    
    for item in items:
        product_name = item.get('product_name', '')
        quantity = item.get('quantity', 1)
        product = find_best_product_match(product_name, products_db)
        if product:
            price = product.get('price', 0)
            subtotal = price * quantity
            items_total += subtotal
            summary_lines.append(f"• {product_name} x{quantity} = ৳{subtotal}")
        else:
            summary_lines.append(f"• {product_name} x{quantity}")
    
    total_amount = items_total + delivery_charge
    
    summary_message = (
        f"📋 অর্ডার সামারি 📋\n\n"
        f"পণ্য:\n" + "\n".join(summary_lines) + f"\n\n"
        f"পণ্যের মূল্য: ৳{items_total}\n"
        f"ডেলিভারি চার্জ: ৳{delivery_charge}\n"
        f"মোট টাকা: ৳{total_amount}\n\n"
        f"গ্রাহক তথ্য:\n"
        f"• নাম: {session_data.get('name', 'নেই')}\n"
        f"• ফোন: {session_data.get('phone', 'নেই')}\n"
        f"• ঠিকানা: {session_data.get('address', 'নেই')}\n\n"
        f"অর্ডারটি কনফার্ম করতে 'Confirm' লিখুন।\n"
        f"কোনো পরিবর্তন করতে চাইলে বলুন।"
    )
    send_message(token, customer_id, summary_message)
    return summary_message

# ================= FOLLOW-UP SYSTEM =================
@app.route("/send-followup", methods=["POST"])
def send_followup():
    try:
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        res = supabase.table("order_sessions").select("*").lt("last_updated", one_hour_ago).is_("last_followup_sent", "null").execute()
        
        if not res.data:
            return jsonify({"status": "no_sessions_found"}), 200
        
        for session in res.data:
            user_id = session['user_id']
            customer_id = session['customer_id']
            page_id = session.get('page_id')
            
            if not check_subscription_status(user_id): continue
                
            page = get_page_client(page_id) if page_id else None
            if page:
                token = page["page_access_token"]
                s_data = session.get('data', {})
                if not s_data.get('name') or not s_data.get('address'):
                    msg = "আপনি কি আমাদের পণ্যটি নিয়ে এখনো ভাবছেন? আপনার নাম ও ঠিকানা দিলে আমি অর্ডারটি রেডি করে দিতে পারতাম। 😊"
                else:
                    msg = "আপনি আপনার সব তথ্য দিয়েছেন, অর্ডারটি কি আমি কনফার্ম করে দেব? কনফার্ম করতে শুধু 'Confirm' লিখুন।"
                
                send_message(token, customer_id, msg)
                supabase.table("order_sessions").update({"last_followup_sent": True}).eq("id", session['id']).execute()
                
        return jsonify({"status": "success", "processed": len(res.data)}), 200
    except Exception as e:
        logger.error(f"Follow-up execution error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ================= BATCHED MESSAGE PROCESSOR =================
def process_batched_messages(sender, user_id, page_id, token):
    try:
        if sender not in user_queues or not user_queues[sender]: return
        
        raw_text_list = user_queues[sender]
        raw_text = " ".join(raw_text_list)
        text = raw_text.lower().strip()
        
        # --- ADMIN COMMAND TO CLEAR CACHE ---
        if text == "!refresh":
            bot_data_cache.clear()
            send_message(token, sender, "✅ System cache cleared. Fetched fresh data.")
            user_queues[sender] = []
            return

        user_queues[sender] = []
        if sender in user_timers: del user_timers[sender]

        send_sender_action(token, sender, "typing_on")

        if not check_subscription_status(user_id): return

        bot_settings = get_bot_settings(user_id)
        if not bot_settings.get("ai_reply_enabled", True): return
        
        delay_ms = bot_settings.get("typing_delay", 0)
        if delay_ms > 0: time.sleep(delay_ms / 1000)

        memory = get_chat_memory(user_id, sender)
        welcome_msg = bot_settings.get("welcome_message")
        
        session_id = f"order_{user_id}_{sender}"
        current_session = get_session_from_db(session_id)
        
        if not current_session:
            if welcome_msg and not memory:
                send_message(token, sender, welcome_msg)
                save_chat_memory(user_id, sender, [{"role": "assistant", "content": welcome_msg}])
            current_session = OrderSession(user_id, sender)

        try:
            supabase.table("order_sessions").update({"page_id": page_id}).eq("id", session_id).execute()
        except: pass

        temp_memory = memory + [{"role": "user", "content": raw_text}]
        business = get_business_settings(user_id)
        delivery_policy = business.get('delivery_info', "তথ্য নেই") if business else "তথ্য নেই"
        
        extracted = extract_order_data_with_retry(user_id, temp_memory, delivery_policy)
        
        if extracted:
            had_address = bool(current_session.data.get("address"))
            data_changed = False
            
            business_address = business.get('address', '') if business else ''
            business_phone = business.get('contact_number', '') if business else ''
            
            if extracted.get("address") and business_address and business_address.lower() not in extracted.get("address", "").lower():
                if extracted.get("address") != current_session.data.get("address"): 
                    current_session.data["address"] = extracted["address"]
                    data_changed = True
            elif extracted.get("address") and not business_address:
                if extracted.get("address") != current_session.data.get("address"): 
                    current_session.data["address"] = extracted["address"]
                    data_changed = True
            
            if extracted.get("phone") and business_phone and business_phone not in extracted.get("phone", ""):
                 if extracted.get("phone") != current_session.data.get("phone"): 
                    current_session.data["phone"] = extracted["phone"]
                    data_changed = True
            elif extracted.get("phone") and not business_phone:
                if extracted.get("phone") != current_session.data.get("phone"): 
                    current_session.data["phone"] = extracted["phone"]
                    data_changed = True
            
            if extracted.get("name") and extracted.get("name") != current_session.data.get("name"): 
                current_session.data["name"] = extracted["name"]
                data_changed = True
            
            if extracted.get("items") and extracted.get("items") != current_session.data.get("items"): 
                current_session.data["items"] = extracted["items"]
                data_changed = True
            
            if "delivery_charge" in extracted and isinstance(extracted["delivery_charge"], (int, float)):
                    current_session.data["delivery_charge"] = extracted["delivery_charge"]
                    if not had_address and extracted.get("address"):
                        send_message(token, sender, f"আপনার ঠিকানায় ডেলিভারি চার্জ ৳{extracted['delivery_charge']}")
            
            try:
                supabase.table("order_sessions").update({"last_followup_sent": None}).eq("id", session_id).execute()
            except: pass
            
            is_confirming_now = any(w in text for w in ['confirm', 'ok', 'tik', 'done', 'yes', 'humm', 'ji', 'hae'])
            
            if data_changed and not is_confirming_now:
                    current_session.data["summary_shown"] = False
            
            save_session_to_db(current_session)

        s_data = current_session.data
        has_all_info = all([s_data.get("name"), s_data.get("phone"), s_data.get("address"), s_data.get("items")])
        is_confirmation, intent_type = detect_order_confirmation_intent(raw_text, s_data)

        if "cancel" in text or "বাতিল" in text:
            delete_session_from_db(session_id)
            send_message(token, sender, "অর্ডার সেশনটি বাতিল করা হয়েছে। নতুন অর্ডার দিতে চাইলে বলুন।")
            save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": "অর্ডার সেশনটি বাতিল করা হয়েছে।"}])
            return

        # --- ORDER CONFIRMATION LOGIC ---
        if is_confirmation:
            if has_all_info:
                # IMPORTANT: Fetch FRESH products (bypass cache) for stock check
                products_db = get_products_with_details(user_id, use_cache=False)
                
                final_delivery_charge = float(s_data.get('delivery_charge', 0))
                items_total = 0
                summary_list = []
                order_success = True
                insufficient_stock_products = []
                out_of_stock_products = []
                
                for item in s_data.get('items', []):
                    product_name = item.get('product_name')
                    qty = int(item.get('quantity', 1))
                    
                    if not product_name:
                        order_success = False
                        continue
                    
                    matched_product = find_best_product_match(product_name, products_db)
                    
                    if matched_product:
                        current_stock = matched_product.get('stock', 0)
                        in_stock_status = matched_product.get('in_stock', True)
                        
                        if not in_stock_status:
                            order_success = False
                            out_of_stock_products.append(f"{matched_product['name']} (স্টক নেই)")
                        elif current_stock < qty:
                            order_success = False
                            insufficient_stock_products.append(f"{matched_product['name']} (স্টক: {current_stock}, চাহিদা: {qty})")
                    else:
                        order_success = False
                        send_message(token, sender, f"❌ দুঃখিত, '{product_name}' পণ্যটি সনাক্ত করা যায়নি।")
                
                if out_of_stock_products:
                    send_message(token, sender, "❌ নিম্নলিখিত পণ্যগুলোর স্টক নেই:\n" + "\n".join(out_of_stock_products))
                    return
                if insufficient_stock_products:
                    send_message(token, sender, "❌ নিম্নলিখিত পণ্যগুলোর পর্যাপ্ত স্টক নেই:\n" + "\n".join(insufficient_stock_products))
                    return
                
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
                        all_stock_updates_successful = True
                        failed_products = []
                        
                        for item in s_data.get('items', []):
                            product_name = item.get('product_name')
                            qty = int(item.get('quantity', 1))
                            if product_name:
                                if not update_product_stock(user_id, product_name, qty):
                                    failed_products.append(product_name)
                                    all_stock_updates_successful = False
                        
                        if not all_stock_updates_successful:
                            send_message(token, sender, f"❌ দুঃখিত, স্টক আপডেট সমস্যা: {', '.join(failed_products)}")
                            return
                        
                        if current_session.save_order(product_total=items_total, delivery_charge=final_delivery_charge):
                            confirm_msg = (
                                f"✅ আপনার অর্ডারটি সফলভাবে কনফার্ম হয়েছে!\n\n"
                                f"অর্ডার সামারি:\n{', '.join(summary_list)}\n"
                                f"মোট: ৳{items_total + final_delivery_charge} (ডেলিভারি চার্জ সহ)\n\n"
                                f"আমরা খুব শীঘ্রই আপনার সাথে যোগাযোগ করবো। ধন্যবাদ! ❤️"
                            )
                            send_message(token, sender, confirm_msg)
                            save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": confirm_msg}])
                            try:
                                supabase.table("chat_history").delete().eq("user_id", user_id).eq("customer_id", sender).execute()
                            except: pass
                            delete_session_from_db(session_id)
                            current_session = None
                            return
                        else:
                            send_message(token, sender, "❌ দুঃখিত, অর্ডার সেভ করতে সমস্যা হয়েছে।")
                            return
            else:
                missing = []
                if not s_data.get("name"): missing.append("নাম")
                if not s_data.get("phone"): missing.append("ফোন নম্বর")
                if not s_data.get("address"): missing.append("ঠিকানা")
                if not s_data.get("items"): missing.append("পণ্য")
                send_message(token, sender, f"দুঃখিত, আপনার {' ও '.join(missing)} এখনো পাওয়া যায়নি। অর্ডার নিশ্চিত করতে এই তথ্যগুলো দিন।")
                return

        elif intent_type == 'delay':
            send_message(token, sender, "বেশ তো, কোনো সমস্যা নেই। যখনই ঠিক করবেন আমাকে জানাবেন। 😊")
            return
        elif intent_type == 'deny':
            send_message(token, sender, "ঠিক আছে, কোনো সমস্যা নেই। ধন্যবাদ! 😊")
            delete_session_from_db(session_id)
            return

        if has_all_info and not s_data.get("summary_shown", False):
            business_name = business.get('name', 'আমাদের শপ') if business else "আমাদের শপ"
            s_data['user_id_from_session'] = user_id 
            summary_message = show_order_summary(token, sender, s_data, business_name)
            s_data["summary_shown"] = True
            current_session.data = s_data
            save_session_to_db(current_session)
            save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": summary_message}])
            return

        # ================= AI REPLY (HYBRID) =================
        if bot_settings.get("hybrid_mode", True):
            session_data_for_ai = current_session.data if current_session else {}
            reply, product_image = generate_ai_reply_with_retry(user_id, sender, raw_text, session_data_for_ai)
            
            if reply:
                if current_session and s_data.get("summary_shown", False):
                    current_session.data["summary_shown"] = False
                    save_session_to_db(current_session)
                
                if product_image:
                    send_image(token, sender, product_image)
                send_message(token, sender, reply)

        elif bot_settings.get("faq_only_mode", False):
            faqs = get_faqs(user_id)
            for f in faqs:
                if f['question'] and f['question'].lower() in text:
                    send_message(token, sender, f['answer'])
                    save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": f['answer']}])
                    break

    except Exception as e:
        logger.error(f"Error in batched processing: {e}", exc_info=True)

# ================= WEBHOOK =================
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    global processed_messages 
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
        now_ts = time.time()
        processed_messages = {k: v for k, v in processed_messages.items() if now_ts - v < 300}
        
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
                if msg_id in processed_messages: continue
                processed_messages[msg_id] = time.time()

                raw_text = msg_event["message"].get("text", "")
                if not raw_text: continue
                
                send_sender_action(token, sender, "mark_seen")

                if sender not in user_queues:
                    user_queues[sender] = []
                user_queues[sender].append(raw_text)

                if sender in user_timers:
                    user_timers[sender].cancel()

                t = threading.Timer(3.0, process_batched_messages, args=[sender, user_id, page_id, token])
                user_timers[sender] = t
                t.start()

    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

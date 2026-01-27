import os
import re
import logging
import requests
import json
import time
import threading  # <--- নতুন যুক্ত করা হয়েছে
from typing import Optional, Dict, Tuple, List, Any
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
from openai import OpenAI
from supabase import create_client, Client

# ================= CONFIG =================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)

processed_messages = {}

# --- নতুন: মেসেজ বাফারিং এর জন্য গ্লোবাল ভেরিয়েবল ---
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

# ================= SUBSCRIPTION CHECKER =================
def check_subscription_status(user_id: str) -> bool:
    try:
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

# ================= BOT SETTINGS FETCHER =================
def get_bot_settings(user_id: str) -> Dict:
    try:
        res = supabase.table("bot_settings").select("*").eq("user_id", user_id).limit(1).execute()
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

# ================= SENDER ACTION HELPER (NEW) =================
def send_sender_action(token, user_id, action):
    """
    Sends sender actions like 'mark_seen' or 'typing_on'
    """
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={token}"
    payload = {
        "recipient": {"id": user_id},
        "sender_action": action
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        logger.error(f"Failed to send sender action {action}: {e}")

# ================= DATA FETCHERS =================
def get_products_with_details(user_id: str):
    # Read stock from 'stock' column (not 'quantity')
    res = supabase.table("products").select("*").eq("user_id", user_id).execute()
    return res.data or []

def get_faqs(user_id: str):
    res = supabase.table("faqs").select("question, answer").eq("user_id", user_id).execute()
    return res.data or []

def get_business_settings(user_id: str) -> Optional[Dict]:
    res = supabase.table("business_settings").select("*").eq("user_id", user_id).limit(1).execute()
    return res.data[0] if res.data else None

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
    """Update product stock in database after order confirmation"""
    try:
        logger.info(f"Updating stock for product '{product_name}' for user {user_id}, quantity: {quantity_sold}")
        
        # First, get all products for the user
        res = supabase.table("products")\
            .select("id, stock, name, in_stock")\
            .eq("user_id", user_id)\
            .execute()
        
        if not res.data:
            logger.warning(f"No products found for user {user_id}")
            return False
        
        # Use the improved matching function
        matched_product = find_best_product_match(product_name, res.data)
        
        if not matched_product:
            logger.warning(f"Product '{product_name}' not found in database. Available products: {[p.get('name') for p in res.data]}")
            return False
        
        product_id = matched_product["id"]
        current_stock = matched_product.get("stock", 0)  # Use 'stock' column
        matched_product_name = matched_product.get("name", "")
        in_stock = matched_product.get("in_stock", True)
        
        logger.info(f"Found product: '{matched_product_name}', Current stock: {current_stock}, In stock: {in_stock}, Requested: {quantity_sold}")
        
        # CRITICAL FIX: Check both stock AND in_stock status
        if not in_stock:
            logger.error(f"Product '{matched_product_name}' is marked as out of stock (in_stock: {in_stock})")
            return False
        
        # Check if enough stock is available
        if current_stock < quantity_sold:
            logger.error(f"Insufficient stock for product '{matched_product_name}': {current_stock} available, {quantity_sold} requested")
            return False
        
        # Calculate new stock
        new_stock = max(0, current_stock - quantity_sold)
        
        # Update stock in database
        update_data = {"stock": new_stock}
        if new_stock == 0:
            update_data["in_stock"] = False
        
        # Update stock in database
        update_res = supabase.table("products")\
            .update(update_data)\
            .eq("id", product_id)\
            .execute()
        
        if update_res.data:
            logger.info(f"Successfully updated stock for product '{matched_product_name}' (ID: {product_id}): {current_stock} -> {new_stock}")
            return True
        else:
            logger.error(f"Failed to update stock for product ID {product_id}")
            return False
            
    except Exception as e:
        logger.error(f"Error updating product stock: {str(e)}", exc_info=True)
    return False

# ================= AI LOGIC =================
def generate_ai_reply_with_retry(user_id, customer_id, user_msg, current_session_data, max_retries=2):
    business = get_business_settings(user_id)
    products = get_products_with_details(user_id)
    faqs = get_faqs(user_id)
    
    biz_phone = business.get('contact_number', '') if business else ""
    business_name = business.get('name', 'আমাদের শপ') if business else "আমাদের শপ"
    business_address = business.get('address', 'ঠিকানা উপলব্ধ নয়') if business else "ঠিকানা উপলব্ধ নয়"
    
    session_charge = current_session_data.get('delivery_charge', 0)
    
    opening_hours = business.get('opening_hours', 'তথ্য নেই') if business else "তথ্য নেই"
    delivery_info = business.get('delivery_info', 'তথ্য নেই') if business else "তথ্য নেই"
    payment_methods = business.get('payment_methods', []) if business else []

    categories = sorted(list(set([p.get('category') for p in products if p.get('category')])))
    category_list_str = ", ".join(categories) if categories else "তথ্য নেই"

    # Get current stock information from 'stock' column
    product_list_with_stock = []
    for p in products:
        stock = p.get("stock", 0)  # Use 'stock' column
        in_stock_status = p.get("in_stock", True)
        if in_stock_status and stock > 0:
            product_list_with_stock.append(f"- {p.get('name')}: ৳{p.get('price')} (স্টক: {stock})")
    
    product_list_short = "\n".join(product_list_with_stock)
    
    # Full product details including stock
    product_details_full = []
    for p in products:
        stock = p.get("stock", 0)  # Use 'stock' column
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
    
    api_key_res = supabase.table("api_keys").select("groq_api_key, groq_api_key_2, groq_api_key_3, groq_api_key_4, groq_api_key_5").eq("user_id", user_id).execute()
    
    if not api_key_res.data:
        logger.error(f"No API keys found for user {user_id}")
        return None, None
    
    row = api_key_res.data[0]
    keys = [row.get('groq_api_key'), row.get('groq_api_key_2'), row.get('groq_api_key_3'), row.get('groq_api_key_4'), row.get('groq_api_key_5')]
    valid_keys = [k for k in keys if k and k.strip()]

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
                    val = extracted_json['delivery_charge']
                    if val is None or str(val).lower() == 'null':
                        extracted_json['delivery_charge'] = 0.0
                    else:
                        extracted_json['delivery_charge'] = float(val)
                except (TypeError, ValueError):
                    extracted_json['delivery_charge'] = 0.0
                    
            return extracted_json
        except Exception as e:
            logger.error(f"Extraction Error: {e}")
            continue
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
    
    # Relaxed patterns to ensure "okay confirm", "hae confirm", etc. are caught
    confirm_patterns = [
        r'confirm',  # Matches "confirm" anywhere in the text
        r'কনফার্ম',
        r'ঠিক আছে',
        r'ok',
        r'okay',
        r'hae',
        r'ji',
        r'হ্যা',
        r'জি',
        r'yes',
        r'done',
        r'agreed',
        r'নিশ্চিত',
        r'পাঠান',
        r'send',
        r'\+1',
        r'\👍',
        r'\✅'
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
            # We return True for confirmation intent regardless of whether info is complete
            # The completeness check happens in the webhook logic
            return True, 'confirm'
    
    # Check for delay
    for pattern in delay_patterns:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return False, 'delay'
    
    # Check for denial
    for pattern in deny_patterns:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return False, 'deny'
    
    return False, 'neutral'

# ================= ORDER SUMMARY DISPLAY =================
def show_order_summary(token, customer_id, session_data, business_name):
    """
    Show order summary when all information is complete
    This runs INSTEAD of AI reply
    """
    items = session_data.get('items', [])
    delivery_charge = session_data.get('delivery_charge', 0)
    
    # Get products to calculate price
    user_id = session_data.get('user_id_from_session', '')
    products_db = get_products_with_details(user_id) if user_id else []
    
    summary_lines = []
    items_total = 0
    
    for item in items:
        product_name = item.get('product_name', '')
        quantity = item.get('quantity', 1)
        
        # Find product to get price
        product = find_best_product_match(product_name, products_db)
        if product:
            price = product.get('price', 0)
            subtotal = price * quantity
            items_total += subtotal
            summary_lines.append(f"• {product_name} x{quantity} = ৳{subtotal}")
        else:
            summary_lines.append(f"• {product_name} x{quantity}")
    
    # Calculate total
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

# ================= FOLLOW-UP SYSTEM (NEW IMPLEMENTATION) =================
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
            page_id = session.get('page_id') # Ensure page_id is saved in session during webhook
            
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
                
                send_message(token, customer_id, msg)
                
                # Update DB to mark follow-up as sent
                supabase.table("order_sessions").update({"last_followup_sent": True}).eq("id", session['id']).execute()
                
        return jsonify({"status": "success", "processed": len(res.data)}), 200
    except Exception as e:
        logger.error(f"Follow-up execution error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ================= BATCHED MESSAGE PROCESSOR (NEW) =================
def process_batched_messages(sender, user_id, page_id, token):
    """
    Processes all accumulated messages from the queue after the timer expires.
    This runs in a separate thread.
    """
    try:
        if sender not in user_queues or not user_queues[sender]:
            return

        # 1. Combine all messages into one text block
        # Using space to join.
        raw_text_list = user_queues[sender]
        raw_text = " ".join(raw_text_list)
        text = raw_text.lower().strip()
        
        # Clear the queue for this user immediately
        user_queues[sender] = []
        if sender in user_timers:
            del user_timers[sender]

        # 2. Show typing indicator (User knows bot is thinking)
        send_sender_action(token, sender, "typing_on")

        # 3. Check Subscription
        if not check_subscription_status(user_id):
            logger.info(f"Subscription inactive for user {user_id}. Bot silent.")
            return

        # 4. Get Bot Settings & Handle Delay
        bot_settings = get_bot_settings(user_id)
        if not bot_settings.get("ai_reply_enabled", True):
            return
        
        # --- DATABASE DELAY LOGIC MOVED HERE ---
        # This sleep happens in the background thread, so it doesn't block the server
        delay_ms = bot_settings.get("typing_delay", 0)
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)

        # 5. Core Logic (Copied from original Webhook)
        memory = get_chat_memory(user_id, sender)
        welcome_msg = bot_settings.get("welcome_message")
        
        # Check if this is a fresh start after order confirmation
        session_id = f"order_{user_id}_{sender}"
        current_session = get_session_from_db(session_id)
        
        # If no session exists, send welcome message
        if not current_session:
            if welcome_msg and not memory:
                send_message(token, sender, welcome_msg)
                save_chat_memory(user_id, sender, [{"role": "assistant", "content": welcome_msg}])
            
            current_session = OrderSession(user_id, sender)

        # Update page_id for follow-up purpose
        try:
            supabase.table("order_sessions").update({"page_id": page_id}).eq("id", session_id).execute()
        except: pass

        temp_memory = memory + [{"role": "user", "content": raw_text}]
        business = get_business_settings(user_id)
        delivery_policy = business.get('delivery_info', "তথ্য নেই") if business else "তথ্য নেই"
        
        extracted = extract_order_data_with_retry(user_id, temp_memory, delivery_policy)
        
        if extracted:
            # --- FIXED: NOTIFY USER ABOUT DELIVERY CHARGE IMMEDIATELY ---
            had_address = bool(current_session.data.get("address"))
            data_changed = False # Track if data actually changes
            
            # Only update if extracted data is NOT business details
            business_address = business.get('address', '') if business else ''
            business_phone = business.get('contact_number', '') if business else ''
            
            # Check if extracted address is actually business address
            if extracted.get("address") and business_address:
                if business_address.lower() in extracted.get("address", "").lower():
                    logger.info(f"Ignoring business address as customer address: {extracted.get('address')}")
                else:
                    if extracted.get("address") and extracted.get("address") != current_session.data.get("address"): 
                        current_session.data["address"] = extracted["address"]
                        data_changed = True
            elif extracted.get("address") and extracted.get("address") != current_session.data.get("address"):
                    current_session.data["address"] = extracted["address"]
                    data_changed = True
            
            # Check if extracted phone is actually business phone
            if extracted.get("phone") and business_phone:
                if business_phone in extracted.get("phone", ""):
                    logger.info(f"Ignoring business phone as customer phone: {extracted.get('phone')}")
                else:
                    if extracted.get("phone") and extracted.get("phone") != current_session.data.get("phone"): 
                        current_session.data["phone"] = extracted["phone"]
                        data_changed = True
            elif extracted.get("phone") and extracted.get("phone") != current_session.data.get("phone"):
                current_session.data["phone"] = extracted["phone"]
                data_changed = True
            
            # For name
            if extracted.get("name") and extracted.get("name") != current_session.data.get("name"): 
                current_session.data["name"] = extracted["name"]
                data_changed = True
            
            # For items
            if extracted.get("items") and extracted.get("items") != current_session.data.get("items"): 
                current_session.data["items"] = extracted["items"]
                data_changed = True
            
            if "delivery_charge" in extracted and isinstance(extracted["delivery_charge"], (int, float)):
                    current_session.data["delivery_charge"] = extracted["delivery_charge"]
                    # Send notification only if address was just now extracted/updated
                    if not had_address and extracted.get("address"):
                        send_message(token, sender, f"আপনার ঠিকানায় ডেলিভারি চার্জ ৳{extracted['delivery_charge']}")
            
            # Reset follow-up status when customer speaks
            try:
                supabase.table("order_sessions").update({"last_followup_sent": None}).eq("id", session_id).execute()
            except: pass
            
            # --- FIXED LOGIC: ONLY RESET SUMMARY IF DATA CHANGED ---
            is_confirming_now = any(w in text for w in ['confirm', 'ok', 'tik', 'done', 'yes', 'humm', 'ji', 'hae'])
            
            if data_changed and not is_confirming_now:
                    current_session.data["summary_shown"] = False
            
            save_session_to_db(current_session)

        s_data = current_session.data
        has_all_info = all([s_data.get("name"), s_data.get("phone"), s_data.get("address"), s_data.get("items")])
        
        # SMART ORDER CONFIRMATION DETECTION
        is_confirmation, intent_type = detect_order_confirmation_intent(raw_text, s_data)

        # Handle cancellation
        if "cancel" in text or "বাতিল" in text:
            delete_session_from_db(session_id)
            send_message(token, sender, "অর্ডার সেশনটি বাতিল করা হয়েছে। নতুন অর্ডার দিতে চাইলে বলুন।")
            save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": "অর্ডার সেশনটি বাতিল করা হয়েছে। নতুন অর্ডার দিতে চাইলে বলুন।"}])
            return

        # ====================================================================
        # LOGIC FLOW FIXED:
        # 1. First Check if User WANTS to Confirm
        # 2. Then Check if Summary NEEDS to be Shown
        # 3. Else let AI reply
        # ====================================================================

        # Handle confirmation intent (PRIORITY 1)
        if is_confirmation:
            if has_all_info:
                products_db = get_products_with_details(user_id)
                final_delivery_charge = float(s_data.get('delivery_charge', 0))
                
                items_total = 0
                summary_list = []
                order_success = True
                insufficient_stock_products = []
                out_of_stock_products = []
                
                # Stock check
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
                        save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": f"❌ দুঃখিত, '{product_name}' পণ্যটি সনাক্ত করা যায়নি।"}])
                
                if out_of_stock_products:
                    stock_msg = "❌ নিম্নলিখিত পণ্যগুলোর স্টক নেই:\n" + "\n".join(out_of_stock_products)
                    send_message(token, sender, stock_msg)
                    return
                
                if insufficient_stock_products:
                    stock_msg = "❌ নিম্নলিখিত পণ্যগুলোর পর্যাপ্ত স্টক নেই:\n" + "\n".join(insufficient_stock_products)
                    send_message(token, sender, stock_msg)
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
                                stock_updated = update_product_stock(user_id, product_name, qty)
                                if not stock_updated:
                                    failed_products.append(product_name)
                                    all_stock_updates_successful = False
                        
                        if not all_stock_updates_successful:
                            error_msg = f"❌ দুঃখিত, স্টক আপডেট সমস্যা: {', '.join(failed_products)}"
                            send_message(token, sender, error_msg)
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
                            
                            # Cleanup
                            try:
                                supabase.table("chat_history").delete().eq("user_id", user_id).eq("customer_id", sender).execute()
                            except Exception as e:
                                logger.error(f"Error clearing chat history: {e}")
                            delete_session_from_db(session_id)
                            current_session = None
                            return # END HERE for confirmed orders
                        else:
                            error_msg = "❌ দুঃখিত, অর্ডার সেভ করতে সমস্যা হয়েছে।"
                            send_message(token, sender, error_msg)
                            return
            else:
                missing = []
                if not s_data.get("name"): missing.append("নাম")
                if not s_data.get("phone"): missing.append("ফোন নম্বর")
                if not s_data.get("address"): missing.append("ঠিকানা")
                if not s_data.get("items"): missing.append("পণ্য")
                needed_info = " ও ".join(missing)
                response_msg = f"দুঃখিত, আপনার {needed_info} এখনো পাওয়া যায়নি। অর্ডার নিশ্চিত করতে এই তথ্যগুলো দিন।"
                send_message(token, sender, response_msg)
                return

        # Handle delay/deny (PRIORITY 2)
        elif intent_type == 'delay':
            delay_msg = "বেশ তো, কোনো সমস্যা নেই। যখনই ঠিক করবেন আমাকে জানাবেন। 😊"
            send_message(token, sender, delay_msg)
            return
        elif intent_type == 'deny':
            deny_msg = "ঠিক আছে, কোনো সমস্যা নেই। ধন্যবাদ! 😊"
            send_message(token, sender, deny_msg)
            delete_session_from_db(session_id)
            return

        # Show Summary Trigger (PRIORITY 3 - Only if not confirming and has all info)
        if has_all_info and not s_data.get("summary_shown", False):
            business = get_business_settings(user_id)
            business_name = business.get('name', 'আমাদের শপ') if business else "আমাদের শপ"
            s_data['user_id_from_session'] = user_id 
            summary_message = show_order_summary(token, sender, s_data, business_name)
            
            s_data["summary_shown"] = True
            current_session.data = s_data
            save_session_to_db(current_session)
            
            save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": summary_message}])
            return

        # ====================================================================
        # HYBRID MODE LOGIC (FINAL PRIORITY)
        # ====================================================================
        if bot_settings.get("hybrid_mode", True):
            # Check if session exists (it might be None if order was JUST completed)
            session_data_for_ai = current_session.data if current_session else {}
            
            reply, product_image = generate_ai_reply_with_retry(user_id, sender, raw_text, session_data_for_ai)
            
            if reply:
                if current_session and s_data.get("summary_shown", False):
                    # Reset summary if user talks after summary but doesn't confirm
                    current_session.data["summary_shown"] = False
                    save_session_to_db(current_session)
                
                if product_image:
                    send_image(token, sender, product_image)
                send_message(token, sender, reply)

        elif bot_settings.get("faq_only_mode", False):
            faqs = get_faqs(user_id)
            faq_reply = None
            for f in faqs:
                if f['question'] and f['question'].lower() in text:
                    faq_reply = f['answer']
                    break
            if faq_reply:
                send_message(token, sender, faq_reply)
                save_chat_memory(user_id, sender, memory + [{"role": "user", "content": raw_text}, {"role": "assistant", "content": faq_reply}])

    except Exception as e:
        logger.error(f"Error in batched processing: {e}", exc_info=True)


# ================= WEBHOOK (UPDATED FOR BATCHING) =================

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
        # Clean old processed messages
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
                
                # --- NEW BATCHING LOGIC START ---
                # 1. Mark Seen immediately so user knows we got it
                send_sender_action(token, sender, "mark_seen")

                # 2. Add message to queue
                if sender not in user_queues:
                    user_queues[sender] = []
                user_queues[sender].append(raw_text)

                # 3. Reset Timer (Debouncing)
                if sender in user_timers:
                    user_timers[sender].cancel()

                # 4. Start new timer (waits 3.0 seconds for more messages)
                t = threading.Timer(3.0, process_batched_messages, args=[sender, user_id, page_id, token])
                user_timers[sender] = t
                t.start()
                # --- NEW BATCHING LOGIC END ---

    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

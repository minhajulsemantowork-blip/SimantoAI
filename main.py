import os
import re
import logging
import requests
import json
import time
from typing import Optional, Dict, Tuple, List, Any
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from openai import OpenAI
from supabase import create_client, Client

# ================= CONFIG =================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)

processed_messages = {}

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

            # অগ্রাধিকার ভিত্তিতে paid_until চেক করবে
            expiry_str = sub.get("paid_until") or sub.get("end_date") or sub.get("trial_end")
            
            if expiry_str:
                now = datetime.now(timezone.utc)
                try:
                    # আপনার ফরম্যাট (2026-02-06 18:51:14.81+00) কে হ্যান্ডেল করার জন্য ফিক্স
                    clean_expiry = expiry_str.strip().replace(' ', 'T')
                    
                    # যদি টাইমজোন শুধু +00 থাকে, সেটাকে +00:00 করে ISO স্ট্যান্ডার্ড করা
                    if clean_expiry.endswith('+00'):
                        clean_expiry = clean_expiry.replace('+00', '+00:00')

                    try:
                        # প্রথমে স্ট্যান্ডার্ড ISO ফরম্যাট চেষ্টা করবে
                        expiry_date = datetime.fromisoformat(clean_expiry)
                    except ValueError:
                        # যদি ফেইল করে, ম্যানুয়াল ফরম্যাট (মাইক্রোসেকেন্ড সহ বা ছাড়া)
                        clean_date_str = expiry_str.strip()
                        # +00 থাকলে %z এর জন্য +0000 ফরম্যাটে আনা (কিছু পাইথন ভার্সনের জন্য)
                        if clean_date_str.endswith('+00'):
                            clean_date_str = clean_date_str.replace('+00', '+0000')
                        
                        try:
                            expiry_date = datetime.strptime(clean_date_str, "%Y-%m-%d %H:%M:%S.%f%z")
                        except ValueError:
                             # যদি সব ফেইল করে, টাইমজোন অংশ কেটে UTC ধরে নেওয়া
                            clean_date_no_tz = expiry_str.split('+')[0].strip()
                            try:
                                expiry_date = datetime.strptime(clean_date_no_tz, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
                            except ValueError:
                                expiry_date = datetime.strptime(clean_date_no_tz, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                
                except Exception as e:
                    logger.error(f"Date Parsing Error: {e}")
                    # পার্সিং এরর হলে সেফটির জন্য False রিটার্ন করবে না
                    return False

                # এক্সপায়ার হয়েছে কিনা চেক
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
    # Default values updated to include new modes
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

# ================= DATA FETCHERS =================
def get_products_with_details(user_id: str):
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

# ================= AI LOGIC =================
def generate_ai_reply_with_retry(user_id, customer_id, user_msg, current_session_data, max_retries=2):
    business = get_business_settings(user_id)
    products = get_products_with_details(user_id)
    faqs = get_faqs(user_id)
    
    biz_phone = business.get('contact_number', '') if business else ""
    business_name = business.get('name', 'আমাদের শপ') if business else "আমাদের শপ"
    business_address = business.get('address', 'ঠিকানা উপলব্ধ নয়') if business else "ঠিকানা উপলব্ধ নয়"
    delivery_charge = business.get('delivery_charge', 60) if business else 60
    
    opening_hours = business.get('opening_hours', 'তথ্য নেই') if business else "তথ্য নেই"
    delivery_info = business.get('delivery_info', 'তথ্য নেই') if business else "তথ্য নেই"
    payment_methods = business.get('payment_methods', []) if business else []

    # --- CATEGORY UPDATE START ---
    # ইউনিক ক্যাটাগরিগুলো বের করা হচ্ছে
    categories = sorted(list(set([p.get('category') for p in products if p.get('category')])))
    category_list_str = ", ".join(categories) if categories else "তথ্য নেই"
    # --- CATEGORY UPDATE END ---

    # প্রোডাক্ট লিস্ট এবং ডিটেইলসে ক্যাটাগরি যোগ করা হয়েছে
    product_list_short = "\n".join([f"- {p.get('name')}: ৳{p.get('price')} (Category: {p.get('category', 'N/A')})" for p in products if p.get("in_stock")])
    product_details_full = "\n".join([f"পণ্য: {p.get('name')}\nদাম: ৳{p.get('price')}\nক্যাটাগরি: {p.get('category')}\nবিবরণ: {p.get('description')}" for p in products if p.get("in_stock")])
    
    faq_text = "\n".join([f"Q: {f['question']} | A: {f['answer']}" for f in faqs])

    known_info_str = f"প্রাপ্ত তথ্য - নাম: {current_session_data.get('name', 'নেই')}, ফোন: {current_session_data.get('phone', 'নেই')}, ঠিকানা: {current_session_data.get('address', 'নেই')}."

    system_prompt = ( f"""
তুমি '{business_name}'-এর একজন বাস্তব মানুষ, তোমার নাম সিমান্ত, তুমি একজন অভিজ্ঞ সেলস এক্সিকিউটিভ।
তুমি কোনো AI, bot বা সফটওয়্যার নও—এমন কিছু কখনো বলবে না।

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

পণ্য সংক্রান্ত নিয়ম:
- পণ্যের নাম ডাটাবেসে যেভাবে (English/Bangla) আছে, ঠিক সেভাবেই বলবে। নামের অনুবাদ করবে না।
- লিস্ট চাইলে শুধু নাম ও দাম দেখাবে
- নির্দিষ্ট পণ্য জিজ্ঞেস করলে সেই পণ্যের ডাটাবেস দেখে তথ্য গুলা নিজের ভাষায় সুন্দর করে বোঝাবে 
- পণ্য সম্পর্কে কোনোরকম মিথ্যে প্রতিশ্রুতি দিবেনা 
- গ্রাহক কোন নির্দিষ্ট তথ্য জানতে চাইলে, ডাটাবেস দেখে নির্দিষ্ট তথ্যটি নিজের ভাষায় সুন্দর করে বলবে

অর্ডার আচরণ (Strict Rules):
- গ্রাহক নিজে না চাইলে অর্ডার চাপাবে না
- নাম, ফোন, ঠিকানা পেলে আগে গ্রাহকের ঠিকানা দেখে {delivery_info} অনুযায়ী ডেলিভারি চার্জ হিসাব করে গ্রাহককে জানাবে এবং সুন্দর করে গ্রাহকের সিদ্ধান্ত জানতে চাইবে।
- তারপর গ্রাহক সম্মতি দিলে গ্রাহকের নাম, ফোন, ঠিকানা এবং ডেলিভারি চার্জ সহ সংক্ষেপে অর্ডার সামারি দেখাবে
- স্পষ্ট করে বলবে: অর্ডার নিশ্চিত করতে “Confirm / কনফার্ম” লিখতে হবে
- নাম, ফোন, ঠিকানা না পেলে কোন ভাবেই অর্ডার কনফার্ম করবে না
- তুমি নিজে কখনো বলবে না যে অর্ডার কনফার্ম হয়েছে বা সফল হয়েছে। তুমি শুধু গ্রাহককে তথ্য দিয়ে সাহায্য করবে এবং বলবে যে অর্ডার করতে চাইলে 'Confirm' লিখতে। অর্ডার সফল করার দায়িত্ব সিস্টেমের।

ব্যবসায়িক তথ্য:
- খোলা থাকে: {opening_hours}
- ডেলিভারি তথ্য: {delivery_info}
- পেমেন্ট মাধ্যম: {payment_methods}
- শপের ঠিকানা: {business_address}
- কল করুন: {biz_phone}
- ডেলিভারি চার্জ: ৳{delivery_charge}

জানা তথ্য:
{known_info_str}

উপলব্ধ ক্যাটাগরি: {category_list_str}

পণ্য তালিকা:
{product_list_short}

পণ্যের বিস্তারিত:
{product_details_full}

FAQ:
{faq_text}

Must Maintain This Rule: সব উত্তর ২–৪ লাইনের মধ্যে রাখবে।
"""
    )

    memory = get_chat_memory(user_id, customer_id)
    
    # --- UPDATED KEY ROTATION LOGIC START ---
    # 5টি কী একসাথে আনা হচ্ছে
    api_key_res = supabase.table("api_keys").select("groq_api_key, groq_api_key_2, groq_api_key_3, groq_api_key_4, groq_api_key_5").eq("user_id", user_id).execute()
    
    if not api_key_res.data:
        logger.error(f"No API keys found for user {user_id}")
        return None, None
    
    row = api_key_res.data[0]
    # একটি লিস্টে ভ্যালিড কীগুলো সাজানো হচ্ছে
    keys = [row.get('groq_api_key'), row.get('groq_api_key_2'), row.get('groq_api_key_3'), row.get('groq_api_key_4'), row.get('groq_api_key_5')]
    valid_keys = [k for k in keys if k and k.strip()]

    if not valid_keys:
        return None, None

    # একটি কী ফেইল করলে পরের কী ব্যবহার করবে
    for key in valid_keys:
        client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=key)
        try:
            res = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": system_prompt}] + memory + [{"role": "user", "content": user_msg}],
                temperature=0.7, 
                timeout=5.0 
            )
            reply = res.choices[0].message.content.strip()
            save_chat_memory(user_id, customer_id, (memory + [{"role": "user", "content": user_msg}, {"role": "assistant", "content": reply}])[-10:])
            
            matched_image = None
            for p in products:
                if p.get('name') and p.get('name').lower() in reply.lower() and p.get('image_url'):
                    matched_image = p.get('image_url')
                    break
            
            return reply, matched_image
        except Exception as e:
            logger.error(f"AI Generation Error with key (switching to next): {e}")
            continue # বর্তমান কী কাজ না করলে লুপের মাধ্যমে পরের কী ট্রাই করবে
    
    # সব কী ফেইল করলে
    return None, None
    # --- UPDATED KEY ROTATION LOGIC END ---

# ================= ORDER EXTRACTION =================
def extract_order_data_with_retry(user_id, messages, max_retries=2):
    # --- UPDATED KEY ROTATION LOGIC START ---
    api_key_res = supabase.table("api_keys").select("groq_api_key, groq_api_key_2, groq_api_key_3, groq_api_key_4, groq_api_key_5").eq("user_id", user_id).execute()
    if not api_key_res.data: return None
    
    row = api_key_res.data[0]
    keys = [row.get('groq_api_key'), row.get('groq_api_key_2'), row.get('groq_api_key_3'), row.get('groq_api_key_4'), row.get('groq_api_key_5')]
    valid_keys = [k for k in keys if k and k.strip()]

    if not valid_keys: return None

    prompt = (
        "Extract order details from the conversation into JSON. "
        "Keys: name, phone, address, items (product_name, quantity). "
        "If a field is not found in the latest text, return null or empty string. "
        "Return ONLY JSON."
    )

    for key in valid_keys:
        client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=key)
        try:
            res = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": prompt}] + messages[-5:], 
                response_format={"type": "json_object"},
                temperature=0,
                timeout=4.0
            )
            content = res.choices[0].message.content
            cleaned_content = re.sub(r"```json|```", "", content).strip()
            return json.loads(cleaned_content)
        except Exception:
            continue
    return None
    # --- UPDATED KEY ROTATION LOGIC END ---

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
        for entry in data.get("entry", []):
            page_id = entry.get("id")
            page = get_page_client(page_id)
            if not page: continue
            user_id, token = page["user_id"], page["page_access_token"]

            # --- processed_messages cleanup (5 min) ---
            now_ts = time.time()
            processed_messages = {k: v for k, v in processed_messages.items() if now_ts - v < 300}

            for msg_event in entry.get("messaging", []):
                sender = msg_event["sender"]["id"]
                if "message" not in msg_event: continue
                if "text" not in msg_event["message"]: continue
                
                msg_id = msg_event["message"].get("mid")
                if not msg_id: continue

                # duplicate skip
                if msg_id in processed_messages: continue
                processed_messages[msg_id] = time.time()

                raw_text = msg_event["message"].get("text", "")
                if not raw_text: continue
                text = raw_text.lower().strip()

                # ... এরপর তোমার মূল subscription, AI, order confirm logic যাবে ...


                # --- Subscription Check ---
                if not check_subscription_status(user_id):
                    # সাবস্ক্রিপশন শেষ হলে বট কোনো মেসেজ দিবে না, জাস্ট স্কিপ করবে
                    logger.info(f"Subscription inactive for user {user_id}. Bot silent.")
                    continue

                # --- Bot Settings & Master Switch ---
                bot_settings = get_bot_settings(user_id)

                # ১. মাস্টার সুইচ (AI Reply Enabled)
                # যদি এটি False হয়, তবে পুরো সিস্টেম (কনফার্ম/AI/FAQ) বন্ধ থাকবে
                if not bot_settings.get("ai_reply_enabled", True):
                    logger.info(f"Bot system is disabled for user {user_id}")
                    continue
                
                # টাইপিং ডিলে
                delay_ms = bot_settings.get("typing_delay", 0)
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000)

                # চ্যাট মেমোরি এবং ওয়েলকাম মেসেজ চেক
                memory = get_chat_memory(user_id, sender)
                welcome_msg = bot_settings.get("welcome_message")
                
                # যদি চ্যাট হিস্ট্রি না থাকে (নতুন কাস্টমার) এবং ডাটাবেসে ওয়েলকাম মেসেজ সেট করা থাকে
                if not memory and welcome_msg:
                    send_message(token, sender, welcome_msg)
                    # ওয়েলকাম মেসেজ পাঠানোর পর মেমোরিতে এটি সেভ করে রাখা যাতে বারবার না যায়
                    save_chat_memory(user_id, sender, [{"role": "assistant", "content": welcome_msg}])

                session_id = f"order_{user_id}_{sender}"
                current_session = get_session_from_db(session_id)
                if not current_session:
                    current_session = OrderSession(user_id, sender)

                temp_memory = memory + [{"role": "user", "content": raw_text}]
                extracted = extract_order_data_with_retry(user_id, temp_memory)
                
                if extracted:
                    if extracted.get("name"): current_session.data["name"] = extracted["name"]
                    if extracted.get("phone"): current_session.data["phone"] = extracted["phone"]
                    if extracted.get("address"): current_session.data["address"] = extracted["address"]
                    if extracted.get("items"): current_session.data["items"] = extracted["items"]
                    save_session_to_db(current_session)

                is_confirm_intent = re.fullmatch(r"(confirm|কনফার্ম|ok|ওকে)", text) is not None or text.startswith(("confirm ", "কনফার্ম "))
                if is_confirm_intent:
                    s_data = current_session.data
                    # তথ্য যাচাই করা হচ্ছে
                    missing = []
                    if not s_data.get("name"): missing.append("নাম")
                    if not s_data.get("phone"): missing.append("ফোন নম্বর")
                    if not s_data.get("address"): missing.append("ঠিকানা")
                    if not s_data.get("items"): missing.append("পণ্য")

                    if not missing:
                        products_db = get_products_with_details(user_id)
                        business = get_business_settings(user_id)
                        delivery_charge = business.get('delivery_charge', 60) if business else 60
                        
                        items_total = 0
                        summary_list = []
                        # AI থেকে পাওয়া পণ্যের সাথে ডাটাবেসের ম্যাচ করা
                        for item in s_data.get('items', []):
                            for p in products_db:
                                if item.get('product_name') and p.get('name'):
                                    if (
                                        item['product_name'].lower() in p['name'].lower()
                                        or p['name'].lower() in item['product_name'].lower()
                                    ):
                                        qty = int(item.get('quantity', 1))
                                        items_total += p['price'] * qty
                                        summary_list.append(f"{p['name']} x{qty}")
                                        break

                        if items_total > 0:
                            current_session.data['product'] = ", ".join(summary_list)

                            if current_session.save_order(
                                product_total=items_total,
                                delivery_charge=delivery_charge
                            ):
                                confirm_msg = (
                                    f"✅ আপনার অর্ডারটি গ্রহণ করা হয়েছে,\n\n"
                                    f"অর্ডার সামারি:\n{', '.join(summary_list)}\n"
                                    f"মোট: ৳{items_total + delivery_charge}\n\n"
                                    f"আমরা খুব শীঘ্রই আপনার সাথে যোগাযোগ করবো, আমাদের প্রতি বিশ্বাস রাখার জন্য আপনাকে অসংখ্য ধন্যবাদ। ❤️"
                                )
                                send_message(token, sender, confirm_msg)
                                delete_session_from_db(session_id)
                            else:
                                logger.error(f"Order Save Failed for customer {sender}")
                        else:
                            send_message(
                                token,
                                sender,
                                "❌ দুঃখিত, আপনি যে পণ্যটি বলেছেন সেটি সনাক্ত করা যায়নি। অনুগ্রহ করে পণ্যের সঠিক নাম আবার লিখুন।"
                            )
                            logger.warning(
                                f"Order confirm blocked — items_total=0 | session={session_id}"
                            )

                    else:
                        needed_info = " ও ".join(missing)
                        send_message(token, sender, f"দুঃখিত, আপনার {needed_info} এখনো পাওয়া যায়নি। অর্ডারটি নিশ্চিত করতে এই তথ্যগুলো দিন।")
                    continue

                if "cancel" in text or "বাতিল" in text:
                    delete_session_from_db(session_id)
                    send_message(token, sender, "আপনার অর্ডার সেশনটি বাতিল করা হয়েছে।")
                    continue

                # --- Response Logic (Hybrid / FAQ Only) ---
                if not is_confirm_intent:
                    if bot_settings.get("hybrid_mode", True):
                        reply, product_image = generate_ai_reply_with_retry(user_id, sender, raw_text, current_session.data)
                        # যদি reply থাকে কেবল তখনই মেসেজ পাঠাবে
                        if reply:
                            if product_image:
                                send_image(token, sender, product_image)
                            send_message(token, sender, reply)

                    elif bot_settings.get("faq_only_mode", False):
                        # ৩. শুধু FAQ মোড (AI অফ, শুধু ডাটাবেস চেক)
                        faqs = get_faqs(user_id)
                        faq_reply = None
                        for f in faqs:
                            # সাধারণ সাবস্ট্রিং ম্যাচিং
                            if f['question'] and f['question'].lower() in text:
                                faq_reply = f['answer']
                                break
                        
                        if faq_reply:
                            send_message(token, sender, faq_reply)
                        # মিল না পেলে কোনো রিপ্লাই যাবে না (Silent)

    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

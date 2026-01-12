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
    
    session_charge = current_session_data.get('delivery_charge', 0)
    if session_charge > 0:
        delivery_charge = session_charge
    else:
        delivery_charge = business.get('delivery_charge', 60) if business else 60
    
    opening_hours = business.get('opening_hours', 'তথ্য নেই') if business else "তথ্য নেই"
    delivery_info = business.get('delivery_info', 'তথ্য নেই') if business else "তথ্য নেই"
    payment_methods = business.get('payment_methods', []) if business else []

    categories = sorted(list(set([p.get('category') for p in products if p.get('category')])))
    category_list_str = ", ".join(categories) if categories else "তথ্য নেই"

    product_list_short = "\n".join([f"- {p.get('name')}: ৳{p.get('price')} (Category: {p.get('category', 'N/A')})" for p in products if p.get("in_stock")])
    product_details_full = "\n".join([f"পণ্য: {p.get('name')}\nদাম: ৳{p.get('price')}\nক্যাটাগরি: {p.get('category')}\nবিবরণ: {p.get('description')}" for p in products if p.get("in_stock")])
    
    faq_text = "\n".join([f"Q: {f['question']} | A: {f['answer']}" for f in faqs])

    known_info_str = f"প্রাপ্ত তথ্য - নাম: {current_session_data.get('name', 'নেই')}, ফোন: {current_session_data.get('phone', 'নেই')}, ঠিকানা: {current_session_data.get('address', 'নেই')}."

    # --- SYSTEM PROMPT ---
    system_prompt = ( f"""
ROLE & IDENTITY:
তুমি '{business_name}'-এর একজন বাস্তব মানুষ।
তোমার নাম সিমান্ত।
তুমি একজন বিশ্বাসযোগ্য, নম্র এবং প্রফেশনাল সেলস এক্সপার্ট।

DELIVERY CHARGE POLICY (VERY IMPORTANT):
- আমাদের স্ট্যান্ডার্ড ডেলিভারি চার্জ: {delivery_charge} টাকা।
- গ্রাহক যখনই ঠিকানা প্রদান করবে, তুমি সাথে সাথে বলবে: "আপনার ঠিকানায় ডেলিভারি চার্জ হবে {delivery_charge} টাকা।"

LANGUAGE & COMMUNICATION (TOP PRIORITY):
- শুধুমাত্র বাংলা ভাষায় কথা বলবে
- কোনো ইংরেজি বা মিশ্র ভাষা ব্যবহার করবে না (তবে পণ্যের নাম ডাটাবেসে যেভাবে আছে সেভাবেই রাখবে)
- পণ্যের নামের অনুবাদ করবে না
- ছোট, পরিষ্কার ও কথ্য বাংলা বাক্য ব্যবহার করবে

CUSTOMER HANDLING STYLE:
- আগে গ্রাহকের প্রয়োজন বোঝার চেষ্টা করবে
- এক উত্তরে বেশি তথ্য দিবে না
- কখনো চাপ সৃষ্টি করবে না

SALES CONVERSION STRATEGY:
- গ্রাহক সব পণ্য দেখতে চাইলে একসাথে সব পণ্যের লিস্ট দিবে না
- প্রথমে {category_list_str} দেখে বলবে আমাদের কাছে কী কী ধরনের পণ্য আছে
- গ্রাহক নির্দিষ্ট কিছু চাইলে, ডাটাবেস থেকে মিল আছে এমন সর্বোচ্চ ২–৩টি পণ্য দেখাবে

ORDER FLOW (VERY STRICT – NO EXCEPTION):
- গ্রাহকের নাম (Name), ফোন নম্বর (Phone) এবং ঠিকানা (Address) না পাওয়া পর্যন্ত:
  - "Confirm" বা "কনফার্ম" শব্দ ব্যবহার করবে না
  - অর্ডার সামারি দেখাবে না
  - কখনো বলবে না "অর্ডার কনফার্ম হয়েছে" বা "সফল হয়েছে"
- নাম, ফোন এবং ঠিকানা পাওয়ার পরেই:
  - অর্ডার সামারি দেখাবে
  - ডেলিভারি চার্জ উল্লেখ করবে
  - গ্রাহককে কনফার্ম করতে বলবে (যেমন: "অর্ডারটি কনফার্ম করতে চাইলে 'Confirm' লিখুন")

IMAGE SENDING RULES:
- প্রতি মেসেজে ছবি পাঠাবে না।
- গ্রাহক নিজে থেকে ছবি চাইলে অথবা কোনো পণ্য নিয়ে আলোচনা শুরু হলে শুধুমাত্র তখন একবার ছবি দেখাবে।
- অযথা একই ছবি বারবার পাঠাবে না।

MOST IMPORTANT RULE:
- পণ্যের নাম ডাটাবেসে যেভাবে আছে, ঠিক সেভাবেই বলবে।
- কখনোই পণ্যের নামের অনুবাদ করবে না।
- তুমি নিজে কখনো অর্ডার প্লেস করবে না, শুধু তথ্য সংগ্রহ করবে।

BUSINESS INFORMATION:
- খোলা থাকে: {opening_hours}
- ডেলিভারি তথ্য: {delivery_info}
- পেমেন্ট মাধ্যম: {payment_methods}
- শপের ঠিকানা: {business_address}
- কল করুন: {biz_phone}

DATABASE CONTEXT:
- জানা তথ্য: {known_info_str}
- পণ্য তালিকা: {product_list_short}
- পণ্যের বিস্তারিত: {product_details_full}
- FAQ: {faq_text}

RESPONSE LIMIT:
- প্রতিটি উত্তর অবশ্যই ২–৪ লাইনের মধ্যে হবে।
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
            
            # --- UPDATED: Smart Image Triggering Logic ---
            matched_image = None
            
            # ১. গ্রাহক কি ছবি দেখতে চেয়েছে? (Keywords check)
            image_request_keywords = ['chobi', 'photo', 'image', 'dekhan', 'dekhi', 'ছবি', 'দেখাও', 'দেখি', 'pic', 'পিক', 'পিকচার']
            wants_to_see_image = any(word in user_msg.lower() for word in image_request_keywords)
            
            # ২. এই চ্যাটে আগে কি ছবি পাঠানো হয়েছে? 
            already_sent_any_image = any("image_url" in str(m) or "attachment" in str(m) for m in memory)

            # ৩. AI-এর রিপ্লাই থেকে পণ্য খুঁজে বের করা (Flexible Matching)
            reply_lower = reply.lower()
            mentioned_products = []
            for p in products:
                p_name = p.get('name', '').lower()
                # পণ্যের পুরো নাম অথবা নামের প্রধান অংশ (৪ অক্ষরের বড় শব্দ) চেক করা
                if p_name in reply_lower or any(word in reply_lower for word in p_name.split() if len(word) > 3):
                    mentioned_products.append(p)

            # ৪. ইমেজ ট্রিগার করার ফাইনাল কন্ডিশন
            if mentioned_products:
                candidate_image = mentioned_products[0].get('image_url')
                
                if candidate_image:
                    # চেক করা এই নির্দিষ্ট ইমেজটি আগে পাঠানো হয়েছে কিনা
                    already_sent_this_specific_image = any(candidate_image in str(m) for m in memory)

                    # লজিক: গ্রাহক চাইলে সবসময় পাঠাবে OR যদি আগে না পাঠিয়ে থাকে তবে একবার পাঠাবে
                    if wants_to_see_image:
                        matched_image = candidate_image
                    elif not already_sent_this_specific_image and len(mentioned_products) == 1:
                        matched_image = candidate_image
            
            return reply, matched_image
        except Exception as e:
            logger.error(f"AI Generation Error: {e}")
            continue 
    
    return None, None

# ================= ORDER EXTRACTION =================
def extract_order_data_with_retry(user_id, messages, delivery_policy_text, max_retries=2):
    api_key_res = supabase.table("api_keys").select("groq_api_key, groq_api_key_2, groq_api_key_3, groq_api_key_4, groq_api_key_5").eq("user_id", user_id).execute()
    if not api_key_res.data: return None
    
    row = api_key_res.data[0]
    keys = [row.get('groq_api_key'), row.get('groq_api_key_2'), row.get('groq_api_key_3'), row.get('groq_api_key_4'), row.get('groq_api_key_5')]
    valid_keys = [k for k in keys if k and k.strip()]

    if not valid_keys: return None

    prompt = (
        "Extract order details from the conversation into JSON. "
        "Keys: name, phone, address, items (product_name, quantity), delivery_charge (number). "
        f"Delivery Policy context: '{delivery_policy_text}'. "
        "STRICT RULES: "
        "1. NAME: Only extract the name if the user explicitly says their name. "
        "2. ITEMS: Check the last messages for interested products. "
        "3. DELIVERY: Calculate delivery_charge from the policy based on address. "
        "4. OUTPUT: Return ONLY JSON."
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
            return json.loads(cleaned_content)
        except Exception:
            continue
    return None

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
        # --- UPDATE: Early Duplicate Check ---
        for entry in data.get("entry", []):
            for msg_event in entry.get("messaging", []):
                msg_id = msg_event.get("message", {}).get("mid")
                if msg_id and msg_id in processed_messages:
                    return jsonify({"status": "already_processed"}), 200

        for entry in data.get("entry", []):
            page_id = entry.get("id")
            page = get_page_client(page_id)
            if not page: continue
            user_id, token = page["user_id"], page["page_access_token"]

            now_ts = time.time()
            processed_messages = {k: v for k, v in processed_messages.items() if now_ts - v < 300}

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
                text = raw_text.lower().strip()

                if not check_subscription_status(user_id):
                    logger.info(f"Subscription inactive for user {user_id}. Bot silent.")
                    continue

                bot_settings = get_bot_settings(user_id)
                if not bot_settings.get("ai_reply_enabled", True):
                    continue
                
                delay_ms = bot_settings.get("typing_delay", 0)
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000)

                memory = get_chat_memory(user_id, sender)
                welcome_msg = bot_settings.get("welcome_message")
                
                if not memory and welcome_msg:
                    send_message(token, sender, welcome_msg)
                    save_chat_memory(user_id, sender, [{"role": "assistant", "content": welcome_msg}])

                session_id = f"order_{user_id}_{sender}"
                current_session = get_session_from_db(session_id)
                if not current_session:
                    current_session = OrderSession(user_id, sender)

                temp_memory = memory + [{"role": "user", "content": raw_text}]
                business = get_business_settings(user_id)
                delivery_policy = business.get('delivery_info', "তথ্য নেই") if business else "তথ্য নেই"
                
                extracted = extract_order_data_with_retry(user_id, temp_memory, delivery_policy)
                
                if extracted:
                    if extracted.get("name"): current_session.data["name"] = extracted["name"]
                    if extracted.get("phone"): current_session.data["phone"] = extracted["phone"]
                    if extracted.get("address"): current_session.data["address"] = extracted["address"]
                    if extracted.get("items"): current_session.data["items"] = extracted["items"]
                    if "delivery_charge" in extracted and isinstance(extracted["delivery_charge"], (int, float)):
                         current_session.data["delivery_charge"] = extracted["delivery_charge"]
                    
                    # --- FIX: Ensure delivery charge is set if address exists ---
                    if current_session.data.get("address") and current_session.data.get("delivery_charge") == 0:
                        default_charge = business.get('delivery_charge', 60) if business else 60
                        current_session.data["delivery_charge"] = default_charge

                    # --- NEW LOGIC: Inform about delivery charge immediately when address is found ---
                    if extracted.get("address"):
                        final_charge = current_session.data.get("delivery_charge", 60)
                        charge_msg = f"ধন্যবাদ। আপনার ঠিকানায় ডেলিভারি চার্জ হবে {final_charge} টাকা।"
                        send_message(token, sender, charge_msg)
                    
                    save_session_to_db(current_session)

                # --- FIX: Expanded Confirmation Keywords ---
                is_confirm_intent = re.fullmatch(r"(confirm|কনফার্ম|ok|ওকে|done|ঠিক আছে|হুম|hmm|hum|ji|জি)", text) is not None or text.startswith(("confirm ", "কনফার্ম "))
                
                if is_confirm_intent:
                    s_data = current_session.data
                    missing = []
                    if not s_data.get("name"): missing.append("নাম")
                    if not s_data.get("phone"): missing.append("ফোন নম্বর")
                    if not s_data.get("address"): missing.append("ঠিকানা")
                    if not s_data.get("items"): missing.append("পণ্য")

                    if not missing:
                        products_db = get_products_with_details(user_id)
                        final_delivery_charge = float(s_data.get("delivery_charge", 0))
                        if final_delivery_charge == 0:
                            final_delivery_charge = float(business.get('delivery_charge', 60)) if business else 60
                        
                        items_total = 0
                        summary_list = []
                        for item in s_data.get('items', []):
                            for p in products_db:
                                if item.get('product_name') and p.get('name'):
                                    if item['product_name'].lower() in p['name'].lower() or p['name'].lower() in item['product_name'].lower():
                                        qty = int(item.get('quantity', 1))
                                        items_total += p['price'] * qty
                                        summary_list.append(f"{p['name']} x{qty}")
                                        break

                        if items_total > 0:
                            current_session.data['product'] = ", ".join(summary_list)
                            if current_session.save_order(product_total=items_total, delivery_charge=final_delivery_charge):
                                confirm_msg = (
                                    f"✅ আপনার অর্ডারটি গ্রহণ করা হয়েছে,\n\n"
                                    f"অর্ডার সামারি:\n{', '.join(summary_list)}\n"
                                    f"মোট: ৳{items_total + final_delivery_charge} (ডেলিভারি চার্জ: ৳{final_delivery_charge})\n\n"
                                    f"আমরা খুব শীঘ্রই আপনার সাথে যোগাযোগ করবো। ধন্যবাদ। ❤️"
                                )
                                send_message(token, sender, confirm_msg)
                                delete_session_from_db(session_id)
                                # --- UPDATE: Clear Memory to avoid confusion ---
                                save_chat_memory(user_id, sender, [])
                            else:
                                logger.error(f"Order Save Failed for customer {sender}")
                        else:
                            send_message(token, sender, "❌ দুঃখিত, পণ্যটি সনাক্ত করা যায়নি।")

                    else:
                        needed_info = " ও ".join(missing)
                        send_message(token, sender, f"দুঃখিত, আপনার {needed_info} এখনো পাওয়া যায়নি। অর্ডার নিশ্চিত করতে এই তথ্যগুলো দিন।")
                    continue

                if "cancel" in text or "বাতিল" in text:
                    delete_session_from_db(session_id)
                    send_message(token, sender, "অর্ডার সেশনটি বাতিল করা হয়েছে।")
                    continue

                if not is_confirm_intent:
                    if bot_settings.get("hybrid_mode", True):
                        reply, product_image = generate_ai_reply_with_retry(user_id, sender, raw_text, current_session.data)
                        if reply:
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

    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

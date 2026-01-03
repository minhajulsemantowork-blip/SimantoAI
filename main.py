import os
import re
import logging
import requests
import json
import time
from typing import Optional, Dict, Tuple, List, Any
from datetime import datetime
from flask import Flask, request, jsonify
from openai import OpenAI
from supabase import create_client, Client

# ================= CONFIG =================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)

# Supabase Client Setup
try:
    supabase: Client = create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_SERVICE_KEY")
    )
except Exception as e:
    logger.error(f"Supabase connection failed: {e}")

# ================= SUBSCRIPTION CHECKER (NEW) =================
def check_subscription_status(user_id: str) -> bool:
    """
    Checks if the user (business owner) has an active subscription.
    Returns True if active, False otherwise.
    """
    try:
        # Status চেক করা হচ্ছে
        res = supabase.table("subscriptions").select("status").eq("user_id", user_id).execute()
        
        # যদি ডাটা থাকে এবং স্ট্যাটাস 'active' হয়
        if res.data and len(res.data) > 0:
            status = res.data[0].get("status")
            if status == "active":
                return True
                
        return False
    except Exception as e:
        logger.error(f"Subscription Check Error for user {user_id}: {e}")
        # সেফটির জন্য False রিটার্ন করছি যাতে পেমেন্ট ছাড়া সার্ভিস না পায়
        return False

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
                "total": float(product_total),
                "delivery_charge": float(delivery_charge),
                "status": "pending",
                "created_at": datetime.utcnow().isoformat()
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
            "last_updated": datetime.utcnow().isoformat()
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
    now = datetime.utcnow().isoformat()
    existing = supabase.table("chat_history").select("id").eq("user_id", user_id).eq("customer_id", customer_id).execute()
    if existing.data:
        supabase.table("chat_history").update({"messages": messages, "last_updated": now}).eq("id", existing.data[0]["id"]).execute()
    else:
        supabase.table("chat_history").insert({"user_id": user_id, "customer_id": customer_id, "messages": messages, "created_at": now, "last_updated": now}).execute()

# ================= AI LOGIC (SMART CONTEXT) =================
def generate_ai_reply_with_retry(user_id, customer_id, user_msg, current_session_data, max_retries=3):
    business = get_business_settings(user_id)
    products = get_products_with_details(user_id)
    faqs = get_faqs(user_id)
    
    biz_phone = business.get('contact_number', '') if business else ""
    business_name = business.get('name', 'আমাদের শপ') if business else "আমাদের শপ"
    delivery_charge = business.get('delivery_charge', 60) if business else 60

    product_list_short = "\n".join([f"- {p.get('name')}: ৳{p.get('price')}" for p in products if p.get("in_stock")])
    product_details_full = "\n".join([f"পণ্য: {p.get('name')}\nদাম: ৳{p.get('price')}\nবিবরণ: {p.get('description')}" for p in products if p.get("in_stock")])
    faq_text = "\n".join([f"Q: {f['question']} | A: {f['answer']}" for f in faqs])

    has_name = bool(current_session_data.get("name"))
    has_phone = bool(current_session_data.get("phone"))
    has_address = bool(current_session_data.get("address"))
    
    known_info_str = f"প্রাপ্ত তথ্য - নাম: {current_session_data.get('name', 'নেই')}, ফোন: {current_session_data.get('phone', 'নেই')}, ঠিকানা: {current_session_data.get('address', 'নেই')}."

    system_prompt = (
        f"আপনার নাম Simanto, আপনি '{business_name}'-এর একজন বন্ধুসুলভ এবং পেশাদার সেলস অ্যাসিস্ট্যান্ট।\n"
        "**আচরণবিধি ও নিয়মাবলী:**\n"
        "১. সর্বদা মার্জিত ও প্রমিত বাংলা ব্যবহার করুন। উত্তর হবে সুন্দর ও সংক্ষিপ্ত।\n"
        "২. পণ্যের নাম ডাটাবেসে ঠিক যেভাবে (Exact Name) আছে সেভাবেই ব্যবহার করুন। ইংরেজি নাম থাকলে ইংরেজিতেই লিখবেন।\n"
        "৩. গ্রাহক যখন পণ্যের তালিকা (List) চাইবে, তখন নিচের '[সংক্ষিপ্ত তালিকা]' থেকে শুধু নাম ও দাম দেখান। বিবরণ দেখাবেন না।\n"
        "৪. গ্রাহক যখন কোনো নির্দিষ্ট পণ্য নিয়ে প্রশ্ন করবে, তখন '[বিস্তারিত ডেটাবেস]' থেকে ওই পণ্যের বিবরণ হুবহু বলুন। নিজে থেকে কিছু বানিয়ে বলবেন না।\n"
        "৫. যতক্ষণ গ্রাহক স্পষ্টভাবে 'কিনতে চাই' বা 'অর্ডার দিন' বলছে না, ততক্ষণ অর্ডার সামারি দেখাবেন না।\n"
        "৬. গ্রাহক অর্ডার করতে চাইলে এবং নাম/ফোন/ঠিকানা সব থাকলে তবেই সামারি দেখান। সামারিতে পণ্যের দামের সাথে গ্রাহকের নাম, ফোন ও ঠিকানাও উল্লেখ করুন।\n"
        f"৭. {known_info_str} (গ্রাহক চাইলে এই তথ্য পরিবর্তন করতে পারে, তখন নতুন তথ্য নিয়ে আপডেট করুন)।\n"
        f"৮. তথ্যের বাইরে কিছু জানতে চাইলে কল করতে বলুন: {biz_phone}।\n"
        f"\n[সংক্ষিপ্ত তালিকা (List)]:\n{product_list_short}\n"
        f"\n[বিস্তারিত ডেটাবেস (Details)]:\n{product_details_full}\n"
        f"\n[FAQ]:\n{faq_text}\n\n[ডেলিভারি চার্জ]: ৳{delivery_charge}"
    )

    memory = get_chat_memory(user_id, customer_id)
    api_key_res = supabase.table("api_keys").select("groq_api_key").eq("user_id", user_id).execute()
    
    if not api_key_res.data:
        return "সিস্টেমের ত্রুটির কারণে উত্তর দেওয়া সম্ভব হচ্ছে না।", None

    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=api_key_res.data[0]["groq_api_key"])

    for i in range(max_retries):
        try:
            res = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": system_prompt}] + memory + [{"role": "user", "content": user_msg}],
                temperature=0.6 
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
            if "429" in str(e):
                if i < max_retries - 1:
                    time.sleep(20)
                    continue
                return "দুঃখিত, সার্ভার এখন খুব ব্যস্ত। দয়া করে কিছুক্ষণ পর আবার চেষ্টা করুন।", None
            return "দুঃখিত, প্রযুক্তিগত সমস্যা হচ্ছে। একটু পরে চেষ্টা করুন।", None

# ================= ORDER EXTRACTION =================
def extract_order_data_with_retry(user_id, messages, max_retries=2):
    api_key_res = supabase.table("api_keys").select("groq_api_key").eq("user_id", user_id).execute()
    if not api_key_res.data: return None
    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=api_key_res.data[0]["groq_api_key"])

    prompt = (
        "Extract order details from the conversation into JSON. "
        "Keys: name, phone, address, items (product_name, quantity). "
        "If a field is not found in the latest text, return null or empty string. "
        "Return ONLY JSON."
    )

    for i in range(max_retries):
        try:
            res = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": prompt}] + messages[-6:], 
                response_format={"type": "json_object"},
                temperature=0
            )
            # FIX: Markdown clean up (just in case AI adds ```json)
            content = res.choices[0].message.content
            cleaned_content = re.sub(r"```json|```", "", content).strip()
            
            return json.loads(cleaned_content)
        except Exception:
            if i < max_retries - 1: time.sleep(2)
            continue
    return None

# ================= WEBHOOK =================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data: return jsonify({"status": "error"}), 400

    # Quick 200 OK to Facebook to prevent retries
    if data.get("object") == "page":
        for entry in data.get("entry", []):
            page_id = entry.get("id")
            page = get_page_client(page_id)
            if not page: continue
            user_id, token = page["user_id"], page["page_access_token"]

            for msg_event in entry.get("messaging", []):
                sender = msg_event["sender"]["id"]
                if "message" not in msg_event: continue
                
                raw_text = msg_event["message"].get("text", "")
                if not raw_text: continue
                text = raw_text.lower().strip()

                # === SUBSCRIPTION CHECK POINT ===
                # এখানে চেক করা হচ্ছে সাবস্ক্রিপশন Active কিনা
                if not check_subscription_status(user_id):
                    # ফ্রেন্ডলি মেসেজ পাঠানো হচ্ছে
                    send_message(token, sender, "দুঃখিত, আমি এই মুহূর্তে উত্তর দিতে পারছি না। আমাদের পেজে দেওয়া ফোন নম্বরে যোগাযোগ করুন অথবা কিছুক্ষণ পর আবার চেষ্টা করুন।")
                    # পরবর্তী কাজ বন্ধ করে লুপ কন্টিনিউ
                    continue

                session_id = f"order_{user_id}_{sender}"
                current_session = get_session_from_db(session_id)
                if not current_session:
                    current_session = OrderSession(user_id, sender)

                memory = get_chat_memory(user_id, sender)
                temp_memory = memory + [{"role": "user", "content": raw_text}]
                extracted = extract_order_data_with_retry(user_id, temp_memory)
                
                if extracted:
                    if extracted.get("name"): current_session.data["name"] = extracted["name"]
                    if extracted.get("phone"): current_session.data["phone"] = extracted["phone"]
                    if extracted.get("address"): current_session.data["address"] = extracted["address"]
                    if extracted.get("items"):
                        current_session.data["items"] = extracted["items"]
                    save_session_to_db(current_session)

                # --- COMMAND HANDLING ---
                if "confirm" in text or "কনফার্ম" in text:
                    if current_session.data.get("name") and current_session.data.get("phone"):
                        products_db = get_products_with_details(user_id)
                        business = get_business_settings(user_id)
                        delivery_charge = business.get('delivery_charge', 60) if business else 60
                        
                        items_total = 0
                        summary_list = []
                        for item in current_session.data.get('items', []):
                            for p in products_db:
                                if item.get('product_name') and p.get('name') and item['product_name'].lower() in p['name'].lower():
                                    qty = int(item.get('quantity', 1))
                                    items_total += p['price'] * qty
                                    summary_list.append(f"{p['name']} x{qty}")
                                    break
                        
                        if items_total > 0:
                            current_session.data['delivery_charge'] = delivery_charge
                            current_session.data['total'] = items_total + delivery_charge 
                            current_session.data['product'] = ", ".join(summary_list)
                        
                            if current_session.save_order(product_total=items_total, delivery_charge=delivery_charge):
                                send_message(token, sender, "✅ আপনার অর্ডারটি সফলভাবে কনফার্ম করা হয়েছে! খুব শীঘ্রই আমরা আপনার সাথে যোগাযোগ করবো, আমাদের প্রতি আস্থা রাখার জন্য আপনাকে অসংখ্য ধন্যবাদ।")
                                delete_session_from_db(session_id)
                            else:
                                send_message(token, sender, "❌ অর্ডার প্রসেস করতে সমস্যা হয়েছে। আবার চেষ্টা করুন।")
                    else:
                        send_message(token, sender, "আপনার অর্ডার কনফার্ম করার জন্য নাম এবং ফোন নম্বর প্রয়োজন।")
                    continue

                if "cancel" in text or "বাতিল" in text:
                    delete_session_from_db(session_id)
                    send_message(token, sender, "আপনার অর্ডারটি বাতিল করা হয়েছে।")
                    continue

                # AI REPLY
                reply, product_image = generate_ai_reply_with_retry(user_id, sender, raw_text, current_session.data)
                if product_image:
                    send_image(token, sender, product_image)
                send_message(token, sender, reply)

    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

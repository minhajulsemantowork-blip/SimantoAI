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

# ================= SESSION DB HELPERS =================
class OrderSession:
    def __init__(self, admin_id: str, customer_id: str):
        self.admin_id = admin_id
        self.customer_id = customer_id
        self.session_id = f"order_{admin_id}_{customer_id}"
        self.step = 0 
        self.data = {"name": "", "phone": "", "product": "", "items": [], "address": "", "delivery_charge": 0, "total": 0}

    def save_order(self) -> bool:
        try:
            # Save final order to 'orders' table
            res = supabase.table("orders").insert({
                "user_id": self.admin_id,
                "customer_name": self.data.get("name"),
                "customer_phone": self.data.get("phone"),
                "product": self.data.get("product"), 
                "address": self.data.get("address"),
                "total": float(self.data.get("total", 0)),
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
            session = OrderSession(row['admin_id'], row['customer_id'])
            session.step = row['step']
            # Ensure data dict has all keys to avoid errors
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
            "admin_id": session.admin_id,
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
def get_products_with_details(admin_id: str):
    res = supabase.table("products").select("*").eq("user_id", admin_id).execute()
    return res.data or []

def get_faqs(admin_id: str):
    res = supabase.table("faqs").select("question, answer").eq("user_id", admin_id).execute()
    return res.data or []

def get_business_settings(admin_id: str) -> Optional[Dict]:
    res = supabase.table("business_settings").select("*").eq("user_id", admin_id).limit(1).execute()
    return res.data[0] if res.data else None

def get_chat_memory(admin_id: str, customer_id: str, limit: int = 10) -> List[Dict]:
    res = supabase.table("chat_history").select("messages").eq("user_id", admin_id).eq("customer_id", customer_id).limit(1).execute()
    return res.data[0].get("messages", [])[-limit:] if res.data else []

def save_chat_memory(admin_id: str, customer_id: str, messages: List[Dict]):
    now = datetime.utcnow().isoformat()
    existing = supabase.table("chat_history").select("id").eq("user_id", admin_id).eq("customer_id", customer_id).execute()
    if existing.data:
        supabase.table("chat_history").update({"messages": messages, "last_updated": now}).eq("id", existing.data[0]["id"]).execute()
    else:
        supabase.table("chat_history").insert({"user_id": admin_id, "customer_id": customer_id, "messages": messages, "created_at": now, "last_updated": now}).execute()

# ================= AI LOGIC (SMART CONTEXT) =================
def generate_ai_reply_with_retry(admin_id, customer_id, user_msg, current_session_data, max_retries=3):
    business = get_business_settings(admin_id)
    products = get_products_with_details(admin_id)
    faqs = get_faqs(admin_id)
    
    # Business Info
    biz_phone = business.get('contact_number', '') if business else ""
    business_name = business.get('name', 'আমাদের শপ') if business else "আমাদের শপ"
    delivery_charge = business.get('delivery_charge', 60) if business else 60

    product_text = "\n".join([f"- {p.get('name')}: ৳{p.get('price')}\n  বিবরণ: {p.get('description')}" for p in products if p.get("in_stock")])
    faq_text = "\n".join([f"Q: {f['question']} | A: {f['answer']}" for f in faqs])

    # === DYNAMIC STATUS CHECK ===
    # চেক করা হচ্ছে আমাদের কাছে ইতিমধ্যে কি কি তথ্য আছে
    has_name = bool(current_session_data.get("name"))
    has_phone = bool(current_session_data.get("phone"))
    has_address = bool(current_session_data.get("address"))
    
    known_info_str = ""
    if has_name or has_phone or has_address:
        known_info_str = f"প্রাপ্ত তথ্য - নাম: {current_session_data.get('name')}, ফোন: {current_session_data.get('phone')}, ঠিকানা: {current_session_data.get('address')}."

    missing_info_instruction = ""
    if not has_name or not has_phone or not has_address:
        missing_info_instruction = "যদি গ্রাহক অর্ডার করতে চায়, তবে বিনীতভাবে বাকি তথ্যগুলো (নাম/ফোন/ঠিকানা) দিন।"
    else:
        missing_info_instruction = "আপনার কাছে গ্রাহকের নাম, ফোন এবং ঠিকানা আছে। **নতুন করে আর চাইবেন না।** সরাসরি অর্ডার সামারি দেখান এবং কনফার্ম করতে বলুন।"

    # === SYSTEM PROMPT ===
    system_prompt = (
        f"আপনি '{business_name}'-এর একজন বন্ধুসুলভ এবং পেশাদার সেলস অ্যাসিস্ট্যান্ট।\n"
        "**আচরণবিধি:**\n"
        "১. সর্বদা মার্জিত ও প্রমিত বাংলা ব্যবহার করুন।\n"
        "২. শুরুতে অযথা নাম/ঠিকানা চাইবেন না। গ্রাহক যখন পণ্য কিনতে চাইবে, তখন তথ্য চাইবেন।\n"
        f"৩. {known_info_str}\n"
        f"৪. {missing_info_instruction}\n"
        "৫. যদি গ্রাহকের নাম, ফোন এবং ঠিকানা সব থাকে, তবে তাকে পূর্ণাঙ্গ অর্ডার সামারি (পণ্যের নাম, দাম, ডেলিভারি চার্জ সহ) দেখান এবং 'Confirm' লিখতে বলুন।\n"
        f"৬. যদি পণ্যের তথ্যের বাইরে কিছু জানতে চায় যা আপনার জানা নেই, তবে কল করতে বলুন: {biz_phone}।\n"
        f"\n[পণ্যের তালিকা]:\n{product_text}\n\n[FAQ]:\n{faq_text}\n\n[ডেলিভারি চার্জ]: ৳{delivery_charge}"
    )

    memory = get_chat_memory(admin_id, customer_id)
    api_key_res = supabase.table("api_keys").select("groq_api_key").eq("user_id", admin_id).execute()
    
    if not api_key_res.data:
        return "সিস্টেমের ত্রুটির কারণে উত্তর দেওয়া সম্ভব হচ্ছে না।", None

    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=api_key_res.data[0]["groq_api_key"])

    for i in range(max_retries):
        try:
            res = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": system_prompt}] + memory + [{"role": "user", "content": user_msg}],
                temperature=0.6 
            )
            reply = res.choices[0].message.content.strip()
            save_chat_memory(admin_id, customer_id, (memory + [{"role": "user", "content": user_msg}, {"role": "assistant", "content": reply}])[-10:])
            
            matched_image = None
            for p in products:
                if p.get('name') and p.get('name').lower() in reply.lower() and p.get('image_url'):
                    matched_image = p.get('image_url')
                    break
            
            return reply, matched_image

        except Exception as e:
            if "429" in str(e) and i < max_retries - 1:
                time.sleep(30)
                continue
            return "দুঃখিত, সার্ভার ব্যস্ত। একটু পরে চেষ্টা করুন।", None

# ================= ORDER EXTRACTION =================
def extract_order_data_with_retry(admin_id, messages, max_retries=2):
    api_key_res = supabase.table("api_keys").select("groq_api_key").eq("user_id", admin_id).execute()
    if not api_key_res.data: return None
    
    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=api_key_res.data[0]["groq_api_key"])

    # এই প্রম্পটটি শুধুমাত্র নতুন তথ্য খুঁজে বের করবে
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
            return json.loads(res.choices[0].message.content)
        except Exception:
            if i < max_retries - 1: time.sleep(2)
            continue
    return None

# ================= WEBHOOK =================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data: return jsonify({"status": "error"}), 400

    for entry in data.get("entry", []):
        page_id = entry.get("id")
        page = get_page_client(page_id)
        if not page: continue
        admin_id, token = page["user_id"], page["page_access_token"]

        for msg_event in entry.get("messaging", []):
            sender = msg_event["sender"]["id"]
            if "message" not in msg_event: continue
            
            raw_text = msg_event["message"].get("text", "")
            if not raw_text: continue
            text = raw_text.lower().strip()

            session_id = f"order_{admin_id}_{sender}"
            # 1. সেশন বা মেমোরি আগেই লোড করা হচ্ছে
            current_session = get_session_from_db(session_id)
            
            # যদি সেশন না থাকে, তবে একটি খালি সেশন অবজেক্ট তৈরি করি (ডাটাবেসে সেভ না করে)
            # যাতে AI কে খালি ডাটা পাঠানো যায়
            if not current_session:
                current_session = OrderSession(admin_id, sender)

            # --- DATA EXTRACTION (প্রতি মেসেজেই চেক করবে নতুন ইনফো আছে কিনা) ---
            # এটি AI রিপ্লাই জেনারেট করার আগেই রান হবে, যাতে AI আপডেটেড তথ্য পায়
            memory = get_chat_memory(admin_id, sender)
            
            # মেমোরিতে বর্তমান মেসেজটি টেম্পোরারি যোগ করে এক্সট্র্যাক্ট করা
            temp_memory = memory + [{"role": "user", "content": raw_text}]
            extracted = extract_order_data_with_retry(admin_id, temp_memory)
            
            if extracted:
                # শুধু নতুন পাওয়া তথ্যগুলো আপডেট হবে, পুরনো তথ্য মুছবে না
                if extracted.get("name"): current_session.data["name"] = extracted["name"]
                if extracted.get("phone"): current_session.data["phone"] = extracted["phone"]
                if extracted.get("address"): current_session.data["address"] = extracted["address"]
                
                # আইটেম আপডেট লজিক
                if extracted.get("items"):
                    # সাধারণ লজিক: নতুন আইটেম আসলে রিপ্লেস করা (চাইলে অ্যাপেন্ড করা যায়)
                    current_session.data["items"] = extracted["items"]
                
                # সেশন আপডেট করা (কিন্তু স্টেপ ১ না হওয়া পর্যন্ত কনফার্ম নয়)
                save_session_to_db(current_session)


            # --- COMMAND HANDLING ---
            if "confirm" in text or "কনফার্ম" in text:
                if current_session.data.get("name") and current_session.data.get("phone"):
                    
                    # টোটাল প্রাইস ক্যালকুলেশন (Confirm এর সময় ফাইনাল চেক)
                    products_db = get_products_with_details(admin_id)
                    business = get_business_settings(admin_id)
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
                    
                    if current_session.save_order():
                        send_message(token, sender, "✅ আপনার অর্ডারটি সফলভাবে কনফার্ম করা হয়েছে! ধন্যবাদ।")
                        delete_session_from_db(session_id)
                    else:
                        send_message(token, sender, "❌ অর্ডার প্রসেস করতে সমস্যা হয়েছে। আবার চেষ্টা করুন।")
                else:
                    send_message(token, sender, "আপনার অর্ডার কনফার্ম করার জন্য নাম এবং ফোন নম্বর প্রয়োজন।")
                continue

            if "cancel" in text or "বাতিল" in text:
                delete_session_from_db(session_id)
                send_message(token, sender, "আপনার অর্ডারটি বাতিল করা হয়েছে।")
                continue

            # --- AI REPLY GENERATION ---
            # আপডেটেড সেশন ডাটা পাঠানো হচ্ছে, যাতে AI জানে কি কি তথ্য আছে
            reply, product_image = generate_ai_reply_with_retry(admin_id, sender, raw_text, current_session.data)
            
            if product_image:
                send_image(token, sender, product_image)
            
            send_message(token, sender, reply)

    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

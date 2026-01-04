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

processed_messages = set()

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
        res = supabase.table("subscriptions").select("status").eq("user_id", user_id).execute()
        if res.data and len(res.data) > 0:
            status = res.data[0].get("status")
            if status == "active":
                return True
        return False
    except Exception as e:
        logger.error(f"Subscription Check Error for user {user_id}: {e}")
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
                "total": float(product_total + delivery_charge),
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

# ================= AI LOGIC =================
def generate_ai_reply_with_retry(user_id, customer_id, user_msg, current_session_data, max_retries=2):
    business = get_business_settings(user_id)
    products = get_products_with_details(user_id)
    faqs = get_faqs(user_id)
    
    biz_phone = business.get('contact_number', '') if business else ""
    business_name = business.get('name', 'আমাদের শপ') if business else "আমাদের শপ"
    business_address = business.get('address', 'ঠিকানা উপলব্ধ নয়') if business else "ঠিকানা উপলব্ধ নয়"
    delivery_charge = business.get('delivery_charge', 60) if business else 60

    product_list_short = "\n".join([f"- {p.get('name')}: ৳{p.get('price')}" for p in products if p.get("in_stock")])
    product_details_full = "\n".join([f"পণ্য: {p.get('name')}\nদাম: ৳{p.get('price')}\nবিবরণ: {p.get('description')}" for p in products if p.get("in_stock")])
    faq_text = "\n".join([f"Q: {f['question']} | A: {f['answer']}" for f in faqs])

    known_info_str = f"প্রাপ্ত তথ্য - নাম: {current_session_data.get('name', 'নেই')}, ফোন: {current_session_data.get('phone', 'নেই')}, ঠিকানা: {current_session_data.get('address', 'নেই')}."

    system_prompt = f"""
তুমি '{business_name}'-এর একজন বাস্তব মানুষ, একজন অভিজ্ঞ সেলস এক্সিকিউটিভ।
তুমি কোনো AI, bot বা সফটওয়্যার নও—এমন কিছু কখনো বলবে না।

তোমার কথা বলার ধরন:
- খুব স্বাভাবিক, কথ্য বাংলা
- ছোট ও পরিষ্কার বাক্য
- WhatsApp / Messenger এ যেভাবে মানুষ কথা বলে সেভাবে
- বেশি formal বা বইয়ের ভাষা ব্যবহার করবে না

আচরণ:
- আগে গ্রাহকের কথা বুঝবে
- এক উত্তরে বেশি তথ্য দেবে না
- দরকার হলে পাল্টা প্রশ্ন করবে
- “আচ্ছা”, “ঠিক আছে”, “বুঝেছি”, “একটু দেখি”—এগুলো ব্যবহার করবে
- মানুষ যেমন থেমে থেমে কথা বলে, তেমন ফিল থাকবে

পণ্য সংক্রান্ত নিয়ম:
- পণ্যের নাম ডাটাবেসে যেভাবে আছে, ঠিক সেভাবেই ব্যবহার করবে
- লিস্ট চাইলে শুধু নাম ও দাম দেখাবে
- নির্দিষ্ট পণ্য জিজ্ঞেস করলে নিজের ভাষায় সুন্দর করে বোঝাবে
- কখনো কপি-পেস্ট করবে না

অর্ডার আচরণ:
- গ্রাহক নিজে না চাইলে অর্ডার চাপাবে না
- নাম, ফোন, ঠিকানা পেলে সংক্ষেপে অর্ডার সামারি দেখাবে
- স্পষ্ট করে বলবে: অর্ডার নিশ্চিত করতে “Confirm / কনফার্ম” লিখতে হবে
- Confirm না পাওয়া পর্যন্ত অর্ডার সেভ হবে না

জানা তথ্য:
{known_info_str}

প্রয়োজনের বাইরে কিছু চাইলে বলবে:
কল করুন: {biz_phone}

ডেলিভারি চার্জ: ৳{delivery_charge}

পণ্য তালিকা:
{product_list_short}

পণ্যের বিস্তারিত:
{product_details_full}

FAQ:
{faq_text}

সব উত্তর ২–৪ লাইনের মধ্যে রাখবে।
"""
    
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
            logger.error(f"AI Generation Error: {e}")
            if i == max_retries - 1:
                return "দুঃখিত, এই মুহুর্তে আমরা উত্তর দিতে পারছিনা। অনুগ্রহ করে কল করুন।", None
            
    return "দুঃখিত, এই মুহুর্তে আমরা উত্তর দিতে পারছিনা।", None

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
    # --- Facebook Webhook Verification ---
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        # Render এর এনভায়রনমেন্ট থেকে VERIFY_TOKEN রিড করা হচ্ছে
        verify_token = os.getenv("VERIFY_TOKEN")
        
        if mode == "subscribe" and token == verify_token:
            logger.info("Webhook verified successfully!")
            return challenge, 200
        return "Verification failed", 403

    # --- Handle Messenger Messages ---
    data = request.get_json()
    if not data: return jsonify({"status": "error"}), 400

    if data.get("object") == "page":
        for entry in data.get("entry", []):
            page_id = entry.get("id")
            page = get_page_client(page_id)
            if not page: continue
            user_id, token = page["user_id"], page["page_access_token"]

            for msg_event in entry.get("messaging", []):
                sender = msg_event["sender"]["id"]
                if "message" not in msg_event: continue
                
                msg_id = msg_event["message"].get("mid")
                if msg_id in processed_messages: continue
                if msg_id: processed_messages.add(msg_id)

                raw_text = msg_event["message"].get("text", "")
                if not raw_text: continue
                text = raw_text.lower().strip()

                if not check_subscription_status(user_id):
                    send_message(token, sender, "দুঃখিত, সার্ভিসটি বর্তমানে বন্ধ আছে।")
                    continue

                session_id = f"order_{user_id}_{sender}"
                current_session = get_session_from_db(session_id)
                if not current_session:
                    current_session = OrderSession(user_id, sender)

                # সেশনে ডাটা আপডেট
                memory = get_chat_memory(user_id, sender)
                temp_memory = memory + [{"role": "user", "content": raw_text}]
                extracted = extract_order_data_with_retry(user_id, temp_memory)
                
                if extracted:
                    if extracted.get("name"): current_session.data["name"] = extracted["name"]
                    if extracted.get("phone"): current_session.data["phone"] = extracted["phone"]
                    if extracted.get("address"): current_session.data["address"] = extracted["address"]
                    if extracted.get("items"): current_session.data["items"] = extracted["items"]
                    save_session_to_db(current_session)

                # --- COMMAND HANDLING (Strict Confirmation) ---
                if "confirm" in text or "কনফার্ম" in text:
                    s_data = current_session.data
                    if s_data.get("name") and s_data.get("phone") and s_data.get("address") and s_data.get("items"):
                        products_db = get_products_with_details(user_id)
                        business = get_business_settings(user_id)
                        delivery_charge = business.get('delivery_charge', 60) if business else 60
                        
                        items_total = 0
                        summary_list = []
                        for item in s_data.get('items', []):
                            for p in products_db:
                                if item.get('product_name') and p.get('name') and item['product_name'].lower() in p['name'].lower():
                                    qty = int(item.get('quantity', 1))
                                    items_total += p['price'] * qty
                                    summary_list.append(f"{p['name']} x{qty}")
                                    break
                        
                        if items_total > 0:
                            current_session.data['product'] = ", ".join(summary_list)
                            if current_session.save_order(product_total=items_total, delivery_charge=delivery_charge):
                                send_message(token, sender, "✅ অভিনন্দন! আপনার অর্ডারটি সফলভাবে গ্রহণ করা হয়েছে। খুব শীঘ্রই আপনার সাথে যোগাযোগ করা হবে।")
                                delete_session_from_db(session_id)
                            else:
                                send_message(token, sender, "❌ কারিগরি সমস্যার কারণে অর্ডারটি সেভ করা যায়নি।")
                        else:
                            send_message(token, sender, "আপনার কার্টে কোনো সঠিক পণ্য পাওয়া যায়নি।")
                    else:
                        send_message(token, sender, "অর্ডার কনফার্ম করতে নাম, ফোন নম্বর এবং ঠিকানা প্রয়োজন। অনুগ্রহ করে তথ্যগুলো দিন।")
                    continue

                if "cancel" in text or "বাতিল" in text:
                    delete_session_from_db(session_id)
                    send_message(token, sender, "আপনার অর্ডার সেশনটি বাতিল করা হয়েছে।")
                    continue

                # AI রিপ্লাই (ইউজারকে সামারি দেখাবে এবং কনফার্ম করতে বলবে)
                reply, product_image = generate_ai_reply_with_retry(user_id, sender, raw_text, current_session.data)
                if product_image:
                    send_image(token, sender, product_image)
                send_message(token, sender, reply)

    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

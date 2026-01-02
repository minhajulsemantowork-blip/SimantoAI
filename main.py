import os
import re
import logging
import requests
import json
import time
from typing import Optional, Dict, Tuple, List
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
def get_session_from_db(session_id: str) -> Optional["OrderSession"]:
    try:
        res = supabase.table("order_sessions").select("*").eq("id", session_id).execute()
        if res.data:
            row = res.data[0]
            session = OrderSession(row['admin_id'], row['customer_id'])
            session.step = row['step']
            session.data = row['data']
            return session
    except Exception as e:
        logger.error(f"Error getting session: {e}")
    return None

def save_session_to_db(session: "OrderSession"):
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
    """ফেসবুকে প্রোডাক্টের ছবি পাঠানোর ফাংশন"""
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
    # products টেবিল থেকে image_url সহ সব তথ্য আনা হচ্ছে
    res = supabase.table("products").select("*").eq("user_id", admin_id).execute()
    return res.data or []

def get_faqs(admin_id: str):
    res = supabase.table("faqs").select("question, answer").eq("user_id", admin_id).execute()
    return res.data or []

def get_business_settings(admin_id: str) -> Optional[Dict]:
    res = supabase.table("business_settings").select("*").eq("user_id", admin_id).limit(1).execute()
    return res.data[0] if res.data else None

# ================= CHAT MEMORY =================
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

# ================= AI LOGIC (PROMITO BANGLA & RETRY) =================
def generate_ai_reply_with_retry(admin_id, customer_id, user_msg, max_retries=3):
    """
    Groq Free API Rate Limit হ্যান্ডেল করার জন্য রি-ট্রাই লজিক।
    """
    business = get_business_settings(admin_id)
    products = get_products_with_details(admin_id)
    faqs = get_faqs(admin_id)
    
    # ফোন নম্বর সেটআপ
    biz_phone = business.get('phone_number', '') if business else ""
    contact_instruction = f"প্রয়োজনে আমাদের হটলাইন নম্বরে কল করুন: {biz_phone}।" if biz_phone else "প্রয়োজনে সরাসরি আমাদের পেইজে কল করুন।"

    # প্রোডাক্ট এবং FAQ টেক্সট তৈরি
    product_text = "\n".join([
        f"- পণ্যের নাম (Exact Name): {p.get('name')}\n  ক্যাটাগরি: {p.get('category')}\n  মূল্য: ৳{p.get('price')}\n  বিবরণ: {p.get('description')}" 
        for p in products if p.get("in_stock")
    ])
    
    faq_text = "\n".join([f"প্রশ্ন: {f['question']} | উত্তর: {f['answer']}" for f in faqs])
    
    delivery_charge = business.get('delivery_charge', 60) if business else 60
    business_name = business.get('name', 'আমাদের শপ') if business else "আমাদের শপ"

    # === সিস্টেম প্রম্পট (প্রমিত বাংলা) ===
    system_prompt = (
        f"আপনি '{business_name}'-এর একজন অত্যন্ত দক্ষ, বিনয়ী এবং পেশাদার বিক্রয় প্রতিনিধি।\n"
        "আপনার প্রধান লক্ষ্য গ্রাহককে সন্তুষ্ট করা এবং পণ্য কিনতে সহায়তা করা।\n\n"
        "কঠোর নির্দেশাবলী:\n"
        "১. সর্বদা প্রমিত বাংলায় (Standard Bangla) কথা বলুন। আঞ্চলিকতা পরিহার করুন। গ্রাহককে সর্বদা 'আপনি' বলে সম্বোধন করবেন।\n"
        f"২. যদি গ্রাহক সরাসরি কথা বলতে চায় বা জরুরি প্রয়োজন হয়, তবে এই নির্দেশ দিন: '{contact_instruction}'\n"
        "৩. পণ্যের নাম ডাটাবেসে যেভাবে ইংরেজিতে বা বাংলায় আছে, হুবহু সেভাবেই বলবেন (কোনো অনুবাদ করবেন না)।\n"
        "৪. শুধুমাত্র নিচে দেওয়া 'পণ্যের বিবরণ' এবং 'FAQ' থেকে তথ্য নিয়ে উত্তর দিন। এর বাইরে নিজের থেকে কোনো তথ্য বানাবেন না।\n"
        "৫. গ্রাহক কোনো নির্দিষ্ট পণ্যের বিস্তারিত জানতে চাইলে, সেই পণ্যটির সঠিক নাম উল্লেখ করে বিস্তারিত সুন্দর করে গুছিয়ে বলুন।\n"
        "৬. অর্ডার কনফার্ম করার জন্য গ্রাহকের নাম, ফোন নম্বর এবং সম্পূর্ণ ঠিকানা বিনয়ের সাথে জানতে চান।\n"
        f"\n[পণ্যের তালিকা ও বিবরণ]:\n{product_text}\n\n[সচরাচর জিজ্ঞাসিত প্রশ্ন (FAQ)]:\n{faq_text}\n\n[ডেলিভারি চার্জ]: ৳{delivery_charge}"
    )

    memory = get_chat_memory(admin_id, customer_id)
    api_key_res = supabase.table("api_keys").select("groq_api_key").eq("user_id", admin_id).execute()
    
    if not api_key_res.data:
        return "দুঃখিত, সিস্টেমের একটি ত্রুটির কারণে আমি এখন উত্তর দিতে পারছি না।", None

    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=api_key_res.data[0]["groq_api_key"])

    # === RETRY LOOP ===
    for i in range(max_retries):
        try:
            res = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": system_prompt}] + memory + [{"role": "user", "content": user_msg}],
                temperature=0.7 
            )
            
            reply = res.choices[0].message.content.strip()
            
            # মেমোরি সেভ করা
            save_chat_memory(admin_id, customer_id, (memory + [{"role": "user", "content": user_msg}, {"role": "assistant", "content": reply}])[-10:])
            
            # প্রোডাক্টের ইমেজ খোঁজা (Semantic check)
            matched_image = None
            for p in products:
                # যদি বটের রিপ্লাইতে প্রোডাক্টের নাম থাকে এবং সেই প্রোডাক্টের ইমেজ থাকে
                if p.get('name') and p.get('name').lower() in reply.lower() and p.get('image_url'):
                    matched_image = p.get('image_url')
                    break
            
            return reply, matched_image

        except Exception as e:
            logger.warning(f"AI Attempt {i+1} failed: {e}")
            if "429" in str(e): # Rate Limit Error
                if i < max_retries - 1:
                    # এখানে ৬০ সেকেন্ড অপেক্ষা করা হচ্ছে (Groq এর লিমিট ১ মিনিটে রিসেট হয়)
                    # ২ মিনিট অপেক্ষা করলে Facebook Webhook Timeout হয়ে যাবে, তাই ৬০ সেকেন্ড নিরাপদ।
                    time.sleep(60) 
                    continue
            
            if i == max_retries - 1:
                return "দুঃখিত, এই মুহূর্তে আমাদের সার্ভার ব্যস্ত আছে। দয়া করে কিছুক্ষণ পর আবার চেষ্টা করুন।", None

    return "দুঃখিত, একটি সমস্যা হয়েছে।", None

# ================= ORDER EXTRACTION (RETRY ADDED) =================
def extract_order_data_with_retry(admin_id, messages, max_retries=2):
    api_key_res = supabase.table("api_keys").select("groq_api_key").eq("user_id", admin_id).execute()
    if not api_key_res.data: return None
    
    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=api_key_res.data[0]["groq_api_key"])

    prompt = (
        "Extract order details into JSON. Keys: name, phone, address, items (product_name, quantity). "
        "Product name MUST match exactly from context."
    )

    for i in range(max_retries):
        try:
            res = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": prompt}] + messages[-8:],
                response_format={"type": "json_object"},
                temperature=0
            )
            return json.loads(res.choices[0].message.content)
        except Exception:
            if i < max_retries - 1: time.sleep(2)
            continue
    return None

# ================= ORDER SESSION CLASS =================
class OrderSession:
    def __init__(self, admin_id: str, customer_id: str):
        self.admin_id = admin_id
        self.customer_id = customer_id
        self.session_id = f"order_{admin_id}_{customer_id}"
        self.step = 0 
        self.data = {"name": "", "phone": "", "product": "", "items": [], "address": "", "delivery_charge": 0, "total": 0}

    def save_order(self) -> bool:
        try:
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
        except Exception: return False

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
            current_session = get_session_from_db(session_id)

            # --- COMMAND HANDLING ---
            if text == "confirm":
                if current_session and current_session.save_order():
                    send_message(token, sender, "✅ আপনার অর্ডারটি সফলভাবে কনফার্ম করা হয়েছে! ধন্যবাদ।")
                    delete_session_from_db(session_id)
                else:
                    send_message(token, sender, "❌ কোনো একটিভ অর্ডার পাওয়া যায়নি।")
                continue

            if text == "cancel":
                delete_session_from_db(session_id)
                send_message(token, sender, "আপনার অর্ডারটি বাতিল করা হয়েছে।")
                continue

            # --- AI REPLY GENERATION ---
            reply, product_image = generate_ai_reply_with_retry(admin_id, sender, raw_text)
            
            # ১. আগে ইমেজ পাঠানো (যদি থাকে)
            if product_image:
                send_image(token, sender, product_image)
            
            # ২. তারপর টেক্সট রিপ্লাই পাঠানো
            send_message(token, sender, reply)

            # --- AUTO EXTRACTION LOGIC ---
            # বট যদি কনফার্ম করতে বলে, তখন আমরা ডাটা এক্সট্র্যাক্ট করার চেষ্টা করব
            if "confirm" in reply.lower() or "অর্ডার" in reply:
                memory = get_chat_memory(admin_id, sender)
                extracted = extract_order_data_with_retry(admin_id, memory)
                
                if extracted and extracted.get("name") and extracted.get("phone"):
                    business = get_business_settings(admin_id)
                    products_db = get_products_with_details(admin_id)
                    delivery_charge = business.get('delivery_charge', 60) if business else 60
                    
                    new_session = OrderSession(admin_id, sender)
                    new_session.data.update(extracted)
                    
                    items_total = 0
                    summary_list = []
                    
                    # প্রাইস ক্যালকুলেশন
                    for item in extracted.get('items', []):
                        for p in products_db:
                            # নাম ম্যাচিং (Case insensitive)
                            if item['product_name'].lower() in p['name'].lower():
                                line_total = p['price'] * int(item.get('quantity', 1))
                                items_total += line_total
                                summary_list.append(f"{p['name']} x{item['quantity']}")
                                break
                    
                    if items_total > 0:
                        new_session.data['delivery_charge'] = delivery_charge
                        new_session.data['total'] = items_total + delivery_charge
                        new_session.data['product'] = ", ".join(summary_list)
                        new_session.step = 1
                        save_session_to_db(new_session)

    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

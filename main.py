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

# ================= AI LOGIC (REVISED FLOW) =================
def generate_ai_reply_with_retry(admin_id, customer_id, user_msg, max_retries=3):
    business = get_business_settings(admin_id)
    products = get_products_with_details(admin_id)
    faqs = get_faqs(admin_id)
    
    # Contact Number Logic (Updated Column Name)
    biz_phone = business.get('contact_number', '') if business else ""
    
    # Product List & FAQ
    product_text = "\n".join([f"- {p.get('name')}: ৳{p.get('price')}\n  বিবরণ: {p.get('description')}" for p in products if p.get("in_stock")])
    faq_text = "\n".join([f"Q: {f['question']} | A: {f['answer']}" for f in faqs])
    business_name = business.get('name', 'আমাদের শপ') if business else "আমাদের শপ"
    delivery_charge = business.get('delivery_charge', 60) if business else 60

    # === SYSTEM PROMPT ===
    system_prompt = (
        f"আপনি '{business_name}'-এর একজন বন্ধুসুলভ এবং পেশাদার সেলস অ্যাসিস্ট্যান্ট।\n"
        "আপনার কাজ হলো গ্রাহকের প্রশ্নের উত্তর দেওয়া এবং পণ্য বিক্রয়ে সহায়তা করা।\n\n"
        "**আচরণবিধি (কঠোরভাবে পালন করবেন):**\n"
        "১. সর্বদা মার্জিত এবং প্রমিত বাংলা ব্যবহার করুন।\n"
        "২. শুরুতে কখনোই গ্রাহকের নাম, ফোন বা ঠিকানা চাইবেন না।\n"
        "৩. যদি গ্রাহক কোনো পণ্য সম্পর্কে জানতে চায়, তবে ডাটাবেস থেকে তথ্য দিন।\n"
        "৪. শুধুমাত্র যখন গ্রাহক পণ্যটি **কিনতে চাইবে** বা **অর্ডার করতে চাইবে**, তখন জিজ্ঞেস করুন: 'আপনি কি এটি অর্ডার করতে চান?'\n"
        "৫. গ্রাহক যদি অর্ডার করতে সম্মতি দেয় (যেমন: হ্যাঁ/Hae/Order korbo), তখনই শুধুমাত্র নাম, ফোন নম্বর এবং ঠিকানা চাইবেন।\n"
        f"৬. যদি গ্রাহকের প্রশ্নের উত্তর [পণ্যের তালিকা] বা [FAQ]-তে না থাকে, তবে বিনীতভাবে বলুন যে এই তথ্যটি আপনার জানা নেই এবং প্রয়োজনে এই নম্বরে কল করতে বলুন: {biz_phone}। (এই নম্বরটি অন্য কোনো ক্ষেত্রে অযথা দেবেন না)।\n"
        "৭. অর্ডার কনফার্ম করার জন্য সব তথ্য (নাম, ফোন, ঠিকানা) পাওয়ার পর একটি সুন্দর অর্ডার সামারি দিন এবং শেষে বলুন: 'অর্ডারটি কনফার্ম করতে Confirm লিখুন।'\n"
        f"\n[পণ্যের তালিকা]:\n{product_text}\n\n[FAQ]:\n{faq_text}\n\n[ডেলিভারি চার্জ]: ৳{delivery_charge}"
    )

    memory = get_chat_memory(admin_id, customer_id)
    api_key_res = supabase.table("api_keys").select("groq_api_key").eq("user_id", admin_id).execute()
    
    if not api_key_res.data:
        return "সিস্টেমের ত্রুটির কারণে উত্তর দেওয়া সম্ভব হচ্ছে না।", None

    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=api_key_res.data[0]["groq_api_key"])

    # === RETRY LOGIC ===
    for i in range(max_retries):
        try:
            res = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": system_prompt}] + memory + [{"role": "user", "content": user_msg}],
                temperature=0.6 
            )
            
            reply = res.choices[0].message.content.strip()
            
            # Memory Save
            save_chat_memory(admin_id, customer_id, (memory + [{"role": "user", "content": user_msg}, {"role": "assistant", "content": reply}])[-10:])
            
            # Image Check
            matched_image = None
            for p in products:
                if p.get('name') and p.get('name').lower() in reply.lower() and p.get('image_url'):
                    matched_image = p.get('image_url')
                    break
            
            return reply, matched_image

        except Exception as e:
            logger.warning(f"AI Attempt {i+1} failed: {e}")
            if "429" in str(e): # Rate Limit
                if i < max_retries - 1:
                    time.sleep(30) # Wait 30s
                    continue
            
            if i == max_retries - 1:
                return "দুঃখিত, সার্ভার ব্যস্ত থাকায় উত্তর দিতে পারছি না। একটু পরে চেষ্টা করুন।", None

    return "দুঃখিত, সমস্যা হয়েছে।", None

# ================= ORDER EXTRACTION =================
def extract_order_data_with_retry(admin_id, messages, max_retries=2):
    api_key_res = supabase.table("api_keys").select("groq_api_key").eq("user_id", admin_id).execute()
    if not api_key_res.data: return None
    
    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=api_key_res.data[0]["groq_api_key"])

    prompt = (
        "Extract order details into JSON. Keys: name, phone, address, items (product_name, quantity). "
        "Return ONLY JSON."
    )

    for i in range(max_retries):
        try:
            res = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": prompt}] + messages[-6:], # Last 6 messages enough for context
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
        except Exception as e:
            logger.error(f"DB Save Error: {e}")
            return False

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

            # --- CONFIRM / CANCEL COMMANDS ---
            if "confirm" in text or "কনফার্ম" in text:
                if current_session and current_session.data.get("name"): # Check if data exists
                    if current_session.save_order():
                        send_message(token, sender, "✅ আপনার অর্ডারটি সফলভাবে কনফার্ম করা হয়েছে! ধন্যবাদ আমাদের সাথে থাকার জন্য।")
                        delete_session_from_db(session_id)
                    else:
                        send_message(token, sender, "❌ অর্ডার সেভ করতে সমস্যা হয়েছে। দয়া করে আবার চেষ্টা করুন।")
                else:
                    # যদি সেশন না থাকে কিন্তু ইউজার কনফার্ম লেখে
                    send_message(token, sender, "আপনার কোনো অর্ডার প্রসেসিংয়ে নেই। নতুন করে অর্ডার করতে পণ্যের নাম বলুন।")
                continue

            if "cancel" in text or "বাতিল" in text:
                delete_session_from_db(session_id)
                send_message(token, sender, "আপনার অর্ডারটি বাতিল করা হয়েছে।")
                continue

            # --- AI REPLY ---
            reply, product_image = generate_ai_reply_with_retry(admin_id, sender, raw_text)
            
            # Send Image First (if any)
            if product_image:
                send_image(token, sender, product_image)
            
            # Send Text Reply
            send_message(token, sender, reply)

            # --- DATA EXTRACTION & SESSION UPDATE ---
            # বট যদি কনফার্ম করতে বলে, তার মানে ইউজারের সব তথ্য (নাম, ফোন, ঠিকানা) দেওয়া শেষ।
            # অথবা ইউজার যদি নিজেই নাম/ঠিকানা দেয়।
            
            memory = get_chat_memory(admin_id, sender)
            extracted = extract_order_data_with_retry(admin_id, memory)
            
            if extracted and extracted.get("name") and extracted.get("phone") and extracted.get("address"):
                business = get_business_settings(admin_id)
                products_db = get_products_with_details(admin_id)
                delivery_charge = business.get('delivery_charge', 60) if business else 60
                
                new_session = OrderSession(admin_id, sender)
                new_session.data.update(extracted)
                
                items_total = 0
                summary_list = []
                
                # Calculate Total
                for item in extracted.get('items', []):
                    for p in products_db:
                        if item['product_name'] and p['name'] and item['product_name'].lower() in p['name'].lower():
                            qty = int(item.get('quantity', 1))
                            items_total += p['price'] * qty
                            summary_list.append(f"{p['name']} x{qty}")
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

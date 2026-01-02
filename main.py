import os
import re
import json
import logging
import requests
from typing import Optional, Dict, Tuple, List
from datetime import datetime
from flask import Flask, request, jsonify
from openai import OpenAI
from supabase import create_client, Client
from difflib import SequenceMatcher

# ================= CONFIG =================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)

# ================= SUPABASE =================
try:
    supabase: Client = create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_SERVICE_KEY")
    )
except Exception as e:
    logger.error(f"Supabase Client Error: {e}")

# ================= HELPERS =================
def get_page_client(page_id):
    try:
        res = supabase.table("facebook_integrations").select("*").eq("page_id", str(page_id)).eq("is_connected", True).execute()
        return res.data[0] if res.data else None
    except: return None

def send_message(token, user_id, text):
    try:
        url = f"https://graph.facebook.com/v18.0/me/messages?access_token={token}"
        requests.post(url, json={"recipient": {"id": user_id}, "message": {"text": text}}).raise_for_status()
    except Exception as e: logger.error(f"Facebook API Error: {e}")

def get_products_with_details(admin_id: str):
    try:
        res = supabase.table("products").select("id, name, price, stock, category, description, in_stock").eq("user_id", admin_id).execute()
        return res.data or []
    except Exception as e: return []

def find_faq(admin_id: str, user_msg: str) -> Optional[str]:
    try:
        res = supabase.table("faqs").select("question, answer").eq("user_id", admin_id).execute()
        faqs = res.data or []
        best_ratio = 0
        best_answer = None
        for faq in faqs:
            ratio = SequenceMatcher(None, user_msg.lower(), faq["question"].lower()).ratio()
            if ratio > best_ratio and ratio > 0.65:
                best_ratio = ratio
                best_answer = faq["answer"]
        return best_answer
    except: return None

def get_business_settings(admin_id: str) -> Optional[Dict]:
    try:
        res = supabase.table("business_settings").select("*").eq("user_id", admin_id).limit(1).execute()
        return res.data[0] if res.data else None
    except: return None

# ================= CHAT MEMORY =================
def get_chat_memory(admin_id: str, customer_id: str, limit: int = 15) -> List[Dict]:
    try:
        res = supabase.table("chat_history").select("messages").eq("user_id", admin_id).eq("customer_id", customer_id).limit(1).execute()
        if res.data and isinstance(res.data[0].get("messages"), list):
            return res.data[0].get("messages")[-limit:]
    except: pass
    return []

def save_chat_memory(admin_id: str, customer_id: str, messages: List[Dict]):
    try:
        now = datetime.utcnow().isoformat()
        supabase.table("chat_history").upsert({
            "user_id": admin_id, "customer_id": customer_id, "messages": messages, "last_updated": now
        }, on_conflict="user_id,customer_id").execute()
    except Exception as e: logger.error(f"Memory Error: {e}")

# ================= AI SMART ORDERING =================
def process_ai_smart_order(admin_id, customer_id, user_msg):
    try:
        res = supabase.table("api_keys").select("groq_api_key").eq("user_id", admin_id).execute()
        if not res.data: return "দুঃখিত, বর্তমানে আমাদের সিস্টেমে কিছু কারিগরি সমস্যা হচ্ছে। অনুগ্রহ করে কিছুক্ষণ পর চেষ্টা করুন।"
        client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=res.data[0]["groq_api_key"])

        business = get_business_settings(admin_id)
        products = get_products_with_details(admin_id)
        history = get_chat_memory(admin_id, customer_id)
        
        # ডেসক্রিপশন সহ ইনভেন্টরি তৈরি
        product_info = ""
        for p in products:
            product_info += f"- পণ্য: {p['name']}, মূল্য: ৳{p['price']}, স্টক: {p.get('stock', 0)}, বিস্তারিত বর্ণনা: {p.get('description', 'উপলব্ধ নেই')}\n"
        
        system_prompt = f"""
        তুমি Simanto, একজন অত্যন্ত মার্জিত এবং দক্ষ স্মার্ট সেলস অ্যাসিস্ট্যান্ট। তোমার কাজ হলো কাস্টমারের সাথে আন্তরিকভাবে কথা বলে তাদের প্রশ্নের উত্তর দেওয়া এবং প্রয়োজনে অর্ডার গ্রহণ করা।

        ব্যবসায়ের নাম ও তথ্য: {business if business else 'N/A'}

        উপলব্ধ পণ্যসমূহ (বিস্তারিত তথ্যসহ):
        {product_info}

        তোমার আচরণের নিয়মাবলী:
        ১. তুমি সবসময় প্রমিত শুদ্ধ বাংলায় কথা বলবে। কোনো আঞ্চলিক ভাষা ব্যবহার করবে না।
        ২. কাস্টমারকে সবসময় 'আপনি' বলে সম্বোধন করবে। তোমার ভাষা হবে অত্যন্ত নম্র এবং সম্মানজনক।
        ৩. কাস্টমার কোনো পণ্যের ফিচার বা গুণগত মান জানতে চাইলে 'বিস্তারিত বর্ণনা' অংশ থেকে তথ্য দিয়ে সাহায্য করো।
        ৪. অর্ডার করার জন্য কাস্টমারের নাম, ১১ ডিজিটের সঠিক ফোন নম্বর, পণ্যের নাম ও পরিমাণ এবং পূর্ণাঙ্গ ঠিকানা সংগ্রহ করো।
        ৫. সব তথ্য পাওয়ার পর একটি 'অর্ডার সামারি' তৈরি করে কাস্টমারকে দেখাও এবং তার সম্মতি (Confirmation) চাও।
        ৬. কাস্টমার যখন স্পষ্ট করে 'হ্যাঁ', 'ঠিক আছে' বা 'অর্ডার কনফার্ম করুন' বলবে, কেবলমাত্র তখনই তুমি 'submit_order_to_db' টুলটি ব্যবহার করবে।
        ৭. যদি কাস্টমার শুধু বলে 'অর্ডার দিতে চাই' কিন্তু কোনো তথ্য না দেয়, তবে সরাসরি অর্ডার সেভ না করে ভদ্রভাবে তার কাছে প্রয়োজনীয় তথ্যগুলো চাও।
        """

        tools = [{
            "type": "function",
            "function": {
                "name": "submit_order_to_db",
                "description": "অর্ডার কনফার্ম হওয়ার পর সব তথ্য ডাটাবেসে সেভ করার জন্য ব্যবহার করো",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "phone": {"type": "string"},
                        "address": {"type": "string"},
                        "items": {"type": "string", "description": "পণ্যের নাম ও পরিমাণ"},
                        "total_qty": {"type": "integer"},
                        "delivery_charge": {"type": "integer"},
                        "total_amount": {"type": "integer"}
                    },
                    "required": ["name", "phone", "address", "items", "total_qty", "delivery_charge", "total_amount"]
                }
            }
        }]

        messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": user_msg}]
        
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0.4
        )

        resp_msg = response.choices[0].message
        
        # ফাংশন কল হ্যান্ডলিং (অর্ডার সেভ)
        if resp_msg.tool_calls:
            args = json.loads(resp_msg.tool_calls[0].function.arguments)
            
            # ডেটা ভ্যালিডেশন: ফোন নম্বর বা ঠিকানা না থাকলে সেভ আটকানো
            if not args.get('phone') or len(str(args.get('phone'))) < 11 or not args.get('address'):
                return "জি, আপনার অর্ডারটি গ্রহণ করার জন্য সঠিক ফোন নম্বর এবং পূর্ণাঙ্গ ঠিকানা প্রয়োজন। অনুগ্রহ করে তথ্যগুলো দিলে আমি অর্ডারটি সম্পন্ন করতে পারব।"

            supabase.table("orders").insert({
                "user_id": admin_id,
                "customer_name": args["name"],
                "customer_phone": args["phone"],
                "product": args["items"],
                "quantity": args["total_qty"],
                "address": args["address"],
                "delivery_charge": args["delivery_charge"],
                "total": args["total_amount"],
                "status": "pending",
                "created_at": datetime.utcnow().isoformat()
            }).execute()
            
            return f"জি ধন্যবাদ {args['name']}! আপনার অর্ডারটি সফলভাবে গ্রহণ করা হয়েছে। আপনার মোট বিল হয়েছে ৳{args['total_amount']}। আমরা খুব শীঘ্রই আপনার সাথে যোগাযোগ করছি। আমাদের সাথে থাকার জন্য ধন্যবাদ।"

        # সাধারণ চ্যাট রিপ্লাই
        reply = resp_msg.content
        new_history = history + [{"role": "user", "content": user_msg}, {"role": "assistant", "content": reply}]
        save_chat_memory(admin_id, customer_id, new_history[-15:])
        return reply

    except Exception as e:
        logger.error(f"AI Error: {e}")
        return "আমি আন্তরিকভাবে দুঃখিত, আপনার মেসেজটি প্রসেস করতে সাময়িক অসুবিধা হচ্ছে। অনুগ্রহ করে কিছুক্ষণ পর আবার চেষ্টা করুন।"

# ================= WEBHOOK =================
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.mode") == "subscribe": return request.args.get("hub.challenge")
        return "OK", 200
    
    data = request.get_json()
    for entry in data.get("entry", []):
        page_id = entry.get("id")
        page = get_page_client(page_id)
        if not page: continue
        token, admin_id = page["page_access_token"], page["user_id"]
        
        for msg_event in entry.get("messaging", []):
            sender = msg_event["sender"]["id"]
            text = msg_event.get("message", {}).get("text")
            if not text: continue
            
            # ১. প্রথমে FAQ চেক করবে (যদি থাকে)
            faq = find_faq(admin_id, text)
            if faq: 
                send_message(token, sender, faq)
            else: 
                # ২. স্মার্ট AI প্রসেসিং
                send_message(token, sender, process_ai_smart_order(admin_id, sender, text))

    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

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
        res = supabase.table("facebook_integrations") \
            .select("*") \
            .eq("page_id", str(page_id)) \
            .eq("is_connected", True) \
            .execute()
        return res.data[0] if res.data else None
    except:
        return None

def send_message(token, user_id, text):
    try:
        url = f"https://graph.facebook.com/v18.0/me/messages?access_token={token}"
        res = requests.post(url, json={
            "recipient": {"id": user_id},
            "message": {"text": text}
        })
        res.raise_for_status()
    except Exception as e:
        logger.error(f"Facebook API Error: {e}")

def get_products_with_details(admin_id: str):
    try:
        res = supabase.table("products") \
            .select("id, name, price, stock, category, description, in_stock") \
            .eq("user_id", admin_id) \
            .execute()
        return res.data or []
    except Exception as e:
        logger.error(f"Product Fetch Error: {e}")
        return []

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
    except:
        return None

def get_business_settings(admin_id: str) -> Optional[Dict]:
    try:
        res = supabase.table("business_settings").select("*").eq("user_id", admin_id).limit(1).execute()
        return res.data[0] if res.data else None
    except:
        return None

# ================= CHAT MEMORY =================
def get_chat_memory(admin_id: str, customer_id: str, limit: int = 15) -> List[Dict]:
    try:
        res = supabase.table("chat_history").select("messages").eq("user_id", admin_id).eq("customer_id", customer_id).limit(1).execute()
        if res.data and isinstance(res.data[0].get("messages"), list):
            return res.data[0].get("messages")[-limit:]
    except:
        pass
    return []

def save_chat_memory(admin_id: str, customer_id: str, messages: List[Dict]):
    try:
        now = datetime.utcnow().isoformat()
        supabase.table("chat_history").upsert({
            "user_id": admin_id, 
            "customer_id": customer_id, 
            "messages": messages, 
            "last_updated": now
        }, on_conflict="user_id,customer_id").execute()
    except Exception as e:
        logger.error(f"Chat Memory Error: {e}")

# ================= AI SMART ORDERING =================
def process_ai_smart_order(admin_id, customer_id, user_msg):
    try:
        # Get Keys
        res = supabase.table("api_keys").select("groq_api_key").eq("user_id", admin_id).execute()
        if not res.data: return "System Error: API key not found."
        client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=res.data[0]["groq_api_key"])

        # Context
        business = get_business_settings(admin_id)
        products = get_products_with_details(admin_id)
        history = get_chat_memory(admin_id, customer_id)
        
        product_list_text = "\n".join([f"- {p['name']} (Price: {p['price']}, Stock: {p.get('stock', 'N/A')})" for p in products])
        
        system_prompt = f"""
        তুমি Simanto, তুমি একজন অভিজ্ঞ বিক্রয় সহকারী, তুমি সবসময় শুদ্ধ প্রমিত বাংলায় উত্তর দেবে, কখনো আন্দাজ করবে না, গ্রাহক কিনতে চাইলে অর্ডারে পাঠাবে।
        ব্যবসায়ের তথ্য: {business if business else 'N/A'}
        উপলব্ধ পণ্যসমূহ:
        {product_list_text}

        অর্ডার সম্পন্ন করতে তোমার এই তথ্যগুলো লাগবে:
        1. কাস্টমারের নাম
        2. ফোন নম্বর
        3. পণ্য এবং পরিমাণ
        4. পূর্ণাঙ্গ ঠিকানা
        5. ডেলিভারি চার্জ (এলাকা অনুযায়ী কাস্টমারকে জানাও)

        তোমার কাজের ধারা:
        - তুমি একজন অভিজ্ঞ বিক্রয় সহকারী, তুমি আগে বুঝার চেষ্টা করবা গ্রাহক কি চাই, তুমি সবসময় শুদ্ধ প্রমিত বাংলায় উত্তর দেবে, কখনো আন্দাজ করবে না, গ্রাহক কে আগে সব তথ্য ঠিক ভাবে জানাও যা যা গ্রাহক জানতে চাই, এরপর গ্রাহক কিনতে চাইলে অর্ডার সম্পন্ন করতে যেই তথ্য গুলো লাগবে সেইগুলা নেওয়া শুরু করবে, কাস্টমার একবারে সব দিলে সব একবারে বুঝে নাও।
        - যখন তোমার কাছে (নাম, ফোন, পণ্য, ঠিকানা, চার্জ) এই ৫টি তথ্য চলে আসবে, তখন তুমি একটি 'অর্ডার সামারি' দেখাবে এবং জিজ্ঞেস করবে "অর্ডারটি কি কনফার্ম করব?"।
        - কাস্টমার যদি 'হ্যাঁ' বা 'confirm' বলে, তখনই তুমি 'submit_order_to_db' টুলটি কল করবে।
        - অর্ডার সেভ হওয়ার আগে অবশ্যই কাস্টমারের সম্মতি নিবে।
        """

        tools = [{
            "type": "function",
            "function": {
                "name": "submit_order_to_db",
                "description": "Save order to database after customer confirmation",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "phone": {"type": "string"},
                        "address": {"type": "string"},
                        "items": {"type": "string", "description": "Comma separated product names and quantities"},
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
            tool_choice="auto"
        )

        resp_msg = response.choices[0].message
        
        if resp_msg.tool_calls:
            for tool_call in resp_msg.tool_calls:
                if tool_call.function.name == "submit_order_to_db":
                    args = json.loads(tool_call.function.arguments)
                    # Database Save
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
                    
                    return f"✅ ধন্যবাদ {args['name']}! আপনার অর্ডারটি সফলভাবে গ্রহণ করা হয়েছে। সর্বমোট: ৳{args['total_amount']}। আমরা শীঘ্রই আপনার সাথে যোগাযোগ করব।"

        # Update History and Return Reply
        reply = resp_msg.content
        new_history = history + [{"role": "user", "content": user_msg}, {"role": "assistant", "content": reply}]
        save_chat_memory(admin_id, customer_id, new_history[-15:])
        return reply

    except Exception as e:
        logger.error(f"AI Smart Order Error: {e}")
        return "দুঃখিত, আমি এই মুহূর্তে প্রসেস করতে পারছি না। একটু পরে আবার চেষ্টা করুন।"

# ================= WEBHOOK =================
@app.route("/webhook", methods=["GET"])
def verify():
    if request.args.get("hub.mode") == "subscribe":
        return request.args.get("hub.challenge")
    return "OK", 200

@app.route("/webhook", methods=["POST"])
def webhook():
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

            # ১. প্রথমে FAQ চেক করবে
            faq = find_faq(admin_id, text)
            if faq:
                send_message(token, sender, faq)
                continue

            # ২. স্মার্ট AI অর্ডার প্রসেসিং (এটিই এখন সব হ্যান্ডেল করবে)
            reply = process_ai_smart_order(admin_id, sender, text)
            send_message(token, sender, reply)

    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

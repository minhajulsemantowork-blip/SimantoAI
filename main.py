import os
import re
import json
import logging
import requests
from typing import Optional, Dict, List
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
        requests.post(url, json={
            "recipient": {"id": user_id},
            "message": {"text": text}
        }).raise_for_status()
    except Exception as e:
        logger.error(f"Facebook API Error: {e}")

def get_products_with_details(admin_id: str):
    try:
        res = supabase.table("products") \
            .select("id, name, price, stock, category, description, in_stock") \
            .eq("user_id", admin_id) \
            .execute()
        return res.data or []
    except:
        return []

def find_faq(admin_id: str, user_msg: str) -> Optional[str]:
    try:
        res = supabase.table("faqs") \
            .select("question, answer") \
            .eq("user_id", admin_id) \
            .execute()

        best_ratio, best_answer = 0, None
        for faq in res.data or []:
            ratio = SequenceMatcher(None, user_msg.lower(), faq["question"].lower()).ratio()
            if ratio > best_ratio and ratio > 0.65:
                best_ratio = ratio
                best_answer = faq["answer"]
        return best_answer
    except:
        return None

def get_business_settings(admin_id: str) -> Optional[Dict]:
    try:
        res = supabase.table("business_settings") \
            .select("*") \
            .eq("user_id", admin_id) \
            .limit(1) \
            .execute()
        return res.data[0] if res.data else None
    except:
        return None

# ================= CHAT MEMORY =================
def get_chat_memory(admin_id: str, customer_id: str, limit: int = 15) -> List[Dict]:
    try:
        res = supabase.table("chat_history") \
            .select("messages") \
            .eq("user_id", admin_id) \
            .eq("customer_id", customer_id) \
            .limit(1) \
            .execute()
        if res.data:
            return res.data[0]["messages"][-limit:]
    except:
        pass
    return []

def save_chat_memory(admin_id: str, customer_id: str, messages: List[Dict]):
    try:
        supabase.table("chat_history").upsert({
            "user_id": admin_id,
            "customer_id": customer_id,
            "messages": messages,
            "last_updated": datetime.utcnow().isoformat()
        }, on_conflict="user_id,customer_id").execute()
    except Exception as e:
        logger.error(f"Memory Error: {e}")

# ================= AI SMART ORDER =================
def process_ai_smart_order(admin_id, customer_id, user_msg):
    try:
        key = supabase.table("api_keys") \
            .select("groq_api_key") \
            .eq("user_id", admin_id) \
            .execute()

        if not key.data:
            return "দুঃখিত, সাময়িক কারিগরি সমস্যা হচ্ছে।"

        client = OpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=key.data[0]["groq_api_key"]
        )

        products = get_products_with_details(admin_id)
        business = get_business_settings(admin_id)
        history = get_chat_memory(admin_id, customer_id)

        product_info = ""
        for p in products:
            product_info += (
                f"- নাম: {p['name']} | মূল্য: ৳{p['price']} | "
                f"স্টক: {p.get('stock', 0)} | "
                f"বর্ণনা: {p.get('description', 'উপলব্ধ নেই')}\n"
            )

        system_prompt = f"""
তুমি Simanto, একজন STRICT সেলস বট।

ভাষা সংক্রান্ত কঠোর নির্দেশনা (অত্যন্ত গুরুত্বপূর্ণ):

- তুমি ১০০% শুদ্ধ প্রমিত বাংলায় কথা বলবে।
- কোনো ইংরেজি শব্দ, বাংলা-ইংরেজি মিশ্রণ (Banglish), রোমান বাংলা ব্যবহার করা সম্পূর্ণ নিষিদ্ধ।
- Product name ছাড়া অন্য কোনো ইংরেজি শব্দ ব্যবহার করা যাবে না।
- বাক্যের গঠন হবে পরিষ্কার, ভদ্র, ব্যবসায়িক এবং পেশাদার।
- কথ্য ভাষা, আঞ্চলিক ভাষা, বা অতিরিক্ত আবেগী শব্দ ব্যবহার করবে না।

গুরুত্বপূর্ণ নিয়ম:
- পণ্যের নাম Database এ যেভাবে আছে EXACT সেভাবেই ব্যবহার করবে।
- description এ যা লেখা আছে তার বাইরে কোনো তথ্য, সুবিধা বা গল্প বানাবে না।
- নতুন ফিচার, উপকারিতা বা অনুমানভিত্তিক কথা সম্পূর্ণ নিষিদ্ধ।
- description শুধু সুন্দর ভাষায় paraphrase করবে।

অর্ডার প্রসেস:
1️⃣ নাম, ১১ ডিজিট ফোন, ঠিকানা, পণ্য, পরিমাণ – সব না পাওয়া পর্যন্ত অর্ডার কনফার্ম করবে না  
2️⃣ সব তথ্য পেলে Order Summary দেখাবে  
3️⃣ কাস্টমার স্পষ্টভাবে "হ্যাঁ / কনফার্ম" বললে তবেই অর্ডার সেভ করবে  

ব্যবসা তথ্য: {business}

পণ্যসমূহ:
{product_info}
"""

        tools = [{
            "type": "function",
            "function": {
                "name": "submit_order_to_db",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "phone": {"type": "string"},
                        "address": {"type": "string"},
                        "items": {"type": "string"},
                        "total_qty": {"type": "integer"},
                        "delivery_charge": {"type": "integer"},
                        "total_amount": {"type": "integer"}
                    },
                    "required": ["name", "phone", "address", "items", "total_qty", "delivery_charge", "total_amount"]
                }
            }
        }]

        messages = [{"role": "system", "content": system_prompt}] + history + [
            {"role": "user", "content": user_msg}
        ]

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0.2
        )

        msg = response.choices[0].message

        # ================= ORDER SUMMARY SAFEGUARD (NEW) =================
        history_text = " ".join(
            m.get("content", "") for m in history if m.get("role") == "assistant"
        )

        if msg.tool_calls:
            if "অর্ডার সারাংশ" not in history_text:
                return "আপনার অর্ডারের সারাংশ আগে দেখানো হবে। অনুগ্রহ করে সেটি যাচাই করে সম্মতি দিন।"

            args = json.loads(msg.tool_calls[0].function.arguments)

            if len(args.get("phone", "")) != 11 or not args.get("address"):
                return "অর্ডার সম্পন্ন করতে সঠিক ফোন নম্বর ও পূর্ণ ঠিকানা প্রয়োজন।"

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

            return f"ধন্যবাদ {args['name']}। আপনার অর্ডারটি সফলভাবে গ্রহণ করা হয়েছে।"

        reply = msg.content
        save_chat_memory(admin_id, customer_id, history + [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": reply}
        ])

        return reply

    except Exception as e:
        logger.error(f"AI Error: {e}")
        return "সাময়িক সমস্যা হচ্ছে, পরে চেষ্টা করুন।"

# ================= WEBHOOK =================
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.mode") == "subscribe":
            return request.args.get("hub.challenge")
        return "OK", 200

    data = request.get_json()
    for entry in data.get("entry", []):
        page = get_page_client(entry.get("id"))
        if not page:
            continue

        for m in entry.get("messaging", []):
            sender = m["sender"]["id"]
            text = m.get("message", {}).get("text")
            if not text:
                continue

            faq = find_faq(page["user_id"], text)
            send_message(
                page["page_access_token"],
                sender,
                faq if faq else process_ai_smart_order(page["user_id"], sender, text)
            )

    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

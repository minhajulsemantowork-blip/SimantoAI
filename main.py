# ================= IMPORTS =================
import os, re, json, logging, time, requests
from typing import Optional, Dict, List
from datetime import datetime, timedelta
from functools import lru_cache
from flask import Flask, request, jsonify
from openai import OpenAI
from supabase import create_client, Client
from difflib import SequenceMatcher

# ================= CONFIG =================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)

class Config:
    GROQ_BASE_URL = "https://api.groq.com/openai/v1"
    GROQ_MODEL = "llama-3.3-70b-versatile"
    MAX_TOKENS = 800
    TEMPERATURE = 0.1
    MAX_SYSTEM_PROMPT_TOKENS = 1200

# ================= SUPABASE =================
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_KEY")
)

# ================= DATA =================
@lru_cache(maxsize=128)
def get_products(admin_id: str) -> List[Dict]:
    return supabase.table("products") \
        .select("id,name,price,stock,description") \
        .eq("user_id", admin_id) \
        .eq("in_stock", True) \
        .execute().data or []

def get_product_exact(admin_id: str, name: str) -> Optional[Dict]:
    for p in get_products(admin_id):
        if p["name"] == name:
            return p
    return None

@lru_cache(maxsize=64)
def get_business(admin_id: str) -> Optional[Dict]:
    res = supabase.table("business_settings") \
        .select("*").eq("user_id", admin_id).limit(1).execute()
    return res.data[0] if res.data else None

# ================= SESSION =================
def get_or_create_session(admin_id, customer_id):
    res = supabase.table("order_sessions") \
        .select("*") \
        .eq("user_id", admin_id) \
        .eq("customer_id", customer_id) \
        .neq("status", "completed") \
        .neq("status", "cancelled") \
        .limit(1).execute()

    if res.data:
        return res.data[0]

    new = {
        "user_id": admin_id,
        "customer_id": customer_id,
        "status": "collecting",
        "collected_data": {},
        "cart_items": [],
        "created_at": datetime.utcnow().isoformat(),
        "expires_at": (datetime.utcnow() + timedelta(hours=2)).isoformat()
    }
    return supabase.table("order_sessions").insert(new).execute().data[0]

def update_session(sid, data):
    data["updated_at"] = datetime.utcnow().isoformat()
    supabase.table("order_sessions").update(data).eq("id", sid).execute()

# ================= SALES SYSTEM PROMPT =================
def build_system_prompt(admin_id: str) -> str:
    business = get_business(admin_id)
    products = get_products(admin_id)

    prompt = f"""
তুমি {business['name'] if business else 'একটি দোকান'}-এর বিক্রয় সহকারী।

নিয়ম:
- শুধু বাংলায় কথা বলবে
- ডাটাবেসে নেই এমন কিছু বলবে না
- Product name ও description একদম DB অনুযায়ী বলবে
- অনুমান / বানানো অফার বলবে না
- Confirm ছাড়া অর্ডার save করবে না

Sales Behaviour:
- গ্রাহক পণ্য দেখলে সংক্ষেপে উপকারিতা বলবে (description থাকলে)
- stock কম থাকলে ভদ্রভাবে জানাবে
- একই ক্যাটাগরির বিকল্প থাকলে suggest করতে পারো
- জোর করবে না, natural tone রাখবে

Available Products:
"""

    for p in products:
        stock_line = f"স্টক: {p['stock']} পিছ" if p["stock"] else "স্টকে নেই"
        prompt += f"\n- {p['name']} | ৳{p['price']} | {stock_line}\n  বিবরণ: {p['description']}"

    return prompt[:Config.MAX_SYSTEM_PROMPT_TOKENS]

# ================= AI =================
def process_ai(admin_id, customer_id, user_msg):
    session = get_or_create_session(admin_id, customer_id)
    msg = user_msg.lower().strip()

    # CANCEL
    if msg in ["cancel", "বাতিল"]:
        update_session(session["id"], {"status": "cancelled"})
        return "❌ অর্ডার বাতিল করা হয়েছে।"

    # DONE → SHOW SUMMARY
    if msg in ["সম্পন্ন", "done"]:
        if not session["cart_items"]:
            return "⚠️ এখনো কোনো পণ্য যোগ করা হয়নি।"

        total = sum(i["subtotal"] for i in session["cart_items"])
        items = "\n".join([f"- {i['name']} x{i['qty']} = ৳{i['subtotal']}" for i in session["cart_items"]])

        update_session(session["id"], {"status": "ready"})

        return f"""📋 অর্ডার সামারি:

{items}

সর্বমোট: ৳{total}

✅ Confirm লিখলে অর্ডার হবে
❌ Cancel লিখলে বাতিল হবে"""

    # CONFIRM
    if msg == "confirm":
        if session["status"] != "ready":
            return "⚠️ কনফার্ম করার মতো কোনো অর্ডার নেই।"

        d = session["collected_data"]
        items = session["cart_items"]

        order = {
            "user_id": admin_id,
            "customer_name": d.get("name", ""),
            "customer_phone": d.get("phone", ""),
            "customer_address": d.get("address", ""),
            "product": ", ".join(i["name"] for i in items),
            "quantity": sum(i["qty"] for i in items),
            "total": sum(i["subtotal"] for i in items),
            "status": "pending",
            "created_at": datetime.utcnow().isoformat()
        }

        supabase.table("orders").insert(order).execute()
        update_session(session["id"], {"status": "completed"})

        return "✅ অর্ডার সফলভাবে কনফার্ম হয়েছে। আমরা শীঘ্রই যোগাযোগ করবো।"

    # PRODUCT ADD (SMART CART)
    for p in get_products(admin_id):
        if p["name"] in user_msg:
            qty = max(1, int(re.findall(r"\d+", user_msg)[0]) if re.findall(r"\d+", user_msg) else 1)

            cart = session["cart_items"]
            found = False

            for item in cart:
                if item["name"] == p["name"]:
                    item["qty"] += qty
                    item["subtotal"] = item["qty"] * item["price"]
                    found = True
                    break

            if not found:
                cart.append({
                    "product_id": p["id"],
                    "name": p["name"],
                    "qty": qty,
                    "price": p["price"],
                    "subtotal": p["price"] * qty
                })

            update_session(session["id"], {"cart_items": cart})

            return f"✅ {p['name']} কার্টে যোগ হয়েছে ({qty} পিছ)। আরও পণ্য যোগ করতে পারেন, না হলে 'সম্পন্ন' লিখুন।"

    # AI CHAT
    client = OpenAI(
        base_url=Config.GROQ_BASE_URL,
        api_key=supabase.table("api_keys")
        .select("groq_api_key").eq("user_id", admin_id).execute().data[0]["groq_api_key"]
    )

    res = client.chat.completions.create(
        model=Config.GROQ_MODEL,
        messages=[
            {"role": "system", "content": build_system_prompt(admin_id)},
            {"role": "user", "content": user_msg}
        ],
        temperature=Config.TEMPERATURE,
        max_tokens=Config.MAX_TOKENS
    )

    return res.choices[0].message.content

# ================= WEBHOOK =================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    for entry in data.get("entry", []):
        page_id = entry["id"]
        page = supabase.table("facebook_integrations") \
            .select("*").eq("page_id", page_id).single().execute().data

        admin_id = page["user_id"]
        token = page["page_access_token"]

        for m in entry.get("messaging", []):
            uid = m["sender"]["id"]
            text = m.get("message", {}).get("text", "")

            if not text:
                continue

            reply = process_ai(admin_id, uid, text)

            requests.post(
                f"https://graph.facebook.com/v18.0/me/messages?access_token={token}",
                json={"recipient": {"id": uid}, "message": {"text": reply}}
            )

    return "ok", 200

import os
import re
import logging
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from openai import OpenAI
from supabase import create_client, Client

# ================= CONFIG =================
BOT_NAME = "Simanto"
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)

# ================= SUPABASE =================
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_KEY")
)

# ================= MEMORY =================
_order_sessions = {}

# ================= HELPERS =================
def detect_language(text: str) -> str:
    return "bangla" if re.search(r'[\u0980-\u09FF]', text) else "english"

def get_products(admin_id: str):
    res = supabase.table("products").select("*").eq("user_id", admin_id).execute()
    return res.data or []

def build_product_knowledge(products):
    knowledge = []
    for p in products:
        if not p.get("in_stock"):
            continue
        knowledge.append(f"""
Product Name: {p.get('name')}
Category: {p.get('category')}
Price: ৳{p.get('price')}
Use Case: {p.get('use_case')}
Target User: {p.get('target_user')}
Mood: {p.get('mood')}
Description: {p.get('description')}
""")
    return "\n".join(knowledge)

def get_page_client(page_id):
    res = supabase.table("facebook_integrations") \
        .select("*") \
        .eq("page_id", str(page_id)) \
        .eq("is_connected", True) \
        .execute()
    return res.data[0] if res.data else None

def send_message(token, user_id, text):
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={token}"
    requests.post(url, json={
        "recipient": {"id": user_id},
        "message": {"text": text}
    })

# ================= ORDER SESSION =================
class OrderSession:
    def __init__(self, admin_id, customer_id, product_name):
        self.admin_id = admin_id
        self.customer_id = customer_id
        self.product = product_name
        self.step = "name"
        self.data = {}

    def next(self, msg):
        if self.step == "name":
            self.data["name"] = msg
            self.step = "phone"
            return "ধন্যবাদ 😊 ফোন নাম্বারটা দিন"

        if self.step == "phone":
            self.data["phone"] = msg
            self.step = "address"
            return "ডেলিভারি ঠিকানা লিখবেন"

        if self.step == "address":
            self.data["address"] = msg
            self.save()
            return "✅ অর্ডার কনফার্ম হয়েছে!\nআমরা শীঘ্রই যোগাযোগ করব ❤️"

    def save(self):
        supabase.table("orders").insert({
            "user_id": self.admin_id,
            "product": self.product,
            "customer_name": self.data["name"],
            "customer_phone": self.data["phone"],
            "address": self.data["address"],
            "status": "pending",
            "created_at": datetime.utcnow().isoformat()
        }).execute()

# ================= AI CORE =================
def generate_ai_reply(admin_id, page_name, user_msg):
    products = get_products(admin_id)
    product_knowledge = build_product_knowledge(products)

    system_prompt = f"""
You are a real human sales assistant.

You ONLY know the products listed below.
You must NEVER invent products, prices, or availability.

Your goals:
1. Understand customer intent
2. Match intent with product knowledge
3. If unsure, ask ONE clarifying question
4. Suggest max 2 products
5. Explain with feeling + use-case
6. Build trust through honesty
7. Close sale naturally (never force)

STRICT RULES:
- If no product matches, say so honestly
- If customer intent is unclear, ask
- Do NOT talk outside product knowledge
- Do NOT push order unless customer agrees

PRODUCT KNOWLEDGE:
{product_knowledge}

Customer message:
"{user_msg}"

Reply naturally in Bangla like a helpful shopkeeper:
"""

    client = OpenAI(
        base_url="https://api.groq.com/openai/v1",
        api_key=supabase.table("api_keys")
            .select("gemini_api_key")
            .eq("user_id", admin_id)
            .execute().data[0]["gemini_api_key"]
    )

    res = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "system", "content": system_prompt}],
        temperature=0.6,
        max_tokens=300
    )

    return res.choices[0].message.content.strip()

# ================= WEBHOOK =================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    for entry in data.get("entry", []):
        page_id = entry.get("id")
        client = get_page_client(page_id)
        if not client:
            continue

        admin_id = client["user_id"]
        token = client["page_access_token"]
        page_name = client.get("page_name", "আমাদের দোকান")

        for event in entry.get("messaging", []):
            sender = event["sender"]["id"]
            msg = event.get("message", {}).get("text")
            if not msg:
                continue

            # ORDER SESSION
            if sender in _order_sessions:
                reply = _order_sessions[sender].next(msg)
                send_message(token, sender, reply)
                if "কনফার্ম" in reply:
                    del _order_sessions[sender]
                continue

            # ORDER CONFIRMATION INTENT
            if re.search(r"(নিব|অর্ডার করব|confirm)", msg.lower()):
                product_match = re.search(r"(.+)", msg)
                product_name = product_match.group(1) if product_match else "Unknown"
                _order_sessions[sender] = OrderSession(admin_id, sender, product_name)
                send_message(token, sender, "চলুন অর্ডারটা করি 😊\nআপনার নামটা বলবেন")
                continue

            # AI RESPONSE
            reply = generate_ai_reply(admin_id, page_name, msg)
            send_message(token, sender, reply)

    return jsonify({"ok": True}), 200

# ================= RUN =================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

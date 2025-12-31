import os
import re
import logging
import requests
from typing import Optional, Dict, Tuple
from datetime import datetime
from flask import Flask, request, jsonify
from openai import OpenAI
from supabase import create_client, Client

# ================= CONFIG =================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)

# ================= SUPABASE =================
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_KEY")
)

# ================= MEMORY =================
_order_sessions: Dict[str, "OrderSession"] = {}

# ================= HELPERS =================
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

def get_products_with_details(admin_id: str):
    res = supabase.table("products") \
        .select("*") \
        .eq("user_id", admin_id) \
        .execute()
    return res.data or []

# ================= ORDER SESSION (UNCHANGED) =================
class OrderSession:
    """Manage order collection session for a customer"""

    def __init__(self, admin_id: str, customer_id: str):
        self.admin_id = admin_id
        self.customer_id = customer_id
        self.session_id = f"order_{admin_id}_{customer_id}"
        self.step = 0
        self.data = {
            "name": "",
            "phone": "",
            "product": "",
            "quantity": "",
            "address": "",
            "status": "pending",
            "total": 0
        }
        self.products = get_products_with_details(admin_id)

    def start_order(self):
        self.step = 1
        _order_sessions[self.session_id] = self
        return "অর্ডার নেওয়া শুরু করছি! প্রথমে আপনার নাম বলুন:"

    def process_response(self, user_message: str) -> Tuple[str, bool]:
        completed = False

        if self.step == 1:
            self.data["name"] = user_message.strip()
            self.step = 2
            return "ধন্যবাদ! এখন আপনার ফোন নম্বর দিন:", False

        elif self.step == 2:
            phone = user_message.strip()
            if self.validate_phone(phone):
                self.data["phone"] = phone
                self.step = 3
                products_text = self.get_available_products()
                return f"ফোন নম্বর সংরক্ষিত! কোন পণ্য অর্ডার করতে চান?\n\n{products_text}\n\nপণ্যের নাম লিখুন:", False
            else:
                return "দুঃখিত, সঠিক ফোন নম্বর দিন:", False

        elif self.step == 3:
            selected_product = self.find_product(user_message)
            if selected_product:
                self.data["product"] = selected_product["name"]
                self.data["product_id"] = selected_product.get("id")
                self.step = 4
                stock = selected_product.get("stock", 0)
                price = selected_product.get("price", 0)
                description = selected_product.get("description", "")
                features = selected_product.get("features", "")
                return (
                    f"✅ **{selected_product['name']}** নির্বাচিত!\n\n"
                    f"💰 দাম: ৳{price:,}\n"
                    f"📝 বিবরণ: {description}\n"
                    f"🌟 বৈশিষ্ট্য: {features}\n\n"
                    f"কত পিস চান? (স্টকে আছে: {stock} পিস):"
                ), False
            else:
                products_text = self.get_available_products()
                return f"পণ্যটি খুঁজে পাইনি। আবার চেষ্টা করুন:\n\n{products_text}\n\nপণ্যের নাম লিখুন:", False

        elif self.step == 4:
            if user_message.isdigit():
                quantity = int(user_message)
                if quantity > 0:
                    product = self.find_product_by_name(self.data["product"])
                    if product:
                        stock = product.get("stock", 0)
                        if stock >= quantity:
                            self.data["quantity"] = quantity
                            price = product.get("price", 0)
                            self.data["total"] = price * quantity
                            self.step = 5
                            return (
                                f"✅ {quantity} পিস নির্বাচিত!\n"
                                f"💰 মোট মূল্য: ৳{self.data['total']:,}\n\n"
                                f"এখন আপনার ডেলিভারি ঠিকানা দিন (বিস্তারিত):"
                            ), False
                        else:
                            return f"দুঃখিত, স্টকে মাত্র {stock} পিস আছে। কম সংখ্যক দিন:", False
                return "দুঃখিত, ১ বা তার বেশি সংখ্যা দিন:", False
            return "দুঃখিত, সংখ্যা দিন (যেমন: 1, 2, 3):", False

        elif self.step == 5:
            self.data["address"] = user_message.strip()
            self.step = 6
            summary = self.get_order_summary()
            return (
                f"ঠিকানা সংরক্ষিত!\n\n{summary}\n\n"
                f"অর্ডার কনফার্ম করতে শুধুমাত্র 'confirm' লিখুন।\n"
                f"অন্য কিছু লিখলে অর্ডার বাতিল হবে।"
            ), False

        elif self.step == 6:
            if user_message.lower().strip() == "confirm":
                if self.save_order():
                    completed = True
                    return (
                        f"✅ অর্ডার সফলভাবে কনফার্ম হয়েছে!\n\n"
                        f"অর্ডার আইডি: {self.data.get('order_id')}\n\n"
                        f"আমরা শীঘ্রই আপনার সাথে যোগাযোগ করব। ধন্যবাদ! 😊"
                    ), True
                return "❌ অর্ডার সেভ করতে সমস্যা হয়েছে।", True
            completed = True
            return "অর্ডার বাতিল হয়েছে। আবার অর্ডার দিতে 'অর্ডার' লিখুন।", True

        return "কিছু সমস্যা হয়েছে। আবার চেষ্টা করুন।", True

    def validate_phone(self, phone: str) -> bool:
        phone_clean = re.sub(r'\D', '', phone)
        return len(phone_clean) == 11 and phone_clean.startswith('01')

    def get_available_products(self) -> str:
        available = []
        for p in self.products:
            if p.get("in_stock") and p.get("stock", 0) > 0:
                available.append(
                    f"- {p.get('name')} (৳{p.get('price'):,}, স্টক: {p.get('stock')})"
                )
        return "স্টকে থাকা পণ্য:\n\n" + "\n".join(available) if available else "এখন কোনো পণ্য স্টকে নেই।"

    def find_product(self, query: str) -> Optional[Dict]:
        q = query.lower().strip()
        for p in self.products:
            name = p.get("name", "").lower()
            if q in name or name in q:
                if p.get("in_stock") and p.get("stock", 0) > 0:
                    return p
        return None

    def find_product_by_name(self, name: str) -> Optional[Dict]:
        for p in self.products:
            if p.get("name", "").lower().strip() == name.lower().strip():
                return p
        return None

    def get_order_summary(self) -> str:
        return (
            f"📦 অর্ডার সামারি:\n"
            f"👤 নাম: {self.data['name']}\n"
            f"📱 ফোন: {self.data['phone']}\n"
            f"🛒 পণ্য: {self.data['product']}\n"
            f"🔢 পরিমাণ: {self.data['quantity']} পিস\n"
            f"💰 মোট: ৳{self.data['total']:,}\n"
            f"🏠 ঠিকানা: {self.data['address']}"
        )

    def save_order(self) -> bool:
        try:
            res = supabase.table("orders").insert({
                "user_id": self.admin_id,
                "customer_name": self.data["name"],
                "customer_phone": self.data["phone"],
                "product": self.data["product"],
                "quantity": int(self.data["quantity"]),
                "address": self.data["address"],
                "total": float(self.data["total"]),
                "status": "pending",
                "created_at": datetime.utcnow().isoformat()
            }).execute()
            if res.data:
                self.data["order_id"] = res.data[0].get("id")
                return True
            return False
        except Exception as e:
            logger.error(e)
            return False

# ================= AI =================
def generate_ai_reply(admin_id, user_msg):
    products = get_products_with_details(admin_id)
    product_text = "\n".join(
        f"- {p.get('name')} | ৳{p.get('price')} | {p.get('description')}"
        for p in products if p.get("in_stock")
    )

    system_prompt = f"""
তুমি একজন অভিজ্ঞ বিক্রয় সহকারী।
তুমি সবসময় শুদ্ধ, প্রমিত বাংলায় উত্তর দেবে।
কখনো আন্দাজ করবে না।
গ্রাহক কিনতে চাইলে অর্ডারে পাঠাবে।

পণ্যের তালিকা:
{product_text}

গ্রাহকের প্রশ্ন:
"{user_msg}"
"""

    api_key = supabase.table("api_keys") \
        .select("gemini_api_key") \
        .eq("user_id", admin_id) \
        .execute().data[0]["gemini_api_key"]

    client = OpenAI(
        base_url="https://api.groq.com/openai/v1",
        api_key=api_key
    )

    res = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "system", "content": system_prompt}],
        temperature=0.6,
        max_tokens=650
    )
    return res.choices[0].message.content.strip()

# ================= WEBHOOK =================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    for entry in data.get("entry", []):
        page_id = entry.get("id")
        page = get_page_client(page_id)
        if not page:
            continue

        admin_id = page["user_id"]
        token = page["page_access_token"]

        for msg_event in entry.get("messaging", []):
            sender = msg_event["sender"]["id"]
            text = msg_event.get("message", {}).get("text")
            if not text:
                continue

            session_id = f"order_{admin_id}_{sender}"

            if session_id in _order_sessions:
                reply, done = _order_sessions[session_id].process_response(text)
                send_message(token, sender, reply)
                if done:
                    del _order_sessions[session_id]
                continue

            if re.search(r"(কিনব|নিব|অর্ডার|order|confirm|নিতে চাই)", text.lower()):
                session = OrderSession(admin_id, sender)
                send_message(token, sender, session.start_order())
                continue

            reply = generate_ai_reply(admin_id, text)
            send_message(token, sender, reply)

    return jsonify({"ok": True}), 200

# ================= RUN =================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

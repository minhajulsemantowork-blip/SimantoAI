import os
import re
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
        logger.error(f"Session Retrieval Error: {e}")
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
        logger.error(f"Session Save Error: {e}")

def delete_session_from_db(session_id: str):
    try:
        supabase.table("order_sessions").delete().eq("id", session_id).execute()
    except Exception as e:
        logger.error(f"Session Delete Error: {e}")

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
            .select("*") \
            .eq("user_id", admin_id) \
            .execute()
        return res.data or []
    except:
        return []

# ================= FAQ (SEMANTIC SEARCH) =================
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

# ================= BUSINESS SETTINGS =================
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
def get_chat_memory(admin_id: str, customer_id: str, limit: int = 10) -> List[Dict]:
    try:
        res = supabase.table("chat_history") \
            .select("messages") \
            .eq("user_id", admin_id) \
            .eq("customer_id", customer_id) \
            .limit(1) \
            .execute()
        if res.data:
            return res.data[0].get("messages", [])[-limit:]
    except:
        pass
    return []

def save_chat_memory(admin_id: str, customer_id: str, messages: List[Dict]):
    try:
        now = datetime.utcnow().isoformat()
        existing = supabase.table("chat_history").select("id").eq("user_id", admin_id).eq("customer_id", customer_id).execute()
        if existing.data:
            supabase.table("chat_history").update({"messages": messages, "last_updated": now}).eq("id", existing.data[0]["id"]).execute()
        else:
            supabase.table("chat_history").insert({"user_id": admin_id, "customer_id": customer_id, "messages": messages, "created_at": now, "last_updated": now}).execute()
    except Exception as e:
        logger.error(f"Chat Memory Error: {e}")

# ================= ORDER SESSION =================
class OrderSession:
    def __init__(self, admin_id: str, customer_id: str):
        self.admin_id = admin_id
        self.customer_id = customer_id
        self.session_id = f"order_{admin_id}_{customer_id}"
        self.step = 0
        self.data = {
            "name": "", "phone": "", "items": [], "address": "", 
            "product_price_total": 0, "delivery_charge": 0, "total": 0, "current_prod": None
        }

    def start_order(self):
        self.step = 1
        return "জি, আমি সিমন্ত (Simanto)। অর্ডার নিতে সাহায্য করছি। প্রথমে আপনার নাম বলুন:"

    def process_response(self, user_message: str) -> Tuple[str, bool]:
        msg = user_message.strip()
        self.products = get_products_with_details(self.admin_id)

        if self.step == 1:
            self.data["name"] = msg
            self.step = 2
            return "ধন্যবাদ! এখন আপনার ফোন নম্বর দিন:", False

        elif self.step == 2:
            phone_clean = re.sub(r'\D', '', msg)
            if len(phone_clean) == 11 and phone_clean.startswith('01'):
                self.data["phone"] = phone_clean
                self.step = 3
                return f"কোন পণ্যটি অর্ডার করতে চান?\n\n{self.get_available_list()}", False
            return "সঠিক ফোন নম্বর দিন (যেমন: 017xxxxxxxx):", False

        elif self.step == 3:
            prod = self.find_product(msg)
            if prod:
                self.data["current_prod"] = prod
                self.step = 4
                return f"✅ {prod['name']}! কয় পিস নিতে চান? (স্টক: {prod['stock']})", False
            return "পণ্যটি পাওয়া যায়নি। আবার নাম লিখুন:", False

        elif self.step == 4:
            if msg.isdigit() and int(msg) > 0:
                qty = int(msg)
                prod = self.data["current_prod"]
                if prod['stock'] >= qty:
                    self.data["items"].append({"id": prod['id'], "name": prod['name'], "qty": qty, "price": prod['price'] * qty})
                    self.data["product_price_total"] += prod['price'] * qty
                    self.step = 5
                    return "যোগ হয়েছে! আরও পণ্য নিতে চাইলে নাম লিখুন, নয়তো 'done' লিখুন:", False
                return f"দুঃখিত, স্টকে মাত্র {prod['stock']} পিস আছে।", False
            return "সঠিক সংখ্যা দিন:", False

        elif self.step == 5:
            if msg.lower() == 'done':
                # Database থেকে delivery_info নিয়ে আসা হচ্ছে
                business = get_business_settings(self.admin_id)
                delivery_info = business.get('delivery_info', "ডেলিভারি চার্জের পরিমাণ লিখুন:") if business else "ডেলিভারি চার্জের পরিমাণ লিখুন:"
                self.step = 6
                return f"{delivery_info}\n\nআপনার জন্য প্রযোজ্য চার্জটি সংখ্যায় লিখুন:", False
            prod = self.find_product(msg)
            if prod:
                self.data["current_prod"] = prod
                self.step = 4
                return f"✅ {prod['name']}! কয় পিস?", False
            return "পণ্যটির নাম অথবা 'done' লিখুন:", False

        elif self.step == 6:
            if msg.isdigit():
                self.data["delivery_charge"] = int(msg)
                self.data["total"] = self.data["product_price_total"] + self.data["delivery_charge"]
                self.step = 7
                return "আপনার পূর্ণাঙ্গ ডেলিভারি ঠিকানা দিন:", False
            return "দয়া করে ডেলিভারি চার্জটি শুধুমাত্র সংখ্যায় লিখুন:", False

        elif self.step == 7:
            self.data["address"] = msg
            self.step = 8
            summary = self.get_summary()
            return f"{summary}\n\nঅর্ডার কনফার্ম করতে 'confirm' লিখুন।", False

        elif self.step == 8:
            if msg.lower() == 'confirm':
                if self.save_order_db():
                    return f"✅ অর্ডার সফল হয়েছে! সর্বমোট ৳{self.data['total']:,} (ডেলিভারি চার্জসহ)। সিমন্ত (Simanto) শীঘ্রই আপনার সাথে যোগাযোগ করবে।", True
                return "দুঃখিত, অর্ডার সেভ করার সময় কারিগরি সমস্যা হয়েছে।", True
            return "অর্ডার বাতিল হয়েছে। পুনরায় অর্ডার দিতে 'অর্ডার' লিখুন।", True

        return "দুঃখিত, আমি বুঝতে পারছি না।", True

    def find_product(self, query):
        for p in self.products:
            if query.lower() in p['name'].lower() and p.get('stock', 0) > 0:
                return p
        return None

    def get_available_list(self):
        items = [f"- {p['name']} (৳{p['price']})" for p in self.products if p.get('stock', 0) > 0]
        return "\n".join(items) if items else "কোনো পণ্য স্টকে নেই।"

    def get_summary(self):
        items_txt = "\n".join([f"• {i['name']} ({i['qty']} পিস)" for i in self.data['items']])
        return (
            f"📋 অর্ডার সামারি:\n"
            f"নাম: {self.data['name']}\n"
            f"ফোন: {self.data['phone']}\n"
            f"পণ্যসমূহ:\n{items_txt}\n"
            f"-------------------\n"
            f"পণ্যের মূল্য: ৳{self.data['product_price_total']:,}\n"
            f"ডেলিভারি চার্জ: ৳{self.data['delivery_charge']:,}\n"
            f"সর্বমোট: ৳{self.data['total']:,}\n"
            f"ঠিকানা: {self.data['address']}"
        )

    def save_order_db(self) -> bool:
        try:
            res = supabase.table("orders").insert({
                "user_id": self.admin_id, 
                "customer_name": self.data["name"],
                "customer_phone": self.data["phone"], 
                "address": self.data["address"],
                "delivery_charge": self.data["delivery_charge"],
                "total": self.data["total"], 
                "items": self.data["items"],
                "status": "pending", 
                "created_at": datetime.utcnow().isoformat()
            }).execute()
            return True if res.data else False
        except Exception as e:
            logger.error(f"Save Order Error: {e}")
            return False

# ================= AI & INTENT DETECTION =================
def detect_intent_nlp(admin_id, text):
    try:
        res = supabase.table("api_keys").select("groq_api_key").eq("user_id", admin_id).execute()
        if not res.data: return False
        
        client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=res.data[0]["groq_api_key"])
        prompt = f"Does the user want to order/buy something? Respond with 'YES' or 'NO' only. Input: {text}"
        comp = client.chat.completions.create(model="llama-3.3-70b-versatile", messages=[{"role": "user", "content": prompt}])
        return "YES" in comp.choices[0].message.content.upper()
    except:
        return re.search(r"(কিনব|নিব|অর্ডার|order|confirm|নিতে চাই)", text.lower()) is not None

def generate_ai_reply(admin_id, customer_id, user_msg):
    try:
        business = get_business_settings(admin_id)
        business_context = f"তুমি Simanto, একজন বন্ধুসুলভ বিক্রয় সহকারী, একজন অভিজ্ঞ বিক্রয় সহকারী, তুমি সবসময় শুদ্ধ প্রমিত বাংলায় উত্তর দেবে, কখনো আন্দাজ করবে না, গ্রাহক কিনতে চাইলে অর্ডারে পাঠাবে।\n"
        if business:
            business_context += f"ব্যবসা: {business.get('name')}\nঠিকানা: {business.get('address')}\nপেমেন্ট: {business.get('payment_methods')}\nডেলিভারি তথ্য: {business.get('delivery_info')}\n"

        memory = get_chat_memory(admin_id, customer_id)
        api_res = supabase.table("api_keys").select("groq_api_key").eq("user_id", admin_id).execute()
        
        client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=api_res.data[0]["groq_api_key"])
        
        products = get_products_with_details(admin_id)
        product_text = "\n".join([f"- {p.get('name')} | ৳{p.get('price')}" for p in products if p.get("in_stock")])

        messages = [
            {
                "role": "system", 
                "content": f"{business_context}\nপণ্যের তালিকা:\n{product_text}\n\nগুরুত্বপূর্ণ: পণ্যের নামগুলি তালিকায় যেভাবে আছে, সেভাবেই ব্যবহার করবে।"
            }
        ]
        messages.extend(memory)
        messages.append({"role": "user", "content": user_msg})

        res = client.chat.completions.create(model="llama-3.3-70b-versatile", messages=messages, temperature=0.3)
        reply = res.choices[0].message.content.strip()

        new_memory = memory + [{"role": "user", "content": user_msg}, {"role": "assistant", "content": reply}]
        save_chat_memory(admin_id, customer_id, new_memory[-10:])
        return reply
    except Exception as e:
        logger.error(f"AI Error: {e}")
        return "হ্যালো আমি Simanto, দুঃখিত একটু সমস্যা হচ্ছে। দয়া করে আবার মেসেজ দিন।"

# ================= WEBHOOK =================
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

            session_id = f"order_{admin_id}_{sender}"
            current_session = get_session_from_db(session_id)

            if current_session:
                reply, done = current_session.process_response(text)
                send_message(token, sender, reply)
                if done: delete_session_from_db(session_id)
                else: save_session_to_db(current_session)
                continue

            if detect_intent_nlp(admin_id, text):
                new_session = OrderSession(admin_id, sender)
                save_session_to_db(new_session)
                send_message(token, sender, new_session.start_order())
                continue

            faq = find_faq(admin_id, text)
            if faq:
                send_message(token, sender, faq)
            else:
                send_message(token, sender, generate_ai_reply(admin_id, sender, text))

    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

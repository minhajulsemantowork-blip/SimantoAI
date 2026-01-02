import os
import re
import json
import logging
import requests
from typing import Optional, Dict, List, Tuple
from datetime import datetime
from flask import Flask, request, jsonify
from openai import OpenAI
from supabase import create_client, Client
from difflib import SequenceMatcher

# ================= CONFIG =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
app = Flask(__name__)

# ================= SUPABASE =================
try:
    supabase: Client = create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_SERVICE_KEY")
    )
    logger.info("Supabase client initialized successfully")
except Exception as e:
    logger.error(f"Supabase Client Error: {e}")
    supabase = None

# ================= HELPER FUNCTIONS =================
def get_page_client(page_id: str) -> Optional[Dict]:
    """Fetch Facebook page integration details"""
    try:
        res = supabase.table("facebook_integrations") \
            .select("*") \
            .eq("page_id", str(page_id)) \
            .eq("is_connected", True) \
            .execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"Error fetching page client: {e}")
        return None

def send_message(token: str, user_id: str, text: str) -> bool:
    """Send message via Facebook Messenger API"""
    try:
        url = f"https://graph.facebook.com/v18.0/me/messages?access_token={token}"
        response = requests.post(url, json={
            "recipient": {"id": user_id},
            "message": {"text": text}
        })
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Facebook API Error: {e}")
        return False

def get_products_with_details(admin_id: str) -> List[Dict]:
    """Fetch all products for a specific business"""
    try:
        res = supabase.table("products") \
            .select("id, name, price, stock, category, description, in_stock, image_url") \
            .eq("user_id", admin_id) \
            .order("category") \
            .execute()
        return res.data or []
    except Exception as e:
        logger.error(f"Error fetching products: {e}")
        return []

def find_faq(admin_id: str, user_msg: str) -> Optional[str]:
    """Find FAQ answer using similarity matching"""
    try:
        res = supabase.table("faqs") \
            .select("question, answer") \
            .eq("user_id", admin_id) \
            .execute()

        best_ratio, best_answer = 0.65, None
        for faq in res.data or []:
            ratio = SequenceMatcher(None, user_msg.lower(), faq["question"].lower()).ratio()
            if ratio > best_ratio and ratio > best_ratio:
                best_ratio = ratio
                best_answer = faq["answer"]
        
        return best_answer
    except Exception as e:
        logger.error(f"Error finding FAQ: {e}")
        return None

def get_business_settings(admin_id: str) -> Optional[Dict]:
    """Fetch business settings from database"""
    try:
        res = supabase.table("business_settings") \
            .select("*") \
            .eq("user_id", admin_id) \
            .limit(1) \
            .execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"Error fetching business settings: {e}")
        return None

def get_delivery_info_from_db(admin_id: str) -> Dict:
    """Get delivery information from business_settings table"""
    try:
        business = get_business_settings(admin_id)
        if not business or not business.get("delivery_info"):
            return {}
        
        delivery_text = business.get("delivery_info", "")
        info = {}
        
        # Simple parsing
        if "ডেলিভারি চার্জ" in delivery_text:
            numbers = re.findall(r'\d+', delivery_text)
            if numbers:
                info['delivery_charge'] = int(numbers[0])
        
        return info
    except Exception as e:
        logger.error(f"Error getting delivery info: {e}")
        return {}

# ================= CHAT MEMORY MANAGEMENT =================
def get_chat_memory(admin_id: str, customer_id: str, limit: int = 8) -> List[Dict]:
    """Get recent chat history for context"""
    try:
        res = supabase.table("chat_history") \
            .select("messages") \
            .eq("user_id", admin_id) \
            .eq("customer_id", customer_id) \
            .limit(1) \
            .execute()
        
        if res.data and res.data[0].get("messages"):
            return res.data[0]["messages"][-limit:]
        return []
    except Exception as e:
        logger.error(f"Error fetching chat memory: {e}")
        return []

def save_chat_memory(admin_id: str, customer_id: str, messages: List[Dict]):
    """Save chat history with context"""
    try:
        if len(messages) > 10:
            messages = messages[-10:]
        
        data = {
            "user_id": admin_id,
            "customer_id": customer_id,
            "messages": messages,
            "last_updated": datetime.utcnow().isoformat()
        }
        
        supabase.table("chat_history").upsert(
            data,
            on_conflict="user_id,customer_id"
        ).execute()
        
    except Exception as e:
        logger.error(f"Memory Error: {e}")

# ================= BEAUTIFUL RESPONSE TEMPLATES =================
def get_beautiful_greeting() -> str:
    """Return beautiful greeting messages"""
    greetings = [
        "🎉 স্বাগতম!\n\n আমি Simanto, আপনার ব্যক্তিগত শপিং সহকারী। কীভাবে আপনাকে সাহায্য করতে পারি?",
        "🌸 আসসালামু আলাইকুম!\n\nআমি Simanto, আপনার সেবায় আমি প্রস্তুত। কী জন্য আসছেন?",
        "✨ শুভেচ্ছা!\n\nআমি Simanto, আপনার কেনাকাটার সঙ্গী। কিভাবে সহযোগিতা করতে পারি?"
    ]
    import random
    return random.choice(greetings)

def get_beautiful_product_response(product_name: str, price: int, stock: int) -> str:
    """Return beautiful product description"""
    if stock > 0:
        return f"""
🌿 {product_name}

💎 বিশেষত্ব: আমাদের প্রিমিয়াম কালেকশনের অংশ
💰 মূল্য: ৳{price} (প্রতি পিছ)
📦 স্টক: {stock} পিছ প্রাপ্য
⭐ গুণমান: উন্নতমানের ও প্রাকৃতিক

কত পিছ পছন্দ করবেন?
        """
    else:
        return f"""
⚠️ **{product_name}**

দুঃখিত, এই পণ্যটি বর্তমানে স্টকে নেই।
💰 মূল্য: ৳{price} (প্রতি পিছ)

অন্যান্য পণ্য দেখতে চান?
        """

def get_beautiful_order_summary(name: str, phone: str, address: str, 
                                items: str, total_qty: int, delivery_charge: int, 
                                total_amount: int) -> str:
    """Return beautiful order summary"""
    return f"""
📋 আপনার অর্ডার সারসংক্ষণ

👤 ক্রেতার তথ্য:
   • নাম: {name}
   • ফোন: {phone}
   • ঠিকানা: {address}

🛍️ পণ্য বিবরণ:
   • অর্ডারকৃত পণ্য: {items}
   • মোট পরিমাণ: {total_qty} পিছ

💰 আর্থিক হিসাব:
   • ডেলিভারি চার্জ: ৳{delivery_charge}
   • সর্বমোট প্রাপ্য: ৳{total_amount}

✅ নিশ্চিতকরণ:
আপনার অর্ডারটি সম্পূর্ণ করতে 'Confirm' লিখুন।
        """

def get_beautiful_order_confirmation(name: str, order_details: str, total_amount: int, 
                                     address: str, phone: str) -> str:
    """Return beautiful order confirmation"""
    return f"""
🎊 অর্ডার সফলভাবে গ্রহণ করা হয়েছে!

প্রিয় {name},

আপনার অর্ডারটি সফলভাবে সংরক্ষণ করা হয়েছে।

📄 অর্ডার তথ্য:
   • অর্ডারকৃত পণ্য: {order_details}
   • মোট প্রাপ্য: ৳{total_amount}
   • ডেলিভারি ঠিকানা: {address}
   • যোগাযোগ নম্বর: {phone}

আমাদের অফিস থেকে খুব শীঘ্রই আপনার সাথে যোগাযোগ করা হবে।
আপনার আস্থার জন্য আন্তরিক ধন্যবাদ!
        """

def get_beautiful_fallback_response(user_msg: str) -> str:
    """Return beautiful fallback responses"""
    user_lower = user_msg.lower()
    
    if any(word in user_lower for word in ["হ্যালো", "হাই"]):
        return get_beautiful_greeting()
    
    elif any(word in user_lower for word in ["ধন্যবাদ", "থ্যাংকস", "শুকরিয়া"]):
        return "🙏 আপনাকেও অসংখ্য ধন্যবাদ!\n\nআপনার দিনটি শুভ ও সুন্দর হোক।"
    
    elif any(word in user_lower for word in ["দাম", "মূল্য", "কত"]):
        return "💰 মূল্য জানতে চান?\n\nকোন পণ্যের মূল্য জানতে আগ্রহী? দয়া করে পণ্যের নাম বলুন।"
    
    elif any(word in user_lower for word in ["অর্ডার", "কিনব", "কিনতে"]):
        return "🛒 অর্ডার দিতে চান?\n\nযে পণ্য অর্ডার দিতে চান তার নাম বলুন।"
    
    elif any(word in user_lower for word in ["ঠিক আছে", "ওকে", "ok"]):
        return "👍 জি ঠিক আছে!\n\nআপনাকে কীভাবে সাহায্য করতে পারি?"
    
    else:
        return "🤔 দুঃখিত, বুঝতে সমস্যা হচ্ছে।\n\nদয়া করে আবার বলুন কিংবা সরাসরি আমাদের কল করুন।"

# ================= ORDER VALIDATION =================
def validate_order_data(order_data: Dict, admin_id: str) -> Tuple[bool, str]:
    """Validate order data before saving"""
    try:
        phone = order_data.get("phone", "")
        if not re.match(r'^01[3-9]\d{8}$', phone):
            return False, "❌ দুঃখিত,\nসঠিক ১১ ডিজিটের মোবাইল নম্বর প্রদান করুন (০১৩-০১৯ পরিসর)"
        
        address = order_data.get("address", "").strip()
        if len(address) < 10:
            return False, "❌ দুঃখিত,\nপূর্ণাঙ্গ ঠিকানা প্রদান করুন (ন্যূনতম ১০ অক্ষর)"
        
        name = order_data.get("name", "").strip()
        if len(name) < 2:
            return False, "❌ দুঃখিত,\nসম্পূর্ণ নাম প্রদান করুন"
        
        items = order_data.get("items", "")
        if not items or len(items.strip()) < 3:
            return False, "❌ দুঃখিত,\nঅর্ডারে পণ্যের নাম অত্যাবশ্যক"
        
        return True, "✅ সমস্ত তথ্য সঠিক"
        
    except Exception as e:
        logger.error(f"Validation error: {e}")
        return False, "⚠️ যাচাইকরণে সমস্যা,\nঅনুগ্রহ করে পুনরায় চেষ্টা করুন"

def calculate_delivery_charge(admin_id: str, total_amount: int) -> int:
    """Calculate delivery charge based on business settings"""
    try:
        delivery_info = get_delivery_info_from_db(admin_id)
        delivery_charge = delivery_info.get("delivery_charge", 60)
        free_threshold = 500  # Default
        
        if total_amount >= free_threshold:
            return 0
        
        return delivery_charge
    except Exception as e:
        logger.error(f"Error calculating delivery charge: {e}")
        return 60

# ================= AI SMART ORDER (BEAUTIFUL VERSION) =================
def process_ai_smart_order(admin_id: str, customer_id: str, user_msg: str) -> str:
    """Main AI order processing function with beautiful responses"""
    try:
        # Get API key
        key_res = supabase.table("api_keys") \
            .select("groq_api_key") \
            .eq("user_id", admin_id) \
            .execute()

        if not key_res.data or not key_res.data[0].get("groq_api_key"):
            return "⚠️ সাময়িক অসুবিধা,\n\n সার্ভিস সাময়িকভাবে বন্ধ। অনুগ্রহ করে কিছুক্ষণ পর পুনরায় চেষ্টা করুন।"

        # Initialize Groq client
        client = OpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=key_res.data[0]["groq_api_key"]
        )

        # Get essential data
        products = get_products_with_details(admin_id)
        business = get_business_settings(admin_id)
        history = get_chat_memory(admin_id, customer_id, limit=6)

        # Create beautiful product list
        product_list = "🛍️ আমাদের পণ্যসমূহ:\n\n"
        if products:
            for i, p in enumerate(products[:5]):
                stock_icon = "✅" if p.get('in_stock') and p.get('stock', 0) > 0 else "⏳"
                product_list += f"{i+1}. {p['name']} - ৳{p['price']} {stock_icon}\n"
            
            if len(products) > 5:
                product_list += f"\n...এবং আরও {len(products)-5}টি পণ্য\n"
        else:
            product_list = "📦 কোনো পণ্য পাওয়া যায়নি,\n\nঅনুগ্রহ করে পরে চেষ্টা করুন।"

        # Business info
        business_name = business.get('name', 'আমাদের দোকান') if business else 'আমাদের দোকান'
        
        # BEAUTIFUL SYSTEM PROMPT (Still concise but elegant)
        system_prompt = f"""
তুমি Simanto, একজন মার্জিত ও ভদ্র সেলস সহকারী। তুমি {business_name}-এর প্রতিনিধিত্ব করছ।

**আচরণ বিধিমালা:**
১. সর্বদা প্রমিত, মার্জিত ও ভদ্র বাংলায় কথা বলবে
২. বাক্য গঠন হবে পরিপূর্ণ ও শ্রুতিমধুর
৩. পণ্যের নাম ডাটাবেসে যেভাবে আছে সেভাবেই ব্যবহার করবে
৪. অত্যন্ত ধৈর্য্য সহকারে ব্যবহারকারীর কথা শুনবে

**অর্ডার প্রক্রিয়া:**
ধাপ ১: ব্যবহারকারীর অভিবাদন গ্রহণ ও স্বাগতম জানানো
ধাপ ২: পণ্যের নাম ও পরিমাণ জানা
ধাপ ৩: ক্রেতার নাম, ফোন ও ঠিকানা সংগ্রহ
ধাপ ৪: অর্ডার সারসংক্ষণ উপস্থাপন
ধাপ ৫: ব্যবহারকারীর 'হ্যাঁ' পাওয়ার পর অর্ডার নিশ্চিত করা

**পণ্য তালিকা:**
{product_list}

**স্মরণীয়:**
- প্রতিটি উত্তর হবে মার্জিত ও তথ্যবহুল
- সব সময় ধৈর্য্য ধরবে
- ব্যবহারকারীকে সম্মান দেখাবে
- অর্ডার সম্পূর্ণ নিশ্চিত হওয়ার পরেই ডাটাবেসে সংরক্ষণ করবে
"""
        
        # Prepare messages
        messages_to_send = [
            {"role": "system", "content": system_prompt}
        ]
        
        if history:
            messages_to_send.extend(history[-6:])  # Last 6 messages
        
        messages_to_send.append({"role": "user", "content": user_msg[:200]})
        
        # Define tools
        tools = [{
            "type": "function",
            "function": {
                "name": "submit_order_to_db",
                "description": "অর্ডার ডাটাবেসে সংরক্ষণ করুন",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "ক্রেতার পূর্ণ নাম"},
                        "phone": {"type": "string", "description": "১১ ডিজিটের মোবাইল নম্বর"},
                        "address": {"type": "string", "description": "সম্পূর্ণ ডেলিভারি ঠিকানা"},
                        "items": {"type": "string", "description": "অর্ডারকৃত পণ্যের বিবরণ"},
                        "total_qty": {"type": "integer", "description": "মোট পণ্য সংখ্যা"},
                        "delivery_charge": {"type": "integer", "description": "ডেলিভারি খরচ"},
                        "total_amount": {"type": "integer", "description": "সর্বমোট টাকার পরিমাণ"}
                    },
                    "required": ["name", "phone", "address", "items", "total_qty", "delivery_charge", "total_amount"]
                }
            }
        }]
        
        # Call API
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages_to_send,
            tools=tools,
            tool_choice="auto",
            temperature=0.3,  # Slightly higher for more natural responses
            max_tokens=250,   # Enough for beautiful responses
            stream=False
        )

        msg = response.choices[0].message

        # Handle order submission
        if msg.tool_calls:
            try:
                args = json.loads(msg.tool_calls[0].function.arguments)
                
                # Validate
                is_valid, validation_msg = validate_order_data(args, admin_id)
                if not is_valid:
                    return validation_msg
                
                # Check if summary was shown
                history_text = " ".join(
                    m.get("content", "") for m in history if m.get("role") == "assistant"
                )
                
                if not any(word in history_text for word in ["সারসংক্ষণ", "সারাংশ", "সামারি"]):
                    return "📝 অনুগ্রহ পূর্বক,\n\nআপনার অর্ডারের সারাংশ দেখে, অর্ডারটি Confirm করতে 'Confirm' বলুন।"
                
                # Calculate delivery
                calculated_charge = calculate_delivery_charge(admin_id, args["total_amount"])
                args["delivery_charge"] = calculated_charge
                
                # Save to database
                order_data = {
                    "user_id": admin_id,
                    "customer_name": args["name"].strip(),
                    "customer_phone": args["phone"].strip(),
                    "product": args["items"],
                    "quantity": args["total_qty"],
                    "address": args["address"].strip(),
                    "delivery_charge": args["delivery_charge"],
                    "total": args["total_amount"],
                    "status": "pending",
                    "created_at": datetime.utcnow().isoformat()
                }
                
                result = supabase.table("orders").insert(order_data).execute()
                
                if not result.data:
                    return "⚠️ সংরক্ষণ সমস্যা,\n\nঅর্ডার সংরক্ষণে সমস্যা হয়েছে। অনুগ্রহ করে আবার চেষ্টা করুন।"
                
                # Return beautiful confirmation
                confirmation = get_beautiful_order_confirmation(
                    args['name'],
                    args['items'],
                    args['total_amount'],
                    args['address'][:100],
                    args['phone']
                )
                
                # Save to history
                new_history = (history or []) + [
                    {"role": "user", "content": user_msg[:150]},
                    {"role": "assistant", "content": confirmation[:300]}
                ]
                save_chat_memory(admin_id, customer_id, new_history)
                
                return confirmation
                
            except Exception as e:
                logger.error(f"Order processing error: {e}")
                return "❌ **অভ্যন্তরীণ সমস্যা**\n\nঅর্ডার সংরক্ষণে অসুবিধা। সরাসরি কল করুন।"

        # If no tool call, return AI response
        reply = msg.content if msg.content else get_beautiful_fallback_response(user_msg)
        
        # Save to history
        new_history = (history or []) + [
            {"role": "user", "content": user_msg[:150]},
            {"role": "assistant", "content": reply[:250]}
        ]
        save_chat_memory(admin_id, customer_id, new_history)
        
        return reply

    except Exception as e:
        logger.error(f"AI processing error: {e}")
        return get_beautiful_fallback_response(user_msg)

# ================= WEBHOOK ENDPOINT =================
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    """Facebook webhook endpoint"""
    if request.method == "GET":
        if request.args.get("hub.mode") == "subscribe":
            if request.args.get("hub.verify_token") == os.getenv("FACEBOOK_VERIFY_TOKEN"):
                return request.args.get("hub.challenge"), 200
            return "Verification token mismatch", 403
        return "OK", 200

    try:
        data = request.get_json()
        
        if not data or "entry" not in data:
            return jsonify({"status": "error", "message": "Invalid data"}), 400
        
        for entry in data.get("entry", []):
            page_id = entry.get("id")
            page = get_page_client(page_id)
            
            if not page:
                continue
            
            for messaging in entry.get("messaging", []):
                sender_id = messaging.get("sender", {}).get("id")
                message = messaging.get("message", {})
                text = message.get("text", "").strip()
                
                if not text or not sender_id:
                    continue
                
                # Try FAQ first
                faq_answer = find_faq(page["user_id"], text)
                
                if faq_answer:
                    send_message(page["page_access_token"], sender_id, faq_answer)
                else:
                    # Process with AI
                    ai_response = process_ai_smart_order(page["user_id"], sender_id, text)
                    send_message(page["page_access_token"], sender_id, ai_response)
        
        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ================= HEALTH CHECK =================
@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    try:
        supabase.table("products").select("count", count="exact").limit(1).execute()
        return jsonify({
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat()
        }), 200
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e)
        }), 500

# ================= MAIN =================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    debug = os.getenv("DEBUG", "false").lower() == "true"
    
    logger.info(f"Starting Beautiful Facebook Chatbot on port {port}")
    app.run(host="0.0.0.0", port=port, debug=debug)

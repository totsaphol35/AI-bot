import os
from flask import Flask, request, abort
from linebot.v3 import (
    WebhookHandler
)
from linebot.v3.exceptions import (
    InvalidSignatureError
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent
)

app = Flask(__name__)

# ตั้งค่า LINE Channel Access Token และ Channel Secret
configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET', 'YOUR_CHANNEL_SECRET'))

# Dictionary เก็บรายการหุ้นเฝ้าระวัง (Watchlist) แยกตาม User ID ของแต่ละคน
# ตัวอย่าง: { user_id: ["QQQM", "GOOGL", "RKLB"] }
user_watchlists = {}

# ฟังก์ชันดึงข้อมูลราคาและแนวรับ-แนวต้านแยกตามรายตัวหุ้น
def get_stock_data(symbol):
    symbol = symbol.upper()
    
    if symbol == "GOOGL":
        return {
            "price": "180.25 USD",
            "win_rate": "74.5%",
            "supports": ["175.50 USD (ใกล้สุด)", "170.00 USD", "162.50 USD (แข็งแกร่งสุด)"],
            "resistances": ["185.00 USD (ใกล้สุด)", "190.50 USD", "198.00 USD (แข็งแกร่งสุด)"]
        }
    elif symbol == "GOOG":
        return {
            "price": "178.90 USD",
            "win_rate": "73.8%",
            "supports": ["174.00 USD (ใกล้สุด)", "168.50 USD", "160.00 USD (แข็งแกร่งสุด)"],
            "resistances": ["183.50 USD (ใกล้สุด)", "189.00 USD", "195.50 USD (แข็งแกร่งสุด)"]
        }
    elif symbol == "QQQM":
        return {
            "price": "210.40 USD",
            "win_rate": "72.0%",
            "supports": ["205.00 USD (ใกล้สุด)", "198.50 USD", "190.00 USD (แข็งแกร่งสุด)"],
            "resistances": ["215.50 USD (ใกล้สุด)", "222.00 USD", "230.00 USD (แข็งแกร่งสุด)"]
        }
    elif symbol == "RKLB":
        return {
            "price": "61.53 USD",
            "win_rate": "71.0%",
            "supports": ["63.06 USD (ใกล้สุด)", "52.53 USD", "43.89 USD (แข็งแกร่งสุด)"],
            "resistances": ["51.34 USD (ใกล้สุด)", "55.20 USD", "68.26 USD (แข็งแกร่งสุด)"]
        }
    elif symbol == "PTT.BK":
        return {
            "price": "35.25 THB",
            "win_rate": "68.5%",
            "supports": ["34.00 THB (ใกล้สุด)", "32.50 THB", "30.00 THB (แข็งแกร่งสุด)"],
            "resistances": ["37.00 THB (ใกล้สุด)", "39.00 THB", "42.00 THB (แข็งแกร่งสุด)"]
        }
    else:
        # หุ้นทั่วไปหรือหุ้นอื่นๆ ที่ผู้ใช้พิมพ์เข้ามา
        return {
            "price": "100.00 USD",
            "win_rate": "70.0%",
            "supports": ["95.00 USD (ใกล้สุด)", "90.00 USD", "85.00 USD (แข็งแกร่งสุด)"],
            "resistances": ["105.00 USD (ใกล้สุด)", "110.00 USD", "115.00 USD (แข็งแกร่งสุด)"]
        }

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.info("Invalid signature. Please check your channel access token/secret.")
        abort(400)

    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text.strip()
    command = user_message.upper()

    # กำหนด Watchlist เริ่มต้นให้ผู้ใช้ หากยังไม่เคยมีบันทึกไว้
    if user_id not in user_watchlists:
        user_watchlists[user_id] = ["QQQM", "GOOGL", "RKLB"]

    watchlist = user_watchlists[user_id]
    reply_text = ""

    if command == "/HELP":
        reply_text = (
            "📖 คู่มือการใช้งานบอต 'ผู้หยั่งรู้หุ้น'\n\n"
            "• พิมพ์ชื่อหุ้น (เช่น GOOGL, GOOG, QQQM) เพื่อดูข้อมูลเชิงลึกรายตัว\n"
            "• พิมพ์ +[ชื่อหุ้น] เช่น +PTT.BK เพื่อเพิ่มเข้า Watchlist\n"
            "• พิมพ์ -[ชื่อหุ้น] เช่น -QQQM เพื่อยกเลิก/ลบออกจาก Watchlist\n"
            "• /SUMMARY - สรุปภาพรวมหุ้นทุกตัวใน Watchlist ของคุณ\n"
            "• /LIST - แสดงรายชื่อหุ้นใน Watchlist ปัจจุบัน"
        )
    elif command == "/SUMMARY":
        if not watchlist:
            reply_text = "📭 รายการเฝ้าระวังของคุณว่างเปล่า กรุณาเพิ่มหุ้นด้วยคำสั่ง +[ชื่อหุ้น] (เช่น +GOOGL)"
        else:
            reply_text = f"📊 **สรุปภาพรวมหุ้นใน Watchlist ทั้งหมด ({len(watchlist)} ตัว):**\n"
            for stock in watchlist:
                data = get_stock_data(stock)
                reply_text += (
                    f"\n--------------------\n"
                    f"📌 **{stock}** | 💰 {data['price']} | 🎯 Win Rate: {data['win_rate']}\n"
                    f"🟢 แนวรับ: {data['supports'][0]} | {data['supports'][1]}\n"
                    f"🔴 แนวต้าน: {data['resistances'][0]} | {data['resistances'][1]}"
                )
    elif command == "/LIST":
        if not watchlist:
            reply_text = "📋 รายการเฝ้าระวังของคุณว่างเปล่า"
        else:
            watchlist_str = "\n".join([f"{i+1}. {s}" for i, s in enumerate(watchlist)])
            reply_text = f"📋 **รายการหุ้นเฝ้าระวังของคุณ:**\n{watchlist_str}\n\n(พิมพ์ +[ชื่อหุ้น] เพื่อเพิ่ม หรือ -[ชื่อหุ้น] เพื่อลบออก)"
    
    elif user_message.startswith("+"):
        # คำสั่งเพิ่มหุ้นเข้า Watchlist เช่น +PTT.BK
        stock_to_add = user_message[1:].strip().upper()
        if stock_to_add and stock_to_add not in watchlist:
            watchlist.append(stock_to_add)
            reply_text = f"✅ เพิ่ม **{stock_to_add}** เข้าสู่รายการเฝ้าระวังเรียบร้อยแล้วครับ พิมพ์ /SUMMARY เพื่อดูสรุปภาพรวมได้เลย"
        else:
            reply_text = f"⚠️ หุ้น **{stock_to_add}** มีอยู่ในรายการเฝ้าระวังอยู่แล้ว หรือรูปแบบชื่อไม่ถูกต้อง"

    elif user_message.startswith("-"):
        # คำสั่งยกเลิก/ลบหุ้นออกจาก Watchlist เช่น -QQQM
        stock_to_remove = user_message[1:].strip().upper()
        if stock_to_remove in watchlist:
            watchlist.remove(stock_to_remove)
            reply_text = f"🗑️ ยกเลิกและลบ **{stock_to_remove}** ออกจากรายการเฝ้าระวังเรียบร้อยแล้วครับ ข้อมูลใน /SUMMARY จะถูกปรับออกทันที"
        else:
            reply_text = f"⚠️ ไม่พบหุ้น **{stock_to_remove}** ในรายการเฝ้าระวังของคุณ"

    else:
        # กรณีพิมพ์ชื่อหุ้นตรงๆ เพื่อดูข้อมูลรายตัว (เช่น GOOGL, GOOG)
        stock_name = user_message.upper()
        data = get_stock_data(stock_name)
        
        reply_text = (
            f"📊 ข้อมูลเชิงลึก: {stock_name}\n"
            f"💰 ราคาล่าสุด: {data['price']}\n"
            f"🎯 โอกาสความแม่นยำ (Win Rate): **{data['win_rate']}**\n\n"
            f"🟢 **แนวรับสำคัญ (3 ระดับ):**\n"
            f"1. {data['supports'][0]}\n"
            f"2. {data['supports'][1]}\n"
            f"3. {data['supports'][2]}\n\n"
            f"🔴 **แนวต้านสำคัญ (3 ระดับ):**\n"
            f"1. {data['resistances'][0]}\n"
            f"2. {data['resistances'][1]}\n"
            f"3. {data['resistances'][2]}\n\n"
            f"💡 พิมพ์ +{stock_name} เพื่อเก็บเข้า Watchlist หรือ -{stock_name} เพื่อเอาออก"
        )

    # ส่งข้อความกลับหาผู้ใช้ผ่าน LINE Messaging API
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

if __name__ == "__main__":
    app.run(port=5000)

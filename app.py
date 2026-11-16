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
# (แนะนำให้เก็บไว้ใน Environment Variables เพื่อความปลอดภัย)
configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET', 'YOUR_CHANNEL_SECRET'))

# Dictionary สำหรับเก็บบันทึกสถานะหุ้นตัวล่าสุดที่ผู้ใช้แต่ละคนกำลังเช็คอยู่ (User State)
user_last_stock = {}

@app.route("/callback", methods=['POST'])
def callback():
    # ดึงค่า signature จาก header
    signature = request.headers['X-Line-Signature']

    # ดึงเนื้อหา body เป็น string
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    # ตรวจสอบลายเซ็นและจัดการ webhook
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

    reply_text = ""

    if command == "/HELP":
        reply_text = (
            "📖 คู่มือการใช้งานบอต 'ผู้หยั่งรู้หุ้น'\n\n"
            "• พิมพ์ชื่อหุ้น (เช่น QQQM, PTT.BK, RKLB) เพื่อดูข้อมูลเชิงลึก แนวรับ และแนวต้าน\n"
            "• /SUMMARY - สรุปข้อมูลเชิงลึกและระดับแนวรับ/แนวต้านของหุ้นตัวล่าสุดที่คุณกำลังเช็คอยู่\n"
            "• /LIST - แสดงรายการหุ้นเฝ้าระวังทั้งหมด\n"
            "• /HELP - แสดงคู่มือการใช้งานนี้"
        )
    elif command == "/SUMMARY":
        # ดึงหุ้นตัวล่าสุดที่ผู้ใช้เคยค้นหา
        current_stock = user_last_stock.get(user_id)
        if current_stock:
            # จำลองข้อมูลสรุปสำหรับหุ้นตัวล่าสุดที่กำลังเช็คอยู่
            reply_text = (
                f"📊 สรุปข้อมูลเชิงลึก: {current_stock}\n"
                f"💰 ราคาล่าสุด: 61.53 USD\n"
                f"🎯 โอกาสความแม่นยำ (Win Rate): **71.0%**\n\n"
                f"🟢 **แนวรับสำคัญ (3 ระดับ):**\n"
                f"1. 63.06 USD (ใกล้สุด)\n"
                f"2. 52.53 USD\n"
                f"3. 43.89 USD (แข็งแกร่งสุด)\n\n"
                f"🔴 **แนวต้านสำคัญ (3 ระดับ):**\n"
                f"1. 51.34 USD (ใกล้สุด)\n"
                f"2. 55.20 USD\n"
                f"3. 68.26 USD (แข็งแกร่งสุด)"
            )
        else:
            reply_text = "⚠️ กรุณาพิมพ์ค้นหาชื่อหุ้น (เช่น RKLB หรือ QQQM) ก่อนเรียกใช้งานคำสั่ง /SUMMARY ครับ"
            
    elif command == "/LIST":
        reply_text = (
            "📋 รายการหุ้นเฝ้าระวัง (Watchlist):\n"
            "1. QQQM (Invesco Nasdaq 100 ETF)\n"
            "2. PTT.BK (บมจ. ปตท.)\n"
            "3. RKLB (Rocket Lab USA)\n\n"
            "พิมพ์ชื่อหุ้นเหล่านี้เพื่อดูรายละเอียดเพิ่มเติมได้ทันทีครับ"
        )
    else:
        # กรณีผู้ใช้พิมพ์ชื่อหุ้นเข้ามา (เช่น RKLB, QQQM, PTT.BK)
        stock_name = user_message.upper()
        # บันทึกสถานะหุ้นตัวล่าสุดของผู้ใช้คนนี้
        user_last_stock[user_id] = stock_name
        
        reply_text = (
            f"📊 ข้อมูลเชิงลึก {stock_name}\n"
            f"💰 ราคาล่าสุด: 61.53 USD\n"
            f"🎯 โอกาสความแม่นยำ (Win Rate): **71.0%**\n\n"
            f"🟢 **แนวรับสำคัญ (3 ระดับ):**\n"
            f"1. 63.06 USD (ใกล้สุด)\n"
            f"2. 52.53 USD\n"
            f"3. 43.89 USD (แข็งแกร่งสุด)\n\n"
            f"🔴 **แนวต้านสำคัญ (3 ระดับ):**\n"
            f"1. 51.34 USD (ใกล้สุด)\n"
            f"2. 55.20 USD\n"
            f"3. 68.26 USD (แข็งแกร่งสุด)"
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

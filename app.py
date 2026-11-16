import os
import requests
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import yfinance as yf
import pandas as pd

app = Flask(__name__)

# ดึง Token และ Secret จาก Environment Variables บน Render
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "YOUR_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "YOUR_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Discord Webhook URL ของคุณ
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1570698801839210506/Bq3O3AoHYUiAqCptfD6xpdW0idBfWafurPkz3DY53_qYxR7gOAFlGF4vFWK4kOC1U1-d"

def send_discord_alert(message):
    """ฟังก์ชันสำหรับส่งข้อความแจ้งเตือนเข้า Discord Webhook"""
    payload = {"content": message}
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        return response.status_code == 204
    except Exception as e:
        print(f"Discord alert error: {e}")
        return False

@app.route("/")
def home():
    return "Totsaphol ผู้หยั่งรู้หุ้น Server is running!"

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    raw_text = event.message.text.strip()
    text = raw_text.upper()
    reply_text = ""

    try:
        # 1. คำสั่งช่วยเหลือ / คู่มือการใช้งาน
        if text in ["/HELP", "HELP", "เมนู", "คำสั่ง"]:
            reply_text = (
                "🤖 คู่มือการใช้งาน Totsaphol ผู้หยั่งรู้หุ้น:\n\n"
                "• พิมพ์ชื่อย่อหุ้น/ทอง เช่น QQQM, GC=F, PTT.BK, SCHG เพื่อดูราคา แนวรับ และแนวต้าน\n"
                "• พิมพ์ XAUUSD เพื่อเช็คราคาทองคำ\n"
                "•พิมพ์ /SUMMARY เพื่อดูสรุปภาพรวมตลาด"
            )

        # 2. คำสั่งสรุปภาพรวมตลาด
        elif text in ["/SUMMARY", "SUMMARY", "สรุปตลาด"]:
            symbols = ["GC=F", "QQQM", "AAPL", "PTT.BK"]
            reply_text = "📊 สรุปภาพรวมตลาด:\n"
            for sym in symbols:
                try:
                    stock = yf.Ticker(sym)
                    df = stock.history(period="1d")
                    if not df.empty:
                        price = df['Close'].iloc[-1]
                        reply_text += f"- {sym}: {price:.2f}\n"
                except:
                    pass

        # 3. กรณีพิมพ์ถามหุ้นหรือทองคำรายตัว
        else:
            target_symbol = "GC=F" if text == "XAUUSD" else raw_text
            
            stock = yf.Ticker(target_symbol)
            df = stock.history(period="30d")
            
            if not df.empty:
                current_price = df['Close'].iloc[-1]
                support = df['Low'].tail(20).min()
                resistance = df['High'].tail(20).max()
                
                currency_unit = "บาท" if ".BK" in target_symbol.upper() else "USD"
                
                reply_text = (
                    f"📊 ข้อมูล {target_symbol.upper()}\n"
                    f"ราคาร่าสุด: {current_price:.2f} {currency_unit}\n"
                    f"🟢 แนวรับ (20D): {support:.2f} {currency_unit}\n"
                    f"🔴 แนวต้าน (20D): {resistance:.2f} {currency_unit}"
                )

                # ส่งข้อมูลเข้า Discord ทันทีที่มีการค้นหา (แก้ไขวงเล็บ f-string ให้ถูกต้องแล้ว)
                discord_msg = (
                    f"🔔 **รายงานข้อมูลสินทรัพย์**: `{target_symbol.upper()}`\n"
                    f"• ราคาร่าสุด: `{current_price:.2f} {currency_unit}`\n"
                    f"• แนวรับ (20D): `{support:.2f} {currency_unit}`\n"
                    f"• แนวต้าน (20D): `{resistance:.2f} {currency_unit}`"
                )
                send_discord_alert(discord_msg)

            else:
                reply_text = f"❌ ไม่พบข้อมูลสำหรับ '{raw_text}' กรุณาตรวจสอบชื่อย่อใหม่อีกครั้งครับ"

    except Exception as e:
        reply_text = "ขออภัยครับ เกิดข้อผิดพลาดในการดึงข้อมูลจากระบบ"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

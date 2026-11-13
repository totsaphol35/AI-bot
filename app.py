import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import yfinance as yf
import pandas as pd

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "YOUR_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "YOUR_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

@app.route("/")
def home():
    return "Totsaphol ผู้หยั่งรู้หุ้น Server is running!"

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
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
                "• พิมพ์ชื่อย่อหุ้น/ทอง เช่น QQQM, GC=F, PTT.BK เพื่อดูราคา แนวรับ และแนวต้าน\n"
                "• พิมพ์ XAUUSD เพื่อเช็คราคาทองคำ\n"
                "• พิมพ์ /summary เพื่อดูสรุปภาพรวมตลาด"
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

        # 3. กรณีพิมพ์ถามหุ้นหรือทองคำรายตัว (เช่น QQQM, PTT.BK, XAUUSD)
        else:
            target_symbol = "GC=F" if text == "XAUUSD" else raw_text
            
            stock = yf.Ticker(target_symbol)
            df = stock.history(period="30d")
            
            if not df.empty:
                current_price = df['Close'].iloc[-1]
                support = df['Low'].tail(20).min()
                resistance = df['High'].tail(20).max()
                
                # ตรวจสอบว่าเป็นสกุลเงินบาทหรือดอลลาร์
                currency_unit = "บาท" if ".BK" in target_symbol.upper() else "USD"
                
                reply_text = (
                    f"📊 ข้อมูล {target_symbol.upper()}\n"
                    f"ราคาร่าสุด: {current_price:.2f} {currency_unit}\n"
                    f"🟢 แนวรับ (20D): {support:.2f} {currency_unit}\n"
                    f"🔴 แนวต้าน (20D): {resistance:.2f} {currency_unit}"
                )
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

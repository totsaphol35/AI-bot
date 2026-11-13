import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import yfinance as yf
import pandas as pd

app = Flask(__name__)

# กำหนด Token และ Secret ของ LINE OA (ดึงจาก Environment Variables บน Render เพื่อความปลอดภัย)
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
    text = event.message.text.strip().upper()
    raw_text = event.message.text.strip()
    reply_text = ""

    try:
        # 1. คำสั่งช่วยเหลือ (/HELP หรือ เมนู)
        if text in ["/HELP", "เมนู", "คำสั่ง"]:
            reply_text = (
                "🤖 คู่มือการใช้งาน Totsaphol ผู้หยั่งรู้หุ้น:\n\n"
                "1. พิมพ์ชื่อย่อหุ้น/ทอง เช่น QQQM, GC=F, AOT.BK เพื่อดูราคาและแนวรับแนวต้าน\n"
                "2. พิมพ์ /SUMMARY เพื่อดูสรุปราคาสินทรัพย์ยอดฮิต\n"
                "3. พิมพ์ /TECH [ชื่อหุ้น] เพื่อดูอินดิเคเตอร์ทางเทคนิค (เช่น /TECH AAPL)\n"
                "4. พิมพ์ RSI เพื่อดูค่า RSI เบื้องต้น"
            )

        # 2. คำสั่งสรุปภาพรวมตลาด (/SUMMARY)
        elif text in ["/SUMMARY", "!MARKET", "สรุปตลาด"]:
            symbols = ["GC=F", "QQQM", "AAPL", "AOT.BK"]
            reply_text = "📊 สรุปภาพรวมตลาดวันนี้:\n"
            for sym in symbols:
                try:
                    stock = yf.Ticker(sym)
                    df = stock.history(period="1d")
                    if not df.empty:
                        price = df['Close'].iloc[-1]
                        reply_text += f"- {sym}: {price:.2f}\n"
                except:
                    pass

        # 3. คำสั่งวิเคราะห์เทคนิคเจาะลึก (/TECH [SYMBOL])
        elif text.startswith("/TECH "):
            parts = raw_text.split(" ")
            if len(parts) > 1:
                sym = parts[1].upper()
                try:
                    stock = yf.Ticker(sym)
                    df = stock.history(period="60d")
                    if not df.empty:
                        current_price = df['Close'].iloc[-1]
                        ema_20 = df['Close'].ewm(span=20).mean().iloc[-1]
                        support = df['Low'].tail(20).min()
                        resistance = df['High'].tail(20).max()
                        
                        reply_text = (
                            f"📈 วิเคราะห์เทคนิค {sym}\n"
                            f"• ราคาปัจจุบัน: {current_price:.2f}\n"
                            f"• EMA 20: {ema_20:.2f}\n"
                            f"• แนวรับ (20 วัน): {support:.2f}\n"
                            f"• แนวต้าน (20 วัน): {resistance:.2f}"
                        )
                    else:
                        reply_text = f"ไม่พบข้อมูลของสินทรัพย์ {sym}"
                except Exception as e:
                    reply_text = f"เกิดข้อผิดพลาดในการดึงข้อมูล {sym}"

        # 4. คำสั่งดึงราคาหุ้น / ทองคำรายตัวแบบเดิม (เช่น QQQM, XAUUSD, AOT.BK, RSI)
        else:
            # แปลง XAUUSD เป็น GC=F อัตโนมัติ
            target_symbol = "GC=F" if text == "XAUUSD" else text
            
            stock = yf.Ticker(target_symbol)
            df = stock.history(period="30d")
            
            if not df.empty:
                current_price = df['Close'].iloc[-1]
                support = df['Low'].tail(20).min()
                resistance = df['High'].tail(20).max()
                
                reply_text = (
                    f"📊 ข้อมูล {target_symbol}\n"
                    f"ราคาร่าสุด: {current_price:.2f} USD\n"
                    f"🟢 แนวรับ (20D): {support:.2f} USD\n"
                    f"🔴 แนวต้าน (20D): {resistance:.2f} USD"
                )
            else:
                reply_text = f"ไม่พบข้อมูลสำหรับ '{target_symbol}' กรุณาตรวจสอบชื่อย่อใหม่อีกครั้งครับ"

    except Exception as e:
        reply_text = "ขออภัยครับ เกิดข้อผิดพลาดในการดึงข้อมูลจากระบบ"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

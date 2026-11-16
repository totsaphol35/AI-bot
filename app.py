import os
import requests
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import yfinance as yf
import pandas as pd
import numpy as np

app = Flask(__name__)

# ดึง Token และ Secret จาก Environment Variables บน Render
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "YOUR_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "YOUR_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Discord Webhook URL ของคุณ
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1570698801839210506/Bq3O3AoHYUiAqCptfD6xpdW0idBfWafurPkz3DY53_qYxR7gOAFlGF4vFWK4kOC1U1-d"

WATCHLIST = {}

def send_discord_alert(message):
    """ฟังก์ชันสำหรับส่งข้อความแจ้งเตือนเข้า Discord Webhook"""
    payload = {"content": message}
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        return response.status_code == 204
    except Exception as e:
        print(f"Discord alert error: {e}")
        return False

def calculate_rsi(data, window=14):
    """คำนวณค่า RSI (Relative Strength Index)"""
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]

def analyze_market_data(df):
    """คำนวณแนวรับ แนวต้าน 3 ระดับ, RSI, Trend และ Win Rate"""
    closes = df['Close']
    highs = df['High'].tail(20).values
    lows = df['Low'].tail(20).values
    current = closes.iloc[-1]

    # คำนวณแนวรับ 3 ระดับ
    support_1 = lows.max() * 0.99 if len(lows) > 0 else current * 0.98
    support_2 = np.percentile(lows, 50)
    support_3 = lows.min()
    supports = sorted([support_1, support_2, support_3], reverse=True)

    # คำนวณแนวต้าน 3 ระดับ
    resistance_1 = highs.min() * 1.01 if len(highs) > 0 else current * 1.02
    resistance_2 = np.percentile(highs, 50)
    resistance_3 = highs.max()
    resistances = sorted([resistance_1, resistance_2, resistance_3])

    # คำนวณ RSI และค่าเฉลี่ยเคลื่อนที่ (EMA 50 / Trend)
    rsi_val = calculate_rsi(closes)
    ema_50 = closes.ewm(span=50, adjust=False).mean().iloc[-1] if len(closes) >= 50 else current
    
    # ประเมินสภาพแนวโน้มและ Win Rate เชิงสถิติ
    trend = "ขาขึ้น (Bullish) 🟢" if current > ema_50 else "ขาลง (Bearish) 🔴"
    
    # ปรับ Win Rate ตามเงื่อนไข RSI (ถ้าราคาใกล้แนวรับ + RSI Oversold จะเพิ่มโอกาสชนะ)
    volatility = np.std(closes.tail(20)) / np.mean(closes.tail(20))
    base_win_rate = max(55.0, min(88.0, round(80 - (volatility * 100), 1)))
    
    if rsi_val < 30:
        win_rate = min(92.0, base_win_rate + 8.0) # โอกาสเด้งสูงเมื่อ Oversold
    elif rsi_val > 70:
        win_rate = max(50.0, base_win_rate - 5.0)
    else:
        win_rate = base_win_rate

    return supports, resistances, rsi_val, trend, win_rate

@app.route("/")
def home():
    return "Totsaphol ผู้หยั่งรู้หุ้น (Advanced Pro) Server is running!"

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
    user_id = event.source.user_id
    reply_text = ""

    try:
        if text in ["/HELP", "HELP", "เมนู", "คำสั่ง"]:
            reply_text = (
                "🤖 คู่มือ Totsaphol (Advanced Pro):\n\n"
                "• พิมพ์ชื่อหุ้น เช่น QQQM, GC=F ดูแนวรับ-ต้าน 3 ระดับ, RSI และ Win Rate\n"
                "• /ADD [หุ้น] - เพิ่มเข้าลิสต์เฝ้าระวัง\n"
                "• /REMOVE [หุ้น] - ยกเลิกเฝ้าระวัง\n"
                "• /LIST - ดูรายการที่เฝ้าระวัง\n"
                "• /SUMMARY - สรุปภาพรวมตลาด"
            )

        elif text in ["/LIST", "LIST", "รายการเฝ้าระวัง"]:
            user_list = WATCHLIST.get(user_id, {})
            if user_list:
                reply_text = "📋 รายการหุ้นที่คุณเฝ้าระวังอยู่:\n" + "".join([f"- {s}\n" for s in user_list.keys()])
            else:
                reply_text = "📭 ยังไม่มีรายการเฝ้าระวัง (เพิ่มด้วยคำสั่ง `/ADD [ชื่อหุ้น]`)"

        elif text.startswith("/ADD "):
            parts = text.split()
            if len(parts) >= 2:
                sym = parts[1]
                if user_id not in WATCHLIST:
                    WATCHLIST[user_id] = {}
                WATCHLIST[user_id][sym] = "ACTIVE"
                reply_text = f"✅ เพิ่มหุ้น `{sym}` เข้า Watchlist แล้ว บอตจะส่งแจ้งเตือนเข้า Discord เมื่อใกล้โซนสำคัญ!"
            else:
                reply_text = "⚠️ รูปแบบไม่ถูกต้อง เช่น: `/ADD QQQM`"

        elif text.startswith("/REMOVE ") or text.startswith("/DEL "):
            parts = text.split()
            if len(parts) >= 2:
                sym = parts[1]
                if user_id in WATCHLIST and sym in WATCHLIST[user_id]:
                    del WATCHLIST[user_id][sym]
                    reply_text = f"🗑️ ยกเลิกเฝ้าระวังหุ้น `{sym}` เรียบร้อยครับ"
                else:
                    reply_text = f"❌ ไม่พบหุ้น `{sym}` ในรายการของคุณ"
            else:
                reply_text = "⚠️ รูปแบบไม่ถูกต้อง เช่น: `/REMOVE QQQM`"

        elif text in ["/SUMMARY", "SUMMARY", "สรุปตลาด"]:
            symbols = ["GC=F", "QQQM", "AAPL", "PTT.BK"]
            reply_text = "📊 สรุปภาพรวมตลาด (Pro View):\n"
            for sym in symbols:
                try:
                    stock = yf.Ticker(sym)
                    df = stock.history(period="30d")
                    if not df.empty:
                        price = df['Close'].iloc[-1]
                        rsi = calculate_rsi(df['Close'])
                        reply_text += f"• {sym}: {price:.2f} (RSI: {rsi:.1f})\n"
                except:
                    pass

        else:
            target_symbol = "GC=F" if text == "XAUUSD" else raw_text
            stock = yf.Ticker(target_symbol)
            df = stock.history(period="60d") # ใช้ข้อมูล 60 วันเพื่อความแม่นยำของ EMA และ RSI
            
            if not df.empty:
                current_price = df['Close'].iloc[-1]
                supports, resistances, rsi_val, trend, win_rate = analyze_market_data(df)
                currency_unit = "บาท" if ".BK" in target_symbol.upper() else "USD"
                
                # ข้อความประเมินสถานะ RSI
                rsi_status = "🔥 Oversold (มีโอกาสเด้งซื้อ)" if rsi_val < 30 else ("⚠️ Overbought (ระวังแรงขาย)" if rsi_val > 70 else "⚖️ ปกติ (Neutral)")

                reply_text = (
                    f"📊 **วิเคราะห์เชิงลึก {target_symbol.upper()}**\n"
                    f"💰 ราคาร่าสุด: `{current_price:.2f} {currency_unit}`\n"
                    f"📈 แนวโน้ม (Trend): {trend}\n"
                    f"⚡ ค่า RSI (14): **{rsi_val:.1f}** ({rsi_status})\n"
                    f"🎯 โอกาสชนะ (Win Rate): **{win_rate}%**\n\n"
                    f"🟢 **แนวรับสำคัญ (3 ระดับ):**\n"
                    f"1. {supports[0]:.2f} {currency_unit} (ใกล้สุด)\n"
                    f"2. {supports[1]:.2f} {currency_unit}\n"
                    f"3. {supports[2]:.2f} {currency_unit} (แข็งแกร่งสุด)\n\n"
                    f"🔴 **แนวต้านสำคัญ (3 ระดับ):**\n"
                    f"1. {resistances[0]:.2f} {currency_unit} (ใกล้สุด)\n"
                    f"2. {resistances[1]:.2f} {currency_unit}\n"
                    f"3. {resistances[2]:.2f} {currency_unit} (แข็งแกร่งสุด)"
                )

                # แจ้งเตือนเข้า Discord ถ้าราคาใกล้แนวรับ/แนวต้าน L1 ในระยะ 1.5%
                threshold = 0.015
                if abs(current_price - supports[0]) / supports[0] <= threshold or abs(resistances[0] - current_price) / resistances[0] <= threshold:
                    discord_msg = (
                        f"🔔 **[Pro Alert] ราคาใกล้โซนสำคัญ!**\n"
                        f"• สินทรัพย์: `{target_symbol.upper()}`\n"
                        f"• ราคา: `{current_price:.2f} {currency_unit}` | RSI: `{rsi_val:.1f}`\n"
                        f"• แนวโน้ม: {trend}\n"
                        f"• โอกาสชนะ (Win Rate): **{win_rate}%**"
                    )
                    send_discord_alert(discord_msg)

            else:
                reply_text = f"❌ ไม่พบข้อมูลสำหรับ '{raw_text}' กรุณาตรวจสอบชื่อย่ออีกครั้งครับ"

    except Exception as e:
        reply_text = "ขออภัยครับ เกิดข้อผิดพลาดในการประมวลผลข้อมูลเชิงลึก"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

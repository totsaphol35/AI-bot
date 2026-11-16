import os
import yfinance as yf
import pandas as pd
import numpy as np

from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)

# ตั้งค่า LINE Access Token และ Secret จาก Environment Variables บน Render
configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET', 'YOUR_CHANNEL_SECRET'))

# หน่วยความจำเก็บ Watchlist แยกตาม User
user_watchlists = {}

# คลังหุ้นชั้นนำระดับโลกสำหรับสแกนหาจุดช้อนซื้อ (S&P 500 / NASDAQ 100 / ETF ยอดนิยม)
GLOBAL_SCAN_POOL = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO", "AMD", "NFLX",
    "QQQM", "SCHG", "JEPQ", "VOO", "RKLB", "PLTR", "COIN", "UBER", "CRM", "COST"
]

# 1. สูตร RSI มาตรฐาน Wilder's Smoothing (ตรงกับ TradingView)
def calculate_wilder_rsi(close_series, period=14):
    delta = close_series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])

# 2. สูตรคำนวณ ATR (Average True Range) วัดความผันผวนจริง
def calculate_atr(df, period=14):
    high_low = df['High'] - df['Low']
    high_close = np.abs(df['High'] - df['Close'].shift())
    low_close = np.abs(df['Low'] - df['Close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return float(true_range.rolling(period).mean().iloc[-1])

# 3. ฟังก์ชันวิเคราะห์หุ้นเชิงลึก พร้อม EMA 50, EMA 200 และโครงสร้างแนวรับ-แนวต้าน
def get_pro_stock_data(symbol):
    try:
        symbol = symbol.upper().strip()
        stock = yf.Ticker(symbol)
        # ดึงข้อมูลย้อนหลัง 1 ปี (ประมาณ 252 วันทำการ) เพื่อให้คำนวณ EMA 200 ได้แม่นยำ
        df = stock.history(period="1y")
        
        if df.empty or len(df) < 30:
            return None

        current_price = float(df['Close'].iloc[-1])
        prev_close = float(df['Close'].iloc[-2])
        price_change = ((current_price - prev_close) / prev_close) * 100

        # คำนวณ Indicators หลัก
        rsi_val = calculate_wilder_rsi(df['Close'], 14)
        atr_val = calculate_atr(df, 14)
        ema_50 = float(df['Close'].ewm(span=50, adjust=False).mean().iloc[-1])
        ema_200 = float(df['Close'].ewm(span=200, adjust=False).mean().iloc[-1])

        # หาจุด Swing Low / Swing High
        swing_low_15d = float(df['Low'].tail(15).min())
        swing_low_60d = float(df['Low'].tail(60).min())
        swing_high_15d = float(df['High'].tail(15).max())
        swing_high_60d = float(df['High'].tail(60).max())

        currency = "THB" if ".BK" in symbol else "USD"

        # --- ระบบคัดกรองแนวรับ (Support) ร่วมกับ EMA 50 & EMA 200 ---
        support_candidates = [
            (min(swing_low_15d, current_price - 0.8 * atr_val), "แนวรับระยะสั้น"),
            (min(swing_low_60d, current_price - 1.8 * atr_val), "แนวรับโครงสร้าง 60 วัน"),
            (current_price - 3.0 * atr_val, "กรอบล่าง ATR (จุดหนี)")
        ]
        # หาก EMA 50 หรือ EMA 200 อยู่ต่ำกว่าราคาปัจจุบัน ให้ทำหน้าที่เป็นแนวรับสำคัญ
        if ema_50 < current_price:
            support_candidates.append((ema_50, "แนวรับเส้น EMA 50"))
        if ema_200 < current_price:
            support_candidates.append((ema_200, "แนวรับใหญ่ EMA 200 🛡️"))

        # เรียงลำดับแนวรับจากใกล้ราคาปัจจุบันมากที่สุดไปไกลสุด และกรองไม่ให้ค่าซ้ำซ้อนกันเกินไป
        support_candidates.sort(key=lambda x: x[0], reverse=True)
        final_supports = []
        for val, label in support_candidates:
            if not final_supports or abs(final_supports[-1][0] - val) / current_price > 0.012:
                final_supports.append((val, label))
            elif "EMA 200" in label:
                # หากแนวรับทับซ้อนกับ EMA 200 ให้ยึดป้ายกำกับ EMA 200 เป็นหลัก (Confluence)
                final_supports[-1] = (val, "แนวรับแข็งแกร่ง (ทับซ้อน EMA 200 🛡️)")
        
        while len(final_supports) < 3:
            last_s = final_supports[-1][0] if final_supports else current_price
            final_supports.append((last_s - atr_val, "แนวรับถัดไป"))

        # --- ระบบคัดกรองแนวต้าน (Resistance) ร่วมกับ EMA 50 & EMA 200 ---
        resistance_candidates = [
            (max(swing_high_15d, current_price + 0.8 * atr_val), "แนวต้านระยะสั้น"),
            (max(swing_high_60d, current_price + 1.8 * atr_val), "แนวต้านสำคัญ 60 วัน"),
            (current_price + 3.0 * atr_val, "เป้าหมายทำกำไร (ATR)")
        ]
        # หากราคาหลุดต่ำกว่า EMA 50 หรือ EMA 200 เส้นเหล่านั้นจะกลายเป็นแนวต้านทันที
        if ema_50 > current_price:
            resistance_candidates.append((ema_50, "แนวต้านเส้น EMA 50"))
        if ema_200 > current_price:
            resistance_candidates.append((ema_200, "แนวต้านใหญ่ EMA 200 🚧"))

        resistance_candidates.sort(key=lambda x: x[0])
        final_resistances = []
        for val, label in resistance_candidates:
            if not final_resistances or abs(val - final_resistances[-1][0]) / current_price > 0.012:
                final_resistances.append((val, label))
            elif "EMA 200" in label:
                final_resistances[-1] = (val, "แนวต้านแข็งแกร่ง (EMA 200 🚧)")

        while len(final_resistances) < 3:
            last_r = final_resistances[-1][0] if final_resistances else current_price
            final_resistances.append((last_r + atr_val, "แนวต้านถัดไป"))

        # --- ประเมินโครงสร้างเทรนด์และ Win Rate ---
        score = 50
        if current_price > ema_200 and current_price > ema_50:
            score += 18
            trend_str = "ขาขึ้นแข็งแกร่ง (เหนือ EMA50 & EMA200)"
        elif current_price > ema_200 and current_price <= ema_50:
            score += 12
            trend_str = "ย่อตัวในขาขึ้นใหญ่ (พักฐานเหนือ EMA200)"
        elif current_price <= ema_200 and current_price > ema_50:
            score += 5
            trend_str = "ฟื้นตัวระยะสั้น (ทดสอบแนวต้าน EMA200)"
        else:
            score -= 8
            trend_str = "ขาลงระยะยาว (ต่ำกว่า EMA50 & EMA200)"

        # ประเมินโซน RSI
        if rsi_val <= 32:
            score += 18
            rsi_status = f"{rsi_val:.1f} (Oversold 🟢 จุดช้อนซื้อ)"
        elif rsi_val <= 45:
            score += 10
            rsi_status = f"{rsi_val:.1f} (ย่อตัวสะสมพลัง)"
        elif rsi_val >= 70:
            score -= 12
            rsi_status = f"{rsi_val:.1f} (Overbought 🔴 ระวังแรงขาย)"
        else:
            score += 4
            rsi_status = f"{rsi_val:.1f} (Neutral แกว่งตัวปกติ)"

        # โบนัสคะแนนพิเศษ: หากหุ้นเป็นขาขึ้นระยะยาวแล้วราคาย่อลงมาใกล้ EMA 200 (ระยะไม่เกิน 3%)
        dist_to_ema200_pct = abs(current_price - ema_200) / ema_200 * 100
        if current_price >= ema_200 and dist_to_ema200_pct <= 3.0:
            score += 10

        win_rate = min(max(score, 45), 90)
        change_sign = "+" if price_change >= 0 else ""
        change_color = "#00D26A" if price_change >= 0 else "#FF4D4D"

        return {
            "symbol": symbol,
            "price_str": f"{current_price:,.2f} {currency}",
            "change_str": f"{change_sign}{price_change:.2f}%",
            "change_color": change_color,
            "win_rate": f"{win_rate:.1f}%",
            "rsi_str": rsi_status,
            "trend": trend_str,
            "ema50_str": f"{ema_50:,.2f}",
            "ema200_str": f"{ema_200:,.2f}",
            "supports": [
                f"{final_supports[0][0]:,.2f} ({final_supports[0][1]})",
                f"{final_supports[1][0]:,.2f} ({final_supports[1][1]})",
                f"{final_supports[2][0]:,.2f} ({final_supports[2][1]})"
            ],
            "resistances": [
                f"{final_resistances[0][0]:,.2f} ({final_resistances[0][1]})",
                f"{final_resistances[1][0]:,.2f} ({final_resistances[1][1]})",
                f"{final_resistances[2][0]:,.2f} ({final_resistances[2][1]})"
            ]
        }
    except Exception as e:
        print(f"Error analyzing {symbol}: {e}")
        return None

# 4. ฟังก์ชันสแกนหุ้นแบบกลุ่ม (Batch Scanner) พร้อมเช็กแนวรับ EMA 200
def batch_scan_stocks(symbols_list):
    try:
        # ดึงย้อนหลัง 1 ปีแบบ Batch เพื่อคำนวณ EMA 200 ของทุกตัวพร้อมกันอย่างรวดเร็ว
        data = yf.download(symbols_list, period="1y", group_by="ticker", progress=False, threads=True)
        results = []
        
        for sym in symbols_list:
            try:
                df = data[sym].dropna() if len(symbols_list) > 1 else data.dropna()
                if df.empty or len(df) < 30:
                    continue
                
                close_p = float(df['Close'].iloc[-1])
                prev_p = float(df['Close'].iloc[-2])
                chg = ((close_p - prev_p) / prev_p) * 100
                rsi = calculate_wilder_rsi(df['Close'], 14)
                low_20d = float(df['Low'].tail(20).min())
                ema_200 = float(df['Close'].ewm(span=200, adjust=False).mean().iloc[-1])
                
                # เลือกแนวรับที่ใกล้ราคาปัจจุบันที่สุดระหว่าง Swing Low 20 วัน กับ EMA 200 (หากอยู่เหนือ EMA 200)
                if close_p >= ema_200 and ema_200 > low_20d:
                    key_support = ema_200
                    sup_type = "EMA 200 🛡️"
                else:
                    key_support = low_20d
                    sup_type = "Swing Low"

                dist_pct = ((close_p - key_support) / key_support) * 100
                dist_ema200_pct = ((close_p - ema_200) / ema_200) * 100
                
                results.append({
                    "symbol": sym,
                    "price": close_p,
                    "change": chg,
                    "rsi": rsi,
                    "support": key_support,
                    "sup_type": sup_type,
                    "ema200": ema_200,
                    "dist_pct": dist_pct,
                    "dist_ema200_pct": dist_ema200_pct
                })
            except Exception:
                continue
        return results
    except Exception as e:
        print(f"Batch scan error: {e}")
        return []

# 5. สร้างการ์ด Flex Message ดีไซน์ Dark Mode Pro (LINE SDK v3)
def build_flex_card(data):
    bubble_dict = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#0F0F1E",
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {
                            "type": "text",
                            "text": f"📊 {data['symbol']}",
                            "weight": "bold",
                            "size": "xl",
                            "color": "#FFFFFF",
                            "flex": 1
                        },
                        {
                            "type": "text",
                            "text": f"Win Rate {data['win_rate']}",
                            "size": "xs",
                            "color": "#FFD700",
                            "align": "end",
                            "gravity": "center",
                            "weight": "bold"
                        }
                    ]
                },
                {
                    "type": "text",
                    "text": f"{data['price_str']} ({data['change_str']})",
                    "size": "lg",
                    "weight": "bold",
                    "color": data['change_color'],
                    "margin": "sm"
                }
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "text",
                    "text": f"📈 RSI (14): {data['rsi_str']}",
                    "size": "xs",
                    "color": "#222222",
                    "weight": "bold"
                },
                {
                    "type": "text",
                    "text": f"⚡ เทรนด์: {data['trend']}",
                    "size": "xs",
                    "color": "#555555",
                    "margin": "xs",
                    "wrap": True
                },
                {
                    "type": "text",
                    "text": f"🎯 ค่าเฉลี่ย: EMA50 = {data['ema50_str']} | EMA200 = {data['ema200_str']}",
                    "size": "xxs",
                    "color": "#0066CC",
                    "margin": "xs",
                    "weight": "bold",
                    "wrap": True
                },
                {
                    "type": "separator",
                    "margin": "md"
                },
                {
                    "type": "text",
                    "text": "🟢 โซนแนวรับแม่นยำสูง (Support):",
                    "size": "sm",
                    "weight": "bold",
                    "color": "#008000",
                    "margin": "md"
                },
                {
                    "type": "text",
                    "text": f"1. {data['supports'][0]}\n2. {data['supports'][1]}\n3. {data['supports'][2]}",
                    "size": "xs",
                    "color": "#333333",
                    "margin": "xs",
                    "wrap": True
                },
                {
                    "type": "text",
                    "text": "🔴 โซนแนวต้านสำคัญ (Resistance):",
                    "size": "sm",
                    "weight": "bold",
                    "color": "#D93025",
                    "margin": "md"
                },
                {
                    "type": "text",
                    "text": f"1. {data['resistances'][0]}\n2. {data['resistances'][1]}\n3. {data['resistances'][2]}",
                    "size": "xs",
                    "color": "#333333",
                    "margin": "xs",
                    "wrap": True
                }
            ]
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "paddingAll": "12px",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#00B900",
                    "height": "sm",
                    "action": {
                        "type": "message",
                        "label": "➕ เก็บเข้าลิสต์",
                        "text": f"+{data['symbol']}"
                    }
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "height": "sm",
                    "action": {
                        "type": "message",
                        "label": "🎯 สแกนแนวรับ",
                        "text": "/SCAN_SUPPORT"
                    }
                }
            ]
        }
    }
    return FlexMessage(
        alt_text=f"วิเคราะห์หุ้น {data['symbol']} ราคา {data['price_str']}",
        contents=FlexContainer.from_dict(bubble_dict)
    )

# Endpoint สำหรับ UptimeRobot ป้องกัน Server หลับ
@app.route("/", methods=['GET'])
def health_check():
    return "Stock Sage AI (EMA 200 Edition) is Awake and Running!", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text.strip()
    command = user_message.upper()

    if user_id not in user_watchlists:
        user_watchlists[user_id] = ["GOOGL", "NVDA", "QQQM", "RKLB"]

    watchlist = user_watchlists[user_id]
    messages_to_reply = []

    if command == "/HELP":
        help_text = (
            "📖 **คู่มือ Stock Sage AI (EMA 200 Quant Pro)**\n\n"
            "• พิมพ์ชื่อหุ้น (เช่น `NVDA`, `GOOGL`, `PTT.BK`) เพื่อดูการ์ดแนวรับ-แนวต้านร่วมกับเส้น EMA 50 และ EMA 200\n"
            "• `+[ชื่อหุ้น]` เพิ่มเข้า Watchlist\n"
            "• `-[ชื่อหุ้น]` ลบออกจาก Watchlist\n"
            "• `/LIST` ดูรายชื่อหุ้นที่เฝ้าระวัง\n"
            "• `/SUMMARY` สรุปราคา, RSI และสถานะ EMA 200 ของพอร์ต\n"
            "• `/SCAN_SUPPORT` สแกนหาหุ้นเด่นทั่วโลกที่ย่อใกล้แนวรับสำคัญหรือเส้น EMA 200"
        )
        messages_to_reply.append(TextMessage(text=help_text))

    elif command == "/LIST":
        if not watchlist:
            messages_to_reply.append(TextMessage(text="📭 Watchlist ของคุณว่างเปล่า พิมพ์ +ตามด้วยชื่อหุ้น เพื่อเพิ่มได้เลยครับ"))
        else:
            list_str = "\n".join([f"{i+1}. {s}" for i, s in enumerate(watchlist)])
            messages_to_reply.append(TextMessage(text=f"📋 **รายการหุ้นเฝ้าระวัง ({len(watchlist)} ตัว):**\n{list_str}\n\n💡 พิมพ์ชื่อหุ้นตัวที่ต้องการเพื่อดูจุดรับ-ต้าน EMA 200 รายตัว"))

    elif command == "/SUMMARY":
        if not watchlist:
            messages_to_reply.append(TextMessage(text="📭 Watchlist ว่างเปล่าครับ"))
        else:
            scan_results = batch_scan_stocks(watchlist)
            if not scan_results:
                messages_to_reply.append(TextMessage(text="⚠️ ไม่สามารถดึงข้อมูลได้ในขณะนี้ กรุณาลองใหม่อีกครั้ง"))
            else:
                summary_lines = [f"📊 **สรุปพอร์ต Watchlist ({len(scan_results)} ตัว):**"]
                for item in scan_results:
                    sign = "+" if item['change'] >= 0 else ""
                    icon = "🟢" if item['rsi'] <= 35 else ("🔴" if item['rsi'] >= 70 else "⚪")
                    ema_status = "เหนือ EMA200 📈" if item['price'] >= item['ema200'] else "ต่ำกว่า EMA200 📉"
                    summary_lines.append(
                        f"\n{icon} **{item['symbol']}** : {item['price']:,.2f} ({sign}{item['change']:.2f}%)\n"
                        f"   ├ RSI: {item['rsi']:.1f} | {ema_status} ({item['ema200']:,.2f})\n"
                        f"   └ แนวรับใกล้สุด: {item['support']:,.2f} ({item['sup_type']})"
                    )
                messages_to_reply.append(TextMessage(text="\n".join(summary_lines)))

    elif command == "/SCAN_SUPPORT":
        scan_results = batch_scan_stocks(GLOBAL_SCAN_POOL)
        # คัดกรองหุ้นที่ย่อเข้าใกล้แนวรับไม่เกิน 3.5%, หรือใกล้เส้น EMA 200 ไม่เกิน 3.5%, หรือ RSI <= 40
        near_support = [
            r for r in scan_results 
            if r['dist_pct'] <= 3.5 or (0 <= r['dist_ema200_pct'] <= 3.5) or r['rsi'] <= 40
        ]
        near_support.sort(key=lambda x: x['dist_pct'])

        if near_support:
            lines = ["🎯 **หุ้นระดับโลกจ่อแนวรับสำคัญ / ทดสอบ EMA 200:**"]
            for item in near_support[:8]:
                sign = "+" if item['change'] >= 0 else ""
                lines.append(
                    f"\n🟢 **{item['symbol']}** (ราคา {item['price']:,.2f} | {sign}{item['change']:.2f}%)\n"
                    f"   • แนวรับ: **{item['support']:,.2f}** [{item['sup_type']}] (ห่าง {item['dist_pct']:.1f}%)\n"
                    f"   • เส้น EMA 200: **{item['ema200']:,.2f}** | RSI: **{item['rsi']:.1f}**"
                )
            lines.append("\n💡 *พิมพ์ชื่อหุ้นเพื่อดูการ์ดวิเคราะห์แนวรับ 3 ระดับแบบละเอียด*")
            messages_to_reply.append(TextMessage(text="\n".join(lines)))
        else:
            messages_to_reply.append(TextMessage(text="🛡️ ตลาดอยู่ในโซนแข็งแกร่ง ยังไม่มีหุ้นตัวแม่ในลิสต์ย่อลงมาใกล้แนวรับสำคัญหรือเส้น EMA 200 ครับ"))

    elif user_message.startswith("+"):
        stock_to_add = user_message[1:].strip().upper()
        if stock_to_add and stock_to_add not in watchlist:
            check = get_pro_stock_data(stock_to_add)
            if check:
                watchlist.append(stock_to_add)
                messages_to_reply.append(TextMessage(text=f"✅ เพิ่ม **{stock_to_add}** ({check['price_str']}) เข้าสู่ Watchlist เรียบร้อย!"))
            else:
                messages_to_reply.append(TextMessage(text=f"⚠️ ไม่พบชื่อย่อหุ้น '{stock_to_add}' ในระบบ Yahoo Finance"))
        else:
            messages_to_reply.append(TextMessage(text=f"⚠️ หุ้น **{stock_to_add}** มีอยู่ใน Watchlist ของคุณแล้วครับ"))

    elif user_message.startswith("-"):
        stock_to_remove = user_message[1:].strip().upper()
        if stock_to_remove in watchlist:
            watchlist.remove(stock_to_remove)
            messages_to_reply.append(TextMessage(text=f"🗑️ ลบ **{stock_to_remove}** ออกจาก Watchlist เรียบร้อยแล้ว"))
        else:
            messages_to_reply.append(TextMessage(text=f"⚠️ ไม่พบ **{stock_to_remove}** ใน Watchlist ของคุณ"))

    else:
        stock_name = user_message.upper()
        data = get_pro_stock_data(stock_name)
        if data:
            messages_to_reply.append(build_flex_card(data))
        else:
            messages_to_reply.append(TextMessage(text=f"⚠️ ไม่พบข้อมูลหุ้น '{stock_name}'\n(หุ้นสหรัฐฯ พิมพ์ชื่อย่อได้เลย เช่น NVDA, หุ้นไทยเติม .BK เช่น PTT.BK)"))

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=messages_to_reply
            )
        )

if __name__ == "__main__":
    app.run(port=5000)

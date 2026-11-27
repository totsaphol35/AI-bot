import os
import time
import threading
import requests
import numpy as np
import pandas as pd
import yfinance as yf
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# ==========================================
# 1. การตั้งค่าหลัก & Environment Variables
# ==========================================
MODEL_VERSION = "1.5.1-universal-pro-myid"

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")          # User ID ส่วนตัว (ขึ้นต้นด้วย U...)
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "900"))  # สแกนทุก 15 นาที (900 วินาที)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

UNIVERSAL_WATCHLIST = [
    {"cmd": "/gold", "symbol": "GC=F",     "name": "GOLD (XAUUSD)", "min_score": 75},
    {"cmd": "/btc",  "symbol": "BTC-USD",  "name": "BITCOIN (BTC)", "min_score": 75},
    {"cmd": "/eur",  "symbol": "EURUSD=X", "name": "EUR/USD",       "min_score": 75},
    {"cmd": "/jpy",  "symbol": "JPY=X",    "name": "USD/JPY",       "min_score": 75},
]

last_universal_alert = {}

# ==========================================
# 2. ฟังก์ชันดึงข้อมูล & คำนวณอินดิเคเตอร์พื้นฐาน
# ==========================================
def fetch_clean_history(symbol, period="1mo", interval="15m"):
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
        return df if len(df) > 20 else None
    except Exception as e:
        print(f"Fetch error ({symbol} {interval}): {e}")
        return None

def calculate_atr_series(df, period=14):
    high = df['High']
    low = df['Low']
    close = df['Close']
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def calculate_wilder_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)

def get_asset_profile(symbol):
    s = symbol.upper()
    if "BTC" in s or "ETH" in s:
        return {"name": "CRYPTO", "is_crypto": True, "is_usd_dir": False, "sl_buf": 0.70, "body_lim": 1.90}
    if "GC=F" in s or "XAU" in s or "GOLD" in s:
        return {"name": "GOLD", "is_crypto": False, "is_usd_dir": False, "sl_buf": 0.50, "body_lim": 1.60}
    if s.startswith("JPY") or s.startswith("USD"):
        return {"name": "FX-DIR", "is_crypto": False, "is_usd_dir": True, "sl_buf": 0.40, "body_lim": 1.60}
    return {"name": "FX-INV", "is_crypto": False, "is_usd_dir": False, "sl_buf": 0.40, "body_lim": 1.60}

# ==========================================
# 3. แกนวิเคราะห์ AXION PRO ENGINE V5.0 (UNIVERSAL)
# ==========================================
def analyze_universal_smc_m15(symbol="GC=F", interval="15m", min_score=75):
    df = fetch_clean_history(symbol, period="1mo", interval=interval)
    if df is None or len(df) < 210:
        return None

    prof = get_asset_profile(symbol)

    # 1. ดึง H1 Trend & กรอบ CRT (แท่ง H1 ที่ปิดแล้วล่าสุด)
    df_h1 = fetch_clean_history(symbol, period="1mo", interval="1h")
    htf_bull, htf_bear = True, False
    crt_high, crt_low = None, None
    if df_h1 is not None and len(df_h1) >= 55:
        h1_c = float(df_h1['Close'].iloc[-2])
        h1_e50 = float(df_h1['Close'].ewm(span=50, adjust=False).mean().iloc[-2])
        htf_bull = bool(h1_c > h1_e50)
        htf_bear = bool(h1_c < h1_e50)
        crt_high = float(df_h1['High'].iloc[-2])
        crt_low = float(df_h1['Low'].iloc[-2])

    # 2. ดึง DXY Macro (ปรับทิศทางตามโปรไฟล์อัตโนมัติ)
    dxy_support_buy, dxy_support_sell = True, True
    dxy_status = "ปิดใช้งาน (Crypto)"
    if not prof["is_crypto"]:
        df_dxy = fetch_clean_history("DX-Y.NYB", period="5d", interval=interval)
        if df_dxy is not None and len(df_dxy) >= 25:
            dxy_c = float(df_dxy['Close'].iloc[-2])
            dxy_e20 = float(df_dxy['Close'].ewm(span=20, adjust=False).mean().iloc[-2])
            dxy_bull = bool(dxy_c > dxy_e20)
            dxy_bear = bool(dxy_c < dxy_e20)
            dxy_support_buy = dxy_bull if prof["is_usd_dir"] else dxy_bear
            dxy_support_sell = dxy_bear if prof["is_usd_dir"] else dxy_bull
            dxy_status = "แข็งค่า (DXY▲)" if dxy_bull else "อ่อนค่า (DXY▼)"
        else:
            dxy_status = "Neutral"

    # 3. ตัดแท่งล่าสุดที่ยังไม่ปิดออก (Non-Repaint 100%)
    df_closed = df.iloc[:-1].copy()
    opens = df_closed['Open'].values
    highs = df_closed['High'].values
    lows = df_closed['Low'].values
    closes = df_closed['Close'].values
    times = df_closed.index

    ema50 = df_closed['Close'].ewm(span=50, adjust=False).mean().values
    ema200 = df_closed['Close'].ewm(span=200, adjust=False).mean().values
    atr = calculate_atr_series(df_closed, 14).values
    rsi_series = calculate_wilder_rsi(df_closed['Close'], 14).values

    bull_zones, bear_zones = [], []
    buy_side_liq, sell_side_liq = [], []
    last_piv_high, last_piv_low = None, None
    last_sell_sweep_idx, last_sell_sweep_eq = None, False
    last_buy_sweep_idx, last_buy_sweep_eq = None, False

    max_zones = 6
    piv_len = 5
    sweep_win = 5
    last_signal = None
    buy_score = sell_score = 0

    start_idx = max(len(df_closed) - 250, 15)
    for i in range(start_idx, len(df_closed)):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        cur_atr = atr[i]
        if not np.isfinite(cur_atr) or cur_atr <= 0:
            continue

        # ตรวจจับ Pivot High/Low (Liquidity $$$ / EQH / EQL)
        if i >= piv_len * 2:
            p_idx = i - piv_len
            win_h = highs[p_idx - piv_len: p_idx + piv_len + 1]
            win_l = lows[p_idx - piv_len: p_idx + piv_len + 1]
            if highs[p_idx] == np.max(win_h):
                is_eqh = (last_piv_high is not None) and (abs(highs[p_idx] - last_piv_high) <= cur_atr * 0.25)
                buy_side_liq.append({"price": float(highs[p_idx]), "is_eq": is_eqh})
                if len(buy_side_liq) > 5: buy_side_liq.pop(0)
                last_piv_high = float(highs[p_idx])
            if lows[p_idx] == np.min(win_l):
                is_eql = (last_piv_low is not None) and (abs(lows[p_idx] - last_piv_low) <= cur_atr * 0.25)
                sell_side_liq.append({"price": float(lows[p_idx]), "is_eq": is_eql})
                if len(sell_side_liq) > 5: sell_side_liq.pop(0)
                last_piv_low = float(lows[p_idx])

        # เช็กการกวาด Liquidity ($$$)
        new_sell_liq = []
        for liq in sell_side_liq:
            if l < liq["price"]:
                last_sell_sweep_idx = i
                last_sell_sweep_eq = liq["is_eq"]
            else:
                new_sell_liq.append(liq)
        sell_side_liq = new_sell_liq

        new_buy_liq = []
        for liq in buy_side_liq:
            if h > liq["price"]:
                last_buy_sweep_idx = i
                last_buy_sweep_eq = liq["is_eq"]
            else:
                new_buy_liq.append(liq)
        buy_side_liq = new_buy_liq

        # เช็กการแตะโซน OB/FVG
        tapped_bull, tapped_bull_bot, new_bull = False, None, []
        for z in bull_zones:
            if l <= z['top'] and c >= z['bot']:
                tapped_bull = True
                tapped_bull_bot = z['bot'] if tapped_bull_bot is None else min(tapped_bull_bot, z['bot'])
            if c >= z['bot']:
                new_bull.append(z)
        bull_zones = new_bull

        tapped_bear, tapped_bear_top, new_bear = False, None, []
        for z in bear_zones:
            if h >= z['bot'] and c <= z['top']:
                tapped_bear = True
                tapped_bear_top = z['top'] if tapped_bear_top is None else max(tapped_bear_top, z['top'])
            if c <= z['top']:
                new_bear.append(z)
        bear_zones = new_bear

        # ประเมินสัญญาณที่แท่งปิดล่าสุด
        if i == len(df_closed) - 1:
            bar_range = h - l
            body_size = abs(c - o)
            lower_wick = min(o, c) - l
            upper_wick = h - max(o, c)
            prev_mid = (highs[i - 1] + lows[i - 1]) / 2.0

            is_normal_body = bar_range > 0 and (body_size <= cur_atr * prof["body_lim"])
            prev_not_dump = (opens[i - 1] - closes[i - 1]) <= cur_atr * prof["body_lim"]
            prev_not_pump = (closes[i - 1] - opens[i - 1]) <= cur_atr * prof["body_lim"]

            is_bull_candle = (c > o) and (c > prev_mid) and bar_range > 0 and (lower_wick / bar_range) >= 0.35
            is_bear_candle = (c < o) and (c < prev_mid) and bar_range > 0 and (upper_wick / bar_range) >= 0.35

            # Discount / Premium (50 แท่ง)
            r_high = float(np.max(highs[max(0, i - 50): i + 1]))
            r_low = float(np.min(lows[max(0, i - 50): i + 1]))
            eq_lvl = (r_high + r_low) / 2.0
            is_discount = c <= eq_lvl
            is_premium = c >= eq_lvl

            not_over_buy = (c - ema50[i]) <= (cur_atr * 2.40)
            not_over_sell = (ema50[i] - c) <= (cur_atr * 2.40)

            bull_div = (l < np.min(lows[i - 10: i])) and (rsi_series[i] > np.min(rsi_series[i - 10: i]))
            bear_div = (h > np.max(highs[i - 10: i])) and (rsi_series[i] < np.max(rsi_series[i - 10: i]))

            recent_sell_sweep = (last_sell_sweep_idx is not None) and ((i - last_sell_sweep_idx) <= sweep_win)
            recent_buy_sweep = (last_buy_sweep_idx is not None) and ((i - last_buy_sweep_idx) <= sweep_win)

            # 3 แพทเทิร์นหลัก (OR Logic)
            setup_liq_buy = recent_sell_sweep and tapped_bull
            setup_crt_buy = (crt_low is not None) and (l < crt_low) and (c > crt_low)
            setup_pb_buy = tapped_bull and (c > ema200[i] and ema50[i] >= ema200[i]) and (abs(l - ema50[i]) <= cur_atr * 1.2)

            setup_liq_sell = recent_buy_sweep and tapped_bear
            setup_crt_sell = (crt_high is not None) and (h > crt_high) and (c < crt_high)
            setup_pb_sell = tapped_bear and (c < ema200[i] and ema50[i] <= ema200[i]) and (abs(h - ema50[i]) <= cur_atr * 1.2)

            any_buy_setup = setup_liq_buy or setup_crt_buy or setup_pb_buy
            any_sell_setup = setup_liq_sell or setup_crt_sell or setup_pb_sell

            buy_tag = "[LIQ]" if setup_liq_buy else ("[CRT]" if setup_crt_buy else "[PB]")
            sell_tag = "[LIQ]" if setup_liq_sell else ("[CRT]" if setup_crt_sell else "[PB]")

            # คำนวณ AI Score (เต็ม 100%)
            buy_score = (
                (15 if (c > ema200[i] and ema50[i] >= ema200[i]) else 0) +
                (15 if htf_bull else 0) +
                (15 if dxy_support_buy else 0) +
                (15 if tapped_bull else 0) +
                ((20 if last_sell_sweep_eq else 15) if (recent_sell_sweep or setup_crt_buy) else 0) +
                (15 if is_discount else 0) +
                (10 if bull_div else 5)
            )
            sell_score = (
                (15 if (c < ema200[i] and ema50[i] <= ema200[i]) else 0) +
                (15 if htf_bear else 0) +
                (15 if dxy_support_sell else 0) +
                (15 if tapped_bear else 0) +
                ((20 if last_buy_sweep_eq else 15) if (recent_buy_sweep or setup_crt_sell) else 0) +
                (15 if is_premium else 0) +
                (10 if bear_div else 5)
            )

            if any_buy_setup and is_bull_candle and is_normal_body and prev_not_dump and not_over_buy and buy_score >= min_score:
                base_sl = min(l, tapped_bull_bot) if tapped_bull_bot is not None else l
                sl_p = base_sl - (prof["sl_buf"] * cur_atr)
                risk = max(c - sl_p, cur_atr * 0.6)
                last_signal = {
                    "type": "BUY", "tag": buy_tag, "score": min(buy_score, 100), "bar_time": str(times[i]),
                    "entry": c, "sl": sl_p,
                    "tp1": c + 1.5 * risk, "tp2": c + 2.5 * risk, "tp3": c + 3.5 * risk
                }
            elif any_sell_setup and is_bear_candle and is_normal_body and prev_not_pump and not_over_sell and sell_score >= min_score:
                base_sl = max(h, tapped_bear_top) if tapped_bear_top is not None else h
                sl_p = base_sl + (prof["sl_buf"] * cur_atr)
                risk = max(sl_p - c, cur_atr * 0.6)
                last_signal = {
                    "type": "SELL", "tag": sell_tag, "score": min(sell_score, 100), "bar_time": str(times[i]),
                    "entry": c, "sl": sl_p,
                    "tp1": c - 1.5 * risk, "tp2": c - 2.5 * risk, "tp3": c - 3.5 * risk
                }

        # สร้างกล่อง FVG และ OB ใหม่
        if i >= 2:
            if l > highs[i - 2] and (l - highs[i - 2]) >= cur_atr * 0.20:
                bull_zones.append({"type": "FVG", "top": float(l), "bot": float(highs[i - 2])})
            if h < lows[i - 2] and (lows[i - 2] - h) >= cur_atr * 0.20:
                bear_zones.append({"type": "FVG", "top": float(lows[i - 2]), "bot": float(h)})
        if i >= 1:
            if closes[i - 1] < opens[i - 1] and (c - o) >= cur_atr * 1.20 and c > highs[i - 1]:
                bull_zones.append({"type": "OB", "top": float(highs[i - 1]), "bot": float(lows[i - 1])})
            if closes[i - 1] > opens[i - 1] and (o - c) >= cur_atr * 1.20 and c < lows[i - 1]:
                bear_zones.append({"type": "OB", "top": float(highs[i - 1]), "bot": float(lows[i - 1])})

        if len(bull_zones) > max_zones: bull_zones = bull_zones[-max_zones:]
        if len(bear_zones) > max_zones: bear_zones = bear_zones[-max_zones:]

    return {
        "symbol": symbol,
        "profile": prof["name"],
        "price": float(closes[-1]),
        "trend": f"H1 {'ขาขึ้น 🟢' if htf_bull else 'ขาลง 🔴'} | DXY {dxy_status}",
        "buy_score": min(buy_score, 100),
        "sell_score": min(sell_score, 100),
        "bar_time": str(times[-1]),
        "signal": last_signal
    }

# ==========================================
# 4. ระบบแจ้งเตือนส่วนตัว (LINE + DISCORD)
# ==========================================
def send_personal_line_alert(text_msg):
    if not line_bot_api or not LINE_USER_ID:
        return
    try:
        line_bot_api.push_message(LINE_USER_ID, TextSendMessage(text=text_msg))
    except Exception as e:
        print(f"LINE Push Alert error: {e}")

def scan_universal_and_notify_all():
    results = []
    for item in UNIVERSAL_WATCHLIST:
        sym = item["symbol"]
        res = analyze_universal_smc_m15(sym, interval="15m", min_score=item["min_score"])
        if not res or not res.get("signal"):
            continue

        sig = res["signal"]
        if sig["bar_time"] != last_universal_alert.get(sym):
            last_universal_alert[sym] = sig["bar_time"]
            icon = "🟢" if sig["type"] == "BUY" else "🔴"
            fmt = ",.2f" if sym in ["GC=F", "BTC-USD"] else ",.4f"

            line_msg = (
                f"{icon} AXION V5.0 | {item['name']} {sig['tag']}\n"
                f"สัญญาณ: {sig['type']} (AI Score {sig['score']}%)\n"
                f"มหภาค: {res['trend']}\n"
                f"━━━━━━━━━━━━━━\n"
                f"🎯 Entry: {sig['entry']:{fmt}}\n"
                f"🛑 SL: {sig['sl']:{fmt}}\n"
                f"✅ TP1: {sig['tp1']:{fmt}}\n"
                f"✅ TP2: {sig['tp2']:{fmt}}\n"
                f"✅ TP3: {sig['tp3']:{fmt}}"
            )
            send_personal_line_alert(line_msg)

            if DISCORD_WEBHOOK_URL:
                discord_msg = (
                    f"{icon} **[AXION V5.0 • {item['name']} M15 {sig['type']} {sig['tag']} | SCORE {sig['score']}%]**\n"
                    f"• มหภาค: {res['trend']}\n"
                    f"• Entry: `{sig['entry']:{fmt}}` | SL: `{sig['sl']:{fmt}}`\n"
                    f"• TP1: `{sig['tp1']:{fmt}}` | TP2: `{sig['tp2']:{fmt}}` | TP3: `{sig['tp3']:{fmt}}`"
                )
                try:
                    requests.post(DISCORD_WEBHOOK_URL, json={"content": discord_msg}, timeout=10)
                except Exception as e:
                    print(f"Discord alert error ({sym}): {e}")
        results.append(res)
    return results

def background_scanner_loop():
    while True:
        try:
            print(f"[{time.strftime('%H:%M:%S')}] Running Axion V5.0 Universal Scan...")
            scan_universal_and_notify_all()
        except Exception as e:
            print(f"Background scan error: {e}")
        time.sleep(SCAN_INTERVAL_SEC)

# ==========================================
# 5. Flask Routes & LINE Webhook
# ==========================================
@app.route("/", methods=["GET"])
def health_check():
    return f"Axion Quant Core {MODEL_VERSION} is running.", 200

@app.route("/trigger-scan", methods=["GET"])
def manual_trigger_scan():
    """ใช้สำหรับให้ Cron-job.org ยิงปลุกเซิร์ฟเวอร์ทุก 15 นาที"""
    res = scan_universal_and_notify_all()
    return {"status": "ok", "version": MODEL_VERSION, "signals_found": len(res)}, 200

@app.route("/callback", methods=["POST"])
def callback():
    if not handler:
        abort(500)
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

if handler:
    @handler.add(MessageEvent, message=TextMessage)
    def handle_message(event):
        text_lower = event.message.text.strip().lower()

        # คำสั่งดึง LINE User ID ส่วนตัว
        if text_lower == "myid":
            uid = getattr(event.source, "user_id", "ไม่พบ User ID")
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"LINE_USER_ID ของคุณคือ:\n{uid}")
            )
            return

        # เช็กภาพรวมทุกตัว
        if text_lower == "/scan":
            lines = ["📊 AXION V5.0 UNIVERSAL SCAN (M15)"]
            for item in UNIVERSAL_WATCHLIST:
                r = analyze_universal_smc_m15(item["symbol"], interval="15m", min_score=item["min_score"])
                if r:
                    fmt = ",.2f" if item["symbol"] in ["GC=F", "BTC-USD"] else ",.4f"
                    sig_txt = f"🔥 {r['signal']['type']} {r['signal']['tag']}" if r.get("signal") else "⏳ รอซิก"
                    lines.append(
                        f"\n• {item['name']} ({r['price']:{fmt}})\n"
                        f"  Score B/S: {r['buy_score']}% / {r['sell_score']}% | {sig_txt}"
                    )
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="\n".join(lines)))
            return

        # เช็กเจาะลึกทีละตัว (/gold, /btc, /eur, /jpy)
        for item in UNIVERSAL_WATCHLIST:
            if text_lower == item["cmd"]:
                r = analyze_universal_smc_m15(item["symbol"], interval="15m", min_score=item["min_score"])
                if r:
                    fmt = ",.2f" if item["symbol"] in ["GC=F", "BTC-USD"] else ",.4f"
                    reply_txt = (
                        f"📌 {item['name']} [{r['profile']}] (M15)\n"
                        f"ราคาปัจจุบัน: {r['price']:{fmt}}\n"
                        f"มหภาค: {r['trend']}\n"
                        f"AI Score: Buy {r['buy_score']}% | Sell {r['sell_score']}%"
                    )
                    if r.get("signal"):
                        s = r["signal"]
                        reply_txt += (
                            f"\n━━━━━━━━━━━━━━\n"
                            f"🔥 สัญญาณ {s['type']} {s['tag']} ({s['score']}%)\n"
                            f"🎯 Entry: {s['entry']:{fmt}}\n"
                            f"🛑 SL: {s['sl']:{fmt}}\n"
                            f"✅ TP1: {s['tp1']:{fmt}} | TP2: {s['tp2']:{fmt}}"
                        )
                    else:
                        reply_txt += "\nสถานะ: ยังไม่ครบเงื่อนไขเข้าเทรด ⏳"
                else:
                    reply_txt = f"ไม่สามารถดึงข้อมูล {item['name']} ได้ในขณะนี้"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_txt))
                return

        if text_lower in ["/help", "help", "คำสั่ง"]:
            help_txt = (
                "🤖 AXION QUANT CORE V1.5 UNIVERSAL\n"
                "• myid — ดู LINE_USER_ID ของคุณ\n"
                "• /scan — เช็กคะแนนทุกตัวพร้อมกัน\n"
                "• /gold — เช็กทองคำ (XAUUSD)\n"
                "• /btc — เช็กบิทคอยน์ (BTCUSD)\n"
                "• /eur — เช็ก EUR/USD\n"
                "• /jpy — เช็ก USD/JPY"
            )
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_txt))

# เริ่ม Background Thread สำหรับสแกนอัตโนมัติ
scanner_thread = threading.Thread(target=background_scanner_loop, daemon=True)
scanner_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

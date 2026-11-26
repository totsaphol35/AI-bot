import os
import time
import threading
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime

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
    FlexContainer,
    QuickReply,
    QuickReplyItem,
    MessageAction
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)

# ตั้งค่า Environment Variables บน Render
configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET', 'YOUR_CHANNEL_SECRET'))
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL', '')
GOOGLE_SHEET_WEBHOOK_URL = os.getenv('GOOGLE_SHEET_WEBHOOK_URL', '')

# Watchlist เริ่มต้นถาวรทุกครั้งที่อัปเดตหรือรีสตาร์ทระบบ
DEFAULT_WATCHLIST = ["SCHG", "JEPQ", "QQQ", "RKLB"]
user_watchlists = {}
discord_alert_cooldown = {}
last_auto_scan_time = 0

GLOBAL_SCAN_POOL = [
    "SCHG", "JEPQ", "QQQ", "RKLB",
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO", "AMD", "QCOM",
    "QQQM", "VOO", "PLTR", "COIN", "UBER", "COST"
]

# ==========================================
# สร้างชุดปุ่มกดด่วนแบบกระชับ (พอดีหน้าจอ LINE)
# ==========================================
def get_quick_reply_buttons(symbol=None):
    if symbol:
        short_sym = symbol.replace(".BK", "")[:6]
        items = [
            QuickReplyItem(action=MessageAction(label=f"➕ {short_sym}", text=f"+{symbol}")),
            QuickReplyItem(action=MessageAction(label=f"⏱️ {short_sym} H1", text=f"{symbol} H1")),
            QuickReplyItem(action=MessageAction(label="📊 พอร์ต", text="/SUMMARY")),
            QuickReplyItem(action=MessageAction(label="📋 ลิสต์", text="/LIST")),
        ]
    else:
        items = [
            QuickReplyItem(action=MessageAction(label="📊 พอร์ต", text="/SUMMARY")),
            QuickReplyItem(action=MessageAction(label="🎯 สแกน", text="/SCAN_SUPPORT")),
            QuickReplyItem(action=MessageAction(label="📋 ลิสต์", text="/LIST")),
            QuickReplyItem(action=MessageAction(label="🏆 TP/SL", text="/WEEKLY_PERF")),
        ]
    return QuickReply(items=items)

# ==========================================
# ส่วนที่ 1: Auditor Agent (บันทึกและตรวจวัดผล Multi-TP / SL)
# ==========================================
def log_weekly_setup_to_sheets(symbol, entry, tp1, tp2, tp3, sl, score):
    if not GOOGLE_SHEET_WEBHOOK_URL:
        return
    week_str = datetime.now().strftime("%Y-W%U")
    payload = {
        "action": "log",
        "week": week_str,
        "symbol": symbol,
        "entry": f"{entry:.2f}",
        "tp1": f"{tp1:.2f}",
        "tp2": f"{tp2:.2f}",
        "tp3": f"{tp3:.2f}",
        "sl": f"{sl:.2f}",
        "score": score
    }
    try:
        requests.post(GOOGLE_SHEET_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"Google Sheets log error: {e}")

def evaluate_weekly_performance_from_sheets():
    if not GOOGLE_SHEET_WEBHOOK_URL:
        return "⚠️ ยังไม่ได้ตั้งค่า GOOGLE_SHEET_WEBHOOK_URL ใน Environment ของ Render"
    try:
        resp = requests.get(GOOGLE_SHEET_WEBHOOK_URL, timeout=10)
        if resp.status_code != 200:
            return "⚠️ ไม่สามารถเชื่อมต่อกับ Google Sheets ได้"

        data = resp.json().get("rows", [])
        if len(data) <= 1:
            return "📭 ยังไม่มีข้อมูลบันทึกสถิติในฐานข้อมูล Axion"

        rows = data[1:]
        total_tp_trades = 0
        total_sl_trades = 0
        tp1_hits = 0
        tp2_hits = 0
        tp3_hits = 0
        pending_count = 0
        current_week = datetime.now().strftime("%Y-W%U")

        for row in rows:
            if len(row) < 14:
                continue
            week_str, symbol, entry_s, tp1_s, tp2_s, tp3_s, sl_s, score, act_low, act_high, tp_cnt_s, sl_cnt_s, status, note = row

            if status == "PENDING" and week_str != current_week:
                df = fetch_clean_history(symbol, period="1mo", interval="1d")
                time.sleep(1.0)
                if not df.empty and len(df) >= 5:
                    recent_df = df.iloc[-5:]
                    recent_low = float(recent_df['Low'].min())
                    recent_high = float(recent_df['High'].max())

                    e_val = float(entry_s)
                    tp1_val = float(tp1_s)
                    tp2_val = float(tp2_s)
                    tp3_val = float(tp3_s)
                    sl_val = float(sl_s)

                    tp_c = 0
                    sl_c = 0

                    if recent_low <= e_val * 1.010:
                        triggered = False
                        stopped_out = False
                        for _, bar in recent_df.iterrows():
                            b_low = float(bar['Low'])
                            b_high = float(bar['High'])
                            if not triggered and b_low <= e_val * 1.010:
                                triggered = True
                            if triggered and not stopped_out:
                                if b_low <= sl_val:
                                    stopped_out = True
                                    sl_c = max(0, 3 - tp_c)
                                    break
                                if b_high >= tp1_val and tp_c < 1:
                                    tp_c = 1
                                if b_high >= tp2_val and tp_c < 2:
                                    tp_c = 2
                                if b_high >= tp3_val and tp_c < 3:
                                    tp_c = 3

                        if tp_c > 0 and sl_c == 0:
                            status = f"WIN (TP{tp_c})"
                            note = f"เข้าออเดอร์ {e_val:.2f} -> ชน TP {tp_c} ไม้ (High {recent_high:.2f})"
                        elif tp_c > 0 and sl_c > 0:
                            status = f"PARTIAL (TP{tp_c}/SL{sl_c})"
                            note = f"เก็บกำไร TP {tp_c} ไม้ ก่อนย่อโดน SL {sl_c} ไม้"
                        elif sl_c > 0:
                            status = "LOSS (SL 3)"
                            note = f"เข้าออเดอร์ {e_val:.2f} -> หลุดโดน SL ทั้ง 3 ไม้ (Low {recent_low:.2f})"
                        else:
                            status = "HOLDING"
                            note = f"เข้าออเดอร์แล้ว ยังไม่ถึง TP1/SL (Low {recent_low:.2f}, High {recent_high:.2f})"
                    else:
                        status = "NO_TOUCH"
                        note = f"ราคาไม่ย่อถึงจุดเข้า {e_val:.2f} (Low {recent_low:.2f})"

                    update_payload = {
                        "action": "update",
                        "week": week_str,
                        "symbol": symbol,
                        "actual_low": f"{recent_low:.2f}",
                        "actual_high": f"{recent_high:.2f}",
                        "tp_count": tp_c,
                        "sl_count": sl_c,
                        "status": status,
                        "note": note
                    }
                    try:
                        requests.post(GOOGLE_SHEET_WEBHOOK_URL, json=update_payload, timeout=5)
                    except Exception:
                        pass

                    tp_cnt_s = tp_c
                    sl_cnt_s = sl_c

            if status == "PENDING":
                pending_count += 1
            else:
                try:
                    t_c = int(tp_cnt_s)
                    s_c = int(sl_cnt_s)
                    total_tp_trades += t_c
                    total_sl_trades += s_c
                    if t_c >= 1:
                        tp1_hits += 1
                    if t_c >= 2:
                        tp2_hits += 1
                    if t_c >= 3:
                        tp3_hits += 1
                except Exception:
                    pass

        total_closed_trades = total_tp_trades + total_sl_trades
        win_rate = (total_tp_trades / total_closed_trades * 100) if total_closed_trades > 0 else 0.0

        return (
            f"📊 Axion Quant • รายงานสรุปผล Multi-TP / SL\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🟢 จำนวนไม้ที่ทำกำไร (TP รวม): {total_tp_trades} ไม้\n"
            f"   ├ ชนเป้า TP1: {tp1_hits} ครั้ง\n"
            f"   ├ ชนเป้า TP2: {tp2_hits} ครั้ง\n"
            f"   └ ชนเป้า TP3: {tp3_hits} ครั้ง\n"
            f"🔴 จำนวนไม้ที่ตัดขาดทุน (SL รวม): {total_sl_trades} ไม้\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🏆 อัตราชนะรายไม้ (Win Rate): {win_rate:.1f}%\n"
            f"⏳ รอปิดรอบสัปดาห์ (PENDING): {pending_count} รายการ"
        )
    except Exception as e:
        return f"⚠️ เกิดข้อผิดพลาดในการประเมินผล: {str(e)}"

# ==========================================
# ส่วนที่ 2: Data, Fair Value & Multi-Timeframe Engine
# ==========================================
def fetch_clean_history(symbol, period="1y", interval="1d"):
    symbol = symbol.upper().strip()
    df = pd.DataFrame()
    range_map = {"60d": "1mo", "14d": "1mo", "1y": "1y", "1mo": "1mo"}
    api_range = range_map.get(period, period)

    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"range": api_range, "interval": interval}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        if resp.status_code == 200:
            payload = resp.json()
            result_list = payload.get("chart", {}).get("result")
            if result_list:
                res = result_list[0]
                timestamps = res.get("timestamp", [])
                quote = res.get("indicators", {}).get("quote", [{}])[0]
                if timestamps and quote:
                    df = pd.DataFrame({
                        "Open": quote.get("open", []),
                        "High": quote.get("high", []),
                        "Low": quote.get("low", []),
                        "Close": quote.get("close", []),
                        "Volume": quote.get("volume", [])
                    }, index=pd.to_datetime(timestamps, unit="s"))
    except Exception as e:
        print(f"Direct Chart API failed for {symbol}: {e}")

    if df is None or df.empty:
        try:
            df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
        except Exception as e:
            print(f"yf.download fallback failed for {symbol}: {e}")

    if df is None or df.empty:
        return pd.DataFrame()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    required_cols = ['Open', 'High', 'Low', 'Close']
    for col in required_cols:
        if col not in df.columns:
            return pd.DataFrame()

    df = df.dropna(subset=required_cols).copy()
    if 'Volume' not in df.columns:
        df['Volume'] = 0
    else:
        df['Volume'] = df['Volume'].fillna(0)

    return df

def fetch_fundamentals_and_meta(symbol, current_price, ema_200):
    meta = {
        "company_name": symbol,
        "exchange": "SET" if ".BK" in symbol else "US",
        "currency": "THB" if ".BK" in symbol else "USD",
        "currency_sym": "฿" if ".BK" in symbol else "$",
        "fair_value": None,
        "fv_note": "Model Est.",
        "after_hours": None
    }
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        if info.get("shortName"):
            meta["company_name"] = str(info["shortName"])[:24]
        if info.get("exchange"):
            ex = str(info["exchange"])
            ex_map = {"NMS": "NASDAQ", "NYQ": "NYSE", "PCX": "NYSE Arca", "BKK": "SET"}
            meta["exchange"] = ex_map.get(ex, ex)
        if info.get("currency"):
            meta["currency"] = str(info["currency"])
            meta["currency_sym"] = "฿" if meta["currency"] == "THB" else "$"

        post_p = info.get("postMarketPrice")
        if post_p and float(post_p) > 0:
            meta["after_hours"] = float(post_p)

        # คำนวณ Fair Value จากหลายโมเดลพื้นฐาน
        models = []
        target_mean = info.get("targetMeanPrice")
        if target_mean and float(target_mean) > 0:
            models.append(float(target_mean))

        fwd_eps = info.get("forwardEps")
        if fwd_eps and float(fwd_eps) > 0:
            models.append(float(fwd_eps) * 20.0)

        trailing_eps = info.get("trailingEps")
        book_val = info.get("bookValue")
        if trailing_eps and book_val and float(trailing_eps) > 0 and float(book_val) > 0:
            graham = float(np.sqrt(22.5 * float(trailing_eps) * float(book_val)))
            if 0.3 * current_price <= graham <= 3.0 * current_price:
                models.append(graham)

        if models:
            meta["fair_value"] = float(np.mean(models))
            meta["fv_note"] = "Fundamental"
        else:
            # สำหรับ ETF (เช่น SCHG, JEPQ, QQQ) ใช้ NAV หรือค่ากลางโครงสร้าง
            nav = info.get("navPrice")
            if nav and float(nav) > 0:
                meta["fair_value"] = float(nav)
                meta["fv_note"] = "ETF NAV"
            elif ema_200 > 0:
                meta["fair_value"] = float((current_price * 0.6) + (ema_200 * 0.45))
                meta["fv_note"] = "Quant Est."
    except Exception as e:
        print(f"Meta fetch fallback for {symbol}: {e}")
        if ema_200 > 0:
            meta["fair_value"] = float((current_price * 0.6) + (ema_200 * 0.45))
            meta["fv_note"] = "Quant Est."

    return meta

def calculate_wilder_rsi(close_series, period=14):
    if len(close_series) < 3:
        return 50.0
    p = min(period, max(len(close_series) - 1, 2))
    delta = close_series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/p, min_periods=1, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/p, min_periods=1, adjust=False).mean()
    last_gain = float(avg_gain.iloc[-1])
    last_loss = float(avg_loss.iloc[-1])
    if last_loss == 0:
        return 100.0 if last_gain > 0 else 50.0
    rs = last_gain / last_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return float(np.clip(rsi, 0.0, 100.0))

def calculate_atr(df, period=14):
    high_low = df['High'] - df['Low']
    high_close = np.abs(df['High'] - df['Close'].shift())
    low_close = np.abs(df['Low'] - df['Close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = ranges.max(axis=1)
    atr_series = true_range.rolling(window=min(period, len(df)), min_periods=1).mean()
    atr_val = float(atr_series.iloc[-1])
    if np.isnan(atr_val) or atr_val <= 0:
        atr_val = float(df['Close'].iloc[-1]) * 0.015
    return atr_val

def calculate_volume_poc(df, bins=25):
    recent_df = df.tail(126).dropna(subset=['High', 'Low', 'Close', 'Volume'])
    if recent_df.empty:
        return float(df['Close'].iloc[-1])
    price_min = float(recent_df['Low'].min())
    price_max = float(recent_df['High'].max())
    if price_max <= price_min or np.isnan(price_min) or np.isnan(price_max):
        return float(recent_df['Close'].iloc[-1])
    bin_edges = np.linspace(price_min, price_max, bins + 1)
    typ_price = (recent_df['High'] + recent_df['Low'] + recent_df['Close']) / 3.0
    bin_indices = np.digitize(typ_price.values, bin_edges) - 1
    bin_indices = np.clip(bin_indices, 0, bins - 1)
    vol_by_bin = np.zeros(bins)
    volumes = recent_df['Volume'].values
    for idx, vol in zip(bin_indices, volumes):
        if not np.isnan(vol) and vol > 0:
            vol_by_bin[int(idx)] += float(vol)
    if np.sum(vol_by_bin) <= 0:
        return float(typ_price.mean())
    max_bin = int(np.argmax(vol_by_bin))
    return float((bin_edges[max_bin] + bin_edges[max_bin + 1]) / 2.0)

def aggregate_ohlc(df, mode="weekly"):
    if df is None or df.empty:
        return pd.DataFrame()
    temp = df.copy()
    if mode == "weekly":
        iso = temp.index.isocalendar()
        temp['_grp'] = iso.year.astype(str) + "-" + iso.week.astype(str).str.zfill(2)
    else:
        temp['_grp'] = temp.index.strftime("%Y-%m")
    agg = temp.groupby('_grp', sort=True).agg({
        'Open': 'first',
        'High': 'max',
        'Low': 'min',
        'Close': 'last',
        'Volume': 'sum'
    })
    return agg

def score_timeframe_trend(df_tf):
    if df_tf is None or len(df_tf) < 4:
        return {"dir": "รอข้อมูล", "color": "#64748B", "conf": "—", "score": 0}
    closes = df_tf['Close']
    curr = float(closes.iloc[-1])
    ema_fast = float(closes.ewm(span=min(8, len(closes)), adjust=False).mean().iloc[-1])
    ema_slow = float(closes.ewm(span=min(21, len(closes)), adjust=False).mean().iloc[-1])
    rsi = calculate_wilder_rsi(closes, 14)

    pts = 0
    if curr > ema_fast:
        pts += 1
    else:
        pts -= 1
    if ema_fast > ema_slow:
        pts += 1
    else:
        pts -= 1
    if rsi >= 55:
        pts += 1
    elif rsi <= 45:
        pts -= 1
    if len(closes) >= 2 and float(closes.iloc[-1]) > float(closes.iloc[-2]):
        pts += 1
    else:
        pts -= 1

    conf_val = min(max(int(55 + abs(pts) * 9), 52), 92)
    if pts >= 3:
        return {"dir": "ขาขึ้นแข็งแกร่ง ↗", "color": "#059669", "conf": f"{conf_val}%", "score": 2}
    elif pts >= 1:
        return {"dir": "แนวโน้มขึ้น ↗", "color": "#10B981", "conf": f"{conf_val}%", "score": 1}
    elif pts <= -3:
        return {"dir": "ขาลงชัดเจน ↘", "color": "#DC2626", "conf": f"{conf_val}%", "score": -2}
    elif pts <= -1:
        return {"dir": "อ่อนแรง/ย่อตัว ↘", "color": "#EF4444", "conf": f"{conf_val}%", "score": -1}
    return {"dir": "พักตัว Sideway →", "color": "#D97706", "conf": "55%", "score": 0}

def compute_mtf_forecast(df_1d, current_price, ema_200, fair_value):
    daily_res = score_timeframe_trend(df_1d.tail(60))
    weekly_df = aggregate_ohlc(df_1d, "weekly")
    monthly_df = aggregate_ohlc(df_1d, "monthly")
    weekly_res = score_timeframe_trend(weekly_df)
    monthly_res = score_timeframe_trend(monthly_df)

    # Yearly อิงจาก Fair Value + โครงสร้าง EMA 200
    upside = ((fair_value - current_price) / current_price * 100) if (fair_value and current_price > 0) else 0.0
    above_200 = current_price >= ema_200
    if upside >= 10 and above_200:
        yearly_res = {"dir": "เติบโต/น่าสะสม ↗", "color": "#059669", "conf": "84%", "score": 2}
    elif upside >= 5 or above_200:
        yearly_res = {"dir": "แนวโน้มบวก ↗", "color": "#10B981", "conf": "72%", "score": 1}
    elif upside <= -10 and not above_200:
        yearly_res = {"dir": "แพงกว่ามูลค่า ↘", "color": "#DC2626", "conf": "78%", "score": -2}
    elif upside <= -5:
        yearly_res = {"dir": "ระวังแรงขาย ↘", "color": "#EF4444", "conf": "66%", "score": -1}
    else:
        yearly_res = {"dir": "สมดุล/ถือต่อ →", "color": "#D97706", "conf": "60%", "score": 0}

    return {
        "Daily": daily_res,
        "Weekly": weekly_res,
        "Monthly": monthly_res,
        "Yearly": yearly_res
    }

def analyze_leorio_smc(df, atr_val, ema_200):
    smc = {
        "structure": "Sideway / รอเลือกทาง",
        "is_bullish_shift": False,
        "liquidity_sweep": False,
        "demand_zone": None,
        "supply_zone": None,
        "bull_ob": None,
        "bear_ob": None,
        "bull_fvg": None,
        "bear_fvg": None,
        "action_note": "รอราคาเข้าโซนสำคัญ"
    }
    if len(df) < 30:
        return smc
    opens = df['Open'].values
    highs = df['High'].values
    lows = df['Low'].values
    closes = df['Close'].values
    curr_price = float(closes[-1])
    prev_swing_high = float(np.max(highs[-25:-5]))
    prev_swing_low = float(np.min(lows[-25:-5]))

    if curr_price > prev_swing_high:
        smc["structure"] = "BOS ขาขึ้น" if curr_price >= ema_200 else "CHOCH ขาขึ้น"
        smc["is_bullish_shift"] = True
    elif curr_price < prev_swing_low:
        smc["structure"] = "BOS ขาลง" if curr_price < ema_200 else "CHOCH ขาลง"
    else:
        smc["structure"] = "Pullback ในกรอบ"

    if any((l < prev_swing_low and c > prev_swing_low) for l, c in zip(lows[-5:], closes[-5:])):
        smc["liquidity_sweep"] = True

    for i in range(len(df) - 2, max(len(df) - 35, 1), -1):
        if closes[i + 1] - opens[i + 1] > 1.1 * atr_val and closes[i + 1] > highs[i]:
            if smc["demand_zone"] is None and curr_price >= lows[i + 1]:
                smc["demand_zone"] = float((lows[i + 1] + opens[i + 1]) / 2.0)
            if closes[i] < opens[i] and smc["bull_ob"] is None and curr_price >= lows[i]:
                smc["bull_ob"] = float((lows[i] + highs[i]) / 2.0)
        if smc["demand_zone"] and smc["bull_ob"]:
            break

    for i in range(len(df) - 2, max(len(df) - 35, 1), -1):
        if opens[i + 1] - closes[i + 1] > 1.1 * atr_val and closes[i + 1] < lows[i]:
            if smc["supply_zone"] is None and curr_price <= highs[i + 1]:
                smc["supply_zone"] = float((highs[i + 1] + opens[i + 1]) / 2.0)
            if closes[i] > opens[i] and smc["bear_ob"] is None and curr_price <= highs[i]:
                smc["bear_ob"] = float((lows[i] + highs[i]) / 2.0)
        if smc["supply_zone"] and smc["bear_ob"]:
            break

    for i in range(len(df) - 1, max(len(df) - 30, 2), -1):
        if lows[i] > highs[i - 2] and (lows[i] - highs[i - 2]) >= 0.15 * atr_val:
            if smc["bull_fvg"] is None and curr_price >= highs[i - 2]:
                smc["bull_fvg"] = float((lows[i] + highs[i - 2]) / 2.0)
        if highs[i] < lows[i - 2] and (lows[i - 2] - highs[i]) >= 0.15 * atr_val:
            if smc["bear_fvg"] is None and curr_price <= lows[i - 2]:
                smc["bear_fvg"] = float((lows[i - 2] + highs[i]) / 2.0)

    return smc

def build_confluence_zones(candidates, current_price, atr_val, is_support=True, rsi_val=50, rvol=1.0, macd_turning_up=False, is_uptrend=True, has_bull_shift=False):
    valid_candidates = [c for c in candidates if (0 < c['price'] <= current_price if is_support else c['price'] >= current_price) and not np.isnan(c['price'])]
    valid_candidates.sort(key=lambda x: x['price'], reverse=is_support)
    clusters = []
    dynamic_merge = max((0.4 * atr_val) / max(current_price, 0.0001), 0.008)

    for item in valid_candidates:
        placed = False
        for cluster in clusters:
            if abs(cluster['price'] - item['price']) / max(current_price, 0.0001) <= dynamic_merge:
                total_w = cluster['weight'] + item['weight']
                cluster['price'] = (cluster['price'] * cluster['weight'] + item['price'] * item['weight']) / total_w
                cluster['weight'] = total_w
                if item['label'] not in cluster['labels']:
                    cluster['labels'].append(item['label'])
                placed = True
                break
        if not placed:
            clusters.append({'price': float(item['price']), 'weight': item['weight'], 'labels': [item['label']]})

    results = []
    for c in clusters:
        base_score = 40 + (c['weight'] * 1.3) + (len(c['labels']) - 1) * 4
        if is_support:
            if is_uptrend:
                base_score += 8
            elif not has_bull_shift:
                base_score -= 10
            if rsi_val <= 38:
                base_score += 5
            if rvol >= 1.2:
                base_score += 4
            if macd_turning_up:
                base_score += 4
            max_cap = 95 if (is_uptrend or has_bull_shift) else 75
        else:
            if not is_uptrend:
                base_score += 8
            if rsi_val >= 68:
                base_score += 5
            max_cap = 95
        score = min(max(int(base_score), 40), max_cap)
        results.append({'price': float(c['price']), 'label': " + ".join(c['labels'][:2]), 'setup_score': score})

    results.sort(key=lambda x: x['price'], reverse=is_support)
    step = max(atr_val, current_price * 0.015)
    fallback_sup = ["ATR Support", "Volatility Support", "Deep Bid"]
    fallback_res = ["ATR Target", "Volatility Target", "Extended Target"]

    while len(results) < 3:
        idx = len(results)
        ref_price = results[-1]['price'] if results else current_price
        next_price = max(ref_price - step, current_price * 0.5) if is_support else ref_price + step
        lbl = (fallback_sup if is_support else fallback_res)[min(idx, 2)]
        results.append({'price': float(next_price), 'label': lbl, 'setup_score': 50 + (idx * 4)})
    return results[:3]

# ==========================================
# ส่วนที่ 3: วิเคราะห์หุ้นรายตัวและสร้างแผน Multi-TP / SL
# ==========================================
def get_pro_stock_data(symbol, interval="1d", include_meta=True):
    try:
        symbol = symbol.upper().strip()
        period = "1mo" if interval in ["15m", "1h"] else "1y"
        df = fetch_clean_history(symbol, period=period, interval=interval)
        if (df is None or len(df) < 15) and "." not in symbol:
            alt_symbol = f"{symbol}.BK"
            alt_df = fetch_clean_history(alt_symbol, period=period, interval=interval)
            if alt_df is not None and len(alt_df) >= 15:
                symbol = alt_symbol
                df = alt_df
        if df is None or len(df) < 15:
            return None

        # ดึงกราฟรายวัน 1 ปี สำหรับคำนวณ Multi-Timeframe Forecast
        df_daily = df if interval == "1d" else fetch_clean_history(symbol, period="1y", interval="1d")
        if df_daily is None or df_daily.empty:
            df_daily = df

        current_price = float(df['Close'].iloc[-1])
        prev_close = float(df['Close'].iloc[-2]) if len(df) >= 2 else current_price
        price_change = ((current_price - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0

        day_low = float(df_daily['Low'].iloc[-1])
        day_high = float(df_daily['High'].iloc[-1])
        w52_low = float(df_daily['Low'].min())
        w52_high = float(df_daily['High'].max())

        rsi_val = calculate_wilder_rsi(df['Close'], 14)
        atr_val = calculate_atr(df, 14)
        ema_50 = float(df['Close'].ewm(span=50, adjust=False).mean().iloc[-1])
        ema_200 = float(df['Close'].ewm(span=200, adjust=False).mean().iloc[-1])

        ema_12 = df['Close'].ewm(span=12, adjust=False).mean()
        ema_26 = df['Close'].ewm(span=26, adjust=False).mean()
        macd_hist = (ema_12 - ema_26) - (ema_12 - ema_26).ewm(span=9, adjust=False).mean()
        macd_turning_up = bool(macd_hist.iloc[-1] > macd_hist.iloc[-2]) if len(macd_hist) >= 2 else False

        vol_avg_20 = float(df['Volume'].tail(20).mean())
        curr_vol = float(df['Volume'].iloc[-1])
        rvol = (curr_vol / vol_avg_20) if vol_avg_20 > 0 else 1.0
        poc_price = calculate_volume_poc(df)

        smc = analyze_leorio_smc(df, atr_val, ema_200)
        is_uptrend = current_price >= ema_200

        raw_supports = [{'price': current_price - 1.8 * atr_val, 'label': 'ATR Support', 'weight': 9}]
        if ema_50 < current_price:
            raw_supports.append({'price': ema_50, 'label': 'EMA 50', 'weight': 10})
        if ema_200 < current_price:
            raw_supports.append({'price': ema_200, 'label': 'EMA 200', 'weight': 16})
        if poc_price < current_price:
            raw_supports.append({'price': poc_price, 'label': 'POC', 'weight': 13})
        if smc["demand_zone"] and smc["demand_zone"] < current_price:
            raw_supports.append({'price': smc["demand_zone"], 'label': 'Demand Zone', 'weight': 15})
        if smc["bull_ob"] and smc["bull_ob"] < current_price:
            raw_supports.append({'price': smc["bull_ob"], 'label': 'Bullish OB', 'weight': 16})
        if smc["bull_fvg"] and smc["bull_fvg"] < current_price:
            raw_supports.append({'price': smc["bull_fvg"], 'label': 'Bullish FVG', 'weight': 14})

        raw_resistances = [{'price': current_price + 1.8 * atr_val, 'label': 'ATR Target', 'weight': 9}]
        if ema_50 > current_price:
            raw_resistances.append({'price': ema_50, 'label': 'EMA 50', 'weight': 10})
        if ema_200 > current_price:
            raw_resistances.append({'price': ema_200, 'label': 'EMA 200', 'weight': 16})
        if poc_price > current_price:
            raw_resistances.append({'price': poc_price, 'label': 'POC', 'weight': 13})
        if smc["supply_zone"] and smc["supply_zone"] > current_price:
            raw_resistances.append({'price': smc["supply_zone"], 'label': 'Supply Zone', 'weight': 15})
        if smc["bear_ob"] and smc["bear_ob"] > current_price:
            raw_resistances.append({'price': smc["bear_ob"], 'label': 'Bearish OB', 'weight': 16})
        if smc["bear_fvg"] and smc["bear_fvg"] > current_price:
            raw_resistances.append({'price': smc["bear_fvg"], 'label': 'Bearish FVG', 'weight': 14})

        sup_zones = build_confluence_zones(raw_supports, current_price, atr_val, True, rsi_val, rvol, macd_turning_up, is_uptrend, smc["is_bullish_shift"])
        res_zones = build_confluence_zones(raw_resistances, current_price, atr_val, False, rsi_val, rvol, macd_turning_up, is_uptrend, smc["is_bullish_shift"])

        best_entry = sup_zones[0]['price']
        risk_unit = max(1.35 * atr_val, best_entry * 0.015)
        stop_loss = best_entry - risk_unit

        tp1 = max(best_entry + (1.5 * risk_unit), res_zones[0]['price'] * 0.995)
        tp2 = max(tp1 + (1.0 * risk_unit), res_zones[1]['price'])
        tp3 = max(tp2 + (1.5 * risk_unit), res_zones[2]['price'])

        rr_ratio = (tp2 - best_entry) / max(best_entry - stop_loss, 0.01)
        max_score = int(max(s['setup_score'] for s in sup_zones[:3]))

        if interval == "1d":
            log_weekly_setup_to_sheets(symbol, best_entry, tp1, tp2, tp3, stop_loss, sup_zones[0]['setup_score'])

        meta = fetch_fundamentals_and_meta(symbol, current_price, ema_200) if include_meta else {
            "company_name": symbol, "exchange": "US", "currency": "THB" if ".BK" in symbol else "USD",
            "currency_sym": "฿" if ".BK" in symbol else "$", "fair_value": None, "fv_note": "", "after_hours": None
        }

        mtf = compute_mtf_forecast(df_daily, current_price, ema_200, meta["fair_value"])
        mtf_total = sum(v["score"] for v in mtf.values())

        # สรุป Investment Decision
        if mtf_total >= 4 and max_score >= 65:
            decision = "STRONG BUY"
            dec_color = "#059669"
            dec_desc = f"เทรนด์หลักและโครงสร้าง ({smc['structure']}) แข็งแกร่ง แนะนำทยอยรับที่ {best_entry:,.2f}"
        elif mtf_total >= 1:
            decision = "BUY ON DIP"
            dec_color = "#10B981"
            dec_desc = f"แนวโน้มยังเป็นบวก รอย่อรับโซน {sup_zones[0]['label']} ({best_entry:,.2f}) เพื่อ R:R ที่คุ้มค่า"
        elif mtf_total <= -3:
            decision = "CAUTION / SELL"
            dec_color = "#DC2626"
            dec_desc = f"โครงสร้างอ่อนแรง ({smc['structure']}) หลีกเลี่ยงการรับมีด รอดูฐานราคาใหม่"
        else:
            decision = "HOLD / WAIT"
            dec_color = "#D97706"
            dec_desc = f"ราคายังแกว่งตัวในกรอบ รอสัญญาณยืนยันที่แนวรับ {best_entry:,.2f} หรือเบรกต้าน"

        cur_sym = meta["currency_sym"]
        fv_val = meta["fair_value"]
        if fv_val and fv_val > 0:
            upside_pct = ((fv_val - current_price) / current_price) * 100
            up_sign = "+" if upside_pct >= 0 else ""
            fv_str = f"{cur_sym}{fv_val:,.2f}"
            fv_sub = f"{up_sign}{upside_pct:.1f}% ({meta['fv_note']})"
            fv_color = "#059669" if upside_pct >= 0 else "#DC2626"
        else:
            fv_str = f"{cur_sym}—"
            fv_sub = "รอข้อมูลงบ"
            fv_color = "#64748B"

        change_sign = "+" if price_change >= 0 else ""
        change_color = "#059669" if price_change >= 0 else "#DC2626"

        return {
            "symbol": symbol,
            "company_meta": f"{meta['company_name']} · {meta['exchange']} · {meta['currency']}",
            "interval": interval.upper(),
            "raw_price": current_price,
            "raw_sup1": sup_zones[0],
            "price_main": f"{cur_sym}{current_price:,.2f}",
            "price_str": f"{current_price:,.2f} {meta['currency']}",
            "change_str": f"{change_sign}{price_change:.2f}%",
            "change_color": change_color,
            "after_hours_str": f"After-Hours: {cur_sym}{meta['after_hours']:,.2f}" if meta["after_hours"] else f"Day: {day_low:,.2f} - {day_high:,.2f}",
            "w52_str": f"52W: {w52_low:,.2f} - {w52_high:,.2f}",
            "fv_str": fv_str,
            "fv_sub": fv_sub,
            "fv_color": fv_color,
            "setup_score": f"{max_score}/100",
            "score_int": max_score,
            "rsi_str": f"{rsi_val:.1f}",
            "mtf": mtf,
            "decision": decision,
            "dec_color": dec_color,
            "dec_desc": dec_desc,
            "supports": [
                f"{sup_zones[0]['price']:,.2f} [Score {sup_zones[0]['setup_score']}] • {sup_zones[0]['label']}",
                f"{sup_zones[1]['price']:,.2f} [Score {sup_zones[1]['setup_score']}] • {sup_zones[1]['label']}"
            ],
            "setup_str": (
                f"Entry: {best_entry:,.2f} | SL: {stop_loss:,.2f} (R:R 1:{rr_ratio:.1f})\n"
                f"TP1: {tp1:,.2f} | TP2: {tp2:,.2f} | TP3: {tp3:,.2f}"
            )
        }
    except Exception as e:
        print(f"Error analyzing {symbol}: {e}")
        return None

# ==========================================
# ส่วนที่ 4: Dashboard Flex Card (สไตล์ Smart Stock Analyzer)
# ==========================================
def build_flex_card(data):
    score_int = data["score_int"]
    badge_bg = "#DCFCE7" if score_int >= 70 else ("#FEF3C7" if score_int >= 55 else "#FEE2E2")
    badge_txt = "#166534" if score_int >= 70 else ("#92400E" if score_int >= 55 else "#991B1B")

    def mtf_row(label, item):
        return {
            "type": "box",
            "layout": "horizontal",
            "paddingTop": "6px",
            "paddingBottom": "6px",
            "contents": [
                {"type": "text", "text": label, "size": "xs", "color": "#334155", "flex": 3},
                {"type": "text", "text": item["dir"], "size": "xs", "color": item["color"], "weight": "bold", "flex": 5},
                {"type": "text", "text": item["conf"], "size": "xs", "color": "#0F172A", "weight": "bold", "align": "end", "flex": 2}
            ]
        }

    bubble_dict = {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#FFFFFF",
            "paddingAll": "18px",
            "contents": [
                # แถวบนสุด: ชื่อระบบ + Badge Score
                {
                    "type": "box",
                    "layout": "horizontal",
                    "alignItems": "center",
                    "contents": [
                        {"type": "text", "text": "AXION SMART ANALYZER", "weight": "bold", "size": "sm", "color": "#0F172A", "flex": 6},
                        {
                            "type": "box",
                            "layout": "vertical",
                            "backgroundColor": badge_bg,
                            "cornerRadius": "99px",
                            "paddingTop": "3px",
                            "paddingBottom": "3px",
                            "paddingStart": "10px",
                            "paddingEnd": "10px",
                            "flex": 0,
                            "contents": [
                                {"type": "text", "text": f"Score {data['setup_score']}", "size": "xxs", "weight": "bold", "color": badge_txt}
                            ]
                        }
                    ]
                },
                # ส่วน Symbol
                {"type": "text", "text": "SYMBOL", "size": "xxs", "color": "#64748B", "margin": "md"},
                {"type": "text", "text": f"{data['symbol']} ({data['interval']})", "size": "xxl", "weight": "bold", "color": "#0F172A"},
                {"type": "text", "text": data["company_meta"], "size": "xs", "color": "#64748B", "margin": "xs", "wrap": True},

                # กล่องราคา 2 คอลัมน์ (Market Price | Fair Value)
                {
                    "type": "box",
                    "layout": "horizontal",
                    "margin": "lg",
                    "spacing": "md",
                    "backgroundColor": "#F8FAFC",
                    "cornerRadius": "12px",
                    "paddingAll": "12px",
                    "contents": [
                        {
                            "type": "box",
                            "layout": "vertical",
                            "flex": 1,
                            "contents": [
                                {"type": "text", "text": "Market Price", "size": "xxs", "color": "#64748B"},
                                {"type": "text", "text": data["price_main"], "size": "lg", "weight": "bold", "color": "#0F172A", "margin": "xs"},
                                {"type": "text", "text": data["change_str"], "size": "xxs", "weight": "bold", "color": data["change_color"]}
                            ]
                        },
                        {
                            "type": "box",
                            "layout": "vertical",
                            "flex": 1,
                            "contents": [
                                {"type": "text", "text": "Fair Value", "size": "xxs", "color": "#64748B"},
                                {"type": "text", "text": data["fv_str"], "size": "lg", "weight": "bold", "color": "#0F172A", "margin": "xs"},
                                {"type": "text", "text": data["fv_sub"], "size": "xxs", "weight": "bold", "color": data["fv_color"]}
                            ]
                        }
                    ]
                },
                # แถบ Day Range / After-Hours / 52W
                {
                    "type": "text",
                    "text": f"{data['after_hours_str']}  |  {data['w52_str']}",
                    "size": "xxs",
                    "color": "#64748B",
                    "margin": "sm",
                    "wrap": True
                },

                # หัวข้อ Multi-Timeframe Forecast
                {"type": "text", "text": "Multi-Timeframe Forecast", "weight": "bold", "size": "sm", "color": "#0F172A", "margin": "lg"},
                {
                    "type": "box",
                    "layout": "horizontal",
                    "margin": "sm",
                    "paddingBottom": "4px",
                    "contents": [
                        {"type": "text", "text": "ระยะ", "size": "xxs", "weight": "bold", "color": "#475569", "flex": 3},
                        {"type": "text", "text": "ทิศทาง", "size": "xxs", "weight": "bold", "color": "#475569", "flex": 5},
                        {"type": "text", "text": "ความมั่นใจ", "size": "xxs", "weight": "bold", "color": "#475569", "align": "end", "flex": 2}
                    ]
                },
                {"type": "separator", "color": "#E2E8F0"},
                mtf_row("Daily", data["mtf"]["Daily"]),
                {"type": "separator", "color": "#F1F5F9"},
                mtf_row("Weekly", data["mtf"]["Weekly"]),
                {"type": "separator", "color": "#F1F5F9"},
                mtf_row("Monthly", data["mtf"]["Monthly"]),
                {"type": "separator", "color": "#F1F5F9"},
                mtf_row("Yearly", data["mtf"]["Yearly"]),
                {"type": "separator", "margin": "md", "color": "#E2E8F0"},

                # หัวข้อ Investment Decision + แผนเทรด 3 ไม้
                {"type": "text", "text": "Investment Decision", "size": "xs", "weight": "bold", "color": "#475569", "margin": "md"},
                {"type": "text", "text": data["decision"], "size": "xl", "weight": "bold", "color": data["dec_color"], "margin": "xs"},
                {"type": "text", "text": data["dec_desc"], "size": "xxs", "color": "#475569", "margin": "xs", "wrap": True},
                {
                    "type": "box",
                    "layout": "vertical",
                    "margin": "md",
                    "backgroundColor": "#F8FAFC",
                    "cornerRadius": "8px",
                    "paddingAll": "10px",
                    "contents": [
                        {"type": "text", "text": data["setup_str"], "size": "xxs", "color": "#0F172A", "wrap": True}
                    ]
                }
            ]
        },
        # ปุ่มกด 2x2 ฝังในการ์ด (อยู่ในหน้าจอ 100% ไม่ล้นจอ)
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "paddingAll": "12px",
            "backgroundColor": "#F8FAFC",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "spacing": "sm",
                    "contents": [
                        {
                            "type": "button",
                            "style": "primary",
                            "color": "#0F172A",
                            "height": "sm",
                            "action": {"type": "message", "label": "📊 สรุปพอร์ต", "text": "/SUMMARY"}
                        },
                        {
                            "type": "button",
                            "style": "primary",
                            "color": "#059669",
                            "height": "sm",
                            "action": {"type": "message", "label": "🎯 สแกนรับ", "text": "/SCAN_SUPPORT"}
                        }
                    ]
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "spacing": "sm",
                    "contents": [
                        {
                            "type": "button",
                            "style": "secondary",
                            "height": "sm",
                            "action": {"type": "message", "label": "📋 Watchlist", "text": "/LIST"}
                        },
                        {
                            "type": "button",
                            "style": "secondary",
                            "height": "sm",
                            "action": {"type": "message", "label": "🏆 สถิติ TP/SL", "text": "/WEEKLY_PERF"}
                        }
                    ]
                }
            ]
        }
    }
    return FlexMessage(
        alt_text=f"Axion Dashboard: {data['symbol']} ({data['decision']})",
        contents=FlexContainer.from_dict(bubble_dict),
        quick_reply=get_quick_reply_buttons(data['symbol'])
    )

# ==========================================
# ส่วนที่ 5: Strategist & Alert Agent (Discord & LINE)
# ==========================================
def scan_and_notify_discord():
    if not DISCORD_WEBHOOK_URL:
        return
    all_symbols = list(GLOBAL_SCAN_POOL)
    for wl in user_watchlists.values():
        for s in wl:
            if s not in all_symbols:
                all_symbols.append(s)

    now_ts = time.time()
    for sym in all_symbols[:20]:
        data = get_pro_stock_data(sym, "1d", include_meta=False)
        time.sleep(2.0)
        if not data:
            continue
        curr_p, sup1 = data["raw_price"], data["raw_sup1"]
        dist_sup = ((curr_p - sup1["price"]) / max(sup1["price"], 0.01)) * 100

        if 0 <= dist_sup <= 2.0 and sup1["setup_score"] >= 68:
            key = f"{sym}_SUP"
            if now_ts - discord_alert_cooldown.get(key, 0) > 6 * 3600:
                discord_alert_cooldown[key] = now_ts
                msg = (
                    f"🟢 **[Axion Quant Alert] `{data['symbol']}` เข้าโซนรับสำคัญ!**\n"
                    f"• ราคา: `{data['price_str']}` ({data['change_str']})\n"
                    f"• แนวรับ L1: `{sup1['price']:,.2f}` [{sup1['label']}] (Score: {sup1['setup_score']}/100)\n"
                    f"• แผนเทรด: {data['setup_str']}"
                )
                try:
                    requests.post(DISCORD_WEBHOOK_URL, json={"content": msg}, timeout=10)
                except Exception:
                    pass

@app.route("/", methods=['GET', 'HEAD'])
def health_check():
    global last_auto_scan_time
    if request.method == 'HEAD':
        return "OK", 200
    now_ts = time.time()
    if DISCORD_WEBHOOK_URL and (now_ts - last_auto_scan_time > 1800):
        last_auto_scan_time = now_ts
        threading.Thread(target=scan_and_notify_discord, daemon=True).start()
    return "Axion Smart Analyzer (Dashboard + MTF + Fair Value) is Awake!", 200

@app.route("/scan_discord", methods=['GET'])
def manual_discord_trigger():
    if not DISCORD_WEBHOOK_URL:
        return "⚠️ ยังไม่ได้ตั้งค่า DISCORD_WEBHOOK_URL", 400
    threading.Thread(target=scan_and_notify_discord, daemon=True).start()
    return "✅ Axion สั่งรันสแกนหุ้นส่งเข้า Discord เรียบร้อยแล้ว!", 200

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

    # ตั้งค่า Watchlist เริ่มต้น SCHG, JEPQ, QQQ, RKLB เสมอ
    if user_id not in user_watchlists:
        user_watchlists[user_id] = list(DEFAULT_WATCHLIST)

    watchlist = user_watchlists[user_id]
    messages_to_reply = []
    default_qr = get_quick_reply_buttons()

    if command in ["/HELP", "HELP"]:
        help_text = (
            "📖 คู่มือการใช้งาน Axion Smart Analyzer\n\n"
            "• พิมพ์ชื่อหุ้น (เช่น QCOM, RKLB, SCHG) ดูหน้าต่าง Dashboard ครบชุด\n"
            "• /WEEKLY_PERF ตรวจสรุปยอด TP/SL รายสัปดาห์\n"
            "• +[ชื่อหุ้น] / -[ชื่อหุ้น] เพิ่มหรือลบ Watchlist\n"
            "• /SUMMARY สรุปพอร์ต | /SCAN_SUPPORT สแกนโซนรับ\n"
            "• /LIST ดูรายชื่อหุ้นใน Watchlist"
        )
        messages_to_reply.append(TextMessage(text=help_text, quick_reply=default_qr))

    elif command in ["/WEEKLY_PERF", "WEEKLY_PERF"]:
        report = evaluate_weekly_performance_from_sheets()
        messages_to_reply.append(TextMessage(text=report, quick_reply=default_qr))

    elif command in ["/LIST", "LIST"]:
        if not watchlist:
            messages_to_reply.append(TextMessage(text="📭 Watchlist ว่างเปล่า พิมพ์ +ตามด้วยชื่อหุ้นเพื่อเพิ่ม", quick_reply=default_qr))
        else:
            list_str = "\n".join([f"{i+1}. {s}" for i, s in enumerate(watchlist)])
            messages_to_reply.append(TextMessage(text=f"📋 Axion Watchlist ({len(watchlist)} ตัว):\n{list_str}", quick_reply=default_qr))

    elif command in ["/SUMMARY", "SUMMARY"]:
        if not watchlist:
            messages_to_reply.append(TextMessage(text="📭 Watchlist ว่างเปล่าครับ", quick_reply=default_qr))
        else:
            summary_lines = ["📊 Axion สรุปโครงสร้างพอร์ต:"]
            for sym in watchlist:
                d = get_pro_stock_data(sym, "1d", include_meta=False)
                if d:
                    summary_lines.append(f"\n🔹 {d['symbol']} : {d['price_str']} ({d['change_str']})\n   └ รับ: {d['supports'][0]}")
            messages_to_reply.append(TextMessage(text="\n".join(summary_lines), quick_reply=default_qr))

    elif command in ["/SCAN_SUPPORT", "SCAN_SUPPORT"]:
        setups = []
        for sym in GLOBAL_SCAN_POOL[:10]:
            d = get_pro_stock_data(sym, "1d", include_meta=False)
            if d and d['score_int'] >= 65:
                setups.append(d)
        if setups:
            lines = ["🎯 Axion คัดหุ้นเข้าโซนรับเด่น:"]
            for s in setups[:5]:
                lines.append(f"\n🏆 {s['symbol']} ({s['price_str']})\n   • {s['supports'][0]}")
            messages_to_reply.append(TextMessage(text="\n".join(lines), quick_reply=default_qr))
        else:
            messages_to_reply.append(TextMessage(text="🛡️ ยังไม่มีหุ้นตัวหลักเข้าโซนรับในรอบนี้", quick_reply=default_qr))

    elif user_message.startswith("+"):
        add_sym = user_message[1:].strip().upper()
        chk = get_pro_stock_data(add_sym, include_meta=False)
        if chk:
            if chk['symbol'] not in watchlist:
                watchlist.append(chk['symbol'])
            messages_to_reply.append(TextMessage(text=f"✅ เพิ่ม {chk['symbol']} เข้า Watchlist แล้ว", quick_reply=default_qr))
        else:
            messages_to_reply.append(TextMessage(text=f"⚠️ ไม่พบข้อมูลหุ้น '{add_sym}'", quick_reply=default_qr))

    elif user_message.startswith("-"):
        rem_sym = user_message[1:].strip().upper()
        if rem_sym in watchlist:
            watchlist.remove(rem_sym)
        elif f"{rem_sym}.BK" in watchlist:
            watchlist.remove(f"{rem_sym}.BK")
        messages_to_reply.append(TextMessage(text=f"🗑️ ลบ {rem_sym} ออกจาก Watchlist แล้ว", quick_reply=default_qr))

    else:
        parts = command.split()
        stock_name = parts[0]
        tf = "1d"
        if len(parts) > 1:
            if parts[1] in ["M15", "15M"]:
                tf = "15m"
            elif parts[1] in ["H1", "1H", "60M"]:
                tf = "1h"
        data = get_pro_stock_data(stock_name, interval=tf, include_meta=True)
        if data:
            messages_to_reply.append(build_flex_card(data))
        else:
            messages_to_reply.append(TextMessage(text=f"⚠️ ไม่พบข้อมูลหุ้น '{stock_name}'", quick_reply=default_qr))

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=messages_to_reply)
        )

if __name__ == "__main__":
    app.run(port=5000)

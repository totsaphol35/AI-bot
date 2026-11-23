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
    FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)

configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET', 'YOUR_CHANNEL_SECRET'))
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL', '')
GOOGLE_SHEET_WEBHOOK_URL = os.getenv('GOOGLE_SHEET_WEBHOOK_URL', '')

user_watchlists = {}
discord_alert_cooldown = {}
last_auto_scan_time = 0

GLOBAL_SCAN_POOL = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO", "AMD", "NFLX",
    "QQQM", "SCHG", "JEPQ", "VOO", "RKLB", "PLTR", "COIN", "UBER", "CRM", "COST"
]

# ==========================================
# ส่วนที่ 1: Auditor Agent (จัดการ Google Sheets)
# ==========================================
def log_weekly_setup_to_sheets(symbol, price, support_l1, resistance_l1, score):
    if not GOOGLE_SHEET_WEBHOOK_URL:
        return
    week_str = datetime.now().strftime("%Y-W%U")
    payload = {
        "action": "log",
        "week": week_str,
        "symbol": symbol,
        "start_price": f"{price:.2f}",
        "support_l1": f"{support_l1:.2f}",
        "resistance_l1": f"{resistance_l1:.2f}",
        "setup_score": score
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
            return "📭 ยังไม่มีข้อมูลบันทึกสถิติใน Google Sheets"

        header = data[0]
        rows = data[1:]
        wins = 0
        total_evaluated = 0
        current_week = datetime.now().strftime("%Y-W%U")

        for row in rows:
            if len(row) < 10:
                continue
            week_str, symbol, start_p, sup_l1, res_l1, score, act_low, act_high, status, note = row

            if status == "PENDING" and week_str != current_week:
                df = fetch_clean_history(symbol, period="1mo", interval="1d")
                time.sleep(1.0)
                if not df.empty and len(df) >= 5:
                    recent_low = float(df['Low'].iloc[-5:].min())
                    recent_high = float(df['High'].iloc[-5:].max())
                    curr_close = float(df['Close'].iloc[-1])
                    s_val = float(sup_l1)

                    if recent_low <= s_val * 1.015:
                        if curr_close >= s_val:
                            status = "WIN"
                            note = f"แตะรับ {s_val:.2f} แล้วดีดกลับ (Low {recent_low:.2f})"
                        else:
                            status = "LOSS"
                            note = f"หลุดรับ {s_val:.2f} (Low {recent_low:.2f})"
                    else:
                        status = "NO_TOUCH"
                        note = "ราคายังไม่ลงมาทดสอบแนวรับ"

                    act_low_str = f"{recent_low:.2f}"
                    act_high_str = f"{recent_high:.2f}"

                    # ส่งคำสั่งอัปเดตกลับไปที่ Google Sheets App Script
                    update_payload = {
                        "action": "update",
                        "week": week_str,
                        "symbol": symbol,
                        "actual_low": act_low_str,
                        "actual_high": act_high_str,
                        "status": status,
                        "note": note
                    }
                    try:
                        requests.post(GOOGLE_SHEET_WEBHOOK_URL, json=update_payload, timeout=5)
                    except Exception:
                        pass

            if status in ["WIN", "LOSS"]:
                total_evaluated += 1
                if status == "WIN":
                    wins += 1

        hit_rate = (wins / total_evaluated * 100) if total_evaluated > 0 else 0.0
        pending_count = sum(1 for r in rows if r[8] == "PENDING")
        return (
            f"📊 **รายงานประสิทธิภาพแนวรับจริง (Google Sheets Audit)**\n"
            f"• ประเมินผลแล้ว: {total_evaluated} รายการ\n"
            f"• แนวรับรับอยู่ (WIN): {wins} ครั้ง\n"
            f"• อัตราความแม่นยำจริง (Hit Rate): **{hit_rate:.1f}%**\n"
            f"• รอปิดสัปดาห์ (PENDING): {pending_count} รายการ\n"
            f"📈 *ตรวจสอบตารางสถิติฉบับเต็มได้โดยตรงใน Google Sheets ของคุณ*"
        )
    except Exception as e:
        return f"⚠️ เกิดข้อผิดพลาดในการประเมินผล: {str(e)}"

# ==========================================
# ส่วนที่ 2: Data & Quant Engine (Yahoo Chart API v8)
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

def calculate_wilder_rsi(close_series, period=14):
    delta = close_series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
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
        "hidden_base": None,
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
        smc["structure"] = "BOS ขาขึ้น (เบรกโครงสร้างไปต่อ ↗️)" if curr_price >= ema_200 else "CHOCH ขาขึ้น (สัญญาณกลับตัวแรก 🔄)"
        smc["is_bullish_shift"] = True
    elif curr_price < prev_swing_low:
        smc["structure"] = "BOS ขาลง (หลุดโครงสร้างลงต่อ ↘️)" if curr_price < ema_200 else "CHOCH ขาลง (เตือนเสียทรงขาขึ้น ⚠️)"
    else:
        smc["structure"] = "ย่อพักตัวในกรอบโครงสร้าง (Pullback)"

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

    return smc

def build_confluence_zones(candidates, current_price, atr_val, is_support=True, rsi_val=50, rvol=1.0, macd_turning_up=False, is_uptrend=True, has_bull_shift=False):
    valid_candidates = [c for c in candidates if (0 < c['price'] <= current_price if is_support else c['price'] >= current_price) and not np.isnan(c['price'])]
    valid_candidates.sort(key=lambda x: x['price'], reverse=is_support)
    clusters = []
    for item in valid_candidates:
        placed = False
        for cluster in clusters:
            if abs(cluster['price'] - item['price']) / max(current_price, 0.0001) <= 0.010:
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
            if is_uptrend: base_score += 8
            elif not has_bull_shift: base_score -= 10
            if rsi_val <= 38: base_score += 5
            if rvol >= 1.2: base_score += 4
            if macd_turning_up: base_score += 4
            max_cap = 95 if (is_uptrend or has_bull_shift) else 75
        else:
            if not is_uptrend: base_score += 8
            if rsi_val >= 68: base_score += 5
            max_cap = 95
        score = min(max(int(base_score), 40), max_cap)
        results.append({'price': float(c['price']), 'label': " + ".join(c['labels'][:2]), 'setup_score': score})

    results.sort(key=lambda x: x['price'], reverse=is_support)
    step = max(atr_val, current_price * 0.015)
    fallback_sup = ["ATR Demand Zone", "Volatility Support", "Deep Structural Bid"]
    fallback_res = ["ATR Supply Zone", "Volatility Target", "Extended Target"]

    while len(results) < 3:
        idx = len(results)
        ref_price = results[-1]['price'] if results else current_price
        next_price = max(ref_price - step, current_price * 0.5) if is_support else ref_price + step
        lbl = (fallback_sup if is_support else fallback_res)[min(idx, 2)]
        results.append({'price': float(next_price), 'label': lbl, 'setup_score': 50 + (idx * 4)})
    return results[:3]

def get_pro_stock_data(symbol, interval="1d"):
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

        current_price = float(df['Close'].iloc[-1])
        prev_close = float(df['Close'].iloc[-2]) if len(df) >= 2 else current_price
        price_change = ((current_price - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0

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

        raw_supports = [
            {'price': current_price - 1.8 * atr_val, 'label': 'ATR Support', 'weight': 9},
        ]
        if ema_50 < current_price: raw_supports.append({'price': ema_50, 'label': 'EMA 50', 'weight': 10})
        if ema_200 < current_price: raw_supports.append({'price': ema_200, 'label': 'EMA 200 🛡️', 'weight': 16})
        if poc_price < current_price: raw_supports.append({'price': poc_price, 'label': 'POC', 'weight': 13})
        if smc["demand_zone"] and smc["demand_zone"] < current_price:
            raw_supports.append({'price': smc["demand_zone"], 'label': 'Demand Zone 🟢', 'weight': 15})
        if smc["bull_ob"] and smc["bull_ob"] < current_price:
            raw_supports.append({'price': smc["bull_ob"], 'label': 'Bullish OB 📦', 'weight': 16})

        raw_resistances = [
            {'price': current_price + 1.8 * atr_val, 'label': 'ATR Target', 'weight': 9},
        ]
        if ema_50 > current_price: raw_resistances.append({'price': ema_50, 'label': 'EMA 50', 'weight': 10})
        if ema_200 > current_price: raw_resistances.append({'price': ema_200, 'label': 'EMA 200 🚧', 'weight': 16})
        if poc_price > current_price: raw_resistances.append({'price': poc_price, 'label': 'POC', 'weight': 13})
        if smc["supply_zone"] and smc["supply_zone"] > current_price:
            raw_resistances.append({'price': smc["supply_zone"], 'label': 'Supply Zone 🔴', 'weight': 15})
        if smc["bear_ob"] and smc["bear_ob"] > current_price:
            raw_resistances.append({'price': smc["bear_ob"], 'label': 'Bearish OB 📦', 'weight': 16})

        sup_zones = build_confluence_zones(raw_supports, current_price, atr_val, True, rsi_val, rvol, macd_turning_up, is_uptrend, smc["is_bullish_shift"])
        res_zones = build_confluence_zones(raw_resistances, current_price, atr_val, False, rsi_val, rvol, macd_turning_up, is_uptrend, smc["is_bullish_shift"])

        best_entry = sup_zones[0]['price']
        stop_loss = best_entry - (1.2 * atr_val)
        take_profit = res_zones[0]['price']
        rr_ratio = max(take_profit - best_entry, 0.01) / max(best_entry - stop_loss, 0.01)
        max_score = int(max(s['setup_score'] for s in sup_zones[:3]))

        # ส่งข้อมูลเข้า Google Sheets ผ่าน Auditor Agent อัตโนมัติเมื่อดูรายวัน
        if interval == "1d":
            log_weekly_setup_to_sheets(symbol, current_price, sup_zones[0]['price'], res_zones[0]['price'], sup_zones[0]['setup_score'])

        currency = "THB" if ".BK" in symbol else "USD"
        change_sign = "+" if price_change >= 0 else ""
        change_color = "#00D26A" if price_change >= 0 else "#FF4D4D"

        return {
            "symbol": symbol,
            "interval": interval.upper(),
            "raw_price": current_price,
            "raw_rsi": rsi_val,
            "raw_rr": rr_ratio,
            "raw_sup1": sup_zones[0],
            "raw_res1": res_zones[0],
            "raw_sl": stop_loss,
            "price_str": f"{current_price:,.2f} {currency}",
            "change_str": f"{change_sign}{price_change:.2f}%",
            "change_color": change_color,
            "setup_score": f"{max_score}/100",
            "rsi_str": f"{rsi_val:.1f}",
            "structure": smc["structure"],
            "action_note": smc["action_note"],
            "ema_poc_str": f"EMA50: {ema_50:,.2f} | EMA200: {ema_200:,.2f}",
            "supports": [
                f"{sup_zones[0]['price']:,.2f} [Score {sup_zones[0]['setup_score']}] • {sup_zones[0]['label']}",
                f"{sup_zones[1]['price']:,.2f} [Score {sup_zones[1]['setup_score']}] • {sup_zones[1]['label']}",
                f"{sup_zones[2]['price']:,.2f} [Score {sup_zones[2]['setup_score']}] • {sup_zones[2]['label']}"
            ],
            "resistances": [
                f"{res_zones[0]['price']:,.2f} [ต้าน {res_zones[0]['setup_score']}] • {res_zones[0]['label']}",
                f"{res_zones[1]['price']:,.2f} [ต้าน {res_zones[1]['setup_score']}] • {res_zones[1]['label']}",
                f"{res_zones[2]['price']:,.2f} [ต้าน {res_zones[2]['setup_score']}] • {res_zones[2]['label']}"
            ],
            "setup_str": (
                f"• จุดรอรับ (Entry): {best_entry:,.2f} ({sup_zones[0]['label']})\n"
                f"• เป้าทำกำไร (TP): {take_profit:,.2f} | ตัดขาดทุน (SL): {stop_loss:,.2f}\n"
                f"• Risk/Reward (R:R): 1 : {rr_ratio:.1f} | {smc['action_note']}"
            )
        }
    except Exception as e:
        print(f"Error analyzing {symbol}: {e}")
        return None

def scan_and_notify_discord():
    if not DISCORD_WEBHOOK_URL:
        return
    all_symbols = list(GLOBAL_SCAN_POOL)
    for wl in user_watchlists.values():
        for s in wl:
            if s not in all_symbols: all_symbols.append(s)

    now_ts = time.time()
    for sym in all_symbols[:20]:
        data = get_pro_stock_data(sym, "1d")
        time.sleep(2.0)
        if not data: continue
        curr_p, sup1, res1 = data["raw_price"], data["raw_sup1"], data["raw_res1"]
        dist_sup = ((curr_p - sup1["price"]) / max(sup1["price"], 0.01)) * 100
        
        if 0 <= dist_sup <= 2.0 and sup1["setup_score"] >= 68:
            key = f"{sym}_SUP"
            if now_ts - discord_alert_cooldown.get(key, 0) > 6 * 3600:
                discord_alert_cooldown[key] = now_ts
                msg = f"🟢 **[Stock Sage AI Alert] ราคาหุ้น `{data['symbol']}` เข้าโซนรับสำคัญ!**\n• ราคา: `{data['price_str']}`\n• แนวรับ L1: `{sup1['price']:,.2f}` [{sup1['label']}] (Score: {sup1['setup_score']}/100)"
                try: requests.post(DISCORD_WEBHOOK_URL, json={"content": msg}, timeout=10)
                except Exception: pass

def build_flex_card(data):
    bubble_dict = {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#0F0F1E", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": f"📊 {data['symbol']}", "weight": "bold", "size": "xl", "color": "#FFFFFF"},
                {"type": "text", "text": f"{data['price_str']} ({data['change_str']})", "size": "lg", "weight": "bold", "color": data['change_color'], "margin": "sm"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": f"🏛️ โครงสร้างตลาด: {data['structure']}", "size": "xs", "weight": "bold", "wrap": True},
                {"type": "text", "text": f"📈 RSI: {data['rsi_str']}", "size": "xs", "margin": "xs"},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "🟢 โซนรับหลัก (Demand / OB):", "size": "sm", "weight": "bold", "color": "#008000", "margin": "md"},
                {"type": "text", "text": f"1. {data['supports'][0]}\n2. {data['supports'][1]}", "size": "xs", "wrap": True},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "💡 แผนเทรด & ความเสี่ยง:", "size": "xs", "weight": "bold", "margin": "md"},
                {"type": "text", "text": data['setup_str'], "size": "xxs", "wrap": True}
            ]
        }
    }
    return FlexMessage(alt_text=f"วิเคราะห์ {data['symbol']}", contents=FlexContainer.from_dict(bubble_dict))

@app.route("/", methods=['GET', 'HEAD'])
def health_check():
    global last_auto_scan_time
    if request.method == 'HEAD': return "OK", 200
    now_ts = time.time()
    if DISCORD_WEBHOOK_URL and (now_ts - last_auto_scan_time > 1800):
        last_auto_scan_time = now_ts
        threading.Thread(target=scan_and_notify_discord, daemon=True).start()
    return "Stock Sage AI (Multi-Agent Google Sheets Edition) is Awake!", 200

@app.route("/scan_discord", methods=['GET'])
def manual_discord_trigger():
    if not DISCORD_WEBHOOK_URL: return "⚠️ ยังไม่ได้ตั้งค่า DISCORD_WEBHOOK_URL", 400
    threading.Thread(target=scan_and_notify_discord, daemon=True).start()
    return "✅ สั่งรันสแกนหุ้นส่งเข้า Discord เรียบร้อยแล้ว!", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
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
            "📖 **คู่มือ Stock Sage AI**\n\n"
            "• พิมพ์ชื่อหุ้น (เช่น `NVDA`, `PTT.BK`, `NVDA H1`) ดูการ์ดแผนเทรด\n"
            "• `/WEEKLY_PERF` ตรวจรายงานความแม่นยำจริงจาก Google Sheets\n"
            "• `+[ชื่อหุ้น]` / `-[ชื่อหุ้น]` จัดการ Watchlist\n"
            "• `/SUMMARY` สรุปพอร์ต | `/SCAN_SUPPORT` สแกนหาโซนรับ"
        )
        messages_to_reply.append(TextMessage(text=help_text))

    elif command == "/WEEKLY_PERF":
        report = evaluate_weekly_performance_from_sheets()
        messages_to_reply.append(TextMessage(text=report))

    elif command == "/SUMMARY":
        summary_lines = ["📊 **สรุปโครงสร้างพอร์ต:**"]
        for sym in watchlist:
            d = get_pro_stock_data(sym, "1d")
            if d: summary_lines.append(f"\n🔹 **{d['symbol']}** : {d['price_str']}\n   └ รับ: {d['supports'][0]}")
        messages_to_reply.append(TextMessage(text="\n".join(summary_lines)))

    elif command == "/SCAN_SUPPORT":
        setups = []
        for sym in GLOBAL_SCAN_POOL[:10]:
            d = get_pro_stock_data(sym, "1d")
            if d and int(d['setup_score'].split('/')[0]) >= 68:
                setups.append(d)
        if setups:
            lines = ["🎯 **หุ้นที่เข้าโซนรับเด่น:**"]
            for s in setups[:5]: lines.append(f"\n🏆 **{s['symbol']}** ({s['price_str']})\n   • {s['supports'][0]}")
            messages_to_reply.append(TextMessage(text="\n".join(lines)))
        else:
            messages_to_reply.append(TextMessage(text="🛡️ ยังไม่มีหุ้นตัวหลักเข้าโซนรับในรอบนี้"))

    elif user_message.startswith("+"):
        add_sym = user_message[1:].strip().upper()
        chk = get_pro_stock_data(add_sym)
        if chk:
            if chk['symbol'] not in watchlist: watchlist.append(chk['symbol'])
            messages_to_reply.append(TextMessage(text=f"✅ เพิ่ม **{chk['symbol']}** เข้า Watchlist แล้ว"))
        else:
            messages_to_reply.append(TextMessage(text=f"⚠️ ไม่พบข้อมูลหุ้น '{add_sym}'"))

    elif user_message.startswith("-"):
        rem_sym = user_message[1:].strip().upper()
        if rem_sym in watchlist: watchlist.remove(rem_sym)
        elif f"{rem_sym}.BK" in watchlist: watchlist.remove(f"{rem_sym}.BK")
        messages_to_reply.append(TextMessage(text=f"🗑️ ลบ **{rem_sym}** ออกจาก Watchlist แล้ว"))

    else:
        parts = command.split()
        stock_name = parts[0]
        tf = "1d"
        if len(parts) > 1:
            if parts[1] in ["M15", "15M"]: tf = "15m"
            elif parts[1] in ["H1", "1H", "60M"]: tf = "1h"
        data = get_pro_stock_data(stock_name, interval=tf)
        if data: messages_to_reply.append(build_flex_card(data))
        else: messages_to_reply.append(TextMessage(text=f"⚠️ ไม่พบข้อมูลหุ้น '{stock_name}'"))

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=messages_to_reply)
        )

if __name__ == "__main__":
    app.run(port=5000)

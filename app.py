import os
import csv
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

# ตั้งค่า LINE Access Token, Secret และ Discord Webhook URL จาก Environment Variables บน Render
configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET', 'YOUR_CHANNEL_SECRET'))
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL', '')

# หน่วยความจำเก็บ Watchlist และระบบกันแจ้งเตือนซ้ำใน Discord
user_watchlists = {}
discord_alert_cooldown = {}  # { "SYMBOL_ZONE": timestamp }
last_auto_scan_time = 0
PERF_LOG_FILE = "weekly_performance_log.csv"

# คลังหุ้นชั้นนำสำหรับสแกนเข้า Discord อัตโนมัติ
GLOBAL_SCAN_POOL = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO", "AMD", "NFLX",
    "QQQM", "SCHG", "JEPQ", "VOO", "RKLB", "PLTR", "COIN", "UBER", "CRM", "COST"
]

# ==========================================
# ส่วนที่ 1: ระบบจัดเก็บและประเมินผลจริงรายสัปดาห์
# ==========================================
def init_perf_log():
    if not os.path.exists(PERF_LOG_FILE):
        with open(PERF_LOG_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                "Week", "Symbol", "Start_Price", "Support_L1",
                "Resistance_L1", "Setup_Score", "Actual_Low",
                "Actual_High", "Status", "Note"
            ])

def log_weekly_setup(symbol, price, support_l1, resistance_l1, score):
    try:
        init_perf_log()
        week_str = datetime.now().strftime("%Y-W%U")
        already_logged = False

        if os.path.exists(PERF_LOG_FILE):
            with open(PERF_LOG_FILE, mode='r', encoding='utf-8') as f:
                reader = csv.reader(f)
                next(reader, None)
                for row in reader:
                    if len(row) >= 2 and row[0] == week_str and row[1] == symbol:
                        already_logged = True
                        break

        if not already_logged:
            with open(PERF_LOG_FILE, mode='a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    week_str, symbol, f"{price:.2f}", f"{support_l1:.2f}",
                    f"{resistance_l1:.2f}", score, "PENDING", "PENDING",
                    "PENDING", "Waiting for weekly price action"
                ])
    except Exception as e:
        print(f"Log weekly setup error: {e}")

def evaluate_weekly_performance():
    init_perf_log()
    if not os.path.exists(PERF_LOG_FILE):
        return "ยังไม่มีข้อมูลบันทึกสถิติรายสัปดาห์ครับ"

    updated_rows = []
    wins = 0
    total_evaluated = 0
    current_week = datetime.now().strftime("%Y-W%U")

    with open(PERF_LOG_FILE, mode='r', encoding='utf-8') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return "ยังไม่มีข้อมูลบันทึกสถิติรายสัปดาห์ครับ"
        updated_rows.append(header)

        for row in reader:
            if len(row) < 10:
                continue
            week_str, symbol, start_p, sup_l1, res_l1, score, act_low, act_high, status, note = row

            # ประเมินเฉพาะรายการที่ค้างอยู่และข้ามสัปดาห์มาแล้ว หรือทดสอบย้อนหลัง 5 วันทำการ
            if status == "PENDING" and week_str != current_week:
                df = fetch_clean_history(symbol, period="14d", interval="1d")
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

                    act_low = f"{recent_low:.2f}"
                    act_high = f"{recent_high:.2f}"

            if status in ["WIN", "LOSS"]:
                total_evaluated += 1
                if status == "WIN":
                    wins += 1

            updated_rows.append([week_str, symbol, start_p, sup_l1, res_l1, score, act_low, act_high, status, note])

    with open(PERF_LOG_FILE, mode='w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerows(updated_rows)

    hit_rate = (wins / total_evaluated * 100) if total_evaluated > 0 else 0.0
    pending_count = sum(1 for r in updated_rows[1:] if r[8] == "PENDING")
    return (
        f"📊 **รายงานประสิทธิภาพแนวรับจริง (Weekly Audit)**\n"
        f"• ทดสอบและประเมินผลแล้ว: {total_evaluated} รายการ\n"
        f"• แนวรับรับอยู่ (WIN): {wins} ครั้ง\n"
        f"• อัตราความแม่นยำจริง (Hit Rate): **{hit_rate:.1f}%**\n"
        f"• รอปิดสัปดาห์ (PENDING): {pending_count} รายการ\n"
        f"⚠️ *บันทึกจากราคาตลาดจริงรายสัปดาห์ ผลในอดีตไม่รับประกันอนาคต*"
    )

# ==========================================
# ส่วนที่ 2: ดึงข้อมูลและคำนวณอินดิเคเตอร์
# ==========================================
def fetch_clean_history(symbol, period="1y", interval="1d"):
    symbol = symbol.upper().strip()
    df = pd.DataFrame()

    # ใช้ yf.download เป็นหลักเพื่อลดปัญหา Invalid Crumb จาก Ticker.history
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
    except Exception as e:
        print(f"yf.download failed for {symbol}: {e}")

    if df is None or df.empty:
        try:
            ticker_obj = yf.Ticker(symbol)
            df = ticker_obj.history(period=period, interval=interval, auto_adjust=True)
        except Exception as e:
            print(f"Ticker.history failed for {symbol}: {e}")

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

def get_upcoming_earnings(symbol):
    try:
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar
        if isinstance(cal, dict) and 'Earnings Date' in cal and cal['Earnings Date']:
            next_date = pd.to_datetime(cal['Earnings Date'][0]).strftime('%Y-%m-%d')
            return f"📅 งบออก: {next_date}"
    except Exception:
        pass
    return "📅 งบ: ยังไม่ระบุ"

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
    poc_price = (bin_edges[max_bin] + bin_edges[max_bin + 1]) / 2.0
    return float(poc_price)

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
        if curr_price >= ema_200:
            smc["structure"] = "BOS ขาขึ้น (เบรกโครงสร้างไปต่อ ↗️)"
        else:
            smc["structure"] = "CHOCH ขาขึ้น (สัญญาณกลับตัวแรก 🔄)"
        smc["is_bullish_shift"] = True
    elif curr_price < prev_swing_low:
        if curr_price < ema_200:
            smc["structure"] = "BOS ขาลง (หลุดโครงสร้างลงต่อ ↘️)"
        else:
            smc["structure"] = "CHOCH ขาลง (เตือนเสียทรงขาขึ้น ⚠️)"
    else:
        smc["structure"] = "ย่อพักตัวในกรอบโครงสร้าง (Pullback)"

    swept_low = any((l < prev_swing_low and c > prev_swing_low) for l, c in zip(lows[-5:], closes[-5:]))
    if swept_low:
        smc["liquidity_sweep"] = True

    for i in range(len(df) - 2, max(len(df) - 35, 1), -1):
        body_next = closes[i + 1] - opens[i + 1]
        is_bearish_ob = closes[i] < opens[i]
        if body_next > 1.1 * atr_val and closes[i + 1] > highs[i]:
            if smc["demand_zone"] is None and curr_price >= lows[i + 1]:
                smc["demand_zone"] = float((lows[i + 1] + opens[i + 1]) / 2.0)
            if is_bearish_ob and smc["bull_ob"] is None and curr_price >= lows[i]:
                smc["bull_ob"] = float((lows[i] + highs[i]) / 2.0)
        if smc["demand_zone"] and smc["bull_ob"]:
            break

    for i in range(len(df) - 2, max(len(df) - 35, 1), -1):
        body_next = opens[i + 1] - closes[i + 1]
        is_bullish_ob = closes[i] > opens[i]
        if body_next > 1.1 * atr_val and closes[i + 1] < lows[i]:
            if smc["supply_zone"] is None and curr_price <= highs[i + 1]:
                smc["supply_zone"] = float((highs[i + 1] + opens[i + 1]) / 2.0)
            if is_bullish_ob and smc["bear_ob"] is None and curr_price <= highs[i]:
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

    for i in range(len(df) - 2, max(len(df) - 30, 1), -1):
        candle_range = highs[i] - lows[i]
        next_push = closes[i + 1] - opens[i + 1]
        if candle_range < 0.6 * atr_val and next_push > 1.0 * atr_val:
            mid_hb = float((highs[i] + lows[i]) / 2.0)
            if mid_hb < curr_price:
                smc["hidden_base"] = mid_hb
                break

    if smc["liquidity_sweep"] and smc["is_bullish_shift"]:
        smc["action_note"] = "✅ Sweep + BOS/CHOCH ครบ! รอย่อรับ OB/FVG"
    elif smc["liquidity_sweep"]:
        smc["action_note"] = "⚡ กวาด Liquidity ล่างแล้ว รอแท่งยืนยันกลับตัว"
    elif smc["is_bullish_shift"]:
        smc["action_note"] = "📈 โครงสร้างขาขึ้นแข็งแกร่ง รอย่อเข้าโซน Demand/OB"
    else:
        smc["action_note"] = "🛡️ เน้นตั้งรับโซนลึก หลีกเลี่ยงไล่ราคา"

    return smc

def build_confluence_zones(candidates, current_price, atr_val, is_support=True, rsi_val=50, rvol=1.0, macd_turning_up=False, is_uptrend=True, has_bull_shift=False):
    if is_support:
        valid_candidates = [c for c in candidates if 0 < c['price'] <= current_price and not np.isnan(c['price'])]
    else:
        valid_candidates = [c for c in candidates if c['price'] >= current_price and not np.isnan(c['price'])]

    valid_candidates.sort(key=lambda x: x['price'], reverse=is_support)
    clusters = []
    merge_threshold = 0.010

    for item in valid_candidates:
        placed = False
        for cluster in clusters:
            if abs(cluster['price'] - item['price']) / max(current_price, 0.0001) <= merge_threshold:
                total_w = cluster['weight'] + item['weight']
                cluster['price'] = (cluster['price'] * cluster['weight'] + item['price'] * item['weight']) / total_w
                cluster['weight'] = total_w
                if item['label'] not in cluster['labels']:
                    cluster['labels'].append(item['label'])
                placed = True
                break
        if not placed:
            clusters.append({
                'price': float(item['price']),
                'weight': item['weight'],
                'labels': [item['label']]
            })

    results = []
    for c in clusters:
        base_score = 40 + (c['weight'] * 1.3) + (len(c['labels']) - 1) * 4
        if is_support:
            if is_uptrend:
                base_score += 8
            elif not has_bull_shift:
                base_score -= 10
            if rsi_val <= 38: base_score += 5
            if rvol >= 1.2: base_score += 4
            if macd_turning_up: base_score += 4
            max_cap = 95 if (is_uptrend or has_bull_shift) else 75
        else:
            if not is_uptrend: base_score += 8
            if rsi_val >= 68: base_score += 5
            max_cap = 95

        score = min(max(int(base_score), 40), max_cap)
        combined_label = " + ".join(c['labels'][:2])
        results.append({
            'price': float(c['price']),
            'label': combined_label,
            'setup_score': score
        })

    results.sort(key=lambda x: x['price'], reverse=is_support)

    step = max(atr_val, current_price * 0.015)
    fallback_labels_sup = ["ATR Demand Zone", "Volatility Support", "Deep Structural Bid"]
    fallback_labels_res = ["ATR Supply Zone", "Volatility Target", "Extended Target"]

    while len(results) < 3:
        idx = len(results)
        ref_price = results[-1]['price'] if results else current_price
        if is_support:
            next_price = max(ref_price - step, current_price * 0.5)
            lbl = fallback_labels_sup[min(idx, 2)]
        else:
            next_price = ref_price + step
            lbl = fallback_labels_res[min(idx, 2)]
        results.append({
            'price': float(next_price),
            'label': lbl,
            'setup_score': 50 + (idx * 4)
        })

    return results[:3]

# ==========================================
# ส่วนที่ 3: วิเคราะห์หุ้นรายตัว
# ==========================================
def get_pro_stock_data(symbol, interval="1d", fetch_earnings=True):
    try:
        symbol = symbol.upper().strip()
        period = "60d" if interval in ["15m", "1h"] else "1y"
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
        high_60d = float(df['High'].tail(60).max())
        low_60d = float(df['Low'].tail(60).min())
        fib_range = max(high_60d - low_60d, atr_val)
        fib_618 = high_60d - (0.618 * fib_range)
        fib_382 = high_60d - (0.382 * fib_range)

        smc = analyze_leorio_smc(df, atr_val, ema_200)
        is_uptrend = current_price >= ema_200

        raw_supports = [
            {'price': min(low_60d, current_price - 1.5 * atr_val), 'label': 'Major Low', 'weight': 12},
            {'price': current_price - 1.8 * atr_val, 'label': 'ATR Support', 'weight': 9},
        ]
        if ema_50 < current_price:
            raw_supports.append({'price': ema_50, 'label': 'EMA 50', 'weight': 10})
        if ema_200 < current_price:
            raw_supports.append({'price': ema_200, 'label': 'EMA 200 🛡️', 'weight': 16})
        if poc_price < current_price:
            raw_supports.append({'price': poc_price, 'label': 'POC (Approx)', 'weight': 13})
        if fib_618 < current_price:
            raw_supports.append({'price': fib_618, 'label': 'Fibo 61.8%', 'weight': 12})
        if smc["demand_zone"] and smc["demand_zone"] < current_price:
            raw_supports.append({'price': smc["demand_zone"], 'label': 'Demand Zone 🟢', 'weight': 15})
        if smc["bull_ob"] and smc["bull_ob"] < current_price:
            raw_supports.append({'price': smc["bull_ob"], 'label': 'Bullish OB 📦', 'weight': 16})
        if smc["bull_fvg"] and smc["bull_fvg"] < current_price:
            raw_supports.append({'price': smc["bull_fvg"], 'label': 'Bullish FVG ⚡', 'weight': 14})
        if smc["hidden_base"] and smc["hidden_base"] < current_price:
            is_fibo_aligned = abs(smc["hidden_base"] - fib_618) / current_price <= 0.025
            hb_label = 'Hidden Base + Fibo' if is_fibo_aligned else 'Hidden Base'
            raw_supports.append({'price': smc["hidden_base"], 'label': hb_label, 'weight': 13 if is_fibo_aligned else 8})

        raw_resistances = [
            {'price': max(high_60d, current_price + 1.5 * atr_val), 'label': 'Major High', 'weight': 12},
            {'price': current_price + 1.8 * atr_val, 'label': 'ATR Target', 'weight': 9},
        ]
        if ema_50 > current_price:
            raw_resistances.append({'price': ema_50, 'label': 'EMA 50', 'weight': 10})
        if ema_200 > current_price:
            raw_resistances.append({'price': ema_200, 'label': 'EMA 200 🚧', 'weight': 16})
        if poc_price > current_price:
            raw_resistances.append({'price': poc_price, 'label': 'POC (Approx)', 'weight': 13})
        if fib_382 > current_price:
            raw_resistances.append({'price': fib_382, 'label': 'Fibo 38.2%', 'weight': 11})
        if smc["supply_zone"] and smc["supply_zone"] > current_price:
            raw_resistances.append({'price': smc["supply_zone"], 'label': 'Supply Zone 🔴', 'weight': 15})
        if smc["bear_ob"] and smc["bear_ob"] > current_price:
            raw_resistances.append({'price': smc["bear_ob"], 'label': 'Bearish OB 📦', 'weight': 16})
        if smc["bear_fvg"] and smc["bear_fvg"] > current_price:
            raw_resistances.append({'price': smc["bear_fvg"], 'label': 'Bearish FVG ⚡', 'weight': 14})

        sup_zones = build_confluence_zones(raw_supports, current_price, atr_val, True, rsi_val, rvol, macd_turning_up, is_uptrend, smc["is_bullish_shift"])
        res_zones = build_confluence_zones(raw_resistances, current_price, atr_val, False, rsi_val, rvol, macd_turning_up, is_uptrend, smc["is_bullish_shift"])

        best_entry = sup_zones[0]['price']
        stop_loss = best_entry - (1.2 * atr_val)
        take_profit = res_zones[0]['price']

        risk = max(best_entry - stop_loss, 0.01)
        reward = max(take_profit - best_entry, 0.01)
        rr_ratio = reward / risk

        max_score = int(max(s['setup_score'] for s in sup_zones[:3]))
        if interval == "1d" and fetch_earnings:
            earnings_info = get_upcoming_earnings(symbol)
        else:
            earnings_info = f"⏱️ TF: {interval.upper()}"

        # บันทึกสถิติรายสัปดาห์อัตโนมัติสำหรับ Timeframe 1d
        if interval == "1d":
            log_weekly_setup(symbol, current_price, sup_zones[0]['price'], res_zones[0]['price'], sup_zones[0]['setup_score'])

        mom_tags = []
        if rsi_val <= 35: mom_tags.append("RSI Oversold 🟢")
        elif rsi_val >= 70: mom_tags.append("RSI Overbought 🔴")
        else: mom_tags.append("RSI ปกติ")
        if macd_turning_up: mom_tags.append("MACD ยกตัว ↗️")
        if smc["liquidity_sweep"]: mom_tags.append("Sweep ล่าง ✅")

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
            "rsi_str": f"{rsi_val:.1f} ({' | '.join(mom_tags)})",
            "structure": smc["structure"],
            "action_note": smc["action_note"],
            "ema_poc_str": f"EMA50: {ema_50:,.2f} | EMA200: {ema_200:,.2f} | {earnings_info}",
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

# ==========================================
# ส่วนที่ 4: สแกนและแจ้งเตือนเข้า Discord (กันโดนบล็อก 429/401)
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
    cooldown_sec = 6 * 3600

    for sym in all_symbols[:20]:
        # ปิด fetch_earnings เพื่อไม่ให้เรียก ticker.calendar และเว้นระยะ 2.5 วินาทีกัน Yahoo บล็อก
        data = get_pro_stock_data(sym, interval="1d", fetch_earnings=False)
        time.sleep(2.5)
        if not data:
            continue

        curr_p = data["raw_price"]
        sup1 = data["raw_sup1"]
        res1 = data["raw_res1"]

        dist_to_sup_pct = ((curr_p - sup1["price"]) / max(sup1["price"], 0.01)) * 100
        dist_to_res_pct = ((res1["price"] - curr_p) / max(curr_p, 0.01)) * 100

        if 0 <= dist_to_sup_pct <= 2.0 and sup1["setup_score"] >= 68:
            key = f"{sym}_SUP"
            if now_ts - discord_alert_cooldown.get(key, 0) > cooldown_sec:
                discord_alert_cooldown[key] = now_ts
                msg = (
                    f"🟢 **[LEORIO & Quant Alert] ราคาเข้าโซนแนวรับสำคัญ!**\n"
                    f"• **สินทรัพย์:** `{data['symbol']}` | **ราคาล่าสุด:** `{data['price_str']}` ({data['change_str']})\n"
                    f"• **โครงสร้าง:** {data['structure']} | **RSI:** `{data['raw_rsi']:.1f}`\n"
                    f"• **แนวรับ L1:** `{sup1['price']:,.2f}` [{sup1['label']}] (ห่าง `{dist_to_sup_pct:.2f}%` | **Setup Score: {sup1['setup_score']}/100**)\n"
                    f"• **แผนบริหารความเสี่ยง:** TP `{res1['price']:,.2f}` | SL `{data['raw_sl']:,.2f}` (**R:R = 1:{data['raw_rr']:.1f}**)\n"
                    f"• **กลยุทธ์:** {data['action_note']}"
                )
                try:
                    requests.post(DISCORD_WEBHOOK_URL, json={"content": msg}, timeout=10)
                except Exception as e:
                    print(f"Discord post error: {e}")

        elif 0 <= dist_to_res_pct <= 1.5 and res1["setup_score"] >= 68:
            key = f"{sym}_RES"
            if now_ts - discord_alert_cooldown.get(key, 0) > cooldown_sec:
                discord_alert_cooldown[key] = now_ts
                msg = (
                    f"🔴 **[Target / Supply Alert] ราคาเข้าใกล้โซนแนวต้านสำคัญ!**\n"
                    f"• **สินทรัพย์:** `{data['symbol']}` | **ราคาล่าสุด:** `{data['price_str']}` ({data['change_str']})\n"
                    f"• **โครงสร้าง:** {data['structure']} | **RSI:** `{data['raw_rsi']:.1f}`\n"
                    f"• **แนวต้าน L1:** `{res1['price']:,.2f}` [{res1['label']}] (ห่าง `{dist_to_res_pct:.2f}%` | **ความแข็งแกร่งต้าน: {res1['setup_score']}/100**)\n"
                    f"• **แนวรับอ้างอิงด้านล่าง:** `{sup1['price']:,.2f}` [{sup1['label']}]"
                )
                try:
                    requests.post(DISCORD_WEBHOOK_URL, json={"content": msg}, timeout=10)
                except Exception as e:
                    print(f"Discord post error: {e}")

# ==========================================
# ส่วนที่ 5: การ์ด LINE Flex Message และ Routes
# ==========================================
def build_flex_card(data):
    tf_badge = f" ({data['interval']})" if data['interval'] != "1D" else ""
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
                            "text": f"📊 {data['symbol']}{tf_badge}",
                            "weight": "bold",
                            "size": "xl",
                            "color": "#FFFFFF",
                            "flex": 1
                        },
                        {
                            "type": "text",
                            "text": f"Setup Score {data['setup_score']}",
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
                    "text": f"🏛️ โครงสร้างตลาด: {data['structure']}",
                    "size": "xs",
                    "color": "#1A1A2E",
                    "weight": "bold",
                    "wrap": True
                },
                {
                    "type": "text",
                    "text": f"📈 โมเมนตัม: {data['rsi_str']}",
                    "size": "xs",
                    "color": "#444444",
                    "margin": "xs",
                    "wrap": True
                },
                {
                    "type": "text",
                    "text": f"🎯 {data['ema_poc_str']}",
                    "size": "xxs",
                    "color": "#0066CC",
                    "margin": "xs",
                    "weight": "bold",
                    "wrap": True
                },
                {"type": "separator", "margin": "md"},
                {
                    "type": "text",
                    "text": "🟢 โซนรับ (Demand / OB / FVG / EMA):",
                    "size": "sm",
                    "weight": "bold",
                    "color": "#008000",
                    "margin": "md"
                },
                {
                    "type": "text",
                    "text": f"1. {data['supports'][0]}\n2. {data['supports'][1]}\n3. {data['supports'][2]}",
                    "size": "xs",
                    "color": "#222222",
                    "margin": "xs",
                    "wrap": True
                },
                {
                    "type": "text",
                    "text": "🔴 โซนต้าน (Supply / OB / FVG / Target):",
                    "size": "sm",
                    "weight": "bold",
                    "color": "#D93025",
                    "margin": "md"
                },
                {
                    "type": "text",
                    "text": f"1. {data['resistances'][0]}\n2. {data['resistances'][1]}\n3. {data['resistances'][2]}",
                    "size": "xs",
                    "color": "#222222",
                    "margin": "xs",
                    "wrap": True
                },
                {"type": "separator", "margin": "md"},
                {
                    "type": "text",
                    "text": "💡 แผนจุดเข้า-ออก & บริหารความเสี่ยง:",
                    "size": "xs",
                    "weight": "bold",
                    "color": "#1A1A2E",
                    "margin": "md"
                },
                {
                    "type": "text",
                    "text": data['setup_str'],
                    "size": "xxs",
                    "color": "#333333",
                    "margin": "xs",
                    "wrap": True
                },
                {
                    "type": "text",
                    "text": "⚠️ การลงทุนมีความเสี่ยง ผลในอดีตไม่รับประกันอนาคต (SEC/FINRA)",
                    "size": "xxs",
                    "color": "#888888",
                    "margin": "md",
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
                        "label": "⏱️ ดูกราฟ H1",
                        "text": f"{data['symbol']} H1"
                    }
                }
            ]
        }
    }
    return FlexMessage(
        alt_text=f"วิเคราะห์ {data['symbol']} ({data['structure']})",
        contents=FlexContainer.from_dict(bubble_dict)
    )

@app.route("/", methods=['GET', 'HEAD'])
def health_check():
    global last_auto_scan_time
    if request.method == 'HEAD':
        return "OK", 200
    now_ts = time.time()
    if DISCORD_WEBHOOK_URL and (now_ts - last_auto_scan_time > 1800):
        last_auto_scan_time = now_ts
        threading.Thread(target=scan_and_notify_discord, daemon=True).start()
    return "Stock Sage AI (LINE Interactive + Discord Auto-Alert + Weekly Audit) is Awake!", 200

@app.route("/scan_discord", methods=['GET'])
def manual_discord_trigger():
    if not DISCORD_WEBHOOK_URL:
        return "⚠️ ยังไม่ได้ตั้งค่า DISCORD_WEBHOOK_URL ใน Environment ของ Render", 400
    threading.Thread(target=scan_and_notify_discord, daemon=True).start()
    return "✅ สั่งรันสแกนหุ้นเข้าแนวรับ-แนวต้านส่งไปยัง Discord เรียบร้อยแล้ว!", 200

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
            "📖 **คู่มือ Stock Sage AI (Hybrid LINE + Discord)**\n\n"
            "• พิมพ์ชื่อหุ้น (เช่น `NVDA`, `PTT.BK`, `NVDA H1`) เพื่อดูการ์ดแผนเทรด\n"
            "• `+[ชื่อหุ้น]` เพิ่มเข้า Watchlist / `-[ชื่อหุ้น]` ลบออก\n"
            "• `/LIST` ดูรายชื่อหุ้นเฝ้าระวัง\n"
            "• `/SUMMARY` สรุปโครงสร้างตลาดและแนวรับของพอร์ต\n"
            "• `/SCAN_SUPPORT` สแกนหาหุ้นที่เข้าใกล้โซนรับสำคัญ\n"
            "• `/WEEKLY_PERF` ตรวจรายงานความแม่นยำจริงรายสัปดาห์"
        )
        messages_to_reply.append(TextMessage(text=help_text))

    elif command == "/WEEKLY_PERF":
        report = evaluate_weekly_performance()
        messages_to_reply.append(TextMessage(text=report))

    elif command == "/LIST":
        if not watchlist:
            messages_to_reply.append(TextMessage(text="📭 Watchlist ว่างเปล่า พิมพ์ +ตามด้วยชื่อหุ้นเพื่อเพิ่ม"))
        else:
            list_str = "\n".join([f"{i+1}. {s}" for i, s in enumerate(watchlist)])
            messages_to_reply.append(TextMessage(text=f"📋 **Watchlist ({len(watchlist)} ตัว):**\n{list_str}"))

    elif command == "/SUMMARY":
        if not watchlist:
            messages_to_reply.append(TextMessage(text="📭 Watchlist ว่างเปล่าครับ"))
        else:
            summary_lines = [f"📊 **สรุปโครงสร้าง & โซนรับพอร์ต ({len(watchlist)} ตัว):**"]
            for sym in watchlist:
                d = get_pro_stock_data(sym, "1d", fetch_earnings=False)
                if d:
                    summary_lines.append(
                        f"\n🔹 **{d['symbol']}** : {d['price_str']} ({d['change_str']})\n"
                        f"   ├ โครงสร้าง: {d['structure']}\n"
                        f"   └ รับหลัก: {d['supports'][0]}"
                    )
            messages_to_reply.append(TextMessage(text="\n".join(summary_lines)))

    elif command == "/SCAN_SUPPORT":
        high_score_setups = []
        for sym in GLOBAL_SCAN_POOL[:10]:
            d = get_pro_stock_data(sym, "1d", fetch_earnings=False)
            if d:
                score_num = int(d['setup_score'].split('/')[0])
                if score_num >= 68:
                    high_score_setups.append((score_num, d))
        high_score_setups.sort(key=lambda x: -x[0])

        if high_score_setups:
            lines = ["🎯 **หุ้นที่เข้าเกณฑ์โครงสร้าง & โซนรับเด่น:**"]
            for sc, item in high_score_setups[:6]:
                lines.append(
                    f"\n🏆 **{item['symbol']}** ({item['price_str']} | {item['change_str']})\n"
                    f"   • {item['structure']}\n"
                    f"   • รับหลัก: {item['supports'][0]}"
                )
            lines.append("\n⚠️ *การลงทุนมีความเสี่ยง ผลในอดีตไม่รับประกันอนาคต (SEC/FINRA)*")
            messages_to_reply.append(TextMessage(text="\n".join(lines)))
        else:
            messages_to_reply.append(TextMessage(text="🛡️ ยังไม่มีหุ้นตัวหลักเข้าโซนรับที่มีคะแนนสูงในรอบนี้"))

    elif user_message.startswith("+"):
        stock_to_add = user_message[1:].strip().upper()
        if stock_to_add:
            check = get_pro_stock_data(stock_to_add, fetch_earnings=False)
            if check:
                actual_sym = check['symbol']
                if actual_sym not in watchlist:
                    watchlist.append(actual_sym)
                    messages_to_reply.append(TextMessage(text=f"✅ เพิ่ม **{actual_sym}** ({check['price_str']}) เข้า Watchlist แล้ว"))
                else:
                    messages_to_reply.append(TextMessage

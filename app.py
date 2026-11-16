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

# คลังหุ้นชั้นนำระดับโลกสำหรับสแกนหาจุดตั้งรับ
GLOBAL_SCAN_POOL = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO", "AMD", "NFLX",
    "QQQM", "SCHG", "JEPQ", "VOO", "RKLB", "PLTR", "COIN", "UBER", "CRM", "COST"
]

# ดึงข้อมูลย้อนหลัง 2 ปี (period="2y") และปิด auto_adjust เพื่อให้ราคาและ EMA 200 ตรงกับกราฟจริงบน TradingView
def fetch_clean_history(symbol, period="2y"):
    symbol = symbol.upper().strip()
    df = pd.DataFrame()
    
    try:
        ticker_obj = yf.Ticker(symbol)
        df = ticker_obj.history(period=period, auto_adjust=False)
    except Exception as e:
        print(f"Ticker.history failed for {symbol}: {e}")
        
    if df is None or df.empty:
        try:
            df = yf.download(symbol, period=period, progress=False, auto_adjust=False)
        except Exception as e:
            print(f"yf.download failed for {symbol}: {e}")
            
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

# 1. สูตร RSI มาตรฐาน Wilder's Smoothing
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

# 2. สูตร ATR มาตรฐาน Wilder's RMA (ตรงกับค่า ATR บน TradingView)
def calculate_atr(df, period=14):
    high_low = df['High'] - df['Low']
    high_close = np.abs(df['High'] - df['Close'].shift())
    low_close = np.abs(df['Low'] - df['Close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = ranges.max(axis=1)
    atr_series = true_range.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    atr_val = float(atr_series.iloc[-1])
    if np.isnan(atr_val) or atr_val <= 0:
        atr_val = float(df['Close'].iloc[-1]) * 0.015
    return atr_val

# 3. สูตรหา Volume Profile POC (Point of Control รอบ 6 เดือน / 126 วันทำการ)
def calculate_volume_poc(df, bins=30):
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

# 4. ระบบจัดกลุ่ม Confluence แบบควบคุมเพดานคะแนนตามความเป็นจริง (ไม่ให้คะแนนเฟ้อในขาลง)
def build_confluence_zones(candidates, current_price, atr_val, is_support=True, rsi_val=50, rvol=1.0, macd_turning_up=False, is_uptrend=True):
    # คัดเฉพาะแนวรับที่อยู่ต่ำกว่าราคาปัจจุบัน และแนวต้านที่อยู่สูงกว่าราคาปัจจุบันจริงๆ
    if is_support:
        valid_candidates = [c for c in candidates if 0 < c['price'] < current_price * 0.998 and not np.isnan(c['price'])]
    else:
        valid_candidates = [c for c in candidates if c['price'] > current_price * 1.002 and not np.isnan(c['price'])]
        
    valid_candidates.sort(key=lambda x: x['price'], reverse=is_support)
    clusters = []
    merge_threshold = 0.012 # รัศมีรวมโซน 1.2%
    
    for item in valid_candidates:
        placed = False
        for cluster in clusters:
            if abs(cluster['price'] - item['price']) / max(current_price, 0.0001) <= merge_threshold:
                # ถัวเฉลี่ยราคาตามความสำคัญ แต่ใช้ค่า Weight สูงสุดเป็นหลัก (ป้องกันคะแนนเฟ้อจากการบวกซ้ำ)
                total_w = cluster['sum_w'] + item['weight']
                cluster['price'] = (cluster['price'] * cluster['sum_w'] + item['price'] * item['weight']) / total_w
                cluster['sum_w'] = total_w
                cluster['max_w'] = max(cluster['max_w'], item['weight'])
                if item['label'] not in cluster['labels']:
                    cluster['labels'].append(item['label'])
                placed = True
                break
        if not placed:
            clusters.append({
                'price': float(item['price']),
                'sum_w': item['weight'],
                'max_w': item['weight'],
                'labels': [item['label']]
            })
            
    results = []
    for c in clusters:
        # คิดคะแนนจากอินดิเคเตอร์ตัวที่แข็งแกร่งที่สุดในโซนนั้น + โบนัสการทับซ้อนตัวละ 6 คะแนน (สูงสุดไม่เกิน 3 ตัว)
        confluence_bonus = min(len(c['labels']) - 1, 2) * 6
        base_score = 42 + c['max_w'] + confluence_bonus
        
        if is_support:
            if is_uptrend:
                base_score += 8   # ขาขึ้นใหญ่ แนวรับมีความน่าเชื่อถือสูงขึ้น
            else:
                base_score -= 6   # ขาลงใหญ่ หักคะแนนแนวรับเพื่อสะท้อนความเสี่ยงหลุดแนวรับ
            if rsi_val <= 35: base_score += 6
            if rvol >= 1.2: base_score += 4
            if macd_turning_up: base_score += 4
        else:
            if not is_uptrend:
                base_score += 8   # ขาลงใหญ่ แนวต้านด้านบนจะแข็งแกร่งมาก
            else:
                base_score -= 4   # ขาขึ้นใหญ่ แนวต้านมีโอกาสถูกเบรกได้ง่ายกว่า
            if rsi_val >= 68: base_score += 6
            
        score = min(max(int(base_score), 45), 92)
        combined_label = " + ".join(c['labels'][:2])
        results.append({
            'price': float(c['price']),
            'label': combined_label,
            'setup_score': score
        })
        
    results.sort(key=lambda x: x['price'], reverse=is_support)
    
    # โซนสำรองด้วยกรอบความผันผวน ATR หากมีแนวรับ/แนวต้านธรรมชาติไม่ครบ 3 ระดับ
    step = max(atr_val * 1.2, current_price * 0.018)
    fallback_labels_sup = ["ATR Support (-1.2x)", "ATR Demand (-2.4x)", "ATR Tail Risk (-3.6x)"]
    fallback_labels_res = ["ATR Target (+1.2x)", "ATR Supply (+2.4x)", "ATR Extended (+3.6x)"]
    
    while len(results) < 3:
        idx = len(results)
        ref_price = results[-1]['price'] if results else current_price
        if is_support:
            next_price = max(ref_price - step, current_price * 0.4)
            lbl = fallback_labels_sup[min(idx, 2)]
        else:
            next_price = ref_price + step
            lbl = fallback_labels_res[min(idx, 2)]
        results.append({
            'price': float(next_price),
            'label': lbl,
            'setup_score': 52
        })
        
    return results[:3]

# 5. ฟังก์ชันวิเคราะห์หุ้นเชิงปริมาณ (Quantitative Analysis)
def get_pro_stock_data(symbol):
    try:
        symbol = symbol.upper().strip()
        df = fetch_clean_history(symbol, period="2y")
        
        if (df is None or len(df) < 20) and "." not in symbol:
            alt_symbol = f"{symbol}.BK"
            alt_df = fetch_clean_history(alt_symbol, period="2y")
            if alt_df is not None and len(alt_df) >= 20:
                symbol = alt_symbol
                df = alt_df
                
        if df is None or len(df) < 20:
            return None

        current_price = float(df['Close'].iloc[-1])
        prev_close = float(df['Close'].iloc[-2]) if len(df) >= 2 else current_price
        price_change = ((current_price - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0

        # คำนวณ Indicators มาตรฐานตรงตาม TradingView
        rsi_val = calculate_wilder_rsi(df['Close'], 14)
        atr_val = calculate_atr(df, 14)
        ema_50 = float(df['Close'].ewm(span=50, adjust=False).mean().iloc[-1])
        ema_200 = float(df['Close'].ewm(span=200, adjust=False).mean().iloc[-1])

        # Bollinger Bands (20, 2)
        win_bb = min(20, len(df))
        sma_20 = df['Close'].rolling(window=win_bb, min_periods=1).mean()
        std_20 = df['Close'].rolling(window=win_bb, min_periods=1).std().fillna(atr_val * 0.5)
        bb_lower = float((sma_20 - 2 * std_20).iloc[-1])
        bb_upper = float((sma_20 + 2 * std_20).iloc[-1])

        # MACD (12, 26, 9)
        ema_12 = df['Close'].ewm(span=12, adjust=False).mean()
        ema_26 = df['Close'].ewm(span=26, adjust=False).mean()
        macd_line = ema_12 - ema_26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = macd_line - signal_line
        macd_turning_up = bool(macd_hist.iloc[-1] > macd_hist.iloc[-2]) if len(macd_hist) >= 2 else False

        # Relative Volume (RVOL 20 วัน)
        vol_avg_20 = float(df['Volume'].tail(20).mean())
        curr_vol = float(df['Volume'].iloc[-1])
        rvol = (curr_vol / vol_avg_20) if vol_avg_20 > 0 else 1

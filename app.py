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

# ฟังก์ชันดึงข้อมูลหุ้นแบบทำความสะอาดข้อมูล (Data Cleaning) ป้องกันค่า NaN
def fetch_clean_history(symbol, period="1y"):
    symbol = symbol.upper().strip()
    df = pd.DataFrame()
    
    try:
        ticker_obj = yf.Ticker(symbol)
        df = ticker_obj.history(period=period, auto_adjust=True)
    except Exception as e:
        print(f"Ticker.history failed for {symbol}: {e}")
        
    if df is None or df.empty:
        try:
            df = yf.download(symbol, period=period, progress=False, auto_adjust=True)
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

# 2. สูตร ATR วัดกรอบความผันผวนจริง
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

# 3. สูตรหา Volume Profile POC (Point of Control - ต้นทุนสะสมหนาแน่น)
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

# 4. ระบบจัดกลุ่ม Confluence และคำนวณ Setup Score (คะแนนทางเทคนิค ไม่ใช่ Win Rate)
def build_confluence_zones(candidates, current_price, atr_val, is_support=True, rsi_val=50, rvol=1.0, macd_turning_up=False, is_uptrend=True):
    valid_candidates = [c for c in candidates if c['price'] > 0 and not np.isnan(c['price'])]
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
        # คำนวณ Setup Score ตามน้ำหนักการทับซ้อนและโมเมนตัม (คะแนนเต็ม 100)
        base_score = 40 + c['weight'] * 1.5 + (len(c['labels']) - 1) * 5
        if is_support:
            if is_uptrend: base_score += 6
            if rsi_val <= 38: base_score += 6
            if rvol >= 1.2: base_score += 4
            if macd_turning_up: base_score += 4
        else:
            if not is_uptrend: base_score += 6
            if rsi_val >= 68: base_score += 6
            
        score = min(max(int(base_score), 40), 95)
        combined_label = " + ".join(c['labels'][:2])
        results.append({
            'price': float(c['price']),
            'label': combined_label,
            'setup_score': score
        })
        
    results.sort(key=lambda x: x['price'], reverse=is_support)
    
    step = max(atr_val, current_price * 0.015)
    fallback_labels_sup = ["ATR Demand Zone", "Volatility Support", "Structural Bid"]
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
            'setup_score': 50 + (idx * 5)
        })
        
    return results[:3]

# 5. ฟังก์ชันวิเคราะห์หุ้นเชิงปริมาณ (Quantitative Analysis)
def get_pro_stock_data(symbol):
    try:
        symbol = symbol.upper().strip()
        df = fetch_clean_history(symbol, period="1y")
        
        if (df is None or len(df) < 15) and "." not in symbol:
            alt_symbol = f"{symbol}.BK"
            alt_df = fetch_clean_history(alt_symbol, period="1y")
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

        win_bb = min(20, len(df))
        sma_20 = df['Close'].rolling(window=win_bb, min_periods=1).mean()
        std_20 = df['Close'].rolling(window=win_bb, min_periods=1).std().fillna(atr_val * 0.5)
        bb_lower = float((sma_20 - 2 * std_20).iloc[-1])
        bb_upper = float((sma_20 + 2 * std_20).iloc[-1])

        ema_12 = df['Close'].ewm(span=12, adjust=False).mean()
        ema_26 = df['Close'].ewm(span=26, adjust=False).mean()
        macd_line = ema_12 - ema_26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = macd_line - signal_line
        macd_turning_up = bool(macd_hist.iloc[-1] > macd_hist.iloc[-2]) if len(macd_hist) >= 2 else False

        vol_avg_20 = float(df['Volume'].tail(20).mean())
        curr_vol = float(df['Volume'].iloc[-1])
        rvol = (curr_vol / vol_avg_20) if vol_avg_20 > 0 else 1.0

        poc_price = calculate_volume_poc(df)
        high_60d = float(df['High'].tail(60).max())
        low_60d = float(df['Low'].tail(60).min())
        fib_range = max(high_60d - low_60d, atr_val)
        fib_618 = high_60d - (0.618 * fib_range)
        fib_500 = high_60d - (0.500 * fib_range)
        fib_382 = high_60d - (0.382 * fib_range)

        swing_low_20d = float(df['Low'].tail(20).min())
        swing_high_20d = float(df['High'].tail(20).max())

        is_uptrend = current_price >= ema_200

        raw_supports = [
            {'price': min(swing_low_20d, current_price - 0.8 * atr_val), 'label': 'Swing Low 20D', 'weight': 10},
            {'price': min(low_60d, current_price - 1.5 * atr_val), 'label': 'Major Low 60D', 'weight': 14},
            {'price': min(bb_lower, current_price - 0.5 * atr_val), 'label': 'Bollinger Lower', 'weight': 8},
            {'price': current_price - 1.8 * atr_val, 'label': 'ATR Support', 'weight': 9},
        ]
        if ema_50 < current_price:
            raw_supports.append({'price': ema_50, 'label': 'EMA 50', 'weight': 11})
        if ema_200 < current_price:
            raw_supports.append({'price': ema_200, 'label': 'EMA 200 🛡️', 'weight': 18})
        if poc_price < current_price:
            raw_supports.append({'price': poc_price, 'label': 'Volume POC 🐳', 'weight': 16})
        if fib_618 < current_price:
            raw_supports.append({'price': fib_618, 'label': 'Fibo 61.8%', 'weight': 13})

        raw_resistances = [
            {'price': max(swing_high_20d, current_price + 0.8 * atr_val), 'label': 'Swing High 20D', 'weight': 10},
            {'price': max(high_60d, current_price + 1.5 * atr_val), 'label': 'Major High 60D', 'weight': 14},
            {'price': max(bb_upper, current_price + 0.5 * atr_val), 'label': 'Bollinger Upper', 'weight': 8},
        ]
        if ema_50 > current_price:
            raw_resistances.append({'price': ema_50, 'label': 'EMA 50', 'weight': 11})
        if ema_200 > current_price:
            raw_resistances.append({'price': ema_200, 'label': 'EMA 200 🚧', 'weight': 18})
        if poc_price > current_price:
            raw_resistances.append({'price': poc_price, 'label': 'Volume POC 🐳', 'weight': 16})
        if fib_382 > current_price:
            raw_resistances.append({'price': fib_382, 'label': 'Fibo 38.2%', 'weight': 12})

        sup_zones = build_confluence_zones(raw_supports, current_price, atr_val, True, rsi_val, rvol, macd_turning_up, is_uptrend)
        res_zones = build_confluence_zones(raw_resistances, current_price, atr_val, False, rsi_val, rvol, macd_turning_up, is_uptrend)

        best_entry = sup_zones[0]['price']
        stop_loss = best_entry - (1.2 * atr_val)
        take_profit = res_zones[0]['price']
        
        risk = max(best_entry - stop_loss, 0.01)
        reward = max(take_profit - best_entry, 0.01)
        rr_ratio = reward / risk

        max_score = int(max(s['setup_score'] for s in sup_zones[:3]))
        
        if current_price > ema_200 and current_price > ema_50:
            trend_str = "ขาขึ้นแข็งแกร่ง (เหนือ EMA50 & 200)"
        elif current_price > ema_200:
            trend_str = "ย่อตัวสะสมในขาขึ้นใหญ่ (เหนือ EMA200)"
        elif current_price > ema_50:
            trend_str = "ฟื้นตัวระยะสั้น (ลุ้นเบรก EMA200)"
        else:
            trend_str = "ขาลงระยะยาว (เน้นตั้งรับโซนลึก)"

        mom_tags = []
        if rsi_val <= 35:
            mom_tags.append("RSI Oversold 🟢")
        elif rsi_val >= 70:
            mom_tags.append("RSI Overbought 🔴")
        else:
            mom_tags.append("RSI ปกติ")
            
        if macd_turning_up:
            mom_tags.append("MACD ยกตัว ↗️")
        if rvol >= 1.25:
            mom_tags.append(f"Vol สูง ({rvol:.1f}x) 🐳")

        currency = "THB" if ".BK" in symbol else "USD"
        change_sign = "+" if price_change >= 0 else ""
        change_color = "#00D26A" if price_change >= 0 else "#FF4D4D"

        return {
            "symbol": symbol,
            "price_str": f"{current_price:,.2f} {currency}",
            "change_str": f"{change_sign}{price_change:.2f}%",
            "change_color": change_color,
            "setup_score": f"{max_score}/100",
            "rsi_str": f"{rsi_val:.1f} ({' | '.join(mom_tags)})",
            "trend": trend_str,
            "ema_poc_str": f"EMA50: {ema_50:,.2f} | EMA200: {ema_200:,.2f} | POC: {poc_price:,.2f}",
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
                f"• จุดรอรับอ้างอิง: {best_entry:,.2f} (Score {sup_zones[0]['setup_score']})\n"
                f"• เป้าทำกำไร (TP): {take_profit:,.2f} | ตัดขาดทุน (SL): {stop_loss:,.2f}\n"
                f"• สัดส่วนความเสี่ยงต่อผลตอบแทน (R:R): 1 : {rr_ratio:.1f}"
            )
        }
    except Exception as e:
        print(f"Error analyzing {symbol}: {e}")
        return None

# 6. ฟังก์ชันสแกนหุ้นแบบกลุ่ม คัดกรอง Setup Score สูง
def batch_scan_stocks(symbols_list):
    try:
        data = yf.download(symbols_list, period="1y", group_by="ticker", progress=False, threads=True, auto_adjust=True)
        results = []
        
        for sym in symbols_list:
            try:
                if len(symbols_list) > 1:
                    if sym not in data.columns.get_level_values(0):
                        continue
                    df = data[sym].copy()
                else:
                    df = data.copy()
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                        
                df = df.dropna(subset=['High', 'Low', 'Close'])
                if df.empty or len(df) < 20:
                    continue
                if 'Volume' not in df.columns:
                    df['Volume'] = 0
                else:
                    df['Volume'] = df['Volume'].fillna(0)
                
                close_p = float(df['Close'].iloc[-1])
                prev_p = float(df['Close'].iloc[-2]) if len(df) >= 2 else close_p
                chg = ((close_p - prev_p) / prev_p) * 100 if prev_p > 0 else 0.0
                rsi = calculate_wilder_rsi(df['Close'], 14)
                ema_200 = float(df['Close'].ewm(span=200, adjust=False).mean().iloc[-1])
                poc_p = calculate_volume_poc(df)
                low_20d = float(df['Low'].tail(20).min())
                
                candidates = [(low_20d, "Swing Low", 60)]
                if ema_200 <= close_p:
                    candidates.append((ema_200, "EMA 200 🛡️", 75))
                if poc_p <= close_p:
                    candidates.append((poc_p, "Volume POC 🐳", 72))
                
                candidates.sort(key=lambda x: x[0], reverse=True)
                best_sup, sup_type, base_score = candidates[0]
                
                if abs(ema_200 - poc_p) / max(close_p, 0.01) <= 0.02 and ema_200 <= close_p:
                    sup_type = "EMA200 + POC 🛡️🐳"
                    base_score = 85
                if rsi <= 38:
                    base_score += 5
                    
                score = min(base_score, 92)
                dist_pct = ((close_p - best_sup) / max(best_sup, 0.01)) * 100
                
                results.append({
                    "symbol": sym,
                    "price": close_p,
                    "change": chg,
                    "rsi": rsi,
                    "support": best_sup,
                    "sup_type": sup_type,
                    "ema200": ema_200,
                    "poc": poc_p,
                    "setup_score": score,
                    "dist_pct": dist_pct
                })
            except Exception as inner_e:
                print(f"Scan skip {sym}: {inner_e}")
                continue
        return results
    except Exception as e:
        print(f"Batch scan error: {e}")
        return []

# 7. สร้างการ์ด Flex Message (แสดง Setup Score และปฏิเสธการันตี Win Rate)
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
                    "text": f"📈 โมเมนตัม: {data['rsi_str']}",
                    "size": "xs",
                    "color": "#222222",
                    "weight": "bold",
                    "wrap": True
                },
                {
                    "type": "text",
                    "text": f"⚡ โครงสร้าง: {data['trend']}",
                    "size": "xs",
                    "color": "#555555",
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
                {
                    "type": "separator",
                    "margin": "md"
                },
                {
                    "type": "text",
                    "text": "🟢 แนวรับ Confluence (Technical Score):",
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
                    "text": "🔴 แนวต้านสำคัญ (Target Zones):",
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
                {
                    "type": "separator",
                    "margin": "md"
                },
                {
                    "type": "text",
                    "text": "💡 แผนบริหารความเสี่ยง (Risk Management Plan):",
                    "size": "xs",
                    "weight": "bold",
                    "color": "#1A1A2E",
                    "margin": "md"
                },
                {
                    "type": "text",
                    "text": data['setup_str'],
                    "size": "xxs",
                    "color": "#444444",
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
                        "label": "🎯 สแกน Setup สูง",
                        "text": "/SCAN_SUPPORT"
                    }
                }
            ]
        }
    }
    return FlexMessage(
        alt_text=f"แผนเทรด {data['symbol']} (Setup Score {data['setup_score']})",
        contents=FlexContainer.from_dict(bubble_dict)
    )

@app.route("/", methods=['GET'])
def health_check():
    return "Stock Sage AI (Real-World Risk Planner Edition) is Awake!", 200

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
            "📖 **คู่มือ Stock Sage AI (Risk Planner)**\n\n"
            "ระบบประเมินโครงสร้างราคาและจุดตั้งรับทางเทคนิคเพื่อช่วยวางแผนบริหารความเสี่ยง มิใช่ระบบการันตีผลกำไร (Past performance does not guarantee future results)\n\n"
            "• พิมพ์ชื่อหุ้น (เช่น `NVDA`, `JEPQ`, `PTT.BK`) เพื่อดูการ์ดแผนเทรด + จุด TP/SL\n"
            "• `+[ชื่อหุ้น]` เพิ่มเข้า Watchlist\n"
            "• `-[ชื่อหุ้น]` ลบออกจาก Watchlist\n"
            "• `/LIST` ดูรายชื่อหุ้นเฝ้าระวัง\n"
            "• `/SUMMARY` สรุปสถานะพอร์ตรายตัว\n"
            "• `/SCAN_SUPPORT` สแกนหาหุ้นที่เข้าใกล้โซน Setup Score สูงสุด"
        )
        messages_to_reply.append(TextMessage(text=help_text))

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
            scan_results = batch_scan_stocks(watchlist)
            if not scan_results:
                messages_to_reply.append(TextMessage(text="⚠️ ไม่สามารถดึงข้อมูลได้ในขณะนี้"))
            else:
                summary_lines = [f"📊 **สรุปพอร์ต & คะแนน Setup ({len(scan_results)} ตัว):**"]
                for item in scan_results:
                    sign = "+" if item['change'] >= 0 else ""
                    icon = "🟢" if item['rsi'] <= 38 else ("🔴" if item['rsi'] >= 70 else "⚪")
                    summary_lines.append(
                        f"\n{icon} **{item['symbol']}** : {item['price']:,.2f} ({sign}{item['change']:.2f}%)\n"
                        f"   ├ RSI: {item['rsi']:.1f} | POC: {item['poc']:,.2f}\n"
                        f"   └ แนวรับ: **{item['support']:,.2f}** [{item['sup_type']} | Score {item['setup_score']}/100]"
                    )
                messages_to_reply.append(TextMessage(text="\n".join(summary_lines)))

    elif command == "/SCAN_SUPPORT":
        scan_results = batch_scan_stocks(GLOBAL_SCAN_POOL)
        high_score_setups = [
            r for r in scan_results 
            if (r['dist_pct'] <= 4.5 or r['rsi'] <= 42) and r['setup_score'] >= 68
        ]
        high_score_setups.sort(key=lambda x: (-x['setup_score'], x['dist_pct']))

        if high_score_setups:
            lines = ["🎯 **หุ้นระดับโลกที่เข้าใกล้โซนรับ (Setup Score 68+):**"]
            for item in high_score_setups[:8]:
                sign = "+" if item['change'] >= 0 else ""
                lines.append(
                    f"\n🏆 **{item['symbol']}** (ราคา {item['price']:,.2f} | {sign}{item['change']:.2f}%)\n"
                    f"   • โซนรับ: **{item['support']:,.2f}** [{item['sup_type']}]\n"
                    f"   • คะแนนเซ็ตอัพ: **{item['setup_score']}/100** (ห่างแนวรับ {item['dist_pct']:.1f}% | RSI {item['rsi']:.1f})"
                )
            lines.append("\n⚠️ *การลงทุนมีความเสี่ยง ผลการดำเนินงานในอดีตไม่สามารถรับประกันผลในอนาคต (SEC/FINRA)*")
            messages_to_reply.append(TextMessage(text="\n".join(lines)))
        else:
            messages_to_reply.append(TextMessage(text="🛡️ ยังไม่มีหุ้นในตะกร้าหลักย่อลงมาในโซนรับที่มี Setup Score สูงในช่วงเวลานี้"))

    elif user_message.startswith("+"):
        stock_to_add = user_message[1:].strip().upper()
        if stock_to_add:
            check = get_pro_stock_data(stock_to_add)
            if check:
                actual_sym = check['symbol']
                if actual_sym not in watchlist:
                    watchlist.append(actual_sym)
                    messages_to_reply.append(TextMessage(text=f"✅ เพิ่ม **{actual_sym}** ({check['price_str']}) เข้า Watchlist แล้ว"))
                else:
                    messages_to_reply.append(TextMessage(text=f"⚠️ หุ้น **{actual_sym}** มีอยู่ใน Watchlist แล้ว"))
            else:
                messages_to_reply.append(TextMessage(text=f"⚠️ ไม่พบข้อมูลหุ้น '{stock_to_add}' ในตลาด"))

    elif user_message.startswith("-"):
        stock_to_remove = user_message[1:].strip().upper()
        if stock_to_remove in watchlist:
            watchlist.remove(stock_to_remove)
            messages_to_reply.append(TextMessage(text=f"🗑️ ลบ **{stock_to_remove}** ออกจาก Watchlist แล้ว"))
        elif f"{stock_to_remove}.BK" in watchlist:
            watchlist.remove(f"{stock_to_remove}.BK")
            messages_to_reply.append(TextMessage(text=f"🗑️ ลบ **{stock_to_remove}.BK** ออกจาก Watchlist แล้ว"))
        else:
            messages_to_reply.append(TextMessage(text=f"⚠️ ไม่พบ **{stock_to_remove}** ใน Watchlist"))

    else:
        stock_name = user_message.upper()
        data = get_pro_stock_data(stock_name)
        if data:
            messages_to_reply.append(build_flex_card(data))
        else:
            messages_to_reply.append(TextMessage(text=f"⚠️ ไม่พบข้อมูลหุ้น '{stock_name}'\n(หุ้นสหรัฐฯ พิมพ์ชื่อย่อได้เลย เช่น NVDA, หุ้นไทยพิมพ์ PTT หรือ PTT.BK)"))

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

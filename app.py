import os
import time
import threading
import sqlite3
import hashlib
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timezone

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
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET', '')
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN or 'MISSING_TOKEN')
handler = WebhookHandler(LINE_CHANNEL_SECRET or 'MISSING_SECRET')
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL', '')
GOOGLE_SHEET_WEBHOOK_URL = os.getenv('GOOGLE_SHEET_WEBHOOK_URL', '')
DB_PATH = os.getenv('AXION_DB_PATH', 'axion_smart_analyzer.db')
MODEL_VERSION = '1.2.0-backtest-both-10y'

# Watchlist เริ่มต้นถาวรทุกครั้งที่อัปเดตหรือรีสตาร์ทระบบ
DEFAULT_WATCHLIST = ["SCHG", "JEPQ", "QQQI", "RKLB"]
user_watchlists = {}
discord_alert_cooldown = {}
last_auto_scan_time = 0

GLOBAL_SCAN_POOL = [
    "SCHG", "JEPQ", "QQQI", "RKLB",
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
            QuickReplyItem(action=MessageAction(label=f"🧪 เทส {short_sym}", text=f"/BACKTEST {symbol}")),
            QuickReplyItem(action=MessageAction(label=f"➕ {short_sym}", text=f"+{symbol}")),
            QuickReplyItem(action=MessageAction(label="📊 พอร์ต", text="/SUMMARY")),
            QuickReplyItem(action=MessageAction(label="📋 ลิสต์", text="/LIST")),
        ]
    else:
        items = [
            QuickReplyItem(action=MessageAction(label="📊 พอร์ต", text="/SUMMARY")),
            QuickReplyItem(action=MessageAction(label="🎯 สแกน", text="/SCAN_SUPPORT")),
            QuickReplyItem(action=MessageAction(label="📋 ลิสต์", text="/LIST")),
            QuickReplyItem(action=MessageAction(label="🧪 เทส 10Y", text="/BACKTEST QCOM")),
        ]
    return QuickReply(items=items)

# ==========================================
# ส่วนที่ 0: Local Audit Database (SQLite)
# ==========================================
def init_database():
    with sqlite3.connect(DB_PATH, timeout=15) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS forecast_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            forecast_key TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            market TEXT NOT NULL,
            forecast_time_utc TEXT NOT NULL,
            data_time TEXT,
            horizon TEXT NOT NULL,
            direction TEXT NOT NULL,
            trend_score INTEGER NOT NULL,
            model_version TEXT NOT NULL,
            data_source TEXT NOT NULL,
            fair_value REAL,
            market_price REAL NOT NULL,
            notes TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_forecast_symbol_time ON forecast_history(symbol, forecast_time_utc)")
        conn.commit()

def save_forecast_record(symbol, market_price, mtf, fair_value=None, data_time=None):
    try:
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        market = 'SET' if symbol.upper().endswith('.BK') else 'US'
        for horizon, item in mtf.items():
            key_raw = f"{symbol}|{data_time}|{horizon}|{MODEL_VERSION}"
            key = hashlib.sha256(key_raw.encode('utf-8')).hexdigest()
            with sqlite3.connect(DB_PATH, timeout=15) as conn:
                conn.execute("""INSERT OR IGNORE INTO forecast_history
                    (forecast_key,symbol,market,forecast_time_utc,data_time,horizon,direction,trend_score,model_version,data_source,fair_value,market_price,notes)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key, symbol, market, now, str(data_time or ''), horizon, item.get('dir', ''),
                     int(item.get('score', 0)), MODEL_VERSION, 'Yahoo Finance via yfinance',
                     float(fair_value) if fair_value and np.isfinite(fair_value) else None, float(market_price),
                     'Rule-based trend classification; audited via 10Y walk-forward'))
                conn.commit()
    except Exception as exc:
        print(f"Forecast audit DB error: {exc}")

def get_forecast_audit_count():
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            return int(conn.execute('SELECT COUNT(*) FROM forecast_history').fetchone()[0])
    except Exception:
        return 0

try:
    init_database()
except Exception as db_exc:
    print(f"Database initialization warning: {db_exc}")

# ==========================================
# ส่วนที่ 1: Google Sheets Logger
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
    return (
        "📊 สถานะ Google Sheets Audit\n"
        "• ระบบยังคงบันทึกแผน Entry / TP1-TP3 / SL ลง Google Sheets อัตโนมัติ\n"
        "• สำหรับการวัดความแม่นยำย้อนหลัง 10 ปี (Forecast + Trading) ที่ปลอด Look-ahead Bias\n"
        "  แนะนำให้ใช้คำสั่ง: /BACKTEST [ชื่อหุ้น] เช่น /BACKTEST QCOM หรือ /BACKTEST RKLB"
    )

# ==========================================
# ส่วนที่ 2: Data, Indicators & Multi-Timeframe Engine
# ==========================================
def fetch_clean_history(symbol, period="1y", interval="1d"):
    symbol = str(symbol).upper().strip()
    if not symbol:
        return pd.DataFrame()
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False,
                         auto_adjust=True, actions=False, threads=False)
    except Exception as exc:
        print(f"Yahoo Finance download failed for {symbol}: {exc}")
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    required = ['Open', 'High', 'Low', 'Close']
    if any(c not in df.columns for c in required):
        return pd.DataFrame()
    df = df.copy()
    df.index = pd.to_datetime(df.index, errors='coerce')
    df = df[~df.index.isna()].sort_index()
    df = df[~df.index.duplicated(keep='last')]
    for col in required + ['Volume']:
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=required)
    df = df[(df['High'] >= df['Low']) & (df['High'] >= df['Open']) &
            (df['High'] >= df['Close']) & (df['Low'] <= df['Open']) &
            (df['Low'] <= df['Close']) & (df['Close'] > 0)]
    df['Volume'] = df['Volume'].fillna(0).clip(lower=0)
    return df

def fetch_fundamentals_and_meta(symbol, current_price, ema_200):
    is_set = str(symbol).upper().endswith('.BK')
    meta = {
        "company_name": symbol, "exchange": "SET" if is_set else "US",
        "currency": "THB" if is_set else "USD",
        "currency_sym": "฿" if is_set else "$",
        "fair_value": None, "fv_note": "Insufficient fundamentals",
        "after_hours": None, "analyst_target": None, "graham_value": None,
        "fair_value_quality": "LOW / NOT ESTABLISHED"
    }
    try:
        info = yf.Ticker(symbol).info or {}
        if info.get('shortName'):
            meta['company_name'] = str(info['shortName'])[:32]
        if info.get('exchange'):
            ex = str(info['exchange'])
            meta['exchange'] = {"NMS": "NASDAQ", "NYQ": "NYSE", "PCX": "NYSE Arca", "BKK": "SET"}.get(ex, ex)
        if info.get('currency'):
            meta['currency'] = str(info['currency'])
            meta['currency_sym'] = '฿' if meta['currency'] == 'THB' else '$'
        post_p = info.get('postMarketPrice')
        if post_p is not None and np.isfinite(float(post_p)) and float(post_p) > 0:
            meta['after_hours'] = float(post_p)

        target = info.get('targetMeanPrice')
        if target is not None and np.isfinite(float(target)) and float(target) > 0:
            meta['analyst_target'] = float(target)

        eps = info.get('trailingEps')
        book = info.get('bookValue')
        if eps is not None and book is not None:
            eps, book = float(eps), float(book)
            if eps > 0 and book > 0:
                gv = float(np.sqrt(22.5 * eps * book))
                if np.isfinite(gv) and current_price > 0 and 0.3 * current_price <= gv <= 3.0 * current_price:
                    meta['graham_value'] = gv

        nav = info.get('navPrice')
        quote_type = str(info.get('quoteType', '')).upper()
        if quote_type == 'ETF' and nav is not None and np.isfinite(float(nav)) and float(nav) > 0:
            meta['fair_value'] = float(nav)
            meta['fv_note'] = 'ETF NAV ref.'
            meta['fair_value_quality'] = 'REFERENCE ONLY'
        elif meta['analyst_target']:
            meta['fair_value'] = meta['analyst_target']
            meta['fv_note'] = 'Analyst Target'
            meta['fair_value_quality'] = 'CONSENSUS REF'
        else:
            meta['fair_value'] = None
            meta['fv_note'] = 'Insufficient data'
    except Exception as exc:
        print(f"Fundamentals fetch failed for {symbol}: {exc}")
    return meta

def calculate_wilder_rsi_series(close_series, period=14):
    delta = close_series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0).clip(0.0, 100.0)

def calculate_wilder_rsi(close_series, period=14):
    if len(close_series) < 3:
        return 50.0
    p = min(period, max(len(close_series) - 1, 2))
    return float(calculate_wilder_rsi_series(close_series, p).iloc[-1])

def calculate_atr_series(df, period=14):
    high = pd.to_numeric(df['High'], errors='coerce')
    low = pd.to_numeric(df['Low'], errors='coerce')
    close = pd.to_numeric(df['Close'], errors='coerce')
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

def calculate_atr(df, period=14):
    if df is None or len(df) < max(2, period + 1):
        return float('nan')
    value = float(calculate_atr_series(df, period).iloc[-1])
    return value if np.isfinite(value) and value > 0 else float('nan')

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
    bin_indices = np.clip(np.digitize(typ_price.values, bin_edges) - 1, 0, bins - 1)
    vol_by_bin = np.zeros(bins)
    for idx, vol in zip(bin_indices, recent_df['Volume'].values):
        if not np.isnan(vol) and vol > 0:
            vol_by_bin[int(idx)] += float(vol)
    if np.sum(vol_by_bin) <= 0:
        return float(typ_price.mean())
    max_bin = int(np.argmax(vol_by_bin))
    return float((bin_edges[max_bin] + bin_edges[max_bin + 1]) / 2.0)

def aggregate_ohlc(df, mode="weekly", exclude_current_partial=True):
    if df is None or df.empty:
        return pd.DataFrame()
    temp = df.copy().sort_index()
    idx = pd.DatetimeIndex(temp.index)
    if mode == 'weekly':
        grp = idx.strftime('%G-%V')
        current_grp = datetime.now().strftime('%G-%V')
    else:
        grp = idx.strftime('%Y-%m')
        current_grp = datetime.now().strftime('%Y-%m')
    temp['_grp'] = grp
    agg = temp.groupby('_grp', sort=True).agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'})
    if exclude_current_partial and len(agg) and str(agg.index[-1]) == current_grp:
        agg = agg.iloc[:-1]
    return agg

def score_timeframe_trend(df_tf):
    if df_tf is None or len(df_tf) < 30:
        return {"dir": "ข้อมูลไม่พอ", "color": "#64748B", "conf": "N/A", "score": 0, "trend_score": 0}
    closes = pd.to_numeric(df_tf['Close'], errors='coerce').dropna()
    if len(closes) < 30:
        return {"dir": "ข้อมูลไม่พอ", "color": "#64748B", "conf": "N/A", "score": 0, "trend_score": 0}
    curr = float(closes.iloc[-1])
    e20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
    e50 = float(closes.ewm(span=50, adjust=False).mean().iloc[-1])
    e200 = float(closes.ewm(span=200, adjust=False).mean().iloc[-1]) if len(closes) >= 200 else None
    rsi = calculate_wilder_rsi(closes, 14)
    pts = 0
    pts += 1 if curr > e20 else -1
    pts += 1 if e20 > e50 else -1
    if e200 is not None:
        pts += 1 if curr > e200 else -1
    pts += 1 if rsi >= 55 else (-1 if rsi <= 45 else 0)
    if len(closes) >= 6:
        pts += 1 if curr > float(closes.iloc[-6]) else -1
    score = int(round((pts + 5) / 10 * 100))
    if pts >= 3:
        direction, color, s = 'แนวโน้มขาขึ้น', '#059669', 2
    elif pts >= 1:
        direction, color, s = 'เอนเอียงขาขึ้น', '#10B981', 1
    elif pts <= -3:
        direction, color, s = 'แนวโน้มขาลง', '#DC2626', -2
    elif pts <= -1:
        direction, color, s = 'เอนเอียงขาลง', '#EF4444', -1
    else:
        direction, color, s = 'Sideways / ไม่ชัดเจน', '#D97706', 0
    return {"dir": direction, "color": color, "conf": f"{score}/100", "score": s, "trend_score": score}

def compute_mtf_forecast(df_1d, current_price, ema_200, fair_value=None):
    daily_res = score_timeframe_trend(df_1d.tail(260))
    weekly_df = aggregate_ohlc(df_1d, 'weekly', exclude_current_partial=True)
    monthly_df = aggregate_ohlc(df_1d, 'monthly', exclude_current_partial=True)
    weekly_res = score_timeframe_trend(weekly_df)
    monthly_res = score_timeframe_trend(monthly_df)
    yearly_res = score_timeframe_trend(df_1d.tail(756)) if len(df_1d) >= 252 else {
        "dir": "ข้อมูลไม่พอ", "color": "#64748B", "conf": "N/A", "score": 0, "trend_score": 0
    }
    return {"Daily": daily_res, "Weekly": weekly_res, "Monthly": monthly_res, "Yearly": yearly_res}

# ==========================================
# ส่วนที่ 2.5: HISTORICAL WALK-FORWARD BACKTEST ENGINE (BOTH: FORECAST + TRADING, 10Y)
# ==========================================
def run_axion_backtest(symbol, period="10y", fee_bps=10.0):
    """
    ทดสอบย้อนหลัง 10 ปี (ปลอด Look-ahead Bias) ครอบคลุมทั้ง 2 ขอบเขต:
    1) Forecast Accuracy: Daily (1d), Weekly (5d), Monthly (21d), Yearly (252d) + Balanced Accuracy
    2) Trading Simulation: เข้าออเดอร์เมื่อเทรนด์บวก + ย่อแตะโซนรับ EMA20/ATR, แบ่งเป้า TP1/TP2/TP3, SL=1.35*ATR
    """
    symbol = symbol.upper().strip()
    df = fetch_clean_history(symbol, period=period, interval="1d")
    if (df is None or len(df) < 80) and "." not in symbol:
        alt_sym = f"{symbol}.BK"
        alt_df = fetch_clean_history(alt_sym, period=period, interval="1d")
        if alt_df is not None and len(alt_df) >= 80:
            symbol = alt_sym
            df = alt_df
    if df is None or len(df) < 80:
        return None

    close = df['Close']
    high = df['High']
    low = df['Low']

    # คำนวณ Features แบบ Causal (ใช้เฉพาะข้อมูลถึงวัน t เท่านั้น ห้ามใช้ข้อมูลอนาคต)
    e20 = close.ewm(span=20, adjust=False).mean()
    e50 = close.ewm(span=50, adjust=False).mean()
    e200 = close.ewm(span=200, adjust=False).mean()
    rsi = calculate_wilder_rsi_series(close, 14)
    atr = calculate_atr_series(df, 14)

    pts = (
        np.where(close > e20, 1, -1)
        + np.where(e20 > e50, 1, -1)
        + np.where(close > e200, 1, -1)
        + np.where(rsi >= 55, 1, np.where(rsi <= 45, -1, 0))
        + np.where(close > close.shift(5), 1, -1)
    )
    pred_up = pd.Series(pts >= 1, index=df.index)

    # 1. Forecast Walk-Forward Evaluation (ตัดช่วง Warmup 200 วันแรกถ้าข้อมูลยาวพอ)
    warmup = 200 if len(df) > 300 else 50
    horizons = {"Daily": 1, "Weekly": 5, "Monthly": 21, "Yearly": 252}
    forecast_metrics = {}
    balanced_accs = []
    total_oos = 0

    for h_name, h_days in horizons.items():
        if len(df) <= warmup + h_days + 10:
            forecast_metrics[h_name] = {"acc": "N/A", "samples": 0}
            continue
        future_ret = close.shift(-h_days) / close - 1.0
        valid = future_ret.iloc[warmup:-h_days].dropna()
        p_slice = pred_up.loc[valid.index]
        actual_up = valid > 0

        n_samples = int(len(valid))
        total_oos += n_samples
        acc = float((p_slice == actual_up).mean() * 100.0) if n_samples > 0 else 0.0

        # Balanced Accuracy = (TPR + TNR) / 2
        pos_mask = actual_up
        neg_mask = ~actual_up
        tpr = float((p_slice[pos_mask] == True).mean()) if pos_mask.sum() > 0 else 0.5
        tnr = float((p_slice[neg_mask] == False).mean()) if neg_mask.sum() > 0 else 0.5
        b_acc = ((tpr + tnr) / 2.0) * 100.0
        balanced_accs.append(b_acc)

        forecast_metrics[h_name] = {"acc": f"{acc:.1f}%", "samples": n_samples}

    avg_bal_acc = float(np.mean(balanced_accs)) if balanced_accs else 0.0

    # 2. Trading Simulation (Walk-Forward Bar-by-Bar, 1 Position at a time)
    closes_v = close.values
    highs_v = high.values
    lows_v = low.values
    e20_v = e20.values
    atr_v = atr.values
    pts_v = pts

    in_trade = False
    entry_p = sl_p = tp1_p = tp2_p = tp3_p = 0.0
    tp1_hit = tp2_hit = False
    trade_returns = []
    tp1_cnt = tp2_cnt = tp3_cnt = sl_cnt = 0
    equity = 1.0
    peak_eq = 1.0
    max_dd = 0.0
    fee_rate = fee_bps / 10000.0

    for i in range(warmup, len(df) - 1):
        if not np.isfinite(atr_v[i]) or atr_v[i] <= 0:
            continue

        if not in_trade:
            # เงื่อนไขเข้า: เทรนด์บวก (pts >= 2) และราคาปิดย่อใกล้ EMA20 (ไม่ไล่ราคาเกิน 1.5%)
            if pts_v[i] >= 2 and lows_v[i] <= e20_v[i] * 1.015 and closes_v[i] >= e20_v[i] * 0.985:
                in_trade = True
                entry_p = closes_v[i]
                risk = max(1.35 * atr_v[i], entry_p * 0.015)
                sl_p = entry_p - risk
                tp1_p = entry_p + 1.5 * risk
                tp2_p = entry_p + 2.5 * risk
                tp3_p = entry_p + 4.0 * risk
                tp1_hit = False
                tp2_hit = False
        else:
            # ตรวจผลในแท่งถัดไปแบบ Conservative (เช็ก SL ก่อน TP หากเกิดในแท่งเดียวกัน)
            b_low = lows_v[i]
            b_high = highs_v[i]
            realized_ret = None

            if b_low <= sl_p:
                if tp2_hit:
                    # เก็บ TP1 (1/3) + TP2 (1/3) + ไม้ 3 โดน SL (1/3)
                    r1 = (tp1_p - entry_p) / entry_p
                    r2 = (tp2_p - entry_p) / entry_p
                    r3 = (sl_p - entry_p) / entry_p
                    realized_ret = (r1 + r2 + r3) / 3.0 - fee_rate
                elif tp1_hit:
                    r1 = (tp1_p - entry_p) / entry_p
                    r23 = (sl_p - entry_p) / entry_p
                    realized_ret = (r1 + 2.0 * r23) / 3.0 - fee_rate
                else:
                    realized_ret = (sl_p - entry_p) / entry_p - fee_rate
                    sl_cnt += 1
                in_trade = False
            else:
                if not tp1_hit and b_high >= tp1_p:
                    tp1_hit = True
                    tp1_cnt += 1
                if not tp2_hit and b_high >= tp2_p:
                    tp2_hit = True
                    tp2_cnt += 1
                if b_high >= tp3_p:
                    tp3_cnt += 1
                    r1 = (tp1_p - entry_p) / entry_p
                    r2 = (tp2_p - entry_p) / entry_p
                    r3 = (tp3_p - entry_p) / entry_p
                    realized_ret = (r1 + r2 + r3) / 3.0 - fee_rate
                    in_trade = False

            if realized_ret is not None:
                trade_returns.append(realized_ret)
                equity *= (1.0 + realized_ret)
                if equity > peak_eq:
                    peak_eq = equity
                dd = (peak_eq - equity) / peak_eq * 100.0
                if dd > max_dd:
                    max_dd = dd

    n_trades = len(trade_returns)
    wins = [r for r in trade_returns if r > 0]
    losses = [r for r in trade_returns if r <= 0]
    win_rate = (len(wins) / n_trades * 100.0) if n_trades > 0 else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.9 if gross_profit > 0 else 0.0)
    net_ret_pct = (equity - 1.0) * 100.0

    start_date = df.index[0].strftime("%Y-%m")
    end_date = df.index[-1].strftime("%Y-%m")
    years_actual = max((df.index[-1] - df.index[0]).days / 365.25, 0.1)

    return {
        "symbol": symbol,
        "period_str": f"{start_date} ถึง {end_date} ({years_actual:.1f}Y)",
        "bars": len(df),
        "forecast": forecast_metrics,
        "balanced_acc": f"{avg_bal_acc:.1f}%",
        "total_oos": total_oos,
        "trades": n_trades,
        "win_rate": f"{win_rate:.1f}%",
        "profit_factor": f"{profit_factor:.2f}",
        "net_return": f"{'+' if net_ret_pct >= 0 else ''}{net_ret_pct:.1f}%",
        "net_ret_color": "#059669" if net_ret_pct >= 0 else "#DC2626",
        "max_dd": f"-{max_dd:.1f}%",
        "tp_sl_detail": f"TP1: {tp1_cnt} | TP2: {tp2_cnt} | TP3: {tp3_cnt} | SL: {sl_cnt}"
    }

def build_backtest_flex_card(bt):
    def row_item(label, val, val_color="#0F172A"):
        return {
            "type": "box",
            "layout": "horizontal",
            "paddingTop": "5px",
            "paddingBottom": "5px",
            "contents": [
                {"type": "text", "text": label, "size": "xs", "color": "#475569", "flex": 6},
                {"type": "text", "text": str(val), "size": "xs", "weight": "bold", "color": val_color, "align": "end", "flex": 4}
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
                {
                    "type": "box",
                    "layout": "horizontal",
                    "alignItems": "center",
                    "contents": [
                        {"type": "text", "text": "AXION BACKTEST AUDITOR", "weight": "bold", "size": "sm", "color": "#0F172A", "flex": 7},
                        {
                            "type": "box",
                            "layout": "vertical",
                            "backgroundColor": "#DBEAFE",
                            "cornerRadius": "99px",
                            "paddingTop": "3px",
                            "paddingBottom": "3px",
                            "paddingStart": "8px",
                            "paddingEnd": "8px",
                            "flex": 0,
                            "contents": [{"type": "text", "text": "10Y BOTH", "size": "xxs", "weight": "bold", "color": "#1E40AF"}]
                        }
                    ]
                },
                {"type": "text", "text": f"{bt['symbol']}", "size": "xxl", "weight": "bold", "color": "#0F172A", "margin": "sm"},
                {"type": "text", "text": f"ช่วงข้อมูลจริง: {bt['period_str']} ({bt['bars']} แท่ง)", "size": "xxs", "color": "#64748B", "margin": "xs"},
                {"type": "separator", "margin": "md", "color": "#E2E8F0"},

                {"type": "text", "text": "1. Forecast Direction Accuracy (Walk-Forward)", "size": "xs", "weight": "bold", "color": "#0F172A", "margin": "md"},
                row_item("Daily (1 วัน)", bt["forecast"]["Daily"]["acc"]),
                row_item("Weekly (5 วัน)", bt["forecast"]["Weekly"]["acc"]),
                row_item("Monthly (21 วัน)", bt["forecast"]["Monthly"]["acc"]),
                row_item("Yearly (252 วัน)", bt["forecast"]["Yearly"]["acc"]),
                row_item("Balanced Accuracy", bt["balanced_acc"], "#059669"),
                row_item("Out-of-Sample Total", f"{bt['total_oos']:,} ตัวอย่าง"),
                {"type": "separator", "margin": "md", "color": "#E2E8F0"},

                {"type": "text", "text": "2. Trading Simulation (Entry / TP1-3 / SL)", "size": "xs", "weight": "bold", "color": "#0F172A", "margin": "md"},
                row_item("จำนวนรอบเทรด (Trades)", f"{bt['trades']} รอบ"),
                row_item("Win Rate", bt["win_rate"], "#059669"),
                row_item("Profit Factor", bt["profit_factor"]),
                row_item("Net Return (หักค่าคอม)", bt["net_return"], bt["net_ret_color"]),
                row_item("Max Drawdown", bt["max_dd"], "#DC2626"),
                {
                    "type": "box",
                    "layout": "vertical",
                    "margin": "sm",
                    "backgroundColor": "#F8FAFC",
                    "cornerRadius": "8px",
                    "paddingAll": "8px",
                    "contents": [
                        {"type": "text", "text": bt["tp_sl_detail"], "size": "xxs", "color": "#334155", "align": "center"}
                    ]
                },
                {"type": "text", "text": "หมายเหตุ: ผลทดสอบย้อนหลัง (No Look-ahead) ไม่รับประกันผลตอบแทนในอนาคต", "size": "xxs", "color": "#B45309", "wrap": True, "margin": "md"}
            ]
        }
    }
    return FlexMessage(
        alt_text=f"Axion Backtest 10Y: {bt['symbol']}",
        contents=FlexContainer.from_dict(bubble_dict),
        quick_reply=get_quick_reply_buttons(bt['symbol'])
    )

# ==========================================
# ส่วนที่ 3: SMC & วิเคราะห์หุ้นรายตัว
# ==========================================
def analyze_leorio_smc(df, atr_val, ema_200):
    smc = {
        "structure": "Sideway / รอเลือกทาง",
        "is_bullish_shift": False,
        "liquidity_sweep": False,
        "demand_zone": None, "supply_zone": None,
        "bull_ob": None, "bear_ob": None,
        "bull_fvg": None, "bear_fvg": None
    }
    if len(df) < 30:
        return smc
    opens, highs, lows, closes = df['Open'].values, df['High'].values, df['Low'].values, df['Close'].values
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
    valid_candidates = []
    for c in candidates:
        try:
            price = float(c['price'])
            if np.isfinite(price) and price > 0 and (price < current_price if is_support else price > current_price):
                valid_candidates.append({**c, 'price': price})
        except (KeyError, TypeError, ValueError):
            continue
    valid_candidates.sort(key=lambda x: x['price'], reverse=is_support)
    if not valid_candidates:
        return []
    merge_distance = max((0.4 * atr_val / current_price) if np.isfinite(atr_val) and current_price > 0 else 0, 0.008)
    clusters = []
    for item in valid_candidates:
        target = next((cl for cl in clusters if abs(cl['price'] - item['price']) / current_price <= merge_distance), None)
        if target is None:
            clusters.append({'price': item['price'], 'weight': float(item.get('weight', 1)), 'labels': [str(item.get('label', 'Level'))]})
        else:
            w = float(item.get('weight', 1))
            total = target['weight'] + w
            target['price'] = (target['price'] * target['weight'] + item['price'] * w) / total
            target['weight'] = total
            if item.get('label') not in target['labels']:
                target['labels'].append(str(item.get('label', 'Level')))
    results = []
    for cl in clusters:
        score = 40 + cl['weight'] * 1.3 + (len(cl['labels']) - 1) * 4
        if is_support:
            score += 8 if is_uptrend else (-10 if not has_bull_shift else 0)
            score += 5 if rsi_val <= 38 else 0
            score += 4 if rvol >= 1.2 else 0
            score += 4 if macd_turning_up else 0
        else:
            score += 8 if not is_uptrend else 0
            score += 5 if rsi_val >= 68 else 0
        results.append({'price': float(cl['price']), 'label': ' + '.join(cl['labels'][:2]), 'setup_score': min(max(int(score), 40), 95)})
    results.sort(key=lambda x: x['price'], reverse=is_support)
    return results[:3]

def get_pro_stock_data(symbol, interval="1d", include_meta=True):
    try:
        symbol = symbol.upper().strip()
        period = "1mo" if interval in ["15m", "1h"] else "5y"
        df = fetch_clean_history(symbol, period=period, interval=interval)
        if (df is None or len(df) < 15) and "." not in symbol:
            alt_symbol = f"{symbol}.BK"
            alt_df = fetch_clean_history(alt_symbol, period=period, interval=interval)
            if alt_df is not None and len(alt_df) >= 15:
                symbol = alt_symbol
                df = alt_df
        if df is None or len(df) < 30:
            return None

        df_daily = df if interval == "1d" else fetch_clean_history(symbol, period="5y", interval="1d")
        if df_daily is None or len(df_daily) < 30:
            df_daily = df if interval == "1d" else pd.DataFrame()
        if df_daily is None or len(df_daily) < 30:
            return None

        current_price = float(df['Close'].iloc[-1])
        prev_close = float(df['Close'].iloc[-2]) if len(df) >= 2 else current_price
        price_change = ((current_price - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0

        day_low = float(df_daily['Low'].iloc[-1])
        day_high = float(df_daily['High'].iloc[-1])
        w52_low = float(df_daily['Low'].tail(252).min())
        w52_high = float(df_daily['High'].tail(252).max())

        rsi_val = calculate_wilder_rsi(df['Close'], 14)
        atr_val = calculate_atr(df, 14)
        if not np.isfinite(atr_val) or atr_val <= 0:
            return None
        context_df = df_daily if interval != "1d" else df
        ema_50 = float(context_df['Close'].ewm(span=50, adjust=False).mean().iloc[-1])
        ema_200 = float(context_df['Close'].ewm(span=200, adjust=False).mean().iloc[-1])

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

        raw_supports = []
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

        raw_resistances = []
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

        if not sup_zones:
            sup_zones = [{'price': max(current_price - 1.5 * atr_val, 0.01), 'label': 'ATR Ref', 'setup_score': 45}]

        best_entry = sup_zones[0]['price']
        risk_unit = max(1.35 * atr_val, best_entry * 0.015)
        stop_loss = best_entry - risk_unit

        tp1_atr = best_entry + (1.5 * risk_unit)
        tp2_atr = best_entry + (2.5 * risk_unit)
        tp3_atr = best_entry + (4.0 * risk_unit)
        tp1 = max(tp1_atr, res_zones[0]['price'] * 0.995) if len(res_zones) >= 1 else tp1_atr
        tp2 = max(tp1 + risk_unit, res_zones[1]['price']) if len(res_zones) >= 2 else max(tp1 + risk_unit, tp2_atr)
        tp3 = max(tp2 + risk_unit, res_zones[2]['price']) if len(res_zones) >= 3 else max(tp2 + risk_unit, tp3_atr)

        rr_ratio = (tp2 - best_entry) / max(best_entry - stop_loss, 0.01)
        max_score = int(max(s['setup_score'] for s in sup_zones[:3]))

        if interval == "1d":
            log_weekly_setup_to_sheets(symbol, best_entry, tp1, tp2, tp3, stop_loss, sup_zones[0]['setup_score'])

        meta = fetch_fundamentals_and_meta(symbol, current_price, ema_200) if include_meta else {
            "company_name": symbol, "exchange": "US", "currency": "THB" if ".BK" in symbol else "USD",
            "currency_sym": "฿" if ".BK" in symbol else "$", "fair_value": None, "fv_note": "", "after_hours": None
        }

        mtf = compute_mtf_forecast(df_daily, current_price, ema_200, meta["fair_value"])
        save_forecast_record(symbol, current_price, mtf, meta.get("fair_value"), df_daily.index[-1])
        mtf_total = sum(v["score"] for v in mtf.values())

        if mtf_total >= 4 and max_score >= 65:
            decision = "BULLISH SETUP"
            dec_color = "#059669"
            dec_desc = f"แนวโน้มหลายกรอบเวลาเป็นบวก โซนรับอ้างอิง {best_entry:,.2f}"
        elif mtf_total >= 1:
            decision = "BULLISH BIAS / WAIT"
            dec_color = "#10B981"
            dec_desc = f"แนวโน้มเอนเอียงบวก รอจังหวะย่อใกล้ {sup_zones[0]['label']} ({best_entry:,.2f})"
        elif mtf_total <= -3:
            decision = "BEARISH BIAS / CAUTION"
            dec_color = "#DC2626"
            dec_desc = f"โครงสร้างอ่อนแรง ({smc['structure']}) ระวังความเสี่ยง"
        else:
            decision = "HOLD / WAIT"
            dec_color = "#D97706"
            dec_desc = f"ทิศทางยังไม่ชัดเจน รอสัญญาณยืนยันที่ {best_entry:,.2f}"

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
                (f"{sup_zones[1]['price']:,.2f} [Score {sup_zones[1]['setup_score']}] • {sup_zones[1]['label']}"
                 if len(sup_zones) > 1 else "ไม่มีแนวรับถัดไปที่ยืนยันได้")
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
                            "contents": [{"type": "text", "text": f"Score {data['setup_score']}", "size": "xxs", "weight": "bold", "color": badge_txt}]
                        }
                    ]
                },
                {"type": "text", "text": "SYMBOL", "size": "xxs", "color": "#64748B", "margin": "md"},
                {"type": "text", "text": f"{data['symbol']} ({data['interval']})", "size": "xxl", "weight": "bold", "color": "#0F172A"},
                {"type": "text", "text": data["company_meta"], "size": "xs", "color": "#64748B", "margin": "xs", "wrap": True},
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
                                {"type": "text", "text": "Valuation Ref.", "size": "xxs", "color": "#64748B"},
                                {"type": "text", "text": data["fv_str"], "size": "lg", "weight": "bold", "color": "#0F172A", "margin": "xs"},
                                {"type": "text", "text": data["fv_sub"], "size": "xxs", "weight": "bold", "color": data["fv_color"]}
                            ]
                        }
                    ]
                },
                {"type": "text", "text": f"{data['after_hours_str']}  |  {data['w52_str']}", "size": "xxs", "color": "#64748B", "margin": "sm", "wrap": True},
                {"type": "text", "text": "Multi-Timeframe Forecast", "weight": "bold", "size": "sm", "color": "#0F172A", "margin": "lg"},
                {
                    "type": "box",
                    "layout": "horizontal",
                    "margin": "sm",
                    "paddingBottom": "4px",
                    "contents": [
                        {"type": "text", "text": "ระยะ", "size": "xxs", "weight": "bold", "color": "#475569", "flex": 3},
                        {"type": "text", "text": "ทิศทาง", "size": "xxs", "weight": "bold", "color": "#475569", "flex": 5},
                        {"type": "text", "text": "คะแนน", "size": "xxs", "weight": "bold", "color": "#475569", "align": "end", "flex": 2}
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
                    "contents": [{"type": "text", "text": data["setup_str"], "size": "xxs", "color": "#0F172A", "wrap": True}]
                }
            ]
        },
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
                        {"type": "button", "style": "primary", "color": "#0F172A", "height": "sm",
                         "action": {"type": "message", "label": "🧪 Backtest 10Y", "text": f"/BACKTEST {data['symbol']}"}},
                        {"type": "button", "style": "primary", "color": "#059669", "height": "sm",
                         "action": {"type": "message", "label": "📊 สรุปพอร์ต", "text": "/SUMMARY"}}
                    ]
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "spacing": "sm",
                    "contents": [
                        {"type": "button", "style": "secondary", "height": "sm",
                         "action": {"type": "message", "label": "🎯 สแกนรับ", "text": "/SCAN_SUPPORT"}},
                        {"type": "button", "style": "secondary", "height": "sm",
                         "action": {"type": "message", "label": "📋 Watchlist", "text": "/LIST"}}
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
    return f"Axion Smart Analyzer v{MODEL_VERSION} is Awake! | Audit rows: {get_forecast_audit_count()}", 200

@app.route("/scan_discord", methods=['GET'])
def manual_discord_trigger():
    if not DISCORD_WEBHOOK_URL:
        return "⚠️ ยังไม่ได้ตั้งค่า DISCORD_WEBHOOK_URL", 400
    threading.Thread(target=scan_and_notify_discord, daemon=True).start()
    return "✅ Axion สั่งรันสแกนหุ้นส่งเข้า Discord เรียบร้อยแล้ว!", 200

@app.route("/callback", methods=['POST'])
def callback():
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
        return "LINE credentials are not configured", 503
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
        user_watchlists[user_id] = list(DEFAULT_WATCHLIST)

    watchlist = user_watchlists[user_id]
    messages_to_reply = []
    default_qr = get_quick_reply_buttons()

    if command in ["/HELP", "HELP"]:
        help_text = (
            "📖 คู่มือ Axion Smart Analyzer v1.2\n\n"
            "• พิมพ์ชื่อหุ้น (เช่น QQQI, RKLB, QCOM) ดู Dashboard\n"
            "• /BACKTEST [ชื่อหุ้น] ทดสอบย้อนหลัง 10 ปี (Forecast + Trading)\n"
            "• +[ชื่อหุ้น] / -[ชื่อหุ้น] เพิ่มหรือลบ Watchlist\n"
            "• /SUMMARY สรุปพอร์ต | /SCAN_SUPPORT สแกนโซนรับ\n"
            "• /LIST ดู Watchlist | /AUDIT ดูสถิติฐานข้อมูล"
        )
        messages_to_reply.append(TextMessage(text=help_text, quick_reply=default_qr))

    elif command.startswith("/BACKTEST") or command.startswith("BACKTEST"):
        parts = command.split()
        target_sym = parts[1] if len(parts) > 1 else (watchlist[0] if watchlist else "QCOM")
        bt = run_axion_backtest(target_sym, period="10y")
        if bt:
            messages_to_reply.append(build_backtest_flex_card(bt))
        else:
            messages_to_reply.append(TextMessage(text=f"⚠️ ข้อมูลย้อนหลังของ '{target_sym}' ไม่เพียงพอสำหรับ Backtest", quick_reply=default_qr))

    elif command in ["/WEEKLY_PERF", "WEEKLY_PERF"]:
        report = evaluate_weekly_performance_from_sheets()
        messages_to_reply.append(TextMessage(text=report, quick_reply=default_qr))

    elif command in ["/AUDIT", "AUDIT"]:
        messages_to_reply.append(TextMessage(
            text=f"🧾 Axion Forecast Audit\nบันทึก Forecast แล้ว: {get_forecast_audit_count()} รายการ\nModel: {MODEL_VERSION}\nBacktest Engine: 10Y Walk-Forward (Both)",
            quick_reply=default_qr))

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
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))

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
                     float(fair_value) if fair_value and np.isfinite(fair

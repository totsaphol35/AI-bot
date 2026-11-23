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
            df = yf.download(

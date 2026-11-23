import csv
import os
from datetime import datetime

# ไฟล์จัดเก็บสถิติผลงานรายสัปดาห์
PERF_LOG_FILE = "weekly_performance_log.csv"

def init_perf_log():
    """สร้างไฟล์ CSV สำหรับเก็บบันทึกผลงาน หากยังไม่มี"""
    if not os.path.exists(PERF_LOG_FILE):
        with open(PERF_LOG_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                "Timestamp", "Symbol", "Week_Start_Price", "Support_L1", 
                "Resistance_L1", "Setup_Score", "Actual_Low_Next_Week", 
                "Actual_High_Next_Week", "Status", "Note"
            ])

def log_weekly_setup(symbol, price, support_l1, resistance_l1, score):
    """บันทึกข้อมูล Setup ของหุ้นในสัปดาห์นี้"""
    init_perf_log()
    week_str = datetime.now().strftime("%Y-W%U")
    
    # ตรวจสอบว่าเคยบันทึกสัปดาห์นี้ของหุ้นตัวนี้ไป รยัง เพื่อไม่ให้บันทึกซ้ำ
    rows = []
    already_logged = False
    if os.path.exists(PERF_LOG_FILE):
        with open(PERF_LOG_FILE, mode='r', encoding='utf-8') as f:
            reader = csv.reader(f)
            rows = list(reader)
            for row in rows[1:]:
                if len(row) > 0 and row[1] == symbol and row[0].startswith(week_str):
                    already_logged = True
                    break
                    
    if not already_logged:
        with open(PERF_LOG_FILE, mode='a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                week_str, symbol, f"{price:.2f}", f"{support_l1:.2f}", 
                f"{resistance_l1:.2f}", score, "PENDING", "PENDING", "PENDING", "Waiting for weekly close"
            ])

def evaluate_weekly_performance():
    """ฟังก์ชันประเมินผลย้อนหลังรายสัปดาห์จากข้อมูลราคาจริง (Actual Price Action)"""
    init_perf_log()
    if not os.path.exists(PERF_LOG_FILE):
        return "ยังไม่มีข้อมูลบันทึกสถิติ"
        
    updated_rows = []
    evaluated_count = 0
    wins = 0
    total_evaluated = 0

    with open(PERF_LOG_FILE, mode='r', encoding='utf-8') as f:
        reader = csv.reader(f)
        header = next(reader)
        updated_rows.append(header)
        
        for row in reader:
            if not row:
                continue
            week_str, symbol, start_p, sup_l1, res_l1, score, act_low, act_high, status, note = row
            
            if status == "PENDING":
                # ดึงข้อมูลราคาช่วง 7 วันล่าสุดเพื่อตรวจสอบว่าราคาจริงทำ Low/High เท่าไหร่
                df = fetch_clean_history(symbol, period="14d", interval="1d")
                if not df.empty and len(df) >= 5:
                    recent_low = float(df['Low'].iloc[-5:].min())
                    recent_high = float(df['High'].iloc[-5:].max())
                    curr_close = float(df['Close'].iloc[-1])
                    
                    s_val = float(sup_l1)
                    r_val = float(res_l1)
                    
                    # เงื่อนไขประเมินประสิทธิภาพจริง:
                    # ถ้า Low ของสัปดาห์ลงมาแตะแนวรับ (อยู่ในรัศมี 1.5% จากแนวรับ) แล้วราคาปัจจุบันหรือ High ดีดตัวกลับขึ้นไปแปลว่าแนวรับทำงานได้ดี
                    if recent_low <= s_val * 1.015:
                        total_evaluated += 1
                        if curr_close >= s_val:
                            status = "WIN (Support Held)"
                            wins += 1
                            note = f"ราคาลงแตะรับ {s_val:.2f} แล้วดีดกลับ (Low: {recent_low:.2f})"
                        else:
                            status = "LOSS (Support Broken)"
                            note = f"หลุดแนวรับ {s_val:.2f} (Low: {recent_low:.2f})"
                    else:
                        status = "NO TOUCH"
                        note = "ราคายังไม่ลงมาทดสอบแนวรับในสัปดาห์นี้"
                        
                    act_low = f"{recent_low:.2f}"
                    act_high = f"{recent_high:.2f}"
                    evaluated_count += 1
            
            updated_rows.append([week_str, symbol, start_p, sup_l1, res_l1, score, act_low, act_high, status, note])

    # เขียนข้อมูลที่อัปเดตผลแล้วกลับลงไฟล์
    with open(PERF_LOG_FILE, mode='w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerows(updated_rows)
        
    win_rate = (wins / total_evaluated * 100) if total_evaluated > 0 else 0.0
    return f"📊 **รายงานประสิทธิภาพแนวรับ (Weekly Performance Audit):**\n• ประเมินแล้ว: {total_evaluated} รายการ\n• แนวรับรับอยู่ (Win): {wins} ครั้ง\n• สถิติความแม่นยำจริง (Hit Rate): **{win_rate:.1f}%**"

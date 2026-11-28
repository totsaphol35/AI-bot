import os
import sys
import time
import py_compile
import requests

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ISSUE_TITLE = os.getenv("ISSUE_TITLE", "")
ISSUE_BODY = os.getenv("ISSUE_BODY", "")
ISSUE_NUM = os.getenv("ISSUE_NUM", "0")
TARGET_FILE = "app.py"

if not GEMINI_API_KEY:
    print("Error: Missing GEMINI_API_KEY")
    sys.exit(1)

with open(TARGET_FILE, "r", encoding="utf-8") as f:
    current_code = f.read()

prompt = (
    f"คุณคือ Senior Python Engineer ผู้ดูแลระบบบอทเทรด Axion Quant Core\n"
    f"คำสั่งอัปเกรดจากเจ้าของระบบ (Issue #{ISSUE_NUM}):\n"
    f"หัวข้อ: {ISSUE_TITLE}\n"
    f"รายละเอียด: {ISSUE_BODY}\n\n"
    f"โค้ดปัจจุบันของไฟล์ {TARGET_FILE}:\n"
    f"{current_code}\n\n"
    f"กฎเหล็ก:\n"
    f"1. แก้ไขโค้ดตามคำสั่งให้สมบูรณ์ โดยรักษาฟีเจอร์เดิมทั้งหมดไว้\n"
    f"2. อัปเดตตัวแปร MODEL_VERSION ด้านบนสุดให้มีรหัสเวอร์ชันใหม่และอ้างอิง Issue #{ISSUE_NUM}\n"
    f"3. ตอบกลับตามรูปแบบด้านล่างนี้เป๊ะๆ เท่านั้น:\n"
    f"ส่วนที่ 1: สรุปภาษาไทยแบบกระชับ อ่านง่าย ระบุ 3 หัวข้อ:\n"
    f"- จุดที่เปลี่ยน: (ระบุฟังก์ชันหรือส่วนที่เพิ่ม/แก้)\n"
    f"- ส่วนอื่นที่กระทบ: (ระบุส่วนอื่นของระบบที่มีการเปลี่ยนแปลง หรือระบุว่าไม่มี)\n"
    f"- เหตุผล: (อธิบายสั้นๆ ว่าทำไมต้องแก้ตรงนี้)\n"
    f"จากนั้นขึ้นบรรทัดใหม่แล้วพิมพ์คำว่า ===CODE_START===\n"
    f"ส่วนที่ 2: โค้ด Python ฉบับเต็มของไฟล์ {TARGET_FILE} ห้ามใส่เครื่องหมาย Markdown code block ครอบโค้ด\n"
)

payload = {
    "contents": [{"parts": [{"text": prompt}]}],
    "generationConfig": {"temperature": 0.2}
}

models = ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-2.0-flash"]
res = None

for model_name in models:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
    print(f"Trying model: {model_name}...")
    for attempt in range(1, 3):
        res = requests.post(url, json=payload, timeout=120)
        if res.ok:
            print(f"Success with {model_name}!")
            break
        print(f"Model {model_name} failed ({res.status_code}).")
        if res.status_code in (429, 500, 503, 504) and attempt < 2:
            time.sleep(10)
        else:
            break
    if res and res.ok:
        break

if not res or not res.ok:
    print(f"All models failed. Last error ({res.status_code if res else 'N/A'}): {res.text if res else ''}")
    sys.exit(1)

raw_text = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

if "===CODE_START===" in raw_text:
    summary_text, new_code = raw_text.split("===CODE_START===", 1)
    summary_text = summary_text.strip()
    new_code = new_code.strip()
else:
    summary_text = "- จุดที่เปลี่ยน: อัปเดตโค้ดใน app.py ตามคำสั่ง\n- ส่วนอื่นที่กระทบ: ไม่มี\n- เหตุผล: ดำเนินการตาม Issue"
    new_code = raw_text

ticks = chr(96) * 3
if new_code.startswith(ticks):
    lines = new_code.splitlines()
    if lines[0].startswith(ticks):
        lines = lines[1:]
    if lines and lines[-1].startswith(ticks):
        lines = lines[:-1]
    new_code = "\n".join(lines)

temp_file = "app_test_compile.py"
with open(temp_file, "w", encoding="utf-8") as f:
    f.write(new_code)

try:
    py_compile.compile(temp_file, doraise=True)
    os.replace(temp_file, TARGET_FILE)
    with open("upgrade_summary.txt", "w", encoding="utf-8") as sf:
        sf.write(summary_text)
    print("Syntax check passed! Updated app.py and saved upgrade_summary.txt.")
except py_compile.PyCompileError as e:
    print(f"Syntax Error detected! Aborting upgrade:\n{e}")
    os.remove(temp_file)
    sys.exit(1)

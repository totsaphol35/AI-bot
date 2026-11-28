import os
import sys
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
    f"2. อัปเดตตัวแปร MODEL_VERSION ด้านบนสุดให้มีรหัสเวอร์ชันใหม่และอ้างอิง Issue #{ISSUE_NUM} (เช่น 1.5.2-issue-{ISSUE_NUM})\n"
    f"3. ตอบกลับมาเป็นโค้ด Python ฉบับเต็มของไฟล์ {TARGET_FILE} เท่านั้น ห้ามมีคำอธิบายอื่นปน\n"
    f"4. ห้ามใส่เครื่องหมาย Markdown code block ครอบโค้ด\n"
)

url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
payload = {
    "contents": [{"parts": [{"text": prompt}]}],
    "generationConfig": {"temperature": 0.2}
}

res = requests.post(url, json=payload, timeout=120)
if not res.ok:
    print(f"Gemini API Error ({res.status_code}): {res.text}")
    sys.exit(1)

new_code = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

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
    print("Syntax check passed! Updated app.py successfully.")
except py_compile.PyCompileError as e:
    print(f"Syntax Error detected! Aborting upgrade:\n{e}")
    os.remove(temp_file)
    sys.exit(1)

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

prompt = f"""คุณคือ Senior Python Engineer ผู้ดูแลระบบบอตเทรด Axion Quant Core
คำสั่งอัปเกรดจากเจ้าของระบบ (Issue #{ISSUE_NUM}):
หัวข้อ: {ISSUE_TITLE}
รายละเอียด: {ISSUE_BODY}

โค้ดปัจจุบันของไฟล์ {TARGET_FILE}:
```python
{current_code}

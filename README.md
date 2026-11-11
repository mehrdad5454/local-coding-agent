# Local Agent

یک **Coding Agent لوکال** برای انجام وظایف مهندسی نرم‌افزار و تحلیل/پژوهش در محیط محلی، با رابط وب و خط فرمان، تمرکز بر کنترل تغییرات (برنامه‌ریزی، تأیید، backup/rollback، journal/evidence) و قابلیت‌های چندایجنتی.

## پیش‌نیازها
- **Python 3.14+** (پروژه با Python 3.14.6 توسعه/آزموده شده)
- **llama.cpp** یا هر سرور مدل سازگار که endpoint تکمیل متن را فراهم کند
- حداقل **16GB RAM** (بسته به مدل؛ مدل‌های بزرگ‌تر به RAM/VRAM بیشتری نیاز دارند)
- سیستم‌عامل: Windows (دستورهای زیر با PowerShell/CMD نوشته شده‌اند)

## نصب
```powershell
# 1) ساخت محیط مجازی
python -m venv .venv

# 2) فعال‌سازی (PowerShell)
.\.venv\Scripts\Activate.ps1
# یا (CMD)
.\.venv\Scripts\activate.bat

# 3) نصب وابستگی‌ها
pip install -r requirements.txt
```

## راه‌اندازی مدل (llama.cpp + Qwen3-8B یا مدل دیگر)
پروژه به‌صورت پیش‌فرض از `MODEL_URL=http://127.0.0.1:8080/completion` استفاده می‌کند (قابل تغییر با متغیر محیطی `MODEL_URL`).

نمونه اجرای سرور `llama.cpp` (مسیرها و نام باینری را مطابق سیستم خود جایگزین کنید):
```powershell
# مثال: اجرای llama-server با یک مدل GGUF (مانند Qwen3-8B)
llama-server `
  -m "D:\models\Qwen3-8B-Instruct-Q4_K_M.gguf" `
  --host 127.0.0.1 `
  --port 8080 `
  -c 8192
```
- می‌توانید هر مدل GGUF سازگار دیگری را جایگزین کنید؛ اندازه context (`-c`) و پارامترها را بر اساس منابع سیستم تنظیم کنید.
- پس از اجرا، endpoint `http://127.0.0.1:8080/completion` باید در دسترس باشد.

## اجرا
```powershell
# رابط وب (Flask)
python web_agent.py

# یا حالت خط فرمان
python agent.py
```

## تست
```powershell
pytest -q
```
(در صورت استفاده از محیط مجازی: `.\.venv\Scripts\python.exe -m pytest -q`)

## ساختار پروژه
- `agent.py` — هسته اصلی عامل: ابزارها، حلقه اجرا، ایمنی، برنامه‌ریزی/تأیید تغییرات، backup/rollback، journal/verify.
- `web_agent.py` — رابط وب برای تعامل با عامل.
- `web_research.py` — ابزارهای وب read-only (جستجو و استخراج متن صفحه).
- `agents/` — معماری چندایجنتی: ارکستراتور، ایجنت‌های تخصصی، قابلیت‌ها/مجوزها، اکشن‌ها، completion gate.
- `evidence/` — ثبت موقت شواهد و دروازه استناد (citation/evidence ledger).
- `memory/` — لایه و پیاده‌سازی حافظه مکالمه (SQLite).
- `tests/` — تست‌های خودکار (pytest) برای evidence، routing، ایمنی تغییرات و سناریوهای وب.
- `general_workspace/` — فضای کاری ایزوله برای اجرای وظایف در حالت عمومی (در صورت استفاده/فعال‌سازی).
- `PROJECT_STATUS.md` — گزارش وضعیت فعلی پروژه و مرجع تصمیم‌گیری.

## یادداشت‌ها
- پیش از تغییرات مهم، از وجود بکاپ و وضعیت Git مطمئن شوید.
- برای جزئیات وضعیت فعلی، به `PROJECT_STATUS.md` مراجعه کنید.

# وضعیت پروژه Local Agent

تاریخ بررسی: 2026-11-11  
مسیر پروژه: `D:\AI\local-agent`  
هدف این سند: ثبت وضعیت فعلی پروژه به‌عنوان مرجع تصمیم‌گیری قبل از هر تغییر جدید (بدون ایجاد تغییر در کد).

## 1) ساختار پروژه

### پوشه‌های اصلی (به‌جز `.venv` و `__pycache__`)
- `.agent-backup-web/`, `.kilo/`, `.pytest_cache/`, `.vscode/`
- `agents/` (هسته چندایجنتی و ارکستراتور) و `agents/specialists/`
- `evidence/` (لجر موقت شواهد و دروازه استناد)
- `general_workspace/` (محیط اجرای sandbox در حالت عمومی)
- `memory/` (ذخیره‌سازی حافظه مکالمه، SQLite)
- `tests/` و `tests_tmp/`

### فایل‌های Python پروژه (به‌جز `.venv`) — خلاصه اندازه/تعداد خطوط
| فایل | خطوط | حجم (بایت) |
|---|---:|---:|
| `agent.py` | 13427 | 561041 |
| `web_agent.py` | 2718 | 93861 |
| `stage4_adversarial_selfcheck.py` | 1653 | 64320 |
| `website_analyst.py` | 378 | 16089 |
| `web_research.py` | 425 | 18569 |
| `review_apply_selfcheck.py` | 420 | 14562 |
| `phase7_selfcheck.py` | 741 | 29972 |
| `step3_selfcheck.py` | 633 | 26362 |
| `plan_target_selfcheck.py` | 593 | 23164 |
| `stage4_plan_summary_selfcheck.py` | 619 | 23065 |
| سایر ماژول‌ها/تست‌ها/اسکریپت‌های selfcheck | — | — |

**جمع:** 57 فایل Python پروژه (غیر از `.venv`) شناسایی شد. هسته اصلی در `agent.py` متمرکز است (بسیار بزرگ و چندمسئولیتی).

### فایل‌های کلیدی سطح ریشه
- `agent.py` (منطق اصلی، ابزارها، ایمنی، journal/verify/rollback و ...)
- `web_agent.py` (رابط وب Flask)
- `web_research.py` (جستجو/استخراج وب read-only)
- `website_analyst.py` (تحلیل وب‌سایت، Phase G)
- `seo_adapter.py`, `projects.json`, `action-journal.json`, `chat-history.db`
- اسکریپت‌های `*_selfcheck.py` متعدد برای سناریوهای مختلف

## 2) وابستگی‌ها و محیط اجرا

### فایل‌های وابستگی/پیکربندی بسته
- `requirements.txt`: **موجود** (تنها وابستگی‌های صریح فاز web-research پین شده‌اند)
  - `ddgs==9.16.0`
  - `trafilatura==2.3.1`
- `pyproject.toml` / `setup.py` / `setup.cfg`: **یافت نشد**

### پکیج‌های نصب‌شده در `.venv` (نمونه/کلیدی، بر اساس `site-packages`)
- وب/HTTP/UI: `flask`, `werkzeug`, `jinja2`, `httpx2`, `httpcore2`, `urllib3`, `requests` (vendor/pip), `ddgs`, `trafilatura`
- داده/اعتبارسنجی: `pydantic`, `pydantic_core`, `lxml`, `regex`, `dateparser`
- تست: `pytest` (+ افزونه‌های داخلی)
- سایر: `openai` (در محیط نصب است)، `certifi`, `charset-normalizer`, `tzdata` و ...

> توضیح: لیست بالا از محتوای `.venv\Lib\site-packages` استخراج شده است؛ بسیاری از آن‌ها وابستگی‌های غیرمستقیم هستند.

### نسخه‌ها
- Python سیستم: `Python 3.14.6`
- Python داخل `.venv`: `Python 3.14.6`
- `llama.cpp`: فایل/اجرای `llama*` در مسیر پروژه یا PATH یافت نشد. اتصال مدل از طریق `MODEL_URL` (پیش‌فرض `http://127.0.0.1:8080/completion`) انجام می‌شود؛ نسخه سرور llama.cpp از این بررسی قابل تعیین نبود.

## 3) ماژول‌ها و ابزارهای موجود

### ابزارها/عملیات اصلی (بر اساس توابع شاخص در `agent.py`)
- فایل/دایرکتوری: `read_file`, `write_file`, `write_file_multi`, `edit_file`, `list_directory`, `read_files_multi`, `search_subfolders`, `search_in_files`, `create_backup`
- اجرا/وظایف: `run_command`, `run_general_task`, `run_generic_task`, `run_agentic_loop` / `run_loop_steps`, `run_agent`
- امنیت/محدودسازی خواندن: `is_sensitive_read_target`, `mask_sensitive_content`, `sensitive_read_refusal_text`, `safe_path`
- برنامه‌ریزی/تغییر: `build_file_plan`, `remember_file_plan`, `extract_file_candidates`, `validate_mutating_args`, `build_pending_revalidation_context`, `execute_pending_action`
- بازگشت/حالت: `rollback_file`, `remember_rollback_result`, بررسی نحو (`check_python_syntax`, `check_json_syntax`, `check_external_syntax`, `validate_written_file`, `verify_*`)

> یادداشت: شناسایی بالا مبتنی بر نام توابع و الگوهای موجود است؛ جزئیات دقیق هر ابزار در بدنه طولانی `agent.py` تعریف شده است.

### ماژول‌های چندایجنتی و کنترل (`agents/`)
- `base.py`: کلاس پایه Agent، مدیریت ابزار/stop، مرز mutation (`MUTATION_TOOL_NAMES`)
- `orchestrator.py` / `SeniorAgent`: مسیریابی درخواست، قرارداد پیشنهاد (`PROPOSAL`)، جداسازی ابزارهای تغییردهنده
- ایجنت‌های تخصصی: `assistant.py`, `marketing.py`, `research.py`, `opportunity.py`, `specialists/content.py`
- زیرساخت: `actions.py` (پیشنهاد عمل)، `capabilities.py`, `permissions.py`, `providers.py`, `persona.py`, `metadata.py`, `core_adapter.py`, `memory_adapter.py`
- `completion_gate.py`: ارزیابی تکمیل درخواست بر اساس الزامات و شواهد واقعی (وضعیت‌های complete/incomplete/blocked/unverifiable)

### ماژول‌های Evidence / تغییرات
- `evidence/ledger_temp.py`: ثبت موقت شواهد ابزارهای وب (search/fetch) به‌صورت in-memory/اشتقاقی
- `evidence/citation_gate.py`: بررسی ردیابی استناد URLها نسبت به شواهد اجرا (caveat-only، بدون بازنویسی/مسدودسازی)
- در `agent.py`: مکانیزم‌های Journal/Action context، Backup، Verify، Undo/Rollback، Approval/Revalidation و مدیریت pending action مشاهده می‌شود (پیاده‌سازی در هسته متمرکز).

### حافظه و وب
- `memory/schema.py`, `memory/store.py`, `memory/sqlite_store.py`: قرارداد و پیاده‌سازی حافظه مکالمه (SQLite)
- `web_research.py`: ابزارهای read-only جستجو و استخراج متن صفحه با سخت‌گیری SSRF و محدودیت خروجی
- `web_agent.py`: رابط وب/مدیریت تعامل و اعمال سیاست Origin محلی

## 4) وضعیت تست‌ها

### فایل‌های تست (`tests/`)
- `test_citation_gate.py` (104)
- `test_completion_gate_wiring.py` (445)
- `test_completion_gate.py` (391)
- `test_conversation_isolation.py` (14)
- `test_delete_intent_negation.py` (305)
- `test_evidence_gating_basic.py` (8)
- `test_evidence_ledger_live.py` (150)
- `test_evidence_ledger_temp.py` (118)
- `test_general_mode_routing.py` (27)
- `test_general_mode_tools.py` (16)
- `test_mutation_path_match.py` (307)
- `test_web_research_mode.py` (205)
- `test_web_research_pinning.py` (211)

**تعداد فایل تست:** 13 فایل (جمعاً 2301 خط).

### نتیجه اجرا (در `.venv`)
- فرمان: `.\.venv\Scripts\python.exe -m pytest -q`
- نتیجه: **55 passed** در **2.09s**
- ناموفق/خطا: **0** (بر اساس اجرای انجام‌شده)

> نکته: مجموعه‌ای از اسکریپت‌های `*_selfcheck.py` در ریشه وجود دارد که خارج از این اجرای pytest گزارش نشده‌اند.

## 5) وضعیت Git

### 5 کامیت اخیر
1. `19fd84f` | 2026-10-12 | `feat: add completion gate and evidence reporting`
2. `f953d42` | 2026-10-08 | `feat: enable general workspace agentic execution`
3. `b19938c` | 2026-10-08 | `fix: increase agent loop output budget to 3200`
4. `3ddc516` | 2026-10-07 | `feat: opt-in multi-agent activation with routing evidence and producer attribution`
5. `4af93e9` | 2026-10-07 | `feat: wire first real specialist into multi-agent flow`

### تغییرات Commit نشده / وضعیت Working Tree
- **Modified:** `agent.py`, `agents/providers.py`, `web_agent.py`
- **Untracked (نمونه‌های مهم):** `.vscode/`, چندین `agent.py.bak*`, `agents/providers.py.bak*`, `web_agent.py.bak*`, `evidence/`, `general_workspace/`, `requirements.txt`, `seo_adapter.py` (+bak), `tests/test_citation_gate.py`, `tests/test_conversation_isolation.py`, `tests/test_delete_intent_negation.py`, `tests/test_evidence_*`, `tests/test_general_mode_*`, `tests/test_mutation_path_match.py`, `tests/test_web_research_*`, `web_research.py`, `out.txt`
- جمعاً وضعیت `git status --short` شامل تعداد قابل‌توجهی فایل تغییر‌یافته/ردیابی‌نشده است (درخت کاری تمیز نیست).

## 6) تحلیل وضعیت (نقاط قوت و ضعف)

### نقاط قوت
- پوشش قابل‌توجه مفاهیم ایمنی/کنترل تغییر: backup، rollback، verify، approval/revalidation، journal و مرز mutation در معماری چندایجنتی.
- وجود لایه‌های Evidence/Completion/Citation gate برای سنجش و ردیابی انجام کار.
- تست‌های واحد مرتبط با evidence، routing، ایمنی mutation و سناریوهای وب در وضعیت سبز (55 passed).
- جداسازی ابزارهای وب read-only با ملاحظات امنیتی (SSRF hardening، محدودیت اندازه/نوع محتوا).

### نقاط ضعف / ریسک‌ها
- `agent.py` بسیار بزرگ و متمرکز (~13.4K خط)؛ نگهداری، بازبینی و تست‌پذیری را دشوار می‌کند.
- وابستگی‌ها عمدتاً در `.venv` و تنها بخشی در `requirements.txt` پین شده‌اند؛ بازتولیدپذیری محیط ناقص است.
- نبود `pyproject.toml`/بسته‌بندی استاندارد و نبود README مستند در ریشه (بر اساس بررسی انجام‌شده).
- Working Tree نامرتب (تغییرات commit نشده، بکاپ‌های متعدد، فایل‌های untracked)؛ ریسک از دست رفتن زمینه تغییرات و دشواری ریلیز.
- وابستگی به سرور مدل محلی (`MODEL_URL`) و نبود اطلاعات نسخه llama.cpp در مخزن؛ راه‌اندازی محیط ممکن است مبهم باشد.

## 7) پیشنهادهای قدم بعدی (به ترتیب اولویت)

1. **تثبیت و پاک‌سازی وضعیت مخزن (اولویت بالا):** دسته‌بندی تغییرات فعلی، حذف/انتقال بکاپ‌های پراکنده از ریشه، تصمیم درباره فایل‌های untracked (افزودن به Git یا `.gitignore`)، و ثبت وضعیت در کامیت‌های منطقی/کوچک بدون تغییر رفتاری.
2. **بازتولیدپذیری محیط:** تکمیل `requirements.txt` یا افزودن `pyproject.toml`/lock برای وابستگی‌های واقعی پروژه، مستندسازی نسخه Python/.venv و نحوه راه‌اندازی سرور مدل/llama.cpp (در صورت استفاده).
3. **کاهش پیچیدگی هسته:** برنامه Refactor تدریجی برای استخراج ماژول‌ها از `agent.py` (ابزارها، verify/journal، approval/rollback، لایه مدل) با حفظ رفتار فعلی و تکیه بر تست‌ها/selfcheckهای موجود.

---
این گزارش صرفاً بر اساس بررسی خواندنی ساختار، فایل‌ها، تست‌ها و وضعیت Git تهیه شده است؛ هیچ تغییری در کد پروژه اعمال نشده است.

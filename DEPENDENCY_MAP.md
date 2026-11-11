# نقشه وابستگی‌های پروژه (DEPENDENCY_MAP)

تاریخ تحلیل: 2026-11-11  
محدوده: `D:\AI\local-agent` (به‌جز `.venv`)  
هدف: تحلیل وابستگی‌ها و شناسایی وابستگی‌های چرخه‌ای **بدون هیچ تغییر در کد**.

## 1) وابستگی‌های `agent.py` به ماژول‌ها (داخلی پروژه)

منابع داخلی بررسی‌شده: `agents/*`, `memory/*`, `evidence/*`, `web_research.py`, `website_analyst.py`.

| ماژول/فایل | نوع import | خط(ها) در `agent.py` | عبارت import | توضیح |
|---|---|---:|---|---|
| `website_analyst` | تنبل (lazy) | 18, 19 | `from website_analyst import analyze_website as _phase_g_analyze` / `format_website_report as _phase_g_format` | داخل `try/except` در بالای فایل؛ عدم موفقیت import نباید شکست واردسازی ایجاد کند. |
| `agents.tools.file_tools` | مستقیم (top-level) | 25 | `from agents.tools.file_tools import *` | استخراج ابزارهای فایل؛ در زمان واردسازی `agent.py` بارگذاری می‌شود. |
| `agents.providers` | تنبل (lazy) | 2512 | `from agents import providers as _providers` | استفاده موضعی داخل تابع. |
| `agents.memory_adapter` | تنبل (lazy) | 6093 | `from agents.memory_adapter import build_memory_adapter` | سیم‌کشی حافظه بادوام (feature-gated/fail-safe). |
| `web_research` | تنبل (lazy) | 8995, 9008 | `import web_research as _wr` | داخل ابزارهای وب (`tool_web_search`, `tool_web_extract`). |
| `agents.completion_gate` | تنبل (lazy) | 9728 | `from agents.completion_gate import evaluate_completion` | ارزیابی تکمیل/شواهد. |
| `agents.orchestrator` | تنبل (lazy) | 13716 | `from agents.orchestrator import extract_agent_proposal` | استخراج proposal در مسیر حلقه. |
| `agents.core_adapter`, `agents.orchestrator` | تنبل (lazy) | 14290, 14291 | `from agents.core_adapter import build_core_adapter` / `from agents.orchestrator import SeniorAgent` | راه‌اندازی مسیر چندایجنتی. |

**خلاصه:** 1 import مستقیم (`agents.tools.file_tools`) و 8 import تنبل به ماژول‌های داخلی/وب تحلیل.

## 2) وابستگی‌های ماژول‌ها به `agent.py`

### 2.1 جدول وابستگی (واردسازی/استفاده مستقیم از `agent`)

| فایل | import از `agent` | نوع | خط(ها) | نمادها/توابع/متغیرهای استفاده‌شده از `agent` (`core.*`) |
|---|---|---|---:|---|
| `agents/tools/file_tools.py` | `import agent as core` | تنبل (داخل توابع) | 90, 116, 168, 306, 334, 387, 420 | `safe_path`, `is_stop_requested`, `remember_stop_blocked_action`, `EMERGENCY_STOP_MESSAGE`, `build_action_before_state`, `validate_written_file`, `record_action`, `ACTION_STATE_DONE`, `verify_state_from_bool`, `summarize_action_result`, `summarize_validation`, `ensure_write_target_is_not_directory`, `MAX_EXPLORE_ENTRIES`, `resolve_project_path`, `scan_directory_entries`, `relative_display`, `MAX_SEARCH_RESULTS`, `MAX_SEARCH_FILE_BYTES`, `IGNORED_DIR_NAMES`, `is_sensitive_read_target`, `is_probably_text_file`, `mask_sensitive_content`, `sensitive_read_refusal_text`, `MAX_TEXT_READ_BYTES`, `is_inside_project` |
| `web_research.py` | ندارد | — | — | — (در کد ذکر شده: وابستگی به `agent.py` ندارد) |
| `website_analyst.py` | ندارد | — | — | — |
| `memory/*` | ندارد | — | — | — (بسته حافظه مستقل از `agent.py`) |
| `evidence/*` | ندارد | — | — | — (مثلاً `evidence/ledger_temp.py` صراحتاً بدون import از `agent.py`) |
| سایر `agents/*.py` (غیر از `tools/file_tools.py`) | ندارد (import اجرایی) | — | — | فقط ارجاعات متنی/کامنت (مثل «never imports agent.py»). |

### 2.2 نکات تفسیری
- وابستگی اجرایی ماژول‌ها به `agent.py` عملاً در **یک نقطه متمرکز** است: `agents/tools/file_tools.py` که به‌صورت تنبل و در زمان فراخوانی توابع، `agent` را برای دسترسی به helperها/ثابت‌ها/سیاست‌های ایمنی و journal/verify وارد می‌کند.
- `web_research.py`, `website_analyst.py`, `memory/*`, `evidence/*` و اکثر فایل‌های `agents/*` import اجرایی از `agent.py` ندارند (بر اساس جستجو).

## 3) وابستگی‌های داخلی بین ماژول‌های هدف (غیر از `agent.py`)

| فایل | نوع | خط | import |
|---|---|---:|---|
| `agents/memory_adapter.py` | مستقیم | 26, 27 | `from memory.store import MemoryStore`, `from memory.sqlite_store import SQLiteMemoryStore` |
| `memory/__init__.py` | مستقیم | 19, 20, 21 | `from memory.store import ...`, `from memory.sqlite_store import ...`, `from memory.schema import ...` |
| `memory/sqlite_store.py` | مستقیم | 14, 15 | `from memory.store import ...`, `from memory.schema import ...` |

> وابستگی معکوس از `memory/*` یا `evidence/*` یا `web_research.py` یا `website_analyst.py` به `agents/*` مشاهده نشد.

## 4) وابستگی‌های چرخه‌ای (Circular Imports)

### چرخه شناسایی‌شده
- **`agent.py` → `agents.tools.file_tools` → `agent.py`** (چرخه در سطح ماژول/زمان اجرا، با شکستن توسط import تنبل)

جزئیات:
- `agent.py:25` (مستقیم): `from agents.tools.file_tools import *`
- `agents/tools/file_tools.py:90,116,168,306,334,387,420` (تنبل): `import agent as core` داخل توابع و استفاده از `core.*`

**اثر:** در زمان import شدن `agent.py`، `agents.tools.file_tools` بارگذاری می‌شود اما بلافاصله `agent` را import نمی‌کند؛ واردسازی `agent` فقط هنگام فراخوانی توابع فایل انجام می‌شود. این الگو معمولاً import چرخه‌ای را در بارگذاری اولیه کاهش می‌دهد، اما **تزویج زمان اجرا** بین هسته و ابزارهای فایل باقی می‌ماند.

### موارد بررسی‌شده بدون چرخه مستقیم
- `agent.py` ↔ `website_analyst` / `web_research`: تنها `agent.py` آن‌ها را (تنبل) وارد می‌کند؛ واردسازی معکوس یافت نشد.
- `agent.py` ↔ `memory/*` / `evidence/*`: واردسازی معکوس یافت نشد.
- بین ماژول‌های هدف (agents/memory/evidence/web_*): ساختار مشاهده‌شده درختی/یک‌سویه است (به‌ویژه `memory/*` و `agents/memory_adapter.py`).

## 5) پیشنهادها برای کاهش وابستگی‌ها (بدون تغییر کد)

1. **شکستن چرخه `agent.py` ↔ `agents.tools.file_tools`:** استخراج قراردادها/ابزارهای مشترک (مثل `safe_path`, `resolve_project_path`, `is_inside_project`, ماسک/سیاست خواندن حساس، ثابت‌های Explore/Search، پیام‌ها/ثابت‌های توقف) به ماژول‌های کم‌وابستگی (مثلاً `agents/tools/path_utils.py`, `agents/tools/safety.py`, `agents/tools/constants.py`) تا `file_tools` به `agent.py` وابسته نباشد.
2. **جداکردن وضعیت و گزارش write/diff:** انتقال stateهای `LAST_WRITE_RESULT`/`LAST_WRITE_MULTI_RESULT` و توابع diff/result به ماژول مستقل (یا بازگرداندن صریح نتایج) برای کاهش اتکا به globals مشترک با هسته.
3. **تزریق وابستگی‌ها به‌جای `import agent as core`:** پاس‌دادن callback/شیء context (برای `record_action`, `validate_written_file`, `build_action_before_state`, stop hooks) به توابع فایل در لایه بالاتر، تا ابزارهای فایل قابل تست و مستقل شوند.
4. **ادامه الگوی import تنبل برای وابستگی‌های سنگین/اختیاری:** `web_research` و `website_analyst` از قبل تنبل هستند؛ حفظ این الگو برای سایر ادغام‌ها ریسک coupling زمان واردسازی را کم می‌کند.
5. **مرزبندی صریح API هسته:** تعریف یک façade کوچک و پایدار (مثلاً ماژول `core_api`/`contracts`) برای توابعی که ابزارها واقعاً نیاز دارند، به‌جای تکیه مستقیم به نمادهای پراکنده در `agent.py` بزرگ.

---
**روش تحلیل:** جستجوی الگوهای import/ارجاع (`from|import` داخلی، `agent.*`, `import agent`) با اسکریپت‌های خواندنی Python روی فایل‌های متنی پروژه. هیچ تغییری در کد اعمال نشده است.

"""
Shared utilities extracted from agent.py to break circular imports.
Used by agents.tools.file_tools and re-exported by agent.py.
"""
import os
import re
import json
import time
import hashlib
import shutil
import subprocess
import sys
import tempfile
import threading

# --- Paths / config ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- Sensitive read/content masking ---
SENSITIVE_KEYS = (
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "DB_HOST",
    "DB_CHARSET",
    "DB_COLLATE",
    "AUTH_KEY",
    "SECURE_AUTH_KEY",
    "LOGGED_IN_KEY",
    "NONCE_KEY",
    "AUTH_SALT",
    "SECURE_AUTH_SALT",
    "LOGGED_IN_SALT",
    "NONCE_SALT",
    "API_KEY",
    "SECRET_KEY",
    "ACCESS_TOKEN",
    "PASSWORD",
    "TOKEN",
)

SENSITIVE_READ_BASENAMES = frozenset({
    ".env",
    "wp-config.php",
    "credentials",
    "htpasswd",
    ".htpasswd",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "shadow",
    "passwd",
})

SENSITIVE_READ_SUFFIXES = (
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".ppk",
    ".crt",
    ".cer",
    ".der",
    ".asc",
    ".gpg",
    ".kdbx",
)

SENSITIVE_READ_PREFIXES = (".env",)

SENSITIVE_READ_ARTIFACT_SUFFIXES = (
    ".bak", ".backup", ".old", ".orig", ".save", ".swp",
    ".swo", ".tmp", ".copy", "~", ".1", ".2",
)

def mask_sensitive_content(content):
    # فقط مقدار حساس Mask می‌شود؛
    # نام تنظیمات و ساختار فایل حفظ می‌شود.
    for key in SENSITIVE_KEYS:

        # الگوی define() وردپرس:
        # define( 'DB_NAME', 'lrn_db' );
        content = re.sub(
            r"(define\s*\(\s*['\"]" + key + r"['\"]\s*,\s*)"
            r"(['\"])([^'\"]*)\2",
            r"\1\2[REDACTED]\2",
            content,
            flags=re.IGNORECASE
        )

        # الگوی کلید-مقدار با کوتیشن یا آرایه:
        # 'DB_PASSWORD', '...'  یا  'DB_PASSWORD' => '...'
        content = re.sub(
            r"(['\"]" + key + r"['\"](?:\s*(?:[,:]|=>)\s*))"
            r"(['\"])([^'\"]*)\2",
            r"\1\2[REDACTED]\2",
            content,
            flags=re.IGNORECASE
        )

        content = re.sub(
            r"(?m)^(\s*" + key + r"\s*[=:]\s*).+$",
            r"\1[REDACTED]",
            content,
            flags=re.IGNORECASE
        )

    return content


# ---------------------------------------------------------
# Phase H — Sensitive Read Security Boundary
# ---------------------------------------------------------
#
# این یک Security Boundary قطعی است، نه یک دستور Prompt.
#
# پیش از این لایه، تنها دو مجموعه‌ی «رد کردن» وجود داشت
# (REVIEW_SKIP_BASENAMES / REVIEW_SKIP_SUFFIXES) که هر دو
# مخصوص رتبه‌بندی و انتخاب فایل بودند و روی محتوا اثری
# نداشتند. نتیجه این بود که read_file و search_in_files
# محتوای خامِ فایل‌های حاوی credential را به مدل می‌دادند
# و آن محتوا از راه observation به context مدل و سپس به
# chat history می‌رسید. دستور Prompt در agent.py تنها
# «خواندن این فایل‌ها ممنوع است» می‌گفت و هیچ چیزی آن را
# اجرا نمی‌کرد.
#
# قاعده‌ی قطعی: اگر مسیر در فهرست زیر باشد، محتوا هرگز
# برنمی‌گردد — نه به مدل، نه به observation، نه به
# حافظه‌ی موقت. فقط یک پیام شفاف برمی‌گردد.
#
# عمداً فقط «خواندن» مسدود می‌شود. مسدودکردن Write یک
# تصمیم جداگانه است و در محدوده‌ی این تغییر نیست.

SENSITIVE_READ_BASENAMES = frozenset({
    ".env",
    "wp-config.php",
    "credentials",
    "htpasswd",
    ".htpasswd",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "shadow",
    "passwd",
})

SENSITIVE_READ_SUFFIXES = (
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".ppk",
    ".crt",
    ".cer",
    ".der",
    ".asc",
    ".gpg",
    ".kdbx",
)

# پیشوند نام‌های حساس: .env، .env.local، .env.production، ...
SENSITIVE_READ_PREFIXES = (".env",)

# پسوندهایی که فایلِ حساس را با خود می‌برند: wp-config.php.bak،
# .env.old، wp-config.php~ و مشابه. بدون این، یک کپی پشتیبان
# راه دور زدن کامل denylist می‌شد.
SENSITIVE_READ_ARTIFACT_SUFFIXES = (
    ".bak", ".backup", ".old", ".orig", ".save", ".swp",
    ".swo", ".tmp", ".copy", "~", ".1", ".2",
)

def is_sensitive_read_target(path):
    """
    Fail-Closed تشخیص مقصد حساس برای Read/Search.

    ورودی می‌تواند نسبی، مطلق یا داخلی باشد؛ فقط نام فایل
    ملاک است و همیشه به حروف کوچک سنجیده می‌شود.

    خروجی:
        False  → خواندن این مسیر مجاز است.
        True   → محتوا نباید هرگز برگردد.
    """
    text = "" if path is None else str(path).replace("\\", "/")
    text = text.strip()

    if not text:
        return False

    name = text.rsplit("/", 1)[-1].strip().lower()

    if not name:
        return False

    if name in SENSITIVE_READ_BASENAMES:
        return True

    for prefix in SENSITIVE_READ_PREFIXES:
        if name == prefix or name.startswith(prefix + "."):
            return True

    for suffix in SENSITIVE_READ_SUFFIXES:
        if name.endswith(suffix):
            return True

    # فایل حساسِ پشتیبان/موقت هم حساس است. این حلقه عمداً
    # تکرار است تا نامِ پس از حذف پسوند هم دوباره سنجیده شود،
    # ولی محدود است تا روی نام‌های عادی حلقه بی‌نهایت نیفتد.
    for artifact in SENSITIVE_READ_ARTIFACT_SUFFIXES:
        if name.endswith(artifact):
            trimmed = name[:-len(artifact)].strip("._- ")

            if trimmed and trimmed != name:
                if is_sensitive_read_target(trimmed):
                    return True

    return False

def sensitive_read_refusal_text(path):
    # پیام مسدودسازی عمداً نام فایل و دلیل را می‌گوید تا مدل
    # به‌جای حدس‌زدن، مسیر درست را از کاربر بپرسد. هیچ بخشی
    # از محتوای فایل در این پیام افشا نمی‌شود.
    return (
        "خواندن «" + str(path) + "» مسدود است: این فایل "
        " credential یا کلید رمزگذاری نگه می‌دارد و محتوایش "
        "هرگز به مدل داده نمی‌شود. اگر واقعاً به آن نیاز "
        "داری، از کاربر بخواه مقدار موردنیاز را خودش بگوید."
    )

def safe_path(
    project_root,
    relative_path
):
    relative_path = (
        relative_path
        .strip()
        .strip('"')
        .strip("'")
    )

    relative_path = relative_path.replace(
        "/",
        os.sep
    )

    full_path = os.path.abspath(
        os.path.join(
            project_root,
            relative_path
        )
    )

    root = os.path.abspath(
        project_root
    )

    if os.path.commonpath(
        [full_path, root]
    ) != root:
        raise ValueError(
            "Path is outside the selected project."
        )

    return full_path


    global LAST_ROLLBACK_RESULT

    LAST_ROLLBACK_RESULT = dict(result)

def is_inside_project(project_root, full_path):
    # کنترل نهایی و مستقل از safe_path: مسیر واقعی
    # (با حل symlink) باید داخل Project Root باشد.
    try:
        root = os.path.realpath(os.path.abspath(project_root))
        target = os.path.realpath(os.path.abspath(full_path))

        return os.path.commonpath([target, root]) == root

    except (ValueError, OSError, TypeError):
        return False

def relative_display(project_root, full_path):
    # نمایش مسیر نسبی با / (یکنواخت و خوانا)
    rel = os.path.relpath(
        os.path.abspath(full_path),
        os.path.abspath(project_root)
    )

    return rel.replace(os.sep, "/")

def resolve_project_path(
    project_root,
    relative_path,
    allow_root=True
):
    # مسیر ورودی (نسبی یا مطلقِ داخل پروژه) به مسیر مطلق
    # داخل Project Root تبدیل می‌شود.
    # مسیر خارج از پروژه و ../ پذیرفته نمی‌شود.
    root = os.path.abspath(project_root)

    text = "" if relative_path is None else str(relative_path)
    text = text.strip().strip('"').strip("'")

    if text in ("", ".", "./", ".\\"):
        if not allow_root:
            raise ValueError("مسیر نسبی لازم است.")

        return root

    full_path = safe_path(root, text)

    if not is_inside_project(root, full_path):
        raise ValueError(
            "مسیر خارج از پروژه انتخاب‌شده است: " + text
        )

    return full_path

def is_probably_text_file(full_path):
    # فایل‌های متنی/کد با پسوند شناخته‌شده تشخیص داده
    # می‌شوند. فایل‌های بدون پسوند (Makefile، Dockerfile و ...)
    # هم متنی در نظر گرفته می‌شوند.
    extension = os.path.splitext(
        os.path.basename(full_path)
    )[1].lower()

    if not extension:
        return True

    return extension in TEXT_FILE_EXTENSIONS

def ensure_write_target_is_not_directory(
    full_path,
    relative_path
):
    # Fail-Closed: نوشتن روی یک مسیرِ پوشه همیشه رد می‌شود.
    #
    # بدون این گارد، os.replace روی ویندوز وقتی مقصد یک
    # پوشه‌ی موجود باشد با [WinError 5] Access is denied
    # شکست می‌خورد و خطای گنگ از موتور نوشتن به مدل می‌رسد.
    if not os.path.isdir(full_path):
        return

    raise ValueError(
        "مسیر مقصد یک پوشه است، نه فایل: "
        + str(relative_path)
    )

def scan_directory_entries(
    project_root,
    target,
    depth=1,
    include_ignored=False,
    max_entries=None
):
    if max_entries is None:
        max_entries = MAX_EXPLORE_ENTRIES
    # پیمایش واقعی دیسک (BFS) برای ساخت فهرست/درخت.
    entries = []
    truncated = False
    queue = [(target, 0)]

    while queue:
        dir_path, level = queue.pop(0)

        try:
            names = sorted(os.listdir(dir_path))

        except OSError:
            continue

        for name in names:
            if len(entries) >= max_entries:
                truncated = True
                break

            full_path = os.path.join(dir_path, name)
            is_dir = os.path.isdir(full_path)

            if is_dir and not include_ignored:
                if name in IGNORED_DIR_NAMES:
                    continue

            entry = {
                "name": name,
                "relative_path": relative_display(
                    project_root, full_path
                ),
                "type": "dir" if is_dir else "file",
                "depth": level,
                "size": None,
                "children_count": None,
            }

            if is_dir:
                try:
                    entry["children_count"] = len(
                        os.listdir(full_path)
                    )
                except OSError:
                    entry["children_count"] = None
            else:
                try:
                    entry["size"] = os.path.getsize(full_path)
                except OSError:
                    entry["size"] = None

            entries.append(entry)

            if is_dir and (level + 1) < depth:
                queue.append((full_path, level + 1))

        if truncated:
            break

    return entries, truncated


# --- Explore/Search constants ---
IGNORED_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".agent-multi-backup",
    ".agent-trash",
    ".idea",
    ".vscode",
}

TEXT_FILE_EXTENSIONS = (
    ".py", ".php", ".phtml", ".js", ".mjs", ".cjs", ".jsx",
    ".ts", ".tsx", ".json", ".html", ".htm", ".css", ".scss",
    ".less", ".md", ".markdown", ".txt", ".rst", ".yml",
    ".yaml", ".xml", ".ini", ".cfg", ".conf", ".env", ".sql",
    ".sh", ".bat", ".ps1", ".twig", ".vue", ".svelte", ".toml",
    ".go", ".rs", ".java", ".c", ".h", ".cpp", ".hpp", ".rb",
    ".cs", ".lua", ".htaccess",
)

MAX_EXPLORE_ENTRIES = 400
MAX_SEARCH_RESULTS = 80
MAX_TEXT_READ_BYTES = 300 * 1024
MAX_SEARCH_FILE_BYTES = 1024 * 1024

# --- Action/Journal/Validation constants ---
ACTION_CLASS_USER = "user_mutation"
ACTION_CLASS_RECOVERY = "recovery"
ACTION_CLASS_EXTERNAL = "external"
ACTION_CLASS_READ = "read"
ACTION_APPROVAL_NOT_REQUIRED = "not_required"
ACTION_STATE_PLANNED = "planned"
ACTION_STATE_RUNNING = "running"
ACTION_STATE_DONE = "done"
ACTION_STATE_FAILED = "failed"
ACTION_STATE_STOPPED = "stopped"
ACTION_VERIFY_UNKNOWN = "unknown"
ACTION_VERIFY_OK = "ok"
ACTION_VERIFY_FAILED = "failed"
ACTION_VERIFY_SKIPPED = "skipped"
ACTION_CONTENT_ARG_KEYS = (
    "content", "text", "body",
    "old", "old_text", "find", "search",
    "new", "new_text", "replace", "with",
)
ACTION_ARG_STRING_LIMIT = 120
ACTION_JOURNAL_LIMIT = 50
ACTION_CONTEXT_FIELDS = (
    "action_id", "created_at", "ended_at", "operation_class", "action_type",
    "tool", "args", "args_hash", "project", "project_root", "target",
    "allow_write", "approval_state", "validation_state", "backup_path",
    "before_state", "execution_state", "verify_state", "result_summary",
    "agent_id", "agent_type", "approval_level", "approval_policy",
)
ACTION_JOURNAL = []
ACTION_JOURNAL_FILE = os.path.join(BASE_DIR, "action-journal.json")
ACTION_JOURNAL_PERSIST_FIELDS = (
    "action_id", "action_type", "tool", "operation_class", "execution_state",
    "verify_state", "validation_state", "approval_state", "project",
    "project_root", "target", "backup_path", "before_state", "args",
    "args_hash", "created_at", "ended_at", "result_summary", "agent_id",
    "agent_type", "approval_level", "approval_policy",
)
_ACTION_ID_COUNTER = 0

PYTHON_FILE_EXTENSIONS = (".py",)
JSON_FILE_EXTENSIONS = (".json",)
JS_FILE_EXTENSIONS = (".js", ".mjs", ".cjs")
PHP_FILE_EXTENSIONS = (".php",)
SYNTAX_TIMEOUT_SECONDS = 30
VALIDATION_ERROR_LIMIT = 2000

EMERGENCY_STOP_EVENT = threading.Event()
EMERGENCY_STOP_MESSAGE = "Emergency Stop فعال است؛ عملیات اجرا نشد."

def _sanitize_journal_value(value, depth=0):
    # پاک‌سازی بازگشتی مقادیر برای persist:
    # رشته‌ها Mask + truncate، دیکشنری/لیست محدود.
    try:
        if value is None or isinstance(value, bool):
            return value

        if isinstance(value, (int, float)):
            return value

        if isinstance(value, str):
            text = mask_sensitive_content(value)

            if len(text) > ACTION_ARG_STRING_LIMIT:
                text = text[:ACTION_ARG_STRING_LIMIT] + "…"

            return text

        if depth >= 3:
            return "<" + type(value).__name__ + ">"

        if isinstance(value, dict):
            out = {}

            for key in list(value.keys())[:20]:
                out[str(key)[:64]] = _sanitize_journal_value(
                    value[key], depth + 1
                )

            return out

        if isinstance(value, (list, tuple)):
            return [
                _sanitize_journal_value(item, depth + 1)
                for item in list(value)[:10]
            ]

        return "<" + type(value).__name__ + ">"
    except Exception:
        return None

def _sanitize_journal_entry(entry):
    # فقط metadata مجاز؛ همه‌چیز دوباره sanitize می‌شود تا حتی اگر
    # entry حافظه آلوده باشد، چیزی حساس روی دیسک نرود.
    try:
        if not isinstance(entry, dict):
            return None

        clean = {}

        for field in ACTION_JOURNAL_PERSIST_FIELDS:
            if field in entry:
                clean[field] = _sanitize_journal_value(entry[field])

        if not isinstance(clean.get("action_id"), str):
            return None

        return clean
    except Exception:
        return None

def _persist_action_journal_best_effort():
    # ذخیره atomic کل Journal؛ هرگز exception نمی‌دهد.
    try:
        clean = []

        for entry in ACTION_JOURNAL:
            item = _sanitize_journal_entry(entry)

            if item is not None:
                clean.append(item)

        clean = clean[-ACTION_JOURNAL_LIMIT:]

        temp_path = ACTION_JOURNAL_FILE + ".tmp"

        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False)
            f.flush()

            try:
                os.fsync(f.fileno())
            except Exception:
                pass

        os.replace(temp_path, ACTION_JOURNAL_FILE)
    except Exception:
        try:
            os.remove(ACTION_JOURNAL_FILE + ".tmp")
        except OSError:
            pass
    except BaseException:
        pass

def _load_action_journal_best_effort():
    # بارگذاری Fail Closed هنگام startup:
    # فایل خراب => Journal خالی، بدون crash، بدون اجرای خودکار.
    global _ACTION_ID_COUNTER

    try:
        if not os.path.isfile(ACTION_JOURNAL_FILE):
            return 0

        with open(ACTION_JOURNAL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            ACTION_JOURNAL.clear()
            return 0

        loaded = []

        for item in data:
            clean = _sanitize_journal_entry(item)

            if clean is not None:
                loaded.append(clean)

        loaded = loaded[-ACTION_JOURNAL_LIMIT:]

        ACTION_JOURNAL.clear()
        ACTION_JOURNAL.extend(loaded)

        # جلوگیری از تکرار action_id بعد از restart.
        try:
            peak = _ACTION_ID_COUNTER

            for item in loaded:
                aid = str(item.get("action_id") or "")

                try:
                    tail = int(aid.rsplit("-", 1)[-1])

                    if tail > peak:
                        peak = tail
                except (ValueError, TypeError):
                    continue

            _ACTION_ID_COUNTER = peak
        except Exception:
            pass

        return len(loaded)
    except Exception:
        try:
            ACTION_JOURNAL.clear()
        except Exception:
            pass

        return 0
    except BaseException:
        try:
            ACTION_JOURNAL.clear()
        except Exception:
            pass

        return 0

_ACTION_ID_COUNTER = 0

def _new_action_id():
    global _ACTION_ID_COUNTER

    _ACTION_ID_COUNTER += 1

    return (
        "act-"
        + time.strftime("%Y%m%d-%H%M%S")
        + "-"
        + str(_ACTION_ID_COUNTER)
    )

def hash_action_args(args):
    # هش آرگومان‌ها برای تشخیص تغییر آن‌ها بعد از Plan.
    # محتوای خام نگه داشته نمی‌شود؛ فقط هش.
    try:
        import hashlib

        payload = json.dumps(
            args if isinstance(args, dict) else {},
            sort_keys=True,
            ensure_ascii=False,
            default=str
        )

        return hashlib.sha1(
            payload.encode("utf-8")
        ).hexdigest()

    except (TypeError, ValueError):
        return None

def summarize_action_args(args):
    # خلاصه امن آرگومان‌ها برای Journal:
    # - مقدارهای محتوایی فقط به شکل "<N chars>"
    # - مقدارهای متنی کوتاه (Mask-شده) حداکثر ۱۲۰ کاراکتر
    # - لیست و سایر نوع‌ها فقط به شکل خلاصه
    if not isinstance(args, dict):
        return None

    summary = {}

    for key in args:
        value = args[key]
        key_text = str(key).lower()

        if isinstance(value, str):

            if key_text in ACTION_CONTENT_ARG_KEYS:
                summary[key] = "<" + str(len(value)) + " chars>"
            else:
                text = mask_sensitive_content(value)

                if len(text) > ACTION_ARG_STRING_LIMIT:
                    text = (
                        text[:ACTION_ARG_STRING_LIMIT] + "…"
                    )

                summary[key] = text

        elif value is None or isinstance(value, bool):
            summary[key] = value

        elif isinstance(value, (int, float)):
            summary[key] = value

        elif isinstance(value, (list, tuple)):
            items = []

            for item in list(value)[:5]:

                if isinstance(item, str):
                    items.append(
                        mask_sensitive_content(item)[
                            :ACTION_ARG_STRING_LIMIT
                        ]
                    )
                elif (
                    item is None
                    or isinstance(item, (int, float, bool))
                ):
                    items.append(item)
                else:
                    items.append(
                        "<" + type(item).__name__ + ">"
                    )

            if len(value) > 5:
                items.append("…")

            summary[key] = items

        else:
            summary[key] = "<" + type(value).__name__ + ">"

    return summary

def summarize_action_result(result, limit=300):
    # خلاصه متنی کوتاه، Mask-شده و تک‌خطی برای Audit.
    if result is None:
        return None

    try:
        if isinstance(result, dict):
            text = result.get("text")

            if text is None:
                text = result.get("error")

            if text is None:
                text = json.dumps(
                    result, ensure_ascii=False, default=str
                )

        elif isinstance(result, (list, tuple)):
            text = " | ".join(str(item) for item in result)

        else:
            text = str(result)

        text = mask_sensitive_content(text)
        text = " ".join(text.split())

        if len(text) > limit:
            text = text[:limit] + "…"

        return text

    except Exception:
        return None



def sha1_of_file(path):
    import hashlib
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()

def build_action_before_state(full_path, extra=None):
    # فقط متادیتای وضعیت قبلی؛ محتوا نگه داشته نمی‌شود.
    state = {"existed_before": False}

    try:
        if full_path and os.path.isdir(full_path):
            state["existed_before"] = True
            state["type"] = "dir"

        elif full_path and os.path.isfile(full_path):
            state["existed_before"] = True
            state["type"] = "file"
            state["size"] = os.path.getsize(full_path)
            state["sha1"] = sha1_of_file(full_path)

    except OSError:
        pass

    if isinstance(extra, dict):
        state.update(extra)

    return state

def verify_state_from_bool(value):
    # تبدیل نتیجه بولی Verify به وضعیت رشته‌ای استاندارد.
    if value is True:
        return ACTION_VERIFY_OK

    if value is False:
        return ACTION_VERIFY_FAILED

    return ACTION_VERIFY_SKIPPED

def new_action_context(
    action_type,
    tool=None,
    args=None,
    project=None,
    project_root=None,
    target=None,
    allow_write=False,
    operation_class=ACTION_CLASS_USER,
    approval_state=ACTION_APPROVAL_NOT_REQUIRED,
    agent_id=None,
    agent_type=None,
    approval_level=None,
    approval_policy=None
):
    # ساخت Action Context سبک؛ همه فیلدها JSON-serializable.
    return {
        "action_id": _new_action_id(),
        "created_at": time.time(),
        "ended_at": None,
        "operation_class": operation_class,
        "action_type": action_type,
        "tool": tool if tool is not None else action_type,
        "args": summarize_action_args(args),
        "args_hash": hash_action_args(args),
        "project": project,
        "project_root": (
            os.path.abspath(project_root)
            if project_root else None
        ),
        "target": target,
        "allow_write": bool(allow_write),
        "approval_state": approval_state,
        "validation_state": None,
        "backup_path": None,
        "before_state": None,
        "execution_state": ACTION_STATE_PLANNED,
        "verify_state": None,
        "result_summary": None,
        "agent_id": agent_id,
        "agent_type": agent_type,
        "approval_level": approval_level,
        "approval_policy": approval_policy,
    }

def update_action_context(context, **fields):
    # به‌روزرسانی امن Context (فقط فیلدهای شناخته‌شده).
    if not isinstance(context, dict):
        return context

    for key in fields:

        if key in ACTION_CONTEXT_FIELDS:
            context[key] = fields[key]

    return context

def remember_action(context):
    # ثبت passive یک Action در Journal.
    # این تابع هرگز استثنا پرتاب نمی‌کند و هیچ عملیاتی را
    # متوقف نمی‌کند.
    try:
        if not isinstance(context, dict):
            return None

        if context.get("ended_at") is None:
            context["ended_at"] = time.time()

        # Snapshot مستقل تا تغییرهای بعدی Context روی Journal
        # اثر نگذارد.
        try:
            entry = json.loads(
                json.dumps(context, default=str)
            )

        except (TypeError, ValueError):
            entry = dict(context)

        ACTION_JOURNAL.append(entry)

        while len(ACTION_JOURNAL) > ACTION_JOURNAL_LIMIT:
            ACTION_JOURNAL.pop(0)

        # Phase F2: اتصال persistence — best-effort، هرگز بلاک نمی‌کند.
        try:
            _persist_action_journal_best_effort()
        except Exception:
            pass
        except BaseException:
            pass

        return entry.get("action_id")

    except Exception:
        return None

def get_action_journal(limit=None):
    # تاریخچه Actionها (قدیمی → جدید) به شکل کپی.
    entries = [
        dict(entry) for entry in ACTION_JOURNAL
    ]

    if isinstance(limit, int) and limit > 0:
        entries = entries[-limit:]

    return entries

def get_last_action():
    # آخرین Action ثبت‌شده (کپی) یا None.
    if not ACTION_JOURNAL:
        return None

    return dict(ACTION_JOURNAL[-1])

def get_last_mutating_action():
    # آخرین Action تغییردهنده/بیرونی (پایه Undo در فاز بعد).
    # فقط Actionهای واقعاً اجرا/تکمیل‌شده (done) کاندید Undo هستند؛
    # ورودی‌های stopped/failed/expired نباید Undo را مسموم کنند.
    # Phase E: بدون تغییر رفتار برای mutationهای done؛ فقط فیلتر
    # execution_state اضافه شد تا رکوردهای مسدودشده (Stop/failed)
    # یا رکوردهای undo ناموفق، Undo بعدی را خراب نکنند.
    for entry in reversed(ACTION_JOURNAL):

        if entry.get("operation_class") in (
            ACTION_CLASS_USER,
            ACTION_CLASS_EXTERNAL
        ):
            if entry.get("execution_state") != ACTION_STATE_DONE:
                continue
            return dict(entry)

    return None

def is_action_undone(action_id):
    # Phase F1: آیا این Action قبلاً با Undo موفق برگردانده شده؟
    # فقط رکورد undo با execution_state=done ملاک است؛
    # رکوردهای blocked/failed چیزی را undone علامت نمی‌زنند.
    try:
        if not action_id:
            return False

        for entry in ACTION_JOURNAL:
            try:
                if entry.get("action_type") != "undo":
                    continue

                if entry.get("execution_state") != ACTION_STATE_DONE:
                    continue

                before = entry.get("before_state")

                if isinstance(before, dict) and before.get("undone_action") == action_id:
                    return True

                args = entry.get("args")

                if isinstance(args, dict) and args.get("action_id") == action_id:
                    return True
            except Exception:
                continue

        return False
    except Exception:
        return False

def find_action(action_id):
    # جستجوی Action با شناسه (Journal محدود است؛ ارزان).
    for entry in reversed(ACTION_JOURNAL):

        if entry.get("action_id") == action_id:
            return dict(entry)

    return None

def clear_action_journal(persist=True):
    # پاک‌کردن Journal (برای تست/ابزارهای آینده).
    # Phase F2: به‌طور پیش‌فرض فایل persist هم پاک می‌شود تا تست‌ها
    # ایزوله بمانند؛ در خطاهای سیستمی persist متوقف نمی‌شود.
    ACTION_JOURNAL.clear()

    if persist:
        try:
            _persist_action_journal_best_effort()
        except Exception:
            pass
        except BaseException:
            pass


# Phase F2: load هنگام import — Fail Closed، بدون اجرای خودکار.
try:
    _load_action_journal_best_effort()
except Exception:
    pass
except BaseException:
    pass

def record_action(
    action_type,
    operation_class=ACTION_CLASS_USER,
    tool=None,
    args=None,
    project=None,
    project_root=None,
    target=None,
    allow_write=True,
    approval_state=ACTION_APPROVAL_NOT_REQUIRED,
    validation_state=None,
    backup_path=None,
    before_state=None,
    execution_state=ACTION_STATE_DONE,
    verify_state=None,
    result_summary=None,
    agent_id=None,
    agent_type=None,
    approval_level=None,
    approval_policy=None
):
    # ساخت + تکمیل + ثبت یک Action در یک فراخوانی.
    # هر خطایی بی‌صدا نادیده گرفته می‌شود تا Journal هرگز
    # عملیات اصلی را متوقف نکند.
    try:
        context = new_action_context(
            action_type,
            tool=tool,
            args=args,
            project=project,
            project_root=project_root,
            target=target,
            allow_write=allow_write,
            operation_class=operation_class,
            approval_state=approval_state,
            agent_id=agent_id,
            agent_type=agent_type,
            approval_level=approval_level,
            approval_policy=approval_policy,
        )

        update_action_context(
            context,
            validation_state=validation_state,
            backup_path=backup_path,
            before_state=before_state,
            execution_state=execution_state,
            verify_state=verify_state,
            result_summary=result_summary,
            ended_at=time.time(),
        )

        return remember_action(context)

    except Exception:
        return None


# ---------------------------------------------------------
# Validation بعد از Write
# ---------------------------------------------------------

# پسوندهایی که برای آن‌ها Syntax پایتون بررسی می‌شود.
PYTHON_FILE_EXTENSIONS = (".py",)

# بررسی Syntax برای این پسوندها بدون هیچ ابزار بیرونی
# انجام می‌شود (json).
JSON_FILE_EXTENSIONS = (".json",)

# بررسی Syntax برای این پسوندها فقط اگر ابزار مربوطه در
# PATH موجود باشد؛ در غیر این صورت فقط «بررسی‌نشده» گزارش
# می‌شود (بدون خطا).
JS_FILE_EXTENSIONS = (".js", ".mjs", ".cjs")

PHP_FILE_EXTENSIONS = (".php",)

# حداکثر زمان بررسی Syntax (ثانیه).
SYNTAX_TIMEOUT_SECONDS = 30

# حداکثر طول متن خطا که گزارش می‌شود.
VALIDATION_ERROR_LIMIT = 2000

# ---------------------------------------------------------
# Step 3 — Verify بعد از Mutation + Error Feedback
# ---------------------------------------------------------
#
# این لایه هیچ چیز جدیدی را «مالک» نمی‌شود: همان
# validate_written_file / check_file_syntax موجود را می‌خواند و
# نتیجه را ساختاریافته به مدل برمی‌گرداند. هیچ فایلی تغییر
# نمی‌کند، هیچ Rollback/Approval ای را دور نمی‌زند و هیچ
# ابزار بیرونی نصب یا اجرا نمی‌کند.

# حداکثر طول پیام خطای هر فایل در Feedback.
VERIFY_ERROR_CHARS = 400

# حداکثر طول کل Feedback که به مدل داده می‌شود.
VERIFY_FEEDBACK_CHARS = 1200

# حداکثر تعداد خطایی که در Feedback فهرست می‌شود.
VERIFY_MAX_ERRORS = 5

# سقف context ای که برای «یک فرصت اصلاح» به مدل داده می‌شود.
# بزرگ‌تر از این، اصلاح انجام نمی‌شود (Fail Closed) تا هرگز
# محتوای ناقص به مدل داده نشود و فایل ناقص نوشته نشود.
MAX_CORRECTION_FILES = 5
MAX_CORRECTION_CONTEXT_CHARS = 24000

# انواع خطای قابل‌تشخیص در Verify.
VERIFY_TYPE_SYNTAX = "syntax_error"
VERIFY_TYPE_MISSING = "missing_file"
VERIFY_TYPE_CONTENT = "content_mismatch"
VERIFY_TYPE_BOUNDARY = "boundary"
VERIFY_TYPE_MUTATION = "mutation_failed"
VERIFY_TYPE_OTHER = "verify_error"

# استخراج line/column از متن خطای Syntax.
VERIFY_LINE_PATTERN = re.compile(r"\bline\s+(\d+)", re.IGNORECASE)
VERIFY_COLUMN_PATTERN = re.compile(r"\bcolumn\s+(\d+)", re.IGNORECASE)

def check_python_syntax(full_path):
    # Syntax فقط با py_compile و با همان مفسر پایتونی
    # بررسی می‌شود که Local Agent با آن اجرا شده است.
    #
    # امنیت:
    # - shell=True استفاده نمی‌شود.
    # - آرگومان‌ها ثابت‌اند و مسیر فایل قبلاً با
    #   safe_path کنترل شده است.
    # - هیچ دستور یا مسیر دلخواه کاربر اجرا نمی‌شود.
    #
    # py_compile به‌طور پیش‌فرض __pycache__ می‌سازد؛
    # برای اینکه Validation هیچ فایلی در پروژه تغییر
    # ندهد، کش بایت‌کد به یک پوشه موقت هدایت می‌شود و
    # در پایان خودکار پاک می‌شود.
    interpreter = sys.executable

    if not interpreter:
        return {
            "checked": False,
            "valid": None,
            "error": (
                "مفسر پایتون پیدا نشد؛ "
                "بررسی Syntax انجام نشد."
            )
        }

    try:

        with tempfile.TemporaryDirectory(
            prefix="local_agent_syntax_"
        ) as cache_dir:

            environment = os.environ.copy()
            environment["PYTHONPYCACHEPREFIX"] = cache_dir

            completed = subprocess.run(
                [
                    interpreter,
                    "-B",
                    "-m",
                    "py_compile",
                    full_path
                ],
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=SYNTAX_TIMEOUT_SECONDS
            )

    except subprocess.TimeoutExpired:

        return {
            "checked": True,
            "valid": False,
            "error": (
                "بررسی Syntax در "
                + str(SYNTAX_TIMEOUT_SECONDS)
                + " ثانیه تمام نشد؛ "
                "نتیجه‌ای گزارش نشد."
            )
        }

    except OSError as e:

        return {
            "checked": False,
            "valid": None,
            "error": (
                "اجرای بررسی Syntax ممکن نشد: "
                + str(e)
            )
        }

    if completed.returncode == 0:
        return {
            "checked": True,
            "valid": True,
            "error": None
        }

    message = (completed.stderr or "").strip()

    if not message:
        message = (
            "py_compile با کد خروج "
            + str(completed.returncode)
            + " پایان یافت."
        )

    # متن خطا به‌صورت امن گزارش می‌شود:
    # مقادیر حساس Mask می‌شوند و طول متن محدود است.
    message = mask_sensitive_content(message)

    if len(message) > VALIDATION_ERROR_LIMIT:
        message = (
            message[:VALIDATION_ERROR_LIMIT]
            + "\n…"
        )

    return {
        "checked": True,
        "valid": False,
        "error": (
            "خطای Syntax در فایل پایتون:\n"
            + message
        )
    }

def check_json_syntax(full_path):
    # بررسی JSON بدون هیچ ابزار بیرونی.
    # مثل check_python_syntax فقط گزارش می‌دهد و
    # هیچ فایلی را تغییر نمی‌دهد.
    try:
        with open(
            full_path,
            "r",
            encoding="utf-8",
            newline=""
        ) as f:
            json.load(f)

    except (OSError, UnicodeDecodeError) as e:

        return {
            "checked": False,
            "valid": None,
            "error": (
                "محتوای فایل JSON قابل خواندن نیست: "
                + str(e)
            )
        }

    except json.JSONDecodeError as e:

        return {
            "checked": True,
            "valid": False,
            "error": (
                "خطای Syntax در فایل JSON:\n"
                + str(e)
            )
        }

    return {
        "checked": True,
        "valid": True,
        "error": None
    }

def check_external_syntax(full_path, tool, args):
    # بررسی Syntax با ابزاری که ممکن است روی سیستم
    # نصب نباشد (node / php). اگر ابزار در PATH پیدا
    # نشود، بررسی «انجام‌نشده» گزارش می‌شود و خطایی
    # ساخته نمی‌شود.
    executable = shutil.which(tool)

    if not executable:
        return {
            "checked": False,
            "valid": None,
            "error": None
        }

    try:
        completed = subprocess.run(
            [executable] + list(args) + [full_path],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SYNTAX_TIMEOUT_SECONDS
        )

    except subprocess.TimeoutExpired:

        return {
            "checked": True,
            "valid": False,
            "error": (
                "بررسی Syntax با "
                + tool
                + " در "
                + str(SYNTAX_TIMEOUT_SECONDS)
                + " ثانیه تمام نشد."
            )
        }

    except OSError as e:

        return {
            "checked": False,
            "valid": None,
            "error": (
                "اجرای بررسی Syntax ممکن نشد: "
                + str(e)
            )
        }

    if completed.returncode == 0:
        return {
            "checked": True,
            "valid": True,
            "error": None
        }

    message = (
        (completed.stderr or "")
        + (completed.stdout or "")
    ).strip()

    if not message:
        message = (
            tool
            + " با کد خروج "
            + str(completed.returncode)
            + " پایان یافت."
        )

    message = mask_sensitive_content(message)

    if len(message) > VALIDATION_ERROR_LIMIT:
        message = message[:VALIDATION_ERROR_LIMIT] + "\n…"

    return {
        "checked": True,
        "valid": False,
        "error": (
            "خطای Syntax (" + tool + "):\n" + message
        )
    }

def check_file_syntax(full_path, relative_path=None):
    # انتخاب نوع بررسی Syntax بر اساس نوع فایل.
    # عمومی است و به نوع پروژه وابسته نیست.
    name = relative_path or full_path

    extension = os.path.splitext(
        str(name)
    )[1].lower()

    if extension in PYTHON_FILE_EXTENSIONS:
        return check_python_syntax(full_path)

    if extension in JSON_FILE_EXTENSIONS:
        return check_json_syntax(full_path)

    if extension in JS_FILE_EXTENSIONS:
        return check_external_syntax(
            full_path,
            "node",
            ["--check"]
        )

    if extension in PHP_FILE_EXTENSIONS:
        return check_external_syntax(
            full_path,
            "php",
            ["-l"]
        )

    return {
        "checked": False,
        "valid": None,
        "error": None
    }

def validate_written_file(
    project_root,
    relative_path,
    expected_content,
    full_path=None
):
    # Validation بعد از Write موفق اجرا می‌شود.
    # این تابع فقط بررسی می‌کند:
    # - هیچ فایلی را تغییر نمی‌دهد.
    # - هیچ Rollback خودکاری انجام نمی‌دهد.
    # - هیچ استثنایی به بیرون پرتاب نمی‌کند؛
    #   نتیجه ساختاریافته برمی‌گرداند.
    result = {
        "valid": False,
        "exists": False,
        "size": None,
        "content_matches": False,
        "syntax_checked": False,
        "syntax_valid": None,
        "error": None
    }

    # مسیر فقط با همان safe_path کنترل می‌شود.
    # Validation هیچ استثنایی به بیرون نمی‌دهد؛ حتی اگر
    # ورودی نامعتبر باشد فقط خطا گزارش می‌شود.
    try:
        checked_path = safe_path(
            project_root,
            relative_path
        )

    except (ValueError, TypeError) as e:

        result["error"] = (
            "Validation انجام نشد: "
            + str(e)
        )

        return result

    # مسیر بررسی‌شده باید همان مسیر نوشته‌شده باشد.
    if (
        full_path is not None
        and os.path.abspath(full_path) != checked_path
    ):
        result["error"] = (
            "Validation انجام نشد: مسیر بررسی‌شده "
            "با مسیر نوشته‌شده یکسان نیست."
        )

        return result

    # فایل باید واقعاً وجود داشته باشد.
    if not os.path.isfile(checked_path):
        result["error"] = (
            "فایل بعد از Write پیدا نشد: "
            + relative_path
        )

        return result

    result["exists"] = True

    # مسیر باید داخل project root باشد.
    root = os.path.abspath(project_root)

    if os.path.commonpath(
        [checked_path, root]
    ) != root:
        result["error"] = (
            "مسیر فایل داخل پروژه "
            "انتخاب‌شده نیست."
        )

        return result

    # اندازه فایل باید قابل خواندن باشد.
    try:
        result["size"] = os.path.getsize(
            checked_path
        )

    except OSError as e:

        result["error"] = (
            "اندازه فایل قابل خواندن نیست: "
            + str(e)
        )

        return result

    # محتوای نوشته‌شده با محتوایی که قرار بوده
    # Write شود مقایسه می‌شود (با همان تنظیمات Write).
    try:
        with open(
            checked_path,
            "r",
            encoding="utf-8",
            newline=""
        ) as f:
            content = f.read()

    except (OSError, UnicodeDecodeError) as e:

        result["error"] = (
            "محتوای فایل قابل خواندن نیست: "
            + str(e)
        )

        return result

    result["content_matches"] = (
        content == expected_content
    )

    if not result["content_matches"]:
        result["error"] = (
            "محتوای فایل با محتوایی که قرار بود "
            "نوشته شود مطابقت ندارد."
        )

    # بررسی Syntax بر اساس نوع فایل:
    # - .py  → py_compile (همیشه در دسترس)
    # - .json → json.load (بدون ابزار بیرونی)
    # - .js / .php → فقط اگر node/php در PATH باشد
    # برای سایر فایل‌ها فقط Validation عمومی انجام
    # می‌شود و هیچ بررسی محتوایی سنگینی نیست.
    syntax = check_file_syntax(
        checked_path,
        relative_path
    )

    result["syntax_checked"] = syntax["checked"]
    result["syntax_valid"] = syntax["valid"]

    if (
        syntax["error"]
        and result["error"] is None
    ):
        result["error"] = syntax["error"]

    result["valid"] = (
        result["exists"]
        and result["content_matches"]
        and (
            not result["syntax_checked"]
            or result["syntax_valid"] is True
        )
    )

    return result

def summarize_validation(validation):
    # خلاصه واقعی نتیجه Verify بعد از Write/Edit.
    if not validation:
        return ["Validation: انجام نشد."]

    lines = [
        "خواندن مجدد از دیسک: انجام شد",
        "وجود فایل: "
        + ("بله" if validation.get("exists") else "خیر"),
        "اندازه: " + str(validation.get("size")),
        "تطابق محتوا با آنچه نوشته شد: "
        + (
            "بله"
            if validation.get("content_matches")
            else "خیر"
        ),
    ]

    if validation.get("syntax_checked"):
        lines.append(
            "بررسی Syntax: "
            + (
                "معتبر"
                if validation.get("syntax_valid")
                else "نامعتبر"
            )
        )
    else:
        lines.append(
            "بررسی Syntax: برای این نوع فایل انجام نشد"
        )

    if validation.get("error"):
        lines.append("خطا: " + str(validation["error"]))

    return lines

def is_stop_requested(stop_event=None):
    # بررسی مشترک: منبع مرکزی + stop_event فعلی.
    if is_emergency_stop_requested():
        return True

    if stop_event is not None and stop_event.is_set():
        return True

    return False

def remember_stop_blocked_action(
    tool=None,
    action_type=None,
    args=None,
    project=None,
    project_root=None,
    target=None,
    context=None,
    approval_state=ACTION_APPROVAL_NOT_REQUIRED
):
    # ثبت «توقف قبل از اجرا» در Action Journal.
    # اگر context یک Plan در انتظار تأیید داده شود، همان
    # action_id حفظ می‌شود. هیچ فایلی تغییر نمی‌کند و هیچ
    # خطایی به بیرون منتشر نمی‌شود.
    try:
        if isinstance(context, dict):
            stopped = context

        else:
            stopped = new_action_context(
                action_type or str(tool),
                tool=tool,
                args=args,
                project=project,
                project_root=project_root,
                target=target,
                allow_write=True,
                approval_state=approval_state,
            )

        update_action_context(
            stopped,
            execution_state=ACTION_STATE_STOPPED,
            verify_state=ACTION_VERIFY_SKIPPED,
            result_summary=summarize_action_result(
                "emergency stop blocked: " + str(tool)
            ),
            ended_at=time.time(),
        )

        return remember_action(stopped)

    except Exception:
        return None


# Phase F2: load هنگام import — Fail Closed، بدون اجرای خودکار.
try:
    _load_action_journal_best_effort()
except Exception:
    pass
except BaseException:
    pass

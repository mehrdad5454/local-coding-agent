import os
import re
import json
import time
import fnmatch
import difflib
import subprocess
import sys
import tempfile
import shutil
import threading
import urllib.request
import urllib.error

# Phase G: Website Analyst (read-only) - additive import only.
# Core A-F untouched; failure here must never break agent import.
try:
    from website_analyst import analyze_website as _phase_g_analyze
    from website_analyst import format_website_report as _phase_g_format
except Exception:
    _phase_g_analyze = None
    _phase_g_format = None

from agents.core_utils import *

# Extracted file tools (read/write/edit/list/search/backup)
from agents.tools.file_tools import *

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_FILE = os.path.join(BASE_DIR, "projects.json")

# General Mode executable workspace (fixed, isolated sandbox).
#
# Text-only requests keep the old behavior. Executive requests in
# General Mode run inside this directory only, through the same
# Safety Pipeline as normal projects. The path is constant and is
# never taken from user input.
GENERAL_WORKSPACE_DIR = os.path.join(BASE_DIR, "general_workspace")

DEFAULT_PROJECTS = {
    "agent-test": r"D:\AI\agent-test"
}

import os as _os
MODEL_URL = _os.environ.get("MODEL_URL", "http://127.0.0.1:8080/completion")

# Phase 2: Multi-Agent feature flag.
# When False (default), Local Agent behaves exactly as before.
# When True, requests are routed through SeniorAgent orchestrator
# instead of the legacy/run_agentic_loop path.
# Phase 7-A: explicit opt-in via the LOCAL_AGENT_MULTI_AGENT env var
# ("1"/"true"/"yes"/"on"). Unset or any other value keeps the flag
# False, so the default runtime path is unchanged.
use_multi_agent = (
    os.environ.get("LOCAL_AGENT_MULTI_AGENT", "")
    .strip()
    .lower()
    in ("1", "true", "yes", "on")
)

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


def _load_projects_document():
    # کل فایل projects.json، شامل کلید رزرو‌شده‌ی approval mode.
    # فقط لایه‌ی داخلی از این استفاده می‌کند.
    if not os.path.isfile(PROJECTS_FILE):
        return dict(DEFAULT_PROJECTS)

    try:
        with open(
            PROJECTS_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError()

        return data

    except Exception:
        return DEFAULT_PROJECTS.copy()


def load_projects():
    # فقط پروژه‌های واقعی برگردانده می‌شود (name -> path).
    # کلید رزرو‌شده‌ی approval mode عمداً پنهان است تا هیچ
    # مصرف‌کننده‌ی فعلی (UI، Routeها، re-validation) آن را
    # به‌عنوان یک پروژه نبیند.
    document = _load_projects_document()

    if not os.path.isfile(PROJECTS_FILE):
        # رفتار اولیه حفظ می‌شود: در اولین اجرا فایل ساخته می‌شود.
        save_projects(document)

    return {
        name: value
        for name, value in document.items()
        if name != PROJECTS_APPROVAL_KEY
    }


def save_projects(projects):
    # فقط پروژه‌های واقعی نوشته می‌شوند. کلید رزرو‌شده‌ی approval
    # mode از ورودی دور ریخته می‌شود تا از مسیر عمومی (افزودن/حذف
    # پروژه) کسی نتواند آن را بازنویسی کند.
    payload = {
        name: value
        for name, value in (projects or {}).items()
        if name != PROJECTS_APPROVAL_KEY
    }

    with open(
        PROJECTS_FILE,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            payload,
            f,
            ensure_ascii=False,
            indent=2
        )


def save_projects_document(document):
    """
    Write the full projects.json document, including approval modes.

    Used ONLY by set_project_approval_mode. Saving through this function
    keeps the project paths already on disk and merges the given approval
    modes into them.
    """
    document = dict(document or {})

    payload = {
        name: value
        for name, value in document.items()
        if name != PROJECTS_APPROVAL_KEY
    }

    modes = document.get(PROJECTS_APPROVAL_KEY)

    if isinstance(modes, dict) and modes:
        payload[PROJECTS_APPROVAL_KEY] = modes

    with open(
        PROJECTS_FILE,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            payload,
            f,
            ensure_ascii=False,
            indent=2
        )


def get_project_path(project_name):
    projects = load_projects()

    return projects.get(project_name)


def add_project(project_name, project_path):
    projects = load_projects()

    projects[project_name] = os.path.abspath(
        project_path
    )

    save_projects(projects)


def remove_project(project_name):
    projects = load_projects()

    if project_name in projects:
        del projects[project_name]
        save_projects(projects)


# ---------------------------------------------------------
# General Mode (بدون پروژه)
# ---------------------------------------------------------
#
# General Mode یعنی «هیچ پروژه‌ای انتخاب نشده است».
#
# این حالت یک پروژهٔ جعلی نیست:
#   * هیچ project_root ساخته یا جعل نمی‌شود،
#   * هیچ مسیری به فایل‌سیستم نگاشت نمی‌شود،
#   * safe_path و resolve_project_path دست‌نخورده می‌مانند.
#
# چون هیچ مسیر فایلی وجود ندارد، در General Mode فقط پاسخ
# متنی/تحلیلی مدل مجاز است:
#   * هیچ ابزار فایل‌محوری (read/write/edit/delete/move/
#     create_directory/command) به مدل داده نمی‌شود،
#   * هیچ مسیر نوشتن، Backup، Approval، Plan، Verify یا
#     Journal وجود ندارد چون اصلاً mutationی ممکن نیست،
#   * دستورهای کنترلی مثل «تأیید» یا «undo» در این حالت
#     هیچ Pending Action را اجرا یا لغو نمی‌کنند.
#
# یعنی General Mode از نظر ایمنی Fail-Closed است: نبودِ
# پروژه هرگز به دسترسی آزاد به مسیرهای دلخواه سیستم تبدیل
# نمی‌شود.
GENERAL_MODE_NAME = "general"

GENERAL_MODE_NAMES = (
    "",
    GENERAL_MODE_NAME,
    "general-mode",
    "none",
)


def is_general_mode(project_name):
    # فقط وقتی General Mode است که:
    #   1) نامِ درخواستی یکی از نام‌های رزرو‌شدهٔ General
    #      باشد، و
    #   2) هیچ پروژهٔ واقعی با همان نام ثبت نشده باشد.
    #
    # شرط دوم لازم است تا یک پروژهٔ واقعی که کاربر نامش را
    # «general» گذاشته، هرگز پشت General Mode پنهان نشود.
    text = (
        ""
        if project_name is None
        else str(project_name)
    ).strip().lower()

    if text not in GENERAL_MODE_NAMES:
        return False

    return get_project_path(project_name) is None


def ensure_general_workspace():
    # Lazy creation of the fixed General Mode sandbox.
    #
    # Only called when an executive request really needs a root.
    # Fail-Closed: existing non-directory, symlink replacement or
    # creation failure returns None and the request must stop.
    # No fallback to any other path is allowed.
    target = os.path.abspath(GENERAL_WORKSPACE_DIR)
    base = os.path.abspath(BASE_DIR)

    if os.path.commonpath([target, base]) != base:
        return None

    try:
        if os.path.lexists(target) and not os.path.isdir(target):
            return None

        os.makedirs(target, exist_ok=True)

        if not os.path.isdir(target):
            return None

        return target
    except Exception:
        return None


def run_general_task(
    user_request,
    project_name=None,
    allow_write=False,
    stop_event=None,
    progress=None,
):
    # General Mode: فقط پاسخ متنی/تحلیلی.
    #
    # این تابع عمداً هیچ ابزار فایل‌محوری را در اختیار مدل
    # نمی‌گذارد و هیچ عملیات فایل‌سیستمی انجام نمی‌دهد.
    # allow_write عمداً نادیده گرفته می‌شود: در General Mode
    # «اعمال تغییر» وجود ندارد، چون هیچ پروژه/مسیری وجود
    # ندارد. این نادیده‌گرفتن، مجوز نیست؛ جهت پیش‌فرض و
    # امن همین است.
    request_text = "" if user_request is None else str(user_request)

    if not request_text.strip():
        return "لطفاً یک درخواست وارد کنید."

    label = GENERAL_MODE_NAME

    # Emergency Stop همچنان بالاترین اولویت است؛ حتی در
    # General Mode هیچ درخواست تازه‌ای اجرا نمی‌شود.
    if is_stop_requested(stop_event):
        return build_emergency_stop_report(
            "General Mode",
            label,
            None,
            notes=[
                "درخواست در حالت عمومی و با Emergency Stop "
                "فعال اجرا نشد؛ هیچ تغییری انجام نشد."
            ],
        )

    # فرمان‌های کنترلی در General Mode هیچ Pending Action
    # را تأیید/لغو/Undo نمی‌کنند. اگر Planی از Project Mode
    # باقی مانده باشد، عمداً دست‌نخورده می‌ماند تا در همان
    # پروژه و با همان Approval قانونی تصمیم‌گیری شود.
    if detect_control_reply(request_text) is not None:
        return build_report(
            "General Mode",
            label,
            None,
            [
                "در حالت «بدون پروژه» هیچ Plan فعالی وجود "
                "ندارد؛ هیچ تغییری انجام نشد.",
                "برای تأیید یا لغو Plan، همان پروژه را "
                "انتخاب کنید.",
            ],
        )

    emit_progress(
        progress,
        "step_start",
        step=1,
        tool="general_mode",
        detail="پاسخ متنی (بدون پروژه)",
    )

    history = get_recent_conversation_text(
        project_name=None,
        current_request=request_text,
    )

    user_prompt = ""

    if history.strip():
        user_prompt += (
            "RECENT CONVERSATION:\n"
            + history
            + "\n\n"
        )

    # Phase 4 — Durable Memory (flag-gated). OFF by default: no change.
    relevant_memory = _get_durable_memory_text(
        request_text, project_name=project_name
    )

    if relevant_memory.strip():
        user_prompt += (
            "RELEVANT MEMORY:\n"
            + relevant_memory
            + "\n\n"
        )

    user_prompt += (
        "USER REQUEST:\n"
        + request_text
    )

    answer = call_model(
        """
You are a helpful general-purpose assistant.

You are in General Mode: NO project is selected.

Rules you must follow:
- Answer in text only.
- You have NO access to any file, folder, project or command.
  You cannot read, create, edit, delete, move or run anything.
- If the request requires real files or a real project, say
  clearly that you are in General Mode and that a project must be
  selected, then explain what you can do without any file access.
- Never claim that a file was read, created, changed, verified or
  deleted. Never invent file contents or paths.
- Be concise, concrete and useful.
""",
        user_prompt,
        stop_event=stop_event,
        no_think=False,
    )

    answer = "" if answer is None else str(answer).strip()

    if not answer:
        answer = (
            "پاسخی تولید نشد. در حالت «بدون پروژه» فقط "
            "پاسخ متنی ممکن است."
        )

    return build_report(
        "General Mode",
        label,
        None,
        [answer],
    )


APPROVAL_MODE_MANUAL = "manual"
APPROVAL_MODE_AUTONOMOUS = "autonomous"

# Step 1 — Project Approval Mode.
#
# approval_mode is stored PER PROJECT inside projects.json, so changing one
# project never affects another.
#
# Backward compatibility is the contract here: projects.json is a flat
# {"name": "path"} mapping and every existing project has no mode at all.
# A missing/invalid value therefore resolves to "manual", so no existing
# project can ever silently become autonomous.
#
# NOTE: "autonomous" only removes the MANUAL APPROVAL prompt. It never
# disables Emergency Stop, safe_path, re-validation, backup, journal or
# command restrictions. Autonomous = Approval Off, NOT Safety Off.
APPROVAL_MODES = (APPROVAL_MODE_MANUAL, APPROVAL_MODE_AUTONOMOUS)

# Reserved key inside projects.json holding per-project approval modes.
# A project literally named like this is not supported.
PROJECTS_APPROVAL_KEY = "__approval_modes__"


def normalize_approval_mode(value):
    # Only an explicit, known mode is honoured. Everything else is manual
    # (fail closed towards asking the user).
    try:
        mode = str(value or "").strip().lower()
    except Exception:
        return APPROVAL_MODE_MANUAL

    if mode in APPROVAL_MODES:
        return mode

    return APPROVAL_MODE_MANUAL


def get_project_approval_mode(project_name):
    """Return the approval mode for one project (default: manual)."""
    modes = _load_projects_document().get(PROJECTS_APPROVAL_KEY)

    if not isinstance(modes, dict):
        return APPROVAL_MODE_MANUAL

    return normalize_approval_mode(modes.get(project_name))


def set_project_approval_mode(project_name, mode):
    """Persist the approval mode for exactly one project."""
    document = _load_projects_document()

    if project_name not in document:
        return False

    modes = document.get(PROJECTS_APPROVAL_KEY)

    if not isinstance(modes, dict):
        modes = {}

    modes = dict(modes)
    modes[project_name] = normalize_approval_mode(mode)

    document[PROJECTS_APPROVAL_KEY] = modes

    save_projects_document(document)

    return True


def is_autonomous_project(project_name):
    return get_project_approval_mode(project_name) == APPROVAL_MODE_AUTONOMOUS


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


def get_last_rollback_result():
    # نتیجه داخلی آخرین Rollback:
    # path, restored_from, backup_path, diff و ...
    if LAST_ROLLBACK_RESULT is None:
        return None

    return dict(LAST_ROLLBACK_RESULT)


def find_backup_path(
    project_root,
    full_path
):
    # مسیر Backup فقط توسط خود Agent ساخته و
    # تشخیص داده می‌شود؛ کاربر مسیر Backup را
    # وارد نمی‌کند، بنابراین path traversal
    # به فایل دیگری ممکن نیست.
    #
    # جدیدترین Backup انتخاب می‌شود:
    # 1) .agent-backup        (قدیمی‌تر)
    # 2) .agent-backup.1
    # 3) .agent-backup.2
    #    ...
    candidates = []

    base_backup = full_path + BACKUP_SUFFIX

    if os.path.isfile(base_backup):
        candidates.append((0, base_backup))

    counter = 1

    while True:

        numbered = (
            full_path
            + BACKUP_SUFFIX
            + "."
            + str(counter)
        )

        if not os.path.isfile(numbered):
            break

        candidates.append((counter, numbered))

        counter += 1

    if not candidates:
        return None

    # بزرگ‌ترین شماره = جدیدترین Backup
    candidates.sort(key=lambda item: item[0])

    newest = candidates[-1][1]

    # مسیر Backup هم باید داخل project root باشد.
    root = os.path.abspath(project_root)

    if os.path.commonpath(
        [os.path.abspath(newest), root]
    ) != root:
        raise ValueError(
            "Backup path is outside the selected project."
        )

    return newest


def rollback_file(
    project_root,
    relative_path,
    return_details=False
):
    # ترتیب Rollback:
    # 1) پیدا کردن فایل هدف (با همان safe_path)
    # 2) بررسی وجود Backup
    # 3) ساخت Backup از وضعیت فعلی فایل
    # 4) جایگزینی فایل با محتوای Backup قبلی
    # 5) ثبت نتیجه
    if not project_root:
        raise ValueError(
            "پروژه انتخاب‌شده پیدا نشد."
        )

    # مرحله 1: فایل هدف فقط داخل project root
    # مجاز است (همان safe_path مسیر Write).
    full_path = safe_path(
        project_root,
        relative_path
    )

    if not os.path.isfile(full_path):
        raise FileNotFoundError(
            f"File not found: {relative_path}"
        )

    # مرحله 2: اگر Backup وجود نداشت،
    # هیچ تغییری انجام نمی‌شود.
    backup_path = find_backup_path(
        project_root,
        full_path
    )

    if backup_path is None:
        raise FileNotFoundError(
            "برای این فایل Backup وجود ندارد؛ "
            "Rollback انجام نشد."
        )

    # محتوای Backup قبل از هر تغییری خوانده می‌شود؛
    # اگر خواندن شکست بخورد، فایل اصلی دست‌نخورده
    # می‌ماند.
    with open(
        backup_path,
        "rb"
    ) as f:
        backup_data = f.read()

    try:
        backup_content = backup_data.decode(
            "utf-8"
        )

    except UnicodeDecodeError:

        raise ValueError(
            "Backup فایل UTF-8 نیست؛ "
            "Rollback انجام نشد."
        )

    # Diff وضعیت جدید نسبت به وضعیت قبل از Rollback؛
    # بعد از Rollback در نتیجه داخلی قابل دسترسی است.
    try:
        with open(
            full_path,
            "r",
            encoding="utf-8",
            newline=""
        ) as f:
            before_content = f.read()

        diff = build_unified_diff(
            before_content,
            backup_content,
            relative_path
        )

    except UnicodeDecodeError:

        diff = (
            "(diff در دسترس نیست: "
            "فایل UTF-8 نیست)"
        )

    # مرحله 3: وضعیت فعلی فایل Backup می‌شود تا
    # خود Rollback هم قابل برگشت باشد.
    # از همان create_backup استفاده می‌شود؛ پس
    # Backup قبلی overwrite نمی‌شود و نسخه
    # شماره‌دار جدید ساخته می‌شود.
    # اگر این مرحله شکست بخورد، فایل اصلی
    # تغییر نمی‌کند.
    safety_backup_path = create_backup(
        project_root,
        full_path
    )

    # مرحله 4: جایگزینی فایل با محتوای Backup قبلی.
    with open(
        full_path,
        "w",
        encoding="utf-8",
        newline=""
    ) as f:
        f.write(backup_content)

    # مرحله 5: ثبت نتیجه.
    result = {
        "project_root": project_root,
        "relative_path": relative_path,
        "path": full_path,
        "restored_from": backup_path,
        "backup_path": safety_backup_path,
        "existed_before": True,
        "diff": diff,
        "changed": bool(diff)
    }

    remember_rollback_result(result)

    # Phase A (Safety Foundation): ثبت passive عملیات Recovery.
    # رفتار Rollback فعلی تغییر نمیکند؛ فقط متادیتا ثبت میشود.
    record_action(
        action_type="rollback",
        operation_class=ACTION_CLASS_RECOVERY,
        tool="rollback_file",
        args={"path": relative_path},
        project_root=project_root,
        target=relative_path,
        backup_path=(
            relative_display(project_root, safety_backup_path)
            if safety_backup_path else None
        ),
        before_state={
            "existed_before": True,
            "type": "file",
            "restored_from": relative_display(
                project_root, backup_path
            ),
            "restore_hint": relative_display(
                project_root, backup_path
            ),
        },
        execution_state=ACTION_STATE_DONE,
        verify_state=ACTION_VERIFY_OK,
        result_summary=(
            "rollback: "
            + relative_display(project_root, backup_path)
        ),
    )

    if return_details:
        return get_last_rollback_result()

    # مانند write_file، خروجی پیش‌فرض مسیر کامل است.
    return full_path


# ---------------------------------------------------------
# Safety Foundation (Phase A)
# ---------------------------------------------------------
# این بخش فقط «ثبت» است و هیچ رفتاری را تغییر نمیدهد:
# - Shared Action Context: ساختار سبک و JSON-serializable
#   برای توصیف یک عملیات.
# - Action Journal: تاریخچه محدود (ring buffer) از متادیتای
#   عملیات تغییردهنده.
#
# قواعد این لایه:
# - هیچ عملیاتی را متوقف یا رد نمیکند (blocking ندارد).
# - هیچ خطایی از این لایه نباید اجرای اصلی را خراب کند.
# - محتوای کامل فایل، transcript مدل و داده حساس خام در
#   Journal نگه داشته نمیشود.
# - LAST_WRITE_RESULT / LAST_ROLLBACK_RESULT /
#   LAST_WRITE_MULTI_RESULT / Pending Action / Approval /
#   Backup / Rollback / Legacy بدون تغییر باقی میمانند.

# کلاس عملیات (برای Safety Gate/TTL/Undo در فازهای بعد).
ACTION_CLASS_USER = "user_mutation"
ACTION_CLASS_RECOVERY = "recovery"
ACTION_CLASS_EXTERNAL = "external"
ACTION_CLASS_READ = "read"

ACTION_CLASSES = (
    ACTION_CLASS_USER,
    ACTION_CLASS_RECOVERY,
    ACTION_CLASS_EXTERNAL,
    ACTION_CLASS_READ,
)

# وضعیت تأیید (فقط ثبت؛ منطق Approval فعلی دستنخورده است).
ACTION_APPROVAL_NOT_REQUIRED = "not_required"
ACTION_APPROVAL_PENDING = "pending"
ACTION_APPROVAL_APPROVED = "approved"
ACTION_APPROVAL_CANCELLED = "cancelled"
ACTION_APPROVAL_EXPIRED = "expired"

# وضعیت اجرا.
ACTION_STATE_PLANNED = "planned"
ACTION_STATE_RUNNING = "running"
ACTION_STATE_DONE = "done"
ACTION_STATE_FAILED = "failed"
ACTION_STATE_STOPPED = "stopped"

# وضعیت Verify.
ACTION_VERIFY_UNKNOWN = "unknown"
ACTION_VERIFY_OK = "ok"
ACTION_VERIFY_FAILED = "failed"
ACTION_VERIFY_SKIPPED = "skipped"

# کلیدهای آرگومان که معمولاً محتوای فایل/متن را حمل میکنند؛
# از این کلیدها فقط اندازه ثبت میشود، نه محتوای خام.
ACTION_CONTENT_ARG_KEYS = (
    "content", "text", "body",
    "old", "old_text", "find", "search",
    "new", "new_text", "replace", "with",
)

# حداکثر طول مقدارهای متنی غیرمحتوایی در Context.
ACTION_ARG_STRING_LIMIT = 120

# حداکثر تعداد Action در Journal (ring buffer).
ACTION_JOURNAL_LIMIT = 50

# فیلدهای استاندارد Action Context.
# agent_id / agent_type / approval_level / approval_policy برای
# معماری چند-Agent آینده رزرو شدهاند و فعلاً در منطق اجرایی
# استفاده نمیشوند.
ACTION_CONTEXT_FIELDS = (
    "action_id",
    "created_at",
    "ended_at",
    "operation_class",
    "action_type",
    "tool",
    "args",
    "args_hash",
    "project",
    "project_root",
    "target",
    "allow_write",
    "approval_state",
    "validation_state",
    "backup_path",
    "before_state",
    "execution_state",
    "verify_state",
    "result_summary",
    "agent_id",
    "agent_type",
    "approval_level",
    "approval_policy",
)

# Journal فقط در حافظه پروسه است و روی دیسک ذخیره نمیشود.
ACTION_JOURNAL = []

# Phase F2: مسیر فایل persist باید زود تعریف شود چون load هنگام import
# (پایین‌تر در همین فایل) به آن نیاز دارد.
ACTION_JOURNAL_FILE = os.path.join(
    BASE_DIR, "action-journal.json"
)

# Phase F2: persistence سبک append-only برای Journal (فقط metadata).
# - فقط فیلدهای مجاز ذخیره می‌شود؛ محتوا/secret هرگز.
# - نوشتن atomic است: temp file → os.replace.
# - هر خطایی best-effort نادیده گرفته می‌شود تا عملیات اصلی متوقف نشود.
ACTION_JOURNAL_PERSIST_FIELDS = (
    "action_id",
    "action_type",
    "tool",
    "operation_class",
    "execution_state",
    "verify_state",
    "validation_state",
    "approval_state",
    "project",
    "project_root",
    "target",
    "backup_path",
    "before_state",
    "args",
    "args_hash",
    "created_at",
    "ended_at",
    "result_summary",
    "agent_id",
    "agent_type",
    "approval_level",
    "approval_policy",
)


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


def verify_state_from_bool(value):
    # تبدیل نتیجه بولی Verify به وضعیت رشته‌ای استاندارد.
    if value is True:
        return ACTION_VERIFY_OK

    if value is False:
        return ACTION_VERIFY_FAILED

    return ACTION_VERIFY_SKIPPED


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


def _caret_column(message):
    # خروجی py_compile/node خط منبع را با 4 فاصله تودرتو چاپ
    # می‌کند و caret (^) را دقیقاً زیر همان خط می‌گذارد.
    # ستون فقط وقتی گزارش می‌شود که این ساختار دقیقاً برقرار
    # باشد؛ در غیر این صورت حدس زده نمی‌شود.
    lines = str(message or "").splitlines()

    for index in range(1, len(lines)):
        caret_line = lines[index]
        source_line = lines[index - 1]
        stripped = caret_line.strip()

        if not stripped or set(stripped) != {"^"}:
            continue

        if not source_line.startswith("    ") or "\t" in source_line:
            continue

        caret_at = caret_line.index("^")

        if caret_at < 4 or caret_line[:caret_at].strip():
            continue

        return caret_at - 3

    return None


def parse_syntax_position(message):
    # استخراج (line, column) از متن خطای Syntax.
    # هر مقدار که قابل‌اعتماد نباشد None می‌ماند؛ هیچ حدسی
    # زده نمی‌شود چون Feedback اشتباه بدتر از نبودن آن است.
    text = "" if message is None else str(message)
    line = None
    column = None

    match = VERIFY_LINE_PATTERN.search(text)

    if match:
        try:
            line = int(match.group(1))

        except ValueError:
            line = None

    match = VERIFY_COLUMN_PATTERN.search(text)

    if match:
        try:
            column = int(match.group(1))

        except ValueError:
            column = None

        return line, column

    if line is None:
        return None, None

    return line, _caret_column(text)


def new_verify_error(path, error_type, message, line=None, column=None):
    # یک خطای Verify ساختاریافته؛ متن خطا Mask و طول آن محدود
    # می‌شود تا Feedback هرگز بی‌کران نشود.
    return {
        "path": "" if path is None else str(path),
        "error_type": str(error_type),
        "message": clip_text(
            mask_sensitive_content(str(message or "")),
            VERIFY_ERROR_CHARS
        ),
        "line": line,
        "column": column,
    }


def empty_verify_result():
    # شکل واحد نتیجه Verify.
    return {
        "ok": True,
        "applied": True,
        "checked": 0,
        "unchecked": [],
        "rolled_back": False,
        "errors": [],
    }


def verify_error_from_validation(relative_path, validation):
    # تبدیل خروجی موجود validate_written_file به خطای
    # ساختاریافته. اگر Validation موفق باشد None برمی‌گردد.
    validation = validation or {}

    if validation.get("valid"):
        return None

    message = validation.get("error")
    path = relative_path

    if not validation.get("exists"):
        error_type = VERIFY_TYPE_MISSING

        if not message:
            message = "فایل بعد از Write پیدا نشد: " + str(path)

    elif validation.get("syntax_valid") is False:
        error_type = VERIFY_TYPE_SYNTAX

    elif validation.get("content_matches") is False:
        error_type = VERIFY_TYPE_CONTENT

    else:
        error_type = VERIFY_TYPE_OTHER

    if not message:
        message = "Validation ناموفق بود."

    line = None
    column = None

    if error_type == VERIFY_TYPE_SYNTAX:
        line, column = parse_syntax_position(message)

    return new_verify_error(path, error_type, message, line, column)


def verify_from_validation(relative_path, validation):
    # نتیجه Verify برای یک فایل واحد (مسیر write_file).
    result = empty_verify_result()

    if (validation or {}).get("syntax_checked"):
        result["checked"] = 1

    else:
        result["unchecked"] = [str(relative_path)]

    error = verify_error_from_validation(relative_path, validation)

    if error is not None:
        result["errors"].append(error)

    result["ok"] = not result["errors"]

    return result


def verify_from_tool_result(result):
    # نتیجه Verify ای که خود ابزار Mutation همراه آورده است.
    data = (result or {}).get("data") or {}
    verify = data.get("verify")

    if isinstance(verify, dict):
        return verify

    return None


def verify_applied_files(project_root, files):
    # Verify بعد از یک Mutation موفق.
    #
    # فقط می‌خواند و هیچ‌چیز را تغییر نمی‌دهد:
    # - وجود فایل روی دیسک بعد از Write
    # - مطابقت محتوا با همان محتوایی که نوشته شد
    # - Syntax فایل بر اساس نوع فایل، اگر چکر قابل‌اعتمادی
    #   روی همین سیستم موجود باشد
    #
    # اگر برای نوع فایلی چکر قابل‌اعتمادی وجود نداشته باشد،
    # آن فایل فقط «بررسی‌نشده» گزارش می‌شود و هیچ خطای جعلی
    # تولید نمی‌شود.
    result = empty_verify_result()

    for entry in (files or []):

        if not isinstance(entry, dict):
            continue

        relative_path = str(entry.get("path") or "")

        if not relative_path:
            result["errors"].append(
                new_verify_error(
                    "",
                    VERIFY_TYPE_OTHER,
                    "مسیر فایل در Batch نامعتبر است."
                )
            )

            continue

        # مرز پروژه و Traversal با همان safe_path کنترل می‌شود.
        try:
            checked_path = safe_path(project_root, relative_path)

        except (ValueError, TypeError) as e:
            result["errors"].append(
                new_verify_error(
                    relative_path,
                    VERIFY_TYPE_BOUNDARY,
                    str(e)
                )
            )

            continue

        try:
            validation = validate_written_file(
                project_root,
                relative_path,
                entry.get("content"),
                checked_path
            )

        except Exception as e:
            result["errors"].append(
                new_verify_error(
                    relative_path,
                    VERIFY_TYPE_OTHER,
                    str(e)
                )
            )

            continue

        if validation.get("syntax_checked"):
            result["checked"] += 1

        else:
            result["unchecked"].append(relative_path)

        error = verify_error_from_validation(
            relative_path, validation
        )

        if error is not None:
            result["errors"].append(error)

    result["ok"] = not result["errors"]

    return result


def verify_failed_mutation(mutation_result):
    # Verify برای Mutation ای که خودش (با Rollback) شکست خورده.
    #
    # تغییرات روی دیسک اعمال نشده‌اند، پس محتوای فایل‌ها در
    # این حالت مقایسه نمی‌شود؛ فقط خطاهای واقعیِ همان تلاش از
    # نتیجه‌ی write_file_multi استخراج می‌شوند تا به مدل
    # برگردانده شوند.
    result = empty_verify_result()
    result["ok"] = False
    result["applied"] = False
    result["rolled_back"] = bool(
        (mutation_result or {}).get("rolled_back")
    )

    files = (mutation_result or {}).get("files") or []
    overall_error = (mutation_result or {}).get("error")

    for item in files:

        if not isinstance(item, dict):
            continue

        relative_path = str(item.get("relative_path") or "")
        validation = item.get("validation") or {}

        if validation.get("syntax_checked"):
            result["checked"] += 1

        elif relative_path:
            result["unchecked"].append(relative_path)

        error = verify_error_from_validation(
            relative_path, validation
        )

        if error is not None:
            result["errors"].append(error)

    if not result["errors"]:
        result["errors"].append(
            new_verify_error(
                "",
                VERIFY_TYPE_MUTATION,
                overall_error or "اجرای Mutation ناموفق بود."
            )
        )

    return result


def format_verify_feedback(verify, tool_name=None):
    # Feedback حداقلی برای مدل:
    # file + error type + message + line/position
    # ورودی نامعتبر هرگز باعث استثنا نمی‌شود؛ فقط Feedback خالی
    # تولید می‌شود (هیچ خطای جعلی ساخته نمی‌شود).
    verify = verify if isinstance(verify, dict) else {}
    errors = verify.get("errors") or []

    if not isinstance(errors, list):
        errors = []

    lines = [
        "VERIFY FAILED"
        + (" (" + str(tool_name) + ")" if tool_name else "")
    ]

    if verify.get("rolled_back"):
        lines.append(
            "- وضعیت: تغییرات این تلاش Rollback شد و روی دیسک "
            "اعمال نشده است."
        )

    elif verify.get("applied") is False:
        lines.append(
            "- وضعیت: تغییرات این تلاش روی دیسک اعمال نشده است."
        )

    for item in errors[:VERIFY_MAX_ERRORS]:
        if not isinstance(item, dict):
            continue

        position = []

        if item.get("line") is not None:
            position.append("line " + str(item.get("line")))

        if item.get("column") is not None:
            position.append("column " + str(item.get("column")))

        lines.append(
            "- file: "
            + (str(item.get("path")) or "?")
            + " | type: "
            + str(item.get("error_type"))
        )
        lines.append("  error: " + str(item.get("message") or ""))

        if position:
            lines.append("  " + ", ".join(position))

    remaining = len(errors) - VERIFY_MAX_ERRORS

    if remaining > 0:
        lines.append(
            "- " + str(remaining) + " خطای دیگر هم گزارش شد."
        )

    lines.append(
        "NOTE: last mutation did NOT pass verification."
    )

    return clip_text("\n".join(lines), VERIFY_FEEDBACK_CHARS)


def call_model(
    system_prompt,
    user_prompt,
    stop_event=None,
    n_predict=None,
    no_think=False,
):
    # Phase 2 — Provider Layer wiring (llama_cpp_local only).
    #
    # run_agent -> call_model -> Provider -> urllib -> llama-server.
    # امضای عمومی، prompt، پارامترها، timeout، stop_event، نگاشت
    # خطا و parse پاسخ دقیقاً مثل قبل حفظ شده‌اند؛ فقط ساخت
    # endpoint/payload/header به agents/providers.py سپرده شده است.
    # import به‌صورت lazy است تا circular import ایجاد نشود.
    # هیچ fallback خاموشی به transport inline وجود ندارد: شکست
    # Provider صریح و قابل تشخیص است (RuntimeError پیش از هر request).
    try:
        from agents import providers as _providers
    except Exception as e:
        raise RuntimeError(
            f"خطا در بارگذاری Provider مدل: {e}"
        )

    try:
        _spec = _providers.default_provider()
        _endpoint = _providers.resolve_endpoint(_spec)
        _headers = _providers.build_headers(_spec)
    except Exception as e:
        raise RuntimeError(
            f"خطا در پیکربندی Provider مدل: {e}"
        )

    _is_raw = _spec.uses_raw_prompt

    payload = _providers.build_payload(
        _spec,
        system_prompt,
        user_prompt,
        n_predict=n_predict,
        no_think=no_think,
    )

    data = json.dumps(
        payload,
        ensure_ascii=False
    ).encode("utf-8")

    request = urllib.request.Request(
        _endpoint,
        data=data,
        headers=_headers,
        method="POST"
    )

    raw = None
    try:
        if is_stop_requested(stop_event):
            raise RuntimeError("Cancelled by user")
        with urllib.request.urlopen(
            request,
            timeout=300
        ) as response:

            raw = response.read().decode(
                "utf-8"
            )

            if is_stop_requested(stop_event):
                raise RuntimeError("Cancelled by user")

            result = json.loads(raw)

    except urllib.error.HTTPError as e:

        try:
            error_body = e.read().decode(
                "utf-8"
            )
        except Exception:
            error_body = str(e)

        raise RuntimeError(
            f"خطای مدل ({e.code}): {error_body}"
        )

    except Exception as e:

        if is_stop_requested(stop_event):
            raise RuntimeError("Cancelled by user")

        raise RuntimeError(
            f"خطا در ارتباط با مدل: {e}"
        )

    if _is_raw:
        content = result.get(
            "content",
            ""
        )
    else:
        try:
            content = result["choices"][0]["message"]["content"]
        except Exception:
            content = ""

    if not content:
        raise RuntimeError(
            "مدل پاسخ متنی برنگرداند."
        )

    return str(content).strip()


def find_requested_directory(
    project_root, user_request
):
    pattern = (
        r"(?<![\w])"
        r"([A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)*)"
        r"(?![\w])"
    )

    matches = re.findall(pattern, user_request)

    for requested in matches:
        normalized = requested.replace(
            "\\", os.sep
        ).replace("/", os.sep)

        try:
            full_path = safe_path(
                project_root, normalized
            )
            if os.path.isdir(full_path):
                return os.path.relpath(
                    full_path, project_root
                )
        except (ValueError, OSError):
            pass

    return None


# ---------------------------------------------------------
# Plan چندفایلی (فقط تشخیص و آماده‌سازی؛ بدون Write)
# ---------------------------------------------------------

# در این مرحله فقط این مسیر مجاز است:
# Request → Detect files → Validate paths → Build Plan → Display Plan
#
# هیچ Write چندفایلی، هیچ Backup چندفایلی و هیچ
# Rollback چندفایلی در این مرحله فعال نمی‌شود.

# نتیجه داخلی آخرین Plan چندفایلی برای نمایش در UI.
LAST_FILE_PLAN = None


def remember_file_plan(plan):
    # یک کپی ذخیره می‌شود تا تغییر بعدی caller
    # روی نتیجه داخلی اثر نگذارد.
    global LAST_FILE_PLAN

    if plan is None:
        LAST_FILE_PLAN = None
        return None

    LAST_FILE_PLAN = {
        "files": [
            dict(item)
            for item in plan.get("files", [])
        ],
        "count": plan.get("count", 0),
        "has_new": plan.get("has_new", False),
        "has_existing": plan.get(
            "has_existing", False
        )
    }

    return LAST_FILE_PLAN


def get_last_file_plan():
    if LAST_FILE_PLAN is None:
        return None

    return {
        "files": [
            dict(item)
            for item in LAST_FILE_PLAN.get("files", [])
        ],
        "count": LAST_FILE_PLAN.get("count", 0),
        "has_new": LAST_FILE_PLAN.get("has_new", False),
        "has_existing": LAST_FILE_PLAN.get(
            "has_existing", False
        )
    }


def is_safe_relative_candidate(project_root, candidate):
    # بررسی امنیتی یک کاندید مسیر:
    # - فقط از طریق safe_path بررسی می‌شود.
    # - ../ و ..\ پذیرفته نمی‌شوند.
    # - مسیر absolute خارج از project root پذیرفته نمی‌شود.
    # - symlink یا path خارج از project root پذیرفته نمی‌شود.
    # - هیچ فایل خارج از پروژه نباید وارد Plan شود.
    if not candidate:
        return False

    text = candidate.strip()

    if not text:
        return False

    # مسیرهای مطلق ویندوزی (مثل C:\... یا C:/...) هرگز
    # به‌عنوان مسیر نسبی داخل پروژه پذیرفته نمی‌شوند.
    if re.match(r"^[A-Za-z]:[\\/]", text):
        return False

    # مسیر UNC (مثل \\server\share) پذیرفته نمی‌شود.
    if text.startswith("\\\\"):
        return False

    # مسیر مطلق یونیکسی (مثل /etc/passwd) هرگز به‌عنوان
    # مسیر نسبی داخل پروژه پذیرفته نمی‌شود؛ چون بعد از
    # join شدن با project_root، os.path.abspath آن را به
    # ریشه درایو جاری می‌برد و از پروژه خارج می‌شود.
    # نمونه: "C:/Windows/win.ini" در متن درخواست توسط
    # extract_file_candidates به "Windows/win.ini" برش
    # نمی‌خورد — اما "/etc/passwd" به‌صورت کامل می‌آید
    # و باید همین‌جا رد شود.
    if text.startswith("/"):
        return False

    # عبور از ریشه پروژه با .. ممنوع است.
    parts = re.split(r"[\\/]+", text)

    if ".." in parts:
        return False

    try:
        full_path = safe_path(
            project_root,
            text
        )

    except (ValueError, TypeError, OSError):
        return False

    # realpath برای پوشش symlink/pathهای خارج از پروژه.
    root = os.path.abspath(project_root)
    resolved_root = os.path.realpath(root)
    resolved_path = os.path.realpath(full_path)

    try:

        if os.path.commonpath(
            [resolved_path, resolved_root]
        ) != resolved_root:

            return False

    except (ValueError, OSError):
        return False

    return True


def extract_file_candidates(user_request):
    # استخراج کاندیدهای مسیر فایل از متن درخواست.
    # فقط توکن‌هایی که یک پسوند فایل (نقطه + حروف)
    # دارند پذیرفته می‌شوند؛ کلمات مبهم بدون این
    # نشانه حدس زده نمی‌شوند.
    pattern = (
        r"(?<![\w])"
        r"([A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)*"
        r"\.[A-Za-z0-9]{1,10})"
        r"(?![\w])"
    )

    found = []
    for match in re.finditer(pattern, user_request or ""):
        # اگر کاندید بلافاصله بعد از : یا / یا \ آمده باشد،
        # ادامه‌ی یک مسیر مطلق (مثل C:/Windows/... یا
        # D:\AI\... یا /etc/...) است نه یک مسیر نسبی داخل
        # پروژه؛ پس کلاً رد می‌شود تا هیچ درخواست خارج از
        # پروژه به Plan داخل پروژه ترجمه نشود.
        if match.start(1) > 0 and (user_request or "")[match.start(1) - 1] in (":", "/", "\\"):
            continue
        found.append(match.group(1))

    candidates = []
    seen = set()

    for item in found:
        text = item.strip().strip("\"").strip("'")

        if not text:
            continue

        key = text.lower()

        if key in seen:
            continue

        seen.add(key)
        candidates.append(text)

    return candidates


def find_requested_files(project_root, user_request):
    # تشخیص چند فایل موردنیاز درخواست.
    # خروجی ساختاریافته برای هر فایل:
    # relative_path / full_path / exists
    #
    # - همه مسیرها فقط از طریق safe_path بررسی می‌شوند.
    # - مسیر نامعتبر یا خارج از پروژه وارد نتیجه نمی‌شود.
    # - برای فایل مبهم یا نامشخص حدس زده نمی‌شود.
    # - هیچ فایلی خوانده یا تغییر داده نمی‌شود؛ فقط
    #   وجود فایل با os.path.isfile بررسی می‌شود.
    results = []

    if not project_root:
        return results

    if not os.path.isdir(
        os.path.abspath(project_root)
    ):
        return results

    for candidate in extract_file_candidates(
        user_request
    ):

        if not is_safe_relative_candidate(
            project_root,
            candidate
        ):
            continue

        try:
            full_path = safe_path(
                project_root,
                candidate
            )

        except (ValueError, TypeError, OSError):
            continue

        # کنترل نهایی ریشه بعد از safe_path.
        root = os.path.abspath(project_root)

        try:

            if os.path.commonpath(
                [
                    os.path.realpath(full_path),
                    os.path.realpath(root)
                ]
            ) != os.path.realpath(root):

                continue

        except (ValueError, OSError):
            continue

        # relative_path را از مسیر واقعی روی disk استخراج می‌کنیم
        # (نه تو خالی از متن درخواست) تا کلید وضعیت در
        # requested_mutation_status با مسیرهای mutation_targets_of_entry
        # (که از آرگومان ابزار می‌آیند) تطبیق یابد، بدون تطبیق نام فایل یا fuzzy.
        try:
            rel_path = os.path.relpath(full_path, root)
            normalized = normalize_relative_target(rel_path)
        except ValueError:
            normalized = normalize_relative_target(candidate)

        results.append(
            {
                "relative_path": normalized,
                "full_path": full_path,
                "exists": os.path.isfile(full_path)
            }
        )

    return results


def build_file_plan(project_root, user_request):
    # ساخت Plan چندفایلی.
    # فقط اطلاعات عملیات تولید می‌کند و هیچ فایل را
    # تغییر نمی‌دهد (بدون Write/Backup/Rollback).
    files = find_requested_files(
        project_root,
        user_request
    )

    planned = []

    for item in files:
        planned.append(
            {
                "relative_path": item["relative_path"],
                "full_path": item["full_path"],
                "exists": item["exists"],
                "action": (
                    "modify"
                    if item["exists"]
                    else "create"
                )
            }
        )

    plan = {
        "files": planned,
        "count": len(planned),
        "has_new": any(
            not item["exists"]
            for item in planned
        ),
        "has_existing": any(
            item["exists"]
            for item in planned
        )
    }

    remember_file_plan(plan)

    return get_last_file_plan()


def format_file_plan_text(plan):
    # نمایش متنی Plan برای گزارش و تست.
    if not plan or not plan.get("files"):
        return "Plan:\n(no files detected)"

    lines = ["Plan:"]

    for index, item in enumerate(
        plan.get("files", []),
        start=1
    ):
        lines.append(
            f"{index}. {item['relative_path']}"
        )
        lines.append(
            f"   action: {item['action']}"
        )
        lines.append(
            "   exists: "
            + str(item["exists"]).lower()
        )

    return "\n".join(lines)


def extract_file_content(
    model_output
):
    pattern = (
        r"BEGIN_FILE\s*\n"
        r"(.*?)"
        r"\nEND_FILE"
    )

    match = re.search(
        pattern,
        model_output,
        re.DOTALL
    )

    if not match:
        return None

    return match.group(1)


def build_project_structure(
    project_root, relative_path=None, depth=1
):
    # ساختار واقعی پروژه برای مرحله Explore/Understand.
    #
    # اصلاح باگ: قبلاً فهرست از مسیر درخواستی خوانده
    # می‌شد ولی تشخیص پوشه/فایل بودن هر مورد نسبت به
    # ریشه پروژه انجام می‌شد؛ در نتیجه زیرپوشه‌های یک
    # مسیر نسبی (مثل wp-content) به‌عنوان فایل نمایش
    # داده می‌شدند. اکنون همه چیز نسبت به همان مسیر
    # resolve‌شده بررسی می‌شود.
    #
    # اگر مسیر درخواستی وجود نداشته باشد، هیچ فهرست
    # جایگزینی حدس زده نمی‌شود و خطای صریح برگردانده
    # می‌شود.
    try:
        info = list_directory(
            project_root,
            relative_path,
            depth=depth
        )

    except (ValueError, OSError) as e:
        return (
            "(error listing project: "
            + str(e)
            + ")"
        )

    if not info["entries"]:
        return (
            "(the project is EMPTY - "
            "no files or folders exist.)"
        )

    lines = []

    if relative_path:
        lines.append("Directory: " + info["path"])

    for entry in info["entries"]:
        indent = "  " * entry["depth"]

        if entry["type"] == "dir":
            count = entry["children_count"]

            lines.append(
                indent
                + entry["name"]
                + "/ ("
                + (str(count) if count is not None else "?")
                + " items)"
            )
        else:
            lines.append(indent + entry["name"])

    if info["truncated"]:
        lines.append("(... truncated)")

    return "\n".join(lines)


# ---------------------------------------------------------
# General Local Agent tools
# (عمومی؛ مستقل از نوع پروژه: WordPress، PHP، Python،
#  JS، Markdown، مستندات و ...)
# ---------------------------------------------------------
#
# معماری: Explore → Understand → Plan → Execute → Verify → Report
#
# همه ابزارها فقط داخل Project Root کار می‌کنند و مسیرها
# با همان safe_path بررسی می‌شوند (بدون دسترسی خارج از پروژه).

# پوشه‌هایی که در Explore/Search نادیده گرفته می‌شوند
# (پوشه‌های ابزار و کش، نه محتوای پروژه).
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

# پسوندهایی که فایل متنی/کد در نظر گرفته می‌شوند.
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
MAX_COMMAND_OUTPUT_CHARS = 4000
DEFAULT_COMMAND_TIMEOUT = 120
TRASH_DIRNAME = ".agent-trash"

# ابزارهایی که اجرای آن‌ها در پروژه مجاز است.
ALLOWED_COMMAND_TOOLS = (
    "python", "python3", "py", "pytest", "pip", "pip3",
    "php", "composer", "node", "npm", "npx", "yarn", "pnpm",
    "tsc", "eslint", "stylelint", "git",
)

# Phase 1 — Command Policy.
#
# ALLOWED_COMMAND_TOOLS فقط argv[0] را بررسی می‌کند؛ پس
# «git» یا «npm» بودنِ ابزار به‌تنهایی مجوز کافی نیست.
# این جدول زیر‌دسته‌هایی را مسدود می‌کند که یا فایل‌های
# پروژه را غیرقابل‌بازگشت تغییر می‌دهند یا کد دلخواه
# دانلود و اجرا می‌کنند.
#
# این مقصد Undo ندارند: run_command در
# UNDOABLE_TOOLS نیست و before_state آن undoable=False
# است. پس مجاز شدنشان یعنی تغییر غیرقابل‌بازگشت، بدون
# Ask و بدون Rollback.
#
# قاعده Fail Closed: هر زیر‌دسته‌ای که در این جدول نباشد
# مجاز است، اما زیر‌دسته‌های فهرست‌شده همیشه رد می‌شوند.
# ابزارهای خارج از ALLOWED_COMMAND_TOOLS همچنان رد می‌شوند.
COMMAND_POLICY_BLOCKED_SUBCOMMANDS = {
    # تغییر/پاک‌کردن تاریخچه و فایل‌های tracked:
    # نه Backup ساخته می‌شود، نه Undo وجود دارد.
    "git": frozenset({
        "checkout", "restore", "reset", "clean", "rm", "mv",
        "push", "pull", "rebase", "merge", "filter-branch",
        "gc", "prune", "reflog", "stash",
    }),
    # نصب/حذف Package: دانلود از شبکه + اجرای
    # post-install script و دست‌کم تغییر lockfile.
    "pip": frozenset({
        "install", "uninstall", "download", "wheel",
    }),
    "pip3": frozenset({
        "install", "uninstall", "download", "wheel",
    }),
    "npm": frozenset({
        "install", "i", "ci", "uninstall", "rm", "update",
        "publish", "exec", "init", "audit", "link", "dedupe",
        # Phase H2 — اجرای script تعریف‌شده در package.json:
        # کدی که قبلاً در پروژه نوشته شده اجرا می‌شود، پس نه
        # قابل بازرسی در Plan است و نه Undo دارد.
        "run", "run-script", "test", "start", "stop", "restart",
    }),
    "yarn": frozenset({
        "install", "add", "remove", "upgrade", "publish", "exec",
        # Phase H2 — همان دلیل npm.
        "run", "run-script", "test", "start", "stop", "restart",
    }),
    "pnpm": frozenset({
        "install", "i", "add", "remove", "update", "publish", "exec",
        # Phase H2 — همان دلیل npm.
        "run", "run-script", "test", "start", "stop", "restart",
    }),
    # npx هر Package نصب‌نشده‌ای را اجرا می‌کند؛
    # عملاً اجرای کد دلخواه از شبکه است.
    "npx": frozenset({"__all__"}),
    "composer": frozenset({
        "require", "install", "update", "remove", "create-project",
        # Phase H2 — اجرای script تعریف‌شده در package.json /
        # composer.json: کد دلخواهِ از پیش نوشته‌شده اجرا می‌شود،
        # نه دستوری که کاربر همین حالا تأیید کرده است.
        "run", "run-script", "test", "exec",
    }),
}

# Phase H2 — Flagهای اجرای کد دلخواه.
#
# اجازه‌ی «اجرای دستورِ تأییدشده» با اجازه‌ی «اجرای هر کدی که
# کاربر خواسته» یکی نیست. وقتی خودِ مفسر در argv حاضر باشد،
# بدنه‌ی کد داخل خودِ فرمان پنهان است و از Plan/Approval بیرون
# می‌ماند؛ همچنین هیچ Backup یا Verify و در نتیجه هیچ Undoیی
# وجود ندارد، چون run_command در UNDOABLE_TOOLS نیست.
#
# بنابراین flagهایی که کد را از خط فرمان به مفسر تزریق
# می‌کنند مسدود می‌شوند. اجرای «فایلِ داخل پروژه» (مثل
# python manage.py) مجاز می‌ماند، چون مسیر و محتوای آن فایل
# روی دیسک قابل بازرسی است.
COMMAND_POLICY_BLOCKED_FLAGS = {
    "python": frozenset({"-c", "-m"}),
    "python3": frozenset({"-c", "-m"}),
    "py": frozenset({"-c", "-m"}),
    "node": frozenset({"-e", "-p", "-r", "--eval", "--print",
                       "--require"}),
    "php": frozenset({"-r", "-S"}),
}

# Flagهای سراسری که یک مقدار مصرف می‌کنند.
#
# بدون این جدول، مقدارِ flag به‌عنوان «زیر‌دسته» اشتباه گرفته
# می‌شد و کل Command Policy دور زده می‌شد:
# «git -c core.pager=x checkout .» مقدارِ x را به‌جای checkout
# می‌دید و مجاز اعلام می‌شد.
COMMAND_POLICY_GLOBAL_VALUE_FLAGS = {
    "git": frozenset({"-c"}),
}


def command_policy_denied_subcommand(tool, argv):
    # Phase 1 — Command Policy: Fail Closed.
    #
    # دو چیز جدا باید سنجیده شود:
    #   1) flag سراسری که کد دلخواه را تزریق می‌کند (مثل
    #      «python -c» یا «node -e»)؛
    #   2) زیر‌دسته‌ای که فایل پروژه را غیرقابل‌بازگشت
    #      تغییر می‌دهد (مثل «git checkout» یا «npm run»).
    #
    # flagهای سراسری تا اولین توکنِ بدون «-» جاری هستند؛ دقیقاً
    # همان‌جایی که مفسرها کد را قبول می‌کنند. flagهایی که یک
    # مقدار مصرف می‌کنند، مقدارشان را هم می‌بلعند تا به‌اشتباه
    # زیر‌دسته شمرده نشوند.
    #
    # خروجی: None یا دلیل مسدودبودن.
    tokens = [str(item) for item in list(argv)[1:]]

    blocked_flags = COMMAND_POLICY_BLOCKED_FLAGS.get(tool)
    value_flags = COMMAND_POLICY_GLOBAL_VALUE_FLAGS.get(tool) or frozenset()

    subcommand = None

    index = 0

    while index < len(tokens):
        text = tokens[index]

        if not text.startswith("-"):
            subcommand = text.lower()
            break

        if blocked_flags and text in blocked_flags:
            return (
                "flag «" + text + "» برای «" + tool + "» "
                "طبق Command Policy مسدود است: این flag کد "
                "دلخواه را از خط فرمان اجرا می‌کند، قابل بازرسی "
                "در Plan نیست و Undo ندارد."
            )

        if text in value_flags:
            # این flag یک مقدار مصرف می‌کند؛ آن مقدار نباید
            # زیر‌دسته محسوب شود.
            index += 2
            continue

        index += 1

    blocked = COMMAND_POLICY_BLOCKED_SUBCOMMANDS.get(tool)

    if not blocked:
        return None

    if subcommand is None:
        # ابزار بدون زیر‌دسته اجرا نمی‌شود مگر آنکه
        # صریحاً مجاز اعلام شده باشد (مثل pip install که
        # همیشه زیر‌دسته دارد و در فهرست مسدود است).
        return None

    if "__all__" in blocked or subcommand in blocked:
        return (
            "زیر‌دستهٔ «" + subcommand + "» برای «" + tool + "» "
            "طبق Command Policy مسدود است: این عملیات "
            "غیرقابل‌بازگشت است و Undo ندارد."
        )

    return None


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


def looks_like_file_path(relative_path):
    # heuristic کم‌تهاجم برای تشخیص «این مسیر نام یک فایل
    # را دارد، نه یک پوشه».
    #
    # عمداً فقط آخرین بخش مسیر بررسی می‌شود و فقط وقتی
    # پسوند آن یک پسوند شناخته‌شده‌ی متنی/کد باشد. نام‌های
    # رایج پوشه (docs, src, assets, node_modules) و حتی
    # نام‌های دارای نقطه که پسوندشان شناخته‌شده نیست
    # (مثل my.folder) بدون تغییر رفتار می‌مانند.
    name = os.path.basename(
        os.path.normpath(str(relative_path or ""))
    )

    if not name:
        return False

    extension = os.path.splitext(name)[1].lower()

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
    max_entries=MAX_EXPLORE_ENTRIES
):
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


def create_directory(
    project_root,
    relative_path
):
    # Phase C: Emergency Stop قبل از ساخت پوشه.
    if is_stop_requested():
        remember_stop_blocked_action(
            tool="create_directory",
            action_type="create_directory",
            args={"path": relative_path},
            project_root=project_root,
            target=relative_path,
        )

        raise RuntimeError(EMERGENCY_STOP_MESSAGE)

    # ساخت پوشه (و پوشه‌های والد) داخل پروژه.
    root = os.path.abspath(project_root)

    if not relative_path or not str(relative_path).strip():
        raise ValueError("مسیر پوشه خالی است.")

    full_path = resolve_project_path(root, relative_path)

    if os.path.isfile(full_path):
        raise ValueError(
            "مسیر موردنظر یک فایل است، نه پوشه: "
            + str(relative_path)
        )

    # ساخت پوشه‌ای که نامش مشخصاً نام یک فایل است
    # (مثل index.html) همان ریشه‌ی خطای WinError 5 در
    # Multi-file Write است: مدل به‌جای فایل، پوشه می‌سازد و
    # بعد os.replace روی پوشه شکست می‌خورد. اینجا پیش از
    # هر تغییری روی دیسک رد می‌شود تا بازخورد روشن به مدل
    # برگردد. مسیرهای از قبل موجودِ پوشه دست‌نخورده می‌مانند.
    if (
        not os.path.isdir(full_path)
        and looks_like_file_path(relative_path)
    ):
        raise ValueError(
            "نام مسیر شبیه نام فایل است و ساختن آن به‌عنوان "
            "پوشه اشتباه است: "
            + str(relative_path)
        )

    # Phase A (Safety Foundation): وضعیت قبلی مسیر.
    existed_before = os.path.isdir(full_path)

    os.makedirs(full_path, exist_ok=True)

    # Verify واقعی: پوشه باید روی دیسک وجود داشته باشد.
    if not os.path.isdir(full_path):
        raise RuntimeError(
            "Verify ناموفق: پوشه ساخته نشد."
        )

    # Phase A (Safety Foundation): ثبت passive در Action Journal.
    record_action(
        action_type="create_directory",
        tool="create_directory",
        args={"path": relative_path},
        project_root=root,
        target=relative_display(root, full_path),
        before_state={
            "existed_before": existed_before,
            "type": "dir",
        },
        execution_state=ACTION_STATE_DONE,
        verify_state=ACTION_VERIFY_OK,
        result_summary=(
            "create_directory: "
            + relative_display(root, full_path)
        ),
    )

    return {
        "operation": "create_directory",
        "relative_path": relative_display(root, full_path),
        "full_path": full_path,
        "verified": True,
    }


def move_entry(
    project_root,
    source_path,
    target_path,
    overwrite=False
):
    # Phase C: Emergency Stop قبل از هر انتقال/Backup.
    if is_stop_requested():
        remember_stop_blocked_action(
            tool="move_entry",
            action_type="move",
            args={
                "source": source_path,
                "target": target_path,
            },
            project_root=project_root,
            target=target_path,
        )

        raise RuntimeError(EMERGENCY_STOP_MESSAGE)

    # Rename/Move واقعی داخل پروژه.
    # مبدأ و مقصد هر دو باید داخل Project Root باشند.
    root = os.path.abspath(project_root)

    if not os.path.isdir(root):
        raise ValueError(
            "پوشه پروژه پیدا نشد: " + str(project_root)
        )

    source = resolve_project_path(root, source_path)
    target = resolve_project_path(root, target_path)

    if not os.path.exists(source):
        raise FileNotFoundError(
            "مبدأ پیدا نشد: " + str(source_path)
        )

    if os.path.abspath(source) == os.path.abspath(target):
        raise ValueError(
            "مبدأ و مقصد یکسان هستند."
        )

    # پوشه را نمی‌توان داخل خودش منتقل کرد.
    if os.path.isdir(source):
        if is_inside_project(source, target):
            raise ValueError(
                "انتقال پوشه به داخل خودش ممکن نیست."
            )

    # Phase A (Safety Foundation): مسیر Backup احتمالی مقصد
    # overwrite‌شده (برای Journal).
    overwrite_backup_path = None

    if os.path.exists(target):
        if not overwrite:
            raise ValueError(
                "مقصد از قبل وجود دارد: " + str(target_path)
            )

        if os.path.isdir(target):
            raise ValueError(
                "مقصد یک پوشه موجود است و overwrite "
                "برای پوشه انجام نمی‌شود: "
                + str(target_path)
            )

        # Phase A (Safety Foundation): مسیر Backup مقصد
        # overwrite‌شده برای ثبت در Journal نگه داشته می‌شود.
        overwrite_backup_path = create_backup(root, target)
        os.remove(target)

    parent = os.path.dirname(target)

    if not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    if not is_inside_project(root, parent):
        raise ValueError(
            "مسیر مقصد خارج از پروژه انتخاب‌شده است."
        )

    source_hash = None

    if os.path.isfile(source):
        source_hash = sha1_of_file(source)

    shutil.move(source, target)

    # Verify واقعی روی دیسک.
    verified = (
        (not os.path.exists(source))
        and os.path.exists(target)
    )

    target_hash = None

    if os.path.isfile(target):
        target_hash = sha1_of_file(target)

    if verified and source_hash and target_hash:
        verified = source_hash == target_hash

    if not verified:
        raise RuntimeError(
            "Verify ناموفق: انتقال انجام نشد."
        )

    # Phase A (Safety Foundation): ثبت passive در Action Journal.
    record_action(
        action_type="move",
        tool="move_entry",
        args={
            "source": source_path,
            "target": target_path,
        },
        project_root=root,
        target=relative_display(root, target),
        backup_path=(
            relative_display(root, overwrite_backup_path)
            if overwrite_backup_path else None
        ),
        before_state={
            "existed_before": True,
            "type": (
                "dir" if os.path.isdir(target) else "file"
            ),
            "sha1": source_hash,
            "moved_from": relative_display(root, source),
            "restore_hint": relative_display(root, source),
        },
        execution_state=ACTION_STATE_DONE,
        verify_state=ACTION_VERIFY_OK,
        result_summary=(
            "move: "
            + relative_display(root, source)
            + " -> "
            + relative_display(root, target)
        ),
    )

    return {
        "operation": "move",
        "source": relative_display(root, source),
        "target": relative_display(root, target),
        "type": "dir" if os.path.isdir(target) else "file",
        "verified": True,
        "same_content": (
            source_hash == target_hash
            if source_hash and target_hash else None
        ),
    }


def delete_entry(
    project_root,
    relative_path,
    recursive=False
):
    # Phase C: Emergency Stop قبل از حذف/Backup/انتقال به Trash.
    if is_stop_requested():
        remember_stop_blocked_action(
            tool="delete_entry",
            action_type="delete",
            args={
                "path": relative_path,
                "recursive": recursive,
            },
            project_root=project_root,
            target=relative_path,
        )

        raise RuntimeError(EMERGENCY_STOP_MESSAGE)

    # Delete واقعی:
    # - فایل: ابتدا Backup کنار فایل (.agent-backup) و بعد حذف.
    # - پوشه: به پوشه بازگردانی‌پذیر .agent-trash منتقل می‌شود
    #   (حذف بازگشت‌پذیر؛ بدون نابودی محتوا).
    root = os.path.abspath(project_root)

    if not os.path.isdir(root):
        raise ValueError(
            "پوشه پروژه پیدا نشد: " + str(project_root)
        )

    full_path = resolve_project_path(root, relative_path)

    if os.path.abspath(full_path) == root:
        raise ValueError(
            "ریشه پروژه قابل حذف نیست."
        )

    if not os.path.exists(full_path):
        raise FileNotFoundError(
            "مسیر پیدا نشد: " + str(relative_path)
        )

    if os.path.isfile(full_path):
        backup_path = create_backup(root, full_path)

        # ثبت رویداد قبل از حذف فیزیکی؛ شکست ثبت => حذف انجام نمی‌شود.
        journal_id = record_action(
            action_type="delete",
            tool="delete_entry",
            args={"path": relative_path, "recursive": False},
            project_root=root,
            target=relative_display(root, full_path),
            backup_path=(
                relative_display(root, backup_path)
                if backup_path else None
            ),
            before_state={
                "existed_before": True,
                "type": "file",
                "sha1": (
                    sha1_of_file(backup_path)
                    if backup_path else None
                ),
                "restore_hint": (
                    relative_display(root, backup_path)
                    if backup_path else None
                ),
            },
            execution_state=ACTION_STATE_PLANNED,
            verify_state=None,
            result_summary=(
                "delete (file) planned: "
                + relative_display(root, full_path)
            ),
        )

        if not journal_id:
            raise RuntimeError(
                "ثبت رویداد Journal شکست خورد؛ حذف انجام نشد."
            )

        os.remove(full_path)

        if os.path.exists(full_path):
            raise RuntimeError(
                "Verify ناموفق: فایل حذف نشد."
            )

        # ثبت نهایی پس از حذف موفق.
        final_journal_id = record_action(
            action_type="delete",
            tool="delete_entry",
            args={"path": relative_path, "recursive": False},
            project_root=root,
            target=relative_display(root, full_path),
            backup_path=(
                relative_display(root, backup_path)
                if backup_path else None
            ),
            before_state={
                "existed_before": True,
                "type": "file",
                "sha1": (
                    sha1_of_file(backup_path)
                    if backup_path else None
                ),
                "restore_hint": (
                    relative_display(root, backup_path)
                    if backup_path else None
                ),
            },
            execution_state=ACTION_STATE_DONE,
            verify_state=ACTION_VERIFY_OK,
            result_summary=(
                "delete (file): "
                + relative_display(root, full_path)
            ),
        )

        if not final_journal_id:
            # بازگردانی فوری از Backup در صورت شکست ثبت نهایی.
            try:
                if backup_path and os.path.isfile(backup_path):
                    with open(backup_path, "rb") as f:
                        data = f.read()
                    with open(full_path, "wb") as f:
                        f.write(data)
            except Exception:
                pass
            raise RuntimeError(
                "ثبت نهایی Journal شکست خورد؛ فایل بازیابی شد. "
                "Backup: " + str(backup_path)
            )

        return {
            "operation": "delete",
            "type": "file",
            "relative_path": relative_display(root, full_path),
            "backup_path": (
                relative_display(root, backup_path)
                if backup_path else None
            ),
            "verified": True,
        }

    if not recursive:
        raise ValueError(
            "برای حذف پوشه باید recursive=True باشد؛ "
            "هیچ تغییری انجام نشد."
        )

    trash_root = os.path.join(root, TRASH_DIRNAME)
    os.makedirs(trash_root, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = os.path.basename(full_path.rstrip("\\/")) or "item"
    destination = os.path.join(
        trash_root, stamp + "_" + name
    )

    shutil.move(full_path, destination)

    if os.path.exists(full_path):
        raise RuntimeError(
            "Verify ناموفق: پوشه حذف نشد."
        )

    # Phase A (Safety Foundation): ثبت passive (حذف بازگردانی‌پذیر).
    record_action(
        action_type="delete",
        tool="delete_entry",
        args={"path": relative_path, "recursive": True},
        project_root=root,
        target=relative_display(root, full_path),
        before_state={
            "existed_before": True,
            "type": "dir",
            "restore_hint": relative_display(root, destination),
        },
        execution_state=ACTION_STATE_DONE,
        verify_state=ACTION_VERIFY_OK,
        result_summary=(
            "delete (dir): "
            + relative_display(root, full_path)
        ),
    )

    return {
        "operation": "delete",
        "type": "dir",
        "relative_path": relative_display(root, full_path),
        "trashed_to": relative_display(root, destination),
        "verified": True,
    }


def split_command(command):
    # تقسیم دستور به آرگومان‌ها بدون اجرای Shell.
    # کوتیشن‌ها حفظ می‌شوند تا مسیرهای ویندوزی
    # (مثل "C:\path\file.php") درست منتقل شوند.
    if isinstance(command, (list, tuple)):
        return [str(item) for item in command]

    text = "" if command is None else str(command).strip()

    if not text:
        raise ValueError("دستور خالی است.")

    tokens = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', text)

    cleaned = []

    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1]:
            if token[0] in ("\"", "'"):
                token = token[1:-1]

        if token:
            cleaned.append(token)

    if not cleaned:
        raise ValueError("دستور خالی است.")

    return cleaned


def run_command(
    project_root,
    command,
    cwd=None,
    timeout=DEFAULT_COMMAND_TIMEOUT,
    allowed_tools=ALLOWED_COMMAND_TOOLS
):
    # Phase C: Emergency Stop قبل از شروع دستور جدید.
    # هیچ process در حال اجرایی kill نمی‌شود؛ فقط از شروع
    # دستور جدید جلوگیری می‌شود.
    if is_stop_requested():
        remember_stop_blocked_action(
            tool="run_command",
            action_type="run_command",
            args={
                "command": command,
                "cwd": cwd,
            },
            project_root=project_root,
            target=cwd,
        )

        raise RuntimeError(EMERGENCY_STOP_MESSAGE)

    # اجرای Command داخل Project Scope.
    # - shell=False (بدون تفسیر Shell)
    # - پیش‌فرض cwd = ریسه پروژه، یا زیرپوشه‌ای داخل پروژه
    # - فقط ابزارهای شناخته‌شده پروژه اجازه اجرا دارند
    # نتیجه شامل returncode و خروجی واقعی است (برای Verify).
    root = os.path.abspath(project_root)

    if not os.path.isdir(root):
        raise ValueError(
            "پوشه پروژه پیدا نشد: " + str(project_root)
        )

    workdir = (
        resolve_project_path(root, cwd)
        if cwd else root
    )

    if not os.path.isdir(workdir):
        raise ValueError(
            "مسیر اجرای دستور پیدا نشد: " + str(cwd)
        )

    argv = split_command(command)
    tool = os.path.basename(argv[0]).lower()

    if tool.endswith(".exe"):
        tool = tool[:-4]

    result = {
        "operation": "command",
        "command": " ".join(argv),
        "cwd": relative_display(root, workdir),
        "tool": tool,
        "allowed": True,
        "executed": False,
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "duration": None,
        "success": False,
        "error": None,
    }

    if allowed_tools is not None and tool not in allowed_tools:
        result["allowed"] = False
        result["error"] = (
            "این ابزار در فهرست ابزارهای مجاز نیست: "
            + tool
            + "\nابزارهای مجاز: "
            + ", ".join(allowed_tools)
        )
        return result

    # Phase 1 — Command Policy: argv[0] به‌تنهایی کافی نیست.
    # زیر‌دسته‌های غیرقابل‌بازگشت (نصب Package یا
    # تغییر/پاک‌کردن tracked files) پیش از هر subprocess
    # و پیش از هر تغییری روی دیسک رد می‌شوند.
    policy_denial = command_policy_denied_subcommand(tool, argv)

    if policy_denial:
        result["allowed"] = False
        result["error"] = policy_denial
        return result

    started = time.time()

    try:
        completed = subprocess.run(
            argv,
            shell=False,
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout
        )

    except FileNotFoundError:
        result["error"] = (
            "ابزار در PATH پیدا نشد: " + tool
        )

        return result

    except subprocess.TimeoutExpired:
        result["error"] = (
            "دستور در "
            + str(timeout)
            + " ثانیه تمام نشد و متوقف شد."
        )

        return result

    except OSError as e:
        result["error"] = (
            "اجرای دستور ممکن نشد: " + str(e)
        )

        return result

    result["duration"] = round(time.time() - started, 2)
    result["executed"] = True
    result["returncode"] = completed.returncode
    result["success"] = completed.returncode == 0

    stdout = mask_sensitive_content(
        (completed.stdout or "").strip()
    )
    stderr = mask_sensitive_content(
        (completed.stderr or "").strip()
    )

    if len(stdout) > MAX_COMMAND_OUTPUT_CHARS:
        stdout = stdout[:MAX_COMMAND_OUTPUT_CHARS] + "\n…"

    if len(stderr) > MAX_COMMAND_OUTPUT_CHARS:
        stderr = stderr[:MAX_COMMAND_OUTPUT_CHARS] + "\n…"

    result["stdout"] = stdout
    result["stderr"] = stderr

    # Phase A (Safety Foundation): ثبت passive دستور اجراشده.
    # run_command غیرقابل‌بازگشت است؛ این موضوع فقط ثبت
    # می‌شود و هیچ تغییری در اجرای دستور ایجاد نمی‌کند.
    record_action(
        action_type="run_command",
        tool="run_command",
        args={
            "command": result.get("command"),
            "cwd": result.get("cwd"),
        },
        project_root=root,
        target=result.get("cwd"),
        before_state={"undoable": False},
        execution_state=(
            ACTION_STATE_DONE
            if result.get("success")
            else ACTION_STATE_FAILED
        ),
        verify_state=verify_state_from_bool(
            result.get("success")
        ),
        result_summary=(
            "ابزار: "
            + str(result.get("tool"))
            + " | کد خروج: "
            + str(result.get("returncode"))
        ),
    )

    return result


def format_command_report(result):
    # گزارش متنی نتیجه اجرای Command.
    lines = [
        "دستور: " + result.get("command", ""),
        "مسیر اجرا: " + str(result.get("cwd", "")),
    ]

    if not result.get("allowed"):
        lines.append("اجرا نشد: " + str(result.get("error")))
        return "\n".join(lines)

    if result.get("error"):
        lines.append("خطا: " + str(result.get("error")))
        return "\n".join(lines)

    lines.append(
        "کد خروج: " + str(result.get("returncode"))
    )
    lines.append(
        "زمان اجرا: " + str(result.get("duration")) + " ثانیه"
    )

    if result.get("stdout"):
        lines.append("")
        lines.append("STDOUT:")
        lines.append(result["stdout"])

    if result.get("stderr"):
        lines.append("")
        lines.append("STDERR:")
        lines.append(result["stderr"])

    lines.append("")
    lines.append(
        "Verify: "
        + (
            "دستور با کد خروج 0 تمام شد."
            if result.get("success")
            else "دستور با کد خروج غیرصفر تمام شد."
        )
    )

    return "\n".join(lines)


# ---------------------------------------------------------
# Intent routing (Explore / Execute)
# ---------------------------------------------------------

# کلمات کلیدی درخواست‌ها (فارسی و انگلیسی).
LIST_KEYWORDS = (
    "لیست",
    "محتویات",
    "محتوای پوشه",
    "چه فایل",
    "چه پوشه",
    "فایلها",
    "فایلهای",
    "پوشه",
    "پوشهها",
    "dir",
    "directory",
    "list ",
    "folder",
    "contents",
)

TREE_KEYWORDS = (
    "ساختار",
    "درخت",
    "tree",
    "structure",
    "explore",
    "کاوش",
    "شناسایی",
)

SEARCH_KEYWORDS = (
    "جستجو",
    "جستوجو",
    "search",
    "grep",
    "پیدا کن",
    "پیداکن",
    "کجاست",
    "از کجا",
    "find ",
)

DEPENDENCY_KEYWORDS = (
    "وابستگی",
    "dependenc",
    "import",
    "require",
    "include",
    "uses",
)

READ_KEYWORDS = (
    "بخوان",
    "خواندن",
    "بررسی کن",
    "تحلیل",
    "مقایسه",
    "مرور",
    "read",
    "analyze",
    "analyse",
    "review",
    "compare",
)

CREATE_KEYWORDS = (
    "بساز",
    "ایجاد کن",
    "ایجاد یک",
    "فایل جدید",
    "پوشه جدید",
    "create",
    "new file",
    "generate",
)

EDIT_KEYWORDS = (
    "جایگزین",
    "replace",
)

MOVE_KEYWORDS = (
    "تغییر نام",
    "rename",
    "انتقال",
    "منتقل کن",
    "move",
)

DELETE_KEYWORDS = (
    "حذف",
    "پاک کن",
    "delete",
    "remove",
)

COMMAND_KEYWORDS = (
    "اجرا کن",
    "اجرای دستور",
    "دستور زیر",
    "run:",
    "execute:",
)

CONTENT_KEYWORDS = (
    "مقاله",
    "مستند",
    "readme",
    "documentation",
    "توضیحات",
    "متن",
)


def normalize_request_text(text):
    # نرمال‌سازی متن فارسی/انگلیسی برای تشخیص intent.
    text = "" if text is None else str(text)

    for char in ("\u200c", "\u200d", "\u200e", "\u200f"):
        text = text.replace(char, "")

    text = text.replace("ي", "ی").replace("ك", "ک")

    return text.lower()


def contains_any(haystack, keywords):
    return any(
        keyword in haystack for keyword in keywords
    )


ABSOLUTE_PATH_PATTERN = re.compile(
    r"[A-Za-z]:[\\/][^\s\"'<>|?*]+"
)

RELATIVE_TOKEN_PATTERN = re.compile(
    r"(?<![\w.\\/:-])"
    r"([A-Za-z0-9_.\-]+(?:[\\/][A-Za-z0-9_.\-]+)*)"
    r"(?![\w])"
)


def extract_path_candidates(user_request):
    # استخراج کاندیدهای مسیر از متن درخواست
    # (هم مسیر مطلق ویندوزی و هم مسیر نسبی).
    raw = "" if user_request is None else str(user_request)

    found = []

    for match in ABSOLUTE_PATH_PATTERN.finditer(raw):
        found.append(match.group(0))

    for match in RELATIVE_TOKEN_PATTERN.finditer(raw):
        found.append(match.group(1))

    results = []
    seen = set()

    for item in found:
        token = item.strip().strip('"').strip("'")
        token = token.strip("!؟?,;:()[]{}«»،.")

        if not token:
            continue

        key = token.lower()

        if key in seen:
            continue

        seen.add(key)
        results.append(token)

    return results


def looks_like_path_token(token):
    # آیا این توکن شبیه مسیر/نام فایل است؟ (برای Create/Move)
    if re.search(r"[\\/]", token):
        return True

    return bool(
        re.match(
            r"^[A-Za-z0-9_.\-]+\.[A-Za-z0-9]{1,10}$",
            token
        )
    )


def resolve_existing_entries(project_root, user_request):
    # کاندیدهایی که واقعاً در پروژه وجود دارند
    # (فایل یا پوشه). مسیرهای خارج از پروژه حذف می‌شوند.
    root = os.path.abspath(project_root)
    entries = []

    for token in extract_path_candidates(user_request):
        try:
            full_path = safe_path(root, token)

        except (ValueError, TypeError, OSError):
            continue

        if not is_inside_project(root, full_path):
            continue

        if os.path.isdir(full_path):
            entry_type = "dir"
        elif os.path.isfile(full_path):
            entry_type = "file"
        else:
            continue

        entries.append({
            "token": token,
            "type": entry_type,
            "full_path": full_path,
            "relative_path": relative_display(root, full_path),
        })

    return entries


def build_report(
    stage,
    project_name,
    project_root,
    lines,
    verify_lines=None
):
    # ساخت Report یکنواخت:
    # Explore → Understand → Plan → Execute → Verify → Report
    parts = [
        "مرحله: " + stage,
        "پروژه: " + str(project_name),
        "ریشه پروژه: " + str(project_root),
        "",
    ]

    parts.extend(lines)

    if verify_lines:
        parts.append("")
        parts.append("Verify:")
        parts.extend(verify_lines)

    return "\n".join(parts)


READ_ONLY_HINT = (
    "این عملیات فقط در حالت «اعمال تغییر» مجاز است؛ "
    "در حالت «فقط بررسی» هیچ تغییری انجام نمی‌شود."
)


def is_write_allowed(permission):
    # Phase D — Safety Gate: قانون Fail Closed.
    # فقط و فقط اگر صریحاً boolean True باشد مجوز داده می‌شود.
    # مقادیر None، اعداد، رشته‌ها یا اشیاء دیگر فاقد مجوز هستند.
    return permission is True


# قرارداد Apply / Review (مرحله ۳):
#
#   Review (فقط بررسی) → read-only: فقط Read/Analysis/Plan.
#                          هیچ mutation، هیچ Pending Action قابل
#                          اجرا، هیچ Planِ قابل تأیید.
#
#   Apply (اعمال تغییر) → تنها یک مسیر canonical:
#                          Plan → Re-validation → Approval
#                          (یا autonomous) → Execute → Verify.
#
# مسیر Legacy از این پس هیچ Write مستقیمی ندارد. قبلاً در بعضی
# شرایط (مثلاً وقتی routing حلقه را انتخاب نمی‌کرد یا وقتی مدل
# در دسترس نبود و حلقه None برمی‌گرداند) مستقیماً
# write_file را صدا می‌زد؛ یعنی Apply یک معنی دوم داشت که از
# Plan/Re-validation/Approval/Verify عبور نمی‌کرد.
LEGACY_DIRECT_WRITE_REFUSED = (
    "در حالت «اعمال تغییر»، نوشتن مستقیم از مسیر Legacy مجاز "
    "نیست؛ هیچ فایلی نوشته یا تغییر داده نشد.\n"
    "هر تغییر باید از مسیر canonical بگذرد: "
    "Plan → Re-validation → تأیید کاربر → Execute → Verify."
)


def collect_missing_path_tokens(
    project_root,
    user_request,
    existing_entries
):
    # توکن‌هایی که شبیه مسیر هستند اما در پروژه پیدا نشدند.
    found = {
        entry["token"].lower()
        for entry in existing_entries
    }

    missing = []

    for token in extract_path_candidates(user_request):
        if not looks_like_path_token(token):
            continue

        if token.lower() in found:
            continue

        missing.append(token)

    return missing


def handle_list_intent(
    user_request,
    project_root,
    project_name
):
    # Explore: Directory Listing واقعی.
    # مسیر نسبی دقیقاً نسبت به Project Root resolve می‌شود.
    text = normalize_request_text(user_request)
    entries = resolve_existing_entries(project_root, user_request)
    directories = [
        entry for entry in entries
        if entry["type"] == "dir"
    ]

    depth = 2 if contains_any(
        text, ("درخت", "tree", "کاوش", "explore")
    ) else 1

    target = (
        directories[0]["relative_path"]
        if directories else None
    )

    if target is None:
        missing = collect_missing_path_tokens(
            project_root, user_request, entries
        )

        if missing:
            lines = [
                "مسیر درخواستی داخل این پروژه پیدا نشد: "
                + " , ".join(missing),
                "",
                "هیچ فهرستی برای آن مسیر ساخته نشد "
                "(بدون حدس).",
                "",
                "محتویات واقعی ریشه پروژه:",
            ]

            try:
                root_info = list_directory(
                    project_root, None, depth=1
                )

                lines.append(root_info["text"])

                return build_report(
                    "Explore (Directory Listing)",
                    project_name,
                    project_root,
                    lines,
                    [
                        "مسیر درخواستی روی دیسک پیدا نشد.",
                        "ریشه پروژه واقعاً خوانده شد: "
                        + str(root_info["count"])
                        + " مورد.",
                    ]
                )

            except (ValueError, OSError) as e:
                return build_report(
                    "Explore (Directory Listing)",
                    project_name,
                    project_root,
                    ["خطا: " + str(e)]
                )

    try:
        info = list_directory(
            project_root, target, depth=depth
        )

    except (ValueError, OSError) as e:
        return build_report(
            "Explore (Directory Listing)",
            project_name,
            project_root,
            ["خطا: " + str(e)]
        )

    lines = [
        "نوع: Directory Listing واقعی (خوانده‌شده از دیسک)",
        "مسیر: " + info["path"],
        "عمق: " + str(depth),
        "",
        "محتویات:",
        info["text"],
    ]

    return build_report(
        "Explore (Directory Listing)",
        project_name,
        project_root,
        lines,
        [
            str(info["count"])
            + " مورد واقعاً روی دیسک خوانده شد.",
            "مسیر مطلق: " + info["full_path"],
        ]
    )


SEARCH_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that",
    "what", "where", "which", "show", "all", "file", "files",
    "search", "find", "grep", "list", "dir", "directory",
    "project", "structure", "readme", "php", "html", "css",
    "json", "txt", "true", "false", "null", "none", "int",
}


def extract_search_terms(user_request):
    # استخراج عبارت جستجو: نقل‌قول‌شده یا شناسه‌های کد.
    raw = "" if user_request is None else str(user_request)
    terms = []

    quote_patterns = (
        r'["“«](.+?)["”»]',
        r"`([^`]+)`",
    )

    scrubbed = raw

    for pattern in quote_patterns:
        for match in re.finditer(pattern, raw, re.DOTALL):
            terms.append(match.group(1).strip())

        # متن نقل‌قول‌شده از اسکن شناسه‌ها حذف می‌شود تا
        # کلمات همان عبارت دوباره به‌عنوان ترم جداگانه
        # جستجو نشوند.
        scrubbed = re.sub(
            pattern, " ", scrubbed, flags=re.DOTALL
        )

    for match in re.finditer(
        r"(?<![\w])([A-Za-z_][A-Za-z0-9_\-]{2,}"
        r"(?:\.[A-Za-z0-9]+)?)(?![\w])",
        scrubbed
    ):
        token = match.group(1)

        if token.lower() in SEARCH_STOPWORDS:
            continue

        if looks_like_path_token(token):
            continue

        terms.append(token)

    results = []
    seen = set()

    for term in terms:
        key = term.lower()

        if not term or key in seen:
            continue

        seen.add(key)
        results.append(term)

    return results[:3]


def handle_search_intent(
    user_request,
    project_root,
    project_name
):
    # Explore: جستجوی واقعی متن داخل فایل‌های پروژه.
    terms = extract_search_terms(user_request)

    if not terms:
        return build_report(
            "Explore (Search)",
            project_name,
            project_root,
            [
                "عبارت جستجو در درخواست مشخص نشد؛ "
                "برای جلوگیری از حدس، جستجویی انجام نشد.",
                "عبارت را داخل \"...\" یا `...` بنویسید.",
            ]
        )

    entries = resolve_existing_entries(project_root, user_request)
    directories = [
        entry for entry in entries
        if entry["type"] == "dir"
    ]
    subdir = (
        directories[0]["relative_path"]
        if directories else None
    )

    lines = []
    verify = []

    for term in terms:
        try:
            results = search_in_files(
                project_root,
                term,
                subdir=subdir,
                max_results=MAX_SEARCH_RESULTS
            )

        except (ValueError, OSError) as e:
            lines.append("خطا در جستجو: " + str(e))
            continue

        lines.append(
            "جستجو برای: "
            + term
            + "  (محدوده: "
            + (subdir or "کل پروژه")
            + ")"
        )
        lines.append(format_search_results(results, term))
        lines.append("")

        verify.append(
            "«"
            + term
            + "»: "
            + str(len(results))
            + " نتیجه واقعی از فایل‌های پروژه."
        )

    return build_report(
        "Explore (Search)",
        project_name,
        project_root,
        lines,
        verify
    )


def handle_dependencies_intent(
    user_request,
    project_root,
    project_name
):
    # Understand: پیدا کردن وابستگی‌ها (include/require/import).
    entries = resolve_existing_entries(project_root, user_request)
    directories = [
        entry for entry in entries
        if entry["type"] == "dir"
    ]
    subdir = (
        directories[0]["relative_path"]
        if directories else None
    )

    try:
        results = find_dependencies(
            project_root, subdir=subdir
        )

    except (ValueError, OSError) as e:
        return build_report(
            "Understand (Dependencies)",
            project_name,
            project_root,
            ["خطا: " + str(e)]
        )

    lines = [
        "محدوده: " + (subdir or "کل پروژه"),
        "الگو: include / require / import / use",
        "",
    ]

    if not results:
        lines.append("وابستگی‌ای پیدا نشد.")
    else:
        for item in results:
            lines.append(
                item["relative_path"]
                + ":"
                + str(item["line"])
                + ": "
                + item["text"]
            )

    return build_report(
        "Understand (Dependencies)",
        project_name,
        project_root,
        lines,
        [
            str(len(results))
            + " ارجاع واقعی از فایل‌های پروژه خوانده شد."
        ]
    )


def handle_read_multi_intent(
    user_request,
    project_root,
    project_name,
    allow_write=False,
    stop_event=None
):
    # Understand: خواندن چند فایل مرتبط و تحلیل بر پایه
    # محتوای واقعی آن‌ها. اگر کمتر از دو فایل واقعی باشد،
    # این مسیر درخواست را برنمی‌دارد و به مسیر قبلی Read
    # سپرده می‌شود (سازگاری با رفتار تست‌شده).
    entries = resolve_existing_entries(project_root, user_request)

    files = []

    for entry in entries:
        if entry["type"] != "file":
            continue

        if entry["relative_path"] in files:
            continue

        files.append(entry["relative_path"])

    if len(files) < 2:
        return None

    items = read_files_multi(project_root, files[:4])
    readable = [
        item for item in items
        if item["content"] is not None
    ]

    if not readable:
        return build_report(
            "Understand (Multi-file Read)",
            project_name,
            project_root,
            [
                "هیچ‌یک از فایل‌های درخواستی قابل خواندن نبود:",
            ] + [
                "- "
                + str(item["relative_path"])
                + ": "
                + str(item["error"])
                for item in items
            ]
        )

    blocks = []

    for item in readable:
        content = item["content"]

        if not allow_write:
            content = mask_sensitive_content(content)

        blocks.append(
            "BEGIN_FILE " + item["relative_path"] + "\n"
            + content
            + "\nEND_FILE " + item["relative_path"]
        )

    system_prompt = """
You are a local coding assistant.

The Python agent has already located and read
every file listed below from the project on disk.

Analyze ONLY the supplied file contents.
Find relationships, dependencies and side effects
between these files.

Do not ask the user to provide files.
Do not invent files or contents that are not given.
If the given contents are not enough to answer,
say exactly what is missing.

Answer directly and briefly.
"""

    user_prompt = (
        "USER REQUEST:\n"
        + str(user_request)
        + "\n\nPROJECT:\n"
        + str(project_name)
        + "\n\nFILES:\n\n"
        + "\n\n".join(blocks)
    )

    try:
        answer = call_model(
            system_prompt,
            user_prompt,
            stop_event=stop_event
        )

    except Exception as e:
        answer = "تحلیل مدل انجام نشد: " + str(e)

    lines = ["فایل‌های واقعاً خوانده‌شده:"]

    for item in readable:
        lines.append(
            "- "
            + item["relative_path"]
            + " ("
            + str(item["size"])
            + " bytes"
            + (", truncated" if item["truncated"] else "")
            + ")"
        )

    lines.append("")
    lines.append("تحلیل مدل بر پایه محتوای واقعی فایل‌ها:")
    lines.append(answer)

    return build_report(
        "Understand (Multi-file Read)",
        project_name,
        project_root,
        lines,
        [
            str(len(readable))
            + " فایل واقعاً از دیسک خوانده و به مدل داده شد."
        ]
    )


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


def build_execute_report(
    stage,
    project_name,
    project_root,
    details,
    extra_lines=None
):
    # Report یکنواخت برای عملیات Write/Edit/Create.
    lines = [
        "فایل: " + str(details.get("relative_path")),
        "نوع عملیات: " + str(details.get("operation", "write")),
        "مسیر مطلق: " + str(details.get("path")),
        "وضعیت قبل: "
        + (
            "فایل موجود بود (Backup گرفته شد)"
            if details.get("existed_before")
            else "فایل جدید بود"
        ),
        "Backup: "
        + str(details.get("backup_path") or "لازم نبود"),
        "تغییر اعمال شد: "
        + ("بله" if details.get("changed") else "خیر"),
    ]

    if extra_lines:
        lines.extend(extra_lines)

    diff = details.get("diff")

    if diff:
        lines.append("")
        lines.append("Diff:")
        lines.append(diff[:4000])

    return build_report(
        stage,
        project_name,
        project_root,
        lines,
        summarize_validation(details.get("validation"))
    )


def extract_edit_blocks(model_output):
    # مدل باید فقط دو بلوک برگرداند:
    # BEGIN_OLD ... END_OLD / BEGIN_NEW ... END_NEW
    pattern = (
        r"BEGIN_OLD\s*\n(.*?)\nEND_OLD\s*"
        r"\nBEGIN_NEW\s*\n(.*?)\nEND_NEW"
    )

    match = re.search(
        pattern,
        model_output or "",
        re.DOTALL
    )

    if not match:
        return None, None

    return match.group(1), match.group(2)


def handle_edit_intent(
    user_request,
    project_root,
    project_name,
    allow_write=False,
    stop_event=None
):
    # Execute: ویرایش دقیق (جایگزینی متن) + Backup + Verify.
    entries = resolve_existing_entries(project_root, user_request)
    files = [
        entry for entry in entries
        if entry["type"] == "file"
    ]

    if not files:
        return None

    target = files[0]["relative_path"]

    # Phase H — Security Boundary. این مسیر فایل را می‌خواند و
    # مستقیم به call_model می‌دهد، پس مسدودسازی باید پیش از
    # هر I/O انجام شود.
    if is_sensitive_read_target(target):
        return build_report(
            "Execute (Edit)",
            project_name,
            project_root,
            [
                sensitive_read_refusal_text(target),
                "هیچ تغییری انجام نشد.",
            ]
        )

    # Phase D — Safety Gate: بررسی قطعی مجوز قبل از خواندن و فراخوانی مدل
    if not is_write_allowed(allow_write):
        return build_report(
            "Execute (Edit)",
            project_name,
            project_root,
            [
                "فایل هدف: " + target,
                READ_ONLY_HINT,
            ]
        )

    try:
        current_content = read_file(project_root, target)

    except Exception as e:
        return build_report(
            "Execute (Edit)",
            project_name,
            project_root,
            ["خطا در خواندن فایل: " + str(e)]
        )

    model_output = call_model(
        """
You are a precise file editor.

Edit ONLY the requested part of the file.

Return ONLY these two blocks and nothing else:

BEGIN_OLD
[the exact existing text that must be replaced]
END_OLD
BEGIN_NEW
[the exact replacement text]
END_NEW

The BEGIN_OLD text must be copied exactly from the
current file, character by character, and must appear
only once inside the file.

Do not return the whole file.
Do not explain.
""",
        "USER REQUEST:\n"
        + str(user_request)
        + "\n\nPROJECT:\n"
        + str(project_name)
        + "\n\nTARGET FILE:\n"
        + target
        + "\n\nBEGIN_CURRENT_FILE\n"
        + current_content
        + "\nEND_CURRENT_FILE\n",
        stop_event=stop_event
    )

    old_text, new_text = extract_edit_blocks(model_output)

    if old_text is None:
        return build_report(
            "Execute (Edit)",
            project_name,
            project_root,
            [
                "فایل هدف: " + target,
                "مدل متن دقیق برای جایگزینی را برنگرداند؛ "
                "هیچ تغییری اعمال نشد.",
            ]
        )

    try:
        details = edit_file(
            project_root,
            target,
            old_text,
            new_text,
            return_details=True
        )

    except Exception as e:
        return build_report(
            "Execute (Edit)",
            project_name,
            project_root,
            [
                "فایل هدف: " + target,
                "ویرایش انجام نشد: " + str(e),
                "هیچ تغییری روی دیسک اعمال نشد.",
            ]
        )

    return build_execute_report(
        "Execute (Edit)",
        project_name,
        project_root,
        details,
        extra_lines=[
            "تعداد جایگزینی: "
            + str(details.get("replacements", 0))
        ]
    )


CREATE_STOPWORDS = {
    "create", "file", "files", "new", "folder", "folders",
    "directory", "dir", "a", "an", "the", "and", "with",
    "in", "to", "for", "of", "text", "readme", "md", "txt",
}


def strip_code_fences(text):
    # اگر مدل محتوا را داخل ``` گذاشت، همان محتوا
    # استخراج می‌شود (بدون دستکاری باقی متن).
    text = (text or "").strip()

    match = re.match(
        r"^```[A-Za-z0-9_+\-]*\s*\n(.*)\n```$",
        text,
        re.DOTALL
    )

    if match:
        return match.group(1)

    return text


def generate_file_content(
    project_root,
    project_name,
    relative_path,
    user_request,
    stop_event=None
):
    # تولید محتوای فایل جدید (متن/کد/مستند/مقاله).
    # مدل فقط محتوا تولید می‌کند؛ نوشتن فایل کار خود
    # Agent است (write_file).
    structure = build_project_structure(
        project_root, None, depth=1
    )

    model_output = call_model(
        """
You are a local coding assistant.

Write the complete content of ONE new file.

Return ONLY:

BEGIN_FILE
[complete file content]
END_FILE

Rules:
- Return only the file content, nothing else.
- Do not add explanations outside the blocks.
- Match the language and style of the project.
- The content must be complete and usable.
""",
        "USER REQUEST:\n"
        + str(user_request)
        + "\n\nPROJECT:\n"
        + str(project_name)
        + "\n\nNEW FILE:\n"
        + relative_path
        + "\n\nREAL PROJECT STRUCTURE (root level):\n"
        + structure
        + "\n",
        stop_event=stop_event
    )

    content = extract_file_content(model_output)

    if content is not None:
        return content

    # Fallback فقط برای فایل‌های متنی/مستنداتی؛ برای
    # فایل‌های کد محتوای نامعتبر نوشته نمی‌شود.
    extension = os.path.splitext(relative_path)[1].lower()

    if extension in (
        ".md", ".markdown", ".txt", ".rst",
        ".html", ".htm",
    ):
        raw = strip_code_fences(model_output)

        if raw.strip():
            if not raw.endswith("\n"):
                raw = raw + "\n"

            return raw

    return None


def handle_create_intent(
    user_request,
    project_root,
    project_name,
    allow_write=False,
    stop_event=None
):
    # Execute: Create (فایل جدید یا پوشه جدید).
    # اگر مسیر هدف از قبل وجود داشته باشد، این مسیر
    # درخواست را برنمی‌دارد و به مسیر Write قبلی
    # سپرده می‌شود (سازگاری با رفتار تست‌شده).
    text = normalize_request_text(user_request)
    entries = resolve_existing_entries(project_root, user_request)

    existing = {
        entry["token"].lower() for entry in entries
    }

    wants_directory = contains_any(
        text, ("پوشه", "folder", "directory")
    )

    file_candidate = None

    for token in extract_path_candidates(user_request):
        if token.lower() in existing:
            continue

        if not looks_like_path_token(token):
            continue

        file_candidate = token
        break

    dir_candidate = None

    for token in extract_path_candidates(user_request):
        lowered = token.lower()

        if lowered in existing:
            continue

        if lowered in CREATE_STOPWORDS:
            continue

        if re.match(r"^[A-Za-z0-9_.\-]+$", token) is None:
            continue

        if re.search(r"[\\/]", token) is None:
            dir_candidate = token
            break

    if file_candidate is None and dir_candidate is None:
        return None

    if not is_write_allowed(allow_write):
        return build_report(
            "Execute (Create)",
            project_name,
            project_root,
            [
                "مسیر موردنظر: "
                + str(file_candidate or dir_candidate),
                READ_ONLY_HINT,
            ]
        )

    if file_candidate is None and wants_directory:
        try:
            details = create_directory(
                project_root, dir_candidate
            )

        except Exception as e:
            return build_report(
                "Execute (Create Directory)",
                project_name,
                project_root,
                ["ساخت پوشه انجام نشد: " + str(e)]
            )

        return build_report(
            "Execute (Create Directory)",
            project_name,
            project_root,
            ["پوشه: " + details["relative_path"]],
            ["پوشه واقعاً روی دیسک ساخته شد."]
        )

    target = file_candidate or dir_candidate

    content = generate_file_content(
        project_root,
        project_name,
        target,
        user_request,
        stop_event=stop_event
    )

    if content is None:
        return build_report(
            "Execute (Create)",
            project_name,
            project_root,
            [
                "فایل هدف: " + target,
                "مدل محتوای کامل فایل را برنگرداند؛ "
                "هیچ فایلی ساخته نشد.",
            ]
        )

    try:
        details = write_file(
            project_root,
            target,
            content,
            return_details=True,
            create_parents=True
        )

    except Exception as e:
        return build_report(
            "Execute (Create)",
            project_name,
            project_root,
            [
                "فایل هدف: " + target,
                "ساخت فایل انجام نشد: " + str(e),
            ]
        )

    details["operation"] = "create"

    return build_execute_report(
        "Execute (Create)",
        project_name,
        project_root,
        details,
        extra_lines=[
            "طول محتوای نوشته‌شده: "
            + str(len(content))
            + " کاراکتر"
        ]
    )


def handle_move_intent(
    user_request,
    project_root,
    project_name,
    allow_write=False
):
    # Execute: Rename/Move.
    entries = resolve_existing_entries(project_root, user_request)

    sources = [
        entry for entry in entries
        if entry["type"] in ("file", "dir")
    ]

    if not sources:
        return None

    source = sources[0]["relative_path"]
    existing_tokens = {
        entry["token"].lower() for entry in entries
    }

    target = None

    for token in extract_path_candidates(user_request):
        lowered = token.lower()

        if lowered in existing_tokens:
            continue

        if lowered in CREATE_STOPWORDS:
            continue

        if not (
            looks_like_path_token(token)
            or re.match(r"^[A-Za-z0-9_.\-]+$", token)
        ):
            continue

        target = token
        break

    if target is None:
        return build_report(
            "Execute (Move)",
            project_name,
            project_root,
            [
                "مبدأ: " + source,
                "مقصد (نام یا مسیر جدید) در درخواست مشخص "
                "نشد؛ هیچ تغییری انجام نشد.",
            ]
        )

    if not is_write_allowed(allow_write):
        return build_report(
            "Execute (Move)",
            project_name,
            project_root,
            [
                "مبدأ: " + source,
                "مقصد: " + target,
                READ_ONLY_HINT,
            ]
        )

    try:
        details = move_entry(
            project_root, source, target
        )

    except Exception as e:
        return build_report(
            "Execute (Move)",
            project_name,
            project_root,
            [
                "مبدأ: " + source,
                "مقصد: " + target,
                "انتقال انجام نشد: " + str(e),
            ]
        )

    verify = [
        "وجود مقصد روی دیسک: بله",
        "حذف مبدأ از مسیر قبلی: بله",
    ]

    if details.get("same_content") is True:
        verify.append("تطابق محتوا (SHA1) قبل و بعد: بله")

    return build_report(
        "Execute (Move)",
        project_name,
        project_root,
        [
            "مبدأ: " + details["source"],
            "مقصد: " + details["target"],
            "نوع: " + details["type"],
        ],
        verify
    )


def handle_delete_intent(
    user_request,
    project_root,
    project_name,
    allow_write=False
):
    # Execute: Delete.
    text = normalize_request_text(user_request)
    entries = resolve_existing_entries(project_root, user_request)

    if not entries:
        return None

    target_entry = entries[0]
    recursive = contains_any(
        text,
        ("با محتویات", "recursive", "-r", "همه", "کامل")
    )

    if not is_write_allowed(allow_write):
        return build_report(
            "Execute (Delete)",
            project_name,
            project_root,
            [
                "مسیر: " + target_entry["relative_path"],
                READ_ONLY_HINT,
            ]
        )

    if target_entry["type"] == "dir" and not recursive:
        return build_report(
            "Execute (Delete)",
            project_name,
            project_root,
            [
                "مسیر: " + target_entry["relative_path"],
                "این مسیر یک پوشه است و حذف پوشه فقط با "
                "تأیید صریح انجام می‌شود.",
                "برای حذف، عبارت «با محتویات» را به درخواست "
                "اضافه کنید؛ هیچ تغییری انجام نشد.",
            ]
        )

    try:
        details = delete_entry(
            project_root,
            target_entry["relative_path"],
            recursive=recursive
        )

    except Exception as e:
        return build_report(
            "Execute (Delete)",
            project_name,
            project_root,
            [
                "مسیر: " + target_entry["relative_path"],
                "حذف انجام نشد: " + str(e),
            ]
        )

    lines = [
        "مسیر: " + details["relative_path"],
        "نوع: " + details["type"],
    ]

    verify = ["مسیر موردنظر دیگر روی دیسک وجود ندارد."]

    if details.get("backup_path"):
        lines.append("Backup: " + details["backup_path"])
        verify.append("Backup محتوا گرفته شد.")

    if details.get("trashed_to"):
        lines.append(
            "منتقل‌شده به (قابل بازگردانی): "
            + details["trashed_to"]
        )

    return build_report(
        "Execute (Delete)",
        project_name,
        project_root,
        lines,
        verify
    )


def extract_command_text(user_request):
    # استخراج دستور از درخواست:
    # - درخواست با ! شروع شود
    # - یا بعد از «اجرا کن» / «run:» بیاید
    raw = "" if user_request is None else str(user_request).strip()

    if raw.startswith("!"):
        return raw[1:].strip()

    match = re.search(
        r"(?:اجرا کن|اجرای دستور|دستور زیر|run:|execute:)"
        r"\s*[:\-]?\s*(.+)",
        raw,
        re.DOTALL
    )

    if not match:
        return ""

    command = match.group(1).strip()
    command = command.strip("`").strip()

    return command.strip()


def handle_command_intent(
    user_request,
    project_root,
    project_name,
    allow_write=False
):
    # Execute: اجرای Command داخل Project Scope.
    command = extract_command_text(user_request)

    if not command:
        return build_report(
            "Execute (Command)",
            project_name,
            project_root,
            [
                "دستور مشخص نشد؛ برای اجرا دستور را پس از "
                "«اجرا کن» یا با ! در ابتدای درخواست بنویسید."
            ]
        )

    if not is_write_allowed(allow_write):
        return build_report(
            "Execute (Command)",
            project_name,
            project_root,
            [
                "دستور: " + command,
                READ_ONLY_HINT,
            ]
        )

    try:
        result = run_command(project_root, command)

    except Exception as e:
        return build_report(
            "Execute (Command)",
            project_name,
            project_root,
            [
                "دستور: " + command,
                "اجرا ممکن نشد: " + str(e),
            ]
        )

    return build_report(
        "Execute (Command)",
        project_name,
        project_root,
        [format_command_report(result)]
    )


def run_generic_task(
    user_request,
    project_root,
    project_name,
    allow_write=False,
    stop_event=None
):
    # Router عمومی Agent.
    # خروجی: (handled, result)
    # اگر handled=False باشد، درخواست به مسیرهای
    # قبلی Read/Write سپرده می‌شود (سازگاری کامل).
    request = "" if user_request is None else str(user_request)

    if not request.strip():
        return False, ""

    text = normalize_request_text(request)

    # 1) Command
    if (
        text.lstrip().startswith("!")
        or contains_any(text, COMMAND_KEYWORDS)
    ):
        return True, handle_command_intent(
            request, project_root, project_name,
            allow_write=allow_write
        )

    # 2) Delete
    if contains_any(text, DELETE_KEYWORDS):
        result = handle_delete_intent(
            request, project_root, project_name,
            allow_write=allow_write
        )

        if result is not None:
            return True, result

    # 3) Rename / Move
    if contains_any(text, MOVE_KEYWORDS):
        result = handle_move_intent(
            request, project_root, project_name,
            allow_write=allow_write
        )

        if result is not None:
            return True, result

    # 4) Edit (جایگزینی دقیق متن)
    if contains_any(text, EDIT_KEYWORDS):
        result = handle_edit_intent(
            request, project_root, project_name,
            allow_write=allow_write,
            stop_event=stop_event
        )

        if result is not None:
            return True, result

    # 5) Create
    if contains_any(text, CREATE_KEYWORDS):
        result = handle_create_intent(
            request, project_root, project_name,
            allow_write=allow_write,
            stop_event=stop_event
        )

        if result is not None:
            return True, result

    # 6) Dependencies
    if contains_any(text, DEPENDENCY_KEYWORDS):
        return True, handle_dependencies_intent(
            request, project_root, project_name
        )

    # 7) Search
    if contains_any(text, SEARCH_KEYWORDS):
        return True, handle_search_intent(
            request, project_root, project_name
        )

    # 8) List / Tree
    if (
        contains_any(text, LIST_KEYWORDS)
        or contains_any(text, TREE_KEYWORDS)
    ):
        return True, handle_list_intent(
            request, project_root, project_name
        )

    # 9) Multi-file Read (Understand)
    if contains_any(text, READ_KEYWORDS):
        result = handle_read_multi_intent(
            request, project_root, project_name,
            allow_write=allow_write,
            stop_event=stop_event
        )

        if result is not None:
            return True, result

    return False, ""


# ---------------------------------------------------------
# Agentic Loop
# (Explore → Understand → Plan → Execute → Verify →
#  Re-plan در صورت نیاز → Report)
# ---------------------------------------------------------
#
# این بخش فقط "اضافه" میشود؛ ابزارهای قبلی و مسیرهای
# Read/Write/Edit/Backup/Rollback/Verify و safe_path
# دستنخورده میمانند و رفتار تستشده تغییر نمیکند.

# حداکثر گامهای مدل در یک درخواست (جلوگیری از loop).
# Phase H: سقف 6 گام برای یک تحلیل واقعی کم بود؛ مدل باید
# فرصت «شواهد» داشته باشد (ساختار → یافتن فایل کلیدی →
# خواندن محتوا). سقف جدید همچنان یک محدودیت قطعی است.
MAX_AGENT_STEPS = 8

# حداکثر گام اصلاحی بعد از شکست Verify.
MAX_CORRECTION_STEPS = 1

# محدودیت اندازه متن برای context مدل.
# Phase H: مقادیر قبلی (900/3500/2000/500) باعث می‌شدند سه
# Observation اول کل بودجه context را پر کنند و گزارش نهایی
# نیز وسط کار قطع شود؛ یعنی فضای صفر برای شواهد متنی.
MAX_OBSERVATION_CHARS = 1200
MAX_OBSERVATIONS_CHARS = 6000
MAX_TOOL_TEXT_CHARS = 1500
MAX_FINAL_ANSWER_CHARS = 3500
MAX_REPORT_STEP_CHARS = 1000

# Phase H: سقف تولید توکن فقط برای Agentic Loop.
# call_model به‌صورت پیش‌فرض رفتار قبلی (1600) را نگه می‌دارد تا
# مسیرهای Legacy دست‌نخورده بمانند؛ فقط حلقه تحلیلی از این
# سقف استفاده می‌کند تا گزارش مشاوره‌ای ناقص تولید نشود.
LOOP_MODEL_N_PREDICT = 3200

# حافظه کوتاه گفتگو:
# فقط در حافظه پروسه نگه داشته میشود (نه روی دیسک) و
# با restart از بین میرود.
CONVERSATION_MEMORY = []
CONVERSATION_MEMORY_LIMIT = 6
import contextvars as _cv
_CURRENT_SESSION = _cv.ContextVar("current_session", default=None)
CONVERSATION_MESSAGE_CHARS = 400

# Plan در انتظار تأیید کاربر برای عملیات تغییردهنده.
# Phase E: TTL متمرکز برای Pending Action (ثانیه).
PENDING_ACTION_TTL_SECONDS = 600
PENDING_ACTION = None
# Pending Action بین processهای Flask مشترک است.
# فایل فقط شامل همین Action موقت است و پس از
# Execute، Cancel یا انقضای درخواست جدید حذف می‌شود.
PENDING_ACTION_FILE = os.path.join(
    BASE_DIR, "pending-action.tmp.json"
)
# Phase F2: Journal بین restart پروسه هم باید بماند تا Undo/Audit گم نشود.
# فقط metadata خلاصه‌شده ذخیره می‌شود؛ محتوا/secret هرگز.
# NOTE: خود ACTION_JOURNAL_FILE بالاتر (کنار ACTION_JOURNAL) تعریف شده
# چون توابع persist/load هنگام import به آن نیاز دارند.


APPROVAL_KEYWORDS = (
    "تایید",
    "تأیید",
    "بله",
    "اوکی",
    "موافقم",
    "ادامه بده",
    "اجرا کن",
    "approve",
    "approved",
    "ok",
    "okay",
    "yes",
    "continue",
)

CANCEL_KEYWORDS = (
    "لغو",
    "انصراف",
    "نخیر",
    "نه",
    "بیخیال",
    "cancel",
    "abort",
)

# پاسخ‌های کوتاهِ «برگرداندن آخرین تغییر».
#
# این کلیدواژه‌ها فقط یک «درخواست Undo» را تشخیص می‌دهند؛ خودِ
# Undo را undo_last_mutating_action انجام می‌دهد که Safety خودش
# را دارد (Emergency Stop، read-only، اعتبارسنجی candidate، journal
# و Fail-Closed). اینجا هیچ مسیر موازی یا ایمنی جدیدی ساخته
# نمی‌شود؛ فقط دروازهٔ ورودیِ همان تابع موجود است.
UNDO_KEYWORDS = (
    "واگرد",
    "برگشت",
    "بازگردان",
    "بازگردانی",
    "undo",
    "revert",
)

# نشانههای درخواست چندمرحلهای (Agentic).
LOOP_TRIGGER_KEYWORDS = (
    "مرحله به مرحله",
    "چند مرحله",
    "agentic",
    "agent loop",
    "خودت تصمیم",
    "خودت بررسی",
    "کاوش",
    "explore",
    "investigate",
    "then",
    "سپس",
    "بعد از آن",
    "و بعد از",
    "بررسی کن و",
    "تحلیل کن و",
)

ACTION_VERB_KEYWORDS = (
    "تغییر",
    "ویرایش",
    "بنویس",
    "جایگزین",
    "بساز",
    "ایجاد",
    "حذف",
    "پاک",
    "منتقل",
    "تغییر نام",
    "rename",
    "move",
    "write",
    "edit",
    "create",
    "delete",
    "اجرا",
    "اصلاح",
    "درست کن",
)

READ_VERB_KEYWORDS = (
    "بخوان",
    "بررسی",
    "لیست",
    "جستجو",
    "کاوش",
    "پیدا کن",
    "ساختار",
    "explore",
    "read",
    "search",
    "list",
    "structure",
    "analyze",
    "تحلیل",
)

# Phase H: قصد تحلیلی/مشاوره‌ای کاربر.
# تشخیص قبلی کاملاً کلیدواژه‌محور بود: هر درخواست طبیعی مثل
# «مشکل این سایت چیست؟» هیچ Keyword ساختاری‌ای نداشت و به
# Agentic Loop نمی‌رسید. این فهرست فقط «قصد» را تشخیص می‌دهد
# و هیچ Keyword tuple موجودی را تغییر نمی‌دهد.
ANALYSIS_INTENT_KEYWORDS = (
    # فارسی
    "بررسی", "بررسی کن", "تحلیل", "تحلیل کن",
    "مشکل", "مشکلات", "ایراد", "ایرادها", "اشکال",
    "ضعف", "نقاط ضعف", "ریسک", "ریسک ها",
    "بهبود", "بهبود دهی", "بهتر", "بهتر کن",
    "پیشنهاد", "پیشنهادها", "پیشنهادات",
    "توصیه", "توصیه ها", "نظر", "ارزیابی",
    "وضعیت", "سلامت", "کیفیت",
    # English
    "analyze", "analyse", "review", "audit", "assess",
    "evaluation", "problem", "problems", "issue",
    "issues", "improve", "improvement", "improvements",
    "recommend", "recommendation", "recommendations",
    "suggest", "suggestion", "suggestions", "advice",
    "risk", "risks", "weakness", "weaknesses",
    "health", "overview", "what is wrong", "how to improve",
)

# Phase H: درخواست دامنه‌دار (Project-wide) است، نه تک‌فایلی.
# وقتی کاربر می‌گوید «این پروژه / این سایت»، حتی اگر نامی از
# فایل‌ها هم آمده باشد، درخواست سطح پروژه است و باید در Loop
# تحلیل شود، نه با خواندن یک فایل تنها.
PROJECT_WIDE_KEYWORDS = (
    "این پروژه", "این سایت", "این وب سایت", "کل پروژه",
    "کل سایت", "کل فایل ها", "کل فایلها", "کل کد",
    "پروژه", "سایت", "وب سایت", "وردپرس",
    "به طور کلی", "عمومی",
    "this project", "this site", "whole project",
    "whole site", "entire project", "entire site",
    "project wide", "site wide",
)


def is_analysis_request(user_request):
    # آیا کاربر قصد ارزیابی/مشاوره دارد (صرفاً خواندن نیست)؟
    text = normalize_request_text(user_request)

    return contains_any_word(
        text, ANALYSIS_INTENT_KEYWORDS
    )


def is_project_wide_request(user_request):
    text = normalize_request_text(user_request)

    return contains_any_word(
        text, PROJECT_WIDE_KEYWORDS
    )


def names_existing_file_target(
    project_root,
    user_request
):
    # آیا کاربر صریحاً یک فایل موجودِ مشخص را هدف گرفته است؟
    # مسیر تک‌فایلیِ روشن، مسیر Legacyِ خوب و تست‌شده را حفظ
    # می‌کند؛ برای آن نیازی به Loop نیست.
    try:
        entries = resolve_existing_entries(
            project_root, user_request
        )

    except Exception:
        return False

    for entry in entries:
        if entry.get("type") == "file":
            return True

    return False


def keyword_matches_text(keyword, text):
    # برای کلمات کلیدی انگلیسی مرزِ کلمه بررسی میشود تا
    # مثلاً «read» داخل readme-test.txt تطبیق داده نشود.
    if all(ord(ch) < 128 for ch in keyword):
        pattern = (
            r"(?<![A-Za-z0-9_])"
            + re.escape(keyword)
            + r"(?![A-Za-z0-9_])"
        )

        return re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        ) is not None

    return keyword in text


def contains_any_word(text, keywords):
    return any(
        keyword_matches_text(keyword, text)
        for keyword in keywords
    )


def remember_conversation_message(
    role,
    text,
    project_name=None,
    session_id=None
):
    # حافظه کوتاه (in-memory) برای درخواستهای پشتسرهم.
    # سیستم حافظه دائمی نیست و روی دیسک ذخیره نمیشود.
    message_text = "" if text is None else str(text).strip()

    if not message_text:
        return

    if len(message_text) > CONVERSATION_MESSAGE_CHARS:
        message_text = (
            message_text[:CONVERSATION_MESSAGE_CHARS] + " …"
        )

    if session_id is None:
        session_id = _CURRENT_SESSION.get()

    import threading as _th
    with _th.Lock():
        CONVERSATION_MEMORY.append({
            "role": role,
            "text": message_text,
            "project": project_name,
            "session": session_id,
        })

        while len(CONVERSATION_MEMORY) > CONVERSATION_MEMORY_LIMIT:
            CONVERSATION_MEMORY.pop(0)


def clear_conversation_memory(session_id=None, project_name=None):
    if session_id is None:
        session_id = _CURRENT_SESSION.get()
    import threading as _th
    with _th.Lock():
        if session_id is None and project_name is None:
            del CONVERSATION_MEMORY[:]
            return
        kept = []
        for m in CONVERSATION_MEMORY:
            if session_id is not None and m.get("session") != session_id:
                kept.append(m)
                continue
            if project_name is not None and m.get("project") != project_name:
                kept.append(m)
                continue
        del CONVERSATION_MEMORY[:]
        CONVERSATION_MEMORY.extend(kept)


def get_recent_conversation_text(
    project_name=None,
    current_request=None,
    session_id=None
):
    if session_id is None:
        session_id = _CURRENT_SESSION.get()

    # فقط پیامهای همین نشست و همین پروژه (یا بدون پروژه).
    messages = [
        message for message in CONVERSATION_MEMORY
        if (session_id is None or message.get("session") in (None, session_id))
        and (not project_name or message.get("project") in (None, project_name))
    ]

    # پیام کاربر جاری دوباره در context تکرار نمیشود.
    if current_request and messages:
        last = messages[-1]

        if last.get("role") == "user" and last.get("text") == (
            "" if current_request is None else str(current_request).strip()
        ):
            messages = messages[:-1]

    lines = []

    for message in messages:
        label = (
            "کاربر"
            if message.get("role") == "user"
            else "Agent"
        )

        lines.append(label + ": " + message.get("text", ""))

    return "\n".join(lines)


# Phase 4 — Durable Memory wiring (flag-gated, fail-safe).
#
# agent.py owns NO memory logic: recall/record policy lives in
# agents/memory_adapter.py. These helpers are pure wiring: a feature
# flag, a lazy per-path singleton, and fail-safe call sites. When the
# flag is OFF (default) every helper is a no-op and prompts are
# byte-identical to before. No Safety Spine function is touched, and
# memory/sqlite are never imported at module top (lazy, inside the
# factory) so a memory failure can never break agent import.
LOCAL_AGENT_MEMORY_ENV_FLAG = "LOCAL_AGENT_MEMORY_ENABLED"
LOCAL_AGENT_MEMORY_DB_ENV = "LOCAL_AGENT_MEMORY_DB_PATH"

_DURABLE_MEMORY_ADAPTERS = {}
_DURABLE_MEMORY_LOCK = threading.Lock()


def is_durable_memory_enabled():
    try:
        return (
            os.environ.get(LOCAL_AGENT_MEMORY_ENV_FLAG, "")
            .strip().lower() in ("1", "true", "yes", "on")
        )
    except Exception:
        return False


def _get_durable_memory_adapter():
    try:
        db_path = os.environ.get(LOCAL_AGENT_MEMORY_DB_ENV, "").strip() or None
    except Exception:
        db_path = None
    key = db_path or "<default>"
    adapter = _DURABLE_MEMORY_ADAPTERS.get(key)
    if adapter is not None:
        return adapter
    with _DURABLE_MEMORY_LOCK:
        adapter = _DURABLE_MEMORY_ADAPTERS.get(key)
        if adapter is not None:
            return adapter
        try:
            from agents.memory_adapter import build_memory_adapter
        except Exception:
            return None
        try:
            adapter = build_memory_adapter(db_path=db_path)
        except Exception:
            return None
        _DURABLE_MEMORY_ADAPTERS[key] = adapter
        return adapter


def _get_durable_memory_text(query, project_name=None):
    try:
        if not is_durable_memory_enabled():
            return ""
        adapter = _get_durable_memory_adapter()
        if adapter is None:
            return ""
        return adapter.recall_for_prompt(query, project=project_name or "")
    except Exception:
        return ""


def _record_durable_interaction(user_text, agent_text, project_name=None):
    try:
        if not is_durable_memory_enabled():
            return
        adapter = _get_durable_memory_adapter()
        if adapter is None:
            return
        adapter.record_interaction(
            user_text, agent_text, project=project_name or ""
        )
    except Exception:
        return


def normalize_control_reply_text(text):
    # نرمال‌سازی محدود و مشترک پاسخ Approval (تأیید/لغو).
    # فقط شکل نوشتاری را یکسان می‌کند: الف/همزه عربی
    # (أإآ→ا)، یای عربی با همزه (ئ→ی) و ة→ه.
    # هیچ کلمه‌ای اضافه یا حذف نمی‌شود و تطبیق همچنان
    # دقیقاً روی متن کامل انجام می‌گیرد، پس این تابع فقط
    # «شکل» ورودی کاربر را تفسیر می‌کند و Gate را تغییر
    # نمی‌دهد. (کنترل‌های Emergency Stop اصلاً از این مسیر
    # نمی‌آیند.)
    text = normalize_request_text(text)

    for source, target in (
        ("\u0623", "\u0627"),
        ("\u0625", "\u0627"),
        ("\u0622", "\u0627"),
        ("\u0626", "\u06cc"),
        ("\u0629", "\u0647"),
    ):
        text = text.replace(source, target)

    return text


def detect_control_reply(user_request):
    # تشخیص پاسخ کوتاه تأیید/لغو برای Gate تأیید.
    text = normalize_control_reply_text(user_request)
    text = text.strip().strip("!.؟?,;:")

    if not text or len(text) > 30:
        return None

    approve_variants = set()

    for keyword in APPROVAL_KEYWORDS:
        approve_variants.add(keyword)
        approve_variants.add(keyword + " کن")
        approve_variants.add(keyword + " کنید")
        approve_variants.add(keyword + " بده")
        approve_variants.add("please " + keyword)

    if text in approve_variants:
        return "approve"

    cancel_variants = set()

    for keyword in CANCEL_KEYWORDS:
        cancel_variants.add(keyword)
        cancel_variants.add(keyword + " کن")
        cancel_variants.add(keyword + " کنید")

    if text in cancel_variants:
        return "cancel"

    # «undo» عمداً آخرین بررسی است: اگر روزی کلیدواژه‌ای مشترک
    # با تأیید/لغو اضافه شود، تأیید و لغو همچنان اولویت دارند.
    undo_variants = set()

    for keyword in UNDO_KEYWORDS:
        undo_variants.add(keyword)
        undo_variants.add(keyword + " کن")
        undo_variants.add(keyword + " کنید")
        undo_variants.add("please " + keyword)

    if text in undo_variants:
        return "undo"

    return None


def _write_pending_action_file(action):
    temp_path = PENDING_ACTION_FILE + ".tmp"

    try:
        with open(
            temp_path,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                action,
                f,
                ensure_ascii=False
            )
            f.flush()
            os.fsync(f.fileno())

        os.replace(
            temp_path,
            PENDING_ACTION_FILE
        )

    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass

        raise


def pending_action_plan_fingerprint(payload):
    """
    اثر انگشتِ محتواییِ یک Plan.

    فقط چهار چیز هویتِ «این Plan» را می‌سازند: ابزار،
    آرگومان‌ها، نام پروژه و ریشه‌ی پروژه. فیلدهای-volatile
    مثل created_at یا read_paths عمداً وارد نمی‌شوند.

    ورودی ناقص یا غیرقابل‌سریال‌سازی → Fail Closed (رشته‌ی
    خالی)، تا هرگز با یک اثر انگشتِ ساختگی عبور نکند.
    """
    import hashlib

    if not isinstance(payload, dict):
        return ""

    root = payload.get("project_root")

    material = {
        "tool": str(payload.get("tool") or ""),
        "args": payload.get("args"),
        "project": str(payload.get("project") or ""),
        "project_root": (
            os.path.abspath(str(root)) if root else ""
        ),
    }

    try:
        canonical = json.dumps(
            material,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )

        return hashlib.sha1(
            canonical.encode("utf-8")
        ).hexdigest()

    except Exception:
        return ""


def pending_action_identity_status(
    pending,
    expected_project=None,
    expected_args=None
):
    """
    Phase I — Pending Action Integrity: Fail Closed.

    خروجی: (ok, reason)

    سه چیز جدا بررسی می‌شود:

    1) هویت وجود دارد. Pending ساخته‌شده پیش از این لایه
       (یا دستی‌سازی‌شده) plan_id ندارد و رد می‌شود؛ همان
       الگوی «ساخته‌شده توسط نسخه قبلی» در Re-validation.

    2) دست‌نخورده است. اثر انگشتِ ثبت‌شده با اثر انگشتِ
       دوباره‌محاسبه‌شده از محتوای فعلی برابر باشد. پس هر
       تغییرِ args/tool/project/project_root روی Pending
       ذخیره‌شده یا روی فایل، لو می‌رود.

    3) همان Context تأییدشده اجرا می‌شود. اگر caller بداند
       کاربر روی کدام پروژه و کدام آرگومان‌ها Approval داده،
       آن‌ها باید با Pending داخل Slot برابر باشند.

    هیچ‌کدام از این‌ها به Re-validation وابسته نیست، چون
    Re-validation فقط سازگاریِ داخلیِ Pending را می‌سنجد و
    نمی‌تواند بداند کاربر کدام Plan را دیده بود.
    """
    if not isinstance(pending, dict):
        return False, "Pending Action نامعتبر است."

    plan_id = pending.get("plan_id")

    if not isinstance(plan_id, str) or not plan_id.strip():
        return (
            False,
            "این Plan هویت ندارد (ساخته‌شده پیش از افزودن "
            "شناسه یا دستی‌سازی‌شده)؛ اجرا نشد. برای اجرا، "
            "درخواست را دوباره مطرح کنید.",
        )

    recorded = pending.get("plan_fingerprint")

    if not isinstance(recorded, str) or not recorded.strip():
        return False, "این Plan اثر انگشت معتبر ندارد؛ اجرا نشد."

    recomputed = pending_action_plan_fingerprint(pending)

    if not recomputed or recomputed != recorded:
        return (
            False,
            "محتوای Plan با اثر انگشت ثبت‌شده هم‌خوانی ندارد؛ "
            "Plan تغییر کرده یا قدیمی است و با Approval قبلی "
            "قابل اجرا نیست.",
        )

    if expected_project is not None:
        actual_project = str(pending.get("project") or "")

        if actual_project != str(expected_project):
            return (
                False,
                "Plan تأییدشده متعلق به پروژهٔ «"
                + str(expected_project) + "» است، ولی Pending "
                "موجود برای پروژهٔ «" + actual_project + "» است. "
                "هیچ تغییری انجام نشد.",
            )

    if expected_args is not None:
        expected_fingerprint = pending_action_plan_fingerprint({
            "tool": pending.get("tool"),
            "args": expected_args,
            "project": pending.get("project"),
            "project_root": pending.get("project_root"),
        })
        actual_fingerprint = pending_action_plan_fingerprint({
            "tool": pending.get("tool"),
            "args": pending.get("args"),
            "project": pending.get("project"),
            "project_root": pending.get("project_root"),
        })

        if not expected_fingerprint or (
            expected_fingerprint != actual_fingerprint
        ):
            return (
                False,
                "Plan موجود در Slot با Plan تأییدشده یکی نیست؛ "
                "هیچ تغییری انجام نشد.",
            )

    return True, ""


def remember_pending_action(action):
    global PENDING_ACTION

    pending_action = dict(action)
    # Phase E: ثبت زمان ایجاد برای TTL (فقط additive).
    try:
        pending_action["created_at"] = time.time()
    except Exception:
        pass

    # Phase I — Pending Action Integrity: هر Plan هنگام ایجاد
    # هویت می‌گیرد تا Approval بعدی فقط روی همین Plan مصرف
    # شود. این دو فیلد بعداً در pending_action_identity_status
    # راستی‌آزمایی می‌شوند.
    try:
        import uuid

        pending_action["plan_id"] = uuid.uuid4().hex
    except Exception:
        # Fail Closed: نبودِ شناسه یعنی Plan بعداً رد می‌شود،
        # که بهتر از اجرای بدون هویت است.
        pending_action["plan_id"] = ""

    pending_action["plan_fingerprint"] = (
        pending_action_plan_fingerprint(pending_action)
    )

    _write_pending_action_file(pending_action)
    PENDING_ACTION = pending_action


def get_pending_action():
    global PENDING_ACTION

    if not os.path.isfile(PENDING_ACTION_FILE):
        PENDING_ACTION = None
        return None

    try:
        with open(
            PENDING_ACTION_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            pending_action = json.load(f)

        if not isinstance(pending_action, dict):
            raise ValueError(
                "Invalid pending action state."
            )

        PENDING_ACTION = pending_action
        return dict(PENDING_ACTION)

    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        PENDING_ACTION = None
        clear_pending_action()
        return None


def clear_pending_action():
    global PENDING_ACTION

    PENDING_ACTION = None

    try:
        os.remove(PENDING_ACTION_FILE)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------
# Phase E: TTL / Expiration برای Pending Action
# ---------------------------------------------------------
def is_pending_action_expired(pending, now=None):
    # Fail Closed: created_at نامعتبر/غایب یعنی منقضی.
    try:
        if not isinstance(pending, dict):
            return True

        created_at = pending.get("created_at")

        if not isinstance(created_at, (int, float)):
            return True

        current = time.time() if now is None else float(now)

        return (current - float(created_at)) > float(
            PENDING_ACTION_TTL_SECONDS
        )

    except Exception:
        return True


def remember_pending_expiration(pending, context=None):
    # ثبت انقضای Pending در Journal؛ هیچ mutation انجام نمی‌شود.
    try:
        if isinstance(context, dict):
            expired = context

        elif isinstance(pending, dict):
            expired = pending.get("context") if isinstance(
                pending.get("context"), dict
            ) else None

            if not isinstance(expired, dict):
                expired = new_action_context(
                    PENDING_TOOL_ACTION_TYPES.get(
                        pending.get("tool"),
                        str(pending.get("tool")),
                    ),
                    tool=pending.get("tool"),
                    args=pending.get("args"),
                    project=pending.get("project"),
                    project_root=pending.get("project_root"),
                    target=None,
                    allow_write=True,
                    approval_state=ACTION_APPROVAL_EXPIRED,
                )

        else:
            return None

        update_action_context(
            expired,
            approval_state=ACTION_APPROVAL_EXPIRED,
            execution_state=ACTION_STATE_FAILED,
            verify_state=ACTION_VERIFY_SKIPPED,
            result_summary=summarize_action_result(
                "pending expired"
            ),
            ended_at=time.time(),
        )

        return remember_action(expired)

    except Exception:
        return None


def build_pending_expiration_report(pending):
    project_name = None
    project_root = None
    tool_name = ""

    if isinstance(pending, dict):
        project_name = pending.get("project")
        project_root = pending.get("project_root")
        tool_name = str(pending.get("tool"))

    return build_report(
        "Plan (منقضی شد / expired)",
        project_name,
        project_root,
        [
            "ابزار: " + tool_name,
            "Plan منقضی شد (expired)؛ عملیات اجرا نشد.",
            "هیچ تغییری روی فایل‌ها اعمال نشد و Backup جدیدی "
            "ساخته نشد.",
            "برای اجرا، درخواست را دوباره مطرح کنید.",
        ],
    )


# ---------------------------------------------------------
# Phase B: Re-validation قبل از Execute
# ---------------------------------------------------------
# قبل از اجرای Pending Action، مشخصات ثبت‌شده در Plan دوباره
# با وضعیت فعلی سیستم مقایسه می‌شود.
#
# اصل: Fail Closed
# در صورت هر شک، نقص اطلاعات یا تغییر، اجرا انجام نمی‌شود و
# Pending Action پاک نمی‌شود.
#
# این بخش هیچ چیزی در Approval / Cancel / Pending فعلی تغییر
# نمی‌دهد؛ فقط یک مرحله اضافه بین Approval و Execute است.
#
# کلیدهای کمکی این مرحله در Action Context (خارج از
# ACTION_CONTEXT_FIELDS):
# - before_state_path : مسیر نسبی‌ای که before_state به آن اشاره دارد
# - target_state      : وضعیت مقصد (فقط برای move_entry)

# نگاشت نام ابزار Registry به action_type در Action Context.
PENDING_TOOL_ACTION_TYPES = {
    "write_file": "write",
    "write_files": "multi_write",
    "edit_file": "edit",
    "create_directory": "create_directory",
    "move_entry": "move",
    "delete_entry": "delete",
    "run_command": "run_command",
}


def pending_action_paths(tool_name, args):
    # مسیرهای نسبی که Re-validation باید بررسی کند.
    # before = مسیری که before_state به آن اشاره دارد
    # target = مقصد (فقط برای move)
    # batch = همه‌ی مسیرهای یک Batch (برای write_files)
    paths = {"before": None, "target": None, "batch": None}

    if tool_name == "write_files":
        # Step 1: هر عضو Batch جداگانه در Re-validation بررسی
        # می‌شود، تا هیچ مسیری خارج از کنترل نماند.
        # اگر ساختار Batch نامعتبر باشد، خطا به بالا منتقل می‌شود
        # تا کل Batch به‌صورت Fail Closed رد شود.
        files = normalize_batch_files(args)

        paths["batch"] = [
            entry.get("path") for entry in files
        ]

        return paths

    if tool_name == "move_entry":
        paths["before"] = tool_arg(
            args, "source", "from", "path", "old"
        )
        paths["target"] = tool_arg(
            args, "target", "to", "destination", "new"
        )

        return paths

    if tool_name == "run_command":
        paths["before"] = tool_arg(args, "cwd", "path")

        return paths

    paths["before"] = tool_arg(
        args, "path", "relative_path", "file", "dir"
    )

    return paths


def build_pending_revalidation_context(
    tool_name,
    args,
    project_root,
    project_name,
    allow_write=True
):
    # ساخت Action Context زمان Plan برای Re-validation.
    # این تابع هرگز استثنا پرتاب نمی‌کند؛ در بدترین حالت
    # Context ناقص ساخته می‌شود و Re-validation بعداً به شکل
    # امن (Fail Closed) رد می‌کند.
    try:
        context = new_action_context(
            PENDING_TOOL_ACTION_TYPES.get(
                tool_name, tool_name
            ),
            tool=tool_name,
            args=args,
            project=project_name,
            project_root=project_root,
            target=None,
            allow_write=allow_write,
            operation_class=ACTION_CLASS_USER,
            approval_state=ACTION_APPROVAL_PENDING,
        )

        context["before_state_path"] = None
        context["before_state"] = None
        context["target_state"] = None

        paths = pending_action_paths(tool_name, args)
        before_rel = paths.get("before")
        target_rel = paths.get("target")

        context["target"] = target_rel or before_rel

        # Step 1: وضعیت «قبل» تک‌تک اعضای Batch ثبت می‌شود تا
        # هنگام Re-validation مشخص باشد کدام فایل‌ها تغییر کرده‌اند.
        context["batch_paths"] = None
        context["batch_states"] = None

        batch_paths = paths.get("batch")

        if batch_paths is not None:
            context["batch_paths"] = list(batch_paths)
            context["batch_states"] = {}

            for batch_rel in batch_paths:
                try:
                    context["batch_states"][batch_rel] = (
                        build_action_before_state(
                            resolve_project_path(
                                project_root, batch_rel
                            )
                        )
                    )
                except Exception:
                    context["batch_states"][batch_rel] = None

        if before_rel:
            context["before_state_path"] = before_rel

            try:
                context["before_state"] = (
                    build_action_before_state(
                        resolve_project_path(
                            project_root, before_rel
                        )
                    )
                )

            except Exception:
                context["before_state"] = None

        if target_rel:
            try:
                context["target_state"] = (
                    build_action_before_state(
                        resolve_project_path(
                            project_root, target_rel
                        )
                    )
                )

            except Exception:
                context["target_state"] = None

        return context

    except Exception:
        return None


def compare_pending_state(recorded, current, label):
    # مقایسه وضعیت ثبت‌شده در Plan با وضعیت فعلی دیسک.
    # خروجی: متن مغایرت، یا None اگر مغایرتی نباشد.
    if not isinstance(recorded, dict):
        return "اطلاعات وضعیت قبلی " + label + " در Plan موجود نیست."

    if not isinstance(current, dict):
        return "وضعیت فعلی " + label + " قابل خواندن نیست."

    recorded_exists = bool(recorded.get("existed_before"))
    current_exists = bool(current.get("existed_before"))

    if recorded_exists != current_exists:
        if current_exists:
            return label + " بعد از Plan ایجاد شده است."

        return label + " بعد از Plan حذف شده است."

    if not recorded_exists:
        return None

    recorded_type = recorded.get("type")
    current_type = current.get("type")

    if recorded_type and current_type and recorded_type != current_type:
        return label + " نوعش بین Plan و Execute تغییر کرده است."

    recorded_sha1 = recorded.get("sha1")
    current_sha1 = current.get("sha1")

    if recorded_sha1 and not current_sha1:
        return label + " بعد از Plan قابل خواندن نیست."

    if recorded_sha1 and current_sha1 != recorded_sha1:
        return label + " محتوایش بین Plan و Execute تغییر کرده است."

    recorded_size = recorded.get("size")
    current_size = current.get("size")

    if (
        recorded_size is not None
        and current_size is not None
        and recorded_size != current_size
    ):
        return label + " اندازه‌اش بین Plan و Execute تغییر کرده است."

    return None


def revalidate_pending_action(pending):
    # مقایسه مشخصات Plan با وضعیت فعلی سیستم.
    # خروجی: (ok, reason, context)
    # Fail Closed: هر نقص اطلاعاتی → اجرا انجام نمی‌شود.
    if not isinstance(pending, dict):
        return False, "Pending Action نامعتبر است.", None

    context = pending.get("context")

    if not isinstance(context, dict):
        # Pending ساخته‌شده توسط نسخه قبلی سیستم.
        # اطلاعات ناقص حدس زده نمی‌شود و اجرا انجام نمی‌شود.
        return (
            False,
            "این Plan اطلاعات Re-validation ندارد "
            "(ساخته‌شده توسط نسخه قبلی)؛ اجرا انجام نشد. "
            "برای اجرا، درخواست را دوباره مطرح کنید.",
            None,
        )

    def fail(reason, ctx=None):
        return (
            False,
            reason,
            context if ctx is None else ctx,
        )

    try:
        tool_name = pending.get("tool")
        args = pending.get("args")

        if not tool_name or tool_name not in TOOL_REGISTRY:
            return fail("ابزار Plan معتبر نیست: " + str(tool_name))

        if context.get("tool") != tool_name:
            return fail("ابزار Plan با اطلاعات ثبت‌شده هم‌خوانی ندارد.")

        action_type = PENDING_TOOL_ACTION_TYPES.get(
            tool_name, tool_name
        )

        if context.get("action_type") != action_type:
            return fail("نوع Action با اطلاعات ثبت‌شده هم‌خوانی ندارد.")

        project_name = pending.get("project")

        if context.get("project") != project_name:
            return fail("پروژه Plan تغییر کرده است.")

        recorded_root = context.get("project_root")
        pending_root = pending.get("project_root")

        if not recorded_root or not pending_root:
            return fail("ریشه پروژه در Plan موجود نیست.")

        recorded_root = os.path.abspath(str(recorded_root))

        if recorded_root != os.path.abspath(str(pending_root)):
            return fail("ریشه پروژه با اطلاعات ثبت‌شده هم‌خوانی ندارد.")

        if not os.path.isdir(recorded_root):
            return fail("پوشه پروژه دیگر موجود نیست.")

        live_root = get_project_path(project_name)

        if live_root and os.path.abspath(live_root) != recorded_root:
            return fail("مسیر پروژه در تنظیمات تغییر کرده است.")

        if not isinstance(args, dict) or not args:
            return fail("آرگومان‌های Action در دسترس نیستند.")

        recorded_hash = context.get("args_hash")
        current_hash = hash_action_args(args)

        if not recorded_hash or not current_hash:
            return fail("هش آرگومان‌ها قابل بررسی نیست.")

        if recorded_hash != current_hash:
            return fail(
                "آرگومان‌های Action نسبت به Plan تغییر کرده‌اند."
            )

        paths = pending_action_paths(tool_name, args)

        # Step 1 — Batch Verification.
        #
        # A batch is verified member by member using the SAME rules as a
        # single action. Fail closed: if ANY member is invalid, outside the
        # project root, or changed since the plan was built, the WHOLE batch
        # is rejected and nothing is executed. On success we return to the
        # existing batch execution path with no change to its behaviour.
        #
        # This adds validation only. It never alters approval, never
        # weakens Emergency Stop, and never touches the single-action path
        # below, which runs unchanged.
        if tool_name == "write_files":
            # پیام خطای normalize_batch_files به Fail-Closed تبدیل
            # می‌شود تا یک Batch ناقص هرگز مثل Single Action عبور نکند.
            try:
                batch_paths = pending_action_paths(
                    tool_name, args
                ).get("batch")
            except Exception as e:
                return fail(
                    "ساختار Batch نامعتبر است؛ اجرا انجام نشد: "
                    + str(e)
                )

            if not batch_paths:
                return fail(
                    "Batch فایل معتبری ندارد؛ اجرا انجام نشد."
                )

            recorded_batch_paths = context.get("batch_paths")

            if (
                not isinstance(recorded_batch_paths, list)
                or list(recorded_batch_paths) != list(batch_paths)
            ):
                return fail(
                    "اعضای Batch نسبت به Plan تغییر کرده‌اند."
                )

            recorded_states = context.get("batch_states")

            if not isinstance(recorded_states, dict):
                return fail(
                    "اطلاعات وضعیت Batch در دسترس نیست."
                )

            for batch_rel in batch_paths:
                try:
                    batch_abs = resolve_project_path(
                        recorded_root, batch_rel
                    )

                except Exception as e:
                    return fail(
                        "مسیر یکی از فایل‌های Batch دیگر معتبر نیست: "
                        + str(batch_rel) + " :: " + str(e)
                    )

                if not is_inside_project(recorded_root, batch_abs):
                    return fail(
                        "مسیر یکی از فایل‌های Batch خارج از پروژه است: "
                        + str(batch_rel)
                    )

                if batch_rel not in recorded_states:
                    return fail(
                        "وضعیت فایل Batch ثبت نشده است: "
                        + str(batch_rel)
                    )

                mismatch = compare_pending_state(
                    recorded_states.get(batch_rel),
                    build_action_before_state(batch_abs),
                    "فایل Batch",
                )

                if mismatch:
                    return fail(mismatch)

            return True, "", context

        before_rel = (
            context.get("before_state_path")
            or paths.get("before")
        )

        if before_rel:
            if paths.get("before") != before_rel:
                return fail("مسیر هدف نسبت به Plan تغییر کرده است.")

            try:
                before_abs = resolve_project_path(
                    recorded_root, before_rel
                )

            except Exception as e:
                return fail("مسیر هدف دیگر معتبر نیست: " + str(e))

            if not is_inside_project(recorded_root, before_abs):
                return fail("مسیر هدف خارج از پروژه است.")

            mismatch = compare_pending_state(
                context.get("before_state"),
                build_action_before_state(before_abs),
                "مسیر هدف",
            )

            if mismatch:
                return fail(mismatch)

        target_rel = paths.get("target")

        if target_rel:
            recorded_target = context.get("target")

            if recorded_target and recorded_target != target_rel:
                return fail("مقصد نسبت به Plan تغییر کرده است.")

            try:
                target_abs = resolve_project_path(
                    recorded_root, target_rel
                )

            except Exception as e:
                return fail("مقصد دیگر معتبر نیست: " + str(e))

            if not is_inside_project(recorded_root, target_abs):
                return fail("مقصد خارج از پروژه است.")

            mismatch = compare_pending_state(
                context.get("target_state"),
                build_action_before_state(target_abs),
                "مقصد",
            )

            if mismatch:
                return fail(mismatch)

        return True, "", context

    except Exception as e:
        return fail(
            "Re-validation با خطای غیرمنتظره متوقف شد: " + str(e)
        )


def remember_revalidation_failure(pending, context, reason):
    # ثبت شکست Re-validation در Action Journal.
    # هیچ فایلی تغییر نمی‌کند و Pending Action پاک نمی‌شود.
    try:
        if not isinstance(context, dict):
            if not isinstance(pending, dict):
                return None

            context = new_action_context(
                PENDING_TOOL_ACTION_TYPES.get(
                    pending.get("tool"),
                    str(pending.get("tool"))
                ),
                tool=pending.get("tool"),
                args=pending.get("args"),
                project=pending.get("project"),
                project_root=pending.get("project_root"),
                target=None,
                allow_write=True,
                approval_state=ACTION_APPROVAL_APPROVED,
            )

        update_action_context(
            context,
            approval_state=ACTION_APPROVAL_APPROVED,
            execution_state=ACTION_STATE_FAILED,
            verify_state=ACTION_VERIFY_SKIPPED,
            result_summary=summarize_action_result(
                "revalidation failed: " + str(reason)
            ),
            ended_at=time.time(),
        )

        return remember_action(context)

    except Exception:
        return None


def build_pending_revalidation_report(pending, reason):
    # گزارش قابل فهم شکست Re-validation برای کاربر.
    project_name = None
    project_root = None
    tool_name = ""

    if isinstance(pending, dict):
        project_name = pending.get("project")
        project_root = pending.get("project_root")
        tool_name = str(pending.get("tool"))

    return build_report(
        "Re-validation (اجرا نشد)",
        project_name,
        project_root,
        [
            "ابزار: " + tool_name,
            "Re-validation ناموفق بود؛ عملیات اجرا نشد.",
            "دلیل: " + str(reason),
            "",
            "هیچ تغییری روی فایل‌ها اعمال نشد و Backup جدیدی "
            "ساخته نشد.",
            "Plan همچنان در انتظار تأیید باقی ماند.",
            "برای لغو این Plan پاسخ دهید: لغو",
        ]
    )


# ---------------------------------------------------------
# Phase C: Emergency Stop
# ---------------------------------------------------------
# منبع مرکزی «توقف اضطراری» برای کل پروسه.
#
# معنی Emergency Stop در این Phase:
#   «Step/Action بعدی اجرا نشود.»
# نه:
#   «هر process در حال اجرا فوراً kill شود.»
#
# قواعد:
# - فقط جلوی ادامه اجرا گرفته می‌شود (قبل از Step و قبل از
#   هر Mutation).
# - هیچ subprocess در حال اجرا kill نمی‌شود.
# - Pending Action به‌خاطر Stop پاک نمی‌شود (تصمیم بعدی با
#   کاربر است).
# - هیچ فایل/Backup تغییر نمی‌کند.
# - هیچ‌وقت خودکار Reset نمی‌شود؛ فقط clear_emergency_stop().
#
# از همان قرارداد stop_event فعلی (is_set) استفاده می‌شود تا
# یک منبع Stop مرکزی داشته باشیم و stop_event فعلی هم بدون
# تغییر کار کند.

EMERGENCY_STOP_EVENT = threading.Event()

EMERGENCY_STOP_MESSAGE = (
    "Emergency Stop فعال است؛ عملیات اجرا نشد."
)


def request_emergency_stop():
    # فعال‌کردن توقف اضطراری (فقط Set؛ هیچ عملیات دیگری).
    EMERGENCY_STOP_EVENT.set()

    return True


def clear_emergency_stop():
    # Reset فقط با درخواست صریح (هرگز خودکار).
    EMERGENCY_STOP_EVENT.clear()

    return True


def is_emergency_stop_requested():
    return EMERGENCY_STOP_EVENT.is_set()


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


def build_emergency_stop_report(
    stage,
    project_name,
    project_root,
    tool_name=None,
    keep_pending=False,
    notes=None
):
    # گزارش کنترل‌شده توقف (بدون هیچ تغییر روی دیسک).
    lines = []

    if tool_name:
        lines.append("ابزار: " + str(tool_name))

    lines.append(EMERGENCY_STOP_MESSAGE)
    lines.append(
        "هیچ فایلی تغییر نکرد، هیچ چیزی حذف/جابه‌جا نشد و "
        "Backup جدیدی ساخته نشد."
    )

    if keep_pending:
        lines.append("Plan همچنان در انتظار تأیید باقی ماند.")

    if notes:
        lines.extend(notes)

    lines.append(
        "برای ادامه، ابتدا Emergency Stop را ریست کنید."
    )

    return build_report(
        stage,
        project_name,
        project_root,
        lines
    )


def handle_pending_action_request(
    user_request,
    allow_write=False,
    stop_event=None,
    expected_project=None
):
    # Gate تأیید:
    # - عملیات Read/Explore نیازی به تأیید ندارد.
    # - عملیات تغییردهنده اول Plan میدهد و منتظر
    #   تأیید کاربر میماند.
    # خروجی None یعنی «این درخواست مربوط به Gate نیست».
    #
    # Phase I: expected_project پروژه‌ای است که کاربر در UI
    # انتخاب کرده و Plan را برای آن دیده. اگر Slot چیز دیگری
    # داشته باشد، Approval مصرف نمی‌شود.
    control = detect_control_reply(user_request)

    if control == "cancel":
        pending = get_pending_action()

        if pending is None:
            return None

        clear_pending_action()

        return build_report(
            "Plan (لغو شد)",
            pending["project"],
            pending["project_root"],
            [
                "ابزار: " + pending["tool"],
                "Plan در انتظار تأیید لغو شد؛ "
                "هیچ تغییری انجام نشد.",
            ]
        )

    if control == "approve":
        pending = get_pending_action()

        if pending is None:
            return None

        if not is_write_allowed(allow_write):
            return build_report(
                "Plan (تأیید)",
                pending["project"],
                pending["project_root"],
                [
                    "ابزار: " + pending["tool"],
                    "Plan تأیید شد اما حالت «اعمال تغییر» "
                    "فعال نیست؛ هیچ تغییری انجام نشد.",
                ]
            )

        # Phase C: با Stop فعال، Execute شروع نمیشود.
        # Pending و Approval semantics دست‌نخورده می‌مانند.
        if is_stop_requested(stop_event):
            stop_pending = (
                pending if isinstance(pending, dict) else {}
            )

            remember_stop_blocked_action(
                tool=stop_pending.get("tool"),
                context=stop_pending.get("context"),
                args=stop_pending.get("args"),
                project=stop_pending.get("project"),
                project_root=stop_pending.get("project_root"),
                approval_state=ACTION_APPROVAL_APPROVED,
            )

            return build_emergency_stop_report(
                "Emergency Stop (اجرا نشد)",
                stop_pending.get("project"),
                stop_pending.get("project_root"),
                tool_name=stop_pending.get("tool"),
                keep_pending=True,
                notes=[
                    "Plan تأیید شد اما Emergency Stop فعال است؛ "
                    "هیچ عملیاتی اجرا نشد.",
                ],
            )

        # Phase E: TTL قبل از Execute (همان ترتیب امن Execute).
        if is_pending_action_expired(pending):
            expired_context = (
                pending.get("context")
                if isinstance(pending.get("context"), dict)
                else None
            )

            remember_pending_expiration(pending, expired_context)
            clear_pending_action()

            return build_pending_expiration_report(pending)

        return execute_pending_action(
            stop_event=stop_event,
            expected_project=expected_project,
        )

    # درخواست جدید: Plan تأییدنشده منقضی میشود.
    if get_pending_action() is not None:
        clear_pending_action()

    return None

# ---------------------------------------------------------
# Phase E: Undo برای mutationهای قابل برگشت
# ---------------------------------------------------------
# Undo خودش یک mutation است و Fail Closed عمل می‌کند.
UNDOABLE_ACTION_TYPES = (
    "write",
    "edit",
    "create",
    "create_directory",
    "move",
    "delete",
    "multi_write",
)
UNDOABLE_TOOLS = (
    "write_file",
    "edit_file",
    "create_directory",
    "move_entry",
    "delete_entry",
    "write_files",
)

# ---------------------------------------------------------
# Stage 4 — Undo برای Batch چندفایلی
# ---------------------------------------------------------
# این بخش فقط «اطلاعات لازم برای Undo» را از همان نتیجهٔ واقعی
# Write می‌سازد و همان contract تک‌فایلی را به‌کار می‌برد:
# هر عضوِ ازپیش‌موجود با همان Backup استانداردِ create_backup
# برگردانده می‌شود و هر عضوِ ساخته‌شده حذف می‌شود (همان رفتار
# Undo یک Create).
#
# هیچ Schema جدیدی در Journal اختراع نشده: فقط همان dict
# before_state که از قبل وجود دارد، یک کلید "files" می‌گیرد.


def build_multi_write_undo_state(project_root, files_result):
    # وضعیت قبلیِ هر عضو Batch، فقط از روی نتیجهٔ واقعی Write.
    entries = []

    for item in files_result or []:
        if not isinstance(item, dict):
            continue

        rel = item.get("relative_path")
        full = item.get("full_path")

        if not rel or not full:
            continue

        entries.append({
            "path": str(rel),
            "full_path": os.path.abspath(str(full)),
            "backup_path": item.get("undo_backup_path"),
            "existed_before": bool(item.get("existed_before")),
        })

    return {
        "type": "multi_write",
        "count": len(entries),
        "files": entries,
    }


def multi_write_target_label(before_state):
    # target باید یک رشتهٔ غیرخالی باشد تا قرارداد موجودِ
    # Action Context حفظ شود؛ مانند primary_target برای Batch.
    files = (before_state or {}).get("files") or []
    paths = sorted(str(item.get("path")) for item in files)

    return ("multi_write:" + "|".join(paths))[:500]


def record_multi_write_undo(
    files_result,
    project_root,
    project=None,
    args=None,
    approval_state=ACTION_APPROVAL_APPROVED,
    verified=True,
    summary="multi-file batch executed",
    agent_id=None,
    agent_type=None
):
    # ثبت Undo برای Batch موفق. فقط وقتی فراخوانی می‌شود که
    # Write واقعاً موفق بوده باشد؛ Batch ناموفق هرگز اینجا
    # نمی‌رسد پس هیچ Action ناقصی Undoable ثبت نمی‌شود.
    before_state = build_multi_write_undo_state(
        project_root, files_result
    )

    if not before_state.get("files"):
        return None

    return record_action(
        "multi_write",
        operation_class=ACTION_CLASS_USER,
        tool="write_files",
        args=args,
        project=project,
        project_root=project_root,
        target=multi_write_target_label(before_state),
        approval_state=approval_state,
        validation_state=bool(verified),
        before_state=before_state,
        execution_state=ACTION_STATE_DONE,
        verify_state=verify_state_from_bool(bool(verified)),
        result_summary=summarize_action_result(summary),
        agent_id=agent_id,
        agent_type=agent_type,
    )


def _resolve_undo_backup_absolute(project_root, backup_ref, full_path=None):
    try:
        if not backup_ref or not project_root:
            return None
        text = str(backup_ref).strip()
        if not text:
            return None
        root = os.path.abspath(project_root)
        cands = []
        if os.path.isabs(text):
            cands.append(os.path.abspath(text))
        else:
            cands.append(os.path.abspath(os.path.join(root, text)))
        if full_path:
            try:
                cands.append(os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(full_path)), os.path.basename(text))))
            except Exception:
                pass
        for cand in cands:
            try:
                if not os.path.isfile(cand):
                    continue
                if os.path.commonpath([cand, root]) != root:
                    continue
                return cand
            except Exception:
                continue
        if full_path:
            try:
                found = find_backup_path(root, os.path.abspath(full_path))
                if found and os.path.isfile(found):
                    if os.path.commonpath([os.path.abspath(found), root]) == root:
                        return os.path.abspath(found)
            except Exception:
                pass
        return None
    except Exception:
        return None

def validate_undo_candidate(action, project=None, project_root=None):
    try:
        if not isinstance(action, dict):
            return False, "Action نامعتبر است.", None
        aid = action.get("action_id")
        atype = action.get("action_type")
        tool = action.get("tool")
        target = action.get("target")
        eproj = action.get("project")
        eroot = action.get("project_root")
        bref = action.get("backup_path")
        status = action.get("execution_state")
        if not aid:
            return False, "شناسه Action موجود نیست.", None
        # Phase F1 — Undo Idempotency: یک Action فقط یک بار Undo می‌شود.
        # فقط Undo موفق قبلی (done) بلاک می‌کند؛ تلاش ناموفق/مسدود قبلی
        # بلاک نمی‌کند تا بعد از رفع مشکل هنوز قابل Undo باشد.
        try:
            if is_action_undone(aid):
                return False, "این Action قبلاً Undo شده است.", None
        except Exception:
            pass
        if atype not in UNDOABLE_ACTION_TYPES and tool not in UNDOABLE_TOOLS:
            return False, "این Action قابل Undo نیست.", None
        if status is not None and status != ACTION_STATE_DONE:
            return False, "وضعیت این Action برای Undo معتبر نیست.", None
        if not eroot or not os.path.isdir(str(eroot)):
            return False, "مسیر پروژه این Action معتبر نیست.", None
        if project is not None and eproj != project:
            return False, "این Action متعلق به پروژه دیگری است.", None
        if project_root is not None:
            try:
                if os.path.abspath(str(project_root)) != os.path.abspath(str(eroot)):
                    return False, "ریشه پروژه با Action هم‌خوانی ندارد.", None
            except Exception:
                return False, "ریشه پروژه معتبر نیست.", None
        if not target or not str(target).strip():
            return False, "هدف Action برای Undo مشخص نیست.", None
        before = action.get("before_state")
        if not isinstance(before, dict):
            return False, "اطلاعات وضعیت قبلی برای Undo کافی نیست.", None

        root = os.path.abspath(str(eroot))

        # Batch چندفایلی: target یک Batch برچسب مرکب است، نه یک
        # مسیر؛ بنابراین این شاخه پیش از resolve_project_pathِ
        # تک‌عضوی می‌آید و مسیر واقعی هر عضو را خودش جداگانه
        # اعتبارسنجی می‌کند.
        #
        # هر عضو ازپیش‌موجود باید Backup معتبر داشته باشد و هر عضو
        # ساخته‌شده فقط باید داخل پروژه باشد (همان رفتار Undo یک
        # Create).
        if atype == "multi_write" or tool == "write_files":
            batch_files = before.get("files")

            if not isinstance(batch_files, list) or not batch_files:
                return False, "اطلاعات اعضای Batch برای Undo کافی نیست.", None

            resolved = []

            for member in batch_files:
                if not isinstance(member, dict):
                    return False, "عضو Batch نامعتبر است.", None

                mrel = member.get("path")

                if not mrel or not str(mrel).strip():
                    return False, "مسیر یک عضو Batch مشخص نیست.", None

                try:
                    mfull = resolve_project_path(root, str(mrel))
                except Exception as e:
                    return False, "مسیر یک عضو Batch نامعتبر است: " + str(e), None

                if not is_inside_project(root, mfull):
                    return False, "مسیر یک عضو Batch خارج از پروژه است.", None

                existed = bool(member.get("existed_before"))
                mbackup = None

                if existed:
                    mbackup = _resolve_undo_backup_absolute(
                        root, member.get("backup_path"), mfull
                    )

                    if not mbackup:
                        return False, "برای یکی از اعضای Batch نسخه Backup معتبر پیدا نشد.", None

                resolved.append({
                    "path": str(mrel),
                    "full_path": mfull,
                    "backup_abs": mbackup,
                    "existed_before": existed,
                })

            return True, "", {
                "action_id": aid,
                "action_type": atype,
                "tool": tool,
                "project": eproj,
                "project_root": root,
                "target": str(target),
                "full_path": None,
                "backup_ref": bref,
                "backup_abs": None,
                "batch_files": resolved,
            }

        try:
            full = resolve_project_path(root, str(target))
        except Exception as e:
            return False, "مسیر هدف دیگر معتبر نیست: " + str(e), None
        if not is_inside_project(root, full):
            return False, "مسیر هدف خارج از پروژه است.", None
        res = {"action_id": aid, "action_type": atype, "tool": tool, "project": eproj, "project_root": root, "target": str(target), "full_path": full, "backup_ref": bref, "backup_abs": None}
        is_move = atype == "move" or tool == "move_entry"
        is_create = atype in ("create", "create_directory") or tool == "create_directory"
        needs_bk = atype in ("write", "edit", "delete") or tool in ("write_file", "edit_file", "delete_entry")
        if is_move:
            src = before.get("moved_from") or before.get("restore_hint")
            if not src:
                ag = action.get("args")
                if isinstance(ag, dict):
                    src = ag.get("source")
            if not src or not str(src).strip():
                return False, "مبدأ بازگشت برای Move مشخص نیست.", None
            try:
                sabs = resolve_project_path(root, str(src))
            except Exception as e:
                return False, "مبدأ بازگشت معتبر نیست: " + str(e), None
            if not is_inside_project(root, sabs):
                return False, "مبدأ بازگشت خارج از پروژه است.", None
            res["move_source"] = str(src)
            res["move_source_abs"] = sabs
            res["overwrite_backup_abs"] = _resolve_undo_backup_absolute(root, bref, full)
            return True, "", res
        if is_create:
            if before.get("existed_before") is True:
                return False, "این Create قبلاً وجود داشته؛ Undo ندارد.", None
            return True, "", res
        if needs_bk:
            if atype == "delete" or tool == "delete_entry":
                hint = before.get("restore_hint")
                ttype = before.get("type")
                ag = action.get("args")
                rec = bool(ag.get("recursive")) if isinstance(ag, dict) else False
                if ttype == "dir" or rec is True:
                    if not hint or not str(hint).strip():
                        return False, "مسیر بازگشت Trash مشخص نیست.", None
                    try:
                        tabs = resolve_project_path(root, str(hint))
                        if not os.path.exists(tabs):
                            return False, "نسخه Trash برای Undo پیدا نشد.", None
                    except Exception as e:
                        return False, "مسیر Trash معتبر نیست: " + str(e), None
                    res["trash_abs"] = tabs
                    return True, "", res
            babs = _resolve_undo_backup_absolute(root, bref, full)
            if not babs:
                return False, "برای این Action نسخه Backup معتبر پیدا نشد.", None
            try:
                with open(babs, "rb") as f:
                    hdr = f.read(4096)
                if hdr is None:
                    raise ValueError("unreadable")
            except Exception:
                return False, "فایل Backup قابل خواندن نیست.", None
            res["backup_abs"] = babs
            return True, "", res
        return False, "این Action قابل Undo نیست.", None
    except Exception as e:
        return False, "بررسی Undo با خطا متوقف شد: " + str(e), None
def remember_undo_result(action, resolved=None, ok=False, reason="", undone_via=None):
    try:
        if not isinstance(action, dict):
            return None
        ctx = new_action_context("undo", tool="undo", args={"action_id": action.get("action_id")}, project=action.get("project"), project_root=action.get("project_root"), target=action.get("target"), allow_write=True, operation_class=ACTION_CLASS_RECOVERY, approval_state=ACTION_APPROVAL_NOT_REQUIRED)
        if ok:
            summary = "undo ok: " + str(action.get("action_id"))
            if undone_via:
                summary += " (" + str(undone_via) + ")"
            exe = ACTION_STATE_DONE
            ver = ACTION_VERIFY_OK
        else:
            summary = "undo blocked/failed: " + str(action.get("action_id")) + ": " + str(reason)
            exe = ACTION_STATE_FAILED
            ver = ACTION_VERIFY_SKIPPED
        bef = {"existed_before": True, "type": "undo", "undone_action": action.get("action_id"), "undone_tool": action.get("tool")}
        babs = resolved.get("backup_abs") if isinstance(resolved, dict) else None
        if babs:
            try:
                bef["restored_from"] = relative_display(resolved.get("project_root"), babs)
            except Exception:
                pass
        update_action_context(ctx, backup_path=babs, before_state=bef, execution_state=exe, verify_state=ver, result_summary=summarize_action_result(summary), ended_at=time.time())
        return remember_action(ctx)
    except Exception:
        return None

def undo_last_mutating_action(project=None, project_root=None, allow_write=False, stop_event=None):
    try:
        if is_stop_requested(stop_event):
            remember_stop_blocked_action(tool="undo", action_type="undo", args={}, project=project, project_root=project_root, target=None)
            return False, EMERGENCY_STOP_MESSAGE, None
        if not is_write_allowed(allow_write):
            return False, "حالت «اعمال تغییر» فعال نیست؛ Undo انجام نشد.", None
        action = get_last_mutating_action()
        if action is None:
            return False, "Action قابل Undo پیدا نشد.", None
        ok, reason, res = validate_undo_candidate(action, project=project, project_root=project_root)
        if not ok:
            # Phase F1: بلاک idempotency نباید mutation یا journal جدید بسازد؛
            # فقط Fail Closed گزارش شود (همان Action قبلاً Undo شده).
            if "قبلاً Undo شده" in str(reason):
                return False, "Undo انجام نشد: " + str(reason), None
            remember_undo_result(action, res, ok=False, reason=reason)
            return False, "Undo انجام نشد: " + str(reason), None
        root = res.get("project_root")
        full = res.get("full_path")
        atype = res.get("action_type")
        tool = res.get("tool")
        target = res.get("target")
        if atype == "multi_write" or tool == "write_files":
            return _undo_multi_write_action(action, res)
        if atype == "move" or tool == "move_entry":
            return _undo_move_action(action, res)
        if atype in ("create", "create_directory") or tool == "create_directory":
            return _undo_create_action(action, res)
        if (atype == "delete" or tool == "delete_entry") and res.get("trash_abs"):
            return _undo_trash_action(action, res)
        return _undo_backup_action(action, res)
    except Exception as e:
        return False, "Undo با خطای غیرمنتظره متوقف شد: " + str(e), None


def _undo_multi_write_action(action, res):
    # Undo کل Batch: هر عضو ازپیش‌موجود از همان Backup استاندارد
    # برگردانده می‌شود و هر عضو ساخته‌شده حذف می‌شود — دقیقاً همان
    # رفتاری که Undo یک Write و یک Create جداگانه دارد.
    #
    # این تابع هیچ قاعدهٔ جدیدی نمی‌سازد: برای هر عضو، همان
    # validate/boundary/verify داخلی همان دو تابع موجود را صدا
    # می‌زند. اگر یک عضو شکست بخورد، نتیجه صادقانه گزارش و Action
    # «Undone» علامت نمی‌خورد تا کاربر بتواند دوباره تلاش کند.
    try:
        members = res.get("batch_files") or []
        restored = []
        removed = []
        failures = []

        for member in members:
            single = {
                "action_id": action.get("action_id"),
                "action_type": action.get("action_type"),
                "tool": action.get("tool"),
                "project": action.get("project"),
                "project_root": res.get("project_root"),
                "target": member.get("path"),
                "full_path": member.get("full_path"),
                "backup_abs": member.get("backup_abs"),
            }

            if member.get("existed_before"):
                ok, reason, _ = _undo_backup_action(action, single)
            else:
                ok, reason, _ = _undo_create_action(
                    action,
                    dict(
                        single,
                        before={"existed_before": False}
                    )
                )

            if ok:
                if member.get("existed_before"):
                    restored.append(member.get("path"))
                else:
                    removed.append(member.get("path"))
            else:
                failures.append(
                    str(member.get("path"))
                    + ": "
                    + str(reason)
                )

        if failures:
            m = (
                "Undo Batch کامل نشد: "
                + " | ".join(failures)
            )
            remember_undo_result(action, res, ok=False, reason=m)
            return False, "Undo انجام نشد: " + m, res

        remember_undo_result(
            action, res, ok=True, undone_via="multi-write-restore"
        )

        parts = []

        if restored:
            parts.append(
                str(len(restored)) + " فایل بازگردانده شد"
            )

        if removed:
            parts.append(
                str(len(removed)) + " فایل ساخته‌شده حذف شد"
            )

        return True, "Undo انجام شد (" + ", ".join(parts) + ").", res
    except Exception as e:
        remember_undo_result(action, res, ok=False, reason=str(e))
        return False, "Undo انجام نشد: " + str(e), res


def _undo_move_action(action, res):
    try:
        full = res.get("full_path")
        sabs = res.get("move_source_abs")
        if os.path.exists(sabs):
            m = "مبدأ بازگشت از قبل وجود دارد؛ Undo انجام نشد."
            remember_undo_result(action, res, ok=False, reason=m)
            return False, m, None
        if not os.path.exists(full):
            m = "مقصد Move برای Undo پیدا نشد."
            remember_undo_result(action, res, ok=False, reason=m)
            return False, "Undo انجام نشد: " + m, None
        mp = os.path.dirname(os.path.abspath(sabs))
        if not os.path.isdir(mp):
            os.makedirs(mp, exist_ok=True)
        if not is_inside_project(res.get("project_root"), mp):
            raise ValueError("outside project")
        shutil.move(full, sabs)
        if os.path.exists(full) or not os.path.exists(sabs):
            raise RuntimeError("verify failed")
        remember_undo_result(action, res, ok=True, undone_via="move-back")
        return True, "Undo انجام شد (move برگردانده شد).", res
    except Exception as e:
        remember_undo_result(action, res, ok=False, reason=str(e))
        return False, "Undo انجام نشد: " + str(e), None


def _undo_create_action(action, res):
    try:
        full = res.get("full_path")
        if not os.path.exists(full):
            remember_undo_result(action, res, ok=True, undone_via="already-gone")
            return True, "Undo انجام شد (مسیر از قبل حذف شده بود).", res
        if os.path.isfile(full):
            os.remove(full)
        elif os.path.isdir(full):
            if os.listdir(full):
                m = "پوشه خالی نیست؛ Undo انجام نشد."
                remember_undo_result(action, res, ok=False, reason=m)
                return False, m, None
            os.rmdir(full)
        else:
            m = "نوع مسیر برای Undo پشتیبانی نمی‌شود."
            remember_undo_result(action, res, ok=False, reason=m)
            return False, "Undo انجام نشد: " + m, None
        if os.path.exists(full):
            raise RuntimeError("verify failed")
        remember_undo_result(action, res, ok=True, undone_via="remove-created")
        return True, "Undo انجام شد (مسیر ساخته‌شده حذف شد).", res
    except Exception as e:
        remember_undo_result(action, res, ok=False, reason=str(e))
        return False, "Undo انجام نشد: " + str(e), None


def _undo_trash_action(action, res):
    try:
        full = res.get("full_path")
        tabs = res.get("trash_abs")
        if os.path.exists(full):
            m = "مسیر هدف از قبل وجود دارد؛ Undo انجام نشد."
            remember_undo_result(action, res, ok=False, reason=m)
            return False, m, None
        par = os.path.dirname(os.path.abspath(full))
        if not os.path.isdir(par):
            os.makedirs(par, exist_ok=True)
        shutil.move(tabs, full)
        if not os.path.exists(full) or os.path.exists(tabs):
            raise RuntimeError("verify failed")
        remember_undo_result(action, res, ok=True, undone_via="trash-restore")
        return True, "Undo انجام شد (پوشه بازگردانده شد).", res
    except Exception as e:
        remember_undo_result(action, res, ok=False, reason=str(e))
        return False, "Undo انجام نشد: " + str(e), None
def _undo_backup_action(action, res):
    try:
        root = res.get("project_root")
        full = res.get("full_path")
        atype = res.get("action_type")
        tool = res.get("tool")
        target = res.get("target")
        babs = res.get("backup_abs")
        if not babs or not os.path.isfile(babs):
            m = "برای این Action نسخه Backup معتبر پیدا نشد."
            remember_undo_result(action, res, ok=False, reason=m)
            return False, "Undo انجام نشد: " + m, None
        with open(babs, "rb") as f:
            data = f.read()
        try:
            btext = data.decode("utf-8")
        except UnicodeDecodeError:
            m = "Backup فایل UTF-8 نیست؛ Undo انجام نشد."
            remember_undo_result(action, res, ok=False, reason=m)
            return False, m, None
        if os.path.isfile(full):
            try:
                create_backup(root, full)
            except Exception as e:
                m = "ساخت Safety Backup شکست خورد؛ Undo انجام نشد: " + str(e)
                remember_undo_result(action, res, ok=False, reason=m)
                return False, m, None
        else:
            par = os.path.dirname(os.path.abspath(full))
            if not os.path.isdir(par):
                try:
                    os.makedirs(par, exist_ok=True)
                except Exception as e:
                    remember_undo_result(action, res, ok=False, reason=str(e))
                    return False, "Undo انجام نشد: " + str(e), None
            if not is_inside_project(root, par):
                m = "مسیر والد خارج از پروژه است."
                remember_undo_result(action, res, ok=False, reason=m)
                return False, "Undo انجام نشد: " + m, None
        with open(full, "w", encoding="utf-8", newline="") as f:
            f.write(btext)
        try:
            if sha1_of_file(full) != sha1_of_file(babs):
                raise RuntimeError("Verify ناموفق")
        except RuntimeError:
            raise
        except Exception:
            pass
        remember_undo_result(action, res, ok=True, undone_via="backup-restore")
        lbl = "فایل حذف‌شده بازگردانده شد" if (atype == "delete" or tool == "delete_entry") else "فایل بازگردانده شد"
        return True, "Undo انجام شد (" + lbl + ": " + str(target) + ").", res
    except Exception as e:
        t = str(e)
        if "Undo انجام نشد" in t:
            return False, t, None
        remember_undo_result(action, res, ok=False, reason=t)
        return False, "Undo انجام نشد: " + t, None





def tool_arg(args, *names):
    # خواندن یک آرگومان با نامهای جایگزین ممکن.
    if not isinstance(args, dict):
        return None

    for name in names:
        if name in args:
            value = args[name]

            if value not in (None, ""):
                return value

    return None


def tool_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def tool_bool(value):
    if isinstance(value, bool):
        return value

    if value is None:
        return False

    return str(value).strip().lower() in (
        "1", "true", "yes", "y", "بله", "درست", "با محتویات"
    )


def tool_ok(text, verified=None, data=None):
    return {
        "ok": True,
        "verified": verified,
        "text": "" if text is None else str(text),
        "data": data or {},
    }


def tool_fail(error):
    return {
        "ok": False,
        "verified": None,
        "text": "خطا: " + str(error),
        "data": {},
    }


def clip_text(text, limit=MAX_TOOL_TEXT_CHARS):
    text = "" if text is None else str(text)

    if len(text) <= limit:
        return text

    return text[:limit] + "\n… (متن کوتاه شد)"


# ---------------------------------------------------------
# ثبت خطای Agentic Loop (بدون تغییر در رفتار fallback)
# ---------------------------------------------------------
# آخرین خطایی که باعث رفتن از Agentic Loop به Legacy شد.
# فقط برای مشاهده و عیب‌یابی؛ در مسیر تصمیم‌گیری هیچ
# نقشی ندارد و هیچ رفتاری را تغییر نمی‌دهد.
LAST_LOOP_FAILURE = None


def record_agentic_loop_failure(
    user_request,
    project_name,
    exc_info=None,
):
    # خطای حلقه را ثبت می‌کند تا شکست پنهان نماند.
    #
    # سه قاعده:
    #   1) هرگز استثنا پرتاب نمی‌کند (ثبت نباید fallback
    #      را خراب کند).
    #   2) اطلاعات حساس درخواست کاربر را لاگ نمی‌کند؛ فقط
    #      نوع خطا و پیامش.
    #   3) مسیر fallback و Safety دست‌نخورده می‌ماند.
    global LAST_LOOP_FAILURE

    error = None

    try:
        if exc_info:
            error = exc_info[1]
        else:
            error = sys.exc_info()[1]

        if error is None:
            return None

        entry = {
            "project": project_name,
            "error_type": type(error).__name__,
            "error": clip_text(str(error), 200),
        }

        LAST_LOOP_FAILURE = entry

        # لاگ در stderr؛ همان الگویی که شاخه‌ی Multi-Agent
        # از قبل استفاده می‌کند.
        print(
            "[agentic-loop] failed; falling back to legacy: "
            "%s: %s" % (entry["error_type"], entry["error"]),
            file=sys.stderr,
        )

        return entry

    except Exception:
        # ثبت خطا هرگز نباید خودش خطای جدید بسازد.
        return None


def get_last_loop_failure():
    # فقط خواندن برای UI/عیب‌یابی؛ کپی برگردانده می‌شود
    # تا caller نتواند منبع را تغییر دهد.
    if not isinstance(LAST_LOOP_FAILURE, dict):
        return None

    return dict(LAST_LOOP_FAILURE)


# تعداد خطوطی که قبل و بعد از هر خط سیگنال حفظ می‌شوند.
REVIEW_SIGNAL_CONTEXT_LINES = 12

# خواندن دسته‌ای چند فایل با بودجه تک‌فایل جواب نمی‌دهد: در
# تست واقعی، elementor-pro سومین فایل بود و سهم ۱/۴ یعنی
# حدود ۳۰۰ کاراکتر که فقط به هدر می‌رسید. برای observation
# چندفایلی سقف جدا دارد، ولی همچنان خیلی کمتر از
# MAX_OBSERVATIONS_CHARS است.
REVIEW_MULTI_FILE_CLIP_FACTOR = 2

# حداکثر دفعاتی که Agent می‌تواند مدل را یادآوری کند که قبل
# از FINAL باید فایل دارای بالاترین سیگنال را بخواند.
MAX_EVIDENCE_NUDGES = 1

# حداکثر دفعاتی که Agent می‌تواند مدل را یادآوری کند که قبل
# از FINAL باید فایل‌های نام‌بردهٔ درخواست را واقعاً تغییر
# دهد و Verify کند. یک یادآوری کافی است؛ اگر مدل باز هم
# زود FINAL بنویسد، گزارش وضعیت واقعی را نشان می‌دهد.
MAX_COMPLETION_NUDGES = 1

# حداکثر دفعاتی که Agent یادآوری می‌کند یک Mutation پیشنهادی
# همهٔ فایل‌های مرتبطِ درخواست را پوشش نداده است.
#
# این یادآوری کاملاً advisory است: فقط یک‌بار در کل حلقه، فقط
# وقتی coverage قطعی باشد. مدل آزاد است نادیده بگیرد و Plan
# خودش را ارائه کند، پس هرگز باعث توقف دائمی نمی‌شود.
MAX_COVERAGE_NUDGES = 1

# سقف تلاش با «نام ابزار نامعتبر».
#
# این خطا گام مصرف نمی‌کند (چون هیچ ابزاری اجرا نشده)، بنابراین
# سقف کوچک و مستقلی می‌خواهد تا حلقه در برابر مدلی که پیوسته
# نام غلط می‌فرستد بی‌نهایت نچرخد.
MAX_INVALID_TOOL_RETRIES = 2

# برای برش هوشمند فقط الگوهای قوی/قابلیت‌محور به کار می‌روند.
# الگوهای تأییدی مثل external-url روی تقریباً هر فایل PHP
# (هدر Plugin URI) خطا می‌دهند و بودجه context را بی‌فایده مصرف
# می‌کنند؛ برای فهرست سیگنال باقی می‌مانند، برای برش نه.
REVIEW_PRIORITY_PATTERNS = None


def get_review_priority_patterns():
    # lazy: REVIEW_SIGNAL_PATTERNS پایین‌تر در همین فایل تعریف
    # شده و در زمان import هنوز وجود ندارد.
    global REVIEW_PRIORITY_PATTERNS

    if REVIEW_PRIORITY_PATTERNS is None:
        chosen = []

        for tier, label, pattern in REVIEW_SIGNAL_PATTERNS:
            if tier >= REVIEW_TIER_CAPABILITY:
                chosen.append((tier, label, pattern))

        REVIEW_PRIORITY_PATTERNS = tuple(chosen)

    return REVIEW_PRIORITY_PATTERNS


def _scan_signal_hits(lines):
    hits = []

    for number, line in enumerate(lines, 1):
        for tier, label, pattern in get_review_priority_patterns():
            if pattern.search(line):
                hits.append((number, tier, label))
                break

    return hits


def _merge_signal_windows(hits, total_lines):
    windows = []

    for number, tier, label in hits:
        start = max(1, number - REVIEW_SIGNAL_CONTEXT_LINES)
        end = min(total_lines, number + REVIEW_SIGNAL_CONTEXT_LINES)

        if windows and start <= windows[-1][1] + 1:
            if end > windows[-1][1]:
                windows[-1][1] = end
            continue

        windows.append([start, end])

    return windows


def _render_numbered_windows(lines, windows, budget):
    # پنجره‌های دور خطوط سیگنال را با شماره خط چاپ می‌کند تا
    # مدل بتواند شماره‌های اعلام‌شده در فهرست سیگنال را به کد
    # وصل کند.
    if not windows:
        return ""

    share = max(200, budget // len(windows))
    out = []
    used = 0
    previous_end = 0

    for start, end in windows:
        if used >= budget:
            out.append("… (سیگنال‌های بیشتری وجود دارد)")
            break

        if start > previous_end + 1:
            gap = (
                "… (خطوط "
                + str(previous_end + 1)
                + "-"
                + str(start - 1)
                + " اینجا خلاصه شد) …"
            )
            out.append(gap)
            used += len(gap) + 1

        body_lines = []
        body_used = 0
        truncated = False

        for number in range(start, end + 1):
            chunk = str(number) + ": " + lines[number - 1]

            if body_used + len(chunk) + 1 > share:
                truncated = True
                break

            body_lines.append(chunk)
            body_used += len(chunk) + 1

        if truncated:
            body_lines.append(
                "… (ادامه تا خط " + str(end) + " خلاصه شد)"
            )

        block = "\n".join(body_lines)
        out.append(block)
        used += len(block) + 1
        previous_end = end

    return "\n".join(out)


def _split_file_blocks(text):
    # خروجی read_files چند بلوک BEGIN_FILE/END_FILE دارد.
    blocks = []
    current_path = None
    current = []

    for line in text.splitlines():
        if line.startswith("BEGIN_FILE "):
            current_path = line[len("BEGIN_FILE "):].strip()
            current = []
            continue

        if line.startswith("END_FILE") and current_path is not None:
            blocks.append((current_path, current))
            current_path = None
            current = []
            continue

        if current_path is not None:
            current.append(line)

    if current_path is not None:
        blocks.append((current_path, current))

    return blocks


def _render_multi_file(text, limit, shares):
    blocks = _split_file_blocks(text)
    out = []

    for index, (path, body) in enumerate(blocks):
        share = shares[index]
        hits = _scan_signal_hits(body)

        if share < 60:
            out.append(
                "BEGIN_FILE " + path
                + "\n… (بودجه context تمام شد؛ این فایل "
                "خوانده نشد)\nEND_FILE " + path
            )
            continue

        if hits:
            rendered = _render_numbered_windows(
                body,
                _merge_signal_windows(hits, len(body)),
                share
            )
        else:
            rendered = clip_text_head_tail("\n".join(body), share)

        if len(rendered) > share:
            rendered = clip_text(rendered, share)

        out.append(
            "BEGIN_FILE " + path + "\n" + rendered
            + "\nEND_FILE " + path
        )

    return "\n".join(out)


def _clip_multi_file_by_signal(text, limit):
    # هر فایل سهم بودجه خودش را می‌گیرد؛ فایلی که سیگنال
    # دارد سهم بیشتری می‌گیرد تا در خواندن چندتایی، فایلِ
    # مشکوک به‌خاطر فایل‌های بی‌ربط قبلش حذف نشود.
    blocks = _split_file_blocks(text)

    if not blocks:
        return clip_text_head_tail(text, limit)

    weights = []
    total_weight = 0

    for path, body in blocks:
        weight = 2 if _scan_signal_hits(body) else 1
        weights.append(weight)
        total_weight += weight

    # سقف اولیه؛ رندر واقعی با شماره خط و سرصفحه‌ها بلندتر
    # می‌شود، پس بعد از رندر دوباره اندازه‌گیری و کوچک می‌شود.
    shares = [
        max(80, int(limit * weight / total_weight))
        for weight in weights
    ]

    result = _render_multi_file(text, limit, shares)

    # کوچک‌سازی از فایل‌های بدون سیگنال شروع می‌شود تا شواهد
    # آخرین چیزی باشد که از دست می‌رود.
    order = sorted(
        range(len(shares)),
        key=lambda i: (weights[i] == 2, -shares[i])
    )

    for _ in range(24):
        if len(result) <= limit:
            break

        target = None

        for index in order:
            if shares[index] > 60:
                target = index
                break

        if target is None:
            break

        shares[target] = max(60, int(shares[target] * 0.75))
        result = _render_multi_file(text, limit, shares)

    if len(result) <= limit:
        return result

    return clip_text(result, limit)


def clip_text_by_signal(text, limit=MAX_OBSERVATION_CHARS):
    # برش هوشمند observation:
    # به‌جای بریدن «سرِ فایل» (که معمولاً فقط هدر و کامنت است)،
    # خطوطی که واقعاً سیگنال دارند و context اطرافشان حفظ
    # می‌شوند و بقیه به‌صورت خلاصه حذف می‌شود.
    #
    # دلیل: در تست واقعی، شواهد elementor-pro در خطوط 30 تا 75
    # بود ولی با برش ساده در 1200 کاراکتر، سر فایل دیده می‌شد و
    # خود شواهد هرگز به مدل نمی‌رسید.
    text = "" if text is None else str(text)

    if len(text) <= limit:
        return text

    if "BEGIN_FILE " in text:
        return _clip_multi_file_by_signal(text, limit)

    lines = text.splitlines()

    if not lines:
        return clip_text(text, limit)

    hits = _scan_signal_hits(lines)

    if not hits:
        return clip_text_head_tail(text, limit)

    return _render_numbered_windows(
        lines, _merge_signal_windows(hits, len(lines)), limit
    )


def clip_text_head_tail(text, limit=MAX_TOOL_TEXT_CHARS):
    # وقتی هیچ سیگنالی نیست، سر و ته فایل هر دو حفظ می‌شوند؛
    # انتهای فایل PHP اغلب همان جایی است که هوک‌ها ثبت می‌شوند.
    text = "" if text is None else str(text)

    if len(text) <= limit:
        return text

    marker = "\n… (میانه فایل خلاصه شد) …\n"
    head_budget = int((limit - len(marker)) * 0.6)
    tail_budget = limit - len(marker) - head_budget

    if tail_budget < 80:
        return clip_text(text, limit)

    return text[:head_budget] + marker + text[-tail_budget:]


def tool_list_directory(project_root, args):
    depth = min(max(tool_int(tool_arg(args, "depth"), 1), 1), 3)

    try:
        info = list_directory(
            project_root,
            tool_arg(args, "path", "relative_path", "dir"),
            depth=depth
        )

    except Exception as e:
        return tool_fail(e)

    return tool_ok(
        "Directory: " + info["path"] + "\n" + info["text"],
        verified=True,
        data={"path": info["path"], "count": info["count"]}
    )


def tool_structure(project_root, args):
    depth = min(max(tool_int(tool_arg(args, "depth"), 2), 1), 3)

    try:
        text = build_project_structure(
            project_root,
            tool_arg(args, "path", "relative_path"),
            depth=depth
        )

    except Exception as e:
        return tool_fail(e)

    return tool_ok(text, verified=True)


def tool_find_entries(project_root, args):
    try:
        results = find_entries(
            project_root,
            name=tool_arg(args, "name", "filename"),
            pattern=tool_arg(args, "pattern", "glob"),
            subdir=tool_arg(args, "path", "subdir", "dir"),
            max_results=20
        )

    except Exception as e:
        return tool_fail(e)

    if not results:
        return tool_ok("چیزی پیدا نشد.", verified=True)

    lines = [
        item["relative_path"]
        + ("/" if item["type"] == "dir" else "")
        for item in results
    ]

    return tool_ok("\n".join(lines), verified=True)


def tool_search_in_files(project_root, args):
    query = tool_arg(args, "query", "text", "term", "pattern")

    if not query:
        return tool_fail("آرگومان query لازم است.")

    try:
        results = search_in_files(
            project_root,
            query,
            subdir=tool_arg(args, "path", "subdir", "dir"),
            max_results=20
        )

    except Exception as e:
        return tool_fail(e)

    return tool_ok(
        format_search_results(results, query),
        verified=True,
        data={"query": query, "hits": len(results)}
    )


def tool_find_dependencies(project_root, args):
    try:
        results = find_dependencies(
            project_root,
            subdir=tool_arg(args, "path", "subdir", "dir"),
            max_results=25
        )

    except Exception as e:
        return tool_fail(e)

    if not results:
        return tool_ok("وابستگیای پیدا نشد.", verified=True)

    lines = [
        item["relative_path"]
        + ":"
        + str(item["line"])
        + ": "
        + item["text"]
        for item in results
    ]

    return tool_ok("\n".join(lines), verified=True)


def tool_read_file(project_root, args):
    path = tool_arg(
        args, "path", "relative_path", "file", "filename", "name"
    )

    if not path:
        return tool_fail("آرگومان path لازم است.")

    # Phase H — Security Boundary. قبل از هر I/O بررسی
    # می‌شود تا حتی یک بایت از فایل حساس خوانده نشود.
    if is_sensitive_read_target(path):
        return tool_fail(sensitive_read_refusal_text(path))

    try:
        content = read_file(project_root, path)

    except Exception as e:
        return tool_fail(e)

    # لایه‌ی دوم: حتی در فایل غیرحساس، مقادیر حساسِ
    # درون‌متنی به مدل داده نمی‌شوند.
    content = mask_sensitive_content(content)

    return tool_ok(
        content,
        verified=True,
        data={"path": path, "size": len(content)}
    )


def tool_read_files(project_root, args):
    paths = tool_arg(args, "paths", "files")

    if isinstance(paths, str):
        paths = [paths]

    if not isinstance(paths, list) or not paths:
        return tool_fail("آرگومان paths (لیست) لازم است.")

    items = read_files_multi(project_root, paths[:6])
    blocks = []

    for item in items:
        if item["content"] is None:
            blocks.append(
                item["relative_path"] + ": " + str(item["error"])
            )
        else:
            # Phase H — Security Boundary. مسیر حساسِ باقی‌مانده
            # هرگز وارد block نمی‌شود؛ فقط دلیل مسدودی گزارش
            # می‌شود تا مدل بداند چرا فایل نبود.
            if is_sensitive_read_target(item["relative_path"]):
                blocks.append(
                    "BEGIN_FILE " + item["relative_path"] + "\n"
                    + sensitive_read_refusal_text(
                        item["relative_path"]
                    )
                    + "\nEND_FILE " + item["relative_path"]
                )
                continue

            blocks.append(
                "BEGIN_FILE " + item["relative_path"] + "\n"
                + mask_sensitive_content(item["content"])
                + "\nEND_FILE " + item["relative_path"]
            )

    return tool_ok("\n\n".join(blocks), verified=True)


def normalize_batch_files(args):
    """
    Normalize a batch write request into a list of file entries.

    Accepts either:
      {"files": [{"path": ..., "content": ...}, ...]}
    or a JSON-encoded string of that shape (the model emits text, so the
    whole object may arrive as a string).

    Returns a list of dicts with "path" and "content" keys. It raises
    ValueError on anything unusable so the caller fails closed.
    """
    payload = args

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            raise ValueError("آرگومان files باید JSON معتبر باشد.")

    if not isinstance(payload, dict):
        raise ValueError("آرگومان files باید یک شیء باشد.")

    files = payload.get("files")

    if files is None:
        files = payload.get("batch")

    if files is None:
        files = payload.get("changes")

    if isinstance(files, str):
        try:
            files = json.loads(files)
        except Exception:
            raise ValueError("لیست files باید JSON معتبر باشد.")

    if not isinstance(files, list) or not files:
        raise ValueError("آرگومان files باید یک لیست غیرخالی باشد.")

    normalized = []

    for item in files:
        if not isinstance(item, dict):
            raise ValueError("هر عضو files باید یک شیء باشد.")

        path = item.get("path") or item.get("relative_path") or item.get("file")

        if not path:
            raise ValueError("هر عضو files باید دارای path باشد.")

        content = item.get("content")

        if content is None:
            content = item.get("text")

        if content is None:
            content = item.get("body")

        if content is None:
            raise ValueError(
                "هر عضو files باید دارای content باشد: " + str(path)
            )

        entry = {"path": str(path), "content": str(content)}

        action = item.get("action")

        if action:
            entry["action"] = str(action)

        normalized.append(entry)

    return normalized


def validate_batch_args(project_root, args):
    """
    Validate EVERY member of a batch before anything is executed.

    This runs at plan time and again at execute time. Any invalid member
    raises, which aborts the WHOLE batch: a partially valid batch is never
    executed. All paths still go through resolve_project_path / safe_path,
    so project boundary and traversal rules are unchanged.
    """
    files = normalize_batch_files(args)

    if not files:
        raise ValueError("Batch باید حداقل یک فایل داشته باشد.")

    for entry in files:
        resolve_project_path(project_root, entry["path"])

    return files


def tool_write_files(project_root, args):
    """
    Execute a whole batch of file writes as ONE atomic operation.

    Delegates to the existing write_file_multi, which already performs
    per-file transactional backup, atomic writes, post-write validation
    and full rollback if any single file fails. No new write engine is
    introduced here.

    Step 3: the batch is verified AFTER the mutation (read-back +
    syntax, read-only) and the outcome is reported honestly instead of
    assuming success. A batch that did not pass verification reports
    "verified": False plus the structured errors, so the Core can feed
    them back to the model for exactly one correction.
    """
    files = validate_batch_args(project_root, args)

    try:
        result = write_file_multi(
            project_root,
            files,
            return_details=True,
        )
    except Exception as e:
        return tool_fail(e)

    if not result.get("success"):
        # Mutation با Rollback شکست خورد: خطاهای واقعی همان تلاش
        # استخراج و ساختاریافته به مدل برگردانده می‌شوند.
        verify = verify_failed_mutation(result)

        return {
            "ok": False,
            "verified": False,
            "text": (
                "خطا: "
                + str(result.get("error") or "Batch ناموفق بود.")
                + "\n"
                + format_verify_feedback(verify, "write_files")
            ),
            "data": {
                "batch": True,
                "count": 0,
                "files": [],
                "rolled_back": bool(result.get("rolled_back")),
                "verify": verify,
            },
        }

    written = [
        item.get("relative_path")
        for item in (result.get("files") or [])
    ]

    # Step 3: Verify بعد از Mutation موفق (فقط خواندن).
    verify = verify_applied_files(project_root, files)
    verified = bool(verify.get("ok"))

    text = (
        "Batch اجرا شد؛ "
        + str(len(written))
        + " فایل نوشته شد: "
        + ", ".join(str(p) for p in written)
        + " | Verify: "
        + ("موفق" if verified else "ناموفق")
    )

    if not verified:
        text += "\n" + format_verify_feedback(verify, "write_files")

    return {
        "ok": True,
        "verified": verified,
        "text": text,
        "data": {
            "batch": True,
            "count": len(written),
            "files": written,
            "rolled_back": False,
            "verify": verify,
        },
    }


def tool_write_file(project_root, args):
    path = tool_arg(args, "path", "relative_path", "file")
    content = tool_arg(args, "content", "text", "body")

    if not path:
        return tool_fail("آرگومان path لازم است.")

    if content is None:
        return tool_fail("آرگومان content لازم است.")

    try:
        details = write_file(
            project_root,
            path,
            content,
            return_details=True,
            create_parents=True
        )

    except Exception as e:
        return tool_fail(e)

    validation = details.get("validation") or {}
    relative_path = details.get("relative_path") or path

    # Step 3: نتیجه Verify همین Mutation به‌صورت ساختاریافته
    # همراه نتیجه برگردانده می‌شود تا Core بتواند خطای واقعی را
    # به مدل برگرداند.
    verify = verify_from_validation(relative_path, validation)

    return {
        "ok": True,
        "verified": bool(validation.get("valid")),
        "text": "\n".join(
            summarize_validation(validation)
        ),
        "data": {
            "path": relative_path,
            "backup_path": details.get("backup_path"),
            "diff": details.get("diff"),
            "content_matches": validation.get("content_matches"),
            "verify": verify,
        },
    }


def tool_edit_file(project_root, args):
    path = tool_arg(args, "path", "relative_path", "file")
    old_text = tool_arg(args, "old", "old_text", "find", "search")
    new_text = tool_arg(args, "new", "new_text", "replace", "with")

    if not path:
        return tool_fail("آرگومان path لازم است.")

    if old_text is None or new_text is None:
        return tool_fail("آرگومانهای old و new لازم است.")

    try:
        details = edit_file(
            project_root,
            path,
            old_text,
            new_text,
            replace_all=tool_bool(tool_arg(args, "replace_all")),
            return_details=True
        )

    except Exception as e:
        return tool_fail(e)

    validation = details.get("validation") or {}
    verified = (
        bool(validation.get("valid"))
        if validation else bool(details.get("changed"))
    )

    # Step 3: نتیجه Verify همین Edit به‌صورت ساختاریافته برگردانده
    # می‌شود (همان Validation موجود). اگر Validation در دسترس نباشد
    # هیچ ادعایی ساخته نمی‌شود و Verify خالی می‌ماند.
    verify = (
        verify_from_validation(
            details.get("relative_path") or path, validation
        )
        if validation else None
    )

    lines = summarize_validation(validation)
    lines.append(
        "تعداد جایگزینی: "
        + str(details.get("replacements", 0))
    )

    return {
        "ok": True,
        "verified": verified,
        "text": "\n".join(lines),
        "data": {
            "path": details.get("relative_path"),
            "backup_path": details.get("backup_path"),
            "diff": details.get("diff"),
            "verify": verify,
        },
    }


def tool_create_directory(project_root, args):
    path = tool_arg(args, "path", "relative_path", "dir")

    if not path:
        return tool_fail("آرگومان path لازم است.")

    try:
        details = create_directory(project_root, path)

    except Exception as e:
        return tool_fail(e)

    return tool_ok(
        "پوشه ساخته شد: " + details["relative_path"],
        verified=True,
        data={"path": details["relative_path"]}
    )


def tool_move_entry(project_root, args):
    source = tool_arg(args, "source", "from", "path", "old")
    target = tool_arg(args, "target", "to", "destination", "new")

    if not source or not target:
        return tool_fail("آرگومانهای source و target لازم است.")

    try:
        details = move_entry(project_root, source, target)

    except Exception as e:
        return tool_fail(e)

    lines = [
        "مبدأ: " + details["source"],
        "مقصد: " + details["target"],
        "Verify: مقصد وجود دارد و مبدأ حذف شد",
    ]

    if details.get("same_content") is True:
        lines.append("Verify: تطابق محتوا (SHA1) تایید شد")

    return tool_ok(
        "\n".join(lines),
        verified=bool(details.get("verified")),
        data={
            "source": details["source"],
            "target": details["target"],
        }
    )


def tool_delete_entry(project_root, args):
    path = tool_arg(args, "path", "relative_path", "file")

    if not path:
        return tool_fail("آرگومان path لازم است.")

    try:
        details = delete_entry(
            project_root,
            path,
            recursive=tool_bool(
                tool_arg(args, "recursive", "force")
            )
        )

    except Exception as e:
        return tool_fail(e)

    lines = [
        "حذف شد: " + details["relative_path"],
        "نوع: " + details["type"],
        "Verify: مسیر دیگر وجود ندارد",
    ]

    if details.get("backup_path"):
        lines.append("Backup: " + details["backup_path"])

    if details.get("trashed_to"):
        lines.append(
            "قابل بازگردانی از: " + details["trashed_to"]
        )

    return tool_ok(
        "\n".join(lines),
        verified=bool(details.get("verified")),
        data={"path": details["relative_path"]}
    )


def tool_website_analyze(project_root, args):
    # Phase G: read-only wrapper. No backup, no write, no command.
    if _phase_g_analyze is None or _phase_g_format is None:
        return tool_fail("website analyzer در دسترس نیست.")
    try:
        data = _phase_g_analyze(project_root)
    except Exception as e:
        return tool_fail(e)
    try:
        text = _phase_g_format(data)
    except Exception as e:
        return tool_fail(e)
    return tool_ok(
        clip_text(text),
        verified=True,
        data={"cms": (data or {}).get("cms", "unknown")},
    )


def tool_run_command(project_root, args):
    command = tool_arg(args, "command", "cmd", "shell")

    if not command:
        return tool_fail("آرگومان command لازم است.")

    try:
        result = run_command(
            project_root,
            command,
            cwd=tool_arg(args, "cwd", "path")
        )

    except Exception as e:
        return tool_fail(e)

    return tool_ok(
        format_command_report(result),
        verified=bool(result.get("success")),
        data={
            "command": result.get("command"),
            "returncode": result.get("returncode"),
        }
    )


def tool_seo_fetch_page(project_root, args):
    try:
        import seo_adapter as _sa
    except Exception as e:
        return tool_fail(e)
    url = tool_arg(args, "url")
    timeout = int(tool_arg(args, "timeout") or 30)
    r = _sa.run("fetch_page.py", [url, "--timeout", str(timeout)])
    if not r.ok:
        return tool_fail(r.error or r.stderr)
    return tool_ok(str(r.data)[:4000] if r.data else r.raw_stdout, verified=True, data=r.data or {})


def tool_seo_parse_html(project_root, args):
    try:
        import seo_adapter as _sa
    except Exception as e:
        return tool_fail(e)
    url = tool_arg(args, "url")
    file_path = tool_arg(args, "file", "path")
    cli_args = ["--json"]
    if url:
        cli_args += ["--url", url]
    if file_path:
        cli_args += [file_path]
    r = _sa.run("parse_html.py", cli_args)
    if not r.ok:
        return tool_fail(r.error or r.stderr)
    return tool_ok(str(r.data)[:4000] if r.data else r.raw_stdout, verified=True, data=r.data or {})


def tool_seo_sitemap(project_root, args):
    try:
        import seo_adapter as _sa
    except Exception as e:
        return tool_fail(e)
    url = tool_arg(args, "url")
    r = _sa.run("sitemap_discovery.py", [url, "--json"])
    if not r.ok:
        return tool_fail(r.error or r.stderr)
    return tool_ok(str(r.data)[:4000] if r.data else r.raw_stdout, verified=True, data=r.data or {})


def tool_seo_audit(project_root, args):
    try:
        import seo_adapter as _sa
    except Exception as e:
        return tool_fail(e)
    url = tool_arg(args, "url")
    r = _sa.run("fetch_page.py", [url])
    if not r.ok:
        return tool_fail(r.error or r.stderr)
    r2 = _sa.run("parse_html.py", ["--url", url, "--json"])
    if not r2.ok:
        return tool_fail(r2.error or r2.stderr)
    return tool_ok("SEO audit completed", verified=True, data={"fetch": r.data, "parse": r2.data})


def tool_web_search(project_root, args):
    try:
        import web_research as _wr
    except Exception as e:
        return tool_fail(e)
    q = tool_arg(args, "query", "q")
    try:
        r = _wr.web_search(q)
    except Exception as e:
        return tool_fail(e)
    return tool_ok(_wr.format_search_results(r, q), verified=True, data={"count": len(r)})


def tool_web_extract(project_root, args):
    try:
        import web_research as _wr
    except Exception as e:
        return tool_fail(e)
    url = tool_arg(args, "url")
    info = _wr.extract_page_text(url)
    return tool_ok(_wr.format_page_text(info), verified=True, data={"url": url})


def tool_seo_pagespeed(project_root, args):
    try:
        import seo_adapter as _sa
    except Exception as e:
        return tool_fail(e)
    url = tool_arg(args, "url")
    r = _sa.run("pagespeed_check.py", [url])
    if not r.ok:
        return tool_fail(r.error or r.stderr)
    return tool_ok(r.raw_stdout[:4000], verified=True, data=r.data or {})


# فهرست سفید ابزارهای Agent.
# Tool-calling فقط روی همین نام‌ها مجاز است.
TOOL_REGISTRY = {
    "list_directory": {
        "mutating": False,
        "run": tool_list_directory,
        "usage": '{"path": "wp-content", "depth": 1}',
        "description": "فهرست واقعی یک پوشه (مسیر نسبی)",
    },
    "structure": {
        "mutating": False,
        "run": tool_structure,
        "usage": '{"path": ".", "depth": 2}',
        "description": "ساختار/درخت پروژه یا یک زیرپوشه",
    },
    "find_entries": {
        "mutating": False,
        "run": tool_find_entries,
        "usage": '{"name": "index.php"} یا {"pattern": "*.php", "path": "wp-content"}',
        "description": "پیدا کردن فایل/پوشه با نام یا glob",
    },
    "search_in_files": {
        "mutating": False,
        "run": tool_search_in_files,
        "usage": '{"query": "wp_head", "path": "wp-content"}',
        "description": "جستجوی متن داخل فایل‌ها",
    },
    "find_dependencies": {
        "mutating": False,
        "run": tool_find_dependencies,
        "usage": '{"path": "wp-content/plugins"}',
        "description": "پیدا کردن include/require/import/use",
    },
    "read_file": {
        "mutating": False,
        "run": tool_read_file,
        "usage": '{"path": "index.php"}',
        "description": "خواندن کامل یک فایل متنی",
    },
    "read_files": {
        "mutating": False,
        "run": tool_read_files,
        "usage": '{"paths": ["a.php", "b.php"]}',
        "description": "خواندن چند فایل مرتبط با هم",
    },
    "website_analyze": {
        "mutating": False,
        "run": tool_website_analyze,
        "usage": '{}',
        "description": "تحلیل فقط-خواندنی سایت/وردپرس (بدون تغییر)",
    },
    "web_search": {
        "mutating": False,
        "run": tool_web_search,
        "usage": '{"query": "seo best practices"}',
        "description": "جستجوی وب (خواندنی)",
    },
    "web_extract": {
        "mutating": False,
        "run": tool_web_extract,
        "usage": '{"url": "https://example.com"}',
        "description": "استخراج متن صفحه (خواندنی)",
    },
    "seo_fetch_page": {
        "mutating": False,
        "run": tool_seo_fetch_page,
        "usage": '{"url": "https://example.com"}',
        "description": "واکشی صفحه برای تحلیل SEO (خواندنی)",
    },
    "seo_parse_html": {
        "mutating": False,
        "run": tool_seo_parse_html,
        "usage": '{"url": "https://example.com"}',
        "description": "پارس HTML و استخراج عناصر SEO (خواندنی)",
    },
    "seo_sitemap": {
        "mutating": False,
        "run": tool_seo_sitemap,
        "usage": '{"url": "https://example.com"}',
        "description": "کشف sitemap (خواندنی)",
    },
    "seo_pagespeed": {
        "mutating": False,
        "run": tool_seo_pagespeed,
        "usage": '{"url": "https://example.com"}',
        "description": "بررسی PageSpeed (خواندنی)",
    },
    "seo_audit": {
        "mutating": True,
        "run": tool_seo_audit,
        "usage": '{"url": "https://example.com"}',
        "description": "تحلیل SEO کامل (نیازمند تأیید)",
    },
    "write_file": {
        "mutating": True,
        "run": tool_write_file,
        "usage": '{"path": "docs/note.md", "content": "..."}',
        "description": "نوشتن/ساخت فایل (نیازمند تأیید)",
    },
    # Step 1: batch write. One tool call = one logical operation and ONE
    # approval for the whole batch. Execution is delegated to the existing
    # atomic write_file_multi (transactional backup + rollback).
    "write_files": {
        "mutating": True,
        "run": tool_write_files,
        "usage": '{"files": [{"path": "a.php", "content": "..."}, {"path": "b.php", "content": "..."}]}',
        "description": "نوشتن گروهی چند فایل به‌صورت یک Batch اتمیک (یک تأیید برای کل Batch؛ با شکست یک فایل، همه rollback می‌شوند)",
    },
    "edit_file": {
        "mutating": True,
        "run": tool_edit_file,
        "usage": '{"path": "style.css", "old": "...", "new": "..."}',
        "description": "جایگزینی دقیق متن در فایل (نیازمند تأیید)",
    },
    "create_directory": {
        "mutating": True,
        "run": tool_create_directory,
        "usage": '{"path": "docs"}',
        "description": "ساخت پوشه (نیازمند تأیید)",
    },
    "move_entry": {
        "mutating": True,
        "run": tool_move_entry,
        "usage": '{"source": "a.txt", "target": "b.txt"}',
        "description": "تغییر نام/انتقال (نیازمند تأیید)",
    },
    "delete_entry": {
        "mutating": True,
        "run": tool_delete_entry,
        "usage": '{"path": "a.txt", "recursive": false}',
        "description": "حذف فایل/پوشه (نیازمند تأیید)",
    },
    "run_command": {
        "mutating": True,
        "run": tool_run_command,
        "usage": '{"command": "python --version", "cwd": "."}',
        "description": "اجرای دستور مجاز داخل پروژه (نیازمند تأیید)",
    },
}

_WEB_RESEARCH_ALLOWED_TOOLS = frozenset(("web_search", "web_extract"))


MUTATING_TOOLS = tuple(
    name for name, spec in TOOL_REGISTRY.items()
    if spec["mutating"]
)


def suggest_tool_name(unknown_name):
    # نزدیک‌ترین نام معتبر را پیشنهاد می‌دهد تا مدل به‌جای
    # تکرار خطای یکسان، نام درست را یاد بگیرد.
    #
    # این «alias» نیست: هیچ ابزاری به نام اشتباه اجرا نمی‌شود.
    # فقط یک پیشنهاد متنی به مدل داده می‌شود و تصمیم با خود
    # مدل است. اگر نام دقیقاً در Registry باشد همان برگردانده
    # می‌شود.
    if not isinstance(unknown_name, str):
        return None

    name = unknown_name.strip()
    if not name:
        return None

    # پسوند/پیشوند رایج مدل‌های محلی هم در نظر گرفته می‌شود
    # تا «edit» به «edit_file» و «write» به «write_file» برسد.
    for candidate in TOOL_REGISTRY:
        if name == candidate:
            return candidate

    matches = difflib.get_close_matches(
        name,
        list(TOOL_REGISTRY.keys()),
        n=1,
        cutoff=0.6,
    )

    if matches:
        return matches[0]

    return None


def build_tool_reference(allowed_tools=None):
    # متن راهنمای ابزارها برای مدل؛ از همان Registry ساخته
    # میشود تا هرگز با فهرست سفید اختلاف نداشته باشد.
    #
    # نام‌ها با تیتر جدا و صریح اعلام می‌شوند تا مدل‌های محلی
    # که نام ابزار را از پیش‌آموزش خود می‌آورند (مثل «edit»
    # به‌جای «edit_file») به سمت نام‌های دقیق هدایت شوند.
    lines = [
        "AVAILABLE TOOL NAMES (use EXACTLY these, nothing else):",
    ]

    for name, spec in TOOL_REGISTRY.items():
        if allowed_tools is not None and name not in allowed_tools:
            continue
        marker = " [needs approval]" if spec["mutating"] else ""
        lines.append("- " + name + marker)
        lines.append("    ARGS: " + spec["usage"])
        lines.append("    -> " + spec["description"])

    lines.append("")
    lines.append(
        "A tool name that is not in the list above does not exist. "
        "If you need to change a file, use edit_file (not 'edit'). "
        "Never invent or abbreviate a tool name."
    )

    return "\n".join(lines)


def request_mentions_path(user_request, relative_path):
    # آیا کاربر همین مسیر را در متن درخواست نام برده است؟
    # هر دو شکل «پوشه/فایل» و «فایل» جداگانه سنجیده می‌شوند تا
    # ذخیره‌سازی هر دو شکل در متن، مسیر را قطعی کند.
    normalized = str(relative_path or "").replace("\\", "/").strip("/")

    if not normalized:
        return False

    haystack = normalize_request_text(user_request or "").replace(
        "\\", "/"
    )
    basename = normalized.rsplit("/", 1)[-1]

    return (
        normalized.lower() in haystack
        or (len(basename) > 2 and basename.lower() in haystack)
    )


def request_asks_for_new_file(user_request):
    # درخواست صریحاً «ساختن چیز جدید» است؛ در این حالت یک مسیر
    # ناموجود حدس نیست و رفتار قبلی حفظ می‌شود.
    return contains_any(
        normalize_request_text(user_request or ""), CREATE_KEYWORDS
    )


def ensure_mutation_target_is_grounded(project_root, path, user_request):
    # ضد حدس (Anti-guess).
    #
    # ریشه‌ی خطای «Plan با index.php به‌جای index.html»: مدل وقتی
    # نام فایل را در درخواست نمی‌بیند، از مثال‌های داخل
    # Tool Reference (که خودشان index.php / style.css دارند)
    # نام کپی می‌کند. validate_mutating_args فقط امنیت مسیر را
    # می‌سنجید، نه وجود مسیر؛ بنابراین یک مسیر کاملاً ساختگی
    # به Plan راه می‌یافت.
    #
    # قانون کم‌ریسک: مسیرِ ناموجود فقط وقتی Plan می‌شود که یکی
    # از این سه برقرار باشد:
    #   1) درخواست اصلاً نیتِ تغییر نمی‌گوید (حالا رفتار قبلی
    #      حفظ می‌شود)، یا
    #   2) درخواست صریحاً «ساختن فایل جدید» می‌گوید، یا
    #   3) کاربر همان نام را در متن درخواست آورده است.
    # در غیر این صورت Plan ساخته نمی‌شود و به مدل گفته می‌شود
    # نام واقعی را از فایل‌های پروژه بردارد.
    #
    # این بررسی فقط برای ابزارهای تغییردهنده اجرا می‌شود؛ ابزارهای
    # فقط‌خواندنی (که کاوش مسیر ناموجود کاملاً مجاز است) اصلاً
    # به این تابع نمی‌رسند.
    if not user_request:
        return

    if not request_asks_for_mutation(user_request):
        return

    if request_asks_for_new_file(user_request):
        return

    full_path = resolve_project_path(project_root, path)

    if os.path.exists(full_path):
        return

    if request_mentions_path(user_request, path):
        return

    raise ValueError(
        "مسیر «" + str(path) + "» نه در پروژه وجود دارد و نه در "
        "درخواست نام برده شده است؛ این یک حدس است و Plan ساخته "
        "نشد. نام واقعی فایل را از فهرست فایل‌های پروژه بردار و "
        "دوباره تلاش کن."
    )


def validate_mutating_args(project_root, tool_name, args,
                          user_request=None):
    # قبل از نمایش Plan، آرگومانها و مسیرها بررسی میشوند.
    # مسیرها با safe_path/resolve_project_path کنترل
    # میشوند تا چیزی خارج از پروژه پیشنهاد نشود.
    #
    # user_request اختیاری است: وقتی فرستاده شود، مسیرهای
    # تغییردهنده علاوه بر کنترل مرز پروژه، از نظر «واقعی بودن»
    # هم سنجیده می‌شوند (ensure_mutation_target_is_grounded).
    # نبودن آن فقط به معنای فعال‌نبودن همین بررسی جدید است؛
    # هیچ کنترل امنیتی دیگری تغییر نمی‌کند.
    if tool_name == "run_command":
        command = tool_arg(args, "command", "cmd", "shell")

        if not command:
            raise ValueError("آرگومان command لازم است.")

        cwd = tool_arg(args, "cwd", "path")

        if cwd:
            resolve_project_path(project_root, cwd)

        return

    # Step 1: Batch. همه‌ی اعضا پیش از نمایش Plan اعتبارسنجی
    # می‌شوند؛ یک عضو نامعتبر یعنی کل Batch رد می‌شود.
    if tool_name == "write_files":
        files = validate_batch_args(project_root, args)

        for entry in files:
            ensure_mutation_target_is_grounded(
                project_root, entry["path"], user_request
            )

            # Fail-Closed پیش از نمایش Plan: مقصدِ پوشه
            # هرگز به Plan راه نمی‌برد.
            ensure_write_target_is_not_directory(
                resolve_project_path(
                    project_root, entry["path"]
                ),
                entry["path"],
            )

        return

    if tool_name == "move_entry":
        source = tool_arg(args, "source", "from", "path", "old")
        target = tool_arg(args, "target", "to", "destination", "new")

        if not source or not target:
            raise ValueError(
                "آرگومانهای source و target لازم است."
            )

        resolve_project_path(project_root, source)
        resolve_project_path(project_root, target)

        return

    path = tool_arg(args, "path", "relative_path", "file", "dir")

    if not path:
        raise ValueError("آرگومان path لازم است.")

    resolve_project_path(project_root, path)

    if tool_name in ("write_file", "edit_file"):
        # Fail-Closed پیش از نمایش Plan: هدفِ پوشه رد می‌شود
        # تا نه Plan اشتباه به کاربر نشان داده شود و نه
        # موتور نوشتن با خطای مبهم مواجه شود.
        ensure_write_target_is_not_directory(
            resolve_project_path(project_root, path),
            path,
        )

        ensure_mutation_target_is_grounded(
            project_root, path, user_request
        )

    if tool_name == "write_file":
        if tool_arg(args, "content", "text", "body") is None:
            raise ValueError("آرگومان content لازم است.")

    if tool_name == "edit_file":
        if tool_arg(
            args, "old", "old_text", "find", "search"
        ) is None:
            raise ValueError("آرگومان old لازم است.")

        if tool_arg(
            args, "new", "new_text", "replace", "with"
        ) is None:
            raise ValueError("آرگومان new لازم است.")


def primary_target(tool_name, args):
    # مسیر اصلی یک عملیات (برای تشخیص «گام اصلاحی»).
    if not isinstance(args, dict):
        return ""

    if tool_name == "move_entry":
        return str(
            tool_arg(args, "source", "from", "path", "old") or ""
        )

    if tool_name == "write_files":
        # Step 3: یک «گام اصلاحی» برای Batch فقط وقتی همان
        # مجموعه مسیرهاست. Batch با مسیرهای جدید یک Plan جدید
        # است و باید از مسیر عادی Approval/Re-validation بگذرد،
        # نه از مسیر گام اصلاحی.
        try:
            paths = sorted(
                str(item.get("path"))
                for item in normalize_batch_files(args)
            )

        except Exception:
            return ""

        return "|".join(paths)[:500]

    return str(
        tool_arg(args, "path", "relative_path", "file", "dir") or ""
    )


def last_mutating_entry(transcript):
    for entry in reversed(transcript or []):
        if entry.get("mutating"):
            return entry

    return None


def is_correction_of_last_failure(tool_name, args, transcript):
    # یک «گام اصلاحی» یعنی همان ابزار و همان مسیرِ
    # عملیاتی که Verify آن ناموفق بوده است.
    last = last_mutating_entry(transcript)

    if last is None:
        return False

    if last.get("verified") is not False:
        return False

    if last.get("tool") != tool_name:
        return False

    return (
        primary_target(tool_name, args)
        == primary_target(last.get("tool"), last.get("args") or {})
    )


def is_failed_mutation_before_apply(entry):
    # شکست Mutation پیش از اعمال تغییر روی دیسک؛ مثلاً
    # edit_file با متن old اشتباه یا فایل ناموجود.
    # در این حالت هیچ تغییری انجام نشده و Verify اصلاً
    # اجرا نشده است، پس verified برابر None می‌ماند و
    # مسیر «اصلاح پس از Verify ناموفق» آن را نمی‌بیند.
    if not entry or not entry.get("mutating"):
        return False

    result = entry.get("result") or {}

    return (
        result.get("ok") is False
        and result.get("verified") is None
    )


def is_correction_of_failed_mutation(tool_name, args, transcript):
    # یعنی «تلاش اصلاحی» پس از شکست پیش از اعمال تغییر.
    # برخلاف is_correction_of_last_failure، این تشخیص فقط
    # یک برچسب است: خودِ اجرا از مسیر عادی Plan →
    # Approval → Re-validation → Execute عبور می‌کند و
    # هیچ فایلی را مستقیم تغییر نمی‌دهد.
    last = last_mutating_entry(transcript)

    if last is None:
        return False

    if not is_failed_mutation_before_apply(last):
        return False

    if last.get("tool") != tool_name:
        return False

    return (
        primary_target(tool_name, args)
        == primary_target(last.get("tool"), last.get("args") or {})
    )


def normalize_relative_target(path):
    # یکسان‌سازی مسیر برای مقایسه: جداکننده، حروف کوچک و
    # نبودِ «./» یا «/» ابتدایی. فقط شکل مسیر را یکسان
    # می‌کند و به هیچ فایلی دست نمی‌زند.
    text = str(path or "").strip().replace("\\", "/")

    while text.startswith("./"):
        text = text[2:]

    return text.strip("/").lower()


def mutation_targets_of_entry(entry):
    # مسیرهایی که یک گام Mutation واقعاً روی آن‌ها اجرا شده.
    target = primary_target(
        entry.get("tool"), entry.get("args") or {}
    )

    targets = []

    for part in str(target or "").split("|"):
        normalized = normalize_relative_target(part)

        if normalized and normalized not in targets:
            targets.append(normalized)

    return targets


def request_asks_for_mutation(user_request):
    # آیا متن درخواست اصلاً یک عمل تغییردهنده خواسته است؟
    # برای درخواست‌های تحلیلی/گفتگویی این قاعده خاموش
    # می‌ماند تا گزارش شلوغ و بی‌معنا نشود.
    text = normalize_request_text(user_request)

    if not text.strip():
        return False

    return any(
        keyword_matches_text(keyword, text)
        for keyword in ACTION_VERB_KEYWORDS
    )


def requested_mutation_status(
    project_root, user_request, transcript, known=None
):
    # وضعیت واقعی هر فایلِ نام‌برده در درخواست، فقط بر پایهٔ
    # عملیات واقعاً اجراشده.
    #
    # این تابع تنها منبع حقیقتِ گزارش نهایی است: نه متن FINAL
    # مدل و نه نیت کاربر. یک فایل «verified» است فقط وقتی که
    # یک گام Mutation روی همان مسیر اجرا شده و Verify آن
    # True بوده باشد.
    #
    # known وضعیت ثبت‌شده در چرخه‌های Approval قبلی را حمل
    # می‌کند. بدون آن، هر بار که یک Plan تأیید و اجرا می‌شود
    # transcript از نو ساخته می‌شود و تغییرات موفقِ قبلی از
    # دید گزارش پاک می‌شوند و به‌اشتباه «بدون تغییر»
    # گزارش می‌شدند.
    if not request_asks_for_mutation(user_request):
        return []

    status = {}

    for item in find_requested_files(project_root, user_request):
        path = normalize_relative_target(
            item.get("relative_path")
        )

        if path:
            status[path] = "not changed"

    for path, state in (known or {}).items():

        if path in status and state != "not changed":
            status[path] = state

    for entry in transcript or []:
        if not entry.get("mutating"):
            continue

        verified = entry.get("verified")
        result = entry.get("result") or {}

        if verified is True:
            state = "verified"
        elif verified is False or result.get("ok") is False:
            state = "failed"
        else:
            state = "not changed"

        for path in mutation_targets_of_entry(entry):
            if path in status and state != "not changed":
                status[path] = state

    return [
        {"path": path, "status": state}
        for path, state in sorted(status.items())
    ]


def pending_mutation_targets(
    project_root, user_request, transcript, known=None
):
    # فایل‌های درخواستی که هنوز Mutation + Verify موفق
    # ندارند. خالی یعنی کارِ خواسته‌شده کامل شده است.
    return [
        item["path"]
        for item in requested_mutation_status(
            project_root, user_request, transcript, known
        )
        if item.get("status") != "verified"
    ]


def request_status_map(
    project_root, user_request, transcript, known=None
):
    # همان وضعیت واقعی، به شکل dict قابل ذخیره در Plan تا در
    # چرخهٔ بعدیِ Approval گم نشود.
    return {
        item["path"]: item.get("status")
        for item in requested_mutation_status(
            project_root, user_request, transcript, known
        )
    }


def build_completion_gate_input(
    project_root, user_request, transcript, known_request_status=None
):
    # Phase 3 — ورودی ساختاریافتهٔ دروازه تکمیل از داده‌های واقعی.
    #
    # الزامات فقط وقتی ساخته می‌شوند که قطعی باشند: همان فایل‌هایی
    # که find_requested_files واقعاً از متن درخواست استخراج کرده
    # (بدون حدس). اگر درخواست الزام‌های دیگری (غیرفایلی) هم داشته
    # باشد، این ورودی آن‌ها را نمی‌سنجد؛ بنابراین نتیجه Gate هرگز
    # دلیلِ «کامل بودن کل درخواست» نمی‌شود و فقط برای همین فایل‌های
    # مشخص شواهد سنجیده می‌شوند.
    #
    # شواهد فقط از نتایج واقعی ابزار ساخته می‌شوند:
    # - mutating entry با verified True و result.ok != False → success
    # - verified False یا result.ok False → failure
    # - بدون Verify معتبر → هیچ شاهدی (هرگز success)
    # ادعای FINAL مدل اینجا اصلاً دیده نمی‌شود؛ فقط به‌صورت
    # final_claim در فراخوانی پایین‌تر ثبت می‌شود.
    if not request_asks_for_mutation(user_request):
        return [], []

    requirements = []
    seen = set()

    for item in find_requested_files(project_root, user_request):
        path = normalize_relative_target(item.get("relative_path"))

        if path and path not in seen:
            seen.add(path)
            requirements.append({"id": path, "text": path})

    if not requirements:
        return [], []

    evidence = []

    # known_request_status فقط برای همان درخواست و همان الزام
    # معتبر است: کلیدها همان relative_pathهای نرمال‌شدهٔ همین
    # درخواست‌اند که در چرخهٔ Approval قبلیِ همین درخواست ساخته
    # شده‌اند (request_status_map). کلیدهای بیرون از مجموعه
    # الزام‌های فعلی نادیده گرفته می‌شوند.
    if isinstance(known_request_status, dict):

        for path, state in known_request_status.items():
            key = normalize_relative_target(path)

            if key not in seen:
                continue

            if state == "verified":
                evidence.append({"requirement": key, "outcome": "success"})
            elif state == "failed":
                evidence.append({"requirement": key, "outcome": "failure"})

    for entry in transcript or []:
        if not isinstance(entry, dict) or not entry.get("mutating"):
            continue

        result = entry.get("result")
        result = result if isinstance(result, dict) else {}
        verified = entry.get("verified")

        if verified is True and result.get("ok") is not False:
            outcome = "success"
        elif verified is False or result.get("ok") is False:
            outcome = "failure"
        else:
            # Mutation بدون Verify معتبر: نه موفقیت است و نه شکست
            # قطعی؛ Gate آن را «بدون شاهد» می‌سنجد.
            outcome = None

        if outcome is None:
            continue

        for path in mutation_targets_of_entry(entry):
            if path in seen:
                evidence.append({"requirement": path, "outcome": outcome})

    return requirements, evidence


def evaluate_completion_gate(
    project_root, user_request, transcript,
    known_request_status=None, final_claim=False
):
    # فراخوانی امنِ دروازه تکمیل: هیچ خطای import یا اجرایی نباید
    # حلقه را بشکند؛ در شکست، None برمی‌گردد و رفتار قبلیِ حلقه
    # بدون تغییر ادامه می‌یابد (Fail-Open به رفتار موجود، نه به
    # ادعای تکمیل).
    try:
        from agents.completion_gate import evaluate_completion

    except Exception as gate_error:
        try:
            print(
                "[completion-gate] import failed: %r" % (gate_error,),
                file=sys.stderr,
            )
        except Exception:
            pass

        return None

    try:
        requirements, evidence = build_completion_gate_input(
            project_root, user_request, transcript,
            known_request_status=known_request_status
        )

        # Gate فقط برای درخواست‌هایی فعال می‌شود که الزاماتشان به‌صورت
        # قطعی استخراج شده باشد؛ وگرنه خاموش است تا رفتار
        # تحلیلی/بدون‌نام‌فایل عوض نشود و هیچ ادعای تکمیلی ساخته نشود.
        if not requirements:
            return None

        return evaluate_completion(
            requirements=requirements,
            evidence=evidence,
            final_claim=bool(final_claim),
        )

    except Exception as gate_error:
        try:
            print(
                "[completion-gate] evaluation failed: %r" % (gate_error,),
                file=sys.stderr,
            )
        except Exception:
            pass

        return None


def result_was_unsuccessful(result):
    # آیا نتیجهٔ یک ابزارِ غیرmutating «بی‌نتیجه» بوده؟
    # (خطا، متن خالی، یا جستجویی با hits صفر)
    if not result.get("ok"):
        return True

    data = result.get("data") or {}

    if data.get("hits") == 0:
        return True

    text = str(result.get("text") or "").strip()

    if not text:
        return True

    return (
        text.startswith("نتیجه‌ای پیدا نشد")
        or text.startswith("وابستگیای پیدا نشد")
    )


def prior_unsuccessful_same_action(tool_name, args, transcript):
    # آیا همین ابزار با همین args قبلاً بی‌نتیجه اجرا شده؟
    try:
        key = json.dumps(args or {}, sort_keys=True, ensure_ascii=False)

    except (TypeError, ValueError):
        return None

    for entry in reversed(transcript or []):

        if entry.get("mutating"):
            continue

        if entry.get("tool") != tool_name:
            continue

        try:
            other = json.dumps(
                entry.get("args") or {},
                sort_keys=True,
                ensure_ascii=False
            )

        except (TypeError, ValueError):
            continue

        if other != key:
            continue

        if result_was_unsuccessful(entry.get("result") or {}):
            return entry

    return None


def prior_successful_same_action(
    tool_name,
    args,
    transcript
):
    # Phase H: آیا همین ابزار read-only با همین args قبلاً
    # با موفقیت اجرا شده است؟
    #
    # دلیل: یک ابزار فقط‌خواندنی قطعی است؛ اجرای دوباره‌اش
    # با همان args دقیقاً همان خروجی را می‌دهد که همین حالا
    # در context هست. در تست واقعی، مدل website_analyze را
    # سه بار تکرار کرد و بودجه گام را همین‌طور سوزاند.
    # گارد موجود فقط تکرار «بی‌نتیجه» را می‌گرفت.
    try:
        key = json.dumps(
            args or {}, sort_keys=True, ensure_ascii=False
        )

    except (TypeError, ValueError):
        return None

    for entry in reversed(transcript or []):

        if entry.get("mutating"):
            continue

        if entry.get("tool") != tool_name:
            continue

        try:
            other = json.dumps(
                entry.get("args") or {},
                sort_keys=True,
                ensure_ascii=False
            )

        except (TypeError, ValueError):
            continue

        if other != key:
            continue

        if not result_was_unsuccessful(
            entry.get("result") or {}
        ):
            return entry

    return None


def plan_requires_approval(allow_write):
    # تنها منبع حقیقت برای این پرسش: «آیا این Plan واقعاً
    # نیازمند تأیید کاربر است؟»
    #
    # شرط عمداً همان شرط سخت‌گیرانه‌ی is_write_allowed است که
    # Gate تأیید و handle_pending_action_request استفاده می‌کنند.
    # قبلاً build_pending_plan_text از truthiness ساده استفاده
    # می‌کرد، بنابراین یک مقدار truthy-but-not-True (مثل 1 یا
    # "write") متن «برای اجرا پاسخ دهید: تأیید» را چاپ می‌کرد
    # در حالی که Gate آن را رد می‌کرد و Pending Action ساخته
    # نمی‌شد؛ یعنی کاربر دستور تأییدی می‌دید که وجود نداشت.
    # Fail Closed: فقط boolean True واقعاً مجاز است.
    return is_write_allowed(allow_write)


def build_pending_plan_text(
    tool_name,
    args,
    project_root,
    write_mode=True,
    plan_summary=None
):
    # Plan قابل نمایش به کاربر قبل از عملیات تغییردهنده.
    lines = ["ابزار: " + tool_name]

    for key in ("path", "source", "target", "cwd", "command"):
        value = tool_arg(args, key)

        if value is not None:
            lines.append(key + ": " + str(value))

    if tool_name == "write_file":
        content = str(
            tool_arg(args, "content", "text", "body") or ""
        )
        lines.append("طول محتوا: " + str(len(content)) + " کاراکتر")
        lines.append(
            "ابتدای محتوا: "
            + content.strip()[:120].replace("\n", " ")
        )

    if tool_name == "write_files":
        # کاربر باید قبل از تأیید دقیقاً ببیند کدام فایل‌ها
        # قرار است تغییر کنند؛ بدون این، Plan چندفایلی فقط نام
        # ابزار را نشان می‌داد و محتوای قابل تأییدی نداشت.
        try:
            batch_paths = [
                str(item.get("path"))
                for item in normalize_batch_files(args)
            ]
        except Exception:
            batch_paths = []

        if batch_paths:
            lines.append("فایل‌های پیشنهادی:")

            for batch_path in batch_paths:
                lines.append("- " + batch_path)

            lines.append(
                "این "
                + str(len(batch_paths))
                + " فایل با هم و با یک تأیید نوشته می‌شوند؛ "
                "شکست هر فایل باعث rollback همه می‌شود."
            )

    if tool_name == "edit_file":
        old_text = str(
            tool_arg(args, "old", "old_text", "find", "search") or ""
        )
        new_text = str(
            tool_arg(args, "new", "new_text", "replace", "with") or ""
        )
        lines.append(
            "جایگزینی: «"
            + old_text.strip()[:120].replace("\n", " ")
            + "» → «"
            + new_text.strip()[:120].replace("\n", " ")
            + "»"
        )

    if tool_name == "delete_entry":
        lines.append(
            "توجه: حذف با Backup انجام میشود و برای "
            "پوشهها بازگردانیپذیر است."
        )

    if tool_name == "run_command":
        lines.append(
            "دستور داخل Project Scope و بدون Shell اجرا میشود."
        )

    # Stage 4 — خلاصهٔ چندفایلی grounded، اگر وجود داشته باشد،
    # دقیقاً قبل از پیام Approval نمایش داده می‌شود تا ترتیب
    # «Plan → Summary → Approval» برای کاربر واقعی باشد.
    # این متن فقط نمایشی است و هیچ اثری روی Plan، Approval،
    # Re-validation یا مسیر اجرا ندارد.
    if plan_summary:
        lines.append("")
        lines.append(str(plan_summary))

    lines.append("")

    # شرط سخت‌گیرانه: فقط وقتی واقعاً مجاز است که Gate هم
    # تأیید را بپذیرد. truthiness ساده می‌توانست دستور تأییدِ
    # بی‌پشتوانه نشان دهد.
    if plan_requires_approval(write_mode):
        lines.append("برای اجرا پاسخ دهید: تأیید")
        lines.append("برای لغو پاسخ دهید: لغو")
    else:
        lines.append(
            "این عملیات فقط در حالت «اعمال تغییر» (Write) اجرا میشود؛"
        )
        lines.append(
            "برای اجرا، همین درخواست را با حالت Write تکرار کنید."
        )

    return "\n".join(lines)


PLAN_SUMMARY_CONTENT_PREVIEW_CHARS = 80


def build_multi_file_plan_summary(pending):
    # Stage 4 — خلاصهٔ چندفایلی grounded.
    #
    # این خلاصه فقط و فقط از همان Plan canonical موجود (همان
    # Pending Action) ساخته می‌شود: همان tool، همان args و همان
    # context معتبر Re-validation. هیچ مسیر، نوع عملیات یا متنی
    # حدس زده نمی‌شود و متن خام درخواست کاربر هیچ نقشی ندارد؛ اگر
    # Plan و Context با هم هم‌خوان نباشند، اصلاً خلاصه‌ای ساخته
    # نمی‌شود (Fail Closed) و رفتار قبلی دست‌نخورده می‌ماند.
    #
    # تابع کاملاً Read-only است: نه Plan را mutate می‌کند، نه فایلی
    # می‌نویسد، نه Approval/Re-validation/Safety را دور می‌زند و نه
    # مسیر Mutation جدیدی می‌سازد. درخواست تک‌فایلی هم اصلاً خلاصهٔ
    # چندفایلی نمی‌گیرد تا رفتار فعلی آن کاملاً حفظ شود.
    if not isinstance(pending, dict):
        return None

    if pending.get("tool") != "write_files":
        return None

    args = pending.get("args")
    context = pending.get("context")

    if not isinstance(args, dict) or not isinstance(context, dict):
        return None

    try:
        files = normalize_batch_files(args)
    except Exception:
        return None

    if len(files) < 2:
        return None

    recorded_paths = context.get("batch_paths")

    if not isinstance(recorded_paths, list):
        return None

    if [str(item) for item in recorded_paths] != [
        str(item.get("path")) for item in files
    ]:
        # اعضای Plan با رکورد Re-validation یکی نیستند؛ به‌جای
        # نمایش چیزی نامطمئن، هیچ خلاصه‌ای ساخته نمی‌شود.
        return None

    project_root = pending.get("project_root")

    if not project_root:
        return None

    lines = [
        "خلاصه Plan چندفایلی ("
        + str(len(files))
        + " فایل، همه با یک تأیید):"
    ]

    for item in files:
        path = str(item.get("path"))
        content = str(item.get("content") or "")

        try:
            exists = os.path.exists(
                resolve_project_path(project_root, path)
            )

        except Exception:
            exists = None

        if exists is None:
            operation = "عملیات: نامشخص (مسیر معتبر نیست)"

        elif exists:
            operation = "عملیات: بازنویسی فایل موجود"

        else:
            operation = "عملیات: ایجاد فایل جدید"

        preview = ""

        for raw_line in content.splitlines():
            candidate = raw_line.strip()

            if candidate:
                preview = candidate
                break

        if len(preview) > PLAN_SUMMARY_CONTENT_PREVIEW_CHARS:
            preview = (
                preview[:PLAN_SUMMARY_CONTENT_PREVIEW_CHARS] + "..."
            )

        lines.append(
            "- "
            + path
            + " | "
            + operation
            + " | "
            + str(len(content))
            + " کاراکتر"
            + (" | " + preview if preview else "")
        )

    lines.append(
        "این خلاصه فقط از روی همین Plan واقعی ساخته شده است؛ "
        "محتوای کامل فایل‌ها اینجا نمایش داده نمی‌شود."
    )

    return "\n".join(lines)


def build_loop_system_prompt(allowed_tools=None):
    # Phase G.2 - Agent Response Intelligence (Guidance only).
    # Core Logic / Registry / Safety / Approval / Journal / Mutation
    # Flow / web_agent.py بدون تغيير باقي مانده‌اند.
    return """
You are a local project agent AND a professional project analyst.
You work ONLY inside the selected project on this computer.

You use the tools below, ONE tool per step.

""" + build_tool_reference(allowed_tools) + """

PROTOCOL - answer exactly in this format:

For using a tool:

THOUGHT: <one short line about why>
TOOL: <tool_name>
ARGS: {"key": "value"}

For finishing:

THOUGHT: <one short line>
FINAL: <answer for the user in Persian, following the FINAL structure below>

IMPORTANT:
- برای انجام کارها، از ابزارهای موجود استفاده کن. به جای توضیح دادن، ابزار را فراخوانی کن.
- When the user asks to read/show/inspect/search files or project state, you MUST call the appropriate tool (e.g. read_file, read_files, list_directory, search_in_files) and use real observations. Do not guess or answer from assumptions.

AUDIENCE AND LANGUAGE:
- The FINAL answer is written for a human, not for debug or terminal.
- The FINAL answer MUST be in Persian.
- THOUGHT stays one short line (English is fine).
- Do NOT output a bare machine log or transcript of tool calls.
  Mention tools only when transparency requires it, then focus on analysis.
- First consider the goal of CURRENT REQUEST; collect only evidence
  that serves that goal.

FINAL STRUCTURE - every FINAL for analysis/review MUST use exactly
these Persian headings, in this order:

## خلاصه وضعیت
## یافته‌های اصلی
## معنی یافته‌ها
## مشکلات یا ریسک‌های احتمالی
## پیشنهاد قدم بعد

RULES:
- Use exactly ONE tool per step. Never write two TOOL blocks.
- Write tool names exactly as listed above.
- ARGS must be valid JSON on one line.
- All paths are RELATIVE to the project root.
  Never use absolute paths like C:\\... or /etc/...
- Never invent files or contents. If you are not sure,
  use list_directory / find_entries / search_in_files first.
- Read-only tools do not need approval.
- write_file, edit_file, create_directory, move_entry,
  delete_entry and run_command need the user's approval:
  the agent shows your plan and waits for approval.
- After a change, VERIFY it (read the file back or check the
  tool result) before writing FINAL.
- If a change failed verification, you may propose ONE
  corrected step with the same tool and the same path.
- "Enough information" means REAL EVIDENCE, never a file
  listing. A directory tree, a theme/plugin name list, or the
  website_analyze report only prove what EXISTS. They never
  prove WHY something is wrong or risky.
- For any review / analysis / recommendation request, you MUST
  read the CONTENT of at least one real project file with
  read_file or read_files before you write FINAL, and your
  FINAL must cite something that only file content revealed
  (a function, an option, a filter, a hardcoded URL, a config
  value, a suspicious line). If you never read a file, you are
  describing a file system, not consulting on a project.
- Reuse previous Observations; never re-run structure /
  list_directory / find_entries for the same path or purpose.
- Never re-read a file you already read. If a path already
  appears in the observations or in EARLIER STEPS, its content
  is already in your context; use it instead of calling
  read_file / read_files again for that path.
- Efficient plan for a read-only project/website review:
  1) ONE structure call (or website_analyze) for orientation.
  2) find_entries or search_in_files to locate the files that
     actually decide the answer (plugin main files, custom
     folders, config, logs, agent code).
  3) read_file on those few files to get real evidence.
  4) FINAL with the five Persian headings above.
  Read the few files that matter; do NOT walk every subfolder
  and do NOT read every file. Selecting the right files is
  part of the analysis.
- FILE BUDGET - read files ONE AT A TIME with read_file, and
  prefer the top path listed under "ANOMALY SIGNALS" first.
  Do NOT batch many paths into read_files: long files are
  condensed to fit the context, so batching several files
  usually hides the evidence of the file you actually needed.
- EVIDENCE JUDGEMENT - when you read a file that was listed
  under "MUST READ FIRST", do NOT report that the file merely
  exists. State what the code actually does, quote the concrete
  line, and say plainly whether it is a real risk or a false
  positive. "This file is installed and contains settings" is
  worthless. Compare the lines you read against the signal line
  numbers and explain the match or the mismatch.
- FILE SELECTION - never draw a conclusion from a stub or a
  filler file. A file that only contains a comment, a header,
  a changelog, a license or a translation string is NOT
  evidence of a problem. In WordPress, an index.php that
  only says "Silence is golden" is normal and healthy; it is
  never a bug and never means the site is broken.
- Prefer files that define real behaviour: the main .php file
  of a plugin, functions.php of a theme, configuration files,
  .htaccess, custom application folders, agent/API code, and
  log files. Those are the files that can prove a risk.
- A file is worth reading only if it can change your
  conclusion. After reading it you must be able to say:
  "this code or setting causes X because of Y". If you
  cannot, you picked the wrong file - go find a better one.
- Do not conclude that a site is broken, insecure or
  incompatible from a single file without proof. State the
  fact you proved, then say what is still unknown.
- A FINAL answer must match the OBSERVATIONS. Never claim
  a file, text, or feature exists or is missing unless proved.
- Never repeat a TOOL with the same ARGS that already
  returned no results. If a search finds 0 results, your
  query is probably wrong: use a simpler literal substring
  (for example <title instead of <title>|<h1>) or read
  the file directly, then answer with FINAL.
- Look for ROOT CAUSES, not symptoms. When something looks
  wrong, try to name the code, option, dependency or
  configuration that causes it. If you cannot prove the
  cause, say so explicitly and name the next check that
  would prove it.
- Under FINDINGS explain each item simply; under MEANING explain
  its effect and why it matters; under RISKS list only evidence-based
  risks (or state none observed); under NEXT STEP give prioritized
  practical steps. Never guess missing facts; state what is missing
  and what check would answer it.
- If the project looks like WordPress (wp-content/themes/plugins),
  do NOT report only file structure: explain Theme, Plugins,
  page-builder, SEO, security, maintenance roles in simple words,
  with security/maintenance first.
- Never read sensitive files or secrets: wp-config.php, .env,
  passwords, tokens, API keys, salts. Masked values are not real.
- Read-only mode stays read-only; no change without clear user
  request plus approval.
- search_in_files matches plain text only (never regex);
  characters like | are matched literally.
- Keep THOUGHT to one short line. Do not explain the rules.
""".strip()


def build_loop_user_prompt(
    user_request,
    project_name,
    allow_write,
    transcript,
    observations,
    correction_attempts,
    last_step=False,
    project_root=None
):
    parts = [
        "PROJECT: " + str(project_name),
        "MODE: " + (
            "write (changes allowed, but each change needs "
            "the user's approval)"
            if allow_write
            else "read-only (no change is allowed)"
        ),
        "",
    ]

    history = get_recent_conversation_text(
        project_name, current_request=user_request
    )

    if history.strip():
        parts.append("RECENT CONVERSATION:")
        parts.append(history)
        parts.append("")

    # Phase 4 — Durable Memory (flag-gated). OFF by default: no change.
    relevant_memory = _get_durable_memory_text(
        user_request, project_name=project_name
    )

    if relevant_memory.strip():
        parts.append("RELEVANT MEMORY:")
        parts.append(relevant_memory)
        parts.append("")

    parts.append("CURRENT REQUEST:")
    parts.append(str(user_request))
    parts.append("")

    # ضد حدس: برای درخواست‌های تغییردهنده، نام فایل‌های واقعی
    # پروژه به مدل داده می‌شود تا Plan روی مسیر واقعی ساخته شود.
    # درخواست‌های فقط‌خواندنی عمداً دست‌نخورده می‌مانند.
    if project_root and request_asks_for_mutation(user_request):
        inventory = build_project_file_inventory(project_root)

        if inventory:
            parts.append(inventory)
            parts.append("")

    # Phase H: برگه واقعیت‌ها در گام‌های اولیه تزریق می‌شود
    # تا مدل به‌جای حدس زدن نام قالب/افزونه/مسیر، از نام‌های
    # قطعی استفاده کند.
    if not transcript:
        facts = build_loop_project_facts(project_root)

        if facts:
            parts.append(facts)
            parts.append("")

    if transcript:
        parts.append("STEPS ALREADY DONE:")

        for entry in transcript:
            verified = entry.get("verified")

            if verified is True:
                status = "verified"
            elif verified is False:
                status = "verify failed"
            else:
                status = "done"

            parts.append(
                "- step "
                + str(entry.get("step"))
                + ": "
                + str(entry.get("tool"))
                + " "
                + json.dumps(
                    entry.get("args") or {},
                    ensure_ascii=False
                )[:160]
                + " → "
                + status
            )

        parts.append("")

    if observations:
        parts.append("OBSERVATIONS (oldest first):")
        parts.append(format_observations(observations))
        parts.append("")

    last = last_mutating_entry(transcript)

    if last is not None and last.get("verified") is False:
        # Step 3: خطای واقعی Verify به مدل برگردانده می‌شود
        # (فایل + نوع خطا + پیام + line/position). اگر جزئیات
        # ساختاریافته‌ای موجود نباشد، فقط همان هشدار قبلی
        # نوشته می‌شود و هیچ خطایی ساخته نمی‌شود.
        last_verify = last.get("verify")

        if isinstance(last_verify, dict) and last_verify.get("errors"):
            parts.append(
                format_verify_feedback(
                    last_verify, last.get("tool")
                )
            )

            parts.append("")

        if correction_attempts < MAX_CORRECTION_STEPS:
            parts.append(
                "NOTE: the last change FAILED verification. "
                "Propose ONE corrected step with the same tool "
                "and the same path, or explain what is missing."
            )
        else:
            parts.append(
                "NOTE: the last change FAILED verification and "
                "the correction limit is used. Do not propose "
                "another change; answer with FINAL."
            )

        parts.append("")

    elif last is not None and is_failed_mutation_before_apply(last):
        # شکست Mutation پیش از اعمال تغییر (مثلاً متن old
        # اشتباه در edit_file): هیچ تغییری روی دیسک انجام
        # نشده است. خطای واقعی در Observation بالا آمده؛
        # اینجا فقط مسیر مجاز بعدی صریح می‌شود: اول فایل
        # فعلی را بخوان، بعد یک گام اصلاحی پیشنهاد بده که
        # دوباره از Approval و Re-validation عبور می‌کند.
        if correction_attempts < MAX_CORRECTION_STEPS:
            parts.append(
                "MUTATION FAILED (nothing was changed on disk)."
            )
            parts.append(
                "NOTE: read the current file first, then propose "
                "ONE corrected step with the same tool and the same "
                "path (it goes through approval and validation "
                "again), or explain what is missing."
            )
        else:
            parts.append(
                "NOTE: the mutation FAILED and the correction limit "
                "is used. Do not propose another change; answer with "
                "FINAL."
            )

        parts.append("")

    if last_step:
        # Phase H: رزرو آخرین گام برای پاسخ.
        # بدون این قاعده، Agent گام‌ها را تا سقف مصرف
        # می‌کرد و حلقه بدون هیچ FINALی تمام می‌شد؛ کاربر
        # فقط یک لاگ مراحل می‌دید و هیچ پاسخی دریافت
        # نمی‌کرد.
        parts.append(
            "IMPORTANT: this is your LAST step. Do not call any "
            "tool now. Write FINAL immediately and answer the "
            "CURRENT REQUEST completely, using only what the "
            "OBSERVATIONS above already proved."
        )
        parts.append("")

    elif observations:
        parts.append(
            "If the OBSERVATIONS already contain enough "
            "information to answer the CURRENT REQUEST, "
            "write FINAL now. Otherwise decide the next "
            "single step."
        )
    else:
        parts.append("Decide the next single step now.")

    return "\n".join(parts)


def extract_first_json_object(text):
    # استخراج اولین JSON object معتبر از متن مدل
    # (با شمارش براکت و احترام به رشته‌ها).
    if not text:
        return None

    start = text.find("{")

    while start != -1:
        depth = 0
        in_string = False
        escape = False

        for index in range(start, len(text)):
            char = text[index]

            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False

                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1

                if depth == 0:
                    candidate = text[start:index + 1]

                    try:
                        parsed = json.loads(candidate)

                        if isinstance(parsed, dict):
                            return parsed

                    except ValueError:
                        pass

                    break

        start = text.find("{", start + 1)

    return None


def parse_agent_decision(model_output):
    # تبدیل خروجی مدل به تصمیم ساختاریافته:
    # {"thought":..., "tool":..., "args":{...}}
    # {"thought":..., "final":"..."}
    text = (model_output or "").strip()

    if not text:
        return None

    decision = {
        "thought": None,
        "tool": None,
        "args": {},
        "final": None,
        "args_error": False,
    }

    thought_match = re.search(
        r"(?im)^\s*(?:THOUGHT|think|فکر)\s*[:\-]\s*(.+)",
        text
    )

    if thought_match:
        decision["thought"] = thought_match.group(1).strip()[:200]

    # فرم JSON کامل: {"tool": "...", "args": {...}}
    payload = extract_first_json_object(text)

    if payload and ("tool" in payload or "name" in payload):
        raw_tool = payload.get("tool") or payload.get("name")
        raw_args = (
            payload.get("args")
            or payload.get("arguments")
            or {}
        )

        decision["tool"] = str(raw_tool).strip()

        if isinstance(raw_args, dict):
            decision["args"] = raw_args
        else:
            decision["args_error"] = True

        return decision

    tool_match = re.search(
        r"(?im)^\s*(?:TOOL|tool|ابزار)\s*[:\-]\s*"
        r"[\"'`]?([A-Za-z_][A-Za-z0-9_]*)[\"'`]?",
        text
    )

    if tool_match:
        decision["tool"] = tool_match.group(1)

        args_marker = re.search(
            r"(?is)(?:ARGS|arguments|args)\s*[:\-]?\s*",
            text
        )

        if args_marker:
            tail = text[args_marker.end():]
            parsed_args = extract_first_json_object(tail)

            if isinstance(parsed_args, dict):
                decision["args"] = parsed_args
            else:
                decision["args_error"] = True
        else:
            parsed_args = extract_first_json_object(
                text[tool_match.end():]
            )

            if isinstance(parsed_args, dict):
                decision["args"] = parsed_args
            else:
                decision["args_error"] = True

        return decision

    final_match = re.search(
        r"(?ism)^\s*(?:FINAL|ANSWER|پاسخ نهایی|پاسخ)\s*[:\-]\s*(.+)$",
        text
    )

    if final_match:
        decision["final"] = final_match.group(1).strip()
    else:
        # مدل بدون پروتکل پاسخ داده است؛ همان متن پاسخ
        # نهایی در نظر گرفته میشود (بدون اجرای ابزار).
        decision["final"] = text

    return decision


def format_observation(
    step_number,
    tool_name,
    args,
    result,
    mutating
):
    status = "ok" if result.get("ok") else "error"

    text = result.get("text")
    limit = MAX_OBSERVATION_CHARS

    # Phase H — لایه‌ی آخر (defense in depth).
    #
    # این نقطه تنها جایی است که هر متن ابزار پیش از ورود به
    # context مدل از آن عبور می‌کند. مسدودسازیِ مسیرمحور در
    # tool_read_file / read_files_multi / search_in_files انجام
    # شده و برای Read/Search فعلی کافی است؛ این لایه فقط
    # تضمین می‌کند که اگر مسیر Read جدیدی در آینده اضافه شد
    # و فراموش شد، مقدار حساسِ درون‌متنی باز هم به مدل نرسد.
    #
    # عمداً کل observation را حذف نمی‌کنیم: یک Read عادی که
    # فقط نام یک فایل حساس را در متن خود دارد (مثلاً «این
    # فایل را نخواندم») باید سالم به مدل برسد. فقط مقدارِ
    # حساس Mask می‌شود.
    if text:
        text = mask_sensitive_content(str(text))

    if (
        text
        and "BEGIN_FILE " in str(text)
        and str(text).count("BEGIN_FILE ") > 1
    ):
        limit = MAX_OBSERVATION_CHARS * REVIEW_MULTI_FILE_CLIP_FACTOR

    if mutating:
        verified = result.get("verified")

        if verified is True:
            status += " | Verify: موفق"
        elif verified is False:
            status += " | Verify: ناموفق"
        else:
            status += " | Verify: انجام نشد"

    return (
        "step "
        + str(step_number)
        + " | "
        + str(tool_name)
        + " | args: "
        + json.dumps(args or {}, ensure_ascii=False)[:200]
        + " | "
        + status
        + "\n"
        + clip_text_by_signal(text, limit)
    )


def format_observations(
    observations,
    max_total=MAX_OBSERVATIONS_CHARS
):
    # جدیدترین مشاهده‌ها مهم‌ترند؛ برای context مدل از
    # انتها جمع می‌شود.
    blocks = []
    total = 0

    for item in reversed(observations):
        # هر observation از قبل در format_observation به
        # MAX_OBSERVATION_CHARS رسیده است؛ برش دوباره اینجا
        # ساختار «خطوط سیگنال + شماره خط» را دوباره می‌برد.
        block = str(item or "")

        if blocks and (total + len(block)) > max_total:
            break

        blocks.append(block)
        total += len(block)

    blocks.reverse()

    # Phase H: حافظه فشرده گام‌های قدیمی.
    # مشکل واقعی: وقتی متن یک مشاهده از context بیرون
    # می‌افتاد، مدل «فراموش» می‌کرد آن ابزار را اجرا کرده و
    # همان فایل را دوباره می‌خواند؛ این تکرارها سقف گام را
    # می‌خوردند و Agent اصلاً به FINAL نمی‌رسید. حذف کامل
    # اطلاعات بدترین حالت بود، پس یک خط فشرده از هر گام
    # قدیمی نگه داشته می‌شود.
    dropped = len(observations) - len(blocks)

    header = ""

    if dropped > 0:
        summaries = []

        for item in observations[:dropped]:
            first_line = str(item or "").split("\n", 1)[0]
            summaries.append(
                clip_text(first_line, 120).replace("\n", " ")
            )

        header = (
            "EARLIER STEPS (their detailed output was dropped to "
            "save context, but they are ALREADY DONE - never "
            "repeat them):\n"
            + "\n".join("- " + s for s in summaries)
            + "\n\n"
        )

    return header + "\n\n".join(blocks)


def build_loop_report(
    project_name,
    project_root,
    transcript,
    final_answer=None,
    pending_plan=None,
    notes=None,
    plan_needs_approval=True,
    user_request=None,
    known_request_status=None
):
    # Report یکنواخت حلقه Agentic.
    lines = [
        "حالت: Agentic Loop",
        "گام‌های اجراشده: " + str(len(transcript)),
    ]

    if transcript:
        lines.append("")
        lines.append("مراحل:")

        for entry in transcript:
            verified = entry.get("verified")
            marker = ""

            if entry.get("mutating"):
                if verified is True:
                    marker = " | Verify: موفق"
                elif verified is False:
                    marker = " | Verify: ناموفق"
                else:
                    marker = " | Verify: انجام نشد"

            lines.append(
                "گام "
                + str(entry.get("step"))
                + ": "
                + str(entry.get("tool"))
                + " "
                + json.dumps(
                    entry.get("args") or {},
                    ensure_ascii=False
                )[:160]
                + marker
            )

            # گزارش نمایشی هم باید شواهد را نشان دهد، وگرنه
            # کاربر دقیقاً همان چیزی را نمی‌بیند که مدل دیده.
            result_entry = entry.get("result")
            if isinstance(result_entry, dict):
                step_body = result_entry.get("text")
                data = result_entry.get("data") or {}
            elif isinstance(result_entry, str):
                step_body = result_entry
                data = {}
            else:
                step_body = None
                data = {}

            step_limit = MAX_REPORT_STEP_CHARS

            if (
                step_body
                and "BEGIN_FILE " in str(step_body)
                and str(step_body).count("BEGIN_FILE ") > 1
            ):
                step_limit = (
                    MAX_REPORT_STEP_CHARS
                    * REVIEW_MULTI_FILE_CLIP_FACTOR
                )

            step_text = clip_text_by_signal(
                step_body, step_limit
            ).replace("\n", "\n   ")

            lines.append("   " + step_text)

            if data.get("backup_path"):
                lines.append(
                    "   Backup: " + str(data["backup_path"])
                )

            if data.get("diff"):
                lines.append("   Diff:")
                lines.append(
                    "   "
                    + str(data["diff"])[:600].replace(
                        "\n", "\n   "
                    )
                )

    # شواهد اجرای فعلی: فقط عملیاتی که در همین transcript با
    # نتیجهٔ موفق ثبت شده‌اند. این بلوک تفکیکِ متن آزاد مدل از
    # واقعیت اجراست: ادعای «بررسی شد» بدون آمدن در این فهرست
    # تأییدنشده محسوب می‌شود و خواندنِ فایل هم به‌تنهایی دالّ
    # بر بررسی کامل یا Verify نیست.
    executed_reads = []
    seen_read_keys = set()

    for entry in transcript:
        if not isinstance(entry, dict):
            continue

        if str(entry.get("tool") or "") not in (
            "read_file", "read_files"
        ):
            continue

        values = read_paths_already_seen([entry])

        if not values:
            continue

        result = entry.get("result")

        if isinstance(result, dict) and result.get("ok") is False:
            continue

        for value in values:
            key = normalize_relative_target(value)

            if key and key not in seen_read_keys:
                seen_read_keys.add(key)
                executed_reads.append(str(value))

    lines.append("")
    lines.append(
        "شواهد اجرای فعلی — فایل‌های واقعاً خوانده‌شده در همین "
        "گام‌ها (خواندن، به‌تنهایی دالّ بر بررسی کامل یا Verify "
        "نیست):"
    )

    if executed_reads:
        for path in executed_reads:
            lines.append("- " + path)
    else:
        lines.append("- هیچ فایلی در اجرای فعلی خوانده نشد.")

    if final_answer:
        lines.append("")
        lines.append("پاسخ نهایی:")
        lines.append(
            clip_text(final_answer, MAX_FINAL_ANSWER_CHARS)
        )
        lines.append(
            "توجه: این متن خروجی مدل است؛ فقط موارد ثبت‌شده در "
            "«مراحل» و «شواهد اجرای فعلی» بالا واقعیتِ اجرای همین "
            "گام‌ها هستند."
        )

    # وضعیت واقعی فایل‌های درخواستی.
    #
    # «پاسخ نهایی» متن آزاد مدل است و ممکن است ادعایی فراتر
    # از عملیات واقعاً اجراشده داشته باشد. این بلوک تنها
    # بر پایهٔ transcript ساخته می‌شود و بلافاصله بعد از آن
    # می‌آید تا ادعای نادرست «همه تغییرات انجام شد» با
    # وضعیت واقعیِ فایل‌ها خنثی شود.
    request_status = requested_mutation_status(
        project_root, user_request, transcript, known_request_status
    )

    if request_status:
        pending_targets = [
            item["path"]
            for item in request_status
            if item.get("status") != "verified"
        ]

        lines.append("")
        lines.append("وضعیت واقعی فایل‌های درخواستی:")

        status_labels = {
            "verified": "تغییر اعمال و Verify موفق",
            "failed": "تغییر اعمال نشد (شکست خورد)",
            "not changed": "بدون تغییر (بدون Verify)",
        }

        for item in request_status:
            lines.append(
                "- "
                + str(item.get("path"))
                + ": "
                + status_labels.get(
                    item.get("status"), item.get("status")
                )
            )

        if pending_targets:
            lines.append("")
            lines.append(
                "هشدار: درخواست کامل انجام نشد. فایل‌های نام‌برده "
                "که تغییر و Verify نشدند: "
                + "، ".join(pending_targets)
            )

    if pending_plan:
        lines.append("")

        # عنوان فقط وقتی «نیازمند تأیید» است که واقعاً یک
        # Pending Action ساخته شده باشد. در غیر این صورت
        # عنوان فریبنده بود: کاربر فکر می‌کرد باید «تأیید»
        # بفرستد در حالی که چنین Pendingی وجود ندارد.
        if plan_needs_approval:
            lines.append("Plan پیشنهادی (نیازمند تأیید):")
        else:
            lines.append("Plan پیشنهادی:")

        lines.append(pending_plan)

    if notes:
        lines.append("")

        for note in notes:
            lines.append(note)

    verify_lines = []

    for entry in transcript:
        if not entry.get("mutating"):
            continue

        label = (
            "گام "
            + str(entry.get("step"))
            + " ("
            + str(entry.get("tool"))
            + "): "
        )

        if entry.get("verified") is True:
            verify_lines.append(
                label + "Read-Back و Verify موفق بود."
            )
        elif entry.get("verified") is False:
            verify_lines.append(label + "Verify ناموفق بود.")
        else:
            verify_lines.append(label + "Verify انجام نشد.")

    try:
        _records = []
        for _e in (transcript or []):
            if not isinstance(_e, dict):
                continue
            _tool = str(_e.get("tool") or "")
            if _tool not in ("fetch_page", "web_search"):
                continue
            _res = _e.get("result")
            _status = "ok"
            if isinstance(_res, dict):
                if _res.get("ok") is False:
                    _status = "failed"
                elif "(no readable text" in str(_res.get("text", "")):
                    _status = "empty"
                elif str(_res.get("text", "")).strip().lower() in ("(no results)",) or (_res.get("data") or {}).get("hits") == 0:
                    _status = "no_hits"
            _rec = {"tool": _tool, "status": _status}
            if isinstance(_res, dict):
                _data = _res.get("data") or {}
                if "url" in _data:
                    _rec["url"] = _data["url"]
                elif "url" in (_e.get("args") or {}):
                    _rec["url"] = (_e.get("args") or {}).get("url")
                if "query" in _data:
                    _rec["query"] = _data["query"]
                if "hits" in _data:
                    _rec["hits"] = _data["hits"]
            _records.append(_rec)
        global _LAST_LOOP_REPORT_LEDGER
        _LAST_LOOP_REPORT_LEDGER = _records
    except Exception:
        pass

    return build_report(
        "Agentic Loop",
        project_name,
        project_root,
        lines,
        verify_lines
    )


def build_forced_final(
    user_request,
    project_name,
    project_root,
    transcript,
    observations,
    stop_event=None
):
    # تضمین اینکه Agent همیشه یک پاسخ انسانی بدهد.
    #
    # دلیل: اگر حلقه بدون FINAL تمام شود (سقف گام، گام
    # تکراری، یا ابزار نامعتبر در گام آخر)، build_loop_report
    # فقط یک لاگ مراحل برمی‌گرداند و کاربر هیچ پاسخی
    # نمی‌گیرد. این تابع یک فراخوانی نهایی و فقط‌متنی انجام
    # می‌دهد: نه ابزاری اجرا می‌شود و نه هیچ تغییری ممکن است.
    # کاملاً read-only و مستقل از Safety/Approval است.
    if not transcript:
        return None

    if is_stop_requested(stop_event):
        return None

    try:
        answer = call_model(
            "You are a senior technical consultant finishing a "
            "project review. The evidence below was already "
            "collected from the real project on disk.\n"
            "Write ONLY the final report, in Persian, using "
            "exactly these headings:\n"
            "## خلاصه وضعیت\n"
            "## یافته‌های اصلی\n"
            "## معنی یافته‌های اصلی\n"
            "## مشکلات یا ریسک‌های احتمالی\n"
            "## پیشنهاد قدم بعد\n"
            "Every statement must be grounded in the evidence "
            "below. Never invent files, code, versions or "
            "settings. If a fact is unknown, say so and name "
            "the check that would answer it. Do not call any "
            "tool and do not repeat the evidence verbatim.",
            "USER REQUEST:\n"
            + str(user_request)
            + "\n\nPROJECT:\n"
            + str(project_name)
            + "\n\nEVIDENCE ALREADY COLLECTED:\n"
            + format_observations(observations),
            stop_event=stop_event,
            n_predict=LOOP_MODEL_N_PREDICT,
            no_think=True
        )

    except Exception:
        return None

    answer = str(answer or "").strip()

    answer = re.sub(
        r"^\s*<think>\s*</think>\s*",
        "",
        answer,
        count=1
    ).strip()

    return answer or None


# Phase H: لایه اولویت‌بندی فایل برای تحلیل مشاوره‌ای.
#
# این «ابزار جدید» نیست: نه ورودی در TOOL_REGISTRY دارد، نه
# مسیر Approval، و نه چیزی را تغییر می‌دهد. فقط یک لایه
# فقط‌خواندنی است که حدس زدنِ «کدام فایل مهم است» را از مدل
# می‌گیرد و به او می‌دهد.
#
# اصل طراحی: رتبه‌بندی و سیگنال ناهنجاری عمداً از هم جدا
# هستند. رتبه‌بندی کاملاً ساختاری و بدون مثبت کاذب است؛
# الگوها فقط «برای بررسی» فهرست می‌شوند و وزنی در رتبه‌بندی
# ندارند، چون در عمل ثابت شد الگوی عمومی روی یک نصب
# استاندارد ده‌ها مثبت کاذب می‌سازد.

REVIEW_EXTRA_SKIP_DIRS = {
    "vendor", "node_modules", "build", "dist", "languages",
    "tests", "test", "docs", "doc", "migrations",
    "cache", "storage", "__snapshots__",
}

# در نصب وردپرس، wp-includes و wp-admin در همه سایت‌ها
# یکسان‌اند؛ محل شناسایی ناهنجاری نیستند.
REVIEW_WP_CORE_DIRS = {"wp-includes", "wp-admin"}

# هیچ‌وقت محتوای این‌ها خوانده نمی‌شود.
REVIEW_SKIP_BASENAMES = {
    "wp-config.php", ".env", "config.php",
}
REVIEW_SKIP_SUFFIXES = (
    ".key", ".pem", ".crt", ".p12", ".pfx",
)

REVIEW_PHP_ENTRY_NAMES = ("functions.php", "plugin.php")

REVIEW_CONFIG_FILES = (
    ".htaccess", "composer.json", "package.json", "wp-cli.yml",
    "requirements.txt", "pyproject.toml", "Dockerfile",
)

REVIEW_NOISE_BASENAMES = (
    "readme.txt", "readme.md", "license.txt", "license.md",
    "changelog.md", "index.php",
)

REVIEW_CODE_SUFFIXES = (".php", ".py", ".js", ".mjs", ".ts")

REVIEW_MAX_SCAN_FILES = 400
REVIEW_MAX_SCAN_BYTES = 400 * 1024
REVIEW_MAX_SCAN_DEPTH = 4
REVIEW_SHORTLIST_LIMIT = 5
REVIEW_SIGNAL_LIMIT = 5

# Tier A: تقریباً قطعاً ناهنجار.
REVIEW_TIER_STRONG = 300
# Tier B: قابلیت خطرناک (لزوماً بد نیست، باید بررسی شود).
REVIEW_TIER_CAPABILITY = 200
# Tier C: فقط تأییدی؛ عمداً وزن کمی دارد.
REVIEW_TIER_CORROBORATION = 40

REVIEW_SIGNAL_PATTERNS = (
    (REVIEW_TIER_STRONG, "license-bypass", re.compile(
        r"(elementor_pro_license|_license_v2_data|"
        r"license['\"]?\s*=>\s*['\"]valid|"
        r"['\"]status['\"]\s*=>\s*['\"]ACTIVE)", re.I)),
    (REVIEW_TIER_STRONG, "ssl-verification-off", re.compile(
        r"sslverify['\"]?\s*=>\s*false", re.I)),
    (REVIEW_TIER_STRONG, "request-interception", re.compile(
        r"(pre_http_request|"
        r"add_filter\s*\(\s*['\"][^'\"]*connect/[^'\"]+)", re.I)),
    (REVIEW_TIER_CAPABILITY, "option-write-at-toplevel", re.compile(
        r"^(update_option|add_option|update_site_option)\s*\(", re.I)),
    (REVIEW_TIER_CAPABILITY, "shell-exec", re.compile(
        r"\b(shell_exec|exec|system|passthru|proc_open|popen)\s*\(",
        re.I)),
    (REVIEW_TIER_CAPABILITY, "dynamic-code-exec", re.compile(
        r"\b(eval|assert|create_function)\s*\(|base64_decode\s*\(",
        re.I)),
    (REVIEW_TIER_CORROBORATION, "credential-literal", re.compile(
        r"(password|passwd|secret|api[_-]?key|token)\s*[:=]>?\s*"
        r"['\"][^'\"]{4,}", re.I)),
    (REVIEW_TIER_CORROBORATION, "external-url", re.compile(
        r"https?://(?!127\.0\.0\.1|localhost|example\.com|"
        r"wordpress\.org|w3\.org|schema\.org|www\.w3\.org|"
        r"github\.com)[a-z0-9.-]+\.[a-z]{2,}", re.I)),
)


def _review_is_sensitive(basename):
    name = (basename or "").lower()

    if name in REVIEW_SKIP_BASENAMES:
        return True

    return name.endswith(REVIEW_SKIP_SUFFIXES)


def _review_skip_dirs(is_wordpress):
    skipped = set(IGNORED_DIR_NAMES) | REVIEW_EXTRA_SKIP_DIRS

    if is_wordpress:
        skipped = skipped | REVIEW_WP_CORE_DIRS

    return skipped


def _review_scan_roots(project_root, is_wordpress):
    # فقط «کد اول‌-party» بررسی می‌شود: فولدرهای اختصاصی،
    # افزونه‌ها، mu-plugins و قالب‌ها. core و vendor رد می‌شوند.
    roots = []
    skip = _review_skip_dirs(is_wordpress)

    try:
        entries = sorted(os.listdir(project_root))

    except OSError:
        return roots

    for name in entries:
        full = os.path.join(project_root, name)

        if not os.path.isdir(full):
            continue

        if name in skip or name.startswith("."):
            continue

        roots.append(full)

    if not is_wordpress:
        return roots

    for group in ("plugins", "mu-plugins", "themes"):
        base = os.path.join(
            project_root, "wp-content", group
        )

        if not os.path.isdir(base):
            continue

        try:
            children = sorted(os.listdir(base))

        except OSError:
            continue

        for name in children:
            full = os.path.join(base, name)

            if not os.path.isdir(full):
                continue

            if name in skip or name.startswith("."):
                continue

            roots.append(full)

    return roots


def _review_scan_file(full_path, relative_path):
    # امضای یک فایل: فقط (امتیاز ساختاری، دلیل، سیگنال‌ها).
    parts = relative_path.replace("\\", "/").split("/")
    basename = parts[-1]
    low = basename.lower()

    score = 0
    reasons = []
    signals = []

    # (3) الگوهای حساس - فقط برای فهرست سیگنال، بدون وزن
    if low.endswith(REVIEW_CODE_SUFFIXES):
        try:
            if os.path.getsize(full_path) <= REVIEW_MAX_SCAN_BYTES:
                with open(
                    full_path, "r", encoding="utf-8",
                    errors="ignore"
                ) as f:
                    for number, line in enumerate(f, 1):
                        for tier, label, pattern in (
                            REVIEW_SIGNAL_PATTERNS
                        ):
                            if pattern.search(line):
                                signals.append(
                                    (tier, label, number)
                                )
        except OSError:
            pass

    # (2) فایل اصلی افزونه
    if (
        len(parts) >= 2
        and low == parts[-2].lower() + ".php"
    ):
        score += 60
        reasons.append("plugin main file")

    # (1) فایل اجرایی
    if low.endswith(".php"):
        score += 20

        if low in REVIEW_PHP_ENTRY_NAMES:
            score += 20
            reasons.append("php entry point")

    # (4) فایل تنظیمات
    if low in REVIEW_CONFIG_FILES:
        score += 30
        reasons.append("config file")

    if low in REVIEW_NOISE_BASENAMES and not signals:
        score -= 40
        reasons.append("boilerplate")

    return score, reasons, signals


def collect_review_priority(
    project_root,
    is_wordpress=False
):
    # خروجی: (shortlist, signals)
    shortlist = []
    signals = []
    seen = set()
    scanned = 0

    if not project_root or not os.path.isdir(project_root):
        return shortlist, signals

    for start in _review_scan_roots(
        project_root, is_wordpress
    ):
        for dir_path, dir_names, file_names in os.walk(start):
            try:
                relative_dir = os.path.relpath(
                    dir_path, project_root
                )
                depth = 0 if relative_dir == "." else (
                    relative_dir.count(os.sep) + 1
                )

            except ValueError:
                continue

            skip = _review_skip_dirs(is_wordpress)

            dir_names[:] = [
                name for name in dir_names
                if name not in skip
            ]

            if depth >= REVIEW_MAX_SCAN_DEPTH:
                dir_names[:] = []

            for name in sorted(file_names):
                scanned += 1

                if scanned > REVIEW_MAX_SCAN_FILES:
                    return _finalize_review(shortlist, signals)

                full_path = os.path.join(dir_path, name)

                if not is_inside_project(project_root, full_path):
                    continue

                try:
                    relative = relative_display(
                        project_root, full_path
                    )

                except Exception:
                    continue

                if relative in seen:
                    continue

                seen.add(relative)

                if _review_is_sensitive(name):
                    shortlist.append((
                        50, relative,
                        [
                            "sensitive file - never read, "
                            "review manually"
                        ]
                    ))
                    continue

                try:
                    score, reasons, hits = _review_scan_file(
                        full_path, relative
                    )

                except Exception:
                    continue

                if hits:
                    best = max(item[0] for item in hits)
                    signals.append((
                        best, relative, hits
                    ))

                if score > 0 and reasons:
                    shortlist.append((score, relative, reasons))

    return _finalize_review(shortlist, signals)


def _finalize_review(shortlist, signals):
    shortlist.sort(key=lambda item: (-item[0], len(item[1])))

    signals.sort(key=lambda item: (-item[0], item[1]))

    return shortlist[:REVIEW_SHORTLIST_LIMIT], signals[:REVIEW_SIGNAL_LIMIT]


def build_review_priority_text(project_root, is_wordpress=False):
    # متن آماده برای تزریق به prompt حلقه.
    try:
        shortlist, signals = collect_review_priority(
            project_root, is_wordpress
        )

    except Exception:
        return ""

    if not shortlist and not signals:
        return ""

    lines = []

    if signals:
        lines.append(
            "MUST READ FIRST - files with raw anomaly signals. "
            "A signal is NOT a proven problem: open the top file, "
            "read the cited lines and judge the code yourself. "
            "These are evidence, not a verdict, and false "
            "positives are expected."
        )

        for _tier, relative, hits in signals:
            labels = []

            for tier, label, number in hits:
                key = label + "@L" + str(number)

                if key not in labels:
                    labels.append(key)

            lines.append(
                "- " + relative + ": " + ", ".join(labels[:5])
            )

    if shortlist:
        lines.append("")

        if signals:
            lines.append(
                "SECONDARY - structural entry points, only if you "
                "need more context than the files above."
            )
        else:
            lines.append(
                "WHERE TO START - structurally important files "
                "(no signals were found), read the top ones "
                "before answering:"
            )

        for index, (score, relative, reasons) in enumerate(
            shortlist, 1
        ):
            lines.append(
                str(index) + ". " + relative
                + "  (" + ", ".join(reasons) + ")"
            )

    return "\n".join(lines)


def top_priority_signal_path(project_root, is_wordpress=None):
    # مسیری که بیشترین تیک مثبت سیگنال را دارد؛ دروازه
    # «قبل از FINAL خوانده شود» روی همین مسیر بسته می‌شود.
    if is_wordpress is None:
        is_wordpress = is_wordpress_project_root(project_root)

    try:
        shortlist, signals = collect_review_priority(
            project_root, is_wordpress
        )

    except Exception:
        return ""

    if not signals:
        return ""

    return signals[0][1]


def read_paths_already_seen(transcript):
    seen = set()

    for entry in transcript or []:
        if not isinstance(entry, dict):
            continue

        tool = str(entry.get("tool") or "")

        if tool not in ("read_file", "read_files"):
            continue

        args = entry.get("args") or {}

        # aliasهای واقعیِ پذیرفته‌شده در tool_read_file:
        # path / relative_path / file / filename / name
        # (dir برای سازگاری با کدهای قبلی نگه داشته می‌شود.)
        for key in (
            "path", "relative_path", "dir", "file",
            "filename", "name"
        ):
            value = args.get(key)

            if isinstance(value, str) and value:
                seen.add(value.replace("\\", "/").strip("/"))

        # tool_read_files هم «paths» می‌پذیرد هم «files» و هر دو
        # ممکن است رشتهٔ تک‌مسیر یا لیست رشته باشند.
        for key in ("paths", "files"):
            value = args.get(key)

            if isinstance(value, str) and value:
                seen.add(value.replace("\\", "/").strip("/"))

            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item:
                        seen.add(
                            item.replace("\\", "/").strip("/")
                        )

    return seen


def normalize_known_read_paths(known_read_paths):
    # مسیرهای خوانده‌شده‌ای که از چرخهٔ قبلی به این چرخه منتقل
    # می‌شوند، با همان normalizer رسمیِ پروژه یکسان‌سازی
    # می‌شوند تا با سمتِ هدف و با read_paths_already_seen دقیقاً
    # هم‌شکل باشند («./» و «\» و بزرگی/کوچکی حروف یکسان شود).
    normalized = set()

    for value in known_read_paths or []:
        if not isinstance(value, str):
            continue

        item = normalize_relative_target(value)

        if item:
            normalized.add(item)

    return normalized


# حداکثر تعداد مسیرهای شواهدِ خوانده‌نشده که در یک یادآوریِ
# واحد به مدل نشان داده می‌شوند. این سقف فقط اندازهٔ همان
# یادآوری را کوتاه نگه می‌دارد؛ خودِ دروازه در هر بار FINAL از
# نو محاسبه می‌شود و به یک فایل ثابتِ از پیش تعیین‌شده وابسته
# نیست.
MAX_EVIDENCE_PATHS_PER_NUDGE = 4


def _evidence_path_matches_request(normalized_path, mentioned):
    # ارتباط «روشن» یعنی نام فایلی که در متن درخواست آمده با
    # مسیر اولویت‌دار یکی باشد: همان مسیر دقیق، یا پسوندِ مسیر
    # وقتی درخواست مسیر نسبی داده، یا basename وقتی درخواست
    # فقط نام فایل را بدون مسیر برده است.
    for candidate in mentioned:
        if not candidate:
            continue

        if normalized_path == candidate:
            return True

        if "/" in candidate:
            if normalized_path.endswith("/" + candidate):
                return True

        elif normalized_path.rsplit("/", 1)[-1] == candidate:
            return True

    return False


def pending_evidence_paths(
    project_root, transcript, known_read_paths=None, is_wordpress=None,
    user_request=None
):
    # مسیرهای شواهدی که هنوز واقعاً خوانده نشده‌اند، به ترتیب
    # اولویت: اول فایل‌های دارای سیگنال ناهنجاری، بعد فایل‌های
    # ساختاریِ کوتاه‌شده.
    #
    # این لیست در هر بار پذیرش FINAL دوباره ساخته می‌شود تا پس
    # از بررسیِ یک مسیر، مسیر مرتبطِ بعدی انتخاب شود؛ نه اینکه
    # کل حلقه روی یک فایل ثابت قفل بماند.
    #
    # قاعده کاملاً عمومی است: نه نام پروژه، نه مسیر خاص و نه نوع
    # درخواست در آن ثابت است و هیچ ابزار یا بررسی خاصی را برای
    # همهٔ درخواست‌ها اجباری نمی‌کند.
    #
    # الزام فقط برای مسیرهایی است که ارتباط روشن با متن درخواست
    # داشته باشند (نام فایلِ صریح). shortlist ساختاری صرفاً به
    # خاطر خوانده‌نشدن، FINAL را معطل یک گام اضافه نمی‌کند.
    if not project_root:
        return []

    # نام فایل‌های صریحِ درخواست؛ بدون آن هیچ مسیری الزام‌آور
    # نیست و اسکن پروژه هم لازم نیست.
    mentioned = [
        normalize_relative_target(item)
        for item in extract_file_candidates(user_request)
    ]
    mentioned = [item for item in mentioned if item]

    if not mentioned:
        return []

    try:
        if is_wordpress is None:
            is_wordpress = is_wordpress_project_root(project_root)

        shortlist, signals = collect_review_priority(
            project_root, is_wordpress
        )

    except Exception:
        return []

    already_read = {
        normalize_relative_target(item)
        for item in read_paths_already_seen(transcript)
    }
    already_read |= normalize_known_read_paths(known_read_paths)

    pending = []

    for _score, relative, _reasons in list(signals) + list(shortlist):
        # فایل حساس هرگز در نقشهٔ شواهد نمی‌آید تا دروازه، مدل
        # را به خواندن رمز/کلید تشویق نکند.
        if _review_is_sensitive(os.path.basename(relative)):
            continue

        normalized = normalize_relative_target(relative)

        if not normalized or normalized in already_read:
            continue

        if not _evidence_path_matches_request(
            normalized, mentioned
        ):
            continue

        already_read.add(normalized)
        pending.append(relative)

        if len(pending) >= MAX_EVIDENCE_PATHS_PER_NUDGE:
            break

    return pending


def successful_read_paths(transcript, known_read_paths=None):
    # مسیرهای نرمال‌شده‌ای که واقعاً با read موفق خوانده شده‌اند؛
    # هم از همین transcript و هم از سابقهٔ منتقل‌شدهٔ همین
    # درخواست. خواندنِ ناموفق (فایل ناموجود یا حساس) هرگز شمرده
    # نمی‌شود.
    paths = set(normalize_known_read_paths(known_read_paths))

    for entry in transcript or []:
        if not isinstance(entry, dict):
            continue

        if str(entry.get("tool") or "") not in (
            "read_file", "read_files"
        ):
            continue

        values = read_paths_already_seen([entry])

        if not values:
            continue

        result = entry.get("result")

        if isinstance(result, dict) and result.get("ok") is False:
            continue

        for value in values:
            normalized = normalize_relative_target(value)

            if normalized:
                paths.add(normalized)

    return paths


def content_evidence_status(read_paths, user_request):
    # وضعیت شواهد محتوا برای درخواست فقط‌خواندنی/تحلیلی:
    #
    # "ok"           → دست‌کم یک خواندنِ موفق با هدف درخواست
    #                  (نام فایلِ صریح) ارتباط روشن دارد.
    # "missing"      → هیچ خواندن موفقی نیست یا خوانده‌شده‌ها به
    #                  هدف درخواست ربط روشنی ندارند؛ یادآوریِ
    #                  موجود (سقف‌دار) فعال می‌شود.
    # "unverifiable" → درخواست نام فایل ندارد و ارتباط شواهد با
    #                  هدف قابل راستی‌آزمایی نیست؛ فقط محدودیت
    #                  صریح در گزارش می‌آید، بدون اجبار به خواندنِ
    #                  فایل نامرتبط.
    # "blocked"      → همهٔ نام‌های صریحِ درخواست حساس‌اند و
    #                  ابزار می‌پذیرد نخواند؛ محدودیت امنیتی
    #                  اعلام می‌شود، نه کوتاهی مدل.
    #
    # فهرست فایل‌ها، درخت پروژه یا نمای کلیِ عمومی هم شواهد
    # محتوا نیست و به‌تنهایی درخواستِ فقط‌خواندنی را کامل نمی‌کند.
    mentioned = []

    for item in extract_file_candidates(user_request):
        normalized = normalize_relative_target(item)

        if normalized and normalized not in mentioned:
            mentioned.append(normalized)

    if mentioned and all(
        is_sensitive_read_target(item) for item in mentioned
    ):
        return "blocked"

    if not read_paths:
        return "missing"

    if not mentioned:
        return "unverifiable"

    for path in read_paths:
        if _evidence_path_matches_request(path, mentioned):
            return "ok"

    return "missing"


def ensure_edit_file_proposal_is_grounded(
    project_root, args, transcript, known_read_paths=None
):
    # پیش‌شرط ساخت Plan برای edit_file (خواندن + محتوای واقعی).
    #
    # ریشه‌ی خطای «Plan با محتوای فرضی»: edit_file به یک رشتهٔ
    # `old` دقیق نیاز دارد، ولی هیچ‌جا اجباری نبود که مدل قبل از
    # پیشنهاد آن، فایل را بخواند. نتیجه: مدل در گام اول بدون
    # هیچ ابزاری یک Plan می‌ساخت و `old` را از خودش اختراع
    # می‌کرد؛ آن Plan به کاربر نشان داده می‌شد و فقط بعد از تأیید
    # (یعنی دیرهنگام) شکست می‌خورد.
    #
    # این تابع دقیقاً همان دو شکست را می‌بندد:
    #   A) فایل هدف باید قبلاً در همین درخواست خوانده شده باشد.
    #   C) رشتهٔ `old` باید واقعاً داخل محتوای فعلی همان فایل باشد.
    #
    # known_read_paths مسیرهایی است که در چرخهٔ قبلیِ همین
    # درخواست (پیش از Approval) واقعاً خوانده شده‌اند و هنگام
    # Resume منتقل می‌شوند. این کار فقط «سابقهٔ خواندن» را حمل
    # می‌کند و invariant را تغییر نمی‌دهد: شرط C همچنان محتوای
    # واقعیِ فعلی فایل را می‌سنجد، پس یک مسیرِ منتقل‌شده هرگز
    # اجازهٔ ویرایشِ بدون مبنا نمی‌دهد.
    #
    # این بررسی فقط برای edit_file و فقط در حالت Write است.
    # در حالت فقط‌خواندنی هیچ تغییری رخ نمی‌دهد، پس مسیر موجود
    # و آزموده‌شدهٔ «فقط بررسی» دست‌نخورده می‌ماند.
    # write_file / write_files محتوای کامل می‌گیرند و به خواندن
    # قبلی نیازی ندارند، و ابزارهای فقط‌خواندنی اصلاً به این مسیر
    # نمی‌رسند.
    path = tool_arg(args, "path", "relative_path", "file", "dir")

    if not path:
        return

    normalized = normalize_relative_target(path)

    if not normalized:
        return

    if normalized not in (
        {
            normalize_relative_target(item)
            for item in read_paths_already_seen(transcript)
        }
        | normalize_known_read_paths(known_read_paths)
    ):
        raise ValueError(
            "قبل از پیشنهاد ویرایش روی «" + str(path) + "»، باید "
            "همین فایل را با read_file بخوانی. بدون محتوای واقعی "
            "فایل، رشتهٔ old قابل ساختن نیست و Plan ساخته نشد."
        )

    old_text = tool_arg(args, "old", "old_text", "find", "search")

    if not isinstance(old_text, str) or not old_text:
        return

    try:
        content = read_file(project_root, path)

    except Exception as e:
        raise ValueError(
            "محتوای «" + str(path) + "» برای بررسی old خوانده نشد "
            "(" + str(e) + ")؛ Plan ساخته نشد."
        )

    if old_text not in content:
        raise ValueError(
            "رشتهٔ old در محتوای واقعی فایل «" + str(path) + "» "
            "وجود ندارد؛ این متن حدسی است و Plan ساخته نشد. "
            "فایل را با read_file بخوان و old را عیناً از محتوای "
            "واقعی کپی کن."
        )


def is_wordpress_project_root(project_root):
    # تشخیص وردپرس فقط از روی ساختار دیسک، مستقل از تحلیل‌گر.
    if not project_root or not os.path.isdir(project_root):
        return False

    try:
        names = set(os.listdir(project_root))

    except OSError:
        return False

    if "wp-content" in names and (
        "wp-includes" in names or "wp-admin" in names
    ):
        return True

    return "wp-config.php" in names


PROJECT_INVENTORY_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules",
    ".idea", ".vscode", ".next", ".nuxt", "dist", "build",
    "vendor", ".venv", "venv", ".cache", ".pytest_cache",
}

PROJECT_INVENTORY_SKIP_SUFFIXES = (
    ".agent-backup",
)

PROJECT_INVENTORY_BACKUP_MARKER = ".agent-backup"

MAX_PROJECT_INVENTORY_FILES = 60


def is_project_backup_file(name):
    # فایل‌های Backup که خودِ ابزار می‌سازد: «name.agent-backup»
    # و نسخه‌های شماره‌دار آن مثل «name.agent-backup.1».
    #
    # این‌ها پشتوانهٔ تغییرات گذشته‌اند، نه فایل پروژه. اگر وارد
    # Inventory شوند، مدل آن‌ها را هدف تغییر می‌گیرد و کاربر
    # یک Plan اشتباه می‌بیند. این بررسی فقط برای Inventory است و
    # رفتار هیچ مسیر دیگری را تغییر نمی‌دهد.
    text = str(name or "")
    index = text.find(PROJECT_INVENTORY_BACKUP_MARKER)

    if index < 0:
        return False

    tail = text[index + len(PROJECT_INVENTORY_BACKUP_MARKER):]

    if not tail:
        return True

    # فقط نسخهٔ شماره‌دار («.agent-backup.1» یا «.agent-backup.12»)
    # تا نامی مثل «my.agent-backup-guide.txt» اشتباهی حذف نشود.
    return bool(re.fullmatch(r"\.\d+", tail))


def build_project_file_inventory(project_root, max_files=None):
    # فهرست قطعی فایل‌های واقعی پروژه (فقط‌خواندنی).
    #
    # ریشه‌ی خطای «index.php به‌جای index.html»: مدل در
    # Tool Reference چند مثال ثابت می‌بیند (index.php در
    # find_entries/read_file و style.css در edit_file) و وقتی
    # نام فایل در درخواست نیامده، همان مثال‌ها را کپی می‌کند.
    # با دادن نام‌های واقعی پروژه، حدس از بین می‌رود.
    #
    # این فهرست فقط‌خواندنی است، سقف‌دار است و هیچ فایلی را
    # تغییر نمی‌دهد.
    if not project_root or not os.path.isdir(project_root):
        return ""

    if max_files is None:
        max_files = MAX_PROJECT_INVENTORY_FILES

    try:
        max_files = max(1, int(max_files))

    except Exception:
        max_files = MAX_PROJECT_INVENTORY_FILES

    found = []

    for current, dirs, files in os.walk(project_root):
        dirs[:] = sorted(
            d for d in dirs if d not in PROJECT_INVENTORY_SKIP_DIRS
        )

        for name in sorted(files):
            if name.startswith("."):
                continue

            if name.endswith(PROJECT_INVENTORY_SKIP_SUFFIXES):
                continue

            if is_project_backup_file(name):
                continue

            full = os.path.join(current, name)
            relative = os.path.relpath(full, project_root)

            if relative.startswith(".."):
                continue

            found.append(relative.replace("\\", "/"))

            if len(found) >= max_files:
                break

        if len(found) >= max_files:
            break

    if not found:
        return ""

    lines = [
        "REAL FILES IN THIS PROJECT (read from disk - these are the "
        "only paths that exist; anything else is a guess):",
    ]
    lines.extend("- " + item for item in found)

    if len(found) >= max_files:
        lines.append(
            "- (list truncated at " + str(max_files)
            + " entries; use structure/list_directory for more)"
        )

    lines.append(
        "- Use these exact relative paths in ARGS. Never invent a "
        "filename, and never copy a filename from a tool example."
    )

    return "\n".join(lines)


COVERAGE_DOC_EXTENSIONS = (
    ".md", ".markdown", ".rst", ".txt", ".pdf",
)

COVERAGE_STYLESHEET_LINK_PATTERN = re.compile(
    r"""<link\b[^>]*\brel\s*=\s*["']?stylesheet["']?[^>]*>""",
    re.IGNORECASE,
)

COVERAGE_HREF_PATTERN = re.compile(
    r"""href\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)

COVERAGE_IMPORT_PATTERN = re.compile(
    r"""(?:@import\s+|import\s+)(?:url\()?["']([^"']+)["']""",
    re.IGNORECASE,
)

COVERAGE_CLASS_PATTERN = re.compile(
    r"""class\s*=\s*["'][^"']*?\b([A-Za-z_][\w\-]*)""",
    re.IGNORECASE,
)


def project_inventory_paths(project_root):
    # مسیرهای واقعی Inventory، به‌صورت فهرست ساده.
    # فقط ورودی‌هایی که واقعاً روی دیسک هستند نگه داشته
    # می‌شوند تا جمله‌های راهنمای انتهای Inventory (که با «- »
    # شروع می‌شوند) به‌اشتباه مسیر شمرده نشوند.
    text = build_project_file_inventory(project_root)

    paths = []

    for line in (text or "").splitlines():
        item = line.strip()

        if not item.startswith("- "):
            continue

        value = item[2:].strip()

        if not value or value.startswith("("):
            continue

        if not os.path.isfile(os.path.join(project_root, value)):
            continue

        if value not in paths:
            paths.append(value)

    return paths


def related_project_files(project_root, user_request):
    # فایل‌های «احتمالاً مرتبط» با یک درخواست طبیعی.
    #
    # ریشه‌ی مشکل «style.css هرگز وارد Plan نمی‌شود»:
    # find_requested_files فقط با یک توکن «name.ext» کار می‌کند،
    # پس درخواستی مثل «عنوان h1 آبی باشد و پاراگراف با کلاس .text
    # خاکستری باشد» هیچ فایلی تولید نمی‌کرد و در نتیجه هیچ
    # Coverageای هم وجود نداشت.
    #
    # این تابع کمکی و فقط‌خواندنی است:
    #   * فقط وقتی درخواست هیچ نام فایل صریحی ندارد فعال می‌شود،
    #     پس رفتار درخواست‌های تک‌فایلیِ فعلی کاملاً حفظ می‌شود.
    #   * سیگنال اصلی، «گراف ارجاع» است: اگر یک فایلِ مرتبط،
    #     فایل دیگری از پروژه را بارگذاری کند (مثلاً <link
    #     rel="stylesheet" href="style.css"> یا @import) آن فایل
    #     دوم هم مرتبط است.
    #   * تطبیق selector/class فقط سیگنال کمکی است.
    #   * فایل‌های مستندات (README و مشابه) هرگز صرفاً به خاطر
    #     وجود کلمه‌ای مثل «html» نامزد نمی‌شوند.
    #   * اگر اطمینان کافی نباشد، خروجی خالی است.
    #
    # این تابع هرگز Mutation انجام نمی‌دهد، Plan نمی‌سازد و
    # فقط پیشنهاد candidate می‌دهد.
    if not project_root or not os.path.isdir(project_root):
        return []

    # درخواستی که خودش نام فایل داده، مسیر قطعیِ خودش را دارد.
    try:
        if extract_file_candidates(user_request):
            return []

    except Exception:
        return []

    paths = project_inventory_paths(project_root)

    if len(paths) < 2:
        return []

    request_text = normalize_request_text(user_request)
    by_name = {}

    for path in paths:
        by_name[path.replace("\\", "/").strip("/").lower()] = path

    contents = {}

    for path in paths:
        full = os.path.join(project_root, path)

        if not os.path.isfile(full):
            continue

        try:
            with open(full, "r", encoding="utf-8") as fh:
                contents[path] = fh.read()

        except (OSError, UnicodeDecodeError):
            continue

    if not contents:
        return []

    # نامزدهای اولیه: فایل‌هایی که selector یا کلاسِ نام‌بردهٔ
    # درخواست را واقعاً در محتوای خود دارند.
    seeds = set()

    for path, content in contents.items():
        lower = path.lower()

        if lower.endswith(COVERAGE_DOC_EXTENSIONS):
            # مستندات نامزد نمی‌شوند.
            continue

        body = content.lower()

        if "." in request_text:
            for match in COVERAGE_CLASS_PATTERN.finditer(content):
                token = match.group(1).lower()

                if token and token in request_text:
                    seeds.add(path)
                    break

        for match in re.finditer(r"[`\"']([.#]?[A-Za-z_][\w\-]*)[`\"']",
                                 user_request or ""):
            token = match.group(1).lower()
            bare = token.lstrip(".")

            if not bare or len(bare) < 2:
                continue

            if bare in body:
                seeds.add(path)
                break

    if not seeds:
        return []

    # گراف ارجاع: هر فایلی که یکی از seedها آن را در
    # <link rel="stylesheet"> یا @import/import صدا می‌زند.
    related = set(seeds)

    for path in list(seeds):
        content = contents.get(path) or ""
        targets = []

        if COVERAGE_STYLESHEET_LINK_PATTERN.search(content):
            targets.extend(COVERAGE_HREF_PATTERN.findall(content))

        targets.extend(COVERAGE_IMPORT_PATTERN.findall(content))

        for raw in targets:
            token = str(raw or "").split("?")[0].split("#")[0].strip()
            token = token.replace("\\", "/").lstrip("./")
            target = by_name.get(token.lower())

            if target and target in contents:
                related.add(target)

    # بیش از دو نامزد یعنی اطمینان پایین است؛ چیزی پیشنهاد
    # نمی‌شود تا مدل بین گزینه‌های نامربوط سرگردان نشود.
    if len(related) < 2 or len(related) > 2:
        return []

    return sorted(related)


def uncovered_coverage_files(
    project_root, user_request, tool_name, args, transcript,
    known_read_paths=None
):
    # فایل‌های مرتبطی که Mutation پیشنهادی پوشش نداده است.
    #
    # کاملاً فقط‌خواندنی و کاملاً اختیاری: اگر coverage معتبری
    # نباشد، خروجی خالی است و هیچ اتفاقی نمی‌افتد. این تابع
    # هیچ تصمیمی نمی‌گیرد و فقط فهرست پیشنهاد می‌دهد؛ تصمیم‌گیری
    # و ساخت Plan همچنان دست مدل می‌ماند.
    if tool_name not in (
        "write_file", "write_files", "edit_file"
    ):
        return []

    try:
        related = related_project_files(project_root, user_request)

    except Exception:
        return []

    if len(related) < 2:
        return []

    if tool_name == "write_files":
        try:
            covered = {
                normalize_relative_target(item.get("path"))
                for item in normalize_batch_files(args)
            }
        except Exception:
            return []

    else:
        path = tool_arg(args, "path", "relative_path", "file", "dir")

        if not path:
            return []

        covered = {normalize_relative_target(path)}

    return [
        item for item in related
        if normalize_relative_target(item) not in covered
    ]


def build_loop_project_facts(project_root):
    # Phase H: برگه واقعیت‌های پروژه، به‌صورت قطعی و فقط‌خواندنی.
    #
    # دلیل معماری: مدل 7B در درخت بزرگ و ناآشنا، نام قالب،
    # نام افزونه یا مسیر درست را حدس می‌زد و حتی مسیر
    # ساختگی می‌ساخت. این اطلاعات نباید به قضاوت مدل واگذار
    # شود.
    #
    # نکته مهم: عمداً فقط بخش‌های «واقعیت» گزارش تحلیل‌گر
    # استفاده می‌شود و بخش issues/recommendations حذف
    # می‌شود؛ چون در تست قبلی همان چند جمله، کل گزارش را به
    # «robots.txt و sitemap نداریم» قلاب کرده بود.
    # «قالب واقعیت» و «اولویت بررسی» دو لایه مستقل هستند:
    # اگر تحلیل‌گر در دسترس نبود، لایه اولویت‌بندی همچنان
    # باید کار کند.
    if not project_root:
        return ""

    priority_text = build_review_priority_text(
        project_root,
        is_wordpress_project_root(project_root)
    )

    if _phase_g_analyze is None:
        return priority_text

    try:
        data = _phase_g_analyze(project_root)

    except Exception:
        data = None

    if not isinstance(data, dict):
        return priority_text

    wp = data.get("wordpress") or {}
    struct = data.get("structure") or {}
    themes = data.get("themes") or {}
    plugins = data.get("plugins") or {}
    sens = data.get("sensitive_files") or {}

    lines = [
        "PROJECT FACTS (already read from disk by the agent - "
        "these are verified facts, not guesses):",
        "- CMS: " + str(data.get("cms", "unknown")),
    ]

    for key in ("wp-content", "wp-admin", "wp-includes"):
        lines.append(
            "- " + key + ": " + str((wp.get("signals") or {}).get(key))
        )

    cfg = struct.get("wp-config.php")

    if isinstance(cfg, dict):
        lines.append(
            "- wp-config.php present: "
            + str(cfg.get("exists"))
            + " (content is sensitive and is never read)"
        )

    theme_items = themes.get("items") or []

    if theme_items:
        names = [
            str(item.get("name"))
            for item in theme_items[:10]
            if item.get("name")
        ]
        lines.append("- Themes installed: " + ", ".join(names))

    else:
        lines.append("- Themes installed: none found")

    plugin_items = plugins.get("items") or []

    if plugin_items:
        names = [
            str(item.get("name"))
            for item in plugin_items[:25]
            if item.get("name")
        ]
        lines.append("- Plugins installed: " + ", ".join(names))

    else:
        lines.append("- Plugins installed: none found")

    env = sens.get(".env")

    if isinstance(env, dict):
        lines.append(
            "- .env present: " + str(env.get("exists"))
            + " (content is sensitive and is never read)"
        )

    if not any(
        str(item.get("name"))
        for item in (sens.get("extra_key_pem_files") or [])
    ):
        lines.append("- *.key/*.pem files at root: none")

    lines.append(
        "- Use these exact names in your ARGS. Never invent a "
        "theme, plugin or folder name."
    )

    if priority_text:
        lines.append("")
        lines.append(priority_text)

    return "\n".join(lines)


def emit_progress(progress, event, **data):
    # گزارش مرحله‌ای اختیاری. progress=None یعنی رفتار
    # دقیقاً مثل قبل؛ خطای callback هرگز اجرای Agent را
    # نمی‌شکند و هیچ داده‌ای را تغییر نمی‌دهد.
    if progress is None:
        return

    try:
        progress(event, data)

    except Exception:
        pass


def run_loop_steps(
    user_request,
    project_root,
    project_name,
    allow_write,
    transcript,
    observations,
    steps_used,
    correction_attempts,
    max_steps=MAX_AGENT_STEPS,
    stop_event=None,
    progress=None,
    known_request_status=None,
    known_read_paths=None,
    allowed_tools=None
):
    # هسته حلقه:
    # مدل یک گام را انتخاب میکند → Agent همان ابزار را
    # اجرا میکند → نتیجه بهعنوان Observation برمیگردد →
    # گام بعدی تا سقف تعیینشده.
    #
    # known_request_status: وضعیت واقعی فایل‌های درخواستی که
    # در چرخه‌های Approval قبلی ثبت شده و اینجا به حلقه باز
    # گشته است. بدون آن، هر Resume یک transcript تازه می‌سازد
    # و تغییرات موفقِ قبلی از گزارش ناپدید می‌شوند.
    #
    # known_read_paths: مسیرهایی که در چرخهٔ قبلیِ همین درخواست
    # واقعاً با read_file / read_files خوانده شده‌اند. چون هر
    # Resume یک transcript تازه می‌سازد، بدون این کلید Readهای
    # واقعیِ همان درخواست فراموش می‌شدند و ویرایشِ درستِ فایل
    # دوم (مثل style.css) به‌اشتباه رد می‌شد.
    system_prompt = build_loop_system_prompt(allowed_tools)
    rejections = []
    duplicate_blocks = 0
    evidence_nudges = 0
    # شمارندهٔ «کار خواسته‌شده هنوز کامل نیست». مثل
    # evidence_nudges سقف کوتاه دارد تا حلقه هرگز در
    # چرخهٔ بی‌پایان گیر نکند.
    completion_nudges = 0
    # شمارنده‌ی یادآوری «پوشش چندفایلی». advisory است و سقف
    # یک‌باره دارد تا حلقه هرگز به‌خاطر آن قفل نشود.
    coverage_nudges = 0
    # شمارنده‌ی خطای «نام ابزار نامعتبر». این خطا گام مصرف
    # نمی‌کند، پس برای جلوگیری از چرخش بی‌نهایت سقف جداگانه
    # و کوچک دارد.
    invalid_tool_retries = 0

    while steps_used < max_steps:
        # Phase C: Emergency Stop قبل از شروع Step بعدی.
        # در این حالت هیچ مدل/ابزار/Mutation جدیدی اجرا
        # نمیشود و خروج کنترل‌شده انجام میشود.
        if is_stop_requested(stop_event):
            return build_loop_report(
                project_name,
                project_root,
                transcript,
                notes=[
                    EMERGENCY_STOP_MESSAGE,
                    "گام جدیدی اجرا نشد؛ تعداد گام‌های "
                    "اجراشده تا این لحظه: "
                    + str(len(transcript)),
                ]
            )

        steps_used += 1

        # سقف گام اصلاحی تمام شده و آخرین Mutation پیش از
        # اعمال تغییر شکست خورده است: همان‌جا متوقف می‌شویم.
        # این بررسی هر دو حالت را می‌گیرد — هم Mutation داخل
        # حلقه و هم Mutation تأییدشده‌ای که در
        # execute_pending_action اجرا و اینجا به حلقه بازگشته
        # است — و پیش از هر مدل/ابزار/Mutation جدید اجرا
        # می‌شود.
        if (
            correction_attempts >= MAX_CORRECTION_STEPS
            and is_failed_mutation_before_apply(
                last_mutating_entry(transcript)
            )
        ):
            return build_loop_report(
                project_name,
                project_root,
                transcript,
                notes=[
                    "Mutation پیش از اعمال تغییر روی دیسک شکست "
                    "خورد و سقف گام اصلاحی ("
                    + str(MAX_CORRECTION_STEPS)
                    + ") پر شده است؛ اجرا متوقف شد."
                ]
            )

        emit_progress(
            progress,
            "step_start",
            step=steps_used,
            max_steps=max_steps,
            tools_used=len(transcript)
        )

        prompt = build_loop_user_prompt(
            user_request,
            project_name,
            allow_write,
            transcript,
            observations,
            correction_attempts,
            last_step=(steps_used >= max_steps),
            project_root=project_root
        )

        try:
            model_output = call_model(
                system_prompt,
                prompt,
                stop_event=stop_event,
                n_predict=LOOP_MODEL_N_PREDICT,
                no_think=True
            )

        except Exception as e:

            if not transcript:
                # مدل در دسترس نیست → مسیرهای قبلی اجرا شوند.
                return None

            return build_loop_report(
                project_name,
                project_root,
                transcript,
                notes=["مدل پاسخ نداد: " + str(e)],
                user_request=user_request
            )

        decision = parse_agent_decision(model_output) or {}
        tool_name = decision.get("tool")
        last_step = steps_used >= max_steps

        emit_progress(
            progress,
            "decision",
            step=steps_used,
            tool=tool_name,
            final=bool(decision.get("final"))
        )

        if not tool_name:
            # دروازهٔ شواهد (عمومی): پیش از پذیرش FINAL کفایت
            # شواهد برای «همین درخواست» سنجیده می‌شود، نه فقط برای
            # یک فایل ثابتِ از پیش تعیین‌شده. در تست واقعی مدل بعد
            # از ۲ گام یک گزارش بی‌ربط نوشت، در حالی که هرگز فایل
            # مشکوک را نخوانده بود.
            #
            # مسیرها در همین لحظه از نو ساخته می‌شوند؛ بنابراین
            # پس از بررسیِ یک مسیر، مسیر مرتبطِ بعدی انتخاب می‌شود
            # و فایل حساس هرگز در این لیست نمی‌آید. فقط مسیرِ
            # دارای ارتباط روشن با متن درخواست الزام‌آور است.
            missing_paths = pending_evidence_paths(
                project_root, transcript, known_read_paths,
                user_request=user_request
            )

            # معیار شواهد متناسب با نوع درخواست: در حالت
            # فقط‌خواندنی، صرف موفقیتِ یک read_file کافی نیست؛
            # مسیرهای خوانده‌شده باید با هدف درخواست (نام فایل‌های
            # صریح) ارتباط روشن داشته باشند. اگر درخواست نام فایل
            # نداشته باشد، ارتباط قابل راستی‌آزمایی نیست (محدودیت
            # صریح در گزارش) و اگر نام‌ها همه حساس باشند، محدودیت
            # امنیتی اعلام می‌شود؛ در هیچ حالتی به خواندنِ فایل
            # نامرتبط اجبار نمی‌شوود. در حالت تغییر، گیت تکمیلِ
            # پایین‌تر معیار پایان است.
            content_reads = set()
            content_status = "ok"

            if (
                not is_write_allowed(allow_write)
                and bool(project_root)
                and os.path.isdir(project_root)
            ):
                content_reads = successful_read_paths(
                    transcript, known_read_paths
                )
                content_status = content_evidence_status(
                    content_reads, user_request
                )

            missing_content = content_status == "missing"

            if (
                not last_step
                and evidence_nudges < MAX_EVIDENCE_NUDGES
                and steps_used < max_steps
                and (missing_paths or missing_content)
            ):
                evidence_nudges += 1
                nudge_parts = []

                if missing_paths:
                    nudge_parts.append(
                        "این مسیرهای اولویت‌دار هنوز خوانده "
                        "نشده‌اند: "
                        + "، ".join(missing_paths)
                        + ". اول read_file روی اولین مسیر بزن و "
                        "اگر برای پاسخ لازم بود، مسیرهای بعدی "
                        "را هم بررسی کن."
                    )

                if missing_content:
                    if content_reads:
                        nudge_parts.append(
                            "این مسیرها خوانده شده‌اند ولی با هدف "
                            "درخواست ارتباط روشنی ندارند: "
                            + "، ".join(
                                sorted(content_reads)[
                                    :MAX_EVIDENCE_PATHS_PER_NUDGE
                                ]
                            )
                            + ". اکنون محتوای یک فایل واقعاً "
                            "مرتبط با درخواست را با read_file "
                            "بخوان و چیزی را که فقط از محتوا "
                            "قابل فهم است در FINAL بیاور."
                        )
                    else:
                        nudge_parts.append(
                            "هنوز محتوای هیچ فایل واقعیِ پروژه را "
                            "نخوانده‌ای؛ فهرست فایل‌ها یا نمای کلی "
                            "پروژه به‌تنهایی شواهد کافی نیست. با "
                            "read_file محتوای یک فایل مرتبط با "
                            "درخواست را بخوان و چیزی را که فقط از "
                            "محتوا قابل فهم است در FINAL بیاور."
                        )

                    mentioned_now = extract_file_candidates(
                        user_request
                    )

                    if mentioned_now:
                        nudge_parts.append(
                            "فایل‌های نام‌برده در درخواست: "
                            + "، ".join(
                                mentioned_now[
                                    :MAX_EVIDENCE_PATHS_PER_NUDGE
                                ]
                            )
                            + " را حتماً بخوان."
                        )

                observations.append(
                    "شواهد کافی جمع نشده است: "
                    + " ".join(nudge_parts)
                    + " اگر پس از خواندن مطمئن شدی مثبت کاذب "
                    "است یا ابزار/دسترسی لازم را نداری، در FINAL "
                    "صریح بگو چرا."
                )
                continue

            # اگر ادامه ممکن نیست (سقف یادآوری یا سقف گام پر
            # شده) ولی شواهد همچنان ناکافی است، پذیرش FINAL باید
            # شفاف گزارش شود؛ وگرنه گزارش دربارهٔ کامل بودن
            # درخواست سکوت می‌کند و کاربر چیزی از محدودیت نمی‌بیند.
            evidence_notes = []

            if missing_paths:
                evidence_notes.append(
                    "محدودیت شواهد: این مسیرهای اولویت‌دار تا "
                    "پایان گام‌ها خوانده نشدند: "
                    + "، ".join(missing_paths)
                    + "."
                )

            if missing_content:
                if content_reads:
                    evidence_notes.append(
                        "محدودیت شواهد: این مسیرها در اجرای فعلی "
                        "خوانده شده ولی با هدف درخواست ارتباط "
                        "روشنی ندارند: "
                        + "، ".join(
                            sorted(content_reads)[
                                :MAX_EVIDENCE_PATHS_PER_NUDGE
                            ]
                        )
                        + "؛ ادعاهای بررسی فراتر از این شواهد "
                        "نیست."
                    )
                else:
                    evidence_notes.append(
                        "محدودیت شواهد: محتوای هیچ فایل واقعیِ "
                        "پروژه خوانده نشد؛ نتیجه فقط بر پایهٔ فهرست "
                        "یا نمای کلی است و قطعی نیست."
                    )

            if content_status == "unverifiable":
                evidence_notes.append(
                    "محدودیت شواهد: درخواست نام فایل مشخصی ندارد "
                    "و ارتباط شواهد خوانده‌شده با هدف درخواست "
                    "قابل راستی‌آزمایی نیست؛ این گزارش فقط "
                    "فایل‌های واقعیِ خوانده‌شده را فهرست می‌کند و "
                    "متن پاسخ نهایی، متن مدل است."
                )

            if content_status == "blocked":
                evidence_notes.append(
                    "محدودیت امنیتی: نام فایل‌های نام‌برده در "
                    "درخواست در فهرست ممنوعِ خواندن است؛ بدون "
                    "خواندنِ این فایل‌ها، ادعای بررسی آن‌ها "
                    "تأییدنشده محسوب می‌شود."
                )

            # دروازهٔ تکمیل کار: برای یک درخواست تغییردهنده،
            # «کافی بودن شواهد» معیار پایان نیست؛ معیار، اعمال
            # و Verify شدنِ همهٔ فایل‌های نام‌برده در درخواست
            # است. بدون این قاعده حلقه بعد از اولین Mutation
            # موفق با یک read_file به FINAL می‌رفت و گزارش،
            # انجام‌شدنِ کل کار را ادعا می‌کرد.
            if (
                not last_step
                and is_write_allowed(allow_write)
                and completion_nudges < MAX_COMPLETION_NUDGES
                and steps_used < max_steps
            ):
                remaining = pending_mutation_targets(
                    project_root, user_request, transcript,
                    known_request_status
                )

                if remaining:
                    completion_nudges += 1
                    observations.append(
                        "کار درخواست هنوز کامل نیست: فایل‌های "
                        + "، ".join(remaining)
                        + " در درخواست تغییر و Verify نشده‌اند. "
                        "قبل از FINAL، تغییر خواسته‌شده را روی "
                        "همین فایل‌ها اعمال کن (از همان مسیر "
                        "تأیید و اعتبارسنجی عبور می‌کند) و "
                        "نتیجه را Verify کن."
                    )
                    continue

            # Phase 3 — Completion Gate (اتصال حداقلی).
            #
            # همین‌جا، درست پیش از پذیرش FINAL: شواهد واقعیِ
            # transcript و الزام‌های قطعیِ فایلیِ استخراجشده از
            # درخواست به evaluate_completion() می‌روند. ادعای
            # FINAL فقط final_claim است و هرگز شاهد تکمیل نیست.
            #
            # Gate فقط گزارش‌رسانی می‌کند و حداکثر یک یادآوری با
            # همان بودجهٔ completion_nudges می‌دهد؛ سقف گام و هیچ
            # مسیر Approval/Undo/Journal تغییری نمی‌کند. خطای Gate
            # هم حلقه را نمی‌شکند (evaluate_completion_gate → None).
            gate_blocking = False
            gate = None

            if is_write_allowed(allow_write):
                gate = evaluate_completion_gate(
                    project_root,
                    user_request,
                    transcript,
                    known_request_status=known_request_status,
                    final_claim=bool(decision.get("final")),
                )

            if isinstance(gate, dict):
                gate_status = gate.get("status")
                gate_states = gate.get("requirement_states") or {}
                gate_pending = sorted(
                    req_id for req_id, state in gate_states.items()
                    if state != "verified"
                )

                # incomplete: فقط وقتی الزام مشخص و قابل‌اقدامی هست و
                # بودجه اجازه می‌دهد، یک تلاش دیگر. blocked دیگر
                # ادامه نمی‌دهد (بی‌فایده) و unverifiable هرگز
                # complete نمی‌شود و حلقه‌ای تازه نمی‌سازد.
                if (
                    gate_status == "incomplete"
                    and gate_pending
                    and not last_step
                    and completion_nudges < MAX_COMPLETION_NUDGES
                    and steps_used < max_steps
                ):
                    completion_nudges += 1
                    observations.append(
                        "نتیجه دروازه تکمیل: این الزام‌های فایلی با "
                        "شاهدهای واقعیِ فعلی تأیید نشده‌اند: "
                        + "، ".join(gate_pending)
                        + ". هر کدام را با همان مسیر تأیید و Verify "
                        "انجام بده؛ ادعای FINAL به‌تنهایی کافی نیست."
                    )
                    continue

                if gate_status == "incomplete" and gate_pending:
                    gate_blocking = True
                    evidence_notes.append(
                        "نتیجه دروازه تکمیل: این الزام‌های فایلی تا "
                        "پایان گام‌ها تأییدنشده ماندند: "
                        + "، ".join(gate_pending)
                        + ". تکمیل کل درخواست بدون شاهد این موارد "
                        "تأییدنشده است."
                    )

                elif gate_status == "blocked":
                    gate_blocking = True
                    evidence_notes.append(
                        "نتیجه دروازه تکمیل: حداقل یک الزام فایلی به "
                        "دلیل محدودیت قطعی قابل ادامه نیست؛ ادامه "
                        "بی‌فایده است و این محدودیت در پذیرش FINAL "
                        "لحاظ شد."
                    )

                elif gate_status == "unverifiable":
                    gate_blocking = True
                    evidence_notes.append(
                        "نتیجه دروازه تکمیل: شواهد یا الزام‌های این "
                        "درخواست برای نتیجه‌گیری قطعی کافی نیست؛ "
                        "تکمیل کار تأییدنشده محسوب می‌شود."
                    )

                # complete: هیچ دخالتی؛ FINAL مثل قبل پذیرفته می‌شود.
                # Gate هرگز ادعای تکمیل تولید نمی‌کند، پس «کامل فرض
                # کردن» درخواست‌های دارای الزام‌های غیرفایلیِ
                # استخراج‌نشده هم ممکن نیست.

            # اگر گیت مسدود/ناقص/تأییدنشده باشد و روش ادامه‌نشود،
            # ادعای مدل به عنوان موفقیت پذیرفته نشود.
            if gate_blocking:
                return build_loop_report(
                    project_name,
                    project_root,
                    transcript,
                    final_answer="",
                    notes=evidence_notes or None,
                    user_request=(
                        user_request
                        if is_write_allowed(allow_write)
                        else None
                    ),
                    known_request_status=known_request_status
                )

            return build_loop_report(
                project_name,
                project_root,
                transcript,
                final_answer=decision.get("final") or "",
                notes=evidence_notes or None,
                user_request=(
                    user_request
                    if is_write_allowed(allow_write)
                    else None
                ),
                known_request_status=known_request_status
            )

        if allowed_tools is not None and tool_name not in allowed_tools:
            observation = "ابزار غیرمجاز در این حالت: " + str(tool_name)
            transcript.append({"step": steps_used + 1, "tool": tool_name, "args": {}, "result": observation})
            steps_used += 1
            continue

        spec = TOOL_REGISTRY.get(tool_name)

        if spec is None:
            # ابزار نامعتبر یک «پیشرفت» نیست، پس نباید بودجه‌ی
            # گام را مصرف کند. این گام هرگز هیچ ابزاری را اجرا
            # نکرده و هیچ اطلاعات تازه‌ای تولید نکرده است.
            #
            # سقف کوتاهی هم نگه داشته می‌شود تا حلقه در برابر
            # مدلی که بی‌نهایت نام غلط می‌فرستد بی‌نهایت نچرخد.
            invalid_tool_retries += 1

            observation = (
                "نام ابزار نامعتبر است: «"
                + str(tool_name)
                + "». چنین ابزاری وجود ندارد و هیچ چیز اجرا "
                "نشد. نام‌های مجاز: "
                + ", ".join(TOOL_REGISTRY.keys())
                + "."
            )

            suggestion = suggest_tool_name(tool_name)

            if suggestion:
                observation += (
                    " احتمالاً منظورت «"
                    + suggestion
                    + "» بود؛ همین نام را دقیقاً استفاده کن."
                )

            observations.append(observation)

            if invalid_tool_retries > MAX_INVALID_TOOL_RETRIES:
                # مدل پیوسته نام غلط می‌فرستد؛ ادامه فایده‌ای
                # ندارد و فقط بودجه را می‌بلعد.
                return build_loop_report(
                    project_name,
                    project_root,
                    transcript,
                    notes=[
                        "پس از "
                        + str(invalid_tool_retries)
                        + " تلاش با نام ابزار نامعتبر، حلقه متوقف "
                        "شد؛ هیچ ابزاری اجرا نشد.",
                        "نام‌های مجاز: "
                        + ", ".join(TOOL_REGISTRY.keys()),
                    ]
                )

            # گام واقعی مصرف نمی‌شود؛ مدل فرصت اصلاح دارد.
            steps_used -= 1

            continue

        args = decision.get("args") or {}

        if decision.get("args_error"):
            observations.append(
                "ARGS نامعتبر برای "
                + str(tool_name)
                + ": باید JSON معتبر باشد."
            )

            continue

        if not spec["mutating"]:
            # Phase H: تکرار عینیِ یک ابزار فقط‌خواندنی
            # هیچ اطلاعات تازه‌ای نمی‌دهد. نتیجه قبلی همین
            # حالا در OBSERVATIONS است، پس اجرای دوباره فقط
            # بودجه گام را می‌بلعد.
            repeat = prior_successful_same_action(
                tool_name, args, transcript
            )

            if repeat is not None:
                observations.append(
                    "این "
                    + str(tool_name)
                    + " با همین args قبلاً اجرا شده و "
                    + "نتیجه‌اش در OBSERVATIONS موجود است؛ "
                    + "دوباره اجرا نمی‌شود. نتیجه موجود را "
                    + "تحلیل کن، ابزار/مسیر/پرس‌وجوی متفاوتی "
                    + "انتخاب کن، یا FINAL بنویس."
                )
                continue

            if prior_unsuccessful_same_action(
                tool_name, args, transcript
            ) is not None:
                duplicate_blocks += 1

                if duplicate_blocks > 1:
                    # تکرار پشت‌سرهمِ همان Action بی‌نتیجه؛
                    # سقف گام فقط safety net است، اینجا توقف
                    # قطعی است.
                    return build_loop_report(
                        project_name,
                        project_root,
                        transcript,
                        notes=[
                            "تکرار یک Action بی‌نتیجه با همان "
                            "args متوقف شد؛ اطلاعات لازم در "
                            "Report همین گام‌ها موجود است."
                        ]
                    )

                observations.append(
                    "این "
                    + str(tool_name)
                    + " با همان args قبلاً بی‌نتیجه بود و "
                    "دوباره اجرا نمی‌شود. یک query/مسیر "
                    "متفاوت انتخاب کنید یا همین حالا "
                    "FINAL بنویسید."
                )

                continue

        if spec["mutating"]:
            try:
                validate_mutating_args(
                    project_root, tool_name, args,
                    user_request=user_request
                )

            except Exception as e:
                observations.append(
                    "این گام رد شد (آرگومان/مسیر نامعتبر): "
                    + str(e)
                )

                continue

            auto_correction = (
                correction_attempts < MAX_CORRECTION_STEPS
                and is_correction_of_last_failure(
                    tool_name, args, transcript
                )
            )

            # شکست Mutation پیش از اعمال تغییر، سقف اصلاح را
            # مصرف می‌کند اما اجرا را از مسیر عادی رد می‌کند:
            # Plan، Approval، Safety و Re-validation عیناً مثل
            # هر Mutation دیگر اجرا می‌شوند.
            failed_mutation_correction = (
                not auto_correction
                and correction_attempts < MAX_CORRECTION_STEPS
                and is_correction_of_failed_mutation(
                    tool_name, args, transcript
                )
            )

            if failed_mutation_correction:
                correction_attempts += 1
                observations.append(
                    "گام اصلاحی پس از شکست Mutation: همان ابزار و "
                    "همان مسیر قبلی؛ این بار از همان مسیر تأیید، "
                    "اعتبارسنجی و Verify عبور می‌کند."
                )

            if not auto_correction:
                # پیش‌شرط edit_file: فایل باید در همین حلقه
                # خوانده شده باشد و `old` باید در محتوای واقعی
                # آن باشد. بدون این دو، Plan ساخته نمی‌شود تا
                # کاربر متن حدسی مدل را تأیید نکند.
                if tool_name == "edit_file" and is_write_allowed(allow_write):
                    try:
                        ensure_edit_file_proposal_is_grounded(
                            project_root, args, transcript,
                            known_read_paths=known_read_paths
                        )

                    except Exception as e:
                        observations.append(
                            "این گام رد شد (ویرایش بدون مبنای "
                            "واقعی): " + str(e)
                        )

                        continue

                # Nudge پوشش چندفایلی (advisory).
                #
                # ریشه‌ی «style.css هرگز وارد Plan نمی‌شود»: در یک
                # درخواست طبیعی، هیچ نام فایلی نوشته نشده، پس
                # find_requested_files هیچ فایلی برنمی‌گرداند و
                # هیچ Coverageای وجود ندارد. مدل Plan تک‌فایلی
                # می‌سازد و کار تمام‌شده به نظر می‌رسد.
                #
                # این nudge «مشورتی» است، نه hard enforcement: فقط
                # یک‌بار در کل حلقه، فقط وقتی coverage قطعی است، و
                # فقط وقتی Plan همهٔ فایل‌های مرتبط را پوشش نداده
                # باشد. حلقه `continue` می‌کند و مدل فرصت دارد پس
                # از خواندن و ارائهٔ Plan کامل ادامه دهد؛ هرگز
                # اجبار دائمی یا توقف ناگهانی وجود ندارد.
                if (
                    coverage_nudges < MAX_COVERAGE_NUDGES
                    and steps_used < max_steps
                    and is_write_allowed(allow_write)
                ):
                    uncovered = uncovered_coverage_files(
                        project_root, user_request, tool_name,
                        args, transcript, known_read_paths
                    )

                    if uncovered:
                        coverage_nudges += 1
                        observations.append(
                            "پوشش کار ناقص است: فایل "
                            + "، ".join(uncovered)
                            + " هم به این درخواست مربوط است ولی "
                            "در Mutation پیشنهادی نیامده. قبل از "
                            "ساخت Plan این فایل‌ها را با read_file "
                            "بخوان، سپس همهٔ فایل‌های لازم را در "
                            "یک Plan کامل بیاور (برای چند فایل از "
                            "write_files استفاده کن تا یک تأیید "
                            "کافی باشد)."
                        )

                        continue

                plan_text = build_pending_plan_text(
                    tool_name, args, project_root,
                    write_mode=allow_write
                )

                if not is_write_allowed(allow_write):
                    # هیچ Pending Action ساخته نمی‌شود، پس عنوان
                    # «نیازمند تأیید» هم نباید نمایش داده شود؛
                    # در غیر این صورت کاربر دستور تأییدی می‌بیند
                    # که Pendingی پشت آن وجود ندارد.
                    return build_loop_report(
                        project_name,
                        project_root,
                        transcript,
                        pending_plan=plan_text,
                        plan_needs_approval=False,
                        notes=[
                            "عملیات تغییردهنده در حالت "
                            "«فقط بررسی» اجرا نمیشود؛ "
                            "هیچ تغییری انجام نشد."
                        ]
                    )

                # Step 1 — Project Approval Mode affects THIS line only.
                #
                # The very same pending action (with its Re-validation
                # context) is built in BOTH modes, so Emergency Stop,
                # safe_path, project-boundary checks, re-validation,
                # backup/undo and the journal behave identically.
                #
                # manual     -> store the pending action and wait for ONE
                #               approval for the whole batch (unchanged).
                # autonomous -> still run Re-validation, but do not ask;
                #               execute immediately.
                autonomous = is_autonomous_project(project_name)

                pending_action = {
                    "tool": tool_name,
                    "args": args,
                    "project": project_name,
                    "project_root": project_root,
                    "user_request": user_request,
                    # سقف گام اصلاحی تا Resume بعد از Approval
                    # حفظ می‌شود تا «حداکثر یک گام اصلاحی»
                    # در کل چرخه معتبر بماند.
                    "correction_attempts": correction_attempts,
                    # وضعیت واقعی فایل‌های درخواستی تا این لحظه.
                    # چون هر Resume یک transcript تازه می‌سازد،
                    # بدون این کلید تغییرات موفقِ قبلی در گزارش
                    # نهایی گم می‌شدند.
                    "request_status": request_status_map(
                        project_root,
                        user_request,
                        transcript,
                        known_request_status
                    ),
                    # Readهای واقعیِ همین درخواست تا این لحظه
                    # (additive). چون هر Resume یک transcript تازه
                    # می‌سازد، بدون این کلید Readهای پیش از Approval
                    # گم می‌شدند و ویرایشِ فایل بعدیِ همان درخواست
                    # (مثل style.css) به‌اشتباه «بدون Read قبلی»
                    # رد می‌شد. فقط سابقهٔ خواندن را حمل می‌کند و
                    # شرط محتوای واقعیِ old را تغییر نمی‌دهد.
                    "read_paths": sorted(
                        normalize_relative_target(item)
                        for item in (
                            read_paths_already_seen(transcript)
                            | normalize_known_read_paths(
                                known_read_paths
                            )
                        )
                        if str(item)
                    ),
                    # Phase B: مشخصات Plan برای Re-validation
                    # قبل از Execute (additive؛ نسخه قدیمی این
                    # کلید را ندارد و Re-validation امن رد
                    # می‌کند).
                    "context": build_pending_revalidation_context(
                        tool_name,
                        args,
                        project_root,
                        project_name,
                        allow_write=allow_write,
                    ),
                }

                if autonomous:
                    # Re-validation runs BEFORE anything is executed and
                    # stays fail-closed. Only the manual prompt is skipped.
                    if is_stop_requested(stop_event):
                        return build_loop_report(
                            project_name,
                            project_root,
                            transcript,
                            notes=[
                                EMERGENCY_STOP_MESSAGE,
                                "Batch اجرا نشد؛ هیچ تغییری انجام نشد.",
                            ]
                        )

                    ok, reason, _ = revalidate_pending_action(
                        pending_action
                    )

                    if not ok:
                        observations.append(
                            "Batch به دلیل Re-validation رد شد: "
                            + str(reason)
                        )

                        return build_loop_report(
                            project_name,
                            project_root,
                            transcript,
                            notes=[
                                "Re-validation ناموفق بود؛ "
                                "هیچ فایلی نوشته نشد."
                            ]
                        )

                    emit_progress(
                        progress,
                        "autonomous_execute",
                        step=steps_used,
                        tool=tool_name
                    )

                    # اجرای مستقیم از همان Registry و همان مسیر
                    # موجودِ execute_pending_action استفاده می‌شود؛
                    # بنابراین Backup، Atomic write، Verify و
                    # Journal هیچ‌کدام دور زده نمی‌شوند.
                    try:
                        autonomous_result = spec["run"](
                            project_root, args
                        )

                    except Exception as e:
                        autonomous_result = tool_fail(e)

                    record_action(
                        "multi_write" if tool_name == "write_files"
                        else str(PENDING_TOOL_ACTION_TYPES.get(
                            tool_name, tool_name
                        )),
                        operation_class=ACTION_CLASS_USER,
                        tool=tool_name,
                        args={"paths": [
                            item.get("path") for item in
                            normalize_batch_files(args)
                        ]} if tool_name == "write_files" else args,
                        project=project_name,
                        project_root=project_root,
                        approval_state=ACTION_APPROVAL_APPROVED,
                        execution_state=ACTION_STATE_DONE,
                        result_summary=summarize_action_result(
                            "autonomous batch executed"
                            if tool_name == "write_files"
                            else "autonomous action executed"
                        ),
                    )

                    entry = {
                        "step": steps_used,
                        "tool": tool_name,
                        "args": args,
                        "result": autonomous_result,
                        "mutating": True,
                        "verified": (
                            autonomous_result.get("verified")
                            if isinstance(autonomous_result, dict)
                            else None
                        ),
                        # Step 3: جزئیات ساختاریافته Verify برای
                        # Error Feedback در گام اصلاحی.
                        "verify": verify_from_tool_result(
                            autonomous_result
                        ),
                        "autonomous": True,
                    }

                    transcript.append(entry)
                    observations.append(
                        format_observation(
                            steps_used,
                            tool_name,
                            args,
                            autonomous_result,
                            True
                        )
                    )

                    emit_progress(
                        progress,
                        "tool_done",
                        step=steps_used,
                        tool=tool_name,
                        mutating=True,
                        ok=not result_was_unsuccessful(
                            autonomous_result
                        ),
                        verified=entry["verified"],
                    )

                    return run_loop_steps(
                        user_request=user_request,
                        project_root=project_root,
                        project_name=project_name,
                        allow_write=True,
                        transcript=transcript,
                        observations=observations,
                        steps_used=steps_used,
                        correction_attempts=correction_attempts,
                        max_steps=max_steps,
                        stop_event=stop_event,
                        progress=progress,
                    )

                remember_pending_action(pending_action)

                emit_progress(
                    progress,
                    "awaiting_approval",
                    step=steps_used,
                    tool=tool_name
                )

                # Stage 4 — خلاصهٔ چندفایلی دقیقاً بعد از ساخت Plan
                # canonical و بعد از آماده شدن Context معتبر
                # Re-validation و قبل از نمایش Approval ساخته
                # می‌شود. متن همان Plan است؛ Approval همان
                # pending_action را مصرف می‌کند و خلاصه هیچ
                # مسیر اجرایی یا Mutation جدیدی نمی‌سازد.
                display_plan_text = build_pending_plan_text(
                    tool_name, args, project_root,
                    write_mode=allow_write,
                    plan_summary=build_multi_file_plan_summary(
                        pending_action
                    ),
                )

                return build_loop_report(
                    project_name,
                    project_root,
                    transcript,
                    pending_plan=display_plan_text,
                    notes=[
                        "هیچ تغییری هنوز روی دیسک انجام نشده "
                        "است. پس از تأیید، همین عملیات اجرا و "
                        "سپس Verify میشود."
                    ]
                )

            correction_attempts += 1
            observations.append(
                "گام اصلاحی: همان ابزار و همان مسیر قبلی که "
                "Verify آن ناموفق بود."
            )

        try:
            result = spec["run"](project_root, args)

        except Exception as e:
            result = tool_fail(e)

        entry = {
            "step": steps_used,
            "tool": tool_name,
            "args": args,
            "result": result,
            "mutating": bool(spec["mutating"]),
            "verified": (
                result.get("verified")
                if spec["mutating"] else None
            ),
            # Step 3: جزئیات ساختاریافته Verify برای Error Feedback
            # در گام اصلاحی.
            "verify": (
                verify_from_tool_result(result)
                if spec["mutating"] else None
            ),
        }

        transcript.append(entry)
        observations.append(
            format_observation(
                steps_used,
                tool_name,
                args,
                result,
                entry["mutating"]
            )
        )

        emit_progress(
            progress,
            "tool_done",
            step=steps_used,
            tool=tool_name,
            mutating=entry["mutating"],
            ok=not result_was_unsuccessful(result),
            verified=entry["verified"]
        )

        if (
            entry["mutating"]
            and result.get("verified") is False
            and correction_attempts >= MAX_CORRECTION_STEPS
        ):
            return build_loop_report(
                project_name,
                project_root,
                transcript,
                notes=[
                    "Verify ناموفق بود و سقف گام اصلاحی ("
                    + str(MAX_CORRECTION_STEPS)
                    + ") پر شده است؛ اجرا متوقف شد."
                ]
            )

    # Phase H: اگر حلقه بدون FINAL تمام شده، یک گزارش نهایی
    # فقط‌متنی ساخته می‌شود تا کاربر هرگز فقط لاگ مراحل
    # نبیند. این مسیر هیچ ابزاری اجرا و هیچ تغییری نمی‌کند.
    forced_final = build_forced_final(
        user_request,
        project_name,
        project_root,
        transcript,
        observations,
        stop_event=stop_event
    )

    return build_loop_report(
        project_name,
        project_root,
        transcript,
        final_answer=forced_final,
        notes=[
            "سقف گامهای Agentic Loop ("
            + str(max_steps)
            + ") پر شد؛ پاسخ نهایی از شواهد جمع‌آوری‌شده "
            "ساخته شد."
        ],
        user_request=(
            user_request
            if is_write_allowed(allow_write)
            else None
        ),
        known_request_status=known_request_status
    )


def run_agentic_loop(
    user_request,
    project_root,
    project_name,
    allow_write=False,
    stop_event=None,
    max_steps=MAX_AGENT_STEPS,
    progress=None,
    allowed_tools=None
):
    # شروع یک حلقه Agentic تازه.
    return run_loop_steps(
        user_request=user_request,
        project_root=project_root,
        project_name=project_name,
        allow_write=allow_write,
        transcript=[],
        observations=[],
        steps_used=0,
        correction_attempts=0,
        max_steps=max_steps,
        stop_event=stop_event,
        progress=progress,
        allowed_tools=allowed_tools
    )


def execute_pending_action(
    stop_event=None,
    expected_project=None,
    expected_args=None
):
    # اجرای Plan تأییدشده و ادامه حلقه برای Verify/Report.
    #
    # Phase I — Pending Action Integrity.
    #
    # expected_project / expected_args یعنی «Approvalی که کاربر
    # داد متعلق به کدام پروژه و کدام آرگومان‌ها بود». بدون آن‌ها
    # Slot سراسری هرچه در آن باشد اجرا می‌شد، یعنی Approvalِ
    # داده‌شده برای یک Plan می‌توانست صرفِ Planِ دیگری در Slot
    # شود. هر دو پارامتر اختیاری‌اند تا callerهایی که واقعاً
    # Context را می‌دانند آن را بدهند؛ راستی‌آزماییِ داخلی
    # (هویت + اثر انگشت) در هر حالت اجباری است.
    pending = get_pending_action()

    if pending is None:
        return None

    # هویت قبل از هر چیز دیگری سنجیده می‌شود: قبل از
    # Emergency Stop، قبل از TTL و قبل از Re-validation. دلیل
    # ترتیب این است که یک Pending بی‌هویت یا تغییرکرده نباید
    # حتی به‌عنوان «متوقف‌شده» یا «منقضی» هم پذیرفته شود.
    identity_ok, identity_reason = pending_action_identity_status(
        pending,
        expected_project=expected_project,
        expected_args=expected_args,
    )

    if not identity_ok:
        return build_report(
            "Execute (هویت Plan)",
            pending.get("project"),
            pending.get("project_root"),
            [
                identity_reason,
                "هیچ فایلی نوشته یا تغییر داده نشد.",
            ],
        )

    # Phase C: Emergency Stop قبل از هر چیز.
    # Pending پاک نمیشود، Re-validation/Execute اجرا نمیشود و
    # هیچ تغییری روی دیسک انجام نمیشود.
    if is_stop_requested(stop_event):
        stop_pending = pending if isinstance(pending, dict) else {}

        remember_stop_blocked_action(
            tool=stop_pending.get("tool"),
            context=stop_pending.get("context"),
            args=stop_pending.get("args"),
            project=stop_pending.get("project"),
            project_root=stop_pending.get("project_root"),
            approval_state=ACTION_APPROVAL_APPROVED,
        )

        return build_emergency_stop_report(
            "Emergency Stop (اجرا نشد)",
            stop_pending.get("project"),
            stop_pending.get("project_root"),
            tool_name=stop_pending.get("tool"),
            keep_pending=True,
            notes=[
                "Plan تأیید شد اما Emergency Stop فعال است؛ "
                "Execute انجام نشد.",
            ],
        )

    # Phase E: TTL / Expiration قبل از Re-validation و Execute.
    # Pending منقضی پاک می‌شود؛ هیچ mutation یا Backup انجام نمی‌شود.
    if is_pending_action_expired(pending):
        expired_context = (
            pending.get("context")
            if isinstance(pending.get("context"), dict)
            else None
        )

        remember_pending_expiration(pending, expired_context)
        clear_pending_action()

        return build_pending_expiration_report(pending)

    # Phase B: Re-validation قبل از Execute.
    # اگر شکست بخورد: هیچ اجرایی انجام نمی‌شود، Pending پاک
    # نمی‌شود، فایلی تغییر نمی‌کند و Backup جدیدی ساخته
    # نمی‌شود (Fail Closed).
    revalidation_ok, revalidation_reason, action_context = (
        revalidate_pending_action(pending)
    )

    if not revalidation_ok:
        remember_revalidation_failure(
            pending, action_context, revalidation_reason
        )

        return build_pending_revalidation_report(
            pending, revalidation_reason
        )

    clear_pending_action()

    project_root = pending["project_root"]
    project_name = pending["project"]
    tool_name = pending["tool"]
    args = pending.get("args") or {}

    spec = TOOL_REGISTRY.get(tool_name)

    if spec is None:
        return build_report(
            "Execute",
            project_name,
            project_root,
            ["ابزار نامعتبر: " + str(tool_name)]
        )

    try:
        result = spec["run"](project_root, args)

    except Exception as e:
        result = tool_fail(e)

    # Journal برای Batch: مسیر Canonical Manual Approval پیش‌تر
    # هیچ Action قابل Undo ثبت نمی‌کرد، بنابراین Undo یک Batch
    # موفق همیشه شکست می‌خورد.
    #
    # فقط وقتی ثبت می‌شود که همان Planِ تأییدشده واقعاً و بدون
    # Rollback اجرا شده باشد (result.ok). اطلاعات Undo از همان
    # نتیجهٔ واقعی write_file_multi خوانده می‌شود؛ هیچ Plan یا
    # مسیر تازه‌ای ساخته نمی‌شود.
    if tool_name == "write_files" and result.get("ok"):
        multi_result = get_last_write_multi_result()

        record_multi_write_undo(
            files_result=(multi_result or {}).get("files") or [],
            project_root=project_root,
            project=project_name,
            args=args,
            approval_state=ACTION_APPROVAL_APPROVED,
            verified=result.get("verified") is not False,
            agent_id=pending.get("agent_id"),
            agent_type=pending.get("agent_type"),
        )

    entry = {
        "step": 1,
        "tool": tool_name,
        "args": args,
        "result": result,
        "mutating": True,
        "verified": result.get("verified"),
        # Step 3: جزئیات ساختاریافته Verify برای Error Feedback
        # در گام اصلاحی.
        "verify": verify_from_tool_result(result),
    }

    observation = format_observation(
        1, tool_name, args, result, True
    )

    # Readهای همین درخواست که پیش از Approval انجام شده‌اند با
    # Resume حمل می‌شوند. transcript تازه فقط Mutation را دارد، پس
    # بدون این کار یک ویرایشِ درستِ فایل بعدی (که قبلاً در همین
    # چرخه خوانده شده) به‌اشتباه «بدون Read قبلی» رد می‌شد.
    # این فقط سابقهٔ خواندن است؛ شرط محتوای واقعیِ old در
    # ensure_edit_file_proposal_is_grounded دست‌نخورده می‌ماند.
    resume_read_paths = pending.get("read_paths")

    if not isinstance(resume_read_paths, list):
        resume_read_paths = []

    return run_loop_steps(
        user_request=pending.get("user_request") or "",
        project_root=project_root,
        project_name=project_name,
        allow_write=True,
        transcript=[entry],
        observations=[observation],
        steps_used=1,
        correction_attempts=int(
            pending.get("correction_attempts") or 0
        ),
        max_steps=max(1, MAX_AGENT_STEPS - 1),
        stop_event=stop_event,
        known_request_status=(
            pending.get("request_status")
            if isinstance(pending.get("request_status"), dict)
            else None
        ),
        known_read_paths=resume_read_paths
    )


def should_use_agentic_loop(user_request, project_root):
    # تشخیص درخواست چندمرحلهای:
    # - نشانههای صریح مرحلهای (سپس/مرحله به مرحله/کاوش/...)
    # - یا ترکیب «اقدام + بررسی» روی یک مسیر
    # درخواستهای ساده تکمرحلهای به مسیرهای قبلی
    # (Router قطعی و سپس Read/Write قدیمی) سپرده میشوند.
    text = normalize_request_text(user_request)

    if not text.strip():
        return False

    stripped = text.lstrip()

    if stripped.startswith("/agent") or stripped.startswith("agent:"):
        return True

    if contains_any_word(text, LOOP_TRIGGER_KEYWORDS):
        return True

    action_count = sum(
        1 for keyword in ACTION_VERB_KEYWORDS
        if keyword_matches_text(keyword, text)
    )
    read_count = sum(
        1 for keyword in READ_VERB_KEYWORDS
        if keyword_matches_text(keyword, text)
    )

    try:
        path_count = len(extract_path_candidates(user_request))

    except Exception:
        path_count = 0

    if action_count >= 1 and read_count >= 1 and path_count >= 1:
        return True

    # فعل تغییردهنده + مسیر صریح، حتی بدون فعل «خواندن»، یک
    # درخواست عملیاتی چندمرحله‌ای است: مدل باید اول فایل را
    # ببیند، Plan بدهد و از Approval/Re-validation عبور کند.
    # پیش از این قاعده، درخواستی مثل «فقط رنگ متن .text را
    # در style.css تغییر بده» به‌اشتباه به مسیر متنی Legacy
    # می‌رفت. درخواست عملیاتیِ بدون مسیر (مثلاً «این تابع را
    # بنویس») دست‌نخورده می‌ماند و همچنان متنی است.
    if action_count >= 1 and path_count >= 1:
        return True

    if action_count == 0 and read_count >= 2 and path_count >= 2:
        return True

    # Phase H: قاعده ساختاری «قصد مشاوره‌ای».
    #
    # دلیل: شرط‌های بالا به کوچک‌ترین جزئیات متن وابسته بودند.
    # یک درخواست کاملاً طبیعی مثل «این پروژه را بررسی کن،
    # مشکلات اصلی را بگو» نه Keyword ساختاری داشت و نه
    # مسیر/فعل؛ در نتیجه به Legacy می‌رفت و فقط یک اسکن
    # ساختار فایل + یک پاسخ مدل تولید می‌کرد (File Analyzer).
    #
    # این قاعده فقط «تشخیص مسیر» را کامل می‌کند و هیچ شرط
    # قبلی را تغییر نمی‌دهد. هیچ ابزار، Approval یا مسیر
    # تغییردهنده‌ای در این تابع دخالت ندارد.
    if not is_analysis_request(user_request):
        return False

    # درخواست دامنه‌دار همیشه تحلیل سطح پروژه است.
    if is_project_wide_request(user_request):
        return True

    # هدف تک‌فایلی روشن: مسیر Legacyِ تست‌شده حفظ می‌شود.
    if not names_existing_file_target(
        project_root, user_request
    ):
        return True

    return False


# Step 3 — Correction.
#
# مدل فقط یک فرصت اصلاح دارد (MAX_CORRECTION_STEPS). Proposal
# اصلاحی از همان Proposal Contract عادی عبور می‌کند؛ هیچ مسیر
# مستقیمی به نوشتن فایل وجود ندارد.
CORRECTION_SYSTEM_PROMPT = (
    "You repair ONE failed change and nothing else.\n"
    "Answer with exactly one line:\n"
    'BATCH_PROPOSAL: {"files": [{"path": "<relative path>", '
    '"content": "<FULL corrected file content>"}]}\n'
    "Rules: only the files listed in the feedback; content must be "
    "the COMPLETE file; change only what the verification error "
    "describes; no markdown, no commentary, no extra text."
)


def correction_context_files(
    proposal_files,
    verify,
    limit=MAX_CORRECTION_FILES
):
    # فقط فایل‌هایی که Verify آن‌ها شکست خورده به مدل داده
    # می‌شوند. فایل‌های سالم و بقیه پروژه دوباره فرستاده
    # نمی‌شوند (Minimal Error Context).
    paths = []

    for error in (verify or {}).get("errors") or []:
        path = str(error.get("path") or "")

        if path and path not in paths:
            paths.append(path)

    if not paths or len(paths) > limit:
        return []

    allowed = set(paths)

    return [
        item for item in (proposal_files or [])
        if isinstance(item, dict)
        and str(item.get("path")) in allowed
    ]


def build_correction_prompt(
    feedback,
    files,
    user_request="",
    project_name=None
):
    # Context حداقلی برای یک فرصت اصلاح.
    #
    # متن هر فایل باید کامل باشد، چون Proposal Contract کل فایل
    # را می‌نویسد؛ اگر همین context حداقلی جا نشود، هیچ اصلاحی
    # انجام نمی‌شود (Fail Closed) تا هرگز فایل ناقص نوشته نشود.
    entries = [
        item for item in (files or [])
        if isinstance(item, dict) and item.get("path") is not None
    ]

    if not entries or len(entries) > MAX_CORRECTION_FILES:
        return None

    total = 0
    blocks = []

    for item in entries:
        content = str(item.get("content") or "")
        total += len(content)

        if total > MAX_CORRECTION_CONTEXT_CHARS:
            return None

        blocks.append(
            "BEGIN_FILE " + str(item.get("path")) + "\n"
            + content + "\nEND_FILE " + str(item.get("path"))
        )

    return "\n".join([
        "PROJECT: " + str(project_name or ""),
        "CURRENT REQUEST: " + str(user_request or ""),
        "",
        "VERIFY FEEDBACK (the previous attempt was NOT verified):",
        str(feedback or ""),
        "",
        "FILES TO CORRECT (current content):",
        "\n".join(blocks),
        "",
        "Return BATCH_PROPOSAL JSON with the corrected FULL "
        "content of these files only.",
    ])


def request_corrected_proposal(
    feedback,
    files,
    project_name=None,
    project_root=None,
    user_request="",
    stop_event=None
):
    # یک فرصت اصلاح: Model → Proposal Contract.
    #
    # این تابع فقط یک Proposal برمی‌گرداند. هیچ ابزاری اجرا
    # نمی‌شود، هیچ فایلی نوشته نمی‌شود و هیچ تصمیمی درباره‌ی
    # مسیر/مجوز گرفته نمی‌شود؛ اعتبار و امنیت Proposal کاملاً
    # در اختیار Safety Layer است (همان مسیر Proposal عادی).
    if is_stop_requested(stop_event):
        return None

    prompt = build_correction_prompt(
        feedback, files, user_request, project_name
    )

    if not prompt:
        return None

    try:
        answer = call_model(
            CORRECTION_SYSTEM_PROMPT,
            prompt,
            stop_event=stop_event,
            n_predict=LOOP_MODEL_N_PREDICT,
            no_think=True,
        )

    except Exception:
        return None

    try:
        # Proposal Contract تنها یک Parser دارد (Step 2).
        from agents.orchestrator import extract_agent_proposal

    except Exception:
        return None

    return extract_agent_proposal(answer)


def handle_agent_batch_proposal(
    proposal,
    project_name,
    project_root,
    user_request,
    allow_write=False,
    stop_event=None,
    correction_attempts=0,
    proposal_source=None,
    agent_id=None,
    agent_type=None
):
    """
    Step 2: run an agent's PROPOSAL through the existing Step 1 batch path.

    A specialized agent may only propose. This function is the only place
    where a proposal becomes a real change, and it deliberately reuses the
    Core machinery instead of re-implementing any of it:

        proposal
          -> validate_batch_args()          (fails closed; project boundary)
          -> is_write_allowed()            (read-only mode still blocks)
          -> approval_mode
               manual     -> ONE pending action for the whole batch
               autonomous -> revalidate -> execute, no prompt
          -> Emergency Stop
          -> revalidate_pending_action()   (fail closed)
          -> tool_write_files() -> write_file_multi (atomic, backup, undo)
          -> record_action()               (journal)

    It does not touch Emergency Stop, Re-validation, Approval Gate,
    write_file_multi, backup/undo or the journal implementations; it only
    calls them in the same order the agentic loop already uses.

    Step 3 addition: after a successful execution the batch is VERIFIED
    (read-back + syntax, read-only). If that verification fails, the real
    error (file + type + message + line/position) is handed back to the
    model for exactly ONE correction (MAX_CORRECTION_STEPS), and the
    corrected proposal re-enters this same loop, so it passes through
    every safety layer again: validation, project boundary, write
    permission, approval/autonomous, Emergency Stop, Re-validation,
    atomic write, journal and Verify. A second failure stops execution.

    Phase 7-A addition: agent_id/agent_type are OPTIONAL producer
    attribution metadata (evidence only). They are copied into the
    journal and, when provided, into the Pending Action as plain keys
    that sit OUTSIDE the plan fingerprint (tool/args/project/project_root).
    No approval, execution or safety decision ever reads them back.
    """
    # تلاش صفر = Proposal اصلی. هر گام اصلاحی از همان ابتدای همین
    # حلقه عبور میکند، پس هیچ Safety Layer ای دور زده نمیشود.
    if isinstance(correction_attempts, int) and correction_attempts > 0:
        attempts_used = correction_attempts

    elif correction_attempts in (0, None):
        attempts_used = 0

    else:
        # مقدار نامعتبر → هیچ فرصت اصلاحی (Fail Closed).
        attempts_used = MAX_CORRECTION_STEPS

    entries = []
    current = proposal

    while True:
        if not current:
            return None

        # Fail closed on a malformed proposal before anything else happens.
        try:
            proposal_args = {"files": current.get("files") or []}

        except Exception:
            return build_report(
                "Agent Proposal",
                project_name,
                project_root,
                ["پیشنهاد Agent نامعتبر بود؛ هیچ تغییری انجام نشد."]
            )

        # validate_batch_args raises on any invalid member or any path
        # outside the project root, which rejects the WHOLE batch.
        try:
            proposal_args = {"files": validate_batch_args(
                project_root, proposal_args
            )}

        except Exception as e:
            return build_report(
                "Agent Proposal",
                project_name,
                project_root,
                [
                    "پیشنهاد Agent معتبر نبود؛ هیچ تغییری انجام نشد.",
                    str(e)[:200],
                ]
            )

        if not is_write_allowed(allow_write):
            return build_report(
                "Agent Proposal",
                project_name,
                project_root,
                [
                    "پیشنهاد Agent در حالت «فقط بررسی» اجرا نمیشود؛ "
                    "هیچ تغییری انجام نشد."
                ]
            )

        tool_name = "write_files"

        # Emergency Stop before creating any plan or writing anything.
        if is_stop_requested(stop_event):
            return build_emergency_stop_report(
                "Emergency Stop",
                project_name,
                project_root,
                notes=[
                    "پیشنهاد Agent اجرا نشد؛ "
                    "هیچ Step/ابزاری اجرا نشد.",
                ]
            )

        plan_text = build_pending_plan_text(
            tool_name, proposal_args, project_root,
            write_mode=allow_write
        )

        pending_action = {
            "tool": tool_name,
            "args": proposal_args,
            "project": project_name,
            "project_root": project_root,
            "user_request": user_request,
            # Readهای واقعیِ همین درخواست تا این لحظه (additive)،
            # مثل Step 1. این کار فقط سابقهٔ خواندن را حمل می‌کند.
            "read_paths": sorted(
                normalize_relative_target(item)
                for item in read_paths_already_seen(entries)
                if str(item)
            ),
            "context": build_pending_revalidation_context(
                tool_name,
                proposal_args,
                project_root,
                project_name,
                allow_write=allow_write,
            ),
        }

        # Phase 7-A: producer attribution (evidence only). Added ONLY
        # when provided, and outside the plan fingerprint, so Pending
        # Action Identity and Re-validation behave exactly as before
        # for every existing caller.
        if agent_id:
            pending_action["agent_id"] = agent_id

        if agent_type:
            pending_action["agent_type"] = agent_type

        # Manual: ONE approval for the entire batch, exactly like Step 1.
        # A correction is never a shortcut: it enters this same gate.
        if not is_autonomous_project(project_name):
            remember_pending_action(pending_action)

            # Stage 4 — همان Plan canonical، همان Context معتبر
            # Re-validation، همان Approval. خلاصه فقط نمایشی است
            # و قبل از پیام Approval نمایش داده می‌شود.
            plan_text = build_pending_plan_text(
                tool_name, proposal_args, project_root,
                write_mode=allow_write,
                plan_summary=build_multi_file_plan_summary(
                    pending_action
                ),
            )

            return build_loop_report(
                project_name,
                project_root,
                entries,
                pending_plan=plan_text,
                notes=[
                    "Agent فقط پیشنهاد داد؛ هیچ تغییری هنوز روی دیسک "
                    "انجام نشده است. برای اجرای کل Batch یک بار تأیید کنید."
                ]
            )

        # Autonomous: approval is skipped, every other safety layer is not.
        ok, reason, context = revalidate_pending_action(pending_action)

        if not ok:
            remember_revalidation_failure(pending_action, context, reason)

            return build_loop_report(
                project_name,
                project_root,
                entries,
                notes=[
                    "Re-validation ناموفق بود؛ هیچ فایلی نوشته نشد.",
                    str(reason)[:200],
                ]
            )

        # Emergency Stop may have been triggered while re-validating.
        if is_stop_requested(stop_event):
            return build_emergency_stop_report(
                "Emergency Stop",
                project_name,
                project_root,
                notes=["پیشنهاد Agent اجرا نشد؛ هیچ تغییری انجام نشد."]
            )

        result = tool_write_files(project_root, proposal_args)

        # Step 3: نتیجه Verify بعد از Mutation. ابزار همان Verify
        # محدود (read-back + syntax) را خودش انجام داده است.
        verify = verify_from_tool_result(result)

        if verify is None:
            # مسیر دفاعی: اگر ابزار نتیجه Verify نداده باشد، اینجا
            # انجام میشود؛ هیچ فرضی درباره موفقیت گرفته نمیشود.
            if result.get("ok"):
                verify = verify_applied_files(
                    project_root, proposal_args.get("files") or []
                )

            else:
                verify = verify_failed_mutation({})

        verified_ok = (
            bool(result.get("ok"))
            and bool(verify.get("ok"))
            and result.get("verified") is not False
        )

        # Journal وضعیت واقعی را ثبت میکند: اجرا + نتیجه Verify.
        record_action(
            "multi_write",
            operation_class=ACTION_CLASS_USER,
            tool=tool_name,
            args=proposal_args,
            project=project_name,
            project_root=project_root,
            approval_state=ACTION_APPROVAL_APPROVED,
            validation_state=bool(verified_ok),
            execution_state=(
                ACTION_STATE_DONE
                if result.get("ok") else ACTION_STATE_FAILED
            ),
            verify_state=verify_state_from_bool(verified_ok),
            result_summary=summarize_action_result(
                "agent batch proposal executed; verify="
                + ("ok" if verified_ok else "failed")
            ),
            agent_id=agent_id,
            agent_type=agent_type,
        )

        entry = {
            "step": len(entries) + 1,
            "tool": tool_name,
            "args": proposal_args,
            "result": result,
            "mutating": True,
            "verified": result.get("verified"),
            "verify": verify,
        }

        entries = entries + [entry]

        if verified_ok:
            return build_loop_report(
                project_name,
                project_root,
                entries,
                final_answer=str(result.get("text") or ""),
            )

        # Step 3: Error Feedback (فایل + نوع خطا + پیام + line/position).
        feedback = format_verify_feedback(verify, tool_name)

        # فقط یک گام اصلاحی؛ همان ثابت موجود در Core Loop.
        if attempts_used >= MAX_CORRECTION_STEPS:
            return build_loop_report(
                project_name,
                project_root,
                entries,
                notes=[
                    "Verify ناموفق بود و سقف گام اصلاحی ("
                    + str(MAX_CORRECTION_STEPS)
                    + ") پر شده است؛ اجرا متوقف شد.",
                    feedback,
                ]
            )

        source = proposal_source or request_corrected_proposal

        try:
            corrected = source(
                feedback,
                correction_context_files(
                    proposal_args.get("files") or [], verify
                ),
                project_name=project_name,
                project_root=project_root,
                user_request=user_request,
                stop_event=stop_event,
            )

        except Exception:
            corrected = None

        if not corrected:
            # Proposal اصلاحی نداریم → هیچ تغییر جدیدی اجرا نمیشود.
            return build_loop_report(
                project_name,
                project_root,
                entries,
                notes=[
                    "Proposal اصلاحی معتبری تولید نشد؛ هیچ تغییر "
                    "جدیدی اجرا نشد.",
                    feedback,
                ]
            )

        if is_stop_requested(stop_event):
            return build_emergency_stop_report(
                "Emergency Stop",
                project_name,
                project_root,
                notes=[
                    "گام اصلاحی اجرا نشد؛ حالت اضطراری فعال است."
                ]
            )

        attempts_used += 1
        current = corrected

        # برگشت به ابتدای همین حلقه: Proposal اصلاحی دقیقاً از همان
        # مسیر عبور میکند (validate → permission → approval/
        # autonomous → Stop → Re-validation → write → journal → Verify).


def run_agent(
    user_request,
    project_name,
    allow_write=False,
    stop_event=None,
    progress=None,
    session_id=None
):
    # دروازه ورودی Agent:
    # 0) General Mode: بدون پروژه، فقط پاسخ متنی (هیچ ابزار
    #    فایل‌محوری، هیچ مسیر، هیچ mutation)
    # 1) حافظه کوتاه گفتگو (in-memory)
    # 2) Gate تأیید Plan برای عملیات تغییردهنده
    # 3) Agentic Loop برای درخواستهای چندمرحلهای
    # 4) وگرنه: مسیرهای قبلی (Router قطعی + Read/Write
    #    تستشده) بدون هیچ تغییری.
    project_root = get_project_path(project_name)

    # Phase 1 — Legacy Fallback Fail-Closed.
    #
    # در ابتدای تابع مقداردهی می‌شود، نه فقط پیش از
    # شاخهٔ حلقه: هر استثنای پیش از رسیدن به آن نقطه
    # (مثلاً مسیر General Mode) در غیر این صورت به
    # UnboundLocalError می‌رسید و به‌جای Fail-Closed یک
    # خطای داخلی گزارش می‌شد.
    canonical_path_failed = False

    _session_token = _CURRENT_SESSION.set(session_id)

    remember_conversation_message(
        "user", user_request, project_name,
        session_id=session_id
    )

    # General Mode:
    # - درخواست صرفاً متنی -> همان run_general_task() قبلی.
    # - درخواست اجرایی (طبق should_use_agentic_loop) -> همان
    #   pipeline پروژه عادی ولی با ریشه ثابت GENERAL_WORKSPACE_DIR.
    # get_project_path دست‌نخورده است و برای general همچنان None
    # برمی‌گرداند؛ general هرگز وارد projects.json نمی‌شود و هیچ
    # مسیری از ورودی کاربر گرفته نمی‌شود.
    if is_general_mode(project_name):
        general_effective_root = None

        try:
            general_effective_root = GENERAL_WORKSPACE_DIR
        except Exception:
            general_effective_root = None

        general_is_executive = False

        try:
            general_is_executive = bool(should_use_agentic_loop(
                user_request, None
            ))
        except Exception:
            general_is_executive = False

        # General Mode has no pending plan of its own; control replies
        # must never enter the project/agentic pipeline.
        general_is_control = False

        if not general_is_executive and not general_is_control:
            general_result = run_general_task(
                user_request,
                project_name=project_name,
                allow_write=allow_write,
                stop_event=stop_event,
                progress=progress,
            )

            remember_conversation_message(
                "agent", general_result, project_name
            )

            # Phase 4 — Durable Memory record (flag-gated, never fails).
            _record_durable_interaction(
                user_request, general_result, project_name=project_name
            )

            return general_result

        general_root = ensure_general_workspace()

        if not general_root or not os.path.isdir(general_root):
            general_fail = build_report(
                "General Mode",
                GENERAL_MODE_NAME,
                None,
                [
                    "Workspace اختصاصی General در دسترس نیست؛ "
                    "هیچ تغییری انجام نشد.",
                ],
            )

            remember_conversation_message(
                "agent", general_fail, project_name
            )

            return general_fail

        # پروژه انتخاب‌شده همچنان دقیقاً مثل قبل از همین خط عبور
        # می‌کند؛ فقط general اجرایی با effective_root ثابت ادامه
        # می‌دهد تا Proposal → Approval → Re-validation → Execute
        # → Verify → Journal طی شود. هیچ pipeline جدیدی ساخته
        # نمی‌شود.
        project_root = general_root

    if project_root and os.path.isdir(project_root):
        try:
            # Phase C: Emergency Stop قبل از Approval/Loop/Legacy.
            # درخواست‌های کنترل (تأیید/لغو) همچنان توسط Gate
            # مدیریت می‌شوند تا کاربر بتواند تصمیم بگیرد؛ اما
            # درخواست اجرایی جدید اجرا نمی‌شود و Pending Action
            # دست‌نخورده می‌ماند.
            if (
                is_stop_requested(stop_event)
                and detect_control_reply(user_request) is None
            ):
                stop_result = build_emergency_stop_report(
                    "Emergency Stop",
                    project_name,
                    project_root,
                    notes=[
                        "درخواست جدید در حالت Emergency Stop "
                        "اجرا نشد؛ هیچ Step/ابزاری اجرا نشد.",
                    ],
                )

                remember_conversation_message(
                    "agent", stop_result, project_name
                )

                return stop_result

            approval_result = handle_pending_action_request(
                user_request,
                allow_write=allow_write,
                stop_event=stop_event,
                expected_project=project_name
            )

            if approval_result is not None:
                remember_conversation_message(
                    "agent", approval_result, project_name
                )

                return approval_result

            # مسیر Undo.
            #
            # این فقط دروازهٔ ورودی است: تشخیص پاسخ کوتاه
            # «واگرد / undo» و صدا زدن همان
            # undo_last_mutating_action موجود. تمام Safetyها
            # (Emergency Stop، read-only، اعتبارسنجی candidate،
            # journal و Fail-Closed) داخل همان تابع و دست‌نخورده
            # باقی می‌مانند و اینجا چیزی موازی ساخته نمی‌شود.
            #
            # این مسیر عمداً قبل از routing معمولی است تا یک
            # درخواست Undo هرگز وارد Agentic Loop نشود و Mutation
            # تازه‌ای نسازد؛ و پاسخ همیشه با همان ساختار گزارش
            # Agent برگردانده می‌شود.
            if detect_control_reply(user_request) == "undo":
                # project عمداً None است و این ضعف امنیتی نیست:
                #
                # ابزارهای پایه (write_file/edit_file و ...) در
                # Journal فقط project_root را ثبت می‌کنند و نامِ
                # نمایشی پروژه در آن‌ها null می‌ماند. بنابراین اگر
                # نام پروژه را بدهیم، validate_undo_candidate همیشه
                # آن را «متعلق به پروژه دیگری» رد می‌کند و Undo
                # هرگز کار نمی‌کند.
                #
                # مرز واقعی و مؤثر، project_root است و همچنان کامل
                # اعمال می‌شود: ریشهٔ ثبت‌شده با ریشهٔ فعلی مقایسه
                # می‌شود، سپس before_state/sha1 و is_inside_project
                # و idempotency کنترل می‌شوند. یعنی Undoی خارج از
                # پروژه همچنان Fail-Closed است.
                undo_ok, undo_reason, undo_result = (
                    undo_last_mutating_action(
                        project=None,
                        project_root=project_root,
                        allow_write=allow_write,
                        stop_event=stop_event,
                    )
                )

                undo_lines = [
                    str(undo_reason),
                ]

                if not undo_ok:
                    # بدون ادعای موفقیت: شکست Undo دقیقاً و با
                    # همان دلیلِ برگشتی گزارش می‌شود.
                    undo_lines.append(
                        "هیچ تغییری برگردانده نشد."
                    )

                undo_report = build_report(
                    "Undo",
                    project_name,
                    project_root,
                    undo_lines,
                )

                remember_conversation_message(
                    "agent", undo_report, project_name
                )

                return undo_report

            # Phase 2: Multi-Agent integration.
            # When use_multi_agent is True, delegate to SeniorAgent
            # before falling through to agentic_loop / legacy path.
            #
            # Step 2: Multi-Agent is Analyze -> Propose only. SeniorAgent
            # holds no mutation/command tool, so it cannot change anything
            # by itself. If it proposed a batch, that proposal is handed to
            # handle_agent_batch_proposal(), which runs it through the very
            # same Core batch path used by the agentic loop (validate ->
            # approval/autonomous -> re-validation -> write_file_multi ->
            # journal). Nothing here bypasses Emergency Stop.
            if use_multi_agent:
                try:
                    from agents.core_adapter import build_core_adapter
                    from agents.orchestrator import SeniorAgent

                    core = build_core_adapter(
                        call_model=call_model,
                        read_file=read_file,
                        safe_path=safe_path,
                        search_in_files=search_in_files,
                        is_write_allowed=is_write_allowed,
                        is_stop_requested=is_stop_requested,
                        record_action=record_action,
                        mask_sensitive_content=mask_sensitive_content,
                        stop_event=stop_event,
                        submit_handler=handle_agent_batch_proposal,
                    )
                    senior = SeniorAgent(
                        tools=core.tools_for_agent(),
                        stop_event=stop_event,
                    )

                    senior_result, agent_proposal = senior.propose(
                        user_request,
                        project_name=project_name,
                        project_root=project_root,
                        allow_write=allow_write,
                        stop_event=stop_event,
                    )

                    if agent_proposal:
                        # A proposal never executes here; the Core does.
                        # Phase 7-A: carry the producing agent's identity
                        # (orchestrator / specialist) into the execution
                        # record as evidence-only metadata.
                        origin = senior.get_proposal_origin() or (None, None)
                        proposal_result = handle_agent_batch_proposal(
                            agent_proposal,
                            project_name=project_name,
                            project_root=project_root,
                            user_request=user_request,
                            allow_write=allow_write,
                            stop_event=stop_event,
                            agent_id=origin[0],
                            agent_type=origin[1],
                        )

                        remember_conversation_message(
                            "agent", proposal_result, project_name
                        )

                        return proposal_result

                    if senior_result is None:
                        # Agent failed. Fall through to the normal paths
                        # instead of returning anything.
                        raise RuntimeError(
                            "SeniorAgent produced no result"
                        )

                    remember_conversation_message(
                        "agent", senior_result, project_name
                    )

                    return senior_result

                except Exception:
                    # Phase 7-A: evidence-only journal record. Control
                    # flow is unchanged: the exception is not re-raised
                    # and the legacy fallback below still runs. The
                    # Safety Spine is untouched; this only makes the
                    # multi-agent failure observable in the journal.
                    try:
                        record_action(
                            action_type="agent_fallback",
                            operation_class=ACTION_CLASS_READ,
                            tool="run_agent",
                            args={"error": str(sys.exc_info()[1])[:120]},
                            project=project_name,
                            project_root=project_root,
                            agent_id="orchestrator",
                            agent_type="orchestrator",
                            execution_state=ACTION_STATE_FAILED,
                            result_summary=summarize_action_result(
                                "multi-agent path failed; fell back to "
                                "agentic_loop/legacy"
                            ),
                        )
                    except Exception:
                        pass
                    # Phase 7: خطای Multi-Agent دیگر کاملاً بی‌صدا
                    # نیست. رفتار کنترل جریان دقیقاً مثل قبل است:
                    # استثنا دوباره raise نمی‌شود و مسیر fallback
                    # (agentic_loop / legacy) بدون تغییر ادامه می‌یابد.
                    # فقط برای عیب‌یابی در stderr ثبت می‌شود.
                    try:
                        print(
                            "[multi-agent] SeniorAgent failed; "
                            "falling back to agentic_loop/legacy: %r"
                            % (sys.exc_info()[1],),
                            file=sys.stderr,
                        )
                    except Exception:
                        pass

            # قرارداد Apply / Review (canonical mutation workflow)
            #
            # Review  → همان مسیر تشخیصیِ امروز؛ فقط خواندن،
            #            تحلیل و Plan. بدون mutation.
            #
            # Apply   → همیشه Agentic Loop، بدون استثنا:
            #            Plan → Re-validation → Approval (یا
            #            autonomous) → Execute → Verify.
            #
            # دلیل اجباری‌کردن حلقه در Apply: routing قدیمی در
            # بعضی درخواست‌ها حلقه را انتخاب نمی‌کرد و در نتیجه
            # مسیر Legacy مستقیماً فایل را می‌نوشت؛ یعنی Apply
            # بدون Plan/Approval/Verify یک معنی دوم داشت. حالا
            # Apply فقط یک معنی دارد.
            #
            # تنها راه رسیدن به Legacy در Apply، fallbackِ
            # «مدل در دسترس نیست» است؛ آن مسیر هم Fail-Closed
            # است و هیچ Write مستقیمی ندارد.
            #
            # Phase 1: این ادعا فقط وقتی درست می‌ماند که
            # Legacy در آن شرایط واقعاً Write نکند. به همین
            # دلیل canonical_path_failed (که در ابتدای
            # همین تابع صفر شده) تعیین می‌شود و پایین‌تر به
            # یک پاسخ Fail-Closed تبدیل می‌گردد، نه به اجازهٔ
            # عبور به intent handlerهای تغییردهنده.
            use_agentic_loop = should_use_agentic_loop(
                user_request, project_root
            )

            if is_write_allowed(allow_write):
                use_agentic_loop = True

            if use_agentic_loop:
                _allowed = None
                if is_general_mode(project_name):
                    _allowed = _WEB_RESEARCH_ALLOWED_TOOLS
                loop_result = run_agentic_loop(
                    user_request,
                    None,
                    project_name,
                    allow_write=allow_write,
                    stop_event=stop_event,
                    progress=progress,
                    allowed_tools=_allowed,
                )

                if loop_result is not None:
                    remember_conversation_message(
                        "agent", loop_result, project_name
                    )

                    # Phase 4 — Durable Memory record (flag-gated).
                    _record_durable_interaction(
                        user_request, loop_result, project_name=project_name
                    )

                    return loop_result

                # Phase 1: canonical path unavailable (model down).
                # درخواستِ تغییردهنده نباید به Legacy برسد.
                canonical_path_failed = True

        except Exception:
            # Agentic Loop هرگز نباید مسیرهای تستشده را
            # خراب کند؛ در صورت خطای غیرمنتظره، اجرای
            # معمولی ادامه پیدا میکند.
            #
            # اما خطا نباید کاملاً بی‌صدا باشد: در غیر این
            # صورت شکست حلقه کاملاً پنهان می‌شد و تشخیص آن
            # ممکن نبود. رفتار کنترل جریان دقیقاً مثل قبل
            # است (fallback حفظ می‌شود و خطا دوباره raise
            # نمی‌شود)؛ فقط خطا ثبت و از مسیر جاری خارج
            # می‌شود.
            record_agentic_loop_failure(
                user_request,
                project_name,
                sys.exc_info(),
            )

            # Phase 1: خطای غیرمنتظرهٔ حلقه هم یعنی مسیر
            # Canonical در دسترس نیست؛ نباید به Legacyِ
            # تغییردهنده سپرده شود.
            canonical_path_failed = True

    # Phase 1 — Legacy Fallback Fail-Closed.
    #
    # قبلاً اگر حلقهٔ Canonical به هر دلیلی برنمی‌گشت
    # (مدل در دسترس نبود یا استثنا داد)، اجرا به
    # run_agent_legacy می‌رسید و intent handlerهای
    # handle_command/delete/move/create/edit مستقیماً
    # run_command / delete_entry / move_entry / write_file /
    # edit_file را صدا می‌زدند — بدون Approval، بدون
    # Re-validation و بدون Plan. یعنی خرابی مدل،
    # سطح ایمنی را پایین می‌آورد.
    #
    # در حالت Apply اکنون Fail-Closed است: نه mutation،
    # نه false success، فقط پیام روشن. حالت Review
    # (allow_write نیست) طبق رفتار قبلیِ خواندنی سالم
    # می‌ماند و از Legacy عبور می‌کند.
    if is_write_allowed(allow_write) and canonical_path_failed:
        result = build_report(
            "Apply",
            project_name,
            project_root,
            [
                "مسیر اصلی Agent (Plan → Re-validation → "
                "Approval → Execute → Verify → Journal) "
                "در دسترس نبود، بنابراین هیچ تغییری انجام "
                "نشد.",
                "این تصمیم عمداً Fail-Closed است: بدون "
                "Plan و Approval هیچ Write، Move، Delete یا "
                "Command اجرا نمی‌شود.",
                "لطفاً سرویس مدل محلی را بررسی کنید "
                "(http://127.0.0.1:8080) و درخواست را "
                "دوباره بفرستید.",
            ],
        )

        remember_conversation_message(
            "agent", result, project_name
        )

        # Phase 4 — Durable Memory record (flag-gated, never fails).
        _record_durable_interaction(
            user_request, result, project_name=project_name
        )

        return result

    if is_general_mode(project_name):
        result = run_general_task(
            user_request,
            project_name=project_name,
            allow_write=allow_write,
            stop_event=stop_event,
            progress=progress,
        )
        remember_conversation_message("agent", result, project_name)
        _record_durable_interaction(user_request, result, project_name=project_name)
        return result

    try:
        result = run_agent_legacy(
            user_request,
            project_name=project_name,
            allow_write=allow_write,
            stop_event=stop_event
        )

    except RuntimeError as e:
        # Phase C: خطای ناشی از Emergency Stop نباید به شکل
        # exception کنترل‌نشده به بیرون برود. اگر Stop فعال
        # نباشد، رفتار قبلی (بالا رفتن خطا) حفظ می‌شود.
        if not is_stop_requested(stop_event):
            raise

        result = build_emergency_stop_report(
            "Emergency Stop",
            project_name,
            project_root,
            notes=[
                "اجرا در حالت Emergency Stop متوقف شد.",
                "جزئیات: " + clip_text(str(e), 200),
            ],
        )

    remember_conversation_message(
        "agent", result, project_name
    )

    # Phase 4 — Durable Memory record (flag-gated, never fails).
    _record_durable_interaction(
        user_request, result, project_name=project_name
    )

    return result


def run_agent_legacy(
    user_request,
    project_name,
    allow_write=False,
    stop_event=None,
):
    project_root = get_project_path(
        project_name
    )

    if not project_root:
        return "پروژه انتخاب‌شده پیدا نشد."

    if not os.path.isdir(
        project_root
    ):
        return (
            "پوشه پروژه پیدا نشد:\n"
            + project_root
        )

    # Plan چندفایلی: فقط تشخیص و آماده‌سازی.
    # هیچ Write چندفایلی، هیچ Backup چندفایلی و هیچ
    # Rollback چندفایلی در این مرحله فعال نمی‌شود.
    build_file_plan(
        project_root,
        user_request
    )

    # مسیر عمومی Agent:
    # Explore → Understand → Plan → Execute → Verify → Report
    # اگر این Router درخواست را بردارد، نتیجه واقعی
    # (خوانده‌شده از دیسک) برگردانده می‌شود؛ در غیر این
    # صورت رفتار قبلی Read/Write بدون تغییر ادامه پیدا
    # می‌کند.
    handled, generic_result = run_generic_task(
        user_request,
        project_root,
        project_name,
        allow_write=allow_write,
        stop_event=stop_event
    )

    if handled:
        return generic_result

    requested_files = find_requested_files(
        project_root,
        user_request
    )
    requested_file = requested_files[0] if requested_files else None

    should_search = False
    if not requested_file:
        should_search = True
    elif is_write_allowed(allow_write):
        if (
            "/" not in requested_file
            and "\\" not in requested_file
            and "." not in requested_file
        ):
            should_search = True

    if should_search:
        if is_write_allowed(allow_write):
            path_pattern = (
                r"(?<![\w])"
                r"([A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)*)"
                r"(?![\w])"
            )
            path_matches = re.findall(
                path_pattern, user_request
            )
            write_path = None
            candidates = []
            for path_match in path_matches:
                normalized = path_match.replace(
                    "\\", os.sep
                ).replace("/", os.sep)
                try:
                    full_path = safe_path(
                        project_root, normalized
                    )
                    parent = os.path.dirname(full_path)
                    if os.path.isdir(parent):
                        candidates.append(
                            os.path.relpath(
                                full_path, project_root
                            )
                        )
                except (ValueError, OSError):
                    pass
            for c in candidates:
                if "/" in c or "\\" in c or "." in c:
                    write_path = c
                    break
            if not write_path and candidates:
                write_path = candidates[0]
            if write_path:
                # Before any rewrite, read the current file content
                # (if it exists) so the model can preserve unchanged
                # parts. Without this, the model is asked to generate
                # the whole file from scratch and existing data is
                # silently lost (especially for extensionless files
                # like Makefile / Dockerfile).
                current_file_content = ""
                try:
                    full_write_path = safe_path(
                        project_root,
                        write_path
                    )

                    if os.path.isfile(full_write_path):
                        with open(
                            full_write_path,
                            "r",
                            encoding="utf-8",
                            newline=""
                        ) as f:
                            current_file_content = f.read()

                except Exception:
                    current_file_content = ""

                model_output = call_model(
                    """
You are a local coding assistant responsible for editing ONE file.

Return ONLY:

BEGIN_FILE
[complete new file content]
END_FILE

Do not change the filename.
Do not change the path.
Do not discuss paths.
Do not ask for the file.

Return ONLY:

BEGIN_FILE
[complete new file content]
END_FILE

Preserve everything that does not need to change.
""",
                    f"""
USER REQUEST:
{user_request}

PROJECT:
{project_name}

TARGET FILE:
{write_path}

BEGIN_CURRENT_FILE
{current_file_content}
END_CURRENT_FILE
""",
                    stop_event=stop_event,
                )
                new_content = extract_file_content(
                    model_output
                )

                if new_content is None:
                    return (
                        "مدل محتوای کامل فایل را "
                        "برنگرداند؛ بنابراین هیچ "
                        "تغییری اعمال نشد."
                    )

                # Fail-Closed: مسیر Legacy اجازهٔ Write مستقیم
                # ندارد. این‌جا Plan/Re-validation/Approval/Verify
                # وجود ندارد، پس حتی با allow_write=True نباید
                # چیزی روی دیسک عوض شود.
                return LEGACY_DIRECT_WRITE_REFUSED

            else:
                requested_dir = find_requested_directory(
                    project_root, user_request
                )
                structure = build_project_structure(
                    project_root,
                    relative_path=requested_dir,
                )
                return call_model(
                    """
You are a local coding assistant.

Answer the user's request directly
and concisely.

The real structure of the selected
project is provided in the user
message.

Base your answer ONLY on that real
structure.

Do not claim to have read file
contents that were not provided.

Do not invent files, folders, or
file contents.

Do not repeat or re-list the
project structure.

Write the answer exactly once.
Do not repeat yourself.
""",
                    f"""
USER REQUEST:
{user_request}

PROJECT:
{project_name}

REAL PROJECT STRUCTURE (root level):
{structure}
"""
                    , stop_event=stop_event)
        else:
            requested_dir = find_requested_directory(
                project_root, user_request
            )
            structure = build_project_structure(
                project_root,
                relative_path=requested_dir,
            )
            return call_model(
                """
You are a local coding assistant.

Answer the user's request directly
and concisely.

The real structure of the selected
project is provided in the user
message.

Base your answer ONLY on that real
structure.

Do not claim to have read file
contents that were not provided.

Do not invent files, folders, or
file contents.

Do not repeat or re-list the
project structure.

Write the answer exactly once.
Do not repeat yourself.
""",
                f"""
USER REQUEST:
{user_request}

PROJECT:
{project_name}

REAL PROJECT STRUCTURE (root level):
{structure}
"""
                , stop_event=stop_event)

    # Phase H — Security Boundary. این مسیر فایل را می‌خواند و
    # مستقیم به call_model می‌دهد، پس مسدودسازی پیش از I/O
    # لازم است.
    if is_sensitive_read_target(requested_file):
        return sensitive_read_refusal_text(requested_file)

    try:
        current_content = read_file(
            project_root,
            requested_file
        )

    except Exception as e:
        return (
            "خطا در خواندن فایل:\n"
            + str(e)
        )

    if not is_write_allowed(allow_write):
        # Mask فقط در مسیر Read-only اعمال می‌شود؛
        # در مسیر Write اعمال نمی‌شود تا مقادیر واقعی
        # فایل با [REDACTED] بازنویسی و از بین نروند.
        masked_content = mask_sensitive_content(
            current_content
        )

        return call_model(
            """
You are a local coding assistant.

The Python agent has already located
and read the requested file.

Analyze ONLY the supplied file content.

Do not ask the user to read the file.
Do not claim the file is outside
the project.
Do not invent missing content.

Answer directly and briefly.
""",
            f"""
USER REQUEST:
{user_request}

PROJECT:
{project_name}

TARGET FILE:
{requested_file}

BEGIN_CURRENT_FILE
{masked_content}
END_CURRENT_FILE
"""
        , stop_event=stop_event)

    model_output = call_model(
        """
You are a local coding assistant
responsible for editing ONE file.

The Python agent has already located
and read the correct file.

Return the COMPLETE new content of
that SAME file.

Do not change the filename.
Do not change the path.
Do not discuss paths.
Do not ask for the file.

Return ONLY:

BEGIN_FILE
[complete new file content]
END_FILE

Preserve everything that does not
need to change.
""",
        f"""
USER REQUEST:
{user_request}

PROJECT:
{project_name}

TARGET FILE:
{requested_file}

BEGIN_CURRENT_FILE
{current_content}
END_CURRENT_FILE
"""
    , stop_event=stop_event)

    new_content = extract_file_content(
        model_output
    )

    if new_content is None:
        model_output = call_model(
            """You are a file content editor. Return ONLY the new file content between BEGIN_FILE and END_FILE. Do NOT explain. Do NOT write code. Just the content.

BEGIN_FILE
[new content]
END_FILE""",
            f"""USER REQUEST:
{user_request}

TARGET FILE:
{requested_file}

CURRENT CONTENT:
{current_content}""",
            stop_event=stop_event,
        )
        new_content = extract_file_content(model_output)
        if new_content is None:
            return (
                "مدل محتوای کامل فایل را "
                "برنگرداند؛ بنابراین هیچ "
                "تغییری اعمال نشد."
            )

    # Fail-Closed: مسیر Legacy اجازهٔ Write مستقیم ندارد.
    # تنها مسیر مجاز برای تغییر فایل، مسیر canonical است
    # (Plan → Re-validation → Approval → Execute → Verify).
    return LEGACY_DIRECT_WRITE_REFUSED



def has_unnegated_keyword(text, keywords):
    text_l = (text or "").lower()
    neg_markers = ("do not", "don't", "not ", "نکن", "نده", "نباشد", "never")
    for kw in (keywords or ()):
        kw_l = str(kw).lower()
        idx = text_l.find(kw_l)
        if idx < 0:
            continue
        left = text_l[max(0, idx - 50): idx]
        right = text_l[idx + len(kw_l): idx + len(kw_l) + 60]
        if any(m in left or m in right for m in neg_markers):
            continue
        return True
    return False


_LAST_LOOP_REPORT_LEDGER = []


def get_loop_report_ledger(report=None):
    return list(_LAST_LOOP_REPORT_LEDGER)

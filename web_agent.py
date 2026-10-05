from flask import (
    Flask,
    request,
    render_template_string,
    redirect,
    url_for,
    jsonify
)
from agent import (
    run_agent,
    MODEL_URL,
    get_last_write_result,
    rollback_file,
    get_last_rollback_result,
    build_file_plan,
    get_last_file_plan,
    format_file_plan_text,
    load_projects,
    add_project,
    remove_project,
    validate_mutating_args,
    build_pending_revalidation_context,
    build_pending_plan_text,
    build_multi_file_plan_summary,
    remember_pending_action,
    execute_pending_action,
    get_last_write_multi_result,
    get_pending_action,
    handle_pending_action_request,
    is_stop_requested,
)
import os
import time
import re
import sqlite3
import tkinter as tk
from tkinter import filedialog
import threading

app = Flask(__name__)

# ---------------------------------------------------------
# Phase 1 — Localhost Origin Policy
# ---------------------------------------------------------
# سرور فقط روی 127.0.0.1 گوش می‌دهد (app.run در انتهای
# فایل) و هیچ سامانهٔ احراز هویتی ندارد. بنابراین تنها
# محافظ مؤثر در برابر یک صفحهٔ وبِ دلخواه، محدودکردن
# Origin است.
#
# قاعده: فقط Origin خالی (درخواست هم‌مبدأ/مستقیم) یا
# localhost پذیرفته می‌شود. هر Origin دیگری — و
# به‌خصوص Origin خالی‌نبودنِ غیرمحلی — رد می‌شود.
LOCAL_ORIGIN_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


def is_local_origin(origin):
    origin = str(origin or "").strip()

    if not origin:
        # درخواست بدون Origin: فرم یا ابزار محلی.
        return True

    # فقط http/https؛ origin دیگری (مثل file://) رد می‌شود.
    match = re.match(
        r"^(https?)://([^/:]+|\[[^\]]+\])(?::\d+)?$",
        origin,
        re.IGNORECASE
    )

    if not match:
        return False

    return match.group(2).lower() in LOCAL_ORIGIN_HOSTS

# نگهداری event مربوط به اجرای فعال Agent
# برای امکان توقف اجرای در حال انجام از رابط وب
current_stop_event = None

# اجرای background: thread فعال، قفل ضد اجرای همزمان
# و آخرین نتیجه. هیچ تغییری در agent.py نیست.
agent_thread = None
agent_running_lock = threading.Lock()
last_result = ""

# تاریخچه گفتگو در SQLite و فقط در لایه web؛
# agent.py هیچ چیز نمی‌داند و هیچ تغییری نمی‌کند.
CHAT_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "chat-history.db"
)
CHAT_HISTORY_LIMIT = 20
CHAT_HISTORY_READ_LIMIT = 5
CHAT_DB_LOCK = threading.Lock()

# ---------------------------------------------------------
# قرارداد حالت اجرا (Review / Apply)
# ---------------------------------------------------------
# دو حالت UI و دو معنی یکتا، بدون حالت سوم:
#
#   "read"  → Review: فقط بررسی/تحلیل/Plan. هیچ mutationی
#             انجام نمی‌شود و هیچ Pending Action قابل اجرایی
#             ساخته نمی‌شود.
#   "write" → Apply: تنها مسیر canonical تغییر فایل در agent.py
#             (Plan → Re-validation → Approval → Execute →
#             Verify).
#
# allow_write فقط از همین نگاشت ساخته می‌شود؛ هیچ مسیر دیگری
# در لایه web آن را نمی‌سازد.
MODE_REVIEW = "read"
MODE_APPLY = "write"


def resolve_allow_write(mode):
    # Fail-Closed: فقط mode == "write" مجوز Apply است.
    return mode == MODE_APPLY


# ---------------------------------------------------------
# Runtime info (فقط لایه web؛ agent.py بی‌خبر است)
# ---------------------------------------------------------
# تنها منبع حقیقت provider/model برای Status Header و
# SQLite. agent.py تغییری نمی‌کند؛ فقط MODEL_URL آن
# خوانده می‌شود.
RUNTIME_PROVIDER = "local"
RUNTIME_MODEL = MODEL_URL

runtime_info = {
    "provider": RUNTIME_PROVIDER,
    "model": RUNTIME_MODEL,
    "started_at": 0.0,
    "finished_at": 0.0,
    "duration": 0.0,
}


def get_runtime_info():
    # کپی برگردانده می‌شود تا caller نتواند منبع را تغییر دهد.
    return dict(runtime_info)


def set_runtime_info(provider=None, model=None):
    if provider:
        runtime_info["provider"] = provider
    if model:
        runtime_info["model"] = model

# ---------------------------------------------------------
# Web-layer status (فقط در لایه web نگه داشته می‌شود)
# ---------------------------------------------------------
# این بخش هیچ رفتاری از agent.py را تغییر نمی‌دهد؛
# فقط قبل و بعد از run_agent وضعیت را به روز می‌کند.
AGENT_STATUS_IDLE = "idle"
AGENT_STATUS_RUNNING = "running"
AGENT_STATUS_DONE = "done"
AGENT_STATUS_ERROR = "error"
AGENT_STATUS_STOPPED = "stopped"

agent_status = AGENT_STATUS_IDLE
agent_status_message = ""
agent_status_updated_at = 0.0

# گزارش مرحله‌ای فقط در لایه web نگه داشته می‌شود.
agent_progress = []


def make_progress_callback():
    # فقط append به لیست؛ agent.py هیچ تغییری در منطق
    # خود ندارد و callback اختیاری است.
    def on_progress(event, data):
        agent_progress.append({
            "event": event,
            "at": time.time(),
            "data": data,
        })

    return on_progress


CHAT_DB_MIGRATIONS = (
    ("provider", "TEXT"),
    ("model", "TEXT"),
    ("started_at", "REAL"),
    ("finished_at", "REAL"),
    ("duration", "REAL"),
)


def init_chat_db():
    # جدول از ابتدا ساخته می‌شود تا endpoint خواندن
    # هیچ‌وقت با خطای «جدول وجود ندارد» مواجه نشود.
    with CHAT_DB_LOCK:
        conn = sqlite3.connect(CHAT_DB_PATH)

        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS chat_turns ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "created_at REAL NOT NULL,"
                "project TEXT,"
                "prompt TEXT,"
                "response TEXT,"
                "state TEXT,"
                "provider TEXT,"
                "model TEXT,"
                "started_at REAL,"
                "finished_at REAL,"
                "duration REAL"
                ")"
            )

            # Migration ساده و غیرمخرب: فقط ستون‌های
            # نبوده اضافه می‌شوند؛ هیچ داده‌ای حذف یا
            # بازنویسی نمی‌شود.
            existing = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(chat_turns)"
                )
            }

            for name, col_type in CHAT_DB_MIGRATIONS:
                if name in existing:
                    continue
                conn.execute(
                    "ALTER TABLE chat_turns ADD COLUMN "
                    + name + " " + col_type
                )

            conn.commit()

        finally:
            conn.close()


init_chat_db()


def remember_chat_turn(
    project_name,
    prompt,
    response,
    state,
    provider=None,
    model=None,
    started_at=None,
    finished_at=None,
    duration=None,
):
    row = (
        time.time(),
        project_name or "",
        prompt or "",
        strip_think_tags(response) or "",
        state,
        provider if provider is not None else RUNTIME_PROVIDER,
        model if model is not None else RUNTIME_MODEL,
        started_at,
        finished_at,
        duration,
    )
    with CHAT_DB_LOCK:
        conn = sqlite3.connect(CHAT_DB_PATH)

        try:
            conn.execute(
                "INSERT INTO chat_turns ("
                "created_at, project, prompt, response,"
                "state, provider, model,"
                "started_at, finished_at, duration"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row
            )
            conn.commit()
            conn.execute(
                "DELETE FROM chat_turns WHERE id NOT IN ("
                "SELECT id FROM chat_turns"
                " ORDER BY id DESC LIMIT ?)",
                (CHAT_HISTORY_LIMIT,)
            )
            conn.commit()

        finally:
            conn.close()


def read_chat_history(limit=CHAT_HISTORY_READ_LIMIT):
    with CHAT_DB_LOCK:
        conn = sqlite3.connect(CHAT_DB_PATH)

        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, created_at, project, prompt,"
                "response, state, provider, model,"
                "started_at, finished_at, duration"
                " FROM chat_turns"
                " ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),)
            ).fetchall()

        finally:
            conn.close()

    items = []
    for row in rows:
        items.append({
            "id": row["id"],
            "time": row["created_at"],
            "project": row["project"] or "",
            "prompt": row["prompt"] or "",
            "response": row["response"] or "",
            "state": row["state"] or "",
            "provider": row["provider"] or "",
            "model": row["model"] or "",
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "duration": row["duration"],
        })

    items.reverse()
    return items


def set_agent_status(state, message=""):
    # فقط در لایه web؛ agent.py دست‌نخورده می‌ماند.
    global agent_status, agent_status_message, agent_status_updated_at
    agent_status = state
    agent_status_message = message
    agent_status_updated_at = time.time()


def get_agent_status():
    return {
        "status": agent_status,
        "message": agent_status_message,
        "updated_at": agent_status_updated_at,
    }


# فقط فیلتر نمایش در لایه web.
# هیچ تغییری در agent.py و معماری Agentic Loop ایجاد نمی‌شود.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think_tags(text):
    if not isinstance(text, str):
        return text
    return _THINK_RE.sub("", text).strip()


def run_agent_with_status(
    user_request,
    project_name,
    allow_write=False,
    stop_event=None,
    progress=None,
):
    # Wrapper در لایه web: قبل از run_agent running،
    # بعد از آن done/error. run_agent خودش حذف نمی‌شود.
    set_agent_status(
        AGENT_STATUS_RUNNING,
        "در حال اجرای درخواست...",
    )
    try:
        result = run_agent(
            user_request,
            project_name=project_name,
            allow_write=allow_write,
            stop_event=stop_event,
            progress=progress,
        )
        if stop_event is not None and stop_event.is_set():
            set_agent_status(
                AGENT_STATUS_STOPPED,
                "درخواست توسط کاربر متوقف شد.",
            )
            return result
        set_agent_status(AGENT_STATUS_DONE, "اجرا کامل شد.")
        return result
    except Exception as e:
        set_agent_status(AGENT_STATUS_ERROR, "خطا: " + str(e))
        raise


def is_agent_running():
    thread = agent_thread
    return bool(thread and thread.is_alive())


def run_agent_background(
    request_text,
    project_name,
    allow_write=False,
    stop_event=None,
):
    global last_result, current_stop_event
    started_at = time.time()
    runtime_info["started_at"] = started_at

    try:
        last_result = run_agent_with_status(
            request_text,
            project_name=project_name,
            allow_write=allow_write,
            stop_event=stop_event,
            progress=make_progress_callback(),
        )

    except Exception as e:

        last_result = "خطای Agent:\n\n" + str(e)
        set_agent_status(
            AGENT_STATUS_ERROR,
            "خطا: " + str(e),
        )

    finally:
        finished_at = time.time()
        duration = max(0.0, finished_at - started_at)
        runtime_info["finished_at"] = finished_at
        runtime_info["duration"] = duration
        remember_chat_turn(
            project_name,
            request_text,
            last_result,
            agent_status,
            started_at=started_at,
            finished_at=finished_at,
            duration=duration,
        )
        if current_stop_event is stop_event:
            current_stop_event = None


@app.before_request
def handle_cors_preflight():
    # Phase 1 — Web Security.
    #
    # Preflight فقط از Originهای localhost پذیرفته می‌شود.
    # پیش از این تغییر هر Originی با Access-Control-Allow-Origin: *
    # می‌توانست state-changing routeها را cross-site صدا بزند.
    origin = request.headers.get("Origin", "")
    if request.method == "OPTIONS":
        if not is_local_origin(origin):
            resp = app.make_response(("", 403))
            return resp
        resp = app.make_response(("", 204))
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = (
            "GET, POST, OPTIONS"
        )
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp


# ---------------------------------------------------------
# Approval actions (فقط لایه web)
# ---------------------------------------------------------
# دکمه‌های «تأیید» / «لغو» دقیقاً همان مسیر امن موجود
# را اجرا می‌کنند: handle_pending_action_request با همان
# کلیدواژه‌ای که کاربر دستی می‌فرستد («تأیید» / «لغو»).
#
# هیچ مسیر Write جدیدی ساخته نمی‌شود و هیچ Approval،
# Emergency Stop، Re-validation یا Manual/Autonomous
# منطقی تغییر نمی‌کند؛ فقط درخواست به‌جای ورود به
# Agentic Loop، مستقیم به Gate تأیید route می‌شود.
APPROVAL_ACTION_TEXTS = {
    "approve": "تأیید",
    "cancel": "لغو",
}


def resolve_approval_action(value):
    # فقط action های صریح پذیرفته می‌شوند.
    # ورودی ناشناخته یعنی «درخواست عادی Agent».
    if not isinstance(value, str):
        return None

    return APPROVAL_ACTION_TEXTS.get(value.strip().lower())


def run_approval_background(
    control_text,
    project_name,
    allow_write=False,
    stop_event=None,
):
    # اجرای همان Gate تأیید موجود در agent.py.
    # خروجی None یعنی «Pending Action وجود ندارد»؛ در آن
    # حالت هیچ درخواستی وارد Loop نمی‌شود و فقط پیام
    # امن نمایش داده می‌شود.
    global last_result, current_stop_event
    started_at = time.time()
    runtime_info["started_at"] = started_at

    try:
        set_agent_status(
            AGENT_STATUS_RUNNING,
            "در حال اجرای تأیید Plan...",
        )

        last_result = handle_pending_action_request(
            control_text,
            allow_write=allow_write,
            stop_event=stop_event,
            # Phase I — Project Context این درخواست به Gate
            # داده می‌شود تا Approval صرفِ Planِ همان پروژه
            # بشود، نه هر Planِ موجود در Slot سراسری.
            expected_project=project_name,
        )

        if last_result is None:
            last_result = (
                "Plan در انتظار تأیید وجود ندارد؛ "
                "هیچ تغییری انجام نشد."
            )

        if stop_event is not None and stop_event.is_set():
            set_agent_status(
                AGENT_STATUS_STOPPED,
                "درخواست توسط کاربر متوقف شد.",
            )
        else:
            set_agent_status(AGENT_STATUS_DONE, "اجرا کامل شد.")

    except Exception as e:

        last_result = "خطای Agent:\n\n" + str(e)
        set_agent_status(
            AGENT_STATUS_ERROR,
            "خطا: " + str(e),
        )

    finally:
        finished_at = time.time()
        duration = max(0.0, finished_at - started_at)
        runtime_info["finished_at"] = finished_at
        runtime_info["duration"] = duration
        remember_chat_turn(
            project_name,
            control_text,
            last_result,
            agent_status,
            started_at=started_at,
            finished_at=finished_at,
            duration=duration,
        )
        if current_stop_event is stop_event:
            current_stop_event = None


def has_pending_approval():
    # فقط خواندن وضعیت Pending Action برای نمایش دکمه‌ها.
    # هیچ تغییری در agent.py و هیچ Write ایجاد نمی‌شود.
    return get_pending_action() is not None


@app.after_request
def add_cors_headers(response):
    # Phase 1 — Web Security.
    #
    # پیش از این تغییر این هدر روی همهٔ پاسخ‌ها با مقدار
    # «*» می‌رفت؛ یعنی هر صفحهٔ وبِ باز می‌توانست
    # POST /multi-write را cross-site بفرستد و داخل
    # پروژه‌های ثبت‌شده فایل بنویسد.
    #
    # حالا هدر فقط وقتی ارسال می‌شود که Origin واقعاً
    # localhost باشد. درخواست‌های بدون Origin (همان
    # حالت معمولِ مرورگر برای فرم‌های هم‌مبدأ) بدون
    # این هدر برگشت می‌آیند و رفتار عادی Localhost-first
    # حفظ می‌شود.
    origin = request.headers.get("Origin", "")

    if is_local_origin(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = (
            "GET, POST, OPTIONS"
        )
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type"
        )

    return response


def build_diff_view(write_info):
    # نتیجه داخلی آخرین Write (از
    # agent.get_last_write_result) به وضعیت
    # قابل نمایش در UI تبدیل می‌شود.
    if not write_info:
        return "", ""

    diff = write_info.get("diff")

    # فایل جدید بوده است؛ Diff قبلی وجود ندارد.
    # (compute_write_diff وقتی فایل وجود ندارد
    # مقدار None برمی‌گرداند)
    if diff is None:
        return "", "new_file"

    # فایل موجود بوده ولی محتوایش تغییری نکرده است.
    if diff == "":
        return "", "unchanged"

    return diff, "changed"


def build_validation_view(write_info):
    # نتیجه Validation آخرین Write (از
    # agent.get_last_write_result) به وضعیت
    # قابل نمایش در UI تبدیل می‌شود.
    # این بخش فقط گزارش است و هیچ تغییری در
    # فایل‌ها ایجاد نمی‌کند.
    if not write_info:
        return "", ""

    validation = write_info.get("validation")

    if not validation:
        return "", ""

    lines = [
        "valid: " + str(validation.get("valid")),
        "exists: " + str(validation.get("exists")),
        "size: " + str(validation.get("size")),
        "content_matches: " + str(
            validation.get("content_matches")
        ),
        "syntax_checked: " + str(
            validation.get("syntax_checked")
        ),
        "syntax_valid: " + str(
            validation.get("syntax_valid")
        )
    ]

    error = validation.get("error")

    if error:
        lines.append("")
        lines.append("error:")
        lines.append(str(error))

    state = (
        "valid"
        if validation.get("valid")
        else "invalid"
    )

    return "\n".join(lines), state

HTML = """
<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Local Coding Agent</title>

<style>
* { box-sizing: border-box; }

body {
    margin: 0;
    min-height: 100vh;
    font-family: Tahoma, Arial, sans-serif;
    background: #f3f5f8;
    color: #172033;
    display: flex;
    justify-content: center;
    align-items: center;
}

.app {
    width: min(920px, 92%);
    background: #fff;
    border: 1px solid #e1e5eb;
    border-radius: 18px;
    box-shadow: 0 15px 45px rgba(0,0,0,.08);
    overflow: hidden;
}

.header {
    padding: 24px 28px;
    background: #155eef;
    color: white;
}

.header h1 {
    margin: 0 0 7px;
    font-size: 23px;
}

.header p {
    margin: 0;
    opacity: .9;
    font-size: 14px;
}

.content {
    padding: 28px;
}

label {
    display: block;
    margin-bottom: 10px;
    font-weight: bold;
}

select,
textarea {
    width: 100%;
    border: 1px solid #d7dce3;
    border-radius: 12px;
    padding: 13px 15px;
    font: inherit;
    outline: none;
    direction: rtl;
    text-align: right;
    background: #fff;
}

select {
    cursor: pointer;
}

textarea {
    min-height: 170px;
    resize: vertical;
    line-height: 1.9;
}

select:focus,
textarea:focus {
    border-color: #155eef;
    box-shadow: 0 0 0 3px rgba(21,94,239,.1);
}

.project-row {
    display: flex;
    gap: 10px;
    margin-bottom: 20px;
}

.project-row select {
    flex: 1;
}

.small-button {
    width: auto;
    margin: 0;
    padding: 0 18px;
}

.remove-button {
    background: #fff;
    color: #c62828;
    border: 1px solid #e2bcbc;
}

.remove-button:hover {
    background: #fff4f4;
}

.mode-box {
    margin-bottom: 20px;
}

.mode-options {
    display: flex;
    gap: 12px;
}

.mode-option {
    flex: 1;
    position: relative;
}

.mode-option input {
    position: absolute;
    opacity: 0;
}

.mode-option label {
    margin: 0;
    padding: 13px 15px;
    border: 1px solid #d7dce3;
    border-radius: 12px;
    text-align: center;
    cursor: pointer;
    background: #fff;
    font-weight: normal;
}

.mode-option input:checked + label {
    border-color: #155eef;
    background: #eef4ff;
    color: #155eef;
    font-weight: bold;
}

button {
    margin-top: 16px;
    width: 100%;
    border: 0;
    border-radius: 10px;
    padding: 14px;
    background: #155eef;
    color: white;
    font: inherit;
    font-weight: bold;
    cursor: pointer;
}

button:hover {
    background: #0f4dcc;
}

.status {
    margin-top: 22px;
    padding: 12px 15px;
    border-radius: 10px;
    background: #eef4ff;
    color: #155eef;
    font-size: 14px;
}

.status-badge {
    display: inline-block;
    margin-left: 10px;
    padding: 2px 10px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: bold;
}

.status-running {
    background: #fff3cd;
    color: #856404;
}

.status-done {
    background: #d4edda;
    color: #155724;
}

.status-error {
    background: #f8d7da;
    color: #721c24;
}

.status-stopped {
    background: #e2e3e5;
    color: #383d41;
}

.status-idle {
    background: #eef4ff;
    color: #155eef;
}

.progress-box {
    margin-top: 18px;
    border: 1px solid #e2e5ea;
    border-radius: 12px;
    overflow: hidden;
}

.progress-list {
    position: relative;
    display: flex;
    flex-direction: row;
    align-items: stretch;
    gap: 0;
    padding: 14px 15px 16px 15px;
    background: #f7f8fa;
    overflow-x: auto;
    overflow-y: hidden;
    direction: rtl;
    scroll-behavior: smooth;
}

.timeline-item {
    position: relative;
    display: flex;
    flex: 0 0 200px;
    flex-direction: column;
    gap: 7px;
    padding: 0 10px;
    direction: rtl;
    text-align: right;
}

.timeline-dot {
    flex: 0 0 auto;
    align-self: flex-start;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: #c3ccd8;
    box-shadow: 0 0 0 3px #eef1f5;
}

.timeline-item::before {
    content: "";
    position: absolute;
    top: 5px;
    right: -10px;
    width: 20px;
    height: 2px;
    background: #e2e5ea;
}

.timeline-item:last-child::before {
    display: none;
}

.timeline-card {
    flex: 1 1 auto;
    min-width: 0;
    height: 100%;
    padding: 9px 12px;
    background: #fff;
    border: 1px solid #e2e5ea;
    border-radius: 10px;
}

.timeline-title {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    font-size: 13px;
    font-weight: bold;
    color: #172033;
}

.timeline-step {
    flex: 0 0 auto;
    padding: 1px 8px;
    border-radius: 20px;
    background: #eef4ff;
    color: #155eef;
    font-size: 11px;
    font-weight: bold;
    direction: rtl;
}

.timeline-tool {
    direction: ltr;
    text-align: left;
    font-family: Consolas, "Courier New", monospace;
    font-size: 12px;
    color: #3c4553;
    unicode-bidi: isolate;
    overflow-wrap: anywhere;
}

.timeline-item-ok .timeline-dot {
    background: #1f9d55;
    box-shadow: 0 0 0 3px #e3f5ea;
}

.timeline-item-bad .timeline-dot {
    background: #c62828;
    box-shadow: 0 0 0 3px #fbeaea;
}

.timeline-item-wait .timeline-dot {
    background: #d79b00;
    box-shadow: 0 0 0 3px #fdf3dc;
}

.timeline-item-latest .timeline-card {
    border-color: #155eef;
    background: #f5f8ff;
    box-shadow: 0 4px 14px rgba(21,94,239,.12);
}

.timeline-item-latest .timeline-dot {
    background: #155eef;
    box-shadow: 0 0 0 3px #e3ecff;
}

.timeline-meta {
    margin-top: 4px;
    font-size: 11px;
    color: #6b7684;
    direction: ltr;
    text-align: left;
    overflow-wrap: anywhere;
}

.progress-list-hint {
    margin-top: 8px;
    font-size: 11px;
    color: #6b7684;
    direction: rtl;
    text-align: right;
}

.agent-header {
    margin-top: 18px;
    padding: 14px 16px;
    background: #fff;
    border: 1px solid #e2e5ea;
    border-right: 4px solid #c3ccd8;
    border-radius: 12px;
    direction: rtl;
    text-align: right;
}

.agent-header-title {
    font-size: 14px;
    font-weight: bold;
    color: #172033;
    margin-bottom: 9px;
}

.agent-header-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 3px 0;
    font-size: 13px;
}

.agent-header-label {
    color: #6b7684;
    white-space: nowrap;
}

.agent-header-value {
    font-weight: bold;
    color: #172033;
}

.agent-header-ltr {
    direction: ltr;
    text-align: left;
    font-family: Consolas, "Courier New", monospace;
    unicode-bidi: isolate;
}

.agent-header-running {
    border-right-color: #d79b00;
    background: #fffdf6;
}

.agent-header-running .agent-header-value#agentState {
    color: #856404;
}

.agent-header-done {
    border-right-color: #1f9d55;
    background: #f6fcf8;
}

.agent-header-done .agent-header-value#agentState {
    color: #0f7b3f;
}

.agent-header-stopped {
    border-right-color: #7a828c;
    background: #f8f9fa;
}

.agent-header-error {
    border-right-color: #c62828;
    background: #fdf6f6;
}

.agent-header-error .agent-header-value#agentState {
    color: #b42318;
}

.result-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 18px;
    padding: 11px 15px;
    background: #eef4ff;
    color: #155eef;
    border: 1px solid #e2e5ea;
    border-bottom: 0;
    border-radius: 12px 12px 0 0;
    font-size: 14px;
    font-weight: bold;
}

.result-head + .result {
    margin-top: 0;
    border-radius: 0 0 12px 12px;
}

.result-head button {
    margin: 0;
    width: auto;
    padding: 7px 14px;
    font-size: 13px;
    background: #fff;
    color: #155eef;
    border: 1px solid #b9cdf7;
}

.result-head button:hover {
    background: #eef4ff;
}

.chat-history-box {
    margin-top: 18px;
    border: 1px solid #e2e5ea;
    border-radius: 12px;
    overflow: hidden;
}

.chat-item {
    padding: 12px 15px;
    border-top: 1px solid #e2e5ea;
    background: #f7f8fa;
}

.chat-item-head {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: #6b7684;
    direction: rtl;
}

.chat-role {
    padding: 1px 8px;
    border-radius: 20px;
    font-weight: bold;
}

.chat-role-user {
    background: #eef4ff;
    color: #155eef;
}

.chat-role-agent {
    background: #edf9f0;
    color: #0f7b3f;
}

.chat-state {
    padding: 1px 8px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: bold;
    direction: rtl;
}

.chat-state-done {
    background: #d4edda;
    color: #155724;
}

.chat-state-stopped {
    background: #e2e3e5;
    color: #383d41;
}

.chat-state-error {
    background: #f8d7da;
    color: #721c24;
}

.chat-state-running {
    background: #fff3cd;
    color: #856404;
}

.chat-state-unknown {
    background: #eef4ff;
    color: #155eef;
}

.chat-project {
    direction: ltr;
    unicode-bidi: isolate;
    font-family: Consolas, "Courier New", monospace;
}

.chat-line {
    margin-top: 6px;
    font-size: 13px;
    color: #172033;
    direction: rtl;
    text-align: right;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
}

.chat-meta {
    margin-top: 6px;
    font-size: 11px;
    color: #6b7684;
    direction: ltr;
    text-align: left;
    font-family: Consolas, "Courier New", monospace;
    unicode-bidi: isolate;
    overflow-wrap: anywhere;
}

.chat-copy {
    margin: 0;
    width: auto;
    padding: 4px 12px;
    font-size: 12px;
    font-weight: bold;
    background: #fff;
    color: #155eef;
    border: 1px solid #b9cdf7;
    border-radius: 20px;
    cursor: pointer;
}

.chat-copy:hover {
    background: #eef4ff;
}

.chat-actions {
    display: flex;
    justify-content: flex-start;
    margin-top: 8px;
}

.result {
    margin-top: 18px;
    padding: 18px;
    background: #f7f8fa;
    border: 1px solid #e2e5ea;
    border-radius: 12px;
    white-space: pre-wrap;
    direction: rtl;
    text-align: right;
    line-height: 1.9;
    overflow-wrap: anywhere;
}

.diff-box,
.rollback-box,
.validation-box {
    margin-top: 18px;
    border: 1px solid #e2e5ea;
    border-radius: 12px;
    overflow: hidden;
}

.diff-title {
    padding: 11px 15px;
    background: #eef4ff;
    color: #155eef;
    font-size: 14px;
    font-weight: bold;
}

.diff-text,
.validation-text {
    margin: 0;
    padding: 15px;
    background: #f7f8fa;
    color: #172033;
    border-top: 1px solid #e2e5ea;
    font-family: Consolas, "Courier New", monospace;
    font-size: 13px;
    line-height: 1.6;
    direction: ltr;
    text-align: left;
    white-space: pre;
    overflow-x: auto;
}

.diff-note {
    padding: 13px 15px;
    background: #f7f8fa;
    color: #5b6472;
    font-size: 14px;
}

.rollback-row {
    display: flex;
    gap: 10px;
    padding: 13px 15px;
    background: #f7f8fa;
    border-top: 1px solid #e2e5ea;
}

.rollback-row input {
    flex: 1;
    border: 1px solid #d7dce3;
    border-radius: 10px;
    padding: 11px 13px;
    font: inherit;
    background: #fff;
    outline: none;
    direction: ltr;
    text-align: left;
}

.rollback-row input:focus {
    border-color: #155eef;
    box-shadow: 0 0 0 3px rgba(21,94,239,.1);
}

.rollback-row button {
    margin: 0;
    width: auto;
    padding: 0 18px;
}

.validation-ok {
    padding: 13px 15px;
    background: #edf9f0;
    color: #0f7b3f;
    font-size: 14px;
    border-top: 1px solid #e2e5ea;
}

.plan-list {
    margin: 0;
    padding: 0;
    list-style: none;
    background: #f7f8fa;
    border-top: 1px solid #e2e5ea;
}

.plan-item {
    padding: 11px 15px;
    border-bottom: 1px solid #e2e5ea;
    font-size: 14px;
    direction: ltr;
    text-align: left;
    font-family: Consolas, "Courier New", monospace;
}

.plan-item:last-child {
    border-bottom: 0;
}

.plan-badge {
    display: inline-block;
    margin-left: 8px;
    padding: 1px 8px;
    border-radius: 20px;
    font-size: 11px;
    background: #eef4ff;
    color: #155eef;
}

.approval-box {
    margin-top: 18px;
    border: 1px solid #e2e5ea;
    border-radius: 12px;
    overflow: hidden;
}

.approval-row {
    display: flex;
    gap: 10px;
    padding: 13px 15px;
    background: #f7f8fa;
    border-top: 1px solid #e2e5ea;
}

.approval-row button {
    margin: 0;
    width: auto;
    padding: 0 22px;
}

.approve-button {
    background: #0f7b3f;
    color: #fff;
    border: 1px solid #0b6231;
}

.approve-button:hover {
    background: #0d6a36;
}

.cancel-button {
    background: #fff;
    color: #c62828;
    border: 1px solid #e2bcbc;
}

.cancel-button:hover {
    background: #fff4f4;
}

.approval-row button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
}

.validation-bad {
    padding: 13px 15px;
    background: #fdf0f0;
    color: #b42318;
    font-size: 14px;
    border-top: 1px solid #e2e5ea;
}

@media (max-width: 600px) {
    .content { padding: 20px; }
    .mode-options { flex-direction: column; }
    .project-row { flex-direction: column; }
    .rollback-row { flex-direction: column; }
    .small-button { padding: 13px; }
    .rollback-row button { padding: 13px; }
}
</style>
</head>

<body>

<div class="app">

<div class="header">
    <h1>Local Coding Agent</h1>
    <p>اجرای کاملاً محلی با Qwen2.5-Coder</p>
</div>

<div class="content">

<form method="post" id="agentForm">

<label for="project">پروژه</label>

<div class="project-row">

<select id="project" name="project">
    <option value="general"
        {% if project_name == "general" %}selected{% endif %}>
        General / بدون پروژه
    </option>
{% for name in projects %}
    <option value="{{ name }}"
        {% if project_name == name %}selected{% endif %}>
        {{ name }}
    </option>
{% endfor %}
</select>

<form
    method="post"
    action="/add-project"
    style="display:inline">
    <button
        type="submit"
        class="small-button">
        + افزودن پروژه
    </button>
</form>

{% if project_name != "agent-test" and project_name != "general" %}
<form
    method="post"
    action="/remove-project"
    style="display:inline">
    <input
        type="hidden"
        name="name"
        value="{{ project_name | e }}">
    <button
        type="submit"
        class="small-button remove-button">
        حذف
    </button>
</form>
{% endif %}

</div>

<div class="mode-box">

<label>حالت اجرا</label>

<div class="mode-options">

<div class="mode-option">
<input
    type="radio"
    id="read"
    name="mode"
    value="read"
    {% if mode != "write" %}checked{% endif %}
>
<label for="read">فقط بررسی</label>
</div>

<div class="mode-option">
<input
    type="radio"
    id="write"
    name="mode"
    value="write"
    {% if mode == "write" %}checked{% endif %}
>
<label for="write">اعمال تغییر</label>
</div>

</div>
</div>

<label for="request">چه کاری انجام دهم؟</label>

<textarea
    id="request"
    name="request"
    placeholder="مثلاً: فایل style.css را بررسی کن."
    autofocus
>{{ request_text }}</textarea>

<button type="submit" id="submitButton">
    اجرای درخواست
</button>

<button
    type="button"
    id="stopButton"
    class="small-button"
    style="margin-top: 10px; width: auto; background: #c62828; color: white; border: 1px solid #e2bcbc;"
    onclick="stopAgent()"
    disabled
>
    توقف اجرا
</button>

</form>

{% if running %}
<div class="status">
    Agent در حال انجام درخواست است...
</div>
{% endif %}

{% if status_text %}
<div class="status status-{{ status_class }}">
    {{ status_text }}
    {% if status_badge %}<span class="status-badge">{{ status_badge }}</span>{% endif %}
</div>
{% endif %}

<div class="agent-header" id="agentHeader">
    <div class="agent-header-title">وضعیت Agent</div>
    <div class="agent-header-row">
        <span class="agent-header-label">وضعیت:</span>
        <span class="agent-header-value" id="agentState">—</span>
    </div>
    <div class="agent-header-row">
        <span class="agent-header-label">گام:</span>
        <span class="agent-header-value" id="agentStep">—</span>
    </div>
    <div class="agent-header-row">
        <span class="agent-header-label">ابزار:</span>
        <span class="agent-header-value agent-header-ltr" id="agentTool">—</span>
    </div>
    <div class="agent-header-row">
        <span class="agent-header-label">زمان اجرا:</span>
        <span class="agent-header-value" id="agentElapsed">—</span>
    </div>
    <div class="agent-header-row">
        <span class="agent-header-label">Provider:</span>
        <span class="agent-header-value agent-header-ltr" id="agentProvider">—</span>
    </div>
    <div class="agent-header-row">
        <span class="agent-header-label">Model:</span>
        <span class="agent-header-value agent-header-ltr" id="agentModel">—</span>
    </div>
</div>

<div class="progress-box" id="progressBox" hidden>
    <div class="diff-title">گزارش مرحله‌ای (۱۰ رویداد آخر)</div>
    <div id="progressList"></div>
</div>

<div class="chat-history-box" id="chatHistoryBox">
    <div class="diff-title">تاریخچه گفتگو (۵ گفتگوی آخر از SQLite)</div>
    <div id="chatHistoryList"></div>
</div>

{% if result %}
<div class="result-head">
    <span>نتیجه Agent</span>
    <button
        type="button"
        class="small-button"
        id="copyButton"
        onclick="copyResult()"
    >کپی نتیجه</button>
</div>
<div class="result" id="resultBox">{{ result }}</div>
{% endif %}

{% if file_plan %}
<div class="diff-box">
    <div class="diff-title">Plan اجرای درخواست</div>
    <ul class="plan-list">
    {% for item in file_plan.files %}
        <li class="plan-item">
            {{ item.relative_path | e }}
            <span class="plan-badge">
                {% if item.exists %}modify / موجود{% else %}create / جدید{% endif %}
            </span>
        </li>
    {% endfor %}
    </ul>
    <div class="diff-note">
        در این مرحله فقط Plan نمایش داده می‌شود؛
        هیچ Write چندفایلی انجام نمی‌شود.
    </div>
</div>
{% endif %}

{% if approval_pending %}
<div class="approval-box" id="approvalBox">
    <div class="diff-title">تصمیم درباره Plan</div>

    <form
        method="post"
        action="/"
        class="approval-row"
        id="approvalForm">

        <input
            type="hidden"
            name="project"
            value="{{ project_name | e }}"
        >

        <input
            type="hidden"
            name="mode"
            value="{{ mode }}"
        >

        <button
            type="submit"
            name="approval_action"
            value="approve"
            class="approve-button"
        >تأیید</button>

        <button
            type="submit"
            name="approval_action"
            value="cancel"
            class="cancel-button"
        >لغو</button>

    </form>

    <div class="diff-note">
        «تأیید» و «لغو» دقیقاً معادل فرمان متنی
        «تأیید» / «لغو» هستند و از همان مسیر امن Approval
        عبور می‌کنند.
    </div>
</div>
{% endif %}

{% if diff_state == "changed" %}
<div class="diff-box">
    <div class="diff-title">{{ diff_title }}</div>
    <pre class="diff-text">{{ diff_text | e }}</pre>
</div>
{% elif diff_state == "unchanged" %}
<div class="diff-box">
    <div class="diff-title">{{ diff_title }}</div>
    <div class="diff-note">
        فایل موجود بود ولی محتوای آن تغییری نکرد (changed = False)؛
        بنابراین Diff خالی است.
    </div>
</div>
{% elif diff_state == "new_file" %}
<div class="diff-box">
    <div class="diff-title">{{ diff_title }}</div>
    <div class="diff-note">
        فایل جدید ایجاد شد؛ Diff قبلی وجود ندارد.
    </div>
</div>
{% endif %}

{% if validation_state == "valid" %}
<div class="validation-box">
    <div class="diff-title">Validation بعد از Write</div>
    <div class="validation-ok">
        فایل نوشته‌شده معتبر است.
    </div>
    <pre class="validation-text">{{ validation_text | e }}</pre>
    <div class="diff-note">
        Validation فقط بررسی می‌کند؛ هیچ فایلی را تغییر نمی‌دهد
        و Rollback خودکار انجام نمی‌دهد.
    </div>
</div>
{% elif validation_state == "invalid" %}
<div class="validation-box">
    <div class="diff-title">Validation بعد از Write</div>
    <div class="validation-bad">
        Validation ناموفق بود؛ تصمیم درباره ادامه کار یا
        بازگردانی فایل با شماست.
    </div>
    <pre class="validation-text">{{ validation_text | e }}</pre>
    <div class="diff-note">
        Validation فقط بررسی می‌کند؛ هیچ فایلی را تغییر نمی‌دهد
        و Rollback خودکار انجام نمی‌دهد.
    </div>
</div>
{% endif %}

<div class="rollback-box">
    <div class="diff-title">بازگردانی از Backup (Rollback)</div>

    <form
        method="post"
        action="/rollback"
        class="rollback-row">

        <input
            type="hidden"
            name="project"
            value="{{ project_name | e }}"
        >

        <input
            type="hidden"
            name="mode"
            value="{{ mode }}"
        >

        <input
            type="text"
            name="file"
            value="{{ rollback_target | e }}"
            placeholder="ai-agent/backup-test.txt"
            required
        >

        <button type="submit">بازگردانی</button>

    </form>

    <div class="diff-note">
        فقط برای فایل‌هایی که کنارشان فایل .agent-backup وجود دارد.
        Rollback هیچ‌وقت خودکار اجرا نمی‌شود و فقط با این درخواست
        انجام می‌شود.
    </div>
</div>

</div>
</div>

<script>
const form = document.getElementById("agentForm");
const button = document.getElementById("submitButton");
const stopButton = document.getElementById("stopButton");

form.addEventListener("submit", function() {
    button.disabled = true;
    button.textContent = "در حال اجرا...";
    stopButton.disabled = false;
});

// Enter ارسال می‌کند، Shift+Enter خط جدید می‌سازد.
// فقط همین textarea؛ submit دکمه و Enter همچنان کار می‌کند.
document.getElementById("request").addEventListener("keydown", function(e) {
    if (e.key !== "Enter") return;
    if (e.shiftKey) return;
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    if (!this.value.trim()) return;
    e.preventDefault();
    form.requestSubmit();
});

// دکمه‌های تأیید / لغو: بعد از کلیک غیرفعال می‌شوند تا
// یک Plan دوبار اجرا یا دوبار لغو نشود.
// هیچ درخواست جدیدی ساخته نمی‌شود؛ همان form موجود به
// همان endpoint "/" با action مشخص ارسال می‌شود.
var approvalForm = document.getElementById("approvalForm");

if (approvalForm) {
    approvalForm.addEventListener("submit", function() {
        var buttons = approvalForm.querySelectorAll("button");
        for (var i = 0; i < buttons.length; i++) {
            buttons[i].disabled = true;
        }
        if (stopButton) stopButton.disabled = false;
    });
}

// نمایش/پنهان‌سازی کادر Approval فقط از داده‌های موجود
// در /status خوانده می‌شود (pending_approval). این تابع
// هیچ Write یا اجرایی انجام نمی‌دهد.
function renderApprovalBox(data) {
    var box = document.getElementById("approvalBox");
    if (!box) return;
    var pending = !!data.pending_approval;
    box.style.display = pending ? "" : "none";
    if (!pending && approvalForm) {
        var buttons = approvalForm.querySelectorAll("button");
        for (var i = 0; i < buttons.length; i++) {
            buttons[i].disabled = false;
        }
    }
}

function stopAgent() {
    fetch("/stop", {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        }
    })
    .then(function(response) {
        return response.json();
    })
    .then(function(data) {
        if (data.success) {
            stopButton.disabled = true;
            button.disabled = false;
            button.textContent = "درخواست متوقف شد";
        }
    })
    .catch(function() {
        stopButton.disabled = true;
        button.disabled = false;
        button.textContent = "درخواست متوقف شد";
    });
}

// Poll /status every 2 seconds to update the status badge
// (فقط در لایه web؛ هیچ تغییری در agent.py ایجاد نمی‌شود).
function pollStatus() {
    fetch("/status")
        .then(function(r) { return r.json(); })
        .then(function(data) {
            renderAgentHeader(data);
            renderProgress(data.progress || []);
            renderLastResult(data);
            renderApprovalBox(data);
            pollChatHistory();
            var box = document.querySelector(".status");
            if (!box) return;
            var state = data.status;
            box.className = "status status-" + state;
            var badgeMap = {
                running: "در حال اجرا",
                done: "کامل شد",
                error: "خطا",
                stopped: "متوقف شد",
                idle: ""
            };
            var badge = badgeMap[state] || "";
            var badgeHtml = badge
                ? '<span class="status-badge">' + badge + "</span>"
                : "";
            box.innerHTML = (data.message || "") + badgeHtml;
        })
        .catch(function() {});
}

// فقط خواندن last_result موجود در /status؛ هیچ thread یا
// async جدیدی ساخته نمی‌شود و agent.py دست‌نخورده است.
function renderLastResult(data) {
    var state = data.status;
    if (
        state !== "done"
        && state !== "error"
        && state !== "stopped"
    ) return;
    var text = data.last_result || "";
    if (!text) return;
    var box = document.querySelector(".result");
    if (!box) {
        var content = document.querySelector(".content");
        if (!content) return;
        var head = document.createElement("div");
        head.className = "result-head";
        head.appendChild(document.createTextNode("نتیجه Agent"));
        var copy = document.createElement("button");
        copy.type = "button";
        copy.className = "small-button";
        copy.id = "copyButton";
        copy.textContent = "کپی نتیجه";
        copy.addEventListener("click", copyResult);
        head.appendChild(copy);
        box = document.createElement("div");
        box.className = "result";
        box.id = "resultBox";
        content.appendChild(head);
        content.appendChild(box);
    }
    box.textContent = text;
}

// کپی متن نتیجه با Clipboard API؛ فقط textContent تغییر می‌کند.
function copyResult() {
    var box = document.getElementById("resultBox");
    var btn = document.getElementById("copyButton");
    if (!box || !btn) return;
    copyText(box.textContent || "", btn);
}

// هسته مشترک کپی: دکمه اصلی نتیجه و دکمه کپی پاسخ
// هر دو از همین تابع استفاده می‌کنند.
function copyText(text, btn) {
    if (!text) return;
    var done = function() {
        btn.textContent = "کپی شد ✓";
        setTimeout(function() {
            btn.textContent = btn === document.getElementById("copyButton")
                ? "کپی نتیجه"
                : "کپی پاسخ";
        }, 2000);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function() {
            btn.textContent = "کپی ناموفق";
        });
        return;
    }
    var area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    try {
        document.execCommand("copy");
        done();
    } catch (e) {
        btn.textContent = "کپی ناموفق";
    }
    document.body.removeChild(area);
}

// فقط خواندن داده‌های موجود در /status؛ هیچ درخواست جدیدی
// و هیچ تغییری در background thread و API انجام نمی‌شود.
function renderAgentHeader(data) {
    var header = document.getElementById("agentHeader");
    if (!header) return;
    var state = document.getElementById("agentState");
    var step = document.getElementById("agentStep");
    var tool = document.getElementById("agentTool");
    var elapsed = document.getElementById("agentElapsed");
    var provider = document.getElementById("agentProvider");
    var model = document.getElementById("agentModel");
    if (!state || !step || !tool || !elapsed
        || !provider || !model) return;

    provider.textContent = data.provider || "—";
    model.textContent = data.model || "—";

    var stateMap = {
        running: "در حال اجرا",
        done: "کامل شد",
        stopped: "متوقف شد",
        error: "خطا",
        idle: "بیکار"
    };
    var classMap = {
        running: "agent-header-running",
        done: "agent-header-done",
        stopped: "agent-header-stopped",
        error: "agent-header-error"
    };
    var key = data.status || "idle";
    state.textContent = stateMap[key] || key;
    header.className = "agent-header " + (classMap[key] || "");

    var events = data.progress || [];
    var lastStep = "";
    var lastTool = "";
    events.forEach(function(ev) {
        var d = ev.data || {};
        if (d.step) lastStep = d.step;
        if (d.tool) lastTool = d.tool;
    });
    step.textContent = lastStep ? String(lastStep) : "—";
    tool.textContent = lastTool || "—";

    if (!events.length) {
        elapsed.textContent = "—";
        return;
    }
    var first = events[0].at || 0;
    var last = events[events.length - 1].at || 0;
    var seconds = Math.max(0, Math.round(last - first));
    var minutes = Math.floor(seconds / 60);
    var rest = seconds % 60;
    elapsed.textContent = minutes
        ? minutes + " دقیقه و " + rest + " ثانیه"
        : rest + " ثانیه";
}

// تاریخچه از /chat-history جداگانه خوانده می‌شود؛
// /status دست‌نخورده است.
var lastHistoryId = -1;

function pollChatHistory() {
    fetch("/chat-history")
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var history = data.history || [];
            // با SQLite، تعداد ردیف‌ها ثابت است؛
            // مقایسه روی شناسه آخرین رکورد انجام می‌شود.
            var lastId = history.length
                ? history[history.length - 1].id
                : 0;
            if (lastId === lastHistoryId) return;
            lastHistoryId = lastId;
            renderChatHistory(history);
        })
        .catch(function() {});
}

function renderChatHistory(history) {
    var box = document.getElementById("chatHistoryBox");
    var list = document.getElementById("chatHistoryList");
    if (!box || !list) return;
    list.innerHTML = "";
    var recent = history.slice(-5);
    // جدیدترین گفتگو بالای فهرست نمایش داده می‌شود.
    recent.reverse();
    if (!recent.length) {
        box.hidden = true;
        return;
    }
    recent.forEach(function(item) {
        var wrap = document.createElement("div");
        wrap.className = "chat-item";
        var head = document.createElement("div");
        head.className = "chat-item-head";
        var role = document.createElement("span");
        role.className = "chat-role chat-role-user";
        role.textContent = "کاربر";
        var project = document.createElement("span");
        project.className = "chat-project";
        project.textContent = item.project || "";
        var time = document.createElement("span");
        time.textContent = formatChatTime(item.time);
        head.appendChild(role);
        head.appendChild(project);
        head.appendChild(time);
        wrap.appendChild(head);
        var meta = document.createElement("div");
        meta.className = "chat-meta";
        meta.textContent = "provider: " + (item.provider || "—")
            + "  |  model: " + (item.model || "—")
            + "  |  مدت: " + formatChatDuration(item.duration);
        wrap.appendChild(meta);
        var prompt = document.createElement("div");
        prompt.className = "chat-line";
        prompt.textContent = item.prompt || "";
        wrap.appendChild(prompt);
        var agentRole = document.createElement("span");
        agentRole.className = "chat-role chat-role-agent";
        agentRole.textContent = "Agent";
        var state = document.createElement("span");
        state.className = "chat-state " + chatStateClass(item.state);
        state.textContent = chatStateLabel(item.state);
        wrap.appendChild(agentRole);
        wrap.appendChild(state);
        var agentLine = document.createElement("div");
        agentLine.className = "chat-line";
        agentLine.textContent = item.response || "";
        wrap.appendChild(agentLine);

        var actions = document.createElement("div");
        actions.className = "chat-actions";
        var copyBtn = document.createElement("button");
        copyBtn.type = "button";
        copyBtn.className = "chat-copy";
        copyBtn.textContent = "کپی پاسخ";
        // فقط response همان آیتم کپی می‌شود.
        copyBtn.addEventListener("click", function(e) {
            copyText(item.response || "", e.currentTarget);
        });
        actions.appendChild(copyBtn);
        wrap.appendChild(actions);

        list.appendChild(wrap);
    });
    box.hidden = false;
}

function formatChatDuration(value) {
    if (value === null || value === undefined) return "—";
    var seconds = Math.max(0, Math.round(value));
    var minutes = Math.floor(seconds / 60);
    var rest = seconds % 60;
    return minutes
        ? minutes + " دقیقه و " + rest + " ثانیه"
        : rest + " ثانیه";
}

function chatStateLabel(value) {
    if (value === "done") return "کامل شد";
    if (value === "stopped") return "متوقف شد";
    if (value === "error") return "خطا";
    if (value === "running") return "در حال اجرا";
    return "نامشخص";
}

function chatStateClass(value) {
    if (value === "done") return "chat-state-done";
    if (value === "stopped") return "chat-state-stopped";
    if (value === "error") return "chat-state-error";
    if (value === "running") return "chat-state-running";
    return "chat-state-unknown";
}

function formatChatTime(value) {
    if (!value) return "";
    var d = new Date(value * 1000);
    return d.toLocaleString("fa-IR");
}

// فقط خواندن progress موجود در /status؛ هیچ thread یا
// async جدیدی ساخته نمی‌شود و agent.py دست‌نخورده است.
function renderProgress(events) {
    var wrap = document.getElementById("progressBox");
    var list = document.getElementById("progressList");
    if (!wrap || !list) return;
    list.innerHTML = "";
    if (!events.length) {
        wrap.hidden = true;
        return;
    }
    var recent = events.slice(-10);
    var lastIndex = recent.length - 1;
    recent.forEach(function(ev, index) {
        var info = describeProgress(ev);
        var row = document.createElement("div");
        row.className = "timeline-item"
            + " " + info.cssClass
            + (index === lastIndex ? " timeline-item-latest" : "");
        var dot = document.createElement("span");
        dot.className = "timeline-dot";
        var card = document.createElement("div");
        card.className = "timeline-card";
        var title = document.createElement("div");
        title.className = "timeline-title";
        var step = document.createElement("span");
        step.className = "timeline-step";
        step.textContent = "گام " + (info.step || "-");
        title.appendChild(step);
        title.appendChild(document.createTextNode(info.title));
        card.appendChild(title);
        if (info.tool) {
            var tool = document.createElement("div");
            tool.className = "timeline-tool";
            tool.textContent = info.tool;
            card.appendChild(tool);
        }
        if (info.meta) {
            var meta = document.createElement("div");
            meta.className = "timeline-meta";
            meta.textContent = info.meta;
            card.appendChild(meta);
        }
        row.appendChild(dot);
        row.appendChild(card);
        list.appendChild(row);
    });
    wrap.hidden = false;
    // در چیدمان افقی، آخرین کارت سمت چپ است؛ برای دیدن
    // آن به انتهای نوار اسکرول می‌رویم.
    list.scrollLeft = 0;
    if (list.scrollWidth > list.clientWidth) {
        list.scrollLeft = -list.scrollWidth;
    }
}

function describeProgress(ev) {
    var d = ev.data || {};
    if (ev.event === "step_start") {
        return {
            step: d.step,
            title: "شروع گام جدید",
            meta: "سقف گام‌ها: " + d.max_steps,
            cssClass: ""
        };
    }
    if (ev.event === "decision") {
        if (d.tool) {
            return {
                step: d.step,
                title: "انتخاب ابزار",
                tool: d.tool,
                cssClass: ""
            };
        }
        return {
            step: d.step,
            title: "تولید پاسخ نهایی",
            cssClass: "timeline-item-ok"
        };
    }
    if (ev.event === "tool_done") {
        return {
            step: d.step,
            title: d.ok ? "اجرای ابزار → موفق" : "اجرای ابزار → ناموفق",
            tool: d.tool,
            cssClass: d.ok
                ? "timeline-item-ok"
                : "timeline-item-bad"
        };
    }
    if (ev.event === "awaiting_approval") {
        return {
            step: d.step,
            title: "منتظر تأیید کاربر",
            tool: d.tool,
            cssClass: "timeline-item-wait"
        };
    }
    return {
        step: (d && d.step) || "",
        title: ev.event,
        cssClass: ""
    };
}
setInterval(pollStatus, 2000);
</script>

</body>
</html>
"""


@app.route("/", methods=["GET", "POST"])
def index():

    projects = load_projects()

    if not projects:
        projects = {"agent-test": r"D:\AI\agent-test"}

    result = ""
    request_text = ""
    project_name = next(iter(projects))
    mode = "read"
    running = False
    diff_text = ""
    diff_state = ""
    diff_title = "Diff تغییرات اعمال‌شده"

    # Plan چندفایلی: فقط تشخیص + نمایش.
    # هیچ Write چندفایلی در این مرحله فعال نمی‌شود.
    file_plan = None

    # Plan در انتظار تأیید: فقط برای نمایش دکمه‌های
    # تأیید/لغو خوانده می‌شود و هیچ تغییری ایجاد نمی‌کند.
    approval_pending = has_pending_approval()

    # نتیجه Validation فقط بعد از Write موفق پر می‌شود؛
    # در حالت Read خالی می‌ماند و چیزی نمایش داده نمی‌شود.
    validation_text = ""
    validation_state = ""

    # برای راحتی کاربر، مسیر آخرین فایل نوشته‌شده
    # به عنوان پیشنهاد در فرم Rollback قرار می‌گیرد.
    rollback_target = ""

    # وضعیت لایه web (فقط در اینجا نگه داشته می‌شود)
    status_text = ""
    status_class = "idle"
    status_badge = ""

    st = get_agent_status()
    if st.get("status") != AGENT_STATUS_IDLE:
        status_text = st.get("message") or ""
        status_class = st.get("status")
        status_badge = {
            AGENT_STATUS_RUNNING: "در حال اجرا",
            AGENT_STATUS_DONE: "کامل شد",
            AGENT_STATUS_ERROR: "خطا",
            AGENT_STATUS_STOPPED: "متوقف شد",
        }.get(st.get("status"), "")

    last_write = get_last_write_result()

    if last_write:
        rollback_target = (
            last_write.get("relative_path") or ""
        )

    if request.method == "POST":

        # گزارش مرحله‌ای هر درخواست از صفر شروع می‌شود.
        global agent_progress, last_result
        del agent_progress[:]

        request_text = request.form.get(
            "request",
            ""
        ).strip()

        project_name = request.form.get(
            "project",
            project_name
        )

        mode = request.form.get(
            "mode",
            "read"
        )

        # دکمه‌های تأیید/لغو action را به‌صورت صریح
        # می‌فرستند. این شاخه قبل از هر پردازش دیگری
        # بررسی می‌شود تا «تأیید» هرگز به‌عنوان درخواست
        # جدید Agent وارد Agentic Loop نشود.
        approval_text = resolve_approval_action(
            request.form.get("approval_action")
        )

        if approval_text is not None:

            if is_agent_running():

                result = "Agent در حال اجراست"

            else:

                del agent_progress[:]
                last_result = ""

                allow_write = resolve_allow_write(mode)

                global current_stop_event
                stop_event = threading.Event()
                current_stop_event = stop_event

                set_agent_status(
                    AGENT_STATUS_RUNNING,
                    "در حال اجرای تأیید Plan...",
                )

                global agent_thread

                agent_thread = threading.Thread(
                    target=run_approval_background,
                    args=(approval_text,),
                    kwargs={
                        "project_name": project_name,
                        "allow_write": allow_write,
                        "stop_event": stop_event,
                    },
                    daemon=True,
                )

                agent_thread.start()

                running = True

        elif request_text:

            # اجرای همزمان مجاز نیست؛ صفحه قفل نمی‌شود.
            if is_agent_running():

                result = "Agent در حال اجراست"

            else:

                # گزارش مرحله‌ای هر درخواست از صفر.
                del agent_progress[:]
                last_result = ""

                allow_write = resolve_allow_write(mode)

                stop_event = threading.Event()
                current_stop_event = stop_event

                set_agent_status(
                    AGENT_STATUS_RUNNING,
                    "در حال اجرای درخواست...",
                )

                agent_thread = threading.Thread(
                    target=run_agent_background,
                    args=(request_text,),
                    kwargs={
                        "project_name": project_name,
                        "allow_write": allow_write,
                        "stop_event": stop_event,
                    },
                    daemon=True,
                )

                agent_thread.start()

                running = True

                # درخواست پیش‌تر به thread و تاریخچه تحویل
                # شده است؛ مقدار باقیمانده فقط برای رندر
                # دوباره است، پس کادر ورودی برای درخواست بعدی
                # خالی می‌ماند. (Enter و رفتار فرم تغییری نمی‌کند.)
                request_text = ""

                # Diff/Plan/Validation در GET بعدی خوانده
                # می‌شوند، نه در همین پاسخ.
                file_plan = get_last_file_plan()

        else:

            result = "لطفاً یک درخواست وارد کنید."

    result = strip_think_tags(result)

    return render_template_string(
        HTML,
        projects=projects,
        result=result,
        request_text=request_text,
        project_name=project_name,
        mode=mode,
        running=running,
        file_plan=file_plan,
        approval_pending=approval_pending,
        diff_text=diff_text,
        diff_state=diff_state,
        diff_title=diff_title,
        rollback_target=rollback_target,
        validation_text=validation_text,
        validation_state=validation_state,
        status_text=status_text,
        status_class=status_class,
        status_badge=status_badge,
    )


@app.route("/add-project", methods=["POST"])
def add_project_route():
    # Phase 1: این route پیش از این تغییر GET بود و
    # projects.json را تغییر می‌داد؛ یعنی یک لینک یا
    # prefetch ساده می‌توانست پیکربندی پروژه‌ها را عوض
    # کند. حالا فقط POST معتبر است.
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    folder = filedialog.askdirectory(
        title="پوشه پروژه را انتخاب کنید"
    )

    root.destroy()

    if not folder:
        return redirect(url_for("index"))

    folder = os.path.abspath(folder)

    name = os.path.basename(
        os.path.normpath(folder)
    )

    if not name:
        name = "project"

    projects = load_projects()

    original_name = name
    counter = 2

    while name in projects:
        name = f"{original_name}-{counter}"
        counter += 1

    add_project(name, folder)

    return redirect(url_for("index"))


@app.route("/remove-project", methods=["POST"])
def remove_project_route():
    # Phase 1: این route پیش از این تغییر GET بود و
    # پروژه را از projects.json حذف می‌کرد. حذف پروژه
    # یک عملیات state-changing است، پس فقط POST.
    name = request.form.get(
        "name",
        ""
    ).strip()

    if name and name != "agent-test":
        remove_project(name)

    return redirect(url_for("index"))


@app.route("/rollback", methods=["POST"])
def rollback_route():

    projects = load_projects()

    if not projects:
        projects = {"agent-test": r"D:\AI\agent-test"}

    project_name = request.form.get(
        "project",
        next(iter(projects))
    ).strip()

    # Phase 1 — Rollback Safety Gate.
    #
    # پیش از این تغییر این route مقدار mode را نمی‌خواند و
    # مستقیم rollback_file را صدا می‌زد؛ بنابراین در حالت
    # «فقط بررسی» هم می‌شد فایلی را با Backup قدیمی
    # بازنویسی کرد و Emergency Stop هم جلویش را نمی‌گرفت.
    #
    # تغییر رفتار عمدی و کمینه است: همان rollback_file با
    # همان ثبت recovery در Journal اجرا می‌شود، فقط دو
    # قانون پیش از آن اعمال می‌شود:
    #   1) Rollback یک Mutation است، پس به اجازهٔ Write نیاز
    #      دارد (همان قاعدهٔ Fail-Closed خود agent.py).
    #   2) Emergency Stop باید مؤثر باشد.
    mode = request.form.get("mode", "read").strip() or "read"
    allow_write = resolve_allow_write(mode)

    relative_path = request.form.get(
        "file",
        ""
    ).strip()

    result = ""
    request_text = ""
    running = False
    diff_text = ""
    diff_state = ""
    diff_title = "Diff بازگردانی"
    rollback_target = relative_path

    if not relative_path:

        result = (
            "برای Rollback باید مسیر فایل "
            "را وارد کنید."
        )

    elif not allow_write:

        # Fail-Closed: بدون اجازهٔ Write هیچ فایلی بازنویسی
        # نمی‌شود. پیام صریح است تا کاربر فکر نکند Rollback
        # انجام شده است.
        result = (
            "Rollback یک عملیات تغییردهنده است و در حالت "
            "«فقط بررسی» انجام نمی‌شود؛ هیچ فایلی تغییر "
            "نکرد.\nبرای بازگردانی، حالت «اعمال تغییر» را "
            "انتخاب کنید."
        )

    elif is_stop_requested():

        # Emergency Stop باید روی Rollback هم مؤثر باشد.
        result = (
            "Emergency Stop فعال است؛ Rollback اجرا نشد و "
            "هیچ فایلی تغییر نکرد."
        )

    else:

        try:

            # Rollback فقط با درخواست صریح کاربر
            # اجرا می‌شود؛ هیچ‌وقت خودکار نیست.
            # مسیر Backup توسط خود Agent تشخیص
            # داده می‌شود و از کاربر گرفته نمی‌شود.
            #
            # ثبت recovery در Journal عیناً حفظ می‌شود؛
            # این تغییر فقط Gate ورودی است، نه منطق Rollback.
            rollback_file(
                projects.get(project_name),
                relative_path,
                return_details=True
            )

            # Diff وضعیت جدید نسبت به وضعیت
            # قبل از Rollback قابل دسترسی است.
            info = get_last_rollback_result()

            result = (
                "فایل از Backup بازگردانده شد.\n"
                f"پروژه: {project_name}\n"
                f"فایل: {info['relative_path']}\n"
                f"مسیر: {info['path']}\n"
                f"Backup استفاده‌شده: "
                f"{info['restored_from']}\n"
                f"Backup وضعیت قبل از Rollback: "
                f"{info['backup_path']}"
            )

            diff_text, diff_state = build_diff_view(
                info
            )

            diff_title = (
                "Diff بازگردانی "
                "(نسبت به قبل از Rollback)"
            )

        except Exception as e:

            result = (
                "خطا در Rollback:\n"
                + str(e)
            )

    result = strip_think_tags(result)

    return render_template_string(
        HTML,
        projects=projects,
        result=result,
        request_text=request_text,
        project_name=project_name,
        mode=mode,
        running=running,
        file_plan=None,
        approval_pending=has_pending_approval(),
        diff_text=diff_text,
        diff_state=diff_state,
        diff_title=diff_title,
        rollback_target=rollback_target,
        validation_text="",
        validation_state="",
        status_text="",
        status_class="idle",
        status_badge="",
    )


def build_multi_write_plan(
    project_name,
    project_root,
    files
):
    # Phase 1 — ساخت Plan canonical برای /multi-write.
    #
    # این تابع عمداً هیچ فایلی نمی‌نویسد. کارش فقط ساخت
    # همان pending_actionای است که Agentic Loop برای
    # write_files می‌سازد:
    #
    #   validate_mutating_args → build_pending_revalidation_context
    #   → remember_pending_action
    #
    # بنابراین درخواست بعدیِ کاربر («تأیید») از
    # handle_pending_action_request → execute_pending_action
    # عبور می‌کند و Re-validation، Execute، Verify و
    # Journal همان‌طور که برای هر Write دیگر اجرا می‌شوند.
    #
    # به‌کارگیری context، plan summary و متادیتای Plan از
    # خود agent.py وام گرفته می‌شود؛ هیچ منطق Plan یا
    # Re-validation دوباره‌نویسی نشده است.
    args = {"files": files}

    try:
        validate_mutating_args(
            project_root,
            "write_files",
            args
        )
    except Exception as e:
        return {
            "success": False,
            "error": (
                "Args این Write معتبر نیست؛ هیچ تغییری انجام "
                "نشد: " + str(e)
            ),
        }

    try:
        context = build_pending_revalidation_context(
            "write_files",
            args,
            project_root,
            project_name,
            allow_write=True,
        )
    except Exception as e:
        return {
            "success": False,
            "error": (
                "ساخت Context برای Re-validation ممکن نشد؛ "
                "هیچ تغییری انجام نشد: " + str(e)
            ),
        }

    pending_action = {
        "tool": "write_files",
        "args": args,
        "project": project_name,
        "project_root": project_root,
        "user_request": (
            "Multi-file Write از رابط وب (HTTP /multi-write)"
        ),
        "correction_attempts": 0,
        "request_status": {},
        "read_paths": [],
        "context": context,
    }

    remember_pending_action(pending_action)

    return {
        "success": True,
        "pending_action": True,
        "needs_approval": True,
        "message": (
            "Plan چندفایلی ساخته شد اما هنوز هیچ فایلی نوشته "
            "نشده است. برای اجرای همین Plan، «تأیید» را "
            "بفرستید."
        ),
        "plan": build_pending_plan_text(
            "write_files",
            args,
            project_root,
            write_mode=True,
            plan_summary=build_multi_file_plan_summary(
                pending_action
            ),
        ),
    }


@app.route("/multi-write", methods=["POST"])
def multi_write_route():
    # Multi-file Write: دریافت درخواست JSON شامل
    # project و لیست فایل‌ها (path, content, action).
    # اگر از URL query استفاده شود، project_name را بگیرد.
    request_data = {}

    if request.is_json:
        request_data = request.get_json(
            silent=True
        ) or {}

    project_name = (
        request_data.get("project")
        or request.args.get("project", "")
    ).strip()

    files = request_data.get("files") or []

    projects = load_projects()

    if not project_name:
        project_name = (
            list(projects.keys())[0]
            if projects else "agent-test"
        )

    project_root = projects.get(project_name)

    result = None
    error_msg = ""

    if not project_root:
        error_msg = (
            "پروژه یافت نشد: "
            + project_name
        )
        return (
            jsonify({
                "success": False,
                "error": error_msg
            }),
            400,
        )
    elif not files:
        error_msg = (
            "لیست فایل‌ها خالی است."
        )
        return (
            jsonify({
                "success": False,
                "error": error_msg
            }),
            400,
        )
    else:
        # Phase 1 — /multi-write دیگر اجازهٔ Write ندارد.
        #
        # پیش از این تغییر این route مستقیم
        # write_file_multi را صدا می‌زد و در نتیجه هیچ
        # Approval، Re-validation یا is_write_allowed و
        # — مهم‌تر از همه — هیچ Journal ثبت نمی‌شد؛ یعنی یک
        # Write موفق از این مسیر هرگز Undo نمی‌شد.
        #
        # این route فقط Plan canonical می‌سازد (همان
        # pending_actionای که Agentic Loop می‌سازد) و آن را
        # برای یک Approval کاربر ذخیره می‌کند. اجرای واقعی
        # بعد از «تأیید» و از مسیر execute_pending_action
        # انجام می‌شود؛ یعنی Plan → Re-validation →
        # Approval → Execute → Verify → Journal → Undo.
        if not resolve_allow_write(
            (request_data.get("mode") or request.args.get("mode", "read"))
        ):
            return (
                jsonify({
                    "success": False,
                    "needs_approval": True,
                    "error": (
                        "Multi-file Write یک عملیات تغییردهنده "
                        "است و فقط با اجازهٔ Write ساخته می‌شود؛ "
                        "هیچ فایلی تغییر نکرد."
                    ),
                }),
                403,
            )

        if is_stop_requested():
            return (
                jsonify({
                    "success": False,
                    "error": (
                        "Emergency Stop فعال است؛ هیچ Plan "
                        "تغییردهنده‌ای ساخته نشد."
                    ),
                }),
                409,
            )

        if not request_data.get("approved"):
            plan_response = build_multi_write_plan(
                project_name,
                project_root,
                files
            )

            if plan_response.get("success"):
                # فقط Plan ساخته شد؛ هنوز هیچ Write یا
                # Backup یا Journalی وجود ندارد.
                return plan_response, 202

            return plan_response, 400

        # این نقطه فقط وقتی می‌رسد که کلاینت یک Plan را
        # صریحاً تأیید کرده باشد؛ اجرای واقعی همچنان از
        # مسیر canonical در agent.py انجام می‌شود.
        #
        # Phase I — Pending Action Integrity.
        #
        # پیش از این تغییر این خط هیچ Contextی به Gate می‌داد و
        # صرفاً Slot سراسری را اجرا می‌کرد. نتیجه: کلاینتی که
        # approved=true می‌فرستاد می‌توانست با project و files
        # دلخواه خودش، Planِ ساخته‌شده برای پروژه/فایل‌های
        # کاملاً متفاوت را اجرا کند. حالا هر دو چیزی که این
        # درخواست ادعا می‌کند باید با Plan داخل Slot یکی باشد.
        result = execute_pending_action(
            expected_project=project_name,
            expected_args={"files": files},
        )

        if result:
            return (
                jsonify(result),
                200,
                {"Content-Type": "application/json"}
            )

        return (
            jsonify({
                "success": False,
                "error": (
                    error_msg
                    or "Multi-file Write انجام نشد؛ هیچ تغییری "
                    "روی دیسک ثبت نشد."
                )
            }),
            400
        )


@app.route("/stop", methods=["POST"])
def stop_route():
    # توقف اجرای فعال Agent از طریق وب
    # این route فقط event اجرای فعال را set می‌کند
    global current_stop_event

    if current_stop_event is None:
        return (
            jsonify({
                "success": False,
                "message": "برای توقف اجرای فعالی وجود ندارد."
            }),
            400,
        )

    current_stop_event.set()

    return (
        jsonify({
            "success": True,
            "message": "درخواست توقف ارسال شد."
        }),
        200,
    )


@app.route("/status", methods=["GET"])
def status_route():
    # وضعیت فعلی Agent (فقط در لایه web نگه داشته می‌شود).
    return (
        jsonify(dict(
            get_agent_status(),
            progress=agent_progress[-40:],
            running=is_agent_running(),
            last_result=strip_think_tags(last_result),
            # فقط خواندن وضعیت Pending Action برای نمایش
            # دکمه‌های تأیید/لغو؛ هیچ تغییری ایجاد نمی‌کند.
            pending_approval=has_pending_approval(),
            provider=runtime_info["provider"],
            model=runtime_info["model"],
            started_at=runtime_info["started_at"],
            finished_at=runtime_info["finished_at"],
            duration=runtime_info["duration"],
        )),
        200,
        {"Content-Type": "application/json"}
    )


@app.route("/chat-history", methods=["GET"])
def chat_history_route():
    # تاریخچه از SQLite خوانده می‌شود؛ /status بی‌تغییر است.
    limit = request.args.get("limit", CHAT_HISTORY_READ_LIMIT)
    return (
        jsonify({
            "history": read_chat_history(limit)
        }),
        200,
        {"Content-Type": "application/json"}
    )


if __name__ == "__main__":

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        threaded=True
    )

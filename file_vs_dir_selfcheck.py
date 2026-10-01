"""
تست focused — یکپارچگی File در برابر Directory.

بازتولید خطای گزارش‌شده در `agent-test`:

    مدل `create_directory("multi-file-test/index.html")` صدا زد و
    یک پوشه به نام `index.html` ساخته شد. چند دقیقه بعد
    `write_files` روی همان مسیر اجرا شد و در
    `os.replace(tmp_name, full_path)` با
    `[WinError 5] Access is denied` شکست خورد، چون روی ویندوز
    جایگزینی یک پوشه‌ی موجود با فایل مجاز نیست.

قانونی که این تست نگه می‌دارد:
  1. ساخت پوشه با نامی که مشخصاً نام فایل است (مثل
     `index.html`) رد می‌شود و بازخورد روشن به مدل می‌رود.
  2. نام‌های معتبر پوشه دست‌نخورده می‌مانند (`docs`,
     `assets/img`, `my.folder`).
  3. ابزارهای فایل‌محور (`write_file`, `write_files`,
     `edit_file`) اگر مقصدشان یک پوشه‌ی موجود باشد، پیش از
     Plan رد می‌شوند (Fail-Closed).
  4. موتور `write_file_multi` هم Fail-Closed است و خطای
     قابل‌فهم می‌دهد، نه WinError 5.
  5. در هیچ‌کدام از مسیرهای رد شده هیچ تغییری روی دیسک
     رخ نمی‌دهد.

این تست فقط یک پروژه‌ی موقت می‌سازد؛ هیچ فایل واقعیِ
`agent-test` یا `local-agent` را تغییر نمی‌دهد.
"""

import os
import shutil
import sys
import tempfile

import agent


results = []


def check(name, condition, detail=""):
    ok = bool(condition)
    results.append((name, ok))
    print("[{}] {}{}".format(
        "PASS" if ok else "FAIL",
        name,
        ("  <- " + detail) if (detail and not ok) else "",
    ))
    return ok


def safe(text):
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def snapshot(root):
    state = {}

    for current, dirs, files in os.walk(root):
        for name in dirs:
            path = os.path.join(current, name)
            state[os.path.relpath(path, root)] = "dir"

        for name in files:
            path = os.path.join(current, name)
            with open(path, "rb") as fh:
                state[os.path.relpath(path, root)] = fh.read()

    return state


def raises(callable_object):
    try:
        callable_object()
    except Exception as e:
        return type(e).__name__, str(e)

    return None, ""


def make_project(root):
    os.makedirs(root, exist_ok=True)

    with open(os.path.join(root, "index.html"), "w",
              encoding="utf-8") as fh:
        fh.write("<h1>ok</h1>\n")

    # همان وضعیت خراب واقعی: پوشه‌ای با نام یک فایل.
    os.makedirs(os.path.join(root, "trap", "index.html"),
                exist_ok=True)


def test_directory_named_like_file_is_rejected(temp_root):
    before = snapshot(temp_root)

    kind, message = raises(
        lambda: agent.create_directory(
            temp_root, "trap/notes.md"
        )
    )

    check(
        "create_directory: file-like name is rejected",
        kind == "ValueError",
        safe((kind, message)),
    )
    check(
        "create_directory: message names the file/dir mistake",
        "فایل" in message and "پوشه" in message,
        safe(message),
    )
    check(
        "create_directory: nothing created on disk",
        snapshot(temp_root) == before,
        safe(sorted(set(snapshot(temp_root)) ^ set(before))),
    )
    check(
        "create_directory: no notes.md path exists",
        not os.path.exists(os.path.join(temp_root, "trap", "notes.md")),
    )


def test_valid_directory_names_still_work(temp_root):
    ok = True
    detail = ""

    for relative in ("docs", "assets/img", "my.folder"):
        try:
            agent.create_directory(temp_root, relative)
        except Exception as e:
            ok = False
            detail = safe((relative, str(e)))

    check(
        "create_directory: normal directory names still work",
        ok,
        detail,
    )
    check(
        "create_directory: docs really exists",
        os.path.isdir(os.path.join(temp_root, "docs")),
    )


def test_existing_directory_keeps_current_behavior(temp_root):
    # رفتار فعلی حفظ می‌شود: مسیر موجودِ فایل همچنان خطای
    # «فایل است نه پوشه» می‌دهد.
    kind, message = raises(
        lambda: agent.create_directory(temp_root, "index.html")
    )

    check(
        "create_directory: existing file still rejected as file",
        kind == "ValueError" and "فایل" in message,
        safe((kind, message)),
    )


def test_mutating_args_reject_directory_target(temp_root):
    before = snapshot(temp_root)

    for tool_name, args in (
        ("write_file", {
            "path": "trap/index.html",
            "content": "x",
        }),
        ("edit_file", {
            "path": "trap/index.html",
            "old": "a",
            "new": "b",
        }),
        ("write_files", {
            "files": [{"path": "trap/index.html", "content": "x"}],
        }),
        ("write_files", {
            "files": [
                {"path": "index.html", "content": "x"},
                {"path": "trap/index.html", "content": "y"},
            ],
        }),
    ):
        kind, message = raises(
            lambda t=tool_name, a=args: agent.validate_mutating_args(
                temp_root, t, a,
                user_request="دو فایل را تغییر بده",
            )
        )

        check(
            "validate_mutating_args: " + tool_name
            + " rejects directory target",
            kind == "ValueError" and "پوشه" in message,
            safe((tool_name, kind, message)),
        )

    check(
        "validate_mutating_args: no disk change",
        snapshot(temp_root) == before,
        safe(sorted(set(snapshot(temp_root)) ^ set(before))),
    )


def test_normal_write_targets_are_still_accepted(temp_root):
    ok = True
    detail = ""

    for tool_name, args in (
        ("write_file", {
            "path": "index.html",
            "content": "x",
        }),
        ("write_files", {
            "files": [
                {"path": "index.html", "content": "x"},
                {"path": "style.css", "content": "y"},
            ],
        }),
        ("create_directory", {"path": "assets"}),
    ):
        try:
            agent.validate_mutating_args(
                temp_root, tool_name, args,
                user_request="index.html و style.css را تغییر بده",
            )
        except Exception as e:
            ok = False
            detail = safe((tool_name, type(e).__name__, str(e)))

    check(
        "validate_mutating_args: normal file targets still accepted",
        ok,
        detail,
    )


def test_engine_is_fail_closed(temp_root):
    trap = os.path.join(temp_root, "trap", "index.html")
    before = snapshot(temp_root)

    # حالت return_details=True: خطا به‌صورت ساختاریافته برمی‌گردد.
    result = agent.write_file_multi(
        temp_root,
        [{"path": "trap/index.html", "content": "x"}],
        return_details=True,
    )

    message = str(result.get("error") or "")

    check(
        "write_file_multi: directory target fails (details)",
        result.get("success") is False,
        safe(result.get("error")),
    )
    check(
        "write_file_multi: message says target is a directory",
        "پوشه" in message and "فایل" in message,
        safe(message),
    )
    check(
        "write_file_multi: no WinError 5 leaked",
        "WinError" not in message and "Access is denied" not in message,
        safe(message),
    )

    # حالت return_details=False: همان خطا raise می‌شود.
    kind, raised_message = raises(
        lambda: agent.write_file_multi(
            temp_root,
            [{"path": "trap/index.html", "content": "x"}],
        )
    )

    check(
        "write_file_multi: directory target raises",
        kind == "RuntimeError" and "پوشه" in raised_message,
        safe((kind, raised_message)),
    )
    check(
        "write_file_multi: directory still untouched",
        os.path.isdir(trap) and not os.listdir(trap),
    )

    after = snapshot(temp_root)
    before.pop(".agent-multi-backup", None)
    after.pop(".agent-multi-backup", None)

    check(
        "write_file_multi: nothing written to disk",
        before == after,
        safe(sorted(set(after) ^ set(before))),
    )


def test_loop_rejects_before_plan(temp_root):
    # مسیر واقعی: ابزار mutating با مقصد پوشه باید در
    # validate_mutating_args رد شود، یعنی قبل از ساخت Plan.
    kind, message = raises(
        lambda: agent.validate_mutating_args(
            temp_root,
            "write_files",
            {"files": [{"path": "trap/index.html", "content": "x"}]},
            user_request="index.html را تغییر بده",
        )
    )

    check(
        "loop: directory target never reaches a Plan",
        kind == "ValueError" and "پوشه" in message,
        safe((kind, message)),
    )


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    temp_root = tempfile.mkdtemp(prefix="file_vs_dir_")
    make_project(temp_root)

    saved = {
        "pending": agent.PENDING_ACTION_FILE,
        "journal": agent.ACTION_JOURNAL_FILE,
        "project_path": agent.get_project_path,
    }
    agent.PENDING_ACTION_FILE = os.path.join(temp_root, "p.json")
    agent.ACTION_JOURNAL_FILE = os.path.join(temp_root, "j.json")
    agent.get_project_path = lambda name: temp_root

    try:
        test_directory_named_like_file_is_rejected(temp_root)
        test_valid_directory_names_still_work(temp_root)
        test_existing_directory_keeps_current_behavior(temp_root)
        test_mutating_args_reject_directory_target(temp_root)
        test_normal_write_targets_are_still_accepted(temp_root)
        test_engine_is_fail_closed(temp_root)
        test_loop_rejects_before_plan(temp_root)
    finally:
        agent.PENDING_ACTION_FILE = saved["pending"]
        agent.ACTION_JOURNAL_FILE = saved["journal"]
        agent.get_project_path = saved["project_path"]
        agent.clear_pending_action()
        shutil.rmtree(temp_root, ignore_errors=True)

    passed = sum(1 for _, ok in results if ok)
    print("RESULT: {}/{} passed".format(passed, len(results)))

    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
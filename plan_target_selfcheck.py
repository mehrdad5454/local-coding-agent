"""
تست focused — Plan باید روی فایل‌های واقعی پروژه ساخته شود،
نه روی نام‌های حدسی.

بازتولید خطای گزارش‌شده در پروژه‌ی `agent-test`:
درخواست طبیعی فقط «عنوان صفحه»، «متن پاراگراف» و «ظاهر صفحه
(h1 آبی / کلاس .text خاکستری)» را توصیف می‌کند و هیچ نام فایلی
نمی‌آورد. پروژه فقط `index.html` و `style.css` دارد.

Plan اشتباه تولیدشده:
  * `index.php` به‌جای `index.html`   → مسیر حدسی که روی دیسک نیست
  * فقط یک Mutation                  → `style.css` جا افتاده است
  * بدون Mutation برای `style.css`

قانونی که این تست نگه می‌دارد:
  1. مسیر واقعیِ موجود (`index.html`, `style.css`) پذیرفته می‌شود.
  2. مسیری که روی دیسک نیست و در متن درخواست هم نیامده
     (یعنی حدس مدل) هرگز به Plan راه نمی‌یابد.
  3. ساخت فایل جدیدِ صریحاً نام‌برده‌شده در درخواست همچنان مجاز است.
  4. درخواست‌های فقط‌خواندنی و ابزارهای فقط‌خواندنی اصلاً
     تحت تأثیر قرار نمی‌گیرند (حتی با مسیر ناموجود).

این تست فقط یک پروژه‌ی موقت می‌سازد؛ هیچ فایل واقعیِ
`agent-test` یا `local-agent` را تغییر نمی‌دهد.
"""

import json
import os
import shutil
import sys
import tempfile

import agent


results = []

REAL_REQUEST = (
    "در پروژه agent-test عنوان اصلی صفحه را به Local Agent Demo "
    "تغییر بده و متن پاراگراف را به This page is used to test the "
    "Local Coding Agent. تغییر بده. همچنین ظاهر صفحه را طوری تنظیم "
    "کن که عنوان h1 آبی باشد و پاراگراف با کلاس .text خاکستری "
    "باشد. ساختار فعلی HTML را حفظ کن و فقط فایل‌های لازم را "
    "تغییر بده. قبل از اجرا Plan ارائه کن و منتظر تأیید من بمان. "
    "بعد از تأیید، تغییرات را اجرا و Verify کن. هیچ فایل دیگری را "
    "تغییر نده."
)

INDEX_HTML = (
    "<!DOCTYPE html>\n"
    "<html lang=\"fa\">\n"
    "<head>\n"
    "  <meta charset=\"utf-8\">\n"
    "  <title>Agent Test</title>\n"
    "  <link rel=\"stylesheet\" href=\"style.css\">\n"
    "</head>\n"
    "<body>\n"
    "  <h1>Agent Test</h1>\n"
    "  <p class=\"text\">This page is a test page.</p>\n"
    "</body>\n"
    "</html>\n"
)

STYLE_CSS = "h1 {\n  color: black;\n}\n\n.text {\n  color: black;\n}\n"


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


def make_project(root):
    os.makedirs(root, exist_ok=True)

    with open(os.path.join(root, "index.html"), "w",
              encoding="utf-8") as fh:
        fh.write(INDEX_HTML)

    with open(os.path.join(root, "style.css"), "w",
              encoding="utf-8") as fh:
        fh.write(STYLE_CSS)

    with open(os.path.join(root, "README.txt"), "w",
              encoding="utf-8") as fh:
        fh.write("agent-test fixture project\n")


def run_loop(tool_script, temp_root, request=REAL_REQUEST,
             allow_write=True, project="probe", max_steps=6):
    state = {"i": 0}

    def stub(system_prompt, user_prompt, *a, **kw):
        idx = state["i"]
        state["i"] += 1

        if idx >= len(tool_script):
            return "FINAL: all done"

        item = tool_script[idx]

        if isinstance(item, tuple):
            tool, args = item
            return "THOUGHT: t\nTOOL: %s\nARGS: %s" % (
                tool,
                json.dumps(args, ensure_ascii=False),
            )

        return item

    saved = agent.call_model
    agent.call_model = stub
    observations = []
    user_prompt = ""

    try:
        out = agent.run_loop_steps(
            user_request=request,
            project_root=temp_root,
            project_name=project,
            allow_write=allow_write,
            transcript=[],
            observations=observations,
            steps_used=0,
            correction_attempts=0,
            max_steps=max_steps,
        )
    finally:
        agent.call_model = saved

    return out, observations, state["i"]


def pending_targets():
    pending = agent.get_pending_action() or {}
    args = pending.get("args") or {}
    tool = pending.get("tool")

    if tool == "write_files":
        try:
            return sorted(
                str(item.get("path"))
                for item in agent.normalize_batch_files(args)
            )

        except Exception:
            return []

    value = args.get("path") or args.get("relative_path")

    return [str(value)] if value else []


# ---------------------------------------------------------------- tests


def test_real_targets_are_accepted(temp_root):
    # 1) هر دو فایل واقعی پروژه باید Plan بسازند.
    for target, old, new in (
        ("index.html", "<title>Agent Test</title>",
         "<title>Local Agent Demo</title>"),
        ("style.css", "  color: black;", "  color: blue;"),
    ):
        agent.clear_pending_action()
        args = {"path": target, "old": old, "new": new}

        try:
            agent.validate_mutating_args(temp_root, "edit_file", args)
            accepted = True
            reason = ""

        except Exception as e:
            accepted = False
            reason = safe(e)

        check("PLAN: real target accepted -> " + target, accepted, reason)

        run_loop([("edit_file", {
            "path": target, "old": old, "new": new})],
            temp_root, max_steps=2)

        targets = pending_targets()
        check(
            "PLAN: pending plan built for " + target,
            target in targets,
            safe(targets),
        )
        agent.clear_pending_action()


def test_guessed_path_never_becomes_a_plan(temp_root):
    # 2) بازتولید باگ: مدل به‌جای index.html، index.php حدس می‌زند.
    #    index.php نه روی دیسک هست و نه در متن درخواست آمده.
    check(
        "SETUP: index.php really is absent on disk",
        not os.path.exists(os.path.join(temp_root, "index.php")),
    )

    agent.clear_pending_action()

    _, observations, _ = run_loop([("edit_file", {
        "path": "index.php",
        "old": "<title>Agent Test</title>",
        "new": "<title>Local Agent Demo</title>",
    })], temp_root, max_steps=4)

    targets = pending_targets()
    check(
        "PLAN: guessed index.php is NOT proposed",
        "index.php" not in targets,
        safe(targets),
    )
    check(
        "PLAN: guessed index.php is rejected with a reason",
        any("index.php" in str(o) for o in observations),
        safe(observations),
    )
    agent.clear_pending_action()


def test_guessed_batch_is_rejected(temp_root):
    # 3) همان حدس در قالب Batch (مسیر دو-تایی).
    agent.clear_pending_action()

    _, observations, _ = run_loop([("write_files", {"files": [
        {"path": "index.php", "content": "<h1>x</h1>"},
        {"path": "style.css", "content": STYLE_CSS},
    ]})], temp_root, max_steps=4)

    targets = pending_targets()
    check(
        "PLAN: guessed index.php in batch is NOT proposed",
        "index.php" not in targets,
        safe(targets),
    )
    agent.clear_pending_action()


def test_explicit_new_file_is_still_allowed(temp_root):
    # 4) ساخت فایلی که کاربر صریحاً نام برده، حدس نیست.
    create_request = "یک فایل جدید به نام notes.md بساز"

    agent.clear_pending_action()

    try:
        agent.validate_mutating_args(
            temp_root,
            "write_file",
            {"path": "notes.md", "content": "hello"},
        )
        accepted = True
        reason = ""

    except Exception as e:
        accepted = False
        reason = safe(e)

    check("PLAN: explicit new file still allowed", accepted, reason)

    run_loop([("write_file", {"path": "notes.md", "content": "hello"})],
             temp_root, request=create_request, max_steps=2)

    check("PLAN: pending plan built for notes.md",
          "notes.md" in pending_targets(),
          safe(pending_targets()))
    agent.clear_pending_action()


def test_read_only_requests_unaffected(temp_root):
    # 5) ابزارهای فقط‌خواندنی نباید تحت تأثیر قرار بگیرند؛
    #    کاوش یک مسیر ناموجود کاملاً مجاز است.
    agent.clear_pending_action()

    _, observations, _ = run_loop([
        ("find_entries", {"name": "index.php"}),
        ("read_file", {"path": "missing.php"}),
    ], temp_root, request="فقط بررسی کن چه فایل‌هایی وجود دارند",
        allow_write=False, max_steps=4)

    agent.clear_pending_action()

    check(
        "READONLY: no pending action is created",
        not (agent.get_pending_action() or {}),
        safe(agent.get_pending_action()),
    )
    check(
        "READONLY: probing a missing file is not rejected",
        len(observations) >= 2
        and any("read_file" in str(o) for o in observations),
        safe(observations),
    )
    check(
        "READONLY: request_asks_for_mutation stays False",
        agent.request_asks_for_mutation(
            "فقط بررسی کن چه فایل‌هایی وجود دارند") is False,
    )


def test_grounded_inventory_is_offered(temp_root):
    # 6) مدل باید نام فایل‌های واقعی پروژه را در پرامپت ببیند،
    #    وگرنه حدس می‌زند (ریشه‌ی خطا).
    captured = {}

    def stub(system_prompt, user_prompt, *a, **kw):
        captured["system"] = system_prompt
        captured["user"] = user_prompt
        return "FINAL: stop"

    saved = agent.call_model
    agent.call_model = stub

    try:
        agent.run_loop_steps(
            user_request=REAL_REQUEST,
            project_root=temp_root,
            project_name="probe",
            allow_write=True,
            transcript=[],
            observations=[],
            steps_used=0,
            correction_attempts=0,
            max_steps=1,
        )
    finally:
        agent.call_model = saved

    prompt = (captured.get("system") or "") + "\n" + (
        captured.get("user") or "")

    check("PROMPT: index.html is grounded in the prompt",
          "index.html" in prompt)
    check("PROMPT: style.css is grounded in the prompt",
          "style.css" in prompt)

    captured_readonly = {}

    def stub_readonly(system_prompt, user_prompt, *a, **kw):
        captured_readonly["user"] = user_prompt
        return "FINAL: stop"

    saved = agent.call_model
    agent.call_model = stub_readonly

    try:
        agent.run_loop_steps(
            user_request="فقط بررسی کن چه فایل‌هایی وجود دارند",
            project_root=temp_root,
            project_name="probe",
            allow_write=False,
            transcript=[],
            observations=[],
            steps_used=0,
            correction_attempts=0,
            max_steps=1,
        )
    finally:
        agent.call_model = saved

    readonly_prompt = captured_readonly.get("user") or ""
    check(
        "PROMPT: read-only request gets no mutation inventory",
        "REAL FILES IN THIS PROJECT" not in readonly_prompt,
    )


def test_no_unrelated_file_touched(temp_root):
    before = {}

    for name in sorted(os.listdir(temp_root)):
        path = os.path.join(temp_root, name)

        if os.path.isfile(path):
            with open(path, "rb") as fh:
                before[name] = fh.read()

    run_loop([("edit_file", {
        "path": "index.php",
        "old": "a",
        "new": "b",
    })], temp_root, max_steps=4)
    agent.clear_pending_action()
    after = {}

    for name in sorted(os.listdir(temp_root)):
        path = os.path.join(temp_root, name)

        if os.path.isfile(path):
            with open(path, "rb") as fh:
                after[name] = fh.read()

    check(
        "DISK: no file was created or modified",
        before == after,
        safe(sorted(set(after) ^ set(before))),
    )
    check(
        "DISK: index.php was never written",
        not os.path.exists(os.path.join(temp_root, "index.php")),
    )


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    temp_root = tempfile.mkdtemp(prefix="plan_target_")
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
        test_real_targets_are_accepted(temp_root)
        test_guessed_path_never_becomes_a_plan(temp_root)
        test_guessed_batch_is_rejected(temp_root)
        test_explicit_new_file_is_still_allowed(temp_root)
        test_read_only_requests_unaffected(temp_root)
        test_grounded_inventory_is_offered(temp_root)
        test_no_unrelated_file_touched(temp_root)
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

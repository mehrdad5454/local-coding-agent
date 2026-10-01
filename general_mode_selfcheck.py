"""
تست focused — General Mode (حالت «بدون پروژه»).

هدف: وقتی هیچ پروژه‌ای انتخاب نشده، Agent باید بتواند کار
عمومی/تحلیلی انجام دهد، اما هیچ دسترسی فایلی و هیچ mutationی
نداشته باشد. در مقابل، Project Mode باید دقیقاً مثل قبل کار
کند.

قانون‌هایی که این تست نگه می‌دارد:
  A) General Mode پاسخ متنی معتبر می‌دهد.
  B) General Mode هیچ فایلی نمی‌سازد/تغییر نمی‌دهد، حتی وقتی
     کاربر صریحاً درخواست ساخت یا ویرایش فایل بدهد.
  B2) فرمان‌های کنترلی («تأیید»/«undo») در General Mode هیچ
     Pending Actionای را اجرا یا لغو نمی‌کنند.
  C) Project Mode: خواندن واقعی مثل قبل کار می‌کند.
  D) Project Mode: مسیر Approval دست‌نخورده است و اجرای بدون
     تأیید انجام نمی‌شود.
  E) نگاشت context: «general» => project_root تهی، و یک پروژهٔ
     واقعی => project_root واقعی از registry.
  F) یک پروژهٔ واقعی که نامش «general» است هرگز پشت General
     Mode پنهان نمی‌شود.

همهٔ سناریوها روی پروژه‌های موقت اجرا می‌شوند؛ هیچ فایل
واقعیِ `agent-test` یا `local-agent` تغییر نمی‌کند و هیچ
درخواستی به مدل واقعی ارسال نمی‌شود (`call_model` stub).
"""

import json
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


def make_project(root):
    os.makedirs(root, exist_ok=True)

    with open(os.path.join(root, "index.html"), "w",
              encoding="utf-8") as fh:
        fh.write("<h1>General Mode probe</h1>\n")

    with open(os.path.join(root, "style.css"), "w",
              encoding="utf-8") as fh:
        fh.write("h1 { color: black; }\n")


class ModelStub:
    """call_model جایگزین: هیچ شبکه‌ای، فقط متن."""

    def __init__(self):
        self.prompts = []

    def __call__(self, system_prompt, user_prompt, *a, **kw):
        self.prompts.append((system_prompt, user_prompt))
        return "پاسخ متنی نمونه از مدل."


def setup(temp_root, stub):
    temp_root = os.path.join(temp_root, "real-project")
    make_project(temp_root)

    saved = {
        "call_model": agent.call_model,
        "get_project_path": agent.get_project_path,
        "pending_file": agent.PENDING_ACTION_FILE,
        "journal_file": agent.ACTION_JOURNAL_FILE,
        "projects_file": agent.PROJECTS_FILE,
    }

    registry = os.path.join(
        os.path.dirname(temp_root), "projects.json"
    )

    with open(registry, "w", encoding="utf-8") as fh:
        json.dump({"probe": temp_root}, fh)

    agent.PROJECTS_FILE = registry
    agent.call_model = stub

    # فایل‌های state بیرون از پروژه‌ی موقت نگه داشته می‌شوند تا
    # در سناریوهای «هیچ تغییری روی دیسک نبود» شمرده نشوند.
    state_dir = os.path.join(os.path.dirname(temp_root), "state")
    os.makedirs(state_dir, exist_ok=True)

    agent.PENDING_ACTION_FILE = os.path.join(
        state_dir, "pending.json"
    )
    agent.ACTION_JOURNAL_FILE = os.path.join(state_dir, "j.json")
    agent.clear_conversation_memory()
    agent.clear_pending_action()

    return temp_root, registry, saved


def restore(saved):
    agent.call_model = saved["call_model"]
    agent.PROJECTS_FILE = saved["projects_file"]
    agent.PENDING_ACTION_FILE = saved["pending_file"]
    agent.ACTION_JOURNAL_FILE = saved["journal_file"]
    agent.clear_pending_action()
    agent.clear_conversation_memory()


def test_general_text_answer(temp_root, stub):
    before = snapshot(temp_root)

    answer = agent.run_agent(
        "تفاوت بین پایتون و جاوااسکریپت چیست؟",
        "general",
    )

    check(
        "A: general mode returns a non-empty text answer",
        bool(str(answer).strip()),
        safe(answer),
    )
    check(
        "A: answer is not the 'no project found' error",
        "پیدا نشد" not in str(answer),
        safe(answer),
    )
    check(
        "A: model was actually asked (text only)",
        len(stub.prompts) >= 1,
    )
    check(
        "A: model prompt declares General Mode",
        any(
            "General Mode" in (system or "")
            for system, _ in stub.prompts
        ),
    )
    check(
        "A: model prompt forbids file access",
        any(
            "NO access to any file" in (system or "")
            for system, _ in stub.prompts
        ),
    )
    check(
        "A: nothing changed on disk",
        snapshot(temp_root) == before,
        safe(sorted(set(snapshot(temp_root)) ^ set(before))),
    )


def test_general_mutation_is_refused(temp_root, stub):
    before = snapshot(temp_root)

    requests = [
        "یک فایل به نام notes.txt بساز و داخلش بنویس: hello",
        "فایل style.css را تغییر بده",
        "پوشه docs را بساز",
        "فایل index.html را حذف کن",
        "style.css را به main.css تغییر نام بده",
    ]

    for request in requests:
        answer = agent.run_agent(request, "general")

        check(
            "B: refused without claiming success: " + request[:34],
            "انجام شد" not in str(answer),
            safe(answer),
        )

    check(
        "B: no file created",
        not os.path.exists(
            os.path.join(temp_root, "notes.txt")
        ),
    )
    check(
        "B: docs was not created",
        not os.path.exists(os.path.join(temp_root, "docs")),
    )
    check(
        "B: nothing changed on disk",
        snapshot(temp_root) == before,
        safe(sorted(set(snapshot(temp_root)) ^ set(before))),
    )
    check(
        "B: no journal entry was written",
        not os.path.exists(agent.ACTION_JOURNAL_FILE),
    )
    check(
        "B: no pending action was created",
        agent.get_pending_action() is None,
    )


def test_general_control_reply_does_not_execute(temp_root, stub):
    # یک Pending Action واقعی از Project Mode شبیه‌سازی می‌شود؛
    # سپس در General Mode «تأیید» فرستاده می‌شود.
    target = os.path.join(temp_root, "guarded.txt")

    with open(target, "w", encoding="utf-8") as fh:
        fh.write("original\n")

    agent.remember_pending_action({
        "tool": "write_file",
        "args": {
            "path": "guarded.txt",
            "content": "replaced\n",
        },
        "project": "probe",
        "project_root": temp_root,
        "user_request": "guarded.txt را تغییر بده",
        "context": None,
        "created_at": 0,
    })

    prompts_before = len(stub.prompts)

    agent.run_agent("تأیید", "general", allow_write=True)

    with open(target, "r", encoding="utf-8") as fh:
        content = fh.read()

    check(
        "B2: approve in general mode changed nothing",
        content == "original\n",
        safe(content),
    )
    check(
        "B2: pending action is still pending",
        agent.get_pending_action() is not None,
    )
    check(
        "B2: model was not asked for a mutation",
        len(stub.prompts) == prompts_before,
        safe(len(stub.prompts) - prompts_before),
    )

    agent.clear_pending_action()


def test_project_mode_read_still_works(temp_root, stub):
    before = snapshot(temp_root)

    answer = agent.run_agent(
        "محتوای فایل style.css را چیست؟",
        "probe",
    )

    check(
        "C: project mode returns an answer",
        bool(str(answer).strip()),
        safe(answer),
    )
    check(
        "C: project mode did not fall back to General",
        "پیدا نشد" not in str(answer),
        safe(answer),
    )
    check(
        "C: nothing changed on disk (read only)",
        snapshot(temp_root) == before,
        safe(sorted(set(snapshot(temp_root)) ^ set(before))),
    )


def test_project_mode_approval_path_intact(temp_root, stub):
    # Project Mode + درخواست تغییر: باید Plan بسازد و منتظر
    # تأیید بماند؛ یعنی هیچ mutationی بدون Approval رخ ندهد.
    tools = [
        {
            "tool": "write_file",
            "args": {
                "path": "style.css",
                "content": "h1 { color: red; }\n",
            },
        },
    ]

    state = {"i": 0}

    def loop_stub(system_prompt, user_prompt, *a, **kw):
        idx = state["i"]
        state["i"] += 1

        if idx >= len(tools):
            return "FINAL: تمام شد."

        return "TOOL: " + json.dumps(tools[idx], ensure_ascii=False)

    agent.call_model = loop_stub

    before = snapshot(temp_root)

    answer = agent.run_agent(
        "رنگ عنوان را در style.css تغییر بده",
        "probe",
        allow_write=True,
    )

    check(
        "D: project mode waits for approval",
        "تأیید" in str(answer),
        safe(answer),
    )
    check(
        "D: nothing written before approval",
        snapshot(temp_root) == before,
        safe(sorted(set(snapshot(temp_root)) ^ set(before))),
    )
    check(
        "D: a real pending action exists",
        agent.get_pending_action() is not None,
    )

    agent.clear_pending_action()
    agent.call_model = stub


def test_context_mapping(temp_root, registry):
    check(
        "E: 'general' maps to no project root",
        agent.get_project_path("general") is None
        and agent.is_general_mode("general"),
    )
    check(
        "E: empty name maps to General Mode",
        agent.is_general_mode(""),
    )
    check(
        "E: real project maps to its real root",
        agent.get_project_path("probe") == temp_root
        and not agent.is_general_mode("probe"),
        safe(agent.get_project_path("probe")),
    )
    check(
        "E: unknown project name is NOT General Mode",
        not agent.is_general_mode("does-not-exist"),
    )

    # پروژهٔ واقعی با نام general نباید پشت General Mode گم شود.
    with open(registry, "w", encoding="utf-8") as fh:
        json.dump({
            "probe": temp_root,
            "general": temp_root,
        }, fh)

    check(
        "F: a real project named 'general' wins over General Mode",
        not agent.is_general_mode("general")
        and agent.get_project_path("general") == temp_root,
    )

    with open(registry, "w", encoding="utf-8") as fh:
        json.dump({"probe": temp_root}, fh)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    base = tempfile.mkdtemp(prefix="general_mode_")
    stub = ModelStub()
    temp_root, registry, saved = setup(base, stub)

    try:
        test_general_text_answer(temp_root, stub)
        test_general_mutation_is_refused(temp_root, stub)
        test_general_control_reply_does_not_execute(temp_root, stub)
        test_project_mode_read_still_works(temp_root, stub)
        test_project_mode_approval_path_intact(temp_root, stub)
        test_context_mapping(temp_root, registry)
    finally:
        restore(saved)
        shutil.rmtree(base, ignore_errors=True)

    passed = sum(1 for _, ok in results if ok)
    print("RESULT: {}/{} passed".format(passed, len(results)))

    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
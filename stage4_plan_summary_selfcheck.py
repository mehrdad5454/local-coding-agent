"""
تست Stage 4 — خلاصهٔ چندفایلی grounded برای Plan.

مسیر canonical پروژه این است:
    Request → Inspect/Read → Canonical Plan → Re-validation
            → Multi-file Plan Summary → Approval → Execute → Verify

این تست نگه می‌دارد که خلاصه:

  * فقط از همان Plan canonical (Pending Action) ساخته شود؛
  * نام فایل‌هایش دقیقاً همان targetهای واقعی Plan باشند؛
  * هیچ target ساختگی، حدسی یا آمده از متن خام درخواست نسازد؛
  * برای هر فایل مسیر، نوع عملیات و خلاصهٔ کوتاه تغییر بدهد؛
  * محتوای کامل فایل‌ها را بی‌دلیل نمایش ندهد؛
  * قبل از Approval دیده شود ولی Approval را دور نزند؛
  * Plan را mutate نکند و هیچ bypassی نسازد؛
  * درخواست تک‌فایلی، Review، General Mode، Legacy و
    Re-validation را دست‌نخورده باقی بگذارد.

همهٔ سناریوها روی پروژه‌های موقت اجرا می‌شوند؛ هیچ فایل واقعی
تغییر نمی‌کند و هیچ درخواستی به مدل واقعی نمی‌رود.
"""

import json
import os
import shutil
import sys
import tempfile

import agent


results = []

ORIGINAL_INDEX_HTML = (
    "<!DOCTYPE html>\n"
    "<html>\n"
    "<head><title>Old</title></head>\n"
    "<body><h1>Old</h1></body>\n"
    "</html>\n"
)

ORIGINAL_STYLE_CSS = "h1 {\n  color: black;\n}\n"

NEW_INDEX_HTML = (
    "<!DOCTYPE html>\n"
    "<html>\n"
    "<head><title>New</title></head>\n"
    "<body><h1>New</h1></body>\n"
    "</html>\n"
)

NEW_STYLE_CSS = "h1 {\n  color: blue;\n}\n"

NEW_APP_JS = "console.log('init');\n"


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


def read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return ""


def snapshot(root):
    state = {}

    for current, dirs, files in os.walk(root):
        for name in dirs:
            state[os.path.relpath(
                os.path.join(current, name), root
            )] = "dir"

        for name in files:
            with open(os.path.join(current, name), "rb") as fh:
                state[os.path.relpath(
                    os.path.join(current, name), root
                )] = fh.read()

    return state


class ScriptedModel:
    """call_model جایگزین: هیچ شبکه‌ای، فقط پاسخ‌های از پیش
    تعریف‌شده."""

    def __init__(self):
        self.scripts = []
        self.calls = 0

    def push(self, *responses):
        self.scripts.append(list(responses))

    def __call__(self, system_prompt, user_prompt, *a, **kw):
        index = min(self.calls, len(self.scripts) - 1)
        self.calls += 1

        responses = self.scripts[index] if self.scripts else []

        if not responses:
            return "FINAL: تمام شد."

        return responses.pop(0)


def write_tool(tool, args):
    return "TOOL: " + json.dumps(
        {"tool": tool, "args": args},
        ensure_ascii=False,
    )


def make_pending(root, project, files):
    """یک Pending Action واقعی، دقیقاً با همان ساختاری که
    agent.py می‌سازد."""
    tool = "write_files"
    args = {"files": files}

    return {
        "tool": tool,
        "args": args,
        "project": project,
        "project_root": root,
        "user_request": "selfcheck",
        "context": agent.build_pending_revalidation_context(
            tool, args, root, project, allow_write=True
        ),
        "created_at": 0,
    }


def setup(base, model):
    root = os.path.join(base, "project")
    os.makedirs(root, exist_ok=True)

    with open(os.path.join(root, "index.html"), "w",
              encoding="utf-8") as fh:
        fh.write(ORIGINAL_INDEX_HTML)

    with open(os.path.join(root, "style.css"), "w",
              encoding="utf-8") as fh:
        fh.write(ORIGINAL_STYLE_CSS)

    state_dir = os.path.join(base, "state")
    os.makedirs(state_dir, exist_ok=True)

    registry = os.path.join(state_dir, "projects.json")

    with open(registry, "w", encoding="utf-8") as fh:
        json.dump({"probe": root}, fh)

    saved = {
        "call_model": agent.call_model,
        "projects_file": agent.PROJECTS_FILE,
        "pending_file": agent.PENDING_ACTION_FILE,
        "journal_file": agent.ACTION_JOURNAL_FILE,
    }

    agent.PROJECTS_FILE = registry
    agent.call_model = model
    agent.PENDING_ACTION_FILE = os.path.join(
        state_dir, "pending.json"
    )
    agent.ACTION_JOURNAL_FILE = os.path.join(
        state_dir, "journal.json"
    )
    agent.clear_pending_action()
    agent.clear_conversation_memory()

    return root, saved


def restore(saved):
    agent.call_model = saved["call_model"]
    agent.PROJECTS_FILE = saved["projects_file"]
    agent.PENDING_ACTION_FILE = saved["pending_file"]
    agent.ACTION_JOURNAL_FILE = saved["journal_file"]
    agent.clear_pending_action()
    agent.clear_conversation_memory()


def summary_lines(text, marker="خلاصه Plan چندفایلی"):
    # هر خطی که واقعاً جزئیات یک target را نمایش می‌دهد.
    out = []

    for line in str(text).splitlines():
        stripped = line.strip()

        if stripped.startswith("- ") and " | " in stripped:
            out.append(stripped)

    return out


# ---------------------------------------------------------
# A — درخواست تک‌فایلی: رفتار فعلی حفظ شود
# ---------------------------------------------------------
def test_single_file_unchanged(root, model):
    css = os.path.join(root, "style.css")

    pending = {
        "tool": "write_file",
        "args": {
            "path": "style.css",
            "content": NEW_STYLE_CSS,
        },
        "project": "probe",
        "project_root": root,
        "user_request": "single file",
        "context": agent.build_pending_revalidation_context(
            "write_file",
            {"path": "style.css", "content": NEW_STYLE_CSS},
            root,
            "probe",
            allow_write=True,
        ),
    }

    check(
        "A: single-file request gets no multi-file summary",
        agent.build_multi_file_plan_summary(pending) is None,
        safe(agent.build_multi_file_plan_summary(pending)),
    )

    model.scripts = [[
        write_tool("write_file", {
            "path": "style.css",
            "content": NEW_STYLE_CSS,
        }),
        "FINAL: تمام شد.",
    ], ["FINAL: پس از اجرا."]]
    model.calls = 0

    before = snapshot(root)

    answer = agent.run_agent(
        "رنگ عنوان را در style.css تغییر بده",
        "probe",
        allow_write=True,
    )

    check(
        "A: single-file plan text is unchanged in shape",
        "برای اجرا پاسخ دهید: تأیید" in str(answer)
        and "خلاصه Plan چندفایلی" not in str(answer),
        safe(answer),
    )
    check(
        "A: single-file plan still waits for approval",
        agent.get_pending_action() is not None
        and snapshot(root) == before,
        safe(agent.get_pending_action()),
    )

    agent.run_agent("تأیید", "probe", allow_write=True)

    check(
        "A: single-file execute still works",
        read(css) == NEW_STYLE_CSS,
        safe(read(css)),
    )
    agent.clear_pending_action()


# ---------------------------------------------------------
# B — درخواست واقعی چندفایلی: Summary شامل همهٔ targetهای واقعی
# ---------------------------------------------------------
def test_multi_file_summary_covers_real_targets(root, model):
    files = [
        {"path": "index.html", "content": NEW_INDEX_HTML},
        {"path": "style.css", "content": NEW_STYLE_CSS},
        {"path": "app.js", "content": NEW_APP_JS},
    ]

    pending = make_pending(root, "probe", files)
    summary = agent.build_multi_file_plan_summary(pending)

    check(
        "B: multi-file summary exists",
        bool(summary),
        safe(summary),
    )
    check(
        "B: summary lists index.html",
        "- index.html |" in str(summary),
        safe(summary),
    )
    check(
        "B: summary lists style.css",
        "- style.css |" in str(summary),
        safe(summary),
    )
    check(
        "B: summary lists the new file app.js",
        "- app.js |" in str(summary),
        safe(summary),
    )
    check(
        "B: summary says three files",
        "3 فایل" in str(summary),
        safe(summary),
    )
    check(
        "B: existing file is reported as an update",
        "بازنویسی فایل موجود" in str(summary),
        safe(summary),
    )
    check(
        "B: missing file is reported as a new file",
        "ایجاد فایل جدید" in str(summary),
        safe(summary),
    )
    check(
        "B: summary does not dump the full file content",
        NEW_INDEX_HTML.strip() not in str(summary)
        and NEW_STYLE_CSS.strip() not in str(summary),
        safe(summary),
    )
    check(
        "B: summary stays short",
        len(str(summary)) < 1200,
        str(len(str(summary))),
    )


# ---------------------------------------------------------
# C — Grounding: هیچ target ساختگی یا حدسی
# ---------------------------------------------------------
def test_summary_is_grounded(root):
    files = [
        {"path": "index.html", "content": NEW_INDEX_HTML},
        {"path": "style.css", "content": NEW_STYLE_CSS},
    ]

    pending = make_pending(root, "probe", files)
    summary = str(agent.build_multi_file_plan_summary(pending))

    for fabricated in (
        "index.php", "style.scss", "app.js", "config.json",
        "README.md", "package.json",
    ):
        check(
            "C: summary invents no target: " + fabricated,
            fabricated not in summary,
            safe(summary),
        )

    #_summary باید دقیقاً همان targetهای Plan را داشته باشد
    listed = [
        line[2:].split(" | ")[0].strip()
        for line in summary_lines(summary)
    ]

    check(
        "C: summary targets are exactly the plan targets",
        listed == ["index.html", "style.css"],
        safe(listed),
    )

    # Fail Closed: اگر Plan و رکورد Re-validation هم‌خوان نباشند،
    # نباید هیچ خلاصه‌ای ساخته شود.
    tampered = make_pending(root, "probe", files)
    tampered["context"]["batch_paths"] = [
        "index.html", "style.css", "ghost.php",
    ]

    check(
        "C: mismatched re-validation record yields no summary",
        agent.build_multi_file_plan_summary(tampered) is None,
        safe(agent.build_multi_file_plan_summary(tampered)),
    )

    broken = make_pending(root, "probe", files)
    del broken["context"]

    check(
        "C: missing context yields no summary",
        agent.build_multi_file_plan_summary(broken) is None,
    )

    check(
        "C: single-member batch is not a multi-file plan",
        agent.build_multi_file_plan_summary(
            make_pending(root, "probe", [files[0]])
        ) is None,
    )

    check(
        "C: non-batch tool yields no summary",
        agent.build_multi_file_plan_summary({
            "tool": "run_command",
            "args": {"command": "echo hi"},
            "project": "probe",
            "project_root": root,
            "context": {},
        }) is None,
    )


# ---------------------------------------------------------
# D — Approval: Summary قبل از Approval، روی همان Plan canonical
# ---------------------------------------------------------
def test_summary_shown_before_approval(root, model):
    files = [
        {"path": "index.html", "content": NEW_INDEX_HTML},
        {"path": "style.css", "content": NEW_STYLE_CSS},
    ]

    before = snapshot(root)

    model.scripts = [[
        write_tool("write_files", {"files": files}),
        "FINAL: تمام شد.",
    ], ["FINAL: پس از اجرا."]]
    model.calls = 0

    answer = agent.run_agent(
        "ظاهر صفحه را کامل کن",
        "probe",
        allow_write=True,
    )
    text = str(answer)

    check(
        "D: summary is shown to the user",
        "خلاصه Plan چندفایلی" in text,
        safe(text),
    )
    check(
        "D: summary names every real plan target",
        "- index.html |" in text and "- style.css |" in text,
        safe(text),
    )
    summary_at = text.find("خلاصه Plan چندفایلی")
    prompt_at = text.find("برای اجرا پاسخ دهید: تأیید")

    check(
        "D: approval prompt comes after the summary",
        summary_at > 0
        and prompt_at > summary_at,
        "summary@"
        + str(summary_at)
        + " approval@"
        + str(prompt_at)
        + " "
        + safe(text),
    )

    pending = agent.get_pending_action()

    check(
        "D: approval still consumes the canonical plan",
        bool(pending)
        and pending.get("tool") == "write_files"
        and [
            item.get("path")
            for item in pending.get("args", {}).get("files", [])
        ] == ["index.html", "style.css"],
        safe(pending),
    )
    check(
        "D: nothing was written before approval",
        snapshot(root) == before,
        safe(sorted(set(snapshot(root)) ^ set(before))),
    )
    check(
        "D: canonical plan re-validates",
        agent.revalidate_pending_action(pending)[0] is True,
        safe(agent.revalidate_pending_action(pending)[1]),
    )

    agent.run_agent("تأیید", "probe", allow_write=True)


# ---------------------------------------------------------
# E + F — Execute و Verify
# ---------------------------------------------------------
def test_execute_then_verify(root):
    check(
        "E: execute wrote exactly the approved plan",
        read(os.path.join(root, "index.html")) == NEW_INDEX_HTML
        and read(os.path.join(root, "style.css")) == NEW_STYLE_CSS,
        safe(read(os.path.join(root, "style.css"))),
    )
    check(
        "E: approval consumed the pending action",
        agent.get_pending_action() is None,
        safe(agent.get_pending_action()),
    )

    validation = agent.get_last_write_result().get("validation", {})

    check(
        "F: verify still runs after execute",
        validation.get("valid") is True,
        safe(validation),
    )


# ---------------------------------------------------------
# G — Review: خلاصه ممکن است دیده شود، هیچ Mutation نه
# ---------------------------------------------------------
def test_review_stays_read_only(root, model):
    with open(os.path.join(root, "style.css"), "w",
              encoding="utf-8") as fh:
        fh.write(ORIGINAL_STYLE_CSS)

    before = snapshot(root)

    model.scripts = [[
        write_tool("write_files", {"files": [
            {"path": "index.html", "content": NEW_INDEX_HTML},
            {"path": "style.css", "content": NEW_STYLE_CSS},
        ]}),
        "FINAL: فقط بررسی.",
    ]]
    model.calls = 0

    answer = agent.run_agent(
        "ظاهر صفحه را بررسی کن",
        "probe",
        allow_write=False,
    )

    check(
        "G: review created no pending action",
        agent.get_pending_action() is None,
        safe(agent.get_pending_action()),
    )
    check(
        "G: review mutated nothing",
        snapshot(root) == before,
        safe(sorted(set(snapshot(root)) ^ set(before))),
    )
    check(
        "G: review does not ask for approval",
        "Plan پیشنهادی (نیازمند تأیید)" not in str(answer),
        safe(answer),
    )


# ---------------------------------------------------------
# H — General Mode: بدون مسیر Mutation
# ---------------------------------------------------------
def test_general_mode(base, model):
    root = os.path.join(base, "project")

    model.scripts = [[
        write_tool("write_files", {"files": [
            {"path": "index.html", "content": NEW_INDEX_HTML},
            {"path": "style.css", "content": NEW_STYLE_CSS},
        ]}),
        "FINAL: تمام شد.",
    ]]
    model.calls = 0

    before = snapshot(root)

    answer = agent.run_agent(
        "ظاهر صفحه را تغییر بده",
        "general",
        allow_write=True,
    )

    check(
        "H: general mode refused the multi-file mutation",
        "انجام شد" not in str(answer),
        safe(answer),
    )
    check(
        "H: general mode created no pending action",
        agent.get_pending_action() is None,
        safe(agent.get_pending_action()),
    )
    check(
        "H: general mode changed nothing on disk",
        snapshot(root) == before,
        safe(sorted(set(snapshot(root)) ^ set(before))),
    )


# ---------------------------------------------------------
# I — Legacy: مسیر مستقیم همچنان Fail-Closed
# ---------------------------------------------------------
def test_legacy_still_refuses(root, model):
    with open(os.path.join(root, "style.css"), "w",
              encoding="utf-8") as fh:
        fh.write(ORIGINAL_STYLE_CSS)

    before = snapshot(root)

    model.scripts = [["BEGIN_FILE\n" + NEW_STYLE_CSS + "END_FILE"]]
    model.calls = 0

    answer = agent.run_agent_legacy(
        "style.css را تغییر بده",
        project_name="probe",
        allow_write=True,
    )

    check(
        "I: legacy direct write is still refused",
        "مجاز نیست" in str(answer),
        safe(answer),
    )
    check(
        "I: legacy created no pending action",
        agent.get_pending_action() is None,
        safe(agent.get_pending_action()),
    )
    check(
        "I: legacy wrote nothing",
        snapshot(root) == before,
        safe(sorted(set(snapshot(root)) ^ set(before))),
    )


# ---------------------------------------------------------
# J — Regressions: قراردادهای قبلی سر جای خود
# ---------------------------------------------------------
def test_previous_contracts(root, model):
    # J1) Plan تک‌فایلی قدیمی هنوز متن تأیید را دارد.
    text = agent.build_pending_plan_text(
        "write_file",
        {"path": "style.css", "content": NEW_STYLE_CSS},
        root,
        write_mode=True,
    )

    check(
        "J: single-file plan text keeps its approval prompt",
        "برای اجرا پاسخ دهید: تأیید" in text
        and "خلاصه Plan چندفایلی" not in text,
        safe(text),
    )

    # J2) متن Plan قدیمی برای Batch هنوز فهرست فایل‌ها را دارد.
    batch_text = agent.build_pending_plan_text(
        "write_files",
        {"files": [
            {"path": "index.html", "content": NEW_INDEX_HTML},
            {"path": "style.css", "content": NEW_STYLE_CSS},
        ]},
        root,
        write_mode=True,
    )

    check(
        "J: batch plan text keeps its file list",
        "فایل‌های پیشنهادی:" in batch_text
        and "- index.html" in batch_text
        and "- style.css" in batch_text,
        safe(batch_text),
    )

    # J3) حالت فقط‌بررسی همچنان Mutation نمی‌سازد.
    read_only_text = agent.build_pending_plan_text(
        "write_files",
        {"files": [
            {"path": "index.html", "content": NEW_INDEX_HTML},
            {"path": "style.css", "content": NEW_STYLE_CSS},
        ]},
        root,
        write_mode=False,
    )

    check(
        "J: read-only plan text never asks for approval",
        "برای اجرا پاسخ دهید: تأیید" not in read_only_text,
        safe(read_only_text),
    )

    # J4) خلاصه هیچ Approval/Re-validation را دور نمی‌زند؛ خودش
    # یک no-op روی Plan است.
    files = [
        {"path": "index.html", "content": NEW_INDEX_HTML},
        {"path": "style.css", "content": NEW_STYLE_CSS},
    ]

    pending = make_pending(root, "probe", files)
    before = json.dumps(pending, sort_keys=True, ensure_ascii=False)

    agent.build_multi_file_plan_summary(pending)

    after = json.dumps(pending, sort_keys=True, ensure_ascii=False)

    check(
        "J: summary does not mutate the canonical plan",
        before == after,
        safe(set(before.split(", ")) ^ set(after.split(", "))),
    )

    # J5) Re-validation دست‌نخورده: دستکاری args بعد از Plan رد
    # می‌شود.
    tampered = make_pending(root, "probe", files)
    tampered["args"]["files"][0]["content"] = "nope"

    check(
        "J: re-validation still rejects a tampered plan",
        agent.revalidate_pending_action(tampered)[0] is False,
        safe(agent.revalidate_pending_action(tampered)[1]),
    )


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    base = tempfile.mkdtemp(prefix="stage4_summary_")
    model = ScriptedModel()
    root, saved = setup(base, model)

    try:
        test_single_file_unchanged(root, model)
        test_multi_file_summary_covers_real_targets(root, model)
        test_summary_is_grounded(root)
        test_summary_shown_before_approval(root, model)
        test_execute_then_verify(root)
        test_review_stays_read_only(root, model)
        test_general_mode(base, model)
        test_legacy_still_refuses(root, model)
        test_previous_contracts(root, model)
    finally:
        restore(saved)
        shutil.rmtree(base, ignore_errors=True)

    passed = sum(1 for _, ok in results if ok)
    print("RESULT: {}/{} passed".format(passed, len(results)))

    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
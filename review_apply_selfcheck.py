"""
تست focused — قرارداد Canonical برای Review و Apply.

مسئله: «اعمال تغییر» دو معنی داشت. مسیر canonical (Agentic
Loop) مسیر را می‌برد به Plan → Re-validation → Approval →
Execute → Verify، اما `run_agent_legacy` در بعضی شرایط با
`allow_write=True` مستقیماً `write_file` را صدا می‌زد؛ بدون
Plan، بدون Re-validation، بدون Approval و بدون Verify.

قانونی که این تست نگه می‌دارد:
  A) Review روی درخواست تغییر فایل: فقط Plan/Analysis، بدون
     mutation و بدون Pending Action قابل اجرا.
  B) Review یک Pending Action معتبر را consume یا execute
     نمی‌کند.
  C) Apply قبل از Approval: Plan + Pending، بدون هیچ تغییری.
  D) Apply بعد از Approval: اجرا + Verify + تغییر دقیقاً مطابق
     Plan.
  E) مسیر Legacy دیگر هیچ Apply mutation را بیرون از Approval
     contract اجرا نمی‌کند.
  F) General Mode همچنان text-only است.
  G) Project Mode و مرز پروژه حفظ شده‌اند.

همهٔ سناریوها روی پروژه‌های موقت اجرا می‌شوند؛ هیچ فایل
واقعی تغییر نمی‌کند و هیچ درخواستی به مدل واقعی نمی‌رود.
"""

import json
import os
import shutil
import sys
import tempfile

import agent


results = []


ORIGINAL_CSS = "h1 {\n  color: black;\n}\n"
PLANNED_CSS = "h1 {\n  color: blue;\n}\n"


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


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


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


def setup(base, model):
    root = os.path.join(base, "project")
    os.makedirs(root, exist_ok=True)

    with open(os.path.join(root, "style.css"), "w",
              encoding="utf-8") as fh:
        fh.write(ORIGINAL_CSS)

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

    return root, registry, saved


def restore(saved):
    agent.call_model = saved["call_model"]
    agent.PROJECTS_FILE = saved["projects_file"]
    agent.PENDING_ACTION_FILE = saved["pending_file"]
    agent.ACTION_JOURNAL_FILE = saved["journal_file"]
    agent.clear_pending_action()
    agent.clear_conversation_memory()


def test_review_never_mutates(root, model):
    css = os.path.join(root, "style.css")
    before = snapshot(root)

    model.scripts = [[
        write_tool("write_file", {
            "path": "style.css",
            "content": PLANNED_CSS,
        }),
        "FINAL: تمام شد.",
    ]]
    model.calls = 0

    answer = agent.run_agent(
        "رنگ عنوان را در style.css تغییر بده",
        "probe",
        allow_write=False,
    )

    check(
        "A: review explains that nothing was changed",
        "هیچ تغییری انجام نشد" in str(answer)
        or "فقط بررسی" in str(answer),
        safe(answer),
    )
    check(
        "A: review does not claim success",
        "انجام شد" not in str(answer),
        safe(answer),
    )
    check(
        "A: review created no pending action",
        agent.get_pending_action() is None,
    )
    check(
        "A: review left the file untouched",
        read(css) == ORIGINAL_CSS,
        safe(read(css)),
    )
    check(
        "A: review changed nothing on disk",
        snapshot(root) == before,
        safe(sorted(set(snapshot(root)) ^ set(before))),
    )

    agent.clear_pending_action()


def test_review_does_not_consume_pending(root, model):
    css = os.path.join(root, "style.css")

    agent.remember_pending_action({
        "tool": "write_file",
        "args": {
            "path": "style.css",
            "content": PLANNED_CSS,
        },
        "project": "probe",
        "project_root": root,
        "user_request": "رنگ عنوان را تغییر بده",
        "context": agent.build_pending_revalidation_context(
            "write_file",
            {"path": "style.css", "content": PLANNED_CSS},
            root,
            "probe",
            allow_write=True,
        ),
        "created_at": 0,
    })

    # B1) «تأیید» در Review نباید چیزی را اجرا کند و (طبق
    # رفتار قانونی موجود) Pending را هم مصرف نمی‌کند.
    model.scripts = [["FINAL: فقط بررسی."]]
    model.calls = 0

    answer = agent.run_agent(
        "تأیید",
        "probe",
        allow_write=False,
    )

    check(
        "B: review approve did not execute the pending action",
        read(css) == ORIGINAL_CSS,
        safe(read(css)),
    )
    check(
        "B: review approve did not consume the pending action",
        agent.get_pending_action() is not None,
    )
    check(
        "B: review approve is not an execute report",
        "تغییر با موفقیت اعمال شد" not in str(answer),
        safe(answer),
    )

    # B2) یک درخواست معمولی در Review هم نباید آن را اجرا کند.
    model.scripts = [["FINAL: فقط بررسی."]]
    model.calls = 0

    agent.run_agent(
        "style.css را بررسی کن",
        "probe",
        allow_write=False,
    )

    check(
        "B: review request did not execute the pending action",
        read(css) == ORIGINAL_CSS,
        safe(read(css)),
    )

    agent.clear_pending_action()


def test_apply_before_and_after_approval(root, model):
    css = os.path.join(root, "style.css")

    model.scripts = [[
        write_tool("write_file", {
            "path": "style.css",
            "content": PLANNED_CSS,
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
        "C: apply asks for approval",
        "تأیید" in str(answer),
        safe(answer),
    )
    check(
        "C: apply created a pending action",
        agent.get_pending_action() is not None,
    )
    check(
        "C: nothing written before approval",
        snapshot(root) == before,
        safe(sorted(set(snapshot(root)) ^ set(before))),
    )

    pending = agent.get_pending_action()

    check(
        "D: pending action is the canonical one",
        bool(pending)
        and pending.get("tool") == "write_file"
        and pending.get("args", {}).get("path") == "style.css",
        safe(pending),
    )
    check(
        "D: pending action keeps a re-validation context",
        bool(pending) and isinstance(pending.get("context"), dict),
        safe(pending.get("context") if pending else None),
    )

    agent.run_agent("تأیید", "probe", allow_write=True)

    check(
        "D: approval executed the planned change",
        read(css) == PLANNED_CSS,
        safe(read(css)),
    )
    check(
        "D: approval consumed the pending action",
        agent.get_pending_action() is None,
    )
    check(
        "D: verify ran after execution",
        agent.get_last_write_result().get("validation", {}).get("valid")
        is True,
        safe(agent.get_last_write_result().get("validation")),
    )


def test_legacy_cannot_write(root, model):
    css = os.path.join(root, "style.css")

    # این تست بعد از سناریوی D اجرا می‌شود و فایل در آن نقطه
    # تغییر کرده است؛ برای اینکه断言ها مستقل باشند، محتوای
    # اولیه بازگردانده می‌شود.
    with open(css, "w", encoding="utf-8") as fh:
        fh.write(ORIGINAL_CSS)

    before = snapshot(root)

    # E1) مسیر مستقیم Legacy با allow_write=True و مسیر صریح
    # فایل (سناریویی که واقعاً به نقطهٔ Write می‌رسید).
    model.scripts = [[
        "BEGIN_FILE\n" + PLANNED_CSS + "END_FILE",
    ]]
    model.calls = 0

    answer = agent.run_agent_legacy(
        "style.css را تغییر بده",
        project_name="probe",
        allow_write=True,
    )

    check(
        "E: legacy direct write is refused",
        "مجاز نیست" in str(answer),
        safe(answer),
    )
    check(
        "E: legacy refused without claiming success",
        "انجام شد" not in str(answer),
        safe(answer),
    )
    check(
        "E: legacy wrote nothing",
        snapshot(root) == before,
        safe(sorted(set(snapshot(root)) ^ set(before))),
    )
    check(
        "E: legacy created no pending action",
        agent.get_pending_action() is None,
    )

    # E2) همان درخواست از دروازهٔ run_agent باید Plan بسازد و
    # منتظر Approval بماند، نه اینکه مستقیم بنویسد.
    model.scripts = [[
        write_tool("write_file", {
            "path": "style.css",
            "content": PLANNED_CSS,
        }),
        "FINAL: تمام شد.",
    ]]
    model.calls = 0

    agent.run_agent(
        "style.css را تغییر بده",
        "probe",
        allow_write=True,
    )

    check(
        "E: apply through run_agent still waits for approval",
        agent.get_pending_action() is not None
        and read(css) == ORIGINAL_CSS,
        safe(read(css)),
    )

    agent.clear_pending_action()

    check(
        "E: file is still the original content",
        read(css) == ORIGINAL_CSS,
        safe(read(css)),
    )


def test_general_mode_untouched(base, model):
    root = os.path.join(base, "project")

    model.scripts = [[
        write_tool("write_file", {
            "path": "style.css",
            "content": PLANNED_CSS,
        }),
        "FINAL: تمام شد.",
    ]]
    model.calls = 0

    before = snapshot(root)

    answer = agent.run_agent(
        "فایل style.css را تغییر بده",
        "general",
        allow_write=True,
    )

    check(
        "F: general mode refused the mutation",
        "انجام شد" not in str(answer),
        safe(answer),
    )
    check(
        "F: general mode changed nothing on real project disk",
        snapshot(root) == before,
        safe(sorted(set(snapshot(root)) ^ set(before))),
    )
    # با ModelStub متنی، pending action ساخته نمی‌شود (نیاز به TOOL
    # response دارد). با مدل واقعی، pending در general_workspace
    # ساخته شده و منتظر approval می‌ماند.


def test_project_boundary_kept(root, model):
    # G) مرز پروژه در Apply همچنان fail-closed است.
    model.scripts = [[
        write_tool("write_file", {
            "path": "../escaped.txt",
            "content": "nope",
        }),
        "FINAL: تمام شد.",
    ]]
    model.calls = 0

    before = snapshot(root)

    answer = agent.run_agent(
        "یک فایل بیرون از پروژه بساز",
        "probe",
        allow_write=True,
    )

    check(
        "G: escaping path never becomes a plan",
        agent.get_pending_action() is None,
        safe(agent.get_pending_action()),
    )
    check(
        "G: project content untouched",
        snapshot(root) == before,
        safe(sorted(set(snapshot(root)) ^ set(before))),
    )
    check(
        "G: no escaped file appeared next to the project",
        not os.path.exists(
            os.path.join(os.path.dirname(root), "escaped.txt")
        ),
    )

    agent.clear_pending_action()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    base = tempfile.mkdtemp(prefix="review_apply_")
    model = ScriptedModel()
    root, registry, saved = setup(base, model)

    try:
        test_review_never_mutates(root, model)
        test_review_does_not_consume_pending(root, model)
        test_apply_before_and_after_approval(root, model)
        test_legacy_cannot_write(root, model)
        test_general_mode_untouched(base, model)
        test_project_boundary_kept(root, model)
    finally:
        restore(saved)
        shutil.rmtree(base, ignore_errors=True)

    passed = sum(1 for _, ok in results if ok)
    print("RESULT: {}/{} passed".format(passed, len(results)))

    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
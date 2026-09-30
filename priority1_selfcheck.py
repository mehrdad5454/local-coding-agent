"""
تست focused — اولویت ۱: یکپارچگی Plan و Pending Action.

قانونی که این تست نگه می‌دارد:

    هر Plan که به کاربر «نیازمند تأیید» گفته می‌شود و دستور
    «تأیید» نشان می‌دهد، باید یک Pending Action واقعی داشته
    باشد؛ و اگر Pending ساخته نمی‌شود، نباید متن Approval
    جعلی نمایش داده شود.

این تست هیچ مسیر امنی را دور نمی‌زند: فقط از
remember_pending_action / handle_pending_action_request /
revalidate_pending_action موجود استفاده می‌کند.
"""

import os
import shutil
import tempfile
import threading

import agent


APPROVE_PROMPT = "برای اجرا پاسخ دهید: تأیید"
APPROVAL_HEADER = "نیازمند تأیید"

results = []


def check(name, condition):
    results.append((name, bool(condition)))
    print("[{}] {}".format("PASS" if condition else "FAIL", name))
    return bool(condition)


def make_pending(temp_root, tool="edit_file", args=None, project="probe"):
    # Pending واقعی با context معتبر Re-validation، دقیقاً
    # مطابق ساختاری که خود agent.py می‌سازد.
    if args is None:
        args = {"path": "p1.txt", "old": "a", "new": "b"}

    pending = {
        "tool": tool,
        "args": args,
        "project": project,
        "project_root": temp_root,
        "user_request": "probe",
        "context": agent.build_pending_revalidation_context(
            tool, args, temp_root, project, allow_write=True
        ),
    }
    agent.remember_pending_action(pending)
    return pending


def run_loop(temp_root, allow_write, tool_name, args_json,
             project="probe", max_steps=3, setup_json=None):
    # اجرای واقعی run_loop_steps با مدل شبیه‌سازی‌شده.
    #
    # setup_json اختیاری است: اگر داده شود، اول یک ابزار
    # فقط‌خواندنی اجرا می‌شود تا transcript واقعی بسازد. این برای
    # edit_file لازم است، چون یک ویرایش فقط وقتی Plan می‌سازد که
    # فایل هدف قبلاً خوانده شده و `old` در محتوای واقعی آن باشد.
    calls = {"n": 0}

    def stub(system_prompt, user_prompt, *a, **kw):
        calls["n"] += 1
        if setup_json and calls["n"] == 1:
            return "THOUGHT: read first\nTOOL: read_file\nARGS: %s" % (
                setup_json
            )
        if calls["n"] == (2 if setup_json else 1):
            return "THOUGHT: probe\nTOOL: %s\nARGS: %s" % (
                tool_name, args_json
            )
        return "FINAL: probe done"

    saved_model = agent.call_model
    agent.call_model = stub
    try:
        out = agent.run_loop_steps(
            user_request="probe request",
            project_root=temp_root,
            project_name=project,
            allow_write=allow_write,
            transcript=[],
            observations=[],
            steps_used=0,
            correction_attempts=0,
            max_steps=max_steps,
        )
    finally:
        agent.call_model = saved_model

    return out, calls["n"]


def test_plan_creates_pending(temp_root):
    # 1) Plan قابل اجرا باید Pending Action واقعی بسازد.
    #
    # edit_file فقط روی فایلی Plan می‌سازد که قبلاً در همین حلقه
    # خوانده شده و `old` واقعاً داخل محتوای آن است؛ پس فایل روی
    # دیسک ساخته و ابتدا با read_file خوانده می‌شود.
    agent.clear_pending_action()
    target = os.path.join(temp_root, "p1.txt")

    with open(target, "w", encoding="utf-8") as fh:
        fh.write("a")

    out, _ = run_loop(
        temp_root, True, "edit_file",
        '{"path":"p1.txt","old":"a","new":"b"}',
        setup_json='{"path":"p1.txt"}',
    )
    pending = agent.get_pending_action()

    check("P1: writable plan creates a real pending action",
          pending is not None)
    check("P1: pending tool matches the plan",
          bool(pending) and pending.get("tool") == "edit_file")
    check("P1: pending carries re-validation context",
          bool(pending)
          and isinstance(pending.get("context"), dict))
    check("P1: report shows the approval prompt",
          APPROVE_PROMPT in str(out))
    check("P1: report header says approval required",
          APPROVAL_HEADER in str(out))


def test_read_mode_no_fake_approval(temp_root):
    # 2) بدون مجوز Write نباید پیام Approval جعلی تولید شود.
    agent.clear_pending_action()

    out, _ = run_loop(
        temp_root, False, "edit_file",
        '{"path":"p2.txt","old":"a","new":"b"}',
    )

    check("P1: read mode creates no pending action",
          agent.get_pending_action() is None)
    check("P1: read mode shows no approve prompt",
          APPROVE_PROMPT not in str(out))
    check("P1: read mode does not claim approval required",
          APPROVAL_HEADER not in str(out))
    check("P1: read mode still explains write mode",
          "Write" in str(out))


def test_truthy_but_not_true_is_fail_closed(temp_root):
    # حالت مرزی: مقدار truthy ولی غیر boolean. Gate فقط
    # boolean True را می‌پذیرد، پس نباید متن Approval نشان
    # داده شود در حالی که Pending ساخته نمی‌شود.
    agent.clear_pending_action()

    out, _ = run_loop(
        temp_root, 1, "edit_file",
        '{"path":"p3.txt","old":"a","new":"b"}',
    )

    check("P1: non-bool truthy creates no pending action",
          agent.get_pending_action() is None)
    check("P1: non-bool truthy shows no approve prompt",
          APPROVE_PROMPT not in str(out))
    check("P1: non-bool truthy not marked as needing approval",
          APPROVAL_HEADER not in str(out))


def test_approval_executes_existing_path(temp_root):
    # 3) Approval از مسیر موجود اجرا شود (بدون بازنویسی).
    target = os.path.join(temp_root, "p1.txt")
    if os.path.exists(target):
        os.remove(target)

    make_pending(temp_root, tool="write_file", args={
        "path": "p1.txt", "content": "approved-content",
    })

    result = agent.handle_pending_action_request(
        "تأیید", allow_write=True
    )

    check("P1: approval via existing gate returns a report",
          result is not None)
    check("P1: approval wrote the file", os.path.exists(target))
    check("P1: approval wrote the expected content",
          os.path.exists(target)
          and open(target, encoding="utf-8").read()
          == "approved-content")
    check("P1: approval consumed the pending action",
          agent.get_pending_action() is None)


def test_cancel_causes_no_mutation(temp_root):
    # 4) Cancel نباید باعث Mutation شود.
    target = os.path.join(temp_root, "cancel.txt")
    if os.path.exists(target):
        os.remove(target)

    make_pending(temp_root, tool="write_file", args={
        "path": "cancel.txt", "content": "should-not-exist",
    })

    result = agent.handle_pending_action_request(
        "لغو", allow_write=True
    )

    check("P1: cancel returns a report", result is not None)
    check("P1: cancel wrote nothing", not os.path.exists(target))
    check("P1: cancel cleared the pending action",
          agent.get_pending_action() is None)


def test_verify_still_runs(temp_root):
    # 5) Verify بعد از Mutation همچنان اجرا شود.
    target = os.path.join(temp_root, "verify.txt")
    if os.path.exists(target):
        os.remove(target)

    make_pending(temp_root, tool="write_file", args={
        "path": "verify.txt", "content": "verify-me",
    })

    saved_model = agent.call_model
    agent.call_model = lambda *a, **k: "FINAL: verified"
    try:
        result = agent.handle_pending_action_request(
            "تأیید", allow_write=True
        )
    finally:
        agent.call_model = saved_model

    check("P1: verify path produced a report", result is not None)
    check("P1: verify path wrote the file", os.path.exists(target))
    check("P1: verify section present in report",
          "Verify" in str(result))





def test_safety_layers_still_active(temp_root):
    # 6) Emergency Stop و Re-validation همچنان فعال باشند.
    # 6a) Re-validation: دستکاری args بعد از Plan باید رد شود.
    agent.clear_pending_action()
    make_pending(temp_root, tool="write_file", args={
        "path": "reval.txt", "content": "original",
    })

    pending = agent.get_pending_action()
    pending["args"] = {"path": "reval.txt", "content": "tampered"}
    agent.remember_pending_action(pending)

    ok, reason, _ = agent.revalidate_pending_action(
        agent.get_pending_action()
    )

    check("P1: re-validation rejects tampered args", ok is False)
    check("P1: re-validation gave a reason", bool(reason))
    check("P1: tampered plan wrote nothing",
          not os.path.exists(os.path.join(temp_root, "reval.txt")))

    # 6b) Emergency Stop: با stop فعال، approve نباید اجرا کند.
    agent.clear_pending_action()
    target = os.path.join(temp_root, "stopped.txt")
    if os.path.exists(target):
        os.remove(target)

    make_pending(temp_root, tool="write_file", args={
        "path": "stopped.txt", "content": "no",
    })

    stop_event = threading.Event()
    stop_event.set()

    result = agent.handle_pending_action_request(
        "تأیید", allow_write=True, stop_event=stop_event
    )

    check("P1: emergency stop blocks execution",
          "Emergency Stop" in str(result))
    check("P1: emergency stop wrote nothing",
          not os.path.exists(target))
    check("P1: emergency stop keeps the plan pending",
          agent.get_pending_action() is not None)
    agent.clear_pending_action()


def main():
    temp_root = tempfile.mkdtemp(prefix="p1-")
    saved = {
        "file": agent.PENDING_ACTION_FILE,
        "project_path": agent.get_project_path,
    }
    agent.PENDING_ACTION_FILE = os.path.join(temp_root, "p.json")
    agent.get_project_path = lambda name: temp_root

    try:
        test_plan_creates_pending(temp_root)
        test_read_mode_no_fake_approval(temp_root)
        test_truthy_but_not_true_is_fail_closed(temp_root)
        test_approval_executes_existing_path(temp_root)
        test_cancel_causes_no_mutation(temp_root)
        test_verify_still_runs(temp_root)
        test_safety_layers_still_active(temp_root)
    finally:
        agent.PENDING_ACTION_FILE = saved["file"]
        agent.get_project_path = saved["project_path"]
        agent.clear_pending_action()
        shutil.rmtree(temp_root, ignore_errors=True)

    passed = sum(1 for _, ok in results if ok)
    print("")
    print("RESULT: {}/{} passed".format(passed, len(results)))

    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()


"""تست کوتاه دکمه‌های تأیید / لغو در UI."""

import os
import shutil
import tempfile
import time

import agent
import web_agent


def make_pending_action(temp_root, args=None):
    # Pending Action کاملاً مطابق ساختار واقعی agent.py
    # ساخته می‌شود تا Re-validation آن عبور کند.
    if args is None:
        args = {"path": "demo.txt", "content": "hello"}

    tool = "write_file"

    pending = {
        "tool": tool,
        "args": args,
        "project": "selftest",
        "project_root": temp_root,
        "user_request": "selftest request",
        "context": agent.build_pending_revalidation_context(
            tool,
            args,
            temp_root,
            "selftest",
            allow_write=True,
        ),
    }

    agent.remember_pending_action(pending)
    return pending


def wait_for_thread():
    for _ in range(200):
        if not web_agent.is_agent_running():
            return True
        time.sleep(0.05)
    return False


def check(name, condition):
    print("[{}] {}".format(
        "PASS" if condition else "FAIL", name
    ))
    return bool(condition)


def post(client, action, mode):
    return client.post("/", data={
        "approval_action": action,
        "project": "selftest",
        "mode": mode,
    })


def test1_buttons(results, temp_root):
    # Plan نمایش داده می‌شود و دو دکمه وجود دارند.
    make_pending_action(temp_root)
    web_agent.set_agent_status(web_agent.AGENT_STATUS_IDLE, "")
    web_agent.agent_thread = None

    with web_agent.app.test_client() as client:
        html = client.get("/").get_data(as_text=True)
        status_json = client.get("/status").get_json()

    results.append(check(
        "Test1: approval box rendered",
        'id="approvalBox"' in html,
    ))
    results.append(check(
        "Test1: approve button present",
        'value="approve"' in html and "تأیید" in html,
    ))
    results.append(check(
        "Test1: cancel button present",
        'value="cancel"' in html and "لغو" in html,
    ))
    results.append(check(
        "Test1: form posts to same endpoint",
        'action="/"' in html,
    ))
    results.append(check(
        "Test1: /status exposes pending_approval",
        status_json.get("pending_approval") is True,
    ))


def test2_cancel(results, temp_root):
    # کلیک «لغو»: Plan لغو می‌شود و هیچ write انجام نمی‌شود.
    target = os.path.join(temp_root, "demo.txt")

    results.append(check(
        "Test2: file absent before cancel",
        not os.path.exists(target),
    ))

    web_agent.agent_thread = None
    with web_agent.app.test_client() as client:
        post(client, "cancel", "write")

    wait_for_thread()

    results.append(check(
        "Test2: pending cleared after cancel",
        agent.get_pending_action() is None,
    ))
    results.append(check(
        "Test2: no file written on cancel",
        not os.path.exists(target),
    ))
    results.append(check(
        "Test2: cancel report returned",
        "لغو شد" in (web_agent.last_result or ""),
    ))


def test3_approve(results, temp_root, forbidden_call_model):
    # کلیک «تأیید»: همان Approval موجود اجرا می‌شود و
    # درخواست به‌عنوان درخواست جدید Agent وارد Loop نمی‌شود.
    make_pending_action(temp_root)
    web_agent.agent_thread = None

    loop_entered = []
    original_loop = agent.run_agentic_loop

    def spy_loop(*a, **k):
        loop_entered.append(True)
        return "LOOP-ENTERED"

    agent.run_agentic_loop = spy_loop
    agent.call_model = forbidden_call_model

    target = os.path.join(temp_root, "demo.txt")

    with web_agent.app.test_client() as client:
        post(client, "approve", "write")

    wait_for_thread()

    agent.run_agentic_loop = original_loop

    results.append(check(
        "Test3: approval executed the pending action",
        os.path.exists(target),
    ))
    results.append(check(
        "Test3: request did NOT enter agent loop",
        not loop_entered,
    ))
    results.append(check(
        "Test3: report is the approved-action result",
        "write_file" in (web_agent.last_result or ""),
    ))
    results.append(check(
        "Test3: not treated as a new agent request",
        "LOOP-ENTERED" not in str(web_agent.last_result),
    ))
    results.append(check(
        "Test3: pending consumed",
        agent.get_pending_action() is None,
    ))

    with web_agent.app.test_client() as client:
        html_after = client.get("/").get_data(as_text=True)

    results.append(check(
        "Test3: approval box gone after execution",
        'id="approvalBox"' not in html_after,
    ))


def test4_read_mode(results, temp_root, original_call_model):
    # حالت read نباید بنویسد: همان رفتار Fail-closed موجود.
    make_pending_action(temp_root, args={
        "path": "readmode.txt",
        "content": "x",
    })
    web_agent.agent_thread = None
    agent.call_model = original_call_model

    with web_agent.app.test_client() as client:
        post(client, "approve", "read")

    wait_for_thread()

    results.append(check(
        "Test4: read mode does not write",
        not os.path.exists(os.path.join(temp_root, "readmode.txt")),
    ))
    results.append(check(
        "Test4: read mode keeps plan pending",
        agent.get_pending_action() is not None,
    ))


def main():
    results = []
    temp_root = tempfile.mkdtemp(prefix="approval-ui-")

    saved = {
        "path": agent.PENDING_ACTION_FILE,
        "project_path": agent.get_project_path,
        "call_model": agent.call_model,
        "loop": agent.run_agentic_loop,
    }

    # مسیرها به پوشه موقت منتقل می‌شوند تا پروژه واقعی
    # دست‌نخورده بماند.
    agent.PENDING_ACTION_FILE = os.path.join(
        temp_root, "pending-action.tmp.json"
    )
    agent.get_project_path = lambda name: temp_root

    def forbidden_call_model(*a, **k):
        raise AssertionError(
            "call_model was invoked: request entered agent loop"
        )

    try:
        test1_buttons(results, temp_root)
        test2_cancel(results, temp_root)
        test3_approve(results, temp_root, forbidden_call_model)
        test4_read_mode(results, temp_root, saved["call_model"])
    finally:
        agent.PENDING_ACTION_FILE = saved["path"]
        agent.get_project_path = saved["project_path"]
        agent.call_model = saved["call_model"]
        agent.run_agentic_loop = saved["loop"]
        agent.clear_pending_action()
        shutil.rmtree(temp_root, ignore_errors=True)

    passed = sum(1 for r in results if r)

    print("")
    print("RESULT: {}/{} passed".format(passed, len(results)))

    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()


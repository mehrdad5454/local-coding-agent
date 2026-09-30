"""
تست focused — اولویت ۲: نام ابزار نامعتبر.

قانون: یک نام ابزار نامعتبر (مثل «edit») نباید
  - یک Step کامل از بودجه‌ی حلقه را مصرف کند،
  - باعث mutation شود،
  - یا حلقه را بی‌نهایت بچرخاند.

و باید با پیشنهاد نزدیک‌ترین نام معتبر («edit_file»)
به مدل برگردد تا خودش اصلاح کند.

این تست alias دائمی اضافه نمی‌کند و نام ابزارهای
Registry را تغییر نمی‌دهد؛ فقط رفتار رد کردن نام نامعتبر
را می‌سنجد.
"""

import os
import shutil
import tempfile

import agent


results = []


def check(name, condition):
    results.append((name, bool(condition)))
    print("[{}] {}".format("PASS" if condition else "FAIL", name))
    return bool(condition)


def run_loop_with(tool_script, temp_root, allow_write=True,
                  project="probe", max_steps=6):
    # tool_script: لیست خروجی‌های پیاپی مدل.
    # هر عضو رشته است یا (tool, args) برای ساخت پاسخ.
    state = {"i": 0}

    def stub(system_prompt, user_prompt, *a, **kw):
        idx = state["i"]
        state["i"] += 1

        if idx >= len(tool_script):
            return "FINAL: done"

        item = tool_script[idx]

        if isinstance(item, tuple):
            tool, args = item
            return "THOUGHT: t\nTOOL: %s\nARGS: %s" % (tool, args)

        return item

    saved = agent.call_model
    agent.call_model = stub
    observations = []

    try:
        out = agent.run_loop_steps(
            user_request="probe request",
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


def test_edit_suggests_edit_file():
    # 1) «edit» باید به «edit_file» هدایت شود.
    check("P2: edit -> edit_file",
          agent.suggest_tool_name("edit") == "edit_file")
    check("P2: write -> write_file",
          agent.suggest_tool_name("write") == "write_file")
    check("P2: unknown name has no suggestion",
          agent.suggest_tool_name("xyzzy") is None)
    check("P2: valid name returns itself",
          agent.suggest_tool_name("edit_file") == "edit_file")
    check("P2: non-string is handled",
          agent.suggest_tool_name(None) is None)
    check("P2: edit_file still in registry",
          "edit_file" in agent.TOOL_REGISTRY)
    check("P2: edit is NOT aliased in registry",
          "edit" not in agent.TOOL_REGISTRY)


def test_invalid_tool_returns_feedback(temp_root):
    # 2) نام کاملاً ناشناخته باید پیام invalid tool بگیرد.
    out, obs, _ = run_loop_with(
        ["THOUGHT: t\nTOOL: xyzzy\nARGS: {}", "FINAL: done"],
        temp_root,
    )

    joined = " ".join(obs)
    check("P2: invalid tool reported to the model",
          "نامعتبر" in joined)
    check("P2: invalid name echoed back", "xyzzy" in joined)
    check("P2: valid names listed in feedback",
          "edit_file" in joined)
    check("P2: no suggestion offered for unknown name",
          "احتمالاً منظورت" not in joined)
    check("P2: invalid tool did not crash the loop",
          "xyzzy" not in agent.TOOL_REGISTRY)


def test_edit_suggests_in_feedback(temp_root):
    # 3) «edit» باید در feedback به «edit_file» هدایت شود.
    _, obs, _ = run_loop_with(
        ["THOUGHT: t\nTOOL: edit\nARGS: {\"path\":\"x.txt\"}",
         "FINAL: done"],
        temp_root,
    )

    joined = " ".join(obs)
    check("P2: edit reported invalid", "نامعتبر" in joined)
    check("P2: edit_file suggested in feedback",
          "edit_file" in joined)
    check("P2: suggestion phrase present",
          "احتمالاً منظورت" in joined)


def test_invalid_tool_does_not_consume_step(temp_root):
    # 4) تلاش نامعتبر نباید Step مصرف کند: با max_steps=1
    #    باید هنوز فرصت اصلاح داشته باشد.
    out, obs, calls = run_loop_with(
        ["THOUGHT: t\nTOOL: edit\nARGS: {\"path\":\"x.txt\"}",
         "THOUGHT: t\nTOOL: edit_file\nARGS: "
         "{\"path\":\"s1.txt\",\"old\":\"a\",\"new\":\"b\"}",
         "FINAL: done"],
        temp_root,
        max_steps=1,
    )

    pending = agent.get_pending_action()
    check("P2: invalid name did not consume the whole budget",
          pending is not None)
    check("P2: recovered with the suggested tool",
          bool(pending) and pending.get("tool") == "edit_file")
    agent.clear_pending_action()


def test_retry_is_bounded(temp_root):
    # 5) retry محدود است.
    script = ["THOUGHT: t\nTOOL: edit\nARGS: {}"] * 40

    out, obs, calls = run_loop_with(
        script, temp_root, max_steps=agent.MAX_AGENT_STEPS
    )

    check("P2: repeated invalid names stop the loop",
          calls <= agent.MAX_INVALID_TOOL_RETRIES + 2)
    check("P2: stop reason reported",
          "نام ابزار نامعتبر" in str(out))
    check("P2: bounded stop wrote nothing",
          not any(f.endswith(".txt") for f in os.listdir(temp_root)))
    agent.clear_pending_action()


def test_invalid_tool_causes_no_mutation(temp_root):
    # 6) تلاش نامعتبر نباید mutation ایجاد کند.
    target = os.path.join(temp_root, "should_not_exist.txt")
    if os.path.exists(target):
        os.remove(target)

    out, obs, _ = run_loop_with(
        ["THOUGHT: t\nTOOL: edit_file\nARGS: {}",
         "THOUGHT: t\nTOOL: edit\nARGS: {\"path\":\"x.txt\"}",
         "FINAL: done"],
        temp_root,
    )

    check("P2: invalid tool caused no file write",
          not os.path.exists(target))
    check("P2: no pending action created by invalid name",
          agent.get_pending_action() is None)
    agent.clear_pending_action()


def test_valid_tool_no_regression(temp_root):
    # 7) ابزار معتبر مثل edit_file بدون regression کار کند.
    out, obs, _ = run_loop_with(
        [("edit_file", "{\"path\":\"ok.txt\",\"old\":\"a\",\"new\":\"b\"}")],
        temp_root,
    )

    pending = agent.get_pending_action()
    check("P2: valid tool still reaches the approval gate",
          pending is not None)
    check("P2: valid tool keeps its registry name",
          bool(pending) and pending.get("tool") == "edit_file")
    check("P2: no invalid-tool feedback for a valid name",
          "نامعتبر" not in " ".join(obs))
    agent.clear_pending_action()


def main():
    temp_root = tempfile.mkdtemp(prefix="p2-")
    saved = {
        "file": agent.PENDING_ACTION_FILE,
        "project_path": agent.get_project_path,
    }
    agent.PENDING_ACTION_FILE = os.path.join(temp_root, "p.json")
    agent.get_project_path = lambda name: temp_root

    try:
        test_edit_suggests_edit_file()
        test_invalid_tool_returns_feedback(temp_root)
        test_edit_suggests_in_feedback(temp_root)
        test_invalid_tool_does_not_consume_step(temp_root)
        test_retry_is_bounded(temp_root)
        test_invalid_tool_causes_no_mutation(temp_root)
        test_valid_tool_no_regression(temp_root)
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

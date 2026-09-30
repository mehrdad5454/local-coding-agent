"""
تست focused — اولویت ۳: خطای Agentic Loop دیگر بی‌صدا نیست.

قانون: خطای حلقه باید ثبت شود، اما رفتار کنترل جریان
  - Legacy fallback حفظ شود،
  - خطا دوباره raise نشود،
  - هیچ Safety Layer دور زده نشود،
  - و مسیر موفق بدون تغییر بماند.
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


def break_the_loop():
    # حلقه را طوری تنظیم می‌کنیم که حتماً Exception بدهد
    # و به مسیر fallback برود.
    #
    # نکته: فقط خراب کردن call_model کافی نیست؛
    # run_loop_models این خطا را داخلی می‌گیرد و None برمی‌گرداند.
    # برای رسیدن به handler سطح run_agent، خودِ
    # run_agentic_loop باید استثنا بدهد.
    saved = {
        "loop": agent.run_agentic_loop,
        "model": agent.call_model,
        "legacy": agent.run_agent_legacy,
        "should": agent.should_use_agentic_loop,
    }

    calls = {"legacy": 0}

    def boom_loop(*a, **kw):
        raise RuntimeError("synthetic loop failure")

    def spy_legacy(*a, **kw):
        calls["legacy"] += 1
        return "LEGACY RESULT"

    agent.run_agentic_loop = boom_loop
    agent.run_agent_legacy = spy_legacy
    agent.should_use_agentic_loop = lambda req, root: True

    return saved, calls


def restore(saved):
    agent.run_agentic_loop = saved["loop"]
    agent.call_model = saved["model"]
    agent.run_agent_legacy = saved["legacy"]
    agent.should_use_agentic_loop = saved["should"]


def test_success_path_no_regression(temp_root):
    # 1) مسیر موفق حلقه بدون regression.
    agent.LAST_LOOP_FAILURE = None

    def stub(system_prompt, user_prompt, *a, **kw):
        return "FINAL: all good"

    saved = agent.call_model
    agent.call_model = stub

    try:
        out = agent.run_agentic_loop(
            "probe request",
            temp_root,
            "probe",
            allow_write=False,
        )
    finally:
        agent.call_model = saved

    check("P3: success path returns a result", out is not None)
    check("P3: success path records no failure",
          agent.get_last_loop_failure() is None)


def test_exception_is_recorded(temp_root):
    # 2) Exception باید ثبت شود و fallback حفظ شود.
    agent.LAST_LOOP_FAILURE = None
    saved, calls = break_the_loop()

    try:
        result = agent.run_agent(
            "probe request",
            project_name="probe",
            allow_write=False,
        )
    finally:
        restore(saved)

    check("P3: legacy fallback still ran", calls["legacy"] == 1)
    check("P3: fallback result returned to caller",
          result == "LEGACY RESULT")

    failure = agent.get_last_loop_failure()
    check("P3: loop failure recorded", failure is not None)

    if failure:
        check("P3: failure keeps the error type",
              failure.get("error_type") == "RuntimeError")
        check("P3: failure keeps a useful message",
              "synthetic loop failure" in failure.get("error", ""))
        check("P3: failure does not leak the user request",
              "probe request" not in failure.get("error", ""))


def test_recording_never_raises():
    # 3) خود تابع ثبت نباید هرگز استثنا بدهد.
    saved = agent.LAST_LOOP_FAILURE

    try:
        result = agent.record_agentic_loop_failure(
            "req", "proj", (None, None, None),
        )
        check("P3: recorder tolerates a missing exception",
              result is None or isinstance(result, dict))
    finally:
        agent.LAST_LOOP_FAILURE = saved

    check("P3: recorder returns a dict or None",
          result is None or isinstance(result, dict))


def test_no_mutation_during_exception(temp_root):
    # 4) Exception نباید باعث mutation شود.
    agent.LAST_LOOP_FAILURE = None
    before = sorted(os.listdir(temp_root))

    saved, calls = break_the_loop()

    try:
        agent.run_agent(
            "probe request",
            project_name="probe",
            allow_write=True,
        )
    finally:
        restore(saved)

    after = sorted(os.listdir(temp_root))
    check("P3: no file changed during the failure", before == after)
    check("P3: no pending action created",
          agent.get_pending_action() is None)


def test_safety_untouched():
    # 5) Safety Layer و Approval دست‌نخورده‌اند.
    check("P3: approval gate still present",
          callable(agent.handle_pending_action_request))
    check("P3: re-validation still present",
          callable(agent.revalidate_pending_action))
    check("P3: pending action helpers intact",
          callable(agent.remember_pending_action)
          and callable(agent.clear_pending_action))
    check("P3: emergency stop helper intact",
          callable(agent.is_stop_requested))
    check("P3: tool registry untouched",
          "edit_file" in agent.TOOL_REGISTRY
          and "write_file" in agent.TOOL_REGISTRY)
    check("P3: legacy fallback not removed",
          callable(agent.run_agent_legacy))


def main():
    temp_root = tempfile.mkdtemp(prefix="p3-")
    saved = {
        "file": agent.PENDING_ACTION_FILE,
        "project_path": agent.get_project_path,
    }
    agent.PENDING_ACTION_FILE = os.path.join(temp_root, "p.json")
    agent.get_project_path = lambda name: temp_root

    try:
        test_success_path_no_regression(temp_root)
        test_exception_is_recorded(temp_root)
        test_recording_never_raises()
        test_no_mutation_during_exception(temp_root)
        test_safety_untouched()
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



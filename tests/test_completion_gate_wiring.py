"""
تست اتصال حداقلی Completion Gate به Agentic Loop (فاز ۳).

الگوی تست پروژه را دنبال می‌کند: check(...) + test_* + runner با
exit code — فقط کتابخانه استاندارد، مدل با stub جایگزین می‌شود و
همهٔ پروژه‌ها موقتی‌اند.

پوشش:
  * استخراج قطعی الزامات فقط از نام فایل‌های درخواست (بدون حدس).
  * نگاشت شواهد فقط از نتایج واقعی ابزار (verified/ok).
  * known_request_status فقط برای همان درخواست و همان الزام.
  * درخواست تحلیلی/بدون‌نام‌فایل: Gate خاموش، رفتار قبلی بدون تغییر.
  * FINAL زودهنگام: حداکثر یک یادآوری در بودجهٔ موجود.
  * خطای import یا اجرای Gate: حلقه crash نمی‌کند.

اجرا از ریشه پروژه:

    .venv\\Scripts\\python.exe tests\\test_completion_gate_wiring.py
"""

import os
import shutil
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import agent
from agents.completion_gate import evaluate_completion, is_complete

results = []

MUTATION_REQUEST = "index.html و style.css را تغییر بده"
READONLY_REQUEST = "این پروژه را بررسی کن"


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


def section(title):
    print("\n=== " + title + " ===")


def make_project():
    root = tempfile.mkdtemp(prefix="gate_wiring_")

    with open(os.path.join(root, "index.html"), "w",
              encoding="utf-8") as fh:
        fh.write("<h1>ok</h1>\n")

    with open(os.path.join(root, "style.css"), "w",
              encoding="utf-8") as fh:
        fh.write("h1 { color: teal; }\n")

    return root


def make_entry(path, verified, ok=True, tool="write_file"):
    # شبیه‌سازی یک entry واقعی transcript (همان ساختار
    # run_loop_steps / execute_pending_action).
    return {
        "step": 1,
        "tool": tool,
        "args": {"path": path, "content": "x"},
        "result": {"ok": ok, "text": "done", "data": {}, "verified": verified},
        "mutating": True,
        "verified": verified,
        "verify": None,
    }


def run_loop(user_request, root, script, allow_write=True,
             transcript=None, known_request_status=None):
    # اجرای حلقه با مدل stubشده؛ خروجی: (report, تعداد فراخوانی مدل).
    calls = {"count": 0, "prompts": []}

    def stub(system_prompt, user_prompt, *args, **kwargs):
        index = calls["count"]
        calls["count"] += 1
        calls["prompts"].append(user_prompt)

        if index >= len(script):
            return "FINAL: پایان"

        return script[index]

    saved_model = agent.call_model
    agent.call_model = stub

    try:
        report = agent.run_loop_steps(
            user_request=user_request,
            project_root=root,
            project_name="gate-wiring",
            allow_write=allow_write,
            transcript=list(transcript or []),
            observations=[],
            steps_used=len(transcript or []),
            correction_attempts=0,
            max_steps=agent.MAX_AGENT_STEPS,
            known_request_status=known_request_status,
        )
    finally:
        agent.call_model = saved_model

    return report, calls["count"]


# ---------------------------------------------------------
# ۱) استخراج قطعی الزامات + نگاشت شواهد از نتایج واقعی ابزار.
# ---------------------------------------------------------
def test_gate_input_extraction_and_evidence():
    section("1. Gate input: deterministic requirements + real evidence")
    root = make_project()

    try:
        check(
            "mutation request is recognized",
            agent.request_asks_for_mutation(MUTATION_REQUEST),
        )

        requirements, evidence = agent.build_completion_gate_input(
            root,
            MUTATION_REQUEST,
            [
                make_entry("index.html", verified=True),
                make_entry("style.css", verified=False, ok=False),
            ],
        )

        check(
            "requirements are exactly the extracted file names",
            [item["id"] for item in requirements] == [
                "index.html", "style.css",
            ],
            safe(requirements),
        )

        outcome_map = {
            item["requirement"]: item["outcome"] for item in evidence
        }
        check(
            "verified True + ok -> success evidence",
            outcome_map.get("index.html") == "success",
            safe(evidence),
        )
        check(
            "verified False / ok False -> failure evidence",
            outcome_map.get("style.css") == "failure",
            safe(evidence),
        )

        # Mutation بدون Verify معتبر: هرگز success نیست.
        _requirements, evidence2 = agent.build_completion_gate_input(
            root, "index.html را تغییر بده",
            [make_entry("index.html", verified=None, ok=True)],
        )
        check(
            "unverified mutation produces NO success evidence",
            evidence2 == [],
            safe(evidence2),
        )

        # درخواست تحلیلی: Gate ورودی نمی‌گیرد (رفار قبلی حفظ می‌شود).
        check(
            "read-only request yields no gate input",
            agent.build_completion_gate_input(root, READONLY_REQUEST, [])
            == ([], []),
        )

        # درخواست تغییردهنده بدون نام فایل قطعی: Gate خاموش است؛
        # هیچ الزامی حدس زده نمی‌شود و هیچ ادعای تکمیلی ساخته نمی‌شود.
        vague_request = "تغییرات کلی اعمال کن"
        check(
            "vague mutation request is still a mutation request",
            agent.request_asks_for_mutation(vague_request),
        )
        check(
            "vague mutation request yields no gate input",
            agent.build_completion_gate_input(root, vague_request, [])
            == ([], []),
        )
        check(
            "vague mutation request: gate stays inactive (None)",
            agent.evaluate_completion_gate(root, vague_request, [])
            is None,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------
# ۲) known_request_status فقط برای همان درخواست و همان الزام.
# ---------------------------------------------------------
def test_gate_input_known_request_status():
    section("2. known_request_status used only when clearly related")
    root = make_project()

    try:
        _requirements, evidence = agent.build_completion_gate_input(
            root,
            MUTATION_REQUEST,
            [],
            known_request_status={
                "style.css": "verified",
                "ghost.css": "verified",
                "index.html": "not changed",
            },
        )

        check(
            "verified known status of a real requirement -> success",
            ("style.css", "success") in {
                (item["requirement"], item["outcome"]) for item in evidence
            },
            safe(evidence),
        )
        check(
            "unknown key (ghost.css) is ignored",
            all(item["requirement"] != "ghost.css" for item in evidence),
            safe(evidence),
        )
        check(
            "not changed produces no evidence",
            all(item["requirement"] != "index.html" for item in evidence),
            safe(evidence),
        )

        # سطح حلقه: Resume بعد از Approval با status معتبرِ همان
        # درخواست → gate complete می‌شود.
        gate = agent.evaluate_completion_gate(
            root,
            MUTATION_REQUEST,
            [make_entry("index.html", verified=True)],
            known_request_status={"style.css": "verified"},
        )
        check(
            "resume: verified known + verified transcript -> complete",
            isinstance(gate, dict) and gate.get("status") == "complete",
            safe(gate),
        )

        # اگر known فقط کلید بی‌ربط داشته باشد، gate complete نمی‌شود.
        gate2 = agent.evaluate_completion_gate(
            root,
            MUTATION_REQUEST,
            [make_entry("index.html", verified=True)],
            known_request_status={"ghost.css": "verified"},
        )
        check(
            "resume: unrelated known key never completes the gate",
            isinstance(gate2, dict)
            and gate2.get("status") == "incomplete",
            safe(gate2),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------
# ۳) خطای import یا اجرای Gate: حلقه crash نمی‌کند.
# ---------------------------------------------------------
def test_gate_error_paths_return_none():
    section("3. Gate import/evaluation errors never break the loop")
    root = make_project()

    try:
        saved_module = sys.modules.get("agents.completion_gate")
        sys.modules["agents.completion_gate"] = None

        try:
            gate = agent.evaluate_completion_gate(
                root, MUTATION_REQUEST, []
            )
        finally:
            if saved_module is not None:
                sys.modules["agents.completion_gate"] = saved_module
            else:
                sys.modules.pop("agents.completion_gate", None)

        check("broken import -> gate returns None", gate is None, safe(gate))

        def exploding_builder(*args, **kwargs):
            raise RuntimeError("synthetic gate failure")

        saved_builder = agent.build_completion_gate_input
        agent.build_completion_gate_input = exploding_builder

        try:
            gate2 = agent.evaluate_completion_gate(
                root, MUTATION_REQUEST, []
            )
            report, calls = run_loop(
                MUTATION_REQUEST, root,
                ["FINAL: انجام شد", "FINAL: باز هم انجام شد"],
            )
        finally:
            agent.build_completion_gate_input = saved_builder

        check("exploding builder -> gate returns None", gate2 is None)
        check(
            "loop survives gate errors and returns a report",
            isinstance(report, str) and report,
            safe(report)[:200],
        )
        check(
            "loop did not exceed its step budget on gate errors",
            1 <= calls <= agent.MAX_AGENT_STEPS,
            str(calls),
        )
        check(
            "no gate note is fabricated when the gate failed",
            "نتیجه دروازه تکمیل" not in report,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------
# ۴) درخواست فایل‌محور با دو الزام و شواهد موفق برای هر دو.
# ---------------------------------------------------------
def test_complete_requirements_final_accepted_without_gate_note():
    section("4. Two verified requirements -> complete, no gate note")
    root = make_project()

    try:
        transcript = [
            make_entry("index.html", verified=True),
            make_entry("style.css", verified=True),
        ]

        gate = agent.evaluate_completion_gate(
            root, MUTATION_REQUEST, transcript
        )
        check(
            "unit: gate status is complete",
            isinstance(gate, dict) and gate.get("status") == "complete",
            safe(gate),
        )
        check(
            "unit: is_complete accepts the verdict",
            is_complete(gate),
            safe(gate),
        )

        report, calls = run_loop(
            MUTATION_REQUEST, root,
            ["FINAL: انجام شد"],
            transcript=transcript,
        )
        check(
            "loop: FINAL accepted without any gate note",
            isinstance(report, str)
            and "نتیجه دروازه تکمیل" not in report,
            safe(report)[:300],
        )
        check(
            "loop: no extra retry was forced",
            calls <= 2,
            str(calls),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------
# ۵) FINAL زودهنگام: نهایتاً یک یادآوری در بودجهٔ موجود و
#    پذیرش شفاف در انتها؛ بدون حلقهٔ بی‌نهایت.
# ---------------------------------------------------------
def test_early_final_nudge_and_gate_note_within_budget():
    section("5. Early FINAL: bounded nudge + transparent gate note")
    root = make_project()

    try:
        report, calls = run_loop(
            MUTATION_REQUEST, root,
            ["FINAL: انجام شد", "FINAL: باز هم انجام شد"],
        )

        check(
            "loop terminated and returned a report",
            isinstance(report, str) and report,
            safe(report)[:200],
        )
        check(
            "model retries stayed within the step budget",
            2 <= calls <= agent.MAX_AGENT_STEPS,
            str(calls),
        )
        check(
            "gate note names the unproven requirements",
            "نتیجه دروازه تکمیل" in report
            and "index.html" in report
            and "style.css" in report,
            safe(report)[-600:],
        )

        check(
            "gate never loops: report is a plain string result",
            isinstance(report, str),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------
# ۶) شکست ابزار و تأیید ناموفق: هرگز شاهد موفقیت نیست.
# ---------------------------------------------------------
def test_failed_tool_not_success_evidence():
    section("6. Failed tool / failed verify is never success evidence")
    root = make_project()

    try:
        transcript = [
            make_entry("index.html", verified=False, ok=False),
        ]

        gate = agent.evaluate_completion_gate(
            root, MUTATION_REQUEST, transcript
        )
        check(
            "unit: failed entry -> incomplete, not complete",
            isinstance(gate, dict)
            and gate.get("status") == "incomplete"
            and not is_complete(gate),
            safe(gate),
        )
        check(
            "unit: failed requirement is marked failed",
            (gate.get("requirement_states") or {}).get("index.html")
            == "failed",
            safe(gate),
        )

        report, _calls = run_loop(
            MUTATION_REQUEST, root,
            ["FINAL: انجام شد", "FINAL: باز هم انجام شد"],
            transcript=transcript,
        )
        check(
            "loop: gate note lists the unproven requirements",
            "نتیجه دروازه تکمیل" in report
            and "index.html" in report
            and "style.css" in report,
            safe(report)[-600:],
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------
# ۷) درخواست فقط‌خواندنی: رفتار قبلی بدون تغییر.
# ---------------------------------------------------------
def test_readonly_behavior_unchanged():
    section("7. Read-only request: behavior identical to pre-gate")
    root = make_project()

    try:
        report_with_gate, _calls = run_loop(
            READONLY_REQUEST, root,
            ["FINAL: بررسی شد"],
            allow_write=False,
        )

        saved_gate = agent.evaluate_completion_gate
        agent.evaluate_completion_gate = lambda *a, **k: None

        try:
            report_without_gate, _calls2 = run_loop(
                READONLY_REQUEST, root,
                ["FINAL: بررسی شد"],
                allow_write=False,
            )
        finally:
            agent.evaluate_completion_gate = saved_gate

        check(
            "read-only report is unchanged with the gate wired in",
            report_with_gate == report_without_gate,
            safe(report_with_gate)[:200],
        )
        check(
            "no gate marker appears for read-only requests",
            "نتیجه دروازه تکمیل" not in report_with_gate,
            safe(report_with_gate)[:200],
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    test_gate_input_extraction_and_evidence()
    test_gate_input_known_request_status()
    test_gate_error_paths_return_none()
    test_complete_requirements_final_accepted_without_gate_note()
    test_early_final_nudge_and_gate_note_within_budget()
    test_failed_tool_not_success_evidence()
    test_readonly_behavior_unchanged()

    passed = sum(1 for _, ok in results if ok)
    print("\nRESULT: {}/{} passed".format(passed, len(results)))

    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
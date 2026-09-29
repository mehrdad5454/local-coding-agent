"""
Step 3 self-check — Verify-after-edit + Error Feedback.

Purpose:
    Focused regression for Step 3 ONLY (no project-wide audit):
      * mutation success + verify success
      * verify failure (python syntax error -> rollback -> structured errors)
      * minimal error feedback (file + error type + message + line/position)
      * correction: valid source, successful correction, ONE-correction limit
      * invalid correction (None / malformed proposal)
      * correction outside the project (rejected by validate_batch_args)
      * Emergency Stop before execution and during the correction step
      * manual approval mode vs autonomous approval mode
      * direct Step 2 regression (proposal contract + mutation boundary)
      * py_compile of the changed Python file (agent.py)

Runs with the standard library only (no pytest, no network, no model):
    .venv\\Scripts\\python.exe step3_selfcheck.py

Isolation guarantees:
    * The real action-journal, pending-action file and projects.json are
      never written: journal/pending paths are redirected into a temp
      sandbox before any test runs.
    * agent.call_model is stubbed to RAISE, so no LLM/network can be
      reached; correction sources are injected via proposal_source.
    * Emergency Stop tests use a local threading.Event, never the global
      stop event (it is asserted clear at the end).
"""

from __future__ import annotations

import json
import os
import py_compile
import shutil
import sys
import tempfile
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Keep console output stable no matter which code page the shell uses.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RESULTS = []


def check(name, condition, detail=""):
    """Record and print a single boolean check."""
    ok = bool(condition)
    safe_detail = str(detail).encode("ascii", "backslashreplace").decode("ascii")
    RESULTS.append((name, ok, safe_detail))
    if ok:
        print("PASS  " + name)
    else:
        print("FAIL  " + name + (("  [" + safe_detail + "]") if safe_detail else ""))
    return ok


def check_equal(name, actual, expected):
    """Record a check that compares two values."""
    ok = actual == expected
    detail = "" if ok else "actual=%r expected=%r" % (actual, expected)
    return check(name, ok, detail)


def check_raises(name, exc_type, func, *args, **kwargs):
    """Record a check that a call raises a specific exception type."""
    try:
        func(*args, **kwargs)
    except exc_type:
        return check(name, True)
    except Exception as exc:
        return check(name, False, "raised %s: %s" % (type(exc).__name__, exc))
    return check(name, False, "no exception raised")


def section(title):
    print("\n=== " + title + " ===")


def file_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def read_text(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


BAD_PY = "def broken(:\n"
GOOD_PY = "print('step3')\n"
FIXED_PY = "def fixed():\n    return 1\n"
STILL_BAD_PY = "def still_bad(:\n"


def new_root(sandbox, name):
    root = os.path.join(sandbox, name)
    os.makedirs(root, exist_ok=True)
    return root


def no_model(*args, **kwargs):
    raise AssertionError("call_model must never be reached in Step 3 self-check")


def failing_source(*args, **kwargs):
    raise AssertionError("correction source must not be called here")


def load_journal(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def journal_entries_for(journal_path, project_name):
    return [
        entry
        for entry in load_journal(journal_path)
        if isinstance(entry, dict) and entry.get("project") == project_name
    ]


def test_success_and_autonomous(agent, sandbox):
    """T1: mutation success + verify success, autonomous = no approval."""
    section("1. Mutation success + verify success (autonomous)")
    root = new_root(sandbox, "success")
    agent.clear_pending_action()
    agent.is_autonomous_project = lambda name: True

    report = agent.handle_agent_batch_proposal(
        {"files": [{"path": "hello.py", "content": GOOD_PY}]},
        project_name="s3-success",
        project_root=root,
        user_request="write hello.py",
        allow_write=True,
        proposal_source=failing_source,
    )

    check("success report shows verify ok", "Verify: موفق" in report)
    written = os.path.join(root, "hello.py")
    check("file exists after mutation", os.path.isfile(written))
    if os.path.isfile(written):
        check_equal("file content matches", read_text(written), GOOD_PY)

    agent.clear_pending_action()
    check(
        "autonomous: no pending approval created",
        agent.get_pending_action() is None,
    )
    entries = journal_entries_for(agent.ACTION_JOURNAL_FILE, "s3-success")
    check(
        "journal records done + verify ok",
        any(
            e.get("execution_state") == "done"
            and e.get("verify_state") == "ok"
            and e.get("approval_state") == "approved"
            for e in entries
        ),
        str(entries)[-300:],
    )


def test_verify_failure(agent, sandbox):
    """T2: verify failure with structured errors + rollback."""
    section("2. Verify failure (syntax error -> rollback)")
    root = new_root(sandbox, "verify-fail")

    result = agent.tool_write_files(
        root, {"files": [{"path": "bad.py", "content": BAD_PY}]}
    )

    check("failed mutation reports ok=False", result.get("ok") is False)
    check("failed mutation reports verified=False", result.get("verified") is False)
    verify = (result.get("data") or {}).get("verify") or {}
    check("verify result present", bool(verify))
    check("verify ok=False", verify.get("ok") is False)
    check("verify applied=False (rolled back)", verify.get("applied") is False)
    check("verify rolled_back=True", verify.get("rolled_back") is True)
    errors = verify.get("errors") or []
    check("structured error reported", len(errors) >= 1)
    if errors:
        err = errors[0]
        check_equal("error file path", err.get("path"), "bad.py")
        check_equal("error type is syntax_error", err.get("error_type"), "syntax_error")
        check("error message present", bool(err.get("message")))
        check("error line extracted", err.get("line") is not None)
    check(
        "failed file removed by rollback",
        not os.path.exists(os.path.join(root, "bad.py")),
    )

    # verify_applied_files: content mismatch and missing file.
    with open(os.path.join(root, "mismatch.txt"), "w", encoding="utf-8") as f:
        f.write("AAA")
    v = agent.verify_applied_files(
        root, [{"path": "mismatch.txt", "content": "BBB"}]
    )
    check("content mismatch detected", v.get("ok") is False)
    if v.get("errors"):
        check_equal(
            "content mismatch type",
            v["errors"][0].get("error_type"),
            "content_mismatch",
        )
    v2 = agent.verify_applied_files(root, [{"path": "ghost.py", "content": "x"}])
    check("missing file detected", v2.get("ok") is False)
    if v2.get("errors"):
        check_equal(
            "missing file type",
            v2["errors"][0].get("error_type"),
            "missing_file",
        )


def test_error_feedback(agent, sandbox):
    """T3: minimal error feedback = file + type + message + line/position."""
    section("3. Error feedback")
    root = new_root(sandbox, "feedback")

    result = agent.tool_write_files(
        root, {"files": [{"path": "bad.py", "content": BAD_PY}]}
    )
    verify = (result.get("data") or {}).get("verify") or {}
    feedback = agent.format_verify_feedback(verify, "write_files")
    check("feedback marks VERIFY FAILED", "VERIFY FAILED" in feedback)
    check("feedback contains file", "bad.py" in feedback)
    check("feedback contains error type", "syntax_error" in feedback)
    check("feedback contains message", "error:" in feedback)
    check("feedback contains line/position", "line " in feedback)

    synthetic = {
        "ok": False,
        "applied": True,
        "rolled_back": False,
        "checked": 1,
        "unchecked": [],
        "errors": [
            agent.new_verify_error("x.js", "syntax_error", "boom", 3, 7)
        ],
    }
    fb2 = agent.format_verify_feedback(synthetic, "write_files")
    check("feedback contains file field", "file: x.js" in fb2)
    check("feedback contains line number", "line 3" in fb2)
    check("feedback contains column number", "column 7" in fb2)

    fb3 = agent.format_verify_feedback(None, "write_files")
    check("feedback is fail-safe on empty verify", "VERIFY FAILED" in fb3)


def test_valid_and_successful_correction(agent, sandbox):
    """T4+T5: valid correction source and a successful correction."""
    section("4+5. Valid correction + successful correction")
    root = new_root(sandbox, "correction-ok")
    agent.is_autonomous_project = lambda name: True
    captured = []

    def source(feedback, files, project_name=None, project_root=None,
               user_request="", stop_event=None):
        captured.append({"feedback": feedback, "files": files})
        return {"files": [{"path": "bad.py", "content": FIXED_PY}]}

    report = agent.handle_agent_batch_proposal(
        {"files": [{"path": "bad.py", "content": BAD_PY}]},
        project_name="s3-correction",
        project_root=root,
        user_request="write bad.py",
        allow_write=True,
        proposal_source=source,
    )

    check_equal("correction source called exactly once", len(captured), 1)
    if captured:
        feedback = captured[0]["feedback"]
        check("correction got VERIFY FAILED feedback", "VERIFY FAILED" in feedback)
        check("correction feedback names file", "bad.py" in feedback)
        check("correction feedback names type", "syntax_error" in feedback)
        check("correction feedback has line", "line " in feedback)
        paths = [(item or {}).get("path") for item in captured[0]["files"]]
        check_equal("minimal context = failed file only", paths, ["bad.py"])

    check("first attempt reported verify failed", "Verify: ناموفق" in report)
    check("corrected attempt reported verify ok", "Verify: موفق" in report)
    written = os.path.join(root, "bad.py")
    check("corrected file exists", os.path.isfile(written))
    if os.path.isfile(written):
        check_equal("corrected content on disk", read_text(written), FIXED_PY)

    entries = journal_entries_for(agent.ACTION_JOURNAL_FILE, "s3-correction")
    states = [(e.get("execution_state"), e.get("verify_state")) for e in entries]
    check(
        "journal: failed attempt recorded",
        ("failed", "failed") in states,
        str(states),
    )
    check(
        "journal: corrected attempt recorded done/ok",
        ("done", "ok") in states,
        str(states),
    )


def test_correction_limit(agent, sandbox):
    """T6: exactly ONE correction is allowed; second failure stops."""
    section("6. One-correction limit (MAX_CORRECTION_STEPS)")
    check_equal("MAX_CORRECTION_STEPS is 1", agent.MAX_CORRECTION_STEPS, 1)

    root = new_root(sandbox, "correction-limit")
    agent.is_autonomous_project = lambda name: True
    captured = []

    def source(feedback, files, **kwargs):
        captured.append(feedback)
        return {"files": [{"path": "bad.py", "content": STILL_BAD_PY}]}

    report = agent.handle_agent_batch_proposal(
        {"files": [{"path": "bad.py", "content": BAD_PY}]},
        project_name="s3-limit",
        project_root=root,
        user_request="write bad.py",
        allow_write=True,
        proposal_source=source,
    )

    check_equal("only ONE correction attempted", len(captured), 1)
    check("limit note present in report", "سقف گام اصلاحی" in report)
    check("second failure reported", "Verify: ناموفق" in report)
    check(
        "still-broken file removed (no bad state kept)",
        not os.path.exists(os.path.join(root, "bad.py")),
    )


def test_invalid_correction(agent, sandbox):
    """T7: invalid correction (None) and malformed correction proposal."""
    section("7. Invalid correction")
    agent.is_autonomous_project = lambda name: True

    root = new_root(sandbox, "correction-none")
    calls = []

    def none_source(*args, **kwargs):
        calls.append(1)
        return None

    report = agent.handle_agent_batch_proposal(
        {"files": [{"path": "bad.py", "content": BAD_PY}]},
        project_name="s3-invalid-none",
        project_root=root,
        user_request="write bad.py",
        allow_write=True,
        proposal_source=none_source,
    )
    check_equal("None correction source was consulted", len(calls), 1)
    check(
        "no-valid-proposal note present",
        "Proposal اصلاحی معتبری تولید نشد" in report,
    )
    check(
        "no new change written",
        not os.path.exists(os.path.join(root, "bad.py")),
    )

    root2 = new_root(sandbox, "correction-malformed")

    def empty_source(*args, **kwargs):
        return {"files": []}

    report2 = agent.handle_agent_batch_proposal(
        {"files": [{"path": "bad.py", "content": BAD_PY}]},
        project_name="s3-invalid-malformed",
        project_root=root2,
        user_request="write bad.py",
        allow_write=True,
        proposal_source=empty_source,
    )
    check(
        "malformed correction rejected as invalid proposal",
        "پیشنهاد Agent معتبر نبود" in report2,
    )
    check(
        "malformed correction wrote nothing",
        not os.path.exists(os.path.join(root2, "bad.py")),
    )


def test_correction_outside_project(agent, sandbox):
    """T8: correction proposing a path outside the project is rejected."""
    section("8. Correction outside the project")
    root = new_root(sandbox, "correction-outside")
    agent.is_autonomous_project = lambda name: True

    def outside_source(*args, **kwargs):
        return {"files": [{"path": "../evil.txt", "content": "x"}]}

    report = agent.handle_agent_batch_proposal(
        {"files": [{"path": "bad.py", "content": BAD_PY}]},
        project_name="s3-outside",
        project_root=root,
        user_request="write bad.py",
        allow_write=True,
        proposal_source=outside_source,
    )

    check(
        "outside correction rejected by validation",
        "پیشنهاد Agent معتبر نبود" in report,
    )
    check(
        "no file created outside the project",
        not os.path.exists(os.path.join(root, "..", "evil.txt")),
    )
    check(
        "in-project failed file still absent",
        not os.path.exists(os.path.join(root, "bad.py")),
    )


def test_emergency_stop(agent, sandbox):
    """T9: Emergency Stop before execution and during correction."""
    section("9. Emergency Stop")
    agent.is_autonomous_project = lambda name: True

    # (a) stop already active before execution.
    root = new_root(sandbox, "stop-entry")
    agent.clear_pending_action()
    stop = threading.Event()
    stop.set()
    report = agent.handle_agent_batch_proposal(
        {"files": [{"path": "hello.py", "content": GOOD_PY}]},
        project_name="s3-stop-entry",
        project_root=root,
        user_request="write hello.py",
        allow_write=True,
        stop_event=stop,
        proposal_source=failing_source,
    )
    check("entry stop returns emergency report", "Emergency Stop" in report)
    check(
        "entry stop wrote no file",
        not os.path.exists(os.path.join(root, "hello.py")),
    )
    check(
        "entry stop created no pending action",
        agent.get_pending_action() is None,
    )

    # (b) stop triggered while the correction step runs.
    root2 = new_root(sandbox, "stop-correction")
    agent.clear_pending_action()
    stop2 = threading.Event()
    captured = []

    def stopping_source(feedback, files, stop_event=None, **kwargs):
        captured.append(feedback)
        if stop_event is not None:
            stop_event.set()
        return {"files": [{"path": "bad.py", "content": FIXED_PY}]}

    report2 = agent.handle_agent_batch_proposal(
        {"files": [{"path": "bad.py", "content": BAD_PY}]},
        project_name="s3-stop-correction",
        project_root=root2,
        user_request="write bad.py",
        allow_write=True,
        stop_event=stop2,
        proposal_source=stopping_source,
    )
    check_equal("correction source consulted once", len(captured), 1)
    check(
        "stop during correction returns emergency report",
        "Emergency Stop" in report2,
    )
    check(
        "stop during correction note present",
        "گام اصلاحی اجرا نشد" in report2,
    )
    check(
        "corrected write blocked by stop",
        not os.path.exists(os.path.join(root2, "bad.py")),
    )


def test_manual_mode(agent, sandbox):
    """T10: manual approval mode stores ONE pending action, writes nothing."""
    section("10. Manual approval mode")
    root = new_root(sandbox, "manual")
    agent.is_autonomous_project = lambda name: False
    agent.clear_pending_action()

    report = agent.handle_agent_batch_proposal(
        {"files": [{"path": "hello.py", "content": GOOD_PY}]},
        project_name="s3-manual",
        project_root=root,
        user_request="write hello.py",
        allow_write=True,
        proposal_source=failing_source,
    )

    check("pending plan shown", "Plan پیشنهادی" in report)
    check("approval requested once", "Agent فقط پیشنهاد داد" in report)
    pending = agent.get_pending_action()
    check("pending action stored", isinstance(pending, dict))
    if isinstance(pending, dict):
        check_equal(
            "pending tool is write_files", pending.get("tool"), "write_files"
        )
    check(
        "manual mode wrote nothing",
        not os.path.exists(os.path.join(root, "hello.py")),
    )
    entries = journal_entries_for(agent.ACTION_JOURNAL_FILE, "s3-manual")
    check("manual mode recorded no execution", len(entries) == 0, str(entries))
    agent.clear_pending_action()

    # Read-only mode must block the write before any approval logic.
    root2 = new_root(sandbox, "read-only")
    agent.is_autonomous_project = lambda name: True
    report2 = agent.handle_agent_batch_proposal(
        {"files": [{"path": "hello.py", "content": GOOD_PY}]},
        project_name="s3-readonly",
        project_root=root2,
        user_request="write hello.py",
        allow_write=False,
        proposal_source=failing_source,
    )
    check("read-only blocked", "«فقط بررسی»" in report2)
    check(
        "read-only wrote nothing",
        not os.path.exists(os.path.join(root2, "hello.py")),
    )


def test_step2_regression(agent, sandbox):
    """T12: direct Step 2 regression (proposal contract + boundary)."""
    section("12. Direct Step 2 regression")
    from agents.orchestrator import (
        SeniorAgent,
        extract_agent_proposal,
        strip_mutation_tools,
    )
    from agents.base import MUTATION_TOOL_NAMES

    prop = extract_agent_proposal(
        'BATCH_PROPOSAL: {"files": [{"path": "a.txt", "content": "x"}]}'
    )
    check_equal(
        "proposal contract parses",
        prop,
        {"files": [{"path": "a.txt", "content": "x"}]},
    )
    check_equal(
        "plain prose is not a proposal",
        extract_agent_proposal("just an ordinary answer"),
        None,
    )

    stripped = strip_mutation_tools(
        {"read_file": 1, "write_file": 2, "write_files": 3, "run_command": 4}
    )
    check_equal(
        "strip_mutation_tools keeps read-only only",
        set(stripped),
        {"read_file"},
    )

    senior = SeniorAgent(
        tools={
            "read_file": lambda: None,
            "write_file": lambda: None,
            "write_files": lambda: None,
            "run_command": lambda: None,
        }
    )
    check(
        "mutation tools stripped from SeniorAgent",
        "write_file" not in senior.tools and "write_files" not in senior.tools,
    )
    check_raises(
        "SeniorAgent cannot obtain a mutation tool",
        PermissionError,
        senior.get_tool,
        "write_file",
    )
    check(
        "SeniorAgent cannot access a command tool",
        senior.can_access_tool("run_command") is False,
    )
    check(
        "MUTATION_TOOL_NAMES still guards write + command",
        {"write_file", "write_files", "run_command"} <= set(MUTATION_TOOL_NAMES),
    )

    # The Core still executes a plain Step 2 proposal (autonomous path).
    root = new_root(sandbox, "step2-core")
    agent.is_autonomous_project = lambda name: True
    report = agent.handle_agent_batch_proposal(
        {"files": [{"path": "step2.txt", "content": "ok\n"}]},
        project_name="s3-step2-core",
        project_root=root,
        user_request="write step2.txt",
        allow_write=True,
        proposal_source=failing_source,
    )
    check("Step 2 proposal executed by Core", "Verify: موفق" in report)
    check(
        "Step 2 proposal file written",
        os.path.isfile(os.path.join(root, "step2.txt")),
    )


def test_py_compile(agent, sandbox):
    """T13: py_compile the file changed by Step 3."""
    section("13. py_compile changed Python files")
    cfile = os.path.join(sandbox, "agent.pyc")
    try:
        py_compile.compile(
            os.path.join(BASE_DIR, "agent.py"), cfile=cfile, doraise=True
        )
        ok, detail = True, ""
    except Exception as exc:
        ok, detail = False, str(exc)
    check("py_compile agent.py", ok, detail)


def main() -> int:
    # ---- Isolation setup before any test runs ----
    sandbox = tempfile.mkdtemp(prefix="step3_selfcheck_")

    import agent

    # Real state files that must never be written by this script.
    real_projects = agent.PROJECTS_FILE
    real_journal = os.path.join(BASE_DIR, "action-journal.json")
    real_pending = os.path.join(BASE_DIR, "pending-action.tmp.json")
    before = {
        real_projects: file_bytes(real_projects),
        real_journal: file_bytes(real_journal),
        real_pending: file_bytes(real_pending),
    }

    # Redirect writable agent state into the sandbox.
    original_journal_file = agent.ACTION_JOURNAL_FILE
    original_pending_file = agent.PENDING_ACTION_FILE
    original_journal_memory = list(agent.ACTION_JOURNAL)
    original_call_model = agent.call_model
    original_is_autonomous = agent.is_autonomous_project

    agent.ACTION_JOURNAL_FILE = os.path.join(sandbox, "action-journal.json")
    agent.PENDING_ACTION_FILE = os.path.join(sandbox, "pending-action.tmp.json")
    agent.call_model = no_model

    try:
        section("0. Step 3 invariants")
        check_equal("MAX_CORRECTION_STEPS == 1", agent.MAX_CORRECTION_STEPS, 1)
        check(
            "verify layer exposes structured error types",
            all(
                hasattr(agent, name)
                for name in (
                    "VERIFY_TYPE_SYNTAX",
                    "VERIFY_TYPE_MISSING",
                    "VERIFY_TYPE_CONTENT",
                    "VERIFY_TYPE_MUTATION",
                )
            ),
        )
        check(
            "correction loop uses the normal safety path",
            hasattr(agent, "handle_agent_batch_proposal"),
        )
        check(
            "global emergency stop starts clear",
            agent.is_emergency_stop_requested() is False,
        )

        test_success_and_autonomous(agent, sandbox)
        test_verify_failure(agent, sandbox)
        test_error_feedback(agent, sandbox)
        test_valid_and_successful_correction(agent, sandbox)
        test_correction_limit(agent, sandbox)
        test_invalid_correction(agent, sandbox)
        test_correction_outside_project(agent, sandbox)
        test_emergency_stop(agent, sandbox)
        test_manual_mode(agent, sandbox)
        test_step2_regression(agent, sandbox)
        test_py_compile(agent, sandbox)
    finally:
        # Clear sandbox pending state while its path is still redirected.
        try:
            agent.clear_pending_action()
        except Exception:
            pass
        # Restore every redirected/global piece of state.
        agent.call_model = original_call_model
        agent.is_autonomous_project = original_is_autonomous
        agent.ACTION_JOURNAL_FILE = original_journal_file
        agent.PENDING_ACTION_FILE = original_pending_file
        del agent.ACTION_JOURNAL[:]
        agent.ACTION_JOURNAL.extend(original_journal_memory)
        agent.clear_emergency_stop()

    section("Hygiene: real state files untouched")
    for path, original in before.items():
        check(
            "unchanged: " + os.path.basename(path),
            file_bytes(path) == original,
        )
    check(
        "global emergency stop still clear",
        agent.is_emergency_stop_requested() is False,
    )

    shutil.rmtree(sandbox, ignore_errors=True)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n%d checks, %d failed" % (len(RESULTS), len(failed)))
    for name in failed:
        print("  FAILED: " + name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())







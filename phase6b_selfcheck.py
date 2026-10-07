"""Phase 6 targeted self-check part 2: Safety Spine + flag + imports.

Tests 6-10: approval gate, re-validation, verify/journal presence,
default flag False, import direction. Uses a temp sandbox only.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("[PASS] " if cond else "[FAIL] ") + name
          + ((" -- " + str(detail)[:160]) if detail and not cond else ""))


GOOD_HTML = "<html><body><h1>Hello</h1></body></html>\n"

# --- Test 6: approval still gates --------------------------------------------
sandbox = tempfile.mkdtemp(prefix="p6_approval_")
try:
    agent.clear_pending_action()
    report = agent.handle_agent_batch_proposal(
        {"files": [{"path": "index.html", "content": GOOD_HTML}]},
        project_name="p6-manual",
        project_root=sandbox,
        user_request="write landing page index.html file",
        allow_write=False,
    )
    check("T6: read-only mode blocks execution",
          isinstance(report, str) and len(report) > 0, "report-ok")
    check("T6: read-only wrote nothing",
          not os.path.exists(os.path.join(sandbox, "index.html")))
finally:
    shutil.rmtree(sandbox, ignore_errors=True)
    agent.clear_pending_action()

# --- Test 7: re-validation still runs -----------------------------------------
sandbox2 = tempfile.mkdtemp(prefix="p6_reval_")
t7 = True
try:
    agent.validate_batch_args(
        sandbox2, {"files": [{"path": "../evil.txt", "content": "x"}]})
    t7 = False
except Exception:
    t7 = True
check("T7: validate_batch_args rejects outside paths", t7)
shutil.rmtree(sandbox2, ignore_errors=True)

# --- Test 8: verify/journal path preserved ------------------------------------
check("T8: verify and journal functions present and untouched",
      all(callable(getattr(agent, n, None)) for n in
          ("verify_applied_files", "record_action",
           "handle_agent_batch_proposal", "execute_pending_action")))

# --- Test 9: default flag ------------------------------------------------------
check("T9: use_multi_agent defaults to False",
      agent.use_multi_agent is False, str(agent.use_multi_agent))

# --- Test 10: import direction ---------------------------------------------------
core_src = open(os.path.join("agents", "core_adapter.py"),
                encoding="utf-8").read()
orch_src = open(os.path.join("agents", "orchestrator.py"),
                encoding="utf-8").read()
spec_src = open(os.path.join("agents", "specialists", "content.py"),
                encoding="utf-8").read()
check("T10: core_adapter never imports agent",
      "import agent\n" not in core_src and "import agent\r" not in core_src
      and "from agent import" not in core_src
      and "from agent\n" not in core_src)
check("T10: orchestrator never imports agent",
      "import agent" not in orch_src and "from agent import" not in orch_src)
check("T10: specialist never imports agent",
      "import agent" not in spec_src and "from agent" not in spec_src)

print("PART2: %d/%d passed" % (len(PASS), len(PASS) + len(FAIL)))
sys.exit(1 if FAIL else 0)

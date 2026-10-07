"""Phase 6 targeted self-check: first real specialist (ContentSpecialist).

Tests 1-5: build, tools, proposal, no-mutation, Core handoff.
No network. No state changes.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent
import agents
from agents import SeniorAgent
from agents.core_adapter import build_core_adapter
from agents.specialists.content import ContentSpecialist

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("[PASS] " if cond else "[FAIL] ") + name
          + ((" -- " + str(detail)[:160]) if detail and not cond else ""))


GOOD_HTML = "<html><body><h1>Hello</h1></body></html>\n"
ANSWER_WITH_PROPOSAL = (
    "Here is the landing page.\n"
    "BATCH_PROPOSAL: "
    + json.dumps({"files": [{"path": "index.html", "content": GOOD_HTML}]})
)


def stub_call_model(system_prompt, user_prompt, **kwargs):
    return ANSWER_WITH_PROPOSAL


def make_tools():
    return {
        "call_model": stub_call_model,
        "read_file": lambda *a, **k: "",
        "safe_path": lambda root, rel: os.path.join(root, rel),
        "search_in_files": lambda *a, **k: [],
        "is_write_allowed": lambda permission: permission is True,
        "is_stop_requested": lambda event=None: False,
        "record_action": lambda **k: None,
        "mask_sensitive_content": lambda content: content,
    }


# --- Test 1: builds without importing agent ---------------------------------
src = open(os.path.join("agents", "specialists", "content.py"),
           encoding="utf-8").read()
check("T1: specialist source has no agent import",
      "import agent" not in src and "from agent" not in src)
adapter = build_core_adapter(**make_tools())
spec = ContentSpecialist(adapter=adapter)
check("T1: ContentSpecialist constructs from CoreAdapter only",
      spec.name == "content_specialist")

# --- Test 2: only allowed tools ----------------------------------------------
allowed = {"call_model", "read_file", "safe_path", "search_in_files",
           "is_write_allowed", "is_stop_requested", "record_action",
           "mask_sensitive_content"}
check("T2: specialist tools are exactly the allowed set",
      set(spec.tools.keys()) == allowed, str(sorted(spec.tools.keys())))

# --- Test 3: valid proposal ---------------------------------------------------
answer, proposal = spec.propose("write landing page index.html",
                                project_name="p6-check")
check("T3: specialist returns answer text", isinstance(answer, str)
      and "BATCH_PROPOSAL" in answer)
check("T3: proposal parses as Proposal-v1",
      proposal == {"files": [{"path": "index.html", "content": GOOD_HTML}]},
      str(proposal)[:120])

# --- Test 4: no direct mutation -----------------------------------------------
try:
    spec.get_tool("write_file")
    t4 = False
except PermissionError:
    t4 = True
except Exception:
    t4 = False
check("T4: get_tool write_file raises PermissionError", t4)
try:
    spec.get_tool("run_command")
    t4b = False
except PermissionError:
    t4b = True
except Exception:
    t4b = False
check("T4: get_tool run_command raises PermissionError", t4b)
try:
    adapter.call_readonly("write_file")
    t4c = False
except (PermissionError, KeyError):
    t4c = True
check("T4: adapter.call_readonly write_file refused", t4c)

# --- Test 5: proposal reaches the Core ----------------------------------------
senior = SeniorAgent(tools=make_tools())
check("T5: SeniorAgent detects content-file request",
      senior._is_content_file_request(
          "write landing page index.html file") is True)
check("T5: plain request is not hijacked",
      senior._is_content_file_request("hello there") is False)
check("T5: marketing trigger still routes to marketing first",
      senior._is_marketing_request(agents.MarketingAgent.TRIGGERS[0])
      is True)
out = senior.run("write landing page index.html file",
                 project_name="p6-check", project_root="",
                 allow_write=False, stop_event=None,
                 user_request="write landing page index.html file")
check("T5: specialist path returns answer via SeniorAgent",
      "BATCH_PROPOSAL" in out, out[:80])
ans, prop = senior.propose("write landing page index.html file",
                           project_name="p6-check", project_root="",
                           allow_write=False, stop_event=None,
                           user_request="write landing page index.html file")
check("T5: SeniorAgent.propose surfaces Proposal-v1",
      prop == {"files": [{"path": "index.html", "content": GOOD_HTML}]},
      str(prop)[:120])
check("T5: Core entry handle_agent_batch_proposal exists",
      callable(getattr(agent, "handle_agent_batch_proposal", None)))

print("PART1: %d/%d passed" % (len(PASS), len(PASS) + len(FAIL)))
sys.exit(1 if FAIL else 0)

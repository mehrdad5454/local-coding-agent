"""Phase 7-A targeted self-check: opt-in activation + evidence records.

A. LOCAL_AGENT_MULTI_AGENT: unset -> False; explicit "1" -> True.
B. Routing evidence (agent_routing) per route, incl. skeleton.
C. Producer origin + proposal evidence (agent_proposal, executor=core).
D. Execution attribution: autonomous journal entry, manual pending keys,
   pending identity/re-validation unchanged; default call stays keyless.
E. Delegation failure + run_agent fallback evidence.
F. Evidence-only: types are not undoable, never create approval state.

No network (call_model stubbed to raise where the Core could call it),
no state changes outside a temp sandbox.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# T1 must observe the true default: drop any env override before import.
os.environ.pop("LOCAL_AGENT_MULTI_AGENT", None)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import agent
import agents
from agents import SeniorAgent

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("[PASS] " if cond else "[FAIL] ") + name
          + ((" -- " + str(detail)[:160]) if detail and not cond else ""))


GOOD_HTML = "<html><body><h1>Hello</h1></body></html>\n"
ANSWER_WITH_PROPOSAL = (
    "Here is the page.\n"
    "BATCH_PROPOSAL: "
    + json.dumps({"files": [{"path": "notes.html", "content": GOOD_HTML}]})
)
PLAIN_ANSWER = "PLAIN ANSWER"


def make_tools(journal=None, answer=PLAIN_ANSWER):
    def _call_model(system_prompt, user_prompt, **kwargs):
        return answer

    return {
        "call_model": _call_model,
        "read_file": lambda *a, **k: "",
        "safe_path": lambda root, rel: os.path.join(root, rel),
        "search_in_files": lambda *a, **k: [],
        "is_write_allowed": lambda permission: permission is True,
        "is_stop_requested": lambda event=None: False,
        "record_action": rec_into(journal) if journal is not None
        else (lambda *a, **k: None),
        "mask_sensitive_content": lambda content: content,
    }


def routing_routes(journal):
    return [e.get("args", {}).get("route")
            for e in journal if e.get("action_type") == "agent_routing"]


def rec_into(journal):
    """record_action stub matching the real signature (positional or kw)."""
    def _rec(*args, **kwargs):
        if args:
            kwargs["action_type"] = args[0]
        journal.append(kwargs)
        return None
    return _rec


# --- A. Flag: default off, explicit env on ----------------------------------
check("A: use_multi_agent is False without LOCAL_AGENT_MULTI_AGENT",
      agent.use_multi_agent is False, str(agent.use_multi_agent))


def flag_in_subprocess(env_value):
    env = dict(os.environ)
    env.pop("LOCAL_AGENT_MULTI_AGENT", None)
    if env_value is not None:
        env["LOCAL_AGENT_MULTI_AGENT"] = env_value
    try:
        out = subprocess.run(
            [sys.executable, "-c",
             "import agent; print(agent.use_multi_agent)"],
            cwd=BASE_DIR, env=env, capture_output=True,
            text=True, timeout=120,
        )
    except Exception as exc:
        return "ERR:" + str(exc)[:120]
    if out.returncode != 0:
        return "ERR:" + (out.stderr or "")[-200:]
    lines = (out.stdout or "").strip().splitlines()
    return lines[-1].strip() if lines else "ERR:empty"


check("A: env LOCAL_AGENT_MULTI_AGENT=1 -> True",
      flag_in_subprocess("1") == "True", flag_in_subprocess("1"))
check("A: env LOCAL_AGENT_MULTI_AGENT=0 -> False",
      flag_in_subprocess("0") == "False", flag_in_subprocess("0"))

# --- B. Routing evidence -----------------------------------------------------
journal = []
senior = SeniorAgent(tools=make_tools(journal))
senior.run(
    agents.MarketingAgent.TRIGGERS[0],
    project_name="p7a-check", project_root="",
    allow_write=False, stop_event=None,
    user_request=agents.MarketingAgent.TRIGGERS[0],
)
check("B: marketing route journaled as agent_routing",
      routing_routes(journal) == ["marketing"], str(routing_routes(journal)))
deleg = [e for e in journal if e.get("action_type") == "agent_delegation"]
check("B: delegation record names marketing_agent",
      bool(deleg) and deleg[0].get("agent_id") == "marketing_agent",
      str(deleg)[:160])
check("B: origin after marketing run",
      senior.get_proposal_origin() == ("marketing_agent", "specialist"),
      str(senior.get_proposal_origin()))

journal2 = []
senior2 = SeniorAgent(tools=make_tools(journal2))
senior2.run(
    "hello there plain request",
    project_name="p7a-check", project_root="",
    allow_write=False, stop_event=None,
    user_request="hello there plain request",
)
check("B: skeleton route journaled for unrecognized request",
      routing_routes(journal2) == ["skeleton"], str(routing_routes(journal2)))
check("B: skeleton origin is orchestrator",
      senior2.get_proposal_origin() == ("orchestrator", "orchestrator"),
      str(senior2.get_proposal_origin()))

# --- C. Content route + proposal evidence ------------------------------------
journal3 = []
senior3 = SeniorAgent(tools=make_tools(journal3,
                                       answer=ANSWER_WITH_PROPOSAL))
content_request = "create a file notes.html"
answer3, proposal3 = senior3.propose(
    content_request,
    project_name="p7a-check", project_root="",
    allow_write=False, stop_event=None,
    user_request=content_request,
)
check("C: content route chosen for file-creation request",
      routing_routes(journal3) == ["content"], str(routing_routes(journal3)))
check("C: origin is content_specialist / specialist",
      senior3.get_proposal_origin() == ("content_specialist", "specialist"),
      str(senior3.get_proposal_origin()))
check("C: proposal parsed as Proposal-v1",
      proposal3 == {"files": [{"path": "notes.html", "content": GOOD_HTML}]},
      str(proposal3)[:120])
prop_ev = [e for e in journal3 if e.get("action_type") == "agent_proposal"]
check("C: agent_proposal evidence names the producing specialist",
      bool(prop_ev)
      and prop_ev[0].get("agent_id") == "content_specialist"
      and prop_ev[0].get("agent_type") == "specialist",
      str(prop_ev)[:200])
check("C: agent_proposal evidence records executor=core",
      bool(prop_ev)
      and (prop_ev[0].get("args") or {}).get("executor") == "core",
      str(prop_ev)[:200])

# --- D. Execution attribution (Core side) ------------------------------------
sandbox = tempfile.mkdtemp(prefix="p7a_")
root = os.path.join(sandbox, "proj")
os.makedirs(root, exist_ok=True)


def boom_model(*args, **kwargs):
    raise RuntimeError("no model calls expected in Phase 7-A tests")


saved_autonomous = agent.is_autonomous_project
saved_record = agent.record_action
saved_call_model = agent.call_model

# D1: autonomous execution carries the producer identity on the journal.
journal5 = []
try:
    agent.is_autonomous_project = lambda name: True
    agent.record_action = rec_into(journal5)
    agent.call_model = boom_model
    report = agent.handle_agent_batch_proposal(
        {"files": [{"path": "p7a.txt", "content": "hello 7a\n"}]},
        project_name="p7a-check",
        project_root=root,
        user_request="create p7a.txt",
        allow_write=True,
        agent_id="content_specialist",
        agent_type="specialist",
    )
finally:
    agent.is_autonomous_project = saved_autonomous
    agent.record_action = saved_record
    agent.call_model = saved_call_model

execs = [e for e in journal5 if e.get("action_type") == "multi_write"]
check("D: autonomous batch executed",
      os.path.exists(os.path.join(root, "p7a.txt")) and report is not None,
      str(report)[:160])
check("D: execution journal carries the producer identity",
      bool(execs)
      and execs[0].get("agent_id") == "content_specialist"
      and execs[0].get("agent_type") == "specialist",
      str(execs)[:200])

# D2: manual pending carries attribution; identity + re-validation pass.
journal6 = []
pending = None
reval_ok = None
identity_ok = None
identity_reason = ""
try:
    agent.is_autonomous_project = lambda name: False
    agent.record_action = rec_into(journal6)
    agent.call_model = boom_model
    agent.handle_agent_batch_proposal(
        {"files": [{"path": "p7a-manual.txt", "content": "x\n"}]},
        project_name="p7a-check",
        project_root=root,
        user_request="create p7a-manual.txt",
        allow_write=True,
        agent_id="content_specialist",
        agent_type="specialist",
    )
    pending = agent.get_pending_action()
    if isinstance(pending, dict):
        identity_ok, identity_reason = agent.pending_action_identity_status(
            pending
        )
        reval_ok = agent.revalidate_pending_action(pending)[0]
finally:
    agent.clear_pending_action()
    agent.is_autonomous_project = saved_autonomous
    agent.record_action = saved_record
    agent.call_model = saved_call_model

check("D: manual pending carries producer attribution keys",
      isinstance(pending, dict)
      and pending.get("agent_id") == "content_specialist"
      and pending.get("agent_type") == "specialist",
      str(pending)[:200])
check("D: pending identity still passes with attribution keys",
      identity_ok is True, str(identity_reason))
check("D: re-validation still passes with attribution keys",
      reval_ok is True)
check("D: nothing written before approval",
      not os.path.exists(os.path.join(root, "p7a-manual.txt")))

# D3: default call (no attribution kwargs) keeps the pending keyless.
pending2 = None
try:
    agent.is_autonomous_project = lambda name: False
    agent.record_action = rec_into([])
    agent.call_model = boom_model
    agent.handle_agent_batch_proposal(
        {"files": [{"path": "p7a-default.txt", "content": "y\n"}]},
        project_name="p7a-check",
        project_root=root,
        user_request="create p7a-default.txt",
        allow_write=True,
    )
    pending2 = agent.get_pending_action()
finally:
    agent.clear_pending_action()
    agent.is_autonomous_project = saved_autonomous
    agent.record_action = saved_record
    agent.call_model = saved_call_model

check("D: default call leaves pending without attribution keys",
      isinstance(pending2, dict)
      and "agent_id" not in pending2 and "agent_type" not in pending2,
      str(pending2)[:200])

shutil.rmtree(sandbox, ignore_errors=True)

# --- E. Failure evidence ------------------------------------------------------
journal7 = []
senior7 = SeniorAgent(
    tools={"record_action": rec_into(journal7)}
)
out7 = senior7.run(
    agents.MarketingAgent.TRIGGERS[0],
    project_name="p7a-check", project_root="",
    allow_write=False, stop_event=None,
    user_request=agents.MarketingAgent.TRIGGERS[0],
)
fails = [e for e in journal7
         if e.get("action_type") == "agent_delegation"
         and e.get("execution_state") == "failed"]
check("E: delegation failure journaled with target identity",
      bool(fails) and fails[0].get("agent_id") == "marketing_agent",
      str(fails)[:200])
check("E: failed delegation still returns text (skeleton fallback)",
      isinstance(out7, str) and bool(out7), str(out7)[:120])

# run_agent-level fallback: SeniorAgent blows up -> legacy keeps answering.
import agents.orchestrator as orch_mod

journal8 = []
temp_root = tempfile.mkdtemp(prefix="p7a_fb_")
saved = {
    "call_model": agent.call_model,
    "record_action": agent.record_action,
    "remember": agent.remember_conversation_message,
    "gpp": agent.get_project_path,
    "flag": agent.use_multi_agent,
    "senior": orch_mod.SeniorAgent,
    "pending": agent.get_pending_action(),
}


class BoomSenior:
    def __init__(self, *args, **kwargs):
        pass

    def propose(self, *args, **kwargs):
        raise RuntimeError("boom")


agent.clear_pending_action()
legacy_ok = False
try:
    # A returning stub (not a raising one): after the orchestrator
    # failure the legacy path itself must still be able to answer.
    # It is a stub, so no network is reachable either way.
    agent.call_model = lambda *a, **k: "STUB_LLM_OUTPUT"
    agent.record_action = rec_into(journal8)
    agent.remember_conversation_message = lambda *a, **k: None
    agent.get_project_path = lambda name: temp_root
    agent.use_multi_agent = True
    orch_mod.SeniorAgent = BoomSenior
    try:
        result = agent.run_agent(
            agents.MarketingAgent.TRIGGERS[0],
            "p7a-fallback",
            allow_write=False,
            stop_event=threading.Event(),
        )
        legacy_ok = isinstance(result, str) and bool(result)
    except Exception:
        legacy_ok = False
finally:
    orch_mod.SeniorAgent = saved["senior"]
    agent.call_model = saved["call_model"]
    agent.record_action = saved["record_action"]
    agent.remember_conversation_message = saved["remember"]
    agent.get_project_path = saved["gpp"]
    agent.use_multi_agent = saved["flag"]
    if saved["pending"] is None:
        agent.clear_pending_action()
    shutil.rmtree(temp_root, ignore_errors=True)

fb = [e for e in journal8 if e.get("action_type") == "agent_fallback"]
check("E: run_agent fallback journaled for orchestrator failure",
      bool(fb) and fb[0].get("agent_id") == "orchestrator"
      and fb[0].get("agent_type") == "orchestrator",
      str(fb)[:200])
check("E: legacy fallback keeps the request alive",
      legacy_ok, "run_agent raised or returned empty")

# --- F. Evidence is observability only ---------------------------------------
check("F: evidence action types are not undoable",
      all(t not in agent.UNDOABLE_ACTION_TYPES
          for t in ("agent_routing", "agent_proposal",
                    "agent_fallback", "agent_delegation")))

journal9 = []
senior9 = SeniorAgent(tools=make_tools(journal9))
senior9.run(
    agents.MarketingAgent.TRIGGERS[0],
    project_name="p7a-check", project_root="",
    allow_write=False, stop_event=None,
    user_request=agents.MarketingAgent.TRIGGERS[0],
)
check("F: evidence records never create a pending action",
      agent.get_pending_action() is None)
check("F: flag still False at the end of the suite",
      agent.use_multi_agent is False, str(agent.use_multi_agent))

print("PHASE7A: %d/%d passed"
      % (len(PASS), len(PASS) + len(FAIL)))
sys.exit(1 if FAIL else 0)



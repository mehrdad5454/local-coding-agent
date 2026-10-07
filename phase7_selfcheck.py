"""
Phase 7 self-check / regression script.

Purpose:
    Verify that the Phase 7 preparation layer did not break anything and
    that the safety invariants still hold.

Runs with the standard library only (no pytest, no network):
    .venv\\Scripts\\python.exe phase7_selfcheck.py

Coverage:
    A. Imports and agent registry
    B. LLM provider preparation (payload captured from agent.call_model)
    C. Agent metadata (consistency + safety invariants)
    D. Persona structure (inert) and multi-agent routing
    E. Legacy path and safety layers
    F. Phase 5 regression (OpportunityAgent, read-only enforcement)
    G. Phase 6 regression (capabilities, permissions, actions, gatekeeper)

The script never writes to the real action journal, to chat-history.db or
to any project file: every tool that would do so is replaced by an
in-memory stub, and the LLM is stubbed out completely.
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import sys
import tempfile
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Keep console output stable no matter which code page the shell uses
# (sub-agent stop notices and skeleton reports contain non-ASCII text).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RESULTS = []


def check(name: str, condition, detail: str = "") -> bool:
    """Record and print a single boolean check."""
    ok = bool(condition)
    safe_detail = str(detail).encode("ascii", "backslashreplace").decode("ascii")
    RESULTS.append((name, ok, safe_detail))
    if ok:
        print("PASS  " + name)
    else:
        print("FAIL  " + name + (("  [" + safe_detail + "]") if safe_detail else ""))
    return ok


def check_equal(name: str, actual, expected) -> bool:
    """Record a check that compares two values."""
    ok = actual == expected
    detail = "" if ok else f"actual={actual!r} expected={expected!r}"
    return check(name, ok, detail)


def check_raises(name: str, exc_type, func, *args, **kwargs) -> bool:
    """Record a check that a call raises a specific exception type."""
    try:
        func(*args, **kwargs)
    except exc_type as exc:
        return check(name, True, f"raised {type(exc).__name__}")
    except Exception as exc:  # a different exception type
        return check(name, False, f"raised {type(exc).__name__}: {exc}")
    return check(name, False, "no exception raised")


def section(title: str) -> None:
    print("\n=== " + title + " ===")


def first_trigger(agent_class, excluded) -> str:
    """
    Return a trigger keyword of `agent_class` that does not appear in the
    other agents' trigger lists, so that routing is unambiguous.
    """
    excluded_lower = {str(item).lower() for item in excluded}
    for trigger in agent_class.TRIGGERS:
        if trigger.lower() not in excluded_lower:
            return trigger
    return agent_class.TRIGGERS[0]




def section_a_imports():
    """A. Imports, package exports and agent registry."""
    section("A. Imports and agent registry")

    import agent
    import agents
    import agents.metadata
    import agents.persona
    import agents.providers

    check("agents package imports", hasattr(agents, "AGENT_REGISTRY"))
    check(
        "AGENT_REGISTRY keys",
        set(agents.AGENT_REGISTRY) == {"marketing", "research", "opportunity"},
        str(sorted(agents.AGENT_REGISTRY)),
    )

    for agent_type, agent_class in sorted(agents.AGENT_REGISTRY.items()):
        check(
            f"{agent_type} is an Agent subclass",
            isinstance(agent_class, type) and issubclass(agent_class, agents.Agent),
        )

    missing_exports = [name for name in agents.__all__ if not hasattr(agents, name)]
    check_equal("__all__ names all exist", missing_exports, [])

    check(
        "agent.py imports",
        hasattr(agent, "run_agent") and hasattr(agent, "MODEL_URL"),
    )
    return agents, agent




def section_b_providers(agent):
    """B. LLM provider preparation (declarative, no behaviour change)."""
    section("B. LLM provider preparation")

    from agents import providers as P

    spec = P.default_provider()
    check_equal("default provider id", spec.id, "llama_cpp_local")
    check(
        "default provider endpoint == agent.MODEL_URL",
        spec.endpoint_url == agent.MODEL_URL,
        f"{spec.endpoint_url} != {agent.MODEL_URL}",
    )
    check("current provider needs no API key", spec.requires_api_key is False)
    check(
        "provider registry contains all planned providers",
        {"llama_cpp_local", "openrouter", "agentrouter", "openai_compatible"}
        <= set(P.PROVIDER_REGISTRY),
        str(sorted(P.PROVIDER_REGISTRY)),
    )
    check(
        "only the local provider is marked local",
        [pid for pid in P.PROVIDER_REGISTRY if P.PROVIDER_REGISTRY[pid].is_local]
        == ["llama_cpp_local"],
    )
    check_equal("list_providers puts the default first", P.list_providers()[0], spec.id)
    check_raises("unknown provider raises KeyError", KeyError, P.get_provider, "nope")

    # Endpoint resolution and env override (pure functions, no network).
    check_equal(
        "env override wins over base_url",
        P.resolve_endpoint(spec, {"LOCAL_AGENT_LLAMA_BASE_URL": "http://elsewhere:1"}),
        "http://elsewhere:1/completion",
    )
    check(
        "placeholder provider has no base URL until env is set",
        P.is_available(P.get_provider("openai_compatible"), {}) is False,
    )
    check("local provider is available without a key", P.is_available(spec, {}) is True)
    check(
        "cloud provider is unavailable without its key",
        P.is_available(P.get_provider("openrouter"), {}) is False,
    )
    check_raises(
        "build_headers fails closed without an API key",
        ValueError,
        P.build_headers,
        P.get_provider("openrouter"),
        {},
    )
    check_equal(
        "build_headers adds Bearer token when the key exists",
        P.build_headers(P.get_provider("openrouter"), {"OPENROUTER_API_KEY": "k"}).get(
            "Authorization"
        ),
        "Bearer k",
    )




def section_b2_payload_parity(agent):
    """B2. Request body parity with the current agent.call_model()."""
    from agents import providers as P

    spec = P.default_provider()

    expected_prompt = (
        P.CHATML_SYSTEM_PREFIX + "SYS" + P.CHATML_SUFFIX
        + P.CHATML_USER_PREFIX + "USER" + P.CHATML_SUFFIX
        + P.CHATML_ASSISTANT_PREFIX
    )
    check_equal(
        "llama payload matches the current wire format",
        P.build_payload(spec, "SYS", "USER"),
        {
            "prompt": expected_prompt,
            "n_predict": 1600,
            "temperature": 0.1,
            "repeat_penalty": 1.1,
            "stop": ["<|im_end|>"],
            "stream": False,
        },
    )
    check_equal(
        "explicit n_predict is respected",
        P.build_payload(spec, "S", "U", n_predict=42)["n_predict"],
        42,
    )
    no_think_payload = P.build_payload(spec, "S", "U", no_think=True)
    check(
        "no_think marker is appended before the assistant turn",
        no_think_payload["prompt"].endswith(P.CHATML_ASSISTANT_PREFIX)
        and "/no_think" in no_think_payload["prompt"],
    )
    chat_payload = P.build_payload(P.get_provider("openrouter"), "S", "U")
    check(
        "openai-style payload uses a messages array",
        isinstance(chat_payload.get("messages"), list)
        and len(chat_payload["messages"]) == 2
        and chat_payload["messages"][0]["role"] == "system",
    )
    check_raises(
        "raw prompt rendering rejects chat-style providers",
        ValueError,
        P.render_prompt,
        P.get_provider("openrouter"),
        "S",
        "U",
    )

    # Capture what agent.call_model really sends (urllib stubbed, no network).
    captured = {}

    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(request, timeout=None, **kwargs):
        captured["url"] = getattr(request, "full_url", None)
        captured["data"] = getattr(request, "data", None)
        captured["headers"] = dict(request.header_items())
        return _FakeResponse(json.dumps({"content": "stub"}).encode("utf-8"))

    real_urlopen = agent.urllib.request.urlopen
    real_is_stop = agent.is_stop_requested
    try:
        agent.urllib.request.urlopen = _fake_urlopen
        agent.is_stop_requested = lambda stop_event=None: False
        agent.call_model("SYS", "USER")
    finally:
        agent.urllib.request.urlopen = real_urlopen
        agent.is_stop_requested = real_is_stop

    check_equal("call_model posts to MODEL_URL", captured.get("url"), agent.MODEL_URL)
    check_equal(
        "call_model body == build_payload() for the same inputs",
        json.loads(captured.get("data", b"{}").decode("utf-8")),
        P.build_payload(spec, "SYS", "USER"),
    )
    check_equal(
        "call_model content type header unchanged",
        captured.get("headers", {}).get("Content-type"),
        "application/json",
    )




def section_c_metadata(agents):
    """C. Agent metadata: content, consistency and safety invariants."""
    section("C. Agent metadata")

    from agents import metadata as M

    check_equal("metadata consistency issues", M.validate_metadata_consistency(), [])
    check_equal(
        "metadata covers the registry + orchestrator",
        sorted(M.AGENT_METADATA),
        ["marketing", "opportunity", "research", "senior"],
    )

    future_capabilities = agents.Capability.get_future_capabilities()
    for agent_id, meta in sorted(M.AGENT_METADATA.items()):
        check(
            f"{agent_id}: id/role/permissions non-empty",
            bool(meta.id) and bool(meta.role) and bool(meta.permissions),
        )
        check(
            f"{agent_id}: no future capability granted",
            not (set(meta.capabilities) & future_capabilities),
            str(sorted(set(meta.capabilities) & future_capabilities)),
        )
        check(f"{agent_id}: read-only until a later phase", meta.is_read_only())
        check(
            f"{agent_id}: cannot use write tools",
            not (set(meta.allowed_tools) & M.WRITE_TOOLS),
        )

    marketing = M.AGENT_METADATA["marketing"]
    check_equal(
        "marketing capabilities", list(marketing.capabilities), ["analysis", "report"]
    )
    check(
        "marketing permission labels",
        M.PERMISSION_NO_WRITE in marketing.permissions
        and M.PERMISSION_WRITE not in marketing.permissions,
    )
    check_equal(
        "research capabilities == for_research_agent()",
        set(M.AGENT_METADATA["research"].capabilities),
        set(agents.AgentPermissionModel.for_research_agent().allowed_capabilities),
    )
    check_equal(
        "metadata to_dict is JSON-serialisable",
        json.loads(json.dumps(marketing.to_dict()))["id"],
        "marketing",
    )

    unknown = M.get_metadata_for_type("brand_new_agent")
    check(
        "unknown agent type -> restrictive default",
        unknown.is_read_only() and M.PERMISSION_NO_WRITE in unknown.permissions,
        str(unknown.permissions),
    )

    # The validator must really detect problems (not be vacuous).
    bad_model = agents.AgentPermissionModel(
        agent_name="bad_agent",
        agent_type="bad",
        allowed_capabilities={"analysis", "file_write"},
        allowed_tools={"call_model", "write_file"},
        restricted_tools=set(),
    )
    bad_metadata = M.build_metadata("bad", model=bad_model)
    check(
        "validator flags future capability and non read-only agent",
        len(M.validate_metadata(bad_metadata, model=bad_model)) >= 2,
        str(M.validate_metadata(bad_metadata, model=bad_model)),
    )

    instance = agents.MarketingAgent(
        tools={},
        permissions=agents.AgentPermissionModel.for_marketing_agent(),
    )
    check_equal("Agent.get_metadata() id", instance.get_metadata().id, "marketing")




def section_d_persona_and_routing(agents):
    """D. Persona structure stays inert + multi-agent routing."""
    section("D. Persona structure and multi-agent routing")

    registry = agents.create_default_registry()
    check_equal("default persona registry is empty", registry.size, 0)
    check(
        "no persona prompt is injected by default",
        registry.get_system_prompt("marketing") == "",
    )
    check_equal(
        "display name falls back to the technical name",
        agents.get_display_name("marketing", registry),
        "marketing",
    )

    journal = []
    llm_calls = []

    def stub_call_model(system_prompt, user_prompt, *args, **kwargs):
        llm_calls.append((system_prompt, user_prompt))
        return "STUB_LLM_OUTPUT"

    def stub_record_action(**kwargs):
        journal.append(kwargs)
        return None

    tools = {
        "call_model": stub_call_model,
        "safe_path": lambda root, rel: os.path.join(root, rel),
        "is_write_allowed": lambda permission: permission is True,
        "is_stop_requested": lambda event=None: False,
        "record_action": stub_record_action,
        "mask_sensitive_content": lambda content: content,
    }

    def route(request, stop_event=None):
        journal.clear()
        llm_calls.clear()
        senior = agents.SeniorAgent(tools=tools, stop_event=stop_event)
        return senior.run(
            request,
            project_name="phase7-check",
            project_root="",
            allow_write=False,
            stop_event=stop_event,
            user_request=request,
        )

    def used_agents():
        # Phase 7-A: the journal also holds orchestrator evidence records
        # (agent_routing). Sub-agent attribution is asserted on the
        # specialists' own agent_delegation entries only.
        return [
            entry.get("agent_id") for entry in journal
            if entry.get("action_type") == "agent_delegation"
        ]

    marketing_request = agents.MarketingAgent.TRIGGERS[0]
    output = route(marketing_request)
    check_equal(
        "marketing request -> MarketingAgent", used_agents(), ["marketing_agent"]
    )
    check_equal("marketing result is returned", output, "STUB_LLM_OUTPUT")

    research_request = first_trigger(
        agents.ResearchAgent,
        agents.MarketingAgent.TRIGGERS + agents.OpportunityAgent.TRIGGERS,
    )
    route(research_request)
    check_equal("research request -> ResearchAgent", used_agents(), ["research_agent"])

    opportunity_request = first_trigger(
        agents.OpportunityAgent,
        agents.MarketingAgent.TRIGGERS + agents.ResearchAgent.TRIGGERS,
    )
    route(opportunity_request)
    check_equal(
        "opportunity request -> OpportunityAgent",
        used_agents(),
        ["opportunity_agent"],
    )

    output = route("hello there, plain request")
    check(
        "unrecognized request -> skeleton analysis, no sub-agent, no LLM",
        # Phase 7-A: a skeleton agent_routing evidence record is expected;
        # the original intent is that NO sub-agent ran and NO LLM was called.
        used_agents() == [] and llm_calls == []
        and isinstance(output, str) and bool(output),
        str(output)[:120],
    )

    stopper = threading.Event()
    stopper.set()
    output = route(marketing_request, stop_event=stopper)
    check(
        "emergency stop blocks delegation before the LLM",
        journal == [] and llm_calls == [] and output != "STUB_LLM_OUTPUT",
    )

    return tools, journal, llm_calls




def section_e_legacy_and_safety(agent):
    """E. Legacy path preserved + safety layers intact."""
    section("E. Legacy path and safety layers")

    check("run_agent exists", callable(getattr(agent, "run_agent", None)))
    check("run_agent_legacy exists", callable(getattr(agent, "run_agent_legacy", None)))
    check(
        "multi-agent flag defaults to False (legacy stays the default path)",
        agent.use_multi_agent is False,
        str(agent.use_multi_agent),
    )

    source = inspect.getsource(agent.run_agent)
    order = [
        source.find("is_stop_requested"),
        source.find("handle_pending_action_request"),
        source.find("use_multi_agent"),
        source.find("should_use_agentic_loop"),
        source.find("run_agent_legacy"),
    ]
    check(
        "dispatch order: stop -> approval -> multi-agent -> loop -> legacy",
        all(index >= 0 for index in order) and order == sorted(order),
        str(order),
    )
    check(
        "multi-agent branch is guarded so failures fall back",
        source.count("except Exception") >= 2,
    )
    check(
        "approval gate is present",
        callable(getattr(agent, "handle_pending_action_request", None))
        and callable(getattr(agent, "detect_control_reply", None)),
    )
    check(
        "rollback/undo support is present",
        callable(getattr(agent, "undo_last_mutating_action", None))
        or callable(getattr(agent, "rollback_file", None)),
    )

    check(
        "plain request does not trigger the agentic loop",
        agent.should_use_agentic_loop("hello there", BASE_DIR) is False,
    )

    check("is_write_allowed(True) is True", agent.is_write_allowed(True) is True)
    check("is_write_allowed fails closed for None", agent.is_write_allowed(None) is False)
    check(
        "is_write_allowed fails closed for strings",
        agent.is_write_allowed("yes") is False,
    )

    if agent.is_emergency_stop_requested():
        check(
            "central emergency stop state is clean",
            False,
            "EMERGENCY_STOP_EVENT is set",
        )
    else:
        check(
            "is_stop_requested(None) is False while no stop is active",
            agent.is_stop_requested(None) is False,
        )
        set_event = threading.Event()
        set_event.set()
        check(
            "is_stop_requested respects a set event",
            agent.is_stop_requested(set_event) is True,
        )

    temp_root = tempfile.mkdtemp(prefix="phase7_check_")
    try:
        inside = os.path.abspath(agent.safe_path(temp_root, "notes.txt"))
        check(
            "safe_path allows a path inside the project",
            inside.startswith(os.path.abspath(temp_root) + os.sep),
            inside,
        )
        check_raises(
            "safe_path blocks path traversal",
            ValueError,
            agent.safe_path,
            temp_root,
            os.path.join("..", "escape.txt"),
        )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    masked = agent.mask_sensitive_content("API_KEY = 'super-secret-value'")
    check(
        "sensitive values are masked, the key name stays",
        "super-secret-value" not in masked and "API_KEY" in masked,
        masked[:80],
    )




def section_f_phase5(agents, tools, llm_calls, journal):
    """F. Phase 5 regression: OpportunityAgent + read-only enforcement."""
    section("F. Phase 5 regression")

    check("opportunity agent is registered", "opportunity" in agents.AGENT_REGISTRY)
    check(
        "opportunity triggers are present",
        len(agents.OpportunityAgent.TRIGGERS) > 5,
        str(len(agents.OpportunityAgent.TRIGGERS)),
    )

    permissions = agents.AgentPermissionModel.for_opportunity_agent()
    sub_agent = agents.OpportunityAgent(tools=tools, permissions=permissions)

    check_raises(
        "read-only agent cannot get the write_file tool",
        PermissionError,
        sub_agent.get_tool,
        "write_file",
    )
    check(
        "read-only agent can get call_model",
        callable(sub_agent.get_tool("call_model")),
    )
    check(
        "opportunity agent declares analysis capability",
        sub_agent.has_capability("analysis") is True,
    )
    check(
        "opportunity agent is denied file_write capability",
        sub_agent.has_capability("file_write") is False,
    )

    llm_calls.clear()
    journal.clear()
    stopper = threading.Event()
    stopper.set()
    stopped_agent = agents.OpportunityAgent(
        tools=tools, permissions=permissions, stop_event=stopper
    )
    result = stopped_agent.run("task", project_name="phase7-check", user_request="task")
    check(
        "stopped sub-agent returns a notice and never calls the LLM",
        bool(result) and llm_calls == [],
        str(result)[:80],
    )

    llm_calls.clear()
    journal.clear()
    active_agent = agents.OpportunityAgent(
        tools=tools, permissions=permissions, stop_event=None
    )
    result = active_agent.run("task", project_name="phase7-check", user_request="task")
    check_equal("active sub-agent uses the shared LLM tool", result, "STUB_LLM_OUTPUT")
    check_equal(
        "sub-agent logs its own attribution",
        [entry.get("agent_id") for entry in journal],
        ["opportunity_agent"],
    )




def section_g_phase6(agents):
    """G. Phase 6 regression: capabilities, permissions, proposals, gatekeeper."""
    section("G. Phase 6 regression")

    from agents import capabilities as C

    check_equal(
        "default marketing capabilities",
        C.get_agent_capabilities("marketing"),
        ["analysis", "report"],
    )
    check(
        "validate_capability accepts a real capability",
        C.validate_capability("analysis"),
    )
    check("validate_capability rejects unknown", C.validate_capability("nope") is False)
    check(
        "active and future capabilities are separated",
        len(agents.Capability.get_future_capabilities()) > 0
        and len(agents.Capability.get_active_capabilities()) > 0,
    )
    check_raises(
        "unknown capability is rejected by the permission model",
        ValueError,
        agents.AgentPermissionModel,
        agent_name="x_agent",
        agent_type="x",
        allowed_capabilities={"not_a_capability"},
    )
    check_raises(
        "allowed and restricted capabilities cannot overlap",
        ValueError,
        agents.AgentPermissionModel,
        agent_name="x_agent",
        agent_type="marketing",
        allowed_capabilities={"analysis"},
        restricted_capabilities={"analysis"},
    )

    check_equal(
        "for_agent_type('marketing') == for_marketing_agent()",
        agents.AgentPermissionModel.for_agent_type("marketing").allowed_capabilities,
        agents.AgentPermissionModel.for_marketing_agent().allowed_capabilities,
    )
    check_equal(
        "for_agent_type tolerates the _agent suffix",
        agents.AgentPermissionModel.for_agent_type(
            "research_agent"
        ).allowed_capabilities,
        agents.AgentPermissionModel.for_research_agent().allowed_capabilities,
    )
    default_model = agents.AgentPermissionModel.for_agent_type("unknown_type")
    check(
        "for_agent_type falls back to a deny-all model (fail closed)",
        default_model.allowed_capabilities == set()
        and default_model.can_use_capability("analysis") is False
        and default_model.can_use_tool("write_file") is False,
        str(sorted(default_model.allowed_capabilities)),
    )
    check(
        "senior permission model cannot use write tools",
        agents.AgentPermissionModel.for_senior_agent().can_use_tool("write_file")
        is False,
    )

    proposal = agents.create_analysis_proposal("marketing_agent", "marketing", "why")
    check("analysis proposal is safe", agents.is_proposal_safe(proposal))
    check("analysis proposal needs no approval", proposal.requires_approval is False)
    check("analysis proposal validates", proposal.validate() is True)

    write_proposal = agents.ActionProposal(
        proposal_id="p1",
        agent_name="x_agent",
        agent_type="x",
        action_type=agents.ActionType.FILE_WRITE,
        reason="r",
    )
    check(
        "file_write proposal requires approval and is not auto-safe",
        write_proposal.requires_approval is True
        and agents.is_proposal_safe(write_proposal) is False,
    )
    check(
        "all future action types are approval-gated",
        {
            agents.ActionType.FILE_WRITE,
            agents.ActionType.WALLET_ACTION,
            agents.ActionType.EXTERNAL_API,
            agents.ActionType.BROWSER_ACTION,
            agents.ActionType.TRANSACTION,
        }
        <= agents.APPROVAL_REQUIRED_ACTIONS,
    )
    empty_proposal = agents.ActionProposal(
        proposal_id="",
        agent_name="",
        agent_type="x",
        action_type=agents.ActionType.ANALYSIS,
        reason="",
    )
    check("incomplete proposal fails validation", empty_proposal.validate() is False)
    check(
        "proposal log line hides params",
        "params" not in proposal.to_safe_string(),
    )

    senior = agents.SeniorAgent(tools={})
    check(
        "gatekeeper allows registered agent types",
        senior._can_delegate_to("marketing", agents.MarketingAgent) is True,
    )
    check(
        "gatekeeper rejects unregistered agent types",
        senior._can_delegate_to("nonexistent", object) is False,
    )
    result = senior._delegate_to_subagent(
        "nonexistent", "task", "phase7-check", "", False, None, "task"
    )
    check(
        "unregistered delegation is refused with a message",
        isinstance(result, str) and bool(result),
        str(result)[:80],
    )




def section_d2_integration(agent):
    """D2. Integration: agent.run_agent -> SeniorAgent (LLM stubbed)."""
    section("D.2 Integration through agent.run_agent")

    import agents

    llm_calls = []
    journal = []

    def stub_call_model(system_prompt, user_prompt, *args, **kwargs):
        llm_calls.append((system_prompt, user_prompt))
        return "STUB_LLM_OUTPUT"

    def stub_record_action(**kwargs):
        journal.append(kwargs)
        return None

    def stub_remember(role, message, project_name):
        return None

    # run_agent only reaches the multi-agent branch when the project root
    # exists. A temporary directory is used so no real project is touched.
    temp_root = tempfile.mkdtemp(prefix="phase7_int_")

    saved_call_model = agent.call_model
    saved_record_action = agent.record_action
    saved_remember = agent.remember_conversation_message
    saved_get_project_path = agent.get_project_path
    saved_flag = agent.use_multi_agent

    try:
        agent.call_model = stub_call_model
        agent.record_action = stub_record_action
        agent.remember_conversation_message = stub_remember
        agent.get_project_path = lambda name: temp_root
        agent.use_multi_agent = True

        result = agent.run_agent(
            agents.MarketingAgent.TRIGGERS[0],
            "phase7-check",
            allow_write=False,
            stop_event=threading.Event(),
        )
    finally:
        agent.call_model = saved_call_model
        agent.record_action = saved_record_action
        agent.remember_conversation_message = saved_remember
        agent.get_project_path = saved_get_project_path
        agent.use_multi_agent = saved_flag
        shutil.rmtree(temp_root, ignore_errors=True)

    check_equal(
        "run_agent with multi-agent on reaches the stubbed LLM",
        result,
        "STUB_LLM_OUTPUT",
    )
    check_equal(
        "run_agent delegated to MarketingAgent",
        # Phase 7-A: ignore orchestrator agent_routing evidence records.
        [entry.get("agent_id") for entry in journal
         if entry.get("action_type") == "agent_delegation"],
        ["marketing_agent"],
    )
    check_equal(
        "run_agent kept its own call_model stub usage (no real LLM call)",
        len(llm_calls),
        1,
    )
    check(
        "use_multi_agent flag restored after the test",
        agent.use_multi_agent is False,
    )


def main() -> int:
    """Run every section and print a summary."""
    print("Phase 7 self-check (no network, no state changes)")
    print("Project: " + BASE_DIR)

    agents, agent = section_a_imports()
    section_b_providers(agent)
    section_b2_payload_parity(agent)
    section_c_metadata(agents)
    tools, journal, llm_calls = section_d_persona_and_routing(agents)
    section_d2_integration(agent)
    section_e_legacy_and_safety(agent)
    section_f_phase5(agents, tools, llm_calls, journal)
    section_g_phase6(agents)

    passed = sum(1 for _name, ok, _detail in RESULTS if ok)
    failures = [(name, detail) for name, ok, detail in RESULTS if not ok]

    print("\n=== Summary ===")
    print(f"checks: {len(RESULTS)}  passed: {passed}  failed: {len(failures)}")
    for name, detail in failures:
        print(f"  FAILED: {name} [{detail}]")

    if failures:
        return 1

    print("Phase 7 self-check: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

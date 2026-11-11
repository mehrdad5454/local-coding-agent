"""
Test Web Research Mode: fetch_page / web_search requests from the web UI
enter the agentic loop, and only web_search / fetch_page are permitted.

Run from project root:
    .venv\\Scripts\\python.exe tests\\test_web_research_mode.py
"""

import os
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import agent
import web_agent

results = []


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


# ---------------------------------------------------------
# 1) should_use_agentic_loop routes web tools
# ---------------------------------------------------------
def test_agentic_loop_routing_for_web_tools():
    section("1. should_use_agentic_loop routes web tool requests")
    root = tempfile.mkdtemp(prefix="web_research_test_")
    try:
        check(
            "fetch_page request does not force project-mode loop",
            not agent.should_use_agentic_loop(
                "fetch_page https://example.com/page", root
            ),
        )
        check(
            "web_search request does not force project-mode loop",
            not agent.should_use_agentic_loop(
                "web_search latest AI news", root
            ),
        )
        check(
            "simple read-only request without web tools does not force loop",
            not agent.should_use_agentic_loop(
                "file style.css را نگاه کن", root
            ),
        )
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------
# 2) Tool restriction in General Mode
# ---------------------------------------------------------
def test_tool_restriction_in_general_mode():
    section("2. Tool dispatch blocks all tools except web_search / web_extract in General Mode")

    check(
        "_WEB_RESEARCH_ALLOWED_TOOLS contains only web tools",
        agent._WEB_RESEARCH_ALLOWED_TOOLS
        == frozenset(("web_search", "web_extract")),
        safe(agent._WEB_RESEARCH_ALLOWED_TOOLS),
    )

    check(
        "allow_write is True for MODE_APPLY (write mode)",
        web_agent.resolve_allow_write("write") is True,
    )

    check(
        "allow_write is False for MODE_REVIEW (read mode)",
        web_agent.resolve_allow_write("read") is False,
    )



# ---------------------------------------------------------
# 3) run_loop_steps blocks non-web tools in general mode
# ---------------------------------------------------------
def test_run_loop_blocks_non_web_tools_in_general_mode():
    section("3. run_loop_steps blocks disallowed tools in General Mode")
    root = tempfile.mkdtemp(prefix="web_research_block_")
    try:
        blocked_calls = {"count": 0}

        def stub_blocked(*args, **kwargs):
            blocked_calls["count"] += 1
            return "UNEXPECTED_TOOL_CALL_RESULT"

        call_log = {"count": 0, "prompts": []}

        def stub_model(system_prompt, user_prompt, *args, **kwargs):
            idx = call_log["count"]
            call_log["count"] += 1
            call_log["prompts"].append(user_prompt)
            if idx == 0:
                return "web_search latest AI news"
            return "FINAL: جستجو کردم"

        saved_model = agent.call_model
        saved_dispatch = getattr(agent, '_dispatch_tool', None)
        agent.call_model = stub_model
        agent._dispatch_tool = stub_blocked
        try:
            report = agent.run_loop_steps(
                user_request="web_search latest AI news",
                project_root=root,
                project_name="general",
                allow_write=False,
                transcript=[],
                observations=[],
                steps_used=0,
                correction_attempts=0,
                max_steps=agent.MAX_AGENT_STEPS,
                known_request_status=None,
                allowed_tools=agent._WEB_RESEARCH_ALLOWED_TOOLS,
            )
            check(
                "loop returns a string report",
                isinstance(report, str) and report,
                safe(report)[:200] if isinstance(report, str) else safe(report),
            )
            check(
                "no disallowed tool was dispatched in general mode",
                blocked_calls["count"] == 0,
                "blocked called {} times".format(blocked_calls["count"]),
            )
        finally:
            agent.call_model = saved_model
            if saved_dispatch is not None:
                agent._dispatch_tool = saved_dispatch
            else:
                if hasattr(agent, '_dispatch_tool'):
                    del agent._dispatch_tool
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_run_loop_rejects_disallowed_tool_and_builds_report():
    section("4. run_loop_steps rejects disallowed tool and builds report")
    root = tempfile.mkdtemp(prefix="web_research_reject_")
    try:
        blocked_calls = {"count": 0}

        def stub_blocked(*args, **kwargs):
            blocked_calls["count"] += 1
            return "UNEXPECTED_TOOL_CALL_RESULT"

        call_log = {"count": 0}

        def stub_model(system_prompt, user_prompt, *args, **kwargs):
            idx = call_log["count"]
            call_log["count"] += 1
            if idx == 0:
                return "TOOL: edit_file\nARGS: {\"path\": \"x.py\"}"
            return "FINAL: رد شد"

        saved_model = agent.call_model
        saved_dispatch = getattr(agent, "_dispatch_tool", None)
        agent.call_model = stub_model
        agent._dispatch_tool = stub_blocked
        try:
            report = agent.run_loop_steps(
                user_request="edit file",
                project_root=root,
                project_name="general",
                allow_write=False,
                transcript=[],
                observations=[],
                steps_used=0,
                correction_attempts=0,
                max_steps=agent.MAX_AGENT_STEPS,
                known_request_status=None,
                allowed_tools=agent._WEB_RESEARCH_ALLOWED_TOOLS,
            )
            check(
                "report is a non-empty string after rejection",
                isinstance(report, str) and bool(report),
                safe(report)[:200] if isinstance(report, str) else safe(report),
            )
            check(
                "disallowed tool was not dispatched",
                blocked_calls["count"] == 0,
                "dispatched {} times".format(blocked_calls["count"]),
            )
            check(
                "report contains rejection observation",
                "ابزار غیرمجاز" in (report or ""),
                safe(report)[:200] if isinstance(report, str) else safe(report),
            )
        finally:
            agent.call_model = saved_model
            if saved_dispatch is not None:
                agent._dispatch_tool = saved_dispatch
            else:
                if hasattr(agent, "_dispatch_tool"):
                    del agent._dispatch_tool
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    test_agentic_loop_routing_for_web_tools()
    test_tool_restriction_in_general_mode()
    test_run_loop_blocks_non_web_tools_in_general_mode()
    test_run_loop_rejects_disallowed_tool_and_builds_report()

    passed = sum(1 for _, ok in results if ok)
    print("\nRESULT: {}/{} passed".format(passed, len(results)))

    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

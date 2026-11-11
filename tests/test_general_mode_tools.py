import agent


def test_allowed_tools_subset_of_registry():
    allowed = agent._WEB_RESEARCH_ALLOWED_TOOLS
    for name in allowed:
        assert name in agent.TOOL_REGISTRY


def test_build_tool_reference_filters_allowed():
    ref = agent.build_tool_reference(agent._WEB_RESEARCH_ALLOWED_TOOLS)
    assert "web_search" in ref
    assert "web_extract" in ref
    assert "read_file" not in ref
    assert "run_command" not in ref


def test_general_workspace_not_in_prompt_when_none():
    prompt = agent.build_loop_user_prompt("q", "general", False, [], [], 0, project_root=None)
    assert "index.html" not in prompt
    assert "style.css" not in prompt
    assert "script.js" not in prompt

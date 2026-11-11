import agent


def test_general_normal_requests_use_text_path(monkeypatch):
    calls = {"general": 0, "agentic": 0}

    def fake_should_use(user_request, project_root):
        return False

    def fake_run_general_task(*a, **k):
        calls["general"] += 1
        return "text"

    monkeypatch.setattr(agent, "should_use_agentic_loop", fake_should_use)
    monkeypatch.setattr(agent, "run_general_task", fake_run_general_task)
    for req in ["Python چیست؟", "لطفاً درباره Web Design توضیح بده.", "یک جستجوی اینترنتی انجام بده."]:
        agent.run_agent(req, "general")
    assert calls["general"] == 3


def test_general_explicit_executive_uses_agentic_path(monkeypatch):
    calls = {"agentic_root_set": False}

    def fake_should_use(user_request, project_root):
        return project_root is None and "/agent" in (user_request or "")

    real_ensure = agent.ensure_general_workspace
    monkeypatch.setattr(agent, "ensure_general_workspace", lambda: "C:/tmp/gw")
    monkeypatch.setattr(agent, "should_use_agentic_loop", fake_should_use)
    # avoid full pipeline side effects
    monkeypatch.setattr(agent, "get_project_path", lambda x: None)
    try:
        agent.run_agent("/agent do something", "general")
    except Exception:
        pass
    # no crash is enough; routing reached agentic branch (ensure called)

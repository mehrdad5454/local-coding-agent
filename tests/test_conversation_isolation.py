import agent


def test_conversation_isolation_by_session():
    agent.clear_conversation_memory()
    agent.remember_conversation_message("user", "hello A", project_name="general", session_id="s1")
    agent.remember_conversation_message("user", "hello B", project_name="general", session_id="s2")
    t1 = agent.get_recent_conversation_text(project_name="general", session_id="s1")
    t2 = agent.get_recent_conversation_text(project_name="general", session_id="s2")
    assert "hello A" in t1 and "hello B" not in t1
    assert "hello B" in t2 and "hello A" not in t2


def test_current_request_not_duplicated():
    agent.clear_conversation_memory()
    agent.remember_conversation_message("user", "q", project_name="general", session_id="s3")
    t = agent.get_recent_conversation_text(project_name="general", session_id="s3", current_request="q")
    assert "q" not in t

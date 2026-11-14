"""Regression tests: project_root forwarding into the Agentic Loop.

These tests assert the ACTUAL argument received by ``run_agentic_loop``.
They do not merely check that no exception was raised.

Before the fix, ``run_agent`` passed a literal ``None`` as ``project_root``
(agent.py:14531), so every file tool inside the loop failed with
``TypeError: _path_normpath: path should be string, bytes or os.PathLike,
not NoneType`` and the report printed ``مسیر پروژه: None``.

No model is called, no network is used, no real project file is touched.
Both tests mock ``run_agentic_loop`` and use temporary directories only.
"""

import os
import shutil
import tempfile

import pytest

import agent


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------
def _mock_loop(monkeypatch):
    """Replace run_agentic_loop with a recorder and return the capture dict."""
    captured = {}

    def fake_run_agentic_loop(user_request, project_root, project_name, **kwargs):
        captured["called"] = True
        captured["user_request"] = user_request
        captured["project_root"] = project_root
        captured["project_name"] = project_name
        captured["kwargs"] = kwargs
        return "LOOP_REPORT_STUB"

    monkeypatch.setattr(agent, "run_agentic_loop", fake_run_agentic_loop)
    return captured


def _neutralize_gates(monkeypatch):
    """Disable every gate that could return before the loop is reached."""
    monkeypatch.setattr(
        agent, "handle_pending_action_request", lambda *a, **k: None
    )
    monkeypatch.setattr(agent, "detect_control_reply", lambda req: None)
    monkeypatch.setattr(agent, "should_use_agentic_loop", lambda req, root: True)
    # Durable memory writes to disk; keep the test side-effect free.
    monkeypatch.setattr(
        agent, "_record_durable_interaction", lambda *a, **k: None
    )
    monkeypatch.setattr(agent, "remember_conversation_message", lambda *a, **k: None)


# ---------------------------------------------------------
# 1) Project mode: the real project root must be forwarded
# ---------------------------------------------------------
def test_project_mode_forwards_real_project_root(monkeypatch):
    root = tempfile.mkdtemp(prefix="root_prop_project_")
    try:
        captured = _mock_loop(monkeypatch)

        monkeypatch.setattr(agent, "is_general_mode", lambda name: False)
        monkeypatch.setattr(agent, "get_project_path", lambda name: root)
        _neutralize_gates(monkeypatch)

        agent.run_agent("ساختار پروژه را بررسی کن", "test-proj")

        assert captured.get("called"), (
            "run_agentic_loop هرگز فراخوانی نشد؛ "
            "درخواست به مسیر Agentic Loop نرسید."
        )
        assert captured["project_root"] is not None, (
            "project_root = None به run_agentic_loop پاس شد؛ "
            "مقدار واقعی پروژه منتقل نشده است."
        )
        assert captured["project_root"] == root, (
            "project_root منتقل‌شده با ریشه واقعی پروژه برابر نیست: "
            "%r != %r" % (captured["project_root"], root)
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------
# 2) General mode: the General Workspace root must be forwarded
# ---------------------------------------------------------
def test_general_mode_forwards_general_workspace_root(monkeypatch):
    workspace = tempfile.mkdtemp(prefix="root_prop_general_")
    try:
        captured = _mock_loop(monkeypatch)

        monkeypatch.setattr(agent, "is_general_mode", lambda name: True)
        monkeypatch.setattr(agent, "get_project_path", lambda name: None)
        monkeypatch.setattr(agent, "ensure_general_workspace", lambda: workspace)
        _neutralize_gates(monkeypatch)

        agent.run_agent("/agent ساختار را بررسی کن", "general")

        assert captured.get("called"), (
            "run_agentic_loop هرگز فراخوانی نشد؛ "
            "درخواست اجرایی General Mode به حلقه نرسید."
        )
        assert captured["project_root"] is not None, (
            "project_root = None به run_agentic_loop پاس شد؛ "
            "مسیر General Workspace منتقل نشده است."
        )
        assert captured["project_root"] == workspace, (
            "project_root منتقل‌شده با مسیر General Workspace برابر نیست: "
            "%r != %r" % (captured["project_root"], workspace)
        )

        # General Mode must still restrict the tool surface to web tools.
        assert captured["kwargs"].get("allowed_tools") == (
            agent._WEB_RESEARCH_ALLOWED_TOOLS
        ), "محدودیت ابزار General Mode تغییر کرده است."
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
import agent


def test_evidence_gating_basic():
    transcript = [{"step": 1, "tool": "read_file", "args": {"path": "a.py"}, "result": {"ok": True, "text": "x"}, "mutating": False, "verified": None}]
    report = agent.build_loop_report("general", "/tmp", transcript, final_answer="done")
    assert "a.py" in report
    assert "هیچ فایلی در اجرای فعلی خوانده نشد" not in report

    report2 = agent.build_loop_report("general", "/tmp", [], final_answer="done")
    assert "هیچ فایلی در اجرای فعلی خوانده نشد" in report2

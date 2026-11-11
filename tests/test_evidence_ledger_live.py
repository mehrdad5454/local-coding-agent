"""Phase 2 tests: minimal live wiring of the evidence ledger.

Covers build_loop_report deriving records from the real transcript,
without network access and without changing visible output.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent


def _web_transcript():
    return [
        {
            "step": 1,
            "tool": "fetch_page",
            "args": {"url": "https://example.com/docs"},
            "result": {
                "ok": True,
                "verified": True,
                "text": "SOURCE: https://example.com/docs\n\nhello world",
                "data": {"url": "https://example.com/docs"},
            },
            "mutating": False,
            "verified": None,
        },
        {
            "step": 2,
            "tool": "fetch_page",
            "args": {"url": "https://example.com/empty"},
            "result": {
                "ok": True,
                "verified": False,
                "text": (
                    "SOURCE: https://example.com/empty\n\n"
                    "(no readable text extracted)"
                ),
                "data": {"url": "https://example.com/empty"},
            },
            "mutating": False,
            "verified": None,
        },
        {
            "step": 3,
            "tool": "fetch_page",
            "args": {"url": "https://example.com/down"},
            "result": {
                "ok": False,
                "verified": None,
                "text": "خطا: timeout",
                "data": {},
            },
            "mutating": False,
            "verified": None,
        },
        {
            "step": 4,
            "tool": "web_search",
            "args": {"query": "fastapi"},
            "result": {
                "ok": True,
                "verified": True,
                "text": (
                    "SEARCH: fastapi\n\n1. Docs\n"
                    "   https://example.com/a\n"
                ),
                "data": {"query": "fastapi", "hits": 1},
            },
            "mutating": False,
            "verified": None,
        },
        {
            "step": 5,
            "tool": "web_search",
            "args": {"query": "nothing-here"},
            "result": {
                "ok": True,
                "verified": True,
                "text": "(no results)",
                "data": {"query": "nothing-here", "hits": 0},
            },
            "mutating": False,
            "verified": None,
        },
        {
            "step": 6,
            "tool": "read_file",
            "args": {"path": "a.py"},
            "result": {"ok": True, "text": "x", "data": {}},
            "mutating": False,
            "verified": None,
        },
    ]


def test_loop_report_derives_live_ledger_records():
    transcript = _web_transcript()
    report = agent.build_loop_report(
        "general", "/tmp/proj", transcript,
    )
    assert isinstance(report, str)
    records = agent.get_loop_report_ledger(report)
    assert [r["status"] for r in records] == [
        "ok", "empty", "failed", "ok", "no_hits",
    ]
    assert records[0]["url"] == "https://example.com/docs"
    assert records[1]["url"] == "https://example.com/empty"
    assert records[2]["url"] == "https://example.com/down"
    assert records[3]["query"] == "fastapi"
    assert records[4]["hits"] == 0
    # Non-web tools are ignored; records stay aligned to transcript.
    assert all(r["tool"] in ("web_search", "fetch_page") for r in records)


def test_loop_report_output_unchanged_by_live_wiring():
    transcript = _web_transcript()
    report = agent.build_loop_report(
        "general", "/tmp/proj", transcript,
    )
    # Capture the exact visible text build_report produces by
    # intercepting it: the wiring must not alter lines/verify_lines.
    captured = {}

    real_build_report = agent.build_report

    def spy_build_report(stage, project_name, project_root,
                         lines, verify_lines=None):
        captured["text"] = real_build_report(
            stage, project_name, project_root, lines, verify_lines,
        )
        return captured["text"]

    saved = agent.build_report
    agent.build_report = spy_build_report
    try:
        report2 = agent.build_loop_report(
            "general", "/tmp/proj", transcript,
        )
    finally:
        agent.build_report = saved
    assert captured.get("text") is not None
    # Byte-identical visible output before/after wiring.
    assert str(report) == captured["text"]
    assert str(report2) == captured["text"]
    assert isinstance(report, str)
    # Ledger rides alongside the text without leaking into it.
    assert "ledger_records" not in str(report)
    assert agent.get_loop_report_ledger(report) != []


def test_forced_final_path_reaches_common_report_with_ledger():
    transcript = _web_transcript()
    forced = "FINAL: done"
    report = agent.build_loop_report(
        "general", "/tmp/proj", transcript, final_answer=forced,
    )
    assert isinstance(report, str)
    assert "FINAL: done" in report
    records = agent.get_loop_report_ledger(report)
    assert [r["status"] for r in records] == [
        "ok", "empty", "failed", "ok", "no_hits",
    ]

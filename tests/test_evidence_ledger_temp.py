"""Phase 1 tests for the temporary evidence ledger.

Covers transcript -> record mapping with local dicts only.
No network access and no agent.py import.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from evidence import ledger_temp


def _ok_fetch():
    return {
        "step": 1,
        "tool": "fetch_page",
        "args": {"url": "https://example.com/docs?b=2"},
        "result": {
            "ok": True,
            "verified": True,
            "text": "SOURCE: https://example.com/docs\n\nhello world",
            "data": {
                "url": "https://example.com/docs",
                "content_type": "text/html",
            },
        },
    }


def test_fetch_ok_with_readable_text():
    record = ledger_temp.entry_to_record(_ok_fetch())
    assert record["status"] == "ok"
    assert record["tool"] == "fetch_page"
    assert record["url"] == "https://example.com/docs"
    assert record["step"] == 1


def test_fetch_ok_but_empty_text_is_empty():
    entry = _ok_fetch()
    entry["result"] = {
        "ok": True,
        "verified": False,
        "text": "SOURCE: https://example.com/empty\n\n(no readable text extracted)",
        "data": {"url": "https://example.com/empty"},
    }
    record = ledger_temp.entry_to_record(entry)
    # ok=True alone must not count as success when verified is False.
    assert record["status"] == "empty"
    assert record["url"] == "https://example.com/empty"


def test_fetch_failed():
    entry = {
        "step": 3,
        "tool": "fetch_page",
        "args": {"url": "https://example.com/down"},
        "result": {"ok": False, "verified": None, "text": "خطا: timeout", "data": {}},
    }
    record = ledger_temp.entry_to_record(entry)
    assert record["status"] == "failed"
    # URL falls back to tool args when data.url is missing.
    assert record["url"] == "https://example.com/down"
    assert record["error"]


def test_search_ok_with_hits():
    entry = {
        "step": 2,
        "tool": "web_search",
        "args": {"query": "fastapi"},
        "result": {
            "ok": True,
            "verified": True,
            "text": "SEARCH: fastapi\n\n1. Docs\n   https://example.com/a\n\n2. Guide\n   https://example.com/b\n",
            "data": {"query": "fastapi", "hits": 2},
        },
    }
    record = ledger_temp.entry_to_record(entry)
    assert record["status"] == "ok"
    assert record["query"] == "fastapi"
    assert record["hits"] == 2
    assert "https://example.com/a" in record["urls"]
    assert "https://example.com/b" in record["urls"]


def test_search_no_hits():
    entry = {
        "step": 4,
        "tool": "web_search",
        "args": {"query": "nothing-here"},
        "result": {
            "ok": True,
            "verified": True,
            "text": "(no results)",
            "data": {"query": "nothing-here", "hits": 0},
        },
    }
    record = ledger_temp.entry_to_record(entry)
    assert record["status"] == "no_hits"
    assert record["hits"] == 0


def test_fetch_url_fallback_from_source_line():
    entry = _ok_fetch()
    entry["result"] = {
        "ok": True,
        "verified": True,
        "text": "SOURCE: https://example.com/from-text\n\nbody",
        "data": {},
    }
    entry["args"] = {}
    record = ledger_temp.entry_to_record(entry)
    assert record["status"] == "ok"
    assert record["url"] == "https://example.com/from-text"


def test_records_survive_observation_compression():
    transcript = [_ok_fetch()]
    observations = ["step 1 | fetch_page | ... (severely truncated)"]
    records = ledger_temp.derive_from_transcript(transcript)
    assert len(records) == 1
    assert records[0]["status"] == "ok"
    # Ledger must not read observations at all.
    assert "example.com/docs" in records[0]["url"]
    # Observations are compressed: the full URL never appears there,
    # so the record could only have come from the transcript itself.
    assert all("example.com/docs" not in obs for obs in observations)


def test_non_web_tools_are_ignored():
    entry = {
        "step": 5,
        "tool": "read_file",
        "args": {"path": "a.py"},
        "result": {"ok": True, "text": "x", "data": {}},
    }
    assert ledger_temp.entry_to_record(entry) is None
    assert ledger_temp.derive_from_transcript([entry]) == []

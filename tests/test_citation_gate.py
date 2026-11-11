"""Phase 3 tests: minimal Citation Gate (no network, no agent import)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from evidence import citation_gate


def _fetch_ok(url):
    return {
        "tool": "fetch_page", "step": 1, "status": "ok",
        "url": url, "urls": [url], "query": None,
        "hits": None, "error": None,
    }


def _fetch_failed(url):
    return {
        "tool": "fetch_page", "step": 2, "status": "failed",
        "url": url, "urls": [url], "query": None,
        "hits": None, "error": "timeout",
    }


def _search_ok():
    return {
        "tool": "web_search", "step": 3, "status": "ok",
        "url": None, "urls": ["https://example.com/a"],
        "query": "fastapi", "hits": 2, "error": None,
    }


def test_traceable_urls_pass():
    text = "خلاصه: https://example.com/docs ابزار خوبی است."
    out = citation_gate.evaluate(text, [_fetch_ok("https://example.com/docs")])
    assert out["mode"] == "pass"
    assert out["unsupported"] == []
    assert out["caveat_lines"] == []
    assert out["stats"]["urls_total"] == 1


def test_unmatched_url_caveat_without_fake_claim():
    text = "ببینید https://example.com/ghost برای جزئیات."
    out = citation_gate.evaluate(text, [_fetch_ok("https://example.com/docs")])
    assert out["mode"] == "caveat"
    assert len(out["caveat_lines"]) <= 2
    assert out["unsupported"][0]["kind"] == "url"
    joined = " ".join(out["unsupported"][0]["reason"])
    assert "جعلی" not in joined or "احراز نشده" in joined


def test_page_claim_failed_fetch_caveat_only_when_linked():
    text = "طبق صفحه https://example.com/down نسخه 3 است."
    out = citation_gate.evaluate(
        text, [_fetch_failed("https://example.com/down")])
    assert out["mode"] == "caveat"
    kinds = [u["kind"] for u in out["unsupported"]]
    assert "page_claim" in kinds


def test_search_only_needs_conservative_conjunction():
    # New contract: the search-only warning is derived SOLELY from
    # ledger state (>=1 search ok, zero fetch ok) -- not from regex or
    # URLs in the answer text, so even a vague answer gets the caveat.
    vague = "نتایج جست‌وجو را بررسی کردم و موضوع جالب است."
    out = citation_gate.evaluate(vague, [_search_ok()])
    assert out["mode"] == "caveat"
    kinds = [u["kind"] for u in out["unsupported"]]
    assert "search_only_no_fetch" in kinds
    assert out["stats"]["search_ok"] is True
    assert out["stats"]["fetch_ok"] is False

    # Fail-safe preserved: no usable ledger evidence -> plain pass.
    assert citation_gate.evaluate(vague, [])["mode"] == "pass"
    assert citation_gate.evaluate(vague, None)["mode"] == "pass"

    # A successful fetch must not trigger the search-only warning.
    precise = "نسخه 3.2 منتشر شد و قیمت 99 دلار است."
    out2 = citation_gate.evaluate(
        precise, [_search_ok(), _fetch_ok("https://example.com/docs")])
    assert out2["mode"] == "pass"
    assert out2["unsupported"] == []


def test_numbers_alone_never_caveat():
    text = "در سال 2024 نسخه 3.2 با قیمت 99 منتشر شد. https://example.com/docs"
    out = citation_gate.evaluate(text, [_fetch_ok("https://example.com/docs")])
    assert out["mode"] == "pass"
    assert out["stats"]["numbers_total"] >= 2


def test_empty_or_invalid_ledger_passes():
    assert citation_gate.evaluate("متن https://x.com", [])["mode"] == "pass"
    assert citation_gate.evaluate("متن", None)["mode"] == "pass"
    assert citation_gate.evaluate("متن", "bad")["mode"] == "pass"
    assert citation_gate.evaluate(None, None)["mode"] == "pass"


def test_plain_text_passes():
    out = citation_gate.evaluate("گزارش متنی بدون ارجاع.", [_fetch_ok("https://a.com")])
    assert out["mode"] == "pass"


def test_examples_and_limits_do_not_warn():
    text = "مثال: https://example.com/demo\nمحدودیت شواهد: https://example.com/x"
    out = citation_gate.evaluate(text, [_fetch_ok("https://example.com/docs")])
    assert out["mode"] == "pass"


def test_pass_keeps_text_unchanged():
    text = "متن https://example.com/docs سالم است."
    out = citation_gate.evaluate(text, [_fetch_ok("https://example.com/docs")])
    assert out["mode"] == "pass"
    assert out["caveat_lines"] == []


def test_gate_errors_never_raise():
    out = citation_gate.evaluate(object(), object())
    assert out["mode"] == "pass"
    assert out["unsupported"] == []


def test_user_supplied_url_is_not_flagged():
    text = "درباره https://example.com/docs توضیح بده."
    out = citation_gate.evaluate(
        "خلاصه: https://example.com/ghost",
        [_fetch_ok("https://example.com/docs")],
        user_request="درباره https://example.com/ghost توضیح بده",
    )
    assert out["mode"] == "pass"


def test_caveat_lines_capped_at_two():
    text = "https://a.example/1 و https://b.example/2 و https://c.example/3"
    out = citation_gate.evaluate(text, [_fetch_ok("https://example.com/docs")])
    assert out["mode"] == "caveat"
    assert len(out["caveat_lines"]) <= 2

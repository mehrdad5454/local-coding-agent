"""Evidence Ledger (temporary, in-memory derivation).

Mirrors the read-only Web Research tools without importing agent.py.
"""

from __future__ import annotations

import re
import time

WEB_TOOLS = ("web_search", "fetch_page")

SOURCE_LINE_RE = re.compile(r"^SOURCE:\s*(.+)$", re.MULTILINE)
URL_RE = re.compile(r"https?://[^\s\"'<>|)\]]+", re.IGNORECASE)


def _str_or_none(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _url_from_fetch_args(args):
    if not isinstance(args, dict):
        return None
    for key in ("url", "link", "href"):
        found = _str_or_none(args.get(key))
        if found:
            return found
    return None


def _query_from_search_args(args):
    if not isinstance(args, dict):
        return None
    for key in ("query", "q", "search", "term"):
        found = _str_or_none(args.get(key))
        if found:
            return found
    return None


def _url_from_source_line(text):
    match = SOURCE_LINE_RE.search(str(text or ""))
    if not match:
        return None
    return _str_or_none(match.group(1).splitlines()[0])


def _urls_from_text(text):
    found = []
    seen = set()
    for url in URL_RE.findall(str(text or "")):
        url = url.strip()
        if url and url not in seen:
            seen.add(url)
            found.append(url)
    return found


def _record(tool, step, status, url=None, urls=None, query=None,
            hits=None, error=None, recorded_at=None):
    return {
        "tool": tool,
        "step": step,
        "status": status,
        "url": url,
        "urls": list(urls or []),
        "query": query,
        "hits": hits,
        "error": error,
        "recorded_at": recorded_at if recorded_at is not None else time.time(),
    }


def entry_to_record(entry, recorded_at=None):
    """Convert one transcript entry to a ledger record.

    Returns None for non web-research tools. Never raises on
    malformed entries: they become ``failed`` records instead.
    """
    if not isinstance(entry, dict):
        return None
    tool = str(entry.get("tool") or "")
    if tool not in WEB_TOOLS:
        return None

    step = entry.get("step")
    args = entry.get("args") if isinstance(entry.get("args"), dict) else {}
    result = entry.get("result")
    ts = recorded_at if recorded_at is not None else time.time()

    if not isinstance(result, dict):
        if tool == "fetch_page":
            return _record(tool, step, "failed", url=_url_from_fetch_args(args),
                           urls=[], query=None, error="missing result",
                           recorded_at=ts)
        return _record(tool, step, "failed", urls=[],
                       query=_query_from_search_args(args),
                       error="missing result", recorded_at=ts)

    ok = result.get("ok") is True
    text = result.get("text") if isinstance(result.get("text"), str) else str(result.get("text") or "")
    data = result.get("data") if isinstance(result.get("data"), dict) else {}

    if tool == "fetch_page":
        url = _str_or_none(data.get("url"))
        if not url:
            url = _url_from_fetch_args(args)
        if not url:
            url = _url_from_source_line(text)
        if not ok:
            return _record(tool, step, "failed", url=url,
                           urls=[url] if url else [], error=text.strip() or None,
                           recorded_at=ts)
        # ok=True alone is NOT enough: verified=False means the page
        # was reached but no readable text was extracted.
        if result.get("verified") is True:
            return _record(tool, step, "ok", url=url,
                           urls=[url] if url else [], recorded_at=ts)
        return _record(tool, step, "empty", url=url,
                       urls=[url] if url else [],
                       error=text.strip() or None, recorded_at=ts)

    # web_search: verified=True only means "the tool ran", never
    # content correctness. Status comes from hits / text only.
    query = _str_or_none(data.get("query")) or _query_from_search_args(args)
    hits = data.get("hits")
    urls = _urls_from_text(text)
    if not ok:
        return _record(tool, step, "failed", urls=urls, query=query,
                       hits=hits if isinstance(hits, int) else None,
                       error=text.strip() or None, recorded_at=ts)
    if isinstance(hits, int):
        if hits == 0:
            return _record(tool, step, "no_hits", urls=[], query=query,
                           hits=0, recorded_at=ts)
        return _record(tool, step, "ok", urls=urls, query=query,
                       hits=hits, recorded_at=ts)
    if "(no results)" in str(text or ""):
        return _record(tool, step, "no_hits", urls=[], query=query,
                       hits=0, recorded_at=ts)
    return _record(tool, step, "ok", urls=urls, query=query,
                   hits=hits if isinstance(hits, int) else None,
                   recorded_at=ts)


def derive_from_transcript(transcript, recorded_at=None):
    """Build ledger records from an existing transcript list.

    Reads only ``transcript`` (tool/args/result); ``observations``
    are never consulted, so compression cannot affect the output.
    """
    records = []
    for entry in transcript or []:
        record = entry_to_record(entry, recorded_at=recorded_at)
        if record is not None:
            records.append(record)
    return records

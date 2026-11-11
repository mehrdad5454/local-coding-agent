"""Minimal Citation Gate (Phase 3, read-only analysis).

Deterministic, dependency-free check comparing URLs cited in a final
Web Research answer against Evidence Ledger records from the same run.

The gate NEVER verifies factual truth. It only answers traceability:

* source found in search results  (search record, status ok)
* page content actually retrieved (fetch record, status ok)
* claim actually true              (NOT checkable here)

Policy is caveat-only: ``pass`` keeps text byte-identical; ``caveat``
appends at most two evidence-limitation lines. No block, no rewrite,
no deletion. Never raises: bad input or internal error yields a safe
``pass`` with an empty list.
"""

from __future__ import annotations

import re

URL_RE = re.compile(r"https?://[^\s\"'<>|)\]]+", re.IGNORECASE)

_PAGE_CLAIM_RES = (
    re.compile(r"طبق\s+(صفحه|سایت|منبع)\b"),
    re.compile(r"بر\s*اساس\s+(صفحه|سایت|منبع|محتوای\s+صفحه)\b"),
    re.compile(r"در\s+صفحه[ٔه]?\s+\S+\s+(آمده|ذکر|نوشته|اعلام)"),
    re.compile(r"according\s+to\s+the\s+page\b", re.IGNORECASE),
    re.compile(r"(?:page|source)\s+states?\b", re.IGNORECASE),
)

_DETAIL_RES = (
    re.compile(r"نسخه\s*[\d۰-۹]"),
    re.compile(r"v?\d+\.\d+(?:\.\d+)?"),
    re.compile(r"\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}"),
    re.compile(r"(?:قیمت|هزینه)\s*[:：]?\s*[\d۰-۹$€تومان]"),
    re.compile(r"[«\"][^«\"]{8,}[»\"]"),
)

_NUMBER_RE = re.compile(
    r"\d{4}[/\-.]\d{1,2}(?:[/\-.]\d{1,2})?|\d+\.\d+|\d[\d٬,]*"
)

_EXEMPT_RES = (
    re.compile(r"محدودیت\s+شواهد"),
    re.compile(r"آمار\s+نهایی"),
    re.compile(r"EVIDENCE\s+LIMIT", re.IGNORECASE),
)

_EXAMPLE_RES = (
    re.compile(r"^(?:مثال|مثلاً|e\.g\.|for\s+example)\b", re.IGNORECASE),
    re.compile(r"example\.com/(?:demo|sample|example)", re.IGNORECASE),
)

_MAX_EXCERPT = 120
_MAX_CAVEAT_LINES = 2

# Search-only state: search succeeded but zero pages were retrieved.
# The verdict must come from ledger statuses only (fetch_ok flag),
# never from guessing on the answer text. "empty" (page reached but
# no readable text) counts as NOT retrieved: its content was never
# independently confirmed.
_SEARCH_ONLY_KIND = "search_only_no_fetch"

_FETCH_OK_LINE = (
    "توجه: برخی جزئیات بالا به صفحه‌ای با محتوای دریافت‌شده در همین "
    "اجرا قابل ردیابی نیست؛ پیش از اقدام، منبع را مستقیم بررسی کنید."
)
_SEARCH_ONLY_LINE = (
    "توجه: در این اجرا محتوای هیچ صفحه‌ای مستقیم دریافت نشد؛ جزئیات "
    "دقیق فقط از نتایج جست‌وجو آمده و مستقل راستی‌آزمایی نشده است."
)


def _safe_str(value):
    try:
        return "" if value is None else str(value)
    except Exception:
        return ""


def _normalize_url(url):
    text = _safe_str(url).strip()
    if not text:
        return ""
    while text.endswith("/"):
        text = text[:-1]
    text = text.rstrip(".,;:")
    return text.lower()


def _excerpt(text):
    short = re.sub(r"\s+", " ", _safe_str(text)).strip()
    if len(short) > _MAX_EXCERPT:
        return short[:_MAX_EXCERPT] + "…"
    return short


def _line_exempt(line):
    for pattern in _EXEMPT_RES:
        try:
            if pattern.search(line):
                return True
        except Exception:
            continue
    stripped = line.strip()
    for pattern in _EXAMPLE_RES:
        try:
            if pattern.search(stripped):
                return True
        except Exception:
            continue
    return False


def _records_index(records):
    ok_urls = set()
    bad_urls = {}
    search_ok = False
    fetch_ok = False
    try:
        items = list(records or [])
    except Exception:
        items = []
    for record in items:
        if not isinstance(record, dict):
            continue
        try:
            tool = _safe_str(record.get("tool"))
            status = _safe_str(record.get("status"))
            if tool == "web_search" and status == "ok":
                search_ok = True
            if tool == "fetch_page" and status == "ok":
                fetch_ok = True
            urls = record.get("urls")
            if not isinstance(urls, list):
                single = record.get("url")
                urls = [single] if single else []
            for url in urls:
                norm = _normalize_url(url)
                if not norm:
                    continue
                if status == "ok":
                    ok_urls.add(norm)
                elif status in ("failed", "empty", "no_hits"):
                    bad_urls.setdefault(norm, status)
        except Exception:
            continue
    return ok_urls, bad_urls, search_ok, fetch_ok


def _empty_stats(text):
    return {
        "urls_total": len(URL_RE.findall(text)),
        "urls_unresolved": 0,
        "numbers_total": len(_NUMBER_RE.findall(text)),
        "page_claims_total": 0,
        "fetch_ok": False,
        "search_ok": False,
    }


def evaluate(report_text, records, user_request=""):
    try:
        return _evaluate(report_text, records, user_request)
    except Exception:
        return {
            "mode": "pass",
            "unsupported": [],
            "caveat_lines": [],
            "stats": _empty_stats(_safe_str(report_text)),
        }


def _evaluate(report_text, records, user_request=""):
    text = _safe_str(report_text)
    if not isinstance(records, list) or not records:
        return {
            "mode": "pass",
            "unsupported": [],
            "caveat_lines": [],
            "stats": _empty_stats(text),
        }

    ok_urls, bad_urls, search_ok, fetch_ok = _records_index(records)
    if not ok_urls and not bad_urls and not search_ok and not fetch_ok:
        return {
            "mode": "pass",
            "unsupported": [],
            "caveat_lines": [],
            "stats": _empty_stats(text),
        }

    user_urls = set()
    try:
        for url in URL_RE.findall(_safe_str(user_request)):
            norm = _normalize_url(url)
            if norm:
                user_urls.add(norm)
    except Exception:
        user_urls = set()

    unsupported = []
    seen_unresolved = set()
    urls_total = 0

    for line in text.splitlines():
        if _line_exempt(line):
            continue
        try:
            found = URL_RE.findall(line)
        except Exception:
            found = []
        for url in found:
            norm = _normalize_url(url)
            if not norm:
                continue
            urls_total += 1
            if norm in user_urls:
                continue
            if norm in ok_urls:
                continue
            if norm in seen_unresolved:
                continue
            seen_unresolved.add(norm)
            status = bad_urls.get(norm)
            if status in ("failed", "empty"):
                kind = "page_claim"
                reason = (
                    "در Ledger همین اجرا قابل‌ردیابی نیست؛ دریافت صفحه "
                    "ناموفق بود (وضعیت: %s)." % status
                )
            else:
                kind = "url"
                reason = (
                    "در Ledger همین اجرا قابل‌ردیابی نیست؛ جعلی یا "
                    "نادرست بودن آن احراز نشده است."
                )
            unsupported.append({
                "kind": kind,
                "excerpt": _excerpt(url),
                "reason": reason,
            })

    page_claims_total = 0
    for pattern in _PAGE_CLAIM_RES:
        try:
            matches = list(pattern.finditer(text))
        except Exception:
            matches = []
        for match in matches:
            start = max(0, match.start() - 200)
            window = text[start:match.end() + 200]
            if _line_exempt(window):
                continue
            try:
                window_urls = [
                    _normalize_url(u) for u in URL_RE.findall(window)
                ]
            except Exception:
                window_urls = []
            window_urls = [u for u in window_urls if u and u not in user_urls]
            if not window_urls:
                continue
            tied = [u for u in window_urls if u in bad_urls]
            tied = [u for u in tied if u not in ok_urls]
            if not tied:
                continue
            page_claims_total += 1
            unsupported.append({
                "kind": "page_claim",
                "excerpt": _excerpt(window),
                "reason": (
                    "ارجاع صریح به صفحه‌ای است که دریافت آن در همین اجرا "
                    "ناموفق بود؛ نادرستی ادعا احراز نشده است."
                ),
            })
            break

    numbers_total = len(_NUMBER_RE.findall(text))

    caveat_lines = []
    if unsupported:
        caveat_lines.append(_FETCH_OK_LINE)
    elif search_ok and not fetch_ok:
        # Rule D (search-only): derived SOLELY from ledger state --
        # at least one web_search ok, zero fetch_page ok. No regex on
        # the answer text gates this; numbers/URLs in the text do not
        # decide it. "empty" fetches do not count as retrieved.
        unsupported.append({
            "kind": _SEARCH_ONLY_KIND,
            "excerpt": _excerpt(text[:200]),
            "reason": (
                "در این اجرا جست‌وجو موفق بود ولی محتوای هیچ صفحه‌ای "
                "مستقلاً دریافت نشد؛ اطلاعات زیر صرفاً از نتایج جست‌وجو "
                "یا دانش عمومی آمده و محتوای هیچ صفحه‌ای تأیید نشده است. "
                "نادرست بودن اعداد احراز نشده است."
            ),
        })
        caveat_lines.append(_SEARCH_ONLY_LINE)

    caveat_lines = list(caveat_lines[:_MAX_CAVEAT_LINES])
    if unsupported and caveat_lines:
        return {
            "mode": "caveat",
            "unsupported": unsupported,
            "caveat_lines": caveat_lines,
            "stats": {
                "urls_total": urls_total,
                "urls_unresolved": len(seen_unresolved),
                "numbers_total": numbers_total,
                "page_claims_total": page_claims_total,
                "fetch_ok": bool(fetch_ok),
                "search_ok": bool(search_ok),
            },
        }
    return {
        "mode": "pass",
        "unsupported": [],
        "caveat_lines": [],
        "stats": {
            "urls_total": urls_total,
            "urls_unresolved": len(seen_unresolved),
            "numbers_total": numbers_total,
            "page_claims_total": page_claims_total,
            "fetch_ok": bool(fetch_ok),
            "search_ok": bool(search_ok),
        },
    }


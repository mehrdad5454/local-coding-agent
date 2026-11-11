"""
Regression tests: درخواست‌های Read-only که فقط در بافت نهی/منع
از حذف یا ساخت حرف می‌زنند («do not delete»، «حذف نکن») نباید
وارد handle_delete_intent / handle_create_intent شوند.

تمرکز فقط روی همین False Positive است؛ هیچ عملیات Delete/Create
واقعی در این تست‌ها انجام نمی‌شود (allow_write=False).

Run from project root:
    .venv\\Scripts\\python.exe tests\\test_delete_intent_negation.py
"""

import os
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import agent

results = []


def check(name, condition, detail=""):
    ok = bool(condition)
    results.append((name, ok))
    print("[{}] {}{}".format(
        "PASS" if ok else "FAIL",
        name,
        ("  <- " + detail) if (detail and not ok) else "",
    ))
    return ok


def section(title):
    print("\n--- {}".format(title))


def cleanup(root):
    import shutil
    shutil.rmtree(root, ignore_errors=True)


# متن دقیق درخواست حادثه (chat-turn 148، پروژه agent-test).
EN_READ_ONLY_REQUEST = (
    "Inspect index.html and style.css in the agent-test project. Report:\r\n"
    "(a) the h1 color value,\r\n"
    "(b) whether the body margin is set,\r\n"
    "(c) the exact file and line number supporting each answer.\r\n"
    "\r\n"
    "Read-only task: do not modify, create, or delete any files."
)

# متن دقیق درخواست فارسی حادثه (chat-turn 149).
FA_READ_ONLY_REQUEST = (
    "فایل‌های index.html و style.css را بخوان و بررسی کن. "
    "مقدار رنگ h1 و مقدار margin مربوط به body را پیدا کن و "
    "برای هر مورد مسیر فایل و شماره خط را گزارش بده.\r\n"
    "\r\n"
    "این درخواست کاملاً Read-only است. هیچ فایل یا پوشه‌ای را "
    "حذف، ایجاد یا ویرایش نکن. هیچ عملیات Execute یا Delete "
    "انجام نده."
)


def make_project():
    root = tempfile.mkdtemp(prefix="delete_negation_test_")

    with open(os.path.join(root, "index.html"), "w",
              encoding="utf-8") as f:
        f.write("<h1>hello</h1>\n")

    with open(os.path.join(root, "style.css"), "w",
              encoding="utf-8") as f:
        f.write("h1 { color: #123456; }\n")

    return root



# ---------------------------------------------------------
# 1) Unit: has_unnegated_keyword
# ---------------------------------------------------------
def test_has_unnegated_keyword():
    section("1. has_unnegated_keyword (negated vs genuine)")

    check(
        "EN prohibitive context is negated",
        agent.has_unnegated_keyword(
            "read-only task: do not modify, create, or "
            "delete any files.",
            agent.DELETE_KEYWORDS,
        ) is False,
    )

    check(
        "FA prohibitive context (حذف ... نکن) is negated",
        agent.has_unnegated_keyword(
            "هیچ فایل یا پوشه‌ای را حذف، ایجاد یا ویرایش نکن.",
            agent.DELETE_KEYWORDS,
        ) is False,
    )

    check(
        "FA prohibitive context (انجام نده) is negated",
        agent.has_unnegated_keyword(
            "هیچ عملیات Execute یا Delete انجام نده.",
            agent.DELETE_KEYWORDS,
        ) is False,
    )

    check(
        "genuine EN delete is not negated",
        agent.has_unnegated_keyword(
            "delete index.html",
            agent.DELETE_KEYWORDS,
        ) is True,
    )

    check(
        "genuine FA delete (حذف کن) is not negated",
        agent.has_unnegated_keyword(
            "index.html را حذف کن",
            agent.DELETE_KEYWORDS,
        ) is True,
    )

    check(
        "genuine remove is not negated",
        agent.has_unnegated_keyword(
            "please remove style.css",
            agent.DELETE_KEYWORDS,
        ) is True,
    )

    check(
        "negation in another sentence does not suppress "
        "a genuine delete",
        agent.has_unnegated_keyword(
            "delete index.html. do not remove style.css.",
            agent.DELETE_KEYWORDS,
        ) is True,
    )

    check(
        "do not delete index.html is negated",
        agent.has_unnegated_keyword(
            "do not delete index.html",
            agent.DELETE_KEYWORDS,
        ) is False,
    )



# ---------------------------------------------------------
# 2) Router: درخواست‌های Read-only حادثه وارد Delete/Create نمی‌شوند
# ---------------------------------------------------------
def test_incident_requests_not_routed_to_execute():
    section("2. incident read-only requests skip Delete/Create")

    saved_model = agent.call_model

    def stub_model(*args, **kwargs):
        return "STUBBED-ANALYSIS"

    agent.call_model = stub_model

    try:
        root = make_project()

        try:
            handled, result = agent.run_generic_task(
                EN_READ_ONLY_REQUEST,
                root,
                "negation-test",
                allow_write=False,
            )

            check(
                "EN incident request is handled",
                handled is True,
            )

            check(
                "EN incident never shows 'Execute (Delete)'",
                "Execute (Delete)" not in str(result),
                ascii(str(result)[:200]),
            )

            check(
                "EN incident never shows 'Execute (Create)'",
                "Execute (Create)" not in str(result),
                ascii(str(result)[:200]),
            )

            check(
                "EN incident reaches the read path",
                "Understand (Multi-file Read)" in str(result),
                ascii(str(result)[:200]),
            )

            check(
                "EN incident left both files intact",
                os.path.isfile(os.path.join(root, "index.html"))
                and os.path.isfile(
                    os.path.join(root, "style.css")
                ),
            )
        finally:
            cleanup(root)

        root = make_project()

        try:
            handled, result = agent.run_generic_task(
                FA_READ_ONLY_REQUEST,
                root,
                "negation-test",
                allow_write=False,
            )

            check(
                "FA incident request is handled",
                handled is True,
            )

            check(
                "FA incident never shows 'Execute (Delete)'",
                "Execute (Delete)" not in str(result),
                ascii(str(result)[:200]),
            )

            check(
                "FA incident never shows 'Execute (Create)'",
                "Execute (Create)" not in str(result),
                ascii(str(result)[:200]),
            )

            check(
                "FA incident produces a report",
                str(result).startswith("مرحله:"),
                ascii(str(result)[:200]),
            )

            check(
                "FA incident left both files intact",
                os.path.isfile(os.path.join(root, "index.html"))
                and os.path.isfile(
                    os.path.join(root, "style.css")
                ),
            )
        finally:
            cleanup(root)
    finally:
        agent.call_model = saved_model



# ---------------------------------------------------------
# 3) درخواست‌های واقعی حذف/ساخت همچنان به Handler می‌رسند
#    (allow_write=False → گیت فقط-بررسی بدون حذف واقعی)
# ---------------------------------------------------------
def test_genuine_requests_still_routed():
    section("3. genuine delete/create requests still routed")

    root = make_project()

    try:
        handled, result = agent.run_generic_task(
            "delete index.html",
            root,
            "negation-test",
            allow_write=False,
        )

        check(
            "genuine EN delete reaches the Delete handler",
            handled is True
            and "Execute (Delete)" in str(result),
            ascii(str(result)[:200]),
        )

        check(
            "genuine EN delete blocked by read-only gate",
            "فقط بررسی" in str(result),
            ascii(str(result)[:200]),
        )

        check(
            "genuine EN delete left the file intact",
            os.path.isfile(os.path.join(root, "index.html")),
        )

        handled, result = agent.run_generic_task(
            "index.html را حذف کن",
            root,
            "negation-test",
            allow_write=False,
        )

        check(
            "genuine FA delete reaches the Delete handler",
            handled is True
            and "Execute (Delete)" in str(result),
            ascii(str(result)[:200]),
        )

        check(
            "genuine FA delete left the file intact",
            os.path.isfile(os.path.join(root, "index.html")),
        )

        handled, result = agent.run_generic_task(
            "create a new file called demo.txt",
            root,
            "negation-test",
            allow_write=False,
        )

        check(
            "genuine EN create reaches the Create handler",
            handled is True
            and "Execute (Create)" in str(result),
            ascii(str(result)[:200]),
        )

        check(
            "genuine EN create blocked (no file created)",
            not os.path.exists(os.path.join(root, "demo.txt")),
        )
    finally:
        cleanup(root)


# ---------------------------------------------------------
# 4) گیت‌های مجوز/فقط-بررسی دست‌نخورده‌اند
# ---------------------------------------------------------
def test_safety_gates_unchanged():
    section("4. write-approval / read-only gates unchanged")

    check(
        "is_write_allowed(True) is True",
        agent.is_write_allowed(True) is True,
    )

    check(
        "is_write_allowed(False) is False",
        agent.is_write_allowed(False) is False,
    )

    check(
        "is_write_allowed(None) is False (fail-closed)",
        agent.is_write_allowed(None) is False,
    )

    check(
        "READ_ONLY_HINT still defined",
        "فقط بررسی" in agent.READ_ONLY_HINT,
    )


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    test_has_unnegated_keyword()
    test_incident_requests_not_routed_to_execute()
    test_genuine_requests_still_routed()
    test_safety_gates_unchanged()

    passed = sum(1 for _, ok in results if ok)
    print("\nRESULT: {}/{} passed".format(passed, len(results)))

    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

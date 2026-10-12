"""
تست مستقل دروازه تکمیل (فاز ۱) — agents/completion_gate.py

الگوی تست پروژه را دنبال می‌کند: توابع test_*، تابع کمکی check(...),
خروجی PASS/FAIL و exit code در صورت شکست — فقط با کتابخانه استاندارد
Python (بدون pytest).

این تست:
  * فقط ماژول agents/completion_gate.py را می‌سنجد;
  * agent.py را import یا تغییر نمی‌دهد;
  * هیچ وابستگی به پروژه WordPress نمونه یا مسیری خارج از این ریپو
    ندارد و هیچ فایل/فرمان خارجی را اجرا نمی‌کند.

اجرا از ریشه پروژه:

    .venv\\Scripts\\python.exe tests\\test_completion_gate.py
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from agents.completion_gate import (
    STATUS_COMPLETE,
    STATUS_INCOMPLETE,
    STATUS_BLOCKED,
    STATUS_UNVERIFIABLE,
    VALID_STATUSES,
    STATE_VERIFIED,
    STATE_BLOCKED,
    STATE_FAILED,
    STATE_INCONCLUSIVE,
    STATE_UNEVIDENCED,
    evaluate_completion,
    is_complete,
)

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


def safe(text):
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def section(title):
    print("\n=== " + title + " ===")


def req(req_id, text=""):
    item = {"id": req_id}
    if text:
        item["text"] = text
    return item


def ev(req_id, outcome):
    return {"requirement": req_id, "outcome": outcome}


def check_result_shape(name, result):
    check(
        name + ": result has the documented keys",
        set(result) == {"status", "requirement_states", "errors",
                        "final_claim", "reason"},
        safe(sorted(result)),
    )
    check(
        name + ": status is one of the four documented values",
        result["status"] in VALID_STATUSES,
        safe(result["status"]),
    )


# ---------------------------------------------------------
# 1) همه الزامات شواهد موفق دارند.
# ---------------------------------------------------------
def test_all_requirements_have_successful_evidence():
    section("1. All requirements have successful evidence")

    result = evaluate_completion(
        requirements=[req("install", "install deps"), req("test", "run tests")],
        evidence=[ev("install", "success"), ev("test", "success")],
    )
    check_result_shape("all-success", result)
    check(
        "all-success: verdict is complete",
        result["status"] == STATUS_COMPLETE,
        safe(result),
    )
    check("all-success: is_complete is True", is_complete(result))
    check(
        "all-success: every requirement is verified",
        result["requirement_states"] == {
            "install": STATE_VERIFIED,
            "test": STATE_VERIFIED,
        },
        safe(result["requirement_states"]),
    )
    check("all-success: no errors", result["errors"] == [], safe(result["errors"]))

    # ادعای FINAL هم اگر شواهد واقعی موجود باشند خللی ندارد؛
    # تکمیل از شواهد می‌آید، نه از ادعا.
    claimed = evaluate_completion(
        requirements=[req("a"), req("b")],
        evidence=[ev("a", "success"), ev("b", "success")],
        final_claim=True,
    )
    check(
        "all-success + FINAL claim: still complete",
        claimed["status"] == STATUS_COMPLETE and claimed["final_claim"] is True,
        safe(claimed),
    )


# ---------------------------------------------------------
# 2) یک الزام انجام‌نشده باقی مانده است.
# ---------------------------------------------------------
def test_one_requirement_left_unfinished():
    section("2. One requirement left unfinished")

    failed = evaluate_completion(
        requirements=[req("build", "build project"), req("deploy", "deploy")],
        evidence=[ev("build", "success"), ev("deploy", "failure")],
    )
    check_result_shape("failure-case", failed)
    check(
        "failure-case: verdict is incomplete",
        failed["status"] == STATUS_INCOMPLETE,
        safe(failed),
    )
    check("failure-case: is_complete is False", not is_complete(failed))
    check(
        "failure-case: failed requirement is marked failed",
        failed["requirement_states"]["deploy"] == STATE_FAILED,
        safe(failed["requirement_states"]),
    )
    check(
        "failure-case: successful requirement stays verified",
        failed["requirement_states"]["build"] == STATE_VERIFIED,
        safe(failed["requirement_states"]),
    )

    unevidenced = evaluate_completion(
        requirements=[req("build"), req("deploy")],
        evidence=[ev("build", "success")],
    )
    check(
        "no-evidence-case: verdict is incomplete",
        unevidenced["status"] == STATUS_INCOMPLETE,
        safe(unevidenced),
    )
    check(
        "no-evidence-case: missing requirement is unevidenced",
        unevidenced["requirement_states"] == {
            "build": STATE_VERIFIED,
            "deploy": STATE_UNEVIDENCED,
        },
        safe(unevidenced["requirement_states"]),
    )


# ---------------------------------------------------------
# 3) یک الزام به دلیل منع دسترسی مسدود شده است.
# ---------------------------------------------------------
def test_blocked_requirement():
    section("3. One requirement is blocked")

    result = evaluate_completion(
        requirements=[req("read", "read file"), req("write", "write file")],
        evidence=[ev("read", "success"), ev("write", "blocked")],
    )
    check_result_shape("blocked-case", result)
    check(
        "blocked-case: verdict is blocked",
        result["status"] == STATUS_BLOCKED,
        safe(result),
    )
    check("blocked-case: is_complete is False", not is_complete(result))
    check(
        "blocked-case: blocked requirement is marked blocked",
        result["requirement_states"]["write"] == STATE_BLOCKED,
        safe(result["requirement_states"]),
    )

    # حتی اگر الزام دیگری هنوز «انجام‌نشده» باشد، منع قطعی ادامه
    # کار را می‌بندد و وضعیت کلی blocked است.
    mixed = evaluate_completion(
        requirements=[req("write", "write file"), req("verify", "verify output")],
        evidence=[ev("write", "blocked")],
    )
    check(
        "blocked beats unevidenced for the overall verdict",
        mixed["status"] == STATUS_BLOCKED,
        safe(mixed),
    )


# ---------------------------------------------------------
# 4) شواهد برای تأیید نتیجه کافی نیستند.
# ---------------------------------------------------------
def test_insufficient_evidence_is_unverifiable():
    section("4. Evidence is insufficient to verify the result")

    only_inconclusive = evaluate_completion(
        requirements=[req("migrate", "migrate database")],
        evidence=[ev("migrate", "inconclusive")],
    )
    check_result_shape("inconclusive-case", only_inconclusive)
    check(
        "inconclusive-case: verdict is unverifiable",
        only_inconclusive["status"] == STATUS_UNVERIFIABLE,
        safe(only_inconclusive),
    )
    check(
        "inconclusive-case: is_complete is False",
        not is_complete(only_inconclusive),
    )
    check(
        "inconclusive-case: requirement is marked inconclusive",
        only_inconclusive["requirement_states"] == {
            "migrate": STATE_INCONCLUSIVE,
        },
        safe(only_inconclusive["requirement_states"]),
    )

    # یک شاهد موفق برای بخشی از کار، وقتی بخش دیگر مبهم است،
    # تکمیل کل درخواست را قابل‌تأیید نمی‌کند.
    partial = evaluate_completion(
        requirements=[req("export", "export data"), req("import", "import data")],
        evidence=[ev("export", "success"), ev("import", "inconclusive")],
    )
    check(
        "partial success + inconclusive: verdict is unverifiable",
        partial["status"] == STATUS_UNVERIFIABLE,
        safe(partial),
    )


# ---------------------------------------------------------
# 5) درخواست چندبخشی فقط یک شاهد موفق دارد.
# ---------------------------------------------------------
def test_multi_part_request_with_single_success():
    section("5. Multi-part request with a single success evidence")

    result = evaluate_completion(
        requirements=[
            req("lint", "run linter"),
            req("test", "run tests"),
            req("docs", "update docs"),
        ],
        evidence=[ev("lint", "success")],
    )
    check_result_shape("one-of-three", result)
    check(
        "one-of-three: single success never completes the request",
        result["status"] == STATUS_INCOMPLETE and not is_complete(result),
        safe(result),
    )
    check(
        "one-of-three: only the evidenced requirement is verified",
        result["requirement_states"] == {
            "lint": STATE_VERIFIED,
            "test": STATE_UNEVIDENCED,
            "docs": STATE_UNEVIDENCED,
        },
        safe(result["requirement_states"]),
    )


# ---------------------------------------------------------
# 6) ادعای FINAL بدون شواهد، تکمیل محسوب نمی‌شود.
# ---------------------------------------------------------
def test_final_claim_alone_is_not_completion():
    section("6. A FINAL claim alone is not completion")

    claim_only = evaluate_completion(
        requirements=[req("refactor", "refactor module")],
        evidence=None,
        final_claim=True,
    )
    check_result_shape("claim-only", claim_only)
    check(
        "claim-only: final_claim is recorded",
        claim_only["final_claim"] is True,
        safe(claim_only["final_claim"]),
    )
    check(
        "claim-only: verdict is NOT complete",
        claim_only["status"] != STATUS_COMPLETE and not is_complete(claim_only),
        safe(claim_only),
    )

    claim_after_failure = evaluate_completion(
        requirements=[req("fix", "fix bug")],
        evidence=[ev("fix", "failure")],
        final_claim=True,
    )
    check(
        "claim-after-failure: failed operation never becomes success",
        claim_after_failure["status"] != STATUS_COMPLETE
        and claim_after_failure["requirement_states"]["fix"] != STATE_VERIFIED,
        safe(claim_after_failure),
    )


# ---------------------------------------------------------
# 7) ورودی نامعتبر یا فاقد الزامات مشخص، به‌اشتباه complete نمی‌شود.
# ---------------------------------------------------------
def test_invalid_input_is_never_complete():
    section("7. Invalid input is never guessed as complete")

    cases = [
        ("requirements is None", dict(requirements=None)),
        ("requirements is a plain string", dict(requirements="just do it")),
        ("requirements list is empty", dict(requirements=[])),
        ("requirement without id", dict(requirements=[{"text": "vague"}])),
        ("duplicate requirement ids", dict(
            requirements=[req("a"), req("a")],
            evidence=[ev("a", "success")],
        )),
        ("evidence is not a list", dict(
            requirements=[req("a")], evidence="success!",
        )),
        ("evidence references unknown requirement", dict(
            requirements=[req("a")],
            evidence=[ev("ghost", "success")],
        )),
        ("evidence has invalid outcome", dict(
            requirements=[req("a")],
            evidence=[ev("a", "maybe")],
        )),
        ("final_claim is not a bool", dict(
            requirements=[req("a")],
            evidence=[ev("a", "success")],
            final_claim="yes",
        )),
    ]

    for name, kwargs in cases:
        result = evaluate_completion(**kwargs)
        check(
            name + ": verdict is unverifiable",
            result["status"] == STATUS_UNVERIFIABLE,
            safe(result),
        )
        check(
            name + ": is_complete stays False",
            not is_complete(result),
            safe(result["status"]),
        )
        check(
            name + ": errors explain the problem",
            len(result["errors"]) > 0,
            safe(result["errors"]),
        )


# ---------------------------------------------------------
# 8) قاعده تکمیلی: عملیات ناموفق هرگز شاهد موفقیت نیست.
# ---------------------------------------------------------
def test_unsuccessful_operations_are_never_success():
    section("8. Unsuccessful operations are never success evidence")

    result = evaluate_completion(
        requirements=[req("a")],
        evidence=[ev("a", "failure")],
    )
    check(
        "failure-only: requirement is not verified",
        result["requirement_states"]["a"] == STATE_FAILED
        and result["status"] != STATUS_COMPLETE,
        safe(result),
    )

    # موفقیت بر یک الزام مقدم است، ولی فقط برای همان الزام.
    mixed = evaluate_completion(
        requirements=[req("a"), req("b")],
        evidence=[ev("a", "success"), ev("a", "failure"), ev("b", "failure")],
    )
    check(
        "mixed evidence: success beats failure on the same requirement",
        mixed["requirement_states"]["a"] == STATE_VERIFIED,
        safe(mixed["requirement_states"]),
    )
    check(
        "mixed evidence: overall verdict still incomplete",
        mixed["status"] == STATUS_INCOMPLETE,
        safe(mixed),
    )


# ---------------------------------------------------------
# 9) استقلال ماژول: بدون agent.py و بدون Flask.
# ---------------------------------------------------------
def test_module_is_independent():
    section("9. Module is independent (no agent.py, no Flask)")

    check(
        "agents.completion_gate is imported",
        "agents.completion_gate" in sys.modules,
    )
    check(
        "agent.py is not imported by the gate",
        "agent" not in sys.modules,
        safe("agent" in sys.modules),
    )
    check(
        "flask is not imported by the gate",
        "flask" not in sys.modules,
        safe("flask" in sys.modules),
    )


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    test_all_requirements_have_successful_evidence()
    test_one_requirement_left_unfinished()
    test_blocked_requirement()
    test_insufficient_evidence_is_unverifiable()
    test_multi_part_request_with_single_success()
    test_final_claim_alone_is_not_completion()
    test_invalid_input_is_never_complete()
    test_unsuccessful_operations_are_never_success()
    test_module_is_independent()

    passed = sum(1 for _, ok in results if ok)
    print("\nRESULT: {}/{} passed".format(passed, len(results)))

    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
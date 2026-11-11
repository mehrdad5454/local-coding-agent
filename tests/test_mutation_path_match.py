"""
تست متمرcedented تطبیق مسیرها در requested_mutation_status.

هدف: تأیید کنConditionِ path در requested_mutation_status مسیرهای
معادل (جداکننده متفاوت، ./، ...) را می‌شناسد، فایل‌های هم‌نام در
پوشه‌های مختلف را اشتباه تشخیص نمی‌دهد، و فقط عملیاتِ موفق با
verified=True را verified علامت‌گذاری می‌کند.

اجرا از ریشه پروژه:
    python tests\\test_mutation_path_match.py

فقط کتابخانه استاندارد Python (بدون pytest).
"""

import os
import sys
import tempfile
import shutil

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from agent import (
    find_requested_files,
    requested_mutation_status,
    normalize_relative_target,
    mutation_targets_of_entry,
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


def section(title):
    print("\n=== " + title + " ===")


# ---------------------------------------------------------
#Setup: موقت دایرکتوری با فایل‌های کمکی
# ---------------------------------------------------------
tmpdir = tempfile.mkdtemp(prefix="mutation_path_match_")
themes_dir = os.path.join(tmpdir, "themes")
os.makedirs(themes_dir, exist_ok=True)

# فایل در themes/
with open(os.path.join(themes_dir, "style.css"), "w", encoding="utf-8") as f:
    f.write("body { color: red; }")

# فایل هم‌نام در ریشه (برای تست عدم تطبیق اشتباه)
with open(os.path.join(tmpdir, "style.css"), "w", encoding="utf-8") as f:
    f.write("body { color: blue; }")


def cleanup():
    shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------
# 1) normalize_relative_target: جداکننده و ./ باید یکسان شوند
# ---------------------------------------------------------
section("1. normalize_relative_target")
check(
    "backslash to slash",
    normalize_relative_target(r"themes\style.css") == "themes/style.css",
)
check(
    "leading ./ stripped",
    normalize_relative_target("./themes/style.css") == "themes/style.css",
)
check(
    "lowercase",
    normalize_relative_target("THEMES/STYLE.CSS") == "themes/style.css",
)
check(
    "trailing slash stripped",
    normalize_relative_target("themes/style.css/") == "themes/style.css",
)
check(
    "empty string",
    normalize_relative_target("") == "",
    normalize_relative_target(""),
)

# ---------------------------------------------------------
# 2) mutation_targets_of_entry: مسیر از args استخراج می‌شود
# ---------------------------------------------------------
section("2. mutation_targets_of_entry")
entry_write = {
    "mutating": True,
    "tool": "write_file",
    "args": {"path": "themes/style.css"},
    "verified": True,
    "result": {"ok": True},
}
targets = mutation_targets_of_entry(entry_write)
check(
    "single path from args",
    targets == ["themes/style.css"],
    str(targets),
)

entry_move = {
    "mutating": True,
    "tool": "move_entry",
    "args": {"source": "old/style.css", "dest": "new/style.css"},
    "verified": True,
    "result": {"ok": True},
}
targets_move = mutation_targets_of_entry(entry_move)
check(
    "move_entry source path",
    "old/style.css" in targets_move,
    str(targets_move),
)

entry_nonmutating = {
    "mutating": False,
    "tool": "read_file",
    "args": {"path": "themes/style.css"},
    "verified": True,
    "result": {"ok": True},
}
targets_non = mutation_targets_of_entry(entry_nonmutating)
check(
    "non-mutating still extracts path (caller filters mutating)",
    "themes/style.css" in targets_non,
    str(targets_non),
)

# ---------------------------------------------------------
# 3) find_requested_files: relative_path از مسیر واقعی disk
# ---------------------------------------------------------
section("3. find_requested_files")

# کاربر "./themes/style.css" می‌نویسد — بایدrelative_path = "themes/style.css" باشد
files_dot = find_requested_files(tmpdir, "Edit ./themes/style.css")
paths_dot = [f["relative_path"] for f in files_dot]
check(
    "./themes/style.css resolves to themes/style.css",
    "themes/style.css" in paths_dot,
    str(paths_dot),
)

# کاربر "style.css" می‌نویسد — فایل در ریشه پیدا می‌شود
files_root = find_requested_files(tmpdir, "Edit style.css")
paths_root = [f["relative_path"] for f in files_root]
check(
    "root style.css found",
    "style.css" in paths_root,
    str(paths_root),
)

# کاربر "themes/style.css" می‌نویسد — فایل در themes پیدا می‌شود
files_sub = find_requested_files(tmpdir, "Edit themes/style.css")
paths_sub = [f["relative_path"] for f in files_sub]
check(
    "themes/style.css found in themes/",
    "themes/style.css" in paths_sub,
    str(paths_sub),
)

# ---------------------------------------------------------
# 4) requested_mutation_status: با verified=True روی همان مسیر
# ---------------------------------------------------------
section("4. requested_mutation_status — verified operation")

# کاربر themes/style.css می‌خواهد و ابزار روی همان مسیر-write موفق است
transcript_verified = [
    {
        "mutating": True,
        "tool": "write_file",
        "args": {"path": "themes/style.css"},
        "verified": True,
        "result": {"ok": True},
    }
]
status = requested_mutation_status(
    tmpdir, "Edit themes/style.css", transcript_verified
)
status_map = {item["path"]: item["status"] for item in status}
check(
    "themes/style.css is verified",
    status_map.get("themes/style.css") == "verified",
    str(status_map),
)

# کاربر style.css (ریشه) می‌خواهد و ابزار روی همان مسیر موفق است
transcript_root_verified = [
    {
        "mutating": True,
        "tool": "write_file",
        "args": {"path": "style.css"},
        "verified": True,
        "result": {"ok": True},
    }
]
status_root = requested_mutation_status(
    tmpdir, "Edit style.css", transcript_root_verified
)
status_root_map = {item["path"]: item["status"] for item in status_root}
check(
    "root style.css is verified",
    status_root_map.get("style.css") == "verified",
    str(status_root_map),
)

# ---------------------------------------------------------
# 5) هم‌نام در دو پوشه — اشتباه تطبیق داده نشود
# ---------------------------------------------------------
section("5. Same-named files in different folders — no false match")

# کاربر هر دو فایل را mentions می‌کند؛ ابزار فقط themes/style.css را writes
# فایل style.css در ریشه نباید verified شود (پوشه khác)
transcript_sub_only = [
    {
        "mutating": True,
        "tool": "write_file",
        "args": {"path": "themes/style.css"},
        "verified": True,
        "result": {"ok": True},
    }
]
status_sub = requested_mutation_status(
    tmpdir,
    "Edit themes/style.css and style.css",
    transcript_sub_only,
)
status_sub_map = {item["path"]: item["status"] for item in status_sub}
check(
    "root style.css stays not changed (different folder)",
    status_sub_map.get("style.css") == "not changed",
    str(status_sub_map),
)
check(
    "themes/style.css is verified",
    status_sub_map.get("themes/style.css") == "verified",
    str(status_sub_map),
)

# ---------------------------------------------------------
# 6) عملیات ناموفق یا فاقد Verify — verified نباشد
# ---------------------------------------------------------
section("6. Failed / unverified operations")

# verified False
transcript_failed = [
    {
        "mutating": True,
        "tool": "write_file",
        "args": {"path": "themes/style.css"},
        "verified": False,
        "result": {"ok": False},
    }
]
status_failed = requested_mutation_status(
    tmpdir, "Edit themes/style.css", transcript_failed
)
status_failed_map = {item["path"]: item["status"] for item in status_failed}
check(
    "verified=False -> failed",
    status_failed_map.get("themes/style.css") == "failed",
    str(status_failed_map),
)

# result.ok False (بدون verified)
transcript_ok_false = [
    {
        "mutating": True,
        "tool": "write_file",
        "args": {"path": "themes/style.css"},
        "result": {"ok": False},
    }
]
status_ok_false = requested_mutation_status(
    tmpdir, "Edit themes/style.css", transcript_ok_false
)
status_ok_false_map = {item["path"]: item["status"] for item in status_ok_false}
check(
    "result.ok=False -> failed",
    status_ok_false_map.get("themes/style.css") == "failed",
    str(status_ok_false_map),
)

# بدون Verify (verified 없 است و result.ok هم نیست)
transcript_no_verify = [
    {
        "mutating": True,
        "tool": "write_file",
        "args": {"path": "themes/style.css"},
    }
]
status_no_verify = requested_mutation_status(
    tmpdir, "Edit themes/style.css", transcript_no_verify
)
status_no_verify_map = {item["path"]: item["status"] for item in status_no_verify}
check(
    "no Verify -> not changed",
    status_no_verify_map.get("themes/style.css") == "not changed",
    str(status_no_verify_map),
)

# ---------------------------------------------------------
# 7) معادل با جداکننده متفاوت یا ./
# ---------------------------------------------------------
section("7. Equivalent paths — different separators or ./")

# کاربر "./themes/style.css" می‌نویسد، ابزار "themes/style.css" می‌فرستد
transcript_sep = [
    {
        "mutating": True,
        "tool": "write_file",
        "args": {"path": "themes/style.css"},
        "verified": True,
        "result": {"ok": True},
    }
]
status_sep = requested_mutation_status(
    tmpdir, "Edit ./themes/style.css", transcript_sep
)
status_sep_map = {item["path"]: item["status"] for item in status_sep}
check(
    "./themes/style.css matches themes/style.css from args",
    status_sep_map.get("themes/style.css") == "verified",
    str(status_sep_map),
)

# ---------------------------------------------------------
# Zusammenfassung
# ---------------------------------------------------------
passed = sum(1 for _, ok in results if ok)
print("\nRESULT: {}/{} passed".format(passed, len(results)))

cleanup()

if passed != len(results):
    raise SystemExit(1)
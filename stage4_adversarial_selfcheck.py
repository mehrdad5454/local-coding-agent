"""
Stage 4 — Adversarial Stress Test (Bug Discovery)

هدف این فایل سبز شدن نیست. هدف آشکار کردن ضعف‌های واقعی
Stage 4 و لایه‌های زیربنایی (Canonical Plan، Re-validation،
Approval، Execute، Verify، Undo، Fail-Closed) است.

قواعد:
  * هیچ فایل Production برای سبز شدن این تست تغییر نمی‌کند.
  * همهٔ سناریوها روی fixture موقت اجرا و پاک می‌شوند.
  * هر تست سه حالت دارد: PASS / FAIL / ERROR / BLOCKED.
  * انتظار‌ها از قرارداد واقعی پروژه نوشته شده‌اند، نه از
    رفتاری که «نباید» داشته باشیم.

اجرا:
    python stage4_adversarial_selfcheck.py
"""

import json
import os
import shutil
import sys
import tempfile
import traceback

import agent


# ---------------------------------------------------------
# Reporting
# ---------------------------------------------------------
results = []          # (name, status, detail)
scenario_status = {}  # letter -> status


def record(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    results.append((name, status, detail))

    print("[{}] {}{}".format(
        status,
        name,
        ("  <- " + str(detail)) if (detail and not ok) else "",
    ))

    return ok


def blocked(name, reason):
    results.append((name, "BLOCKED", reason))
    print("[BLOCKED] {}  <- {}".format(name, reason))


def errored(name, exc):
    detail = type(exc).__name__ + ": " + str(exc)
    results.append((name, "ERROR", detail))
    print("[ERROR] {}  <- {}".format(name, detail))
    traceback.print_exc()


def finish(letter):
    """اولین نتیجهٔ غیر-PASS یک سناریو، وضعیت همان سناریو است."""
    prefix = letter + ":"

    for name, status, _ in results:
        if name.startswith(prefix):
            if status == "PASS":
                continue

            scenario_status[letter] = status
            return

    scenario_status[letter] = "PASS"


def safe(text):
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


# ---------------------------------------------------------
# Fixtures
# ---------------------------------------------------------
ORIGINAL = {
    "index.html": "<!DOCTYPE html>\n<html>\n<body><h1>Old</h1></body>\n"
                  "</html>\n",
    "style.css": "h1 {\n  color: black;\n}\n",
    "app.js": "console.log('old');\n",
}

PLANNED = {
    "index.html": "<!DOCTYPE html>\n<html>\n<body><h1>New</h1></body>\n"
                  "</html>\n",
    "style.css": "h1 {\n  color: blue;\n}\n",
    "app.js": "console.log('new');\n",
}

REQUEST = "index.html و style.css و app.js را با هم تغییر بده"


def read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return ""


def snapshot(root):
    state = {}

    for current, dirs, files in os.walk(root):
        for name in dirs:
            state[os.path.relpath(
                os.path.join(current, name), root
            )] = "dir"

        for name in files:
            with open(os.path.join(current, name), "rb") as fh:
                state[os.path.relpath(
                    os.path.join(current, name), root
                )] = fh.read()

    return state


class ScriptedModel:
    def __init__(self):
        self.scripts = []
        self.calls = 0

    def push(self, *responses):
        self.scripts.append(list(responses))

    def __call__(self, system_prompt, user_prompt, *a, **kw):
        index = min(self.calls, len(self.scripts) - 1)
        self.calls += 1

        responses = self.scripts[index] if self.scripts else []

        if not responses:
            return "FINAL: تمام شد."

        return responses.pop(0)


def write_tool(tool, args):
    return "TOOL: " + json.dumps(
        {"tool": tool, "args": args}, ensure_ascii=False
    )


def batch_tool(files):
    return write_tool("write_files", {"files": files})


def planned_files(names=("index.html", "style.css", "app.js")):
    return [
        {"path": name, "content": PLANNED[name]} for name in names
    ]


# پوشهٔ زیرساختی تراکنش Batch و Backupهای ایمنی که خود ابزار
# می‌سازد. این‌ها «فایل اضافه» نیستند: دقیقاً همان artifactای
# هستند که write_file/delete_entry تک‌فایلی از قبل تولید
# می‌کنند و در build_project_file_inventory هم صریحاً از فهرست
# فایل‌های پروژه کنار گذاشته می‌شوند. تشخیص با همان تابع
# Production انجام می‌شود، نه با حدس محلی.
INFRA_DIR = ".agent-multi-backup"


def is_infra(name):
    if name == INFRA_DIR or name.startswith(INFRA_DIR + "."):
        return True

    try:
        return bool(agent.is_project_backup_file(name))
    except Exception:
        return False


def payload_snapshot(root):
    """فقط فایل‌های واقعی پروژه، بدون زیرساخت تراکنش."""
    state = {}

    for current, dirs, files in os.walk(root):
        for name in list(dirs):
            if is_infra(name):
                dirs.remove(name)

        for name in files:
            if is_infra(name):
                continue

            full = os.path.join(current, name)
            with open(full, "rb") as fh:
                state[os.path.relpath(full, root)] = fh.read()

    return state


def write_fixture(base, name="project", content=None):
    root = os.path.join(base, name)
    os.makedirs(root, exist_ok=True)

    for rel, text in (content or ORIGINAL).items():
        full = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(full), exist_ok=True)

        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)

    return root


def write_registry(base, root, modes=None, name="probe"):
    state_dir = os.path.join(base, "state")
    os.makedirs(state_dir, exist_ok=True)

    document = {name: root}

    if modes:
        document[agent.PROJECTS_APPROVAL_KEY] = modes

    registry = os.path.join(state_dir, "projects.json")

    with open(registry, "w", encoding="utf-8") as fh:
        json.dump(document, fh)

    return registry, state_dir


def install(registry, state_dir, model):
    saved = {
        "call_model": agent.call_model,
        "projects_file": agent.PROJECTS_FILE,
        "pending_file": agent.PENDING_ACTION_FILE,
        "journal_file": agent.ACTION_JOURNAL_FILE,
    }

    agent.PROJECTS_FILE = registry
    agent.call_model = model
    agent.PENDING_ACTION_FILE = os.path.join(
        state_dir, "pending.json"
    )
    agent.ACTION_JOURNAL_FILE = os.path.join(
        state_dir, "journal.json"
    )
    agent.clear_pending_action()
    agent.clear_conversation_memory()

    try:
        agent.ACTION_JOURNAL[:] = []
    except Exception:
        pass

    return saved


def uninstall(saved):
    agent.call_model = saved["call_model"]
    agent.PROJECTS_FILE = saved["projects_file"]
    agent.PENDING_ACTION_FILE = saved["pending_file"]
    agent.ACTION_JOURNAL_FILE = saved["journal_file"]
    agent.clear_pending_action()
    agent.clear_conversation_memory()

    try:
        agent.ACTION_JOURNAL[:] = []
    except Exception:
        pass


def make_pending(root, project, files, tool="write_files"):
    args = {"files": files} if tool == "write_files" else files

    return {
        "tool": tool,
        "args": args,
        "project": project,
        "project_root": root,
        "user_request": REQUEST,
        "context": agent.build_pending_revalidation_context(
            tool, args, root, project, allow_write=True
        ),
        "created_at": 0,
    }


def summary_targets(summary):
    out = []

    for line in str(summary or "").splitlines():
        stripped = line.strip()

        if stripped.startswith("- ") and " | " in stripped:
            out.append(stripped[2:].split(" | ")[0].strip())

    return out


def summary_body_targets(text):
    return summary_targets(text)


# ---------------------------------------------------------
# A — Real multi-file happy path
# ---------------------------------------------------------
def scenario_a():
    base = tempfile.mkdtemp(prefix="adv_a_")
    model = ScriptedModel()
    root = write_fixture(base)
    registry, state_dir = write_registry(base, root)
    saved = install(registry, state_dir, model)

    try:
        model.scripts = [[
            batch_tool(planned_files()),
            "FINAL: تمام شد.",
        ], ["FINAL: پس از اجرا."]]
        model.calls = 0

        before = payload_snapshot(root)
        answer = str(agent.run_agent(REQUEST, "probe", allow_write=True))

        pending = agent.get_pending_action()

        record(
            "A: canonical plan has exactly 3 targets",
            bool(pending)
            and pending.get("tool") == "write_files"
            and [
                item.get("path")
                for item in pending.get("args", {}).get("files", [])
            ] == ["index.html", "style.css", "app.js"],
            safe(pending),
        )
        record(
            "A: summary shows exactly those 3 targets",
            summary_body_targets(
                answer[answer.find("خلاصه Plan چندفایلی"):]
                if "خلاصه Plan چندفایلی" in answer else ""
            ) == ["index.html", "style.css", "app.js"],
            safe(answer),
        )
        record(
            "A: no fabricated target in summary",
            not any(
                token in answer
                for token in ("index.php", "app.jsx", "style.scss")
            ),
            safe(answer),
        )
        record(
            "A: summary is shown before the approval prompt",
            "خلاصه Plan چندفایلی" in answer
            and answer.find("خلاصه Plan چندفایلی")
            < answer.find("برای اجرا پاسخ دهید: تأیید"),
            safe(answer),
        )
        record(
            "A: nothing written before approval",
            payload_snapshot(root) == before,
            safe(sorted(set(payload_snapshot(root)) ^ set(before))),
        )

        approval_answer = str(
            agent.run_agent("تأیید", "probe", allow_write=True)
        )

        record(
            "A: execute wrote exactly the approved plan",
            all(
                read(os.path.join(root, rel)) == PLANNED[rel]
                for rel in PLANNED
            ),
            safe({rel: read(os.path.join(root, rel)) for rel in PLANNED}),
        )
        record(
            "A: verify confirmed the real result",
            "Read-Back و Verify موفق بود" in approval_answer
            and "تغییر اعمال و Verify موفق" in approval_answer,
            safe(approval_answer[-700:]),
        )
    except Exception as exc:
        errored("A: unexpected harness error", exc)
    finally:
        uninstall(saved)
        shutil.rmtree(base, ignore_errors=True)

    finish("A")


# ---------------------------------------------------------
# B — Fabricated target injection
# ---------------------------------------------------------
def scenario_b():
    base = tempfile.mkdtemp(prefix="adv_b_")
    root = write_fixture(base)
    files = [
        {"path": "a.txt", "content": "A\n"},
        {"path": "b.txt", "content": "B\n"},
        {"path": "c.txt", "content": "C\n"},
    ]

    try:
        for rel, text in files:
            with open(os.path.join(root, rel), "w",
                      encoding="utf-8") as fh:
                fh.write(text)

        variants = {
            "extra target appended": [
                "a.txt", "b.txt", "c.txt", "fake.txt",
            ],
            "extra target prepended": [
                "fake.txt", "a.txt", "b.txt", "c.txt",
            ],
            "missing target": ["a.txt", "b.txt"],
            "reordered targets": ["c.txt", "a.txt", "b.txt"],
            "duplicate target": ["a.txt", "a.txt", "b.txt", "c.txt"],
            "similar but different path": [
                "a.txt", "b.txt", "c.txtx",
            ],
            "case changed path": ["a.txt", "b.txt", "C.TXT"],
            "separator changed path": [
                "a.txt", "b.txt", "./c.txt",
            ],
        }

        for label, recorded in variants.items():
            pending = make_pending(root, "probe", files)
            pending["context"]["batch_paths"] = list(recorded)

            summary = agent.build_multi_file_plan_summary(pending)

            record(
                "B: no summary for " + label,
                summary is None,
                safe(summary),
            )

        # Plan خودش شامل target تکراری باشد.
        dup_files = [
            {"path": "a.txt", "content": "A1\n"},
            {"path": "a.txt", "content": "A2\n"},
            {"path": "b.txt", "content": "B\n"},
        ]

        pending = make_pending(root, "probe", dup_files)
        summary = agent.build_multi_file_plan_summary(pending)

        if summary is None:
            record(
                "B: duplicate member inside the plan yields no summary",
                True,
            )
        else:
            listed = summary_targets(summary)
            unique = sorted(set(listed))

            record(
                "B: duplicate member inside the plan is reported "
                "honestly (3 lines == 3 unique files)",
                len(listed) == 3 and unique == ["a.txt", "b.txt"],
                "listed="
                + safe(listed)
                + " header="
                + safe(summary.splitlines()[0]),
            )
    except Exception as exc:
        errored("B: unexpected harness error", exc)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    finish("B")


# ---------------------------------------------------------
# C — Missing / mismatched context
# ---------------------------------------------------------
def scenario_c():
    base = tempfile.mkdtemp(prefix="adv_c_")
    root = write_fixture(base)
    files = planned_files()

    try:
        no_context = make_pending(root, "probe", files)
        del no_context["context"]

        record(
            "C: no summary when context is missing",
            agent.build_multi_file_plan_summary(no_context) is None,
        )

        no_batch = make_pending(root, "probe", files)
        del no_batch["context"]["batch_paths"]

        record(
            "C: no summary when batch_paths is missing",
            agent.build_multi_file_plan_summary(no_batch) is None,
        )

        empty_batch = make_pending(root, "probe", files)
        empty_batch["context"]["batch_paths"] = []

        record(
            "C: no summary when batch_paths is empty",
            agent.build_multi_file_plan_summary(empty_batch) is None,
        )

        not_a_list = make_pending(root, "probe", files)
        not_a_list["context"]["batch_paths"] = "a.txt,b.txt,c.txt"

        record(
            "C: no summary when batch_paths is not a list",
            agent.build_multi_file_plan_summary(not_a_list) is None,
        )

        string_args = {
            "tool": "write_files",
            "args": json.dumps(
                {"files": files}, ensure_ascii=False
            ),
            "project": "probe",
            "project_root": root,
            "context": {"batch_paths": ["a.txt"]},
        }

        record(
            "C: string args with non-matching record yield no summary",
            agent.build_multi_file_plan_summary(string_args) is None,
        )

        broken_files = make_pending(root, "probe", files)
        broken_files["args"] = {"files": "not-json"}

        record(
            "C: unparsable batch args yield no summary",
            agent.build_multi_file_plan_summary(broken_files) is None,
        )

        no_root = make_pending(root, "probe", files)
        no_root["project_root"] = None

        record(
            "C: missing project_root yields no summary",
            agent.build_multi_file_plan_summary(no_root) is None,
        )

        record(
            "C: non-dict pending yields no summary",
            agent.build_multi_file_plan_summary("nope") is None,
        )

        record(
            "C: None pending yields no summary",
            agent.build_multi_file_plan_summary(None) is None,
        )

        escaped = make_pending(root, "probe", files)
        escaped["context"]["batch_paths"] = [
            "../escape.txt", "style.css", "app.js",
        ]

        record(
            "C: escaping recorded path yields no summary",
            agent.build_multi_file_plan_summary(escaped) is None,
        )

        # Summary نباید حتی وقتی فقط بخشی از targetها معتبرند،
        # خروجی ناقص بدهد.
        partial = make_pending(root, "probe", files)
        partial["args"] = {"files": files[:1]}
        partial["context"]["batch_paths"] = ["index.html", "style.css"]

        record(
            "C: partially mismatched plan yields no summary",
            agent.build_multi_file_plan_summary(partial) is None,
        )
    except Exception as exc:
        errored("C: unexpected harness error", exc)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    finish("C")


# ---------------------------------------------------------
# D — File changes between Plan and Approval
# ---------------------------------------------------------
def scenario_d():
    base = tempfile.mkdtemp(prefix="adv_d_")
    model = ScriptedModel()
    root = write_fixture(base)
    registry, state_dir = write_registry(base, root)
    saved = install(registry, state_dir, model)

    try:
        model.scripts = [[
            batch_tool(planned_files()),
            "FINAL: تمام شد.",
        ], ["FINAL: پس از اجرا."]]
        model.calls = 0

        answer = str(agent.run_agent(REQUEST, "probe", allow_write=True))
        pending = agent.get_pending_action()

        record(
            "D: plan is waiting for approval",
            bool(pending),
            safe(answer),
        )
        record(
            "D: summary was shown",
            "خلاصه Plan چندفایلی" in answer,
            safe(answer),
        )

        # محتوای یکی از targetها را بین Plan و Approval عوض کن.
        target = os.path.join(root, "style.css")

        with open(target, "w", encoding="utf-8") as fh:
            fh.write("h1 {\n  color: red;\n}\n")

        snapshot_after_tamper = payload_snapshot(root)
        approve = str(agent.run_agent("تأیید", "probe", allow_write=True))

        record(
            "D: re-validation refused the stale plan",
            "Re-validation ناموفق بود" in approve
            or "اجرا نشد" in approve,
            safe(approve),
        )
        record(
            "D: tampered file was not overwritten",
            read(target) == "h1 {\n  color: red;\n}\n",
            safe(read(target)),
        )
        record(
            "D: no other file was written either",
            payload_snapshot(root) == snapshot_after_tamper,
            safe(sorted(set(payload_snapshot(root))
                       ^ set(snapshot_after_tamper))),
        )
        record(
            "D: stale plan is not silently marked executed",
            "موفقیت" not in approve
            or "بررسی کن" in approve,
            safe(approve),
        )
    except Exception as exc:
        errored("D: unexpected harness error", exc)
    finally:
        uninstall(saved)
        shutil.rmtree(base, ignore_errors=True)

    finish("D")


# ---------------------------------------------------------
# E — File deleted between Plan and Execute
# ---------------------------------------------------------
def scenario_e():
    base = tempfile.mkdtemp(prefix="adv_e_")
    model = ScriptedModel()
    root = write_fixture(base)
    registry, state_dir = write_registry(base, root)
    saved = install(registry, state_dir, model)

    try:
        model.scripts = [[
            batch_tool(planned_files()),
            "FINAL: تمام شد.",
        ], ["FINAL: پس از اجرا."]]
        model.calls = 0

        answer = str(agent.run_agent(REQUEST, "probe", allow_write=True))

        record(
            "E: plan is waiting for approval",
            agent.get_pending_action() is not None,
            safe(answer),
        )

        os.remove(os.path.join(root, "app.js"))

        after_delete = payload_snapshot(root)
        approve = str(agent.run_agent("تأیید", "probe", allow_write=True))

        record(
            "E: re-validation refused the plan after deletion",
            "Re-validation ناموفق بود" in approve
            or "اجرا نشد" in approve,
            safe(approve),
        )
        record(
            "E: deleted file was not silently recreated "
            "(no blind re-execute)",
            not os.path.exists(os.path.join(root, "app.js")),
            safe(sorted(os.listdir(root))),
        )
        record(
            "E: nothing else changed either",
            payload_snapshot(root) == after_delete,
            safe(sorted(set(payload_snapshot(root)) ^ set(after_delete))),
        )
        record(
            "E: failure is reported, not hidden",
            "هیچ فایلی نوشته نشد" in approve
            or "هیچ تغییری" in approve,
            safe(approve),
        )
    except Exception as exc:
        errored("E: unexpected harness error", exc)
    finally:
        uninstall(saved)
        shutil.rmtree(base, ignore_errors=True)

    finish("E")


# ---------------------------------------------------------
# F — Directory pretending to be a file
# ---------------------------------------------------------
def scenario_f():
    base = tempfile.mkdtemp(prefix="adv_f_")
    model = ScriptedModel()
    root = write_fixture(base)
    registry, state_dir = write_registry(base, root)
    saved = install(registry, state_dir, model)

    try:
        # Fixture مخصوص: یکی از targetها باید روی دیسک یک پوشه
        # باشد، نه فایل. فایل اولیهٔ همان مسیر حذف و به‌جایش پوشه
        # ساخته می‌شود.
        os.remove(os.path.join(root, "index.html"))
        os.makedirs(os.path.join(root, "index.html"), exist_ok=True)

        with open(
            os.path.join(root, "index.html", "keep.txt"), "w",
            encoding="utf-8"
        ) as fh:
            fh.write("inside directory\n")

        before = payload_snapshot(root)

        model.scripts = [[
            batch_tool(planned_files()),
            "FINAL: تمام شد.",
        ], ["FINAL: تمام شد."]]
        model.calls = 0

        answer = str(agent.run_agent(REQUEST, "probe", allow_write=True))
        pending = agent.get_pending_action()

        record(
            "F: directory target never becomes an executable plan",
            pending is None,
            safe(pending),
        )
        record(
            "F: directory target produced no summary",
            "خلاصه Plan چندفایلی" not in answer,
            safe(answer),
        )
        record(
            "F: the directory itself was untouched",
            payload_snapshot(root) == before,
            safe(sorted(set(payload_snapshot(root)) ^ set(before))),
        )
        record(
            "F: directory child file still exists",
            os.path.exists(os.path.join(root, "index.html", "keep.txt")),
        )

        # Direct function-level attack: build the summary by hand on a
        # pending whose target is a directory.
        pending_dir = make_pending(root, "probe", planned_files())
        summary = agent.build_multi_file_plan_summary(pending_dir)

        if summary is None:
            record("F: hand-built summary for a directory target", True)
        else:
            line = [
                ln for ln in str(summary).splitlines()
                if ln.strip().startswith("- index.html")
            ]
            honest = bool(line) and (
                "نامشخص" in line[0] or "پوشه" in line[0]
                or "بازنویسی فایل موجود" in line[0]
            )

            record(
                "F: hand-built summary labels a directory "
                "without claiming a safe file overwrite",
                honest,
                safe(line[0] if line else summary),
            )

        # A nested path whose parent is a directory is legitimate.
        # خود موتور باید یا raise کند یا نتیجهٔ ناموفق بدهد؛ مهم
        # این است که هیچ‌وقت روی پوشه نوشته نشود.
        dir_result = agent.tool_write_files(
            root, {"files": [{"path": "index.html", "content": "x"}]}
        )

        record(
            "F: engine refuses a directory write target",
            (
                isinstance(dir_result, dict)
                and dir_result.get("ok") is False
                and dir_result.get("verified") is not True
            )
            or _raises(agent.tool_write_files, root, {"files": [
                {"path": "index.html", "content": "x"},
            ]}),
            safe(dir_result),
        )
        record(
            "F: directory target survived the write attempt intact",
            os.path.isdir(os.path.join(root, "index.html"))
            and os.path.exists(
                os.path.join(root, "index.html", "keep.txt")
            )
            and not os.path.isfile(os.path.join(root, "index.html")),
            safe(sorted(os.listdir(os.path.join(root, "index.html")))),
        )
        record(
            "F: the directory guard itself rejects the target",
            _raises(
                agent.ensure_write_target_is_not_directory,
                os.path.join(root, "index.html"),
                "index.html",
            ),
        )
    except Exception as exc:
        errored("F: unexpected harness error", exc)
    finally:
        uninstall(saved)
        shutil.rmtree(base, ignore_errors=True)

    finish("F")


def _raises(fn, *args):
    try:
        fn(*args)
    except Exception:
        return True

    return False


# ---------------------------------------------------------
# G — Review Mode
# ---------------------------------------------------------
def scenario_g():
    base = tempfile.mkdtemp(prefix="adv_g_")
    model = ScriptedModel()
    root = write_fixture(base)
    registry, state_dir = write_registry(base, root)
    saved = install(registry, state_dir, model)

    try:
        before = payload_snapshot(root)

        model.scripts = [[
            "TOOL: " + json.dumps(
                {"tool": "read_file", "args": {"path": "index.html"}},
                ensure_ascii=False,
            ),
            batch_tool(planned_files()),
            "FINAL: تمام شد.",
        ]]
        model.calls = 0

        answer = str(agent.run_agent(REQUEST, "probe", allow_write=False))

        record(
            "G: review ran inspection",
            "read_file" in answer or "index.html" in answer,
            safe(answer),
        )
        record(
            "G: review created no executable pending action",
            agent.get_pending_action() is None,
            safe(agent.get_pending_action()),
        )
        record(
            "G: review changed nothing on disk",
            payload_snapshot(root) == before,
            safe(sorted(set(payload_snapshot(root)) ^ set(before))),
        )
        record(
            "G: review did not ask for approval",
            "Plan پیشنهادی (نیازمند تأیید)" not in answer,
            safe(answer),
        )
        record(
            "G: review did not execute anything",
            "انجام شد" not in answer,
            safe(answer),
        )

        # allow_write=True روی همان درخواست Review نباید از Gate
        # عبور کند (این اجرای Apply است، نه Review — فقط برای
        # اطمینان از اینکه Review چیزی در گذشته لو نداده).
        model.scripts = [[batch_tool(planned_files()),
                          "FINAL: تمام شد."]]
        model.calls = 0
        agent.run_agent(REQUEST, "probe", allow_write=False)

        record(
            "G: after review, still no pending action",
            agent.get_pending_action() is None,
            safe(agent.get_pending_action()),
        )
        record(
            "G: after review, disk is still untouched",
            payload_snapshot(root) == before,
            safe(sorted(set(payload_snapshot(root)) ^ set(before))),
        )
    except Exception as exc:
        errored("G: unexpected harness error", exc)
    finally:
        uninstall(saved)
        shutil.rmtree(base, ignore_errors=True)

    finish("G")


# ---------------------------------------------------------
# H — General Mode
# ---------------------------------------------------------
def scenario_h():
    base = tempfile.mkdtemp(prefix="adv_h_")
    model = ScriptedModel()
    root = write_fixture(base)
    registry, state_dir = write_registry(base, root)
    saved = install(registry, state_dir, model)

    try:
        before = payload_snapshot(root)

        model.scripts = [[
            batch_tool(planned_files()),
            "FINAL: تمام شد.",
        ]]
        model.calls = 0

        answer = str(agent.run_agent(
            REQUEST, "general", allow_write=True
        ))

        # قرارداد جدید: General Mode اجرایی Pending Action در
        # general_workspace می‌سازد و خلاصه Plan چندفایلی نمایش
        # می‌دهد.
        record(
            "H: general mode produced multi-file summary",
            "خلاصه Plan چندفایلی" in answer,
            safe(answer),
        )
        record(
            "H: general mode created pending action in general_workspace",
            agent.get_pending_action() is not None
            and str(agent.get_pending_action().get("project_root", "")).endswith("general_workspace"),
            safe(agent.get_pending_action()),
        )
        record(
            "H: general mode never claimed success",
            "انجام شد" not in answer,
            safe(answer),
        )
        record(
            "H: general mode changed nothing on real project disk",
            payload_snapshot(root) == before,
            safe(sorted(set(payload_snapshot(root)) ^ set(before))),
        )

        # Clear pending from general mode call
        agent.clear_pending_action()

        # A project that does not exist at all is also not General Mode.
        model.scripts = [[batch_tool(planned_files()),
                          "FINAL: تمام شد."]]
        model.calls = 0
        answer2 = str(agent.run_agent(
            REQUEST, "no-such-project", allow_write=True
        ))

        record(
            "H: unknown project returns project-not-found error",
            "پیدا نشد" in answer2 or "انتخابشده" in answer2,
            safe(answer2),
        )
        record(
            "H: unknown project created no pending action",
            agent.get_pending_action() is None,
            safe(agent.get_pending_action()),
        )
        record(
            "H: unknown project changed nothing on disk",
            payload_snapshot(root) == before,
            safe(sorted(set(payload_snapshot(root)) ^ set(before))),
        )
    except Exception as exc:
        errored("H: unexpected harness error", exc)
    finally:
        uninstall(saved)
        shutil.rmtree(base, ignore_errors=True)

    finish("H")


# ---------------------------------------------------------
# I — Approval replay / double execution
# ---------------------------------------------------------
def scenario_i():
    base = tempfile.mkdtemp(prefix="adv_i_")
    model = ScriptedModel()
    root = write_fixture(base)
    registry, state_dir = write_registry(base, root)
    saved = install(registry, state_dir, model)

    try:
        model.scripts = [[
            batch_tool(planned_files()),
            "FINAL: تمام شد.",
        ], ["FINAL: پس از اجرا."], ["FINAL: تمام شد."]]
        model.calls = 0

        agent.run_agent(REQUEST, "probe", allow_write=True)
        first_pending = agent.get_pending_action()

        record(
            "I: a real pending action exists before approval",
            first_pending is not None,
            safe(first_pending),
        )

        agent.run_agent("تأیید", "probe", allow_write=True)

        after_first = {
            rel: read(os.path.join(root, rel)) for rel in PLANNED
        }
        consumed = agent.get_pending_action()

        record(
            "I: first approval executed the plan",
            after_first == PLANNED,
            safe(after_first),
        )
        record(
            "I: pending action was consumed",
            consumed is None,
            safe(consumed),
        )

        # Replay: همان پاسخ تأیید دوباره.
        after_snapshot = payload_snapshot(root)
        replay = str(agent.run_agent("تأیید", "probe", allow_write=True))

        record(
            "I: replayed approval changed nothing",
            payload_snapshot(root) == after_snapshot,
            safe(sorted(set(payload_snapshot(root)) ^ set(after_snapshot))),
        )
        record(
            "I: replayed approval did not re-run the batch",
            read(os.path.join(root, "style.css")) == PLANNED["style.css"],
            safe(read(os.path.join(root, "style.css"))),
        )
        record(
            "I: replayed approval is not an execute report",
            "تغییر با موفقیت اعمال شد" not in replay,
            safe(replay),
        )
        record(
            "I: no new pending action appeared after replay",
            agent.get_pending_action() is None,
            safe(agent.get_pending_action()),
        )

        # اجرای مستقیم همان Pending ذخیره‌شده.
        replay2 = agent.execute_pending_action()

        record(
            "I: direct execute of a consumed pending is a no-op",
            replay2 is None,
            safe(replay2),
        )
        record(
            "I: disk still identical after direct execute",
            payload_snapshot(root) == after_snapshot,
            safe(sorted(set(payload_snapshot(root)) ^ set(after_snapshot))),
        )
    except Exception as exc:
        errored("I: unexpected harness error", exc)
    finally:
        uninstall(saved)
        shutil.rmtree(base, ignore_errors=True)

    finish("I")


# ---------------------------------------------------------
# J — Legacy direct-write bypass
# ---------------------------------------------------------
def scenario_j():
    base = tempfile.mkdtemp(prefix="adv_j_")
    model = ScriptedModel()
    root = write_fixture(base)
    registry, state_dir = write_registry(base, root)
    saved = install(registry, state_dir, model)

    try:
        before = payload_snapshot(root)

        model.scripts = [["BEGIN_FILE\n" + PLANNED["style.css"]
                          + "END_FILE"]]
        model.calls = 0

        answer = str(agent.run_agent_legacy(
            "style.css را تغییر بده",
            project_name="probe",
            allow_write=True,
        ))

        record(
            "J: legacy direct write is refused",
            "مجاز نیست" in answer,
            safe(answer),
        )
        record(
            "J: legacy created no pending action",
            agent.get_pending_action() is None,
            safe(agent.get_pending_action()),
        )
        record(
            "J: legacy wrote nothing",
            payload_snapshot(root) == before,
            safe(sorted(set(payload_snapshot(root)) ^ set(before))),
        )
        record(
            "J: legacy claimed no success",
            "انجام شد" not in answer,
            safe(answer),
        )

        # Legacy با allow_write=False هم باید بی‌اثر باشد.
        model.scripts = [["BEGIN_FILE\n" + PLANNED["app.js"]
                          + "END_FILE"]]
        model.calls = 0
        agent.run_agent_legacy(
            "app.js را تغییر بده",
            project_name="probe",
            allow_write=False,
        )

        record(
            "J: legacy read-only mode also wrote nothing",
            payload_snapshot(root) == before,
            safe(sorted(set(payload_snapshot(root)) ^ set(before))),
        )

        # General Mode از مسیر Legacy هم نباید بنویسد.
        model.scripts = [["BEGIN_FILE\n" + PLANNED["index.html"]
                          + "END_FILE"]]
        model.calls = 0
        general_answer = str(agent.run_agent_legacy(
            "index.html را تغییر بده",
            project_name="general",
            allow_write=True,
        ))

        record(
            "J: legacy general mode wrote nothing",
            payload_snapshot(root) == before,
            safe(sorted(set(payload_snapshot(root)) ^ set(before))),
        )
        record(
            "J: legacy general mode claimed no success",
            "انجام شد" not in general_answer,
            safe(general_answer),
        )
    except Exception as exc:
        errored("J: unexpected harness error", exc)
    finally:
        uninstall(saved)
        shutil.rmtree(base, ignore_errors=True)

    finish("J")


# ---------------------------------------------------------
# K — False success / partial failure
# ---------------------------------------------------------
def scenario_k():
    base = tempfile.mkdtemp(prefix="adv_k_")
    model = ScriptedModel()
    root = write_fixture(base)
    registry, state_dir = write_registry(base, root)
    saved = install(registry, state_dir, model)

    try:
        before = payload_snapshot(root)

        bad = [
            {"path": "index.html", "content": PLANNED["index.html"]},
            {"path": "style.css", "content": PLANNED["style.css"]},
            {"path": "app.js", "content": "function ( { broken\n"},
        ]

        result = agent.tool_write_files(root, {"files": bad})

        record(
            "K: a failing batch is not reported as ok",
            result.get("ok") is False,
            safe(result.get("ok")),
        )
        record(
            "K: a failing batch is not reported as verified",
            result.get("verified") is not True,
            safe(result.get("verified")),
        )
        record(
            "K: a failing batch reports rollback state",
            bool((result.get("data") or {}).get("rolled_back")) is True
            or "rolled_back" in (result.get("data") or {}),
            safe(result.get("data")),
        )
        record(
            "K: disk is back to the pre-batch state",
            payload_snapshot(root) == before,
            safe(sorted(set(payload_snapshot(root)) ^ set(before))),
        )

        # حالا همان شکست را از مسیر canonical کامل بررسی کن.
        model.scripts = [[
            batch_tool(bad),
            "FINAL: تمام شد.",
        ], ["FINAL: تمام شد."]]
        model.calls = 0

        plan_answer = str(agent.run_agent(REQUEST, "probe",
                                         allow_write=True))
        pending = agent.get_pending_action()

        record(
            "K: canonical plan was still built for the bad batch",
            pending is not None,
            safe(plan_answer),
        )
        record(
            "K: the plan summary does not promise success",
            "موفق" not in str(
                plan_answer[
                    plan_answer.find("خلاصه Plan چندفایلی"):
                    plan_answer.find("خلاصه Plan چندفایلی") + 900
                ] if "خلاصه Plan چندفایلی" in plan_answer else ""
            ),
            safe(plan_answer),
        )

        pre_approval = payload_snapshot(root)
        exec_answer = str(agent.run_agent("تأیید", "probe",
                                          allow_write=True))

        record(
            "K: execution result is not a blanket success claim",
            "با موفقیت اعمال شد" not in exec_answer
            or "ناموفق" in exec_answer,
            safe(exec_answer),
        )
        record(
            "K: rollback left the disk unchanged",
            payload_snapshot(root) == pre_approval,
            safe(sorted(set(payload_snapshot(root)) ^ set(pre_approval))),
        )
        record(
            "K: verify reported the real outcome",
            "ناموفق" in exec_answer
            or "Verify" in exec_answer
            or "خطا" in exec_answer,
            safe(exec_answer),
        )
    except Exception as exc:
        errored("K: unexpected harness error", exc)
    finally:
        uninstall(saved)
        shutil.rmtree(base, ignore_errors=True)

    finish("K")


# ---------------------------------------------------------
# L — Multi-file Undo
# ---------------------------------------------------------
def scenario_l():
    base = tempfile.mkdtemp(prefix="adv_l_")
    model = ScriptedModel()
    root = write_fixture(base)
    registry, state_dir = write_registry(base, root)
    saved = install(registry, state_dir, model)

    try:
        before = payload_snapshot(root)

        model.scripts = [[
            batch_tool(planned_files()),
            "FINAL: تمام شد.",
        ], ["FINAL: پس از اجرا."], ["FINAL: تمام شد."]]
        model.calls = 0

        agent.run_agent(REQUEST, "probe", allow_write=True)
        agent.run_agent("تأیید", "probe", allow_write=True)

        after_apply = {
            rel: read(os.path.join(root, rel)) for rel in PLANNED
        }

        record(
            "L: batch applied before undo",
            after_apply == PLANNED,
            safe(after_apply),
        )

        undo_answer = str(agent.run_agent("undo", "probe",
                                          allow_write=True))
        after_undo = {
            rel: read(os.path.join(root, rel)) for rel in ORIGINAL
        }

        record(
            "L: undo restored index.html",
            after_undo["index.html"] == ORIGINAL["index.html"],
            safe(after_undo["index.html"]),
        )
        record(
            "L: undo restored style.css",
            after_undo["style.css"] == ORIGINAL["style.css"],
            safe(after_undo["style.css"]),
        )
        record(
            "L: undo restored app.js",
            after_undo["app.js"] == ORIGINAL["app.js"],
            safe(after_undo["app.js"]),
        )

        undone = (
            after_undo["index.html"] == ORIGINAL["index.html"]
            and after_undo["style.css"] == ORIGINAL["style.css"]
            and after_undo["app.js"] == ORIGINAL["app.js"]
        )

        journal = [
            (e.get("action_type"), e.get("tool"), e.get("target"))
            for e in agent.ACTION_JOURNAL
        ]

        if not undone:
            still_planned = [
                rel for rel in PLANNED
                if read(os.path.join(root, rel)) == PLANNED[rel]
            ]

            # کنترل: همان Undo روی یک Write تک‌فایلی باید کار کند.
            # اگر این هم کار نکند، مشکل از Undo نیست؛ از harness.
            control = write_fixture(base, "control")
            control_registry, control_state = write_registry(
                base, control, name="ctl"
            )

            agent.PROJECTS_FILE = control_registry
            agent.PENDING_ACTION_FILE = os.path.join(
                control_state, "pending.json"
            )
            agent.ACTION_JOURNAL[:] = []

            class ControlModel:
                def __init__(self):
                    self.n = 0

                def __call__(self, *a, **kw):
                    self.n += 1

                    if self.n == 1:
                        return write_tool("write_file", {
                            "path": "style.css",
                            "content": PLANNED["style.css"],
                        })

                    return "FINAL: تمام شد."

            agent.call_model = ControlModel()

            agent.run_agent(
                "style.css را تغییر بده", "ctl", allow_write=True
            )
            agent.run_agent("تأیید", "ctl", allow_write=True)
            agent.run_agent("undo", "ctl", allow_write=True)

            control_restored = (
                read(os.path.join(control, "style.css"))
                == ORIGINAL["style.css"]
            )

            record(
                "L: CONTROL — single-file undo works in the same "
                "harness (proves the gap is batch-specific)",
                control_restored,
                safe(read(os.path.join(control, "style.css"))),
            )
            record(
                "L: FAILURE REPORT — multi-file undo did not restore "
                "the batch (journal entries: "
                + str(journal)
                + ")",
                False,
                "restored="
                + safe(after_undo)
                + " still_planned="
                + safe(still_planned)
                + " answer="
                + safe(undo_answer),
            )
        else:
            after_first_undo = payload_snapshot(root)
            second = str(agent.run_agent("undo", "probe",
                                         allow_write=True))

            record(
                "L: second undo did not mutate again",
                payload_snapshot(root) == after_first_undo,
                safe(sorted(set(payload_snapshot(root))
                           ^ set(after_first_undo))),
            )
            record(
                "L: second undo is refused, not faked",
                "Undo انجام نشد" in second or "Undo" in second,
                safe(second),
            )
    except Exception as exc:
        errored("L: unexpected harness error", exc)
    finally:
        uninstall(saved)
        shutil.rmtree(base, ignore_errors=True)

    finish("L")


# ---------------------------------------------------------
# M — Summary / Execute identity
# ---------------------------------------------------------
def scenario_m():
    base = tempfile.mkdtemp(prefix="adv_m_")
    model = ScriptedModel()
    root = write_fixture(base)
    registry, state_dir = write_registry(base, root)
    saved = install(registry, state_dir, model)

    try:
        # Summary مستقیم از همان Plan canonical.
        pending = make_pending(root, "probe", planned_files())
        summary_targets_direct = summary_targets(
            agent.build_multi_file_plan_summary(pending)
        )
        plan_targets_direct = [
            item.get("path")
            for item in pending["args"]["files"]
        ]

        record(
            "M: summary targets == plan targets (function level)",
            summary_targets_direct == plan_targets_direct,
            safe(summary_targets_direct)
            + " vs "
            + safe(plan_targets_direct),
        )

        before_json = json.dumps(pending, sort_keys=True,
                                 ensure_ascii=False)
        agent.build_multi_file_plan_summary(pending)
        after_json = json.dumps(pending, sort_keys=True,
                                ensure_ascii=False)

        record(
            "M: summary building does not mutate the plan",
            before_json == after_json,
        )
        record(
            "M: summary creates no second pending action",
            agent.get_pending_action() is None,
        )

        # Summary از مسیر واقعی UI.
        model.scripts = [[
            batch_tool(planned_files()),
            "FINAL: تمام شد.",
        ], ["FINAL: پس از اجرا."]]
        model.calls = 0

        answer = str(agent.run_agent(REQUEST, "probe", allow_write=True))
        start = answer.find("خلاصه Plan چندفایلی")
        end = answer.find("این خلاصه فقط از روی همین Plan")

        shown = summary_body_targets(
            answer[start:end] if start >= 0 and end > start else ""
        )
        pending_targets = [
            item.get("path")
            for item in (agent.get_pending_action() or {})
            .get("args", {}).get("files", [])
        ]

        record(
            "M: shown summary == approved plan targets",
            shown == pending_targets
            and shown == ["index.html", "style.css", "app.js"],
            "shown="
            + safe(shown)
            + " approved="
            + safe(pending_targets),
        )

        mtimes = {
            rel: os.path.getmtime(os.path.join(root, rel))
            for rel in PLANNED
        }
        existed = set(os.listdir(root))

        agent.run_agent("تأیید", "probe", allow_write=True)

        touched = {
            rel for rel in PLANNED
            if os.path.getmtime(os.path.join(root, rel)) != mtimes[rel]
        }
        appeared = {n for n in set(os.listdir(root)) - existed if not is_infra(n)}

        record(
            "M: execute touched exactly the approved targets",
            touched == set(pending_targets),
            "touched=" + safe(sorted(touched))
            + " approved=" + safe(pending_targets),
        )
        record(
            "M: execute created no extra file",
            not appeared,
            safe(sorted(appeared)),
        )
        record(
            "M: final content matches the plan exactly",
            all(
                read(os.path.join(root, rel)) == PLANNED[rel]
                for rel in PLANNED
            ),
            safe({rel: read(os.path.join(root, rel)) for rel in PLANNED}),
        )
    except Exception as exc:
        errored("M: unexpected harness error", exc)
    finally:
        uninstall(saved)
        shutil.rmtree(base, ignore_errors=True)

    finish("M")


# ---------------------------------------------------------
# N — Single-file regression
# ---------------------------------------------------------
def scenario_n():
    base = tempfile.mkdtemp(prefix="adv_n_")
    model = ScriptedModel()
    root = write_fixture(base)
    registry, state_dir = write_registry(base, root)
    saved = install(registry, state_dir, model)

    try:
        css = os.path.join(root, "style.css")
        new_css = "h1 {\n  color: green;\n}\n"

        model.scripts = [[
            write_tool("write_file", {
                "path": "style.css",
                "content": new_css,
            }),
            "FINAL: تمام شد.",
        ], ["FINAL: پس از اجرا."]]
        model.calls = 0

        before = payload_snapshot(root)
        answer = str(agent.run_agent(
            "رنگ عنوان را در style.css تغییر بده",
            "probe",
            allow_write=True,
        ))

        record(
            "N: single-file plan shows no multi-file summary",
            "خلاصه Plan چندفایلی" not in answer,
            safe(answer),
        )
        record(
            "N: single-file approval prompt is intact",
            "برای اجرا پاسخ دهید: تأیید" in answer,
            safe(answer),
        )
        record(
            "N: single-file nothing written before approval",
            payload_snapshot(root) == before,
            safe(sorted(set(payload_snapshot(root)) ^ set(before))),
        )

        agent.run_agent("تأیید", "probe", allow_write=True)

        record(
            "N: single-file execute path unchanged",
            read(css) == new_css,
            safe(read(css)),
        )
        record(
            "N: single-file verify unchanged",
            agent.get_last_write_result() is not None
            and agent.get_last_write_result()
            .get("validation", {}).get("valid") is True,
            safe(agent.get_last_write_result()),
        )

        # Review تک‌فایلی
        with open(css, "w", encoding="utf-8") as fh:
            fh.write(ORIGINAL["style.css"])

        before_review = payload_snapshot(root)
        model.scripts = [[
            write_tool("write_file", {
                "path": "style.css",
                "content": new_css,
            }),
            "FINAL: تمام شد.",
        ]]
        model.calls = 0

        review = str(agent.run_agent(
            "style.css را بررسی کن", "probe", allow_write=False
        ))

        record(
            "N: single-file review still read-only",
            payload_snapshot(root) == before_review,
            safe(sorted(set(payload_snapshot(root)) ^ set(before_review))),
        )
        record(
            "N: single-file review leaves no pending action",
            agent.get_pending_action() is None,
            safe(agent.get_pending_action()),
        )
    except Exception as exc:
        errored("N: unexpected harness error", exc)
    finally:
        uninstall(saved)
        shutil.rmtree(base, ignore_errors=True)

    finish("N")


# ---------------------------------------------------------
# O — Autonomous Mode
# ---------------------------------------------------------
def scenario_o():
    base = tempfile.mkdtemp(prefix="adv_o_")
    model = ScriptedModel()
    root = write_fixture(base)
    registry, state_dir = write_registry(
        base, root, modes={"probe": "autonomous"}
    )
    saved = install(registry, state_dir, model)

    try:
        record(
            "O: fixture project is really autonomous",
            agent.is_autonomous_project("probe") is True,
        )

        before = payload_snapshot(root)

        model.scripts = [[
            batch_tool(planned_files()),
            "FINAL: تمام شد.",
        ]]
        model.calls = 0

        answer = str(agent.run_agent(REQUEST, "probe", allow_write=True))
        pending = agent.get_pending_action()

        record(
            "O: autonomous mode creates no pending action",
            pending is None,
            safe(pending),
        )
        record(
            "O: autonomous mode executed the plan",
            all(
                read(os.path.join(root, rel)) == PLANNED[rel]
                for rel in PLANNED
            ),
            safe({rel: read(os.path.join(root, rel)) for rel in PLANNED}),
        )
        record(
            "O: autonomous mode still wrote only the plan targets",
            not ({n for n in set(os.listdir(root)) - set(ORIGINAL) if not is_infra(n)}),
            safe(sorted({n for n in set(os.listdir(root)) - set(ORIGINAL) if not is_infra(n)})),
        )

        # Re-validation باید در autonomous هم فعال باشد. نقطهٔ
        # حمله درست، «بین Plan و Re-validation» است (نه بعد از آن):
        # یک spy فقط-مشاهده‌گر روی revalidate_pending_action
        # می‌گذاریم تا مطمئن شویم واقعاً فراخوانی می‌شود، و محتوا
        # را همان‌جا دستکاری می‌کنیم.
        root2 = write_fixture(base, "project2")
        record(
            "O: second fixture created for re-validation check",
            os.path.isdir(root2),
        )

        registry2, state2 = write_registry(
            base, root2, modes={"probe2": "autonomous"}, name="probe2"
        )

        agent.PROJECTS_FILE = registry2
        agent.clear_pending_action()

        revalidated = {"called": 0, "touched": False}
        real_revalidate = agent.revalidate_pending_action

        def spy_revalidate(pending):
            revalidated["called"] += 1
            # تغییر عمدی درست پیش از اجرای Re-validation واقعی.
            with open(
                os.path.join(root2, "style.css"), "w", encoding="utf-8"
            ) as fh:
                fh.write("h1 {\n  color: purple;\n}\n")

            revalidated["touched"] = True

            return real_revalidate(pending)

        agent.revalidate_pending_action = spy_revalidate

        model.scripts = [[
            batch_tool(planned_files()),
            "FINAL: تمام شد.",
        ]]
        model.calls = 0

        try:
            tampered_answer = str(agent.run_agent(
                REQUEST, "probe2", allow_write=True
            ))
        finally:
            agent.revalidate_pending_action = real_revalidate

        record(
            "O: re-validation actually runs in autonomous mode",
            revalidated["called"] >= 1 and revalidated["touched"],
            safe(revalidated),
        )
        record(
            "O: autonomous re-validation is not bypassed",
            "Re-validation" in tampered_answer
            or "هیچ فایلی نوشته نشد" in tampered_answer,
            safe(tampered_answer),
        )
        record(
            "O: tampered file was not overwritten in autonomous mode",
            read(os.path.join(root2, "style.css"))
            == "h1 {\n  color: purple;\n}\n",
            safe(read(os.path.join(root2, "style.css"))),
        )
        record(
            "O: no other file was written either in autonomous mode",
            (
                read(os.path.join(root2, "index.html"))
                == ORIGINAL["index.html"]
                and read(os.path.join(root2, "app.js"))
                == ORIGINAL["app.js"]
            ),
            safe({
                rel: read(os.path.join(root2, rel))
                for rel in ORIGINAL
            }),
        )
        record(
            "O: autonomous failure is not reported as success",
            "با موفقیت اعمال شد" not in tampered_answer
            or "ناموفق" in tampered_answer,
            safe(tampered_answer),
        )
    except Exception as exc:
        errored("O: unexpected harness error", exc)
    finally:
        uninstall(saved)
        shutil.rmtree(base, ignore_errors=True)

    finish("O")


# ---------------------------------------------------------
# Invariants
# ---------------------------------------------------------
def invariants():
    def inv(number, name, ok, detail=""):
        record(
            "INV{}: {}".format(number, name), ok, detail
        )

    by_name = {name: status for name, status, _ in results}

    def all_pass(prefix):
        seen = False

        for name, status, _ in results:
            if name.startswith(prefix):
                seen = True

                if status != "PASS":
                    return False, name

        return (True, "") if seen else (None, "not exercised")

    inv(
        1,
        "no fabricated target enters the summary",
        all_pass("B:")[0] and all_pass("C:")[0],
        safe([all_pass("B:"), all_pass("C:")]),
    )
    inv(
        2,
        "summary does not build an independent plan",
        all_pass("M:")[0],
        safe(all_pass("M:")),
    )
    inv(
        3,
        "approval and execute use the same canonical plan",
        all_pass("M:")[0] and all_pass("A:")[0],
        safe([all_pass("M:"), all_pass("A:")]),
    )
    inv(
        4,
        "review never mutates",
        all_pass("G:")[0] and all_pass("N:")[0],
        safe([all_pass("G:"), all_pass("N:")]),
    )
    inv(
        5,
        "general mode never mutates",
        all_pass("H:")[0],
        safe(all_pass("H:")),
    )
    inv(
        6,
        "legacy never writes directly",
        all_pass("J:")[0],
        safe(all_pass("J:")),
    )
    inv(
        7,
        "re-validation is not bypassable",
        all_pass("D:")[0] and all_pass("E:")[0] and all_pass("O:")[0],
        safe([all_pass("D:"), all_pass("E:"), all_pass("O:")]),
    )
    inv(
        8,
        "a directory is never mutated as a file",
        all_pass("F:")[0],
        safe(all_pass("F:")),
    )
    inv(
        9,
        "no false success is reported",
        all_pass("K:")[0],
        safe(all_pass("K:")),
    )
    inv(
        10,
        "undo restores a multi-file mutation",
        all_pass("L:")[0],
        safe(all_pass("L:")),
    )
    inv(
        11,
        "a pending action is not executable twice",
        all_pass("I:")[0],
        safe(all_pass("I:")),
    )

    _ = by_name


# ---------------------------------------------------------
# main
# ---------------------------------------------------------
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    scenario_a()
    scenario_b()
    scenario_c()
    scenario_d()
    scenario_e()
    scenario_f()
    scenario_g()
    scenario_h()
    scenario_i()
    scenario_j()
    scenario_k()
    scenario_l()
    scenario_m()
    scenario_n()
    scenario_o()
    invariants()

    counts = {"PASS": 0, "FAIL": 0, "ERROR": 0, "BLOCKED": 0}

    for _, status, _ in results:
        counts[status] = counts.get(status, 0) + 1

    print("")
    print("=" * 60)
    print("SCENARIOS")
    print("=" * 60)

    for letter in "ABCDEFGHIJKLMNO":
        print("{} — {}".format(letter, scenario_status.get(letter, "?")))

    print("")
    print("=" * 60)
    print("RESULT")
    print("=" * 60)
    print("PASS: {}".format(counts["PASS"]))
    print("FAIL: {}".format(counts["FAIL"]))
    print("ERROR: {}".format(counts["ERROR"]))
    print("BLOCKED: {}".format(counts["BLOCKED"]))

    real_bug = (
        counts["FAIL"] > 0
        or counts["ERROR"] > 0
        or any(
            status != "PASS"
            for status in scenario_status.values()
        )
    )

    print("")
    print(
        "STAGE 4 ADVERSARIAL TEST: "
        + ("FAIL" if real_bug else "PASS")
    )

    if real_bug:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
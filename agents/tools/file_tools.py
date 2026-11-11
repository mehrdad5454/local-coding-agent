import os, re, fnmatch, difflib, shutil, tempfile, time
from pathlib import Path

from agents.core_utils import *

# State/results (kept compatible with previous agent.py globals)
LAST_WRITE_RESULT = None
LAST_WRITE_MULTI_RESULT = None
MULTI_BACKUP_DIRNAME = ".agent-multi-backup"

def build_unified_diff(old_content, new_content, relative_path, context_lines=3):
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    if old_lines == new_lines:
        return ""
    display_path = relative_path.replace("\\", "/")
    diff_lines = difflib.unified_diff(
        old_lines, new_lines,
        fromfile="a/" + display_path,
        tofile="b/" + display_path,
        n=context_lines, lineterm=""
    )
    return "\n".join(diff_lines)

def compute_write_diff(full_path, relative_path, new_content):
    if not os.path.isfile(full_path):
        return None
    try:
        with open(full_path, "r", encoding="utf-8", newline="") as f:
            old_content = f.read()
    except UnicodeDecodeError:
        return "(diff در دسترس نیست: فایل UTF-8 نیست)"
    return build_unified_diff(old_content, new_content, relative_path)

def remember_write_result(result):
    global LAST_WRITE_RESULT
    LAST_WRITE_RESULT = dict(result)

def get_last_write_result():
    if LAST_WRITE_RESULT is None:
        return None
    return dict(LAST_WRITE_RESULT)

def remember_write_multi_result(result):
    global LAST_WRITE_MULTI_RESULT
    LAST_WRITE_MULTI_RESULT = dict(result)

def get_last_write_multi_result():
    if LAST_WRITE_MULTI_RESULT is None:
        return None
    return dict(LAST_WRITE_MULTI_RESULT)

def _remove_empty_dirs(dir_path, stop_at):
    stop_at = os.path.abspath(stop_at)
    current = os.path.abspath(dir_path)
    while True:
        if not current or not os.path.isdir(current):
            break
        if current == stop_at:
            break
        try:
            if os.listdir(current):
                break
            os.rmdir(current)
        except OSError:
            break
        current = os.path.dirname(current)

def _rollback_multi_file(project_root, written_entries):
    for entry in reversed(written_entries):
        full_path = entry["full_path"]
        action = entry["action"]
        backup_path = entry.get("backup_path")
        try:
            if action == "modify" and backup_path is not None:
                with open(backup_path, "rb") as f:
                    data = f.read()
                with open(full_path, "wb") as f:
                    f.write(data)
            elif action == "create":
                if os.path.isfile(full_path):
                    os.remove(full_path)
                parent = os.path.dirname(full_path)
                _remove_empty_dirs(parent, project_root)
        except Exception:
            pass

def _get_timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())

def read_file(project_root, relative_path):
    full_path = safe_path(project_root, relative_path)
    if not os.path.isfile(full_path):
        raise FileNotFoundError(f"File not found: {relative_path}")
    with open(full_path, "r", encoding="utf-8") as f:
        return f.read()

def create_backup(project_root, full_path):
    if not os.path.isfile(full_path):
        return None
    backup_path = full_path + ".agent-backup"
    counter = 1
    while os.path.exists(backup_path):
        backup_path = full_path + ".agent-backup." + str(counter)
        counter += 1
    root = os.path.abspath(project_root)
    backup_abs = os.path.abspath(backup_path)
    if os.path.commonpath([backup_abs, root]) != root:
        raise ValueError("Backup path is outside the selected project.")
    with open(full_path, "rb") as src:
        data = src.read()
    with open(backup_path, "wb") as dst:
        dst.write(data)
    return backup_path

def write_file(project_root, relative_path, content, return_details=False, create_parents=False, action_type="write"):
    if is_stop_requested():
        remember_stop_blocked_action(
            tool=("edit_file" if action_type == "edit" else "write_file"),
            action_type=action_type,
            args={"path": relative_path, "content": content},
            project_root=project_root,
            target=relative_path,
        )
        raise RuntimeError(EMERGENCY_STOP_MESSAGE)
    full_path = safe_path(project_root, relative_path)
    parent = os.path.dirname(full_path)
    if not os.path.isdir(parent):
        if not create_parents:
            raise FileNotFoundError(f"Parent directory does not exist: {parent}")
        os.makedirs(parent, exist_ok=True)
        if not os.path.commonpath([os.path.abspath(parent), os.path.abspath(project_root)]) == os.path.abspath(project_root):
            raise ValueError("Path is outside the selected project.")
    existed_before = os.path.isfile(full_path)
    diff = compute_write_diff(full_path, relative_path, content)
    before_state = build_action_before_state(full_path)
    backup_path = create_backup(project_root, full_path)
    with open(full_path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    try:
        validation = validate_written_file(project_root, relative_path, content, full_path)
    except Exception as e:
        validation = {
            "valid": False, "exists": os.path.isfile(full_path), "size": None,
            "content_matches": False, "syntax_checked": False, "syntax_valid": None,
            "error": "Validation با خطای پیش‌بینی‌نشده متوقف شد: " + str(e)
        }
    remember_write_result({
        "project_root": project_root, "relative_path": relative_path, "path": full_path,
        "backup_path": backup_path, "existed_before": existed_before, "diff": diff,
        "changed": (not existed_before or bool(diff)), "validation": validation
    })
    record_action(
        action_type=action_type,
        tool=("edit_file" if action_type == "edit" else "write_file"),
        args={"path": relative_path, "content": content},
        project_root=project_root, target=relative_path,
        validation_state=validation.get("valid"), backup_path=backup_path,
        before_state=before_state, execution_state=ACTION_STATE_DONE,
        verify_state=verify_state_from_bool(validation.get("valid")),
        result_summary=summarize_action_result(summarize_validation(validation)),
    )
    if return_details:
        return get_last_write_result()
    return full_path

def write_file_multi(project_root, files, return_details=False):
    files = files or []
    root = os.path.abspath(project_root)
    if not os.path.isdir(root):
        raise ValueError("پوشه پروژه پیدا نشد:\n" + project_root)
    if is_stop_requested():
        remember_stop_blocked_action(
            tool="write_file_multi", action_type="multi_write",
            args={"paths": [item.get("relative_path") or item.get("path") for item in files if isinstance(item, dict)]},
            project_root=root,
        )
        raise RuntimeError(EMERGENCY_STOP_MESSAGE)
    multi_root = os.path.join(root, MULTI_BACKUP_DIRNAME)
    os.makedirs(multi_root, exist_ok=True)
    transaction_dir = tempfile.mkdtemp(prefix="mfp_", dir=multi_root)
    if os.path.commonpath([os.path.abspath(transaction_dir), root]) != root:
        shutil.rmtree(transaction_dir, ignore_errors=True)
        raise ValueError("Transaction directory is outside the project.")
    written_entries = []
    file_results = []
    overall_error = None
    rolled_back = False
    try:
        for index, file_item in enumerate(files):
            relative_path = file_item.get("relative_path") or file_item.get("path", "")
            content = file_item.get("content")
            action = file_item.get("action", "")
            if not relative_path:
                raise ValueError("هر فایل باید دارای relative_path یا path باشد.")
            full_path = safe_path(root, relative_path)
            resolved_path = os.path.realpath(full_path)
            resolved_root = os.path.realpath(root)
            if os.path.commonpath([resolved_path, resolved_root]) != resolved_root:
                raise ValueError("Path is outside the selected project.")
            ensure_write_target_is_not_directory(full_path, relative_path)
            existed_before = os.path.isfile(full_path)
            undo_backup_path = create_backup(root, full_path)
            if not action:
                action = "modify" if existed_before else "create"
            diff = compute_write_diff(full_path, relative_path, content)
            backup_path = None
            if action == "modify" and existed_before:
                backup_filename = str(index) + "_" + os.path.basename(full_path) + ".tmp"
                tx_backup_path = os.path.join(transaction_dir, backup_filename)
                if os.path.commonpath([os.path.abspath(tx_backup_path), root]) != root:
                    raise ValueError("Backup path is outside project.")
                with open(full_path, "rb") as src:
                    backup_data = src.read()
                with open(tx_backup_path, "wb") as dst:
                    dst.write(backup_data)
                backup_path = tx_backup_path
            parent = os.path.dirname(full_path)
            if not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            tmp_fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=".mfp_tmp_")
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="") as f:
                    f.write(content)
                os.replace(tmp_name, full_path)
            except Exception:
                if os.path.exists(tmp_name):
                    try:
                        os.remove(tmp_name)
                    except OSError:
                        pass
                raise
            written_entries.append({
                "full_path": full_path, "relative_path": relative_path, "action": action,
                "backup_path": backup_path, "undo_backup_path": undo_backup_path,
                "existed_before": existed_before
            })
            try:
                validation = validate_written_file(root, relative_path, content, full_path)
            except Exception as e:
                validation = {
                    "valid": False, "exists": os.path.isfile(full_path), "size": None,
                    "content_matches": False, "syntax_checked": False, "syntax_valid": None,
                    "error": "Validation با خطای پیش‌بینی‌نشدن متوقف شد: " + str(e)
                }
            file_results.append({
                "relative_path": relative_path, "full_path": full_path, "action": action,
                "backup_path": backup_path, "undo_backup_path": undo_backup_path,
                "existed_before": existed_before, "diff": diff, "validation": validation, "error": None
            })
            if not validation.get("valid"):
                overall_error = "Validation ناموفق: " + relative_path + " :: " + str(validation.get("error"))
                break
    except Exception as e:
        overall_error = "Multi-file Write با خطا متوقف شد: " + str(e)
    if overall_error is not None:
        rolled_back = True
        _rollback_multi_file(root, written_entries)
        for entry in written_entries:
            stale = entry.get("undo_backup_path")
            if not stale:
                continue
            try:
                if os.path.isfile(stale):
                    os.remove(stale)
            except Exception:
                pass
        for entry in file_results:
            if entry["action"] == "modify":
                entry["existed_before"] = True
            elif entry["action"] == "create":
                entry["existed_before"] = os.path.isfile(entry["full_path"])
        result = {"success": False, "files": file_results, "rolled_back": rolled_back, "error": overall_error, "timestamp": _get_timestamp()}
        shutil.rmtree(transaction_dir, ignore_errors=True)
        remember_write_multi_result(result)
        if return_details:
            return result
        raise RuntimeError(overall_error)
    shutil.rmtree(transaction_dir, ignore_errors=True)
    result = {"success": True, "files": file_results, "rolled_back": False, "error": None, "timestamp": _get_timestamp()}
    remember_write_multi_result(result)
    if return_details:
        return result
    return [item["full_path"] for item in file_results]

def search_subfolders(project_root, filename):
    root = os.path.abspath(project_root)
    max_depth = 5
    max_files = 5000
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        relative_dir = os.path.relpath(dirpath, root)
        if relative_dir != "." and len(relative_dir.split(os.sep)) >= max_depth:
            dirnames[:] = []
        for name in filenames:
            if name == filename:
                relative = os.path.relpath(os.path.join(dirpath, name), root)
                return relative.replace(os.sep, "/")
        scanned += len(filenames)
        if scanned >= max_files:
            break
    return None

def list_directory(project_root, relative_path=None, depth=1, include_ignored=False, max_entries=None):
    if max_entries is None:
        max_entries = MAX_EXPLORE_ENTRIES
    root = os.path.abspath(project_root)
    if not os.path.isdir(root):
        raise ValueError("پوشه پروژه پیدا نشد: " + str(project_root))
    target = resolve_project_path(root, relative_path)
    if not os.path.isdir(target):
        raise ValueError("پوشه پیدا نشد: " + (str(relative_path) if relative_path else "."))
    entries, truncated = scan_directory_entries(root, target, depth=depth, include_ignored=include_ignored, max_entries=max_entries)
    lines = []
    for entry in entries:
        indent = "  " * entry["depth"]
        if entry["type"] == "dir":
            count = entry["children_count"]
            suffix = " (" + str(count) + " items)" if count is not None else ""
            lines.append(indent + entry["name"] + "/" + suffix)
        else:
            size = entry["size"]
            suffix = " (" + str(size) + " bytes)" if size is not None else ""
            lines.append(indent + entry["name"] + suffix)
    if not entries:
        lines.append("(پوشه خالی است)")
    if truncated:
        lines.append("(... فهرست کامل نمایش داده نشد)")
    return {"path": relative_display(root, target), "full_path": target, "exists": True, "is_dir": True, "entries": entries, "count": len(entries), "truncated": truncated, "text": "\n".join(lines)}

def search_in_files(project_root, query, subdir=None, max_results=None, case_sensitive=False, max_file_bytes=None):
    if max_results is None:
        max_results = MAX_SEARCH_RESULTS
    if max_file_bytes is None:
        max_file_bytes = MAX_SEARCH_FILE_BYTES
    root = os.path.abspath(project_root)
    if not os.path.isdir(root):
        raise ValueError("پوشه پروژه پیدا نشد: " + str(project_root))
    needle = "" if query is None else str(query)
    if not needle.strip():
        raise ValueError("عبارت جستجو خالی است.")
    start = resolve_project_path(root, subdir) if subdir else root
    if os.path.isfile(start):
        files = [start]
    elif os.path.isdir(start):
        files = []
        for dir_path, dir_names, file_names in os.walk(start):
            dir_names[:] = [item for item in dir_names if item not in IGNORED_DIR_NAMES]
            for item in sorted(file_names):
                files.append(os.path.join(dir_path, item))
    else:
        raise ValueError("مسیر شروع جستجو پیدا نشد: " + str(subdir))
    haystack_needle = needle if case_sensitive else needle.lower()
    results = []
    for full_path in files:
        if is_sensitive_read_target(relative_display(root, full_path)):
            continue
        if not is_probably_text_file(full_path):
            continue
        try:
            if os.path.getsize(full_path) > max_file_bytes:
                continue
        except OSError:
            continue
        try:
            with open(full_path, "r", encoding="utf-8", newline="") as f:
                content = f.read()
        except (UnicodeDecodeError, OSError):
            continue
        for line_number, line in enumerate(content.splitlines(), start=1):
            haystack = line if case_sensitive else line.lower()
            if haystack_needle not in haystack:
                continue
            results.append({
                "relative_path": relative_display(root, full_path),
                "line": line_number,
                "text": mask_sensitive_content(line.strip()[:200]),
            })
            if len(results) >= max_results:
                return results
    return results

def read_files_multi(project_root, relative_paths, max_bytes=None):
    if max_bytes is None:
        max_bytes = MAX_TEXT_READ_BYTES
    root = os.path.abspath(project_root)
    results = []
    for relative_path in relative_paths or []:
        item = {
            "relative_path": str(relative_path).replace(os.sep, "/"),
            "full_path": None, "content": None, "size": None,
            "truncated": False, "error": None,
        }
        if is_sensitive_read_target(relative_path):
            item["error"] = sensitive_read_refusal_text(relative_path)
            results.append(item)
            continue
        try:
            full_path = resolve_project_path(root, relative_path)
            if not os.path.isfile(full_path):
                raise FileNotFoundError("فایل پیدا نشد: " + str(relative_path))
            item["full_path"] = full_path
            item["size"] = os.path.getsize(full_path)
            with open(full_path, "r", encoding="utf-8", newline="") as f:
                content = f.read()
            if len(content) > max_bytes:
                content = content[:max_bytes]
                item["truncated"] = True
            item["content"] = content
        except Exception as e:
            item["error"] = str(e)
        results.append(item)
    return results

def edit_file(project_root, relative_path, old_text, new_text, replace_all=False, return_details=False):
    if not old_text:
        raise ValueError("متن موردنظر برای ویرایش خالی است.")
    root = os.path.abspath(project_root)
    full_path = safe_path(root, relative_path)
    if not is_inside_project(root, full_path):
        raise ValueError("مسیر خارج از پروژه انتخاب‌شده است.")
    if not os.path.isfile(full_path):
        raise FileNotFoundError("فایل پیدا نشد: " + str(relative_path))
    with open(full_path, "r", encoding="utf-8", newline="") as f:
        current_content = f.read()
    occurrences = current_content.count(old_text)
    if occurrences == 0:
        raise ValueError("متن موردنظر در فایل پیدا نشد؛ هیچ تغییری اعمال نشد.")
    if occurrences > 1 and not replace_all:
        raise ValueError("متن موردنظر " + str(occurrences) + " بار در فایل تکرار شده است؛ برای جلوگیری از تغییر ناخواسته ویرایش انجام نشد.")
    if replace_all:
        new_content = current_content.replace(old_text, new_text)
        applied = occurrences
    else:
        new_content = current_content.replace(old_text, new_text, 1)
        applied = 1
    if new_content == current_content:
        result = {"operation": "edit", "relative_path": relative_path, "changed": False, "replacements": 0, "diff": "", "backup_path": None, "validation": None}
        if return_details:
            return result
        return full_path
    details = write_file(root, relative_path, new_content, return_details=True, action_type="edit")
    details["operation"] = "edit"
    details["replacements"] = applied
    if return_details:
        return details
    return full_path

__all__ = [
    'read_file','write_file','write_file_multi','edit_file','list_directory','read_files_multi','search_subfolders','search_in_files','create_backup',
    'build_unified_diff','compute_write_diff','remember_write_result','get_last_write_result','remember_write_multi_result','get_last_write_multi_result',
    'LAST_WRITE_RESULT','LAST_WRITE_MULTI_RESULT','MULTI_BACKUP_DIRNAME','_remove_empty_dirs','_rollback_multi_file','_get_timestamp'
]

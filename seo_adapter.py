from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

SEO_ROOT = Path(r"D:\AI\seo-module\claude-seo\tmp-claude-seo")
SCRIPTS = SEO_ROOT / "scripts"
PYTHON = Path(r"D:\AI\seo-module\claude-seo\test-env\Scripts\python.exe")

ALLOWED_SCRIPTS = frozenset({
    "fetch_page.py", "parse_html.py", "sitemap_discovery.py", "url_safety.py",
    "pagespeed_check.py",
})


@dataclass
class AdapterResult:
    ok: bool
    script: str
    exit_code: int
    data: Optional[Any]
    raw_stdout: str
    stderr: str
    error: Optional[str]
    duration_ms: int
    timed_out: bool


def _clean_env():
    env = dict(os.environ)
    for k in ("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env.setdefault("SEO_TRUSTED_PROXY_HOSTS", "127.0.0.1")
    return env


def run(script_name: str, args: list, timeout: int = 30) -> AdapterResult:
    if script_name not in ALLOWED_SCRIPTS:
        return AdapterResult(False, script_name, -1, None, "", "script not allowed", "script not in allowlist", 0, False)
    script_path = SCRIPTS / script_name
    env = _clean_env()
    start = time.time()
    try:
        if script_path.exists():
            cmd = [str(PYTHON), str(script_path)] + [str(a) for a in args]
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=str(SCRIPTS), env=env)
        else:
            git_proc = subprocess.Popen(["git", "-C", str(SEO_ROOT), "show", f"HEAD:scripts/{script_name}"], stdout=subprocess.PIPE)
            proc = subprocess.run([str(PYTHON), "-"] + [str(a) for a in args], stdin=git_proc.stdout, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=str(SCRIPTS), env=env)
            git_proc.stdout.close()
            git_proc.wait()
        dur = int((time.time() - start) * 1000)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        data = None
        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            data = None
        err = None
        if proc.returncode != 0:
            err = stderr.strip() or f"exit code {proc.returncode}"
        return AdapterResult(proc.returncode == 0, script_name, proc.returncode, data, stdout, stderr, err, dur, False)
    except subprocess.TimeoutExpired:
        dur = int((time.time() - start) * 1000)
        return AdapterResult(False, script_name, -1, None, "", "timeout", f"timeout after {timeout}s", dur, True)
    except Exception as e:
        dur = int((time.time() - start) * 1000)
        return AdapterResult(False, script_name, -1, None, "", str(e), str(e), dur, False)

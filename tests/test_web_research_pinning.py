"""
Focused tests for web_research DNS validation-to-connection pinning.

Run from project root:
    python tests\\test_web_research_pinning.py

All DNS (socket.getaddrinfo) and subprocess (curl) calls are mocked;
no live network access is required.
"""

import os
import sys
from unittest import mock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import web_research

results = []


def check(name, condition, detail=""):
    ok = bool(condition)
    results.append((name, ok))
    print("[{}] {}{}".format(
        "PASS" if ok else "FAIL",
        name,
        ("  <- " + str(detail)) if (detail and not ok) else "",
    ))
    return ok


def fake_addrinfo(*ips):
    infos = []
    for ip in ips:
        family = 10 if ":" in ip else 2
        infos.append((family, 1, 6, "", (ip, 0)))
    return infos


def fake_curl_result(body=b"<html>hello</html>", redirect_url=""):
    payload = (
        body
        + b"\n__CURL_META__\n200\ntext/html; charset=utf-8\n"
        + redirect_url.encode("utf-8") + b"\n"
    )

    class R:
        returncode = 0
        stderr = b""
        stdout = payload

    return R()


def run_fetch(url, ips, redirect_url="", body=b"<html>hello</html>",
              host_ips=None):
    calls = {"dns": 0, "cmds": [], "hosts": []}

    def fake_getaddrinfo(host, port, proto=0):
        calls["dns"] += 1
        calls["hosts"].append(host)
        per_host = host_ips.get(host, ips) if host_ips else ips
        return fake_addrinfo(*per_host)

    def fake_run(cmd, **kwargs):
        calls["cmds"].append(list(cmd))
        return fake_curl_result(body=body, redirect_url=redirect_url)

    with mock.patch.object(
        web_research.socket, "getaddrinfo", side_effect=fake_getaddrinfo
    ), mock.patch.object(
        web_research.subprocess, "run", side_effect=fake_run
    ), mock.patch.object(
        web_research, "_find_curl_path", return_value="curl.exe"
    ), mock.patch.dict(os.environ, {}, clear=False):
        for key in list(web_research._PROXY_ENV_VARS):
            os.environ.pop(key, None)
        try:
            out = web_research._guarded_fetch(url)
        except web_research.URLRejected as e:
            return ("rejected: %s" % e, calls)
        return (out, calls)


def assert_raises_blocked(name, url, ips):
    outcome, calls = run_fetch(url, ips)
    check(
        name,
        isinstance(outcome, str) and outcome.startswith("rejected: destination blocked"),
        outcome,
    )
    check(name + " (no subprocess)", calls["cmds"] == [], calls["cmds"])


# --- 1) Public IPv4 hostname ---------------------------------------------
outcome, calls = run_fetch("https://example.com/", ["93.184.215.14"])
check("ipv4: fetch succeeds", isinstance(outcome, tuple), outcome)
if calls["cmds"]:
    cmd = calls["cmds"][0]
    check(
        "ipv4: correct --resolve pin",
        "--resolve" in cmd and "example.com:443:93.184.215.14" in cmd,
        cmd,
    )
    check("ipv4: original hostname URL kept", cmd[-1] == "https://example.com/", cmd)
    check(
        "ipv4: TLS not disabled",
        "--insecure" not in cmd and "--proxy-insecure" not in cmd and "-k" not in cmd,
        cmd,
    )
    check("ipv4: no silent --noproxy", "--noproxy" not in cmd, cmd)
else:
    check("ipv4: curl invoked", False, calls)

# --- 2) Public IPv6 hostname ---------------------------------------------
V6 = "2606:2800:21f:cb07:6820:db35:1ec9:1357"
outcome, calls = run_fetch("https://ipv6.example.com/", [V6])
check("ipv6: fetch succeeds", isinstance(outcome, tuple), outcome)
if calls["cmds"]:
    cmd = calls["cmds"][0]
    check(
        "ipv6: correct unbracketed pin",
        "--resolve" in cmd and ("ipv6.example.com:443:" + V6) in cmd,
        cmd,
    )
    check("ipv6: hostname URL kept", cmd[-1] == "https://ipv6.example.com/", cmd)

# --- 3) Non-default port ---------------------------------------------------
outcome, calls = run_fetch("https://example.com:8443/", ["93.184.215.14"])
check("port: fetch succeeds", isinstance(outcome, tuple), outcome)
if calls["cmds"]:
    check(
        "port: pin uses 8443",
        "example.com:8443:93.184.215.14" in calls["cmds"][0],
        calls["cmds"][0],
    )

# --- 4) Blocked destinations (incl. Fake-IP) -------------------------------
assert_raises_blocked("fake-ip 198.18.0.19", "https://example.com/", ["198.18.0.19"])
assert_raises_blocked("fake-ip 10.10.34.36", "https://example.com/", ["10.10.34.36"])
assert_raises_blocked("loopback", "https://example.com/", ["127.0.0.1"])
assert_raises_blocked("link-local", "https://example.com/", ["169.254.1.1"])
assert_raises_blocked("multicast", "https://example.com/", ["224.0.0.0"])
assert_raises_blocked("unspec", "https://example.com/", ["0.0.0.0"])


# --- 5) Redirect to blocked destination ------------------------------------
outcome, calls = run_fetch(
    "https://example.com/", ["93.184.215.14"],
    redirect_url="http://169.254.169.254/latest",
    host_ips={"example.com": ["93.184.215.14"],
              "169.254.169.254": ["169.254.169.254"]},
)
check(
    "redirect-blocked: rejected",
    isinstance(outcome, str) and "destination blocked" in outcome,
    outcome,
)
check("redirect-blocked: one curl only", len(calls["cmds"]) == 1, calls["cmds"])
if len(calls["cmds"]) == 1:
    check(
        "redirect-blocked: blocked host never fetched",
        all("169.254.169.254" not in str(a) or a == "--resolve"
            or "169.254.169.254" not in a
            for a in calls["cmds"][0][:-1])
        and calls["cmds"][0][-1] == "https://example.com/",
        calls["cmds"][0],
    )

# --- 6) DNS rebinding: only validated pin used -----------------------------
# One validation per hop: the pinned snapshot is the validated snapshot,
# so even if system DNS later answers differently, app code performs no
# second target resolution and curl receives only the validated pin.
outcome, calls = run_fetch("https://example.com/", ["93.184.215.14"])
check("rebinding: one DNS validation for one hop", calls["dns"] == 1, calls)
if calls["cmds"]:
    check(
        "rebinding: curl pinned, no second resolution by app",
        "example.com:443:93.184.215.14" in calls["cmds"][0]
        and len(calls["cmds"]) == 1,
        calls["cmds"],
    )

# --- 7) Multiple valid answers: single pinned IP, no fallback --------------
outcome, calls = run_fetch(
    "https://example.com/", ["93.184.215.14", "93.184.216.14"]
)
check("multi-ip: fetch succeeds", isinstance(outcome, tuple), outcome)
if calls["cmds"]:
    pins = [c[i + 1] for c in calls["cmds"] for i, a in enumerate(c) if a == "--resolve"]
    check("multi-ip: exactly one pin", len(pins) == 1, calls["cmds"][0])
    check(
        "multi-ip: first sorted validated IP",
        pins and pins[0] == "example.com:443:93.184.215.14",
        pins,
    )

# --- 8) Proxy env: fail closed ----------------------------------------------
with mock.patch.object(
    web_research.socket, "getaddrinfo",
    side_effect=lambda *a, **k: fake_addrinfo("93.184.215.14"),
), mock.patch.object(
    web_research.subprocess, "run",
    side_effect=AssertionError("must not connect when proxied"),
), mock.patch.object(
    web_research, "_find_curl_path", return_value="curl.exe"
), mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy:8080"}):
    try:
        web_research._guarded_fetch("https://example.com/")
        check("proxy: fail closed", False, "no exception")
    except web_research.URLRejected as e:
        check("proxy: fail closed", "proxy" in str(e).lower(), e)

# --- 9) guard_url() API compatibility ----------------------------------------
with mock.patch.object(
    web_research.socket, "getaddrinfo",
    side_effect=lambda *a, **k: fake_addrinfo("93.184.215.14"),
):
    parts = web_research.guard_url("https://example.com/docs")
    check(
        "guard_url: returns parts (not tuple)",
        hasattr(parts, "hostname") and parts.hostname == "example.com",
        parts,
    )
with mock.patch.object(
    web_research.socket, "getaddrinfo",
    side_effect=lambda *a, **k: fake_addrinfo("198.18.0.19"),
):
    try:
        web_research.guard_url("https://example.com/")
        check("guard_url: still blocks Fake-IP", False, "no exception")
    except web_research.URLRejected as e:
        check("guard_url: still blocks Fake-IP", "destination blocked" in str(e), e)


def main():
    passed = sum(1 for _, ok in results if ok)
    print("\nRESULT: {}/{} passed".format(passed, len(results)))
    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

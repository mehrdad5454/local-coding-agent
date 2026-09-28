# Phase G - Website Analyst Layer (Read-Only v1)
# Pure read-only module: stdlib only, no writes, no backups,
# no subprocess, no mutation of user files.

import os


SENSITIVE_BASENAMES = (
    "wp-config.php",
    ".env",
)

SENSITIVE_SUFFIXES = (
    ".key",
    ".pem",
)

MAX_HTML_SCAN_BYTES = 200 * 1024
MAX_HTML_FILES_TO_SCAN = 20


def _is_sensitive_name(basename):
    name = (basename or "").lower()
    if name in SENSITIVE_BASENAMES:
        return True
    for suffix in SENSITIVE_SUFFIXES:
        if name.endswith(suffix):
            return True
    return False


def _safe_join(root, *parts):
    # Fail Closed: keep every access inside project root.
    root_abs = os.path.abspath(root)
    full = os.path.abspath(os.path.join(root_abs, *parts))
    try:
        if os.path.commonpath([full, root_abs]) != root_abs:
            return None
    except Exception:
        return None
    return full


def _file_metadata(full_path):
    try:
        if not full_path or not os.path.isfile(full_path):
            return {"exists": False, "size": None, "mtime": None}
        st = os.stat(full_path)
        return {
            "exists": True,
            "size": int(st.st_size),
            "mtime": float(st.st_mtime),
        }
    except Exception:
        return {"exists": None, "size": None, "mtime": None}


def _dir_exists(full_path):
    try:
        return bool(full_path and os.path.isdir(full_path))
    except Exception:
        return False


def _list_subdirs(full_path):
    # Only immediate folder names, sorted. No recursion.
    try:
        names = []
        with os.scandir(full_path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        names.append(entry.name)
                except Exception:
                    continue
        names.sort(key=lambda s: s.lower())
        return names
    except Exception:
        return []


def _read_text_head(full_path, limit=MAX_HTML_SCAN_BYTES):
    # Never called for sensitive files; caller must guard first.
    try:
        with open(full_path, "rb") as f:
            data = f.read(limit + 1)
        try:
            text = data[:limit].decode("utf-8", errors="strict")
        except Exception:
            return None
        return text.lower()
    except Exception:
        return None


def _collect_html_candidates(root_abs):
    # Bounded: top-level *.html/*.htm only, no recursion.
    candidates = []
    try:
        with os.scandir(root_abs) as it:
            for entry in it:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    name = (entry.name or "").lower()
                    if name.endswith((".html", ".htm")):
                        full = os.path.abspath(entry.path)
                        try:
                            if os.path.commonpath([full, root_abs]) != root_abs:
                                continue
                        except Exception:
                            continue
                        candidates.append(entry.name)
                except Exception:
                    continue
    except Exception:
        return []
    candidates.sort(key=lambda s: s.lower())
    return candidates[:MAX_HTML_FILES_TO_SCAN]

def analyze_website(project_root):
    result = {
        "cms": "unknown",
        "wordpress": {"is_wordpress": False, "signals": {}},
        "structure": {},
        "themes": {"count": 0, "items": [], "active": None},
        "plugins": {"count": 0, "items": []},
        "seo_signals": {},
        "sensitive_files": {},
        "issues": [],
        "recommendations": [],
    }
    try:
        if not project_root or not os.path.isdir(project_root):
            result["issues"].append("project root not found")
            result["recommendations"].append("pass a valid project path")
            return result
        root_abs = os.path.abspath(project_root)
        has_content = _dir_exists(_safe_join(root_abs, "wp-content"))
        has_admin = _dir_exists(_safe_join(root_abs, "wp-admin"))
        has_inc = _dir_exists(_safe_join(root_abs, "wp-includes"))
        cfg_meta = _file_metadata(_safe_join(root_abs, "wp-config.php"))
        signals = {
            "wp-content": has_content,
            "wp-admin": has_admin,
            "wp-includes": has_inc,
            "wp-config.php": bool(cfg_meta.get("exists")),
        }
        score = sum(1 for v in signals.values() if v is True)
        is_wp = bool(score >= 2 or (has_content and cfg_meta.get("exists")))
        result["wordpress"] = {"is_wordpress": is_wp, "signals": signals}
        result["cms"] = "wordpress" if is_wp else "unknown"
        themes_dir = _safe_join(root_abs, "wp-content", "themes")
        plugins_dir = _safe_join(root_abs, "wp-content", "plugins")
        uploads_dir = _safe_join(root_abs, "wp-content", "uploads")
        has_themes = _dir_exists(themes_dir) if has_content else False
        has_plugins = _dir_exists(plugins_dir) if has_content else False
        has_uploads = _dir_exists(uploads_dir) if has_content else False
        result["structure"] = {
            "wp-content": has_content,
            "wp-admin": has_admin,
            "wp-includes": has_inc,
            "wp-config.php": cfg_meta,
            "themes_dir": bool(has_themes),
            "plugins_dir": bool(has_plugins),
            "uploads_dir": bool(has_uploads),
        }
        theme_items = []
        if has_themes:
            for name in _list_subdirs(themes_dir):
                base = _safe_join(themes_dir, name)
                if not base:
                    continue
                theme_items.append({
                    "name": name,
                    "style.css": _file_metadata(
                        os.path.join(base, "style.css")).get("exists"),
                    "functions.php": _file_metadata(
                        os.path.join(base, "functions.php")).get("exists"),
                    "front-page.php": _file_metadata(
                        os.path.join(base, "front-page.php")).get("exists"),
                })
        result["themes"] = {
            "count": len(theme_items), "items": theme_items, "active": None,
        }
        plugin_items = []
        if has_plugins:
            for name in _list_subdirs(plugins_dir):
                plugin_items.append({"name": name})
        result["plugins"] = {"count": len(plugin_items), "items": plugin_items}
        return _analyze_website_tail(
            root_abs, result, is_wp, has_content, has_themes,
            has_plugins, has_uploads, cfg_meta)
    except Exception as e:
        try:
            result["issues"].append("analyzer error: " + str(e))
        except Exception:
            pass
        return result

def _analyze_website_tail(root_abs, result, is_wp, has_content,
                           has_themes, has_plugins, has_uploads, cfg_meta):
    robots_m = _file_metadata(_safe_join(root_abs, "robots.txt"))
    sm_m = _file_metadata(_safe_join(root_abs, "sitemap.xml"))
    wpsm_m = _file_metadata(_safe_join(root_abs, "wp-sitemap.xml"))
    html_files = _collect_html_candidates(root_abs)
    has_title = None
    has_h1 = None
    if html_files:
        ft = False
        fh = False
        for rel in html_files:
            full = _safe_join(root_abs, rel)
            if not full or _is_sensitive_name(os.path.basename(full)):
                continue
            head = _read_text_head(full)
            if head is None:
                continue
            if "<title" in head:
                ft = True
            if "<h1" in head:
                fh = True
            if ft and fh:
                break
        has_title = bool(ft)
        has_h1 = bool(fh)
    result["seo_signals"] = {
        "robots.txt": bool(robots_m.get("exists")),
        "sitemap.xml": bool(sm_m.get("exists")),
        "wp-sitemap.xml": bool(wpsm_m.get("exists")),
        "static_html_files_checked": html_files,
        "has_title_in_static_html": has_title,
        "has_h1_in_static_html": has_h1,
    }
    sens = {}
    sens["wp-config.php"] = cfg_meta
    sens[".env"] = _file_metadata(_safe_join(root_abs, ".env"))
    sens["extra_key_pem_files"] = _scan_root_keys(root_abs)
    result["sensitive_files"] = sens
    issues = []
    recs = []
    if is_wp:
        if not cfg_meta.get("exists"):
            issues.append("wp-config.php not found at project root")
            recs.append("verify WordPress root path; no change was made")
        if has_content and not has_themes:
            issues.append("wp-content exists but themes dir is missing")
        if has_content and not has_plugins:
            issues.append("wp-content exists but plugins dir is missing")
        if has_content and not has_uploads:
            issues.append("uploads dir not found under wp-content")
        if not robots_m.get("exists"):
            issues.append("robots.txt not found")
            recs.append("consider adding robots.txt (suggestion only)")
        if not (sm_m.get("exists") or wpsm_m.get("exists")):
            issues.append("no sitemap.xml / wp-sitemap.xml found")
            recs.append("consider enabling a sitemap (suggestion only)")
    else:
        issues.append("WordPress signals not detected")
        recs.append("if this is WordPress, check the project root")
    result["issues"] = issues
    if not recs:
        recs.append("no action required; read-only scan completed")
    result["recommendations"] = recs
    return result


def _scan_root_keys(root_abs):
    extra = []
    try:
        with os.scandir(root_abs) as it:
            for entry in it:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if entry.name in ("wp-config.php", ".env"):
                        continue
                    if _is_sensitive_name(entry.name):
                        full = os.path.abspath(entry.path)
                        try:
                            if os.path.commonpath(
                                [full, root_abs]) != root_abs:
                                continue
                        except Exception:
                            continue
                        m = _file_metadata(full)
                        extra.append({"name": entry.name,
                                      "size": m.get("size"),
                                      "mtime": m.get("mtime")})
                except Exception:
                    continue
    except Exception:
        return []
    extra.sort(key=lambda d: str(d.get("name", "")).lower())
    return extra[:20]


def format_website_report(data, project_name=None, project_root=None):
    try:
        data = data if isinstance(data, dict) else {}
        wp = data.get("wordpress") or {}
        signals = wp.get("signals") or {}
        struct = data.get("structure") or {}
        themes = data.get("themes") or {}
        plugins = data.get("plugins") or {}
        seo = data.get("seo_signals") or {}
        sens = data.get("sensitive_files") or {}
        issues = data.get("issues") or []
        recs = data.get("recommendations") or []
        lines = []
        lines.append("Website Analysis Report (Read-Only)")
        if project_name:
            lines.append("Project: " + str(project_name))
        if project_root:
            lines.append("Path: " + str(project_root))
        lines.append("")
        lines.append("## Environment")
        lines.append("- Type: " + str(data.get("cms", "unknown")))
        cms_line = "WordPress (signals detected)" if wp.get(
            "is_wordpress") else "unknown / not WordPress"
        lines.append("- CMS: " + cms_line)
        lines.append("- wp-content: " + str(signals.get("wp-content")))
        lines.append("- wp-admin: " + str(signals.get("wp-admin")))
        lines.append("- wp-includes: " + str(signals.get("wp-includes")))
        lines.append("- wp-config.php: " + str(signals.get("wp-config.php")))
        cfg = struct.get("wp-config.php")
        if isinstance(cfg, dict) and cfg.get("exists"):
            lines.append("  (wp-config metadata only: size="
                         + str(cfg.get("size")) + ", content never read)")
        lines.append("")
        lines.append("## Structure")
        lines.append("- themes_dir: " + str(struct.get("themes_dir")))
        lines.append("- plugins_dir: " + str(struct.get("plugins_dir")))
        lines.append("- uploads_dir: " + str(struct.get("uploads_dir")))
        titems = themes.get("items") or []
        lines.append("- Themes (" + str(themes.get("count", 0)) + "):")
        if not titems:
            lines.append("  (none found)")
        for t in titems[:50]:
            try:
                lines.append("  - " + str(t.get("name"))
                             + " [style.css=" + str(t.get("style.css"))
                             + ", functions.php=" + str(t.get("functions.php"))
                             + ", front-page.php="
                             + str(t.get("front-page.php")) + "]")
            except Exception:
                continue
        pitems = plugins.get("items") or []
        lines.append("- Plugins (" + str(plugins.get("count", 0)) + "):")
        if not pitems:
            lines.append("  (none found)")
        for p in pitems[:100]:
            try:
                lines.append("  - " + str(p.get("name")))
            except Exception:
                continue
        lines.append("")
        lines.append("## SEO Signals")
        lines.append("- robots.txt: " + str(seo.get("robots.txt")))
        lines.append("- sitemap.xml: " + str(seo.get("sitemap.xml")))
        lines.append("- wp-sitemap.xml: " + str(seo.get("wp-sitemap.xml")))
        lines.append("- static html checked: "
                     + str(seo.get("static_html_files_checked")))
        lines.append("- <title> seen: "
                     + str(seo.get("has_title_in_static_html")))
        lines.append("- <h1> seen: " + str(seo.get("has_h1_in_static_html")))
        lines.append("")
        lines.append("## Sensitive Files (metadata only, never read)")
        for key in ("wp-config.php", ".env"):
            meta = sens.get(key)
            if isinstance(meta, dict):
                lines.append("- " + str(key) + ": exists="
                             + str(meta.get("exists")) + ", size="
                             + str(meta.get("size")))
        extra = sens.get("extra_key_pem_files") or []
        if extra:
            for e in extra:
                try:
                    lines.append("- " + str(e.get("name")) + ": size="
                                 + str(e.get("size"))
                                 + " (content never read)")
                except Exception:
                    continue
        else:
            lines.append("- *.key/*.pem at root: none found")
        lines.append("")
        lines.append("## Potential Issues")
        if not issues:
            lines.append("- none detected")
        for i in issues:
            lines.append("- " + str(i))
        lines.append("")
        lines.append("## Recommendations (suggestions only, no changes made)")
        if not recs:
            lines.append("- none")
        for r in recs:
            lines.append("- " + str(r))
        lines.append("")
        lines.append("Note: read-only scan; no files created or modified.")
        return "\n".join(lines)
    except Exception as e:
        return ("Website Analysis Report (Read-Only)\n"
                "(error formatting report: " + str(e) + ")")



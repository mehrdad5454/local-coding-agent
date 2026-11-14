# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-11-14

### Added
- Pending Approval for mutating tools: a real pending action is created and the
  operation waits for the user's explicit approve/cancel before touching disk
- Approval popup (`#approvalModal`) in the web UI, driven by the new
  `pending_approval` / `pending_plan_id` / `pending_project` / `pending_tool` /
  `pending_files` / `pending_summary` keys on `/status`
- Regression tests for `project_root` resolution and emergency-stop wiring
  (57 tests total)
- Strengthened system prompt with professional coding assistant guidance
- Copy button rendered for every agent response in chat history
- Cross-project file fallback: when a requested file is not found in the
  selected project, other configured projects are scanned automatically
- Clear file-not-found error with project suggestion (e.g. "did you mean
  local-agent?")
- `format_search_results` defined locally in `agent.py` (no longer depends on
  `web_research` importability)
- `LICENSE` (MIT), `CONTRIBUTING.md`, `CHANGELOG.md` for GitHub release

### Changed
- Word-boundary project-name matching in `detect_project_from_request`
  (no false substring matches such as "agent" matching "agent-test")
- Explicit UI project selection now takes priority over auto-detection
- `read_file` / `read_files_multi` / `tool_read_file` normalize dict/list
  path arguments (`_normalize_path_arg`)
- Stronger system prompt for tool selection: `write_file` for a new file,
  `create_directory` for a new directory, extension-based disambiguation
  (`.txt`, `.py`, `.md`, ...) and a non-empty `content` requirement

### Fixed
- UI mode detection threw `TypeError: toLower is not a function` on every
  input, so Write mode was never sent to the server
- `project_root` was passed to the agentic loop as `None` instead of the
  resolved project root
- `NameError: is_emergency_stop_requested` in `agents/core_utils.py`, and
  `EMERGENCY_STOP_EVENT` unified so the core and `agents/tools/file_tools.py`
  share a single emergency-stop source
- Project detection ambiguity when `agent.py` exists in multiple projects
- `'dict' object has no attribute 'strip'` crash in `read_file`
- Search tools no longer fail with `NameError` (`format_search_results`)

## [0.1.0] - 2026-11-13

### Added
- Initial public release of Local Agent
- Core agent loop (Explore → Understand → Plan → Execute → Verify → Report)
- Tool registry with read/write/edit/list/search/backup tools
- Change-control spine: plan, approval, backup, rollback, journal/evidence
- Multi-agent architecture (orchestrator, specialists, capabilities, permissions)
- Durable memory layer (SQLite conversation store)
- Web research tools (search, page extraction) via ddgs/trafilatura
- Flask web UI with dark theme, chat history, live progress console
- 55 automated pytest tests

[Unreleased]: https://github.com/mehrdad5454/local-coding-agent/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/mehrdad5454/local-coding-agent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/mehrdad5454/local-coding-agent/releases/tag/v0.1.0
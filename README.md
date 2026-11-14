# Local Coding Agent

A local coding agent powered by Qwen3-8B and llama.cpp. No cloud, no API keys, runs entirely on your machine.

It ships with a web UI and a CLI, and focuses on change control (plan, approval, backup/rollback, journal/evidence) and multi-agent capabilities.

[Persian version](README.fa.md)

## Prerequisites
- **Python 3.14+** (developed and tested on Python 3.14.6)
- **llama.cpp** or any model server that exposes a text-completion endpoint
- At least **16GB RAM** (depends on the model; larger models need more RAM/VRAM)
- OS: Windows (the commands below are written for PowerShell/CMD)

## Installation
```powershell
# 1) create a virtual environment
python -m venv .venv

# 2) activate it (PowerShell)
.\.venv\Scripts\Activate.ps1
# or (CMD)
.\.venv\Scripts\activate.bat

# 3) install dependencies
pip install -r requirements.txt
```

## Model Setup
The project uses `MODEL_URL=http://127.0.0.1:8080/completion` by default (override it with the `MODEL_URL` environment variable).

Example `llama.cpp` server launch (replace paths and binary name for your system):
```powershell
# example: run llama-server with a GGUF model (e.g. Qwen3-8B)
llama-server `
  -m "D:\models\Qwen3-8B-Instruct-Q4_K_M.gguf" `
  --host 127.0.0.1 `
  --port 8080 `
  -c 8192
```
- You can use any compatible GGUF model; tune the context size (`-c`) and parameters to your hardware.
- After it starts, `http://127.0.0.1:8080/completion` must be reachable.

## Running
```powershell
# web interface (Flask)
python web_agent.py

# or the command-line mode
python agent.py
```

## Features
- **Pending Approval:** mutating tools build a plan first and wait for explicit user approval; nothing is written to disk until you approve.
- **Approval popup in the web UI:** when a plan is waiting, the approve/cancel dialog opens automatically, and the result plus Verify is shown after execution.
- **Live monitoring:** step-by-step view of the model's decision, the chosen tool and the approval state.
- **Emergency Stop:** one unified stop event for the core and the tools (shared `EMERGENCY_STOP_EVENT`).

## Testing
```powershell
pytest -q
```
(inside the virtual environment: `.\.venv\Scripts\python.exe -m pytest -q`)

## Project Structure
- `agent.py` — the agent core: tools, execution loop, safety, change planning/approval, backup/rollback, journal/verify.
- `web_agent.py` — the web interface for talking to the agent.
- `web_research.py` — read-only web tools (search and page text extraction).
- `agents/` — multi-agent architecture: orchestrator, specialist agents, capabilities/permissions, actions, completion gate.
- `evidence/` — temporary evidence store and the citation/evidence ledger.
- `memory/` — conversation memory layer (SQLite).
- `tests/` — automated pytest suite for evidence, routing, change safety and web scenarios.
- `general_workspace/` — isolated workspace for tasks in general mode (when used/enabled).
- `PROJECT_STATUS.md` — current project status report and decision reference.

## Contributing
See [CONTRIBUTING.md](CONTRIBUTING.md). Contributions are accepted through issues and pull requests.

## Changelog
Release history is tracked in [CHANGELOG.md](CHANGELOG.md).

## License
Released under the [MIT](LICENSE) license.

## Connect with Me
- [LinkedIn](https://www.linkedin.com/in/mehrdad-haghi-955525383/)
- [GitHub](https://github.com/mehrdad5454)
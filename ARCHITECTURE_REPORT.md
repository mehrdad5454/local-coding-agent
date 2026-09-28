# Technical Architecture Report: Local Agent → Multi-Agent Migration

## 1. Current Architecture Overview

### Entry Point & Launch Flow
- **`Start Local Agent.vbs`** — Single entry point that:
  1. Launches `llama-server.exe` (Qwen3-8B via llama.cpp at `http://127.0.0.1:8080/completion`)
  2. Starts `python web_agent.py` (Flask web UI on port 5001)

### Layer Breakdown
| Layer | File | Role |
|-------|------|------|
| UI | `web_agent.py` | Flask web server; manages project sessions, chat history (SQLite), threading; delegates to `agent.py` |
| Core Agent | `agent.py` | LLM interaction, file operations, safety phases, action journal, agentic loop |
| Phase G | `website_analyst.py` | Read-only website analysis (no writes) |
| Project Registry | `projects.json` | Maps project names → filesystem paths |
| Persistence | `action-journal.json` | Records all agent actions (loaded at import); `chat-history.db` | SQLite session cache |

### Control Flow (Single Agent)
```
User Request
  ↓
run_agent() — Phase A (Emergency Stop / Approval / Agentic Loop / Legacy)
  ↓
run_agent_legacy() — Phase B-F
  ├─ build_file_plan() — multi-file plan detection
  ├─ run_generic_task() — exploratory queries
  ├─ find_requested_file() / find_requested_directory()
  ├─ safe_path() — path traversal protection
  ├─ read_file() → mask_sensitive_content() (read-only mode)
  ├─ call_model() — LLM prompt for file edit
  ├─ extract_file_content() — parse BEGIN_FILE/END_FILE
  └─ write_file() — safe write with directory creation
  ↓
record_action() — append to action-journal.json
  ↓
remember_conversation_message() — cache in SQLite
```

### Key Functions Inventory
| Function | Purpose |
|----------|---------|
| `run_agent` | Top-level dispatcher; Phase A routing |
| `run_agent_legacy` | Original single-agent path (10k+ lines) |
| `run_agentic_loop` | Iterative tool-use loop with LLM planning |
| `build_file_plan` | Detects multi-file request patterns |
| `handle_pending_action_request` | Approval flow (Y/N) |
| `execute_pending_action` | Executes approved pending actions |
| `undo_last_mutating_action` | Revert via backup copy |
| `call_model` | HTTP POST to llama-server /completion endpoint |
| `safe_path` | Prevents path traversal outside project root |
| `read_file` / `write_file` | File I/O within project sandbox |
| `mask_sensitive_content` | Redacts API keys, passwords in read-only mode |
| `build_emergency_stop_report` | Generates stop status report |
| `record_action` | Logs action to JSON journal |
| `remember_conversation_message` | Stores in SQLite chat cache |

---

## 2. Data Flow Analysis

### User Request Flow
```
User → Web UI (web_agent.py) → run_agent() → LLM (call_model) → 
  File read → LLM edit generation → extract_file_content → 
  write_file → action-journal.json → SQLite cache → User
```

### LLM Interaction Pattern
- All LLM calls go through `call_model(prompt, context)` → HTTP POST to `localhost:8080/completion`
- No streaming (synchronous request/response)
- Single LLM endpoint; no model switching

### Data Storage
- **`action-journal.json`** — Loaded at import time (`ACTION_JOURNAL = load_action_journal()`). Append-only log of actions with timestamps.
- **`chat-history.db`** — SQLite, stores conversation messages per project for continuity
- **File backups** — `write_file` creates `*.bak` before overwriting (enables undo)

### State Management
- No formal state machine; relies on:
  - `stop_event` — threading.Event for emergency stop
  - `pending_action` — global for approval/rejection queue
  - Local variables in `run_agent_legacy` function scope

---

## 3. Current Safety & Governance

### Phase System (Safety Gates)
| Phase | Purpose |
|-------|---------|
| Phase A | Entry-point routing: emergency stop check, pending-action approval, agentic loop decision, legacy fallback |
| Phase B | Generic task handling (exploratory/analysis) |
| Phase C | Emergency stop detection — blocks new actions if stop is triggered |
| Phase D | Pending action approval workflow |
| Phase E | Agentic loop (iterative tool use) |
| Phase F | Legacy single-request processing |
| Phase G | Read-only website analysis (`website_analyst.py`) |

### Security Measures
- `safe_path()` — Prevents `../`, absolute paths, symlink escapes
- `mask_sensitive_content()` — Redacts secrets in read-only mode
- `is_write_allowed()` — Gate function for write operations
- Backup files (`*.bak`) created before every write
- `undo_last_mutating_action()` — Restores from backup
- Emergency stop — Can be triggered mid-execution via `stop_event`

### Governance
- Pending action queue requires explicit user approval (`Y`/`N`)
- Action journal provides audit trail
- All file operations confined to project root via `projects.json` mapping

---

## 4. Extensibility Analysis

### Current Extension Points
1. **`run_agentic_loop`** — Already supports iterative tool-use (best candidate for sub-agent spawning)
2. **`run_generic_task`** — Router for exploratory queries (could dispatch to Research agent)
3. **`call_model`** — Central LLM interface (could route to different prompts/agents)
4. **Global functions in `agent.py`** — All imported/available for internal calls

### Limitations
1. **Single LLM endpoint** — All calls go to same Qwen3-8B; no model routing
2. **No sub-agent framework** — No concept of agent-to-agent communication
3. **Synchronous execution** — No parallelism between agents
4. **Global state** — `pending_action`, `ACTION_JOURNAL` are module-level globals
5. **Huge `agent.py`** — 10,869 lines in single file; hard to modularize without refactoring

### Recommended Approach
- Keep `call_model`, `safe_path`, `read_file`, `write_file`, `action-journal` as shared utilities
- Introduce agent dispatch layer in `run_agent()` or `run_agentic_loop()`
- Each sub-agent gets specialized LLM prompt + tool access
- Shared `journal`, `sqlite`, and `stop_event` for cross-agent safety/governance

---

## 5. Multi-Agent Architecture Design

### Proposed Agent Roles

| Agent | Responsibility | LLM Prompt Focus | Key Tools |
|-------|----------------|------------------|-----------|
| **Orchestrator / Senior Agent** | Task decomposition, agent assignment, result synthesis | Planning, coordination, delegation | All tools (spawns sub-agents) |
| **Marketing Agent** | Content generation, copywriting, campaign planning, SEO | Creative writing, marketing strategy | read_file, write_file, web search |
| **Research Agent** | Code analysis, technical exploration, documentation | Code comprehension, pattern detection | read_file, grep, safe_path |
| **Opportunity Agent** | Market analysis, gap identification, feature prioritization | Strategic analysis, opportunity scoring | read_file, write_file, website analysis |

### Communication Model
```
User Request
  ↓
Orchestrator (Senior Agent)
  ├─ Analyzes request complexity
  ├─ Decomposes into sub-tasks
  ├─ Spawns sub-agents with:
  │     {task_description, context, tools_allowed, return_format}
  ├─ Collects results
  ├─ Synthesizes final output
  └─ Records actions in shared journal
```

### Agent Framework (Pseudocode)
```python
class Agent:
    def __init__(self, name, role_prompt, tools):
        self.name = name
        self.role_prompt = role_prompt
        self.tools = tools  # subset of available tools

    def run(self, task, context=None):
        prompt = f"{self.role_prompt}\n\nTASK: {task}\nCONTEXT: {context}"
        return call_model(prompt)

class OrchestratorAgent(Agent):
    def orchestrate(self, user_request):
        plan = self.plan_task(user_request)
        results = {}
        for sub_task in plan.sub_tasks:
            agent = self.assign_agent(sub_task.type)
            results[sub_task.id] = agent.run(sub_task.description, sub_task.context)
        return self.synthesize(results)
```

### Shared Services
| Service | Implementation | Used By |
|---------|----------------|---------|
| LLM Interface | `call_model()` in `agent.py` | All agents |
| File Operations | `read_file`, `write_file`, `safe_path` | Research, Marketing, Opportunity |
| Action Journal | `action-journal.json` + `record_action()` | All agents (append-only) |
| Chat Cache | `chat-history.db` + `remember_conversation_message()` | All agents |
| Emergency Stop | `stop_event` (threading.Event) | All agents |
| Approval Queue | `pending_action` + `handle_pending_action_request()` | All agents (for writes) |

---

## 6. Migration Roadmap

### Phase 1: Foundation (Agent Class + Dispatcher)
**Estimated: 2-3 days**
- Extract `Agent` base class from existing `agent.py` functions
- Create `agents/` package with:
  - `__init__.py` — agent registry
  - `base.py` — Agent, SubAgent classes
  - `orchestrator.py` — SeniorAgent (task decomposition)
  - `marketing.py` — MarketingAgent
  - `research.py` — ResearchAgent
  - `opportunity.py` — OpportunityAgent
- Add agent dispatch in `run_agent()` when user requests multi-agent processing

### Phase 2: Sub-Agent Implementation
**Estimated: 3-4 days**
- Implement each sub-agent with specialized prompts
- Wire sub-agents to shared tools (call_model, safe_path, file I/O, journal)
- Implement result aggregation in Orchestrator
- Test agent-to-agent handoff

### Phase 3: Safety Integration
**Estimated: 1-2 days**
- Ensure sub-agents respect `stop_event`
- Ensure sub-agents log to shared `action-journal.json`
- Ensure approval queue works across all agents

### Phase 4: UI Integration
**Estimated: 1-2 days**
- Update `web_agent.py` to show agent activity (which sub-agent is running)
- Add ability to interrupt individual sub-agents
- Display aggregated results

---

## 7. Migration Recommendations

1. **Start with `run_agent()` as the orchestrator entry point** — minimal disruption to existing flow
2. **Preserve all existing safety functions** — don't rewrite `safe_path`, `mask_sensitive_content`, etc.
3. **Use a flag to enable/disable multi-agent mode** — e.g., `use_multi_agent=True` parameter, defaults to False for backward compatibility
4. **Keep `agent.py` as the utilities/safety layer** — don't refactor the 10k lines prematurely
5. **New agent classes in separate files** — `agents/base.py`, `agents/orchestrator.py`, etc.
6. **Leverage `run_agentic_loop`** — existing iterative loop is the best foundation for sub-agent spawning

### Backward Compatibility Strategy
- `run_agent_legacy()` remains untouched as fallback
- New path: `run_agent()` → check if request is complex → `run_agentic_loop()` → optionally spawn sub-agents
- User can force legacy mode with a flag

### Risks & Mitigations
| Risk | Mitigation |
|------|-----------|
| Agent deadlock (circular delegation) | Timeout on sub-agent execution; max depth of 3 |
| Inconsistent journal format | Standardize `record_action()` calls across all agents |
| State corruption from globals | Pass context explicitly; minimize global usage in new code |
| Performance (multiple LLM calls) | Streamline prompts; cache intermediate results |

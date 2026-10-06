"""
AssistantAgent — First External Agent (Phase 3A).

Lives entirely outside agent.py and proves that a new agent can work
without enlarging or redesigning the Core:

- Uses ONLY injected read-only tools (call_model + read helpers).
- Never writes, never executes commands, never touches TOOL_REGISTRY.
- Never imports agent.py (no circular import; tools are injected).
- Output contract: propose() -> (answer, proposal_or_None)
  - answer is always plain text.
  - proposal, when present, is exactly the Core Proposal-v1 shape:
      {"files": [{"path": "<relative>", "content": "<full>"}]}
  - Detection reuses orchestrator.extract_agent_proposal.
- Execution stays in the Core: a proposal only becomes real via
  agent.handle_agent_batch_proposal. This agent never calls that path.

Not registered in AGENT_REGISTRY on purpose (Phase 3A scope).
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .base import Agent
from .capabilities import Capability
from .permissions import AgentPermissionModel
from .persona import PersonaRegistry


# Read-only tools this agent may receive. Subset of what run_agent injects
# into SeniorAgent; deliberately contains no mutation or command tool.
ASSISTANT_ALLOWED_TOOLS = frozenset(
    {
        "call_model",
        "read_file",
        "safe_path",
        "search_in_files",
        "is_write_allowed",
        "is_stop_requested",
        "record_action",
        "mask_sensitive_content",
    }
)


def _default_assistant_permissions() -> AgentPermissionModel:
    """Restrictive read-only permission model for the assistant agent."""
    return AgentPermissionModel(
        agent_name="assistant_agent",
        agent_type="assistant",
        allowed_capabilities={
            Capability.READ_FILE.value,
            Capability.ANALYSIS.value,
            Capability.REPORT.value,
        },
        allowed_tools=set(ASSISTANT_ALLOWED_TOOLS),
        restricted_tools={"write_file", "write_file_multi"},
    )


class AssistantAgent(Agent):
    """General-purpose external agent: Analyze -> Propose (never Execute)."""

    def __init__(
        self,
        tools: Optional[Dict[str, Any]] = None,
        stop_event=None,
        permissions: Optional[AgentPermissionModel] = None,
        persona_registry: Optional[PersonaRegistry] = None,
    ):
        super().__init__(
            name="assistant_agent",
            tools=tools,
            permissions=permissions or _default_assistant_permissions(),
            persona_registry=persona_registry,
        )
        self._shared_stop_event = stop_event
    def run(self, task: str, **kwargs) -> str:
        """Return the text answer (proposal dropped; ABC compatibility)."""
        answer, _proposal = self.propose(task, **kwargs)
        return answer

    def propose(
        self,
        task: str,
        project_name: str = "unknown",
        project_root: str = ".",
        allow_write: bool = False,
        stop_event=None,
        user_request: Optional[str] = None,
    ) -> Tuple[str, Optional[dict]]:
        """Generate an answer; separate a file-change proposal if present."""
        eff_stop = stop_event if stop_event is not None else self._shared_stop_event
        if eff_stop is not None and eff_stop.is_set():
            return ("AssistantAgent متوقف شد: حالت اضطراری فعال است.", None)

        call_model = self.get_tool("call_model")

        system_prompt = (
            "شما یک دستیار عمومی تحلیل و تولید متن هستید.\n"
            "فقط متن برگردانید؛ هیچ فایلی را تغییر ندهید.\n"
            "اگر تغییر فایل لازم است، آن را فقط به‌صورت بلوک BATCH_PROPOSAL با فرمت "
            '{"files": [{"path": "...", "content": "..."}]} اعلام کنید.'
        )
        user_context = (
            f"درخواست کاربر: {user_request if user_request is not None else task}\n"
            f"نام پروژه: {project_name}\n"
        )

        try:
            answer = call_model(system_prompt, user_context, no_think=True)
        except Exception:
            self._log_action(task, project_name, failed=True)
            raise

        self._log_action(task, project_name)

        # Single parser: the same contract the Core uses. Anything that does
        # not parse is ordinary text (Fail-Closed: no mutation).
        from .orchestrator import extract_agent_proposal

        proposal = extract_agent_proposal(answer)
        return (answer, proposal)

    def _log_action(
        self,
        task: str,
        project_name: str,
        failed: bool = False,
    ) -> None:
        """Attribute read-only delegation in the journal; never breaks run."""
        record_action = self.tools.get("record_action")
        if record_action is None:
            return
        failure_fields = (
            {"execution_state": "failed", "result_summary": "call_model failed"}
            if failed
            else {}
        )
        try:
            record_action(
                action_type="agent_delegation",
                tool="call_model",
                operation_class="read",
                args={"task": task[:200]},
                project=project_name,
                agent_id="assistant_agent",
                agent_type="sub-agent",
                **failure_fields,
            )
        except Exception:
            pass


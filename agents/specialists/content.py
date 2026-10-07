"""
ContentSpecialist - Phase 3C real specialist outside the Core.

Pattern:
- Built ONLY from a CoreAdapter (never raw Core callables, never agent.py).
- Contract: propose() -> (answer, proposal_or_None); run() -> str.
- Read-only: text via adapter.propose_text, reads via adapter.call_readonly.
- Never writes, never runs commands, never imports agent.py.
- A proposal only becomes real when the Core feeds it into its own
  handle_agent_batch_proposal path. This module never executes.

Not registered anywhere on purpose (3C proof scope, routing is future).
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ..base import Agent
from ..capabilities import Capability
from ..core_adapter import CoreAdapter
from ..permissions import AgentPermissionModel
from ..persona import PersonaRegistry


ALLOWED = frozenset(
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


def _perms() -> AgentPermissionModel:
    return AgentPermissionModel(
        agent_name="content_specialist",
        agent_type="assistant",
        allowed_capabilities={
            Capability.READ_FILE.value,
            Capability.ANALYSIS.value,
            Capability.REPORT.value,
        },
        allowed_tools=set(ALLOWED),
        restricted_tools={"write_file", "write_file_multi"},
    )


class ContentSpecialist(Agent):
    """Marketing/content specialist: Analyze -> Propose (never Execute)."""

    def __init__(
        self,
        adapter: CoreAdapter,
        stop_event=None,
        permissions: Optional[AgentPermissionModel] = None,
        persona_registry: Optional[PersonaRegistry] = None,
    ) -> None:
        super().__init__(
            name="content_specialist",
            tools=adapter.tools_for_agent(),
            permissions=permissions or _perms(),
            persona_registry=persona_registry,
        )
        self._adapter = adapter
        self._shared_stop_event = stop_event

    def run(self, task: str, **kwargs) -> str:
        answer, _proposal = self.propose(task, **kwargs)
        return answer

    def propose(
        self,
        task: str,
        project_name: str = "unknown",
        stop_event=None,
        user_request: Optional[str] = None,
    ) -> Tuple[str, Optional[dict]]:
        eff = stop_event if stop_event is not None else self._shared_stop_event
        if eff is not None and eff.is_set():
            return ("ContentSpecialist stopped.", None)
        system_prompt = (
            "You are a marketing copy specialist.\n"
            "Return text only; never change files.\n"
            "Normal answer: if no file has to be created or edited, reply "
            "with plain prose only.\n"
            "Proposal: if a file must be created or edited, your ENTIRE "
            "reply must be exactly one block that starts with the literal "
            "marker BATCH_PROPOSAL, immediately followed by a JSON object "
            "in this exact shape:\n"
            'BATCH_PROPOSAL: {"files": [{"path": "relative/path.ext", '
            '"content": "the FULL new file content"}]}\n'
            'The reply must start with the characters "BATCH_PROPOSAL: '
            '{" and nothing else - no prose, no code fences, no '
            "quotation marks around the block.\n"
            'Raw JSON without the "BATCH_PROPOSAL:" prefix is NOT a '
            "proposal and will be rejected. Path must stay inside the "
            "project. Content must be complete file content, not a "
            "description."
        )
        user_prompt = "Request: %s\nProject: %s\n" % (
            user_request if user_request is not None else task,
            project_name,
        )
        answer = self._adapter.propose_text(
            system_prompt, user_prompt, stop_event=eff
        )
        self._log(task, project_name)
        return (answer, self._adapter.prepare_proposal(answer))

    def _log(self, task: str, project_name: str) -> None:
        fn = self.tools.get("record_action")
        if fn is None:
            return
        try:
            fn(
                action_type="agent_delegation",
                tool="call_model",
                operation_class="read",
                args={"task": task[:200]},
                project=project_name,
                agent_id="content_specialist",
                agent_type="sub-agent",
            )
        except Exception:
            pass

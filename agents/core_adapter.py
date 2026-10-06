"""
CoreAdapter - Phase 3B thin bridge between external agents and the Core.

Lives entirely outside agent.py. Exactly three allowed duties:

1. propose_text()  -> text via injected call_model (Phase-2 wired path).
2. Read-only tools -> forward injected read-only helpers only.
3. submit_proposal() -> hand Proposal-v1 toward the Core path.
   The adapter NEVER executes: real mutation happens only inside
   agent.handle_agent_batch_proposal, which is never imported here.

Forbidden: write_file(s)/write_file_multi/run_command, direct
mutating TOOL_REGISTRY access, and any ``import agent``.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from .base import MUTATION_TOOL_NAMES
from .orchestrator import extract_agent_proposal, strip_mutation_tools


# Read-only / control helpers the Core injects into SeniorAgent today
# (agent.py run_agent tools={...}). Only these are forwarded.
READ_ONLY_TOOL_NAMES = frozenset(
    {
        "read_file",
        "safe_path",
        "search_in_files",
        "is_write_allowed",
        "is_stop_requested",
        "record_action",
        "mask_sensitive_content",
    }
)


class CoreAdapter:
    """Thin read-only bridge. Holds injected callables, owns no execution."""

    def __init__(
        self,
        tools: Optional[Dict[str, Any]] = None,
        stop_event=None,
        submit_handler: Optional[Callable[..., Any]] = None,
    ) -> None:
        # Defense in depth: strip mutation tools even if a future
        # caller accidentally passes one in.
        self._tools: Dict[str, Any] = strip_mutation_tools(dict(tools or {}))
        self._shared_stop_event = stop_event
        # Optional Core-owned executor (its handle_agent_batch_proposal),
        # injected by the Core in the future. Never imported here.
        self._submit_handler = submit_handler

    def propose_text(
        self,
        system_prompt: str,
        user_prompt: str,
        n_predict: Optional[int] = None,
        no_think: bool = True,
        stop_event=None,
    ) -> str:
        """Generate text via injected call_model. No transport logic here."""
        eff_stop = stop_event if stop_event is not None else self._shared_stop_event
        if eff_stop is not None and eff_stop.is_set():
            raise RuntimeError("Cancelled by user")
        call_model = self._tools.get("call_model")
        if call_model is None:
            raise RuntimeError("CoreAdapter: 'call_model' tool is not available")
        kwargs: Dict[str, Any] = {"no_think": no_think}
        if n_predict is not None:
            kwargs["n_predict"] = n_predict
        return call_model(system_prompt, user_prompt, **kwargs)

    def call_readonly(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Call one injected read-only helper. Mutation names are refused."""
        if name in MUTATION_TOOL_NAMES:
            raise PermissionError(
                "CoreAdapter never exposes mutation tool %r; "
                "the Core Loop owns all execution." % (name,)
            )
        if name == "call_model":
            raise PermissionError(
                "CoreAdapter: use propose_text() for the model."
            )
        fn = self._tools.get(name)
        if fn is None:
            raise KeyError("CoreAdapter: tool %r is not available" % (name,))
        return fn(*args, **kwargs)

    def tools_for_agent(self) -> Dict[str, Any]:
        """Sanitized copy of injected tools for constructing an Agent."""
        return dict(self._tools)

    def is_stopped(self, stop_event=None) -> bool:
        eff = stop_event if stop_event is not None else self._shared_stop_event
        return bool(eff is not None and eff.is_set())

    def prepare_proposal(self, answer_text: str) -> Optional[dict]:
        """Split a Proposal-v1 dict off model text (shared Core parser)."""
        return extract_agent_proposal(answer_text)

    def propose_with_text(
        self,
        system_prompt: str,
        user_prompt: str,
        stop_event=None,
    ) -> Tuple[str, Optional[dict]]:
        """One step: propose_text + prepare_proposal. Still no execution."""
        answer = self.propose_text(system_prompt, user_prompt, stop_event=stop_event)
        return (answer, self.prepare_proposal(answer))

    def submit_proposal(self, proposal: dict, **kwargs: Any) -> Any:
        """Hand a proposal toward the Core WITHOUT executing it here.

        If the Core injected its submit_handler (its own
        handle_agent_batch_proposal), delegate to it: execution then
        happens inside the Core, under the full Safety Spine.
        Otherwise return the proposal unchanged so the Core caller can
        feed it into its own path. Either way: no write, no approval,
        no journal of its own in this method.
        """
        if self._submit_handler is not None:
            return self._submit_handler(proposal, **kwargs)
        return proposal


def build_core_adapter(
    *,
    call_model,
    read_file=None,
    safe_path=None,
    search_in_files=None,
    is_write_allowed=None,
    is_stop_requested=None,
    record_action=None,
    mask_sensitive_content=None,
    stop_event=None,
    submit_handler: Optional[Callable[..., Any]] = None,
) -> CoreAdapter:
    """Core-side factory from the same callables run_agent injects today.

    Not wired into run_agent in this phase; provided for 3C/future use.
    """
    tools: Dict[str, Any] = {"call_model": call_model}
    if read_file is not None:
        tools["read_file"] = read_file
    if safe_path is not None:
        tools["safe_path"] = safe_path
    if search_in_files is not None:
        tools["search_in_files"] = search_in_files
    if is_write_allowed is not None:
        tools["is_write_allowed"] = is_write_allowed
    if is_stop_requested is not None:
        tools["is_stop_requested"] = is_stop_requested
    if record_action is not None:
        tools["record_action"] = record_action
    if mask_sensitive_content is not None:
        tools["mask_sensitive_content"] = mask_sensitive_content
    return CoreAdapter(tools=tools, stop_event=stop_event,
                       submit_handler=submit_handler)


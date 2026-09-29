"""
Senior Agent / Orchestrator for the multi-agent system.

Phase 2: Skeleton that receives requests and returns analysis.
Phase 3: Adds MarketingAgent delegation for marketing/content requests.
Phase 4: Adds ResearchAgent delegation for research/analysis requests.
Phase 5: Adds OpportunityAgent delegation for opportunity analysis requests.
Phase 6: Adds Permission Model as gatekeeper before delegation.

The SeniorAgent:
- Inherits from Agent (agents/base.py).
- Overrides run() to receive a user request.
- Detects request type and routes to registered sub-agents via AGENT_REGISTRY.
- Extensible: new agent types can be added by extending detection logic.

Integration:
- Uses tools passed from agent.py (call_model, safe_path, etc.)
- Respects the shared stop_event from the main agent thread.
- Does NOT perform writes directly — leaves that to sub-agents or legacy path.
- Does NOT duplicate existing infrastructure functions.

Phase 6: SeniorAgent now acts as a gatekeeper:
1. Understand request (keyword-based routing)
2. Select Agent (from AGENT_REGISTRY)
3. Check capability (via permission model)
4. Validate action (proposals are always analysis-only in Phase 5)
5. Delegate to sub-agent
6. Return result
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .base import Agent, MUTATION_TOOL_NAMES
from .marketing import MarketingAgent
from .research import ResearchAgent
from .opportunity import OpportunityAgent

# Phase 6: Permission models for each agent type
from .permissions import (
    AgentPermissionModel,
)

# Phase 7: Persona layer
from .persona import PersonaRegistry, create_default_registry


# Step 2 — Agent Proposal Contract.
#
# A specialized agent is "Analyze -> Propose". It NEVER writes and NEVER
# executes a tool that mutates. The only thing it may return instead of a
# plain answer is a PROPOSAL, in exactly this shape:
#
#   {"files": [{"path": "<relative path>", "content": "<full content>"}]}
#
# That shape is deliberately identical to the arguments of the existing
# `write_files` tool, so the Core Loop can feed a proposal into the Step 1
# batch path (validate_batch_args -> approval/autonomous -> Re-validation
# -> write_file_multi) without translating or re-implementing anything.
#
# Nothing is invented: a proposal exists only if the agent actually
# emitted this JSON. Everything else is treated as ordinary text.
PROPOSAL_KEY = "files"
MAX_PROPOSAL_FILES = 20
MAX_PROPOSAL_CONTENT_CHARS = 300 * 1024

# Step 2 — Mutation Boundary.
#
# The tool names live in agents/base.py (a plain constant, no dependencies)
# and are imported here so there is exactly one source of truth. They must
# never reach a specialized agent: the Core Loop in agent.py remains the
# single owner of every real mutation.


def strip_mutation_tools(tools):
    """
    Return a copy of `tools` with every mutation/command tool removed.

    Used by SeniorAgent so the boundary is enforced at the point where the
    agent is built, regardless of what the caller passed in. Read-only and
    control tools (call_model, read_file, safe_path, search_in_files,
    record_action, is_stop_requested, is_write_allowed, ...) are kept.
    """
    if not tools:
        return {}

    return {
        name: tool
        for name, tool in tools.items()
        if name not in MUTATION_TOOL_NAMES
    }


def extract_agent_proposal(text):
    """
    Return {"files": [...]} when an agent emitted a valid proposal.

    Returns None for ordinary prose. Fails closed: anything ambiguous,
    oversized, malformed, or without a usable path/content is rejected
    rather than guessed. This function performs no filesystem access and
    no project-boundary decision — validation of the paths belongs to the
    Core Loop's validate_batch_args.
    """
    if not text or not isinstance(text, str):
        return None

    candidates = []

    # Explicit, self-describing tag first (what the prompt asks for).
    for match in re.finditer(
        r"(?is)BATCH_PROPOSAL\s*[:\-]?\s*(\{.*?\})\s*(?:\n|$)", text
    ):
        candidates.append(match.group(1))

    # Otherwise: every well-formed JSON object in the text.
    candidates.extend(re.findall(r"(?s)\{.*?\}", text))

    for raw in candidates:
        try:
            payload = json.loads(raw)

        except Exception:
            continue

        if not isinstance(payload, dict):
            continue

        files = payload.get(PROPOSAL_KEY)

        if not isinstance(files, list) or not files:
            continue

        if len(files) > MAX_PROPOSAL_FILES:
            continue

        normalized = []

        for item in files:
            if not isinstance(item, dict):
                normalized = []
                break

            path = (
                item.get("path")
                or item.get("relative_path")
                or item.get("file")
            )
            content = item.get("content")

            if content is None:
                content = item.get("text")

            if not path or not isinstance(path, str):
                normalized = []
                break

            if content is None or not isinstance(content, str):
                normalized = []
                break

            if len(content) > MAX_PROPOSAL_CONTENT_CHARS:
                normalized = []
                break

            normalized.append({
                "path": path,
                "content": content,
            })

        if not normalized:
            continue

        return {PROPOSAL_KEY: normalized}

    return None


class SeniorAgent(Agent):
    """
    Orchestrator that routes requests to sub-agents based on content.

    Phase 2: Skeleton — returns analysis message.
    Phase 3: Delegates marketing/content requests to MarketingAgent.
    Phase 4: Delegates research/analysis requests to ResearchAgent.
    Future: Decomposes complex requests, manages multi-agent workflows.
    """

    def __init__(
        self,
        tools: Optional[Dict[str, Any]] = None,
        stop_event=None,
        persona_registry: Optional[PersonaRegistry] = None,
    ):
        # Step 2 — Mutation Boundary (defense in depth).
        #
        # Multi-Agent is "Analyze -> Propose" and nothing else. Any mutation
        # or command-execution tool is stripped here, so no future caller can
        # reintroduce a write path into an agent. Execution of changes belongs
        # exclusively to the Core Loop in agent.py, which keeps Emergency
        # Stop, Re-validation, Approval/Autonomous, backup/undo and the
        # journal in one place.
        tools = strip_mutation_tools(tools)

        super().__init__(
            name="senior_agent",
            tools=tools,
            persona_registry=persona_registry,
        )
        self._shared_stop_event = stop_event
        # Phase 7: Persona registry for resolving sub-agent personas.
        # If not provided, use the default registry.
        self.persona_registry: PersonaRegistry = persona_registry or create_default_registry()

    def run(self, task: str, **kwargs) -> str:
        """
        Receive a user request and orchestrate.

        Args:
            task (str): The user's request.
            **kwargs: Expected keys:
                - project_name (str)
                - project_root (str)
                - allow_write (bool)
                - stop_event (threading.Event)
                - user_request (str): The original full request from user.

        Returns:
            str: Orchestrator result.
        """
        project_name = kwargs.get("project_name", "unknown")
        project_root = kwargs.get("project_root", "")
        allow_write = kwargs.get("allow_write", False)
        stop_event = kwargs.get("stop_event") or self._shared_stop_event
        user_request = kwargs.get("user_request", task)

        # Check for emergency stop using the shared event.
        if self._is_stopped(stop_event):
            return (
                "درخواست توسط Senior Agent رد شد: حالت "
                "اضطراری فعال است."
            )

        # Phase 3: Check if this is a marketing content request.
        if self._is_marketing_request(user_request):
            return self._delegate_to_subagent(
                agent_type="marketing",
                task=user_request,
                project_name=project_name,
                project_root=project_root,
                allow_write=allow_write,
                stop_event=stop_event,
                user_request=user_request,
            )

        # Phase 4: Check if this is a research/analysis request.
        if self._is_research_request(user_request):
            return self._delegate_to_subagent(
                agent_type="research",
                task=user_request,
                project_name=project_name,
                project_root=project_root,
                allow_write=allow_write,
                stop_event=stop_event,
                user_request=user_request,
            )

        # Phase 5: Check if this is an opportunity analysis request.
        if self._is_opportunity_request(user_request):
            return self._delegate_to_subagent(
                agent_type="opportunity",
                task=user_request,
                project_name=project_name,
                project_root=project_root,
                allow_write=allow_write,
                stop_event=stop_event,
                user_request=user_request,
            )

        # Fallback: skeleton analysis for unrecognized requests.
        return self._analyze_request(
            task,
            project_name=project_name,
            project_root=project_root,
            allow_write=allow_write,
        )

    def propose(self, task: str, **kwargs):
        """
        Step 2: run the normal routing, then separate PROPOSAL from ANSWER.

        Returns (answer_text, proposal_or_None) where proposal has exactly
        the shape the Core Loop's `write_files` tool expects:
        {"files": [{"path": ..., "content": ...}]}.

        The agent still never executes anything. Validation, approval and
        execution stay in the Core Loop. If the delegated agent fails,
        the failure is contained here: no proposal is returned, so no
        mutation can result, and the caller falls back to its normal path.
        """
        try:
            answer = self.run(task, **kwargs)

        except Exception:
            # Agent failure must never produce a proposal and therefore
            # never produce a mutation.
            return None, None

        if not isinstance(answer, str):
            return answer, None

        return answer, extract_agent_proposal(answer)

    def _is_stopped(self, stop_event) -> bool:
        """Check if the shared stop_event is set."""
        if stop_event is None:
            return False
        return stop_event.is_set()

    def _is_marketing_request(self, user_request: str) -> bool:
        """
        Check if the user request is a marketing/content task.

        Uses keyword matching to determine if the request should be
        routed to MarketingAgent.
        """
        request_lower = user_request.lower()

        for trigger in MarketingAgent.TRIGGERS:
            if trigger.lower() in request_lower:
                return True

        return False

    def _is_research_request(self, user_request: str) -> bool:
        """
        Check if the user request is a research/analysis task.

        Uses keyword matching to determine if the request should be
        routed to ResearchAgent.

        Note: This is checked AFTER marketing, so requests that contain
        both "تبلیغاتی" (marketing) and "بررسی" (research) keywords
        will be routed to MarketingAgent first.
        """
        request_lower = user_request.lower()

        for trigger in ResearchAgent.TRIGGERS:
            if trigger.lower() in request_lower:
                return True

        # Phase 9 Section 2C: an explicit, delimited search phrase
        # ("...", `...`, «...») is by itself a research request, so the
        # Section 2B search capability is actually reachable. This reuses
        # ResearchAgent's own detection (no second parser) and adds NO
        # generic search keyword: without a delimited phrase nothing here
        # matches, so no project scan can ever be triggered by free text.
        #
        # Marketing is still checked first (unchanged), and this branch runs
        # before the opportunity check and before the skeleton fallback, so an
        # explicit search can never be swallowed by the generic route.
        if ResearchAgent._extract_explicit_search_phrase(user_request):
            return True

        return False

    def _is_opportunity_request(self, user_request: str) -> bool:
        """
        Check if the user request is an opportunity analysis task.

        Uses keyword matching to determine if the request should be
        routed to OpportunityAgent.

        This is checked AFTER marketing and research. The OpportunityAgent
        covers broad domains: Affiliate, Airdrop, Online Income, Digital
        Products, Freelancing, Remote work, Business opportunities, etc.

        Note: Some keywords like "تبلیغات" appear in both marketing and
        opportunity triggers. Marketing is checked first, so a request
        containing "تبلیغ" will route to MarketingAgent if it matches
        marketing triggers before reaching opportunity detection.
        """
        request_lower = user_request.lower()

        for trigger in OpportunityAgent.TRIGGERS:
            if trigger.lower() in request_lower:
                return True

        return False

    def _delegate_to_subagent(
        self,
        agent_type: str,
        task: str,
        project_name: str,
        project_root: str,
        allow_write: bool,
        stop_event,
        user_request: str,
    ) -> str:
        """
        Delegate a task to a registered sub-agent.

        Phase 3-5: Routes to Marketing, Research, or Opportunity agents.
        Phase 6: Adds permission validation before delegation.

        Security:
        - The sub-agent's permission model is validated before delegation.
        - Sub-agents cannot access tools they don't have permission for.
        - In Phase 5, all sub-agents are limited to read-only operations.
        """
        from . import AGENT_REGISTRY

        if agent_type not in AGENT_REGISTRY:
            return (
                f"SeniorAgent: sub-agent '{agent_type}' ثبت نشده است."
            )

        agent_class = AGENT_REGISTRY[agent_type]

        try:
            # Phase 6: Create the appropriate permission model for this agent
            permissions = self._get_permissions_for_agent(agent_type, agent_class)

            # Phase 6: Validate that the agent is permitted to run
            if not self._can_delegate_to(agent_type, agent_class):
                return (
                    f"SeniorAgent: ارسال به '{agent_type}' مجاز نیست."
                )

            sub_agent = agent_class(
                tools=self.tools,
                stop_event=stop_event,
                permissions=permissions,
                persona_registry=self.persona_registry,
            )

            # Phase 6: Verify the agent has the required capability
            required_cap = agent_type  # e.g., "marketing" → analysis capability
            if not sub_agent.has_capability("analysis"):
                # Fallback: still allow delegation, but log the concern
                # In Phase 6+, this could block delegation
                pass

            result = sub_agent.run(
                task,
                project_name=project_name,
                project_root=project_root,
                allow_write=allow_write,
                stop_event=stop_event,
                user_request=user_request,
            )

            return result

        except PermissionError as e:
            # Agent tried to access a restricted tool
            return self._analyze_request(
                task,
                project_name=project_name,
                project_root=project_root,
                allow_write=allow_write,
                error_note=f"دسترسی رد توسط Permission Model: {str(e)[:100]}",
            )

        except Exception:
            # If delegation fails, fall back to skeleton analysis.
            # Never break the request entirely.
            return self._analyze_request(
                task,
                project_name=project_name,
                project_root=project_root,
                allow_write=allow_write,
                error_note=f"خطا در delegate به {agent_type}",
            )

    def _get_permissions_for_agent(
        self,
        agent_type: str,
        agent_class: type,
    ) -> Optional[AgentPermissionModel]:
        """
        Get the permission model for a specific agent type.

        Phase 6: Each agent type has a predefined permission model.
        Phase 7: The mapping lives in AgentPermissionModel.for_agent_type(),
        so the permission model has a single source of truth (it is shared
        with the Agent Metadata layer). Behaviour is unchanged: unknown
        agent types still receive a restrictive default model.

        The `agent_class` parameter is kept for backwards compatibility.
        """
        return AgentPermissionModel.for_agent_type(agent_type)

    def _can_delegate_to(
        self,
        agent_type: str,
        agent_class: type,
    ) -> bool:
        """
        Check if SeniorAgent is permitted to delegate to this agent type.

        Phase 6: This is where future restrictions (e.g., disabling
        certain agent types for specific users) would be implemented.

        Currently: all registered agents are eligible for delegation.
        """
        from . import AGENT_REGISTRY
        return agent_type in AGENT_REGISTRY

    def _analyze_request(
        self,
        user_request: str,
        project_name: str,
        project_root: str,
        allow_write: bool,
        error_note: str = None,
    ) -> str:
        """
        Skeleton: analyze the request and return a placeholder.

        Used for:
        - Non-marketing requests (Phase 3)
        - Fallback when delegation fails (Phase 3)

        In Phase 4+, this will decompose complex requests into
        multiple sub-agent tasks and synthesize results.
        """
        lines = [
            "سیستم Multi-Agent فعال است (Phase 3).",
            f"پروژه: {project_name}",
            f"مسیر: {project_root}",
            f"حالت نوشتن: {'فعال' if allow_write else 'غیرفعال'}",
            f"درخواست کاربر: {user_request[:100]}",
        ]

        if error_note:
            lines.append(f"خطا: {error_note}")

        lines.append("")
        lines.append("(SeniorAgent درحالت skeleton — فقط marketing فعال است.)")

        if allow_write:
            lines.append(
                "توجه: درخواست شامل نوشتن است. در Phase 4+، "
                "SeniorAgent این کار را delegate خواهد کرد "
                "و از مکانیزم تأیید pending_action استفاده خواهد کرد."
            )

        return "\n".join(lines)

"""
Base Agent class for the multi-agent system.

This provides the foundation for all agents, including:
- name, tools, stop_event management
- Permission model (Phase 6)
- Action proposal support (Phase 6)

Phase 1: Base Agent class.
Phase 2: SeniorAgent / Orchestrator skeleton.
Phase 3: MarketingAgent.
Phase 4: ResearchAgent.
Phase 5: OpportunityAgent.
Phase 6: Capability & Permission system + Action Proposal framework.

All sub-agents inherit from this class and override `run()`.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from .permissions import AgentPermissionModel
from .persona import Persona, PersonaRegistry


class Agent(ABC):
    """
    Abstract base class for all agents in the multi-agent system.

    Attributes:
        name (str): Human-readable identifier (e.g. "marketing", "research").
        tools (dict): Mapping of tool name to callable. These are shared
            infrastructure functions from agent.py (call_model, safe_path,
            read_file, write_file, etc.). Sub-agents call them through
            self.tools['tool_name'].
        permissions (AgentPermissionModel): What this agent is allowed to do.
            Defaults to a restrictive model (read-only, no write).
        persona (Persona | None): Optional user-facing personality. When set,
            the agent prepends the persona's system prompt to its LLM calls.
        persona_registry (PersonaRegistry | None): If set, used to resolve
            persona for this agent if persona is not explicitly provided.
    """

    def __init__(
        self,
        name: str,
        tools: Optional[Dict[str, Any]] = None,
        permissions: Optional[AgentPermissionModel] = None,
        persona: Optional[Persona] = None,
        persona_registry: Optional[PersonaRegistry] = None,
    ):
        self.name = name
        self.tools: Dict[str, Any] = tools or {}
        self._stop_event = threading.Event()

        # Phase 6: Permission model.
        # If not provided, create a default permissive-for-read model.
        if permissions is None:
            permissions = AgentPermissionModel.default_for_agent_type(
                agent_name=name,
                agent_type=name.replace("_agent", ""),
            )
        self.permissions = permissions

        # Phase 7: Persona layer.
        # Resolve persona: explicit > registry lookup > None.
        if persona is not None:
            self.persona = persona
        elif persona_registry is not None:
            self.persona = persona_registry.get(name)
        else:
            self.persona = None

    @abstractmethod
    def run(self, task: str, **kwargs) -> str:
        """
        Execute the agent's task.

        Args:
            task (str): The specific task description.
            **kwargs: Additional context (project_root, user_request, etc.).

        Returns:
            str: The agent's result/summary.
        """
        ...

    def get_tool(self, tool_name: str) -> Any:
        """
        Retrieve a tool from the agent's tool set.

        Raises KeyError if the tool is not available, or PermissionError
        if the agent's permission model restricts it.
        """
        if not self.permissions.can_use_tool(tool_name):
            raise PermissionError(
                f"Agent '{self.name}' does not have permission to use tool '{tool_name}'"
            )
        return self.tools[tool_name]

    def has_capability(self, capability: str) -> bool:
        """
        Check if this agent has a specific capability.

        Args:
            capability: The capability name to check.

        Returns:
            True if the agent is permitted to use this capability.
        """
        return self.permissions.can_use_capability(capability)

    def can_access_tool(self, tool_name: str) -> bool:
        """Check if the agent's permission model allows a tool."""
        return self.permissions.can_use_tool(tool_name)

    def get_persona_prefix(self) -> str:
        """
        Get the persona-specific system prompt prefix for this agent.

        Returns:
            The persona's system_prompt string, or "" if no persona is set.
        """
        if self.persona is None:
            return ""
        return self.persona.system_prompt()

    def get_metadata(self):
        """
        Phase 7: Return the declarative metadata for this agent's type.

        Metadata is descriptive only — it does not enable anything. It is
        derived from the same permission model that governs this agent, so
        it can never claim a capability the agent does not have.

        Returns:
            AgentMetadata for this agent's type.
        """
        from .metadata import get_metadata_for_type

        agent_type = self.permissions.agent_type
        return get_metadata_for_type(agent_type)

    def stop(self) -> None:
        """Signal the agent to stop (respects Emergency Stop)."""
        self._stop_event.set()

    def is_stopped(self) -> bool:
        """Check if the agent has been signaled to stop."""
        return self._stop_event.is_set()

    def __repr__(self) -> str:
        return f"<Agent name={self.name!r}>"

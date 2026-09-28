"""
Permission Model for Multi-Agent Framework.

Phase 6: Introduces an AgentPermissionModel that allows each agent to declare
its allowed and restricted capabilities and tools. This is purely
declarative — it defines what an agent CAN and CANNOT do, but does not
enforce execution.

The permission model is used by SeniorAgent (Phase 6) as a gatekeeper
before delegation. In the current Phase 5 state, all sub-agents are
restricted to read-only operations.

Future phases will add:
- file_write: Requires user approval
- wallet_read: Requires user approval
- transaction_execute: Requires explicit multi-step approval
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Set

from .capabilities import Capability, get_agent_capabilities, validate_capability


@dataclass
class AgentPermissionModel:
    """
    Defines the permission boundaries for an agent.

    Attributes:
        agent_name (str): Identifier for this agent (e.g. "marketing_agent").
        agent_type (str): Type of agent ("marketing", "research", "opportunity", "senior").
        allowed_capabilities (set): Capabilities the agent is permitted to use.
        allowed_tools (set): Tools the agent can invoke (from Shared Core).
        restricted_capabilities (set): Capabilities explicitly forbidden.
        restricted_tools (set): Tools explicitly forbidden.
        requires_approval_for (set): Capabilities that require user approval
                                      before execution.
    """

    agent_name: str
    agent_type: str
    allowed_capabilities: Set[str] = field(default_factory=set)
    allowed_tools: Set[str] = field(default_factory=set)
    restricted_capabilities: Set[str] = field(default_factory=set)
    restricted_tools: Set[str] = field(default_factory=set)
    requires_approval_for: Set[str] = field(default_factory=set)

    def __post_init__(self):
        """Validate and populate defaults after initialization."""

        # Validate all capability strings
        all_caps = (
            self.allowed_capabilities
            | self.restricted_capabilities
            | self.requires_approval_for
        )
        for cap in all_caps:
            if not validate_capability(cap):
                raise ValueError(f"Unknown capability: {cap}")

        # If no capabilities specified, use defaults for this agent type
        if not self.allowed_capabilities:
            defaults = get_agent_capabilities(self.agent_type)
            self.allowed_capabilities = set(defaults)

        # Ensure no overlap between allowed and restricted
        overlap = self.allowed_capabilities & self.restricted_capabilities
        if overlap:
            raise ValueError(
                f"Capabilities cannot be both allowed and restricted: {overlap}"
            )

    def can_use_capability(self, capability: str) -> bool:
        """
        Check if an agent is permitted to use a capability.

        Args:
            capability: The capability to check.

        Returns:
            True if allowed, False if restricted or not in allowed set.
        """
        if not validate_capability(capability):
            return False

        if capability in self.restricted_capabilities:
            return False

        return capability in self.allowed_capabilities

    def can_use_tool(self, tool_name: str) -> bool:
        """
        Check if an agent is permitted to use a tool.

        Args:
            tool_name: The tool name to check.

        Returns:
            True if allowed, False if restricted or not in allowed set.
        """
        if tool_name in self.restricted_tools:
            return False

        return tool_name in self.allowed_tools

    def requires_approval(self, capability: str) -> bool:
        """
        Check if a capability requires user approval before execution.

        Args:
            capability: The capability to check.

        Returns:
            True if approval is required, False otherwise.
        """
        return capability in self.requires_approval_for

    def get_all_allowed(self) -> Set[str]:
        """Return all capabilities (allowed minus restricted)."""
        return self.allowed_capabilities - self.restricted_capabilities

    @classmethod
    def for_senior_agent(cls) -> "AgentPermissionModel":
        """Create a permission model for the SeniorAgent."""
        return cls(
            agent_name="senior_agent",
            agent_type="senior",
            allowed_capabilities={
                Capability.ANALYSIS.value,
            },
            allowed_tools={"call_model"},
            restricted_tools={"write_file", "write_file_multi", "safe_path"},
        )

    @classmethod
    def for_marketing_agent(cls) -> "AgentPermissionModel":
        """Create a permission model for the MarketingAgent."""
        return cls(
            agent_name="marketing_agent",
            agent_type="marketing",
            allowed_capabilities={
                Capability.ANALYSIS.value,
                Capability.REPORT.value,
            },
            allowed_tools={"call_model"},
            restricted_tools={
                "write_file",
                "write_file_multi",
                "safe_path",
                "read_file",
            },
        )

    @classmethod
    def for_research_agent(cls) -> "AgentPermissionModel":
        """Create a permission model for the ResearchAgent."""
        return cls(
            agent_name="research_agent",
            agent_type="research",
            allowed_capabilities={
                Capability.READ_FILE.value,
                Capability.ANALYSIS.value,
                Capability.REPORT.value,
            },
            allowed_tools={"call_model", "read_file", "safe_path"},
            restricted_tools={"write_file", "write_file_multi"},
        )

    @classmethod
    def for_opportunity_agent(cls) -> "AgentPermissionModel":
        """Create a permission model for the OpportunityAgent."""
        return cls(
            agent_name="opportunity_agent",
            agent_type="opportunity",
            allowed_capabilities={
                Capability.READ_FILE.value,
                Capability.ANALYSIS.value,
                Capability.REPORT.value,
                Capability.RESEARCH.value,
            },
            allowed_tools={"call_model", "read_file", "safe_path"},
            restricted_tools={"write_file", "write_file_multi"},
        )

    @classmethod
    def for_agent_type(
        cls,
        agent_type: str,
        agent_name: Optional[str] = None,
    ) -> "AgentPermissionModel":
        """
        Phase 7: Single entry point that answers "which permission model
        belongs to this agent type?".

        This is the ONE place that maps an agent type to a permission model.
        SeniorAgent and the Phase 7 Agent Metadata layer both use it, so the
        metadata can never drift away from the enforced permission model.

        Behaviour:
        - Known types return their predefined model (identical to calling
          the specific `for_<type>_agent()` factory directly).
        - Unknown types fall back to the restrictive default model, exactly
          as the previous inline map in SeniorAgent did.

        Args:
            agent_type: e.g. "marketing", "research", "opportunity", "senior".
                A trailing "_agent" suffix is tolerated and stripped.
            agent_name: Name to record in the model. Defaults to
                "<agent_type>_agent".

        Returns:
            AgentPermissionModel for the requested type.
        """
        normalized = (agent_type or "").strip().lower()

        if normalized.endswith("_agent"):
            normalized = normalized[: -len("_agent")]

        factories = {
            "senior": cls.for_senior_agent,
            "marketing": cls.for_marketing_agent,
            "research": cls.for_research_agent,
            "opportunity": cls.for_opportunity_agent,
        }

        factory = factories.get(normalized)
        if factory is not None:
            return factory()

        return cls.default_for_agent_type(
            agent_name=agent_name or (normalized + "_agent"),
            agent_type=normalized,
        )

    @classmethod
    def default_for_agent_type(cls, agent_name: str, agent_type: str) -> "AgentPermissionModel":
        """
        Create a default (restrictive) permission model for any agent type.

        By default, agents get read/analysis/report capabilities only.
        No future capabilities (write, wallet, browser, etc.) are granted.
        """
        from .capabilities import get_agent_capabilities

        default_caps = set(get_agent_capabilities(agent_type))

        return cls(
            agent_name=agent_name,
            agent_type=agent_type,
            allowed_capabilities=default_caps,
            allowed_tools={"call_model"},
            restricted_tools={"write_file", "write_file_multi"},
        )

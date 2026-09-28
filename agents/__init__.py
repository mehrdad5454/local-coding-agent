"""
Multi-Agent System for Local Agent.

This package contains the base Agent class and (in future phases)
all specialized sub-agents and the Senior Agent / Orchestrator.

Phase 1: Base Agent class.
Phase 2: SeniorAgent / Orchestrator skeleton added.
Phase 3: MarketingAgent (first specialized sub-agent) added.
Phase 4: ResearchAgent (second specialized sub-agent) added.
Phase 5: OpportunityAgent (third specialized sub-agent) added.
Phase 6: Capability System, Permission Model, Action Proposal framework added.
Phase 7: Preparation layer added (persona structure, LLM provider registry,
         agent metadata). Nothing in Phase 7 changes runtime behaviour or
         enables a new capability.

Phase 6+ additions:
- agents/capabilities.py: Capability enum and helpers
- agents/permissions.py: AgentPermissionModel
- agents/actions.py: ActionProposal framework

Phase 7 additions (all inert / declarative):
- agents/persona.py: Persona + PersonaRegistry (empty by default)
- agents/providers.py: LLMProviderSpec + PROVIDER_REGISTRY (not wired in)
- agents/metadata.py: AgentMetadata + AGENT_METADATA (descriptive only)
"""

from .base import Agent
from .orchestrator import SeniorAgent
from .marketing import MarketingAgent
from .research import ResearchAgent
from .opportunity import OpportunityAgent

# Phase 6 imports
from .capabilities import Capability
from .permissions import AgentPermissionModel
from .actions import (
    ActionProposal,
    ActionStatus,
    ActionType,
    create_analysis_proposal,
    is_proposal_safe,
    SAFE_ACTIONS,
    APPROVAL_REQUIRED_ACTIONS,
)

# Phase 7 imports: Persona Layer
from .persona import Persona, PersonaRegistry, create_default_registry, get_display_name

# Phase 7 imports: LLM Provider preparation (declarative, not wired in)
from .providers import (
    LLMProviderSpec,
    LLMStyle,
    PROVIDER_REGISTRY,
    DEFAULT_PROVIDER_ID,
    get_provider,
    list_providers,
    default_provider,
    resolve_endpoint,
    build_payload as build_llm_payload,
    build_headers as build_llm_headers,
    is_available as is_provider_available,
    describe_providers,
)

# Phase 7 imports: Agent Metadata (descriptive only)
from .metadata import (
    AgentMetadata,
    AGENT_METADATA,
    get_metadata_for_type,
    list_agent_metadata,
    describe_agents,
    validate_metadata,
    validate_metadata_consistency,
    PERMISSION_READ,
    PERMISSION_NO_READ,
    PERMISSION_WRITE,
    PERMISSION_NO_WRITE,
    PERMISSION_APPROVAL_REQUIRED,
)

__all__ = [
    # Core
    "Agent",
    "SeniorAgent",
    "AGENT_REGISTRY",

    # Sub-Agents
    "MarketingAgent",
    "ResearchAgent",
    "OpportunityAgent",

    # Phase 6: Capability & Permission System
    "Capability",
    "AgentPermissionModel",

    # Phase 6: Action Proposal
    "ActionProposal",
    "ActionStatus",
    "ActionType",
    "create_analysis_proposal",
    "is_proposal_safe",
    "SAFE_ACTIONS",
    "APPROVAL_REQUIRED_ACTIONS",

    # Phase 7: Persona Layer
    "Persona",
    "PersonaRegistry",
    "create_default_registry",
    "get_display_name",

    # Phase 7: LLM Provider preparation (declarative, not wired in)
    "LLMProviderSpec",
    "LLMStyle",
    "PROVIDER_REGISTRY",
    "DEFAULT_PROVIDER_ID",
    "get_provider",
    "list_providers",
    "default_provider",
    "resolve_endpoint",
    "build_llm_payload",
    "build_llm_headers",
    "is_provider_available",
    "describe_providers",

    # Phase 7: Agent Metadata (descriptive only)
    "AgentMetadata",
    "AGENT_METADATA",
    "get_metadata_for_type",
    "list_agent_metadata",
    "describe_agents",
    "validate_metadata",
    "validate_metadata_consistency",
    "PERMISSION_READ",
    "PERMISSION_NO_READ",
    "PERMISSION_WRITE",
    "PERMISSION_NO_WRITE",
    "PERMISSION_APPROVAL_REQUIRED",
]


# Registry for specialized sub-agents.
# SeniorAgent uses this to discover and instantiate sub-agents.
# To add a new agent: just add an import above and a line here.
#
# Future agents (Phase 5+):
#   - "content_planner": ContentPlannerAgent
#   - "seo": SEOAgent
#   - "code_reviewer": CodeReviewerAgent
#   - etc.
AGENT_REGISTRY = {
    "marketing": MarketingAgent,
    "research": ResearchAgent,
    "opportunity": OpportunityAgent,
}

"""
Agent Metadata Layer — Phase 7 (declarative only).

Purpose:
    Give every agent a simple, machine-readable description:
        id, role, capabilities, permissions
    so future phases (UI, routing, permissions review, documentation) can
    inspect agents without reading agent internals.

Example:
    MarketingAgent ->
        id: "marketing"
        role: "Marketing Content Specialist"
        capabilities: ("analysis", "report")
        permissions: ("no_read", "no_write")

Phase 7 rules honoured by this module:
    - METADATA ONLY. Nothing here is executed and nothing is enabled.
      Declaring a capability in this file does NOT grant it.
    - Single source of truth: capabilities/permissions are DERIVED from
      AgentPermissionModel.for_agent_type(), which is also what SeniorAgent
      uses for enforcement. Metadata can therefore never drift from the
      enforced model (validate_metadata_consistency() proves this).
    - No agent gains a future capability (write / wallet / browser /
      transaction / api_access). This is asserted by the regression check.
    - No new dependency: standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .capabilities import Capability, validate_capability
from .permissions import AgentPermissionModel


# ---------------------------------------------------------------------------
# Permission vocabulary (descriptive labels for metadata only).
# ---------------------------------------------------------------------------

PERMISSION_READ = "read"
PERMISSION_NO_READ = "no_read"
PERMISSION_WRITE = "write"
PERMISSION_NO_WRITE = "no_write"
PERMISSION_APPROVAL_REQUIRED = "approval_required"

KNOWN_PERMISSIONS: frozenset = frozenset(
    {
        PERMISSION_READ,
        PERMISSION_NO_READ,
        PERMISSION_WRITE,
        PERMISSION_NO_WRITE,
        PERMISSION_APPROVAL_REQUIRED,
    }
)

# Tool classification used to derive permissions from a permission model.
READ_TOOLS: frozenset = frozenset({"read_file"})
WRITE_TOOLS: frozenset = frozenset({"write_file", "write_file_multi"})


# ---------------------------------------------------------------------------
# Human-readable role / description text (ASCII only, display metadata).
# ---------------------------------------------------------------------------

AGENT_ROLES: Dict[str, str] = {
    "senior": "Orchestrator and Safety Gatekeeper",
    "marketing": "Marketing Content Specialist",
    "research": "Research and Analysis Specialist",
    "opportunity": "Opportunity Analyst",
}

AGENT_DESCRIPTIONS: Dict[str, str] = {
    "senior": (
        "Receives the user request, applies the safety gates, selects a "
        "sub-agent and delegates. Analysis-only."
    ),
    "marketing": (
        "Generates marketing copy (headlines, landing page text, product "
        "descriptions) through the shared LLM tool. Text output only."
    ),
    "research": (
        "Answers questions and analyses project files in read-only mode "
        "using the shared LLM and read tools."
    ),
    "opportunity": (
        "Analyses and compares income opportunities (affiliate, airdrop, "
        "digital products, freelancing) in read-only mode."
    ),
}


@dataclass(frozen=True)
class AgentMetadata:
    """
    Declarative description of an agent.

    Attributes:
        id (str): Stable ASCII identifier (registry key, e.g. "marketing").
        agent_name (str): Runtime agent name (e.g. "marketing_agent").
        role (str): Short human-readable role.
        capabilities (tuple): Capability identifiers this agent MAY use.
        permissions (tuple): Descriptive permission labels
            (read / no_read / write / no_write / approval_required).
        allowed_tools (tuple): Tools from the shared core it may call.
        restricted_tools (tuple): Tools that are explicitly forbidden.
        requires_approval_for (tuple): Capabilities that need user approval
            before execution (empty for all agents in Phase 7).
        description (str): Longer human-readable summary.
    """

    id: str
    agent_name: str
    role: str
    capabilities: Tuple[str, ...]
    permissions: Tuple[str, ...]
    allowed_tools: Tuple[str, ...] = ()
    restricted_tools: Tuple[str, ...] = ()
    requires_approval_for: Tuple[str, ...] = ()
    description: str = ""

    def has_capability(self, capability: str) -> bool:
        """True if this agent declares the given capability."""
        return capability in self.capabilities

    def has_permission(self, permission: str) -> bool:
        """True if this agent declares the given permission label."""
        return permission in self.permissions

    def can_use_tool(self, tool_name: str) -> bool:
        """
        True if the metadata says the tool may be used.

        A tool is usable when it is explicitly allowed and not restricted.
        """
        return tool_name in self.allowed_tools and tool_name not in self.restricted_tools

    def is_read_only(self) -> bool:
        """True if the agent can neither write nor use a write tool."""
        if self.has_permission(PERMISSION_WRITE):
            return False
        return not (set(self.allowed_tools) & WRITE_TOOLS)

    def to_dict(self) -> Dict[str, object]:
        """Return a JSON-serialisable dict (tuples become lists)."""
        return {
            "id": self.id,
            "agent_name": self.agent_name,
            "role": self.role,
            "capabilities": list(self.capabilities),
            "permissions": list(self.permissions),
            "allowed_tools": list(self.allowed_tools),
            "restricted_tools": list(self.restricted_tools),
            "requires_approval_for": list(self.requires_approval_for),
            "description": self.description,
            "read_only": self.is_read_only(),
        }


def derive_permissions(model: AgentPermissionModel) -> Tuple[str, ...]:
    """
    Derive descriptive permission labels from an AgentPermissionModel.

    Rules (deterministic, no guesswork):
    - "read"     : a read tool is explicitly allowed.
    - "no_read"  : read tools are explicitly restricted.
    - "write"    : a write tool is explicitly allowed.
    - "no_write" : write tools are not granted (default for Phase 7).
    - "approval_required" : the model declares capabilities that need approval.

    Returns:
        Sorted tuple of permission labels.
    """
    permissions = set()

    allowed = set(model.allowed_tools)
    restricted = set(model.restricted_tools)

    if allowed & READ_TOOLS:
        permissions.add(PERMISSION_READ)
    elif restricted & READ_TOOLS:
        permissions.add(PERMISSION_NO_READ)

    if allowed & WRITE_TOOLS:
        permissions.add(PERMISSION_WRITE)
    else:
        permissions.add(PERMISSION_NO_WRITE)

    if model.requires_approval_for:
        permissions.add(PERMISSION_APPROVAL_REQUIRED)

    return tuple(sorted(permissions))


def build_metadata(
    agent_type: str,
    model: Optional[AgentPermissionModel] = None,
) -> AgentMetadata:
    """
    Build metadata for an agent type.

    Capabilities and permissions come from the enforced permission model
    (AgentPermissionModel.for_agent_type), so metadata stays truthful.

    Args:
        agent_type: e.g. "marketing", "research", "opportunity", "senior".
        model: Optional pre-built permission model (mainly for tests).

    Returns:
        AgentMetadata (an unknown agent type gets the deny-all default:
        no capabilities, no write tools).
    """
    normalized = (agent_type or "").strip().lower()
    if normalized.endswith("_agent"):
        normalized = normalized[: -len("_agent")]

    if model is None:
        model = AgentPermissionModel.for_agent_type(normalized)

    return AgentMetadata(
        id=normalized,
        agent_name=model.agent_name,
        role=AGENT_ROLES.get(normalized, "Unclassified Agent"),
        capabilities=tuple(sorted(model.allowed_capabilities)),
        permissions=derive_permissions(model),
        allowed_tools=tuple(sorted(model.allowed_tools)),
        restricted_tools=tuple(sorted(model.restricted_tools)),
        requires_approval_for=tuple(sorted(model.requires_approval_for)),
        description=AGENT_DESCRIPTIONS.get(normalized, ""),
    )


# Pre-built metadata for the agents that exist today (Phase 7).
# SeniorAgent is included: it is the orchestrator, not a sub-agent.
AGENT_METADATA: Dict[str, AgentMetadata] = {
    "senior": build_metadata("senior"),
    "marketing": build_metadata("marketing"),
    "research": build_metadata("research"),
    "opportunity": build_metadata("opportunity"),
}


def get_metadata_for_type(agent_type: str) -> AgentMetadata:
    """
    Return metadata for an agent type.

    Known types return the pre-built entry; unknown types are described
    on the fly with the restrictive default permission model.
    """
    normalized = (agent_type or "").strip().lower()
    if normalized.endswith("_agent"):
        normalized = normalized[: -len("_agent")]

    prebuilt = AGENT_METADATA.get(normalized)
    if prebuilt is not None:
        return prebuilt

    return build_metadata(normalized)


def list_agent_metadata() -> Dict[str, AgentMetadata]:
    """Return a copy of the pre-built metadata registry (id -> metadata)."""
    return dict(AGENT_METADATA)


def describe_agents() -> str:
    """Return a multi-line text report of all registered agent metadata."""
    lines = ["Agent metadata:"]
    for agent_id in sorted(AGENT_METADATA):
        meta = AGENT_METADATA[agent_id]
        lines.append(f"- {meta.id} ({meta.agent_name})")
        lines.append(f"    role: {meta.role}")
        lines.append(f"    capabilities: {', '.join(meta.capabilities) or '-'}")
        lines.append(f"    permissions: {', '.join(meta.permissions) or '-'}")
        lines.append(
            "    tools: allowed="
            f"{', '.join(meta.allowed_tools) or '-'} | restricted="
            f"{', '.join(meta.restricted_tools) or '-'}"
        )
        lines.append(
            "    read_only: " + ("yes" if meta.is_read_only() else "no")
        )
    return "\n".join(lines)



def validate_metadata(
    meta: AgentMetadata,
    model: Optional[AgentPermissionModel] = None,
) -> List[str]:
    """
    Validate a single metadata entry.

    Checks:
    - id / agent_name / role are non-empty,
    - every capability exists in the Capability enum,
    - every permission label is from the known vocabulary,
    - "read" and "no_read" are not both present,
    - "write" and "no_write" are not both present,
    - the metadata matches the enforced permission model,
    - the agent is read-only (no future capability is granted).

    Returns:
        List of problem descriptions (empty list == valid).
    """
    issues: List[str] = []

    if not meta.id:
        issues.append("metadata id is empty")
    if not meta.agent_name:
        issues.append(f"[{meta.id}] agent_name is empty")
    if not meta.role:
        issues.append(f"[{meta.id}] role is empty")

    for capability in meta.capabilities:
        if not validate_capability(capability):
            issues.append(f"[{meta.id}] unknown capability '{capability}'")

    for permission in meta.permissions:
        if permission not in KNOWN_PERMISSIONS:
            issues.append(f"[{meta.id}] unknown permission '{permission}'")

    if PERMISSION_READ in meta.permissions and PERMISSION_NO_READ in meta.permissions:
        issues.append(f"[{meta.id}] permissions contain both 'read' and 'no_read'")
    if (
        PERMISSION_WRITE in meta.permissions
        and PERMISSION_NO_WRITE in meta.permissions
    ):
        issues.append(f"[{meta.id}] permissions contain both 'write' and 'no_write'")

    # Safety invariant: no agent may declare a future capability in Phase 7.
    future_capabilities = Capability.get_future_capabilities()
    granted_future = set(meta.capabilities) & future_capabilities
    if granted_future:
        issues.append(
            f"[{meta.id}] future capability granted: {sorted(granted_future)}"
        )

    if not meta.is_read_only():
        issues.append(f"[{meta.id}] agent is not read-only")

    # Consistency with the enforced permission model.
    if model is None:
        model = AgentPermissionModel.for_agent_type(meta.id)

    if set(meta.capabilities) != set(model.allowed_capabilities):
        issues.append(
            f"[{meta.id}] capabilities differ from permission model: "
            f"metadata={sorted(meta.capabilities)} "
            f"model={sorted(model.allowed_capabilities)}"
        )

    if set(meta.allowed_tools) != set(model.allowed_tools):
        issues.append(f"[{meta.id}] allowed_tools differ from permission model")

    if set(meta.restricted_tools) != set(model.restricted_tools):
        issues.append(f"[{meta.id}] restricted_tools differ from permission model")

    return issues


def validate_metadata_consistency() -> List[str]:
    """
    Validate the whole metadata registry against the agent registry.

    Checks:
    - every registered sub-agent (AGENT_REGISTRY) has metadata,
    - every metadata entry corresponds to a registered agent (plus the
      orchestrator, which is intentionally included),
    - every entry passes validate_metadata().

    Returns:
        List of problem descriptions (empty list == valid).
    """
    from . import AGENT_REGISTRY  # lazy: avoids an import cycle

    issues: List[str] = []

    for agent_type in sorted(AGENT_REGISTRY):
        if agent_type not in AGENT_METADATA:
            issues.append(f"registered agent '{agent_type}' has no metadata")

    allowed_ids = set(AGENT_REGISTRY) | {"senior"}
    for agent_id, meta in AGENT_METADATA.items():
        if agent_id not in allowed_ids:
            issues.append(f"metadata '{agent_id}' has no registered agent")
        issues.extend(validate_metadata(meta))

    return issues

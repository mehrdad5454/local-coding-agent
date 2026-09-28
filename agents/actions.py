"""
Action Proposal Framework for Multi-Agent System.

Phase 6: Introduces ActionProposal — a structured representation of an
action that an agent proposes to take. This is the bridge between LLM
output and the Safety/Approval layer.

Current state (Phase 5): Sub-Agents only return text from call_model.
They do NOT produce ActionProposals yet. This framework prepares the
architecture for when sub-agents need to propose actions that require
approval (e.g., file writes, external API calls).

Future use (Phase 6+):
- Sub-Agents will return ActionProposals alongside their text output
- SeniorAgent will validate proposals against the agent's permission model
- If approval is needed, the proposal goes through pending_action → user approval
- Then re-validation → execution
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ActionStatus(Enum):
    """Status of an action proposal through the pipeline."""
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    BLOCKED = "blocked"
    EXPIRED = "expired"


class ActionType(Enum):
    """
    Types of actions that can be proposed.

    Phase 5 (current): Only analysis/report — these do NOT require
    approval because they don't modify state.

    Future (Phase 6+):
    - FILE_WRITE: Requires approval + re-validation
    - WALLET_ACTION: Requires approval + simulated validation
    - EXTERNAL_API: Requires approval + re-validation
    - BROWSER_ACTION: Requires approval
    """

    # Current (no approval needed)
    ANALYSIS = "analysis"
    REPORT = "report"
    RESEARCH = "research"

    # Future (approval needed)
    FILE_WRITE = "file_write"
    WALLET_ACTION = "wallet_action"
    EXTERNAL_API = "external_api"
    BROWSER_ACTION = "browser_action"
    TRANSACTION = "transaction"


# Actions that require user approval before execution
APPROVAL_REQUIRED_ACTIONS = {
    ActionType.FILE_WRITE,
    ActionType.WALLET_ACTION,
    ActionType.EXTERNAL_API,
    ActionType.BROWSER_ACTION,
    ActionType.TRANSACTION,
}

# Actions that are read-only / non-mutating
SAFE_ACTIONS = {
    ActionType.ANALYSIS,
    ActionType.REPORT,
    ActionType.RESEARCH,
}


@dataclass
class ActionProposal:
    """
    A structured proposal for an action that an agent wants to perform.

    This is the formal contract between sub-agents and the Safety/Approval
    layer. Every proposed action must go through this structure.

    Attributes:
        proposal_id (str): Unique identifier for tracing.
        agent_name (str): Name of the proposing agent.
        agent_type (str): Type of the proposing agent.
        action_type (ActionType): The type of action.
        target (str): The target resource (file path, URL, wallet address, etc.).
        reason (str): Why this action is needed.
        params (dict): Additional parameters for the action.
        requires_approval (bool): Whether user approval is needed.
        status (ActionStatus): Current status of the proposal.
        error_note (str): Optional error message if proposal was rejected.
    """

    proposal_id: str
    agent_name: str
    agent_type: str
    action_type: ActionType
    reason: str
    target: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    requires_approval: bool = False
    status: ActionStatus = ActionStatus.PROPOSED
    error_note: Optional[str] = None

    def __post_init__(self):
        """Set requires_approval based on action_type if not explicitly set."""
        if not self.requires_approval:
            self.requires_approval = self.action_type in APPROVAL_REQUIRED_ACTIONS

    def is_safe(self) -> bool:
        """Check if this action is safe (read-only, no approval needed)."""
        return self.action_type in SAFE_ACTIONS

    def validate(self) -> bool:
        """
        Validate the proposal is well-formed.

        Returns:
            True if valid, False otherwise.
        """
        if not self.proposal_id:
            self.error_note = "Proposal ID is missing"
            return False

        if not self.agent_name:
            self.error_note = "Agent name is missing"
            return False

        if not self.action_type:
            self.error_note = "Action type is missing"
            return False

        if not self.reason:
            self.error_note = "Reason is missing"
            return False

        return True

    def to_safe_string(self) -> str:
        """
        Generate a safe string representation for logging.

        Does NOT include params (which may contain sensitive data).
        """
        return (
            f"ActionProposal(id={self.proposal_id}, "
            f"agent={self.agent_name}, "
            f"type={self.action_type.value}, "
            f"target={self.target}, "
            f"approval={'required' if self.requires_approval else 'not_required'}, "
            f"status={self.status.value})"
        )


def create_analysis_proposal(
    agent_name: str,
    agent_type: str,
    reason: str,
    target: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> ActionProposal:
    """
    Create a safe (non-approval-required) analysis action proposal.

    These are the only types of actions that sub-agents in Phase 5
    are expected to produce.
    """
    import uuid

    return ActionProposal(
        proposal_id=str(uuid.uuid4()),
        agent_name=agent_name,
        agent_type=agent_type,
        action_type=ActionType.ANALYSIS,
        reason=reason,
        target=target,
        params=params or {},
        requires_approval=False,
        status=ActionStatus.PROPOSED,
    )


def is_proposal_safe(proposal: ActionProposal) -> bool:
    """
    Check if a proposal represents a safe action that does NOT need
    approval and can proceed.

    Safe actions: ANALYSIS, REPORT, RESEARCH.
    These do not modify any state and require no user approval.
    """
    return proposal.is_safe() and proposal.action_type in SAFE_ACTIONS

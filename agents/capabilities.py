"""
Capability System for the Multi-Agent Framework.

Phase 6: Introduces a Capability abstraction that allows agents to declare
what they are allowed to do. This is purely declarative — no actual
execution happens in this module.

Capabilities are organized into categories:
- read: Read-only operations (file reading, analysis, querying)
- analysis: LLM-powered reasoning and analysis
- report: Report generation and summarization
- future_write: Future write capability (not active in Phase 5/6)
- future_wallet: Future wallet operations (not active in any phase)
- future_browser: Future browser automation (not active in any phase)
- future_transaction: Future transaction execution (not active in any phase)

Future agents can declare capabilities without changing the core
permission model.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Set


class Capability(str, Enum):
    """
    Enumerates all possible capabilities for agents in the system.

    Phase 5 (current): Only read/analysis/report are available.
    Future: write, wallet, browser, transaction, etc.
    """

    # Current capabilities (Phase 5)
    READ_FILE = "read_file"
    ANALYSIS = "analysis"
    REPORT = "report"
    RESEARCH = "research"

    # Future capabilities (not yet implemented)
    FILE_WRITE = "file_write"
    WALLET_READ = "wallet_read"
    TRANSACTION_SIMULATION = "transaction_simulation"
    BROWSER = "browser"
    API_ACCESS = "api_access"
    TRANSACTION_EXECUTE = "transaction_execute"

    @classmethod
    def get_category(cls, capability: str) -> str:
        """
        Get the category name for a capability.

        Categories:
        - read: Read-only operations
        - analysis: LLM-powered analysis
        - report: Reporting and summarization
        - future: Capabilities that are planned but not yet implemented
        """
        future_caps = {
            "file_write",
            "wallet_read",
            "transaction_simulation",
            "browser",
            "api_access",
            "transaction_execute",
        }

        if capability in future_caps:
            return "future"

        read_caps = {"read_file"}
        analysis_caps = {"analysis", "research"}
        report_caps = {"report"}

        if capability in read_caps:
            return "read"
        if capability in analysis_caps:
            return "analysis"
        if capability in report_caps:
            return "report"

        return "unknown"

    @classmethod
    def get_future_capabilities(cls) -> Set[str]:
        """Return the set of all future/planned capabilities."""
        return {cap.value for cap in cls if cls.get_category(cap.value) == "future"}

    @classmethod
    def get_active_capabilities(cls) -> Set[str]:
        """Return the set of currently active capabilities."""
        return {cap.value for cap in cls if cls.get_category(cap.value) != "future"}


def validate_capability(capability: str) -> bool:
    """
    Check if a capability name is valid (exists in the enum).

    Args:
        capability: The capability name to validate.

    Returns:
        True if valid, False otherwise.
    """
    return capability in {cap.value for cap in Capability}


def get_agent_capabilities(agent_name: str) -> List[str]:
    """
    Get the default capabilities for a built-in agent type.

    Args:
        agent_name: The type name ("marketing", "research", "opportunity").

    Returns:
        List of capability strings.
    """
    defaults = {
        "marketing": [
            Capability.ANALYSIS.value,
            Capability.REPORT.value,
        ],
        "research": [
            Capability.READ_FILE.value,
            Capability.ANALYSIS.value,
            Capability.REPORT.value,
        ],
        "opportunity": [
            Capability.ANALYSIS.value,
            Capability.REPORT.value,
            Capability.RESEARCH.value,
        ],
        "senior": [
            Capability.ANALYSIS.value,
        ],
    }

    return defaults.get(agent_name, [])

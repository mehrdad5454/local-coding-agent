"""
Persona Layer for the multi-agent system.

Purpose:
    Separate technical Agent identity from user-facing personality.

Example (structure only — no persona is defined in Phase 7):
    Technical identity: ResearchAgent
    Persona id: "girinovich"
    Persona display_name: a human-readable label supplied by external
        configuration in a later phase (never a Python identifier).

Encoding rule (Phase 7):
    Internal identifiers (id, role, style) are ASCII/English only.
    Non-ASCII display labels are NEVER hardcoded in the source tree; they
    will be loaded from external data (e.g. a JSON config file) in the
    phase that actually introduces personas.
    No non-ASCII persona literal exists in this module.

Phase 7: Architecture Preparation Layer (Lightweight extensions).
    create_default_registry() returns an EMPTY registry: the persona
    plumbing exists, but no agent is given a persona, and no persona
    prompt is injected anywhere in the runtime path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class Persona:
    """
    User-facing personality for an Agent.

    Attributes:
        id: ASCII-only internal identifier (e.g. "girinovich").
        display_name: Unicode display label shown to users. It is supplied
            from external configuration when personas are introduced; no
            non-ASCII persona literal lives in the source tree.
        role: ASCII-only role title (e.g. "Research Analyst").
        style: ASCII-only description of communication style.
        prompt_prefix: Optional system prompt prefix that embodies the persona.
            Applied before the task, after the standard agent instructions.
    """

    id: str
    display_name: str
    role: str
    style: str
    prompt_prefix: Optional[str] = None

    def system_prompt(self) -> str:
        """
        Generate a system prompt fragment embodying this persona.

        Returns empty string if style/prompt_prefix are not set.
        """
        parts = []
        if self.style:
            parts.append(f"You are {self.display_name} ({self.role}). {self.style}.")
        if self.prompt_prefix:
            parts.append(self.prompt_prefix)
        return " ".join(parts)


@dataclass
class PersonaRegistry:
    """
    Registry mapping agent technical names to their personas.

    Usage:
        registry = PersonaRegistry()
        persona = registry.get("research")  # returns ResearchAgent's Persona
    """

    _personas: Dict[str, Persona] = field(default_factory=dict)

    def register(self, agent_name: str, persona: Persona) -> None:
        """Register a persona for a given agent technical name."""
        self._personas[agent_name] = persona

    def get(self, agent_name: str) -> Optional[Persona]:
        """Retrieve the persona for an agent name, or None if not registered."""
        return self._personas.get(agent_name)

    def get_system_prompt(self, agent_name: str) -> str:
        """Get the persona-specific system prompt fragment for an agent."""
        persona = self.get(agent_name)
        if persona is None:
            return ""
        return persona.system_prompt()

    def contains(self, agent_name: str) -> bool:
        """Check if a persona is registered for the given agent name."""
        return agent_name in self._personas

    @property
    def all(self) -> Dict[str, Persona]:
        return dict(self._personas)

    @property
    def size(self) -> int:
        return len(self._personas)


def create_default_registry() -> PersonaRegistry:
    """
    Create an empty PersonaRegistry for built-in agents.

    Phase 7: This is infrastructure preparation only.
    No default personas are registered in this phase.
    Personas can be added later by calling registry.register().

    Returns:
        An empty PersonaRegistry.
    """
    return PersonaRegistry()


def get_display_name(agent_name: str, registry: Optional[PersonaRegistry] = None) -> str:
    """
    Get the display name for an agent.

    Args:
        agent_name: The technical agent name (e.g. "research_agent").
        registry: Optional PersonaRegistry. If None, returns agent_name.

    Returns:
        The display name if a persona exists in the provided registry,
        otherwise the agent_name unchanged.
    """
    if registry is None:
        return agent_name

    persona = registry.get(agent_name)
    if persona is not None:
        return persona.display_name
    return agent_name

"""
MarketingAgent — Specialized sub-agent for marketing/content tasks.

Phase 3: Initial implementation. This agent:
- Receives a marketing/content task from SeniorAgent
- Uses call_model to generate text output
- Does NOT perform any file writes or filesystem operations
- Does NOT bypass safety layers (Emergency Stop, Approval, safe_path)

Future (Phase 4+): Will optionally delegate to write tools after approval.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .base import Agent
from .persona import PersonaRegistry


class MarketingAgent(Agent):
    """
    Specialized agent for marketing content generation.

    Supports tasks like:
    - Writing advertising copy
    - Creating website/landing page text
    - Generating headlines
    - Product description copy
    - General marketing content

    This agent does NOT write files. It only generates text via LLM.
    """

    # Keywords that indicate a marketing/content request.
    # Used by SeniorAgent to route requests to this agent.
    TRIGGERS: List[str] = [
        # Persian keywords
        "متن تبلیغاتی",
        "کپی بنویس",
        "کپی تبلیغاتی",
        "headline",
        "هِدْلاین",
        "لندینگ",
        "landing page",
        "برای سایت متن",
        "متن معرفی",
        "محتوای marketing",
        "محتوای بازاریابی",
        "بازاریابی",
        "کمپین بازاریابی",
        "جذب مشتری",
        "تبلیغ بنویس",
        "کمپین تبلیغاتی",
        "copy",
        # English keywords
        "marketing copy",
        "ad copy",
        "advertising text",
        "product description",
        "sales copy",
        "landing page copy",
    ]

    def __init__(
        self,
        tools: Optional[Dict[str, Any]] = None,
        stop_event=None,
        permissions=None,
        persona_registry: Optional[PersonaRegistry] = None,
    ):
        super().__init__(
            name="marketing_agent",
            tools=tools,
            permissions=permissions,
            persona_registry=persona_registry,
        )
        self._shared_stop_event = stop_event

    def run(self, task: str, **kwargs) -> str:
        """
        Generate marketing content for the given task.

        Args:
            task (str): The marketing task description (e.g. "write ad copy for X").
            **kwargs: Expected keys:
                - project_name (str): Name of the project context.
                - project_root (str): Root path of the project.
                - user_request (str): The original full user request.

        Returns:
            str: Generated marketing content.
        """
        # Check emergency stop via shared event.
        if self._is_stopped():
            return (
                "MarketingAgent متوقف شد: حالت اضطراری فعال است."
            )

        user_request = kwargs.get("user_request", task)
        project_name = kwargs.get("project_name", "unknown")

        # Generate content using the shared LLM interface.
        content = self._generate_content(task, project_name)

        return content

    def _is_stopped(self) -> bool:
        """Check if the shared stop_event has been triggered."""
        if self._shared_stop_event is None:
            return False
        return self._shared_stop_event.is_set()

    def _generate_content(
        self,
        task: str,
        project_name: str,
    ) -> str:
        """
        Generate marketing content using the shared LLM (call_model).

        Uses the tools dict to access call_model — does NOT duplicate it.
        """
        call_model = self.get_tool("call_model")

        system_prompt = (
            "شما یک کپی‌رایتر تخصصی بازاریابی هستید.\n"
            "وظیفه شما تولید متن‌های تبلیغاتی، معرفی‌کننده، "
            "هدر و محتواهای جذاب برای وب‌سایت، لندینگ‌صفحه و "
            "کمپین‌های تبلیغاتی است.\n"
            "متن خروجی باید:\n"
            "- جذاب و مؤثر باشد\n"
            "- مخاطب هدف را درک کند\n"
            "- پیام روشن و واضح داشته باشد\n"
            "- در صورت نیاز، ساختار شامل: خادم (Headline)، "
            "مزیت‌ها (Benefits)، فراخوان به اقدام (CTA) باشد\n"
            "هیچ توضیح اضافی ندهید؛ فقط متن نهایی را بنویسید."
        )

        user_context = (
            f"درخواست کاربر: {task}\n"
            f"نام پروژه: {project_name}\n"
        )

        try:
            result = call_model(system_prompt, user_context, no_think=True)
        except Exception:
            # Phase 7: log the failed delegation, then re-raise unchanged.
            self._log_action(task, project_name, failed=True)
            raise

        # Log that we generated content (for audit/journal purposes).
        self._log_action(task, project_name)

        return result

    def _log_action(
        self,
        task: str,
        project_name: str,
        failed: bool = False,
    ) -> None:
        """
        Log this agent's action using the shared record_action if available.
        This does NOT replace the global action journal — it supplements
        it with agent attribution.
        """
        record_action = self.tools.get("record_action")
        if record_action is None:
            return

        # Only fields record_action already supports. On the success path
        # this dict is empty, so the call is identical to before.
        failure_fields = (
            {
                "execution_state": "failed",
                "result_summary": "call_model failed",
            }
            if failed else {}
        )

        try:
            record_action(
                action_type="agent_delegation",
                tool="call_model",
                # Phase 7: read-only delegation (text generation only).
                # Same value as agent.ACTION_CLASS_READ; written as a
                # literal so agents/ never imports agent.py.
                operation_class="read",
                args={"task": task[:200]},
                project=project_name,
                agent_id="marketing_agent",
                agent_type="sub-agent",
                **failure_fields,
            )
        except Exception:
            # Journal should never break agent execution.
            pass

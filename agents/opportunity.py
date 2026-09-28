"""
OpportunityAgent — Specialized sub-agent for opportunity analysis tasks.

Phase 5: This agent:
- Receives an opportunity/research task from SeniorAgent
- Analyzes online income opportunities, affiliate programs, airdrops, etc.
- Compares opportunities, extracts conditions/risks, provides analysis
- Uses call_model for text analysis and synthesis
- Uses read_file for project context (read-only)
- Does NOT perform any file writes or filesystem modifications
- Does NOT execute commands, automate browsers, or interact externally
- Does NOT handle wallets, keys, transactions, or financial actions
- Does NOT bypass safety layers

This is a general opportunity analysis agent — it covers multiple domains:
Affiliate, Airdrop, Online Income, Digital Products, Freelancing,
Remote work, Business opportunities, and future domains.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import Agent
from .persona import PersonaRegistry


class OpportunityAgent(Agent):
    """
    Specialized agent for opportunity discovery, analysis, and comparison.

    Supports tasks like:
    - Analyzing affiliate programs or opportunities
    - Evaluating airdrop opportunities
    - Comparing online income methods
    - Assessing digital product opportunities
    - Reviewing freelancing/remote opportunities
    - Examining business opportunities
    - Extracting conditions, requirements, and risks
    - Classifying and categorizing opportunities

    This agent is general-purpose for opportunities — it does NOT hard-code
    to any single domain (e.g., not affiliate-only). The trigger keywords
    are broad enough to cover all current and future opportunity domains.

    This agent does NOT write files, execute commands, or handle
    financial actions. It only analyzes and reports.
    """

    # Keywords that indicate an opportunity/opportunity-analysis request.
    # These are broad and domain-agnostic to support Affiliate, Airdrop,
    # Online Income, Digital Products, Freelancing, Remote work, etc.
    # Used by SeniorAgent to route requests to this agent.
    TRIGGERS: List[str] = [
        # Persian keywords — opportunity/income domain
        "فرصت",
        "فرصت درآمد",
        "فرصت آنلاین",
        "فرصت کسب درآمد",
        "فرصت کاری",
        "فرصت دورکاری",
        "فرصت شغلی",
        "فرصت فریلنس",
        "فرصت بازاریابی",
        "فرصت تجاری",
        "فرصت دیجیتال",
        "فرصت محصول",
        "درآمد آنلاین",
        "کسب درآمد",
        "درآمد پاسیو",
        "درآمد فعال",
        "شیوه درآمد",
        "روش کسب درآمد",
        "پولساز",
        "درآمدگزای",
        "affiliate",
        "اینفلوآنسر",
        "airdrop",
        "ایردراپ",
        "توکن",
        "کریپتو",
        "نود",
        "تبلیغات",
        "تبلیغگر",
        "فیلیال",
        "شراکت",
        "دیجیتالی",
        "محصول دیجیتال",
        "دوره آموزشی",
        "فریلنس",
        "فریلنسر",
        "دورکار",
        "از دور",
        "ریموت",
        "پروژه فریلنس",
        "کار از راه دور",
        "شرکت دیجیتال",
        "استارتاپ",
        "کسب‌وکار آنلاین",
        "online business",
        "opportunity",
        # English keywords — analysis
        "opportunity analysis",
        "analyze opportunity",
        "evaluate opportunity",
        "compare opportunities",
        "income method",
        "passive income",
        "side hustle",
        "make money online",
        "online income",
        "affiliate program",
        "affiliate marketing",
        "airdrop analysis",
        "freelance opportunity",
        "remote work",
        "remote job",
        "business opportunity",
        "digital product",
        "monetize",
        "revenue stream",
        "earning potential",
        # Persian keywords — analysis
        "تحلیل فرصت",
        "بررسی فرصت",
        "مقایسه فرصت",
        "ارزیابی فرصت",
        "شرایط این فرصت",
        "ریسک این فرصت",
        "مزایای این فرصت",
        "معایب این فرصت",
        "چه ریسکی دارد",
        "چه شرایطی دارد",
        "چگونه کسب درآمد کنم",
        "بهترین روش",
        "کدام بهتر است",
        "کدام مناسب‌تر",
        # General opportunity analysis
        "possibility",
        "possibility analysis",
        "chance analysis",
    ]

    def __init__(
        self,
        tools: Optional[Dict[str, Any]] = None,
        stop_event=None,
        permissions=None,
        persona_registry: Optional[PersonaRegistry] = None,
    ):
        super().__init__(
            name="opportunity_agent",
            tools=tools,
            permissions=permissions,
            persona_registry=persona_registry,
        )
        self._shared_stop_event = stop_event

    def run(self, task: str, **kwargs) -> str:
        """
        Analyze and evaluate opportunities for the given task.

        Args:
            task (str): The opportunity analysis task description.
            **kwargs: Expected keys:
                - project_name (str): Name of the project context.
                - project_root (str): Root path of the project.
                - allow_write (bool): Whether writing is allowed (ignored — read-only).
                - user_request (str): The original full user request.
                - stop_event (threading.Event): Shared emergency stop event.

        Returns:
            str: Opportunity analysis result.
        """
        if self._is_stopped():
            return (
                "OpportunityAgent متوقف شد: حالت اضطراری فعال است."
            )

        project_name = kwargs.get("project_name", "unknown")
        project_root = kwargs.get("project_root", "")
        user_request = kwargs.get("user_request", task)

        # Read any file mentioned in the request for context.
        file_path = self._find_referenced_file(user_request, project_root)
        file_content = ""
        if file_path and project_root:
            file_content = self._read_project_file(
                project_root, file_path
            )

        # Generate analysis using the shared LLM interface.
        result = self._analyze_opportunity(
            task,
            project_name=project_name,
            file_path=file_path,
            file_content=file_content,
        )

        return result

    def _is_stopped(self) -> bool:
        """Check if the shared stop_event has been triggered."""
        if self._shared_stop_event is None:
            return False
        return self._shared_stop_event.is_set()

    def _find_referenced_file(
        self,
        user_request: str,
        project_root: str,
    ) -> Optional[str]:
        """
        Look for a filename reference in the request.

        Returns the path if found, None otherwise.
        Uses simple pattern matching — not a full parser.
        """
        import re

        # Match patterns like: file.py, src/main.py, path/to/file.js
        pattern = r"([\w./-]+\.\w+)"
        matches = re.findall(pattern, user_request)

        for match in matches:
            full_path = None
            safe_path_tool = self.tools.get("safe_path")
            if safe_path_tool and project_root:
                try:
                    full_path = safe_path_tool(project_root, match)
                    import os
                    if os.path.isfile(full_path):
                        return match
                except Exception:
                    pass

        return None

    def _read_project_file(
        self,
        project_root: str,
        relative_path: str,
    ) -> str:
        """
        Read a file from the project using the shared read_file tool.

        Returns empty string if the file cannot be read.
        """
        read_file_tool = self.tools.get("read_file")
        if read_file_tool is None:
            return ""

        try:
            return read_file_tool(project_root, relative_path)
        except Exception:
            return ""

    def _analyze_opportunity(
        self,
        task: str,
        project_name: str,
        file_path: Optional[str],
        file_content: str,
    ) -> str:
        """
        Generate opportunity analysis using the shared LLM (call_model).

        Uses the tools dict to access call_model — does NOT duplicate it.

        This is a general-purpose opportunity analyzer that covers
        multiple domains (Affiliate, Airdrop, Online Income, etc.)
        without hardcoding to any single domain.
        """
        call_model = self.get_tool("call_model")

        system_prompt = (
            "شما یک متخصص تحلیل فرصت‌ها و کسب درآمد آنلاین هستید.\n"
            "وظیفه شما تحلیل فرصت‌های مختلف درآمدی، مقایسه آن‌ها، "
            "استخراج شرایط و الزامات، شناسایی ریسک‌ها و مزایا، "
            "و ارائه بررسی‌های منطقی و ساختارمند است.\n"
            "حوزه‌های قابل بررسی شامل:\n"
            "- Affiliate Marketing\n"
            "- Airdrop و توکن‌های ارز دیجیتال\n"
            "- درآمد پاسیو و آنلاین\n"
            "- محصولات دیجیتال\n"
            "- فریلنس و کار از راه دور\n"
            "- فرصت‌های تجاری و کسب‌وکار آنلاین\n"
            "و سایر فرصت‌های درآمدی.\n"
            "همیشه سعی کنید:\n"
            "- ساختارمند و منطقی پاسخ بدهید\n"
            "- نکات مثبت و منفی را متعادل بررسی کنید\n"
            "- ریسک‌ها را واضح کنید\n"
            "- دقیقاً به سؤال پاسخ دهید\n"
            "- اگر نمی‌دانید چیزی را، آن را قلم نزنید\n"
            "هیچ اقدام مالی یا اجرایی انجام ندهید — فقط تحلیل بدهید."
        )

        user_context_parts = [
            f"درخواست کاربر: {task}",
            f"نام پروژه: {project_name}",
        ]

        if file_path and file_content:
            user_context_parts.append(
                f"\nفایل مرجع: {file_path}\n"
                f"محتوای فایل:\n{file_content[:3000]}"
                + ("..." if len(file_content) > 3000 else "")
            )
        elif file_path and not file_content:
            user_context_parts.append(
                f"\nفایل مرجع ذکر شده: {file_path} "
                "(قابل خواندن نشد یا یافت نشد)"
            )

        user_context = "\n\n".join(user_context_parts)

        result = call_model(system_prompt, user_context, no_think=True)

        # Log this action for audit/journal purposes.
        self._log_action(task, project_name)

        return result

    def _log_action(
        self,
        task: str,
        project_name: str,
    ) -> None:
        """
        Log this agent's action using the shared record_action if available.
        This supplements the global action journal with agent attribution.
        """
        record_action = self.tools.get("record_action")
        if record_action is None:
            return

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
                agent_id="opportunity_agent",
                agent_type="sub-agent",
            )
        except Exception:
            # Journal should never break agent execution.
            pass

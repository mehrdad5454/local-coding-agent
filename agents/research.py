"""
ResearchAgent — Specialized sub-agent for research and analysis tasks.

Phase 4: This agent:
- Receives a research/analysis task from SeniorAgent
- Uses call_model for text analysis and synthesis
- Uses read_file for file analysis (read-only)
- Uses safe_path for secure file access
- Does NOT perform any file writes or filesystem modifications
- Does NOT bypass safety layers (Emergency Stop, Approval, safe_path)
- Does NOT execute commands or external operations

Future: Will support advanced research capabilities like
file comparison, pattern analysis, and structured reports.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import Agent
from .persona import PersonaRegistry


class ResearchAgent(Agent):
    """
    Specialized agent for research and analysis tasks.

    Supports tasks like:
    - Explaining how code/concepts work
    - Analyzing files and code
    - Summarizing information
    - Comparing options or approaches
    - Researching topics
    - Extracting key findings from data

    This agent does NOT write files. It only reads files and
    generates analysis output via LLM.
    """

    # Keywords that indicate a research/analysis request.
    # Used by SeniorAgent to route requests to this agent.
    TRIGGERS: List[str] = [
        # Persian keywords
        "بررسی کن",
        "تحلیل کن",
        "تحقیق کن",
        "توضیح بده",
        "چطور کار می‌کنه",
        "چرا",
        "چیست",
        "تفاوت",
        "مقایسه کن",
        "چطور می‌توان",
        "بهترین راه",
        "راه‌حل",
        "چالش",
        "اشکال",
        "مزیت",
        "معایب",
        "جمع‌آوری اطلاعات",
        "منابع",
        "خلاصه",
        "چکیده",
        "بررسی کد",
        "کد را بررسی کن",
        "تحلیل سورس",
        "مدرک",
        "شواهد",
        "بررسی فایل",
        "فایل را بررسی کن",
        # English keywords
        "analyze",
        "research",
        "explain",
        "how does",
        "what is",
        "compare",
        "summary",
        "summarize",
        "findings",
        "investigate",
        "review code",
        "code analysis",
        "file analysis",
        "examine file",
        "study",
        "investigate",
        "look into",
    ]

    def __init__(
        self,
        tools: Optional[Dict[str, Any]] = None,
        stop_event=None,
        permissions=None,
        persona_registry: Optional[PersonaRegistry] = None,
    ):
        super().__init__(
            name="research_agent",
            tools=tools,
            permissions=permissions,
            persona_registry=persona_registry,
        )
        self._shared_stop_event = stop_event

    def run(self, task: str, **kwargs) -> str:
        """
        Analyze and research the given task.

        Args:
            task (str): The research task description.
            **kwargs: Expected keys:
                - project_name (str): Name of the project context.
                - project_root (str): Root path of the project.
                - allow_write (bool): Whether writing is allowed (ignored — read-only).
                - user_request (str): The original full user request.
                - stop_event (threading.Event): Shared emergency stop event.

        Returns:
            str: Analysis/research result.
        """
        if self._is_stopped():
            return (
                "ResearchAgent متوقف شد: حالت اضطراری فعال است."
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
        result = self._analyze_request(
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
            # Verify the file exists in the project (read-only check).
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

    def _analyze_request(
        self,
        task: str,
        project_name: str,
        file_path: Optional[str],
        file_content: str,
    ) -> str:
        """
        Generate research/analysis output using the shared LLM.

        Uses the tools dict to access call_model — does NOT duplicate it.
        """
        call_model = self.get_tool("call_model")

        system_prompt = (
            "شما یک تحلیلگر و محقق تخصصی هستید.\n"
            "وظیفه شما پاسخ دهی به سؤالات، تحلیل کردن اطلاعات، "
            "توضیح مفاهیم، مقایسه گزینه‌ها و ارائه "
            "جمع‌بندی‌های واضح و منطقی است.\n"
            "اگر فایل یا کدی ارائه شده است، آن را تحلیل کنید "
            "و بر اساس آن پاسخ دهید.\n"
            "اگر سؤال کلی است، دانش عمومی خود را بکار بگیرید "
            "ولی مواردی را که نمی‌دانید قلم نزنید.\n"
            "همیشه سعی کنید ساختارمند و منطقی پاسخ بدهید."
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
                "(قابل خواندن نبود یا یافت نشد)"
            )

        user_context = "\n\n".join(user_context_parts)

        try:
            result = call_model(system_prompt, user_context, no_think=True)
        except Exception:
            # Phase 7: شکست call_model هم ثبت می‌شود تا عیب‌یابی از
            # ژورنال ممکن باشد، سپس استثنا عیناً بالا می‌رود
            # (مانند قبل — نه swallow می‌شود و نه fallback را تغییر می‌دهد).
            self._log_action(task, project_name, failed=True)
            raise

        # Log this action for audit/journal purposes.
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
        This supplements the global action journal with agent attribution.
        """
        record_action = self.tools.get("record_action")
        if record_action is None:
            return

        # فقط از فیلدهایی استفاده می‌کند که record_action از قبل پشتیبانی
        # می‌کند. در مسیر موفقیت این dict خالی است، پس فراخوانی
        # record_action دقیقاً همان آرگومان‌های قبلی را می‌گیرد.
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
                # Phase 7: این delegation فقط تولید متن است و هیچ
                # تغییری در وضعیت کاربر/فایل ایجاد نمی‌کند، پس
                # operation_class آن "read" است نه "user_mutation".
                # مقدار با agent.ACTION_CLASS_READ یکسان است؛ اینجا
                # رشته ثابت نوشته می‌شود تا agents به agent.py وابسته
                # نشود (record_action از بیرون تزریق می‌شود).
                operation_class="read",
                args={"task": task[:200]},
                project=project_name,
                agent_id="research_agent",
                agent_type="sub-agent",
                **failure_fields,
            )
        except Exception:
            # Journal should never break agent execution.
            pass

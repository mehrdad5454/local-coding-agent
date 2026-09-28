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

import re
from typing import Any, Dict, List, Optional

from .base import Agent
from .persona import PersonaRegistry


# Phase 9 Section 1: read at most this many explicitly referenced files.
MAX_REFERENCED_FILES = 2

# Phase 9 Section 1: combined character budget for ALL injected file
# content. This is a total, not a per-file allowance, so context size
# stays bounded regardless of how many files are read.
MAX_TOTAL_FILE_CONTENT = 4000

# Phase 9 Section 2A: safety floor for the per-file share. It is used ONLY in
# the degenerate edge case where (remaining budget // remaining files) rounds
# down to 0, so a referenced file is never dropped silently. It is NOT an extra
# allowance: every block still fits inside its own share, the truncation notice
# is paid for out of that same share, and the unreadable-file notice is shorter
# than this floor. The TOTAL cap MAX_TOTAL_FILE_CONTENT above therefore remains
# absolute — the sum of all injected file content never exceeds it.
MIN_FILE_CONTENT = 200

# Phase 9 Section 2A: appended when a file is truncated. It is charged to the
# same per-file share as the content itself, so the notice never increases the
# total injected characters beyond MAX_TOTAL_FILE_CONTENT.
TRUNCATION_NOTICE = "\n...[محتوای فایل به دلیل محدودیت حجم ناقص است]"

# Phase 9 Section 2B: explicit, read-only phrase search.
#
# The search runs ONLY when the user delimits the phrase with one of these
# three pairs. Nothing is ever guessed, and a request without such a phrase
# performs no disk access at all.
SEARCH_PHRASE_PATTERNS = (
    r'"([^"\n]{1,120})"',
    r"`([^`\n]{1,120})`",
    r"«([^»\n]{1,120})»",
)

# A candidate ending like this is a quoted sentence/message, not a phrase.
SEARCH_PHRASE_SENTENCE_ENDS = (".", "!", "?", "؟", "…")

# A candidate that is a file/path reference is a file request, not a search.
SEARCH_PATH_LIKE = re.compile(r"^[^\s]*[\\/]|^[^\s]+\.[A-Za-z0-9]{1,8}$")

# Hard limits: the search is a project-wide walk, so it stays small.
MAX_SEARCH_RESULTS_FOR_RESEARCH = 20
MAX_SEARCH_LINE_CHARS = 200

SEARCH_BLOCK_HEADER = "نتایج جستجوی واقعی در فایل‌های پروژه"
SEARCH_NO_RESULTS = "هیچ نتیجه‌ای در فایل‌های پروژه یافت نشد."

# Shown when a referenced file cannot be read. It is charged to the same
# shared budget as file content, and no content is ever invented.
FILE_UNAVAILABLE_NOTE = "در دسترس نیست (فایل خوانده نشد یا یافت نشد)."


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
        file_paths = self._find_referenced_file(user_request, project_root)

        # Phase 9 Section 2B: explicit, read-only phrase search. Returns an
        # empty string (and touches no disk) unless the user delimited a
        # search phrase.
        search_block = self._build_search_block(
            user_request,
            project_root,
        )

        # Phase 9 Section 2A: assemble the referenced-file context with a fair,
        # dynamic per-file allocation inside the SAME total budget, so every
        # referenced file gets a chance to contribute and nothing is dropped
        # silently. The search block joins that same budget — there is no
        # second, separate budget.
        file_content = self._build_file_content_blocks(
            file_paths,
            project_root,
            search_block=search_block,
        )

        # Emergency Stop may have been triggered while the files were being
        # read. Never start an LLM call after that.
        if self._is_stopped():
            return (
                "ResearchAgent متوقف شد: حالت اضطراری فعال است."
            )

        # Generate analysis using the shared LLM interface.
        result = self._analyze_request(
            task,
            project_name=project_name,
            file_content=file_content,
        )

        return result

    def _build_search_block(
        self,
        user_request: str,
        project_root: str,
    ) -> str:
        """
        Build a bounded, read-only search block for an EXPLICIT phrase.

        Phase 9 Section 2B. Rules, in order:

        - no explicitly delimited phrase -> no disk access at all, and an
          empty string is returned;
        - Emergency Stop is checked before the disk search and again right
          after it;
        - the shared `search_in_files` tool is used only when the permission
          model allows it AND the caller actually injected it, so the
          PermissionModel is never bypassed and a missing tool simply means
          "no search";
        - the search always starts at `project_root`: no user-controlled
          subdir/path is ever passed;
        - max_results is capped and every returned line is re-clipped;
        - no results -> an explicit "not found" statement, never invented
          content;
        - only expected search-input / filesystem errors are turned into a
          short note; anything else propagates exactly as before.
        """
        if not project_root:
            return ""

        phrase = self._extract_explicit_search_phrase(user_request)
        if not phrase:
            return ""

        if self._is_stopped():
            return ""

        if not self.can_access_tool("search_in_files"):
            return ""

        search_tool = self.tools.get("search_in_files")
        if search_tool is None:
            return ""

        try:
            results = search_tool(
                project_root,
                phrase,
                max_results=MAX_SEARCH_RESULTS_FOR_RESEARCH,
            )
        except (ValueError, OSError) as exc:
            # Expected search-input / filesystem errors only.
            return (
                f'{SEARCH_BLOCK_HEADER} برای: "{phrase}"\n'
                f"جستجو انجام نشد: {exc}"
            )

        if self._is_stopped():
            return ""

        if not results:
            return (
                f'{SEARCH_BLOCK_HEADER} برای: "{phrase}"\n'
                f"{SEARCH_NO_RESULTS}"
            )

        lines = [
            f'{SEARCH_BLOCK_HEADER} برای: "{phrase}" '
            f"(تعداد: {len(results)})"
        ]

        for item in list(results)[:MAX_SEARCH_RESULTS_FOR_RESEARCH]:
            text = str(item.get("text", ""))[:MAX_SEARCH_LINE_CHARS]
            lines.append(
                f'{item.get("relative_path", "")}:'
                f'{item.get("line", "")}: {text}'
            )

        return "\n".join(lines)

    def _extract_explicit_search_phrase(self, user_request: str) -> str:
        """
        Return the first explicitly delimited search phrase, or "".

        Only "..." , `...` and «...» count as an explicit phrase. Two guards
        keep this from hijacking the existing explicit-file path:

        - a candidate that is a file/path reference (it has a path separator
          or a file extension) is treated as a file request, not a search;
        - a candidate that ends like a whole sentence is a quoted message,
          not a search phrase.

        Free text is never interpreted as a query, so no project scan happens
        by accident and nothing is guessed.
        """
        import re

        raw = "" if user_request is None else str(user_request)

        for pattern in SEARCH_PHRASE_PATTERNS:
            for match in re.finditer(pattern, raw):
                phrase = " ".join(match.group(1).split())

                if not phrase:
                    continue

                if phrase.endswith(SEARCH_PHRASE_SENTENCE_ENDS):
                    continue

                if SEARCH_PATH_LIKE.match(phrase):
                    continue

                return phrase

        return ""


    def _build_file_content_blocks(
        self,
        file_paths: List[str],
        project_root: str,
        search_block: str = "",
    ) -> str:
        """
        Build the referenced-file context for the analysis prompt.

        Phase 9 Section 2A. The total character budget stays at
        MAX_TOTAL_FILE_CONTENT and is never increased, but it is shared fairly
        instead of greedily:

        - the budget still left is divided by the number of blocks still to
          process, so the last participant is guaranteed a real share;
        - a block that is shorter than its share returns the unused remainder
          to the budget, so later blocks can use it;
        - a block that is longer than its share is truncated and marked as
          truncated — no content is ever invented;
        - a file that cannot be read gets a short explicit note instead of
          being dropped without a trace.

        Phase 9 Section 2B: an optional, already-formatted `search_block`
        joins the SAME budget as one more participant. There is no separate
        search budget. The file list is still produced by
        _find_referenced_file(), so the safe_path check and the
        MAX_REFERENCED_FILES cap are preserved.

        Returns the joined blocks, or an empty string when there is nothing
        to inject.
        """
        if not project_root:
            return ""

        # Ordered participants of the one shared budget: the explicit search
        # block first (cheap and structural), then the explicitly referenced
        # files in request order.
        participants: List[Any] = []
        if search_block:
            participants.append(("search", search_block))
        for file_path in file_paths:
            participants.append(("file", file_path))

        blocks: List[str] = []
        budget = MAX_TOTAL_FILE_CONTENT
        total = len(participants)

        for index, (kind, payload) in enumerate(participants):
            remaining_blocks = total - index

            # Fair share: what is left, divided among what is still to come.
            share = budget // remaining_blocks
            if share <= 0:
                # Hard floor: every block still gets a real (small) share so
                # nothing disappears silently. The total budget cap stays
                # authoritative once the floor is hit.
                share = MIN_FILE_CONTENT

            if self._is_stopped():
                return "\n\n".join(blocks)

            if kind == "search":
                # Already formatted: only bounded here, never re-formatted.
                body = self._bounded_body(payload, share)
                budget -= len(body)
                blocks.append(body)
                continue

            content = self._read_project_file(project_root, payload)

            if not content:
                # Explicit, honest indication. No content is fabricated, and
                # the note itself is charged to the same shared budget.
                body = FILE_UNAVAILABLE_NOTE
                budget -= len(body)
                blocks.append(
                    f"فایل مرجع: {payload}\n"
                    f"محتوای فایل:\n{body}"
                )
                continue

            body = self._bounded_body(content, share)
            budget -= len(body)
            blocks.append(
                f"فایل مرجع: {payload}\n"
                f"محتوای فایل:\n{body}"
            )

        return "\n\n".join(blocks)

    def _bounded_body(self, content: str, share: int) -> str:
        """
        Fit `content` into its fair share.

        Returns the content unchanged when it fits; otherwise returns a
        clipped version followed by the truncation notice, so a partial read
        is always visible and the charged length never exceeds the share.
        """
        if len(content) <= share:
            return content

        room = max(share - len(TRUNCATION_NOTICE), 0)
        return content[:room] + TRUNCATION_NOTICE

    def _is_stopped(self) -> bool:
        """Check if the shared stop_event has been triggered."""
        if self._shared_stop_event is None:
            return False
        return self._shared_stop_event.is_set()

    def _find_referenced_file(
        self,
        user_request: str,
        project_root: str,
    ) -> List[str]:
        """
        Find explicitly referenced project files in the request.

        Returns the paths in order of appearance, capped at MAX_REFERENCED_FILES.
        Every candidate still passes through safe_path, so path traversal stays
        blocked. Returns an empty list when nothing valid is referenced.
        """
        import re

        # Match patterns like: file.py, src/main.py, path/to/file.js
        pattern = r"([\w./-]+\.\w+)"
        matches = re.findall(pattern, user_request)

        found: List[str] = []
        for match in matches:
            # Verify the file exists in the project (read-only check).
            safe_path_tool = self.tools.get("safe_path")
            if safe_path_tool and project_root:
                try:
                    full_path = safe_path_tool(project_root, match)
                    import os
                    if os.path.isfile(full_path) and match not in found:
                        found.append(match)
                        if len(found) >= MAX_REFERENCED_FILES:
                            break
                except Exception:
                    pass

        return found

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
        file_content: str,
    ) -> str:
        """
        Generate research/analysis output using the shared LLM.

        Uses the tools dict to access call_model — does NOT duplicate it.

        `file_content` is the already-assembled, budget-bounded block of
        referenced file content built by run() (empty string when none).
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

        if file_content:
            user_context_parts.append("\n" + file_content)

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

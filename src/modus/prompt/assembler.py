from __future__ import annotations

from datetime import datetime
from pathlib import Path

from modus.config import ModusConfig

# Prompt-cache boundary markers (Wave2 C1): the assembled prompt is split into a
# static block (role/capability/tool declarations and guidelines — stable across
# turns) and a dynamic block (current time, working directory, model/provider —
# changes every session).  ``modus.llm.cache.split_system_blocks`` splits at
# these markers so the static block can be cached independently of the dynamic
# one.  When prompt caching is disabled the markers are stripped before sending.
from modus.llm.cache import DYNAMIC_BOUNDARY, STATIC_BOUNDARY

class PromptAssembler:
    """组装 system prompt——注入工具列表、时间、工作目录、项目记忆

    The prompt is built as two blocks separated by cache boundary markers:

    - static block: role definition, model/capability declaration, tool list
      and the guidelines.  Content is deterministic for a given config/workspace
      (tool names and cwd are stable within a session), so the block is a good
      cache prefix.
    - dynamic block: current time, working directory, model/provider.  This is
      what changes between sessions; splitting it off keeps the static block's
      provider-side cache valid.
    """

    def __init__(
        self,
        config: ModusConfig,
        cwd: str,
        tool_names: list[str],
        model: str,
        provider: str,
    ):
        self.config = config
        self.cwd = str(Path(cwd).resolve()) if str(cwd or "").strip() else ""
        self.tool_names = tool_names
        self.model = model
        self.provider = provider

    def build(self) -> str:
        static_parts = [
            "You are Modus, a capable AI coding agent.",
            f"Model: {self.model} ({self.provider})",
            f"Available tools: {', '.join(self.tool_names)}",
            "",
            "Guidelines:",
            "- Be concise and implementation-oriented.",
            (
                "- Use tools to inspect files and verify behavior."
                if self.tool_names else
                "- No local tools are available in this conversation until the user enables a workspace."
            ),
            "- A selected workspace is a local data source, not blanket permission to upload its contents.",
            "- Decide the smallest useful data flow from the user's goal: prefer bounded local metadata or local computation, then report only the necessary aggregate result.",
            "- list_dir and glob expose bounded names/structure metadata. read_file, grep, and search_code put file content or excerpts into the current model context; the system records that disclosure in the audit trail.",
            "- If raw content is genuinely needed, call the precise content-reading tool directly; the system reports the disclosure scope. Never recursively read an entire large workspace or imply that selecting a folder uploads it.",
            "- Side-effect tools (write_file, edit_file, bash, run_tests, web_fetch) must be called directly when needed. "
            "The system handles approval by presenting an approval card with the exact parameters.",
            "- Do not ask for permission in your text response and do not wait for a textual confirmation. "
            "Call it directly, then wait for the tool result: the system reports whether approval was granted or denied.",
            "- For a small existing-file change, prefer edit_file with enough exact context; use write_file for intentional full-file writes.",
            "- After changing code, use run_tests with the narrowest relevant validation command and report its evidence honestly.",
            "- A run that changed files is not complete until a run_tests result reports status=passed; if validation fails, inspect the failure, make a bounded fix, and run validation again.",
            "- Never describe unverified edits as finished. If the run ends before a passing validation, clearly state that verification is still required.",
            "- If the user's request has a few plausible interpretations and you are not confident which is correct, do not guess. "
            "Finish your reply with a fenced ```choice block listing each interpretation as its own line, "
            "then stop and let the user click the option they want you to pursue.",
            "- When you finish a task, wrap the closing status in a fenced ```summary block: "
            "put the status word on the fence line (success, warn, error, or info), then one `- ` bullet "
            "per key point. Keep the summary concise and specific to what you actually did.",
            "- If you have a recommendation or insight worth calling out, add a fenced ```insight block "
            "with a single-line takeaway, clearly marked as your own view.",
            "- Structure your final reply for easy scanning: lead with the direct answer or result, "
            "then short sections for context and details. Keep each section brief. "
            "Never put your reasoning chain into the visible reply body — the system renders thinking "
            "separately below the user's message.",
            "- For multi-step work, outline your plan as a fenced ```plan block (phases as `## ` "
            "headings with `- ` bullets) before starting, then follow the numbered execution "
            "with a fenced ```steps block listing each step you actually took.",
        ]
        static = "\n".join(static_parts)

        dynamic_parts = [
            f"Current time: {datetime.now().isoformat(timespec='seconds')}",
            (
                f"Working directory: {self.cwd}"
                if self.cwd else
                "No workspace is selected. Answer conversationally and do not claim to read, write, run, or inspect local files."
            ),
        ]
        dynamic = "\n".join(dynamic_parts)

        # The static block is emitted first (stable cache prefix), then the
        # dynamic boundary marker, then the dynamic block.
        return (
            f"{static}\n"
            f"{STATIC_BOUNDARY}\n"
            f"{DYNAMIC_BOUNDARY}\n"
            f"{dynamic}"
        )

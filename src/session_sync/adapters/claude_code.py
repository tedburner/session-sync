"""Claude Code adapter: ~/.claude/projects/<encoded-cwd>/<sid>.jsonl."""

from __future__ import annotations

import os
from pathlib import Path

from .claude_like import ClaudeLikeAdapter


class ClaudeCodeAdapter(ClaudeLikeAdapter):
    name = "claude-code"
    display_name = "Claude Code"
    projects_subdir = "projects"

    @classmethod
    def default_root(cls) -> Path | None:
        env = os.environ.get("CLAUDE_CONFIG_DIR")
        return Path(env).expanduser() if env else Path.home() / ".claude"

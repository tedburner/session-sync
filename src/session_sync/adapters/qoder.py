"""Qoder adapters (international ~/.qoder and CN ~/.qoder-cn).

Claude-Code-derived format: leading workspace-directories + runtime-config meta
lines, then user/assistant lines with uuid/parentUuid chains.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..utils import read_jsonl
from .base import SessionSummary
from .claude_like import ClaudeLikeAdapter


class QoderAdapter(ClaudeLikeAdapter):
    name = "qoder"
    display_name = "Qoder"
    projects_subdir = "projects"
    version_string = "1.0.0"
    cn = False

    @classmethod
    def default_root(cls) -> Path | None:
        if cls.cn:
            return Path.home() / ".qoder-cn"
        env = os.environ.get("QODER_CONFIG_DIR")
        return Path(env).expanduser() if env else Path.home() / ".qoder"

    # ---------------------------------------------------------------- reading

    def _summarize_file(self, path: Path) -> SessionSummary | None:
        info = super()._summarize_file(path)
        if info is None:
            return info
        # prefer cwd from workspace-directories meta line
        try:
            for line in read_jsonl(path):
                if line.get("type") == "workspace-directories":
                    dirs = line.get("directories") or []
                    if dirs:
                        info.cwd = dirs[0]
                    break
        except OSError:
            pass
        return info

    def _parse_file(self, path: Path, sid: str) -> list[dict]:
        events = super()._parse_file(path, sid)
        for line in read_jsonl(path):
            if line.get("type") == "workspace-directories":
                dirs = line.get("directories") or []
                if dirs:
                    events[0]["cwd"] = dirs[0]
                break
        return events

    # ---------------------------------------------------------------- writing

    def _meta_lines(self, events, cwd: str, sid: str) -> list[dict]:
        model = events[0].get("model")
        return [
            {"type": "workspace-directories", "sessionId": sid, "directories": [cwd]},
            {
                "type": "runtime-config",
                "sessionId": sid,
                "model": model or "dfmodel",
                "timestamp": events[0].get("created_at"),
            },
        ]


class QoderCNAdapter(QoderAdapter):
    name = "qoder-cn"
    display_name = "Qoder CN"
    cn = True

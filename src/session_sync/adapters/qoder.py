"""Qoder adapters (international ~/.qoder and CN ~/.qoder-cn).

Claude-Code-derived format: leading workspace-directories + runtime-config meta
lines, then user/assistant lines with uuid/parentUuid chains.
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import ir
from ..utils import now_ms, read_jsonl, truncate
from .base import SessionSummary
from .claude_like import ClaudeLikeAdapter
from .codex import _human_user_text


def _message_text(m: dict) -> str:
    return "\n".join(
        b.get("text", "") for b in m.get("blocks") or [] if b.get("type") == "text"
    ).strip()


def _human_prompt_text(m: dict) -> str:
    """Cleaned text of a genuine human prompt, or "" for injected/tool/system lines."""
    if m.get("role") != "user" or (m.get("ext") or {}).get("isMeta"):
        return ""
    return _human_user_text(_message_text(m))


def _first_human_prompt(events: list[dict], limit: int = 60) -> str:
    for m in ir.messages(events):
        text = _human_prompt_text(m)
        if text:
            return truncate(text, limit)
    return ""


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

    # The Qoder daemon recognizes a session via human-origin prompts, a title
    # line and an active-leaf chain tail; foreign native lines are not portable.
    _NATIVE_KEEP = {"native:attachment", "native:file-history-snapshot", "native:system"}

    def _enrich_line(self, common: dict, m: dict) -> None:
        text = _human_prompt_text(m)
        if not text:
            return
        common["origin"] = {"kind": "human"}
        common["humanInput"] = {"text": text, "mode": "prompt"}
        common["userType"] = "external"
        common["entrypoint"] = "cli"
        common.setdefault("isSidechain", False)

    def _native_lines(self, events: list[dict], warnings: list[str]) -> list[dict]:
        out: list[dict] = []
        dropped = 0
        for e in events:
            if e.get("type") == "event" and e.get("kind", "").startswith("native:"):
                if e.get("kind") in self._NATIVE_KEEP:
                    raw = (e.get("ext") or {}).get("raw_line")
                    if raw is not None:
                        out.append(raw)
                else:
                    dropped += 1
        if dropped:
            warnings.append(f"dropped {dropped} foreign native lines (not portable to Qoder)")
        return out

    def _trailing_meta_lines(self, events: list[dict], sid: str, last_uuid: str | None) -> list[dict]:
        out: list[dict] = []
        title = ir.meta(events).get("title") or _first_human_prompt(events)
        if title:
            out.append({"type": "last-prompt", "sessionId": sid, "lastPrompt": title})
        if last_uuid:
            out.append(
                {"type": "active-leaf", "sessionId": sid, "leafUuid": last_uuid,
                 "explicit": False, "timestamp": now_ms()}
            )
        return out

    def import_warnings(self, events: list[dict]) -> list[str]:
        if not _first_human_prompt(events):
            return ["Qoder: 源会话没有可识别的人类提问，导入后可能显示为“Empty conversation”"]
        return []


class QoderCNAdapter(QoderAdapter):
    name = "qoder-cn"
    display_name = "Qoder CN"
    cn = True

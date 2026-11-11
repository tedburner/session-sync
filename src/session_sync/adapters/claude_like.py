"""Shared logic for Claude-Code-like formats (Claude Code, Qoder).

Line shape: {"type": "user"|"assistant", "uuid", "parentUuid", "sessionId",
"timestamp"(ISO), "cwd", "message": {"role", "content": str|blocks, ...}}
"""

from __future__ import annotations

from pathlib import Path

from .. import ir
from ..utils import (
    encode_cwd_claude,
    extract_text_content,
    iso_to_ms,
    ms_to_iso,
    read_jsonl,
    truncate,
    write_jsonl,
)
from .base import Adapter, AdapterError, SessionSummary


class ClaudeLikeAdapter(Adapter):
    """Subclasses set projects_attr (name of the projects subdir) and optionally emit_meta_lines."""

    projects_subdir = "projects"
    version_string = "1.0.0"

    # ---------------------------------------------------------------- reading

    def _projects_dir(self) -> Path:
        return self.root / self.projects_subdir

    def _session_file(self, sid: str) -> Path | None:
        for f in self._projects_dir().glob(f"*/{sid}.jsonl"):
            return f
        return None

    def _meta_lines(self, events, cwd: str, sid: str) -> list[dict]:
        """Hook: harness-specific leading meta lines (Qoder overrides)."""
        return []

    def list_sessions(self, cwd: str | None = None) -> list[SessionSummary]:
        out: list[SessionSummary] = []
        pdir = self._projects_dir()
        if not pdir.exists():
            return out
        for f in sorted(pdir.glob("*/*.jsonl")):
            info = self._summarize_file(f)
            if info and (cwd is None or info.cwd == cwd):
                out.append(info)
        out.sort(key=lambda s: s.updated_at or 0, reverse=True)
        return out

    def _summarize_file(self, path: Path) -> SessionSummary | None:
        sid = path.stem
        title = None
        cwd = None
        created_at = None
        updated_at = None
        count = 0
        first_text = ""
        try:
            for line in read_jsonl(path):
                t = line.get("type")
                if cwd is None and line.get("cwd"):
                    cwd = line.get("cwd")
                ts = iso_to_ms(line.get("timestamp"))
                if ts:
                    updated_at = ts
                    if created_at is None:
                        created_at = ts
                if t == "summary" and title is None:
                    title = line.get("summary")
                elif t in ("user", "assistant"):
                    count += 1
                    if t == "user" and not first_text:
                        msg = line.get("message") or {}
                        text = extract_text_content(msg.get("content"))
                        if text.strip():
                            first_text = truncate(text, 160)
        except OSError:
            return None
        return SessionSummary(
            harness=self.name,
            id=sid,
            title=title or (first_text or None),
            cwd=cwd,
            created_at=created_at,
            updated_at=updated_at,
            message_count=count,
            preview=first_text,
            path=str(path),
        )

    def read_session(self, sid: str) -> list[dict]:
        path = self._session_file(sid)
        if not path:
            raise AdapterError(f"{self.display_name}: session {sid} not found")
        return self._parse_file(path, sid)

    def _parse_file(self, path: Path, sid: str) -> list[dict]:
        events: list[dict] = []
        cwd = None
        created_at = None
        model = None
        events.append(
            {
                "type": "session_meta",
                "id": sid,
                "harness": self.name,
                "cwd": None,  # filled after scan
                "title": None,
                "model": None,
                "created_at": None,
                "ext": {},
            }
        )
        n = 0
        for line in read_jsonl(path):
            t = line.get("type")
            ts = iso_to_ms(line.get("timestamp"))
            if cwd is None and line.get("cwd"):
                cwd = line["cwd"]
            if ts and (created_at is None or ts < created_at):
                created_at = ts
            if t in ("user", "assistant"):
                n += 1
                msg = line.get("message") or {}
                role = msg.get("role", t)
                if role not in ("user", "assistant"):
                    role = t
                blocks = self._blocks_from_content(msg.get("content"))
                if t == "assistant" and msg.get("model") and not model:
                    model = msg.get("model")
                ext = {}
                for k in ("isMeta", "isSidechain", "userType", "entrypoint"):
                    if line.get(k) is not None:
                        ext[k] = line[k]
                if line.get("requestId"):
                    ext["requestId"] = line["requestId"]
                events.append(
                    {
                        "type": "message",
                        "id": line.get("uuid") or f"{sid}-m{n}",
                        "parent_id": line.get("parentUuid"),
                        "timestamp": ts,
                        "role": role,
                        "blocks": blocks,
                        "ext": ext,
                    }
                )
            elif t == "summary":
                events.append(
                    {
                        "type": "event",
                        "kind": "summary",
                        "timestamp": ts,
                        "data": {"summary": line.get("summary"), "leafUuid": line.get("leafUuid")},
                        "ext": {"raw_line": line},
                    }
                )
            else:
                # attachment / file-history-snapshot / system / ... preserved for same-format roundtrip
                events.append(
                    {
                        "type": "event",
                        "kind": f"native:{t}",
                        "timestamp": ts,
                        "data": {},
                        "ext": {"raw_line": line},
                    }
                )
        meta = events[0]
        if cwd:
            meta["cwd"] = cwd
        if created_at:
            meta["created_at"] = created_at
        meta["model"] = model
        if not meta["title"]:
            meta["title"] = ir.first_user_text(events, 60) or None
        return events

    @staticmethod
    def _blocks_from_content(content) -> list[dict]:
        blocks: list[dict] = []
        if isinstance(content, str):
            if content:
                blocks.append({"type": "text", "text": content})
            return blocks
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    blocks.append({"type": "text", "text": b.get("text", "")})
                elif bt == "thinking":
                    blocks.append({"type": "thinking", "text": b.get("thinking", b.get("text", ""))})
                elif bt == "tool_use":
                    blocks.append(
                        {
                            "type": "tool_call",
                            "id": b.get("id"),
                            "name": b.get("name"),
                            "arguments": b.get("input"),
                            "arguments_raw": None,
                        }
                    )
                elif bt == "tool_result":
                    blocks.append(
                        {
                            "type": "tool_result",
                            "call_id": b.get("tool_use_id"),
                            "output": extract_text_content(b.get("content")),
                            "is_error": bool(b.get("is_error")),
                            "name": None,
                        }
                    )
                elif bt in ("input_text", "output_text"):
                    blocks.append({"type": "text", "text": b.get("text", "")})
        return blocks

    # ---------------------------------------------------------------- writing

    def session_exists(self, sid: str) -> bool:
        return self._session_file(sid) is not None

    def write_session(
        self,
        events: list[dict],
        *,
        cwd: str | None = None,
        on_conflict: str = "skip",
        dry_run: bool = False,
    ) -> dict:
        from ..utils import backup_file, new_uuid

        warnings: list[str] = []
        meta = ir.meta(events)
        sid = meta.get("id") or new_uuid()
        target_cwd = cwd or meta.get("cwd")
        if not target_cwd:
            raise AdapterError("target cwd unknown; pass --cwd or --cwd-map")

        existing = self._session_file(sid)
        skipped = False
        backup = None
        if existing and on_conflict == "skip":
            skipped = True
        elif existing and on_conflict == "new-id":
            sid = new_uuid()
            existing = None
        elif existing and on_conflict == "overwrite" and not dry_run:
            backup = backup_file(existing)

        path = self._projects_dir() / encode_cwd_claude(target_cwd) / f"{sid}.jsonl"
        if not skipped and not dry_run:
            lines = self._build_lines(events, sid, target_cwd, warnings)
            write_jsonl(path, lines)
        result = {
            "harness": self.name,
            "target_id": sid,
            "path": str(path),
            "backup": backup,
            "created": not skipped and not dry_run,
            "skipped": skipped,
            "dry_run": dry_run,
            "warnings": warnings,
        }
        if skipped:
            result["note"] = f"session id already exists: {existing}"
        return result

    def _build_lines(self, events: list[dict], sid: str, cwd: str, warnings: list[str]) -> list[dict]:
        msgs = ir.messages(events)
        uuids: list[str] = []
        id_map: dict[str, str] = {}
        for i, m in enumerate(msgs):
            u = m.get("ext", {}).get("uuid") or f"{sid}-w{i + 1}"
            uuids.append(u)
            mid = m.get("id")
            if mid is not None and mid not in id_map:
                id_map[mid] = u

        lines: list[dict] = []
        lines.extend(self._meta_lines(events, cwd, sid))

        # summary lines first (picker titles), referencing the last message uuid
        last_uuid = uuids[-1] if msgs else None
        for e in events:
            if e.get("type") == "event" and e.get("kind") == "summary":
                text = (e.get("data") or {}).get("summary")
                if text:
                    lines.append({"type": "summary", "summary": text, "leafUuid": last_uuid})

        prev_uuid = None
        for i, m in enumerate(msgs):
            uuid = uuids[i]
            parent_uuid = id_map.get(m.get("parent_id")) or prev_uuid
            ts = ms_to_iso(m.get("timestamp"))
            ext = m.get("ext") or {}
            common = {
                "uuid": uuid,
                "parentUuid": parent_uuid,
                "sessionId": sid,
                "timestamp": ts,
                "cwd": cwd,
            }
            if "isMeta" in ext:
                common["isMeta"] = ext["isMeta"]
            if "isSidechain" in ext:
                common["isSidechain"] = ext["isSidechain"]
            content = self._content_from_blocks(m, warnings)
            role = m["role"] if m["role"] in ("user", "assistant") else "user"
            if m["role"] == "tool":
                # tool result message -> user line with tool_result blocks
                role = "user"
            elif m["role"] == "system":
                role = "user"
                common["isMeta"] = True
            self._enrich_line(common, m)
            lines.append(
                {
                    "type": role,
                    **common,
                    "message": {"role": role, "content": content},
                }
            )
            prev_uuid = uuid

        lines.extend(self._native_lines(events, warnings))
        lines.extend(self._trailing_meta_lines(events, sid, last_uuid))
        return lines

    def _enrich_line(self, common: dict, m: dict) -> None:
        """Hook: add harness-specific fields to a user/assistant line (Qoder overrides)."""

    def _native_lines(self, events: list[dict], warnings: list[str]) -> list[dict]:
        """Hook: native passthrough lines. Base preserves every native:* payload."""
        out: list[dict] = []
        for e in events:
            if e.get("type") == "event" and e.get("kind", "").startswith("native:"):
                raw = (e.get("ext") or {}).get("raw_line")
                if raw is not None:
                    out.append(raw)
                else:
                    warnings.append(f"dropped native line {e.get('kind')} (no raw payload)")
        return out

    def _trailing_meta_lines(self, events: list[dict], sid: str, last_uuid: str | None) -> list[dict]:
        """Hook: harness-specific trailing meta lines (Qoder overrides)."""
        return []

    @staticmethod
    def _content_from_blocks(m: dict, warnings: list[str]):
        blocks = m.get("blocks") or []
        if m["role"] == "tool":
            items = []
            for b in blocks:
                if b.get("type") == "tool_result":
                    items.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": b.get("call_id"),
                            "content": [{"type": "text", "text": b.get("output", "")}],
                            "is_error": bool(b.get("is_error")),
                        }
                    )
                else:
                    warnings.append(f"dropped block {b.get('type')} in tool message")
            return items
        if m["role"] == "system":
            texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            return "\n".join(t for t in texts if t)
        if len(blocks) == 1 and blocks[0].get("type") == "text" and m["role"] == "user":
            return blocks[0].get("text", "")
        items = []
        for b in blocks:
            bt = b.get("type")
            if bt == "text":
                items.append({"type": "text", "text": b.get("text", "")})
            elif bt == "thinking":
                items.append({"type": "thinking", "thinking": b.get("text", "")})
            elif bt == "tool_call":
                items.append(
                    {"type": "tool_use", "id": b.get("id"), "name": b.get("name"), "input": b.get("arguments") or {}}
                )
            elif bt == "tool_result":
                items.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": b.get("call_id"),
                        "content": [{"type": "text", "text": b.get("output", "")}],
                        "is_error": bool(b.get("is_error")),
                    }
                )
            else:
                warnings.append(f"dropped block {bt} in {m['role']} message")
        return items

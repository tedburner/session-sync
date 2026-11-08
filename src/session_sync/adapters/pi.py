"""Pi (badlogic/pi-mono) adapter: ~/.pi/agent/sessions/--<cwd>--/<ts>_<id>.jsonl (v3 tree)."""

from __future__ import annotations

import json
from pathlib import Path

from .. import ir
from ..utils import extract_text_content, hex_id, iso_to_ms, ms_to_iso, pi_session_dir_name
from .base import Adapter, AdapterError, SessionSummary


class PiAdapter(Adapter):
    name = "pi"
    display_name = "Pi"

    @classmethod
    def default_root(cls) -> Path | None:
        env = __import__("os").environ.get("PI_CODING_AGENT_SESSION_DIR")
        return Path(env).expanduser() if env else Path.home() / ".pi" / "agent" / "sessions"

    def is_available(self) -> bool:
        return bool(self.root) and Path(self.root).exists()

    # ---------------------------------------------------------------- reading

    def _iter_files(self):
        root = self.root
        if not root.exists():
            return
        for f in sorted(root.glob("--*--/*.jsonl")):
            yield f

    def list_sessions(self, cwd: str | None = None) -> list[SessionSummary]:
        out: list[SessionSummary] = []
        for f in self._iter_files():
            info = self._summarize_file(f)
            if info and (cwd is None or info.cwd == cwd):
                out.append(info)
        out.sort(key=lambda s: s.updated_at or 0, reverse=True)
        return out

    def _summarize_file(self, path: Path) -> SessionSummary | None:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = []
                for i, line in enumerate(fh):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        lines.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                    if i > 400:
                        break
        except OSError:
            return None
        if not lines or lines[0].get("type") != "session":
            return None
        header = lines[0]
        title = None
        count = 0
        first_text = ""
        created = iso_to_ms(header.get("timestamp"))
        updated = created
        for e in lines[1:]:
            ts = iso_to_ms(e.get("timestamp"))
            if ts:
                updated = ts
            if e.get("type") == "session_info" and e.get("name"):
                title = e["name"]
            elif e.get("type") == "message":
                count += 1
                msg = e.get("message") or {}
                if msg.get("role") == "user" and not first_text:
                    text = extract_text_content(msg.get("content"))
                    if text.strip():
                        first_text = text.strip()[:160]
        sid = path.stem.rsplit("_", 1)[-1] if "_" in path.stem else path.stem
        return SessionSummary(
            harness=self.name,
            id=sid,
            title=title or (first_text or None),
            cwd=header.get("cwd"),
            created_at=created,
            updated_at=updated,
            message_count=count,
            preview=first_text,
            path=str(path),
        )

    def read_session(self, sid: str) -> list[dict]:
        for f in self._iter_files():
            if f.name.endswith(f"_{sid}.jsonl") or f.stem == sid:
                return self._parse_file(f, sid)
        raise AdapterError(f"Pi: session {sid} not found")

    def session_exists(self, sid: str) -> bool:
        return any(f.name.endswith(f"_{sid}.jsonl") or f.stem == sid for f in self._iter_files())

    def _parse_file(self, path: Path, sid: str) -> list[dict]:
        from ..utils import read_jsonl

        lines = read_jsonl(path)
        if not lines or lines[0].get("type") != "session":
            raise AdapterError(f"Pi: {path} is not a session file")
        header = lines[0]
        meta = {
            "type": "session_meta",
            "id": header.get("id") or sid,
            "harness": self.name,
            "cwd": header.get("cwd"),
            "title": None,
            "model": None,
            "created_at": iso_to_ms(header.get("timestamp")),
            "ext": {},
        }
        events: list[dict] = [meta]
        for e in lines[1:]:
            t = e.get("type")
            ts = iso_to_ms(e.get("timestamp"))
            if t == "message":
                msg = e.get("message") or {}
                self._append_message(events, e, msg, ts, meta)
            elif t == "session_info":
                if e.get("name"):
                    meta["title"] = e["name"]
            elif t == "model_change":
                if not meta["model"]:
                    meta["model"] = e.get("modelId")
                events.append({"type": "event", "kind": "model_change", "timestamp": ts,
                               "data": {"provider": e.get("provider"), "modelId": e.get("modelId")}, "ext": {}})
            elif t in ("thinking_level_change", "compaction", "usage", "custom", "custom_message",
                       "context_edit", "branch_summary", "label", "session_info"):
                events.append({"type": "event", "kind": f"native:{t}", "timestamp": ts,
                               "data": {k: v for k, v in e.items() if k not in ("type", "id", "parentId", "timestamp")},
                               "ext": {"raw_entry": e}})
            else:
                events.append({"type": "event", "kind": f"native:{t}", "timestamp": ts, "data": {},
                               "ext": {"raw_entry": e}})
        if not meta["title"]:
            meta["title"] = ir.first_user_text(events, 60) or None
        return events

    def _append_message(self, events, entry, msg, ts, meta) -> None:
        role = msg.get("role")
        if role == "toolResult":
            events.append(
                {
                    "type": "message",
                    "id": entry.get("id"),
                    "parent_id": entry.get("parentId"),
                    "timestamp": ts,
                    "role": "tool",
                    "blocks": [
                        {
                            "type": "tool_result",
                            "call_id": msg.get("toolCallId"),
                            "output": extract_text_content(msg.get("content")),
                            "is_error": bool(msg.get("isError")),
                            "name": msg.get("toolName"),
                        }
                    ],
                    "ext": {},
                }
            )
            return
        blocks: list[dict] = []
        content = msg.get("content")
        if isinstance(content, str):
            if content:
                blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    blocks.append({"type": "text", "text": b.get("text", "")})
                elif bt == "thinking":
                    blocks.append({"type": "thinking", "text": b.get("thinking", b.get("text", ""))})
                elif bt == "image":
                    blocks.append({"type": "image", "media_type": b.get("mimeType"), "data": None})
        for call in msg.get("toolCalls") or []:
            args = call.get("arguments")
            if isinstance(args, str):
                blocks.append({"type": "tool_call", "id": call.get("id"), "name": call.get("name"),
                               "arguments": None, "arguments_raw": args})
            else:
                blocks.append({"type": "tool_call", "id": call.get("id"), "name": call.get("name"),
                               "arguments": args, "arguments_raw": None})
        if role == "assistant" and msg.get("model") and not meta["model"]:
            meta["model"] = msg.get("model")
        ext = {}
        for k in ("provider", "api", "thinkingLevel", "usage", "stopReason"):
            if msg.get(k) is not None:
                ext[k] = msg[k]
        events.append(
            {"type": "message", "id": entry.get("id"), "parent_id": entry.get("parentId"),
             "timestamp": ts, "role": role if role in ("user", "assistant", "system") else "user",
             "blocks": blocks, "ext": ext}
        )

    # ---------------------------------------------------------------- writing

    def write_session(
        self,
        events: list[dict],
        *,
        cwd: str | None = None,
        on_conflict: str = "skip",
        dry_run: bool = False,
    ) -> dict:
        from ..utils import backup_file, new_uuid, write_jsonl

        warnings: list[str] = []
        meta = ir.meta(events)
        sid = meta.get("id") or new_uuid()
        target_cwd = cwd or meta.get("cwd")
        if not target_cwd:
            raise AdapterError("target cwd unknown; pass --cwd or --cwd-map")

        dir_name = pi_session_dir_name(target_cwd)
        created = meta.get("created_at")
        stem = (ms_to_iso(created or 0) or "").replace(":", "-").replace(".", "-") or "unknown"
        path = self.root / dir_name / f"{stem}_{sid}.jsonl"

        existing = path if path.exists() else None
        if existing is None:
            for f in self._iter_files():
                # sid encoded into filename after the timestamp prefix
                if f.name.endswith(f"_{sid}.jsonl"):
                    existing = f
                    break
        skipped = False
        backup = None
        if existing and on_conflict == "skip":
            skipped = True
        elif existing and on_conflict == "new-id":
            sid = new_uuid()
            existing = None
            path = self.root / dir_name / f"{stem}_{sid}.jsonl"
        elif existing and on_conflict == "overwrite" and not dry_run:
            backup = backup_file(existing)

        if not skipped and not dry_run:
            write_jsonl(path, self._build_lines(events, sid, target_cwd, warnings))
        return {
            "harness": self.name,
            "target_id": sid,
            "path": str(path),
            "backup": backup,
            "created": not skipped and not dry_run,
            "skipped": skipped,
            "dry_run": dry_run,
            "warnings": warnings,
        }

    def _build_lines(self, events: list[dict], sid: str, cwd: str, warnings: list[str]) -> list[dict]:
        meta = ir.meta(events)
        lines: list[dict] = [
            {
                "type": "session",
                "version": 3,
                "id": sid,
                "timestamp": ms_to_iso(meta.get("created_at")) or ms_to_iso(0),
                "cwd": cwd,
            }
        ]
        if meta.get("title"):
            lines.append({"type": "session_info", "id": hex_id(8), "parentId": None,
                          "timestamp": ms_to_iso(meta.get("created_at")), "name": meta["title"]})
        prev = None
        emitted_results: set[str] = set()

        def tool_result_entry(b, parent_id, ts_iso, ts_ms):
            return {
                "type": "message", "id": hex_id(8), "parentId": parent_id, "timestamp": ts_iso,
                "message": {
                    "role": "toolResult", "toolCallId": b.get("call_id"),
                    "toolName": b.get("name") or "tool",
                    "content": [{"type": "text", "text": b.get("output", "")}],
                    "isError": bool(b.get("is_error")), "timestamp": ts_ms,
                },
            }

        for m in ir.messages(events):
            ts_iso = ms_to_iso(m.get("timestamp")) or ms_to_iso(0)
            eid = hex_id(8)
            base_msg: dict = {"timestamp": m.get("timestamp")}
            role = m["role"]
            texts = [b.get("text", "") for b in m.get("blocks") or [] if b.get("type") == "text"]
            if role == "tool":
                for b in m.get("blocks") or []:
                    if b.get("type") == "tool_result" and b.get("call_id") not in emitted_results:
                        entry = tool_result_entry(b, prev, ts_iso, m.get("timestamp"))
                        lines.append(entry)
                        emitted_results.add(b.get("call_id"))
                if lines and lines[-1]["type"] == "message":
                    prev = lines[-1]["id"]
                continue
            content_blocks = []
            for b in m.get("blocks") or []:
                bt = b.get("type")
                if bt == "text":
                    content_blocks.append({"type": "text", "text": b.get("text", "")})
                elif bt == "thinking":
                    content_blocks.append({"type": "thinking", "thinking": b.get("text", "")})
                elif bt == "image":
                    warnings.append("image block dropped (pi writer)")
                elif bt == "tool_call":
                    continue  # handled as toolCalls below
                elif bt == "tool_result":
                    continue  # handled as a toolResult entry below (paired with toolCalls)
            tool_calls = [
                {"id": b.get("id"), "name": b.get("name"),
                 "arguments": b.get("arguments_raw") if b.get("arguments_raw") else b.get("arguments")}
                for b in m.get("blocks") or [] if b.get("type") == "tool_call"
            ]
            message: dict = {"role": role if role in ("user", "assistant", "system") else "user", **base_msg}
            if role == "assistant":
                message["content"] = content_blocks or [{"type": "text", "text": ""}]
                message["api"] = "openai-completions"
                if meta.get("model"):
                    message["model"] = meta["model"]
                if tool_calls:
                    message["toolCalls"] = tool_calls
                for k in ("provider", "usage", "stopReason", "thinkingLevel"):
                    if k in (m.get("ext") or {}):
                        message[k] = m["ext"][k]
            elif role == "system":
                message["content"] = "\n".join(t for t in texts if t)
            else:
                message["content"] = (
                    "\n".join(t for t in texts if t) if len(content_blocks) <= 1 else content_blocks
                )
            lines.append({"type": "message", "id": eid, "parentId": prev, "timestamp": ts_iso, "message": message})
            prev = eid
            if role == "assistant":
                # tool results merged into this assistant message (ZCode-style IR)
                for b in m.get("blocks") or []:
                    if b.get("type") == "tool_result" and b.get("call_id") not in emitted_results:
                        lines.append(tool_result_entry(b, prev, ts_iso, m.get("timestamp")))
                        emitted_results.add(b.get("call_id"))
                if lines and lines[-1]["type"] == "message" and lines[-1]["message"]["role"] == "toolResult":
                    prev = lines[-1]["id"]
        for e in events:
            kind = e.get("kind") or ""
            if e.get("type") == "event" and kind.startswith("native:"):
                raw = (e.get("ext") or {}).get("raw_entry")
                if raw is not None and raw.get("type") != "session_info":
                    raw = dict(raw)
                    raw["id"] = hex_id(8)
                    raw["parentId"] = prev
                    lines.append(raw)
                    prev = raw["id"]
        return lines

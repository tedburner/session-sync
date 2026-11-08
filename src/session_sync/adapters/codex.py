"""OpenAI Codex CLI adapter: ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<sid>.jsonl(.zst)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .. import ir
from ..utils import (
    extract_text_content,
    ms_to_compact,
    ms_to_iso,
    now_ms,
    read_jsonl,
    truncate,
    zstd_decompress_file,
    write_jsonl,
)
from .base import Adapter, AdapterError, SessionSummary

_SID_RE = re.compile(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(?P<sid>[A-Za-z0-9-]+)\.jsonl(?:\.zst)?$")


def _lines_of(path: Path) -> list[dict]:
    if str(path).endswith(".zst"):
        data = zstd_decompress_file(path)
        out = []
        for line in data.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
    return read_jsonl(path)


def _output_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("output"), str):
            return value["output"]
        if value.get("content") is not None:
            return extract_text_content(value["content"])
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class CodexAdapter(Adapter):
    name = "codex"
    display_name = "Codex CLI"

    @classmethod
    def default_root(cls) -> Path | None:
        env = os.environ.get("CODEX_HOME")
        return Path(env).expanduser() if env else Path.home() / ".codex"

    # ---------------------------------------------------------------- reading

    def _sessions_root(self) -> Path:
        return self.root / "sessions"

    def _iter_files(self):
        sroot = self._sessions_root()
        if not sroot.exists():
            return
        for f in sorted(sroot.rglob("rollout-*.jsonl*")):
            if f.name.endswith(".jsonl") or f.name.endswith(".jsonl.zst"):
                yield f

    @staticmethod
    def _sid_of(path: Path) -> str | None:
        m = _SID_RE.search(path.name)
        return m.group("sid") if m else None

    def list_sessions(self, cwd: str | None = None) -> list[SessionSummary]:
        out: list[SessionSummary] = []
        for f in self._iter_files():
            sid = self._sid_of(f)
            if not sid:
                continue
            info = self._summarize_file(f, sid)
            if info and (cwd is None or info.cwd == cwd):
                out.append(info)
        out.sort(key=lambda s: s.updated_at or 0, reverse=True)
        return out

    def _summarize_file(self, path: Path, sid: str) -> SessionSummary | None:
        try:
            lines = _lines_of(path)
        except (OSError, RuntimeError):
            return None
        cwd = None
        created_at = updated_at = None
        count = 0
        first_text = ""
        for line in lines:
            t = line.get("type")
            ts = line.get("timestamp")
            if isinstance(ts, str):
                from ..utils import iso_to_ms

                ms = iso_to_ms(ts)
                if ms:
                    updated_at = ms
                    if created_at is None:
                        created_at = ms
            if t == "session_meta":
                payload = line.get("payload") or {}
                cwd = payload.get("cwd") or cwd
            elif t == "response_item":
                payload = line.get("payload") or {}
                if payload.get("type") == "message":
                    count += 1
                    if payload.get("role") == "user" and not first_text:
                        text = extract_text_content(payload.get("content"))
                        if text.strip():
                            first_text = truncate(text, 160)
        return SessionSummary(
            harness=self.name,
            id=sid,
            title=first_text or None,
            cwd=cwd,
            created_at=created_at,
            updated_at=updated_at,
            message_count=count,
            preview=first_text,
            path=str(path),
        )

    def read_session(self, sid: str) -> list[dict]:
        for f in self._iter_files():
            if self._sid_of(f) == sid:
                return self._parse_file(f, sid)
        raise AdapterError(f"Codex: session {sid} not found")

    def _parse_file(self, path: Path, sid: str) -> list[dict]:
        events: list[dict] = [
            {
                "type": "session_meta",
                "id": sid,
                "harness": self.name,
                "cwd": None,
                "title": None,
                "model": None,
                "created_at": None,
                "ext": {},
            }
        ]
        created_at = None
        n = 0
        skipped_event_msg = 0
        for line in _lines_of(path):
            t = line.get("type")
            ts = line.get("timestamp")
            ms = None
            if isinstance(ts, str):
                from ..utils import iso_to_ms

                ms = iso_to_ms(ts)
            if ms and (created_at is None or ms < created_at):
                created_at = ms
            if t == "session_meta":
                payload = line.get("payload") or {}
                events[0]["cwd"] = payload.get("cwd")
                events[0]["ext"]["session_meta"] = payload
            elif t == "response_item":
                payload = line.get("payload") or {}
                pt = payload.get("type")
                if pt == "message":
                    n += 1
                    role = payload.get("role", "user")
                    role = {"user": "user", "assistant": "assistant", "developer": "system", "system": "system"}.get(
                        role, "user"
                    )
                    blocks = [
                        {"type": "text", "text": b.get("text", "")}
                        for b in payload.get("content") or []
                        if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text")
                    ]
                    events.append(
                        {
                            "type": "message",
                            "id": payload.get("id") or f"{sid}-r{n}",
                            "parent_id": None,
                            "timestamp": ms,
                            "role": role,
                            "blocks": blocks,
                            "ext": {},
                        }
                    )
                elif pt in ("function_call", "custom_tool_call"):
                    n += 1
                    events.append(
                        {
                            "type": "message",
                            "id": payload.get("call_id") or f"{sid}-f{n}",
                            "parent_id": None,
                            "timestamp": ms,
                            "role": "assistant",
                            "blocks": [
                                {
                                    "type": "tool_call",
                                    "id": payload.get("call_id"),
                                    "name": payload.get("name"),
                                    "arguments": None,
                                    "arguments_raw": payload.get("arguments"),
                                }
                            ],
                            "ext": {},
                        }
                    )
                elif pt in ("function_call_output", "custom_tool_call_output"):
                    n += 1
                    events.append(
                        {
                            "type": "message",
                            "id": payload.get("call_id") or f"{sid}-fo{n}",
                            "parent_id": None,
                            "timestamp": ms,
                            "role": "tool",
                            "blocks": [
                                {
                                    "type": "tool_result",
                                    "call_id": payload.get("call_id"),
                                    "output": _output_text(payload.get("output")),
                                    "is_error": False,
                                    "name": None,
                                }
                            ],
                            "ext": {},
                        }
                    )
                elif pt == "reasoning":
                    events.append(
                        {
                            "type": "event",
                            "kind": "reasoning",
                            "timestamp": ms,
                            "data": payload,
                            "ext": {},
                        }
                    )
                else:
                    events.append(
                        {
                            "type": "event",
                            "kind": f"native:response_item:{pt}",
                            "timestamp": ms,
                            "data": {},
                            "ext": {"raw_line": line},
                        }
                    )
            elif t == "turn_context":
                events.append(
                    {"type": "event", "kind": "turn_context", "timestamp": ms, "data": line.get("payload") or {}, "ext": {}}
                )
            elif t == "event_msg":
                skipped_event_msg += 1
            else:
                events.append(
                    {"type": "event", "kind": f"native:{t}", "timestamp": ms, "data": {}, "ext": {"raw_line": line}}
                )
        meta = events[0]
        if created_at:
            meta["created_at"] = created_at
        meta["title"] = ir.first_user_text(events, 60) or None
        if skipped_event_msg:
            events[0]["ext"]["skipped_event_msg"] = skipped_event_msg
        return events

    # ---------------------------------------------------------------- writing

    def session_exists(self, sid: str) -> bool:
        return any(self._sid_of(f) == sid for f in self._iter_files())

    def write_session(
        self,
        events: list[dict],
        *,
        cwd: str | None = None,
        on_conflict: str = "skip",
        dry_run: bool = False,
    ) -> dict:
        from ..utils import backup_file, new_uuid, now_ms

        warnings: list[str] = []
        meta = ir.meta(events)
        sid = meta.get("id") or new_uuid()
        target_cwd = cwd or meta.get("cwd")
        if not target_cwd:
            raise AdapterError("target cwd unknown; pass --cwd or --cwd-map")

        existing = None
        for f in self._iter_files():
            if self._sid_of(f) == sid:
                existing = f
                break
        skipped = False
        backup = None
        if existing and on_conflict == "skip":
            skipped = True
        elif existing and on_conflict == "new-id":
            sid = new_uuid()
            existing = None
        elif existing and on_conflict == "overwrite" and not dry_run:
            backup = backup_file(existing)

        created = meta.get("created_at") or now_ms()
        day_dir = self._sessions_root() / ms_to_compact(created)[:10].replace("-", "/")
        path = day_dir / f"rollout-{ms_to_compact(created)}-{sid}.jsonl"
        if not skipped and not dry_run:
            lines = self._build_lines(events, sid, target_cwd, warnings)
            write_jsonl(path, lines)
            self._update_index(meta.get("title") or ir.first_user_text(events, 60), sid, warnings, dry_run=False)
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

    def _update_index(self, title: str | None, sid: str, warnings: list[str], dry_run: bool) -> None:
        try:
            index_path = self.root / "session_index.jsonl"
            index_path.parent.mkdir(parents=True, exist_ok=True)
            with open(index_path, "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {"id": sid, "thread_name": title or "Imported session", "updated_at": ms_to_iso(now_ms())},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError as exc:
            warnings.append(f"session_index.jsonl update failed: {exc}")

    def _build_lines(self, events: list[dict], sid: str, cwd: str, warnings: list[str]) -> list[dict]:
        ordinal = 0
        lines: list[dict] = []
        meta = ir.meta(events)
        created = meta.get("created_at")

        def stamp(ms):
            return ms_to_iso(ms or created)

        lines.append(
            {
                "timestamp": stamp(created),
                "type": "session_meta",
                "ordinal": ordinal,
                "payload": {
                    "session_id": sid,
                    "id": sid,
                    "timestamp": stamp(created),
                    "cwd": cwd,
                    "originator": "session-sync",
                    "cli_version": "session-sync",
                    "source": "cli",
                },
            }
        )
        ordinal += 1
        lines.append(
            {
                "timestamp": stamp(created),
                "type": "turn_context",
                "ordinal": ordinal,
                "payload": {"cwd": cwd, "approval_policy": "on-request", "sandbox_policy": {"type": "workspace-write"}},
            }
        )
        ordinal += 1

        for m in ir.messages(events):
            ts = stamp(m.get("timestamp"))
            role = m["role"]
            texts = [b.get("text", "") for b in m.get("blocks") or [] if b.get("type") == "text"]
            if role in ("user", "system", "assistant") and texts:
                api_role = "assistant" if role == "assistant" else ("developer" if role == "system" else "user")
                ctype = "output_text" if api_role == "assistant" else "input_text"
                lines.append(
                    {
                        "timestamp": ts,
                        "type": "response_item",
                        "ordinal": ordinal,
                        "payload": {
                            "type": "message",
                            "role": api_role,
                            "content": [{"type": ctype, "text": t} for t in texts if t],
                        },
                    }
                )
                ordinal += 1
            for b in m.get("blocks") or []:
                bt = b.get("type")
                if bt == "tool_call":
                    lines.append(
                        {
                            "timestamp": ts,
                            "type": "response_item",
                            "ordinal": ordinal,
                            "payload": {
                                "type": "function_call",
                                "name": b.get("name") or "",
                                "arguments": b.get("arguments_raw")
                                or json.dumps(b.get("arguments") or {}, ensure_ascii=False),
                                "call_id": b.get("id") or "",
                            },
                        }
                    )
                    ordinal += 1
                elif bt == "tool_result":
                    lines.append(
                        {
                            "timestamp": ts,
                            "type": "response_item",
                            "ordinal": ordinal,
                            "payload": {
                                "type": "function_call_output",
                                "call_id": b.get("call_id") or "",
                                "output": b.get("output", ""),
                            },
                        }
                    )
                    ordinal += 1
                elif bt == "thinking":
                    warnings.append("thinking block dropped (provider-private reasoning is not portable)")
                elif bt == "image":
                    warnings.append("image block dropped (codex writer)")
            if role == "tool":
                continue  # tool results already emitted as function_call_output items

        for e in events:
            if e.get("type") == "event" and e.get("kind", "").startswith("native:") and e.get("kind") != "native:session_meta":
                raw = (e.get("ext") or {}).get("raw_line")
                if raw is not None:
                    raw = dict(raw)
                    raw["ordinal"] = ordinal
                    lines.append(raw)
                    ordinal += 1
        return lines

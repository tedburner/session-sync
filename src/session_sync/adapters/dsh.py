"""DeepSeek Harness (dsh) adapter: ~/.dsh/sessions/<--cwd-->/session-<id>/session.v4.jsonl.zstd.

Format (verified against local data + official docs):
  line 1: {"type": "session", "version": 4, "id", "createdAt"(ms), "cwd", "isSeeded",
           "delegationDepth", "agentPreset"}
  then:   {"type", "seq", "time"(ms), "data"} event envelope with types like
          permission/preset, sandbox/mode, approval/policy, session/end-seed,
          user/message, assistant/message, tool/result, system/message,
          developer/message, turn/start, turn/end, step/*.
Writing is EXPERIMENTAL and gated behind allow_write=True (dsh validates events
fail-closed, so a wrong event refuses to load rather than corrupting data).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .. import ir
from ..utils import extract_text_content, new_uuid, zstd_decompress_file, zstd_compress_bytes
from .base import Adapter, AdapterError, SessionSummary

_SESSION_FILE_RE = re.compile(r"session\.(v\d+)\.jsonl(?:\.zstd|\.zst)?$")


class DshAdapter(Adapter):
    name = "dsh"
    display_name = "DeepSeek dsh"

    def __init__(self, root=None, allow_write: bool = False):
        super().__init__(root)
        self.allow_write = allow_write

    @classmethod
    def default_root(cls) -> Path | None:
        import os

        env = os.environ.get("DSH_HOME")
        return Path(env).expanduser() if env else Path.home() / ".dsh"

    def is_available(self) -> bool:
        return (self.root / "sessions").exists()

    # ---------------------------------------------------------------- reading

    def _workspace_index(self) -> dict:
        idx = self.root / "storages" / "workspace.json"
        if idx.exists():
            try:
                return json.loads(idx.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
        return {}

    def _workspace_paths(self) -> dict[str, str]:
        """session id -> workspace path."""
        out = {}
        for ws in (self._workspace_index().get("tables") or {}).get("workspaces", {}).values():
            for sid in ws.get("sessionIds") or []:
                out[sid] = ws.get("path")
        return out

    def _iter_session_dirs(self):
        sroot = self.root / "sessions"
        if not sroot.exists():
            return
        for d in sorted(sroot.glob("--*--/session-*")):
            if d.is_dir():
                yield d

    def _session_file(self, sid: str) -> Path | None:
        for d in self._iter_session_dirs():
            if d.name == f"session-{sid}" or d.name == sid:
                for f in sorted(d.iterdir()):
                    if _SESSION_FILE_RE.search(f.name):
                        return f
        return None

    def _read_lines(self, f: Path) -> list[dict]:
        if f.suffix in (".zstd", ".zst") or f.name.endswith(".zstd"):
            data = zstd_decompress_file(f)
            text = data.decode("utf-8", errors="replace")
        else:
            text = f.read_text(encoding="utf-8", errors="replace")
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def list_sessions(self, cwd: str | None = None) -> list[SessionSummary]:
        ws_paths = self._workspace_paths()
        out: list[SessionSummary] = []
        for d in self._iter_session_dirs():
            f = None
            for cand in sorted(d.iterdir()):
                if _SESSION_FILE_RE.search(cand.name):
                    f = cand
                    break
            if not f:
                continue
            sid = d.name.removeprefix("session-")
            try:
                lines = self._read_lines(f)
            except (OSError, RuntimeError):
                continue
            if not lines or lines[0].get("type") != "session":
                continue
            header = lines[0]
            s_cwd = header.get("cwd") or ws_paths.get(sid)
            if cwd is not None and s_cwd != cwd:
                continue
            count = sum(1 for e in lines[1:] if e.get("type", "").endswith("/message") or e.get("type") == "tool/result")
            first_text = ""
            for e in lines[1:]:
                if e.get("type") == "user/message":
                    data = e.get("data") or {}
                    msg = data.get("message") or data
                    text = extract_text_content(msg.get("content"))
                    if text.strip():
                        first_text = text.strip()[:160]
                        break
            out.append(
                SessionSummary(
                    harness=self.name,
                    id=sid,
                    title=first_text or None,
                    cwd=s_cwd,
                    created_at=header.get("createdAt"),
                    updated_at=max((e.get("time") for e in lines[1:] if isinstance(e.get("time"), int)), default=header.get("createdAt")),
                    message_count=count,
                    preview=first_text,
                    path=str(f),
                )
            )
        out.sort(key=lambda s: s.updated_at or 0, reverse=True)
        return out

    def read_session(self, sid: str) -> list[dict]:
        f = self._session_file(sid)
        if not f:
            raise AdapterError(f"dsh: session {sid} not found")
        lines = self._read_lines(f)
        if not lines or lines[0].get("type") != "session":
            raise AdapterError(f"dsh: {f} is not a session file")
        return self._parse(lines, sid, f)

    def session_exists(self, sid: str) -> bool:
        return self._session_file(sid) is not None

    def _parse(self, lines: list[dict], sid: str, f: Path) -> list[dict]:
        header = lines[0]
        meta = {
            "type": "session_meta",
            "id": header.get("id") or sid,
            "harness": self.name,
            "cwd": header.get("cwd"),
            "title": None,
            "model": None,
            "created_at": header.get("createdAt"),
            "ext": {k: header.get(k) for k in ("version", "isSeeded", "delegationDepth", "agentPreset") if header.get(k) is not None},
        }
        events: list[dict] = [meta]
        n = 0
        for e in lines[1:]:
            t = e.get("type")
            ts = e.get("time")
            data = e.get("data") or {}
            if t in ("user/message", "assistant/message", "system/message", "developer/message"):
                n += 1
                msg = data.get("message") if isinstance(data.get("message"), dict) else data
                role = {
                    "user/message": "user",
                    "assistant/message": "assistant",
                    "system/message": "system",
                    "developer/message": "system",
                }[t]
                blocks: list[dict] = []
                content = msg.get("content")
                if isinstance(content, str):
                    blocks.append({"type": "text", "text": content})
                elif isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt in ("text", "input_text", "output_text"):
                            blocks.append({"type": "text", "text": b.get("text", "")})
                        elif bt == "thinking":
                            blocks.append({"type": "thinking", "text": b.get("thinking", b.get("text", ""))})
                for call in msg.get("toolCalls") or []:
                    args = call.get("arguments")
                    blocks.append(
                        {"type": "tool_call", "id": call.get("id"), "name": call.get("name"),
                         "arguments": args if isinstance(args, (dict, type(None))) else None,
                         "arguments_raw": args if isinstance(args, str) else None}
                    )
                if msg.get("model") and not meta["model"]:
                    meta["model"] = msg.get("model")
                events.append(
                    {"type": "message", "id": f"{sid}-s{e.get('seq', n)}", "parent_id": None,
                     "timestamp": ts, "role": role, "blocks": blocks,
                     "ext": {"seq": e.get("seq"), "event_type": t}}
                )
            elif t == "tool/result":
                n += 1
                msg = data.get("message") if isinstance(data.get("message"), dict) else data
                output = msg.get("output") or msg.get("content") or ""
                if not isinstance(output, str):
                    output = extract_text_content(output) or json.dumps(output, ensure_ascii=False)
                events.append(
                    {"type": "message", "id": f"{sid}-s{e.get('seq', n)}", "parent_id": None,
                     "timestamp": ts, "role": "tool",
                     "blocks": [{"type": "tool_result", "call_id": msg.get("toolCallId") or msg.get("callId"),
                                 "output": output, "is_error": bool(msg.get("isError")),
                                 "name": msg.get("toolName")}],
                     "ext": {"seq": e.get("seq"), "event_type": t}}
                )
            elif t == "session/end-seed":
                continue
            else:
                events.append(
                    {"type": "event", "kind": f"native:{t}", "timestamp": ts, "data": data,
                     "ext": {"seq": e.get("seq"), "raw_event": e}}
                )
        if not meta["title"]:
            meta["title"] = ir.first_user_text(events, 60) or None
        return events

    # ---------------------------------------------------------------- writing (experimental)

    def write_session(
        self,
        events: list[dict],
        *,
        cwd: str | None = None,
        on_conflict: str = "skip",
        dry_run: bool = False,
    ) -> dict:
        from ..utils import backup_file, now_ms

        warnings: list[str] = ["dsh writer is EXPERIMENTAL: dsh validates sessions fail-closed;"
                               " if the event shape drifts, dsh will refuse to open the imported session"]
        if not self.allow_write:
            warnings.append("dsh write disabled (read-only by default); pass allow_write to enable")
            return {
                "harness": self.name, "target_id": None, "path": None, "backup": None,
                "created": False, "skipped": True, "dry_run": dry_run, "warnings": warnings,
            }
        meta = ir.meta(events)
        sid = meta.get("id") or f"session-{new_uuid()}"
        if not sid.startswith("session-"):
            sid = f"session-{sid}"
        target_cwd = cwd or meta.get("cwd")
        if not target_cwd:
            raise AdapterError("target cwd unknown; pass --cwd or --cwd-map")

        existing = self._session_file(sid)
        skipped = False
        backup = None
        if existing and on_conflict == "skip":
            skipped = True
        elif existing and on_conflict == "new-id":
            sid = f"session-{new_uuid()}"
            existing = None
        elif existing and on_conflict == "overwrite" and not dry_run:
            backup = backup_file(existing)

        sdir = self.root / "sessions" / ("--" + target_cwd.replace("/", "-").lstrip("-") + "--") / sid
        path = sdir / "session.v4.jsonl.zstd"
        if not skipped and not dry_run:
            backup = backup or backup_file(self.root / "storages" / "workspace.json")
            payload = self._build_lines(events, sid, target_cwd, warnings)
            sdir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(zstd_compress_bytes("\n".join(json.dumps(l, ensure_ascii=False) for l in payload).encode("utf-8") + b"\n"))
            self._register_workspace(sid, target_cwd, warnings)
        return {
            "harness": self.name, "target_id": sid, "path": str(path), "backup": backup,
            "created": not skipped and not dry_run, "skipped": skipped, "dry_run": dry_run,
            "warnings": warnings,
        }

    def _build_lines(self, events: list[dict], sid: str, cwd: str, warnings: list[str]) -> list[dict]:
        meta = ir.meta(events)
        created = meta.get("created_at") or now_ms()
        lines: list[dict] = [
            {
                "type": "session", "version": 4, "id": sid, "createdAt": created, "cwd": cwd,
                "isSeeded": False, "delegationDepth": 0, "agentPreset": "standard",
            }
        ]
        seq = 0
        for preset, mode, policy in (("workspace-write", "workspace-write", "ask"),):
            for etype, payload in (
                ("permission/preset", {"preset": preset}),
                ("sandbox/mode", {"mode": mode}),
                ("approval/policy", {"policy": policy}),
            ):
                lines.append({"type": etype, "seq": seq, "time": created, "data": payload})
                seq += 1
        turn = 1
        step = 1
        for m in ir.messages(events):
            ts = m.get("timestamp") or created
            role = m["role"]
            blocks = m.get("blocks") or []
            if role == "tool":
                for b in blocks:
                    if b.get("type") == "tool_result":
                        lines.append({
                            "type": "tool/result", "seq": seq, "time": ts,
                            "data": {"message": {
                                "toolCallId": b.get("call_id"), "toolName": b.get("name") or "tool",
                                "content": [{"type": "text", "text": b.get("output", "")}],
                                "isError": bool(b.get("is_error")),
                            }},
                        })
                        seq += 1
                continue
            content = []
            for b in blocks:
                if b.get("type") == "text":
                    content.append({"type": "text", "text": b.get("text", "")})
                elif b.get("type") == "thinking":
                    content.append({"type": "thinking", "thinking": b.get("text", "")})
                elif b.get("type") in ("tool_call", "tool_result", "image"):
                    warnings.append(f"block {b.get('type')} dropped (experimental dsh writer)")
            if not content:
                continue
            if role == "assistant":
                lines.append({
                    "type": "assistant/message", "seq": seq, "time": ts,
                    "data": {"turn": turn, "step": step, "message": {
                        "role": "assistant", "content": content,
                        "model": meta.get("model"),
                    }, "interrupted": False},
                })
                step += 1
            else:
                lines.append({
                    "type": ("user/message" if role == "user" else "system/message"),
                    "seq": seq, "time": ts, "data": {"message": {
                        "role": role, "content": "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text"),
                    }},
                })
            seq += 1
        return lines

    def _register_workspace(self, sid: str, cwd: str, warnings: list[str]) -> None:
        idx_path = self.root / "storages" / "workspace.json"
        try:
            idx = json.loads(idx_path.read_text(encoding="utf-8")) if idx_path.exists() else {
                "unit": {"name": "workspace", "version": 2},
                "global": {"initialized": True, "defaultWorkspaceId": None, "workspaceIds": [],
                           "archivedSessionIds": [], "pinnedSessionIds": []},
                "tables": {"workspaces": {}},
            }
            tables = idx.setdefault("tables", {}).setdefault("workspaces", {})
            ws = None
            for w in tables.values():
                if w.get("path") == cwd:
                    ws = w
                    break
            if ws is None:
                ws_id = new_uuid()
                tables[ws_id] = {"path": cwd, "title": Path(cwd).name, "sessionIds": [],
                                 "createdAt": None, "updatedAt": None}
                ws = tables[ws_id]
            ids = ws.setdefault("sessionIds", [])
            if sid not in ids:
                ids.append(sid)
            idx_path.parent.mkdir(parents=True, exist_ok=True)
            idx_path.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"workspace.json registration failed: {exc}")

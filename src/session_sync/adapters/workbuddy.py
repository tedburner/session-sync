"""WorkBuddy adapter: ~/.workbuddy/projects/<encoded-cwd>/<sid>.jsonl + workbuddy.db sessions index."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .. import ir
from ..utils import (
    extract_text_content,
    new_uuid,
    now_ms,
    truncate,
    ulid,
    write_jsonl,
)
from .base import Adapter, AdapterError, SessionSummary


def _encode_cwd(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


class WorkBuddyAdapter(Adapter):
    name = "workbuddy"
    display_name = "WorkBuddy"
    running_process_names = ["WorkBuddy", "workbuddy"]

    @classmethod
    def default_root(cls) -> Path | None:
        return Path.home() / ".workbuddy"

    @property
    def db_path(self) -> Path:
        return self.root / "workbuddy.db"

    def is_available(self) -> bool:
        return self.root.exists()

    # ---------------------------------------------------------------- reading

    def list_sessions(self, cwd: str | None = None) -> list[SessionSummary]:
        out: list[SessionSummary] = []
        # file-side facts
        files: dict[str, Path] = {}
        pdir = self.root / "projects"
        if pdir.exists():
            for f in sorted(pdir.glob("*/*.jsonl")):
                files[f.stem] = f
        # db-side metadata (title/times); db may be absent
        db_rows: dict[str, sqlite3.Row] = {}
        if self.db_path.exists():
            try:
                conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                try:
                    for row in conn.execute(
                        "SELECT id, cwd, title, custom_title, status, created_at, updated_at, model, mode,"
                        " transport FROM sessions WHERE deleted_at IS NULL OR deleted_at = -1"
                    ):
                        db_rows[row["id"]] = row
                finally:
                    conn.close()
            except sqlite3.Error:
                pass
        sids = set(files) | set(db_rows)
        for sid in sids:
            f = files.get(sid)
            row = db_rows.get(sid)
            s_cwd = row["cwd"] if row else None
            if cwd is not None and s_cwd != cwd:
                continue
            if f and (s_cwd is None):
                s_cwd = self._cwd_from_file(f)
            title = None
            if row:
                title = row["custom_title"] or row["title"]
            count = None
            preview = None
            if f:
                count, preview, file_cwd = self._scan_file(f)
                if s_cwd is None:
                    s_cwd = file_cwd
                title = title or (preview or None)
            out.append(
                SessionSummary(
                    harness=self.name,
                    id=sid,
                    title=title,
                    cwd=s_cwd,
                    created_at=row["created_at"] if row else None,
                    updated_at=row["updated_at"] if row else None,
                    message_count=count,
                    preview=preview,
                    path=str(f) if f else str(self.db_path),
                )
            )
        out.sort(key=lambda s: s.updated_at or 0, reverse=True)
        return out

    def _cwd_from_file(self, f: Path) -> str | None:
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("cwd"):
                        return obj["cwd"]
        except OSError:
            pass
        return None

    def _scan_file(self, f: Path):
        count = 0
        first_text = ""
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == "message":
                        count += 1
                        if not first_text and obj.get("role") == "user":
                            text = extract_text_content(
                                obj.get("content") or [{"type": "input_text", "text": ""}]
                            )
                            if text.strip():
                                first_text = truncate(text, 160)
        except OSError:
            pass
        return count, first_text, None

    def read_session(self, sid: str) -> list[dict]:
        f = self._session_file(sid)
        if not f:
            raise AdapterError(f"WorkBuddy: session {sid} not found")
        from ..utils import read_jsonl

        meta = {
            "type": "session_meta",
            "id": sid,
            "harness": self.name,
            "cwd": None,
            "title": None,
            "model": None,
            "created_at": None,
            "ext": {},
        }
        events: list[dict] = [meta]
        n = 0
        for line in read_jsonl(f):
            t = line.get("type")
            ts = line.get("timestamp")
            if isinstance(ts, (int, float)) and (meta["created_at"] is None or ts < meta["created_at"]):
                meta["created_at"] = int(ts)
            if line.get("cwd") and not meta["cwd"]:
                meta["cwd"] = line["cwd"]
            if t == "message":
                n += 1
                role = line.get("role", "user")
                blocks = [
                    {"type": "text", "text": b.get("text", "")}
                    for b in line.get("content") or []
                    if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text")
                ]
                ext = {}
                pd = line.get("providerData") or {}
                if pd.get("model") and not meta["model"]:
                    meta["model"] = pd["model"]
                if pd:
                    ext["providerData"] = pd
                if line.get("status"):
                    ext["status"] = line["status"]
                events.append(
                    {"type": "message", "id": line.get("id") or f"{sid}-w{n}", "parent_id": line.get("parentId"),
                     "timestamp": int(ts) if isinstance(ts, (int, float)) else None, "role": role,
                     "blocks": blocks, "ext": ext}
                )
            elif t == "function_call":
                n += 1
                events.append(
                    {"type": "message", "id": line.get("id") or line.get("callId") or f"{sid}-fc{n}",
                     "parent_id": line.get("parentId"), "timestamp": int(ts) if isinstance(ts, (int, float)) else None,
                     "role": "assistant",
                     "blocks": [{"type": "tool_call", "id": line.get("callId"), "name": line.get("name"),
                                 "arguments": None, "arguments_raw": line.get("arguments")}],
                     "ext": {}}
                )
            elif t == "function_call_result":
                n += 1
                output = line.get("output")
                if isinstance(output, dict):
                    output = output.get("text") or json.dumps(output, ensure_ascii=False)
                events.append(
                    {"type": "message", "id": line.get("id") or f"{sid}-fcr{n}",
                     "parent_id": line.get("parentId"), "timestamp": int(ts) if isinstance(ts, (int, float)) else None,
                     "role": "tool",
                     "blocks": [{"type": "tool_result", "call_id": line.get("callId"), "output": output or "",
                                 "is_error": line.get("status") == "error", "name": line.get("name")}],
                     "ext": {}}
                )
            elif t == "ai-title":
                meta["title"] = line.get("title") or meta["title"]
            elif t == "session-meta":
                continue
            else:
                events.append(
                    {"type": "event", "kind": f"native:{t}",
                     "timestamp": int(ts) if isinstance(ts, (int, float)) else None,
                     "data": {}, "ext": {"raw_line": line}}
                )
        if not meta["title"]:
            meta["title"] = ir.first_user_text(events, 60) or None
        return events

    def _session_file(self, sid: str) -> Path | None:
        pdir = self.root / "projects"
        if pdir.exists():
            for f in sorted(pdir.glob(f"*/{sid}.jsonl")):
                return f
        return None

    def session_exists(self, sid: str) -> bool:
        return self._session_file(sid) is not None

    # ---------------------------------------------------------------- writing

    def write_session(
        self,
        events: list[dict],
        *,
        cwd: str | None = None,
        on_conflict: str = "skip",
        dry_run: bool = False,
    ) -> dict:
        from ..utils import backup_file, backup_sqlite

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

        path = self.root / "projects" / _encode_cwd(target_cwd) / f"{sid}.jsonl"
        if not skipped and not dry_run:
            self.root.mkdir(parents=True, exist_ok=True)
            self._warn_running(warnings)
            backup = backup_sqlite(self.db_path)
            write_jsonl(path, self._build_lines(events, sid, target_cwd, warnings))
            self._insert_db_row(events, sid, target_cwd, warnings)
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
        created = meta.get("created_at") or now_ms()
        model = meta.get("model")
        lines: list[dict] = [
            {"type": "session-meta", "id": ulid(created), "sessionId": sid, "timestamp": created,
             "meta": {"imported_by": "session-sync"}}
        ]
        prev = None
        emitted_results: set[str] = set()

        def result_row(b, ts):
            return {
                "id": ulid(ts), "parentId": prev, "timestamp": ts, "type": "function_call_result",
                "name": b.get("name") or "tool", "callId": b.get("call_id"),
                "status": "error" if b.get("is_error") else "completed",
                "output": {"type": "text", "text": b.get("output", "")},
                "sessionId": sid, "cwd": cwd,
            }

        for m in ir.messages(events):
            ts = m.get("timestamp") or created
            mid = ulid(ts)
            blocks = m.get("blocks") or []
            if m["role"] == "tool":
                for b in blocks:
                    if b.get("type") == "tool_result" and b.get("call_id") not in emitted_results:
                        lines.append(result_row(b, ts))
                        emitted_results.add(b.get("call_id"))
                prev = lines[-1]["id"]
                continue
            texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            content_type = "output_text" if m["role"] == "assistant" else "input_text"
            if m["role"] == "assistant":
                for t in texts:
                    if t:
                        lines.append(
                            {"id": mid, "parentId": prev, "timestamp": ts, "type": "message", "role": "assistant",
                             "content": [{"type": content_type, "text": t}], "status": "completed",
                             "sessionId": sid, "cwd": cwd,
                             "providerData": {"agent": "cli", "model": model} if model else {"agent": "cli"}}
                        )
                        prev = mid
                for b in blocks:
                    if b.get("type") == "tool_call":
                        args = b.get("arguments_raw") or json.dumps(b.get("arguments") or {}, ensure_ascii=False)
                        lines.append(
                            {"id": ulid(ts), "parentId": prev, "timestamp": ts, "type": "function_call",
                             "name": b.get("name") or "", "callId": b.get("id"), "arguments": args,
                             "sessionId": sid, "cwd": cwd}
                        )
                        prev = lines[-1]["id"]
                    elif b.get("type") == "tool_result" and b.get("call_id") not in emitted_results:
                        lines.append(result_row(b, ts))
                        emitted_results.add(b.get("call_id"))
                        prev = lines[-1]["id"]
                if not texts and not any(b.get("type") == "tool_call" for b in blocks):
                    lines.append(
                        {"id": mid, "parentId": prev, "timestamp": ts, "type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": ""}], "status": "completed",
                         "sessionId": sid, "cwd": cwd}
                    )
                    prev = mid
            else:  # user / system
                lines.append(
                    {"id": mid, "parentId": prev, "timestamp": ts, "type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "\n".join(t for t in texts if t)}],
                     "status": "completed", "sessionId": sid, "cwd": cwd}
                )
                prev = mid
        return lines

    def _insert_db_row(self, events: list[dict], sid: str, cwd: str, warnings: list[str]) -> None:
        meta = ir.meta(events)
        db = self.db_path
        if not db.exists():
            warnings.append("workbuddy.db not found; jsonl written without index row")
            return
        created = meta.get("created_at") or now_ms()
        updated = max((m.get("timestamp") or created) for m in ir.messages(events)) if ir.messages(events) else created
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute("SELECT user_id FROM sessions LIMIT 1").fetchone()
            user_id = row[0] if row else "session-sync"
            try:
                conn.execute(
                    "INSERT INTO sessions (id, cwd, user_id, title, status, created_at, updated_at,"
                    " last_activity_at, deleted_at, mode, model, permission_mode, transport)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        sid,
                        cwd,
                        user_id,
                        meta.get("title") or "Imported session",
                        "Pending",
                        created,
                        updated,
                        updated,
                        None,
                        None,
                        meta.get("model"),
                        "default",
                        "local",
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                warnings.append(f"sessions row already exists for {sid}")
        finally:
            conn.close()

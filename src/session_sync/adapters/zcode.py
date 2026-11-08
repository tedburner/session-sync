"""ZCode adapter: ~/.zcode/cli/db/db.sqlite (OpenCode-style session/message/part schema)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .. import ir
from ..utils import new_uuid, now_ms, opencode_rand, zcode_project_id
from .base import Adapter, AdapterError, SessionSummary


def _connect(db: Path, readonly: bool = True) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


class ZCodeAdapter(Adapter):
    name = "zcode"
    display_name = "ZCode"
    running_process_names = ["ZCode", "zcode"]

    @classmethod
    def default_root(cls) -> Path | None:
        return Path.home() / ".zcode"

    @property
    def db_path(self) -> Path:
        return self.root / "cli" / "db" / "db.sqlite"

    def is_available(self) -> bool:
        return self.db_path.exists()

    # ---------------------------------------------------------------- reading

    def list_sessions(self, cwd: str | None = None) -> list[SessionSummary]:
        out: list[SessionSummary] = []
        db = self.db_path
        if not db.exists():
            return out
        try:
            conn = _connect(db)
        except sqlite3.Error:
            return out
        try:
            counts = dict(conn.execute("SELECT session_id, COUNT(*) FROM message GROUP BY session_id").fetchall())
            for row in conn.execute(
                "SELECT id, title, directory, time_created, time_updated, task_type FROM session "
                "WHERE time_deleted IS NULL ORDER BY time_updated DESC"
            ):
                if cwd is not None and row["directory"] != cwd:
                    continue
                out.append(
                    SessionSummary(
                        harness=self.name,
                        id=row["id"],
                        title=row["title"],
                        cwd=row["directory"],
                        created_at=row["time_created"],
                        updated_at=row["time_updated"],
                        message_count=counts.get(row["id"]),
                        preview=None,
                        path=str(db),
                    )
                )
        finally:
            conn.close()
        return out

    def read_session(self, sid: str) -> list[dict]:
        db = self.db_path
        if not db.exists():
            raise AdapterError(f"ZCode: db not found at {db}")
        conn = _connect(db)
        try:
            row = conn.execute("SELECT id, title, directory, time_created, time_updated, task_type FROM session WHERE id=?", (sid,)).fetchone()
            if not row:
                raise AdapterError(f"ZCode: session {sid} not found")
            meta = {
                "type": "session_meta",
                "id": sid,
                "harness": self.name,
                "cwd": row["directory"],
                "title": row["title"],
                "model": None,
                "created_at": row["time_created"],
                "ext": {"task_type": row["task_type"], "time_updated": row["time_updated"]},
            }
            events: list[dict] = [meta]
            for m in conn.execute(
                "SELECT id, time_created, time_updated, data FROM message WHERE session_id=? ORDER BY time_created, id",
                (sid,),
            ):
                data = json.loads(m["data"])
                role = data.get("role", "user")
                if role not in ("user", "assistant"):
                    role = "user"
                blocks: list[dict] = []
                native_parts = 0
                for p in conn.execute(
                    "SELECT data FROM part WHERE message_id=? ORDER BY COALESCE(sequence, time_created), id",
                    (m["id"],),
                ):
                    pdata = json.loads(p["data"])
                    pt = pdata.get("type")
                    if pt == "text":
                        block = {"type": "text", "text": pdata.get("text", "")}
                        if pdata.get("synthetic"):
                            block["ext"] = {"synthetic": True}
                        blocks.append(block)
                    elif pt == "reasoning":
                        blocks.append({"type": "thinking", "text": pdata.get("text", "")})
                    elif pt == "tool":
                        state = pdata.get("state") or {}
                        blocks.append(
                            {
                                "type": "tool_call",
                                "id": pdata.get("callID"),
                                "name": pdata.get("tool"),
                                "arguments": state.get("input"),
                                "arguments_raw": None,
                            }
                        )
                        if state.get("output") is not None:
                            out = state["output"]
                            if not isinstance(out, str):
                                out = json.dumps(out, ensure_ascii=False)
                            blocks.append(
                                {
                                    "type": "tool_result",
                                    "call_id": pdata.get("callID"),
                                    "output": out,
                                    "is_error": state.get("status") == "error",
                                    "name": pdata.get("tool"),
                                }
                            )
                    elif pt in ("step-start", "step-finish"):
                        continue
                    else:
                        native_parts += 1
                        events.append(
                            {
                                "type": "event",
                                "kind": f"native:part:{pt}",
                                "timestamp": m["time_created"],
                                "data": pdata,
                                "ext": {},
                            }
                        )
                ext = {}
                if data.get("modelSelection"):
                    ext["modelSelection"] = data["modelSelection"]
                    if not meta["model"]:
                        meta["model"] = data["modelSelection"].get("modelId")
                if data.get("agent"):
                    ext["agent"] = data["agent"]
                if native_parts:
                    ext["native_parts"] = native_parts
                events.append(
                    {
                        "type": "message",
                        "id": m["id"],
                        "parent_id": None,
                        "timestamp": m["time_created"],
                        "role": role,
                        "blocks": blocks,
                        "ext": ext,
                    }
                )
            return events
        finally:
            conn.close()

    # ---------------------------------------------------------------- writing

    def session_exists(self, sid: str) -> bool:
        if not self.db_path.exists():
            return False
        conn = _connect(self.db_path)
        try:
            return conn.execute("SELECT 1 FROM session WHERE id=?", (sid,)).fetchone() is not None
        finally:
            conn.close()

    def write_session(
        self,
        events: list[dict],
        *,
        cwd: str | None = None,
        on_conflict: str = "skip",
        dry_run: bool = False,
    ) -> dict:
        from ..utils import backup_sqlite

        warnings: list[str] = []
        meta = ir.meta(events)
        sid = meta.get("id") or f"sess_{new_uuid()}"
        target_cwd = cwd or meta.get("cwd")
        if not target_cwd:
            raise AdapterError("target cwd unknown; pass --cwd or --cwd-map")

        db = self.db_path
        exists = self.session_exists(sid)
        skipped = False
        backup = None
        if exists and on_conflict == "skip":
            skipped = True
        elif exists and on_conflict == "new-id":
            sid = f"sess_{new_uuid()}"
            exists = False
        elif exists and on_conflict == "overwrite" and not dry_run:
            backup = backup_sqlite(db)

        path = f"sqlite:{db}#session/{sid}"
        if not skipped and not dry_run:
            db.parent.mkdir(parents=True, exist_ok=True)
            if not db.exists():
                raise AdapterError(f"ZCode db not found; refusing to create: {db}")
            self._warn_running(warnings)
            backup = backup or backup_sqlite(db)
            self._insert(events, sid, target_cwd, warnings)
        return {
            "harness": self.name,
            "target_id": sid,
            "path": path,
            "backup": backup,
            "created": not skipped and not dry_run,
            "skipped": skipped,
            "dry_run": dry_run,
            "warnings": warnings,
        }

    def _insert(self, events: list[dict], sid: str, cwd: str, warnings: list[str]) -> None:
        meta = ir.meta(events)
        conn = _connect(self.db_path, readonly=False)
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM session WHERE id=?", (sid,))
            if cur.fetchone():
                raise AdapterError(f"ZCode: session {sid} appeared during write; aborting (conflict)")
            created = meta.get("created_at") or now_ms()
            updated = meta.get("ext", {}).get("time_updated") or created
            cur.execute(
                "INSERT INTO session (id, project_id, workspace_id, parent_id, slug, directory, path, title,"
                " version, permission, time_created, time_updated, task_type)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    zcode_project_id(cwd),
                    None,
                    None,
                    f"session_{new_uuid().replace('-', '')[:32]}",
                    cwd,
                    cwd,
                    meta.get("title") or "Imported session",
                    "0.17.0",
                    '{"mode":"build"}',
                    created,
                    updated,
                    "interactive",
                ),
            )
            msgs = ir.messages(events)
            call_ids = {
                b.get("id") for m in msgs for b in m.get("blocks") or [] if b.get("type") == "tool_call"
            }
            results: dict[str, dict] = {}
            for m in msgs:
                for b in m.get("blocks") or []:
                    if b.get("type") != "tool_result":
                        continue
                    if b.get("call_id") in call_ids:
                        results[b.get("call_id")] = b
                    else:
                        warnings.append(f"orphan tool_result {b.get('call_id')} dropped (no matching tool_call)")

            for m in msgs:
                if m["role"] == "tool":
                    continue  # merged into assistant tool parts
                ts = m.get("timestamp") or created
                data: dict = {"role": "user" if m["role"] in ("user", "system") else "assistant", "time": {"created": ts}}
                ext = m.get("ext") or {}
                if m["role"] == "assistant":
                    data["agent"] = ext.get("agent") or "zcode-agent"
                    ms = ext.get("modelSelection") or (
                        {"providerId": "session-sync", "modelId": meta.get("model")} if meta.get("model") else None
                    )
                    if ms:
                        data["modelSelection"] = ms
                data["semantics"] = {
                    "origin": "session-sync",
                    "kind": "message",
                    "uiVisibility": "visible",
                    "providerVisibility": "visible",
                }
                msg_id = f"msg_{opencode_rand(8)}_{new_uuid()}"
                cur.execute(
                    "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?,?,?,?,?)",
                    (msg_id, sid, ts, ts, json.dumps(data, ensure_ascii=False)),
                )
                for b in m.get("blocks") or []:
                    self._insert_part(cur, msg_id, sid, b, ts, results, warnings, call_ids)
            conn.commit()
        finally:
            conn.close()

    def _insert_part(self, cur, msg_id, sid, b, ts, results, warnings, call_ids=()) -> None:
        bt = b.get("type")
        part_id = f"part_{opencode_rand(8)}_{new_uuid()}"
        time_blob = {"start": ts, "end": ts}
        if bt == "text":
            data = {"type": "text", "text": b.get("text", ""), "time": time_blob}
            if (b.get("ext") or {}).get("synthetic"):
                data["synthetic"] = True
        elif bt == "thinking":
            data = {"type": "reasoning", "text": b.get("text", ""), "time": time_blob}
        elif bt == "tool_call":
            result = results.get(b.get("id"))
            out = result.get("output") if result else None
            state = {
                "status": "error" if result and result.get("is_error") else "completed",
                "input": b.get("arguments") if b.get("arguments") is not None else {"raw": b.get("arguments_raw") or ""},
                "title": b.get("name") or "tool",
                "time": time_blob,
            }
            if out is not None:
                state["output"] = out
            data = {"type": "tool", "callID": b.get("id"), "tool": b.get("name") or "tool", "state": state}
        elif bt == "tool_result":
            if b.get("call_id") in call_ids:
                return  # already merged into the matching tool part's state.output
            warnings.append(f"unpaired tool_result {b.get('call_id')} written as synthetic user text")
            data = {
                "type": "text",
                "text": f"[tool result {b.get('call_id')}] {str(b.get('output', ''))[:2000]}",
                "synthetic": True,
                "time": time_blob,
            }
        elif bt == "image":
            warnings.append("image block dropped (zcode writer)")
            return
        else:
            warnings.append(f"dropped block {bt} (zcode writer)")
            return
        cur.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?,?,?,?,?,?)",
            (part_id, msg_id, sid, ts, ts, json.dumps(data, ensure_ascii=False)),
        )

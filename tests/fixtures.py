"""Synthetic native fixtures for adapter tests."""

from __future__ import annotations

import json
import sqlite3

from session_sync.utils import ms_to_iso, ulid, zstd_compress_bytes, write_jsonl

CWD = "/tmp/proj-a"


# ---------------------------------------------------------------- claude / qoder


def claude_lines(sid: str) -> list[dict]:
    return [
        {"type": "summary", "summary": "Fix login bug", "leafUuid": "u4"},
        {
            "type": "user", "uuid": "u1", "parentUuid": None, "sessionId": sid,
            "timestamp": ms_to_iso(1700000000000), "cwd": CWD, "version": "1.0.0",
            "message": {"role": "user", "content": "please fix the login bug"},
        },
        {
            "type": "assistant", "uuid": "u2", "parentUuid": "u1", "sessionId": sid,
            "timestamp": ms_to_iso(1700000001000), "cwd": CWD, "version": "1.0.0",
            "message": {
                "role": "assistant", "model": "claude-x", "id": "msg_1",
                "content": [
                    {"type": "thinking", "thinking": "I will read auth.py"},
                    {"type": "text", "text": "Let me look at auth.py"},
                    {"type": "tool_use", "id": "tc1", "name": "Read", "input": {"file_path": "/tmp/auth.py"}},
                ],
            },
        },
        {
            "type": "user", "uuid": "u3", "parentUuid": "u2", "sessionId": sid,
            "timestamp": ms_to_iso(1700000002000), "cwd": CWD, "version": "1.0.0",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tc1", "content": [{"type": "text", "text": "def login(): pass"}], "is_error": False},
                ],
            },
        },
        {
            "type": "assistant", "uuid": "u4", "parentUuid": "u3", "sessionId": sid,
            "timestamp": ms_to_iso(1700000003000), "cwd": CWD, "version": "1.0.0",
            "message": {"role": "assistant", "model": "claude-x", "id": "msg_2",
                        "content": [{"type": "text", "text": "Found it, the token check is inverted."}]},
        },
    ]


def build_claude(root, sid="cla-s1"):
    pdir = root / "projects" / "-tmp-proj-a"
    pdir.mkdir(parents=True)
    write_jsonl(pdir / f"{sid}.jsonl", claude_lines(sid))
    return pdir / f"{sid}.jsonl"


def build_qoder(root, sid="qod-s1"):
    pdir = root / "projects" / "-tmp-proj-a"
    pdir.mkdir(parents=True)
    lines = [
        {"type": "workspace-directories", "sessionId": sid, "directories": [CWD]},
        {"type": "runtime-config", "sessionId": sid, "model": "dfmodel", "timestamp": 1700000000000},
    ] + [l for l in claude_lines(sid)]
    write_jsonl(pdir / f"{sid}.jsonl", lines)
    return pdir / f"{sid}.jsonl"


# ---------------------------------------------------------------- codex


def build_codex(root, sid="cod-s1"):
    day = root / "sessions" / "2026" / "11" / "08"
    day.mkdir(parents=True)
    lines = [
        {"timestamp": ms_to_iso(1700000000000), "type": "session_meta", "payload": {
            "session_id": sid, "id": sid, "cwd": CWD, "originator": "Codex CLI", "cli_version": "0.9"}},
        {"timestamp": ms_to_iso(1700000000000), "type": "turn_context", "payload": {"cwd": CWD}},
        {"timestamp": ms_to_iso(1700000000500), "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "please fix the login bug"}]}},
        {"timestamp": ms_to_iso(1700000001000), "type": "response_item", "payload": {
            "type": "reasoning", "summary": []}},
        {"timestamp": ms_to_iso(1700000001000), "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Looking at auth.py"}]}},
        {"timestamp": ms_to_iso(1700000001500), "type": "response_item", "payload": {
            "type": "function_call", "name": "shell", "arguments": json.dumps({"cmd": "cat auth.py"}), "call_id": "call_1"}},
        {"timestamp": ms_to_iso(1700000002000), "type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "call_1", "output": "def login(): pass"}},
        {"timestamp": ms_to_iso(1700000003000), "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Found the bug."}]}},
        {"timestamp": ms_to_iso(1700000003000), "type": "event_msg", "payload": {"type": "task_complete"}},
    ]
    write_jsonl(day / f"rollout-2026-11-08T10-00-00-{sid}.jsonl", lines)
    return day / f"rollout-2026-11-08T10-00-00-{sid}.jsonl"


def build_codex_with_native(root, sid="cod-s2"):
    """Codex rollout carrying native lines that are NOT portable to Qoder."""
    day = root / "sessions" / "2026" / "11" / "08"
    day.mkdir(parents=True)
    lines = [
        {"timestamp": ms_to_iso(1700000000000), "type": "session_meta", "payload": {
            "session_id": sid, "id": sid, "cwd": CWD, "originator": "Codex CLI", "cli_version": "0.9"}},
        {"timestamp": ms_to_iso(1700000000000), "type": "world_state", "payload": {"state": "x"}},
        {"timestamp": ms_to_iso(1700000000500), "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "please fix the login bug"}]}},
        {"timestamp": ms_to_iso(1700000001000), "type": "response_item", "payload": {
            "type": "ghost_snapshot", "data": "y"}},
        {"timestamp": ms_to_iso(1700000002000), "type": "token_usage_record", "payload": {"tokens": 1}},
        {"timestamp": ms_to_iso(1700000003000), "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Found the bug."}]}},
    ]
    write_jsonl(day / f"rollout-2026-11-08T10-00-00-{sid}.jsonl", lines)
    return day / f"rollout-2026-11-08T10-00-00-{sid}.jsonl"


# ---------------------------------------------------------------- zcode


def build_zcode_db(root):
    db = root / "cli" / "db" / "db.sqlite"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE session (
          id text primary key, project_id text, workspace_id text, parent_id text, slug text,
          directory text, path text, title text, version text, permission text,
          time_created integer, time_updated integer, task_type text, time_deleted integer);
        CREATE TABLE message (
          id text primary key, session_id text, time_created integer, time_updated integer,
          data text, sequence integer);
        CREATE TABLE part (
          id text primary key, message_id text, session_id text, time_created integer,
          time_updated integer, data text, sequence integer);
        """
    )
    sid = "sess_11111111-2222-3333-4444-555555555555"
    conn.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "proj-tmp-proj-a", None, None, "session_" + "a" * 32, CWD, CWD, "ZCode fixture",
         "0.17.0", '{"mode":"build"}', 1700000000000, 1700000003000, "interactive", None),
    )
    msgs = [
        ("msg_1", 1700000000000, {"role": "user", "time": {"created": 1700000000000}}, [
            {"type": "text", "text": "please fix the login bug", "time": {}}]),
        ("msg_2", 1700000001000, {"role": "assistant", "time": {"created": 1700000001000},
                                  "modelSelection": {"providerId": "p", "modelId": "zai-x"}}, [
            {"type": "reasoning", "text": "I will read auth.py", "time": {}},
            {"type": "text", "text": "Let me look at auth.py", "time": {}},
            {"type": "tool", "callID": "tc1", "tool": "Read",
             "state": {"status": "completed", "input": {"file_path": "/tmp/auth.py"},
                       "output": "def login(): pass", "title": "Read", "time": {}}}]),
        ("msg_3", 1700000003000, {"role": "assistant", "time": {"created": 1700000003000}}, [
            {"type": "text", "text": "Found it, the token check is inverted.", "time": {}}]),
    ]
    seq = 0
    for mid, ts, data, parts in msgs:
        conn.execute("INSERT INTO message VALUES (?,?,?,?,?,?)", (mid, sid, ts, ts, json.dumps(data), seq))
        seq += 1
        for pseq, pdata in enumerate(parts):
            conn.execute(
                "INSERT INTO part VALUES (?,?,?,?,?,?,?)",
                (f"part_{mid}_{pseq}", mid, sid, ts, ts, json.dumps(pdata), pseq),
            )
    conn.commit()
    conn.close()
    return sid


# ---------------------------------------------------------------- pi


def build_pi(root, sid="pi-s1"):
    pdir = root / "--tmp-proj-a--"
    pdir.mkdir(parents=True)
    lines = [
        {"type": "session", "version": 3, "id": sid, "timestamp": ms_to_iso(1700000000000), "cwd": CWD},
        {"type": "message", "id": "a1b2c3d4", "parentId": None, "timestamp": ms_to_iso(1700000000000),
         "message": {"role": "user", "content": "please fix the login bug", "timestamp": 1700000000000}},
        {"type": "message", "id": "b2c3d4e5", "parentId": "a1b2c3d4", "timestamp": ms_to_iso(1700000001000),
         "message": {"role": "assistant", "api": "openai-completions", "provider": "p", "model": "pi-x",
                     "content": [{"type": "text", "text": "Looking at auth.py"}],
                     "toolCalls": [{"id": "tc1", "name": "read", "arguments": {"path": "/tmp/auth.py"}}],
                     "timestamp": 1700000001000}},
        {"type": "message", "id": "c3d4e5f6", "parentId": "b2c3d4e5", "timestamp": ms_to_iso(1700000002000),
         "message": {"role": "toolResult", "toolCallId": "tc1", "toolName": "read",
                     "content": [{"type": "text", "text": "def login(): pass"}], "isError": False,
                     "timestamp": 1700000002000}},
        {"type": "message", "id": "d4e5f6a7", "parentId": "c3d4e5f6", "timestamp": ms_to_iso(1700000003000),
         "message": {"role": "assistant", "api": "openai-completions", "model": "pi-x",
                     "content": [{"type": "text", "text": "Found the bug."}], "timestamp": 1700000003000}},
    ]
    write_jsonl(pdir / f"2026-11-08T10-00-00-000Z_{sid}.jsonl", lines)
    return pdir / f"2026-11-08T10-00-00-000Z_{sid}.jsonl"


# ---------------------------------------------------------------- workbuddy


def build_workbuddy(root):
    pdir = root / "projects" / "tmp-proj-a"
    pdir.mkdir(parents=True)
    sid = "b2507d2d-1111-4222-8333-944455556666"
    lines = [
        {"type": "session-meta", "id": ulid(1700000000000), "sessionId": sid, "timestamp": 1700000000000,
         "meta": {}},
        {"id": ulid(1700000000100), "parentId": None, "timestamp": 1700000000100, "type": "message",
         "role": "user", "content": [{"type": "input_text", "text": "please fix the login bug"}],
         "status": "completed", "sessionId": sid, "cwd": CWD},
        {"id": ulid(1700000001000), "parentId": None, "timestamp": 1700000001000, "type": "message",
         "role": "assistant", "content": [{"type": "output_text", "text": "Looking at auth.py"}],
         "status": "completed", "sessionId": sid, "cwd": CWD,
         "providerData": {"agent": "cli", "model": "deepseek-v4.1-flash"}},
        {"id": ulid(1700000001500), "parentId": None, "timestamp": 1700000001500, "type": "function_call",
         "name": "Read", "callId": "call_1", "arguments": json.dumps({"path": "/tmp/auth.py"}),
         "sessionId": sid, "cwd": CWD},
        {"id": ulid(1700000002000), "parentId": None, "timestamp": 1700000002000, "type": "function_call_result",
         "name": "Read", "callId": "call_1", "status": "completed",
         "output": {"type": "text", "text": "def login(): pass"}, "sessionId": sid, "cwd": CWD},
        {"id": ulid(1700000003000), "parentId": None, "timestamp": 1700000003000, "type": "message",
         "role": "assistant", "content": [{"type": "output_text", "text": "Found the bug."}],
         "status": "completed", "sessionId": sid, "cwd": CWD},
        {"type": "ai-title", "title": "Fix login bug", "sessionId": sid},
    ]
    write_jsonl(pdir / f"{sid}.jsonl", lines)
    db = root / "workbuddy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE sessions (
          id TEXT PRIMARY KEY, cwd TEXT NOT NULL, user_id TEXT NOT NULL, title TEXT,
          custom_title TEXT, status TEXT, created_at INTEGER, updated_at INTEGER,
          last_activity_at INTEGER, deleted_at INTEGER, mode TEXT, model TEXT,
          permission_mode TEXT, transport TEXT)"""
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, CWD, "u1", "Fix login bug", None, "completed", 1700000000000, 1700000003000,
         1700000003000, None, "craft", "deepseek-v4.1-flash", "default", "local"),
    )
    conn.commit()
    conn.close()
    return sid


# ---------------------------------------------------------------- dsh


def build_dsh(root, sid="11111111-2222-3333-4444-555555555555"):
    sdir = root / "sessions" / "--tmp-proj-a--" / f"session-{sid}"
    sdir.mkdir(parents=True)
    header = {"type": "session", "version": 4, "id": f"session-{sid}", "createdAt": 1700000000000,
              "cwd": CWD, "isSeeded": False, "delegationDepth": 0, "agentPreset": "standard"}
    events = [
        {"type": "permission/preset", "seq": 0, "time": 1700000000000, "data": {"preset": "workspace-write"}},
        {"type": "user/message", "seq": 1, "time": 1700000000100,
         "data": {"message": {"role": "user", "content": "please fix the login bug"}}},
        {"type": "assistant/message", "seq": 2, "time": 1700000001000,
         "data": {"turn": 1, "step": 1, "interrupted": False,
                  "message": {"role": "assistant", "model": "deepseek-v4.1",
                              "content": [{"type": "text", "text": "Looking at auth.py"}]}}},
        {"type": "tool/result", "seq": 3, "time": 1700000002000,
         "data": {"message": {"toolCallId": "tc1", "toolName": "read",
                              "content": [{"type": "text", "text": "def login(): pass"}], "isError": False}}},
        {"type": "turn/end", "seq": 4, "time": 1700000003000, "data": {"reason": {"kind": "end_turn"}}},
    ]
    blob = "\n".join(json.dumps(x, ensure_ascii=False) for x in [header, *events]) + "\n"
    (sdir / "session.v4.jsonl.zstd").write_bytes(zstd_compress_bytes(blob.encode("utf-8")))
    (sdir / "session.lock").touch()
    return sid

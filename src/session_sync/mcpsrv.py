"""Minimal MCP server over stdio (line-delimited JSON-RPC 2.0, no dependencies).

Exposes: doctor, list_sessions, show_session, migrate_session.
Run with `session-sync mcp`. Testable via handle_message().
"""

from __future__ import annotations

import json
import sys
from traceback import format_exc

from . import __version__, ir
from .adapters import ADAPTER_ORDER, get_adapter
from .adapters.base import AdapterError
from .migrate import copy_session, print_manifest

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "doctor",
        "description": "Probe which agent harnesses are installed locally and where their session data lives.",
        "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string", "description": "project cwd to count sessions for"}}, "additionalProperties": False},
    },
    {
        "name": "list_sessions",
        "description": "List sessions of one harness (or all) for a project cwd, newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "harness": {"type": "string", "enum": ADAPTER_ORDER},
                "cwd": {"type": "string", "description": "project directory; omit for all"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "show_session",
        "description": "Render one session transcript as markdown.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "harness": {"type": "string", "enum": ADAPTER_ORDER},
                "session_id": {"type": "string"},
            },
            "required": ["harness", "session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "migrate_session",
        "description": "Copy a session from one harness to another (dry-run by default; returns a manifest).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_harness": {"type": "string", "enum": ADAPTER_ORDER},
                "session_id": {"type": "string"},
                "target_harness": {"type": "string", "enum": ADAPTER_ORDER},
                "cwd": {"type": "string", "description": "override target cwd"},
                "dry_run": {"type": "boolean", "default": True},
                "on_conflict": {"type": "string", "enum": ["skip", "new-id", "overwrite"], "default": "skip"},
            },
            "required": ["source_harness", "session_id", "target_harness"],
            "additionalProperties": False,
        },
    },
]


def _tool_dispatch(name: str, args: dict) -> str:
    if name == "doctor":
        from .discover import doctor, render_doctor

        return render_doctor(doctor(args.get("cwd")), args.get("cwd"))
    if name == "list_sessions":
        names = [args["harness"]] if args.get("harness") else ADAPTER_ORDER
        limit = args.get("limit") or 50
        out = []
        for n in names:
            adapter = get_adapter(n)
            if not adapter.is_available():
                continue
            for s in adapter.list_sessions(args.get("cwd"))[:limit]:
                out.append(s.as_dict())
        return json.dumps(out, ensure_ascii=False, indent=2)
    if name == "show_session":
        adapter = get_adapter(args["harness"])
        events = adapter.read_session(args["session_id"])
        return ir.render_markdown(events)
    if name == "migrate_session":
        manifest = copy_session(
            args["source_harness"],
            args["session_id"],
            args["target_harness"],
            cwd=args.get("cwd"),
            dry_run=bool(args.get("dry_run", True)),
            on_conflict=args.get("on_conflict", "skip"),
        )
        return print_manifest(manifest)
    raise AdapterError(f"unknown tool {name}")


class ServerState:
    def __init__(self):
        self.initialized = False


def handle_message(msg: dict, state: ServerState) -> dict | None:
    """Process one JSON-RPC request/notification; returns a response or None."""
    method = msg.get("method", "")
    msg_id = msg.get("id")
    is_notification = "id" not in msg

    if method == "initialize":
        state.initialized = True
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": msg.get("params", {}).get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "session-sync", "version": __version__},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            text = _tool_dispatch(name, args)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": False},
            }
        except Exception as exc:  # noqa: BLE001
            detail = f"{exc}" or format_exc()
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text", "text": f"error: {detail}"}], "isError": True},
            }
    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    state = ServerState()
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        resp = handle_message(msg, state)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> None:
    serve()

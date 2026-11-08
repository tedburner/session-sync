"""migrate.copy_session + CLI + MCP handler tests (sandboxed via ROOT_OVERRIDES)."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parent))

import fixtures  # noqa: E402

from session_sync import mcpsrv  # noqa: E402
from session_sync.adapters import ROOT_OVERRIDES  # noqa: E402
from session_sync.cli import main  # noqa: E402
from session_sync.ir import validate  # noqa: E402
from session_sync.migrate import copy_session  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmpobj = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpobj.name)
        self.src_root = self.tmp / "src"
        self.dst_root = self.tmp / "dst"
        self.src_root.mkdir(parents=True)
        self.dst_root.mkdir(parents=True)
        self.addCleanup(self._tmpobj.cleanup)
        self._old_overrides = dict(ROOT_OVERRIDES)
        ROOT_OVERRIDES.clear()

    def tearDown(self):
        ROOT_OVERRIDES.clear()
        ROOT_OVERRIDES.update(self._old_overrides)


class MigrateTest(Base):
    def test_claude_to_codex_dry_run_then_execute(self):
        fixtures.build_claude(self.src_root)
        ROOT_OVERRIDES["claude-code"] = str(self.src_root)
        ROOT_OVERRIDES["codex"] = str(self.dst_root)
        manifest = copy_session("claude-code", "cla-s1", "codex", dry_run=True)
        self.assertTrue(manifest["dry_run"])
        self.assertEqual(manifest["source"]["cwd"], fixtures.CWD)
        self.assertEqual(manifest["target"]["cwd"], fixtures.CWD)
        self.assertFalse(manifest["target"]["created"])
        self.assertEqual(manifest["stats"]["messages"], 4)

        manifest = copy_session("claude-code", "cla-s1", "codex", dry_run=False, on_conflict="skip")
        self.assertTrue(manifest["target"]["created"])
        from session_sync.adapters import get_adapter

        events = get_adapter("codex", root=self.dst_root).read_session(manifest["target"]["id"])
        self.assertEqual(validate(events), [])
        first_user = [b["text"] for m in events if m.get("type") == "message" and m.get("role") == "user"
                      for b in m.get("blocks", []) if b.get("type") == "text"][0]
        self.assertIn("please fix the login bug", first_user)

    def test_conflict_skip_and_new_id(self):
        fixtures.build_claude(self.src_root)
        ROOT_OVERRIDES["claude-code"] = str(self.src_root)
        ROOT_OVERRIDES["codex"] = str(self.dst_root)
        first = copy_session("claude-code", "cla-s1", "codex", dry_run=False)
        second = copy_session("claude-code", "cla-s1", "codex", dry_run=False, on_conflict="skip")
        self.assertTrue(second["target"]["skipped"])
        third = copy_session("claude-code", "cla-s1", "codex", dry_run=False, on_conflict="new-id")
        self.assertTrue(third["target"]["created"])
        self.assertNotEqual(first["target"]["id"], third["target"]["id"])

    def test_cwd_map(self):
        fixtures.build_claude(self.src_root)
        ROOT_OVERRIDES["claude-code"] = str(self.src_root)
        ROOT_OVERRIDES["codex"] = str(self.dst_root)
        manifest = copy_session(
            "claude-code", "cla-s1", "codex", dry_run=True,
            cwd_map={fixtures.CWD: "/tmp/proj-b"},
        )
        self.assertEqual(manifest["target"]["cwd"], "/tmp/proj-b")

    def test_zcode_to_pi(self):
        fixtures.build_zcode_db(self.src_root)
        ROOT_OVERRIDES["zcode"] = str(self.src_root)
        ROOT_OVERRIDES["pi"] = str(self.dst_root)
        from session_sync.adapters import get_adapter

        sid = get_adapter("zcode", root=self.src_root).list_sessions()[0].id
        manifest = copy_session("zcode", sid, "pi", dry_run=False)
        self.assertTrue(manifest["target"]["created"])
        events = get_adapter("pi", root=self.dst_root).read_session(manifest["target"]["id"])
        self.assertEqual(validate(events), [])


class CliTest(Base):
    def _run(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(list(argv))
        return code, buf.getvalue()

    def test_list_json(self):
        fixtures.build_claude(self.src_root)
        ROOT_OVERRIDES["claude-code"] = str(self.src_root)
        code, out = self._run("list", "--harness", "claude-code", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["harness"], "claude-code")

    def test_doctor(self):
        ROOT_OVERRIDES["claude-code"] = str(self.src_root)
        code, out = self._run("doctor", "--cwd", str(fixtures.CWD))
        self.assertEqual(code, 0)
        self.assertIn("Claude Code", out)

    def test_show(self):
        fixtures.build_claude(self.src_root)
        ROOT_OVERRIDES["claude-code"] = str(self.src_root)
        code, out = self._run("show", "claude-code", "cla-s1")
        self.assertEqual(code, 0)
        self.assertIn("please fix the login bug", out)
        self.assertIn("tool_call", out)

    def test_copy_dry_run_and_execute(self):
        fixtures.build_claude(self.src_root)
        ROOT_OVERRIDES["claude-code"] = str(self.src_root)
        ROOT_OVERRIDES["codex"] = str(self.dst_root)
        code, out = self._run("copy", "claude-code", "cla-s1", "--to", "codex")
        self.assertEqual(code, 0)
        self.assertIn("[dry-run]", out)
        code, out = self._run("copy", "claude-code", "cla-s1", "--to", "codex", "--execute")
        self.assertEqual(code, 0)
        self.assertIn("已写入", out)
        self.assertIn("rollout-", out)

    def test_report(self):
        fixtures.build_claude(self.src_root)
        ROOT_OVERRIDES["claude-code"] = str(self.src_root)
        out_path = self.tmp / "report.html"
        code, out = self._run("report", "--out", str(out_path))
        self.assertEqual(code, 0)
        html = out_path.read_text(encoding="utf-8")
        self.assertIn("session-sync", html)
        self.assertIn("cla-s1", html)


class McpTest(Base):
    def _rpc(self, state, method, params=None, msg_id=1):
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        return mcpsrv.handle_message(msg, state)

    def test_handshake_and_tools(self):
        state = mcpsrv.ServerState()
        resp = self._rpc(state, "initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(resp["result"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(self._rpc(state, "notifications/initialized", msg_id=None))
        resp = self._rpc(state, "tools/list")
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertEqual(names, {"doctor", "list_sessions", "show_session", "migrate_session"})

    def test_tools_call(self):
        fixtures.build_claude(self.src_root)
        ROOT_OVERRIDES["claude-code"] = str(self.src_root)
        ROOT_OVERRIDES["codex"] = str(self.dst_root)
        state = mcpsrv.ServerState()
        resp = self._rpc(state, "tools/call", {"name": "list_sessions", "arguments": {"harness": "claude-code"}})
        rows = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(rows[0]["id"], "cla-s1")
        resp = self._rpc(state, "tools/call", {
            "name": "migrate_session",
            "arguments": {"source_harness": "claude-code", "session_id": "cla-s1", "target_harness": "codex"},
        })
        self.assertIn("[dry-run]", resp["result"]["content"][0]["text"])
        resp = self._rpc(state, "tools/call", {"name": "nonsense", "arguments": {}})
        self.assertTrue(resp["result"]["isError"])


if __name__ == "__main__":
    unittest.main()

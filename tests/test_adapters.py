"""Adapter roundtrip tests: fixture -> IR -> write to sandbox -> read back -> compare.

Each side (source fixture / target write) gets its own sandbox root so that
harnesses sharing the same `projects/<enc>/` layout never collide.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parent))

import fixtures  # noqa: E402

from session_sync.adapters import get_adapter  # noqa: E402
from session_sync.adapters.dsh import DshAdapter  # noqa: E402
from session_sync.adapters.workbuddy import _encode_cwd  # noqa: E402


def texts(events, role=None):
    out = []
    for e in events:
        if e.get("type") == "message" and (role is None or e.get("role") == role):
            out.extend(b.get("text", "") for b in e.get("blocks") or [] if b.get("type") == "text")
    return out


def tool_calls(events):
    out = []
    for e in events:
        if e.get("type") == "message":
            for b in e.get("blocks") or []:
                if b.get("type") == "tool_call":
                    out.append((b.get("name"), b.get("id")))
    return out


def tool_results(events):
    out = []
    for e in events:
        if e.get("type") == "message":
            for b in e.get("blocks") or []:
                if b.get("type") == "tool_result":
                    out.append((b.get("call_id"), b.get("output")))
    return out


class RoundtripTest(unittest.TestCase):
    def setUp(self):
        self._tmpobj = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpobj.name)
        self.src_root = self.tmp / "src"
        self.dst_root = self.tmp / "dst"
        self.src_root.mkdir(parents=True)
        self.dst_root.mkdir(parents=True)
        self.addCleanup(self._tmpobj.cleanup)

    def _roundtrip(self, src_name, sid, dst_name):
        src = get_adapter(src_name, root=self.src_root)
        events = src.read_session(sid)
        self.assertGreater(len(texts(events, "user")), 0)
        self.assertIn("please fix the login bug", texts(events, "user")[0])
        self.assertTrue(tool_calls(events), f"{src_name} parse lost tool calls")
        dst = get_adapter(dst_name, root=self.dst_root)
        result = dst.write_session(events, on_conflict="skip", dry_run=False)
        self.assertTrue(result["created"], result)
        return events, dst.read_session(result["target_id"])

    # ------------------------------------------------------------ claude

    def test_claude_roundtrip(self):
        fixtures.build_claude(self.src_root)
        events, back = self._roundtrip("claude-code", "cla-s1", "claude-code")
        self.assertEqual(texts(events, "user"), texts(back, "user"))
        self.assertEqual(texts(events, "assistant"), texts(back, "assistant"))
        self.assertEqual(tool_calls(events), tool_calls(back))
        self.assertEqual(tool_results(events), tool_results(back))

    def test_claude_to_codex(self):
        fixtures.build_claude(self.src_root)
        self._roundtrip("claude-code", "cla-s1", "codex")

    def test_claude_to_zcode(self):
        fixtures.build_claude(self.src_root)
        fixtures.build_zcode_db(self.dst_root)
        self._roundtrip("claude-code", "cla-s1", "zcode")

    def test_claude_list_and_cwd_filter(self):
        fixtures.build_claude(self.src_root)
        adapter = get_adapter("claude-code", root=self.src_root)
        all_s = adapter.list_sessions()
        self.assertEqual(len(all_s), 1)
        self.assertEqual(all_s[0].cwd, fixtures.CWD)
        self.assertEqual(len(adapter.list_sessions(cwd="/elsewhere")), 0)
        self.assertIn("Fix login bug", all_s[0].title)

    # ------------------------------------------------------------ codex

    def test_codex_roundtrip(self):
        fixtures.build_codex(self.src_root)
        events, back = self._roundtrip("codex", "cod-s1", "codex")
        self.assertEqual(texts(events, "user"), texts(back, "user"))
        self.assertEqual(tool_calls(events), tool_calls(back))
        self.assertEqual(tool_results(events), tool_results(back))

    def test_codex_to_claude(self):
        fixtures.build_codex(self.src_root)
        events, back = self._roundtrip("codex", "cod-s1", "claude-code")
        self.assertIn("please fix the login bug", texts(back, "user")[0])
        self.assertEqual(tool_calls(events), tool_calls(back))

    def test_codex_zst_read(self):
        from session_sync.utils import zstd_compress_bytes

        path = fixtures.build_codex(self.src_root)
        raw = path.read_bytes()
        zst_path = path.with_name(path.name + ".zst")
        path.unlink()
        zst_path.write_bytes(zstd_compress_bytes(raw))
        adapter = get_adapter("codex", root=self.src_root)
        events = adapter.read_session("cod-s1")
        self.assertIn("please fix the login bug", texts(events, "user")[0])
        self.assertEqual(len(adapter.list_sessions()), 1)

    # ------------------------------------------------------------ zcode

    def test_zcode_roundtrip(self):
        fixtures.build_zcode_db(self.src_root)
        src = get_adapter("zcode", root=self.src_root)
        sid = src.list_sessions()[0].id
        events = src.read_session(sid)
        self.assertIn("please fix the login bug", texts(events, "user")[0])
        self.assertIn(("Read", "tc1"), tool_calls(events))
        self.assertEqual([r[0] for r in tool_results(events)], ["tc1"])
        result = src.write_session(events, on_conflict="new-id", dry_run=False)
        self.assertTrue(result["created"])
        back = src.read_session(result["target_id"])
        self.assertEqual(texts(events, "user"), texts(back, "user"))
        self.assertEqual(texts(events, "assistant"), texts(back, "assistant"))
        self.assertEqual(tool_calls(events), tool_calls(back))
        self.assertEqual(tool_results(events), tool_results(back))

    def test_zcode_conflict_and_dry_run(self):
        fixtures.build_zcode_db(self.src_root)
        src = get_adapter("zcode", root=self.src_root)
        sid = src.list_sessions()[0].id
        events = src.read_session(sid)
        result = src.write_session(events, on_conflict="skip", dry_run=False)
        self.assertTrue(result["skipped"])
        result = src.write_session(events, on_conflict="skip", dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["created"])

    def test_zcode_to_claude(self):
        fixtures.build_zcode_db(self.src_root)
        sid = get_adapter("zcode", root=self.src_root).list_sessions()[0].id
        self._roundtrip("zcode", sid, "claude-code")

    # ------------------------------------------------------------ qoder

    def test_qoder_roundtrip(self):
        fixtures.build_qoder(self.src_root)
        events, back = self._roundtrip("qoder", "qod-s1", "qoder")
        self.assertEqual(texts(events, "user"), texts(back, "user"))
        self.assertEqual(events[0]["cwd"], fixtures.CWD)
        # qoder meta lines roundtrip
        self.assertEqual(texts(back, "user"), texts(events, "user"))

    def test_qoder_cn_registry(self):
        from session_sync.adapters import REGISTRY

        self.assertIn("qoder-cn", REGISTRY)

    # ------------------------------------------------------------ pi

    def test_pi_roundtrip(self):
        fixtures.build_pi(self.src_root)
        events, back = self._roundtrip("pi", "pi-s1", "pi")
        self.assertEqual(texts(events, "user"), texts(back, "user"))
        self.assertEqual(tool_calls(events), tool_calls(back))
        self.assertEqual(tool_results(events), tool_results(back))

    def test_pi_to_claude(self):
        fixtures.build_pi(self.src_root)
        self._roundtrip("pi", "pi-s1", "claude-code")

    def test_pi_list_id_extraction(self):
        fixtures.build_pi(self.src_root)
        sessions = get_adapter("pi", root=self.src_root).list_sessions()
        self.assertEqual(sessions[0].id, "pi-s1")

    # ------------------------------------------------------------ workbuddy

    def test_workbuddy_roundtrip(self):
        fixtures.build_workbuddy(self.src_root)
        adapter = get_adapter("workbuddy", root=self.src_root)
        sessions = adapter.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].title, "Fix login bug")
        events = adapter.read_session(sessions[0].id)
        self.assertIn("please fix the login bug", texts(events, "user")[0])
        self.assertEqual(tool_calls(events), [("Read", "call_1")])
        result = adapter.write_session(events, on_conflict="new-id", dry_run=False)
        self.assertTrue(result["created"])
        back = adapter.read_session(result["target_id"])
        self.assertEqual(texts(events, "user"), texts(back, "user"))
        self.assertEqual(tool_calls(events), tool_calls(back))
        self.assertEqual(tool_results(events), tool_results(back))
        conn = sqlite3.connect(self.src_root / "workbuddy.db")
        n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        conn.close()
        self.assertEqual(n, 2)

    def test_workbuddy_to_claude(self):
        fixtures.build_workbuddy(self.src_root)
        self._roundtrip("workbuddy", "b2507d2d-1111-4222-8333-944455556666", "claude-code")

    def test_workbuddy_writes_to_workbuddy_project_key(self):
        fixtures.build_workbuddy(self.src_root)
        adapter = get_adapter("workbuddy", root=self.src_root)
        events = adapter.read_session("b2507d2d-1111-4222-8333-944455556666")
        result = adapter.write_session(events, cwd="/tmp/proj-a", on_conflict="new-id")
        expected = self.src_root / "projects" / "tmp-proj-a" / f"{result['target_id']}.jsonl"
        self.assertEqual(Path(result["path"]), expected)
        self.assertTrue(expected.exists())

    def test_workbuddy_path_key_matches_native_layout(self):
        self.assertEqual(
            _encode_cwd("/Users/kiturone/Documents/Project/person/session-sync"),
            "Users-kiturone-Documents-Project-person-session-sync",
        )
        self.assertEqual(_encode_cwd("/tmp//proj-a/"), "tmp-proj-a")
        self.assertEqual(_encode_cwd("C:\\work\\proj"), "C-work-proj")
        self.assertEqual(
            _encode_cwd("/Users/kiturone/WorkBuddy/闲聊"),
            "Users-kiturone-WorkBuddy-闲聊",
        )

    def test_workbuddy_path_key_truncates_long_paths(self):
        key = _encode_cwd("/" + "a" * 300)
        self.assertLessEqual(len(key.encode("utf-8")), 255)
        self.assertRegex(key, r"^a{180}-[0-9a-z]+$")

    # ------------------------------------------------------------ dsh

    def test_dsh_read(self):
        sid = fixtures.build_dsh(self.src_root)
        adapter = DshAdapter(root=self.src_root)
        sessions = adapter.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].cwd, fixtures.CWD)
        events = adapter.read_session(sid)
        self.assertIn("please fix the login bug", texts(events, "user")[0])
        self.assertIn("Looking at auth.py", texts(events, "assistant"))
        self.assertEqual([r[0] for r in tool_results(events)], ["tc1"])

    def test_dsh_write_disabled_by_default(self):
        sid = fixtures.build_dsh(self.src_root)
        adapter = DshAdapter(root=self.src_root)
        events = adapter.read_session(sid)
        result = adapter.write_session(events, on_conflict="skip", dry_run=False)
        self.assertTrue(result["skipped"])
        self.assertFalse(result["created"])

    def test_dsh_experimental_write_roundtrip(self):
        sid = fixtures.build_dsh(self.src_root)
        adapter = DshAdapter(root=self.src_root, allow_write=True)
        events = adapter.read_session(sid)
        result = adapter.write_session(events, on_conflict="new-id", dry_run=False)
        self.assertTrue(result["created"])
        back = adapter.read_session(result["target_id"])
        self.assertIn("please fix the login bug", texts(back, "user")[0])
        idx = json.loads((self.src_root / "storages" / "workspace.json").read_text())
        sids = [s for ws in idx["tables"]["workspaces"].values() for s in ws["sessionIds"]]
        self.assertIn(result["target_id"], sids)


class CodexTitleTest(unittest.TestCase):
    """Codex injects AGENTS.md/env/review text as user messages; titles must skip them."""

    def setUp(self):
        self._tmpobj = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpobj.name)
        self.addCleanup(self._tmpobj.cleanup)
        self.day = self.root / "sessions" / "2026" / "11" / "08"
        self.day.mkdir(parents=True)

    def _write(self, sid, user_texts):
        lines = [
            {"timestamp": "2026-11-08T10:00:00Z", "type": "session_meta",
             "payload": {"session_id": sid, "cwd": fixtures.CWD}}
        ]
        for text in user_texts:
            lines.append({
                "timestamp": "2026-11-08T10:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            })
        fixtures.write_jsonl(self.day / f"rollout-2026-11-08T10-00-00-{sid}.jsonl", lines)
        return get_adapter("codex", root=self.root)

    def test_unwraps_files_mentioned_request(self):
        adapter = self._write("cod-t1", [
            "# AGENTS.md instructions for /tmp/proj-a\n\n<INSTRUCTIONS>rules</INSTRUCTIONS>",
            "<environment_context>\n<cwd>/tmp/proj-a</cwd>\n</environment_context>",
            "# Files mentioned by the user:\n\n## request.md:\nbody\n\n## My request:\n修复登录 bug",
        ])
        summary = adapter.list_sessions()[0]
        self.assertEqual(summary.title, "修复登录 bug")
        self.assertEqual(summary.preview, "修复登录 bug")
        self.assertEqual(adapter.read_session("cod-t1")[0]["title"], "修复登录 bug")

    def test_internal_review_session_keeps_no_title(self):
        adapter = self._write("cod-t2", [
            "# AGENTS.md instructions for /tmp/proj-a\n\n<INSTRUCTIONS>rules</INSTRUCTIONS>",
            "The following is the Codex agent history added since your last approval assessment. ...",
            "[42] tool send_message call: {\"target\":\"x\"}",
            "## Code review guidelines:\n# Review Guidelines",
        ])
        summary = adapter.list_sessions()[0]
        self.assertIsNone(summary.title)
        self.assertEqual(summary.preview, "")
        self.assertIsNone(adapter.read_session("cod-t2")[0]["title"])

    def test_prefers_codex_index_thread_name(self):
        self._write("cod-t4", ["原始提问内容"])
        fixtures.write_jsonl(self.root / "session_index.jsonl", [
            {"id": "cod-t4", "thread_name": "修复登录问题", "updated_at": "2026-11-08T10:00:00Z"},
        ])
        adapter = get_adapter("codex", root=self.root)
        summary = adapter.list_sessions()[0]
        self.assertEqual(summary.title, "修复登录问题")
        self.assertEqual(summary.preview, "原始提问内容")
        self.assertEqual(adapter.read_session("cod-t4")[0]["title"], "修复登录问题")

    def test_uses_first_human_message(self):
        adapter = self._write("cod-t3", [
            "<turn_aborted> The user interrupted the previous turn",
            "<skill>\n<name>git-commit</name>\n</skill>",
            "继续审核",
        ])
        self.assertEqual(adapter.list_sessions()[0].title, "继续审核")


if __name__ == "__main__":
    unittest.main()

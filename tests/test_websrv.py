"""Local GUI server tests: auth, loopback guard, preview, plan/execute."""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import fixtures  # noqa: E402

from session_sync import websrv  # noqa: E402
from session_sync.adapters import ROOT_OVERRIDES, get_adapter  # noqa: E402
from session_sync.cli import main  # noqa: E402

TOKEN = "test-token"


class WebGuiTest(unittest.TestCase):
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
        fixtures.build_claude(self.src_root)
        ROOT_OVERRIDES["claude-code"] = str(self.src_root)
        ROOT_OVERRIDES["codex"] = str(self.dst_root)

        self.server = websrv.make_server(0, token=TOKEN)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.anon = urllib.request.build_opener()
        self.ui_state = self.tmp / "ui-state.json"
        self._old_ui_env = os.environ.get(websrv.UI_STATE_ENV)
        os.environ[websrv.UI_STATE_ENV] = str(self.ui_state)
        self.addCleanup(self._restore_ui_env)

    def _restore_ui_env(self):
        if self._old_ui_env is None:
            os.environ.pop(websrv.UI_STATE_ENV, None)
        else:
            os.environ[websrv.UI_STATE_ENV] = self._old_ui_env

    def tearDown(self):
        ROOT_OVERRIDES.clear()
        ROOT_OVERRIDES.update(self._old_overrides)

    def _stop(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()

    def _call(self, path, *, data=None, opener=None, headers=None):
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=body,
            method="POST" if data is not None else "GET",
        )
        if data is not None:
            req.add_header("Content-Type", "application/json")
            req.add_header("Origin", self.base)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with (opener or self.opener).open(req, timeout=5) as resp:
                return resp.status, resp.read().decode("utf-8"), resp.headers
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read().decode("utf-8"), exc.headers
            finally:
                exc.close()

    def _login(self):
        status, body, _ = self._call(f"/?token={TOKEN}")
        self.assertEqual(status, 200, body)
        return body

    # ------------------------------------------------------------------ auth

    def test_api_requires_token(self):
        status, _, _ = self._call("/api/sessions", opener=self.anon)
        self.assertEqual(status, 401)
        status, _, _ = self._call("/?token=wrong", opener=self.anon)
        self.assertEqual(status, 401)
        status, _, _ = self._call("/api/sessions?harness=claude-code", opener=self.anon,
                                  headers={"X-Session-Sync-Token": TOKEN})
        self.assertEqual(status, 200)
        status, _, _ = self._call("/api/pick-dir", data={}, opener=self.anon)
        self.assertEqual(status, 401)

    def test_index_sets_httponly_cookie(self):
        body = self._login()
        self.assertIn("session-sync", body)
        cookies = {c.name: c for c in self.jar}
        self.assertIn("ss_token", cookies)
        self.assertTrue(cookies["ss_token"].has_nonstandard_attr("HttpOnly"))
        status, payload, _ = self._call("/api/sessions?harness=claude-code")
        self.assertEqual(status, 200)
        rows = json.loads(payload)["sessions"]
        self.assertEqual([r["id"] for r in rows], ["cla-s1"])

    def test_rejects_foreign_host(self):
        self._login()
        status, _, _ = self._call("/api/sessions", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_rejects_cross_site_post(self):
        self._login()
        status, _, _ = self._call(
            "/api/plan",
            data={"src": "claude-code", "id": "cla-s1", "dst": "codex"},
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)

    # ------------------------------------------------------------------ data

    def test_session_preview(self):
        self._login()
        status, payload, _ = self._call("/api/session?harness=claude-code&id=cla-s1")
        self.assertEqual(status, 200)
        data = json.loads(payload)
        self.assertEqual(data["stats"]["messages"], 4)
        self.assertIn("please fix the login bug", json.dumps(data["messages"], ensure_ascii=False))
        self.assertEqual(data["warnings"], [])

    def test_plan_then_execute_requires_confirm(self):
        self._login()
        body = {"src": "claude-code", "id": "cla-s1", "dst": "codex"}
        status, payload, _ = self._call("/api/plan", data=body)
        self.assertEqual(status, 200)
        plan = json.loads(payload)
        self.assertTrue(plan["dry_run"])
        self.assertFalse(plan["target"]["created"])

        status, payload, _ = self._call("/api/copy", data=body)
        self.assertEqual(status, 400)
        self.assertIn("confirm", payload)

        status, payload, _ = self._call("/api/copy", data={**body, "confirm": True})
        self.assertEqual(status, 200)
        manifest = json.loads(payload)
        self.assertTrue(manifest["target"]["created"])
        events = get_adapter("codex", root=self.dst_root).read_session(manifest["target"]["id"])
        self.assertTrue(any(e.get("type") == "session_meta" for e in events))

    def test_dsh_write_blocked_by_default(self):
        self._login()
        status, payload, _ = self._call(
            "/api/plan", data={"src": "claude-code", "id": "cla-s1", "dst": "dsh"}
        )
        self.assertEqual(status, 403)
        self.assertIn("allow-dsh-write", payload)

    def test_unknown_session_returns_400(self):
        self._login()
        status, _, _ = self._call("/api/copy", data={
            "src": "claude-code", "id": "missing", "dst": "codex", "confirm": True,
        })
        self.assertEqual(status, 400)

    def test_pick_dir_endpoint(self):
        self._login()
        with mock.patch.object(websrv, "pick_directory", return_value={"path": str(self.tmp)}) as pick:
            status, payload, _ = self._call("/api/pick-dir", data={})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), {"path": str(self.tmp)})
        pick.assert_called_once_with()

        with mock.patch.object(websrv, "pick_directory", return_value={"cancelled": True}):
            status, payload, _ = self._call("/api/pick-dir", data={})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(payload)["cancelled"])

        with mock.patch.object(websrv, "pick_directory", side_effect=RuntimeError("no picker")):
            status, payload, _ = self._call("/api/pick-dir", data={})
        self.assertEqual(status, 501)
        self.assertIn("no picker", payload)

    # ------------------------------------------------------------------ recents

    def test_recent_records_persist_and_sort(self):
        self._login()
        for path in ("/tmp/proj-a", "/tmp/proj-a", "/tmp/proj-b"):
            status, payload, _ = self._call("/api/recent", data={"path": path})
            self.assertEqual(status, 200, payload)
        rows = json.loads(payload)["projects"]
        self.assertEqual([r["path"] for r in rows], ["/tmp/proj-a", "/tmp/proj-b"])
        self.assertEqual(rows[0]["uses"], 2)
        self.assertTrue(self.ui_state.exists())
        self.assertEqual(self.ui_state.stat().st_mode & 0o777, 0o600)

        status, payload, _ = self._call("/api/recent")
        self.assertEqual(status, 200, payload)
        self.assertEqual(
            [r["path"] for r in json.loads(payload)["projects"]],
            ["/tmp/proj-a", "/tmp/proj-b"],
        )

    def test_recent_migration_merges_without_double_count(self):
        self._login()
        self._call("/api/recent", data={"path": "/tmp/proj-a"})
        status, payload, _ = self._call(
            "/api/recent",
            data={"path": "/tmp/proj-a", "uses": 5, "lastUsed": 1700000000000},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(json.loads(payload)["projects"][0]["uses"], 5)

    def test_recent_expands_home(self):
        self._login()
        status, payload, _ = self._call("/api/recent", data={"path": "~/proj"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(
            json.loads(payload)["projects"][0]["path"],
            os.path.expanduser("~/proj"),
        )

    def test_recent_rejects_invalid_body(self):
        self._login()
        for body in ({"path": ""}, {"path": "/tmp/x", "uses": -1}, {"path": "/tmp/x", "lastUsed": "x"}):
            status, _, _ = self._call("/api/recent", data=body)
            self.assertEqual(status, 400, body)


class FolderPickerTest(unittest.TestCase):
    def test_macos_returns_normalized_path(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(websrv.sys, "platform", "darwin"), \
             mock.patch.object(websrv.shutil, "which", return_value="/usr/bin/osascript"), \
             mock.patch.object(websrv.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=tmp + "/\n", stderr="")
            self.assertEqual(websrv.pick_directory(), {"path": tmp})
        self.assertEqual(run.call_args.args[0][0], "osascript")

    def test_macos_cancel(self):
        with mock.patch.object(websrv.sys, "platform", "darwin"), \
             mock.patch.object(websrv.shutil, "which", return_value="/usr/bin/osascript"), \
             mock.patch.object(websrv.subprocess, "run") as run:
            run.return_value = mock.Mock(
                returncode=1, stdout="", stderr="execution error: User canceled. (-128)",
            )
            self.assertEqual(websrv.pick_directory(), {"cancelled": True})

    def test_no_picker_available(self):
        with mock.patch.object(websrv.sys, "platform", "aix"), \
             mock.patch.object(websrv.shutil, "which", return_value=None):
            with self.assertRaises(RuntimeError):
                websrv.pick_directory()


class BrowserLaunchTest(unittest.TestCase):
    def test_darwin_uses_chrome_app_mode(self):
        with mock.patch.object(websrv.sys, "platform", "darwin"), \
             mock.patch.object(websrv.shutil, "which", return_value="/usr/bin/open"), \
             mock.patch.object(websrv.os.path, "exists", return_value=True), \
             mock.patch.object(websrv.subprocess, "run") as run:
            self.assertEqual(websrv._open_app_window("http://127.0.0.1:1/?x"), "chrome")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:4], ["open", "-na", "Google Chrome", "--args"])
        self.assertEqual(cmd[4], "--app=http://127.0.0.1:1/?x")

    def test_falls_back_to_default_browser(self):
        with mock.patch.object(websrv.sys, "platform", "linux"), \
             mock.patch.object(websrv.shutil, "which", return_value=None), \
             mock.patch.object(websrv.webbrowser, "open") as open_browser:
            self.assertEqual(websrv._open_app_window("http://127.0.0.1:1/"), "browser")
        open_browser.assert_called_once_with("http://127.0.0.1:1/")


class ServerPortTest(unittest.TestCase):
    def test_busy_port_falls_back_to_ephemeral(self):
        first = websrv.make_server(0)
        self.addCleanup(first.server_close)
        second = websrv.make_server(first.server_address[1])
        self.addCleanup(second.server_close)
        self.assertNotEqual(second.server_address[1], first.server_address[1])


class UiCliTest(unittest.TestCase):
    def test_ui_command_wiring(self):
        with mock.patch("session_sync.websrv.run_ui", return_value=0) as run:
            code = main(["gui", "--no-open", "-p", "9999", "-H", "claude-code"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs["port"], 9999)
        self.assertFalse(run.call_args.kwargs["open_window"])
        self.assertEqual(run.call_args.kwargs["harness"], "claude-code")


if __name__ == "__main__":
    unittest.main()

"""Local GUI: Chrome app-mode client backed by a loopback-only HTTP server.

Security model:
- bind 127.0.0.1 only; reject foreign Host headers (DNS rebinding)
- per-run random token delivered as HttpOnly/SameSite=Strict cookie
- unsafe methods reject cross-site Origin / Sec-Fetch-Site
- writes always go through a dry-run preview and require an explicit confirm flag
- experimental dsh writes stay disabled unless the user opted in on the CLI
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, ir
from .adapters import ADAPTER_ORDER, get_adapter
from .adapters.base import AdapterError
from .htmlreport import build_transcripts
from .migrate import copy_session
from .utils import ms_to_iso, now_ms

TOKEN_COOKIE = "ss_token"
MAX_BODY = 1 << 20
DEFAULT_LIMIT = 400
CONFLICT_MODES = {"skip", "new-id", "overwrite"}
UI_STATE_ENV = "SESSION_SYNC_UI_STATE"
RECENT_LIMIT = 20

_UI_LOCK = threading.Lock()


def _embed(obj) -> str:
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def _ui_state_path() -> Path:
    override = os.environ.get(UI_STATE_ENV)
    return Path(override).expanduser() if override else Path.home() / ".session-sync" / "ui.json"


def _recent_score(item: dict, now: int) -> float:
    try:
        uses = float(item.get("uses") or 0)
        last_used = int(item.get("lastUsed") or 0)
    except (TypeError, ValueError):
        return 0.0
    age_days = max(0.0, (now - last_used) / 86_400_000)
    return uses / (1 + age_days / 14)


def _read_recent() -> list[dict]:
    try:
        data = json.loads(_ui_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("recent_projects") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("path"), str)]


def _write_recent(rows: list[dict]) -> None:
    path = _ui_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"recent_projects": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _list_recent() -> list[dict]:
    with _UI_LOCK:
        rows = _read_recent()
    now = now_ms()
    rows.sort(key=lambda row: _recent_score(row, now), reverse=True)
    return rows[:RECENT_LIMIT]


def _record_recent(path: str, uses: int | None = None, last_used: int | None = None) -> list[dict]:
    now = now_ms()
    with _UI_LOCK:
        rows = _read_recent()
        found = next((row for row in rows if row.get("path") == path), None)
        if found is None:
            found = {"path": path, "uses": 0, "lastUsed": 0}
            rows.append(found)
        if uses is None:
            found["uses"] = int(found.get("uses") or 0) + 1
            found["lastUsed"] = now
        else:
            # 迁移旧 localStorage：取 max 而不是累加，重复执行也安全
            found["uses"] = max(int(found.get("uses") or 0), uses)
            found["lastUsed"] = max(int(found.get("lastUsed") or 0), int(last_used or now))
        rows.sort(key=lambda row: _recent_score(row, now), reverse=True)
        rows = rows[:RECENT_LIMIT]
        _write_recent(rows)
    return rows


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, token: str, *, allow_dsh_write: bool, initial_cwd: str | None,
                 initial_harness: str | None):
        super().__init__(addr, _Handler)
        self.token = token
        self.allow_dsh_write = allow_dsh_write
        self.initial_cwd = initial_cwd
        self.initial_harness = initial_harness

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    server_version = f"session-sync/{__version__}"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ plumbing

    def log_message(self, fmt, *args):  # noqa: A003 - silence per-request logs
        pass

    def _send(self, status, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, payload) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status, message: str) -> None:
        self._json(status, {"error": message})

    def _query(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    def _cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:  # noqa: BLE001 - malformed cookie header
            return None
        morsel = jar.get(TOKEN_COOKIE)
        return morsel.value if morsel else None

    def _token_ok(self, query: dict) -> bool:
        supplied = (
            self.headers.get("X-Session-Sync-Token"),
            self._cookie_token(),
            (query.get("token") or [None])[0],
        )
        expected = self.server.token
        return any(s and hmac.compare_digest(s, expected) for s in supplied)

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _same_origin_ok(self) -> bool:
        port = self.server.server_address[1]
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            return False
        return self.headers.get("Sec-Fetch-Site") != "cross-site"

    def _guard(self, query: dict, *, unsafe: bool) -> bool:
        if not self._host_ok():
            self._error(HTTPStatus.FORBIDDEN, "非法 Host（仅允许 127.0.0.1）")
            return False
        if not self._token_ok(query):
            self._error(HTTPStatus.UNAUTHORIZED, "未授权：请使用启动时打印的带 token 的 URL")
            return False
        if unsafe and not self._same_origin_ok():
            self._error(HTTPStatus.FORBIDDEN, "拒绝跨站请求")
            return False
        return True

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "无效的 Content-Length")
            return None
        if length <= 0 or length > MAX_BODY:
            self._error(HTTPStatus.BAD_REQUEST, "请求体为空或过大")
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, f"JSON 解析失败: {exc}")
            return None
        if not isinstance(data, dict):
            self._error(HTTPStatus.BAD_REQUEST, "请求体必须是 JSON 对象")
            return None
        return data

    # ------------------------------------------------------------------ routing

    def do_GET(self) -> None:  # noqa: N802
        try:
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path in ("/", "/index.html"):
                self._serve_index(query)
                return
            if url.path == "/favicon.ico":
                self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
                return
            if not self._guard(query, unsafe=False):
                return
            if url.path == "/api/doctor":
                self._api_doctor()
            elif url.path == "/api/sessions":
                self._api_sessions(query)
            elif url.path == "/api/session":
                self._api_session(query)
            elif url.path == "/api/recent":
                self._json(HTTPStatus.OK, {"projects": _list_recent()})
            else:
                self._error(HTTPStatus.NOT_FOUND, "未知路径")
        except Exception as exc:  # noqa: BLE001 - keep the server alive
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:  # noqa: N802
        try:
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if not self._guard(query, unsafe=True):
                return
            body = self._read_json()
            if body is None:
                return
            if url.path == "/api/plan":
                self._api_copy(body, dry_run=True)
            elif url.path == "/api/copy":
                self._api_copy(body, dry_run=False)
            elif url.path == "/api/pick-dir":
                self._api_pick_dir()
            elif url.path == "/api/recent":
                self._api_recent(body)
            elif url.path == "/api/quit":
                self._json(HTTPStatus.OK, {"ok": True})
                threading.Timer(0.2, self.server.shutdown).start()
            else:
                self._error(HTTPStatus.NOT_FOUND, "未知路径")
        except Exception as exc:  # noqa: BLE001 - keep the server alive
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    # ------------------------------------------------------------------ endpoints

    def _serve_index(self, query: dict) -> None:
        cookie = self._cookie_token()
        token = (query.get("token") or [None])[0]
        if cookie and hmac.compare_digest(cookie, self.server.token):
            self._serve_page()
            return
        if token and hmac.compare_digest(token, self.server.token):
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.send_header(
                "Set-Cookie",
                f"{TOKEN_COOKIE}={self.server.token}; Path=/; HttpOnly; SameSite=Strict",
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            return
        self._send(
            HTTPStatus.UNAUTHORIZED,
            "未授权：请使用启动时打印的带 token 的 URL。".encode("utf-8"),
            "text/plain; charset=utf-8",
        )

    def _serve_page(self) -> None:
        boot = {
            "version": __version__,
            "cwd": self.server.initial_cwd or "",
            "harness": self.server.initial_harness or "",
        }
        html = _PAGE.replace("__BOOT__", _embed(boot)).encode("utf-8")
        csp = (
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'"
        )
        self._send(HTTPStatus.OK, html, "text/html; charset=utf-8", {
            "Content-Security-Policy": csp,
            "X-Frame-Options": "DENY",
        })

    def _api_doctor(self) -> None:
        rows = []
        for name in ADAPTER_ORDER:
            try:
                rows.append(get_adapter(name).describe())
            except Exception as exc:  # noqa: BLE001 - doctor must not crash
                rows.append({"name": name, "display_name": name, "root": None,
                             "available": False, "error": str(exc)})
        self._json(HTTPStatus.OK, {
            "adapters": rows,
            "allow_dsh_write": self.server.allow_dsh_write,
        })

    def _api_pick_dir(self) -> None:
        try:
            result = pick_directory()
        except RuntimeError as exc:
            self._error(HTTPStatus.NOT_IMPLEMENTED, str(exc))
            return
        self._json(HTTPStatus.OK, result)

    def _api_recent(self, body: dict) -> None:
        path = body.get("path")
        if not isinstance(path, str) or not path.strip():
            self._error(HTTPStatus.BAD_REQUEST, "path 必须是非空字符串")
            return
        uses = body.get("uses")
        last_used = body.get("lastUsed")
        if uses is not None and (not isinstance(uses, int) or isinstance(uses, bool) or uses < 1):
            self._error(HTTPStatus.BAD_REQUEST, "uses 必须是正整数")
            return
        if last_used is not None and (not isinstance(last_used, int) or isinstance(last_used, bool) or last_used < 0):
            self._error(HTTPStatus.BAD_REQUEST, "lastUsed 必须是非负整数")
            return
        path = os.path.abspath(os.path.expanduser(path.strip()))
        self._json(HTTPStatus.OK, {"projects": _record_recent(path, uses, last_used)})

    def _api_sessions(self, query: dict) -> None:
        requested = (query.get("harness") or [None])[0]
        if requested and requested not in ADAPTER_ORDER:
            self._error(HTTPStatus.BAD_REQUEST, f"未知 harness: {requested}")
            return
        cwd = (query.get("cwd") or [None])[0]
        if cwd:
            cwd = os.path.abspath(os.path.expanduser(cwd))
            if not os.path.isdir(cwd):
                self._error(HTTPStatus.BAD_REQUEST, f"目录不存在: {cwd}")
                return
        try:
            limit = int((query.get("limit") or [DEFAULT_LIMIT])[0])
        except ValueError:
            limit = DEFAULT_LIMIT
        limit = max(1, min(limit, 2000))

        names = [requested] if requested else list(ADAPTER_ORDER)
        rows, counts, errors = [], {}, {}
        for name in names:
            adapter = get_adapter(name)
            if not adapter.is_available():
                continue
            try:
                sessions = adapter.list_sessions(cwd)
            except Exception as exc:  # noqa: BLE001 - one broken harness must not hide others
                errors[name] = str(exc)
                continue
            counts[name] = len(sessions)
            rows.extend(sessions)
        rows.sort(key=lambda s: (s.updated_at or 0), reverse=True)
        rows = rows[:limit]
        self._json(HTTPStatus.OK, {
            "cwd": cwd,
            "sessions": [
                {
                    "harness": s.harness,
                    "id": s.id,
                    "title": s.title,
                    "cwd": s.cwd,
                    "updated": ms_to_iso(s.updated_at),
                    "created": ms_to_iso(s.created_at),
                    "message_count": s.message_count,
                    "preview": (s.preview or "")[:200],
                }
                for s in rows
            ],
            "counts": counts,
            "errors": errors,
        })

    def _api_session(self, query: dict) -> None:
        harness = (query.get("harness") or [""])[0]
        sid = (query.get("id") or [""])[0].strip()
        if harness not in ADAPTER_ORDER:
            self._error(HTTPStatus.BAD_REQUEST, f"未知 harness: {harness}")
            return
        if not sid:
            self._error(HTTPStatus.BAD_REQUEST, "缺少会话 ID")
            return
        try:
            events = get_adapter(harness).read_session(sid)
        except (AdapterError, OSError, ValueError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        meta = ir.meta(events)
        self._json(HTTPStatus.OK, {
            "harness": meta.get("harness") or harness,
            "id": meta.get("id") or sid,
            "title": ir.session_title(events),
            "cwd": meta.get("cwd"),
            "model": meta.get("model"),
            "created": ms_to_iso(meta.get("created_at")),
            "stats": ir.stats(events),
            "warnings": ir.validate(events),
            "messages": build_transcripts([events])[0],
        })

    def _api_copy(self, body: dict, *, dry_run: bool) -> None:
        src = body.get("src")
        dst = body.get("dst")
        sid = (body.get("id") or "").strip()
        if src not in ADAPTER_ORDER or dst not in ADAPTER_ORDER:
            self._error(HTTPStatus.BAD_REQUEST, "源或目标 harness 无效")
            return
        if src == dst:
            self._error(HTTPStatus.BAD_REQUEST, "源和目标不能相同")
            return
        if not sid:
            self._error(HTTPStatus.BAD_REQUEST, "缺少会话 ID")
            return
        on_conflict = body.get("on_conflict") or "skip"
        if on_conflict not in CONFLICT_MODES:
            self._error(HTTPStatus.BAD_REQUEST, f"无效冲突策略: {on_conflict}")
            return
        if dst == "dsh" and not self.server.allow_dsh_write:
            self._error(HTTPStatus.FORBIDDEN, "dsh 写入为实验性功能，请以 session-sync ui --allow-dsh-write 启动")
            return
        if not dry_run and body.get("confirm") is not True:
            self._error(HTTPStatus.BAD_REQUEST, "执行写入需要 confirm: true")
            return
        cwd = body.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            self._error(HTTPStatus.BAD_REQUEST, "cwd 必须是字符串")
            return
        cwd = os.path.expanduser(cwd) if cwd else None
        try:
            manifest = copy_session(src, sid, dst, cwd=cwd, on_conflict=on_conflict, dry_run=dry_run)
        except AdapterError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._json(HTTPStatus.OK, manifest)


# ---------------------------------------------------------------------- browser

def _open_app_window(url: str) -> str:
    """Open a standalone Chrome app window; fall back to the default browser."""
    if sys.platform == "darwin":
        if shutil.which("open") and os.path.exists("/Applications/Google Chrome.app"):
            try:
                subprocess.run(
                    ["open", "-na", "Google Chrome", "--args", f"--app={url}"],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return "chrome"
            except (OSError, subprocess.CalledProcessError):
                pass
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                     "microsoft-edge", "msedge"):
            binary = shutil.which(name)
            if binary:
                try:
                    subprocess.Popen(
                        [binary, f"--app={url}"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    return "chrome"
                except OSError:
                    pass
    webbrowser.open(url)
    return "browser"


def pick_directory() -> dict:
    """Open the OS folder picker; return {"path": ...} or {"cancelled": True}."""
    if sys.platform == "darwin" and shutil.which("osascript"):
        cmd = ["osascript", "-e", 'POSIX path of (choose folder with prompt "选择项目目录")']
    elif sys.platform.startswith("linux") and shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", "--directory", "--title=选择项目目录"]
    elif sys.platform.startswith("linux") and shutil.which("kdialog"):
        cmd = ["kdialog", "--getexistingdirectory", os.path.expanduser("~"), "--title", "选择项目目录"]
    else:
        raise RuntimeError("当前系统没有可用的文件夹选择器，请直接输入路径")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"无法打开文件夹选择器: {exc}") from exc
    out = (proc.stdout or "").strip()
    err = ((proc.stderr or "") + (proc.stdout or "")).strip()
    if proc.returncode != 0:
        if "-128" in err or "cancel" in err.lower():
            return {"cancelled": True}
        if cmd[0] in {"zenity", "kdialog"} and proc.returncode == 1:
            return {"cancelled": True}
        raise RuntimeError(f"文件夹选择器失败: {err or proc.returncode}")
    if not out:
        return {"cancelled": True}
    path = os.path.normpath(os.path.expanduser(out))
    if not os.path.isdir(path):
        raise RuntimeError(f"所选路径不是目录: {path}")
    return {"path": path}


# ---------------------------------------------------------------------- server

def make_server(
    port: int = 8765,
    *,
    token: str | None = None,
    allow_dsh_write: bool = False,
    initial_cwd: str | None = None,
    initial_harness: str | None = None,
) -> _Server:
    token = token or secrets.token_urlsafe(24)
    kwargs = dict(
        token=token,
        allow_dsh_write=allow_dsh_write,
        initial_cwd=initial_cwd,
        initial_harness=initial_harness,
    )
    try:
        return _Server(("127.0.0.1", port), **kwargs)
    except OSError:
        if not port:
            raise
        return _Server(("127.0.0.1", 0), **kwargs)


def run_ui(
    *,
    port: int = 8765,
    open_window: bool = True,
    cwd: str | None = None,
    harness: str | None = None,
    allow_dsh_write: bool = False,
) -> int:
    if allow_dsh_write:
        os.environ["SESSION_SYNC_DSH_WRITE"] = "1"
    initial_cwd = os.path.abspath(os.path.expanduser(cwd)) if cwd else os.getcwd()
    server = make_server(
        port,
        allow_dsh_write=allow_dsh_write,
        initial_cwd=initial_cwd,
        initial_harness=harness,
    )
    actual_port = server.server_address[1]
    if port and actual_port != port:
        print(f"⚠ 端口 {port} 被占用，已改用 {actual_port}", file=sys.stderr, flush=True)
    url = f"{server.origin}/?token={server.token}"
    print(f"🧩 session-sync GUI: {url}", flush=True)
    if open_window:
        kind = _open_app_window(url)
        if kind == "browser":
            print("（未找到 Chrome，已用默认浏览器打开）", flush=True)
    else:
        print("（--no-open：请手动打开上面的 URL）", flush=True)
    print("关闭窗口不会退出服务；可点左侧栏底部“退出”，或终端 Ctrl+C 结束。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        server.server_close()
    return 0


_PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>session-sync</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #f6f7f9;
  --panel: #ffffff;
  --text: #1f2328;
  --dim: #6b7280;
  --line: #e3e6ea;
  --accent: #2563eb;
  --accent-weak: #dbeafe;
  --user: #2563eb;
  --assistant: #059669;
  --warn: #b45309;
  --danger: #dc2626;
  --shadow: 0 1px 3px #00000012, 0 8px 24px #0000000a;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14161a;
    --panel: #1c1f24;
    --text: #e6e8eb;
    --dim: #9aa3ae;
    --line: #2b3038;
    --accent: #60a5fa;
    --accent-weak: #1e3a5f;
    --user: #60a5fa;
    --assistant: #34d399;
    --warn: #fbbf24;
    --danger: #f87171;
    --shadow: 0 1px 3px #00000055, 0 8px 24px #00000033;
  }
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0;
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "PingFang SC", "Segoe UI", sans-serif;
  background: var(--bg);
  color: var(--text);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
button, input, select { font: inherit; color: inherit; }
button {
  border: 1px solid var(--line);
  background: var(--panel);
  border-radius: 8px;
  padding: 6px 12px;
  cursor: pointer;
}
button:hover:not(:disabled) { border-color: var(--accent); }
button:disabled { opacity: .55; cursor: default; }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button.danger { background: var(--danger); border-color: var(--danger); color: #fff; }
button.ghost { background: transparent; }
input, select {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 6px 10px;
  outline: none;
}
input:focus, select:focus { border-color: var(--accent); }
header {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 10px 16px;
  background: var(--panel);
  border-bottom: 1px solid var(--line);
  flex-shrink: 0;
}
.brand { font-weight: 700; font-size: 15px; white-space: nowrap; }
.brand .ver { font-size: 11px; color: var(--dim); font-weight: 400; margin-left: 4px; }
.project { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; }
.project input { flex: 1; max-width: 620px; min-width: 160px; font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
.chk { display: inline-flex; align-items: center; gap: 5px; color: var(--dim); font-size: 12px; white-space: nowrap; }
.envbox { position: relative; margin-left: auto; }
.envbox summary { list-style: none; cursor: pointer; color: var(--dim); font-size: 12px; padding: 4px 8px; border: 1px solid var(--line); border-radius: 8px; }
.envbox summary::-webkit-details-marker { display: none; }
.env {
  position: absolute; right: 0; top: 34px; z-index: 20;
  width: 340px; max-height: 60vh; overflow: auto;
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 10px; box-shadow: var(--shadow);
  display: flex; flex-direction: column; gap: 7px;
}
.etag { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 12px; }
.etag .name { white-space: nowrap; }
.etag .root { color: var(--dim); font-family: ui-monospace, Menlo, monospace; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; direction: rtl; }
.etag.off { opacity: .45; }
main { flex: 1; display: flex; min-height: 0; }
aside {
  width: 340px; flex-shrink: 0; display: flex; flex-direction: column;
  border-right: 1px solid var(--line); background: var(--panel); min-height: 0;
}
.side-top { padding: 10px; display: flex; flex-direction: column; gap: 8px; border-bottom: 1px solid var(--line); }
.search { width: 100%; }
.counts { display: flex; flex-wrap: wrap; gap: 5px; }
.chip {
  border: 1px solid var(--line); background: transparent; color: var(--dim);
  border-radius: 999px; padding: 2px 9px; font-size: 11px;
}
.chip.on { background: var(--accent-weak); border-color: var(--accent); color: var(--accent); }
#list { flex: 1; overflow-y: auto; padding: 6px 8px 16px; }
.hgroup { font-size: 11px; font-weight: 700; color: var(--dim); letter-spacing: .04em; margin: 12px 6px 4px; text-transform: uppercase; }
.sess { padding: 8px 10px; border-radius: 8px; cursor: pointer; border: 1px solid transparent; }
.sess:hover { background: var(--bg); }
.sess.active { background: var(--accent-weak); border-color: var(--accent); }
.sess .t { font-weight: 600; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sess .m { font-size: 11px; color: var(--dim); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hint { color: var(--dim); font-size: 12px; padding: 14px 10px; }
#detail { flex: 1; overflow-y: auto; padding: 20px 26px 48px; min-width: 0; }
.empty { color: var(--dim); text-align: center; margin-top: 18vh; }
.dhead { display: flex; align-items: flex-start; gap: 12px; margin-bottom: 14px; }
.dtitle { flex: 1; min-width: 0; }
.dtitle h2 { font-size: 19px; margin: 0 0 4px; line-height: 1.3; }
.badge {
  display: inline-block; font-size: 11px; font-weight: 600; color: var(--accent);
  background: var(--accent-weak); border-radius: 6px; padding: 1px 7px; margin-bottom: 6px;
}
.sub { color: var(--dim); font-size: 12px; overflow-wrap: anywhere; }
.panel {
  background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
  padding: 14px 16px; margin: 14px 0; box-shadow: var(--shadow);
}
.panel-title { font-size: 12px; font-weight: 700; color: var(--dim); letter-spacing: .03em; margin-bottom: 10px; }
.panel-title .dim { font-weight: 400; margin-left: 8px; }
.mrow { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 10px; }
.mrow label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: var(--dim); }
.mrow select, .mrow input { min-width: 150px; }
.mrow input { min-width: 260px; font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
.plancard { margin-top: 12px; border: 1px dashed var(--line); border-radius: 10px; padding: 12px; }
.planhead { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.tag { font-size: 10px; font-weight: 700; color: var(--warn); border: 1px solid var(--warn); border-radius: 999px; padding: 0 7px; }
.route { font-family: ui-monospace, Menlo, monospace; font-size: 12px; margin-bottom: 10px; overflow-wrap: anywhere; }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 5px 14px; margin: 0; font-size: 12px; }
.kv dt { color: var(--dim); }
.kv dd { margin: 0; overflow-wrap: anywhere; font-family: ui-monospace, Menlo, monospace; }
.planactions { margin-top: 12px; display: flex; gap: 8px; }
.warnbox { margin-top: 10px; font-size: 12px; color: var(--warn); }
.warnbox summary { cursor: pointer; }
.warnbox ul { margin: 6px 0 0; padding-left: 18px; }
.warnbox li { margin: 2px 0; }
.errbox { margin-top: 10px; color: var(--danger); font-size: 12px; white-space: pre-wrap; }
.okbox { margin-top: 12px; border: 1px solid var(--assistant); border-radius: 10px; padding: 12px; }
.okbox .oktitle { color: var(--assistant); font-weight: 700; margin-bottom: 8px; }
.transcript { margin-top: 4px; }
.msg { margin: 12px 0; padding: 10px 14px; border-radius: 10px; background: var(--bg); border-left: 3px solid var(--line); }
.msg.user { border-left-color: var(--user); }
.msg.assistant { border-left-color: var(--assistant); }
.msg.system, .msg.tool, .msg.event { border-left-color: var(--dim); }
.msg .who { font-size: 11px; color: var(--dim); margin-bottom: 5px; }
.blk { white-space: pre-wrap; word-break: break-word; }
.blk + .blk { margin-top: 8px; }
.blk.tool, .blk.toolresult { background: #88888812; border-radius: 6px; padding: 6px 9px; }
.blk.toolresult.err { border-left: 3px solid var(--danger); }
.toolname { font-weight: 600; font-size: 12px; display: block; margin-bottom: 3px; }
pre { white-space: pre-wrap; word-break: break-word; margin: 0; font: 11px/1.45 ui-monospace, Menlo, monospace; }
details.thinking summary { cursor: pointer; color: var(--dim); font-size: 12px; }
details.thinking { color: var(--dim); border-left: 2px solid var(--line); padding-left: 8px; }
.dim { color: var(--dim); }
#toast {
  position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%) translateY(20px);
  background: #1f2328; color: #fff; padding: 9px 16px; border-radius: 9px;
  font-size: 12px; opacity: 0; pointer-events: none; transition: .18s ease;
  max-width: 70vw; z-index: 100; box-shadow: var(--shadow);
}
#toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
#toast.err { background: #b91c1c; }
#toast.ok { background: #047857; }
dialog {
  border: 1px solid var(--line); border-radius: 14px; padding: 20px 22px;
  background: var(--panel); color: var(--text); max-width: 560px; width: calc(100vw - 48px);
  box-shadow: var(--shadow);
}
dialog::backdrop { background: #00000055; }
dialog h3 { margin: 0 0 10px; font-size: 16px; }
dialog p { margin: 8px 0; color: var(--dim); font-size: 13px; }
dialog pre { background: var(--bg); border-radius: 8px; padding: 9px 11px; max-height: 180px; overflow: auto; }
.dlg-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
</style>
<style>
/* ---- airy light theme ---- */
:root {
  color-scheme: light;
  --canvas: #eef0f3;
  --surface: #ffffff;
  --surface-2: #f8f9fa;
  --surface-3: #f1f2f4;
  --text: #17191c;
  --text-2: #5f656e;
  --text-3: #8d939c;
  --line: #e8eaee;
  --line-strong: #d9dde3;
  --accent: #1b1d21;
  --on-accent: #ffffff;
  --accent-weak: #eff1f4;
  --user: #9db4f5;
  --assistant: #9dd3b8;
  --warn: #a97a2c;
  --danger: #b4554f;
  --shadow-sm: 0 1px 2px rgba(22, 26, 32, .04);
  --shadow-md: 0 1px 1px rgba(22, 26, 32, .03), 0 18px 50px rgba(22, 26, 32, .08);
  --sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Segoe UI", sans-serif;
  --serif: "Iowan Old Style", "Palatino Linotype", Palatino, "Songti SC", "Noto Serif SC", Georgia, serif;
  --mono: ui-monospace, "SF Mono", Menlo, monospace;
  /* legacy variables kept for shared rules */
  --bg: var(--canvas);
  --panel: var(--surface);
  --dim: var(--text-3);
}
body {
  display: block;
  height: 100dvh;
  padding: 12px;
  overflow: hidden;
  background:
    radial-gradient(1100px 700px at 12% -8%, #ffffff 0%, rgba(255, 255, 255, 0) 55%),
    radial-gradient(900px 700px at 105% 110%, #e5e9ef 0%, rgba(229, 233, 239, 0) 60%),
    var(--canvas);
  color: var(--text);
  font: 13px/1.55 var(--sans);
}
.app { display: flex; gap: 12px; height: 100%; min-height: 0; }
.sidebar {
  width: 336px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 0;
  background: rgba(255, 255, 255, .86);
  border: 1px solid rgba(255, 255, 255, .9);
  border-radius: 16px;
  box-shadow: var(--shadow-md);
  overflow: hidden;
  backdrop-filter: blur(24px) saturate(1.15);
}
.side-head { display: flex; align-items: center; gap: 10px; padding: 16px 16px 12px; }
.brand { display: flex; align-items: center; gap: 9px; font-size: 13px; font-weight: 600; letter-spacing: -.01em; }
.brand-mark {
  width: 26px; height: 26px; border-radius: 8px;
  background: var(--accent); color: var(--on-accent);
  display: grid; place-items: center; font-size: 13px; font-weight: 700;
}
.brand .ver { font-size: 10px; color: var(--text-3); font-weight: 500; margin-left: -2px; }
.project-box { padding: 4px 14px 12px; display: flex; flex-direction: column; gap: 8px; }
.field-label { font-size: 9.5px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; color: var(--text-3); }
.project-row { display: flex; align-items: center; gap: 8px; }
.project-row input {
  flex: 1; min-width: 0; height: 36px; border-radius: 11px;
  border: 1px solid var(--line); background: var(--surface-2);
  padding: 0 11px; font: 11.5px var(--mono); color: var(--text);
}
.project-row input:focus { background: var(--surface); border-color: var(--line-strong); box-shadow: 0 0 0 3px rgba(27, 29, 33, .05); }
.project-row .primary { height: 36px; border-radius: 11px; padding: 0 14px; flex-shrink: 0; }
.project-row .ghost { height: 36px; border-radius: 11px; padding: 0 9px; flex-shrink: 0; }
.recent { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.recent:empty { display: none; }
.rlabel { font-size: 9.5px; font-weight: 700; letter-spacing: .1em; color: var(--text-3); }
.rchip {
  max-width: 118px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  border: 1px solid transparent; background: var(--surface-3); color: var(--text-2);
  border-radius: 999px; padding: 3px 9px; font-size: 10.5px; font-weight: 500;
}
.rchip:hover { border-color: var(--line-strong); }
.rchip.on { background: var(--accent); color: var(--on-accent); }
.chk { font-size: 11px; color: var(--text-2); gap: 6px; }
.chk input { accent-color: var(--accent); }
.side-search { padding: 0 14px 8px; }
.search {
  width: 100%; height: 38px; margin: 0; border-radius: 12px;
  border: 1px solid transparent; background: var(--surface-3);
  padding: 0 12px; font-size: 12px;
}
.search:focus { background: var(--surface); border-color: var(--line-strong); box-shadow: 0 0 0 3px rgba(27, 29, 33, .05); }
.counts { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 14px 10px; }
.chip {
  border: 1px solid transparent; background: var(--surface-3); color: var(--text-2);
  border-radius: 999px; padding: 4px 10px; font-size: 10.5px; font-weight: 500;
}
.chip:hover { border-color: var(--line-strong); }
.chip.on { background: var(--accent); color: var(--on-accent); }
#list { flex: 1; overflow-y: auto; padding: 2px 8px 12px; }
.hgroup {
  font-size: 9.5px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase;
  color: var(--text-3); margin: 14px 8px 6px;
}
.sess { padding: 9px 10px; border-radius: 12px; border: 1px solid transparent; cursor: pointer; }
.sess:hover { background: var(--surface-2); }
.sess.active { background: var(--surface-3); border-color: var(--line); }
.sess .t { font-size: 13px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sess .m { font-size: 10.5px; color: var(--text-3); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hint { color: var(--text-3); font-size: 11.5px; padding: 14px 10px; }
.side-foot {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 10px 14px 12px; border-top: 1px solid var(--line);
}
.envbox { position: relative; margin-left: 0; }
.envbox summary {
  list-style: none; cursor: pointer; font-size: 11px; color: var(--text-3);
  padding: 4px 8px; border-radius: 8px; border: 1px solid transparent;
}
.envbox summary:hover { background: var(--surface-3); color: var(--text-2); }
.envbox summary::-webkit-details-marker { display: none; }
.env {
  position: absolute; left: 0; bottom: 36px; top: auto; right: auto;
  width: 320px; max-height: 50vh; overflow: auto;
  background: rgba(255, 255, 255, .96); border: 1px solid var(--line);
  border-radius: 14px; padding: 12px; box-shadow: var(--shadow-md);
  display: flex; flex-direction: column; gap: 7px;
  backdrop-filter: blur(20px);
}
.etag { font-size: 11px; }
.etag .root { color: var(--text-3); }
.etag.off { opacity: .45; }
.link-btn {
  border: none; background: transparent; color: var(--text-3);
  font-size: 11px; padding: 4px 8px; border-radius: 8px; height: auto;
}
.link-btn:hover { background: var(--surface-3); color: var(--text); }
#detail { display: block; flex: 1; min-width: 0; overflow-y: auto; padding: 36px 46px 64px; background: transparent; }
#detail > * { max-width: 920px; margin-left: auto; margin-right: auto; }
.hero, #detail > .empty {
  min-height: 62vh; display: flex; flex-direction: column;
  align-items: center; justify-content: center; text-align: center; gap: 0;
}
.hero-mark {
  width: 62px; height: 62px; border-radius: 19px; display: grid; place-items: center;
  background: linear-gradient(160deg, #ffffff, #eceff3);
  border: 1px solid #ffffff;
  box-shadow: 0 1px 1px rgba(22, 26, 32, .04), 0 14px 34px rgba(22, 26, 32, .08);
  color: var(--text); font-size: 25px; margin-bottom: 22px;
}
.hero h1 {
  font: 600 clamp(30px, 3.4vw, 42px)/1.15 var(--serif);
  letter-spacing: -.02em; margin: 0 0 12px; color: var(--text);
}
.hero p { margin: 0; max-width: 520px; color: var(--text-3); font-size: 12.5px; line-height: 1.7; }
#detail > .empty { font: 500 18px/1.5 var(--serif); color: var(--text-2); margin-top: 0; }
.dhead { display: flex; align-items: flex-start; gap: 16px; margin: 0 0 20px; }
.dtitle { flex: 1; min-width: 0; }
.dtitle h2 {
  font: 600 clamp(24px, 2.2vw, 32px)/1.2 var(--serif);
  letter-spacing: -.015em; margin: 8px 0 8px;
}
.badge {
  display: inline-flex; align-items: center;
  background: var(--surface-3); color: var(--text-2);
  border-radius: 999px; padding: 3px 9px;
  font-size: 9.5px; letter-spacing: .09em; text-transform: uppercase; font-weight: 700;
}
.sub { color: var(--text-3); font: 11px var(--mono); overflow-wrap: anywhere; }
.headbtns button { height: 32px; border-radius: 999px; font-size: 11.5px; }
button {
  border-radius: 10px; border: 1px solid var(--line); background: var(--surface);
  height: 34px; padding: 0 14px; font-size: 12px; font-weight: 500;
  transition: background .15s, border-color .15s, transform .05s;
}
button:hover:not(:disabled) { background: var(--surface-2); border-color: var(--line-strong); }
button:active:not(:disabled) { transform: translateY(1px); }
button.primary { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
button.primary:hover:not(:disabled) { background: #2a2d33; border-color: #2a2d33; }
button.danger { background: var(--danger); border-color: var(--danger); color: #fff; }
button.danger:hover:not(:disabled) { background: #9d4843; border-color: #9d4843; }
button.ghost { background: transparent; border-color: transparent; color: var(--text-2); }
button.ghost:hover:not(:disabled) { background: var(--surface-3); border-color: transparent; }
input, select {
  background: var(--surface-2); border: 1px solid var(--line);
  border-radius: 10px; padding: 0 10px; height: 36px; outline: none;
}
input:focus, select:focus { border-color: var(--line-strong); box-shadow: 0 0 0 3px rgba(27, 29, 33, .05); }
.panel {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: 18px; padding: 18px 20px; margin: 16px 0;
  box-shadow: var(--shadow-sm);
}
.panel-title {
  font-size: 10px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase;
  color: var(--text-3); margin-bottom: 12px;
}
.panel-title .dim { text-transform: none; letter-spacing: 0; font-weight: 400; }
.export-fields { display: flex; flex-direction: column; gap: 12px; margin-top: 4px; }
.export-fields label { display: flex; flex-direction: column; gap: 6px; font-size: 9.5px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--text-3); }
.export-fields select, .export-fields input { width: 100%; font-size: 12px; }
.export-fields input { font-family: var(--mono); font-size: 11.5px; }
.plancard { margin-top: 14px; background: var(--surface-2); border: 1px solid var(--line); border-radius: 14px; padding: 14px 16px; }
.planhead { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.tag {
  font-size: 9px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
  color: var(--warn); border: 1px solid #ead9bd; background: #fdf8ee;
  border-radius: 999px; padding: 1px 8px;
}
.route { font: 11.5px/1.5 var(--mono); color: var(--text-2); margin-bottom: 10px; overflow-wrap: anywhere; }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 6px 16px; margin: 0; font-size: 11px; }
.kv dt { color: var(--text-3); }
.kv dd { margin: 0; color: var(--text-2); font: 11px var(--mono); overflow-wrap: anywhere; }
.warnbox { font-size: 11.5px; color: var(--warn); margin-top: 12px; }
.warnbox summary { cursor: pointer; font-weight: 600; }
.warnbox ul { margin: 6px 0 0; padding-left: 18px; }
.warnbox li { margin: 2px 0; }
.errbox { font-size: 11.5px; color: var(--danger); margin-top: 10px; white-space: pre-wrap; }
.okbox { margin-top: 12px; border: 1px solid #cfe3d8; background: #fbfdfb; border-radius: 14px; padding: 14px 16px; }
.okbox .oktitle { color: #3f8463; font-weight: 700; margin-bottom: 10px; }
.transcript { margin-top: 2px; }
.transcript .empty { margin-top: 14px; }
.msg {
  margin: 10px 0; padding: 12px 16px; border-radius: 14px;
  background: var(--surface-2); border: 1px solid var(--line);
}
.msg.user { background: var(--surface-2); border-left: 2px solid var(--user); }
.msg.assistant { background: var(--surface-2); border-left: 2px solid var(--assistant); }
.msg.system, .msg.tool, .msg.event { background: var(--surface-2); border-left: 2px solid var(--line-strong); }
.msg .who {
  display: flex; align-items: center; gap: 6px;
  font-size: 9.5px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
  color: var(--text-3); margin-bottom: 7px;
}
.msg .who::before { content: ""; width: 5px; height: 5px; border-radius: 50%; background: var(--line-strong); }
.msg.user .who::before { background: var(--user); }
.msg.assistant .who::before { background: var(--assistant); }
.blk { font-size: 13px; line-height: 1.65; }
.blk.tool, .blk.toolresult { background: var(--surface-3); border-radius: 9px; padding: 8px 10px; }
pre { font: 11px/1.5 var(--mono); }
details.thinking { border-left: 2px solid var(--line); }
dialog { border: 1px solid var(--line); border-radius: 20px; padding: 24px 26px; background: var(--surface); box-shadow: 0 30px 80px rgba(15, 18, 23, .28); }
dialog::backdrop { background: rgba(18, 21, 26, .28); backdrop-filter: blur(4px); }
dialog h3 { font: 600 22px/1.25 var(--serif); }
dialog p { font-size: 12px; }
dialog pre { background: var(--surface-2); border: 1px solid var(--line); border-radius: 10px; }
#toast { background: #1b1d21; color: #fff; border-radius: 999px; padding: 10px 18px; font-size: 11.5px; box-shadow: 0 12px 30px rgba(0, 0, 0, .18); }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: #d9dde3; border-radius: 999px; border: 3px solid transparent; background-clip: content-box; }
::-webkit-scrollbar-track { background: transparent; }
@media (max-width: 860px) {
  .sidebar { width: 300px; }
  #detail { padding: 24px 24px 48px; }
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --canvas: #0f1114;
    --surface: #181a1e;
    --surface-2: #1d2025;
    --surface-3: #23272d;
    --text: #eef0f3;
    --text-2: #b4bac3;
    --text-3: #858c96;
    --line: #2a2e35;
    --line-strong: #373c44;
    --accent: #f2f4f7;
    --on-accent: #141619;
    --accent-weak: #23272d;
    --user: #6f8fe8;
    --assistant: #63b98e;
    --warn: #d3a45a;
    --danger: #c96a63;
    --shadow-sm: 0 1px 2px rgba(0, 0, 0, .3);
    --shadow-md: 0 1px 1px rgba(0, 0, 0, .25), 0 18px 50px rgba(0, 0, 0, .35);
    --bg: var(--canvas);
    --panel: var(--surface);
    --dim: var(--text-3);
  }
  body {
    background:
      radial-gradient(1100px 700px at 12% -8%, #1a1d22 0%, rgba(26, 29, 34, 0) 55%),
      radial-gradient(900px 700px at 105% 110%, #12161c 0%, rgba(18, 22, 28, 0) 60%),
      var(--canvas);
  }
  .sidebar { background: rgba(24, 26, 30, .86); border-color: rgba(255, 255, 255, .06); }
  .hero-mark { background: linear-gradient(160deg, #22262c, #181a1e); border-color: #2a2e35; }
  .env { background: rgba(24, 26, 30, .96); }
  .tag { background: #2a2418; border-color: #3d3423; }
  .okbox { background: #16201a; border-color: #2c4636; }
  .okbox .oktitle { color: #7fc29b; }
  button.primary:hover:not(:disabled) { background: #dfe3e9; border-color: #dfe3e9; }
  ::-webkit-scrollbar-thumb { background: #333840; }
}
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="side-head">
      <div class="brand"><span class="brand-mark">⇄</span><span>session-sync</span><span class="ver" id="ver"></span></div>
    </div>
    <div class="project-box">
      <div class="field-label">项目目录</div>
      <div class="project-row">
        <input id="cwd" placeholder="项目目录（默认当前项目）" spellcheck="false">
        <button id="pick" class="ghost" title="打开系统文件夹选择器切换项目">浏览</button>
        <button id="scan" class="primary">扫描</button>
      </div>
      <div class="recent" id="recent"></div>
      <label class="chk"><input type="checkbox" id="allProjects"> 全部项目</label>
    </div>
    <div class="side-search">
      <input class="search" id="q" placeholder="搜索标题 / ID / 路径…">
    </div>
    <div class="counts" id="counts"></div>
    <div id="list"><div class="hint">扫描中…</div></div>
    <div class="side-foot">
      <details class="envbox">
        <summary id="envsummary">环境</summary>
        <div id="env" class="env"></div>
      </details>
      <button id="quit" class="link-btn" title="停止本地服务">退出</button>
    </div>
  </aside>
  <main id="detail">
    <div class="hero">
      <div class="hero-mark">⇄</div>
      <h1>从哪个会话开始？</h1>
      <p>在左侧选择一个会话，预览正文，再把它导出到另一个 harness。</p>
    </div>
  </main>
</div>
<div id="toast"></div>
<dialog id="exportDlg">
  <h3>导出会话</h3>
  <p class="route" id="exportSrc"></p>
  <div id="exportSelect">
    <div class="export-fields">
      <label>目标 harness<select id="dst"></select></label>
      <label>目标 cwd<input id="targetCwd" placeholder="留空 = 保持原 cwd" spellcheck="false"></label>
      <label>冲突策略<select id="conflict">
        <option value="skip">跳过（默认）</option>
        <option value="new-id">生成新 ID</option>
        <option value="overwrite">覆盖</option>
      </select></label>
    </div>
    <div class="hint">先做 dry-run 预览，确认后才会写入目标 harness 的本地数据目录。</div>
  </div>
  <div id="exportPlan"></div>
  <div id="exportResult"></div>
  <div id="exportErr"></div>
  <div class="dlg-actions">
    <button id="exportCancel" class="ghost">取消</button>
    <button id="exportGo" class="primary">导出</button>
  </div>
</dialog>
<script>
const BOOT = __BOOT__;
const $ = (id) => document.getElementById(id);
const state = {
  sessions: [],
  counts: {},
  harnesses: [],
  filterHarness: BOOT.harness || null,
  selected: null,
  detail: null,
  plan: null,
  planBody: null,
  scanCwd: BOOT.cwd || "",
  recents: [],
};

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}
function fmtTime(iso) {
  if (!iso) return "?";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString();
}
function displayName(name) {
  const h = state.harnesses.find((x) => x.name === name);
  return h ? h.display_name : name;
}
function toast(msg, kind) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "show " + (kind || "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.className = ""; }, 4200);
}
async function api(path, opts) {
  const options = Object.assign({ headers: {} }, opts || {});
  if (options.body) options.headers["Content-Type"] = "application/json";
  const res = await fetch(path, options);
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-json error body */ }
  if (!res.ok) throw new Error((data && data.error) || ("HTTP " + res.status));
  return data;
}

async function loadDoctor() {
  try {
    const data = await api("/api/doctor");
    state.harnesses = data.adapters || [];
    renderEnv();
    renderCounts();
    renderList();
    if (state.detail) renderDetail(state.detail);
  } catch (e) {
    toast(e.message, "err");
  }
}
function renderEnv() {
  const box = $("env");
  box.innerHTML = "";
  const avail = state.harnesses.filter((h) => h.available).length;
  $("envsummary").textContent = "环境 " + avail + "/" + state.harnesses.length;
  state.harnesses.forEach((h) => {
    const row = document.createElement("div");
    row.className = "etag" + (h.available ? "" : " off");
    row.innerHTML = '<span class="name">' + (h.available ? "● " : "○ ") + esc(h.display_name) + "</span>"
      + '<span class="root" title="' + esc(h.root || "未安装") + '">' + esc(h.root || "未安装") + "</span>";
    box.appendChild(row);
  });
  if (!state.harnesses.some((h) => h.name === "dsh" && h.available)) {
    const note = document.createElement("div");
    note.className = "hint";
    note.textContent = "dsh 写入默认关闭，需要 --allow-dsh-write 启动。";
    box.appendChild(note);
  }
}

async function loadSessions() {
  const cwd = $("cwd").value.trim();
  const qs = new URLSearchParams();
  if (!$("allProjects").checked && cwd) qs.set("cwd", cwd);
  $("list").innerHTML = '<div class="hint">扫描中…</div>';
  try {
    const data = await api("/api/sessions?" + qs.toString());
    state.sessions = data.sessions || [];
    state.counts = data.counts || {};
    state.scanCwd = data.cwd || "";
    renderCounts();
    renderList();
    Object.entries(data.errors || {}).forEach(([name, msg]) => toast(name + " 扫描失败: " + msg, "err"));
    if (state.selected) {
      const still = state.sessions.find((s) => s.id === state.selected.id && s.harness === state.selected.harness);
      if (!still) {
        state.selected = null;
        state.plan = null;
        state.planBody = null;
        $("detail").innerHTML = '<div class="hero"><div class="hero-mark">⇄</div><h1>从哪个会话开始？</h1><p>在左侧选择一个会话，预览正文，再把它导出到另一个 harness。</p></div>';
      }
    }
    return true;
  } catch (e) {
    $("list").innerHTML = "";
    toast(e.message, "err");
    return false;
  }
}

function renderCounts() {
  const box = $("counts");
  box.innerHTML = "";
  const names = state.harnesses.filter((h) => h.available).map((h) => h.name);
  if (state.filterHarness && !names.includes(state.filterHarness)) state.filterHarness = null;
  const total = names.reduce((n, name) => n + (state.counts[name] || 0), 0);
  const all = document.createElement("button");
  all.className = "chip" + (state.filterHarness ? "" : " on");
  all.textContent = total ? "全部 " + total : "全部";
  all.title = "显示所有 harness";
  all.onclick = () => { state.filterHarness = null; renderCounts(); loadSessions(); };
  box.appendChild(all);
  names.forEach((name) => {
    const count = state.counts[name];
    if (!count && state.filterHarness !== name) return;
    const chip = document.createElement("button");
    chip.className = "chip" + (state.filterHarness === name ? " on" : "");
    chip.textContent = displayName(name) + " " + (count || 0);
    chip.title = state.filterHarness === name ? "点击切回全部" : "只显示该 harness";
    chip.onclick = () => {
      state.filterHarness = state.filterHarness === name ? null : name;
      renderCounts();
      loadSessions();
    };
    box.appendChild(chip);
  });
}

const LEGACY_RECENT_KEY = "session-sync.recent-projects.v1";

async function loadRecents() {
  try {
    state.recents = (await api("/api/recent")).projects || [];
  } catch (e) {
    state.recents = [];
  }
  await migrateLegacyRecents();
  renderRecents();
}
async function migrateLegacyRecents() {
  let legacy = null;
  try { legacy = JSON.parse(localStorage.getItem(LEGACY_RECENT_KEY) || "null"); } catch (e) { legacy = null; }
  if (!Array.isArray(legacy)) return;
  for (const item of legacy) {
    if (!item || typeof item.path !== "string") continue;
    try {
      const data = await api("/api/recent", { method: "POST", body: JSON.stringify({
        path: item.path,
        uses: item.uses || 1,
        lastUsed: item.lastUsed || Date.now(),
      }) });
      state.recents = data.projects || state.recents;
    } catch (e) {
      return; // 迁移失败保留旧数据，下次启动重试
    }
  }
  try { localStorage.removeItem(LEGACY_RECENT_KEY); } catch (e) { /* ignore */ }
}
function topRecents() {
  return (state.recents || []).slice(0, 5);
}
function projectName(path) {
  const clean = String(path || "").replace(/[/]+$/, "");
  return clean.split("/").pop() || clean || String(path || "");
}
async function recordProject(path) {
  if (!path) return;
  try {
    const data = await api("/api/recent", { method: "POST", body: JSON.stringify({ path: path }) });
    state.recents = data.projects || state.recents;
    renderRecents();
  } catch (e) { /* 记录失败不影响扫描 */ }
}
function renderRecents() {
  const box = $("recent");
  const top = topRecents();
  box.innerHTML = "";
  if (!top.length) return;
  const label = document.createElement("span");
  label.className = "rlabel";
  label.textContent = "最近";
  box.appendChild(label);
  const current = $("cwd").value.trim().replace(/[/]+$/, "");
  top.forEach((item) => {
    const chip = document.createElement("button");
    chip.className = "rchip" + (current === item.path ? " on" : "");
    chip.textContent = projectName(item.path);
    chip.title = item.path;
    chip.onclick = () => useRecentProject(item.path);
    box.appendChild(chip);
  });
}
async function useRecentProject(path) {
  $("cwd").value = path;
  $("allProjects").checked = false;
  if (await loadSessions()) recordProject(state.scanCwd);
}
async function scanAndRecord() {
  if (await loadSessions()) recordProject(state.scanCwd);
}

function renderList() {
  const q = $("q").value.trim().toLowerCase();
  const list = $("list");
  const order = state.harnesses.map((h) => h.name);
  const groups = new Map();
  state.sessions.forEach((s) => {
    if (state.filterHarness && s.harness !== state.filterHarness) return;
    const hay = [s.title, s.preview, s.id, s.cwd, s.harness].join(" ").toLowerCase();
    if (q && !hay.includes(q)) return;
    if (!groups.has(s.harness)) groups.set(s.harness, []);
    groups.get(s.harness).push(s);
  });
  list.innerHTML = "";
  let shown = 0;
  order.concat([...groups.keys()].filter((n) => !order.includes(n))).forEach((name) => {
    const rows = groups.get(name);
    if (!rows || !rows.length) return;
    const head = document.createElement("div");
    head.className = "hgroup";
    head.textContent = displayName(name);
    list.appendChild(head);
    rows.forEach((s) => {
      const d = document.createElement("div");
      const active = state.selected && state.selected.id === s.id && state.selected.harness === s.harness;
      d.className = "sess" + (active ? " active" : "");
      d.innerHTML = '<div class="t">' + esc(s.title || "(无标题)") + "</div>"
        + '<div class="m">' + esc(s.cwd || "(无 cwd)") + " · " + (s.message_count == null ? "?" : s.message_count)
        + " 条 · " + esc(fmtTime(s.updated)) + "</div>";
      d.onclick = () => selectSession(s);
      list.appendChild(d);
      shown += 1;
    });
  });
  if (!shown) {
    list.innerHTML = '<div class="hint">' + (q ? "没有匹配的会话" : "当前过滤下没有会话") + "</div>";
  }
}

async function selectSession(s) {
  state.selected = s;
  state.plan = null;
  state.planBody = null;
  renderList();
  $("detail").innerHTML = '<div class="empty">加载会话…</div>';
  try {
    const d = await api("/api/session?harness=" + encodeURIComponent(s.harness) + "&id=" + encodeURIComponent(s.id));
    state.detail = d;
    renderDetail(d);
  } catch (e) {
    $("detail").innerHTML = '<div class="empty">' + esc(e.message) + "</div>";
  }
}

function renderDetail(d) {
  const stats = d.stats || {};
  const roleText = Object.entries(stats.roles || {}).map(([k, v]) => k + " " + v).join(" · ");
  const warnings = (d.warnings || []).filter(Boolean);
  const targets = state.harnesses.filter((h) => h.available && h.name !== d.harness);
  const msgs = (d.messages || []).map((m) =>
    '<div class="msg ' + esc(m.role) + '"><div class="who">' + esc(m.role) + " · " + esc(fmtTime(m.time)) + "</div>" + m.html + "</div>"
  ).join("");
  const warnHtml = warnings.length
    ? '<details class="warnbox"><summary>' + warnings.length + ' 条结构警告</summary><ul>'
      + warnings.map((w) => "<li>" + esc(w) + "</li>").join("") + "</ul></details>"
    : "";
  const canExport = targets.length > 0;
  $("detail").innerHTML = `
    <div class="dhead">
      <div class="dtitle">
        <div class="badge">${esc(displayName(d.harness))}</div>
        <h2>${esc(d.title || "(无标题)")}</h2>
        <div class="sub">${esc(d.id)} · ${esc(d.cwd || "(无 cwd)")}${d.model ? " · " + esc(d.model) : ""}</div>
      </div>
      <div class="headbtns">
        <button id="copyId" class="ghost">复制 ID</button>
        <button id="exportBtn" class="primary" ${canExport ? "" : "disabled"} title="导出到其他 harness">导出…</button>
      </div>
    </div>
    ${warnHtml}
    <div class="panel">
      <div class="panel-title">会话正文 <span class="dim">${stats.messages == null ? (d.messages || []).length : stats.messages} 条消息${roleText ? " · " + esc(roleText) : ""}</span></div>
      <div class="transcript">${msgs || '<div class="hint">（无消息）</div>'}</div>
    </div>`;
  $("copyId").onclick = async () => {
    try {
      await navigator.clipboard.writeText(d.id);
      toast("已复制会话 ID", "ok");
    } catch (e) {
      toast("复制失败，请手动选择 ID", "err");
    }
  };
  if (canExport) $("exportBtn").onclick = openExport;
}

function openExport() {
  const s = state.selected;
  if (!s) return;
  const targets = state.harnesses.filter((h) => h.available && h.name !== s.harness);
  if (!targets.length) {
    toast("没有可用的目标 harness", "err");
    return;
  }
  $("dst").innerHTML = targets.map((h) =>
    '<option value="' + esc(h.name) + '">' + esc(h.display_name) + "</option>").join("");
  $("exportSrc").textContent = s.harness + ":" + s.id + " · " + (s.title || "(无标题)");
  $("targetCwd").value = "";
  $("conflict").value = "skip";
  $("exportSelect").style.display = "";
  $("exportPlan").innerHTML = "";
  $("exportResult").innerHTML = "";
  $("exportErr").innerHTML = "";
  $("exportCancel").textContent = "取消";
  $("exportCancel").onclick = () => $("exportDlg").close();
  const go = $("exportGo");
  go.style.display = "";
  go.className = "primary";
  go.textContent = "导出";
  go.disabled = false;
  go.onclick = exportPreview;
  state.plan = null;
  state.planBody = null;
  $("exportDlg").showModal();
}

function exportBack() {
  state.plan = null;
  state.planBody = null;
  $("exportSelect").style.display = "";
  $("exportPlan").innerHTML = "";
  $("exportErr").innerHTML = "";
  $("exportCancel").textContent = "取消";
  $("exportCancel").onclick = () => $("exportDlg").close();
  const go = $("exportGo");
  go.className = "primary";
  go.textContent = "导出";
  go.disabled = false;
  go.onclick = exportPreview;
}

async function exportPreview() {
  if (!state.selected) return;
  const go = $("exportGo");
  go.disabled = true;
  go.textContent = "生成计划…";
  $("exportErr").innerHTML = "";
  $("exportPlan").innerHTML = "";
  const body = {
    src: state.selected.harness,
    id: state.selected.id,
    dst: $("dst").value,
    cwd: $("targetCwd").value.trim() || null,
    on_conflict: $("conflict").value,
  };
  try {
    const plan = await api("/api/plan", { method: "POST", body: JSON.stringify(body) });
    state.plan = plan;
    state.planBody = body;
    $("exportSelect").style.display = "none";
    $("exportPlan").innerHTML = planHtml(plan);
    $("exportCancel").textContent = "返回";
    $("exportCancel").onclick = exportBack;
    go.className = "danger";
    go.textContent = "确认写入";
    go.onclick = exportExecute;
  } catch (e) {
    $("exportErr").innerHTML = '<div class="errbox">' + esc(e.message) + "</div>";
    toast(e.message, "err");
  } finally {
    go.disabled = false;
  }
}

async function exportExecute() {
  if (!state.planBody) return;
  const go = $("exportGo");
  go.disabled = true;
  go.textContent = "写入中…";
  $("exportErr").innerHTML = "";
  try {
    const m = await api("/api/copy", {
      method: "POST",
      body: JSON.stringify(Object.assign({}, state.planBody, { confirm: true })),
    });
    const t = m.target || {};
    $("exportPlan").innerHTML = "";
    $("exportResult").innerHTML = resultHtml(m);
    $("exportCancel").textContent = "关闭";
    $("exportCancel").onclick = () => $("exportDlg").close();
    go.style.display = "none";
    state.plan = null;
    state.planBody = null;
    toast(t.skipped ? "目标已存在，已跳过" : "导出完成", t.skipped ? "" : "ok");
    await loadSessions();
  } catch (e) {
    $("exportErr").innerHTML = '<div class="errbox">' + esc(e.message) + "</div>";
    toast(e.message, "err");
    go.disabled = false;
    go.textContent = "确认写入";
  }
}

function planHtml(m) {
  const t = m.target || {};
  const s = m.source || {};
  const stats = m.stats || {};
  const warn = (m.warnings || []).filter(Boolean);
  const status = t.skipped ? "目标已存在同 ID 会话，执行时将跳过"
    : "dry-run 通过，确认后按冲突策略写入";
  return `
    <div class="plancard">
      <div class="planhead"><span class="tag">dry-run</span><b>导出计划</b></div>
      <div class="route">${esc(s.harness + ":" + s.id)} → ${esc(t.harness + ":" + (t.id || "(新 ID)"))}</div>
      <dl class="kv">
        <dt>状态</dt><dd>${esc(status)}</dd>
        <dt>目标路径</dt><dd>${esc(t.path || "—")}</dd>
        <dt>目标 cwd</dt><dd>${esc(t.cwd || "—")}</dd>
        <dt>备份</dt><dd>${esc(t.backup || "—")}</dd>
        <dt>消息</dt><dd>${esc(String(stats.messages == null ? "?" : stats.messages))} 条</dd>
      </dl>
      ${warn.length ? '<details class="warnbox" open><summary>' + warn.length + " 条警告</summary><ul>"
        + warn.map((w) => "<li>" + esc(w) + "</li>").join("") + "</ul></details>" : ""}
    </div>`;
}

function resultHtml(m) {
  const t = m.target || {};
  const warn = (m.warnings || []).filter(Boolean);
  const title = t.skipped ? "已跳过（目标存在同 ID 会话）" : "✅ 已写入目标 harness";
  const rows = [
    ["目标会话", t.harness + ":" + (t.id || "—")],
    ["目标路径", t.path || "—"],
    ["备份", t.backup || "—"],
    ["目标 cwd", t.cwd || "—"],
  ];
  return `
    <div class="okbox">
      <div class="oktitle">${esc(title)}</div>
      <dl class="kv">${rows.map(([k, v]) => "<dt>" + esc(k) + "</dt><dd>" + esc(v) + "</dd>").join("")}</dl>
      ${warn.length ? '<details class="warnbox" open><summary>' + warn.length + " 条警告</summary><ul>"
        + warn.map((w) => "<li>" + esc(w) + "</li>").join("") + "</ul></details>" : ""}
    </div>`;
}

$("scan").onclick = scanAndRecord;
$("pick").onclick = async () => {
  const btn = $("pick");
  btn.disabled = true;
  btn.textContent = "选择中…";
  try {
    const data = await api("/api/pick-dir", { method: "POST", body: "{}" });
    if (data.cancelled) return;
    $("cwd").value = data.path;
    $("allProjects").checked = false;
    if (await loadSessions()) recordProject(state.scanCwd);
  } catch (e) {
    toast(e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "浏览";
  }
};
$("cwd").addEventListener("keydown", (e) => { if (e.key === "Enter") scanAndRecord(); });
$("allProjects").addEventListener("change", loadSessions);
$("q").addEventListener("input", renderList);
$("quit").onclick = async () => {
  if (!window.confirm("停止 session-sync 本地服务？当前页面将失效。")) return;
  try { await api("/api/quit", { method: "POST", body: "{}" }); } catch (e) { /* server may close first */ }
  document.body.innerHTML = '<div class="empty">服务已停止，可关闭此窗口。</div>';
};

$("ver").textContent = "v" + BOOT.version;
$("cwd").value = BOOT.cwd || "";
loadRecents();
loadDoctor();
loadSessions();
</script>
</body>
</html>
"""

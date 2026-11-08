"""Self-contained HTML dashboard: cross-harness session list + chat transcripts."""

from __future__ import annotations

import json
from pathlib import Path

from . import ir
from .adapters.base import SessionSummary


def _esc(s) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _json_embed(obj) -> str:
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def _block_html(b) -> str:
    bt = b.get("type")
    if bt == "text":
        return f'<div class="blk text">{_esc(b.get("text", ""))}</div>'
    if bt == "thinking":
        return (
            '<details class="blk thinking"><summary>thinking</summary>'
            f'<div>{_esc(b.get("text", ""))}</div></details>'
        )
    if bt == "tool_call":
        import json as _json

        args = b.get("arguments_raw") or _json.dumps(b.get("arguments") or {}, ensure_ascii=False)
        return (
            f'<div class="blk tool"><span class="toolname">🔧 {_esc(b.get("name", "?"))}</span>'
            f"<pre>{_esc(args[:3000])}</pre></div>"
        )
    if bt == "tool_result":
        out = str(b.get("output", ""))
        more = f" <span class='dim'>(共 {len(out)} 字符)</span>" if len(out) > 3000 else ""
        err = " err" if b.get("is_error") else ""
        return (
            f'<div class="blk toolresult{err}"><span class="toolname">📄 {_esc(b.get("call_id", ""))}</span>'
            f"<pre>{_esc(out[:3000])}</pre>{more}</div>"
        )
    if bt == "image":
        return '<div class="blk dim">🖼 image</div>'
    return f'<div class="blk dim">[{_esc(bt)}]</div>'


def _transcript(events) -> list[dict]:
    from .utils import ms_to_iso

    msgs = []
    for e in events:
        if e.get("type") == "message":
            msgs.append(
                {
                    "role": e.get("role"),
                    "time": ms_to_iso(e.get("timestamp")),
                    "html": "".join(_block_html(b) for b in e.get("blocks") or []),
                }
            )
        elif e.get("type") == "event" and not (e.get("kind") or "").startswith("native:"):
            data = e.get("data") or {}
            msgs.append(
                {
                    "role": "event",
                    "time": ms_to_iso(e.get("timestamp")),
                    "html": f'<div class="blk event">{_esc(e.get("kind"))}: {_esc(str(data.get("summary") or data.get("text") or "")[:200])}</div>',
                }
            )
    return msgs


_PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>session-sync dashboard</title>
<style>
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.5 -apple-system, "PingFang SC", "Segoe UI", sans-serif; display: flex; height: 100vh; }
#side { width: 340px; border-right: 1px solid #8884; overflow-y: auto; padding: 12px; flex-shrink: 0; }
#main { flex: 1; overflow-y: auto; padding: 20px 28px; }
h1 { font-size: 16px; margin: 4px 0 8px; }
.search { width: 100%; padding: 6px 10px; margin-bottom: 10px; border: 1px solid #8886; border-radius: 8px; background: transparent; color: inherit; }
.hgroup { margin: 10px 0 2px; font-weight: 600; color: #7aa; }
.sess { padding: 7px 9px; border-radius: 8px; cursor: pointer; }
.sess:hover { background: #8882; }
.sess.active { background: #36c3; }
.sess .t { font-weight: 500; }
.sess .m { font-size: 11px; color: #888; }
.meta { color: #888; font-size: 12px; margin-bottom: 14px; }
.msg { margin: 14px 0; padding: 10px 14px; border-radius: 10px; }
.msg.user { background: #2563eb22; border-left: 3px solid #3b82f6; }
.msg.assistant { background: #10b98122; border-left: 3px solid #10b981; }
.msg.system, .msg.tool, .msg.event { background: #88888818; border-left: 3px solid #888; }
.msg .who { font-size: 11px; color: #888; margin-bottom: 4px; }
.blk { white-space: pre-wrap; word-break: break-word; }
.blk + .blk { margin-top: 8px; }
.blk.tool, .blk.toolresult { background: #88888814; border-radius: 6px; padding: 6px 9px; }
.blk.toolresult.err { border-left: 3px solid #ef4444; }
.toolname { font-weight: 600; font-size: 12px; display: block; margin-bottom: 3px; }
pre { white-space: pre-wrap; word-break: break-word; margin: 0; font: 11px/1.45 ui-monospace, Menlo, monospace; }
details.thinking summary { cursor: pointer; color: #888; font-size: 12px; }
details.thinking { color: #aaa; border-left: 2px solid #8884; padding-left: 8px; }
.dim { color: #888; }
.empty { color: #888; margin-top: 40px; text-align: center; }
</style>
</head>
<body>
<div id="side">
  <h1>🧩 session-sync</h1>
  <input class="search" id="q" placeholder="搜索会话…">
  <div id="list"></div>
</div>
<div id="main"><div class="empty">← 从左侧选择一个会话</div></div>
<script>
const DATA = __DATA__;
const list = document.getElementById("list");
const q = document.getElementById("q");
const main = document.getElementById("main");

function render(filter) {
  list.innerHTML = "";
  let last = null;
  DATA.sessions.forEach((s, i) => {
    const hay = ((s.title||"") + " " + (s.preview||"") + " " + (s.harness||"")).toLowerCase();
    if (filter && !hay.includes(filter.toLowerCase())) return;
    if (s.harness !== last) {
      const g = document.createElement("div");
      g.className = "hgroup";
      g.textContent = s.harness;
      list.appendChild(g);
      last = s.harness;
    }
    const d = document.createElement("div");
    d.className = "sess";
    d.innerHTML = '<div class="t">' + esc(s.title || "(无标题)") + '</div>'
      + '<div class="m">' + esc(s.cwd || "") + ' · ' + (s.message_count ?? "?") + ' 条消息'
      + (s.updated ? " · " + new Date(s.updated).toLocaleString() : "") + '</div>';
    d.onclick = () => show(i, d);
    list.appendChild(d);
  });
}
function esc(s) { const d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }
function show(i, el) {
  document.querySelectorAll(".sess.active").forEach(e => e.classList.remove("active"));
  if (el) el.classList.add("active");
  const s = DATA.sessions[i];
  const msgs = (DATA.transcripts[s.key] || []);
  let html = '<div class="meta">' + esc(s.harness) + ' · ' + esc(s.id) + ' · ' + esc(s.cwd || "")
    + (s.model ? " · " + esc(s.model) : "") + '</div>';
  if (!msgs.length) html += '<div class="empty">（未加载该会话的正文；命令行执行 session-sync show 可查看）</div>';
  msgs.forEach(m => {
    html += '<div class="msg ' + esc(m.role) + '"><div class="who">' + esc(m.role) + ' · ' + esc(m.time || "") + '</div>' + m.html + '</div>';
  });
  main.innerHTML = html;
  main.scrollTop = 0;
}
q.oninput = () => render(q.value.trim());
render("");
</script>
</body>
</html>
"""


def render_dashboard(sessions: list[SessionSummary], transcripts: dict[str, list[dict]], out_path: str | Path) -> str:
    payload = {
        "sessions": [
            {
                "key": f"{s.harness}:{s.id}",
                "harness": s.harness,
                "id": s.id,
                "title": s.title,
                "cwd": s.cwd,
                "updated": s.updated_at,
                "message_count": s.message_count,
                "model": None,
            }
            for s in sessions
        ],
        "transcripts": transcripts,
    }
    html = _PAGE.replace("__DATA__", _json_embed(payload))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return str(out_path)


def build_transcripts(events_list) -> list[dict]:
    return [_transcript(events) for events in events_list]

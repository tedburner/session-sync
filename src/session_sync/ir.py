"""Unified Conversation Format (UCF / IR).

A session is an ordered list of JSON-serializable events:

  {"type": "session_meta", "id", "harness", "cwd", "title", "model", "created_at"(ms), "ext": {}}
  {"type": "message", "id", "parent_id", "timestamp"(ms), "role": user|assistant|system|tool,
       "blocks": [...], "ext": {}}
  {"type": "event", "kind", "timestamp"(ms), "data": {}, "ext": {}}

Blocks:
  {"type": "text", "text"}
  {"type": "thinking", "text"}
  {"type": "tool_call", "id", "name", "arguments": dict|None, "arguments_raw": str|None}
  {"type": "tool_result", "call_id", "output", "is_error", "name"}
  {"type": "image", "media_type", "data"}

Writers may re-emit native payload preserved by readers via ext.raw_line.
"""

from __future__ import annotations

import json

ROLES = ("user", "assistant", "system", "tool")
BLOCK_TYPES = ("text", "thinking", "tool_call", "tool_result", "image")


def meta(events) -> dict:
    for e in events:
        if e.get("type") == "session_meta":
            return e
    return {}


def messages(events) -> list[dict]:
    return [e for e in events if e.get("type") == "message"]


def validate(events) -> list[str]:
    """Return a list of structural warnings (does not raise)."""
    warns = []
    if not events or events[0].get("type") != "session_meta":
        warns.append("first event is not session_meta")
    for i, e in enumerate(events):
        t = e.get("type")
        if t == "session_meta":
            for key in ("id", "cwd"):
                if not e.get(key):
                    warns.append(f"event {i}: session_meta missing {key}")
        elif t == "message":
            if e.get("role") not in ROLES:
                warns.append(f"event {i}: unknown role {e.get('role')!r}")
            for b in e.get("blocks") or []:
                if b.get("type") not in BLOCK_TYPES:
                    warns.append(f"event {i}: unknown block type {b.get('type')!r}")
        elif t == "event":
            if not e.get("kind"):
                warns.append(f"event {i}: event missing kind")
        else:
            warns.append(f"event {i}: unknown event type {t!r}")
    return warns


def stats(events) -> dict:
    out = {"messages": 0, "events": 0, "blocks": {}, "roles": {}}
    for e in events:
        t = e.get("type")
        if t == "message":
            out["messages"] += 1
            out["roles"][e.get("role", "?")] = out["roles"].get(e.get("role", "?"), 0) + 1
            for b in e.get("blocks") or []:
                bt = b.get("type", "?")
                out["blocks"][bt] = out["blocks"].get(bt, 0) + 1
        elif t == "event":
            out["events"] += 1
    return out


def first_user_text(events, limit: int = 200) -> str:
    for e in events:
        if e.get("type") == "message" and e.get("role") == "user":
            for b in e.get("blocks") or []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    text = b["text"].strip()
                    return text if len(text) <= limit else text[: limit - 1] + "…"
    return ""


def session_title(events) -> str | None:
    m = meta(events)
    if m.get("title"):
        return m["title"]
    text = first_user_text(events, 60)
    return text or None


def save(path, events) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def load(path) -> list[dict]:
    from .utils import iter_jsonl

    return list(iter_jsonl(path))


# ---------------------------------------------------------------- rendering


def _block_md(b, max_tool_output: int = 2000) -> str:
    bt = b.get("type")
    if bt == "text":
        return b.get("text", "")
    if bt == "thinking":
        text = b.get("text", "")
        return f"<details><summary>thinking</summary>\n\n{text}\n\n</details>"
    if bt == "tool_call":
        import json as _json

        args = b.get("arguments_raw") or _json.dumps(b.get("arguments") or {}, ensure_ascii=False)
        return f"**🔧 tool_call `{b.get('name', '?')}`**\n\n```json\n{args}\n```"
    if bt == "tool_result":
        out = str(b.get("output", ""))
        if len(out) > max_tool_output:
            out = out[:max_tool_output] + f"\n… (truncated, {len(out)} chars total)"
        err = " (error)" if b.get("is_error") else ""
        return f"**📄 tool_result{err} `{b.get('call_id', '?')}`**\n\n```\n{out}\n```"
    if bt == "image":
        return "**🖼 image** (media not rendered)"
    return f"**[{bt}]**"


def render_markdown(events, max_tool_output: int = 2000) -> str:
    from .utils import ms_to_iso, truncate

    m = meta(events)
    lines = []
    head = " · ".join(
        x for x in [
            m.get("harness"),
            m.get("title"),
            m.get("model"),
            m.get("cwd"),
            ms_to_iso(m.get("created_at")),
        ] if x
    )
    if head:
        lines.append(f"> {head}\n")
    for e in events:
        if e.get("type") == "message":
            label = {"user": "🧑 user", "assistant": "🤖 assistant", "system": "⚙️ system", "tool": "🔨 tool"}[
                e.get("role", "?")
            ]
            lines.append(f"### {label} · {ms_to_iso(e.get('timestamp'))}")
            for b in e.get("blocks") or []:
                lines.append(_block_md(b, max_tool_output))
            lines.append("")
        elif e.get("type") == "event":
            kind = e.get("kind", "?")
            if kind.startswith("native:"):
                continue
            data = e.get("data") or {}
            summary = data.get("summary") or data.get("text") or ""
            lines.append(f"_— {truncate(kind, 40)}: {truncate(summary, 120)}_\n")
    return "\n".join(lines).strip() + "\n"

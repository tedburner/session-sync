"""doctor: probe installed harnesses and their data roots."""

from __future__ import annotations

from .adapters import ADAPTER_ORDER, get_adapter


def doctor(cwd: str | None = None) -> list[dict]:
    rows = []
    for name in ADAPTER_ORDER:
        adapter = get_adapter(name)
        desc = adapter.describe()
        all_sessions = []
        if desc["available"]:
            try:
                all_sessions = adapter.list_sessions()
            except Exception as exc:  # noqa: BLE001 - doctor must not crash
                desc["error"] = str(exc)
        desc["sessions_total"] = len(all_sessions) if desc["available"] else None
        if desc["available"] and cwd:
            desc["sessions_in_cwd"] = sum(1 for s in all_sessions if s.cwd == cwd)
        else:
            desc["sessions_in_cwd"] = None
        rows.append(desc)
    return rows


def render_doctor(rows: list[dict], cwd: str | None = None) -> str:
    import platform

    lines = [f"session-sync doctor · python {platform.python_version()}", ""]
    for r in rows:
        mark = "✅" if r["available"] else "—"
        line = f"{mark} {r['display_name']:<14} {r['root'] or ''}"
        if r["available"]:
            if cwd is not None:
                line += f"   (本项目 {r['sessions_in_cwd']} / 总 {r['sessions_total']})"
            else:
                line += f"   (总会话 {r['sessions_total']})"
        if r.get("error"):
            line += f"   ⚠ {r['error']}"
        lines.append(line)
    return "\n".join(lines)

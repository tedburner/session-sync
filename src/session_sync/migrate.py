"""Session migration: reader -> IR -> writer, with manifest and backups."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from . import __version__, ir
from .adapters import get_adapter
from .adapters.base import AdapterError


def resolve_target_cwd(src_meta: dict, cwd: str | None, cwd_map: dict[str, str] | None) -> str | None:
    """Explicit cwd wins; else apply OLD=NEW map; else keep source cwd."""
    if cwd:
        return cwd
    src_cwd = src_meta.get("cwd")
    if cwd_map and src_cwd:
        for old, new in cwd_map.items():
            if src_cwd == old or src_cwd.startswith(old.rstrip("/") + "/"):
                remainder = src_cwd[len(old):] if src_cwd.startswith(old) else ""
                return new.rstrip("/") + remainder
    return src_cwd


def copy_session(
    src: str,
    sid: str,
    dst: str,
    *,
    src_root: str | None = None,
    dst_root: str | None = None,
    cwd: str | None = None,
    cwd_map: dict[str, str] | None = None,
    on_conflict: str = "skip",
    dry_run: bool = True,
) -> dict:
    src_adapter = get_adapter(src, src_root)
    dst_adapter = get_adapter(dst, dst_root)
    if not src_adapter.is_available():
        raise AdapterError(f"{src_adapter.display_name} data not found at {src_adapter.root}")
    if not dst_adapter.is_available():
        raise AdapterError(f"{dst_adapter.display_name} data not found at {dst_adapter.root}")

    events = src_adapter.read_session(sid)
    ir_warnings = ir.validate(events)
    meta = ir.meta(events)
    target_cwd = resolve_target_cwd(meta, cwd, cwd_map)
    result = dst_adapter.write_session(events, cwd=target_cwd, on_conflict=on_conflict, dry_run=dry_run)

    stats = ir.stats(events)
    manifest = {
        "tool": "session-sync",
        "version": __version__,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "source": {
            "harness": src,
            "id": sid,
            "title": meta.get("title"),
            "cwd": meta.get("cwd"),
            "model": meta.get("model"),
        },
        "target": {
            "harness": dst,
            "id": result.get("target_id"),
            "cwd": target_cwd,
            "path": result.get("path"),
            "backup": result.get("backup"),
            "created": result.get("created"),
            "skipped": result.get("skipped"),
        },
        "stats": stats,
        "warnings": ir_warnings + result.get("warnings", []),
    }
    if meta.get("ext", {}).get("skipped_event_msg"):
        manifest["warnings"].append(
            f"source: {meta['ext']['skipped_event_msg']} codex event_msg lines skipped (UI telemetry)"
        )
    return manifest


def print_manifest(manifest: dict) -> str:
    src = manifest["source"]
    dst = manifest["target"]
    stats = manifest["stats"]
    lines = []
    tag = "[dry-run] " if manifest.get("dry_run") else ""
    lines.append(f"{tag}{src['harness']}:{src['id']}  ->  {dst['harness']}:{dst['id']}")
    if src.get("title"):
        lines.append(f"  会话: {src['title']}")
    if dst.get("created"):
        lines.append(f"  已写入: {dst['path']}")
    elif dst.get("skipped"):
        lines.append("  已跳过（目标已存在同 id 会话）")
    else:
        lines.append(f"  目标路径: {dst.get('path')}")
    if dst.get("backup"):
        lines.append(f"  备份: {dst['backup']}")
    lines.append(
        f"  消息 {stats['messages']} 条（{' '.join(f'{k}:{v}' for k, v in stats['roles'].items())}），"
        f"blocks {' '.join(f'{k}:{v}' for k, v in stats['blocks'].items()) or '0'}"
    )
    if manifest["warnings"]:
        lines.append("  ⚠ 警告:")
        for w in manifest["warnings"][:12]:
            lines.append(f"    - {w}")
        if len(manifest["warnings"]) > 12:
            lines.append(f"    … 共 {len(manifest['warnings'])} 条")
    return "\n".join(lines)


def save_manifest(manifest: dict, out_dir: str | None = None) -> str | None:
    """Persist a manifest next to nothing by default; used by wizard/report runs."""
    import pathlib

    if not out_dir:
        return None
    p = pathlib.Path(out_dir).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = p / f"manifest-{ts}-{manifest['target']['harness']}.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)

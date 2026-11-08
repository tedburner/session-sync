"""Shared helpers: JSONL I/O, zstd, time conversion, cwd encodings, ids."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_to_ms(value):
    """ISO-8601 string -> epoch ms; returns None on failure."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError, AttributeError):
        return None


def ms_to_iso(ms) -> str | None:
    if ms is None:
        return None
    dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(ms) % 1000:03d}Z"


def ms_to_compact(ms) -> str:
    """Codex rollout filename style: 2026-10-30T09-53-29."""
    dt = datetime.fromtimestamp(int(ms or now_ms()) / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H-%M-%S")


# ---------------------------------------------------------------- JSONL


def iter_jsonl(path: Path | str):
    """Yield parsed JSON objects; malformed lines are skipped (counted via .bad_lines attr on file object)."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def read_jsonl(path: Path | str) -> list[dict]:
    return list(iter_jsonl(path))


def write_jsonl(path: Path | str, rows) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- zstd


def _zstd_stdlib():
    try:
        import compression.zstd  # Python >= 3.14

        return compression.zstd
    except ImportError:
        return None


def _zstd_cli(args, data: bytes | None = None) -> bytes:
    exe = shutil.which("zstd")
    if not exe:
        raise RuntimeError("zstd CLI not found")
    proc = subprocess.run([exe, *args], input=data, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"zstd CLI failed: {proc.stderr.decode(errors='replace')[:200]}")
    return proc.stdout


def zstd_decompress_file(path: Path | str) -> bytes:
    """Decompress a (possibly multi-frame) zstd file: stdlib first, zstd CLI fallback."""
    path = Path(path)
    mod = _zstd_stdlib()
    if mod is not None:
        try:
            with open(path, "rb") as raw:
                with mod.ZstdFile(raw) as zf:
                    return zf.read()
        except Exception:
            pass  # fall through to CLI
    data = path.read_bytes()
    return _zstd_cli(["-dcq"], data=data)


def zstd_compress_bytes(data: bytes) -> bytes:
    mod = _zstd_stdlib()
    if mod is not None:
        try:
            return mod.compress(data)
        except Exception:
            pass
    return _zstd_cli(["-q", "-"], data=data)


# ---------------------------------------------------------------- encodings


def encode_cwd_claude(cwd: str) -> str:
    """Claude Code / Qoder project-dir encoding: every non-alphanumeric becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def encode_cwd_pi(cwd: str) -> str:
    """Pi / dsh encoding: strip leading separator, replace / \\ : with '-' (spaces kept)."""
    return cwd.replace("/", "-").replace("\\", "-").replace(":", "-").lstrip("-")


def pi_session_dir_name(cwd: str) -> str:
    return "--" + encode_cwd_pi(cwd) + "--"


def zcode_project_id(cwd: str) -> str:
    return "proj_" + re.sub(r"[^a-z0-9]+", "-", cwd.lower()).strip("-")


# ---------------------------------------------------------------- ids


def ulid(ts_ms: int | None = None) -> str:
    ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
    rand = os.urandom(10)
    n = int.from_bytes(ts.to_bytes(6, "big") + rand, "big")
    return "".join(_B32[(n >> (155 - 5 * i)) & 31] for i in range(26))


def new_uuid() -> str:
    return str(uuid.uuid4())


def hex_id(n: int = 4) -> str:
    return uuid.uuid4().hex[:n]


def opencode_rand(k: int = 12) -> str:
    import secrets

    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    return "".join(secrets.choice(alphabet) for _ in range(k))


# ---------------------------------------------------------------- misc


def truncate(text, limit: int = 120) -> str:
    if text is None:
        return ""
    text = str(text).replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def find_running_processes(names: list[str]) -> list[str]:
    """Return which of the given exact process names currently have running pids."""
    found = []
    for name in names:
        try:
            proc = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True)
            if proc.returncode == 0 and proc.stdout.strip():
                found.append(name)
        except OSError:
            continue
    return found


def backup_file(path: Path | str, suffix: str | None = None) -> str | None:
    """Copy an existing file next to itself as <name>.bak-<suffix>. Returns backup path or None."""
    path = Path(path)
    if not path.exists():
        return None
    suffix = suffix or str(now_ms())
    target = path.with_name(path.name + f".bak-{suffix}")
    shutil.copy2(path, target)
    return str(target)


def backup_sqlite(db_path: Path | str, suffix: str | None = None) -> str | None:
    """Backup a sqlite db plus its -wal/-shm sidecars into <name>.bak-<suffix>/."""
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    suffix = suffix or str(now_ms())
    bdir = db_path.with_name(db_path.name + f".bak-{suffix}")
    bdir.mkdir(parents=True, exist_ok=True)
    for f in (db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
        if f.exists():
            shutil.copy2(f, bdir / f.name)
    return str(bdir)


def extract_text_content(content) -> str:
    """Best-effort plain text from a str or list-of-blocks content field."""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                if isinstance(block, str):
                    parts.append(block)
                continue
            if block.get("type") in ("text", "input_text", "output_text", "text_delta"):
                parts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_result":
                parts.append(extract_text_content(block.get("content")))
    return "\n".join(p for p in parts if p)

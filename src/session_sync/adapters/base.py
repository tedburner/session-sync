"""Adapter contract shared by all harness backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


class AdapterError(Exception):
    pass


@dataclass
class SessionSummary:
    harness: str
    id: str
    title: str | None = None
    cwd: str | None = None
    created_at: int | None = None
    updated_at: int | None = None
    message_count: int | None = None
    preview: str | None = None
    path: str | None = None

    def as_dict(self) -> dict:
        return {
            "harness": self.harness,
            "id": self.id,
            "title": self.title,
            "cwd": self.cwd,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
            "preview": (self.preview or "")[:200],
            "path": self.path,
        }


class Adapter(ABC):
    """One harness backend. All paths are derived from `root` (explicit or default)."""

    name: str = "?"
    display_name: str = "?"
    # process names used for "is the app running?" warnings on sqlite writers
    running_process_names: list[str] = []

    def __init__(self, root: str | Path | None = None):
        self._explicit_root = Path(root).expanduser() if root else None

    @classmethod
    @abstractmethod
    def default_root(cls) -> Path | None: ...

    @property
    def root(self) -> Path | None:
        return self._explicit_root or self.default_root()

    def is_available(self) -> bool:
        root = self.root
        return bool(root) and Path(root).exists()

    @abstractmethod
    def list_sessions(self, cwd: str | None = None) -> list[SessionSummary]: ...

    @abstractmethod
    def read_session(self, sid: str) -> list[dict]:
        """Parse a native session into IR events. Raises AdapterError when missing."""

    def session_exists(self, sid: str) -> bool:
        return any(s.id == sid for s in self.list_sessions())

    def import_warnings(self, events: list[dict]) -> list[str]:
        """Target-harness necessary-condition checks surfaced at import time."""
        return []

    @abstractmethod
    def write_session(
        self,
        events: list[dict],
        *,
        cwd: str | None = None,
        on_conflict: str = "skip",
        dry_run: bool = False,
    ) -> dict:
        """Write IR events as a native session.

        Returns {harness, target_id, path, backup, created, skipped, dry_run, warnings: [...]}.
        """

    # ------------------------------------------------------------------ helpers

    def _warn_running(self, warnings: list[str]) -> None:
        from ..utils import find_running_processes

        if self.running_process_names:
            running = find_running_processes(self.running_process_names)
            if running:
                warnings.append(
                    f"{self.display_name} 进程正在运行 ({', '.join(running)})；"
                    "建议先退出应用再写入，避免并发写冲突"
                )

    def describe(self) -> dict:
        root = self.root
        return {
            "name": self.name,
            "display_name": self.display_name,
            "root": str(root) if root else None,
            "available": self.is_available(),
        }

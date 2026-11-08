"""Adapter registry."""

from __future__ import annotations

from .base import Adapter, AdapterError
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .dsh import DshAdapter
from .pi import PiAdapter
from .qoder import QoderAdapter, QoderCNAdapter
from .workbuddy import WorkBuddyAdapter
from .zcode import ZCodeAdapter

ADAPTER_ORDER = ["claude-code", "codex", "zcode", "qoder", "qoder-cn", "pi", "dsh", "workbuddy"]

REGISTRY: dict[str, type[Adapter]] = {
    cls.name: cls
    for cls in (
        ClaudeCodeAdapter,
        CodexAdapter,
        ZCodeAdapter,
        QoderAdapter,
        QoderCNAdapter,
        PiAdapter,
        DshAdapter,
        WorkBuddyAdapter,
    )
}

# root override flags: --root name=path
ROOT_OVERRIDES: dict[str, str] = {}


def get_adapter(name: str, root: str | None = None) -> Adapter:
    cls = REGISTRY.get(name)
    if not cls:
        raise AdapterError(
            f"unknown harness {name!r}; available: {', '.join(ADAPTER_ORDER)}"
        )
    if root is None:
        root = ROOT_OVERRIDES.get(name)
    kwargs = {}
    if name == "dsh":
        import os

        kwargs["allow_write"] = os.environ.get("SESSION_SYNC_DSH_WRITE") == "1"
    return cls(root=root, **kwargs)


def available_names() -> list[str]:
    return [n for n in ADAPTER_ORDER if n in REGISTRY]

"""Interactive terminal wizard: pick source harness -> session -> target -> confirm -> migrate."""

from __future__ import annotations

import os
import sys

from .adapters import ADAPTER_ORDER, get_adapter
from .adapters.base import AdapterError
from .migrate import copy_session, print_manifest
from .utils import ms_to_iso, truncate

DISPLAY = {n: get_adapter(n).display_name for n in ADAPTER_ORDER}


def _input(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        sys.exit(130)


def _menu(title: str, options: list[tuple[str, str]], extra: list[tuple[str, str]] | None = None) -> str:
    """Render numbered menu; returns the chosen value ('' for extras keys)."""
    print(f"\n{title}")
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i}) {label}")
    extras = extra or []
    for key, label in extras:
        print(f"  [{key}] {label}")
    while True:
        raw = _input("> ")
        if raw.lower() in {k for k, _ in extras}:
            return ""
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print("无效输入，请输入编号或方括号选项。")


def run_wizard() -> None:
    if not sys.stdin.isatty():
        print("向导模式需要交互终端；请直接使用子命令（session-sync --help）。")
        sys.exit(2)

    print("🧩 session-sync —— 跨 harness 会话迁移")
    cwd = os.getcwd()

    while True:
        # step 0: project cwd
        print(f"\n▸ 当前项目: {cwd}")
        raw = _input("  回车确认，或输入其他项目路径: ").strip()
        if raw:
            cwd = os.path.expanduser(raw)
        if not os.path.isdir(cwd):
            print("目录不存在，重新选择。")
            continue

        # step 1: scan harnesses for this cwd
        counts: dict[str, list] = {}
        for name in ADAPTER_ORDER:
            adapter = get_adapter(name)
            if not adapter.is_available():
                continue
            try:
                counts[name] = adapter.list_sessions(cwd)
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠ {adapter.display_name} 扫描失败: {exc}")
        print("\n▸ 本项目会话分布:")
        for name in ADAPTER_ORDER:
            if name in counts:
                print(f"    {DISPLAY[name]:<14} {len(counts[name])} 个会话")
        if not any(counts.values()):
            print("    （该项目下没有发现任何 harness 的会话）")
            raw = _input("\n[r] 换个项目  [q] 退出 > ").lower()
            if raw == "q":
                return
            if raw == "r":
                cwd = _input("项目路径: ")
                continue
            cwd = _input("项目路径: ")
            continue

        # step 2: pick source harness
        with_sessions = [n for n in ADAPTER_ORDER if counts.get(n)]
        src = _menu(
            "① 选择源 harness（会话从哪里来）",
            [(n, f"{DISPLAY[n]} — {len(counts[n])} 个会话") for n in with_sessions],
            extra=[("q", "退出"), ("r", "换项目")],
        )
        if not src:
            raw = _input("> ").lower()
            if raw == "q":
                return
            cwd = _input("项目路径: ")
            continue

        # step 3: pick session
        sessions = counts[src]
        chosen = _menu(
            "② 选择会话",
            [
                (
                    s.id,
                    f"{truncate(s.title or s.preview or '(无标题)', 56)} · "
                    f"{ms_to_iso(s.updated_at or s.created_at) or '?'} · {s.message_count or '?'} 条",
                )
                for s in sessions[:30]
            ],
            extra=[("b", "返回上一步"), ("q", "退出")],
        )
        if not chosen:
            raw = _input("> ").lower()
            if raw == "q":
                return
            continue
        summary = next(s for s in sessions if s.id == chosen)

        # step 4: pick target harness
        targets = [n for n in ADAPTER_ORDER if n != src and get_adapter(n).is_available()]
        dst = _menu("③ 迁移到哪个 harness", [(n, DISPLAY[n]) for n in targets], extra=[("b", "返回"), ("q", "退出")])
        if not dst:
            raw = _input("> ").lower()
            if raw == "q":
                return
            continue

        # step 5: plan (dry-run) and confirm
        try:
            plan = copy_session(src, chosen, dst, cwd=None, dry_run=True, on_conflict="skip")
        except AdapterError as exc:
            print(f"⚠ 计划失败: {exc}")
            continue
        print("\n▸ 迁移计划（dry-run）:")
        print(print_manifest(plan))
        answer = _input("\n④ 确认执行迁移? [y/N] > ").lower()
        if answer != "y":
            print("已取消。")
            continue
        try:
            manifest = copy_session(
                src,
                chosen,
                dst,
                cwd=None,
                dry_run=False,
                on_conflict=plan["target"].get("skipped") and "new-id" or "skip",
            )
        except AdapterError as exc:
            print(f"⚠ 执行失败: {exc}")
            continue
        print("\n✅ 完成:")
        print(print_manifest(manifest))
        again = _input("\n[a] 继续迁移其他会话  [q] 退出 > ").lower()
        if again != "a":
            return

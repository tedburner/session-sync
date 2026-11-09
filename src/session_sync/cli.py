"""session-sync CLI: wizard (default), doctor, list, show, report, copy, mcp."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .adapters import ADAPTER_ORDER, ROOT_OVERRIDES


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="session-sync",
        description="跨 agent harness 会话互导入：Claude Code / Codex / ZCode / Qoder / Pi / dsh / WorkBuddy",
    )
    p.add_argument("--version", action="version", version=f"session-sync {__version__}")
    sub = p.add_subparsers(dest="cmd")

    d = sub.add_parser("doctor", aliases=["doc"], help="探测各 harness 安装与数据位置")
    d.add_argument("--cwd", "-c", help="同时统计该项目的会话数")

    lst = sub.add_parser("list", aliases=["ls"], help="列出会话（默认全部 harness）")
    lst.add_argument("--harness", "-H", "--h", choices=ADAPTER_ORDER)
    lst.add_argument("--cwd", "-c", help="按项目目录过滤（默认不过滤）")
    lst.add_argument("--limit", "-l", type=int, default=30)
    lst.add_argument("--json", "-j", action="store_true", help="输出 JSON")
    lst.add_argument("--copy", "-y", action="store_true", help="把所列会话的 id 复制到系统剪贴板")

    sh = sub.add_parser("show", aliases=["cat"], help="渲染一个会话为 Markdown")
    sh.add_argument("harness", choices=ADAPTER_ORDER)
    sh.add_argument("session_id")
    sh.add_argument("--out", "-o", help="写入文件而非 stdout")
    sh.add_argument("--max-tool-output", type=int, default=2000)

    rp = sub.add_parser("report", aliases=["rpt"], help="生成自包含 HTML dashboard")
    rp.add_argument("--harness", "-H", "--h", choices=ADAPTER_ORDER)
    rp.add_argument("--cwd", "-c", help="按项目目录过滤")
    rp.add_argument("--out", "-o", default="session-sync-report.html")
    rp.add_argument("--max", "-m", type=int, default=20, help="嵌入正文的最大会话数（按最近排序）")
    rp.add_argument("--max-chars", type=int, default=400_000, help="单个会话正文字符上限")

    cp = sub.add_parser("copy", aliases=["cp"], help="迁移一个会话（默认 dry-run，--execute 真正写入）")
    cp.add_argument("src", choices=ADAPTER_ORDER)
    cp.add_argument("session_id")
    cp.add_argument("--to", "-t", dest="dst", choices=ADAPTER_ORDER, required=True)
    cp.add_argument("--cwd", "-c", help="覆盖目标 cwd")
    cp.add_argument("--cwd-map", metavar="OLD=NEW", action="append", default=[], help="按前缀重映射 cwd，可多次")
    cp.add_argument("--on-conflict", choices=["skip", "new-id", "overwrite"], default="skip")
    cp.add_argument("--execute", "-x", action="store_true", help="真正执行（默认 dry-run）")
    cp.add_argument("--manifest-out", help="把 manifest JSON 写到该目录")
    cp.add_argument("--src-root", help="覆盖源 harness 数据根目录")
    cp.add_argument("--dst-root", help="覆盖目标 harness 数据根目录")
    cp.add_argument("--allow-dsh-write", action="store_true", help="允许实验性 dsh 写入")

    ui = sub.add_parser("ui", aliases=["gui"], help="启动本地 GUI（Chrome 应用窗口）")
    ui.add_argument("--port", "-p", type=int, default=8765, help="监听端口（默认 8765，占用时自动改用随机端口）")
    ui.add_argument("--no-open", "-n", action="store_true", help="只启动服务，不自动打开窗口")
    ui.add_argument("--cwd", "-c", help="初始项目目录（默认当前目录）")
    ui.add_argument("--harness", "-H", "--h", choices=ADAPTER_ORDER, help="初始只显示该 harness")
    ui.add_argument("--allow-dsh-write", action="store_true", help="允许实验性 dsh 写入")

    sub.add_parser("mcp", help="以 MCP server 模式运行（stdio）")
    return p


def _apply_root_overrides(args) -> None:
    for name, attr in (("src", "src_root"), ("dst", "dst_root")):
        value = getattr(args, attr, None)
        if value:
            # resolve for the harness used in that slot
            harness = getattr(args, "src" if name == "src" else "dst")
            ROOT_OVERRIDES[harness] = value


# 子命令别名 → 规范名（argparse 传入别名时 args.cmd 保留的是别名原样）
_CMD_ALIASES = {"doc": "doctor", "ls": "list", "cat": "show", "cp": "copy", "rpt": "report", "gui": "ui"}


def _resolve_session_id(adapter, sid: str):
    """会话 ID 支持唯一前缀缩写：精确命中原样返回；唯一前缀补全；歧义返回 None；无匹配原样返回。"""
    ids = [s.id for s in adapter.list_sessions(None)]
    if sid in ids:
        return sid
    matches = sorted({i for i in ids if i.startswith(sid)})
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return None
    return sid


def _copy_to_clipboard(text: str) -> bool:
    """写入系统剪贴板，按平台探测可用命令；无可用命令时返回 False。"""
    import shutil
    import subprocess

    for cmd in (
        ["pbcopy"],  # macOS
        ["wl-copy"],  # Linux Wayland
        ["xclip", "-selection", "clipboard"],  # Linux X11
        ["xsel", "--clipboard", "--input"],
        ["clip.exe"],  # WSL
    ):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, input=text.encode(), check=True)
            return True
    return False


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    if not argv:
        from .wizard import run_wizard

        run_wizard()
        return 0
    args = parser.parse_args(argv)
    if not args.cmd:
        from .wizard import run_wizard

        run_wizard()
        return 0
    args.cmd = _CMD_ALIASES.get(args.cmd, args.cmd)

    if args.cmd == "doctor":
        from .discover import doctor, render_doctor

        cwd = os.path.expanduser(args.cwd) if args.cwd else None
        print(render_doctor(doctor(cwd), cwd))
        return 0

    if args.cmd == "list":
        from .adapters import get_adapter

        names = [args.harness] if args.harness else list(ADAPTER_ORDER)
        cwd = os.path.expanduser(args.cwd) if args.cwd else None
        rows = []
        for n in names:
            adapter = get_adapter(n)
            if not adapter.is_available():
                if args.harness:
                    print(f"⚠ {adapter.display_name} 数据不存在: {adapter.root}", file=sys.stderr)
                continue
            for s in adapter.list_sessions(cwd):
                rows.append(s)
        rows.sort(key=lambda s: (s.updated_at or 0), reverse=True)
        rows = rows[: args.limit]
        if args.json:
            print(json.dumps([r.as_dict() for r in rows], ensure_ascii=False, indent=2))
            return 0
        id_w = max((len(r.id) for r in rows), default=2)
        print(f"{'harness':<12} {'id':<{id_w}} {'updated':<19} {'msgs':>5}  title")
        for r in rows:
            from .utils import ms_to_iso

            ts = ms_to_iso(r.updated_at)
            print(
                f"{r.harness:<12} {r.id:<{id_w}} {(ts[:19].replace('T', ' ') if ts else '?'):<19} "
                f"{r.message_count if r.message_count is not None else '?':>5}  "
                f"{(r.title or r.preview or '(无标题)')[:56]}"
            )
        print(
            f"\n共 {len(rows)} 个会话" + (f"（cwd={cwd}）" if cwd else "")
            + "\n（copy/show 可用完整 id 或唯一前缀）"
        )
        if args.copy and rows:
            if _copy_to_clipboard("\n".join(r.id for r in rows)):
                print(f"📋 已复制 {len(rows)} 个会话 id 到剪贴板")
            else:
                print("错误: 未找到剪贴板命令（pbcopy/wl-copy/xclip/xsel/clip.exe）", file=sys.stderr)
                return 1
        return 0

    if args.cmd == "show":
        from .adapters import get_adapter
        from .ir import render_markdown

        adapter = get_adapter(args.harness)
        sid = _resolve_session_id(adapter, args.session_id)
        if sid is None:
            print(f"会话 ID 前缀不唯一: {args.session_id}", file=sys.stderr)
            return 2
        try:
            events = adapter.read_session(sid)
        except Exception as exc:  # noqa: BLE001
            print(f"错误: {exc}", file=sys.stderr)
            return 1
        text = render_markdown(events, max_tool_output=args.max_tool_output)
        if args.out:
            with open(os.path.expanduser(args.out), "w", encoding="utf-8") as fh:
                fh.write(text)
            print(f"已写入 {args.out}")
        else:
            print(text)
        return 0

    if args.cmd == "report":
        from .adapters import get_adapter
        from .htmlreport import build_transcripts, render_dashboard

        names = [args.harness] if args.harness else list(ADAPTER_ORDER)
        cwd = os.path.expanduser(args.cwd) if args.cwd else None
        sessions = []
        for n in names:
            adapter = get_adapter(n)
            if not adapter.is_available():
                continue
            sessions.extend(adapter.list_sessions(cwd))
        sessions.sort(key=lambda s: (s.updated_at or 0), reverse=True)

        transcripts: dict[str, list] = {}
        loaded = 0
        for s in sessions:
            if loaded >= args.max:
                break
            try:
                events = get_adapter(s.harness).read_session(s.id)
            except Exception as exc:  # noqa: BLE001
                continue
            size = sum(len(str(b)) for m in events if m.get("type") == "message" for b in m.get("blocks") or [])
            if size > args.max_chars:
                continue
            transcripts[f"{s.harness}:{s.id}"] = build_transcripts([events])[0]
            loaded += 1
        out = os.path.expanduser(args.out)
        path = render_dashboard(sessions, transcripts, out)
        print(f"dashboard 已生成: {path}（{len(sessions)} 个会话，嵌入正文 {loaded} 个）")
        return 0

    if args.cmd == "copy":
        from .adapters import get_adapter
        from .migrate import copy_session, print_manifest, save_manifest

        _apply_root_overrides(args)
        if args.allow_dsh_write:
            os.environ["SESSION_SYNC_DSH_WRITE"] = "1"
        cwd_map = {}
        for item in args.cwd_map:
            if "=" not in item:
                print(f"无效 --cwd-map: {item}", file=sys.stderr)
                return 2
            old, new = item.split("=", 1)
            cwd_map[os.path.expanduser(old)] = os.path.expanduser(new)
        src_sid = _resolve_session_id(get_adapter(args.src), args.session_id)
        if src_sid is None:
            print(f"会话 ID 前缀不唯一: {args.session_id}", file=sys.stderr)
            return 2
        try:
            manifest = copy_session(
                args.src,
                src_sid,
                args.dst,
                src_root=args.src_root,
                dst_root=args.dst_root,
                cwd=os.path.expanduser(args.cwd) if args.cwd else None,
                cwd_map=cwd_map or None,
                on_conflict=args.on_conflict,
                dry_run=not args.execute,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"错误: {exc}", file=sys.stderr)
            return 1
        print(print_manifest(manifest))
        if args.manifest_out:
            path = save_manifest(manifest, args.manifest_out)
            print(f"manifest 已写入: {path}")
        return 0

    if args.cmd == "ui":
        from .websrv import run_ui

        return run_ui(
            port=args.port,
            open_window=not args.no_open,
            cwd=args.cwd,
            harness=args.harness,
            allow_dsh_write=args.allow_dsh_write,
        )

    if args.cmd == "mcp":
        from .mcpsrv import main as mcp_main

        mcp_main()
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

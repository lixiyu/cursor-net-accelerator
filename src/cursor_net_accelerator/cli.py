from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from . import __version__
from .config import Settings
from .connect_proxy import generate_pac
from .paths import data_dir
from .resolver_macos import disable as resolver_disable
from .resolver_macos import enable as resolver_enable
from .resolver_macos import neutralize as resolver_neutralize
from .resolver_macos import status as resolver_status
from .service import AcceleratorService


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if args.no_proxy:
        settings = replace(settings, proxy_enabled=False)
    if args.no_web:
        settings = replace(settings, web_enabled=False)
    svc = AcceleratorService(settings)
    svc.run_forever()
    return 0


def _running_run_pids() -> list[int]:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "cursor-net-accelerator run"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    pids: list[int] = []
    for tok in out.split():
        try:
            pids.append(int(tok))
        except ValueError:
            pass
    return pids


def _dns_bound_by(pid: int, port: int) -> bool:
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iUDP:{port}", "-a", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:  # noqa: BLE001
        return False
    return f":{port}" in out


def cmd_restart(_: argparse.Namespace) -> int:
    """零空窗重启：先起新进程（UDP 靠 SO_REUSEPORT 双绑），就绪后再退旧进程。

    直接 kill 再 start 会有 1~2 秒 DNS 空窗；macOS 会把空窗期间的失败缓存住，
    之后几分钟 Cursor 都解析不了域名。这个命令专门消掉这段空窗。
    """
    settings = Settings.from_env()
    me = os.getpid()
    old = [pid for pid in _running_run_pids() if pid != me]
    if not old:
        AcceleratorService(settings).run_forever()
        return 0

    log_file = Path(os.environ.get("CURSOR_NET_ACCEL_LOG") or (data_dir() / "run.log"))
    log_file.parent.mkdir(parents=True, exist_ok=True)
    exe = sys.argv[0] or "cursor-net-accelerator"
    with open(log_file, "ab") as logf:
        child = subprocess.Popen(
            [exe, "run"],
            stdout=logf,
            stderr=logf,
            start_new_session=True,
        )

    deadline = time.time() + 20.0
    ready = False
    while time.time() < deadline:
        if child.poll() is not None:
            print(
                f"新进程启动失败（退出码 {child.returncode}），旧进程保持不变。详见 {log_file}",
                file=sys.stderr,
            )
            return 1
        if _dns_bound_by(child.pid, settings.dns_port):
            ready = True
            break
        time.sleep(0.2)
    if not ready:
        print(
            f"新进程 20 秒内没接管 DNS {settings.dns_port}，保留旧进程。详见 {log_file}",
            file=sys.stderr,
        )
        return 1

    for pid in old:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    print(f"已无缝切换：新 pid={child.pid}；旧 pid={','.join(map(str, old))} 已通知退出")
    return 0


def cmd_probe(_: argparse.Namespace) -> int:
    settings = Settings.from_env()
    svc = AcceleratorService(settings)
    svc.probe_once()
    print(json.dumps(svc.pool.snapshot(), indent=2, ensure_ascii=False))
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    settings = Settings.from_env()
    print(
        json.dumps(
            {
                "version": __version__,
                "settings": {
                    "hosts": list(settings.hosts),
                    "dns": f"{settings.dns_bind}:{settings.dns_port}",
                    "web": f"{settings.web_bind}:{settings.web_port}",
                    "web_enabled": settings.web_enabled,
                    "interval": settings.probe_interval,
                    "upstream_dns": list(settings.upstream_dns),
                    "resolver_domain": settings.resolver_domain,
                },
                "macos_resolver": resolver_status(settings),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def cmd_enable_dns(_: argparse.Namespace) -> int:
    settings = Settings.from_env()
    resolver_enable(settings)
    st = resolver_status(settings)
    print(json.dumps(st, indent=2, ensure_ascii=False))
    print(
        "\n下一步：先启动 `cursor-net-accelerator run`，再确认本机解析白名单域名"
        "已指向可用 IP。",
        file=sys.stderr,
    )
    return 0


def cmd_disable_dns(_: argparse.Namespace) -> int:
    settings = Settings.from_env()
    resolver_disable(settings)
    return 0


def cmd_stop(_: argparse.Namespace) -> int:
    """停用加速器：结束进程，并把 cursor.sh 分域解析交回系统 DNS。

    不动系统代理 / Clash。暂时不用加速器时跑这一条即可。
    """
    settings = Settings.from_env()
    me = os.getpid()
    pids = [pid for pid in _running_run_pids() if pid != me]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    # 等旧进程自己在 stop() 里释放；超时再强杀
    deadline = time.time() + 5.0
    while time.time() < deadline and _running_run_pids():
        time.sleep(0.2)
    for pid in [p for p in _running_run_pids() if p != me]:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    # 再兜底写一次（进程已挂、或从未跑过时也要把规则放开）
    released = resolver_neutralize(settings)
    st = resolver_status(settings)
    if released or st.get("neutralized") or not st.get("installed"):
        print("已停止加速器；cursor.sh 已交回系统 DNS。Clash / 系统代理未改动。")
        if st.get("installed") and st.get("neutralized"):
            print(f"分域文件仍在但已停用: {st.get('path')}（彻底删除可再跑 disable-dns）")
        return 0

    print(
        "进程已停，但无法改写 /etc/resolver（可能仍属 root）。\n"
        "请执行一次：cursor-net-accelerator enable-dns（会 chown 给当前用户），\n"
        "或：cursor-net-accelerator disable-dns（直接删掉分域文件）。",
        file=sys.stderr,
    )
    return 1


def cmd_pac(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    text = generate_pac(settings.hosts, settings.proxy_bind, settings.proxy_port)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        print(args.output)
    else:
        sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cursor-net-accelerator",
        description="Cursor 网络加速器（本机）：排除坏入口 IP，优先连可用地址",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="启动探测循环 + 本地 DNS + 状态页")
    p_run.add_argument(
        "--no-proxy",
        action="store_true",
        help=argparse.SUPPRESS,  # 内部验证用，不在公开帮助里强调
    )
    p_run.add_argument(
        "--no-web",
        action="store_true",
        help="不启动状态页 Web",
    )
    p_run.set_defaults(func=cmd_run)

    p_restart = sub.add_parser(
        "restart",
        help="无缝重启（零 DNS 空窗）：先起新进程，就绪后再退旧进程",
    )
    p_restart.set_defaults(func=cmd_restart)

    p_probe = sub.add_parser("probe", help="探测一轮并打印可用 IP 池")
    p_probe.set_defaults(func=cmd_probe)

    p_status = sub.add_parser("status", help="查看配置与 macOS resolver 状态")
    p_status.set_defaults(func=cmd_status)

    p_en = sub.add_parser(
        "enable-dns",
        help="安装 macOS /etc/resolver（需 sudo），把白名单域指到本地 DNS",
    )
    p_en.set_defaults(func=cmd_enable_dns)

    p_dis = sub.add_parser("disable-dns", help="移除 macOS /etc/resolver 配置")
    p_dis.set_defaults(func=cmd_disable_dns)

    p_stop = sub.add_parser(
        "stop",
        help="停用：结束加速器进程，并把 cursor.sh 交回系统 DNS（不动 Clash）",
    )
    p_stop.set_defaults(func=cmd_stop)

    # 保留 CLI 入口，但不在公开 README 推广
    p_pac = sub.add_parser("pac", help=argparse.SUPPRESS)
    p_pac.add_argument("-o", "--output", help="写入文件路径")
    p_pac.set_defaults(func=cmd_pac)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

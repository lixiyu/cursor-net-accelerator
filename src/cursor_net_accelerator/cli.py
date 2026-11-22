from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace

from . import __version__
from .config import Settings
from .connect_proxy import generate_pac
from .resolver_macos import disable as resolver_disable
from .resolver_macos import enable as resolver_enable
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

    # 保留 CLI 入口，但不在公开 README 推广
    p_pac = sub.add_parser("pac", help=argparse.SUPPRESS)
    p_pac.add_argument("-o", "--output", help="写入文件路径")
    p_pac.set_defaults(func=cmd_pac)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import Settings

RESOLVER_DIR = Path("/etc/resolver")


def resolver_file_path(domain: str) -> Path:
    return RESOLVER_DIR / domain


def resolver_contents(settings: Settings) -> str:
    # macOS per-domain resolver: queries for domain + subdomains go here.
    return (
        f"# managed by cursor-net-accelerator — do not edit by hand\n"
        f"nameserver {settings.dns_bind}\n"
        f"port {settings.dns_port}\n"
    )


def status(settings: Settings) -> dict:
    path = resolver_file_path(settings.resolver_domain)
    exists = path.is_file()
    content = path.read_text(encoding="utf-8") if exists else None
    expected = resolver_contents(settings)
    return {
        "path": str(path),
        "installed": exists,
        "matches": content == expected if exists else False,
        "content": content,
        "expected": expected,
    }


def enable(settings: Settings) -> None:
    """Install /etc/resolver/<domain> (needs root)."""
    path = resolver_file_path(settings.resolver_domain)
    body = resolver_contents(settings)
    if os.geteuid() == 0:
        RESOLVER_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        _flush_dns_cache()
        return

    # escalate once
    script = (
        f"mkdir -p {RESOLVER_DIR} && "
        f"cat > {path} <<'EOF'\n{body}EOF\n"
        f"dscacheutil -flushcache 2>/dev/null; "
        f"killall -HUP mDNSResponder 2>/dev/null; true"
    )
    cmd = ["sudo", "sh", "-c", script]
    print(f"需要管理员权限写入 {path}", file=sys.stderr)
    subprocess.check_call(cmd)
    print(f"已启用域名分流解析: {path}")


def disable(settings: Settings) -> None:
    path = resolver_file_path(settings.resolver_domain)
    if not path.exists():
        print(f"未安装: {path}")
        return
    if os.geteuid() == 0:
        path.unlink()
        _flush_dns_cache()
        return
    print(f"需要管理员权限删除 {path}", file=sys.stderr)
    subprocess.check_call(["sudo", "rm", "-f", str(path)])
    subprocess.call(
        ["sudo", "sh", "-c", "dscacheutil -flushcache; killall -HUP mDNSResponder"],
    )
    print(f"已移除: {path}")


def _flush_dns_cache() -> None:
    subprocess.call(["dscacheutil", "-flushcache"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.call(
        ["killall", "-HUP", "mDNSResponder"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

RESOLVER_DIR = Path("/etc/resolver")


def resolver_file_path(domain: str) -> Path:
    return RESOLVER_DIR / domain


# 国内常用公共 DNS（阿里 / DNSPod）。大陆可达性通常好于 8.8.8.8；
# 不保证绕过污染——只适合当我们服务挂了时，把 cursor.sh 等未封域名解析出来。
CN_PUBLIC_DNS = ("223.5.5.5", "223.6.6.6", "119.29.29.29")


def _system_nameservers(limit: int = 2) -> list[str]:
    """系统当前在用的 DNS（跳过本机）。"""
    ips: list[str] = []
    # 首选 scutil --dns 的 resolver #1：这才是 macOS 实际生效的 DNS
    try:
        out = subprocess.run(
            ["scutil", "--dns"], capture_output=True, text=True, timeout=3
        ).stdout
        in_first = False
        for line in out.splitlines():
            st = line.strip()
            if st.startswith("resolver #1"):
                in_first = True
                continue
            if in_first and st.startswith("resolver #"):
                break
            if in_first and st.startswith("nameserver["):
                ip = st.split(":", 1)[1].strip().split("%")[0]
                if ip.startswith("127.") or ip in ("::1", "localhost"):
                    continue
                if ip and ip not in ips:
                    ips.append(ip)
    except Exception:  # noqa: BLE001
        pass
    if not ips:
        try:
            text = Path("/etc/resolv.conf").read_text(encoding="utf-8")
        except OSError:
            text = ""
        for line in text.splitlines():
            st = line.strip()
            if not st.startswith("nameserver"):
                continue
            parts = st.split()
            if len(parts) < 2:
                continue
            ip = parts[1]
            if ip.startswith("127.") or ip in ("::1", "localhost"):
                continue
            if ip not in ips:
                ips.append(ip)
    return ips[:limit]


def _fallback_nameservers(limit: int = 4) -> list[str]:
    """兜底 DNS：系统已有的 + 国内公共 DNS（去重，总数封顶）。

    macOS 的 nameserver 按顺序试：本机加速器连不上时，再问这些。
    """
    ips = _system_nameservers(limit=limit)
    for ip in CN_PUBLIC_DNS:
        if len(ips) >= limit:
            break
        if ip not in ips:
            ips.append(ip)
    return ips or list(CN_PUBLIC_DNS[:2])


ACTIVE_HEADER = "# managed by cursor-net-accelerator — do not edit by hand"
NEUTRAL_HEADER = "# cursor-net-accelerator: 已停用，cursor.sh 走系统 DNS"


def resolver_contents(settings: Settings) -> str:
    """运行中：先问本机加速器（自定义端口），再退到系统 / 国内公共 DNS。"""
    lines = [ACTIVE_HEADER, f"nameserver {settings.dns_bind}.{settings.dns_port}"]
    for ip in _fallback_nameservers():
        lines.append(f"nameserver {ip}")
    return "\n".join(lines) + "\n"


def neutral_contents() -> str:
    """退出后：只留系统 / 国内公共 DNS，不再指向本机加速器。"""
    lines = [NEUTRAL_HEADER]
    for ip in _fallback_nameservers():
        lines.append(f"nameserver {ip}")
    return "\n".join(lines) + "\n"


def status(settings: Settings) -> dict:
    path = resolver_file_path(settings.resolver_domain)
    exists = path.is_file()
    content = path.read_text(encoding="utf-8") if exists else None
    active = resolver_contents(settings)
    neutral = neutral_contents()
    return {
        "path": str(path),
        "installed": exists,
        # active=True 表示当前确实在劫持并指向本机加速器
        "active": content == active,
        "neutralized": content == neutral,
        "matches": content == active if exists else False,
        "content": content,
        "expected": active,
    }


def activate(settings: Settings) -> bool:
    """把分域规则切成「走加速器」。文件属主是当前用户时不需要 sudo。"""
    path = resolver_file_path(settings.resolver_domain)
    try:
        path.write_text(resolver_contents(settings), encoding="utf-8")
        return True
    except OSError as exc:  # 文件仍是 root 的，需要先执行一次 enable-dns
        log.debug("activate resolver failed: %s", exc)
        return False


def neutralize(settings: Settings) -> bool:
    """退出时把分域规则改回「只走系统 DNS」，释放对 cursor.sh 的劫持。"""
    path = resolver_file_path(settings.resolver_domain)
    if not path.is_file():
        return True
    try:
        path.write_text(neutral_contents(), encoding="utf-8")
        return True
    except OSError as exc:
        log.debug("neutralize resolver failed: %s", exc)
        return False


def enable(settings: Settings) -> None:
    """安装 /etc/resolver/<domain>（需要 root）。

    安装时顺手把文件属主交给当前用户，之后程序启动/退出就能自己切换，
    不再需要 sudo。
    """
    path = resolver_file_path(settings.resolver_domain)
    body = resolver_contents(settings)
    if os.geteuid() == 0:
        RESOLVER_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        _flush_dns_cache()
        return

    # sudo 下 $SUDO_UID/$SUDO_GID 是发起者；os.getuid() 在 python 里也是发起者
    owner = f"{os.getuid()}:{os.getgid()}"
    script = (
        f"mkdir -p {RESOLVER_DIR} && "
        f"cat > {path} <<'EOF'\n{body}EOF\n"
        f"chown {owner} {path} && "
        f"dscacheutil -flushcache 2>/dev/null; "
        f"killall -HUP mDNSResponder 2>/dev/null; true"
    )
    print(f"需要管理员权限写入 {path}", file=sys.stderr)
    subprocess.check_call(["sudo", "sh", "-c", script])
    print(f"已启用域名分流解析: {path}（文件已交给当前用户，之后启停无需 sudo）")


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

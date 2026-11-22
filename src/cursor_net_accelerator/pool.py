from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .probe import IpProbeResult


@dataclass
class IpEntry:
    ip: str
    ok: bool
    connect_ms: float | None
    error: str | None
    updated_at: float
    quarantine_until: float = 0.0


@dataclass
class HostPool:
    hostname: str
    resolved: list[str] = field(default_factory=list)
    entries: dict[str, IpEntry] = field(default_factory=dict)
    updated_at: float = 0.0


class IpPool:
    """Thread-safe short-TTL pool of probed IPs per hostname."""

    def __init__(self, *, quarantine_seconds: float, pool_ttl_seconds: float) -> None:
        self._quarantine = quarantine_seconds
        self._ttl = pool_ttl_seconds
        self._lock = threading.RLock()
        self._hosts: dict[str, HostPool] = {}

    def update(self, hostname: str, resolved: list[str], results: list[IpProbeResult]) -> None:
        now = time.monotonic()
        with self._lock:
            hp = self._hosts.get(hostname) or HostPool(hostname=hostname)
            hp.resolved = list(resolved)
            hp.updated_at = now
            for r in results:
                prev = hp.entries.get(r.ip)
                quarantine_until = prev.quarantine_until if prev else 0.0
                if not r.ok:
                    quarantine_until = now + self._quarantine
                elif prev and prev.quarantine_until > now and r.ok:
                    # recovered: clear quarantine
                    quarantine_until = 0.0
                hp.entries[r.ip] = IpEntry(
                    ip=r.ip,
                    ok=r.ok,
                    connect_ms=r.connect_ms,
                    error=r.error,
                    updated_at=now,
                    quarantine_until=quarantine_until,
                )
            # drop IPs no longer in resolution set (keep briefly if quarantined? drop)
            alive = set(resolved)
            hp.entries = {ip: e for ip, e in hp.entries.items() if ip in alive}
            self._hosts[hostname] = hp

    def resolved_ips(self, hostname: str) -> list[str]:
        with self._lock:
            hp = self._hosts.get(hostname)
            return list(hp.resolved) if hp else []

    def healthy_ips(self, hostname: str) -> list[str]:
        """Return currently usable IPs, best (lowest connect_ms) first."""
        now = time.monotonic()
        with self._lock:
            hp = self._hosts.get(hostname)
            if not hp:
                return []
            usable: list[IpEntry] = []
            for e in hp.entries.values():
                if not e.ok:
                    continue
                if e.quarantine_until > now:
                    continue
                if self._ttl > 0 and (now - e.updated_at) > self._ttl:
                    continue
                usable.append(e)
            usable.sort(
                key=lambda e: (
                    e.connect_ms is None,
                    e.connect_ms if e.connect_ms is not None else 1e9,
                )
            )
            return [e.ip for e in usable]

    def unhealthy_ips(self, hostname: str) -> list[str]:
        """IPs currently considered bad (failed probe and/or quarantined)."""
        now = time.monotonic()
        with self._lock:
            hp = self._hosts.get(hostname)
            if not hp:
                return []
            bad: list[IpEntry] = []
            for e in hp.entries.values():
                if (not e.ok) or e.quarantine_until > now:
                    bad.append(e)
            # worst first (no connect_ms / higher error preference)
            bad.sort(
                key=lambda e: (
                    e.ok,
                    e.connect_ms is not None,
                    -(e.connect_ms or 0),
                )
            )
            return [e.ip for e in bad]

    def select_for_mode(self, hostname: str, mode: str) -> tuple[list[str], dict]:
        """
        Pick A records for DNS answer.

        Modes:
          good: only healthy (accelerate). If none healthy → degrade to all resolved.
          off:  all resolved as upstream order (no picking); annotate known bad.
          bad:  拉闸——故意不返回任何 A（与探测无关）。
        """
        resolved = self.resolved_ips(hostname)
        healthy = self.healthy_ips(hostname)
        unhealthy = self.unhealthy_ips(hostname)
        bad_set = set(unhealthy)

        meta: dict = {
            "mode": mode,
            "resolved": resolved,
            "healthy": healthy,
            "unhealthy": unhealthy,
            "degraded": False,
            "note": None,
        }

        if mode == "off":
            meta["note"] = "关闭加速：原样返回解析结果"
            meta["bad_in_answer"] = [ip for ip in resolved if ip in bad_set]
            return list(resolved), meta

        if mode == "bad":
            meta["note"] = "拉闸测试：不返回任何 IP"
            meta["bad_in_answer"] = []
            return [], meta

        # good (default)
        if healthy:
            meta["note"] = "开启加速：只返回健康 IP"
            meta["bad_in_answer"] = []
            return list(healthy), meta
        if resolved:
            meta["degraded"] = True
            meta["note"] = "开启加速但全部探测失败：降级为原样返回全部解析 IP"
            meta["bad_in_answer"] = list(resolved)
            return list(resolved), meta
        meta["note"] = "无解析结果"
        meta["bad_in_answer"] = []
        return [], meta

    def snapshot(self) -> dict[str, dict]:
        now = time.monotonic()
        with self._lock:
            out: dict[str, dict] = {}
            for host, hp in self._hosts.items():
                entries = []
                for e in hp.entries.values():
                    entries.append(
                        {
                            "ip": e.ip,
                            "ok": e.ok,
                            "connect_ms": e.connect_ms,
                            "error": e.error,
                            "age_s": round(now - e.updated_at, 1),
                            "quarantined": e.quarantine_until > now,
                            "quarantine_left_s": max(0.0, round(e.quarantine_until - now, 1)),
                        }
                    )
                entries.sort(
                    key=lambda x: (
                        not x["ok"],
                        x["connect_ms"] is None,
                        x["connect_ms"] if x["connect_ms"] is not None else 1e9,
                    )
                )
                out[host] = {
                    "resolved": list(hp.resolved),
                    "healthy": self.healthy_ips(host),
                    "updated_age_s": round(now - hp.updated_at, 1) if hp.updated_at else None,
                    "entries": entries,
                }
            return out

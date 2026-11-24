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
    updated_at: float  # monotonic，用于 TTL / 隔离
    first_seen_at: float  # wall clock，入池时间
    last_ok_at: float | None  # wall clock，最近一次探测可用
    quarantine_until: float = 0.0


@dataclass
class HostPool:
    hostname: str
    resolved: list[str] = field(default_factory=list)
    entries: dict[str, IpEntry] = field(default_factory=dict)
    updated_at: float = 0.0


class IpPool:
    """Thread-safe pool: DNS 当前解析 + 备选 IP（曾见过的继续探测）。

    选路（healthy_ips / select_for_mode）目前仍只用「当前 DNS 解析到的」健康 IP；
    备选只负责多探、多记，供监控与后续入选路。
    """

    def __init__(
        self,
        *,
        quarantine_seconds: float,
        pool_ttl_seconds: float,
        candidate_stale_seconds: float = 6 * 3600,
    ) -> None:
        self._quarantine = quarantine_seconds
        self._ttl = pool_ttl_seconds
        self._candidate_stale = max(1.0, float(candidate_stale_seconds))
        self._lock = threading.RLock()
        self._hosts: dict[str, HostPool] = {}

    def known_ips(self, hostname: str) -> list[str]:
        """池里见过的全部 IP，供本轮与「新 DNS」合并探测。

        不能只用「当前 resolved 以外的备选」：DNS 一翻盘时，旧 resolved
        在 update 前仍写在池里，会被误判成「已在解析集」而漏探。
        """
        with self._lock:
            hp = self._hosts.get(hostname)
            if not hp:
                return []
            return list(hp.entries.keys())

    def candidate_ips(self, hostname: str) -> list[str]:
        """相对「池内上一轮 resolved」的备选（展示/调试用，探测请用 known_ips）。"""
        with self._lock:
            hp = self._hosts.get(hostname)
            if not hp:
                return []
            resolved = set(hp.resolved)
            return [ip for ip in hp.entries if ip not in resolved]

    def update(self, hostname: str, resolved: list[str], results: list[IpProbeResult]) -> None:
        now_mono = time.monotonic()
        now_wall = time.time()
        resolved_list = list(resolved)
        resolved_set = set(resolved_list)
        with self._lock:
            hp = self._hosts.get(hostname) or HostPool(hostname=hostname)
            hp.resolved = resolved_list
            hp.updated_at = now_mono
            for r in results:
                prev = hp.entries.get(r.ip)
                quarantine_until = prev.quarantine_until if prev else 0.0
                if not r.ok:
                    quarantine_until = now_mono + self._quarantine
                elif prev and prev.quarantine_until > now_mono and r.ok:
                    quarantine_until = 0.0
                first_seen = prev.first_seen_at if prev else now_wall
                last_ok = prev.last_ok_at if prev else None
                if r.ok:
                    last_ok = now_wall
                hp.entries[r.ip] = IpEntry(
                    ip=r.ip,
                    ok=r.ok,
                    connect_ms=r.connect_ms,
                    error=r.error,
                    updated_at=now_mono,
                    first_seen_at=first_seen,
                    last_ok_at=last_ok,
                    quarantine_until=quarantine_until,
                )
            hp.entries = self._prune_entries(hp.entries, resolved_set, now_wall)
            self._hosts[hostname] = hp

    def _prune_entries(
        self,
        entries: dict[str, IpEntry],
        resolved: set[str],
        now_wall: float,
    ) -> dict[str, IpEntry]:
        kept: dict[str, IpEntry] = {}
        stale = self._candidate_stale
        for ip, e in entries.items():
            if ip in resolved:
                kept[ip] = e
                continue
            # 无成功记录：从入池起算；有成功：从最后可用起算
            anchor = e.last_ok_at if e.last_ok_at is not None else e.first_seen_at
            if now_wall - anchor <= stale:
                kept[ip] = e
        return kept

    def resolved_ips(self, hostname: str) -> list[str]:
        with self._lock:
            hp = self._hosts.get(hostname)
            return list(hp.resolved) if hp else []

    def healthy_ips(self, hostname: str) -> list[str]:
        """选路用：仅当前 DNS 解析集里的健康 IP（备选暂不进入选路）。"""
        now = time.monotonic()
        with self._lock:
            hp = self._hosts.get(hostname)
            if not hp:
                return []
            resolved = set(hp.resolved)
            usable: list[IpEntry] = []
            for e in hp.entries.values():
                if e.ip not in resolved:
                    continue
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
        """当前 DNS 集里判定为坏的 IP。"""
        now = time.monotonic()
        with self._lock:
            hp = self._hosts.get(hostname)
            if not hp:
                return []
            resolved = set(hp.resolved)
            bad: list[IpEntry] = []
            for e in hp.entries.values():
                if e.ip not in resolved:
                    continue
                if (not e.ok) or e.quarantine_until > now:
                    bad.append(e)
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
        now_mono = time.monotonic()
        now_wall = time.time()
        with self._lock:
            out: dict[str, dict] = {}
            for host, hp in self._hosts.items():
                resolved_set = set(hp.resolved)
                entries = []
                candidates = []
                for e in hp.entries.values():
                    in_dns = e.ip in resolved_set
                    row = {
                        "ip": e.ip,
                        "ok": e.ok,
                        "connect_ms": e.connect_ms,
                        "error": e.error,
                        "age_s": round(now_mono - e.updated_at, 1),
                        "quarantined": e.quarantine_until > now_mono,
                        "quarantine_left_s": max(
                            0.0, round(e.quarantine_until - now_mono, 1)
                        ),
                        "in_dns": in_dns,
                        "first_seen_at": e.first_seen_at,
                        "last_ok_at": e.last_ok_at,
                        "first_seen_age_s": round(now_wall - e.first_seen_at, 1),
                        "last_ok_age_s": (
                            round(now_wall - e.last_ok_at, 1)
                            if e.last_ok_at is not None
                            else None
                        ),
                        "role": "dns" if in_dns else "candidate",
                    }
                    entries.append(row)
                    if not in_dns:
                        candidates.append(row)
                entries.sort(
                    key=lambda x: (
                        not x["in_dns"],
                        not x["ok"],
                        x["connect_ms"] is None,
                        x["connect_ms"] if x["connect_ms"] is not None else 1e9,
                    )
                )
                candidates.sort(
                    key=lambda x: (
                        not x["ok"],
                        x["last_ok_age_s"] is None,
                        x["last_ok_age_s"] if x["last_ok_age_s"] is not None else 1e9,
                    )
                )
                out[host] = {
                    "resolved": list(hp.resolved),
                    "healthy": self.healthy_ips(host),
                    "updated_age_s": round(now_mono - hp.updated_at, 1)
                    if hp.updated_at
                    else None,
                    "entries": entries,
                    "candidates": candidates,
                    "candidate_stale_s": self._candidate_stale,
                }
            return out

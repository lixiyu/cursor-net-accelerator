from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _ip_sort_key(ip: str) -> tuple:
    """IPv4 按数值排（10.0.0.2 在 10.0.0.10 之前），其它退回字符串序。"""
    parts = (ip or "").split(".")
    if len(parts) == 4:
        try:
            return (0, tuple(int(p) for p in parts))
        except ValueError:
            pass
    return (1, ip or "")


@dataclass
class IpStats:
    ip: str
    hostnames: set[str] = field(default_factory=set)
    dns_serve_count: int = 0
    dns_preferred_count: int = 0  # appeared as first A record we returned
    peer_seen_count: int = 0
    last_dns_at: float | None = None
    last_peer_at: float | None = None
    last_probe_ok: bool | None = None
    last_probe_ms: float | None = None
    last_probe_at: float | None = None

    def to_dict(self, now: float) -> dict[str, Any]:
        return {
            "ip": self.ip,
            "hostnames": sorted(self.hostnames),
            "dns_serve_count": self.dns_serve_count,
            "dns_preferred_count": self.dns_preferred_count,
            "peer_seen_count": self.peer_seen_count,
            "last_dns_age_s": None
            if self.last_dns_at is None
            else round(now - self.last_dns_at, 1),
            "last_peer_age_s": None
            if self.last_peer_at is None
            else round(now - self.last_peer_at, 1),
            "last_probe_ok": self.last_probe_ok,
            "last_probe_ms": self.last_probe_ms,
            "last_probe_age_s": None
            if self.last_probe_at is None
            else round(now - self.last_probe_at, 1),
        }


class EventStore:
    """In-memory observability: recent events + per-IP aggregates."""

    def __init__(self, *, maxlen: int = 500) -> None:
        self._lock = threading.RLock()
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._ips: dict[str, IpStats] = {}
        self.started_at = time.monotonic()
        self.started_at_iso = _utc_iso()

    def _ip(self, ip: str) -> IpStats:
        st = self._ips.get(ip)
        if st is None:
            st = IpStats(ip=ip)
            self._ips[ip] = st
        return st

    def emit(self, kind: str, **payload: Any) -> None:
        ev = {"ts": _utc_iso(), "kind": kind, **payload}
        with self._lock:
            self._events.appendleft(ev)

    def record_probe(
        self,
        hostname: str,
        *,
        ip: str,
        ok: bool,
        connect_ms: float | None,
        error: str | None,
    ) -> None:
        now = time.monotonic()
        with self._lock:
            st = self._ip(ip)
            st.hostnames.add(hostname)
            st.last_probe_ok = ok
            st.last_probe_ms = connect_ms
            st.last_probe_at = now
            self._events.appendleft(
                {
                    "ts": _utc_iso(),
                    "kind": "probe",
                    "hostname": hostname,
                    "ip": ip,
                    "ok": ok,
                    "connect_ms": connect_ms,
                    "error": error,
                }
            )

    def record_dns(
        self,
        hostname: str,
        ips: list[str],
        *,
        client: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        now = time.monotonic()
        meta = meta or {}
        bad_in_answer = list(meta.get("bad_in_answer") or [])
        with self._lock:
            for i, ip in enumerate(ips):
                st = self._ip(ip)
                st.hostnames.add(hostname)
                st.dns_serve_count += 1
                if i == 0:
                    st.dns_preferred_count += 1
                st.last_dns_at = now
            self._events.appendleft(
                {
                    "ts": _utc_iso(),
                    "kind": "dns",
                    "hostname": hostname,
                    "ips": list(ips),
                    "preferred_ip": ips[0] if ips else None,
                    "client": client,
                    "mode": meta.get("mode"),
                    "note": meta.get("note"),
                    "degraded": bool(meta.get("degraded")),
                    "bad_in_answer": bad_in_answer,
                    "healthy": list(meta.get("healthy") or []),
                    "unhealthy": list(meta.get("unhealthy") or []),
                }
            )

    def record_peer(self, *, ip: str, hostname_guess: str | None, process: str, raw: str) -> None:
        now = time.monotonic()
        with self._lock:
            st = self._ip(ip)
            if hostname_guess:
                st.hostnames.add(hostname_guess)
            st.peer_seen_count += 1
            st.last_peer_at = now
            self._events.appendleft(
                {
                    "ts": _utc_iso(),
                    "kind": "peer",
                    "ip": ip,
                    "hostname": hostname_guess,
                    "process": process,
                    "raw": raw,
                }
            )

    def record_cursor_failure(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._events.appendleft(
                {
                    "ts": event.get("ts") or _utc_iso(),
                    "kind": "cursor_fail",
                    **{k: v for k, v in event.items() if k != "ts"},
                }
            )

    def snapshot(self, *, event_limit: int = 80) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            ips = [st.to_dict(now) for st in self._ips.values()]
            # 先按域名、再按 IP 排：同域名的 IP 聚在一起，比“按热度”更好扫
            ips.sort(
                key=lambda x: (
                    (x["hostnames"] or ["\uffff"])[0],
                    _ip_sort_key(x["ip"]),
                )
            )
            return {
                "started_at": self.started_at_iso,
                "uptime_s": round(now - self.started_at, 1),
                "by_ip": ips,
                "events": list(self._events)[:event_limit],
            }

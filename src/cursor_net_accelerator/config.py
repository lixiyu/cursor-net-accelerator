from __future__ import annotations

import os
from dataclasses import dataclass, replace

from . import user_config


def _csv(name: str, default: str) -> tuple[str, ...]:
    raw = os.environ.get(name, default).strip()
    if not raw:
        return ()
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    """Runtime settings (config.toml + env-overridable)."""

    hosts: tuple[str, ...]
    probe_interval: float  # 兼容旧字段：等于 busy 间隔
    probe_timeout: float
    quarantine_seconds: float
    pool_ttl_seconds: float
    upstream_dns: tuple[str, ...]
    dns_bind: str
    dns_port: int
    proxy_bind: str
    proxy_port: int
    proxy_enabled: bool
    he_parallel: int
    he_timeout: float
    resolver_domain: str
    web_bind: str
    web_port: int
    web_enabled: bool
    peer_interval: float
    fail_interval: float
    health_retention_hours: float
    # 用户配置
    gate_test_enabled: bool
    transport: str  # dns | proxy
    probe_busy_interval: float
    probe_idle_interval: float
    probe_idle_after: float
    probe_min_gap: float

    @classmethod
    def from_env(cls) -> Settings:
        uc = user_config.load()
        busy = float(uc["probe_busy_interval_seconds"])
        # 环境变量仍可覆盖探测忙时间隔（兼容旧 CURSOR_NET_ACCEL_INTERVAL）
        busy = _float("CURSOR_NET_ACCEL_INTERVAL", busy)
        idle = _float(
            "CURSOR_NET_ACCEL_IDLE_INTERVAL",
            float(uc["probe_idle_interval_seconds"]),
        )
        idle_after = _float(
            "CURSOR_NET_ACCEL_IDLE_AFTER",
            float(uc["probe_idle_after_seconds"]),
        )
        min_gap = _float(
            "CURSOR_NET_ACCEL_PROBE_MIN_GAP",
            float(uc["probe_min_gap_seconds"]),
        )
        gate = uc["gate_test_enabled"]
        if os.environ.get("CURSOR_NET_ACCEL_GATE_TEST") is not None:
            gate = _bool("CURSOR_NET_ACCEL_GATE_TEST", False)
        transport = str(uc["transport"])
        env_t = os.environ.get("CURSOR_NET_ACCEL_TRANSPORT", "").strip().lower()
        if env_t in ("dns", "proxy"):
            transport = env_t

        return cls(
            hosts=_csv(
                "CURSOR_NET_ACCEL_HOSTS",
                "api2.cursor.sh,api2direct.cursor.sh,api3.cursor.sh,"
                "repo42.cursor.sh,agentn.global.api5.cursor.sh",
            ),
            probe_interval=busy,
            probe_timeout=_float("CURSOR_NET_ACCEL_TIMEOUT", 3.0),
            quarantine_seconds=_float("CURSOR_NET_ACCEL_QUARANTINE", 120.0),
            pool_ttl_seconds=_float("CURSOR_NET_ACCEL_POOL_TTL", 90.0),
            upstream_dns=_csv("CURSOR_NET_ACCEL_UPSTREAM_DNS", "1.1.1.1,8.8.8.8"),
            dns_bind=os.environ.get("CURSOR_NET_ACCEL_DNS_BIND", "127.0.0.1"),
            dns_port=_int("CURSOR_NET_ACCEL_DNS_PORT", 53553),
            proxy_bind=os.environ.get("CURSOR_NET_ACCEL_PROXY_BIND", "127.0.0.1"),
            proxy_port=_int("CURSOR_NET_ACCEL_PROXY_PORT", 8788),
            # 双模式：默认起代理监听，方便网页切换；--no-proxy / env 可关
            proxy_enabled=_bool("CURSOR_NET_ACCEL_PROXY", True),
            he_parallel=_int("CURSOR_NET_ACCEL_HE_PARALLEL", 3),
            he_timeout=_float("CURSOR_NET_ACCEL_HE_TIMEOUT", 2.0),
            resolver_domain=os.environ.get(
                "CURSOR_NET_ACCEL_RESOLVER_DOMAIN", "cursor.sh"
            ),
            web_bind=os.environ.get("CURSOR_NET_ACCEL_WEB_BIND", "127.0.0.1"),
            web_port=_int("CURSOR_NET_ACCEL_WEB_PORT", 8789),
            web_enabled=_bool("CURSOR_NET_ACCEL_WEB", True),
            peer_interval=_float("CURSOR_NET_ACCEL_PEER_INTERVAL", 10.0),
            fail_interval=_float("CURSOR_NET_ACCEL_FAIL_INTERVAL", 60.0),
            health_retention_hours=_float("CURSOR_NET_ACCEL_HEALTH_RETENTION_HOURS", 6.0),
            gate_test_enabled=bool(gate),
            transport=transport,
            probe_busy_interval=busy,
            probe_idle_interval=idle,
            probe_idle_after=idle_after,
            probe_min_gap=min_gap,
        )

    def with_transport(self, transport: str) -> Settings:
        t = (transport or "").strip().lower()
        if t not in ("dns", "proxy"):
            raise ValueError("transport must be dns or proxy")
        return replace(self, transport=t)

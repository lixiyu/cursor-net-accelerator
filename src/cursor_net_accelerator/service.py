from __future__ import annotations

import logging
import signal
import subprocess
import sys
import threading
import time
from typing import Any

from .config import Settings
from .connect_proxy import ConnectProxy
from .cursor_fail import CursorFailWatcher
from .dns_server import DnsService
from .health import HealthHistory
from .observe import EventStore
from .paths import config_path
from .peer_watch import PeerWatcher
from .pool import IpPool
from .probe import probe_host
from .resolver_macos import status as resolver_status

log = logging.getLogger(__name__)

# good=加速剔坏IP; off=原样返回; bad=拉闸（默认配置关闭）
ROUTING_MODES = ("good", "off", "bad")


class AcceleratorService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = EventStore()
        self.health = HealthHistory(
            retention_hours=settings.health_retention_hours,
        )
        self._mode_lock = threading.Lock()
        self._routing_mode = "good"
        self._activity_lock = threading.Lock()
        self._last_activity_at = time.monotonic()
        self._last_probe_at = 0.0
        self._activity_event = threading.Event()
        self._probe_lock = threading.Lock()
        self.pool = IpPool(
            quarantine_seconds=settings.quarantine_seconds,
            pool_ttl_seconds=settings.pool_ttl_seconds,
        )
        self.dns = DnsService(
            pool=self.pool,
            hosts=settings.hosts,
            upstream_dns=settings.upstream_dns,
            bind=settings.dns_bind,
            port=settings.dns_port,
            get_mode=self.get_routing_mode,
            on_answer=self._on_dns_answer,
            resolver_domain=settings.resolver_domain,
        )
        self.proxy: ConnectProxy | None = None
        if settings.proxy_enabled:
            self.proxy = ConnectProxy(
                pool=self.pool,
                hosts=settings.hosts,
                bind=settings.proxy_bind,
                port=settings.proxy_port,
                he_parallel=settings.he_parallel,
                he_timeout=settings.he_timeout,
                on_connect=lambda _host: self.note_activity("proxy"),
            )
        self._stop = threading.Event()
        self._probe_thread: threading.Thread | None = None
        self._peer_thread: threading.Thread | None = None
        self._fail_thread: threading.Thread | None = None
        self._web_server = None
        self.fail_watcher = CursorFailWatcher(
            self.store,
            interval=settings.fail_interval,
            stop_event=self._stop,
        )
        self.peer_watcher = PeerWatcher(
            self.store,
            interval=settings.peer_interval,
            known_ips=self._known_ips,
            ip_to_host=self._ip_to_host,
            stop_event=self._stop,
        )

    def _on_dns_answer(
        self,
        host: str,
        ips: list[str],
        client: str | None,
        meta: dict[str, Any],
    ) -> None:
        self.store.record_dns(host, ips, client=client, meta=meta)
        # 白名单域名被问到 → 视为在用
        if host.lower() in {h.lower() for h in self.settings.hosts}:
            self.note_activity("dns")

    def note_activity(self, source: str = "") -> None:
        with self._activity_lock:
            self._last_activity_at = time.monotonic()
        self._activity_event.set()
        if source:
            log.debug("activity from %s", source)

    def get_routing_mode(self) -> str:
        with self._mode_lock:
            mode = self._routing_mode
        if not self.settings.gate_test_enabled and mode == "bad":
            return "good"
        return mode

    def set_routing_mode(self, mode: str) -> str:
        mode = (mode or "").strip().lower()
        if mode not in ROUTING_MODES:
            raise ValueError(f"mode must be one of {ROUTING_MODES}")
        if mode == "bad" and not self.settings.gate_test_enabled:
            raise ValueError("拉闸测试已关闭（config.toml: gate_test_enabled = false）")
        with self._mode_lock:
            old = self._routing_mode
            self._routing_mode = mode
        flushed = self._flush_system_dns_cache()
        self.store.emit(
            "routing_mode",
            from_mode=old,
            to_mode=mode,
            dns_cache_flushed=flushed,
            note={
                "good": "开启加速：DNS 只返回健康 IP",
                "off": "关闭加速：DNS 原样返回，仍经本机 DNS",
                "bad": "拉闸测试：DNS 不返回任何 IP",
            }[mode],
        )
        log.info("routing mode %s → %s (dns cache flushed=%s)", old, mode, flushed)
        return mode

    def _flush_system_dns_cache(self) -> bool:
        if sys.platform != "darwin":
            return False
        ok = False
        for cmd in (
            ["dscacheutil", "-flushcache"],
            ["killall", "-HUP", "mDNSResponder"],
        ):
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=3)
                ok = ok or proc.returncode == 0
            except Exception as exc:  # noqa: BLE001
                log.debug("flush dns via %s failed: %s", cmd[0], exc)
        log.debug("system dns cache flush: %s", "ok" if ok else "skipped (need root)")
        return ok

    def _known_ips(self) -> set[str]:
        snap = self.pool.snapshot()
        out: set[str] = set()
        for host_data in snap.values():
            out.update(host_data.get("resolved") or [])
            for e in host_data.get("entries") or []:
                out.add(e["ip"])
        return out

    def _ip_to_host(self, ip: str) -> str | None:
        snap = self.pool.snapshot()
        for host, host_data in snap.items():
            if ip in (host_data.get("resolved") or []):
                return host
            for e in host_data.get("entries") or []:
                if e["ip"] == ip:
                    return host
        return None

    def _idle(self) -> bool:
        with self._activity_lock:
            age = time.monotonic() - self._last_activity_at
        return age >= self.settings.probe_idle_after

    def current_probe_interval(self) -> float:
        return (
            self.settings.probe_idle_interval
            if self._idle()
            else self.settings.probe_busy_interval
        )

    def probe_once(self) -> None:
        with self._probe_lock:
            for host in self.settings.hosts:
                try:
                    resolved, results = probe_host(host, self.settings)
                    self.pool.update(host, resolved, results)
                    self.health.add_round(
                        host,
                        [(r.ip, r.ok, r.connect_ms) for r in results],
                        routing_mode=self.get_routing_mode(),
                    )
                    healthy = self.pool.healthy_ips(host)
                    log.info(
                        "probe %s resolved=%s healthy=%s",
                        host,
                        resolved,
                        healthy,
                    )
                    for r in results:
                        self.store.record_probe(
                            host,
                            ip=r.ip,
                            ok=r.ok,
                            connect_ms=r.connect_ms,
                            error=r.error,
                        )
                        mark = "ok" if r.ok else "FAIL"
                        ms = f"{r.connect_ms:.0f}ms" if r.connect_ms is not None else "-"
                        extra = f" ({r.error})" if r.error else ""
                        log.info("  [%s] %s %s%s", mark, r.ip, ms, extra)
                except Exception as exc:  # noqa: BLE001
                    log.error("probe %s failed: %s", host, exc)
                    self.store.emit("probe_error", hostname=host, error=str(exc))
            self._last_probe_at = time.monotonic()

    def status_payload(self) -> dict[str, Any]:
        mode = self.get_routing_mode()
        routing_modes = {
            "good": "开启加速",
            "off": "关闭加速",
        }
        if self.settings.gate_test_enabled:
            routing_modes["bad"] = "拉闸测试"
        with self._activity_lock:
            activity_age = round(time.monotonic() - self._last_activity_at, 1)
        return {
            "ok": True,
            "routing_mode": mode,
            "routing_modes": routing_modes,
            "gate_test_enabled": self.settings.gate_test_enabled,
            # DNS 与代理可同时开：谁被 Cursor 用到谁就生效
            "paths": {
                "dns": {
                    "enabled": True,
                    "listen": f"{self.settings.dns_bind}:{self.settings.dns_port}",
                    "label": "DNS 选路",
                },
                "proxy": {
                    "enabled": self.proxy is not None,
                    "listen": f"{self.settings.proxy_bind}:{self.settings.proxy_port}"
                    if self.proxy is not None
                    else None,
                    "label": "CONNECT 代理选路",
                    "pac_url": "/api/pac" if self.proxy is not None else None,
                },
            },
            "probe_schedule": {
                "busy_interval_s": self.settings.probe_busy_interval,
                "idle_interval_s": self.settings.probe_idle_interval,
                "idle_after_s": self.settings.probe_idle_after,
                "min_gap_s": self.settings.probe_min_gap,
                "idle": self._idle(),
                "current_interval_s": self.current_probe_interval(),
                "activity_age_s": activity_age,
                "last_probe_age_s": round(time.monotonic() - self._last_probe_at, 1)
                if self._last_probe_at
                else None,
            },
            "settings": {
                "hosts": list(self.settings.hosts),
                "dns": f"{self.settings.dns_bind}:{self.settings.dns_port}",
                "proxy": f"{self.settings.proxy_bind}:{self.settings.proxy_port}"
                if self.proxy is not None
                else None,
                "proxy_enabled": self.proxy is not None,
                "web": f"{self.settings.web_bind}:{self.settings.web_port}",
                "interval": self.settings.probe_busy_interval,
                "resolver_domain": self.settings.resolver_domain,
                "health_retention_hours": self.settings.health_retention_hours,
                "config_path": str(config_path()),
            },
            "macos_resolver": resolver_status(self.settings),
            "dns_stats": self.dns.stats(),
            "pool": self.pool.snapshot(),
            "observe": self.store.snapshot(),
            "health": self.health.timelines(
                probe_interval=self.settings.probe_busy_interval,
            ),
            "cursor_failures": self.fail_watcher.last_scan,
        }

    def start(self) -> None:
        self._stop.clear()
        self.dns.start()
        if self.proxy is not None:
            self.proxy.start()
        if self.settings.web_enabled:
            self._start_web()
        self._probe_thread = threading.Thread(
            target=self._probe_loop_boot, name="probe-loop", daemon=True
        )
        self._probe_thread.start()
        self._peer_thread = threading.Thread(
            target=self.peer_watcher.loop, name="peer-watch", daemon=True
        )
        self._peer_thread.start()
        self._fail_thread = threading.Thread(
            target=self.fail_watcher.loop, name="fail-watch", daemon=True
        )
        self._fail_thread.start()

    def _start_web(self) -> None:
        from .server import create_app

        import uvicorn

        app = create_app(self)
        config = uvicorn.Config(
            app,
            host=self.settings.web_bind,
            port=self.settings.web_port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        self._web_server = server

        def _run() -> None:
            log.info(
                "status UI http://%s:%s/",
                self.settings.web_bind,
                self.settings.web_port,
            )
            server.run()

        t = threading.Thread(target=_run, name="web", daemon=True)
        t.start()

    def stop(self) -> None:
        self._stop.set()
        self._activity_event.set()
        if self._web_server is not None:
            self._web_server.should_exit = True
        for th in (self._probe_thread, self._peer_thread, self._fail_thread):
            if th is not None:
                th.join(timeout=3)
        self._probe_thread = None
        self._peer_thread = None
        self._fail_thread = None
        self.dns.stop()
        if self.proxy is not None:
            self.proxy.stop()

    def _probe_loop_boot(self) -> None:
        self.probe_once()
        while not self._stop.is_set():
            interval = self.current_probe_interval()
            woke = self._activity_event.wait(timeout=interval)
            self._activity_event.clear()
            if self._stop.is_set():
                break
            now = time.monotonic()
            gap = now - self._last_probe_at
            if gap < self.settings.probe_min_gap:
                remain = self.settings.probe_min_gap - gap
                if self._stop.wait(remain):
                    break
            reason = "activity" if woke else ("idle" if self._idle() else "busy")
            log.debug(
                "probe tick reason=%s interval=%.0fs",
                reason,
                interval,
            )
            self.probe_once()
            removed = self.health.prune()
            if removed:
                log.debug("pruned %s expired health rounds", removed)

    def run_forever(self) -> None:
        self.start()

        def _handle(signum: int, _frame: object) -> None:
            log.info("signal %s, shutting down", signum)
            self._stop.set()
            self._activity_event.set()

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, _handle)
            signal.signal(signal.SIGTERM, _handle)
        log.info(
            "running: dns=%s:%s proxy=%s web=%s hosts=%s gate=%s",
            self.settings.dns_bind,
            self.settings.dns_port,
            f"{self.settings.proxy_bind}:{self.settings.proxy_port}"
            if self.proxy is not None
            else "off",
            f"{self.settings.web_bind}:{self.settings.web_port}"
            if self.settings.web_enabled
            else "off",
            ",".join(self.settings.hosts),
            self.settings.gate_test_enabled,
        )
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        finally:
            self.stop()

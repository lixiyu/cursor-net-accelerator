from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import health_history_path


def _utc_iso(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class ProbeSample:
    ts: float
    hostname: str
    ip: str
    ok: bool
    connect_ms: float | None


@dataclass
class ProbeRound:
    ts: float
    hostname: str
    samples: list[ProbeSample] = field(default_factory=list)
    routing_mode: str = "good"  # good | off | bad，当时选路模式

    @property
    def any_ok(self) -> bool:
        return any(s.ok for s in self.samples)

    @property
    def all_ok(self) -> bool:
        return bool(self.samples) and all(s.ok for s in self.samples)


def picked_ips_for_mode(samples: list[ProbeSample], mode: str) -> list[str]:
    """与 IpPool.select_for_mode 同一套挑选：用本轮探测结果当健康真相。"""
    order = [s.ip for s in samples]
    ok_ips = [s.ip for s in samples if s.ok]
    bad_ips = [s.ip for s in samples if not s.ok]
    m = (mode or "good").strip().lower()
    if m == "off":
        return order
    if m == "bad":
        return []
    # good：健康优先；全挂则降级为全部
    if ok_ips:
        return ok_ips
    return order


class HealthHistory:
    """Probe rounds for health strips; memory + optional disk under data/."""

    def __init__(
        self,
        *,
        retention_hours: float = 6.0,
        maxlen: int = 5000,
        persist_path: Path | None = None,
        persist: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self._rounds: deque[ProbeRound] = deque(maxlen=maxlen)
        self.retention_hours = max(0.1, float(retention_hours))
        self._persist = persist
        self._persist_path = persist_path if persist_path is not None else health_history_path()
        self._dirty = False
        if self._persist:
            self._load_locked()

    def add_round(
        self,
        hostname: str,
        results: list[tuple[str, bool, float | None]],
        *,
        ts: float | None = None,
        routing_mode: str = "good",
    ) -> None:
        now = ts if ts is not None else time.time()
        samples = [
            ProbeSample(ts=now, hostname=hostname, ip=ip, ok=ok, connect_ms=ms)
            for ip, ok, ms in results
        ]
        mode = (routing_mode or "good").strip().lower() or "good"
        with self._lock:
            self._rounds.append(
                ProbeRound(ts=now, hostname=hostname, samples=samples, routing_mode=mode)
            )
            self._prune_locked(now)
            self._dirty = True
            self._save_locked()

    def prune(self, now: float | None = None) -> int:
        """Drop rounds older than retention. Returns number removed."""
        with self._lock:
            removed = self._prune_locked(now if now is not None else time.time())
            if removed:
                self._dirty = True
                self._save_locked()
            return removed

    def clear(self) -> None:
        with self._lock:
            self._rounds.clear()
            self._dirty = True
            self._save_locked()

    def seed_demo(
        self,
        hostname: str,
        ips: list[str],
        *,
        hours: float | None = None,
        interval: float = 30.0,
        now: float | None = None,
        routing_mode: str = "good",
    ) -> int:
        """Fill history with varied patterns for UI review."""
        if len(ips) < 1:
            return 0
        ip_a = ips[0]
        ip_b = ips[1 % len(ips)]
        ip_c = ips[2 % len(ips)] if len(ips) >= 2 else ips[0]
        if len(ips) >= 3:
            ip_a, ip_b, ip_c = ips[0], ips[1], ips[2]

        window_h = self.retention_hours if hours is None else max(0.1, float(hours))
        end = now if now is not None else time.time()
        start = end - window_h * 3600
        step = max(5.0, float(interval))
        mode = (routing_mode or "good").strip().lower() or "good"
        n = 0
        t = start
        with self._lock:
            self._rounds.clear()
            while t <= end + 1e-6:
                p = 0.0 if end <= start else (t - start) / (end - start)
                tick = int((t - start) / step)
                ok_a = p >= 0.55
                ok_b = p < 0.78
                ok_c = 0.32 <= p < 0.40
                if 0.14 <= p < 0.18:
                    ok_a = ok_b = ok_c = False
                if 0.60 <= p < 0.64:
                    ok_a = ok_b = ok_c = True
                by_ip = {ip_a: ok_a, ip_b: ok_b, ip_c: ok_c}
                results = [
                    (ip, by_ip[ip], 40.0 + (tick % 7) * 3.0 if by_ip[ip] else None)
                    for ip in dict.fromkeys([ip_a, ip_b, ip_c])
                ]
                samples = [
                    ProbeSample(ts=t, hostname=hostname, ip=ip, ok=ok, connect_ms=ms)
                    for ip, ok, ms in results
                ]
                self._rounds.append(
                    ProbeRound(ts=t, hostname=hostname, samples=samples, routing_mode=mode)
                )
                n += 1
                t += step
            self._prune_locked(end)
            self._dirty = True
            self._save_locked()
        return n

    def _prune_locked(self, now: float) -> int:
        cutoff = now - self.retention_hours * 3600
        before = len(self._rounds)
        while self._rounds and self._rounds[0].ts < cutoff:
            self._rounds.popleft()
        return before - len(self._rounds)

    def _round_to_dict(self, rnd: ProbeRound) -> dict[str, Any]:
        return {
            "ts": rnd.ts,
            "hostname": rnd.hostname,
            "routing_mode": rnd.routing_mode,
            "samples": [
                {
                    "ip": s.ip,
                    "ok": s.ok,
                    "connect_ms": s.connect_ms,
                }
                for s in rnd.samples
            ],
        }

    def _round_from_dict(self, raw: dict[str, Any]) -> ProbeRound | None:
        try:
            ts = float(raw["ts"])
            hostname = str(raw["hostname"])
            mode = str(raw.get("routing_mode") or "good")
            samples: list[ProbeSample] = []
            for s in raw.get("samples") or []:
                samples.append(
                    ProbeSample(
                        ts=ts,
                        hostname=hostname,
                        ip=str(s["ip"]),
                        ok=bool(s["ok"]),
                        connect_ms=s.get("connect_ms"),
                    )
                )
            return ProbeRound(ts=ts, hostname=hostname, samples=samples, routing_mode=mode)
        except (KeyError, TypeError, ValueError):
            return None

    def _load_locked(self) -> None:
        path = self._persist_path
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        rows = data.get("rounds") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return
        loaded: list[ProbeRound] = []
        for row in rows:
            if isinstance(row, dict):
                rnd = self._round_from_dict(row)
                if rnd is not None:
                    loaded.append(rnd)
        loaded.sort(key=lambda r: r.ts)
        self._rounds.clear()
        self._rounds.extend(loaded)
        self._prune_locked(time.time())

    def _save_locked(self) -> None:
        if not self._persist:
            return
        path = self._persist_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "retention_hours": self.retention_hours,
                "saved_at": _utc_iso(),
                "rounds": [self._round_to_dict(r) for r in self._rounds],
            }
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp.replace(path)
            self._dirty = False
        except OSError:
            pass

    def timelines(
        self,
        *,
        hours: float | None = None,
        target_buckets: int = 90,
        probe_interval: float = 30.0,
    ) -> dict[str, Any]:
        window_h = self.retention_hours if hours is None else max(0.1, float(hours))
        cutoff = time.time() - window_h * 3600
        with self._lock:
            removed = self._prune_locked(time.time())
            if removed:
                self._save_locked()
            rounds = [r for r in self._rounds if r.ts >= cutoff]

        if not rounds:
            return {
                "hours": window_h,
                "retention_hours": self.retention_hours,
                "slot_count": 0,
                "slot_seconds": None,
                "hosts": {},
                "persist_path": str(self._persist_path),
                "note": "暂无探测历史（已开启落盘，跑几轮探测后会留下近几小时条）",
            }

        t0 = min(r.ts for r in rounds)
        t1 = max(r.ts for r in rounds)
        span = max(t1 - t0, 1.0)
        min_slot = max(float(probe_interval) * 0.9, 20.0)
        slot_seconds = max(span / max(target_buckets, 1), min_slot)
        slot_count = max(1, int(span / slot_seconds) + 1)

        def slot_index(ts: float) -> int:
            return min(slot_count - 1, max(0, int((ts - t0) / slot_seconds)))

        host_slots: dict[str, list[dict[str, int]]] = defaultdict(
            lambda: [{"ok": 0, "fail": 0} for _ in range(slot_count)]
        )
        ip_slots: dict[str, dict[str, list[dict[str, int]]]] = defaultdict(
            lambda: defaultdict(lambda: [{"ok": 0, "fail": 0} for _ in range(slot_count)])
        )
        # 加速效果：只统计「当时模式挑中的 IP」探测
        accel_slots: dict[str, list[dict[str, int]]] = defaultdict(
            lambda: [{"ok": 0, "fail": 0} for _ in range(slot_count)]
        )

        for rnd in rounds:
            i = slot_index(rnd.ts)
            hcell = host_slots[rnd.hostname][i]
            for s in rnd.samples:
                cell = ip_slots[rnd.hostname][s.ip][i]
                if s.ok:
                    cell["ok"] += 1
                    hcell["ok"] += 1
                else:
                    cell["fail"] += 1
                    hcell["fail"] += 1

            picked = set(picked_ips_for_mode(rnd.samples, rnd.routing_mode))
            acell = accel_slots[rnd.hostname][i]
            if not picked:
                # 拉闸但无可返回的坏 IP → 视为不可达
                acell["fail"] += 1
            else:
                for s in rnd.samples:
                    if s.ip not in picked:
                        continue
                    if s.ok:
                        acell["ok"] += 1
                    else:
                        acell["fail"] += 1

        def paint_service_state(ok: int, fail: int) -> tuple[str, str]:
            if ok == 0 and fail == 0:
                return "empty", "无数据"
            if fail == 0:
                return "ok", "健康"
            if ok == 0:
                return "bad", "不可达"
            return "mixed", "有红有绿"

        def paint_ip_state(ok: int, fail: int) -> tuple[str, str]:
            if ok == 0 and fail == 0:
                return "empty", "无数据"
            if fail == 0:
                return "ok", "健康"
            if ok == 0:
                return "bad", "不可达"
            if ok > fail:
                return "ok", "健康"
            return "bad", "不可达"

        def segments_from_cells(
            cells: list[dict[str, int]],
            *,
            occupied: set[int],
            paint,
        ) -> list[dict[str, Any]]:
            segs: list[dict[str, Any]] = []
            for i, c in enumerate(cells):
                if i not in occupied:
                    continue
                state, label = paint(c["ok"], c["fail"])
                if state == "empty":
                    continue
                segs.append(
                    {
                        "start": _utc_iso(t0 + i * slot_seconds),
                        "end": _utc_iso(t0 + (i + 1) * slot_seconds),
                        "state": state,
                        "label": label,
                        "count": 1,
                        "ok": c["ok"],
                        "fail": c["fail"],
                    }
                )
            return segs

        hosts_out: dict[str, Any] = {}
        for hostname in sorted(set(r.hostname for r in rounds)):
            occupied = {
                i
                for i, c in enumerate(host_slots[hostname])
                if c["ok"] or c["fail"]
            }
            # 加速格也可能只有「空挑选」的 fail
            for i, c in enumerate(accel_slots[hostname]):
                if c["ok"] or c["fail"]:
                    occupied.add(i)
            ips = sorted(ip_slots[hostname].keys())
            hosts_out[hostname] = {
                "accelerated": {
                    "label": "加速效果（按当时选路挑中的 IP 上色）",
                    "slot_segments": segments_from_cells(
                        accel_slots[hostname],
                        occupied=occupied,
                        paint=paint_service_state,
                    ),
                },
                "service": {
                    "label": "服务可达性（上游全部 IP）",
                    "slot_segments": segments_from_cells(
                        host_slots[hostname],
                        occupied=occupied,
                        paint=paint_service_state,
                    ),
                },
                "ips": [
                    {
                        "ip": ip,
                        "label": f"IP {ip}",
                        "slot_segments": segments_from_cells(
                            ip_slots[hostname][ip],
                            occupied=occupied,
                            paint=paint_ip_state,
                        ),
                    }
                    for ip in ips
                ],
            }

        return {
            "hours": window_h,
            "retention_hours": self.retention_hours,
            "slot_count": slot_count,
            "slot_seconds": round(slot_seconds, 1),
            "from": _utc_iso(t0),
            "to": _utc_iso(t1),
            "round_count": len(rounds),
            "persist_path": str(self._persist_path),
            "hosts": hosts_out,
        }

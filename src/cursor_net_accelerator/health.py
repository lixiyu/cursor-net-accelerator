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
    # 当时 DNS 解析集 / 选路实际下发（挑中）的 IP；旧落盘可能为空
    resolved_ips: list[str] = field(default_factory=list)
    answered_ips: list[str] = field(default_factory=list)

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


def hold_segments(
    events: list[tuple[float, str]],
    *,
    t0: float,
    t1: float,
    hold_s: float,
) -> list[dict[str, Any]]:
    """探测点向前覆盖：每点覆盖 (T-hold, T]，但左边界不低于上一探时刻；未覆盖为空白。

    events: (ts, state) state ∈ ok|bad|mixed
    """
    if not events or t1 <= t0 or hold_s <= 0:
        return []
    ordered = sorted(events, key=lambda x: x[0])
    labels = {"ok": "健康", "bad": "不可达", "mixed": "有红有绿"}
    raw: list[tuple[float, float, str]] = []
    prev_ts: float | None = None
    for ts, state in ordered:
        if state not in labels:
            continue
        left = max(t0, ts - hold_s)
        if prev_ts is not None:
            left = max(left, prev_ts)
        right = min(float(ts), t1)
        if left < right:
            raw.append((left, right, state))
        prev_ts = float(ts)
    if not raw:
        return []
    # 合并相邻同色
    merged: list[tuple[float, float, str]] = [raw[0]]
    for a, b, st in raw[1:]:
        la, lb, lst = merged[-1]
        if st == lst and abs(a - lb) < 1e-6:
            merged[-1] = (la, b, st)
        else:
            merged.append((a, b, st))
    return [
        {
            "start": _utc_iso(a),
            "end": _utc_iso(b),
            "state": st,
            "label": labels[st],
            "count": 1,
        }
        for a, b, st in merged
    ]


class HealthHistory:
    """Probe rounds for health strips; memory + optional disk under data/."""

    def __init__(
        self,
        *,
        retention_hours: float = 6.0,
        hold_seconds: float = 300.0,
        maxlen: int = 5000,
        persist_path: Path | None = None,
        persist: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self._rounds: deque[ProbeRound] = deque(maxlen=maxlen)
        self.retention_hours = max(0.1, float(retention_hours))
        # 每次探测结果向前覆盖的时长（默认 5 分钟）
        self.hold_seconds = max(30.0, float(hold_seconds))
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
        resolved_ips: list[str] | tuple[str, ...] | None = None,
        answered_ips: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        now = ts if ts is not None else time.time()
        samples = [
            ProbeSample(ts=now, hostname=hostname, ip=ip, ok=ok, connect_ms=ms)
            for ip, ok, ms in results
        ]
        mode = (routing_mode or "good").strip().lower() or "good"
        with self._lock:
            self._rounds.append(
                ProbeRound(
                    ts=now,
                    hostname=hostname,
                    samples=samples,
                    routing_mode=mode,
                    resolved_ips=list(resolved_ips or ()),
                    answered_ips=list(answered_ips or ()),
                )
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
                answered = [ip for ip, ok, _ in results if ok] or [ip for ip, _, _ in results]
                self._rounds.append(
                    ProbeRound(
                        ts=t,
                        hostname=hostname,
                        samples=samples,
                        routing_mode=mode,
                        resolved_ips=[ip for ip, _, _ in results],
                        answered_ips=answered,
                    )
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
            "resolved_ips": list(rnd.resolved_ips),
            "answered_ips": list(rnd.answered_ips),
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
            resolved = [str(x) for x in (raw.get("resolved_ips") or [])]
            answered = [str(x) for x in (raw.get("answered_ips") or [])]
            rnd = ProbeRound(
                ts=ts,
                hostname=hostname,
                samples=samples,
                routing_mode=mode,
                resolved_ips=resolved,
                answered_ips=answered,
            )
            return self._ensure_round_fields(rnd)
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _ensure_round_fields(rnd: ProbeRound) -> ProbeRound:
        """旧落盘补齐 resolved_ips / answered_ips（尽力而为，不求完美）。"""
        if not rnd.samples:
            return rnd
        if not rnd.resolved_ips:
            # 旧数据分不清备选：整轮样本都当解析集
            rnd.resolved_ips = [s.ip for s in rnd.samples]
        if not rnd.answered_ips:
            rnd.answered_ips = list(
                picked_ips_for_mode(rnd.samples, rnd.routing_mode)
            )
        return rnd

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
        # 读入即按新格式写回，方便下次与对外一致
        self._dirty = True
        self._save_locked()

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
        hold_seconds: float | None = None,
    ) -> dict[str, Any]:
        del target_buckets, probe_interval  # 旧分桶参数，改由 hold 覆盖
        window_h = self.retention_hours if hours is None else max(0.1, float(hours))
        hold_s = self.hold_seconds if hold_seconds is None else max(30.0, float(hold_seconds))
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
                "hold_seconds": hold_s,
                "slot_count": 0,
                "slot_seconds": None,
                "hosts": {},
                "persist_path": str(self._persist_path),
                "note": "暂无探测历史（已开启落盘，跑几轮探测后会留下近几小时条）",
            }

        # 左边界再往前留出 hold，让第一探也能画出「向前覆盖」
        t1 = max(r.ts for r in rounds)
        t0 = min(r.ts for r in rounds) - hold_s

        # hostname -> list[(ts, state)]
        accel_ev: dict[str, list[tuple[float, str]]] = defaultdict(list)
        service_ev: dict[str, list[tuple[float, str]]] = defaultdict(list)
        ip_ev: dict[str, dict[str, list[tuple[float, str]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        # 最近一轮解析集，用于标注「当前是否仍在 DNS」
        latest_resolved: dict[str, set[str]] = defaultdict(set)
        latest_ts: dict[str, float] = {}

        for rnd in rounds:
            host = rnd.hostname
            if host not in latest_ts or rnd.ts >= latest_ts[host]:
                latest_ts[host] = rnd.ts
                latest_resolved[host] = set(rnd.resolved_ips) if rnd.resolved_ips else {
                    s.ip for s in rnd.samples
                }

            resolved_set = set(rnd.resolved_ips) if rnd.resolved_ips else None
            ok_n = fail_n = 0
            for s in rnd.samples:
                ip_ev[host][s.ip].append((rnd.ts, "ok" if s.ok else "bad"))
                if resolved_set is not None and s.ip not in resolved_set:
                    continue
                if s.ok:
                    ok_n += 1
                else:
                    fail_n += 1
            if ok_n or fail_n:
                if fail_n == 0:
                    service_ev[host].append((rnd.ts, "ok"))
                elif ok_n == 0:
                    service_ev[host].append((rnd.ts, "bad"))
                else:
                    service_ev[host].append((rnd.ts, "mixed"))

            # 加速效果：每轮只可能 ok 或 bad（有可用下发=绿，否则红），禁止 mixed
            picked = list(rnd.answered_ips) if rnd.answered_ips else list(
                picked_ips_for_mode(rnd.samples, rnd.routing_mode)
            )
            by_ip = {s.ip: s for s in rnd.samples}
            if picked and all((by_ip[ip].ok if ip in by_ip else False) for ip in picked):
                accel_ev[host].append((rnd.ts, "ok"))
            else:
                accel_ev[host].append((rnd.ts, "bad"))

        hosts_out: dict[str, Any] = {}
        for hostname in sorted(set(r.hostname for r in rounds)):
            resolved_now = latest_resolved.get(hostname) or set()
            ips = sorted(ip_ev[hostname].keys())
            # 加速效果事件只写 ok|bad；若条上出现 mixed，是上游数据/逻辑 bug，原样露出以便追查
            accel_segs = hold_segments(
                accel_ev[hostname], t0=t0, t1=t1, hold_s=hold_s
            )
            hosts_out[hostname] = {
                "accelerated": {
                    "label": "加速效果（按当时选路挑中的 IP 上色）",
                    "slot_segments": accel_segs,
                },
                "service": {
                    "label": "服务可达性（当时 DNS 解析集）",
                    "slot_segments": hold_segments(
                        service_ev[hostname], t0=t0, t1=t1, hold_s=hold_s
                    ),
                },
                "ips": [
                    {
                        "ip": ip,
                        "label": f"IP {ip}",
                        "in_dns": ip in resolved_now,
                        "role": "dns" if ip in resolved_now else "candidate",
                        "slot_segments": hold_segments(
                            ip_ev[hostname][ip], t0=t0, t1=t1, hold_s=hold_s
                        ),
                    }
                    for ip in ips
                ],
            }

        return {
            "hours": window_h,
            "retention_hours": self.retention_hours,
            "hold_seconds": hold_s,
            "slot_count": None,
            "slot_seconds": hold_s,  # 兼容旧前端字段：表示向前覆盖秒数
            "from": _utc_iso(t0),
            "to": _utc_iso(t1),
            "round_count": len(rounds),
            "persist_path": str(self._persist_path),
            "hosts": hosts_out,
        }

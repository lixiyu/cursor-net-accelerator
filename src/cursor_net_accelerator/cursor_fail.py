"""Read Cursor Agent Host local logs for transport failures.

Cursor logs currently do NOT include peer IP. We surface failures for
correlation with our IP pool / peer samples by timestamp.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .observe import EventStore
from .paths import happy_eyeballs_gate_path, state_dir

DIAG_TAG = "[AGENT_ERROR_DIAGNOSTICS]"
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)")
KILL_SWITCH_RE = re.compile(
    r"\[KillSwitchGate\]\s+(\S+)\s+is now\s+(on|off)\b",
    re.I,
)
SINGLE_ADDRESS_GATE = "agent_host_single_address_dial"


def default_logs_root() -> Path:
    env = os.environ.get("CURSOR_NET_ACCEL_CURSOR_LOGS")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library/Application Support/Cursor/logs"


def default_state_dir() -> Path:
    return state_dir()


def gate_store_path() -> Path:
    return happy_eyeballs_gate_path()


def _parse_gate_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    raw = str(ts).strip().replace("T", " ")[:19]
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def load_happy_eyeballs_gate() -> dict[str, Any] | None:
    path = gate_store_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("state") else None


def save_happy_eyeballs_gate(record: dict[str, Any]) -> None:
    path = gate_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": record.get("name") or SINGLE_ADDRESS_GATE,
        "state": record.get("state"),
        "enabled": bool(record.get("enabled")),
        "ts": record.get("ts"),
        "source": record.get("source") or "cursor_log",
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def merge_happy_eyeballs_gate(from_log: dict[str, Any] | None) -> dict[str, Any] | None:
    """Prefer newer log hit; if Cursor logs are gone, fall back to our cache."""
    cached = load_happy_eyeballs_gate()
    if from_log is None:
        if not cached:
            return None
        out = dict(cached)
        out["source"] = "cache"
        return out

    log_ts = _parse_gate_ts(from_log.get("ts"))
    cache_ts = _parse_gate_ts((cached or {}).get("ts"))
    if cached is None or log_ts is None or cache_ts is None or log_ts >= cache_ts:
        record = {
            "name": from_log.get("name") or SINGLE_ADDRESS_GATE,
            "state": from_log.get("state"),
            "enabled": bool(from_log.get("enabled")),
            "ts": from_log.get("ts"),
            "source": "cursor_log",
        }
        try:
            save_happy_eyeballs_gate(record)
        except OSError:
            pass
        return record

    out = dict(cached)
    out["source"] = "cache"
    return out


def _parse_ts(line: str) -> datetime | None:
    m = TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return None


def _load_diag(line: str) -> dict[str, Any] | None:
    j = line.find("{")
    if j < 0:
        return None
    try:
        data = json.loads(line[j:])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def scan_recent_failures(
    logs_root: Path | None = None,
    *,
    hours: float = 24.0,
    limit: int = 100,
) -> dict[str, Any]:
    root = logs_root or default_logs_root()
    cutoff = datetime.now() - timedelta(hours=hours)
    files = sorted(root.rglob("*Agent Host*.log")) if root.exists() else []
    events: list[dict[str, Any]] = []
    single_dial: dict[str, Any] | None = None
    latest_gate_ts: datetime | None = None

    for path in files:
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue

        for line in lines:
            if "[KillSwitchGate]" in line:
                km = KILL_SWITCH_RE.search(line)
                if km and km.group(1) == SINGLE_ADDRESS_GATE:
                    ts_gate = _parse_ts(line)
                    if ts_gate and (latest_gate_ts is None or ts_gate > latest_gate_ts):
                        latest_gate_ts = ts_gate
                        state = km.group(2).lower()
                        single_dial = {
                            "name": SINGLE_ADDRESS_GATE,
                            "state": state,
                            "enabled": state == "on",
                            "ts": ts_gate.strftime("%Y-%m-%d %H:%M:%S"),
                        }

            if DIAG_TAG not in line:
                continue
            ts = _parse_ts(line)
            if ts is None or ts < cutoff:
                continue
            data = _load_diag(line)
            if not data:
                continue
            abort_origin = data.get("abortOrigin")
            error_name = data.get("errorName")
            if abort_origin == "owner" or error_name == "CancelledError":
                continue
            events.append(
                {
                    "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "request_id": data.get("requestId"),
                    "transport_failure_kind": data.get("transportFailureKind"),
                    "transport_mode": data.get("transportMode"),
                    "error_name": error_name,
                    "abort_origin": abort_origin,
                    "connect_code": (
                        (data.get("connectCause") or {}).get("code")
                        if isinstance(data.get("connectCause"), dict)
                        else None
                    ),
                    "file": str(path.name),
                    "note": "Cursor 日志不含对端 IP；请对照同时段的 peer / DNS / 探测",
                }
            )

    events.sort(key=lambda e: e["ts"], reverse=True)
    gate = merge_happy_eyeballs_gate(single_dial)
    return {
        "logs_root": str(root),
        "files_scanned": len(files),
        "hours": hours,
        "count": len(events),
        "events": events[:limit],
        "single_address_dial": gate,
        "happy_eyeballs_gate_file": str(gate_store_path()),
        "limits": [
            "Cursor Agent Host 日志目前不记录对端 IP",
            "失败事件需与本机 peer 采样、DNS 应答、探测结果按时间对照",
            "Happy Eyeballs 开关会写入本机缓存，Cursor 日志被清后仍可读",
        ],
    }


class CursorFailWatcher:
    """Poll Cursor logs and emit new failure events into EventStore."""

    def __init__(
        self,
        store: EventStore,
        *,
        interval: float,
        stop_event,
        hours: float = 6.0,
    ) -> None:
        self.store = store
        self.interval = interval
        self._stop = stop_event
        self.hours = hours
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self.last_scan: dict[str, Any] | None = None

    def run_once(self) -> dict[str, Any]:
        result = scan_recent_failures(hours=self.hours, limit=200)
        with self._lock:
            self.last_scan = result
            for ev in reversed(result["events"]):
                key = f"{ev.get('ts')}|{ev.get('request_id')}|{ev.get('error_name')}"
                if key in self._seen:
                    continue
                self._seen.add(key)
                self.store.record_cursor_failure(ev)
            # bound memory
            if len(self._seen) > 2000:
                self._seen = set(list(self._seen)[-1000:])
        return result

    def loop(self) -> None:
        self.run_once()
        while not self._stop.wait(self.interval):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001
                pass

from __future__ import annotations

import logging
import re
import subprocess
from typing import Callable

from .observe import EventStore

log = logging.getLogger(__name__)

# lsof -nP line: Cursor  12345 user  12u  IPv4 0x..  0t0  TCP 10.0.0.1:54321->34.1.2.3:443 (ESTABLISHED)
_LSOF_RE = re.compile(
    r"^(?P<proc>\S+)\s+(?P<pid>\d+)\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+"
    r"TCP\s+\S+->(?P<ip>[0-9.]+):(?P<port>\d+)\s+\((?P<state>[^)]+)\)"
)


def sample_cursor_peers() -> list[dict[str, str]]:
    """Return established TCP peers of Cursor-related processes (best-effort)."""
    try:
        out = subprocess.check_output(
            ["lsof", "-nP", "-iTCP:443", "-sTCP:ESTABLISHED"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        log.debug("lsof failed: %s", exc)
        return []

    peers: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in out.splitlines()[1:]:
        if not re.search(r"(?i)cursor", line):
            continue
        m = _LSOF_RE.match(line)
        if not m:
            continue
        if m.group("state") != "ESTABLISHED":
            continue
        ip = m.group("ip")
        proc = m.group("proc")
        key = (proc, ip)
        if key in seen:
            continue
        seen.add(key)
        peers.append({"process": proc, "ip": ip, "raw": line.strip()})
    return peers


class PeerWatcher:
    """Periodically sample Cursor→:443 peers and record by IP."""

    def __init__(
        self,
        store: EventStore,
        *,
        interval: float,
        known_ips: Callable[[], set[str]],
        ip_to_host: Callable[[str], str | None],
        stop_event,
    ) -> None:
        self.store = store
        self.interval = interval
        self.known_ips = known_ips
        self.ip_to_host = ip_to_host
        self._stop = stop_event
        self._last_keys: set[tuple[str, str]] = set()

    def run_once(self) -> None:
        peers = sample_cursor_peers()
        known = self.known_ips()
        current: set[tuple[str, str]] = set()
        for p in peers:
            ip = p["ip"]
            # Prefer IPs we know from pool; still record unknown Cursor :443 peers lightly
            if known and ip not in known:
                # skip unrelated Cursor HTTPS (analytics etc.) when we have a known set
                continue
            key = (p["process"], ip)
            current.add(key)
            if key in self._last_keys:
                continue
            self.store.record_peer(
                ip=ip,
                hostname_guess=self.ip_to_host(ip),
                process=p["process"],
                raw=p["raw"],
            )
        self._last_keys = current

    def loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                log.debug("peer watch error: %s", exc)

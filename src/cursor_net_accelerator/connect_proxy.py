from __future__ import annotations

import logging
import select
import socket
import threading
import time
from typing import Callable

from .pool import IpPool

log = logging.getLogger(__name__)

ConnectHook = Callable[[str], None]


class ConnectProxy:
    """
    Minimal HTTP CONNECT proxy for whitelist hosts.
    On CONNECT host:443, race healthy IPs (Happy Eyeballs-ish) and tunnel bytes.
    TLS is end-to-end; we do not decrypt.
    """

    def __init__(
        self,
        *,
        pool: IpPool,
        hosts: tuple[str, ...],
        bind: str,
        port: int,
        he_parallel: int,
        he_timeout: float,
        on_connect: ConnectHook | None = None,
    ) -> None:
        self.pool = pool
        self.hosts = {h.lower() for h in hosts}
        self.bind = bind
        self.port = port
        self.he_parallel = max(1, he_parallel)
        self.he_timeout = he_timeout
        self.on_connect = on_connect
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind, self.port))
        sock.listen(128)
        sock.settimeout(1.0)
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop, name="connect-proxy", daemon=True)
        self._thread.start()
        log.info("CONNECT proxy on %s:%s", self.bind, self.port)

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                client, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                raise
            t = threading.Thread(
                target=self._handle_client,
                args=(client, addr),
                name="connect-client",
                daemon=True,
            )
            t.start()

    def _handle_client(self, client: socket.socket, addr: tuple) -> None:
        client.settimeout(10)
        try:
            req = _recv_headers(client)
            if not req:
                client.close()
                return
            first = req.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
            parts = first.split()
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
                client.close()
                return
            target = parts[1]
            if ":" in target:
                host, port_s = target.rsplit(":", 1)
                port = int(port_s)
            else:
                host, port = target, 443
            host_l = host.lower()
            if host_l not in self.hosts:
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                client.close()
                return
            if port != 443:
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                client.close()
                return

            if self.on_connect is not None:
                try:
                    self.on_connect(host_l)
                except Exception:  # noqa: BLE001
                    pass

            upstream = self._dial_he(host_l)
            if upstream is None:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                client.close()
                return

            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            _relay(client, upstream)
        except Exception as exc:  # noqa: BLE001
            log.debug("client %s error: %s", addr, exc)
            try:
                client.close()
            except OSError:
                pass

    def _dial_he(self, hostname: str) -> socket.socket | None:
        ips = self.pool.healthy_ips(hostname)
        if not ips:
            snap = self.pool.snapshot().get(hostname) or {}
            ips = list(snap.get("resolved") or [])
        if not ips:
            return None

        candidates = ips[: self.he_parallel]
        winner: socket.socket | None = None
        winner_ip: str | None = None
        lock = threading.Lock()
        done = threading.Event()

        def try_one(ip: str) -> None:
            nonlocal winner, winner_ip
            if done.is_set():
                return
            try:
                s = socket.create_connection((ip, 443), timeout=self.he_timeout)
            except OSError:
                return
            with lock:
                if winner is None and not done.is_set():
                    winner = s
                    winner_ip = ip
                    done.set()
                else:
                    try:
                        s.close()
                    except OSError:
                        pass

        threads = [threading.Thread(target=try_one, args=(ip,), daemon=True) for ip in candidates]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        # staggered start would be more HE-like; v1: near-parallel is fine
        done.wait(timeout=self.he_timeout + 0.5)
        for t in threads:
            t.join(timeout=0.1)

        if winner is not None:
            log.info(
                "HE %s → %s (%.0fms, tried %s)",
                hostname,
                winner_ip,
                (time.perf_counter() - t0) * 1000,
                candidates,
            )
        else:
            log.warning("HE %s failed candidates=%s", hostname, candidates)
        return winner


def _recv_headers(sock: socket.socket, limit: int = 65536) -> bytes:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            break
    return buf


def _relay(a: socket.socket, b: socket.socket) -> None:
    a.setblocking(False)
    b.setblocking(False)
    sockets = [a, b]
    try:
        while True:
            r, _, x = select.select(sockets, [], sockets, 60)
            if x:
                break
            if not r:
                break
            for s in r:
                other = b if s is a else a
                try:
                    data = s.recv(65536)
                except OSError:
                    data = b""
                if not data:
                    return
                try:
                    other.sendall(data)
                except OSError:
                    return
    finally:
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass


def generate_pac(hosts: tuple[str, ...], proxy_host: str, proxy_port: int) -> str:
    conditions = " || ".join(f'host == "{h}"' for h in hosts)
    return f"""// Generated by cursor-net-accelerator — whitelist only
function FindProxyForURL(url, host) {{
  host = host.toLowerCase();
  if ({conditions}) {{
    return "PROXY {proxy_host}:{proxy_port}";
  }}
  return "DIRECT";
}}
"""

from __future__ import annotations

import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from dnslib import DNSHeader, DNSQuestion, DNSRecord, QTYPE

from .config import Settings


@dataclass(frozen=True)
class IpProbeResult:
    ip: str
    ok: bool
    connect_ms: float | None
    error: str | None


def resolve_ips_upstream(hostname: str, upstream_dns: tuple[str, ...]) -> list[str]:
    """Resolve A records via upstream DNS (avoid circular resolve via our local DNS)."""
    if not upstream_dns:
        raise RuntimeError("no upstream DNS configured")
    last_err: Exception | None = None
    for server in upstream_dns:
        try:
            return _dns_query_a(hostname, server)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    assert last_err is not None
    raise last_err


def _dns_query_a(hostname: str, server: str, timeout: float = 2.0) -> list[str]:
    q = DNSRecord(DNSHeader(rd=1), q=DNSQuestion(hostname, QTYPE.A))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(q.pack(), (server, 53))
        data, _ = sock.recvfrom(4096)
    finally:
        sock.close()
    resp = DNSRecord.parse(data)
    ips: list[str] = []
    seen: set[str] = set()
    for rr in resp.rr:
        if rr.rtype == QTYPE.A:
            ip = str(rr.rdata)
            if ip not in seen:
                seen.add(ip)
                ips.append(ip)
    if not ips:
        raise RuntimeError(f"no A records for {hostname} from {server}")
    return ips


def probe_ip(ip: str, *, sni: str, timeout: float, do_tls: bool = True) -> IpProbeResult:
    """TCP :443 success = ok; optional short TLS+SNI (failure noted but TCP ok still counts)."""
    connect_ms: float | None = None
    t0 = time.perf_counter()
    try:
        raw = socket.create_connection((ip, 443), timeout=timeout)
        connect_ms = (time.perf_counter() - t0) * 1000
        tls_err: str | None = None
        if do_tls:
            tls_err = _optional_tls(raw, sni, timeout)
        else:
            raw.close()
        return IpProbeResult(ip=ip, ok=True, connect_ms=connect_ms, error=tls_err)
    except Exception as exc:  # noqa: BLE001
        return IpProbeResult(
            ip=ip,
            ok=False,
            connect_ms=connect_ms,
            error=f"{type(exc).__name__}: {exc}",
        )


def _optional_tls(raw: socket.socket, sni: str, timeout: float) -> str | None:
    try:
        raw.settimeout(timeout)
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(raw, server_hostname=sni) as sock:
            sock.do_handshake()
        return None
    except Exception as exc:  # noqa: BLE001
        try:
            raw.close()
        except OSError:
            pass
        return f"tls:{type(exc).__name__}: {exc}"


def probe_host(hostname: str, settings: Settings) -> tuple[list[str], list[IpProbeResult]]:
    resolved = resolve_ips_upstream(hostname, settings.upstream_dns)
    results_by_ip: dict[str, IpProbeResult] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(resolved))) as ex:
        futs = {
            ex.submit(probe_ip, ip, sni=hostname, timeout=settings.probe_timeout): ip
            for ip in resolved
        }
        for fut in as_completed(futs):
            r = fut.result()
            results_by_ip[r.ip] = r
    # keep stable order matching resolution order
    results = [results_by_ip[ip] for ip in resolved if ip in results_by_ip]
    return resolved, results

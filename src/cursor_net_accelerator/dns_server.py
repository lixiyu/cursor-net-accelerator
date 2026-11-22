from __future__ import annotations

import logging
import socket
import socketserver
import threading
import time
from typing import Any, Callable

from dnslib import A, DNSRecord, QTYPE, RR, SOA
from dnslib.server import BaseResolver, DNSLogger, DNSServer

from .pool import IpPool

log = logging.getLogger(__name__)

# hostname, ips, client, meta
DnsAnswerHook = Callable[[str, list[str], str | None, dict[str, Any]], None]
ModeGetter = Callable[[], str]

# A 记录的 TTL（秒）。短 TTL 让客户端跟随探测结果滚动换 IP。
DEFAULT_ANSWER_TTL = 15

# 空应答的负缓存 TTL（秒）。必须很小：拉闸/池空时我们回 NODATA，
# 若不带 SOA，macOS 的 mDNSResponder 会把“该域名没有地址”当结论长期缓存，
# 切回 good 后系统不再重新查询，表现为“加速救不回来”。
DEFAULT_NEGATIVE_TTL = 1

# 上游转发：单次超时 / 缓存时长 / 并发上限。
# dnslib 的 UDP 服务是「一个请求一个线程」，上游一旦变慢，线程会按请求量
# 线性堆积 —— 这正是「拉闸后机器像被拖住」的机制。所以这里三重设限：
#   1) 单次上游超时足够短，尽快失败
#   2) 失败结果也短暂缓存，避免同一域名被反复打上游
#   3) 并发转发用信号量封顶，超出的直接 SERVFAIL，宁可快速失败也不堆积
FORWARD_TIMEOUT = 0.8
FORWARD_CACHE_TTL = 15.0
FORWARD_FAIL_CACHE_TTL = 2.0
FORWARD_MAX_CONCURRENCY = 8
FORWARD_ACQUIRE_TIMEOUT = 0.25
# 单飞开关：同一 (域名,类型) 的并发查询合并成一次上游请求。
# 关掉只用于对照实验，正常运行不要关。
FORWARD_COALESCE = True


def _under_domain(qname: str, domain: str) -> bool:
    q = (qname or "").lower().rstrip(".")
    d = (domain or "").lower().rstrip(".")
    if not q or not d:
        return False
    return q == d or q.endswith("." + d)


class ReusableUDPServer(socketserver.ThreadingMixIn, socketserver.UDPServer):
    """UDP DNS 服务，带 SO_REUSEPORT，允许新旧进程短暂共存。

    重启加速器时如果先停旧的再起新的，会有 1~2 秒空窗；这期间 macOS 的
    mDNSResponder 会把「解析器不可达」记下来并对相关域名长期缓存，表现为
    Cursor 长时间解析失败（重启前还好，重启后反而更糟）。
    开了 SO_REUSEPORT 就能「先起新的、再退旧的」，DNS 零空窗。
    """

    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self) -> None:  # noqa: D102
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:  # 老系统没有 SO_REUSEPORT，退化为普通绑定
            pass
        super().server_bind()


def _soa_rr(domain: str, ttl: int) -> RR:
    """构造一个 SOA 记录，minimum 决定客户端对“无记录”的缓存时长。"""
    d = (domain or "local").strip().rstrip(".") or "local"
    return RR(
        f"{d}.",
        QTYPE.SOA,
        rdata=SOA(
            mname=f"{d}.",
            rname=f"admin.{d}.",
            times=(0, 1, 0, 0, ttl),
        ),
        ttl=ttl,
    )


def _nodata(reply: DNSRecord, domain: str, ttl: int) -> DNSRecord:
    """NODATA（NOERROR + 0 条记录），但带 SOA 把负缓存压到 ttl 秒。"""
    reply.header.rcode = 0
    reply.header.aa = 1
    reply.add_auth(_soa_rr(domain, ttl))
    return reply


def _servfail(reply: DNSRecord, domain: str, ttl: int) -> DNSRecord:
    """上游失败：SERVFAIL + SOA，避免留下长期缓存。"""
    reply.header.rcode = 2
    reply.add_auth(_soa_rr(domain, ttl))
    return reply


class PoolResolver(BaseResolver):
    """Answer whitelist hosts from IpPool according to routing mode."""

    def __init__(
        self,
        pool: IpPool,
        hosts: tuple[str, ...],
        upstream_dns: tuple[str, ...],
        get_mode: ModeGetter,
        on_answer: DnsAnswerHook | None = None,
        resolver_domain: str = "cursor.sh",
        answer_ttl: int = DEFAULT_ANSWER_TTL,
        negative_ttl: int = DEFAULT_NEGATIVE_TTL,
    ) -> None:
        self.pool = pool
        self.hosts = {h.lower() for h in hosts}
        self.upstream_dns = upstream_dns
        self.get_mode = get_mode
        self.on_answer = on_answer
        self.resolver_domain = resolver_domain
        self.answer_ttl = answer_ttl
        self.negative_ttl = negative_ttl

        # 转发缓存：(qname, qtype) -> (过期时间, 应答字节 or None 表示失败)
        self._cache: dict[tuple[str, int], tuple[float, bytes | None]] = {}
        self._cache_lock = threading.Lock()
        self._forward_slots = threading.BoundedSemaphore(FORWARD_MAX_CONCURRENCY)
        # 单飞：同一个 (qname, qtype) 同时只允许一条上游查询
        self._inflight: dict[tuple[str, int], threading.Event] = {}
        self._inflight_lock = threading.Lock()

        # 轻量统计：用来判断「到底是不是被查询风暴拖住」
        self._stats_lock = threading.Lock()
        self._stats: dict[str, int] = {
            "queries": 0,
            "zone_queries": 0,
            "forwarded": 0,
            "cache_hit": 0,
            "coalesced": 0,
            "shed": 0,
            "upstream_fail": 0,
            "slowest_ms": 0,
        }
        self._started_at = time.monotonic()
        self._last_warn_at: dict[str, float] = {}

    def _warn_rate_limited(self, key: str, msg: str, *args: Any, every: float = 5.0) -> None:
        """风暴时每个失败都打日志，日志本身就成了负担——这里按 key 限速。"""
        now = time.monotonic()
        last = self._last_warn_at.get(key, 0.0)
        if now - last >= every:
            self._last_warn_at[key] = now
            log.warning(msg, *args)
        else:
            log.debug(msg, *args)

    def _bump(self, key: str, n: int = 1) -> None:
        with self._stats_lock:
            self._stats[key] = self._stats.get(key, 0) + n

    def _note_latency(self, elapsed_ms: float) -> None:
        with self._stats_lock:
            if elapsed_ms > self._stats.get("slowest_ms", 0):
                self._stats["slowest_ms"] = int(elapsed_ms)

    def stats(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._stats_lock:
            snap = dict(self._stats)
        up = max(now - self._started_at, 1.0)
        snap["uptime_s"] = round(up, 1)
        snap["queries_per_s"] = round(snap["queries"] / up, 2)
        snap["cache_entries"] = len(self._cache)
        return snap

    def _emit(
        self,
        qname: str,
        ips: list[str],
        handler,  # noqa: ANN001
        meta: dict[str, Any],
    ) -> None:
        if self.on_answer is None:
            return
        try:
            self.on_answer(qname, list(ips), _client(handler), meta)
        except Exception as exc:  # noqa: BLE001
            log.debug("on_answer hook failed: %s", exc)

    def resolve(self, request: DNSRecord, handler) -> DNSRecord:  # noqa: ANN001
        t0 = time.perf_counter()
        self._bump("queries")
        try:
            return self._resolve_inner(request, handler)
        finally:
            self._note_latency((time.perf_counter() - t0) * 1000.0)

    def _resolve_inner(self, request: DNSRecord, handler) -> DNSRecord:  # noqa: ANN001
        reply = request.reply()
        qname = str(request.q.qname).rstrip(".").lower()
        qtype = request.q.qtype
        mode = self.get_mode()
        in_zone = _under_domain(qname, self.resolver_domain)
        if in_zone:
            self._bump("zone_queries")

        # 拉闸：整个分流域（含 api2geo/api2direct/api3…）一律不给地址，也不转发上游。
        # 空应答必须带 SOA(minimum=negative_ttl)，否则 macOS 会把“查不到”当结论
        # 长期缓存，切回 good/off 后系统不再重新查询 —— 就是“拉闸后救不回来”的根因。
        if mode == "bad" and in_zone:
            if qtype in (QTYPE.A, QTYPE.AAAA, QTYPE.ANY):
                meta = {
                    "mode": mode,
                    "note": "拉闸测试：不返回任何 IP",
                    "degraded": False,
                    "bad_in_answer": [],
                }
                self._emit(qname, [], handler, meta)
                log.debug("DNS mode=bad %s → [] (nodata, neg_ttl=%s)", qname, self.negative_ttl)
            return _nodata(reply, self.resolver_domain, self.negative_ttl)

        if qname not in self.hosts:
            return self._forward(request)

        # 白名单域名只回答 A；其它类型（含 AAAA）回短负缓存，避免 IPv6 绕过选路
        if qtype in (QTYPE.AAAA, QTYPE.ANY):
            return _nodata(reply, self.resolver_domain, self.negative_ttl)
        if qtype != QTYPE.A:
            return reply

        ips, meta = self.pool.select_for_mode(qname, mode)

        if not ips:
            # 冷启动 / 还没探测过这个域名：不要急着说“没有地址”。
            # NODATA 会被系统当成权威结论缓存住，等探测出结果也救不回来。
            # 此时按上游原样回答，等首轮探测完成后再交给选路。
            if mode != "bad" and not self.pool.resolved_ips(qname):
                log.debug("DNS cold-start %s → forward upstream", qname)
                return self._forward(request)
            # 池子空 / 全部探测失败：NODATA + 短负缓存，等下一轮探测自愈
            self._emit(qname, [], handler, meta)
            return _nodata(reply, self.resolver_domain, self.negative_ttl)

        for ip in ips:
            reply.add_answer(RR(qname, QTYPE.A, rdata=A(ip), ttl=self.answer_ttl))
        self._emit(qname, list(ips), handler, meta)
        log.debug("DNS mode=%s %s → %s (%s)", mode, qname, ips, meta.get("note"))
        return reply

    def _forward(self, request: DNSRecord) -> DNSRecord:
        """Proxy non-whitelist queries to upstream.

        四重保护，避免上游变慢时线程按请求量堆积（拉闸后 Cursor 高频重试
        配合一个慢上游，会把本机 DNS 拖成雪崩）：
          1) 成功/失败都进短缓存，同一域名不再重复打上游
          2) 单飞：同一个 (域名, 类型) 正在查时，后面的请求等它的结果，
             而不是各自再开一条上游连接
          3) 单次上游超时压到 FORWARD_TIMEOUT
          4) 并发转发用信号量封顶，真挤满了才 SERVFAIL（宁可快速失败也不堆积）
        """
        key = (str(request.q.qname).rstrip(".").lower(), request.q.qtype)

        hit = self._cache_get(key, request)
        if hit is not None:
            return hit

        # --- 单飞：同一个 key 只允许一个 leader 出去查 ---
        waiter: threading.Event | None = None
        if FORWARD_COALESCE:
            with self._inflight_lock:
                waiter = self._inflight.get(key)
                leader = waiter is None
                if leader:
                    waiter = threading.Event()
                    self._inflight[key] = waiter
            if not leader:
                self._bump("coalesced")
                wait_s = FORWARD_TIMEOUT * max(1, len(self.upstream_dns)) + 0.5
                if waiter.wait(wait_s):
                    hit = self._cache_get(key, request)
                    if hit is not None:
                        return hit
                return _servfail(request.reply(), self.resolver_domain, self.negative_ttl)

        try:
            if not self._forward_slots.acquire(timeout=FORWARD_ACQUIRE_TIMEOUT):
                # 槽位耗尽 = 上游正在变慢。快速失败好过让线程堆积。
                self._bump("shed")
                self._warn_rate_limited("shed", "forward shed (too many in flight): %s", key[0])
                self._cache_put(key, None, 0.5)  # 让同一 key 的等待者立刻拿到结果
                return _servfail(request.reply(), self.resolver_domain, self.negative_ttl)

            try:
                self._bump("forwarded")
                last_err: Exception | None = None
                for server in self.upstream_dns:
                    try:
                        raw = request.send(server, 53, timeout=FORWARD_TIMEOUT)
                        if not isinstance(raw, (bytes, bytearray)):
                            raw = raw.pack()
                        self._cache_put(key, bytes(raw), FORWARD_CACHE_TTL)
                        return DNSRecord.parse(raw)
                    except Exception as exc:  # noqa: BLE001
                        last_err = exc
                self._warn_rate_limited("upstream", "upstream forward failed: %s", last_err)
                self._bump("upstream_fail")
                self._cache_put(key, None, FORWARD_FAIL_CACHE_TTL)
                return _servfail(request.reply(), self.resolver_domain, self.negative_ttl)
            finally:
                self._forward_slots.release()
        finally:
            if waiter is not None:
                with self._inflight_lock:
                    self._inflight.pop(key, None)
                waiter.set()

    def _cache_get(self, key: tuple[str, int], request: DNSRecord) -> DNSRecord | None:
        """命中的缓存应答（失败结果 -> SERVFAIL）；未命中返回 None。"""
        now = time.monotonic()
        with self._cache_lock:
            ent = self._cache.get(key)
            if not ent or ent[0] <= now:
                return None
            cached = ent[1]
        self._bump("cache_hit")
        if cached is None:
            return _servfail(request.reply(), self.resolver_domain, self.negative_ttl)
        reply = DNSRecord.parse(cached)
        reply.header.id = request.header.id
        return reply

    def _cache_put(self, key: tuple[str, int], payload: bytes | None, ttl: float) -> None:
        now = time.monotonic()
        with self._cache_lock:
            if len(self._cache) > 1024:
                # 简单清理：丢掉已过期的条目，避免无限增长
                for k in [k for k, (exp, _) in self._cache.items() if exp <= now]:
                    self._cache.pop(k, None)
                if len(self._cache) > 1024:
                    self._cache.clear()
            self._cache[key] = (now + ttl, payload)


def _client(handler) -> str | None:  # noqa: ANN001
    try:
        return f"{handler.client_address[0]}:{handler.client_address[1]}"
    except Exception:  # noqa: BLE001
        return None


class DnsService:
    def __init__(
        self,
        *,
        pool: IpPool,
        hosts: tuple[str, ...],
        upstream_dns: tuple[str, ...],
        bind: str,
        port: int,
        get_mode: ModeGetter,
        on_answer: DnsAnswerHook | None = None,
        resolver_domain: str = "cursor.sh",
        answer_ttl: int = DEFAULT_ANSWER_TTL,
        negative_ttl: int = DEFAULT_NEGATIVE_TTL,
    ) -> None:
        self.pool = pool
        self.hosts = hosts
        self.upstream_dns = upstream_dns
        self.bind = bind
        self.port = port
        self.get_mode = get_mode
        self.on_answer = on_answer
        self.resolver_domain = resolver_domain
        self.answer_ttl = answer_ttl
        self.negative_ttl = negative_ttl
        self._server: DNSServer | None = None
        self._resolver: PoolResolver | None = None

    def start(self) -> None:
        resolver = PoolResolver(
            self.pool,
            self.hosts,
            self.upstream_dns,
            get_mode=self.get_mode,
            on_answer=self.on_answer,
            resolver_domain=self.resolver_domain,
            answer_ttl=self.answer_ttl,
            negative_ttl=self.negative_ttl,
        )
        self._resolver = resolver
        self._server = DNSServer(
            resolver,
            port=self.port,
            address=self.bind,
            logger=DNSLogger(
                prefix=False,
                log="-request,-reply,-truncated,-error,-data",
            ),
            server=ReusableUDPServer,
        )
        self._server.start_thread()
        log.info("DNS listening on %s:%s (hosts=%s)", self.bind, self.port, ",".join(self.hosts))

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None

    def stats(self) -> dict[str, Any]:
        """运行期查询统计：看是否出现查询风暴 / 转发堆积。"""
        if self._resolver is None:
            return {}
        return self._resolver.stats()

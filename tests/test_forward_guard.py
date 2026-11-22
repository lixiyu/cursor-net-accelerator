"""上游转发的防护：缓存、失败缓存、并发封顶。

这三条是为了避免「拉闸后 Cursor 高频重试 + 上游变慢」把本机 DNS 拖成雪崩：
dnslib 的 UDP 服务一个请求一个线程，没有上限的话线程会跟着请求量线性堆积。

跑法：.venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import threading
import time
import unittest

from dnslib import A, DNSRecord, QTYPE, RR

import cursor_net_accelerator.dns_server as ds
from cursor_net_accelerator.pool import IpPool


def make_resolver(upstream=("1.1.1.1",), *, hosts=(), pool=None):
    pool = pool or IpPool(quarantine_seconds=60.0, pool_ttl_seconds=60.0)
    return ds.PoolResolver(
        pool, hosts, upstream, get_mode=lambda: "good", resolver_domain="cursor.sh"
    )


class SendStub:
    """替代 DNSRecord.send 的假上游。

    必须以「函数」形式装到类上：把可调用对象直接赋给类属性不会绑定 self。
    """

    def __init__(
        self,
        *,
        answer: str | None = "1.2.3.4",
        delay: float = 0.0,
        gate: threading.Event | None = None,
    ):
        self.calls = 0
        self.answer = answer
        self.delay = delay
        self.gate = gate

    def install(self) -> None:
        stub = self

        def _send(request, dest, port, timeout=2.0, **kw):
            stub.calls += 1
            if stub.gate is not None:
                stub.gate.wait(2.0)
            if stub.delay:
                time.sleep(stub.delay)
            if stub.answer is None:
                raise TimeoutError("simulated upstream timeout")
            reply = request.reply()
            reply.add_answer(RR(request.q.qname, QTYPE.A, rdata=A(stub.answer), ttl=30))
            return reply.pack()

        DNSRecord.send = _send


class ForwardGuardTest(unittest.TestCase):
    def setUp(self):
        self._orig_send = DNSRecord.send
        self._orig = (
            ds.FORWARD_TIMEOUT,
            ds.FORWARD_MAX_CONCURRENCY,
            ds.FORWARD_ACQUIRE_TIMEOUT,
            ds.FORWARD_CACHE_TTL,
            ds.FORWARD_FAIL_CACHE_TTL,
        )

    def tearDown(self):
        DNSRecord.send = self._orig_send
        (
            ds.FORWARD_TIMEOUT,
            ds.FORWARD_MAX_CONCURRENCY,
            ds.FORWARD_ACQUIRE_TIMEOUT,
            ds.FORWARD_CACHE_TTL,
            ds.FORWARD_FAIL_CACHE_TTL,
        ) = self._orig

    def test_success_is_cached(self):
        stub = SendStub(answer="1.2.3.4")
        stub.install()
        resolver = make_resolver()
        q = DNSRecord.question("api3.cursor.sh", "A")

        r1 = resolver.resolve(q, None)
        r2 = resolver.resolve(q, None)

        self.assertEqual(stub.calls, 1, "同一域名第二次不应再打上游")
        self.assertEqual([str(rr.rdata) for rr in r1.rr], ["1.2.3.4"])
        self.assertEqual([str(rr.rdata) for rr in r2.rr], ["1.2.3.4"])
        self.assertEqual(resolver.stats()["cache_hit"], 1)

    def test_failure_is_cached_briefly(self):
        stub = SendStub(answer=None)
        stub.install()
        resolver = make_resolver(upstream=("1.1.1.1", "8.8.8.8"))
        q = DNSRecord.question("api4.cursor.sh", "A")

        r1 = resolver.resolve(q, None)
        calls_after_first = stub.calls
        r2 = resolver.resolve(q, None)

        self.assertEqual(r1.header.rcode, 2)
        self.assertEqual(r2.header.rcode, 2)
        self.assertEqual(calls_after_first, 2, "两个上游各试一次")
        self.assertEqual(stub.calls, 2, "失败结果也要缓存，别反复打上游")
        self.assertEqual(resolver.stats()["upstream_fail"], 1)

    def test_identical_queries_are_coalesced(self):
        """同一域名的并发查询应合并成一次上游请求（单飞）。"""
        gate = threading.Event()
        stub = SendStub(answer="1.2.3.4", gate=gate)
        stub.install()
        resolver = make_resolver()
        out = []

        def fire():
            out.append(resolver.resolve(DNSRecord.question("api9.cursor.sh", "A"), None))

        threads = [threading.Thread(target=fire) for _ in range(20)]
        for t in threads:
            t.start()
        time.sleep(0.1)
        gate.set()
        for t in threads:
            t.join(timeout=3.0)

        self.assertEqual(stub.calls, 1, "20 个并发同域名查询只应打一次上游")
        self.assertEqual(len(out), 20)
        self.assertTrue(all(r.header.rcode == 0 for r in out))

    def test_sheds_instead_of_piling_up(self):
        ds.FORWARD_MAX_CONCURRENCY = 1
        ds.FORWARD_ACQUIRE_TIMEOUT = 0.01
        gate = threading.Event()
        stub = SendStub(answer="1.2.3.4", gate=gate)
        stub.install()
        resolver = make_resolver()

        q1 = DNSRecord.question("a.cursor.sh", "A")
        q2 = DNSRecord.question("b.cursor.sh", "A")

        t = threading.Thread(target=resolver.resolve, args=(q1, None), daemon=True)
        t.start()
        time.sleep(0.05)  # 让第一个请求占住唯一的槽位

        r2 = resolver.resolve(q2, None)
        gate.set()
        t.join(timeout=2.0)

        self.assertEqual(r2.header.rcode, 2, "拿不到槽位应立刻 SERVFAIL")
        self.assertEqual(resolver.stats()["shed"], 1)
        self.assertFalse(t.is_alive())

    def test_cold_start_forwards_instead_of_nodata(self):
        """还没探测过的域名不能回 NODATA —— 那会被系统当成“权威结论”缓存住。"""
        stub = SendStub(answer="9.9.9.9")
        stub.install()
        resolver = make_resolver(hosts=("api2.cursor.sh",))  # 白名单，但池子是空的
        reply = resolver.resolve(DNSRecord.question("api2.cursor.sh", "A"), None)

        self.assertEqual(reply.header.rcode, 0)
        self.assertEqual([str(rr.rdata) for rr in reply.rr], ["9.9.9.9"])
        self.assertEqual(stub.calls, 1, "冷启动应回源上游")

    def test_stats_shape(self):
        stub = SendStub(answer="1.2.3.4")
        stub.install()
        resolver = make_resolver()
        resolver.resolve(DNSRecord.question("x.cursor.sh", "A"), None)
        st = resolver.stats()
        for key in (
            "queries",
            "zone_queries",
            "forwarded",
            "cache_hit",
            "coalesced",
            "shed",
            "upstream_fail",
            "queries_per_s",
            "cache_entries",
        ):
            self.assertIn(key, st)


if __name__ == "__main__":
    unittest.main()


class ReusePortTest(unittest.TestCase):
    """重启零空窗的前提：新旧进程能同时绑同一个端口。"""

    def test_two_servers_can_share_the_same_port(self):
        import socket
        import socketserver

        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        first = ds.ReusableUDPServer(("127.0.0.1", port), socketserver.BaseRequestHandler)
        try:
            second = ds.ReusableUDPServer(("127.0.0.1", port), socketserver.BaseRequestHandler)
            second.server_close()
        finally:
            first.server_close()

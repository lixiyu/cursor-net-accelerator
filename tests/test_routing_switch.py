"""三态切换（good / off / bad）回归测试。

重点覆盖两个曾经踩过的坑：
1. 空应答必须带 SOA（negative TTL 很小），否则 macOS 把“查不到”长期缓存，
   切回 good 后系统不再重新查询 —— 表现为“拉闸之后加速救不回来”。
2. 切换语义要覆盖整个 resolver_domain，而不只是白名单里那一个域名。

跑法：.venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from dnslib import DNSRecord, QTYPE, SOA

from cursor_net_accelerator.dns_server import (
    DEFAULT_ANSWER_TTL,
    DEFAULT_NEGATIVE_TTL,
    PoolResolver,
    PoolResolver as _PoolResolver,  # noqa: F401  保持导入稳定
    _under_domain,
)
from cursor_net_accelerator.pool import IpPool
from cursor_net_accelerator.probe import IpProbeResult

ZONE = "cursor.sh"
HOST = "api2.cursor.sh"
ALL_IPS = ["34.225.159.174", "52.21.130.202", "3.92.129.183"]
HEALTHY = ["52.21.130.202"]


class ModeSwitch:
    """可变的 mode 提供者，模拟运行时切换。"""

    def __init__(self, mode: str = "good") -> None:
        self.mode = mode

    def __call__(self) -> str:
        return self.mode

    def set(self, mode: str) -> None:
        self.mode = mode


def make_pool(hostname: str = HOST) -> IpPool:
    pool = IpPool(quarantine_seconds=120.0, pool_ttl_seconds=90.0)
    results = [
        IpProbeResult(
            ip=ip,
            ok=ip in HEALTHY,
            connect_ms=120.0 if ip in HEALTHY else None,
            error=None if ip in HEALTHY else "TimeoutError: timed out",
        )
        for ip in ALL_IPS
    ]
    pool.update(hostname, list(ALL_IPS), results)
    return pool


def make_resolver(mode: ModeSwitch, *, hosts=(HOST,), upstream=()) -> PoolResolver:
    return PoolResolver(
        make_pool(),
        hosts,
        upstream,
        get_mode=mode,
        on_answer=None,
        resolver_domain=ZONE,
    )


def ask(resolver: PoolResolver, name: str, qtype: str = "A") -> DNSRecord:
    request = DNSRecord.question(name, qtype)
    return resolver.resolve(request, handler=None)


def a_answers(reply: DNSRecord) -> list[str]:
    return [str(rr.rdata) for rr in reply.rr if rr.rtype == QTYPE.A]


def soa_minimum(reply: DNSRecord) -> int | None:
    for rr in reply.auth:
        if rr.rtype == QTYPE.SOA and isinstance(rr.rdata, SOA):
            return rr.rdata.times[-1]
    return None


class ZoneMatchTest(unittest.TestCase):
    def test_under_domain_covers_subdomains(self) -> None:
        for name in (
            "cursor.sh",
            "api2.cursor.sh",
            "api2direct.cursor.sh",
            "api3.cursor.sh",
            "repo42.cursor.sh",
            "agentn.global.api5.cursor.sh",
            "deep.nested.api5.cursor.sh",
        ):
            self.assertTrue(_under_domain(name, ZONE), name)

    def test_under_domain_rejects_outsiders(self) -> None:
        for name in ("example.com", "notcursor.sh", "cursor.sh.evil.com"):
            self.assertFalse(_under_domain(name, ZONE), name)


class GoodModeTest(unittest.TestCase):
    def test_returns_only_healthy_with_ttl(self) -> None:
        reply = ask(make_resolver(ModeSwitch("good")), HOST)
        self.assertEqual(a_answers(reply), HEALTHY)
        for rr in reply.rr:
            if rr.rtype == QTYPE.A:
                self.assertEqual(rr.ttl, DEFAULT_ANSWER_TTL)


class OffModeTest(unittest.TestCase):
    def test_returns_all_resolved(self) -> None:
        reply = ask(make_resolver(ModeSwitch("off")), HOST)
        self.assertEqual(set(a_answers(reply)), set(ALL_IPS))


class BadModeTest(unittest.TestCase):
    def test_whitelisted_host_gets_nodata_with_soa(self) -> None:
        reply = ask(make_resolver(ModeSwitch("bad")), HOST)
        self.assertEqual(reply.header.rcode, 0)
        self.assertEqual(a_answers(reply), [])
        self.assertEqual(soa_minimum(reply), DEFAULT_NEGATIVE_TTL)

    def test_every_subdomain_in_zone_is_cut(self) -> None:
        """拉闸必须覆盖整个分流域，而不只是白名单里的那一个域名。"""
        resolver = make_resolver(ModeSwitch("bad"))
        for name in (
            "api2.cursor.sh",
            "api2direct.cursor.sh",
            "api3.cursor.sh",
            "api4.cursor.sh",
            "api5.cursor.sh",
            "repo42.cursor.sh",
            "agentn.global.api5.cursor.sh",
        ):
            reply = ask(resolver, name)
            self.assertEqual(a_answers(reply), [], name)
            self.assertEqual(
                soa_minimum(reply), DEFAULT_NEGATIVE_TTL, f"{name} 缺少 SOA 负缓存"
            )

    def test_aaaa_is_nodata_with_soa(self) -> None:
        reply = ask(make_resolver(ModeSwitch("good")), HOST, qtype="AAAA")
        self.assertEqual(a_answers(reply), [])
        self.assertEqual(soa_minimum(reply), DEFAULT_NEGATIVE_TTL)


class SwitchRecoveryTest(unittest.TestCase):
    def test_bad_then_good_recovers_immediately(self) -> None:
        """拉闸 → 开启加速：同一个 resolver 实例必须能立刻恢复应答。"""
        mode = ModeSwitch("good")
        resolver = make_resolver(mode)

        self.assertEqual(a_answers(ask(resolver, HOST)), HEALTHY)

        mode.set("bad")
        cut = ask(resolver, HOST)
        self.assertEqual(a_answers(cut), [])
        # 关键：负缓存 TTL 必须很小，否则系统切回后不会重新问
        self.assertEqual(soa_minimum(cut), DEFAULT_NEGATIVE_TTL)

        mode.set("good")
        back = ask(resolver, HOST)
        self.assertEqual(a_answers(back), HEALTHY)

    def test_off_then_good_keeps_working(self) -> None:
        mode = ModeSwitch("off")
        resolver = make_resolver(mode)
        self.assertEqual(set(a_answers(ask(resolver, HOST))), set(ALL_IPS))
        mode.set("good")
        self.assertEqual(a_answers(ask(resolver, HOST)), HEALTHY)

    def test_all_subdomains_recover_together(self) -> None:
        """三个模式下，域内每个名字的行为要一致可逆。"""
        mode = ModeSwitch("bad")
        resolver = make_resolver(mode)
        names = ["api2.cursor.sh", "api3.cursor.sh", "repo42.cursor.sh"]
        for name in names:
            self.assertEqual(a_answers(ask(resolver, name)), [], name)
        mode.set("good")
        for name in names:
            # 非白名单走转发；上游为空 → SERVFAIL，但必须是“再次尝试过”的应答
            reply = ask(resolver, name)
            self.assertIn(reply.header.rcode, (0, 2), name)


class ForwardFailureTest(unittest.TestCase):
    def test_upstream_failure_is_servfail_with_soa(self) -> None:
        reply = ask(make_resolver(ModeSwitch("good"), upstream=()), "example.com")
        self.assertEqual(reply.header.rcode, 2)
        self.assertEqual(soa_minimum(reply), DEFAULT_NEGATIVE_TTL)


if __name__ == "__main__":
    unittest.main()

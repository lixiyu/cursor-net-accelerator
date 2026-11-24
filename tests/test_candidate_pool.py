"""备选 IP：DNS 下掉后仍保留探测；过期淘汰；选路仍只用 DNS 当前。"""

from __future__ import annotations

import time
import unittest

from cursor_net_accelerator.pool import IpPool
from cursor_net_accelerator.probe import IpProbeResult


def _r(ip: str, ok: bool, ms: float | None = 10.0) -> IpProbeResult:
    return IpProbeResult(ip=ip, ok=ok, connect_ms=ms if ok else None, error=None if ok else "x")


class CandidatePoolTests(unittest.TestCase):
    def test_keeps_dns_dropped_ip_as_candidate(self) -> None:
        pool = IpPool(
            quarantine_seconds=60,
            pool_ttl_seconds=90,
            candidate_stale_seconds=3600,
        )
        pool.update("api2.cursor.sh", ["1.1.1.1", "2.2.2.2"], [_r("1.1.1.1", True), _r("2.2.2.2", True)])
        pool.update("api2.cursor.sh", ["1.1.1.1"], [_r("1.1.1.1", True), _r("2.2.2.2", True)])
        self.assertEqual(pool.candidate_ips("api2.cursor.sh"), ["2.2.2.2"])
        self.assertEqual(pool.healthy_ips("api2.cursor.sh"), ["1.1.1.1"])
        snap = pool.snapshot()["api2.cursor.sh"]
        roles = {e["ip"]: e["role"] for e in snap["entries"]}
        self.assertEqual(roles["1.1.1.1"], "dns")
        self.assertEqual(roles["2.2.2.2"], "candidate")

    def test_evicts_after_stale_without_ok(self) -> None:
        pool = IpPool(
            quarantine_seconds=60,
            pool_ttl_seconds=90,
            candidate_stale_seconds=1.0,
        )
        pool.update("api2.cursor.sh", ["1.1.1.1", "9.9.9.9"], [_r("1.1.1.1", True), _r("9.9.9.9", False)])
        pool.update("api2.cursor.sh", ["1.1.1.1"], [_r("1.1.1.1", True), _r("9.9.9.9", False)])
        time.sleep(1.2)
        pool.update("api2.cursor.sh", ["1.1.1.1"], [_r("1.1.1.1", True)])
        self.assertEqual(pool.candidate_ips("api2.cursor.sh"), [])

    def test_refreshes_last_ok_keeps_candidate(self) -> None:
        pool = IpPool(
            quarantine_seconds=60,
            pool_ttl_seconds=90,
            candidate_stale_seconds=2.0,
        )
        pool.update("api2.cursor.sh", ["1.1.1.1", "8.8.8.8"], [_r("1.1.1.1", True), _r("8.8.8.8", True)])
        pool.update("api2.cursor.sh", ["1.1.1.1"], [_r("1.1.1.1", True), _r("8.8.8.8", True)])
        time.sleep(0.6)
        pool.update("api2.cursor.sh", ["1.1.1.1"], [_r("1.1.1.1", True), _r("8.8.8.8", True)])
        self.assertEqual(pool.candidate_ips("api2.cursor.sh"), ["8.8.8.8"])

    def test_dns_flip_known_ips_keeps_old_resolved(self) -> None:
        """DNS A→B 后，探测合并集仍须含旧 A（update 前 known_ips）。"""
        pool = IpPool(
            quarantine_seconds=60,
            pool_ttl_seconds=90,
            candidate_stale_seconds=3600,
        )
        pool.update(
            "api2.cursor.sh",
            ["104.18.18.125", "104.18.19.125"],
            [_r("104.18.18.125", True), _r("104.18.19.125", True)],
        )
        # 此时池内 resolved 仍是 104.18；若只用 candidate_ips 会得到 []，旧 IP 漏探
        self.assertEqual(pool.candidate_ips("api2.cursor.sh"), [])
        known = set(pool.known_ips("api2.cursor.sh"))
        self.assertEqual(known, {"104.18.18.125", "104.18.19.125"})
        # 模拟本轮 DNS 翻到 AWS：合并探测目标 = 新 DNS ∪ known
        new_resolved = ["3.92.129.183", "52.21.130.202"]
        targets = list(dict.fromkeys([*new_resolved, *pool.known_ips("api2.cursor.sh")]))
        self.assertIn("104.18.18.125", targets)
        self.assertIn("3.92.129.183", targets)


if __name__ == "__main__":
    unittest.main()

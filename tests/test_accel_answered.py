"""加速效果分母 = 当时 answered_ips（选路下发），不是全部探测样本。"""

from __future__ import annotations

import time
import unittest

from cursor_net_accelerator.health import HealthHistory


class AccelAnsweredTests(unittest.TestCase):
    def test_accel_uses_answered_not_candidates(self) -> None:
        h = HealthHistory(retention_hours=1, persist=False)
        now = time.time()
        # DNS 挑中 1.1.1.1（通）；备选 9.9.9.9 不通——不应把加速效果打黄/红
        h.add_round(
            "api2.cursor.sh",
            [("1.1.1.1", True, 20.0), ("9.9.9.9", False, None)],
            ts=now,
            routing_mode="good",
            resolved_ips=["1.1.1.1"],
            answered_ips=["1.1.1.1"],
        )
        tl = h.timelines(probe_interval=30)
        host = tl["hosts"]["api2.cursor.sh"]
        acc = host["accelerated"]["slot_segments"]
        self.assertTrue(acc)
        self.assertTrue(all(s["state"] == "ok" for s in acc))
        svc = host["service"]["slot_segments"]
        # 服务只看 resolved，不应被备选失败打成 mixed
        self.assertTrue(all(s["state"] == "ok" for s in svc))


if __name__ == "__main__":
    unittest.main()

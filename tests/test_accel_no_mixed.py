"""加速效果条禁止出现 mixed（黄色）。"""

from __future__ import annotations

import time
import unittest

from cursor_net_accelerator.health import HealthHistory


class AccelNoMixedTests(unittest.TestCase):
    def test_accel_segments_never_mixed(self) -> None:
        h = HealthHistory(retention_hours=2, hold_seconds=300, persist=False)
        now = time.time()
        # 多轮：有通有不通、含备选失败，加速仍只能 ok/bad
        for i, (resolved, answered, samples) in enumerate(
            [
                (
                    ["1.1.1.1", "2.2.2.2"],
                    ["1.1.1.1"],
                    [("1.1.1.1", True, 10.0), ("2.2.2.2", False, None), ("9.9.9.9", False, None)],
                ),
                (
                    ["1.1.1.1", "2.2.2.2"],
                    ["1.1.1.1", "2.2.2.2"],
                    [("1.1.1.1", True, 10.0), ("2.2.2.2", True, 12.0)],
                ),
                (
                    ["1.1.1.1", "2.2.2.2"],
                    ["1.1.1.1", "2.2.2.2"],
                    [("1.1.1.1", False, None), ("2.2.2.2", False, None)],
                ),
                (
                    ["1.1.1.1"],
                    [],
                    [("1.1.1.1", False, None)],
                ),
            ]
        ):
            h.add_round(
                "api2.cursor.sh",
                samples,
                ts=now - 600 + i * 120,
                routing_mode="good",
                resolved_ips=resolved,
                answered_ips=answered,
            )
        tl = h.timelines()
        for host, blk in tl["hosts"].items():
            for seg in blk["accelerated"]["slot_segments"]:
                self.assertIn(seg["state"], ("ok", "bad"), msg=f"{host} {seg}")


if __name__ == "__main__":
    unittest.main()

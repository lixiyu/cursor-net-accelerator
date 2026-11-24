from __future__ import annotations

import random
import unittest

from cursor_net_accelerator.probe_schedule import (
    fmt_duration,
    fmt_duration_range,
    interval_for_age,
    pick_tier,
    tiers_public,
)


class ProbeScheduleTests(unittest.TestCase):
    def test_fmt_duration_human(self) -> None:
        self.assertEqual(fmt_duration(5), "5秒")
        self.assertEqual(fmt_duration(30), "30秒")
        self.assertEqual(fmt_duration(180), "3分钟")
        self.assertEqual(fmt_duration(900), "15分钟")
        self.assertEqual(fmt_duration(7200), "2小时")
        self.assertEqual(fmt_duration(None), "∞")
        self.assertEqual(fmt_duration_range(5, 7), "5~7秒")
        self.assertEqual(fmt_duration_range(1800, 3600), "30分钟~1小时")
        self.assertEqual(fmt_duration_range(900, 7200), "15分钟~2小时")
        tip = "\n".join(
            f"{t['label']}：无活动 {fmt_duration_range(t['activity_age_from_s'], t['activity_age_to_s'])} → 每 {t['interval']}"
            for t in tiers_public()
        )
        self.assertNotIn("7200", tip)
        self.assertIn("2小时", tip)

    def test_tiers_by_age(self) -> None:
        self.assertEqual(pick_tier(0).name, "hot")
        self.assertEqual(pick_tier(29.9).name, "hot")
        self.assertEqual(pick_tier(30).name, "busy")
        self.assertEqual(pick_tier(179).name, "busy")
        self.assertEqual(pick_tier(180).name, "mild")
        self.assertEqual(pick_tier(899).name, "mild")
        self.assertEqual(pick_tier(900).name, "idle")
        self.assertEqual(pick_tier(7199).name, "idle")
        self.assertEqual(pick_tier(7200).name, "deep")

    def test_intervals(self) -> None:
        rng = random.Random(0)
        _, hot = interval_for_age(10, rng=rng)
        self.assertGreaterEqual(hot, 5.0)
        self.assertLessEqual(hot, 7.0)
        _, busy = interval_for_age(60, rng=rng)
        self.assertEqual(busy, 30.0)
        _, mild = interval_for_age(400, rng=rng)
        self.assertEqual(mild, 60.0)
        _, idle = interval_for_age(2000, rng=rng)
        self.assertEqual(idle, 900.0)
        _, deep = interval_for_age(8000, rng=rng)
        self.assertGreaterEqual(deep, 1800.0)
        self.assertLessEqual(deep, 3600.0)


if __name__ == "__main__":
    unittest.main()

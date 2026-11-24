"""健康条向前覆盖：与约定示例一致。"""

from __future__ import annotations

import unittest

from cursor_net_accelerator.health import hold_segments


class HoldSegmentsTests(unittest.TestCase):
    def test_split_at_next_probe(self) -> None:
        # t=180 绿 → [0,180]；t=600 红 → [180,600]
        t0, t1 = 0.0, 600.0
        segs = hold_segments(
            [(180.0, "ok"), (600.0, "bad")],
            t0=t0,
            t1=t1,
            hold_s=600.0,
        )
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0]["state"], "ok")
        self.assertEqual(segs[1]["state"], "bad")

    def test_single_probe_fills_lookback(self) -> None:
        # 仅 t=600 红、hold=600 → [0,600] 全红
        segs = hold_segments([(600.0, "bad")], t0=0.0, t1=600.0, hold_s=600.0)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["state"], "bad")

    def test_gap_when_beyond_hold(self) -> None:
        # t=600 红、t=1800 绿、hold=600 → [0,600] 红，[1200,1800] 绿，中间灰
        segs = hold_segments(
            [(600.0, "bad"), (1800.0, "ok")],
            t0=0.0,
            t1=1800.0,
            hold_s=600.0,
        )
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0]["state"], "bad")
        self.assertEqual(segs[1]["state"], "ok")
        # 第二段左边界应是 1800-600=1200
        from datetime import datetime, timezone

        def epoch(iso: str) -> float:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()

        self.assertAlmostEqual(epoch(segs[1]["start"]) - epoch(segs[0]["end"]), 600.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()

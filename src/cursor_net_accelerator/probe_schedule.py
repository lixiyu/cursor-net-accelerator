"""按「多久没活动」分档决定探测间隔。"""

from __future__ import annotations

import random
from typing import NamedTuple


class ProbeTier(NamedTuple):
    """age_lt: 活动年龄小于该秒数则命中本档（从短到长匹配）。"""

    name: str
    label: str
    age_lt: float
    base_s: float
    jitter_s: float  # 实际间隔 ∈ [base, base+jitter]


# 约定（秒）：
#   <30s → 5s + 随机；<3min → 30s；3~15min → 60s；
#   15min~2h → 15min；>2h → 30~60min
TIERS: tuple[ProbeTier, ...] = (
    ProbeTier("hot", "热", 30.0, 5.0, 2.0),
    ProbeTier("busy", "忙", 180.0, 30.0, 0.0),
    ProbeTier("mild", "淡", 900.0, 60.0, 0.0),
    ProbeTier("idle", "闲", 7200.0, 900.0, 0.0),
    ProbeTier("deep", "沉", float("inf"), 1800.0, 1800.0),
)


def pick_tier(activity_age_s: float) -> ProbeTier:
    age = max(0.0, float(activity_age_s))
    for tier in TIERS:
        if age < tier.age_lt:
            return tier
    return TIERS[-1]


def interval_for_age(activity_age_s: float, *, rng: random.Random | None = None) -> tuple[ProbeTier, float]:
    tier = pick_tier(activity_age_s)
    r = rng or random
    if tier.jitter_s > 0:
        interval = tier.base_s + r.uniform(0.0, tier.jitter_s)
    else:
        interval = tier.base_s
    return tier, float(interval)


def fmt_duration(seconds: float | None) -> str:
    """人类可读时长：秒 / 分钟 / 小时（整除优先）。"""
    if seconds is None:
        return "∞"
    if seconds == float("inf"):
        return "∞"
    s = int(round(float(seconds)))
    if s < 0:
        s = 0
    if s < 60:
        return f"{s}秒"
    if s < 3600:
        m, rem = divmod(s, 60)
        return f"{m}分钟" if rem == 0 else f"{m}分{rem}秒"
    h, rem = divmod(s, 3600)
    if rem == 0:
        return f"{h}小时"
    if rem % 60 == 0:
        return f"{h}小时{rem // 60}分钟"
    return f"{h}小时{rem // 60}分{rem % 60}秒"


def fmt_duration_range(lo: float, hi: float | None) -> str:
    """区间两端同单位时写成「5~7秒」，否则「15分钟~2小时」。"""
    a = fmt_duration(lo)
    b = fmt_duration(hi)
    for unit in ("秒", "分钟", "小时"):
        if a.endswith(unit) and b.endswith(unit):
            return f"{a[: -len(unit)]}~{b}"
    return f"{a}~{b}"


def tiers_public() -> list[dict]:
    """状态页说明用。"""
    out = []
    prev = 0.0
    for t in TIERS:
        hi = t.age_lt if t.age_lt != float("inf") else None
        if t.jitter_s:
            interval = fmt_duration_range(t.base_s, t.base_s + t.jitter_s)
        else:
            interval = fmt_duration(t.base_s)
        out.append(
            {
                "name": t.name,
                "label": t.label,
                "activity_age_from_s": prev,
                "activity_age_to_s": hi,
                "interval": interval,
            }
        )
        prev = t.age_lt if t.age_lt != float("inf") else prev
    return out

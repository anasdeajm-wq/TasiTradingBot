"""
اختبارات بناء الخصائص - the no-lookahead property above all.

الخاصية المحروسة هنا هي أساس مصداقية النظام كله: لقطة الخصائص عند
الشمعة i يجب أن تطابق تماماً لقطة مبنية من سلسلة مقطوعة عند i. أي
اختلاف يعني أن القرار رأى مستقبلاً لم يكن متاحاً، وكل نتيجة بعده باطلة.
"""

import random
import pytest

from tasi import features as F


def series(n=320, seed=3):
    rng = random.Random(seed)
    price = 50.0
    o = h = l = c = None
    opens, highs, lows, closes, volumes, stamps = [], [], [], [], [], []
    for i in range(n):
        price *= 1 + 0.0006 + rng.gauss(0, 0.012)
        opens.append(price * 0.998)
        highs.append(price * 1.007)
        lows.append(price * 0.993)
        closes.append(price)
        volumes.append(rng.uniform(5e5, 2e6))
        stamps.append(f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}")
    return opens, highs, lows, closes, volumes, stamps


class TestNoLookahead:
    @pytest.mark.parametrize("index", [250, 275, 300, 319])
    def test_snapshot_matches_truncated_series(self, index):
        o, h, l, c, v, ts = series()
        at_index = F.build("X", o, h, l, c, v, ts, index=index)
        truncated = F.build("X", o[:index + 1], h[:index + 1], l[:index + 1],
                            c[:index + 1], v[:index + 1], ts[:index + 1])
        for field in ("close", "ma20", "ma50", "ma200", "rsi", "macd", "atr",
                      "volume_ratio", "bb_upper", "high_20"):
            assert getattr(at_index, field) == pytest.approx(
                getattr(truncated, field)), f"تسرّب في {field}"

    def test_build_series_matches_point_builds(self):
        o, h, l, c, v, ts = series()
        built = F.build_series("X", o, h, l, c, v, ts, start=300)
        for offset, snap in enumerate(built):
            point = F.build("X", o, h, l, c, v, ts, index=300 + offset)
            assert snap.ma50 == pytest.approx(point.ma50)
            assert snap.rsi == pytest.approx(point.rsi)
            assert snap.atr == pytest.approx(point.atr)

    def test_future_bars_do_not_change_a_past_snapshot(self):
        o, h, l, c, v, ts = series()
        before = F.build("X", o[:260], h[:260], l[:260], c[:260], v[:260], ts[:260])
        after = F.build("X", o, h, l, c, v, ts, index=259)
        assert before.close == pytest.approx(after.close)
        assert before.rsi == pytest.approx(after.rsi)


class TestCompleteness:
    def test_short_history_is_incomplete_not_fabricated(self):
        o, h, l, c, v, ts = series(n=30)
        snap = F.build("X", o, h, l, c, v, ts)
        assert snap.ma50 is None and snap.ma200 is None
        assert not snap.is_complete

    def test_empty_input_returns_empty_snapshot(self):
        snap = F.build("X", [], [], [], [], [], [])
        assert snap.bars == 0 and snap.close is None

    def test_full_history_is_complete(self):
        o, h, l, c, v, ts = series()
        assert F.build("X", o, h, l, c, v, ts).is_complete


class TestDerivedProperties:
    def test_above_ma50_and_trend_flags(self):
        snap = F.FeatureSnapshot(symbol="X", ts="d", close=100.0, ma50=95.0,
                                 ma200=90.0)
        assert snap.above_ma50 is True
        assert snap.trend_up is True

    def test_flags_are_none_when_inputs_missing(self):
        snap = F.FeatureSnapshot(symbol="X", ts="d", close=100.0)
        assert snap.above_ma50 is None and snap.trend_up is None

    def test_atr_pct_is_relative_to_price(self):
        o, h, l, c, v, ts = series()
        snap = F.build("X", o, h, l, c, v, ts)
        assert snap.atr_pct == pytest.approx(snap.atr / snap.close * 100)

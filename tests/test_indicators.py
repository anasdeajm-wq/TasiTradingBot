"""
اختبارات المؤشرات - verified against hand-computed values, not against
whatever the code currently returns.

قاعدة هذه الاختبارات: كل رقم متوقع محسوب يدوياً أو من خاصية رياضية
معروفة. اختبار يقارن المخرجات بمخرجات سابقة يثبّت الخطأ ولا يكشفه.
"""

import math
import pytest

from tasi import indicators as ind


class TestSMA:
    def test_matches_hand_computed_mean(self):
        values = [1, 2, 3, 4, 5, 6]
        out = ind.sma(values, 3)
        assert out[:2] == [None, None]      # لا قيمة قبل اكتمال النافذة
        assert out[2] == pytest.approx(2.0)  # (1+2+3)/3
        assert out[5] == pytest.approx(5.0)  # (4+5+6)/3

    def test_constant_series_returns_the_constant(self):
        assert ind.sma([7.0] * 10, 4)[-1] == pytest.approx(7.0)

    def test_rejects_non_positive_period(self):
        with pytest.raises(ValueError):
            ind.sma([1, 2, 3], 0)

    def test_series_shorter_than_period_is_all_none(self):
        assert ind.sma([1, 2], 5) == [None, None]


class TestEMA:
    def test_seeded_with_sma_of_first_window(self):
        values = [1, 2, 3, 4, 5]
        out = ind.ema(values, 3)
        assert out[2] == pytest.approx(2.0)          # بذرة = متوسط أول ثلاثة
        multiplier = 2 / 4
        expected = (4 - 2.0) * multiplier + 2.0
        assert out[3] == pytest.approx(expected)

    def test_constant_series_stays_constant(self):
        assert ind.ema([5.0] * 20, 5)[-1] == pytest.approx(5.0)


class TestRSI:
    def test_monotonic_rise_is_100(self):
        assert ind.rsi([float(i) for i in range(1, 40)], 14)[-1] == pytest.approx(100.0)

    def test_monotonic_fall_is_0(self):
        assert ind.rsi([float(i) for i in range(40, 1, -1)], 14)[-1] == pytest.approx(0.0)

    def test_stays_within_bounds_on_noisy_series(self):
        import random
        rng = random.Random(4)
        series = [100.0]
        for _ in range(300):
            series.append(series[-1] * (1 + rng.gauss(0, 0.02)))
        for value in ind.rsi(series, 14):
            if value is not None:
                assert 0.0 <= value <= 100.0

    def test_undefined_until_period_elapsed(self):
        out = ind.rsi([float(i) for i in range(1, 20)], 14)
        assert all(v is None for v in out[:14])
        assert out[14] is not None


class TestMACD:
    def test_histogram_is_macd_minus_signal(self):
        values = [100 + i * 0.5 for i in range(80)]
        out = ind.macd(values)
        for m, s, h in zip(out["macd"], out["signal"], out["hist"]):
            if m is None or s is None:
                assert h is None
            else:
                assert h == pytest.approx(m - s)

    def test_all_series_have_input_length(self):
        values = [float(i) for i in range(90)]
        out = ind.macd(values)
        assert len(out["macd"]) == len(out["signal"]) == len(out["hist"]) == 90

    def test_rejects_fast_not_below_slow(self):
        with pytest.raises(ValueError):
            ind.macd([1.0] * 50, fast=26, slow=12)


class TestVolumeRatio:
    def test_baseline_excludes_the_current_bar(self):
        # القفزة تُقاس على متوسط ما قبلها، وإلا ضخّمت خط أساسها فبدت أصغر
        volumes = [100.0] * 20 + [200.0]
        assert ind.volume_ratio(volumes, 20)[-1] == pytest.approx(2.0)

    def test_undefined_before_a_full_baseline_exists(self):
        assert ind.volume_ratio([100.0] * 20 + [200.0], 20)[19] is None

    def test_zero_baseline_returns_none_not_infinity(self):
        assert ind.volume_ratio([0.0] * 20 + [500.0], 20)[-1] is None


class TestATR:
    def test_true_range_accounts_for_gaps(self):
        # إغلاق أمس 10، واليوم فتح عند 14 وأعلى 15: المدى الحقيقي 5 لا 1
        highs, lows, closes = [12, 15], [8, 14], [10, 14.5]
        assert ind.true_range(highs, lows, closes)[1] == pytest.approx(5.0)

    def test_first_bar_true_range_is_high_minus_low(self):
        assert ind.true_range([12], [8], [10])[0] == pytest.approx(4.0)

    def test_atr_of_constant_range_equals_that_range(self):
        n = 40
        highs = [11.0] * n
        lows = [10.0] * n
        closes = [10.5] * n
        assert ind.atr(highs, lows, closes, 14)[-1] == pytest.approx(1.0, abs=1e-6)

    def test_undefined_when_history_too_short(self):
        assert all(v is None for v in ind.atr([1, 2], [0, 1], [1, 2], 14))


class TestVWAP:
    def test_single_bar_equals_typical_price(self):
        assert ind.vwap([12], [8], [10], [100])[0] == pytest.approx(30 / 3)

    def test_weights_by_volume(self):
        # شمعتان بنفس السعر النموذجي 10 و 20، والحجم يرجّح الثانية ثلاثة أضعاف
        out = ind.vwap([10, 20], [10, 20], [10, 20], [100, 300])
        assert out[-1] == pytest.approx((10 * 100 + 20 * 300) / 400)

    def test_zero_volume_yields_none(self):
        assert ind.vwap([10], [10], [10], [0])[0] is None


class TestBollinger:
    def test_bands_straddle_the_middle(self):
        import random
        rng = random.Random(1)
        values = [100 + rng.gauss(0, 3) for _ in range(60)]
        bands = ind.bollinger(values, 20)
        assert bands["lower"][-1] < bands["middle"][-1] < bands["upper"][-1]

    def test_constant_series_has_zero_width(self):
        bands = ind.bollinger([10.0] * 30, 20)
        assert bands["upper"][-1] == pytest.approx(bands["lower"][-1])
        assert bands["width"][-1] == pytest.approx(0.0)


class TestROCAndRelativeStrength:
    def test_roc_is_percentage_change_over_period(self):
        values = [100.0] * 10 + [110.0]
        assert ind.roc(values, 10)[-1] == pytest.approx(10.0)

    def test_relative_strength_is_difference_of_returns(self):
        stock = [100.0] * 20 + [110.0]
        index = [100.0] * 20 + [104.0]
        assert ind.relative_strength(stock, index, 20)[-1] == pytest.approx(6.0)

    def test_relative_strength_zero_when_identical(self):
        series = [100 + i for i in range(40)]
        assert ind.relative_strength(series, list(series), 20)[-1] == pytest.approx(0.0)


class TestComputeAll:
    def test_short_history_leaves_slow_indicators_none(self):
        closes = [float(i) for i in range(30)]
        snap = ind.compute_all("X", closes, [1000.0] * 30)
        assert snap.ma20 is not None
        assert snap.ma50 is None       # لا يُخترع رقم من تاريخ غير كافٍ
        assert snap.ma200 is None

    def test_empty_input_does_not_raise(self):
        snap = ind.compute_all("X", [], [])
        assert snap.bars == 0 and snap.close is None

    def test_volumes_shorter_than_closes_are_padded(self):
        snap = ind.compute_all("X", [1.0] * 40, [100.0] * 10)
        assert snap.bars == 40

"""اختبارات آليات الحماية - volatility scaling and the recorded verdicts."""

import pytest

from tasi import protection as pr


class TestRealizedVolatility:
    def test_constant_series_has_zero_volatility(self):
        assert pr.realized_volatility([100.0] * 100, 90) == pytest.approx(0.0)

    def test_volatile_series_scores_higher_than_calm_one(self):
        calm = [100.0 * (1.001 ** i) for i in range(100)]
        wild = [100.0 * (1.05 if i % 2 else 0.96) ** 1 for i in range(100)]
        assert (pr.realized_volatility(wild, 90)
                > pr.realized_volatility(calm, 90))

    def test_thin_history_returns_none_rather_than_a_guess(self):
        assert pr.realized_volatility([100.0, 101.0], 2) is None

    def test_uses_only_data_before_the_index(self):
        series = [100.0] * 60 + [100.0 * (1.1 ** i) for i in range(40)]
        # عند الفهرس 60 لم تبدأ الفترة المتقلبة بعد
        assert pr.realized_volatility(series, 60) == pytest.approx(0.0)


class TestVolatilityScaling:
    def test_calm_market_gets_full_exposure(self):
        calm = [100.0 * (1.0002 ** i) for i in range(200)]
        assert pr.volatility_scaled_exposure(calm, 150, 0.10) == pytest.approx(1.0)

    def test_volatile_market_gets_reduced_exposure(self):
        import random
        rng = random.Random(2)
        wild = [100.0]
        for _ in range(200):
            wild.append(wild[-1] * (1 + rng.gauss(0, 0.05)))
        assert pr.volatility_scaled_exposure(wild, 150, 0.10) < 0.5

    def test_exposure_never_exceeds_the_cap(self):
        calm = [100.0] * 200
        assert pr.volatility_scaled_exposure(calm, 150, 0.10,
                                             max_exposure=1.0) <= 1.0

    def test_unknown_volatility_falls_back_to_the_cap(self):
        assert pr.volatility_scaled_exposure([100.0, 101.0], 2, 0.10) == 1.0


class TestRecordedVerdicts:
    def test_every_verdict_carries_an_explanation(self):
        for verdict in pr.MEASURED.values():
            assert verdict.verdict_ar

    def test_no_protection_mechanism_beat_the_baseline(self):
        baseline = pr.MEASURED["none"].efficiency
        for key, verdict in pr.MEASURED.items():
            if key in ("none", "absolute_momentum"):
                continue          # الأخير بلا أثر فعلي، ليس آلية حماية
            assert verdict.efficiency <= baseline, key

    def test_stop_loss_increased_drawdown(self):
        assert (pr.MEASURED["stop_loss_10"].max_drawdown
                > pr.MEASURED["none"].max_drawdown)

    def test_volatility_targeting_lowered_drawdown_and_return_together(self):
        target = pr.MEASURED["vol_target_10"]
        base = pr.MEASURED["none"]
        assert target.max_drawdown < base.max_drawdown
        assert target.cagr < base.cagr

    def test_comparison_renders(self):
        assert "الكفاءة" in pr.compare_ar()

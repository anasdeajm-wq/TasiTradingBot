"""اختبارات المسح - guarding against the multiple-comparisons trap."""

import pytest

from tasi import sweep as sw


def result(train, test, params, survived, regime="RANGE"):
    return sw.SweepResult(setup="t", params=params, regime=regime,
                          train_trades=60, train_expectancy=train,
                          test_trades=50, test_expectancy=test,
                          survived=survived)


class TestGrid:
    def test_expands_cartesian_product(self):
        combos = sw.expand_grid({"a": (1, 2), "b": (10, 20, 30)})
        assert len(combos) == 6
        assert {"a": 2, "b": 30} in combos

    def test_empty_grid_yields_one_empty_combination(self):
        assert sw.expand_grid({}) == [{}]

    def test_every_default_grid_expands(self):
        for name, grid in sw.DEFAULT_GRIDS.items():
            assert len(sw.expand_grid(grid)) > 0, name

    def test_setup_lookup_rejects_unknown_name(self):
        with pytest.raises(ValueError):
            sw.setup_class_by_name("not_a_setup")


class TestNullEstimate:
    """الفرضية الصفرية تُقدَّر من البيانات، لا تُفترض."""

    def test_null_derives_from_observed_win_rates(self):
        report = sw.SweepReport(combinations_tested=100)
        # ٥٠٪ ربحت في التدريب و٥٠٪ في التحقق => المتوقع ٢٥ ناجياً
        report.results = [result(0.1 if i % 2 else -0.1,
                                 0.1 if i % 2 else -0.1, {"a": i}, False)
                          for i in range(100)]
        assert report.expected_by_chance == pytest.approx(25.0, abs=1.0)

    def test_null_is_low_when_most_combinations_lose(self):
        report = sw.SweepReport(combinations_tested=100)
        report.results = [result(-0.1, -0.1, {"a": i}, False) for i in range(95)]
        report.results += [result(0.1, 0.1, {"a": 100 + i}, True) for i in range(5)]
        assert report.expected_by_chance < 1.0

    def test_no_results_means_no_expectation(self):
        assert sw.SweepReport().expected_by_chance == 0.0


class TestVerdict:
    def test_zero_survivors_is_stated_plainly(self):
        report = sw.SweepReport(combinations_tested=50)
        report.results = [result(-0.1, -0.1, {"a": i}, False) for i in range(50)]
        assert "لم تصمد" in report.verdict_ar()

    def test_survivors_at_chance_level_are_called_noise(self):
        # نافذتان مستقلتان: ٥٠٪ تربح في التدريب و٥٠٪ في التحقق بلا ارتباط،
        # فالناجون ٢٥ وهو تماماً ما تتوقعه الصدفة
        report = sw.SweepReport(combinations_tested=100)
        report.results = [
            result(0.1 if i % 2 == 0 else -0.1,
                   0.1 if (i // 2) % 2 == 0 else -0.1, {"a": i},
                   i % 2 == 0 and (i // 2) % 2 == 0)
            for i in range(100)]
        assert len(report.survivors) == pytest.approx(25, abs=2)
        assert "ضجيج" in report.verdict_ar()

    def test_safety_margin_sits_above_the_raw_null(self):
        assert sw.SweepReport.CHANCE_MARGIN > 1.0

    def test_survivors_far_above_chance_are_called_a_candidate(self):
        report = sw.SweepReport(combinations_tested=100)
        report.results = [result(-0.1, -0.1, {"a": i, "b": 0}, False)
                          for i in range(90)]
        report.results += [result(0.2, 0.2, {"a": 1, "b": j}, True)
                           for j in range(10)]
        verdict = report.verdict_ar()
        assert "مرشّح" in verdict
        assert "تشغيل" in verdict        # لا يُوصى بالتشغيل حتى للمرشّح

    def test_no_results_says_so(self):
        assert "كافية" in sw.SweepReport().verdict_ar()


class TestClustering:
    def test_adjacent_survivors_form_one_cluster(self):
        report = sw.SweepReport()
        report.results = [result(0.1, 0.1, {"a": 1, "b": j}, True)
                          for j in range(5)]
        assert report.survivor_clusters == 1

    def test_distant_survivors_form_separate_clusters(self):
        report = sw.SweepReport()
        report.results = [
            result(0.1, 0.1, {"a": 1, "b": 1}, True),
            result(0.1, 0.1, {"a": 9, "b": 9}, True),
        ]
        assert report.survivor_clusters == 2

    def test_survivors_in_different_regimes_never_cluster(self):
        report = sw.SweepReport()
        report.results = [
            result(0.1, 0.1, {"a": 1}, True, regime="RANGE"),
            result(0.1, 0.1, {"a": 1}, True, regime="TREND_UP"),
        ]
        assert report.survivor_clusters == 2

    def test_no_survivors_means_no_clusters(self):
        report = sw.SweepReport()
        report.results = [result(-0.1, -0.1, {"a": 1}, False)]
        assert report.survivor_clusters == 0

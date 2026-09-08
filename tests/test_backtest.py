"""
اختبارات محرك القياس - the constraints that keep results honest.

كل اختبار هنا يحرس قيداً واحداً. لو سقط أحدها صار الاختبار التاريخي
يعطي أرقاماً متفائلة كاذبة، وهي أخطر من عدم وجود أرقام لأنه يُبنى عليها
قرار برأس مال حقيقي.
"""

import pytest

from tasi import backtest as bt
from tasi import setups as S


def match(stop=98.0, target=106.0, price=100.0, setup="t"):
    return S.SetupMatch(setup=setup, symbol="X", ts="d0", score=50.0,
                        price=price, reason_ar="", stop_hint=stop,
                        target_hint=target)


FREE = dict(commission_pct=0.0, slippage_pct=0.0)


class TestEntryTiming:
    def test_entry_is_next_bar_open_not_signal_bar_close(self):
        # لا يمكن الشراء بسعر لم يُعرف إلا بعد إغلاق شمعة الإشارة
        opens = [100, 103, 103]
        trade = bt.simulate_trade(
            match(), opens, [100, 104, 107], [100, 102, 103], [100, 103, 106],
            ["d0", "d1", "d2"], signal_index=0, **FREE)
        assert trade.entry_price == pytest.approx(103)
        assert trade.entry_ts == "d1"

    def test_no_trade_when_no_bar_follows_the_signal(self):
        assert bt.simulate_trade(match(), [100], [100], [100], [100],
                                 ["d0"], 0, **FREE) is None

    def test_rejects_stop_at_or_above_entry(self):
        assert bt.simulate_trade(match(stop=101.0), [100, 100], [100, 100],
                                 [100, 100], [100, 100], ["d0", "d1"], 0,
                                 **FREE) is None


class TestExitPriority:
    def test_target_hit_gives_expected_r(self):
        trade = bt.simulate_trade(
            match(), [100, 100, 100], [100, 101, 107], [100, 99.5, 100],
            [100, 101, 106], ["a", "b", "c"], 0, **FREE)
        assert trade.exit_reason == "TARGET"
        assert trade.r_multiple == pytest.approx(3.0)   # (106-100)/(100-98)

    def test_stop_hit_gives_minus_one_r(self):
        trade = bt.simulate_trade(
            match(), [100, 100, 100], [100, 100.5, 100], [100, 99, 97.5],
            [100, 100, 98], ["a", "b", "c"], 0, **FREE)
        assert trade.exit_reason == "STOP"
        assert trade.r_multiple == pytest.approx(-1.0)

    def test_bar_touching_both_counts_as_stop(self):
        # بيانات الشموع لا تخبرنا أيهما وقع أولاً، فالافتراض المتحفظ هو الأمين
        trade = bt.simulate_trade(
            match(), [100, 100], [100, 107], [100, 97], [100, 103],
            ["a", "b"], 0, **FREE)
        assert trade.exit_reason == "STOP"
        assert trade.r_multiple == pytest.approx(-1.0)

    def test_time_exit_holds_exactly_max_bars(self):
        n = 6
        trade = bt.simulate_trade(
            match(), [100] * n, [100.9] * n, [99.5] * n, [100.5] * n,
            [f"d{i}" for i in range(n)], 0, max_bars=3, **FREE)
        assert trade.bars_held == 3
        assert trade.exit_reason == "TIME"


class TestCosts:
    def test_costs_reduce_the_result(self):
        args = ([100, 100, 100], [100, 101, 107], [100, 99.5, 100],
                [100, 101, 106], ["a", "b", "c"], 0)
        free = bt.simulate_trade(match(), *args, **FREE)
        charged = bt.simulate_trade(match(), *args)
        assert charged.r_multiple < free.r_multiple

    def test_slippage_worsens_both_ends(self):
        args = ([100, 100, 100], [100, 101, 107], [100, 99.5, 100],
                [100, 101, 106], ["a", "b", "c"], 0)
        clean = bt.simulate_trade(match(), *args, **FREE)
        slipped = bt.simulate_trade(match(), *args, commission_pct=0.0,
                                    slippage_pct=0.01)
        assert slipped.entry_price > clean.entry_price
        assert slipped.exit_price < clean.exit_price


def make_trades(spec):
    out = []
    for r, regime in spec:
        out.append(bt.Trade(setup="t", symbol="X", regime=regime, entry_ts="d",
                            entry_price=100, stop_price=98, target_price=106,
                            r_multiple=r, bars_held=3))
    return out


class TestAggregate:
    def test_computes_hit_rate_and_expectancy(self):
        stats = bt.aggregate(make_trades(
            [(2.0, "UP"), (-1.0, "UP"), (3.0, "UP"), (-1.0, "UP")]))[("t", "UP")]
        assert stats.trades == 4
        assert stats.hit_rate == pytest.approx(0.5)
        assert stats.expectancy == pytest.approx(0.75)
        assert stats.profit_factor == pytest.approx(2.5)

    def test_max_drawdown_tracks_worst_run(self):
        stats = bt.aggregate(make_trades(
            [(1.0, "UP"), (-1.0, "UP"), (-1.0, "UP"), (2.0, "UP")]))[("t", "UP")]
        assert stats.max_drawdown_r == pytest.approx(2.0)

    def test_separates_regimes(self):
        stats = bt.aggregate(make_trades([(1.0, "UP"), (-1.0, "RANGE")]))
        assert set(stats) == {("t", "UP"), ("t", "RANGE")}

    def test_small_sample_is_not_viable_even_when_profitable(self):
        stats = bt.aggregate(make_trades([(5.0, "UP")] * 4))[("t", "UP")]
        assert stats.expectancy > 0
        assert not stats.is_viable       # أقل من ٢٠ صفقة لا يكفي للحكم

    def test_profit_factor_none_when_no_losses(self):
        stats = bt.aggregate(make_trades([(1.0, "UP")] * 3))[("t", "UP")]
        assert stats.profit_factor is None


class TestOutOfSampleValidation:
    @staticmethod
    def timed(spec):
        out = []
        for i, (r, regime) in enumerate(spec):
            out.append(bt.Trade(setup="t", symbol="X", regime=regime,
                                entry_ts=f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
                                entry_price=100, stop_price=98, target_price=106,
                                r_multiple=r, bars_held=3))
        return out

    def test_split_is_chronological(self):
        trades = self.timed([(1.0, "UP")] * 100)
        train, test, cut = bt.split_by_time(trades, 0.6)
        assert len(train) + len(test) == 100
        assert all(t.entry_ts < cut for t in train)
        assert all(t.entry_ts >= cut for t in test)

    def test_setup_profitable_in_both_windows_survives(self):
        trades = self.timed([(0.5, "UP")] * 200)
        verdict = bt.validate(trades)[("t", "UP")]
        assert verdict["survived"] is True

    def test_setup_that_collapses_out_of_sample_fails(self):
        # رابح في التدريب وخاسر في التحقق: ملاءمة زائدة لا أفضلية
        trades = self.timed([(1.0, "UP")] * 120 + [(-1.0, "UP")] * 80)
        verdict = bt.validate(trades)[("t", "UP")]
        assert verdict["train_expectancy"] > 0
        assert verdict["test_expectancy"] < 0
        assert verdict["survived"] is False

    def test_thin_sample_never_survives(self):
        trades = self.timed([(1.0, "UP")] * 20)
        assert bt.validate(trades)[("t", "UP")]["survived"] is False


class TestWeightGate:
    def test_weight_stays_zero_for_setup_that_failed_validation(self, tmp_path):
        from tasi import db
        conn = db.connect(str(tmp_path / "w.db"))
        trades = TestOutOfSampleValidation.timed(
            [(1.0, "UP")] * 120 + [(-1.0, "UP")] * 80)
        bt.save_validated_performance(conn, trades)
        weight = conn.execute(
            "SELECT weight FROM setup_performance WHERE setup='t'").fetchone()[0]
        assert weight == pytest.approx(0.0)

    def test_weight_positive_only_for_survivor(self, tmp_path):
        from tasi import db
        conn = db.connect(str(tmp_path / "w2.db"))
        bt.save_validated_performance(
            conn, TestOutOfSampleValidation.timed([(0.5, "UP")] * 200))
        weight = conn.execute(
            "SELECT weight FROM setup_performance WHERE setup='t'").fetchone()[0]
        assert weight > 0

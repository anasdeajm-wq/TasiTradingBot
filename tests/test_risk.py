"""اختبارات إدارة المخاطر - sizing, limits, and cost viability."""

from datetime import date, datetime, timedelta

import pytest

from tasi import db
from tasi.risk import RiskManager


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "risk.db"))


class TestSizing:
    def test_risk_per_trade_matches_the_configured_fraction(self):
        risk = RiskManager(capital=100_000, risk_per_trade=0.01,
                           max_position_pct=1.0, commission_pct=0.0)
        sizing = risk.size_from_atr("X", entry_price=100.0, atr=2.0)
        # الوقف = 100 - 2×1.5 = 97، والمخاطرة للسهم 3 ريالات
        assert sizing.stop_price == pytest.approx(97.0)
        assert sizing.shares == 333          # floor(1000 / 3)
        assert sizing.risk_amount == pytest.approx(999.0, abs=1.0)

    def test_position_cap_binds_before_risk_budget(self):
        risk = RiskManager(capital=100_000, max_position_pct=0.10,
                           commission_pct=0.0)
        sizing = risk.size_from_atr("X", entry_price=10.0, atr=0.05)
        assert sizing.position_value <= 100_000 * 0.10 + 10

    def test_atr_sizing_equalises_risk_across_volatilities(self):
        # رأس مال كبير ومخاطرة صغيرة حتى لا يقيّد سقف الصفقة أياً منهما،
        # فيظهر أثر ATR وحده على الحجم
        risk = RiskManager(capital=1_000_000, risk_per_trade=0.001,
                           max_position_pct=1.0, commission_pct=0.0)
        quiet = risk.size_from_atr("Q", 100.0, atr=0.5)
        wild = risk.size_from_atr("W", 100.0, atr=5.0)
        assert wild.shares < quiet.shares
        assert quiet.risk_amount == pytest.approx(wild.risk_amount, rel=0.02)

    def test_position_cap_lowers_risk_below_budget_never_above(self):
        # حين يقيّد السقف الحجم تقل المخاطرة الفعلية عن الميزانية، وهو
        # الاتجاه الآمن الوحيد المقبول
        risk = RiskManager(capital=100_000, max_position_pct=0.2,
                           commission_pct=0.0)
        sizing = risk.size_from_atr("X", 100.0, atr=0.5)
        assert sizing.risk_amount <= risk.risk_amount

    def test_missing_atr_is_refused_not_guessed(self):
        sizing = RiskManager(capital=100_000).size_from_atr("X", 100.0, atr=0.0)
        assert not sizing.approved and "ATR" in sizing.reason

    def test_capital_too_small_for_one_share_is_refused(self):
        sizing = RiskManager(capital=100).size_from_atr("X", 500.0, atr=10.0)
        assert not sizing.approved and sizing.shares == 0


class TestCostViability:
    def test_trade_refused_when_costs_eat_the_target(self):
        risk = RiskManager(capital=100_000, commission_pct=0.05,
                           max_cost_ratio=0.25, max_position_pct=1.0)
        sizing = risk.size_from_atr("X", 100.0, atr=1.0, target_price=100.2)
        assert not sizing.approved
        assert "التكاليف" in sizing.reason

    def test_same_trade_passes_at_a_realistic_commission(self):
        # ٠.١٥٥٪ لكل جهة وهي عمولة وسيط سعودي معتادة
        risk = RiskManager(capital=100_000, commission_pct=0.00155,
                           max_cost_ratio=0.25, max_position_pct=1.0)
        sizing = risk.size_from_atr("X", 100.0, atr=1.0, target_price=110.0)
        assert sizing.approved
        assert sizing.cost_ratio < 0.25
        assert sizing.expected_profit > 0

    def test_round_trip_cost_counts_both_sides(self):
        risk = RiskManager(capital=100_000, commission_pct=0.01,
                           max_position_pct=1.0)
        sizing = risk.size_from_atr("X", 100.0, atr=1.0, target_price=120.0)
        assert sizing.round_trip_cost == pytest.approx(
            sizing.position_value * 0.01 * 2)


class TestLimits:
    @staticmethod
    def close_position(conn, pnl, when):
        conn.execute(
            "INSERT INTO positions (symbol, quantity, avg_entry, realized_pnl,"
            " status, opened_at, closed_at) VALUES ('X',1,1,?,'CLOSED',?,?)",
            (pnl, when, when))
        conn.commit()

    def test_daily_limit_blocks_new_entries(self, conn):
        risk = RiskManager(capital=100_000, conn=conn)
        assert risk.status().trading_allowed
        self.close_position(conn, -3_100, datetime.now().isoformat())
        status = risk.status()
        assert status.daily_limit_hit and not status.trading_allowed
        assert not risk.approve_entry("X", 100.0, 2.0).approved

    def test_profit_offsets_loss_within_the_day(self, conn):
        risk = RiskManager(capital=100_000, conn=conn)
        now = datetime.now().isoformat()
        self.close_position(conn, -3_100, now)
        self.close_position(conn, 500, now)
        assert risk.status().trading_allowed      # الصافي -2,600 دون الحد

    def test_weekly_limit_blocks_and_outranks_daily(self, conn):
        risk = RiskManager(capital=100_000, conn=conn)
        start = RiskManager.week_start()
        for offset in (0, 1):
            self.close_position(conn, -3_100, (start + timedelta(days=offset)).isoformat())
        status = risk.status()
        assert status.weekly_limit_hit
        assert "الأسبوعي" in status.blocked_reason

    def test_week_starts_on_sunday(self):
        assert RiskManager.week_start(date(2026, 9, 9)).weekday() == 6

    def test_duplicate_position_on_same_symbol_refused(self, conn):
        conn.execute("INSERT INTO positions (symbol, quantity, avg_entry,"
                     " status, opened_at) VALUES ('X',10,100,'OPEN','now')")
        conn.commit()
        risk = RiskManager(capital=100_000, conn=conn)
        assert not risk.approve_entry("X", 100.0, 2.0).approved

    def test_rejects_non_positive_capital(self):
        with pytest.raises(ValueError):
            RiskManager(capital=0)

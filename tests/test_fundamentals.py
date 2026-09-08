"""اختبارات القوائم المالية ومرشّح الجودة."""

import pytest

from tasi import db
from tasi import fundamentals as fu


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(str(tmp_path / "fund.db"))
    fu.ensure_schema(connection)
    return connection


def seed(conn, symbol, years):
    """years = {تاريخ: {حقل: قيمة}}"""
    fu.store(conn, symbol, years)


HEALTHY = {
    "TotalRevenue": 1000.0, "NetIncome": 120.0, "OperatingIncome": 180.0,
    "GrossProfit": 400.0, "TotalDebt": 200.0, "StockholdersEquity": 800.0,
    "TotalAssets": 1500.0, "CurrentAssets": 500.0, "CurrentLiabilities": 250.0,
    "InterestExpense": 20.0, "FreeCashFlow": 90.0,
}


class TestStorage:
    def test_round_trip(self, conn):
        seed(conn, "X", {"2024-12-31": HEALTHY})
        loaded = fu.load(conn, "X")
        assert loaded["2024-12-31"]["NetIncome"] == 120.0

    def test_rewrite_updates_rather_than_duplicates(self, conn):
        seed(conn, "X", {"2024-12-31": {"NetIncome": 1.0}})
        seed(conn, "X", {"2024-12-31": {"NetIncome": 2.0}})
        assert fu.load(conn, "X")["2024-12-31"]["NetIncome"] == 2.0


class TestRatios:
    def test_computes_margins_and_leverage(self, conn):
        seed(conn, "X", {"2023-12-31": HEALTHY, "2024-12-31": HEALTHY})
        q = fu.score(conn, "X")
        assert q.net_margin == pytest.approx(0.12)
        assert q.operating_margin == pytest.approx(0.18)
        assert q.debt_to_equity == pytest.approx(0.25)
        assert q.current_ratio == pytest.approx(2.0)
        assert q.interest_coverage == pytest.approx(9.0)
        assert q.profitable is True

    def test_negative_equity_yields_none_not_a_nonsense_ratio(self, conn):
        broken = dict(HEALTHY, StockholdersEquity=-100.0)
        seed(conn, "X", {"2024-12-31": broken})
        q = fu.score(conn, "X")
        assert q.debt_to_equity is None
        assert q.roe is None

    def test_zero_denominator_is_none(self, conn):
        seed(conn, "X", {"2024-12-31": dict(HEALTHY, TotalRevenue=0.0)})
        assert fu.score(conn, "X").net_margin is None

    def test_growth_needs_two_years(self, conn):
        seed(conn, "X", {"2024-12-31": HEALTHY})
        assert fu.score(conn, "X").revenue_growth is None
        seed(conn, "X", {"2023-12-31": dict(HEALTHY, TotalRevenue=800.0)})
        assert fu.score(conn, "X").revenue_growth == pytest.approx(0.25)


class TestScreen:
    def test_healthy_company_passes(self, conn):
        seed(conn, "X", {"2023-12-31": HEALTHY, "2024-12-31": HEALTHY})
        assert fu.score(conn, "X").passes()

    def test_loss_making_company_is_rejected(self, conn):
        losing = dict(HEALTHY, NetIncome=-50.0)
        seed(conn, "X", {"2023-12-31": losing, "2024-12-31": losing})
        reasons = fu.score(conn, "X").failures()
        assert any("خسارة" in r for r in reasons)

    def test_negative_gross_profit_is_rejected(self, conn):
        bad = dict(HEALTHY, GrossProfit=-100.0)
        seed(conn, "X", {"2023-12-31": bad, "2024-12-31": bad})
        assert any("إجمالي سالب" in r for r in fu.score(conn, "X").failures())

    def test_heavy_leverage_is_rejected(self, conn):
        levered = dict(HEALTHY, TotalDebt=2500.0)
        seed(conn, "X", {"2023-12-31": levered, "2024-12-31": levered})
        assert any("دين" in r for r in fu.score(conn, "X").failures())

    def test_threshold_is_configurable(self, conn):
        levered = dict(HEALTHY, TotalDebt=2500.0)
        seed(conn, "X", {"2023-12-31": levered, "2024-12-31": levered})
        assert fu.score(conn, "X").passes(max_debt_to_equity=5.0)

    def test_missing_data_is_not_a_pass(self, conn):
        assert not fu.score(conn, "UNKNOWN").has_enough_data
        assert fu.screen(conn, ["UNKNOWN"], require_data=True)["UNKNOWN"]


class TestPointInTime:
    def test_as_of_excludes_later_reports(self, conn):
        seed(conn, "X", {"2023-12-31": dict(HEALTHY, NetIncome=50.0),
                         "2024-12-31": dict(HEALTHY, NetIncome=500.0)})
        early = fu.score(conn, "X", as_of="2024-06-30")
        assert early.as_of == "2023-12-31"
        assert early.net_margin == pytest.approx(0.05)

    def test_no_data_before_cutoff_yields_empty_score(self, conn):
        seed(conn, "X", {"2024-12-31": HEALTHY})
        assert fu.score(conn, "X", as_of="2020-01-01").years_of_data == 0

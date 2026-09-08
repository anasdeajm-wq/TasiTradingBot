"""اختبارات الزخم المقطعي - ranking, liquidity filter, and accounting."""

import pytest

from tasi import db
from tasi import momentum as mo


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "mom.db"))


def seed(conn, symbol, closes, volume=1e6, market="MAIN"):
    from datetime import date, timedelta
    day = date(2022, 1, 1)
    rows = []
    for i, close in enumerate(closes):
        rows.append((symbol, "1day", (day + timedelta(days=i)).isoformat(),
                     close, close * 1.005, close * 0.995, close, volume,
                     None, None))
    conn.executemany(
        "INSERT OR REPLACE INTO bars (symbol, interval, ts, open, high, low,"
        " close, volume, turnover, trades) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("INSERT OR REPLACE INTO companies (symbol, market, is_active,"
                 " updated_at) VALUES (?, ?, 1, 'now')", (symbol, market))
    conn.commit()


class TestRebalanceDates:
    def test_picks_first_session_of_each_month(self):
        calendar = [f"2024-{m:02d}-{d:02d}" for m in range(1, 13)
                    for d in (1, 15)]
        points = mo.rebalance_dates(calendar, warmup=0)
        assert len(points) == 12
        assert all(ts.endswith("-01") for _, ts in points)

    def test_warmup_is_respected(self):
        calendar = [f"2024-{m:02d}-01" for m in range(1, 13)]
        # warmup=5 يعني أول فهرس مقبول هو 5 نفسه
        assert mo.rebalance_dates(calendar, warmup=5)[0][0] == 5
        assert mo.rebalance_dates(calendar, warmup=0)[0][0] == 0


class TestRanking:
    def test_stronger_stock_ranks_higher(self, conn):
        from tasi.sweep import load_series
        # تذبذب طفيف في الضعيف: سلسلة ثابتة تماماً يرفضها حارس الجودة
        # كشموع مكررة، وهو سلوك صحيح لا يُلتف عليه في الاختبار
        rising = [100.0 * (1.01 ** i) for i in range(200)]
        weak = [100.0 + (i % 3) * 0.1 for i in range(200)]
        index = [100.0 + (i % 5) * 0.05 for i in range(200)]
        seed(conn, "STRONG", rising)
        seed(conn, "WEAK", weak)
        seed(conn, "TASI", index)

        prepared = mo._prepare(conn, min_bars=100)
        bench = load_series(conn, "TASI")
        bench_index = {ts: i for i, ts in enumerate(bench["ts"])}
        ranked = mo.rank_at(prepared, bench, bench_index, bench["ts"][150],
                            lookback=100, min_turnover=0)
        assert ranked[0].symbol == "STRONG"
        assert ranked[0].score > ranked[-1].score

    def test_score_is_relative_to_the_index(self, conn):
        from tasi.sweep import load_series
        series = [100.0 * (1.01 ** i) for i in range(200)]
        seed(conn, "X", series)
        seed(conn, "TASI", list(series))
        prepared = mo._prepare(conn, min_bars=100)
        bench = load_series(conn, "TASI")
        bench_index = {ts: i for i, ts in enumerate(bench["ts"])}
        ranked = mo.rank_at(prepared, bench, bench_index, bench["ts"][150],
                            lookback=100, min_turnover=0)
        # يتحرك مع المؤشر تماماً، فقوته النسبية صفر
        assert ranked[0].score == pytest.approx(0.0, abs=1e-9)

    def test_illiquid_names_are_filtered_out(self, conn):
        from tasi.sweep import load_series
        rising = [100.0 * (1.01 ** i) for i in range(200)]
        seed(conn, "THIN", rising, volume=10)
        seed(conn, "TASI", [100.0 + (i % 5) * 0.05 for i in range(200)])
        prepared = mo._prepare(conn, min_bars=100)
        bench = load_series(conn, "TASI")
        bench_index = {ts: i for i, ts in enumerate(bench["ts"])}
        ranked = mo.rank_at(prepared, bench, bench_index, bench["ts"][150],
                            lookback=100, min_turnover=1e7)
        assert ranked == []

    def test_insufficient_history_yields_no_ranking(self, conn):
        from tasi.sweep import load_series
        seed(conn, "X", [100.0 + (i % 3) * 0.1 for i in range(200)])
        seed(conn, "TASI", [100.0 + (i % 5) * 0.05 for i in range(200)])
        prepared = mo._prepare(conn, min_bars=100)
        bench = load_series(conn, "TASI")
        bench_index = {ts: i for i, ts in enumerate(bench["ts"])}
        assert mo.rank_at(prepared, bench, bench_index, bench["ts"][5],
                          lookback=100, min_turnover=0) == []


class TestResultAccounting:
    def make(self, returns):
        result = mo.MomentumResult(lookback=120, hold=10, min_turnover=0,
                                   slippage=0.0)
        for i, value in enumerate(returns):
            result.rebalances.append(mo.Rebalance(
                date=f"2024-{1 + i % 12:02d}-01", picks=[],
                period_return=value, benchmark_return=0.0))
            result.equity *= 1 + value
        return result

    def test_equity_compounds(self):
        result = self.make([0.1, 0.1])
        assert result.total_return_pct == pytest.approx(21.0)

    def test_max_drawdown_measures_peak_to_trough(self):
        result = self.make([0.5, -0.5])
        assert result.max_drawdown_pct() == pytest.approx(50.0)

    def test_win_rate_counts_positive_periods(self):
        assert self.make([0.1, -0.1, 0.1]).win_rate() == pytest.approx(2 / 3)

    def test_worst_period_is_reported(self):
        assert self.make([0.1, -0.3, 0.05]).worst_period()[1] == pytest.approx(-0.3)

    def test_annualised_needs_a_full_year(self):
        assert self.make([0.01] * 6).annualised() is None
        assert self.make([0.01] * 24).annualised() is not None

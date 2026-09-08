"""اختبارات التشغيل الورقي - the integrity of an unedited record."""

import pytest

from tasi import db
from tasi import paper as pp


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(str(tmp_path / "paper.db"))
    pp.ensure_schema(connection)
    return connection


@pytest.fixture
def run_id(conn):
    return pp.create_run(conn, "test-run", "momentum", 1000.0)


def decision(symbol="X", price=100.0, rank=1):
    return pp.Decision(symbol=symbol, action="BUY", price_at_decision=price,
                       rank=rank, score=0.5, weight=0.1, rationale_ar="اختبار")


class TestRuns:
    def test_run_names_are_unique(self, conn, run_id):
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            pp.create_run(conn, "test-run", "momentum", 1000.0)

    def test_run_is_retrievable(self, conn, run_id):
        assert pp.get_run(conn, "test-run")["capital"] == 1000.0

    def test_unknown_run_is_none(self, conn):
        assert pp.get_run(conn, "nope") is None


class TestRecordIntegrity:
    """السجل لا يُعدَّل بعد معرفة النتيجة."""

    def test_decisions_are_written(self, conn, run_id):
        assert pp.record_decisions(conn, run_id, "2026-09-01",
                                   [decision("A"), decision("B", rank=2)]) == 2

    def test_rerecording_the_same_day_does_not_overwrite(self, conn, run_id):
        pp.record_decisions(conn, run_id, "2026-09-01", [decision("A", 100.0)])
        written = pp.record_decisions(conn, run_id, "2026-09-01",
                                      [decision("A", 999.0)])
        assert written == 0
        stored = conn.execute(
            "SELECT price_at_decision FROM paper_decisions").fetchone()[0]
        assert stored == 100.0        # السعر الأصلي محفوظ

    def test_same_symbol_on_a_later_date_is_a_new_decision(self, conn, run_id):
        pp.record_decisions(conn, run_id, "2026-09-01", [decision("A")])
        assert pp.record_decisions(conn, run_id, "2026-10-01", [decision("A")]) == 1

    def test_price_is_captured_at_decision_time(self, conn, run_id):
        pp.record_decisions(conn, run_id, "2026-09-01", [decision("A", 42.5)])
        assert conn.execute(
            "SELECT price_at_decision FROM paper_decisions").fetchone()[0] == 42.5


class TestOutcomes:
    def test_return_is_computed_not_supplied(self, conn, run_id):
        pp.record_decisions(conn, run_id, "2026-09-01", [decision("A", 100.0)])
        did = conn.execute("SELECT id FROM paper_decisions").fetchone()[0]
        result = pp.resolve(conn, did, exit_price=110.0)
        assert result["return_pct"] == pytest.approx(10.0)

    def test_excess_subtracts_the_benchmark(self, conn, run_id):
        pp.record_decisions(conn, run_id, "2026-09-01", [decision("A", 100.0)])
        did = conn.execute("SELECT id FROM paper_decisions").fetchone()[0]
        result = pp.resolve(conn, did, 110.0, benchmark_return_pct=4.0)
        assert result["excess_pct"] == pytest.approx(6.0)

    def test_losing_trade_is_recorded_as_negative(self, conn, run_id):
        pp.record_decisions(conn, run_id, "2026-09-01", [decision("A", 100.0)])
        did = conn.execute("SELECT id FROM paper_decisions").fetchone()[0]
        assert pp.resolve(conn, did, 80.0)["return_pct"] == pytest.approx(-20.0)

    def test_unknown_decision_raises(self, conn):
        with pytest.raises(ValueError):
            pp.resolve(conn, 9999, 100.0)

    def test_resolved_decisions_leave_the_open_queue(self, conn, run_id):
        pp.record_decisions(conn, run_id, "2026-09-01", [decision("A")])
        did = pp.open_decisions(conn, run_id)[0]["id"]
        pp.resolve(conn, did, 105.0)
        assert pp.open_decisions(conn, run_id) == []


class TestSummary:
    def test_counts_open_and_resolved(self, conn, run_id):
        pp.record_decisions(conn, run_id, "2026-09-01",
                            [decision("A"), decision("B", rank=2)])
        did = pp.open_decisions(conn, run_id)[0]["id"]
        pp.resolve(conn, did, 110.0)
        summary = pp.summarise(conn, "test-run")
        assert summary.decisions == 2
        assert summary.resolved == 1
        assert summary.open_decisions == 1

    def test_win_rate_and_mean_return(self, conn, run_id):
        pp.record_decisions(conn, run_id, "2026-09-01",
                            [decision("A", 100.0), decision("B", 100.0, rank=2)])
        ids = [r["id"] for r in pp.open_decisions(conn, run_id)]
        pp.resolve(conn, ids[0], 120.0)
        pp.resolve(conn, ids[1], 90.0)
        summary = pp.summarise(conn, "test-run")
        assert summary.win_rate == pytest.approx(0.5)
        assert summary.mean_return == pytest.approx(5.0)

    def test_summary_says_so_when_nothing_resolved(self, conn, run_id):
        pp.record_decisions(conn, run_id, "2026-09-01", [decision("A")])
        assert "لم تُقيَّم" in pp.summarise(conn, "test-run").render_ar()

    def test_currency_defaults_to_sar_when_market_unspecified(self, conn, run_id):
        summary = pp.summarise(conn, "test-run")
        assert summary.currency == "ريال"
        assert "ريال" in summary.render_ar()

    def test_us_market_run_reports_dollars(self, conn):
        # الخطأ الذي وُجد: التقرير كان يطبع "ريال" دائماً حتى لصفقات
        # أمريكية بالدولار، بصرف النظر عن سوق التشغيل الفعلي
        pp.create_run(conn, "us-run", "momentum", 1000.0,
                      params={"market": "US", "benchmark": "SPY"})
        summary = pp.summarise(conn, "us-run")
        assert summary.currency == "دولار"
        assert "دولار" in summary.render_ar()
        assert "ريال" not in summary.render_ar()

    def test_unknown_run_summarises_to_none(self, conn):
        assert pp.summarise(conn, "nope") is None

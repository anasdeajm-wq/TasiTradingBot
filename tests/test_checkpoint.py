"""
اختبارات تثبيت الحالة - the export/import round trip for irreplaceable state.

القاعدة المحروسة هنا: بيانات السوق (bars، fundamentals) قابلة لإعادة الجلب
مجاناً في أي وقت فلا تُثبَّت، بينما سجل التشغيل الورقي والتوقعات والتصنيف
الشرعي المستورد لا تُعاد أبداً بنفس القيمة إن فُقدت.
"""

import json

import pytest

from tasi import checkpoint as ck
from tasi import db
from tasi import paper as pp
from tasi import universe as u


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "src.db"))


class TestScope:
    def test_market_data_tables_are_excluded(self):
        assert "bars" not in ck.CRITICAL_TABLES
        assert "fundamentals" not in ck.CRITICAL_TABLES
        assert "ticks" not in ck.CRITICAL_TABLES

    def test_paper_trading_tables_are_included(self):
        assert "paper_decisions" in ck.CRITICAL_TABLES
        assert "paper_outcomes" in ck.CRITICAL_TABLES

    def test_shariah_status_is_included(self):
        assert "shariah_status" in ck.CRITICAL_TABLES


class TestExport:
    def test_empty_database_exports_zero_counts(self, conn, tmp_path):
        counts = ck.export_all(conn, str(tmp_path / "snap.json"))
        assert all(v == 0 for v in counts.values())

    def test_export_creates_parent_directories(self, conn, tmp_path):
        target = tmp_path / "nested" / "dir" / "snap.json"
        ck.export_all(conn, str(target))
        assert target.exists()

    def test_counts_match_actual_row_counts(self, conn, tmp_path):
        u.upsert_companies(conn, [u.Company("AAPL", name_en="Apple")])
        counts = ck.export_all(conn, str(tmp_path / "snap.json"))
        assert counts["companies"] == 1

    def test_output_is_valid_json_with_real_data(self, conn, tmp_path):
        u.upsert_companies(conn, [u.Company("2222")])
        u.set_shariah_status(conn, "2222", u.PURE, "مصدر اختبار", "2026-01-01")
        path = tmp_path / "snap.json"
        ck.export_all(conn, str(path))
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["shariah_status"][0]["symbol"] == "2222"


class TestRoundTrip:
    def test_shariah_status_survives_round_trip(self, conn, tmp_path):
        u.upsert_companies(conn, [u.Company("2222")])
        u.set_shariah_status(conn, "2222", u.PURE, "مصدر اختبار", "2026-01-01",
                             purification_rate=1.5)
        path = str(tmp_path / "snap.json")
        ck.export_all(conn, path)

        fresh = db.connect(str(tmp_path / "restored.db"))
        counts = ck.import_all(fresh, path)
        assert counts["shariah_status"] == 1
        status = u.get_shariah_status(fresh, "2222", source="مصدر اختبار")
        assert status["status"] == u.PURE
        assert status["purification_rate"] == pytest.approx(1.5)

    def test_paper_trading_record_survives_round_trip(self, conn, tmp_path):
        run_id = pp.create_run(conn, "test-run", "momentum", 1000.0)
        pp.record_decisions(conn, run_id, "2026-09-01", [
            pp.Decision(symbol="AAPL", action="BUY", price_at_decision=200.0,
                       rank=1, score=0.5, weight=1.0, rationale_ar="اختبار")])
        decision_id = pp.open_decisions(conn, run_id)[0]["id"]
        pp.resolve(conn, decision_id, 220.0)

        path = str(tmp_path / "snap.json")
        ck.export_all(conn, path)

        fresh = db.connect(str(tmp_path / "restored.db"))
        ck.import_all(fresh, path)
        summary = pp.summarise(fresh, "test-run")
        assert summary.decisions == 1
        assert summary.resolved == 1
        assert summary.mean_return == pytest.approx(10.0)

    def test_ignore_mode_does_not_clobber_existing_rows(self, conn, tmp_path):
        u.upsert_companies(conn, [u.Company("2222")])
        u.set_shariah_status(conn, "2222", u.PURE, "م", "2026-01-01")
        path = str(tmp_path / "snap.json")
        ck.export_all(conn, path)

        target = db.connect(str(tmp_path / "target.db"))
        u.upsert_companies(target, [u.Company("2222")])
        u.set_shariah_status(target, "2222", u.NON_COMPLIANT, "م", "2026-01-01")
        ck.import_all(target, path, mode="ignore")
        # السجل الموجود مسبقاً يبقى كما هو، لا يُستبدل بالمستورد
        status = u.get_shariah_status(target, "2222", source="م")
        assert status["status"] == u.NON_COMPLIANT

    def test_replace_mode_overwrites_existing_rows(self, conn, tmp_path):
        u.upsert_companies(conn, [u.Company("2222")])
        u.set_shariah_status(conn, "2222", u.PURE, "م", "2026-01-01")
        path = str(tmp_path / "snap.json")
        ck.export_all(conn, path)

        target = db.connect(str(tmp_path / "target.db"))
        u.upsert_companies(target, [u.Company("2222")])
        u.set_shariah_status(target, "2222", u.NON_COMPLIANT, "م", "2026-01-01")
        ck.import_all(target, path, mode="replace")
        status = u.get_shariah_status(target, "2222", source="م")
        assert status["status"] == u.PURE

    def test_import_is_a_pure_addition_to_market_data(self, conn, tmp_path):
        """التثبيت لا يمس جداول بيانات السوق أبداً - ليست جزءاً منه."""
        conn.execute(
            "INSERT INTO bars (symbol,interval,ts,close) VALUES ('X','1day','d',1.0)")
        conn.commit()
        path = str(tmp_path / "snap.json")
        ck.export_all(conn, path)
        assert "bars" not in json.loads(open(path, encoding="utf-8").read())

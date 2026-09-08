"""
اختبارات مزوّد أرقام (Argaam) - معيار العصيمي.

يحاكي نمط HTML الحقيقي المرصود فعلياً من صفحة
/ar/company/shariahcompaniesbyinstitution/{id}?marketid={id}: عمود مبلغ
تطهير رقمي، وعمود تصنيف نصّي قد يكون "نقية" حتى مع تطهير > 0، أو "-" عند
مؤسسات لا تفصح عن الدرجة (الراجحي/البلاد).
"""

import pytest

from tasi.providers import argaam_shariah as ag


class FakeSession:
    """جلسة وهمية: تُرجع HTML محدداً مسبقاً حسب (institution, marketid)."""

    def __init__(self, pages):
        self.pages = pages   # {(institution_id, market_id): html}

    def get(self, url, params=None, timeout=None):
        institution_id = int(url.rstrip("/").split("/")[-1])
        market_id = params["marketid"]

        class Resp:
            def __init__(self, text):
                self.text = text
            def raise_for_status(self):
                pass
        return Resp(self.pages.get((institution_id, market_id), ""))


def row_html(symbol, name, amount, label):
    return (f'<tr><td class="">{symbol}</td>'
           f'<td class="argaam-font"><a href="/x">{name}</a></td>'
           f'<td class="center">{amount}</td>'
           f'<td class="center">{label}</td></tr>')


OSAIMI_MAIN_HTML = (
    row_html("2350", "شركة كيان السعودية للبتروكيماويات", "0.0000", "نقية")
    + row_html("2310", "شركة الصحراء العالمية للبتروكيماويات", "0.0400", "نقية")
)
OSAIMI_NOMU_HTML = row_html("9548", "شركة صناعة البلاستيك العربية", "0.0300", "نقية")

# الراجحي/البلاد: قائمة "متوافق" دون تفصيل - عمود التصنيف "-" فعلياً في الموقع
ALRAJHI_MAIN_HTML = (
    row_html("2350", "شركة كيان السعودية للبتروكيماويات", "-", "-")
    + row_html("1140", "بنك البلاد", "-", "-")   # سهم لا يظهر عند العصيمي إطلاقاً
)


class TestParsing:
    def test_zero_purification_is_pure(self):
        session = FakeSession({(2, 3): OSAIMI_MAIN_HTML})
        results = ag.screen_osaimi(markets=["MAIN"], session=session)
        by_symbol = {r.symbol: r for r in results}
        assert by_symbol["2350"].purification_amount == 0.0

    def test_nonzero_purification_is_captured_even_when_labelled_pure(self):
        """نقطة دقيقة: العصيمي يسمي 2310 'نقية' نصياً رغم تطهير > 0."""
        session = FakeSession({(2, 3): OSAIMI_MAIN_HTML})
        results = ag.screen_osaimi(markets=["MAIN"], session=session)
        by_symbol = {r.symbol: r for r in results}
        assert by_symbol["2310"].purification_amount == pytest.approx(0.04)
        assert by_symbol["2310"].source_label == "نقية"

    def test_both_markets_are_fetched(self):
        session = FakeSession({(2, 3): OSAIMI_MAIN_HTML, (2, 14): OSAIMI_NOMU_HTML})
        results = ag.screen_osaimi(markets=["MAIN", "NOMU"], session=session)
        symbols = {r.symbol for r in results}
        assert symbols == {"2350", "2310", "9548"}
        markets = {r.symbol: r.market for r in results}
        assert markets["9548"] == "NOMU"

    def test_undisclosed_dash_rows_are_excluded_not_guessed(self):
        """صفحات الراجحي/البلاد تعرض '-' بدل رقم - لا نخترع مبلغ تطهير."""
        session = FakeSession({(1, 3): ALRAJHI_MAIN_HTML})
        symbols = ag.fetch_compliant_symbols("alrajhi", markets=["MAIN"], session=session)
        assert symbols == {"2350", "1140"}   # القائمة الخام صالحة كتوافق فقط

        # لكن screen_osaimi نفسه يستبعد أي صف بلا رقم فعلي
        results = ag.screen_osaimi(markets=["MAIN"], session=session)
        assert results == []   # لا صفوف عصيمي في هذا الـHTML


class TestImport:
    def test_zero_purification_status_is_pure(self, tmp_path):
        from tasi import db, universe as u
        conn = db.connect(str(tmp_path / "tasi.db"))
        result = ag.OsaimiResult("2350", "كيان", "MAIN", 0.0, "نقية")
        counts = ag.import_into(conn, [result], as_of="2025-08-17")
        assert counts[u.PURE] == 1
        status = u.get_shariah_status(conn, "2350", source=ag.SOURCE_NAME)
        assert status["status"] == u.PURE

    def test_nonzero_purification_status_is_mixed_despite_pure_label(self, tmp_path):
        """الحسم من الرقم لا من التسمية - جوهر هذا المزوّد."""
        from tasi import db, universe as u
        conn = db.connect(str(tmp_path / "tasi.db"))
        result = ag.OsaimiResult("2310", "الصحراء", "MAIN", 0.04, "نقية")
        counts = ag.import_into(conn, [result], as_of="2025-08-17")
        assert counts[u.MIXED] == 1
        status = u.get_shariah_status(conn, "2310", source=ag.SOURCE_NAME)
        assert status["status"] == u.MIXED
        note = conn.execute(
            "SELECT notes FROM shariah_status WHERE symbol='2310'").fetchone()[0]
        assert "0.0400" in note
        assert "نقية" in note   # التسمية الأصلية محفوظة رغم الحسم المخالف لها

    def test_import_creates_missing_company_with_correct_market(self, tmp_path):
        from tasi import db
        conn = db.connect(str(tmp_path / "tasi.db"))
        ag.import_into(conn, [ag.OsaimiResult("9548", "بلاستيك", "NOMU", 0.03, "نقية")],
                       as_of="2025-08-17")
        row = conn.execute(
            "SELECT market FROM companies WHERE symbol='9548'").fetchone()
        assert row["market"] == "NOMU"

    def test_corroboration_is_noted_but_does_not_change_status(self, tmp_path):
        from tasi import db, universe as u
        conn = db.connect(str(tmp_path / "tasi.db"))
        result = ag.OsaimiResult("2350", "كيان", "MAIN", 0.0, "نقية")
        ag.import_into(conn, [result], as_of="2025-08-17",
                       corroboration={"alrajhi": {"2350"}, "albilad": set()})
        status = u.get_shariah_status(conn, "2350", source=ag.SOURCE_NAME)
        assert status["status"] == u.PURE
        note = conn.execute(
            "SELECT notes FROM shariah_status WHERE symbol='2350'").fetchone()[0]
        assert "الراجحي المالية" in note
        assert "البلاد المالية" not in note

    def test_existing_company_row_is_not_overwritten(self, tmp_path):
        from tasi import db, universe as u
        conn = db.connect(str(tmp_path / "tasi.db"))
        u.upsert_companies(conn, [u.Company(symbol="2350", name_ar="الاسم الرسمي",
                                            market="MAIN")])
        ag.import_into(conn, [ag.OsaimiResult("2350", "اسم مختلف من أرقام", "MAIN",
                                              0.0, "نقية")], as_of="2025-08-17")
        row = conn.execute(
            "SELECT name_ar FROM companies WHERE symbol='2350'").fetchone()
        assert row["name_ar"] == "الاسم الرسمي"   # INSERT OR IGNORE لا يستبدل

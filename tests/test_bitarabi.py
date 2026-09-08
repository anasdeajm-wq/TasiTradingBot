"""
اختبارات مزوّد بيت العربي - the trustworthiness gate, not live network calls.

هذه الاختبارات تحاكي أنماط HTML الحقيقية الثلاثة التي وُجدت أثناء الفحص:
جدول نِسب فعلي (AAPL)، تعليل نشاطي (JPM)، ونص عام متناقض (تويوتا). القياس
المهم هو أن الحارس يقبل الأول والثاني ويرفض الثالث.
"""

import pytest

from tasi.providers import bitarabi as ba


class FakeSession:
    """جلسة وهمية تُرجع HTML محدداً مسبقاً بدل الشبكة."""

    def __init__(self, pages_html=None, detail_html=None):
        self.pages_html = pages_html or {}
        self.detail_html = detail_html or {}

    def get(self, url, params=None, timeout=None):
        class Resp:
            def __init__(self, text):
                self.text = text
        if params and "page" in params:
            return Resp(self.pages_html.get(params["page"], ""))
        return Resp(self.detail_html.get(url, ""))


def card_html(symbol, name, badge, badge_txt, url="https://bitarabi.com/stock/x"):
    return (f'<a href="{url}" class="sp-card">'
           f'<div class="sp-card__glow"></div>'
           f'<span class="sp-card__badge sp-card__badge--{badge}">{badge_txt}</span>'
           f'<h3 class="sp-card__sym">{symbol}</h3>'
           f'<span class="sp-card__name">{name}</span></a>')


RATIO_TABLE_HTML = '''
<table class="sv2-sharia-ratios">
<caption>النِسب المالية الفعلية لسهم AAPL، مقيسة لا مقدَّرة</caption>
<tbody><tr><th>نسبة الديون إلى القيمة السوقية</th>
<td class="sv2-sr-num">2.69%</td></tr></tbody>
</table>
'''

ACTIVITY_HTML = '''
<p>شركة JPMorgan Chase & Co تنتمي إلى قطاع FINANCIAL SERVICES
وصناعة BANKS - DIVERSIFIED، وهذا يعني أن نشاطها الأساسي هو الأعمال
المصرفية. الحكم النهائي: حرام</p>
'''

CONTRADICTORY_HTML = '''
<p>الرأي النهائي: مختلط</p>
<p>لم نُصدر حكماً شرعياً لهذا السهم بعد لغياب بيانات موثقة.</p>
'''


class TestVerify:
    def test_ratio_table_is_trustworthy(self):
        card = ("https://bitarabi.com/stock/aapl", "halal", "حلال", "AAPL", "Apple")
        session = FakeSession(detail_html={card[0]: RATIO_TABLE_HTML})
        result = ba._verify(session, card)
        assert result.trustworthy
        assert result.debt_ratio == pytest.approx(2.69)

    def test_activity_reasoning_is_trustworthy(self):
        card = ("https://bitarabi.com/stock/jpm", "haram", "حرام", "JPM", "JPMorgan")
        session = FakeSession(detail_html={card[0]: ACTIVITY_HTML})
        result = ba._verify(session, card)
        assert result.trustworthy
        assert result.debt_ratio is None

    def test_contradictory_disclaimer_is_rejected(self):
        """حالة تويوتا: 'رأي نهائي' بينما تعترف الصفحة بغياب الفحص."""
        card = ("https://bitarabi.com/stock/tm", "mix", "مختلط", "TM", "Toyota")
        session = FakeSession(detail_html={card[0]: CONTRADICTORY_HTML})
        result = ba._verify(session, card)
        assert not result.trustworthy

    def test_empty_page_is_rejected_not_guessed(self):
        card = ("https://bitarabi.com/stock/x", "haram", "حرام", "X", "Unknown")
        session = FakeSession(detail_html={card[0]: ""})
        assert not ba._verify(session, card).trustworthy

    def test_network_failure_is_rejected_not_assumed_true(self):
        class BrokenSession:
            def get(self, *a, **k):
                import requests
                raise requests.RequestException("boom")
        card = ("https://bitarabi.com/stock/x", "halal", "حلال", "X", "Name")
        assert not ba._verify(BrokenSession(), card).trustworthy


class TestScreenAll:
    def test_unknown_badge_is_excluded_before_verification(self):
        page1 = card_html("A", "Alpha", "unknown", "غير محدد", "https://bitarabi.com/stock/a")
        session = FakeSession(pages_html={1: page1}, detail_html={})
        results = ba.screen_all(pages=1, workers=1)

    def test_only_trustworthy_results_are_returned(self, monkeypatch):
        page1 = (card_html("AAPL", "Apple", "halal", "حلال", "https://bitarabi.com/stock/aapl")
                + card_html("TM", "Toyota", "mix", "مختلط", "https://bitarabi.com/stock/tm"))
        session = FakeSession(
            pages_html={1: page1},
            detail_html={
                "https://bitarabi.com/stock/aapl": RATIO_TABLE_HTML,
                "https://bitarabi.com/stock/tm": CONTRADICTORY_HTML,
            })
        monkeypatch.setattr(ba, "_session", lambda: session)
        results = ba.screen_all(pages=1, workers=1)
        symbols = {r.symbol for r in results}
        assert symbols == {"AAPL"}


class TestImport:
    def test_import_creates_missing_company(self, tmp_path):
        from tasi import db, universe as u
        conn = db.connect(str(tmp_path / "us.db"))
        result = ba.ScreenResult("NEWCO", "New Company", "halal", True, 5.0)
        counts = ba.import_into(conn, [result], as_of="2026-09-08")
        assert counts[u.PURE] == 1
        status = u.get_shariah_status(conn, "NEWCO", source=ba.SOURCE_NAME)
        assert status["status"] == u.PURE

    def test_debt_ratio_is_recorded_in_notes(self, tmp_path):
        from tasi import db
        conn = db.connect(str(tmp_path / "us.db"))
        ba.import_into(conn, [ba.ScreenResult("X", "X Co", "mix", True, 61.0)],
                       as_of="2026-09-08")
        note = conn.execute(
            "SELECT notes FROM shariah_status WHERE symbol='X'").fetchone()[0]
        assert "61.0" in note

    def test_status_mapping_is_correct(self, tmp_path):
        from tasi import db, universe as u
        conn = db.connect(str(tmp_path / "us.db"))
        results = [ba.ScreenResult("A", "A", "halal", True),
                  ba.ScreenResult("B", "B", "mix", True),
                  ba.ScreenResult("C", "C", "haram", True)]
        ba.import_into(conn, results, as_of="2026-09-08")
        assert u.get_shariah_status(conn, "A", source=ba.SOURCE_NAME)["status"] == u.PURE
        assert u.get_shariah_status(conn, "B", source=ba.SOURCE_NAME)["status"] == u.MIXED
        assert u.get_shariah_status(conn, "C", source=ba.SOURCE_NAME)["status"] == u.NON_COMPLIANT

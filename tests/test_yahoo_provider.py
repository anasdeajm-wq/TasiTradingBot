"""
اختبارات تحويل الرموز في مزوّد ياهو.

السبب المباشر لهذا الملف: `to_yahoo` كانت تلحق ".SR" بأي رمز غير TASI
بلا شرط، فتحوّلت "AMD" إلى "AMD.SR" غير الموجود، وفشل جلب الأسعار
للتشغيل الورقي الأمريكي بخطأ "رمز غير موجود". القاعدة الصحيحة: اللاحقة
تُلحق فقط برموز تداول الرقمية البحتة، لا برموز أي سوق آخر.
"""

import pytest

from tasi.providers.yahoo import from_yahoo, to_yahoo


class TestToYahoo:
    def test_numeric_tadawul_symbol_gets_sr_suffix(self):
        assert to_yahoo("2222") == "2222.SR"

    def test_symbol_already_suffixed_stays_correct(self):
        assert to_yahoo("2222.SR") == "2222.SR"

    def test_colon_form_is_normalised(self):
        assert to_yahoo("2222:XSAU") == "2222.SR"

    def test_tasi_maps_to_index_symbol(self):
        assert to_yahoo("TASI") == "^TASI.SR"
        assert to_yahoo("^TASI") == "^TASI.SR"

    def test_caret_prefixed_symbol_passes_through(self):
        assert to_yahoo("^GSPC") == "^GSPC"

    def test_us_alphabetic_ticker_gets_no_suffix(self):
        # هذا هو الخلل الذي كُشف: AMD كانت تتحول إلى AMD.SR
        assert to_yahoo("AMD") == "AMD"
        assert to_yahoo("AAPL") == "AAPL"
        assert to_yahoo("SPY") == "SPY"

    def test_hyphenated_us_ticker_passes_through(self):
        assert to_yahoo("BRK-B") == "BRK-B"

    def test_lowercase_input_is_normalised_to_upper(self):
        assert to_yahoo("amd") == "AMD"


class TestFromYahoo:
    def test_strips_sr_suffix(self):
        assert from_yahoo("2222.SR") == "2222"

    def test_index_symbol_maps_back_to_tasi(self):
        assert from_yahoo("^TASI.SR") == "TASI"

    def test_us_ticker_is_unchanged(self):
        assert from_yahoo("AMD") == "AMD"
        assert from_yahoo("BRK-B") == "BRK-B"


class TestRoundTrip:
    @pytest.mark.parametrize("symbol", ["2222", "AMD", "AAPL", "BRK-B"])
    def test_us_and_tadawul_symbols_round_trip(self, symbol):
        assert from_yahoo(to_yahoo(symbol)) == symbol

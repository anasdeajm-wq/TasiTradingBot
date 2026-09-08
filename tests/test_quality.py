"""اختبارات فحص الجودة - the gate that keeps corrupt data out of decisions."""

import pytest

from tasi.quality import check_bars


def _dates(n):
    """تواريخ متتابعة صحيحة وقابلة للترتيب أبجدياً."""
    from datetime import date, timedelta
    start = date(2024, 1, 1)
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def bars(n=10, **overrides):
    data = {
        "ts": _dates(n),
        "open": [10 + i * 0.1 for i in range(n)],
        "high": [10.5 + i * 0.1 for i in range(n)],
        "low": [9.5 + i * 0.1 for i in range(n)],
        "close": [10.2 + i * 0.1 for i in range(n)],
        "volume": [1e6 + i for i in range(n)],
    }
    data.update(overrides)
    return data


class TestOrdering:
    def test_clean_series_passes(self):
        assert check_bars("A", bars()).ok

    def test_unsorted_timestamps_rejected(self):
        data = bars()
        data["ts"] = data["ts"][::-1]
        report = check_bars("A", data)
        assert not report.ok
        assert "مرتّب" in report.errors[0]

    def test_duplicate_timestamps_rejected(self):
        data = bars()
        data["ts"][3] = data["ts"][2]
        assert not check_bars("A", data).ok


class TestPriceSanity:
    def test_high_below_low_rejected(self):
        data = bars(n=3)
        data["high"][1], data["low"][1] = 9.0, 11.0
        assert not check_bars("A", data).ok

    def test_zero_price_rejected(self):
        data = bars(n=3)
        data["close"][1] = 0.0
        assert not check_bars("A", data).ok

    def test_impossible_jump_rejected(self):
        data = bars(n=3)
        data["close"][2] = data["close"][1] * 2.5
        report = check_bars("A", data)
        assert not report.ok
        assert report.suspicious_dates

    def test_move_within_daily_limit_only_warns(self):
        data = bars(n=3)
        data["close"][2] = data["close"][1] * 1.12    # ١٢٪ فوق حد السوق الرئيسية
        report = check_bars("A", data)
        assert report.ok
        assert report.warnings


class TestDuplicateBars:
    def test_many_identical_bars_rejected(self):
        data = bars(n=10)
        for i in (3, 5, 7):
            for key in ("open", "high", "low", "close", "volume"):
                data[key][i] = data[key][i - 1]
        assert not check_bars("A", data).ok

    def test_a_single_duplicate_in_a_long_series_only_warns(self):
        data = bars(n=200)
        for key in ("open", "high", "low", "close", "volume"):
            data[key][100] = data[key][99]
        report = check_bars("A", data)
        assert report.ok
        assert any("مكررة" in w for w in report.warnings)


class TestEdgeCases:
    def test_empty_series_rejected(self):
        assert not check_bars("A", {"ts": [], "close": []}).ok

    def test_mismatched_lengths_rejected(self):
        data = bars(n=5)
        data["close"] = data["close"][:3]
        assert not check_bars("A", data).ok

    def test_mostly_zero_volume_warns(self):
        data = bars(n=20)
        data["volume"] = [0.0] * 15 + [1e6] * 5
        report = check_bars("A", data)
        assert any("صفري" in w for w in report.warnings)

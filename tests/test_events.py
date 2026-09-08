"""اختبارات دراسة الأحداث - abnormal return measurement."""

import pytest

from tasi import db
from tasi import events as ev


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "ev.db"))


def seed(conn, symbol, closes, start_index=0):
    from datetime import date, timedelta
    day = date(2024, 1, 1)
    rows = []
    for i, close in enumerate(closes):
        rows.append((symbol, "1day", (day + timedelta(days=i)).isoformat(),
                     close, close * 1.01, close * 0.99, close, 1e6, None, None))
    conn.executemany(
        "INSERT OR REPLACE INTO bars (symbol, interval, ts, open, high, low,"
        " close, volume, turnover, trades) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return [r[2] for r in rows]


class TestStorage:
    def test_events_round_trip(self, conn):
        ev.store_events(conn, [ev.Event("2222", ev.DIVIDEND, "2024-03-01", 1.5)])
        loaded = ev.load_events(conn, ev.DIVIDEND)
        assert len(loaded) == 1
        assert loaded[0].symbol == "2222" and loaded[0].amount == 1.5

    def test_duplicates_are_ignored(self, conn):
        event = ev.Event("2222", ev.DIVIDEND, "2024-03-01", 1.5)
        ev.store_events(conn, [event])
        ev.store_events(conn, [event])
        assert len(ev.load_events(conn, ev.DIVIDEND)) == 1

    def test_kinds_are_separated(self, conn):
        ev.store_events(conn, [
            ev.Event("2222", ev.DIVIDEND, "2024-03-01", 1.5),
            ev.Event("2222", ev.SPLIT, "2024-04-01"),
        ])
        assert len(ev.load_events(conn, ev.DIVIDEND)) == 1
        assert len(ev.load_events(conn, ev.SPLIT)) == 1


class TestStudy:
    def test_abnormal_return_subtracts_the_index(self, conn):
        # السهم والمؤشر يتحركان بنفس النسبة تماماً: العائد غير العادي صفر
        prices = [100.0 * (1.01 ** i) for i in range(40)]
        stamps = seed(conn, "X", prices)
        seed(conn, "TASI", prices)
        study = ev.study(conn, [ev.Event("X", ev.DIVIDEND, stamps[20], 1.0)],
                         before=3, after=3, min_events=1)
        for window in study.windows:
            assert window.mean_abnormal == pytest.approx(0.0, abs=1e-9)

    def test_outperformance_shows_as_positive_abnormal(self, conn):
        stock = [100.0 * (1.02 ** i) for i in range(40)]
        index = [100.0 * (1.01 ** i) for i in range(40)]
        stamps = seed(conn, "X", stock)
        seed(conn, "TASI", index)
        study = ev.study(conn, [ev.Event("X", ev.DIVIDEND, stamps[20], 1.0)],
                         before=3, after=3, min_events=1)
        assert all(w.mean_abnormal > 0 for w in study.windows)

    def test_events_too_close_to_series_edges_are_skipped(self, conn):
        prices = [100.0] * 40
        stamps = seed(conn, "X", prices)
        seed(conn, "TASI", prices)
        study = ev.study(conn, [ev.Event("X", ev.DIVIDEND, stamps[1], 1.0)],
                         before=10, after=10, min_events=1)
        assert study.events_studied == 0

    def test_window_covers_the_requested_offsets(self, conn):
        prices = [100.0 * (1.005 ** i) for i in range(60)]
        stamps = seed(conn, "X", prices)
        seed(conn, "TASI", [100.0] * 60)
        study = ev.study(conn, [ev.Event("X", ev.DIVIDEND, stamps[30], 1.0)],
                         before=5, after=5, min_events=1)
        assert [w.offset for w in study.windows] == list(range(-5, 6))


class TestSignificance:
    def test_thin_sample_is_never_significant(self):
        window = ev.WindowStats(offset=0, count=5, mean_abnormal=2.0,
                                std=0.1, t_stat=40.0, positive_share=1.0)
        assert not window.significant

    def test_large_sample_with_strong_t_is_significant(self):
        window = ev.WindowStats(offset=0, count=500, mean_abnormal=0.5,
                                std=1.0, t_stat=11.0, positive_share=0.6)
        assert window.significant

    def test_weak_t_is_not_significant(self):
        window = ev.WindowStats(offset=0, count=500, mean_abnormal=0.05,
                                std=1.0, t_stat=1.1, positive_share=0.51)
        assert not window.significant

    def test_verdict_refuses_to_conclude_from_few_events(self):
        study = ev.EventStudy(kind=ev.DIVIDEND, events_studied=5)
        assert "لا يكفي" in study.verdict_ar()

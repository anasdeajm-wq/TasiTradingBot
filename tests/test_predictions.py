"""اختبارات حلقة التوقعات - scoring, calibration, and failure attribution."""

import pytest

from tasi import db
from tasi import predictions as P


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "pred.db"))


def log(conn, direction=P.UP, predicted=1.0, confidence=0.7, regime="TREND_UP"):
    return P.log_prediction(conn, P.Prediction(
        scope="SYMBOL", target="2222", horizon="NEXT_SESSION",
        direction=direction, predicted_pct=predicted, low_band=predicted - 1,
        high_band=predicted + 1, confidence=confidence, regime=regime))


class TestLogging:
    def test_rejects_unknown_horizon(self, conn):
        with pytest.raises(ValueError):
            P.log_prediction(conn, P.Prediction(
                scope="SYMBOL", target="X", horizon="SOMEDAY", direction=P.UP))

    def test_rejects_confidence_outside_zero_to_one(self, conn):
        with pytest.raises(ValueError):
            P.log_prediction(conn, P.Prediction(
                scope="SYMBOL", target="X", horizon="1W", direction=P.UP,
                confidence=1.4))

    def test_stores_features_for_later_attribution(self, conn):
        pid = P.log_prediction(conn, P.Prediction(
            scope="SYMBOL", target="X", horizon="1W", direction=P.UP,
            features={"rsi": 61.5}))
        stored = conn.execute("SELECT features FROM predictions WHERE id=?",
                              (pid,)).fetchone()[0]
        assert "61.5" in stored


class TestDirection:
    def test_small_move_is_flat_not_a_direction(self):
        assert P.direction_of(0.1) == P.FLAT
        assert P.direction_of(-0.1) == P.FLAT

    def test_clear_moves_are_directional(self):
        assert P.direction_of(1.5) == P.UP
        assert P.direction_of(-1.5) == P.DOWN


class TestResolution:
    def test_correct_call_is_scored_correct(self, conn):
        result = P.resolve_prediction(conn, log(conn), actual_pct=1.2)
        assert result["correct"] is True
        assert result["failure_cause"] is None

    def test_wrong_call_gets_a_failure_cause(self, conn):
        result = P.resolve_prediction(conn, log(conn), actual_pct=-2.0)
        assert result["correct"] is False
        assert result["failure_cause"] is not None

    def test_within_band_is_tracked(self, conn):
        assert P.resolve_prediction(conn, log(conn), 1.5)["within_band"] == 1
        assert P.resolve_prediction(conn, log(conn), 9.0)["within_band"] == 0

    def test_resolving_twice_updates_rather_than_duplicates(self, conn):
        pid = log(conn)
        P.resolve_prediction(conn, pid, 1.0)
        P.resolve_prediction(conn, pid, -1.0)
        count = conn.execute(
            "SELECT COUNT(*) FROM prediction_results WHERE prediction_id=?",
            (pid,)).fetchone()[0]
        assert count == 1

    def test_unknown_id_raises(self, conn):
        with pytest.raises(ValueError):
            P.resolve_prediction(conn, 9999, 1.0)


class TestFailureAttribution:
    """الأسباب الخارجية تُفحص قبل إلقاء اللوم على النموذج."""

    def test_news_outranks_everything(self):
        cause, _ = P.classify_failure(1.0, -3.0, 0.9, "TREND_UP", "RISK_OFF",
                                      volume_ratio=0.1, had_news=True)
        assert cause == P.NEWS_SHOCK

    def test_regime_shift_outranks_model_blame(self):
        cause, _ = P.classify_failure(1.0, -2.0, 0.5, "TREND_UP", "RISK_OFF")
        assert cause == P.REGIME_SHIFT

    def test_thin_liquidity_recognised(self):
        cause, _ = P.classify_failure(1.0, -2.0, 0.5, "RANGE", "RANGE",
                                      volume_ratio=0.3)
        assert cause == P.LOW_LIQUIDITY

    def test_high_confidence_large_error_is_overconfidence(self):
        cause, _ = P.classify_failure(1.0, -3.0, 0.9, "RANGE", "RANGE")
        assert cause == P.OVERCONFIDENCE

    def test_small_error_is_normal_variance(self):
        cause, _ = P.classify_failure(1.0, 0.4, 0.5, "RANGE", "RANGE")
        assert cause == P.NORMAL_VARIANCE

    def test_unexplained_large_error_lands_on_the_model(self):
        cause, _ = P.classify_failure(1.0, -4.0, 0.5, "RANGE", "RANGE")
        assert cause == P.MODEL_BIAS


class TestCalibration:
    def test_overconfident_system_scores_badly_on_brier(self, conn):
        # يعلن ٩٠٪ ويصيب ٥٠٪
        for i in range(40):
            pid = log(conn, confidence=0.9)
            P.resolve_prediction(conn, pid, 1.0 if i % 2 == 0 else -1.0)
        report = P.accuracy_report(conn)
        assert report["hit_rate"] == pytest.approx(0.5)
        assert report["brier_score"] > 0.25

    def test_honest_system_scores_well_on_brier(self, conn):
        # يعلن ٥٠٪ ويصيب ٥٠٪
        for i in range(40):
            pid = log(conn, confidence=0.5)
            P.resolve_prediction(conn, pid, 1.0 if i % 2 == 0 else -1.0)
        assert P.accuracy_report(conn)["brier_score"] == pytest.approx(0.25)

    def test_report_is_explicit_when_nothing_resolved(self, conn):
        assert P.accuracy_report(conn)["resolved"] == 0

    def test_lessons_flag_overconfidence(self, conn):
        for i in range(40):
            pid = log(conn, confidence=0.9)
            P.resolve_prediction(conn, pid, 1.0 if i % 4 == 0 else -1.0)
        assert any("ثقة" in lesson for lesson in P.lessons(conn))

    def test_lessons_refuse_to_generalise_from_a_thin_sample(self, conn):
        P.resolve_prediction(conn, log(conn), 1.0)
        assert "قليل" in P.lessons(conn)[0]


class TestDueQueue:
    def test_future_predictions_are_not_due(self, conn):
        P.log_prediction(conn, P.Prediction(
            scope="MARKET", target="TASI", horizon="1W", direction=P.UP,
            made_at="2026-09-07T10:00:00", resolve_at="2030-01-01"))
        assert P.due_predictions(conn, as_of="2026-09-10") == []

    def test_resolved_predictions_leave_the_queue(self, conn):
        P.log_prediction(conn, P.Prediction(
            scope="MARKET", target="TASI", horizon="1W", direction=P.UP,
            made_at="2026-01-01T10:00:00", resolve_at="2026-01-08"))
        due = P.due_predictions(conn, as_of="2026-09-10")
        assert len(due) == 1
        P.resolve_prediction(conn, int(due[0]["id"]), 1.0)
        assert P.due_predictions(conn, as_of="2026-09-10") == []

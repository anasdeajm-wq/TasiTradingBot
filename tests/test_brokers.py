"""اختبارات طبقة التنفيذ - DryRunBroker يجب ألا ينفّذ شيئاً فعلياً أبداً."""

from tasi.brokers.base import DryRunBroker


class TestDryRunBroker:
    def test_is_never_connected(self):
        assert DryRunBroker().is_connected() is False

    def test_has_no_positions_and_no_cash(self):
        broker = DryRunBroker()
        assert broker.get_positions() == []
        assert broker.get_cash("SAR") == 0.0
        assert broker.get_cash("USD") == 0.0

    def test_order_is_logged_but_never_accepted(self):
        broker = DryRunBroker()
        result = broker.place_market_order("2222", 10, "BUY")
        assert result.accepted is False
        assert result.broker_order_id is None
        assert "2222" in result.message
        assert len(broker._log) == 1

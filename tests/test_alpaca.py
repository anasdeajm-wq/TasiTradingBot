"""
اختبارات وسيط Alpaca - جلسة وهمية لا شبكة حقيقية، تحاكي استجابات REST
الفعلية الموثّقة (account/positions/orders).
"""

import pytest

from tasi.brokers.alpaca import AlpacaBroker


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(payload)

    def json(self):
        return self._payload


class FakeSession:
    """يحاكي requests.Session: get/post مضبوطة مسبقاً حسب المسار."""

    def __init__(self, account=None, positions=None, order_response=None,
                order_status=200):
        self.headers = {}
        self.account = account if account is not None else {"status": "ACTIVE", "cash": "500.0"}
        self.positions = positions or []
        self.order_response = order_response or {"id": "abc123"}
        self.order_status = order_status
        self.last_order_payload = None

    def get(self, url, timeout=None):
        if url.endswith("/account"):
            return FakeResponse(200, self.account)
        if url.endswith("/positions"):
            return FakeResponse(200, self.positions)
        return FakeResponse(404, {})

    def post(self, url, json=None, timeout=None):
        self.last_order_payload = json
        return FakeResponse(self.order_status, self.order_response)


def broker_with(session) -> AlpacaBroker:
    broker = AlpacaBroker("key", "secret", paper=True)
    broker._session = session
    return broker


class TestConnection:
    def test_active_account_is_connected(self):
        broker = broker_with(FakeSession(account={"status": "ACTIVE", "cash": "0"}))
        assert broker.is_connected() is True

    def test_inactive_account_is_not_connected(self):
        broker = broker_with(FakeSession(account={"status": "SUBMITTED", "cash": "0"}))
        assert broker.is_connected() is False

    def test_network_failure_is_not_connected(self):
        class BrokenSession:
            headers = {}
            def get(self, *a, **k):
                raise __import__("requests").RequestException("boom")
        broker = broker_with(BrokenSession())
        assert broker.is_connected() is False


class TestPositionsAndCash:
    def test_positions_are_parsed(self):
        session = FakeSession(positions=[
            {"symbol": "AMD", "qty": "1.5", "avg_entry_price": "150.0"},
        ])
        broker = broker_with(session)
        positions = broker.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "AMD"
        assert positions[0].quantity == pytest.approx(1.5)
        assert positions[0].currency == "USD"

    def test_cash_is_read_from_account(self):
        broker = broker_with(FakeSession(account={"status": "ACTIVE", "cash": "266.67"}))
        assert broker.get_cash("USD") == pytest.approx(266.67)

    def test_non_usd_currency_returns_zero(self):
        broker = broker_with(FakeSession())
        assert broker.get_cash("SAR") == 0.0


class TestOrders:
    def test_fractional_quantity_is_sent_as_is(self):
        session = FakeSession()
        broker = broker_with(session)
        result = broker.place_market_order("AMD", 0.558, "BUY")
        assert result.accepted is True
        assert session.last_order_payload["qty"] == "0.558"
        assert session.last_order_payload["side"] == "buy"
        assert session.last_order_payload["type"] == "market"

    def test_disconnected_account_refuses_order_locally(self):
        session = FakeSession(account={"status": "SUBMITTED", "cash": "0"})
        broker = broker_with(session)
        result = broker.place_market_order("AMD", 1, "BUY")
        assert result.accepted is False
        assert session.last_order_payload is None   # لم يُرسَل أي طلب فعلاً

    def test_rejected_order_is_reported_not_silently_dropped(self):
        session = FakeSession(order_status=422,
                              order_response={"message": "insufficient buying power"})
        broker = broker_with(session)
        result = broker.place_market_order("AMD", 100, "BUY")
        assert result.accepted is False
        assert "422" in result.message

    def test_accepted_order_returns_broker_order_id(self):
        session = FakeSession(order_response={"id": "xyz-789"})
        broker = broker_with(session)
        result = broker.place_market_order("INTC", 2, "BUY")
        assert result.broker_order_id == "xyz-789"

    def test_paper_flag_is_reflected_in_message(self):
        broker = broker_with(FakeSession())
        assert "ورقي" in broker.place_market_order("INTC", 1, "BUY").message

        live_broker = AlpacaBroker("key", "secret", paper=False)
        live_broker._session = FakeSession()
        assert "حقيقي" in live_broker.place_market_order("INTC", 1, "BUY").message


class TestBaseUrls:
    def test_paper_uses_paper_endpoint(self):
        broker = AlpacaBroker("k", "s", paper=True)
        assert "paper-api" in broker.base_url

    def test_live_uses_live_endpoint(self):
        broker = AlpacaBroker("k", "s", paper=False)
        assert "paper-api" not in broker.base_url
        assert "api.alpaca.markets" in broker.base_url

"""اختبارات إعادة التوازن الآلي - وسيط وهمي يسجّل الأوامر دون شبكة."""

from tasi.brokers.base import BrokerPosition, OrderResult
from tasi.execute import execute_rebalance, plan_rebalance


class FakeBroker:
    def __init__(self, positions, cash=10000.0):
        self._positions = positions
        self._cash = cash
        self.orders = []   # (symbol, qty, side)

    def get_positions(self):
        return self._positions

    def get_cash(self, currency="USD"):
        return self._cash

    def place_market_order(self, symbol, quantity, side):
        self.orders.append((symbol, quantity, side))
        return OrderResult(True, f"ord-{symbol}", f"{side} {quantity} {symbol}")


def pos(symbol, qty=1.0, cost=100.0):
    return BrokerPosition(symbol=symbol, quantity=qty, avg_cost=cost, currency="USD")


class TestPlanRebalance:
    def test_held_and_targeted_needs_no_action(self):
        plan = plan_rebalance(["AMD"], ["AMD"])
        assert plan.sells == []
        assert plan.buys == []

    def test_held_but_not_targeted_is_sold(self):
        plan = plan_rebalance(["AMD", "INTC"], ["AMD"])
        assert plan.sells == ["INTC"]
        assert plan.buys == []

    def test_targeted_but_not_held_is_bought(self):
        plan = plan_rebalance(["AMD"], ["AMD", "PANW"])
        assert plan.sells == []
        assert plan.buys == ["PANW"]


class TestExecuteRebalance:
    def test_sells_positions_no_longer_targeted(self):
        broker = FakeBroker([pos("WDC", qty=2.0)])
        execute_rebalance(broker, ["AMD"], {"AMD": 500.0})
        sell_orders = [o for o in broker.orders if o[2] == "SELL"]
        assert sell_orders == [("WDC", 2.0, "SELL")]

    def test_buys_new_targets_with_equal_weight(self):
        broker = FakeBroker([], cash=1000.0)
        execute_rebalance(broker, ["AMD", "INTC"], {"AMD": 500.0, "INTC": 100.0})
        buy_orders = {o[0]: o[1] for o in broker.orders if o[2] == "BUY"}
        assert buy_orders["AMD"] == 1.0     # 500 ميزانية / 500 سعر
        assert buy_orders["INTC"] == 5.0    # 500 ميزانية / 100 سعر

    def test_already_held_target_is_not_rebought(self):
        broker = FakeBroker([pos("AMD", qty=1.0)], cash=1000.0)
        execute_rebalance(broker, ["AMD"], {"AMD": 500.0})
        assert broker.orders == []

    def test_missing_price_skips_the_buy_not_guesses(self):
        broker = FakeBroker([], cash=1000.0)
        execute_rebalance(broker, ["AMD", "GHOST"], {"AMD": 500.0})
        symbols_bought = {o[0] for o in broker.orders}
        assert "GHOST" not in symbols_bought

    def test_no_changes_needed_places_no_orders(self):
        broker = FakeBroker([pos("AMD"), pos("INTC")], cash=1000.0)
        execute_rebalance(broker, ["AMD", "INTC"], {"AMD": 500.0, "INTC": 100.0})
        assert broker.orders == []

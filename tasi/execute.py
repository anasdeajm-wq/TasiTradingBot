"""
tasi/execute.py
=================
تنفيذ آلي لقرارات الزخم على وسيط حقيقي - الجسر بين "النظام يقول اشترِ" و
"الأمر وصل فعلاً" الذي ظل ناقصاً طوال هذا المشروع حتى ربط حساب Alpaca.

المنطق بسيط ومتعمد البساطة: قارن المراكز المفتوحة الآن بقائمة الأسهم
المستهدفة (نتيجة momentum.current_picks بعد الفلترة الشرعية) -
    - أي مركز مفتوح خارج القائمة المستهدفة → بيع كامل الكمية
    - أي رمز بالقائمة المستهدفة وليس له مركز → شراء بحصة متساوية من السيولة

هذا أول تنفيذ فعلي - لا يحاول تحسين توقيت الصفقة أو تجزئتها، لأن أولوية
هذه المرحلة إثبات أن القرار يصل وينفَّذ فعلاً، لا الكفاءة الدقيقة للتنفيذ.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence

from .brokers.base import Broker, OrderResult


@dataclass
class RebalancePlan:
    sells: List[str]
    buys: List[str]


def plan_rebalance(held_symbols: Sequence[str],
                   target_symbols: Sequence[str]) -> RebalancePlan:
    """يقرر فقط - لا ينفّذ. يسهّل اختباره دون وسيط حقيقي."""
    held = set(held_symbols)
    target = set(target_symbols)
    return RebalancePlan(
        sells=[s for s in held if s not in target],
        buys=[s for s in target if s not in held],
    )


def execute_rebalance(broker: Broker, target_symbols: Sequence[str],
                      price_lookup: Dict[str, float]) -> List[OrderResult]:
    """نفّذ إعادة توازن كاملة على `broker` الفعلي.

    ``price_lookup``: سعر حي لكل رمز مستهدف، لحساب الكمية الكسرية. رمز
    بلا سعر معروف يُستبعد من الشراء بدل تخمين كمية بسعر قديم.
    """
    positions = {p.symbol: p for p in broker.get_positions()}
    plan = plan_rebalance(list(positions.keys()), target_symbols)
    results: List[OrderResult] = []

    for symbol in plan.sells:
        pos = positions[symbol]
        results.append(broker.place_market_order(symbol, pos.quantity, "SELL"))

    if plan.buys:
        cash = broker.get_cash("USD")
        budget_each = cash / len(target_symbols)   # وزن متساوٍ على القائمة كاملة
        for symbol in plan.buys:
            price = price_lookup.get(symbol)
            if not price or price <= 0:
                continue
            qty = round(budget_each / price, 4)
            if qty > 0:
                results.append(broker.place_market_order(symbol, qty, "BUY"))

    return results

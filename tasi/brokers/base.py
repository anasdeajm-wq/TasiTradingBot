"""
tasi/brokers/base.py
======================
واجهة تنفيذ موحّدة للوسطاء - عقد ثابت لا يتغيّر مهما تغيّر الوسيط خلفه.

**حد صريح لهذا النظام:** لا شيء هنا يفتح حساباً أو يودع مالاً حقيقياً أو
ينفّذ صفقة فعلية بمفرده. فتح حساب وسيط يتطلب هوية المستخدم القانونية (KYC:
هوية وطنية/جواز، إثبات عنوان) وتمويله يتطلب تحويلاً من حسابه البنكي الخاص -
كلاهما فعل شخصي لا يملك هذا النظام صلاحية أو وسيلة القيام به نيابة عن أحد،
ولن يدّعي غير ذلك. ما يوفره هذا الملف: طبقة تنفيذ **جاهزة** بمجرد أن يوفّر
المستخدم بيانات اعتماد حساب حقيقي فتحه بنفسه (مفتاح API أو بوابة محلية مثل
IBKR Client Portal Gateway) - راجع tasi/brokers/ibkr.py ووثيقة القرار في
docs/broker_options.md.

هذه الطبقة تفصل قرار *ماذا نشتري* (المحرك: engine.py + momentum.py) عن
*كيف يُنفَّذ فعلياً* (هنا). أي وسيط جديد يُضاف بتطبيق هذا العقد فقط، دون
تعديل بقية النظام.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class BrokerPosition:
    symbol: str
    quantity: float
    avg_cost: float
    currency: str


@dataclass
class OrderResult:
    accepted: bool
    broker_order_id: Optional[str]
    message: str


class Broker(ABC):
    """عقد أدنى: ما يحتاجه المحرك ليحوّل مقترحاً إلى أمر فعلي، لا أكثر."""

    @abstractmethod
    def is_connected(self) -> bool:
        """False يعني: لا تنفيذ حقيقي ممكن الآن - تحقّق من الاعتماد/البوابة."""

    @abstractmethod
    def get_positions(self) -> List[BrokerPosition]:
        ...

    @abstractmethod
    def get_cash(self, currency: str) -> float:
        ...

    @abstractmethod
    def place_market_order(self, symbol: str, quantity: float,
                           side: str) -> OrderResult:
        """``side``: ``BUY`` أو ``SELL``. ``quantity`` كسري إن دعمه الوسيط."""


class DryRunBroker(Broker):
    """وسيط لا وجود له فعلياً - يسجّل الأمر ولا ينفّذه. الافتراضي الآمن.

    يُستخدم حين لا يوجد اعتماد وسيط حقيقي متاح بعد، حتى لا يظن أحد أن
    مقترحاً وصل من `engine.py` قد نُفِّذ فعلاً بمجرد وجود طبقة تنفيذ.
    """

    def __init__(self) -> None:
        self._log: List[str] = []

    def is_connected(self) -> bool:
        return False

    def get_positions(self) -> List[BrokerPosition]:
        return []

    def get_cash(self, currency: str) -> float:
        return 0.0

    def place_market_order(self, symbol: str, quantity: float,
                           side: str) -> OrderResult:
        message = f"[تشغيل جاف - لا تنفيذ حقيقي] {side} {quantity} {symbol}"
        self._log.append(message)
        return OrderResult(accepted=False, broker_order_id=None, message=message)

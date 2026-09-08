"""
tasi/brokers/alpaca.py
========================
تنفيذ عبر Alpaca - اكتُشف هذا الخيار بعد أن أظهر المستخدم حسابه الفعلي في
تطبيق "أبيان تداول" (Abyan Capital)، وتبيّن بالبحث أن أبيان نفسه مبني على
**Alpaca Broker API** خلف الكواليس. هذا لا يعني أن حساب أبيان نفسه قابل
للبرمجة - أبيان (لا العميل) هو من يملك اعتماد Broker API، ولا دليل على أنه
يُصدر مفاتيح API لعملائه الأفراد. **الخلاصة العملية:** لا يمكن التحكم بحساب
أبيان الحالي برمجياً، لكن نفس البنية التحتية (Alpaca) متاحة مباشرة للأفراد
كمنتج مستقل - بما في ذلك مقيمي السعودية (Alpaca أعلنت شراكات إقليمية مع
ديرة المالية وZAD تحديداً لتغطية السعودية والخليج).

**لماذا Alpaca أفضل من IBKR لهذا النظام تحديداً:**
    - واجهة REST بسيطة (JSON عادي) دون بوابة محلية أو بحث conid معقّد
      كما في IBKR - يمكن تنفيذها كاملة هنا فعلياً لا هيكلاً ناقصاً فقط
    - **كمية كسرية مباشرة** (``qty`` يقبل عدداً عشرياً) - يحل بالضبط مشكلة
      "74% من الميزانية تبقى نقداً عاطلاً" المقيسة سابقاً في هذا المشروع
    - بيئة Paper Trading مجانية مدمجة في نفس الـAPI (base URL مختلف فقط)
      تطابق فلسفة `tasi/paper.py` في هذا المشروع تماماً: نفس الكود يعمل
      على paper-api.alpaca.markets للتدريب، وعلى api.alpaca.markets
      للحساب الحقيقي، بتغيير رابط واحد فقط

**التوثيق الرسمي:** https://docs.alpaca.markets/reference/

**ما لا يزال يحتاج فعلاً شخصياً من المستخدم:** فتح حساب Alpaca مباشر (لا
عبر أبيان) والتحقق من قبوله لمقيمي السعودية وقت التسجيل الفعلي - الشراكات
المعلنة (ديرة، ZAD) لا تضمن بالضرورة فتح حساب فردي مباشر دون وسيط محلي.
هذا يحتاج تحققاً من المستخدم نفسه عند التسجيل، لا افتراضاً مني.
"""

from __future__ import annotations

from typing import List, Optional

import requests

from .base import Broker, BrokerPosition, OrderResult

LIVE_BASE = "https://api.alpaca.markets/v2"
PAPER_BASE = "https://paper-api.alpaca.markets/v2"


class AlpacaBroker(Broker):
    """يخاطب Alpaca Trading API مباشرة. لا مفاتيح مضمّنة في الكود أبداً."""

    def __init__(self, api_key: str, api_secret: str, paper: bool = True) -> None:
        # paper=True افتراضياً عمداً - لا تنفيذ حقيقي إلا بطلب صريح
        self.base_url = PAPER_BASE if paper else LIVE_BASE
        self.paper = paper
        self._session = requests.Session()
        self._session.headers.update({
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
        })

    def _get(self, path: str) -> Optional[object]:
        try:
            response = self._session.get(f"{self.base_url}{path}", timeout=10)
            if response.status_code != 200:
                return None
            return response.json()
        except requests.RequestException:
            return None

    def is_connected(self) -> bool:
        account = self._get("/account")
        return bool(account and account.get("status") == "ACTIVE")

    def get_positions(self) -> List[BrokerPosition]:
        data = self._get("/positions")
        if not data:
            return []
        return [
            BrokerPosition(symbol=p["symbol"], quantity=float(p["qty"]),
                           avg_cost=float(p["avg_entry_price"]),
                           currency="USD")
            for p in data
        ]

    def get_cash(self, currency: str = "USD") -> float:
        if currency != "USD":
            return 0.0   # Alpaca حسابات دولارية فقط - لا تحويل عملة هنا
        account = self._get("/account")
        return float(account["cash"]) if account else 0.0

    def place_market_order(self, symbol: str, quantity: float,
                           side: str) -> OrderResult:
        if not self.is_connected():
            return OrderResult(False, None, "الحساب غير نشط أو المفاتيح غير صحيحة")

        payload = {
            "symbol": symbol,
            "qty": str(quantity),          # كسري مقبول مباشرة - راجع رأس الملف
            "side": side.lower(),          # "buy" أو "sell"
            "type": "market",
            "time_in_force": "day",
        }
        try:
            response = self._session.post(f"{self.base_url}/orders",
                                          json=payload, timeout=10)
        except requests.RequestException as exc:
            return OrderResult(False, None, f"فشل الاتصال: {exc}")

        if response.status_code not in (200, 201):
            return OrderResult(False, None,
                               f"رفض Alpaca الأمر ({response.status_code}): "
                               f"{response.text[:200]}")

        order = response.json()
        return OrderResult(True, order.get("id"),
                           f"{'شراء' if side.lower()=='buy' else 'بيع'} "
                           f"{quantity} {symbol} - "
                           f"{'ورقي' if self.paper else 'حقيقي'}")

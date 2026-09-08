"""
tasi/brokers/ibkr.py
======================
تنفيذ عبر Interactive Brokers - السبب في اختياره تحديداً بعد بحث فعلي:

    - يقبل مقيمي السعودية (مؤكَّد من صفحة "Available Countries" الرسمية)
    - يدعم الأسهم الكسرية (fractional shares) - يحل تحديداً المشكلة
      المقيسة فعلياً: بميزانية صغيرة، 3 من 5 أسهم الزخم الأمريكية المختارة
      أعلى سعراً من حصة السهم الواحد بالوزن المتساوي، فيبقى معظم الميزانية
      نقداً عاطلاً بدون كسور. راجع docs/broker_options.md للحساب الكامل
    - يتيح الوصول لتاسي نفسه من نفس الحساب (Access the Saudi Exchange with
      Your IBKR Account) - إمكانية وسيط واحد للسوقين معاً، لم تُختبر هنا
    - واجهة برمجية موثّقة رسمياً (Client Portal Web API / TWS API)

**ما لا يوفره هذا الملف ولن يدّعي توفيره:** فتح الحساب نفسه (KYC: هوية
المستخدم وعنوانه) وتمويله (تحويل بنكي من حساب المستخدم) فعلان شخصيان لا
يملك هذا النظام صلاحية تنفيذهما. هذا الملف يفترض أن المستخدم فتح حسابه
بنفسه، وشغّل IBKR Client Portal Gateway محلياً (تنزيل مجاني من IBKR، يعمل
كخادم HTTPS ذاتي التوقيع على المنفذ 5000 افتراضياً)، وسجّل دخوله يدوياً في
تلك البوابة - عندها فقط تصبح هذه الطبقة قابلة للاستخدام الفعلي.

**التوثيق:** https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/
"""

from __future__ import annotations

from typing import List, Optional

import requests

from .base import Broker, BrokerPosition, OrderResult

DEFAULT_GATEWAY = "https://localhost:5000/v1/api"


class IBKRBroker(Broker):
    """يخاطب Client Portal Gateway محلياً. لا اعتماد مضمّن، لا مفاتيح مخزَّنة."""

    def __init__(self, account_id: str, gateway_url: str = DEFAULT_GATEWAY,
                verify_ssl: bool = False) -> None:
        # verify_ssl=False لأن البوابة المحلية تستخدم شهادة موقّعة ذاتياً -
        # هذا مقبول لأنها على localhost فقط، وليس اتصالاً بخادم بعيد
        self.account_id = account_id
        self.gateway_url = gateway_url.rstrip("/")
        self.verify_ssl = verify_ssl
        self._session = requests.Session()

    def _get(self, path: str) -> Optional[dict]:
        try:
            response = self._session.get(
                f"{self.gateway_url}{path}", verify=self.verify_ssl, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            return None

    def is_connected(self) -> bool:
        """يتحقق من تسجيل دخول فعلي في البوابة المحلية - لا يفترضه أبداً."""
        status = self._get("/iserver/auth/status")
        return bool(status and status.get("authenticated"))

    def get_positions(self) -> List[BrokerPosition]:
        if not self.is_connected():
            return []
        data = self._get(f"/portfolio/{self.account_id}/positions/0") or []
        return [
            BrokerPosition(symbol=p.get("ticker", p.get("contractDesc", "")),
                           quantity=p.get("position", 0.0),
                           avg_cost=p.get("avgCost", 0.0),
                           currency=p.get("currency", "USD"))
            for p in data
        ]

    def get_cash(self, currency: str) -> float:
        if not self.is_connected():
            return 0.0
        summary = self._get(f"/portfolio/{self.account_id}/summary") or {}
        key = f"availablefunds-{currency}" if currency != "USD" else "availablefunds"
        entry = summary.get(key, {})
        return float(entry.get("amount", 0.0)) if isinstance(entry, dict) else 0.0

    def place_market_order(self, symbol: str, quantity: float,
                           side: str) -> OrderResult:
        if not self.is_connected():
            return OrderResult(False, None,
                               "غير متصل بالبوابة المحلية - سجّل دخولاً في "
                               "IBKR Client Portal Gateway أولاً")
        # ملاحظة: يتطلب فعلياً بحثاً عن conid السهم أولاً عبر /iserver/secdef/search
        # ثم تأكيد الأمر عبر /iserver/reply/{replyid} - محذوف هنا عمداً؛ هذا
        # هيكل جاهز للإكمال بمجرد أن يصبح لدينا حساب فعلي لاختباره ضده، لا
        # كود يُدَّعى أنه مختبر دون بيئة حقيقية لاختباره
        return OrderResult(False, None,
                           "غير مُفعَّل بعد: يحتاج بحث conid وتأكيد الأمر عبر "
                           "التدفق الفعلي للبوابة - راجع التوثيق في رأس الملف")

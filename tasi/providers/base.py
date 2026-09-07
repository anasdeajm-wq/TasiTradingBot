"""
tasi/providers/base.py
======================
واجهة مزوّد البيانات - the contract every market data source must satisfy.

الغرض من هذه الطبقة أن اختيار المزوّد يبقى قراراً قابلاً للتغيير. النظام
كله يتعامل مع هذه الواجهة فقط، فالانتقال من مزوّد إلى آخر - أو من بيانات
مؤجلة إلى بث لحظي - يصبح تغيير سطر في الإعدادات لا إعادة كتابة.

هذا ليس تجريداً زائداً: تغطية سوق تداول تختلف كثيراً بين المزوّدين،
وبعضها يتطلب اشتراكاً مدفوعاً. بناء المنطق فوق واجهة ثابتة يسمح ببناء
واختبار النظام كاملاً قبل شراء أي اشتراك.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Sequence


@dataclass
class Quote:
    """لقطة سعر لحظية."""

    symbol: str
    price: float
    ts: datetime
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    prev_close: Optional[float] = None
    volume: Optional[float] = None
    turnover: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None

    @property
    def change_pct(self) -> Optional[float]:
        if not self.prev_close:
            return None
        return (self.price - self.prev_close) / self.prev_close * 100.0

    @property
    def spread(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


@dataclass
class Bar:
    """شمعة."""

    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    turnover: Optional[float] = None
    trades: Optional[int] = None


@dataclass
class NewsItem:
    """خبر أو إفصاح."""

    headline: str
    published_at: datetime
    symbol: Optional[str] = None
    body: str = ""
    source: str = ""
    url: str = ""
    category: str = ""


@dataclass
class ProviderCapabilities:
    """ما يدعمه المزوّد فعلياً - يُقرأ قبل تشغيل أي وضع يعتمد عليه."""

    name: str
    realtime: bool = False          # بث لحظي أم بيانات مؤجلة
    delay_minutes: int = 0          # مقدار التأخير إن لم يكن لحظياً
    websocket: bool = False         # بث مستمر بدل الاستطلاع المتكرر
    depth: bool = False             # عمق السوق (دفتر الأوامر)
    intraday_history: bool = False
    news: bool = False
    fundamentals: bool = False
    max_symbols_per_call: int = 1
    daily_request_limit: Optional[int] = None
    notes: str = ""

    def require(self, **needs: bool) -> None:
        """تأكد أن المزوّد يدعم ما يحتاجه وضع التشغيل، وإلا ارفع خطأ واضحاً."""
        missing = [
            key for key, needed in needs.items()
            if needed and not getattr(self, key, False)
        ]
        if missing:
            raise ProviderCapabilityError(
                f"المزوّد '{self.name}' لا يدعم: {', '.join(missing)}. "
                f"{self.notes}".strip()
            )


class ProviderError(RuntimeError):
    """خطأ عام من مزوّد البيانات."""


class ProviderCapabilityError(ProviderError):
    """طُلبت ميزة لا يدعمها المزوّد."""


class RateLimitError(ProviderError):
    """تجاوز حد الطلبات."""


class MarketDataProvider(ABC):
    """الواجهة التي يجب أن ينفّذها كل مزوّد."""

    @property
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """قدرات المزوّد - تُفحص قبل الاعتماد عليه."""

    @abstractmethod
    def list_symbols(self) -> List[Dict[str, str]]:
        """قائمة الأسهم المدرجة مع أسمائها وقطاعاتها."""

    @abstractmethod
    def get_quotes(self, symbols: Sequence[str]) -> Dict[str, Quote]:
        """أسعار لحظية لمجموعة أسهم."""

    @abstractmethod
    def get_bars(self, symbol: str, interval: str = "1day",
                 limit: int = 250) -> List[Bar]:
        """شموع تاريخية، الأقدم أولاً."""

    # اختيارية: ترفع ProviderCapabilityError افتراضياً
    def stream_quotes(self, symbols: Sequence[str],
                      on_quote: Callable[[Quote], None]) -> None:
        """بث لحظي مستمر. يستدعي on_quote عند كل تحديث."""
        self.capabilities.require(websocket=True)
        raise NotImplementedError

    def get_news(self, symbols: Optional[Sequence[str]] = None,
                 limit: int = 50) -> List[NewsItem]:
        """أحدث الأخبار والإفصاحات."""
        self.capabilities.require(news=True)
        raise NotImplementedError

    def health_check(self) -> Dict[str, object]:
        """تحقق من أن المزوّد يستجيب فعلاً بمفتاح المستخدم الحالي.

        يُستدعى قبل بدء الجلسة: الفشل هنا أرخص بكثير من اكتشاف انقطاع
        البيانات بعد فتح السوق.
        """
        caps = self.capabilities
        try:
            symbols = self.list_symbols()
            return {
                "provider": caps.name,
                "ok": True,
                "symbols_available": len(symbols),
                "realtime": caps.realtime,
                "delay_minutes": caps.delay_minutes,
            }
        except Exception as exc:                          # noqa: BLE001
            return {"provider": caps.name, "ok": False, "error": str(exc)}

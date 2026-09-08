"""
tasi/providers/yahoo.py
=======================
مزوّد ياهو فاينانس - free Tadawul data, good enough to calibrate on.

لماذا هذا المزوّد: هو المصدر المجاني الوحيد الذي وجدته يغطي سوق تداول
بعمق كافٍ للمعايرة - عشر سنوات من الشموع اليومية بـ OHLC وأحجام وأسعار
معدّلة، ومؤشر تاسي نفسه.

**حدوده، وهي مهمة:**
    - واجهة غير رسمية بلا التزام خدمة. قد تتغيّر أو تتوقف بلا إشعار.
    - الأسعار مؤجلة قرابة ١٥ دقيقة. لا تصلح للمضاربة داخل الجلسة.
    - تظهر فيها أحياناً شموع مكررة أو قديمة. لذلك يمر كل ما تُنتجه هذه
      الطبقة على فحص الجودة قبل أن يصير قراراً.

الاستخدام الصحيح: معايرة الأنماط على التاريخ، والتحليل بعد إغلاق الجلسة.
الاستخدام الخاطئ: اتخاذ قرار لحظي داخل السوق المفتوح.

الرموز: تداول في ياهو بصيغة ``2222.SR``، والمؤشر ``^TASI.SR``.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence

import requests

from .base import (Bar, MarketDataProvider, ProviderCapabilities, ProviderError,
                   Quote, RateLimitError)

log = logging.getLogger("tasi.yahoo")

BASE = "https://query1.finance.yahoo.com"
SUFFIX = ".SR"
INDEX_SYMBOL = "^TASI.SR"

# ياهو يرفض الطلبات بلا ترويسة متصفح
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json",
}

INTERVAL_MAP = {
    "1min": "1m", "5min": "5m", "15min": "15m", "30min": "30m",
    "60min": "60m", "1h": "60m", "1day": "1d", "1week": "1wk",
}

# سقف تاريخ ياهو لكل فاصل. تجاوزه يرجع رداً فارغاً لا خطأ، فنقيّده هنا.
MAX_RANGE = {"1m": "7d", "5m": "60d", "15m": "60d", "30m": "60d",
             "60m": "730d", "1d": "10y", "1wk": "10y"}

SPARK_BATCH = 40          # عدد الرموز في طلب الأسعار المجمّع
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4


def to_yahoo(symbol: str) -> str:
    """حوّل 2222 أو 2222.SR إلى صيغة ياهو."""
    s = str(symbol).strip().upper()
    if s.startswith("^"):
        return s
    if s in ("TASI", "^TASI"):
        return INDEX_SYMBOL
    base = s.split(".")[0].split(":")[0]
    return f"{base}{SUFFIX}"


def from_yahoo(symbol: str) -> str:
    """أرجع الرمز إلى صيغة النظام الداخلية."""
    s = str(symbol).strip().upper()
    if s == INDEX_SYMBOL:
        return "TASI"
    return s.replace(SUFFIX, "").lstrip("^")


class YahooProvider(MarketDataProvider):
    """مزوّد مجاني للشموع التاريخية والأسعار المؤجلة."""

    def __init__(self, session: Optional[requests.Session] = None,
                 workers: int = 6, pause: float = 0.15) -> None:
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)
        self.workers = workers
        self.pause = pause          # فاصل بين الطلبات لتجنّب الحظر

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="yahoo",
            realtime=False,
            delay_minutes=15,
            websocket=False,
            depth=False,
            intraday_history=True,
            news=False,
            fundamentals=False,
            max_symbols_per_call=SPARK_BATCH,
            daily_request_limit=None,
            notes=("واجهة غير رسمية، أسعار مؤجلة ١٥ دقيقة. تصلح للمعايرة "
                   "والتحليل بعد الإغلاق، لا للمضاربة داخل الجلسة."),
        )

    # ------------------------------------------------------------------
    def _get(self, path: str, params: dict) -> dict:
        """طلب مع إعادة محاولة وتراجع أسّي."""
        delay = 1.5
        last = ""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.get(f"{BASE}/{path}", params=params,
                                            timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                last = f"خطأ شبكة: {exc}"
                log.warning("محاولة %d/%d: %s", attempt, MAX_RETRIES, last)
                time.sleep(delay); delay *= 2
                continue

            if response.status_code == 429:
                last = "تجاوز حد الطلبات (429)"
                log.warning("%s - انتظار %.1f ثانية", last, delay)
                time.sleep(delay); delay *= 2
                continue
            if response.status_code == 404:
                raise ProviderError(f"رمز غير موجود: {params}")
            if response.status_code != 200:
                last = f"HTTP {response.status_code}: {response.text[:160]}"
                time.sleep(delay); delay *= 2
                continue

            try:
                return response.json()
            except ValueError:
                raise ProviderError(f"رد غير صالح: {response.text[:160]}")

        if "429" in last:
            raise RateLimitError(last)
        raise ProviderError(f"فشل الطلب بعد {MAX_RETRIES} محاولات: {last}")

    # ------------------------------------------------------------------
    def discover_symbols(self, candidates: Iterable[str],
                         on_found: Optional[Callable[[str, str], None]] = None
                         ) -> List[Dict[str, str]]:
        """اكتشف الرموز الموجودة فعلاً بفحص قائمة مرشّحة.

        يبني الكون من مصدر حقيقي بدل الاعتماد على قائمة محفوظة قد تكون
        قديمة أو ناقصة. الرمز غير الموجود يُتجاهل بهدوء.
        """
        found: List[Dict[str, str]] = []

        def probe(code: str) -> Optional[Dict[str, str]]:
            try:
                payload = self._get(f"v8/finance/chart/{to_yahoo(code)}",
                                    {"range": "5d", "interval": "1d"})
            except ProviderError:
                return None
            result = (payload.get("chart") or {}).get("result")
            if not result:
                return None
            meta = result[0].get("meta", {})
            name = meta.get("longName") or meta.get("shortName") or ""
            if not name:
                return None
            return {
                "symbol": from_yahoo(meta.get("symbol", code)),
                "name_en": name,
                "currency": meta.get("currency", "SAR"),
                "exchange": meta.get("fullExchangeName", ""),
            }

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for entry in pool.map(probe, list(candidates)):
                if entry:
                    found.append(entry)
                    if on_found:
                        on_found(entry["symbol"], entry["name_en"])
        return sorted(found, key=lambda e: e["symbol"])

    def list_symbols(self) -> List[Dict[str, str]]:
        """ياهو لا يوفّر سرداً للسوق، فالاكتشاف يتم عبر discover_symbols.

        نتحقق هنا فقط من أن الخدمة تستجيب، حتى يبقى health_check ذا معنى.
        """
        payload = self._get(f"v8/finance/chart/{INDEX_SYMBOL}",
                            {"range": "5d", "interval": "1d"})
        result = (payload.get("chart") or {}).get("result")
        if not result:
            raise ProviderError("ياهو لا يرجع بيانات لمؤشر تاسي.")
        meta = result[0].get("meta", {})
        return [{"symbol": "TASI", "name_en": meta.get("longName", "TASI"),
                 "exchange": meta.get("fullExchangeName", "")}]

    # ------------------------------------------------------------------
    def get_quotes(self, symbols: Sequence[str]) -> Dict[str, Quote]:
        """أسعار مؤجلة لمجموعة رموز عبر نقطة spark المجمّعة."""
        out: Dict[str, Quote] = {}
        symbols = list(symbols)

        for start in range(0, len(symbols), SPARK_BATCH):
            chunk = symbols[start:start + SPARK_BATCH]
            joined = ",".join(to_yahoo(s) for s in chunk)
            payload = self._get("v7/finance/spark",
                                {"symbols": joined, "range": "5d",
                                 "interval": "1d"})
            entries = (payload.get("spark") or {}).get("result") or []
            for entry in entries:
                for response in entry.get("response", []):
                    quote = self._quote_from_response(response)
                    if quote:
                        out[quote.symbol] = quote
            if self.pause:
                time.sleep(self.pause)
        return out

    def _quote_from_response(self, response: dict) -> Optional[Quote]:
        meta = response.get("meta") or {}
        symbol = from_yahoo(meta.get("symbol", ""))
        price = meta.get("regularMarketPrice")
        if not symbol or price is None:
            return None

        stamp = meta.get("regularMarketTime")
        ts = (datetime.fromtimestamp(stamp, tz=timezone.utc)
              if stamp else datetime.now(timezone.utc))

        return Quote(
            symbol=symbol, price=float(price), ts=ts,
            prev_close=meta.get("chartPreviousClose") or meta.get("previousClose"),
            high=meta.get("regularMarketDayHigh"),
            low=meta.get("regularMarketDayLow"),
            volume=meta.get("regularMarketVolume"),
        )

    # ------------------------------------------------------------------
    def get_bars(self, symbol: str, interval: str = "1day",
                 limit: int = 250) -> List[Bar]:
        """شموع تاريخية، الأقدم أولاً.

        يستخدم ``adjclose`` عند توفره حتى تكون السلسلة معدّلة للتجزئة
        والتوزيعات. السلسلة غير المعدّلة تُنتج قفزات وهمية تفسد كل قياس
        مبني عليها.
        """
        yahoo_interval = INTERVAL_MAP.get(interval, interval)
        span = MAX_RANGE.get(yahoo_interval, "10y")

        payload = self._get(f"v8/finance/chart/{to_yahoo(symbol)}",
                            {"range": span, "interval": yahoo_interval,
                             "events": "div,split"})
        result = (payload.get("chart") or {}).get("result")
        if not result:
            error = (payload.get("chart") or {}).get("error")
            raise ProviderError(f"{symbol}: لا توجد بيانات ({error})")

        block = result[0]
        stamps = block.get("timestamp") or []
        quote = (block.get("indicators", {}).get("quote") or [{}])[0]
        adj = (block.get("indicators", {}).get("adjclose") or [{}])
        adjclose = adj[0].get("adjclose") if adj else None

        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        bars: List[Bar] = []
        for i, stamp in enumerate(stamps):
            close = closes[i] if i < len(closes) else None
            if close is None:
                continue

            # نسبة التعديل تُطبَّق على الشمعة كاملة حتى تبقى متسقة
            factor = 1.0
            if adjclose and i < len(adjclose) and adjclose[i] and close:
                factor = adjclose[i] / close

            ts = datetime.fromtimestamp(stamp, tz=timezone.utc)
            bars.append(Bar(
                symbol=from_yahoo(symbol), ts=ts,
                open=(opens[i] or close) * factor,
                high=(highs[i] or close) * factor,
                low=(lows[i] or close) * factor,
                close=close * factor,
                volume=float(volumes[i] or 0) if i < len(volumes) else 0.0,
            ))

        return bars[-limit:] if limit else bars

    # ------------------------------------------------------------------
    def health_check(self) -> Dict[str, object]:
        caps = self.capabilities
        try:
            bars = self.get_bars("TASI", "1day", limit=5)
            return {
                "provider": caps.name, "ok": bool(bars),
                "index_bars": len(bars),
                "last_close": round(bars[-1].close, 2) if bars else None,
                "last_date": bars[-1].ts.date().isoformat() if bars else None,
                "realtime": caps.realtime,
                "delay_minutes": caps.delay_minutes,
                "warning": caps.notes,
            }
        except Exception as exc:                          # noqa: BLE001
            return {"provider": caps.name, "ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
def tadawul_candidates() -> List[str]:
    """قائمة الرموز المرشّحة للفحص.

    تداول يخصص نطاقات رقمية للقطاعات. نفحص النطاقات كاملة ونحتفظ بما
    يرجع اسماً فعلياً، فلا نعتمد على قائمة محفوظة قد تفوتها إدراجات
    جديدة أو تُبقي شركات شُطبت.
    """
    ranges = [
        (1010, 1215),   # البنوك والاستثمار والتعدين
        (1301, 1835),   # مواد وخدمات
        (2001, 2400),   # الطاقة والمواد الأساسية
        (3001, 3095),   # الأسمنت
        (4001, 4350),   # التجزئة والعقار والرعاية الصحية والصناديق
        (5110, 5115),   # المرافق
        (6001, 6095),   # الأغذية والزراعة
        (7010, 7205),   # الاتصالات والتقنية
        (8010, 8320),   # التأمين
        (9500, 9610),   # السوق الموازية نمو
    ]
    out: List[str] = []
    for low, high in ranges:
        out.extend(str(code) for code in range(low, high + 1, 1))
    return out

"""
tasi/features.py
================
بناء الخصائص - turns raw bars into the full feature vector a decision uses.

كل قرار في النظام يُبنى على لقطة خصائص واحدة (FeatureSnapshot). وكل إشارة
تُخزَّن مع اللقطة كاملة التي أنتجتها. الفائدة عملية: عند تقييم أي إشارة
لاحقاً نعرف بالضبط ما الذي كان النظام يراه لحظة القرار، فيصبح تحليل سبب
الخطأ ممكناً بدل التخمين.

القاعدة هنا: أي خاصية لا يكفي تاريخها تبقى None، ولا تُملأ بقيمة افتراضية.
تعبئة الفراغات بأصفار تُنتج إشارات تبدو صحيحة وهي مبنية على لا شيء.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Sequence

from . import indicators as ind

Number = Optional[float]


@dataclass
class FeatureSnapshot:
    """كل ما يعرفه النظام عن سهم في لحظة واحدة."""

    symbol: str
    ts: str
    close: Number = None
    open: Number = None
    high: Number = None
    low: Number = None
    volume: Number = None

    # اتجاه
    ma20: Number = None
    ma50: Number = None
    ma200: Number = None

    # زخم
    rsi: Number = None
    macd: Number = None
    macd_signal: Number = None
    macd_hist: Number = None
    roc5: Number = None
    roc10: Number = None

    # تقلب
    atr: Number = None
    atr_pct: Number = None            # ATR كنسبة من السعر
    volatility: Number = None
    bb_upper: Number = None
    bb_lower: Number = None
    bb_width: Number = None

    # حجم
    avg_volume: Number = None
    volume_ratio: Number = None

    # موقع نسبي
    high_20: Number = None            # أعلى سعر في 20 شمعة
    low_20: Number = None
    high_52w: Number = None
    pct_from_high20: Number = None    # بعد السعر عن قمة 20 شمعة
    rel_strength: Number = None       # مقابل المؤشر

    # سياق
    regime: Optional[str] = None
    bars: int = 0

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    # -- خصائص مشتقة تُستخدم كثيراً في الأنماط -------------------------
    @property
    def above_ma50(self) -> Optional[bool]:
        if self.close is None or self.ma50 is None:
            return None
        return self.close > self.ma50

    @property
    def trend_up(self) -> Optional[bool]:
        """MA50 فوق MA200 - التقاطع الذهبي."""
        if self.ma50 is None or self.ma200 is None:
            return None
        return self.ma50 > self.ma200

    @property
    def macd_bullish(self) -> Optional[bool]:
        if self.macd is None or self.macd_signal is None:
            return None
        return self.macd > self.macd_signal

    @property
    def is_complete(self) -> bool:
        """هل تتوفر الخصائص الأساسية اللازمة لاتخاذ قرار؟"""
        return None not in (self.close, self.ma20, self.ma50, self.rsi,
                            self.atr, self.volume_ratio)


def build(
    symbol: str,
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    timestamps: Sequence[str],
    benchmark_closes: Optional[Sequence[float]] = None,
    regime: Optional[str] = None,
    index: int = -1,
) -> FeatureSnapshot:
    """احسب لقطة الخصائص عند الشمعة رقم ``index`` (الافتراضي: الأخيرة).

    تمرير ``index`` يسمح للاختبار التاريخي ببناء اللقطة كما كانت تُرى في
    ذلك الوقت تماماً، دون أي تسرّب من المستقبل.
    """
    n = len(closes)
    if n == 0:
        return FeatureSnapshot(symbol=symbol, ts="", bars=0)

    i = index if index >= 0 else n + index
    i = max(0, min(i, n - 1))

    # كل السلاسل تُقطع عند i، فلا ترى الدوال أي شمعة لاحقة.
    o, h, l, c = opens[:i + 1], highs[:i + 1], lows[:i + 1], closes[:i + 1]
    v = volumes[:i + 1]

    snap = FeatureSnapshot(
        symbol=symbol,
        ts=timestamps[i] if i < len(timestamps) else "",
        close=c[i], open=o[i] if i < len(o) else None,
        high=h[i] if i < len(h) else None, low=l[i] if i < len(l) else None,
        volume=v[i] if i < len(v) else None,
        regime=regime, bars=i + 1,
    )

    snap.ma20 = ind.sma(c, 20)[i] if len(c) >= 20 else None
    snap.ma50 = ind.sma(c, 50)[i] if len(c) >= 50 else None
    snap.ma200 = ind.sma(c, 200)[i] if len(c) >= 200 else None

    snap.rsi = ind.rsi(c, 14)[i] if len(c) > 14 else None
    macd_series = ind.macd(c)
    snap.macd = macd_series["macd"][i]
    snap.macd_signal = macd_series["signal"][i]
    snap.macd_hist = macd_series["hist"][i]
    snap.roc5 = ind.roc(c, 5)[i] if len(c) > 5 else None
    snap.roc10 = ind.roc(c, 10)[i] if len(c) > 10 else None

    atr_series = ind.atr(h, l, c, 14)
    snap.atr = atr_series[i] if i < len(atr_series) else None
    if snap.atr is not None and snap.close:
        snap.atr_pct = snap.atr / snap.close * 100.0
    vol_series = ind.realized_volatility(c, 20)
    snap.volatility = vol_series[i] if i < len(vol_series) else None

    if len(c) >= 20:
        bands = ind.bollinger(c, 20)
        snap.bb_upper = bands["upper"][i]
        snap.bb_lower = bands["lower"][i]
        snap.bb_width = bands["width"][i]

    snap.avg_volume = ind.average_volume(v, 20)[i] if len(v) >= 21 else None
    snap.volume_ratio = ind.volume_ratio(v, 20)[i] if len(v) >= 21 else None

    if len(c) >= 20:
        window_h = h[max(0, i - 19):i + 1]
        window_l = l[max(0, i - 19):i + 1]
        snap.high_20 = max(window_h)
        snap.low_20 = min(window_l)
        if snap.high_20:
            snap.pct_from_high20 = (snap.close - snap.high_20) / snap.high_20 * 100.0
    if len(c) >= 250:
        snap.high_52w = max(h[max(0, i - 249):i + 1])

    if benchmark_closes and len(benchmark_closes) > i and len(c) > 20:
        bench = list(benchmark_closes[:i + 1])
        if len(bench) == len(c):
            rel = ind.relative_strength(c, bench, 20)
            snap.rel_strength = rel[i]

    return snap


def build_series(
    symbol: str,
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    timestamps: Sequence[str],
    benchmark_closes: Optional[Sequence[float]] = None,
    start: int = 200,
) -> List[FeatureSnapshot]:
    """ابنِ لقطة لكل شمعة من ``start`` فصاعداً - يستخدمها الاختبار التاريخي.

    يحسب المؤشرات مرة واحدة على كامل السلسلة بدل إعادة حسابها لكل شمعة،
    وهو الفرق بين اختبار يستغرق ثوانٍ وآخر يستغرق دقائق.
    """
    n = len(closes)
    if n == 0:
        return []

    ma20 = ind.sma(closes, 20)
    ma50 = ind.sma(closes, 50)
    ma200 = ind.sma(closes, 200)
    rsi = ind.rsi(closes, 14)
    macd_series = ind.macd(closes)
    roc5 = ind.roc(closes, 5)
    roc10 = ind.roc(closes, 10)
    atr_series = ind.atr(highs, lows, closes, 14)
    vol_series = ind.realized_volatility(closes, 20)
    bands = ind.bollinger(closes, 20)
    avg_vol = ind.average_volume(volumes, 20)
    vol_ratio = ind.volume_ratio(volumes, 20)
    rel = (ind.relative_strength(closes, benchmark_closes, 20)
           if benchmark_closes and len(benchmark_closes) == n else [None] * n)

    out: List[FeatureSnapshot] = []
    for i in range(max(0, start), n):
        snap = FeatureSnapshot(
            symbol=symbol,
            ts=timestamps[i] if i < len(timestamps) else "",
            close=closes[i], open=opens[i], high=highs[i], low=lows[i],
            volume=volumes[i], bars=i + 1,
            ma20=ma20[i], ma50=ma50[i], ma200=ma200[i],
            rsi=rsi[i], macd=macd_series["macd"][i],
            macd_signal=macd_series["signal"][i],
            macd_hist=macd_series["hist"][i],
            roc5=roc5[i], roc10=roc10[i],
            atr=atr_series[i], volatility=vol_series[i],
            bb_upper=bands["upper"][i], bb_lower=bands["lower"][i],
            bb_width=bands["width"][i],
            avg_volume=avg_vol[i], volume_ratio=vol_ratio[i],
            rel_strength=rel[i],
        )
        if snap.atr is not None and snap.close:
            snap.atr_pct = snap.atr / snap.close * 100.0
        if i >= 19:
            snap.high_20 = max(highs[i - 19:i + 1])
            snap.low_20 = min(lows[i - 19:i + 1])
            if snap.high_20:
                snap.pct_from_high20 = (
                    (snap.close - snap.high_20) / snap.high_20 * 100.0)
        if i >= 249:
            snap.high_52w = max(highs[i - 249:i + 1])
        out.append(snap)
    return out

"""
tasi/regime.py
==============
كشف حالة السوق - market regime detection.

لماذا هذا الملف موجود: نفس النمط الفني يعطي نتائج مختلفة تماماً باختلاف
حالة السوق. اختراق المقاومة في سوق صاعد هادئ شيء، ونفس الاختراق في سوق
هابط عالي التذبذب شيء آخر. بدون تصنيف الحالة، نسبة إصابة أي نمط تكون
متوسطاً مضللاً لحالات متناقضة.

لذلك كل إشارة وكل توقع يُسجَّل مع الحالة التي أُطلق فيها، ويُقاس أداء كل
نمط داخل كل حالة على حدة.

الحالات:
    TREND_UP    اتجاه صاعد   : المؤشر فوق متوسطاته والاتساع إيجابي
    TREND_DOWN  اتجاه هابط   : المؤشر تحت متوسطاته والاتساع سلبي
    RANGE       عرضي         : لا اتجاه واضح، تذبذب طبيعي
    HIGH_VOL    تذبذب عالٍ   : التقلب فوق المعتاد بوضوح
    RISK_OFF    هروب مخاطرة  : هبوط حاد مع اتساع سلبي شديد
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

from . import indicators as ind

TREND_UP = "TREND_UP"
TREND_DOWN = "TREND_DOWN"
RANGE = "RANGE"
HIGH_VOL = "HIGH_VOL"
RISK_OFF = "RISK_OFF"

REGIME_AR = {
    TREND_UP: "اتجاه صاعد",
    TREND_DOWN: "اتجاه هابط",
    RANGE: "سوق عرضي",
    HIGH_VOL: "تذبذب عالٍ",
    RISK_OFF: "هروب من المخاطرة",
}

# عتبات التصنيف. مضبوطة على سلوك مؤشر تاسي، وقابلة للتعديل بعد القياس.
HIGH_VOL_THRESHOLD = 1.6      # التقلب الحالي مقابل متوسطه طويل المدى
RISK_OFF_DROP = -2.5          # هبوط المؤشر بالنسبة المئوية في الجلسة
RISK_OFF_BREADTH = 0.25       # نسبة الأسهم الصاعدة تحت هذا الحد
WEAK_BREADTH = 0.45
STRONG_BREADTH = 0.55


@dataclass
class RegimeSnapshot:
    """حالة السوق في يوم معيّن مع الأرقام التي بُني عليها التصنيف."""

    date: str
    regime: str
    close: Optional[float] = None
    change_pct: Optional[float] = None
    ma20: Optional[float] = None
    ma50: Optional[float] = None
    ma200: Optional[float] = None
    volatility: Optional[float] = None
    vol_ratio: Optional[float] = None      # التقلب الحالي / متوسطه
    breadth: Optional[float] = None        # نسبة الأسهم الصاعدة
    reason_ar: str = ""

    @property
    def regime_ar(self) -> str:
        return REGIME_AR.get(self.regime, self.regime)

    def to_dict(self) -> Dict[str, object]:
        data = asdict(self)
        data["regime_ar"] = self.regime_ar
        return data


def classify(
    closes: Sequence[float],
    breadth: Optional[float] = None,
    as_of: Optional[str] = None,
) -> RegimeSnapshot:
    """صنّف حالة السوق من سلسلة إغلاقات المؤشر واتساع السوق.

    ``closes`` مرتّبة من الأقدم إلى الأحدث. الاتساع (breadth) هو نسبة
    الأسهم الصاعدة إلى إجمالي المتداولة، من 0 إلى 1. تمريره اختياري لكنه
    يحسّن التصنيف كثيراً: مؤشر يصعد بأسهم قليلة فقط ليس سوقاً صاعداً.
    """
    snap = RegimeSnapshot(
        date=as_of or date.today().isoformat(),
        regime=RANGE,
        breadth=breadth,
    )
    if len(closes) < 30:
        snap.reason_ar = "تاريخ غير كافٍ لتصنيف الحالة (أقل من 30 جلسة)."
        return snap

    ma20 = ind.sma(closes, 20)[-1]
    ma50 = ind.sma(closes, 50)[-1] if len(closes) >= 50 else None
    ma200 = ind.sma(closes, 200)[-1] if len(closes) >= 200 else None
    vol_series = ind.realized_volatility(closes, 20)
    volatility = vol_series[-1]

    defined_vol = [v for v in vol_series if v is not None]
    baseline = sum(defined_vol) / len(defined_vol) if defined_vol else None
    vol_ratio = (volatility / baseline) if (volatility and baseline) else None

    close = closes[-1]
    change_pct = ((close - closes[-2]) / closes[-2] * 100.0) if closes[-2] else None

    snap.close = close
    snap.change_pct = change_pct
    snap.ma20 = ma20
    snap.ma50 = ma50
    snap.ma200 = ma200
    snap.volatility = volatility
    snap.vol_ratio = vol_ratio

    reasons: List[str] = []

    # 1) هروب المخاطرة: هبوط حاد مع اتساع سلبي شديد. يُفحص أولاً لأنه
    #    يتجاوز كل ما عداه في قرارات الدخول.
    if (change_pct is not None and change_pct <= RISK_OFF_DROP
            and (breadth is None or breadth <= RISK_OFF_BREADTH)):
        snap.regime = RISK_OFF
        reasons.append(f"هبوط {change_pct:.2f}% في الجلسة")
        if breadth is not None:
            reasons.append(f"اتساع {breadth:.0%} فقط")
        snap.reason_ar = " | ".join(reasons)
        return snap

    # 2) تذبذب عالٍ: يتجاوز تصنيف الاتجاه لأن إدارة المخاطر تتغيّر أولاً.
    if vol_ratio is not None and vol_ratio >= HIGH_VOL_THRESHOLD:
        snap.regime = HIGH_VOL
        snap.reason_ar = (
            f"التقلب {volatility:.1f}% أي {vol_ratio:.2f}x متوسطه المعتاد"
        )
        return snap

    # 3) الاتجاه: موقع السعر من متوسطاته، مدعوماً بالاتساع إن توفر.
    above_20 = ma20 is not None and close > ma20
    above_50 = ma50 is not None and close > ma50
    above_200 = ma200 is not None and close > ma200
    golden = ma50 is not None and ma200 is not None and ma50 > ma200

    up_votes = sum([above_20, above_50, above_200, golden])
    down_votes = sum([
        ma20 is not None and close < ma20,
        ma50 is not None and close < ma50,
        ma200 is not None and close < ma200,
        ma50 is not None and ma200 is not None and ma50 < ma200,
    ])

    if up_votes >= 3 and (breadth is None or breadth >= WEAK_BREADTH):
        snap.regime = TREND_UP
        reasons.append("السعر فوق متوسطاته")
        if golden:
            reasons.append("MA50 فوق MA200")
        if breadth is not None:
            reasons.append(f"اتساع {breadth:.0%}")
    elif down_votes >= 3 and (breadth is None or breadth <= STRONG_BREADTH):
        snap.regime = TREND_DOWN
        reasons.append("السعر تحت متوسطاته")
        if breadth is not None:
            reasons.append(f"اتساع {breadth:.0%}")
    else:
        snap.regime = RANGE
        reasons.append("لا اتجاه واضح من المتوسطات")
        if breadth is not None:
            reasons.append(f"اتساع {breadth:.0%}")

    snap.reason_ar = " | ".join(reasons)
    return snap


# ---------------------------------------------------------------------------
def compute_breadth(conn: sqlite3.Connection, day: str,
                    interval: str = "1day") -> Optional[float]:
    """احسب اتساع السوق ليوم معيّن: نسبة الأسهم الصاعدة إلى المتداولة.

    يُرجع None إذا لم توجد بيانات كافية، بدل أن يرجع رقماً مضللاً محسوباً
    من حفنة أسهم.
    """
    rows = conn.execute(
        """
        SELECT b.symbol, b.close, (
            SELECT p.close FROM bars p
             WHERE p.symbol = b.symbol AND p.interval = b.interval
               AND p.ts < b.ts
             ORDER BY p.ts DESC LIMIT 1
        ) AS prev_close
        FROM bars b
        WHERE b.interval = ? AND b.ts = ?
        """,
        (interval, day),
    ).fetchall()

    valid = [r for r in rows if r["prev_close"]]
    if len(valid) < 10:
        return None
    advancing = sum(1 for r in valid if r["close"] > r["prev_close"])
    return advancing / len(valid)


def save_regime(conn: sqlite3.Connection, snap: RegimeSnapshot,
                turnover: Optional[float] = None) -> None:
    """احفظ حالة السوق ليُربط بها كل توقع وإشارة في ذلك اليوم."""
    conn.execute(
        """
        INSERT INTO market_regimes
            (date, regime, tasi_close, tasi_change, breadth, volatility,
             turnover, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            regime = excluded.regime, tasi_close = excluded.tasi_close,
            tasi_change = excluded.tasi_change, breadth = excluded.breadth,
            volatility = excluded.volatility, turnover = excluded.turnover,
            notes = excluded.notes
        """,
        (snap.date, snap.regime, snap.close, snap.change_pct, snap.breadth,
         snap.volatility, turnover, snap.reason_ar),
    )
    conn.commit()


def get_regime(conn: sqlite3.Connection, day: str) -> Optional[str]:
    row = conn.execute(
        "SELECT regime FROM market_regimes WHERE date = ?", (day,)
    ).fetchone()
    return row["regime"] if row else None
